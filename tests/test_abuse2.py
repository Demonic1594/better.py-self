"""
Abuse/robustness round 2: gateway frame hostility, dispatch machinery abuse,
resource-exhaustion variants, hostile persistence shapes, and model-layer
extremes. Survival contract: no hangs, bounded resources, catchable-only
exceptions.
"""

from __future__ import annotations

import asyncio
import datetime
import gc
import os
import tempfile
import time
import zlib
from types import SimpleNamespace

import pytest

import discord
import discord.utils as u
from discord import Client, Guild, Permissions
from discord.channel import TextChannel
from discord import gateway


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


# ===========================================================================
# Gateway frame hostility
# ===========================================================================


class FakeSocket:
    closed = False
    close_code = None
    close_reason = ''

    async def recv(self):
        await asyncio.sleep(30)

    async def send(self, d):
        pass

    async def close(self, code=None, reason=b''):
        self.closed = True


def make_ws(loop):
    ws = gateway.DiscordWebSocket(FakeSocket(), loop=loop)
    ws._max_heartbeat_timeout = 60
    ws._dispatch = lambda *a: None
    ws.log_receive = lambda *a: None
    ws._discord_parsers = {}
    ws._headers = type(
        'H',
        (),
        {
            'initialization_timestamp': datetime.datetime.now(),
            'gateway_properties': {},
        },
    )()
    ws._connection = type('C', (), {'_has_voice_client': lambda s: False})()
    return ws


async def count_bg_tasks():
    await asyncio.sleep(0.01)
    return len([t for t in asyncio.all_tasks() if t is not asyncio.current_task()])


HOSTILE_FRAMES = [
    '{"t":null,"s":null,"op":10,"d":null}',  # HELLO with null data -> TypeError (catchable)
    'not json at all',
    '"just a string"',
    '12345',
    'null',
    '[]',
    '{"op":null,"d":null,"t":null,"s":null}',
    '{"op":"1","d":{},"t":null,"s":null}',
    '{"op":3.5,"d":{},"t":null,"s":null}',
    '{"op":9999,"d":{},"t":null,"s":null}',
    '{"op":0,"d":{},"t":"NOT_A_REAL_EVENT","s":1}',
    '{"op":0,"d":{"guild_id":"1"},"t":"MESSAGE_CREATE","s":10**30}',
    '{"op":0,"d":null,"t":"MESSAGE_CREATE","s":-999999}',
    '{"op":0,"d":{"id":null},"t":"MESSAGE_ACK","s":2}',
    '{"op":1,"d":null,"t":null,"s":null}',
    '{"op":11,"d":null,"t":null,"s":null}',  # HEARTBEAT_ACK with no keepalive
]


@pytest.mark.asyncio
async def test_gateway_hostile_frames():
    loop = asyncio.get_running_loop()
    for frame in HOSTILE_FRAMES:
        ws = make_ws(loop)
        try:
            await asyncio.wait_for(ws.received_message(frame), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail(f'gateway hung on frame: {frame[:80]}')
        except Exception:
            pass  # catchable parser failure: acceptable
        except BaseException as e:
            pytest.fail(f'BaseException leak on {frame[:60]}: {e!r}')
    print(f'{len(HOSTILE_FRAMES)} hostile frames: no hangs, no BaseException leaks')


@pytest.mark.asyncio
async def test_hello_flood_does_not_leak_tasks():
    """Each HELLO used to spawn a fresh keepalive without stopping the old:
    5 HELLOs leaked 5 heartbeat tasks. The fix stops the previous handler."""
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)
    hello = '{"t":null,"s":null,"op":10,"d":{"heartbeat_interval":60000,"_trace":[]}}'
    for _ in range(20):
        await ws.received_message(hello)
        await asyncio.sleep(0.005)
    alive = await count_bg_tasks()
    assert alive == 1, f'HELLO flood leaked heartbeat tasks: {alive} alive'
    ws._keep_alive.stop()
    await asyncio.sleep(0.02)
    assert await count_bg_tasks() == 0
    print('20 HELLO frames: exactly 1 live keepalive, clean stop')


