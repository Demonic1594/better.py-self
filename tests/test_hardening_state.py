"""
Robustness of the connection state, shutdown and background loops: failures
must be bounded, visible and must not leave state behind that hangs later calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from discord import Client
from discord.errors import ClientException
from discord.state import ChunkRequest, GuildSubscriptions


async def _state():
    client = Client()
    await client._async_setup_hook()
    client._handlers['ready'] = lambda: None
    return client, client._connection


# --- GuildSubscriptions ----------------------------------------------------------------------


class FailingWS:
    open = True

    def __init__(self):
        self.calls = 0

    async def bulk_guild_subscribe(self, payload):
        self.calls += 1
        raise RuntimeError('send failed')


def _members(n=8):
    return {'members': [str(10**17 + i) for i in range(n)]}


@pytest.mark.asyncio
async def test_failing_flush_never_recurses_without_end(monkeypatch):
    monkeypatch.setattr(GuildSubscriptions, 'MAX_PAYLOAD_SIZE', 200)
    ws = FailingWS()
    subs = GuildSubscriptions(SimpleNamespace(ws=ws, loop=asyncio.get_running_loop()))  # type: ignore[arg-type]
    subs._blocked = True  # no background ticks: isolate _checked_add

    await subs._checked_add({'1': _members()})  # type: ignore[dict-item]
    await subs._checked_add({'2': _members()})  # used to recurse ~1000 failing sends into RecursionError

    assert ws.calls <= 3, f'{ws.calls} send attempts for one change'
    assert '1' in subs._pending and '2' in subs._pending, 'nothing may be dropped'
    assert subs._flush_failures == ws.calls


@pytest.mark.asyncio
async def test_flush_failures_are_logged_and_reset_on_success(caplog):
    class FlakyWS:
        open = True
        fail = True

        async def bulk_guild_subscribe(self, payload):
            if self.fail:
                raise RuntimeError('transient')

    ws = FlakyWS()
    subs = GuildSubscriptions(SimpleNamespace(ws=ws, loop=asyncio.get_running_loop()))  # type: ignore[arg-type]
    subs._blocked = True
    subs._pending = {'1': {'typing': True}}  # type: ignore[typeddict-item]

    with caplog.at_level(logging.WARNING, logger='discord.state'):
        await subs._flush()
    assert subs._flush_failures == 1
    assert any('Bulk guild subscribe failed' in r.getMessage() for r in caplog.records), 'first failure is visible'
    assert subs._pending, 'the change is kept for the retry'

    ws.fail = False
    await subs._flush()
    assert subs._flush_failures == 0 and not subs._pending


@pytest.mark.asyncio
async def test_retry_delay_grows_with_failures_and_idle_ticks_do_not_rearm(monkeypatch):
    import discord.state as state_module

    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(state_module.asyncio, 'sleep', fake_sleep)
    subs = GuildSubscriptions(SimpleNamespace(ws=None, loop=asyncio.get_running_loop()))  # type: ignore[arg-type]

    await subs._tick_task()
    subs._flush_failures = 3
    await subs._tick_task()
    subs._flush_failures = 50
    await subs._tick_task()

    assert delays == [0.5, 4.0, 30.0], delays
    assert subs._task is None, 'with nothing queued there is nothing to retry for'


# --- chunk requests ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_guild_failed_send_does_not_leave_a_stale_request():
    client, state = await _state()
    guild = SimpleNamespace(id=7)
    sent = []

    async def broken_chunker(*args, **kwargs):
        raise RuntimeError('websocket closed')

    state.chunker = broken_chunker  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await state.chunk_guild(guild)  # type: ignore[arg-type]
    assert state._chunk_requests == {}, 'the unsent request must not stay registered'

    async def working_chunker(ids, **kwargs):
        sent.append(ids)

    state.chunker = working_chunker  # type: ignore[assignment]
    waiter = asyncio.ensure_future(state.chunk_guild(guild))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert sent == [[7]], 'a later chunk must send again instead of joining a dead request'
    state._chunk_requests[7].done()
    assert await asyncio.wait_for(waiter, 1) == []
    await client.close()


@pytest.mark.asyncio
async def test_joined_waiters_fail_with_the_request():
    client, state = await _state()
    guild = SimpleNamespace(id=9)
    gate = asyncio.Event()

    async def chunker(*args, **kwargs):
        await gate.wait()
        raise RuntimeError('websocket closed')

    state.chunker = chunker  # type: ignore[assignment]
    first = asyncio.ensure_future(state.chunk_guild(guild))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    joiner = asyncio.ensure_future(state.chunk_guild(guild))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    gate.set()

    with pytest.raises(RuntimeError):
        await first
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(joiner, 1)  # used to wait forever
    await client.close()


@pytest.mark.asyncio
async def test_clear_fails_pending_chunk_requests():
    client, state = await _state()
    request = ChunkRequest(3, asyncio.get_running_loop(), state._get_guild)
    state._chunk_requests[3] = request
    waiter = request.get_future()

    state.clear()

    with pytest.raises(ClientException):
        await asyncio.wait_for(waiter, 1)
    assert state._chunk_requests == {}
    await client.close()


# --- _delay_ready ------------------------------------------------------------------------------------


async def _ready_env(monkeypatch, guilds=4, timeout=0.3):
    client, state = await _state()
    state.gc_freeze = False
    state._subscribe_guilds = False
    state._guilds = {i: SimpleNamespace(id=i) for i in range(guilds)}  # type: ignore[assignment]
    state._guild_needs_chunking = lambda guild: True  # type: ignore[method-assign]

    async def can_chunk(guild):
        return True

    async def chunk_guild(guild, **kwargs):
        return asyncio.get_running_loop().create_future()  # never completes: a stalled guild

    async def chunker(*args, **kwargs):
        return None

    state._can_chunk_guild = can_chunk  # type: ignore[method-assign]
    state.chunk_guild = chunk_guild  # type: ignore[method-assign]
    state.chunker = chunker  # type: ignore[assignment]
    state._chunk_timeout = lambda guild: timeout  # type: ignore[method-assign]
    return client, state


@pytest.mark.asyncio
async def test_stalled_guilds_share_one_deadline(monkeypatch):
    client, state = await _ready_env(monkeypatch, guilds=4, timeout=0.3)

    start = time.perf_counter()
    await state._delay_ready()
    elapsed = time.perf_counter() - start

    # One shared deadline of ~0.3s; the old per-guild waits summed to ~1.2s
    assert elapsed < 0.8, f'{elapsed:.2f}s for 4 stalled guilds'
    await client.close()


@pytest.mark.asyncio
async def test_ready_sequence_failure_is_logged_not_swallowed(monkeypatch, caplog):
    client, state = await _ready_env(monkeypatch, guilds=1)

    async def broken_chunker(*args, **kwargs):
        raise RuntimeError('gateway closed mid-ready')

    state.chunker = broken_chunker  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING, logger='discord.state'):
        await state._delay_ready()  # must not raise out of the (never awaited) ready task
    assert any('ready sequence failed' in r.getMessage() for r in caplog.records)
    assert state._ready_task is None
    assert not state.subscriptions.blocked
    await client.close()


# --- Client.close ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_survives_a_caller_that_gives_up():
    client, state = await _state()
    release = asyncio.Event()
    closed = []

    async def slow_http_close():
        await release.wait()
        closed.append(True)

    client.http.close = slow_http_close  # type: ignore[method-assign]

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.close(), 0.05)  # the common "close with a timeout" pattern

    release.set()
    await asyncio.wait_for(client.close(), 1)  # used to raise CancelledError at once
    assert closed == [True], 'the shutdown must still have finished'
    assert client.is_closed()


@pytest.mark.asyncio
async def test_close_continues_after_a_gateway_close_error():
    client, state = await _state()
    closed = []

    class BadWS:
        open = True

        async def close(self, code=None):
            raise RuntimeError('socket already gone')

    async def http_close():
        closed.append(True)

    client.ws = BadWS()  # type: ignore[assignment]
    client.http.close = http_close  # type: ignore[method-assign]
    await client.close()

    assert closed == [True], 'a failing ws.close() used to skip http.close()'


@pytest.mark.asyncio
async def test_close_cancels_the_ready_sequence():
    client, state = await _state()
    state._ready_task = asyncio.ensure_future(asyncio.sleep(30))
    task = state._ready_task
    await client.close()
    await asyncio.sleep(0)
    assert task.cancelled(), 'a ready sequence must not dispatch `ready` on a closed client'
    assert state.subscriptions.blocked


# --- ext.tasks ------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_loop_failure_reaches_the_error_handler():
    from discord.ext import tasks

    errors = []

    @tasks.loop(seconds=0.01)
    async def ticker():
        pass

    @ticker.before_loop
    async def before():
        raise RuntimeError('before_loop failed')

    @ticker.error
    async def on_error(exc):
        errors.append(exc)

    ticker.start()
    task = ticker.get_task()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(task, 1)

    assert [str(e) for e in errors] == ['before_loop failed'], 'it used to vanish until the task was garbage collected'
    assert ticker.failed()
