"""
End-to-end message pipeline + dispatch machinery regression tests,
driven through a real ConnectionState.
"""

from __future__ import annotations

import asyncio

import pytest

import discord
from discord import Client, Guild, Message
from discord.channel import TextChannel
from discord.threads import Thread


def up(uid):
    return {'id': str(uid), 'username': f'u{uid}', 'discriminator': '0001', 'global_name': None, 'avatar': None}


def msg_payload(mid, uid, content='hi', member=True, mention=None):
    d = {
        'id': str(mid),
        'channel_id': '500',
        'author': up(uid),
        'content': content,
        'timestamp': '2024-01-01T00:00:00+00:00',
        'edited_timestamp': None,
        'tts': False,
        'mention_everyone': False,
        'mentions': [],
        'mention_roles': [],
        'attachments': [],
        'embeds': [],
        'pinned': False,
        'type': 0,
        'flags': 0,
    }
    if member:
        d['member'] = {'roles': ['300'], 'joined_at': '2023-06-01T00:00:00+00:00', 'nick': None, 'pending': False}
    if mention:
        d['mentions'] = [{**up(mention), 'member': {'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}}]
    return d


@pytest.fixture()
def env():
    async def setup():
        client = Client()
        await client._async_setup_hook()
        state = client._connection
        guild = Guild(
            state=state,
            data={'id': '100', 'name': 'G', 'member_count': 50, 'roles': [{'id': '100', 'name': '@everyone'}]},
        )
        state._guilds[100] = guild
        channel = TextChannel(state=state, guild=guild, data={'id': '500', 'name': 'general', 'type': 0, 'position': 0})
        guild._add_channel(channel)
        return client, state, guild, channel

    return setup


@pytest.mark.asyncio
async def test_message_lifecycle(env):
    client, state, guild, channel = await env()

    state.parse_message_create(msg_payload(1000, 7001))
    message = state._get_message(1000)
    assert message is not None and message.id == 1000
    author = guild.get_member(7001)
    assert author is not None, 'author re-warmed into member cache'
    assert message.author is author

    # Update with no listeners -> cache mutated, no crash
    upd = msg_payload(1000, 7001, content='edited')
    upd.pop('timestamp')
    state.parse_message_update(upd)
    assert state._get_message(1000).content == 'edited'

    # Update with a listener -> full dispatch
    seen = []

    @client.event
    async def on_message_edit(before, after):
        seen.append((before.content, after.content))

    upd2 = msg_payload(1000, 7001, content='edited again')
    upd2.pop('timestamp')
    state.parse_message_update(upd2)
    await asyncio.sleep(0)
    assert seen == [('edited', 'edited again')]

    # Delete -> index cleanup, member retained
    state.parse_message_delete({'id': '1000', 'channel_id': '500', 'guild_id': '100'})
    assert state._get_message(1000) is None
    assert guild.get_member(7001) is not None


@pytest.mark.asyncio
async def test_mention_rewarm(env):
    client, state, guild, channel = await env()
    state.parse_message_create(msg_payload(1001, 7001, content='ping <@7002>', mention=7002))
    assert guild.get_member(7002) is not None, 'mentioned member re-warmed'
    assert state._get_message(1001).mentions[0].id == 7002


@pytest.mark.asyncio
async def test_deque_eviction_keeps_index_consistent(env):
    client, state, guild, channel = await env()
    state.max_member_cache = 10000
    for i in range(1100):
        state.parse_message_create(msg_payload(2000 + i, 7001))
    assert len(state._messages) == 1000
    assert state._get_message(2000) is None
    assert state._get_message(2000 + 1099) is not None
    assert {m.id for m in state._messages} == set(state._messages_index)


@pytest.mark.asyncio
async def test_dispatch_handler_cache_lifecycle(env):
    client, state, guild, channel = await env()
    calls = []
    client.dispatch('probe', 'a')
    await asyncio.sleep(0)

    async def handler(x):
        calls.append(('new', x))

    client.on_probe = handler  # direct assignment invalidates the cache
    client.dispatch('probe', 'b')
    await asyncio.sleep(0)
    assert ('new', 'b') in calls

    del client.on_probe
    client.dispatch('probe', 'c')
    await asyncio.sleep(0)
    assert ('new', 'c') not in calls
    assert 'on_probe' not in client._event_handlers


@pytest.mark.asyncio
async def test_reentrant_dispatch_safety(env):
    client, state, guild, channel = await env()
    fut = client.loop.create_future()
    reentered = []

    def pred(_):
        if not reentered:
            reentered.append(1)
            client.dispatch('probe2', 'inner')
        return True

    @client.event
    async def on_probe2(x):
        reentered.append(x)

    client._listeners['probe2'] = [(fut, pred)]
    client.dispatch('probe2', 'outer')  # must not raise InvalidStateError/KeyError
    await asyncio.sleep(0)
    assert fut.done() and fut.result() in ('outer', 'inner')
    assert 'inner' in reentered


@pytest.mark.asyncio
async def test_has_listener_honors_extra_events(env):
    client, state, guild, channel = await env()
    assert state._has_listener('message_edit') is False
    client.extra_events = {'on_message_edit': [None]}
    assert state._has_listener('message_edit') is True
    client.extra_events = {}
    assert state._has_listener('message_edit') is False


@pytest.mark.asyncio
async def test_thread_update_semantics(env):
    client, state, guild, channel = await env()
    tdata = {
        'id': '7',
        'parent_id': '500',
        'owner_id': '2',
        'name': 't',
        'type': 11,
        'message_count': 5,
        'member_count': 2,
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
    th = Thread(guild=guild, state=state, data={**tdata, 'applied_tags': ['3', '4']})
    assert th._update({'applied_tags': ['3', '4']}) is None  # identical -> no dispatch
    old = th._update({})  # absent tags reset (upstream semantics)
    assert old is not None and list(th._applied_tags) == []
    th._update({'thread_metadata': {**tdata['thread_metadata'], 'unknown_key': 1}})
    assert th._update({'thread_metadata': tdata['thread_metadata']}) is None  # metadata-only diff doesn't dispatch


@pytest.mark.asyncio
async def test_member_update_fast_paths(env):
    client, state, guild, channel = await env()
    base = {
        'user': up(31),
        'roles': ['1'],
        'joined_at': '2023-01-01T00:00:00+00:00',
        'nick': None,
        'pending': False,
    }
    m = discord.Member(data=base, guild=guild, state=state)
    # No-op update dispatches nothing
    assert m._update({'user': base['user'], 'roles': ['1'], 'premium_since': None, 'nick': None, 'pending': False}) is None
    # Real change returns the old copy
    old = m._update({'user': base['user'], 'roles': ['1'], 'premium_since': None, 'nick': 'new', 'pending': False})
    assert old is not None and old.nick is None and m.nick == 'new'
    # Message updates with identical raw fields don't re-parse
    m._raw_joined_at = 'SAME'
    m._update_from_message({'roles': ['1'], 'joined_at': 'SAME'})
    assert m._raw_joined_at == 'SAME'
    # Absent joined_at clears it (upstream overwrite semantics)
    m._update_from_message({'roles': ['1']})
    assert m._raw_joined_at is None and m.joined_at is None
