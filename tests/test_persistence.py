"""End-to-end cache persistence tests."""

from __future__ import annotations

import asyncio
import os

import pytest

import discord
from discord import Client, Guild
from discord.channel import TextChannel


def up(uid):
    return {'id': str(uid), 'username': f'u{uid}', 'discriminator': '0001', 'global_name': f'U{uid}', 'avatar': None}


def ready_payload(user_id=5):
    return {
        'v': 9,
        'user': up(user_id) | {'email': 'x@x', 'verified': True, 'purchased_flags': 0, 'flags': 0},
        'users': [up(100 + i) for i in range(12)],
        'guilds': [],  # filled per scenario
        'relationships': [{'id': str(200 + i), 'type': 1, 'user_id': str(100 + i)} for i in range(3)],
        'private_channels': [
            {'id': '900', 'type': 1, 'recipient_ids': ['101'], 'last_message_id': '1'},
        ],
        'sessions': [],
        'user_settings_proto': '',
        'required_action': None,
        'auth_token': None,
        'read_state': {'version': 0, 'entries': []},
        'user_guild_settings': {'version': 0, 'entries': []},
        'experiments': [],
        'guild_experiments': [],
        'friend_suggestion_count': 0,
        'connected_accounts': [],
        'consents': {},
        'country_code': 'US',
        'api_code_version': 1,
        'session_type': 'normal',
    }


def guild_payload(gid, n_members=5, member_count=None):
    return {
        'id': str(gid),
        'name': f'G{gid}',
        'member_count': member_count if member_count is not None else n_members,
        'roles': [{'id': str(gid), 'name': '@everyone'}],
        'channels': [{'id': str(500 + gid), 'name': 'general', 'type': 0, 'position': 0}],
        'members': [{'user': up(100 + i), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'} for i in range(n_members)],
        'presences': [],
        'voice_states': [],
        'threads': [],
        'emojis': [],
        'stickers': [],
        'features': [],
        'unavailable': False,
    }


def supplemental_for(ready):
    return {
        'guilds': [{} for _ in ready.get('guilds', [])],
        'merged_members': [[] for _ in ready.get('guilds', [])],
        'merged_presences': {'guilds': [[] for _ in ready.get('guilds', [])], 'friends': []},
        'lazy_private_channels': [],
    }


async def drive_ready(client, ready):
    state = client._connection
    state._subscribe_guilds = False
    state._chunk_guilds = False
    # Offline harness: no websocket behind the connect/ready handlers
    client._handlers['connect'] = lambda: None
    client._handlers['ready'] = lambda: None

    async def _noop_chunker(*args, **kwargs):
        return None

    state.chunker = _noop_chunker
    state.parsers['READY'](ready)
    state.parsers['READY_SUPPLEMENTAL'](supplemental_for(ready))
    # Let _delay_ready settle without a websocket
    if state._ready_task is not None:
        try:
            await asyncio.wait_for(state._ready_task, timeout=2)
        except asyncio.TimeoutError:
            pass
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_persistence_roundtrip(tmp_path):
    path = tmp_path / 'cache.bin'

    # Session 1: full login-style ready, snapshot written
    c1 = Client(persistence_path=path)
    await c1._async_setup_hook()
    state1 = c1._connection
    state1.user = discord.ClientUser(state=state1, data=up(5))  # static_login stand-in
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, 6), guild_payload(2, 3)]
    await drive_ready(c1, ready)
    await asyncio.sleep(0.05)  # let the post-ready write task finish
    assert path.exists(), 'persistence file written after ready'
    assert len(state1._guilds) == 2
    assert state1._get_guild(1).get_member(103) is not None

    # Session 2: replay from disk
    c2 = Client(persistence_path=path)
    await c2._async_setup_hook()
    state2 = c2._connection
    state2.user = discord.ClientUser(state=state2, data=up(5))

    async def _noop_chunker(*args, **kwargs):
        return None

    state2.chunker = _noop_chunker
    c2._handlers['connect'] = lambda: None
    c2._handlers['ready'] = lambda: None
    from discord.state import GuildSubscriptions

    original_has_feature = GuildSubscriptions.has_feature
    GuildSubscriptions.has_feature = lambda self, g, f: True  # offline: member_updates assumed
    try:
        replayed = await state2._replay_persistence()
        assert replayed
        assert set(state2._guilds) == {1, 2}
        g1 = state2._get_guild(1)
        assert g1.get_member(103) is not None, 'members replayed'
        assert g1.chunked, 'replayed guild counts as chunked -> no re-scrape'
    finally:
        GuildSubscriptions.has_feature = original_has_feature
    assert state2.get_channel(501) is not None
    assert len(state2._relationships) == 3
    assert state2._get_private_channel(900) is not None
    assert state2._persistence_loaded

    # Fresh READY: guild 2 left offline, guild 1 merges (members preserved)
    fresh = ready_payload()
    fresh['guilds'] = [guild_payload(1, 1, member_count=6)]  # only me in the fresh payload
    state2._ready_task = None
    state2.parsers['READY'](fresh)
    assert set(state2._guilds) == {1, 2}, 'guilds preserved across fresh READY'
    state2.parsers['READY_SUPPLEMENTAL'](supplemental_for(fresh))
    assert set(state2._guilds) == {1}, 'guild 2 pruned (left while offline)'
    g1_after = state2._get_guild(1)
    assert g1_after is g1, 'guild identity preserved (merged in place)'
    assert g1_after.get_member(104) is not None, 'replayed members survive the merge'
    assert not state2._persistence_loaded, 'flag consumed'


@pytest.mark.asyncio
async def test_persistence_guards(tmp_path):
    # Disabled by default
    c = Client()
    await c._async_setup_hook()
    assert await c._connection._replay_persistence() is False
    assert await c.write_persistence() is False

    # Corrupt file ignored
    path = tmp_path / 'bad.bin'
    path.write_bytes(b'garbage-not-zlib')
    c2 = Client(persistence_path=path)
    await c2._async_setup_hook()
    c2._connection.user = None  # replay before login user set is tolerated by the guard
    assert await c2._connection._replay_persistence() is False

    # Wrong account ignored
    path2 = tmp_path / 'wrong-account.bin'
    c3 = Client(persistence_path=path2)
    await c3._async_setup_hook()
    state3 = c3._connection
    state3.user = discord.ClientUser(state=state3, data=up(5))
    ready = ready_payload()
    ready['guilds'] = [guild_payload(7, 2)]
    await drive_ready(c3, ready)
    await asyncio.sleep(0.05)
    c4 = Client(persistence_path=path2)
    await c4._async_setup_hook()
    c4._connection.user = discord.ClientUser(state=c4._connection, data=up(999))
    assert await c4._connection._replay_persistence() is False
    assert not c4._connection._guilds
