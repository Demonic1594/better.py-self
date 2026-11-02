"""
Regressions for the fork-owned internals refactor: guild subscription
batching must never drop a payload, gateway waiters must be cleaned up,
and rate-limit bucket sweeps must stay amortized.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from discord.state import GuildSubscriptions


class SlowWS:
    """bulk_guild_subscribe blocks until released, like a rate-limited send."""

    open = True

    def __init__(self) -> None:
        self.sent: list = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def bulk_guild_subscribe(self, payload) -> None:
        self.started.set()
        await self.release.wait()
        self.sent.append(payload)


def make_subscriptions(ws, monkeypatch) -> GuildSubscriptions:
    monkeypatch.setattr(GuildSubscriptions, 'TICK_TIMEOUT', 0.01)
    state = SimpleNamespace(ws=ws, loop=asyncio.get_running_loop())
    return GuildSubscriptions(state)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_subscribe_during_inflight_flush_is_not_dropped(monkeypatch):
    ws = SlowWS()
    subs = make_subscriptions(ws, monkeypatch)

    await subs.subscribe_to(SimpleNamespace(id=1), typing=True)  # type: ignore[arg-type]
    await asyncio.wait_for(ws.started.wait(), 1)  # first flush is now blocked mid-send

    # A subscribe arriving mid-send used to cancel the flushing task, losing guild 1
    await subs.subscribe_to(SimpleNamespace(id=2), typing=True)  # type: ignore[arg-type]
    ws.release.set()

    for _ in range(100):
        if len(ws.sent) == 2:
            break
        await asyncio.sleep(0.01)

    assert ws.sent == [{'1': {'typing': True}}, {'2': {'typing': True}}]
    assert 1 in subs._subscribed and 2 in subs._subscribed, 'local bookkeeping must reflect both sends'


@pytest.mark.asyncio
async def test_overflow_flush_merges_changes_queued_during_the_flush(monkeypatch):
    from discord import utils

    ws = SlowWS()
    ws.release.set()
    subs = make_subscriptions(ws, monkeypatch)
    changes = {'2': {'typing': True, 'threads': True}}
    queued_meanwhile = {'3': {'typing': True}}
    # Big enough for the merged result, too small for the original batch + changes
    monkeypatch.setattr(GuildSubscriptions, 'MAX_PAYLOAD_SIZE', len(utils._to_json({**queued_meanwhile, **changes})))
    subs._pending = {'1': {'typing': True, 'threads': True, 'activities': True}}  # type: ignore[typeddict-item]

    real_flush = GuildSubscriptions._flush
    fired = []

    async def flush_with_concurrent_add(self):
        await real_flush(self)
        if not fired:  # another subscriber queues something once, during the await
            fired.append(True)
            self._pending = dict(queued_meanwhile)

    monkeypatch.setattr(GuildSubscriptions, '_flush', flush_with_concurrent_add)
    await subs._checked_add(changes)  # type: ignore[arg-type]

    assert ws.sent == [{'1': {'typing': True, 'threads': True, 'activities': True}}]
    assert subs._pending == {**queued_meanwhile, **changes}, 'neither batch may be overwritten'
    subs._cancel()


@pytest.mark.asyncio
async def test_tick_before_gateway_connects_retries_instead_of_crashing(monkeypatch):
    subs = make_subscriptions(None, monkeypatch)
    subs._pending = {'1': {'typing': True}}  # type: ignore[typeddict-item]
    subs._tick()
    first = subs._task
    await asyncio.sleep(0.05)
    assert first is not None and first.done() and first.exception() is None
    assert subs._task is not None and not subs._task.done(), 'it keeps re-ticking until the ws is up'
    subs._cancel()


def _gateway_ws():
    from discord import gateway

    ws = gateway.DiscordWebSocket.__new__(gateway.DiscordWebSocket)
    ws.loop = asyncio.get_running_loop()
    ws._dispatch_listeners = []
    return ws


@pytest.mark.asyncio
async def test_gateway_wait_for_timeout_removes_listener():
    ws = _gateway_ws()
    future = ws.wait_for('THREAD_MEMBER_LIST_UPDATE', lambda d: True)
    assert len(ws._dispatch_listeners) == 1

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(future, timeout=0.01)
    await asyncio.sleep(0)  # done callbacks run on the next loop iteration

    assert ws._dispatch_listeners == [], 'a timed-out waiter must not linger'


@pytest.mark.asyncio
async def test_bulk_delete_removes_in_one_pass_and_keeps_deque():
    from collections import deque

    from discord.state import ConnectionState

    state = ConnectionState.__new__(ConnectionState)
    state._messages = deque(maxlen=10)
    state._messages_index = {}
    state._call_message_cache = {}

    msgs = [SimpleNamespace(id=i) for i in range(10)]
    for m in msgs:
        state._add_message_to_cache(m)  # type: ignore[arg-type]
    state._call_message_cache[3] = msgs[3]
    original = state._messages

    found = [msgs[1], msgs[3], msgs[7]]
    state._remove_messages_from_cache(found, {1, 3, 7, 999})  # type: ignore[arg-type]

    assert state._messages is original and original.maxlen == 10
    assert [m.id for m in state._messages] == [0, 2, 4, 5, 6, 8, 9]
    assert sorted(state._messages_index) == [0, 2, 4, 5, 6, 8, 9]
    assert 3 not in state._call_message_cache
