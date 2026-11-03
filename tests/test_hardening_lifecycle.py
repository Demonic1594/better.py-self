"""
Lifecycle, extension rollback, audio-helper and message-handling robustness.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import textwrap
import threading
from types import SimpleNamespace

import pytest

import discord
from discord import Client, Guild, Message
from discord.errors import ClientException, LoginFailure
from discord.ext import commands

from traffic_harness import make_http


# --- extension rollback -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_reload_with_failing_rollback_leaves_nothing_registered(tmp_path, monkeypatch):
    (tmp_path / 'rb_ext.py').write_text(
        textwrap.dedent(
            """
            import sys
            from discord.ext import commands

            class A(commands.Cog):
                pass

            class B(commands.Cog):
                async def cog_load(self):
                    if sys.modules['rb_flag'].fail:
                        raise RuntimeError('service down')

            async def setup(bot):
                await bot.add_cog(A())
                await bot.add_cog(B())
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    flag = SimpleNamespace(fail=False)
    monkeypatch.setitem(sys.modules, 'rb_flag', flag)

    bot = commands.Bot(command_prefix='!', self_bot=True)
    await bot.load_extension('rb_ext')
    assert 'A' in bot.cogs and 'B' in bot.cogs

    flag.fail = True  # the new load fails, and so does restoring the old version
    with pytest.raises(commands.ExtensionFailed):  # used to be the raw RuntimeError from the rollback
        await bot.reload_extension('rb_ext')

    assert 'A' not in bot.cogs, 'cog A, registered before B failed, must not stay behind'
    assert 'rb_ext' not in bot.extensions

    flag.fail = False  # the extension can be loaded again (used to fail: "Cog named 'A' already loaded")
    await bot.load_extension('rb_ext')
    assert 'A' in bot.cogs and 'B' in bot.cogs
    await bot.close()


# --- Client lifecycle -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_on_a_closed_client_warns_instead_of_spinning(caplog):
    client = Client()
    await client._async_setup_hook()
    client._closing_task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]

    async def login(token):
        raise AssertionError('must not log in on a closed client')

    client.login = login  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger='discord.client'):
        await client.start('token')
    assert any('closed Client' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_token_fails_before_any_request():
    client = Client()
    await client._async_setup_hook()

    async def static_login(token):
        raise AssertionError('no request for an empty token')

    client.http.static_login = static_login  # type: ignore[method-assign]
    with pytest.raises(LoginFailure):
        await client.login('   \n')


@pytest.mark.asyncio
async def test_change_presence_before_connect_is_a_clear_error():
    client = Client()
    await client._async_setup_hook()
    with pytest.raises(ClientException, match='connected gateway'):
        await client.change_presence(status=discord.Status.idle)  # used to be AttributeError


def test_clear_before_login_does_not_fail():
    Client().clear()  # `_ready` is still the MISSING sentinel here


@pytest.mark.asyncio
async def test_aexit_does_not_cancel_a_shutdown_in_progress():
    client = Client()
    await client._async_setup_hook()
    release = asyncio.Event()

    async def slow_close():
        await release.wait()

    task = asyncio.ensure_future(slow_close())
    client._closing_task = task

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.__aexit__(None, None, None), 0.05)
    assert not task.cancelled(), 'the shutdown a cancelled runner waits for must keep going'
    release.set()
    await task


def test_setup_logging_replaces_its_previous_handler():
    logger = logging.getLogger()  # setup_logging defaults to the root logger
    before, level = list(logger.handlers), logger.level
    try:
        for _ in range(3):
            discord.utils.setup_logging()
        ours = [h for h in logger.handlers if getattr(h, '_discord_setup_logging', False)]
        assert len(ours) == 1, 'every run() used to add another handler and double each log line'
    finally:
        logger.handlers[:] = before
        logger.setLevel(level)


def test_parse_time_accepts_a_trailing_z():
    from discord.utils import parse_time

    parsed = parse_time('2024-05-01T12:30:00Z')
    assert parsed is not None and parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert parse_time('2024-05-01T12:30:00.123000+00:00') is not None
    with pytest.raises(ValueError):
        parse_time('not a time')


# --- HTTPClient startup/login ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_first_requests_start_one_session(monkeypatch):
    import discord.http as http_module

    http = make_http(None)
    http._started = False
    gate = asyncio.Event()
    calls = []
    sessions = []

    async def headers_context():
        calls.append(1)
        await gate.wait()
        return SimpleNamespace(
            user_agent='ua', super_properties={}, impersonate='chrome', client_hints={}, encoded_super_properties='x'
        )

    class FakeSession:
        def __init__(self, **kwargs):
            sessions.append(self)

    http.client = SimpleNamespace(headers_context=headers_context)  # type: ignore[assignment]
    monkeypatch.setattr(http_module.requests, 'AsyncSession', FakeSession)

    first = asyncio.ensure_future(http.startup())
    second = asyncio.ensure_future(http.startup())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)

    assert len(calls) == 1 and len(sessions) == 1, 'a second session replaced the first without closing it'


@pytest.mark.asyncio
async def test_clear_after_close_allows_a_new_startup():
    http = make_http(None)
    http._started = True
    http._HTTPClient__session = SimpleNamespace(_closed=True)  # type: ignore[attr-defined]
    http.clear()
    assert http._started is False, 'otherwise the next request skipped startup() and hit the MISSING session'


@pytest.mark.asyncio
async def test_static_login_restores_the_token_after_a_network_error():
    http = make_http(None, token='old-token')

    async def get_me():
        raise OSError('network down')

    http.get_me = get_me  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await http.static_login('unverified-token')
    assert http.token == 'old-token', 'an unverified token must not stay set'