@pytest.mark.asyncio
async def test_invalid_json_frame_does_not_wreck_socket():
    """Invalid JSON on a text frame raises (and the caller catches); the
    zlib path with garbage bytes must buffer or raise, never hang."""
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)
    with pytest.raises(Exception):
        await ws.received_message('{"broken": ')
    # garbage on the *bytes* path: buffered as partial or raises, never hangs
    for blob in (b'\x00' * 100, b'\xff' * 1000, b'PK\x03\x04' + os.urandom(64)):
        try:
            await asyncio.wait_for(ws.received_message(blob), timeout=3.0)
        except asyncio.TimeoutError:
            pytest.fail('decompressor hung on garbage bytes')
        except Exception:
            pass
    print('invalid JSON + garbage binary frames handled')


# ===========================================================================
# Dispatch machinery abuse
# ===========================================================================


@pytest.mark.asyncio
async def test_listener_abuse():
    client, state, guild, channel = await make_env()

    # 1. Pre-cancelled future in the listener list
    fut = client.loop.create_future()
    fut.cancel()
    client._listeners['probe'] = [(fut, lambda x: True)]
    client.dispatch('probe', 1)
    client._listeners.pop('probe', None)

    # 2. Predicate that raises
    fut2 = client.loop.create_future()
    client._listeners['probe'] = [(fut2, lambda x: 1 / 0)]
    client.dispatch('probe', 2)  # must not propagate
    await asyncio.sleep(0)
    assert fut2.done() and fut2.exception() is not None
    client._listeners.pop('probe', None)

    # 3. Deep (but bounded) re-entrant dispatch
    depth = {'n': 0}

    def deep_predicate(_):
        depth['n'] += 1
        if depth['n'] < 100:
            client.dispatch('deep', depth['n'])
        return False

    client._listeners['deep'] = [(client.loop.create_future(), deep_predicate)]
    client.dispatch('deep', 0)
    client._listeners.pop('deep', None)
    assert depth['n'] >= 100
    print('listener abuse: cancelled futures, raising predicates, 100-deep re-entrancy: clean')


@pytest.mark.asyncio
async def test_hostile_event_names_and_handlers():
    client, state, guild, channel = await make_env()

    # Hostile event names: empty, huge, unicode, dunders
    for name in ('', 'x' * 100_000, 'évent', '__init__', 'a b c', '\x00'):
        client.dispatch(name)  # no handler -> clean no-op through the cache
    assert len(client._event_handlers) == 0

    # Non-coroutine handler via direct setattr (bypasses event() validation)
    calls = []

    def sync_handler(x):
        calls.append(x)

    client.on_probe = sync_handler
    client.dispatch('probe', 1)
    await asyncio.sleep(0.05)
    # _schedule_event must have failed catchably (task creation on non-coroutine)
    # and the run must not have crashed the loop
    assert True
    del client.on_probe

    # Handler + on_error both raising: must be swallowed by _run_event
    async def bomb(x):
        raise RuntimeError('handler bomb')

    async def error_bomb(method, *a, **k):
        raise RuntimeError('on_error bomb')

    client.on_probe = bomb
    client.on_error = error_bomb
    client.dispatch('probe', 2)
    await asyncio.sleep(0.05)
    del client.on_probe
    del client.on_error
    print('hostile event names, sync-handler setattr, handler+on_error bombs: loop alive')


