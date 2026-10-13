"""
Cache cap regression tests: member LRU + global rebalance, thread caps,
presence LRU, re-warm behavior, chunked accounting.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import discord
import discord.state as state_mod
from discord import Client, Guild, Member
from discord.flags import MemberCacheFlags


class StubSubscriptions:
    def has_feature(self, guild, feature):
        return True


def make_state(loop, max_member_cache=10, max_total=None, max_thread_cache=None, max_total_thread=None):
    state = SimpleNamespace(
        max_member_cache=max_member_cache,
        max_total_member_cache=max_total,
        _member_add_counter=0,
        max_thread_cache=max_thread_cache,
        max_total_thread_cache=max_total_thread,
        _thread_add_counter=0,
        max_guild_presence_cache=None,
        max_presence_cache=None,
        gc_freeze=False,
        _guilds={},
        member_cache_flags=MemberCacheFlags.all(),
        subscriptions=StubSubscriptions(),
        self_id=999,
        _presences={},
        _member_hits=0,
        _member_misses=0,
    )
    # Borrow the real amortized implementations
    state._note_member_add = state_mod.ConnectionState.__dict__['_note_member_add'].__get__(state)
    state._maybe_evict_members_global = state_mod.ConnectionState.__dict__['_maybe_evict_members_global'].__get__(state)
    state._note_thread_add = (
        state_mod.ConnectionState.__dict__['_note_thread_add'].__get__(state) if max_total_thread else (lambda: None)
    )
    state.store_presence = lambda user_id, presence, guild_id=None: state._presences.update({user_id: presence})
    state.remove_presence = lambda user_id, guild_id=None: state._presences.pop(user_id, None)
    state.store_user = lambda data: SimpleNamespace(id=int(data['id']))
    return state


def make_member(state, guild, uid):
    return Member(
        data={
            'user': {
                'id': str(uid),
                'username': f'u{uid}',
                'discriminator': '0001',
                'global_name': None,
                'avatar': None,
            },
            'roles': [],
            'joined_at': '2023-01-01T00:00:00+00:00',
        },
        guild=guild,
        state=state,
    )


def thread_payload(i):
    return {
        'id': str(1000 + i),
        'parent_id': '500',
        'owner_id': '2',
        'name': f't{i}',
        'type': 11,
        'message_count': 1,
        'member_count': 1,
        'rate_limit_per_user': 0,
        'flags': 0,
        'thread_metadata': {
            'archived': False,
            'auto_archive_duration': 1440,
            'archive_timestamp': '2024-01-01T00:00:00+00:00',
            'locked': False,
            'invitable': True,
        },
    }


def make_guild(state, gid=1, member_count=50):
    guild = Guild(
        state=state, data={'id': gid, 'member_count': member_count, 'roles': [{'id': str(gid), 'name': '@everyone'}]}
    )
    state._guilds[gid] = guild
    return guild


def test_member_cap_and_lru_order():
    state = make_state(None, max_member_cache=10)
    guild = make_guild(state)
    for i in range(15):
        guild._add_member(make_member(state, guild, i))
    # Batched eviction (hysteresis): lands within [95% of cap, cap]
    assert 9 <= len(guild._members) <= 10
    assert set(guild._members) <= set(range(5, 15))
    assert len(guild._members) >= 9
    assert list(guild._members)[-1] == 14


def test_member_touch_protection():
    state = make_state(None, max_member_cache=10)
    guild = make_guild(state)
    for i in range(15):
        guild._add_member(make_member(state, guild, i))
    # Batched eviction (hysteresis) keeps ~95% of the cap; touch a live
    # member and confirm it survives the next eviction batch
    guild._touch_member(10)
    for i in range(100, 106):
        guild._add_member(make_member(state, guild, i))
    assert 10 in guild._members and 8 not in guild._members


def test_member_voice_and_self_protection():
    state = make_state(None, max_member_cache=10)
    guild = make_guild(state)
    guild._add_member(make_member(state, guild, 999))  # self
    guild._voice_states[777] = SimpleNamespace(user_id=777)
    guild._add_member(make_member(state, guild, 777))
    for i in range(300, 360):
        guild._add_member(make_member(state, guild, i))
    assert 999 in guild._members and 777 in guild._members


def test_member_seen_and_chunked_accounting():
    state = make_state(None, max_member_cache=10)
    guild = make_guild(state, member_count=50)
    guild._add_member(make_member(state, guild, 999))  # self
    for i in range(30):
        guild._add_member(make_member(state, guild, i))
    assert guild._members_seen == 31  # 30 range + self
    assert not guild.chunked  # 30 < 50
    for i in range(30, 50):
        guild._add_member(make_member(state, guild, i))
    assert guild.chunked  # seen >= count despite eviction
    # Removal decrements (999 is protected by the self-id and still cached)
    before = guild._members_seen
    guild._remove_member(SimpleNamespace(id=999))
    assert guild._members_seen == before - 1
    # Removing an uncached member cannot go negative
    guild._remove_member(SimpleNamespace(id=123456))
    assert guild._members_seen >= 0


def test_global_member_rebalance_average_policy():
    state = make_state(None, max_member_cache=None, max_total=100)
    for g in range(10):
        guild = make_guild(state, gid=g)
        for i in range(30):
            guild._add_member(make_member(state, guild, i))
    state._maybe_evict_members_global()
    total = sum(len(g._members) for g in state._guilds.values())
    assert total <= 100 + 10  # protected slop
    sizes = sorted(len(g._members) for g in state._guilds.values())
    assert max(sizes) <= min(sizes) + 1


def test_should_cache_member_gating():
    state = make_state(None)
    guild = make_guild(state)
    assert guild._should_cache_member(make_member(state, guild, 42))  # flags.all
    state.member_cache_flags = MemberCacheFlags.none()
    assert not guild._should_cache_member(make_member(state, guild, 42))
    assert guild._should_cache_member(make_member(state, guild, 999))  # self


def test_thread_cap_lru_and_join_protection():
    state = make_state(None, max_member_cache=None, max_thread_cache=10)
    guild = make_guild(state)
    for i in range(25):
        guild._add_thread(discord.Thread(guild=guild, state=state, data=thread_payload(i)))
    assert 9 <= len(guild._threads) <= 10
    assert set(guild._threads) <= set(range(1015, 1025))
    guild._touch_thread(1016)
    guild._add_thread(discord.Thread(guild=guild, state=state, data=thread_payload(1100)))
    guild._add_thread(discord.Thread(guild=guild, state=state, data=thread_payload(1101)))
    assert 1016 in guild._threads and 1017 not in guild._threads
    # Joined threads survive
    joined = guild.get_thread(1024)
    joined._members[999] = SimpleNamespace(id=999)
    for i in range(200, 215):
        guild._add_thread(discord.Thread(guild=guild, state=state, data=thread_payload(i)))
    assert 1024 in guild._threads
    assert guild._evicted_threads > 0


def test_thread_store_and_eviction_counters():
    state = make_state(None, max_member_cache=None, max_thread_cache=5)
    guild = make_guild(state)
    for i in range(8):
        guild._store_thread(thread_payload(i))
    assert 4 <= len(guild._threads) <= 5
    assert guild._evicted_threads == 4  # two hysteresis batches


@pytest.mark.asyncio
async def test_presence_lru_and_member_gating():
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    state.max_member_cache = None
    guild = make_guild(state)
    payload = lambda uid, status='online': {
        'user': {'id': str(uid)},
        'status': status,
        'activities': [],
        'client_status': {},
        'processed_at_timestamp': 0,
    }

    # Cached member: identical updates dispatch once; changes dispatch again
    guild._add_member(make_member(state, guild, 7))
    seen = []

    @client.event
    async def on_presence_update(before, after):
        seen.append(after.status)

    for _ in range(50):
        state._handle_presence_update(guild, payload(7))
    await asyncio.sleep(0)
    assert len(seen) == 1
    state._handle_presence_update(guild, payload(7, 'idle'))
    await asyncio.sleep(0)
    assert [s.name for s in seen] == ['online', 'idle']

    # Absent member: presence dropped, no dispatch, no orphan
    state._handle_presence_update(guild, payload(424242))
    assert state.get_presence(424242, guild.id) is None

    # Eviction removes the member's presence
    state.max_guild_presence_cache = None
    m = make_member(state, guild, 8)
    m._presence = state.create_presence(payload(8), 8)
    guild._add_member(m)
    assert state.get_presence(8, guild.id) is not None
    guild._evict_members(0)
    assert state.get_presence(8, guild.id) is None


@pytest.mark.asyncio
async def test_presence_global_lru():
    from collections import OrderedDict

    client = Client()
    await client._async_setup_hook()
    stub = SimpleNamespace(
        self_id=1,
        client_presence=None,
        _guild_presences=OrderedDict(),
        _presences=OrderedDict(),
        max_presence_cache=5,
        max_guild_presence_cache=None,
        _presence_evictions=0,
        remove_presence=lambda user_id, guild_id=None: None,
    )
    make = lambda status='online': SimpleNamespace(
        client_status=SimpleNamespace(status=status), activities=(), hidden_activities=()
    )
    store = lambda uid: state_mod.ConnectionState.store_presence(stub, uid, make(), None)
    for i in range(10):
        store(i)
    assert len(stub._presences) == 5 and set(stub._presences) == set(range(5, 10))
    assert state_mod.ConnectionState.get_presence(stub, 5) is not None
    assert list(stub._presences)[0] == 6  # 5 refreshed to MRU
    store(3)
    assert 3 in stub._presences and 6 not in stub._presences
