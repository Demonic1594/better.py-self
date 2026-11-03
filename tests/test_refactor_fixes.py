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


@pytest.mark.parametrize('use_orjson', [False, True])
def test_incremental_subscription_size_matches_full_encoding(monkeypatch, use_orjson):
    import json
    import random

    from discord import utils

    if use_orjson:
        orjson = pytest.importorskip('orjson')
        monkeypatch.setattr(utils, '_to_json', lambda obj: orjson.dumps(obj).decode('utf-8'))
    else:
        monkeypatch.setattr(utils, '_to_json', lambda obj: json.dumps(obj, separators=(',', ':'), ensure_ascii=True))

    rng = random.Random(7)
    sizes: dict = {}
    payload: dict = {}
    for _ in range(300):
        key = str(rng.randrange(10**17, 10**17 + 40))
        value = {
            'typing': rng.random() < 0.5,
            'members': [str(rng.randrange(10**17, 10**18)) for _ in range(rng.randrange(0, 6))],
            'channels': {str(rng.randrange(10**17, 10**18)): [[0, 99]]} if rng.random() < 0.5 else {},
        }
        payload[key] = value
        sizes[key] = GuildSubscriptions._entry_size(key, value)
        assert GuildSubscriptions._encoded_size(sizes) == len(utils._to_json(payload))
    assert GuildSubscriptions._encoded_size({}) == len(utils._to_json({}))


@pytest.mark.asyncio
async def test_bucket_sweep_is_throttled_but_still_evicts(monkeypatch):
    import discord.http as http_module
    from traffic_harness import make_http

    http = make_http(None)
    clock = [1000.0]
    monkeypatch.setattr(http_module.time, 'monotonic', lambda: clock[0])
    scans = []
    real_inactive = http_module.Ratelimit.is_inactive

    def counting_inactive(self):
        scans.append(1)
        return real_inactive(self)

    monkeypatch.setattr(http_module.Ratelimit, 'is_inactive', counting_inactive)

    for i in range(400):
        http.get_ratelimit(f'route:{i}')
    # One sweep when the table reached 256, then none until the interval passes
    assert len(scans) == 256, len(scans)

    # Age every bucket past the 300s idle threshold, then pass the interval
    for bucket in http._buckets.values():
        bucket._last_request -= 301
    clock[0] += http.BUCKET_SWEEP_INTERVAL
    http.get_ratelimit('route:new')
    assert list(http._buckets) == ['route:new'], 'idle buckets are still evicted'


@pytest.mark.asyncio
async def test_dispatch_handler_add_replace_remove_and_none():
    import discord

    client = discord.Client()
    await client._async_setup_hook()
    calls = []

    async def first(*args):
        calls.append('first')

    async def second(*args):
        calls.append('second')

    client.dispatch('thing')  # no handler yet: a no-op
    client.on_thing = first  # type: ignore[attr-defined]
    client.dispatch('thing')
    client.on_thing = second  # type: ignore[attr-defined]
    client.dispatch('thing')
    client.on_thing = None  # type: ignore[attr-defined]
    client.dispatch('thing')  # used to cache None and crash scheduling it
    del client.on_thing  # type: ignore[attr-defined]
    client.dispatch('thing')
    await asyncio.sleep(0.05)

    assert calls == ['first', 'second']
    await client.close()


async def _presence_env():
    import discord
    from discord import Client, Guild

    client = Client()
    await client._async_setup_hook()
    state = client._connection
    guild = Guild(state=state, data={'id': '1', 'roles': [{'id': '1', 'name': '@everyone'}], 'member_count': 5})
    state._guilds[1] = guild
    user = {'id': '7', 'username': 'u', 'discriminator': '0001', 'global_name': None, 'avatar': None}
    guild._add_member(
        discord.Member(data={'user': user, 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}, guild=guild, state=state)
    )
    return client, state, guild


def _presence(status):
    return {'user': {'id': '7'}, 'status': status, 'activities': [], 'client_status': {'desktop': status}}


@pytest.mark.asyncio
async def test_presence_cache_updates_without_listener():
    import discord

    client, state, guild = await _presence_env()
    state._handle_presence_update(guild, _presence('online'))
    state._handle_presence_update(guild, _presence('dnd'))
    assert guild.get_member(7).status is discord.Status.dnd
    await client.close()


@pytest.mark.asyncio
async def test_presence_wait_for_only_listener_gets_before_and_after():
    import discord

    client, state, guild = await _presence_env()
    state._handle_presence_update(guild, _presence('online'))

    waiter = asyncio.ensure_future(client.wait_for('presence_update', timeout=1))
    await asyncio.sleep(0)
    state._handle_presence_update(guild, _presence('idle'))
    before, after = await waiter

    assert before.status is discord.Status.online and after.status is discord.Status.idle
    await client.close()


@pytest.mark.asyncio
async def test_webhook_lock_released_when_cancelled_during_deferral():
    from discord.webhook.async_ import AsyncDeferredLock

    lock = asyncio.Lock()

    async def holder():
        async with AsyncDeferredLock(lock) as deferred:
            deferred.delay_by(30)  # bucket exhausted: defer the release

    task = asyncio.ensure_future(holder())
    await asyncio.sleep(0.01)
    assert lock.locked()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked(), 'later senders to this webhook would hang forever'


def test_sync_webhook_lock_released_when_interrupted(monkeypatch):
    import threading

    import discord.webhook.sync as sync_module

    def interrupted_sleep(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(sync_module.time, 'sleep', interrupted_sleep)
    lock = threading.Lock()
    with pytest.raises(KeyboardInterrupt):
        with sync_module.DeferredLock(lock) as deferred:
            deferred.delay_by(5)
    assert not lock.locked()


def test_connection_reset_errnos_cover_this_platform():
    import errno

    from discord import utils

    assert errno.ECONNRESET in utils._CONNECTION_RESET_ERRNOS  # 104 on Linux
    assert {54, 10054} <= utils._CONNECTION_RESET_ERRNOS


def test_ratelimit_header_without_reset_fields_does_not_raise():
    from discord import utils

    response = SimpleNamespace(headers={'X-Ratelimit-Remaining': '0'})
    assert utils._parse_ratelimit_header(response) == 0.0
    assert utils._parse_ratelimit_header(SimpleNamespace(headers={'X-Ratelimit-Reset-After': '1.5'})) == 1.5


@pytest.mark.asyncio
async def test_webhook_gives_up_without_a_final_sleep(monkeypatch):
    import discord.webhook.async_ as async_module
    from discord.errors import DiscordServerError
    from discord.http import Route

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(async_module.asyncio, 'sleep', fake_sleep)

    class Response:
        status = 503
        headers: dict = {}
        reason = 'Service Unavailable'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def text(self):
            return 'down'

        async def read(self):
            return b'down'

    class Session:
        def request(self, *args, **kwargs):
            return Response()

    adapter = async_module.AsyncWebhookAdapter()
    route = Route('POST', '/webhooks/{webhook_id}/{webhook_token}', webhook_id=1, webhook_token='t')
    with pytest.raises(DiscordServerError):
        await adapter.request(route, session=Session())  # type: ignore[arg-type]

    assert len(sleeps) == 4, f'5 attempts need 4 backoffs, not 5: {sleeps}'