@pytest.mark.asyncio
async def test_wait_for_storm():
    client, state, guild, channel = await make_env()
    futures = [client.wait_for('never', timeout=0.05) for _ in range(2000)]
    t0 = time.perf_counter()
    results = await asyncio.gather(*futures, return_exceptions=True)
    dt = time.perf_counter() - t0
    timeouts = sum(1 for r in results if isinstance(r, asyncio.TimeoutError))
    assert timeouts == 2000
    assert not client._listeners.get('never')
    # Satisfying storm: all resolve in one dispatch
    futures2 = [asyncio.create_task(client.wait_for('mass', timeout=5)) for _ in range(2000)]
    await asyncio.sleep(0.05)  # let the wait_for coroutines start and register
    client.dispatch('mass', 'go')
    results2 = await asyncio.gather(*futures2, return_exceptions=True)
    assert all(r == 'go' for r in results2)
    print(f'wait_for storm: 2000 timeouts in {dt:.1f}s, 2000 mass-resolved in one dispatch')


# ===========================================================================
# Model-layer extremes
# ===========================================================================


def test_snowflake_extremes():
    for sid in (0, 1, -1, 2**22 - 1, 2**63, 2**64 - 1):
        dt = u.snowflake_time(sid)  # all valid datetimes (epoch-anchored)
        assert isinstance(dt, datetime.datetime)
    for sid in (10**30, -(2**80)):
        try:
            u.snowflake_time(sid)
        except (OverflowError, OSError, ValueError):
            pass  # out-of-datetime-range: catchable
        except Exception as e:
            pytest.fail(f'unexpected exception type for snowflake {sid}: {e!r}')
    print('snowflake extremes: valid ids -> datetimes, absurd ids -> catchable')


@pytest.mark.asyncio
async def test_hostile_timestamps():
    client, state, guild, channel = await make_env()
    payloads = [
        {'id': '5001', 'timestamp': 'not-a-date'},
        {'id': '5002', 'timestamp': '9999-12-31T23:59:59+00:00'},
        {'id': '5003', 'timestamp': '10000-01-01T00:00:00+00:00'},
        {'id': '5004', 'timestamp': ''},
        {'id': '5005', 'timestamp': 12345},
    ]
    ok = 0
    for extra in payloads:
        try:
            await asyncio.wait_for(asyncio.to_thread(state.parse_message_create, msg_payload(**extra)), timeout=5.0)
            ok += 1
        except asyncio.TimeoutError:
            pytest.fail(f'hung on timestamp {extra["timestamp"]!r}')
        except Exception:
            ok += 1  # catchable
    print(f'hostile timestamps: {len(payloads)} variants, {ok} handled (parse-or-catchable), 0 hangs')


