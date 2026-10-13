"""
Regressions for the plans_v2.md review fixes: GC freeze holder accounting
(N-02), non-destructive throttle reads (N-03), ``throttle_window`` validation
(N-04), compact evicted-id tracking (N-06) and the ``Range`` type check (N-10).
"""

from __future__ import annotations

import gc
import time
import tracemalloc
from collections import deque

import pytest

import discord.state as state_module
from discord import Client
from discord.ext import commands
from discord.guild import _EvictedIds


# --------------------------------------------------------------------------
# N-02: per-state GC freeze holds
# --------------------------------------------------------------------------


def _hold(client: Client) -> None:
    # Mirrors what ConnectionState._schedule_gc_freeze's task does
    gc.freeze()
    state_module._gc_freeze_holders += 1
    client._connection._holds_gc_freeze = True


@pytest.fixture
def clean_gc_freeze():
    gc.unfreeze()
    state_module._gc_freeze_holders = 0
    yield
    gc.unfreeze()
    state_module._gc_freeze_holders = 0


@pytest.mark.asyncio
async def test_new_client_does_not_thaw_another_clients_graph(clean_gc_freeze):
    a = Client()
    _hold(a)
    assert gc.get_freeze_count() > 0

    Client()  # __init__ calls clear(full=True); it never froze, so it must not decrement
    assert state_module._gc_freeze_holders == 1
    assert gc.get_freeze_count() > 0, "a second client must not thaw the first client's graph"

    a._connection._release_gc_freeze()
    assert state_module._gc_freeze_holders == 0
    assert gc.get_freeze_count() == 0


@pytest.mark.asyncio
async def test_closed_client_releases_its_hold(clean_gc_freeze):
    a = Client()
    _hold(a)
    a._connection._release_gc_freeze()  # what Client.close() does
    assert state_module._gc_freeze_holders == 0

    b = Client()
    _hold(b)
    assert state_module._gc_freeze_holders == 1
    b._connection.clear()  # a reconnect: the last holder thaws
    assert state_module._gc_freeze_holders == 0
    assert gc.get_freeze_count() == 0


@pytest.mark.asyncio
async def test_release_is_idempotent(clean_gc_freeze):
    a = Client()
    _hold(a)
    a._connection._release_gc_freeze()
    a._connection._release_gc_freeze()
    assert state_module._gc_freeze_holders == 0


# --------------------------------------------------------------------------
# N-03 / N-04: throttle telemetry
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_throttle_read_with_narrow_window_loses_nothing():
    client = Client()
    now = time.monotonic()
    client.http._throttle_events['PUT x'] = deque([(now - 100.0, 1.0), (now - 1.0, 2.0)], maxlen=64)

    assert client.http_throttle_events(window=10.0)['PUT x']['count'] == 1
    assert client.http_throttle_events()['PUT x']['count'] == 2, 'a narrow read must not delete events'
    assert client.http_throttle_events(window=0.0) == {}
    assert len(client.http._throttle_events['PUT x']) == 2


@pytest.mark.asyncio
async def test_throttle_read_rejects_negative_window():
    with pytest.raises(ValueError):
        Client().http_throttle_events(window=-1.0)


@pytest.mark.asyncio
async def test_throttle_window_zero_is_honoured():
    assert Client(throttle_window=0).http._throttle_window == 0
    assert Client(throttle_window=None).http._throttle_window == 300.0
    assert Client().http._throttle_window == 300.0


@pytest.mark.asyncio
async def test_negative_throttle_window_rejected():
    with pytest.raises(ValueError):
        Client(throttle_window=-5)


# --------------------------------------------------------------------------
# N-06: compact evicted-id set
# --------------------------------------------------------------------------


def test_evicted_ids_behave_like_a_set():
    ids = _EvictedIds()
    ids.add_many([30, 10, 20])
    ids.add(40)
    assert len(ids) == 4 and bool(ids)
    assert 10 in ids and 25 not in ids and 'x' not in ids and -1 not in ids
    ids.add(10)  # duplicate: no double count
    assert len(ids) == 4
    ids.discard(20)
    ids.discard(999)  # absent: no error
    assert 20 not in ids and len(ids) == 3
    for i in range(50):  # force several run merges
        ids.add_many([1000 + i * 2, 1001 + i * 2])
    assert len(ids._runs) <= _EvictedIds.MAX_RUNS
    assert len(ids) == 103 and 1000 in ids and 1099 in ids


def test_evicted_ids_memory_is_compact():
    base = 1 << 60
    ids = _EvictedIds()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for batch in range(10):
            ids.add_many(base + batch * 100_000 + i for i in range(100_000))
        used = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert len(ids) == 1_000_000
    assert used / len(ids) < 16, f'{used / len(ids):.1f} B/id (a set costs ~70)'


# --------------------------------------------------------------------------
# N-10: Range min/max type check is symmetric
# --------------------------------------------------------------------------


@pytest.mark.parametrize('lo, hi', [(True, 5), (5, True), (1, 5.0), (1.0, 5)])
def test_range_rejects_mixed_types_in_either_order(lo, hi):
    with pytest.raises(TypeError):
        commands.Range[int, lo, hi]