# --- player helpers ---------------------------------------------------------------------------------------


def _audio():
    from discord.player import FFmpegAudio

    audio = FFmpegAudio.__new__(FFmpegAudio)
    audio._process = SimpleNamespace(  # type: ignore[assignment]
        terminate=lambda: None, kill=lambda: None, poll=lambda: 0, pid=1, returncode=0
    )
    return audio


def test_pipe_writer_closes_stdin_when_the_source_raises():
    audio = _audio()
    closed = []
    audio._stdin = SimpleNamespace(close=lambda: closed.append(True), write=lambda data: None)  # type: ignore[assignment]

    class BrokenSource:
        def read(self, n):
            raise RuntimeError('network source died')

    audio._pipe_writer(BrokenSource())  # type: ignore[arg-type]  # used to raise, leaving ffmpeg waiting on stdin
    assert closed == [True], 'ffmpeg must see EOF so playback ends instead of hanging'


def test_stderr_reader_stops_at_eof():
    audio = _audio()
    audio._stderr = SimpleNamespace(read=lambda n: b'', close=lambda: None)  # type: ignore[assignment]
    sink = SimpleNamespace(write=lambda data: None)

    worker = threading.Thread(target=audio._pipe_reader, args=(sink,), daemon=True)
    worker.start()
    worker.join(2)
    assert not worker.is_alive(), 'the reader used to spin at 100% CPU once the pipe closed'


def test_probe_fallback_is_bounded_and_reaps_its_child(monkeypatch):
    import discord.player as player_module

    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout=b'Stream #0:0: Audio: opus, 48000 Hz\n  bitrate: 64 kb/s')

    monkeypatch.setattr(player_module.subprocess, 'run', fake_run)
    codec, bitrate = player_module.FFmpegOpusAudio._probe_codec_fallback('input.ogg')

    assert seen['timeout'] == 20, 'run() kills and reaps the child when the timeout expires'
    assert codec == 'opus' and bitrate == 512


# --- message handling ------------------------------------------------------------------------------------------


async def _guild_env():
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(
        state=state, data={'id': '5', 'username': 'me', 'discriminator': '0', 'global_name': None, 'avatar': None}
    )
    guild = Guild(
        state=state,
        data={
            'id': '1',
            'name': 'g',
            'roles': [{'id': '1', 'name': '@everyone'}],
            'channels': [{'id': '11', 'type': 0, 'name': 'c', 'position': 0}],
            'member_count': 1,
        },
    )
    state._guilds[1] = guild
    return client, state, guild


def _message_data(**extra):
    data = {
        'id': '100',
        'channel_id': '11',
        'guild_id': '1',
        'type': 0,
        'content': 'hi',
        'author': {'id': '7', 'username': 'u', 'discriminator': '0', 'global_name': None, 'avatar': None},
        'attachments': [],
        'embeds': [],
        'mentions': [],
        'mention_roles': [],
        'pinned': False,
        'mention_everyone': False,
        'tts': False,
        'timestamp': '2024-01-01T00:00:00+00:00',
        'edited_timestamp': None,
        'flags': 0,
        'components': [],
    }
    data.update(extra)
    return data


@pytest.mark.asyncio
async def test_message_guild_id_is_set_for_guild_channels():
    client, state, guild = await _guild_env()
    message = Message(state=state, channel=guild.get_channel(11), data=_message_data())  # type: ignore[arg-type]
    assert message.guild_id == 1, 'a documented attribute that used to raise AttributeError'

    partial = discord.PartialMessageable(state=state, id=11, guild_id=99)  # guild 99 is not cached
    assert Message(state=state, channel=partial, data=_message_data()).guild_id == 99  # type: ignore[arg-type]
    dm = discord.PartialMessageable(state=state, id=12)
    assert Message(state=state, channel=dm, data=_message_data(guild_id=None)).guild_id is None  # type: ignore[arg-type]
    await client.close()


@pytest.mark.asyncio
async def test_partial_message_update_still_dispatches_edit_and_updates_the_cache():
    client, state, guild = await _guild_env()
    cached = Message(state=state, channel=guild.get_channel(11), data=_message_data())  # type: ignore[arg-type]
    state._add_message_to_cache(cached)
    seen = []

    async def on_message_edit(before, after):
        seen.append((before, after))

    client.on_message_edit = on_message_edit  # type: ignore[attr-defined]
    # An embed-only update: no `type`, no `content`
    state.parse_message_update(
        {'id': '100', 'channel_id': '11', 'guild_id': '1', 'embeds': [{'type': 'link', 'url': 'https://e.com'}]}  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)

    assert len(seen) == 1, 'the edit event used to be dropped by a KeyError in Message()'
    before, after = seen[0]
    assert len(before.embeds) == 0 and len(after.embeds) == 1
    assert len(cached.embeds) == 1, 'and the cache used to keep the stale embeds'
    await client.close()


@pytest.mark.asyncio
async def test_thread_constructor_tolerates_missing_counts():
    client, state, guild = await _guild_env()
    payload = {
        'id': '500',
        'parent_id': '11',
        'owner_id': '7',
        'name': 't',
        'type': 11,
        'thread_metadata': {
            'archived': False,
            'auto_archive_duration': 60,
            'archive_timestamp': '2024-01-01T00:00:00+00:00',
            'locked': False,
        },
    }
    thread = discord.Thread(guild=guild, state=state, data=payload)  # type: ignore[arg-type]
    assert thread.message_count == 0 and thread.member_count == 0
    await client.close()
