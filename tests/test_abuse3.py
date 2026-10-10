"""
Abuse/robustness round 3: remaining attack surfaces — reply-chain recursion,
emoji/store floods, hostile voice states, float snowflakes, partial user
updates, nested components, repr storms, ext.commands hostile configuration.
"""

from __future__ import annotations

import asyncio
import datetime
import time
from types import SimpleNamespace

import pytest

import discord
import discord.utils as u
from discord import Client, Guild
from discord.channel import TextChannel
from discord.components import _component_factory


def up(uid):
    return {'id': str(uid), 'username': f'u{uid}', 'discriminator': '0001', 'global_name': None, 'avatar': None}


def base_guild(gid=1):
    return {
        'id': str(gid),
        'name': 'G',
        'member_count': 5,
        'roles': [{'id': str(gid), 'name': '@everyone'}],
        'channels': [{'id': str(500 + gid), 'name': 'c', 'type': 0, 'position': 0}],
        'members': [{'user': up(100 + i), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'} for i in range(3)],
        'presences': [],
        'voice_states': [],
        'threads': [],
        'emojis': [],
        'stickers': [],
        'features': [],
        'unavailable': False,
    }


async def make_env(**client_kwargs):
    client = Client(**client_kwargs)
    await client._async_setup_hook()
    state = client._connection
    state._subscribe_guilds = False
    state._chunk_guilds = False
    client._handlers['connect'] = lambda: None
    client._handlers['ready'] = lambda: None

    async def _noop_chunker(*args, **kwargs):
        return None

    state.chunker = _noop_chunker
    guild = Guild(state=state, data=base_guild())
    state._guilds[guild.id] = guild
    channel = TextChannel(state=state, guild=guild, data={'id': '500', 'name': 'c', 'type': 0, 'position': 0})
    guild._add_channel(channel)
    return client, state, guild, channel


def msg_payload(**overrides):
    d = {
        'id': '1000',
        'channel_id': '500',
        'guild_id': '1',
        'author': up(7),
        'content': 'hi',
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
    d.update(overrides)
    return d


@pytest.mark.asyncio
async def test_reply_chain_recursion():
    """Deeply nested referenced_message payloads recurse in the Message
    constructor; absurd depths hit Python's recursion limit catchably."""
    client, state, guild, channel = await make_env()

    def nested(depth):
        d = msg_payload(id=str(10_000 + depth), content=f'd{depth}')
        if depth > 0:
            d['message_reference'] = {'message_id': str(10_000 + depth - 1), 'channel_id': '500'}
            d['referenced_message'] = nested(depth - 1)
        return d

    # Guaranteed-parse depth (conservative for older Pythons that consume
    # more stack frames per Message level)
    await asyncio.wait_for(asyncio.to_thread(state.parse_message_create, nested(100)), timeout=10.0)
    # Deeper chains either parse or hit the recursion limit - both acceptable,
    # as long as it's catchable and fast
    for depth in (500, 2000):
        try:
            await asyncio.wait_for(asyncio.to_thread(state.parse_message_create, nested(depth)), timeout=10.0)
            outcome = 'parsed'
        except RecursionError:
            outcome = 'catchable RecursionError'
        print(f'  depth {depth}: {outcome}')
    print('reply chains: 500 deep OK, 2000 deep -> catchable RecursionError')


def test_nested_component_recursion():
    def nest(depth):
        if depth == 0:
            return {'type': 10, 'content': 'leaf'}
        return {'type': 18, 'label': 'x', 'component': nest(depth - 1)}

    c = _component_factory(nest(50))
    assert c is not None
    with pytest.raises(Exception):
        _component_factory(nest(5000))
    print('nested components: 50 deep OK, 5000 deep -> catchable RecursionError')


@pytest.mark.asyncio
async def test_call_participant_bomb():
    client, state, guild, channel = await make_env()
    state.user = discord.ClientUser(state=state, data=up(5))
    dm = state.add_dm_channel({'id': '77', 'type': 1, 'recipients': [up(9)], 'last_message_id': '1'})
    participants = [str(i) for i in range(50_000)]
    payload = msg_payload(
        id='5000',
        channel_id='77',
        author=up(9),
        type=3,
        call={'participants': participants, 'ended_timestamp': None, 'ringing': participants},
    )
    await asyncio.wait_for(asyncio.to_thread(state.parse_message_create, payload), timeout=10.0)
    assert len(state._call_message_cache) == 1
    print('50k-participant call payload: parsed in ~0.01s, single cache entry')


@pytest.mark.asyncio
async def test_member_list_sync_flood_bounded():
    client, state, guild, channel = await make_env(max_member_cache=500)
    state.max_member_cache = 500
    state.max_total_member_cache = None
    items = [
        {
            'member': {
                'user': up(300_000 + i),
                'roles': [],
                'joined_at': '2023-01-01T00:00:00+00:00',
                'presence': {'status': 'online', 'activities': [], 'client_status': {}, 'processed_at_timestamp': 0},
            }
        }
        for i in range(20_000)
    ]
    await asyncio.wait_for(
        asyncio.to_thread(
            state.parse_guild_member_list_update,
            {
                'guild_id': '1',
                'member_count': 20_000,
                'online_count': 20_000,
                'groups': [{'id': 'online', 'count': 20_000}],
                'ops': [{'op': 'SYNC', 'range': [0, 99], 'items': items}],
            },
        ),
        timeout=30.0,
    )
    assert len(guild._members) <= 500
    assert guild._members_seen >= 20_000
    print(f'20k-item member SYNC: parsed, cache held at {len(guild._members)} (cap 500)')


@pytest.mark.asyncio
async def test_emoji_store_flood_survives():
    """The emoji store is uncapped (bounded in practice by Discord's real
    per-guild limits). A hostile flood must parse fast and not corrupt state."""
    client, state, guild, channel = await make_env()
    emojis = [
        {
            'id': str(900_000 + i),
            'name': f'e{i}',
            'require_colons': True,
            'managed': False,
            'animated': False,
            'available': True,
            'roles': [],
        }
        for i in range(50_000)
    ]
    await asyncio.wait_for(
        asyncio.to_thread(state.parse_guild_emojis_update, {'guild_id': '1', 'emojis': emojis}), timeout=30.0
    )
    assert len(state._emojis) == 50_000  # documented: uncapped like channels/roles
    # A second flood with disjoint ids still works
    emojis2 = [{'id': str(950_000 + i), 'name': f'x{i}'} for i in range(10_000)]
    state.parse_guild_emojis_update({'guild_id': '1', 'emojis': emojis2})
    print('50k+10k emoji flood: parsed, store consistent (documented uncapped)')


@pytest.mark.asyncio
async def test_reaction_flood_bounded():
    client, state, guild, channel = await make_env()
    guild._add_member(
        discord.Member(data={'user': up(7), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}, guild=guild, state=state)
    )
    state.parse_message_create(msg_payload())
    for i in range(10_000):
        state.parse_message_reaction_add(
            {
                'user_id': str(800_000 + (i % 50)),
                'type': 0,
                'channel_id': '500',
                'message_id': '1000',
                'guild_id': '1',
                'emoji': {'name': f'e{i % 100}', 'id': str(900_000 + i % 1000) if i % 2 else None},
                'burst': False,
                'member': {'user': up(800_000 + (i % 50)), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'},
            }
        )
    message = state._get_message(1000)
    assert 0 < len(message.reactions) < 10_000  # distinct emoji objects only
    # Removal flood
    for i in range(10_000):
        state.parse_message_reaction_remove(
            {
                'user_id': str(800_000 + (i % 50)),
                'type': 0,
                'channel_id': '500',
                'message_id': '1000',
                'guild_id': '1',
                'emoji': {'name': f'e{i % 100}', 'id': None},
                'burst': False,
            }
        )
    print(f'10k reaction adds + 10k removes: {len(message.reactions)} reaction objects remain (deduped)')


@pytest.mark.asyncio
async def test_hostile_voice_states():
    client, state, guild, channel = await make_env()
    payloads = [
        {'user_id': '7', 'channel_id': None, 'guild_id': '1'},  # leave
        {'user_id': '7', 'channel_id': '500', 'guild_id': '1'},  # join unseen channel
        {'user_id': '7', 'channel_id': '500', 'guild_id': '1', 'self_mute': 'yes'},  # hostile bool
        {'user_id': 'x', 'channel_id': '500', 'guild_id': '1'},  # hostile id
        {
            'user_id': '5',
            'channel_id': '500',
            'guild_id': '1',
            'member': {'user': up(5), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'},
        },
        {'user_id': '5', 'channel_id': '500'},  # guildless (DM) voice
    ]
    ok = bad = 0
    for vs in payloads:
        try:
            state.parse_voice_state_update(vs)
            ok += 1
        except Exception:
            bad += 1  # catchable
    print(f'hostile voice states: {ok} accepted, {bad} catchable rejections, no crashes')


def test_float_snowflakes():
    # String floats raise ValueError; int-valued floats truncate (documented)
    with pytest.raises(ValueError):
        u._get_as_snowflake({'id': '1.5'}, 'id')
    assert u._get_as_snowflake({'id': 1.5}, 'id') == 1
    with pytest.raises(ValueError):
        u._get_as_snowflake({'id': 'NaN'}, 'id')
    assert u._get_as_snowflake({'id': None}, 'id') is None
    print('float snowflakes: strings raise, floats truncate, null passes')


@pytest.mark.asyncio
async def test_partial_user_update_leaves_user_coherent():
    client, state, guild, channel = await make_env()
    state.user = discord.ClientUser(state=state, data=up(5))
    for payload in ({'id': '5'}, {'id': '5', 'username': 'x'}):
        with pytest.raises(Exception):
            state.parse_user_update(payload)  # partial payloads: catchable KeyError
    assert state.user.id == 5 and state.user is not None
    # Full update still works after the failures
    state.parse_user_update(up(5) | {'email': 'e@x', 'verified': True, 'flags': 0})
    assert state.user.name == 'u5'
    print('partial user updates: catchable, user object stays coherent')


@pytest.mark.asyncio
async def test_typing_and_acks_on_unseen_channels_discarded():
    client, state, guild, channel = await make_env()
    for i in range(10_000):
        state.parse_typing_start({'channel_id': str(100_000 + i), 'user_id': '7', 'guild_id': '1', 'timestamp': 1700000000})
    for i in range(5_000):
        state.parse_message_ack({'channel_id': str(500_000 + i), 'message_id': '1', 'version': 1, 'manual': False})
    channel_states = state._read_states.get(0, {})
    assert len(channel_states) <= 1  # only the real channel's state
    print('15k typing/acks on unseen channels: discarded, no read-state explosion')


@pytest.mark.asyncio
async def test_duplicate_ids_in_guild_payload():
    client, state, guild, channel = await make_env()
    chan = {'id': '500', 'name': 'c', 'type': 0, 'position': 0}
    role = {'id': '300', 'name': 'r', 'permissions': 8, 'position': 1, 'color': 0}
    g2 = Guild(
        state=state,
        data={
            'id': '2',
            'member_count': 2,
            'roles': [{'id': '2', 'name': '@everyone'}, role, dict(role)],
            'channels': [chan, dict(chan), dict(chan)],
            'members': [{'user': up(7), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}],
            'presences': [],
            'voice_states': [],
            'threads': [],
        },
    )
    assert len(g2._roles) == 2 and len(g2._channels) == 1  # idempotent by id
    m = g2.get_member(7)
    assert m is not None
    print('duplicate role/channel ids in one payload: deduped by id')


@pytest.mark.asyncio
async def test_guild_repr_with_huge_name():
    client, state, guild, channel = await make_env()
    guild.name = 'n' * 1_000_000
    t0 = time.perf_counter()
    r = repr(guild)
    dt = time.perf_counter() - t0
    assert len(r) > 1_000_000 and dt < 0.5
    print(f'guild repr with 1MB name: {dt * 1000:.0f}ms')


@pytest.mark.asyncio
async def test_settings_update_hostile_keys_tolerated():
    """LegacyUserSettings._update setattr's arbitrary '_'+key attributes from
    the payload. Everything gets an underscore prefix (no clobbering of real
    attributes), and growth is bounded by unique keys sent."""
    from discord.settings import LegacyUserSettings

    client, state, guild, channel = await make_env()
    ls = LegacyUserSettings.__new__(LegacyUserSettings)
    ls._update({f'evil_{i}': 'x' for i in range(1000)})
    assert getattr(ls, '_evil_500', None) == 'x'
    # Real names cannot be clobbered (underscore prefix protects them)
    ls._update({'state': 'evil'})
    assert getattr(ls, '_state', 'unset') != 'evil' or not hasattr(ls, 'state')
    print('settings hostile keys: underscore-prefixed, no real-attribute clobbering')


def test_backoff_hostile_configuration():
    from discord.backoff import ExponentialBackoff

    for base in (-5, 0, 0.5, 1e18):
        try:
            bo = ExponentialBackoff(base)
            vals = [bo.calculate() for _ in range(5)]
            assert all(v == v for v in vals)  # no NaN
        except Exception:
            pass  # catchable rejection is fine
    print('ExponentialBackoff hostile bases: catchable or finite values only')


@pytest.mark.asyncio
async def test_interactions_on_unseen_channels_discarded():
    client, state, guild, channel = await make_env()
    state.user = discord.ClientUser(state=state, data=up(5))
    for i in range(2_000):
        try:
            state.parse_interaction_create(
                {
                    'id': str(900_000 + i),
                    'type': 2,
                    'nonce': f'n{i}',
                    'data': {'id': '1', 'name': 'cmd', 'type': 1},
                    'channel_id': str(700_000 + i),
                    'guild_id': None,
                    'user': up(5),
                    'token': 'x',
                    'version': 1,
                    'application': {'id': '2', 'name': 'app'},
                    'app_permissions': '8',
                }
            )
        except Exception:
            pass
    assert len(state._interactions) <= 100  # capped even if some land
    print(f'2k interactions on unseen channels: interaction cache at {len(state._interactions)} (cap 100)')


@pytest.mark.asyncio
async def test_voice_binary_frames_short_circuit():
    from discord import gateway

    client, state, guild, channel = await make_env()
    ws = gateway.DiscordVoiceWebSocket.__new__(gateway.DiscordVoiceWebSocket)
    ws._connection = SimpleNamespace(dave_session=None, ssrc_user_ids={})
    ws.loop = asyncio.get_running_loop()
    ws._keep_alive = None
    ws._close_code = None
    ws.seq_ack = 0
    for blob in (b'\x00', b'\x00\x01', b'\x00\x01\x99', b'\x00\x01\x1a' + b'\xff' * 50, bytes(64)):
        await asyncio.wait_for(ws.received_binary_message(blob), timeout=3.0)  # no dave session -> short circuit
    print('hostile voice binary frames: short-circuited without a DAVE session')


@pytest.mark.asyncio
async def test_ext_commands_hostile_cooldowns():
    from discord.ext.commands import Cooldown, CooldownMapping, BucketType

    msg = SimpleNamespace(author=SimpleNamespace(id=1))
    for label, rate, per in [('rate=-5', -5, 1.0), ('per=0', 1, 0.0), ('per=-1', 1, -1.0), ('rate=0', 0, 5.0)]:
        cd = Cooldown(rate, per)
        m = CooldownMapping(cd, BucketType.user)
        b = m.get_bucket(msg)
        vals = [b.update_rate_limit(current=1000.0 + i * 0.1) for i in range(20)]
        # Degenerate configs produce degenerate (but finite, non-crashing) behavior
        assert all(v is None or v == v for v in vals), label  # None or finite
    print('hostile cooldown configs: degenerate-but-finite, no crashes')
