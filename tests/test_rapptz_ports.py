"""Regression tests for changes ported from Rapptz/discord.py (round 1)."""

from __future__ import annotations

import asyncio
import datetime
import warnings
from types import SimpleNamespace

import pytest
import yarl
from curl_cffi import CurlError

import discord
from discord.channel import StageChannel, TextChannel, VoiceChannel
from discord.ext import commands
from discord.gateway import DiscordWebSocket
from discord.poll import Poll
from test_abuse import make_env
from test_audit_round3 import _ready_client
from test_persistence import up

RESUME_URL = yarl.URL('wss://gateway-us-east1-b.discord.gg')


class _FakeSocket:
    closed = False
    close_code = None
    close_reason = None

    def __init__(self):
        self.close_calls = []

    async def recv(self):
        await asyncio.sleep(3600)

    async def close(self, code=1000, reason=b''):
        self.close_calls.append(code)
        self.closed = True


async def _connect_with(monkeypatch, refuse_status):
    """Runs DiscordWebSocket.from_client for a RESUME on RESUME_URL, where the
    resume URL refuses the websocket upgrade with ``refuse_status``."""
    client = await _ready_client()
    urls, calls = [], []

    async def ws_connect(url, **kwargs):
        urls.append(url)
        if url.startswith(str(RESUME_URL)):
            raise CurlError(f'Failed to perform, curl: (22) Refused WebSocket upgrade: {refuse_status}.', 22)
        return _FakeSocket()

    async def identify(self):
        calls.append('identify')

    async def resume(self):
        calls.append('resume')

    monkeypatch.setattr(client.http, 'ws_connect', ws_connect)
    monkeypatch.setattr(DiscordWebSocket, 'identify', identify)
    monkeypatch.setattr(DiscordWebSocket, 'resume', resume)
    ws = await DiscordWebSocket.from_client(client, gateway=RESUME_URL, session='abc', sequence=5, resume=True)
    return ws, urls, calls


@pytest.mark.asyncio
async def test_resume_url_server_error_falls_back_to_default_gateway(monkeypatch):
    """65232c3: a resume URL answering the upgrade with a 5xx failed every RESUME
    attempt; it now identifies on the default gateway, keeping the query string."""
    ws, urls, calls = await _connect_with(monkeypatch, 503)
    assert len(urls) == 2
    assert urls[1].startswith(str(DiscordWebSocket.DEFAULT_GATEWAY))
    assert 'v=' in urls[1] and 'encoding=json' in urls[1] and 'compress=' in urls[1]
    assert calls == ['identify']
    assert ws.gateway == DiscordWebSocket.DEFAULT_GATEWAY
    assert ws.session_id is None and ws.sequence is None


@pytest.mark.asyncio
async def test_resume_url_client_error_still_raises(monkeypatch):
    """Only server errors fall back; a 4xx is a real failure and is raised."""
    with pytest.raises(CurlError):
        await _connect_with(monkeypatch, 404)


@pytest.mark.asyncio
async def test_poll_timeout_closes_the_silent_socket():
    """1985498: a receive timeout raised ReconnectWebSocket without closing the
    socket, so every such reconnect left one connection open."""
    ws = object.__new__(DiscordWebSocket)
    ws.socket = _FakeSocket()  # type: ignore[assignment]
    ws._max_heartbeat_timeout = 0.05
    ws._keep_alive = None
    with pytest.raises(discord.gateway.ReconnectWebSocket):
        await ws.poll_event()
    assert ws.socket.close_calls == [4000]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_duplicate_member_add_is_ignored():
    """22ade98: a repeated GUILD_MEMBER_ADD for a cached member counted them twice
    and dispatched on_member_join again."""
    client, state, guild, channel = await make_env()
    joins = []
    state.dispatch = lambda event, *args: joins.append(event) if event == 'member_join' else None  # type: ignore[method-assign]
    guild._member_count = 1
    payload = {'guild_id': str(guild.id), 'user': up(300), 'roles': [], 'joined_at': '2024-01-01T00:00:00+00:00'}
    state.parse_guild_member_add(dict(payload))  # type: ignore[arg-type]
    state.parse_guild_member_add(dict(payload))  # type: ignore[arg-type]
    assert joins == ['member_join']
    assert guild._member_count == 2