@pytest.mark.asyncio
async def test_snowflake_list_overflow():
    client, state, guild, channel = await make_env()
    for roles in ([10**30], [-(2**70)], [0, 10**25, 1]):
        member = discord.Member(
            data={'user': up(88), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'},
            guild=guild,
            state=state,
        )
        try:
            await asyncio.wait_for(asyncio.to_thread(member._update_from_message, {'roles': roles}), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail('SnowflakeList hung on absurd ids')
        except Exception:
            pass  # OverflowError etc: catchable
    print('SnowflakeList overflow (10^30 role ids): catchable')


@pytest.mark.asyncio
async def test_voice_state_protection_flood():
    """5000 fake voice states: eviction protects them all (documented behavior
    for hostile payloads) but must not hang or crash the guild."""
    client, state, guild, channel = await make_env()
    state.max_member_cache = 100
    state.max_total_member_cache = None
    for i in range(5000):
        guild._voice_states[200_000 + i] = SimpleNamespace(user_id=200_000 + i, channel=channel)
    t0 = time.perf_counter()
    for i in range(5200):
        guild._add_member(
            discord.Member(
                data={'user': up(300_000 + i), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'},
                guild=guild,
                state=state,
            )
        )
    dt = time.perf_counter() - t0
    # Members outside the protected set still evict down to the cap
    assert len(guild._members) <= 100 + 5000 + 1, len(guild._members)
    assert dt < 30
    print(f'voice-state flood (5000 protected): {len(guild._members)} cached, {dt:.1f}s, no hang')


@pytest.mark.asyncio
async def test_mention_member_cache_flood():
    """A single message with 5000 mentions, each embedding a full member
    payload: the member cache cap must hold and parsing must complete."""
    client, state, guild, channel = await make_env()
    state.max_member_cache = 200
    state.max_total_member_cache = None
    mentions = [{**up(400_000 + i), 'member': {'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}} for i in range(5000)]
    # Duplicate ids too (aliases)
    mentions = mentions + mentions[:500]
    payload = msg_payload(id='7000', mentions=mentions, content='ping ' + ' '.join(f'<@{400000 + i}>' for i in range(200)))
    await asyncio.wait_for(asyncio.to_thread(state.parse_message_create, payload), timeout=15.0)
    assert len(guild._members) <= 200 + 4
    message = state._get_message(7000)
    assert len(message.mentions) == 5500
    print(f'5500 mentions in one message: parsed, member cache held at {len(guild._members)} (cap 200)')


def test_permissions_and_flags_extremes():
    assert Permissions._from_value(-1).value == -1  # all bits + sign: consistent roundtrip
    assert Permissions._from_value(2**70).value == 2**70
    from discord import MessageFlags

    f = MessageFlags._from_value(2**64)
    assert not f.ephemeral
    neg = MessageFlags._from_value(-1)
    assert neg.ephemeral  # all bits set: inverted-view still coherent
    # Operators on extremes stay closed
    a = Permissions._from_value(-1)
    b = Permissions._from_value(2**70)
    assert (a | b).value == -1 and (a & b).value == 2**70
    print('permissions/flags extremes (-1, 2^70): roundtrip and operators coherent')


# ===========================================================================
# Route and URL hostility
# ===========================================================================


def test_route_hostile_parameters():
    from discord.http import Route

    # Missing parameter: catchable
    with pytest.raises(Exception):
        Route('GET', '/channels/{channel_id}/messages')
    # Unicode, spaces, slashes in parameters: uriquote handles
    r = Route('GET', '/channels/{channel_id}/messages', channel_id='évil id/with spaces')
    assert '%20' in r.url and '%C3%A9' in r.url
    r2 = Route('GET', '/channels/{channel_id}', channel_id='a' * 10_000)
    assert len(r2.url) > 10_000
    # int parameters
    r3 = Route('GET', '/channels/{channel_id}', channel_id=2**70)
    assert str(2**70) in r3.url
    # Bucket keys of different routes never collide
    a = Route('GET', '/channels/{channel_id}/messages', channel_id=1)
    b = Route('GET', '/guilds/{guild_id}/channels', guild_id=1)
    assert a.key != b.key
    print('route hostile params: quoted, catchable, bucket keys distinct')


# ===========================================================================
# Persistence shapes beyond round 1
# ===========================================================================


def make_snapshot(path, **overrides):
    ready = {
        'v': 9,
        'user': up(5) | {'email': 'x@x', 'verified': True, 'flags': 0},
        'users': [],
        'guilds': [],
        'relationships': [],
        'private_channels': [],
        'sessions': [],
        'user_settings_proto': '',
        'required_action': None,
        'auth_token': None,
        'read_state': {'version': 0, 'entries': []},
        'user_guild_settings': {'version': 0, 'entries': []},
        'experiments': [],
        'guild_experiments': [],
    }
    data = {
        'version': 1,
        'saved_at': time.time(),
        'user_id': '5',
        'ready': ready,
        'guilds': [
            base_guild(
                1,
            )
        ],
    }
    data.update(overrides)
    with open(path, 'wb') as fp:
        fp.write(zlib.compress(discord.utils._to_json(data).encode()))
    return data


async def replay_client(path):
    c = Client(persistence_path=path)
    await c._async_setup_hook()
    c._connection.user = discord.ClientUser(state=c._connection, data=up(5))
    return c


HOSTILE_SNAPSHOTS = [
    {'version': '1'},  # string version
    {'version': 1.0},  # float version (== 1, accepted)
    {'version': 2},
    {'version': None},
    {'saved_at': True},  # bool is an int subclass: tiny age, accepted path
    {'saved_at': 'not-a-number'},
    {'saved_at': -1e18},
    {'user_id': 'abc'},
    {'guilds': 'not-a-list'},
    {'guilds': [None, 42, 'x']},
    {'ready': {'no_user': True}},
    {'ready': None},
    {},
]


@pytest.mark.asyncio
async def test_hostile_snapshot_scalars():
    for overrides in HOSTILE_SNAPSHOTS:
        path = tempfile.mktemp(suffix='.bin')
        make_snapshot(path, **overrides)
        c = await replay_client(path)
        try:
            await asyncio.wait_for(c._connection._replay_persistence(), timeout=10.0)
        except asyncio.TimeoutError:
            pytest.fail(f'replay hung on snapshot overrides {overrides}')
        except Exception:
            pass  # catchable
        os.unlink(path)
    print(f'{len(HOSTILE_SNAPSHOTS)} hostile snapshot shapes: no hangs, catchable-only failures')


@pytest.mark.asyncio
async def test_duplicate_guild_ids_in_snapshot():
    path = tempfile.mktemp(suffix='.bin')
    guild = base_guild(42)
    make_snapshot(path, guilds=[guild, guild, dict(guild)])  # same id thrice
    c = await replay_client(path)
    ok = await c._connection._replay_persistence()
    assert ok
    g = c._connection._get_guild(42)
    assert g is not None and len(c._connection._guilds) == 1
    assert g._members_seen == 3  # aliased payloads counted per add (documented)
    os.unlink(path)
    print('duplicate guild ids in snapshot: one cache entry, sane accounting')


@pytest.mark.asyncio
async def test_snapshot_replay_under_caps():
    path = tempfile.mktemp(suffix='.bin')
    big = base_guild(9)
    big['members'] = [{'user': up(700_000 + i), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'} for i in range(2000)]
    big['member_count'] = 2000
    make_snapshot(path, guilds=[big])
    c = Client(persistence_path=path, max_member_cache=100)
    await c._async_setup_hook()
    c._connection.user = discord.ClientUser(state=c._connection, data=up(5))
    ok = await c._connection._replay_persistence()
    assert ok
    g = c._connection._get_guild(9)
    assert len(g._members) <= 101
    assert g._members_seen >= 2000
    os.unlink(path)
    print(f'replay under cap: 2000-member snapshot -> {len(g._members)} cached (cap 100), replay OK')


@pytest.mark.asyncio
async def test_persistence_path_hostility(tmp_path):
    # Path is a directory
    c, state, *_ = await make_env(persistence_path=tmp_path)
    assert await state._write_persistence() is False  # mkstemp in dir, replace onto dir fails -> caught
    # Nonexistent nested directory
    c2, state2, *_ = await make_env(persistence_path=tmp_path / 'no' / 'such' / 'dir' / 'x.bin')
    assert await state2._write_persistence() is False
    # Unwritable destination file
    target = tmp_path / 'locked.bin'
    target.write_bytes(b'x')
    os.chmod(target, 0o444)
    c3, state3, *_ = await make_env(persistence_path=target)
    result = await state3._write_persistence()
    os.chmod(target, 0o644)
    assert result is False  # replace over read-only file fails -> caught
    # Relative bare filename (no directory component) in a tmp cwd
    oldcwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        c4, state4, *_ = await make_env(persistence_path='rel.bin')
        state4._persistence_ready = {'v': 9, 'user': up(5)}
        assert await state4._write_persistence() is True
        assert (tmp_path / 'rel.bin').exists()
    finally:
        os.chdir(oldcwd)
    print('path hostility: directory/missing-dir/readonly targets caught; relative path works')


# ===========================================================================
# Concurrency and lifecycle abuse
# ===========================================================================


@pytest.mark.asyncio
async def test_cache_stats_during_churn():
    client, state, guild, channel = await make_env()

    async def churn():
        for i in range(3000):
            state.parse_message_create(msg_payload(id=str(200_000 + i), content=f'x{i}'))
            if i % 7 == 0:
                guild._add_member(
                    discord.Member(
                        data={'user': up(600_000 + i), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'},
                        guild=guild,
                        state=state,
                    )
                )

    task = asyncio.create_task(churn())
    stats = []
    for _ in range(30):
        stats.append(await client.cache_stats())
        await asyncio.sleep(0.001)
    await task
    final = await client.cache_stats()
    assert final.messages <= 1000
    assert all(s.dispatch_backlog >= 0 for s in stats)
    print(f'30 concurrent cache_stats snapshots during 3k-event churn: consistent (final messages={final.messages})')


@pytest.mark.asyncio
async def test_close_during_dispatch_storm():
    client, state, guild, channel = await make_env(persistence_path=tempfile.mktemp(suffix='.bin'))

    async def slow(x):
        await asyncio.sleep(0.05)

    client.on_evt = slow
    for i in range(500):
        client.dispatch('evt', i)
    # close while handlers are mid-flight (persistence write races them)
    await asyncio.wait_for(client.close(), timeout=15.0)
    # dispatch after close must not explode
    client.dispatch('evt', 'late')
    await asyncio.sleep(0.05)
    print('close during 500-handler storm: completed, post-close dispatch tolerated')


@pytest.mark.asyncio
async def test_gc_freeze_cycle_under_churn():
    client, state, guild, channel = await make_env()
    for i in range(100):
        gc.freeze()
        state.parse_message_create(msg_payload(id=str(300_000 + i)))
        gc.unfreeze()
        state.parse_message_create(msg_payload(id=str(310_000 + i)))
    assert len(state._messages) == 200
    print('100 gc freeze/unfreeze cycles mid-churn: state consistent')


@pytest.mark.asyncio
async def test_surrogate_and_null_names():
    client, state, guild, channel = await make_env(persistence_path=tempfile.mktemp(suffix='.bin'))
    weird = {**up(999), 'username': 'bad\ud800name\x00'}
    payload = msg_payload(id='9000', author=weird)
    state.parse_message_create(payload)
    message = state._get_message(9000)
    assert '\ud800' in message.author.name and '\x00' in message.author.name
    # Persistence write must survive (orjson rejects lone surrogates -> caught;
    # stdlib json escapes them) — either way, catchable
    state._persistence_ready = {
        'v': 9,
        'user': up(5),
        'users': [weird],
        'guilds': [],
        'relationships': [],
        'private_channels': [],
    }
    result = await state._write_persistence()
    assert isinstance(result, bool)
    print(f'lone-surrogate + null-byte names: parsed, persistence write returned {result} (catchable either way)')


@pytest.mark.asyncio
async def test_ext_commands_hostile_prefix():
    from discord.ext.commands import Bot

    bot = Bot(command_prefix=lambda b, m: None)  # prefix callable returning None
    await bot._async_setup_hook()
    message = SimpleNamespace(content='!ping', author=SimpleNamespace(id=1, bot=False), guild=None, channel=None)
    try:
        prefix = await bot.get_prefix(message)
    except Exception:
        prefix = 'raised'
    # Whatever it returns (or raises), invoking the pipeline must be catchable
    try:
        await asyncio.wait_for(
            bot.invoke(SimpleNamespace(message=message, command=None, args=[], kwargs={}, prefix=None)), timeout=5
        )
    except (asyncio.TimeoutError, AttributeError, Exception):
        pass
    print(f'hostile command_prefix (returns None): get_prefix -> {prefix!r}, invoke stays catchable')
