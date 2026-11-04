"""Regression tests for audit round 6: HTTP, voice and ext edge cases, logging gaps."""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
import re
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import discord
import discord.client as client_mod
import discord.gateway as gateway_mod
from discord.components import _component_factory
from discord.ext.commands.cooldowns import Cooldown
from discord.http import Ratelimit, _webhook_retry_after
from discord.voice_state import ConnectionFlowState, VoiceConnectionState
from test_rate_limit_behavior import make_http

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_ratelimit_headers_without_reset_do_not_raise():
    """A2-04: X-Ratelimit-Remaining without any reset header raised KeyError."""
    ratelimit = Ratelimit(None, 1)
    ratelimit.update(SimpleNamespace(headers={'X-Ratelimit-Remaining': '1'}))  # type: ignore[arg-type]
    assert ratelimit.reset_after == 1.0
    ratelimit.update(SimpleNamespace(headers={'X-Ratelimit-Remaining': '1', 'X-Ratelimit-Reset': 'garbage'}))  # type: ignore[arg-type]
    assert ratelimit.reset_after == 1.0
    ratelimit.update(SimpleNamespace(headers={'X-Ratelimit-Remaining': '1', 'X-Ratelimit-Reset-After': 'nan'}))  # type: ignore[arg-type]
    assert ratelimit.reset_after == 1.0


def test_webhook_retry_after_parsing():
    """A2-05: webhooks read data['retry_after'] raw: a missing key raised KeyError, a string raised TypeError."""
    assert _webhook_retry_after({}, {}) == 1.0
    assert _webhook_retry_after({'retry_after': '0.5'}, {}) == 0.5
    assert _webhook_retry_after('<html>slow down</html>', {'Retry-After': '2'}) == 2.0
    assert _webhook_retry_after({'retry_after': float('nan')}, {}) == 1.0
    assert _webhook_retry_after({'retry_after': -3}, {}) == 1.0


class _FakeCDNResponse:
    status = 503
    reason = 'Service Unavailable'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_cdn_server_errors_are_discord_server_errors():
    """A2-09: a CDN 5xx raised a bare HTTPException; it is now the DiscordServerError subclass."""
    http = make_http([])

    async def session():
        return SimpleNamespace(get=lambda url, **kwargs: _FakeCDNResponse())

    http._ensure_asession = session  # type: ignore[method-assign]
    with pytest.raises(discord.DiscordServerError):
        await http.get_from_cdn('https://cdn.discordapp.com/x.png')


def test_gateway_ratelimiter_ignores_wall_clock_steps(monkeypatch):
    """A1-06: GatewayRatelimiter used time.time, so a 1 h backward clock step meant a 1 h stall."""
    limiter = gateway_mod.GatewayRatelimiter(count=1, per=60.0)
    assert limiter.get_delay() == 0.0
    real_time = time.time
    monkeypatch.setattr(gateway_mod.time, 'time', lambda: real_time() - 3600)
    assert limiter.get_delay() <= 60.0


@pytest.mark.asyncio
async def test_repeated_voice_hello_stops_the_old_keepalive(monkeypatch):
    """A1-04: a second voice HELLO started a new heartbeat thread and left the old one running."""
    events = []

    class FakeKeepAlive:
        def __init__(self, *, ws, interval):
            self.name = f'k{len(events)}'

        def start(self):
            events.append(('start', self.name))

        def stop(self):
            events.append(('stop', self.name))

    monkeypatch.setattr(gateway_mod, 'VoiceKeepAliveHandler', FakeKeepAlive)
    ws = object.__new__(gateway_mod.DiscordVoiceWebSocket)
    ws._keep_alive = None
    ws.seq_ack = 0
    hello = {'op': ws.HELLO, 'd': {'heartbeat_interval': 1000}}
    await ws.received_message(hello)
    first = ws._keep_alive
    await ws.received_message(hello)
    assert ('stop', first.name) in events
    assert events[-1] == ('start', ws._keep_alive.name)


