"""Regression tests for issues reported upstream (dolfies/discord.py-self)."""

from __future__ import annotations

import asyncio
import logging

import pytest

import discord
from discord import Client

from test_persistence import drive_ready, ready_payload, up


def _small_guild(gid=1, member_count=173):
    return {
        'id': str(gid),
        'name': f'G{gid}',
        'member_count': member_count,
        'roles': [{'id': str(gid), 'name': '@everyone', 'permissions': '1024'}],
        'channels': [{'id': str(500 + gid), 'name': 'general', 'type': 0, 'position': 0}],
        'members': [{'user': up(5), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}],
        'presences': [],
        'voice_states': [],
        'threads': [],
        'emojis': [],
        'stickers': [],
        'features': [],
        'unavailable': False,
    }


class _SilentGateway:
    """A gateway that never sends the member list SYNC the scraper waits for."""

    def wait_for(self, event, predicate):
        return asyncio.get_running_loop().create_future()


@pytest.mark.asyncio
async def test_member_scrape_without_sync_returns_and_warns(caplog, monkeypatch):
    """dolfies/discord.py-self#907: Discord answered the member list subscription
    with INVALIDATE/DELETE/INSERT ops but never a SYNC. fetch_members must return
    (not hang) and say why the result is empty instead of failing silently."""
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(state=state, data=up(5))
    ready = ready_payload()
    ready['guilds'] = [_small_guild()]
    await drive_ready(client, ready)
    guild = client.get_guild(1)
    assert guild is not None and guild.me is not None

    async def _subscribe(self, *args, **kwargs):
        return None

    monkeypatch.setattr(state, '_chunk_timeout', lambda guild: 0.05)
    monkeypatch.setattr(type(state.subscriptions), 'subscribe_to_channels', _subscribe)
    client.ws = _SilentGateway()  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger='discord.state'):
        members = await asyncio.wait_for(guild.fetch_members(), timeout=5)
    assert members == []
    assert any('no member list' in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]


class _FakeResponse:
    ok = True
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RecordingSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(self.payload)


_PROPERTIES = {
    'properties': {'os': 'Mac OS X', 'browser': 'Chrome', 'browser_version': '150.0.0.0', 'client_build_number': 1},
    'encoded': 'x',
    'extra_gateway_properties': {},
}


@pytest.mark.asyncio
async def test_info_api_request_sends_its_filters():
    """dolfies/discord.py-self#939: release_channel/browser/platform were built
    into a params dict that was never sent, so they were silently ignored."""
    session = _RecordingSession(_PROPERTIES)
    await discord.HeadersContext.fetch_api_properties(session, 'web', 'canary', 'firefox', 'linux')  # type: ignore
    _, kwargs = session.calls[0]
    assert kwargs.get('params') == {'release_channel': 'canary', 'browser': 'firefox', 'platform': 'linux'}


@pytest.mark.asyncio
async def test_default_headers_follow_the_returned_properties():
    """The platform and browser come from the info API instead of being
    hardcoded to Windows/Chrome while the super properties say otherwise."""
    session = _RecordingSession(_PROPERTIES)
    ctx = await discord.HeadersContext.default(session)  # type: ignore
    assert ctx.platform == 'Mac OS X'
    assert ctx.browser_type == 'chrome'
    assert ctx.super_properties['os'] == 'Mac OS X'
