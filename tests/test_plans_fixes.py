"""
Regressions for the plans.md review fixes: exact ever-seen member counting
(F-03), batched eviction (F-07), the O(1) voice-state merge (F-06), and
telemetry semantics — throttle-event expiry and a success-only, upload-safe
latency EWMA (F-09).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import discord
from discord import Client, Guild

from traffic_harness import REACTION_ROUTE, FakeResponse, MockDiscord, make_http, react


def up(uid):
    return {'id': str(uid), 'username': f'u{uid}', 'discriminator': '0001', 'global_name': None, 'avatar': None}


def member_payload(uid):
    return {'user': up(uid), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}


# --------------------------------------------------------------------------
# F-03 + F-07: exact ever-seen counting; batched eviction
# --------------------------------------------------------------------------


def make_state(cap=5, **kwargs):
    client = Client(**kwargs)
    state = client._connection
    state.user = discord.ClientUser(state=state, data=up(5))
    state.max_member_cache = cap
    return state


def make_member(state, guild, uid):
    return discord.Member(state=state, guild=guild, data=member_payload(uid))


def make_guild(state, member_count=1000):
    return Guild(
        state=state,
        data={'id': '1', 'roles': [{'id': '1', 'name': '@everyone'}], 'member_count': member_count},
    )


def test_members_seen_exact_across_evict_cycles():
    state = make_state(cap=3)
    guild = make_guild(state)

    for i in range(10):
        guild._add_member(make_member(state, guild, i))
    after_evictions = guild._members_seen
    assert after_evictions == 10, 'ever-seen counts every unique member once'

    # Re-adding an evicted member must not double-count (F-03)
    for i in range(10):
        guild._add_member(make_member(state, guild, i))
    assert guild._members_seen == 10

    # A member who left while evicted still decrements (F-03): ids 0-6
    # were evicted by the cap-3 churn above
    cached = next(iter(guild._members))
    assert 0 in guild._evicted_member_ids
    guild._remove_member(SimpleNamespace(id=cached))
    guild._remove_member(SimpleNamespace(id=0))
    assert guild._members_seen == 8
    assert 0 not in guild._evicted_member_ids
    print('members_seen: exact across evict -> re-add and evict -> leave')


def test_eviction_batches_amortize():
    state = make_state(cap=100)
    guild = make_guild(state)
    batches = 0
    last_len = 0
    for i in range(500):
        guild._add_member(make_member(state, guild, i))
        if len(guild._members) < last_len:
            batches += 1
        last_len = len(guild._members)
    # Hysteresis of ~5% of the cap: at most ~adds/(cap//20) batches, versus
    # ~400 per-insert evictions without batching
    assert batches <= 500 // max(1, 100 // 20) + 2, f'eviction ran per-insert ({batches} batches)'
    assert len(guild._members) <= 100
    assert guild._members_seen == 500
    print(f'eviction: 500 inserts at cap 100 -> {batches} batches, cache within cap')


# --------------------------------------------------------------------------
# F-06: voice-state merge correctness and complexity
# --------------------------------------------------------------------------


def guild_fragment(members, voice_states):
    return {
        'id': '1',
        'name': 'G',
        'member_count': max(1, len(members)),
        'roles': [{'id': '1', 'name': '@everyone'}],
        'channels': [],
        'members': members,
        'presences': [],
        'voice_states': voice_states,
        'threads': [],
        'emojis': [],
        'stickers': [],
        'features': [],
        'unavailable': False,
    }


def drive_supplemental(state, ready, extra):
    state._ready_data = ready
    state.parsers['READY_SUPPLEMENTAL'](extra)


@pytest.mark.asyncio
async def test_voice_merge_resolves_and_never_raises():
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    state._subscribe_guilds = False
    state._chunk_guilds = False
    client._handlers['connect'] = lambda: None
    client._handlers['ready'] = lambda: None

    async def _noop_chunker(*args, **kwargs):
        return None

    state.chunker = _noop_chunker

    # Members: one with inline user, one user_id-only (resolvable), one with
    # no user and no user_id (hostile -> must be skipped, not raise)
    members = [
        member_payload(11),
        {'user_id': '12', 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'},
        {'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'},
    ]
    voice_states = [
        {'user_id': '11', 'session_id': 'a', 'channel_id': '500'},
        {'user_id': '12', 'session_id': 'b', 'channel_id': '500'},
        {'user_id': '424242', 'session_id': 'c', 'channel_id': '500'},  # unknown member
    ]
    ready = {
        'user': up(5),
        'users': [up(11), up(12)],
        'guilds': [guild_fragment(members, voice_states)],
        'merged_members': [[]],
        'relationships': [],
        'private_channels': [],
        'sessions': [],
    }
    state.parsers['READY'](ready)
    drive_supplemental(
        state,
        ready,
        {'guilds': [{'voice_states': []}], 'merged_members': [[]], 'merged_presences': {'guilds': [[]], 'friends': []}},
    )
    if state._ready_task is not None:
        try:
            await asyncio.wait_for(state._ready_task, timeout=2)
        except asyncio.TimeoutError:
            pass

    guild = state._get_guild(1)
    assert guild is not None
    assert guild.get_member(11) is not None
    assert guild.get_member(12) is not None, 'user_id-only member resolved through temp users'
    print('voice merge: resolvable members resolved, hostile member skipped, no raise')


@pytest.mark.asyncio
async def test_voice_merge_is_linear_at_scale():
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    state._subscribe_guilds = False
    state._chunk_guilds = False
    client._handlers['connect'] = lambda: None
    client._handlers['ready'] = lambda: None

    async def _noop_chunker(*args, **kwargs):
        return None

    state.chunker = _noop_chunker

    n_members, n_voice = 5000, 500
    members = [member_payload(i) for i in range(n_members)]
    voice_states = [{'user_id': str(i % n_members), 'session_id': f's{i}', 'channel_id': '500'} for i in range(n_voice)]
    ready = {
        'user': up(5),
        'users': [],
        'guilds': [guild_fragment(members, voice_states)],
        'merged_members': [[]],
        'relationships': [],
        'private_channels': [],
        'sessions': [],
    }
    t0 = time.monotonic()
    state.parsers['READY'](ready)
    drive_supplemental(
        state,
        ready,
        {'guilds': [{}], 'merged_members': [[]], 'merged_presences': {'guilds': [[]], 'friends': []}},
    )
    if state._ready_task is not None:
        try:
            await asyncio.wait_for(state._ready_task, timeout=5)
        except asyncio.TimeoutError:
            pass
    elapsed = time.monotonic() - t0
    # O(members + voice_states): the old quadratic scan (5000 x 500 lambda
    # probes) does not finish anywhere near this bound
    assert elapsed < 3.0, elapsed
    print(f'voice merge: {n_members} members + {n_voice} voice states in {elapsed * 1000:.0f}ms')


# --------------------------------------------------------------------------
# F-09: telemetry semantics
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_throttle_events_expire_from_summary():
    mock = MockDiscord()
    http = make_http(mock)
    client = Client()
    client.http = http

    await asyncio.gather(*(react(http, ch) for ch in (10, 11, 12)))
    assert client.http_throttle_events(), 'throttles recorded'
    expected = len(http._throttle_events[REACTION_ROUTE.key])

    # Backdate every event past the default window: the summary empties
    events = http._throttle_events[REACTION_ROUTE.key]
    stale = time.monotonic() - 400.0
    http._throttle_events[REACTION_ROUTE.key] = type(events)((stale, ra) for _, ra in events)
    assert client.http_throttle_events() == {}

    # Reads are non-destructive: a wider horizon still sees the same events
    assert client.http_throttle_events(window=1000.0)[REACTION_ROUTE.key]['count'] == expected
    print('throttle telemetry: counts expire with the window; window= overrides the horizon')


@pytest.mark.asyncio
async def test_client_throttle_window_option():
    client = Client(throttle_window=0.05)
    assert client.http._throttle_window == 0.05
    http = client.http
    http._throttle_events['x'] = __import__('collections').deque([(time.monotonic() - 1.0, 0.1)], maxlen=64)
    assert client.http_throttle_events() == {}, 'events older than the configured window are gone'
    print('throttle telemetry: Client(throttle_window=...) plumbs through')


@pytest.mark.asyncio
async def test_latency_ewma_success_only_and_upload_keyed():
    mock = MockDiscord()
    http = make_http(mock)

    # A 429 + retry pair: only the successful attempt feeds the EWMA
    tripped = {'v': False}
    original = mock.request

    async def scripted(method, url, **kwargs):
        if method == 'PUT' and '/reactions/' in url and not tripped['v']:
            tripped['v'] = True
            return mock._shared_429(0.01)
        return await original(method, url, **kwargs)

    http._HTTPClient__session.request = scripted  # type: ignore
    await react(http, 20)
    http._HTTPClient__session.request = original  # type: ignore
    assert REACTION_ROUTE.key in http._route_latencies

    # 5xx responses never create an entry
    failing = {'v': False}

    async def failing_read(method, url, **kwargs):
        if method == 'GET' and not failing['v']:
            failing['v'] = True
            return FakeResponse(500, body=b'{"code": 0}', headers={'content-type': 'application/json'})
        return await original(method, url, **kwargs)

    http._HTTPClient__session.request = failing_read  # type: ignore
    from discord.errors import DiscordServerError

    with pytest.raises(DiscordServerError):
        await http.get_user(2)
    http._HTTPClient__session.request = original  # type: ignore
    assert not any(k.startswith('GET') for k in http._route_latencies), http._route_latencies

    # Uploads are keyed separately from plain sends
    fake_file = SimpleNamespace(reset=lambda seek=None: None)
    from traffic_harness import SEND_ROUTE, send

    await send(http, 30)  # plain send first: feeds the plain-send signal
    await http.send_message(
        30,
        params=__import__('discord.http', fromlist=['MultipartParameters']).MultipartParameters(
            {'content': 'x'}, None, [fake_file]
        ),
    )
    assert SEND_ROUTE.key in http._route_latencies
    assert SEND_ROUTE.key + '#files' in http._route_latencies
    print('latency EWMA: success-only, 5xx excluded, uploads keyed #files')