@pytest.mark.asyncio
async def test_cancelled_voice_disconnect_still_raises_after_cleanup():
    """C3-08: disconnect(wait=True) swallowed its own cancellation."""
    state = object.__new__(VoiceConnectionState)
    state.voice_client = MagicMock()
    state.ws = None
    state.socket = None
    state.timeout = 30.0
    state._connected = asyncio.Event()
    state._disconnected = asyncio.Event()
    state._state_event = asyncio.Event()
    state._state = ConnectionFlowState.connected
    state.is_connected = lambda: True  # type: ignore[method-assign]

    async def voice_disconnect():
        return None

    state._voice_disconnect = voice_disconnect  # type: ignore[method-assign]
    state._pause_socket_reader = lambda: None  # type: ignore[method-assign]
    state._stop_socket_reader = lambda: None  # type: ignore[method-assign]
    state._inside_runner = lambda: False  # type: ignore[method-assign]

    task = asyncio.ensure_future(state.disconnect(wait=True))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state.voice_client.cleanup.assert_called_once()


def test_cooldown_retry_after_is_never_negative():
    """A6-03: a zero-rate cooldown returned a negative retry_after once its window passed."""
    cooldown = Cooldown(0, 10)
    cooldown.update_rate_limit(1000.0)
    assert cooldown.get_retry_after(1015.0) == 0.0


@pytest.mark.asyncio
async def test_exception_in_on_error_is_logged(caplog):
    """A1-10: an exception raised by on_error escaped into the task and was only reported at GC."""
    client = discord.Client()

    async def failing_on_error(event, *args, **kwargs):
        raise ValueError('on_error failed')

    async def handler():
        raise RuntimeError('handler failed')

    client.on_error = failing_on_error  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger='discord.client'):
        await client._run_event(handler, 'on_test')  # must not raise
    records = [r for r in caplog.records if 'on_error' in r.getMessage()]
    assert records and records[0].exc_info is not None


@pytest.mark.asyncio
async def test_unknown_client_options_are_logged(caplog):
    """B2-05: a misspelled option (max_mesages=5) was silently ignored."""
    with caplog.at_level(logging.WARNING, logger='discord.client'):
        discord.Client(max_mesages=5)  # type: ignore[call-arg]
        discord.ext.commands.Bot(command_prefix='!', case_insensitive=True, self_bot=True)
    warnings = [r.getMessage() for r in caplog.records if 'unknown Client option' in r.getMessage()]
    assert warnings == ['Ignoring unknown Client option(s): max_mesages. Check them for typos.']


@pytest.mark.asyncio
async def test_apm_tracing_option_enables_tracing():
    """The documented apm_tracing option was never read (only an undocumented 'trace' key was)."""
    client = discord.Client(apm_tracing=True)
    assert 'trace' in client.http.debug_options


def test_known_options_match_the_typed_options_and_the_code():
    """_KNOWN_CLIENT_OPTIONS must list every option the code reads and the TypedDicts declare,
    or the unknown-option warning would fire on valid options."""
    declared = set()
    for path, cls in (('discord/client.py', '_ClientOptions'), ('discord/ext/commands/bot.py', '_BotOptions')):
        for node in ast.walk(ast.parse((ROOT / path).read_text())):
            if isinstance(node, ast.ClassDef) and node.name == cls:
                declared |= {a.target.id for a in node.body if isinstance(a, ast.AnnAssign)}  # type: ignore[union-attr]
    read = set()
    for path in ('discord/client.py', 'discord/state.py', 'discord/ext/commands/bot.py'):
        read |= set(re.findall(r"options\.(?:get|pop)\(\s*'([a-z_]+)'", (ROOT / path).read_text()))
    known = client_mod._KNOWN_CLIENT_OPTIONS
    assert declared <= known, declared - known
    assert read <= known, read - known
    assert known <= declared | read, known - (declared | read)


def test_unknown_component_type_is_logged(caplog):
    """A5-06: an unknown component type was dropped without any log."""
    with caplog.at_level(logging.DEBUG, logger='discord.components'):
        assert _component_factory({'type': 999}) is None  # type: ignore[typeddict-item]
    assert any('unknown type 999' in r.getMessage() for r in caplog.records)


def test_flag_classes_have_slots():
    """A8-02: six flag classes lacked __slots__, so every instance carried a __dict__."""
    for flags in (discord.ActivityFlags(), discord.AttachmentFlags(), discord.EmbedFlags(), discord.RoleFlags()):
        assert not hasattr(flags, '__dict__'), type(flags).__name__
    assert '__slots__' in vars(discord.flags.ArrayFlags) and '__slots__' in vars(discord.flags.ZeroBasedArrayFlags)


def test_bulk_ban_result_is_exported():
    """B3-03: BulkBanResult was documented under discord but not exported."""
    assert discord.BulkBanResult is discord.guild.BulkBanResult