async def _clone_payload(channel):
    captured = {}

    async def create_channel(guild_id, channel_type, *, reason=None, **options):
        captured.update(options)
        return {**options, 'id': '999', 'type': channel_type, 'position': 0, 'guild_id': str(guild_id)}

    channel._state.http.create_channel = create_channel
    return captured


@pytest.mark.asyncio
async def test_clone_announcement_channel_omits_slowmode():
    """354ae42: cloning an announcement channel sent rate_limit_per_user, which they do not have."""
    client, state, guild, _ = await make_env()
    news = TextChannel(state=state, guild=guild, data={'id': '600', 'name': 'news', 'type': 5, 'position': 1})  # type: ignore[arg-type]
    captured = await _clone_payload(news)
    await news.clone()
    assert 'rate_limit_per_user' not in captured


@pytest.mark.asyncio
async def test_voice_and_stage_clone_copy_vocal_settings_and_category():
    """354ae42: VoiceChannel and StageChannel clones dropped slowmode, nsfw and region,
    and VocalGuildChannel.clone ignored category."""
    client, state, guild, _ = await make_env()
    base = {
        'type': 2,
        'position': 0,
        'bitrate': 64000,
        'user_limit': 5,
        'rate_limit_per_user': 10,
        'nsfw': True,
        'rtc_region': 'rotterdam',
    }
    voice = VoiceChannel(state=state, guild=guild, data={'id': '601', 'name': 'v', **base})  # type: ignore[arg-type]
    category = SimpleNamespace(id=777)
    captured = await _clone_payload(voice)
    await voice.clone(category=category)  # type: ignore[arg-type]
    assert captured['rate_limit_per_user'] == 10 and captured['nsfw'] is True and captured['rtc_region'] == 'rotterdam'
    assert captured['parent_id'] == 777
    stage = StageChannel(state=state, guild=guild, data={'id': '602', 'name': 's', **base, 'type': 13})  # type: ignore[arg-type]
    captured = await _clone_payload(stage)
    await stage.clone()
    assert captured['bitrate'] == 64000 and captured['user_limit'] == 5


def test_activity_repr_includes_platform():
    """826aa22: Game and Streaming reprs omitted the platform."""
    activity = discord.Activity(type=discord.ActivityType.playing, name='x', platform='xbox')
    assert 'platform=' in repr(activity)


def test_deprecation_warning_points_at_the_caller():
    """aeab0d4: deprecation warnings pointed one frame too far up (<sys>:0)."""
    activity = discord.Activity(type=discord.ActivityType.playing, name='x', timestamps={'start': 1000})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        activity.start
    assert caught and caught[0].filename == __file__


@pytest.mark.asyncio
async def test_poll_duration_is_rounded_to_hours():
    """38bbfed: expiry minus the message timestamp is microseconds off the stored duration."""
    created = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    message = SimpleNamespace(created_at=created, id=1, channel=None, _state=None)
    data = {
        'question': {'text': 'q'},
        'answers': [],
        'expiry': (created + datetime.timedelta(hours=24, microseconds=4321)).isoformat(),
        'allow_multiselect': False,
        'layout_type': 1,
    }
    poll = Poll._from_data(data=data, message=message, state=None)  # type: ignore[arg-type]
    assert poll.duration == datetime.timedelta(hours=24)


def test_paginator_pages_does_not_close_the_current_page():
    """8f3224b: reading Paginator.pages closed the current page, so lines added
    afterwards went to a new page."""
    paginator = commands.Paginator()
    paginator.add_line('first')
    assert paginator.pages == ['```\nfirst\n```']
    paginator.add_line('second')
    assert paginator.pages == ['```\nfirst\nsecond\n```']
