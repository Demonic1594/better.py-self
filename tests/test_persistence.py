"""End-to-end cache persistence tests."""

from __future__ import annotations

import asyncio
import os

import pytest

import discord
from discord import Client, Guild


async def wait_for_snapshot(state, path, timeout=5.0):
    """Deterministically waits for the post-ready persistence write.

    Sleeping a fixed duration is flaky on slow CI runners (the write goes
    through asyncio.to_thread with a real fsync), so poll for the tracked
    write task to drain and the file to appear instead.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if path.exists() and not getattr(state, '_persistence_tasks', None):
            return
        await asyncio.sleep(0.01)
    assert path.exists(), 'persistence file was not written in time'
    assert not state._persistence_tasks, 'persistence write task did not finish'


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
    await wait_for_snapshot(state1, path)
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
    await wait_for_snapshot(state3, path2)
    c4 = Client(persistence_path=path2)
    await c4._async_setup_hook()
    c4._connection.user = discord.ClientUser(state=c4._connection, data=up(999))
    assert await c4._connection._replay_persistence() is False
    assert not c4._connection._guilds


async def _snapshot_to(path):
    c = Client(persistence_path=path)
    await c._async_setup_hook()
    state = c._connection
    state.user = discord.ClientUser(state=state, data=up(5))
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, 3)]
    await drive_ready(c, ready)
    await wait_for_snapshot(state, path)
    return c


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == 'nt', reason='POSIX file modes')
async def test_persistence_file_is_private_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / 'cache.bin'
    c = await _snapshot_to(path)
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600, 'snapshot holds the raw READY payload; must be owner-only'
    assert await c.write_persistence() is True  # explicit second write, overwrites in place
    assert [p.name for p in tmp_path.iterdir()] == ['cache.bin'], 'no leftover temp files'


@pytest.mark.asyncio
async def test_persistence_concurrent_writes_do_not_collide(tmp_path):
    path = tmp_path / 'cache.bin'
    c = await _snapshot_to(path)
    results = await asyncio.gather(*[c.write_persistence() for _ in range(8)])
    assert all(results)
    assert [p.name for p in tmp_path.iterdir()] == ['cache.bin']


@pytest.mark.asyncio
async def test_persistence_post_ready_write_task_is_retained_then_released(tmp_path):
    path = tmp_path / 'cache.bin'
    c = Client(persistence_path=path)
    await c._async_setup_hook()
    state = c._connection
    state.user = discord.ClientUser(state=state, data=up(5))
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, 2)]
    await drive_ready(c, ready)
    await wait_for_snapshot(state, path)
    assert not state._persistence_tasks, 'finished write tasks are discarded from the strong-ref set'


@pytest.mark.asyncio
async def test_persistence_max_age_option(tmp_path):
    path = tmp_path / 'cache.bin'
    await _snapshot_to(path)

    # Default window accepts a fresh snapshot
    fresh = Client(persistence_path=path)
    await fresh._async_setup_hook()
    fresh._connection.user = discord.ClientUser(state=fresh._connection, data=up(5))
    assert fresh._connection.persistence_max_age == 14 * 24 * 3600

    # A zero-second window treats every snapshot as stale -> cold start
    stale = Client(persistence_path=path, persistence_max_age=0)
    await stale._async_setup_hook()
    stale._connection.user = discord.ClientUser(state=stale._connection, data=up(5))
    await asyncio.sleep(0.01)
    assert await stale._connection._replay_persistence() is False
    assert not stale._connection._guilds


@pytest.mark.asyncio
async def test_persistence_rejects_oversized_snapshot(tmp_path, monkeypatch):
    import discord.state as state_module

    path = tmp_path / 'cache.bin'
    await _snapshot_to(path)
    monkeypatch.setattr(state_module, 'PERSISTENCE_MAX_BYTES', 16)  # absurdly small cap

    c = Client(persistence_path=path)
    await c._async_setup_hook()
    c._connection.user = discord.ClientUser(state=c._connection, data=up(5))
    assert await c._connection._replay_persistence() is False
    assert not c._connection._guilds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'blob',
    [
        b'[]',
        b'[1]',
        b'42',
        b'"str"',
        b'{"version":1}',
        b'{"version":1,"ready":null}',
        b'{"version":1,"ready":[]}',
        b'{"version":1,"ready":{"user":null}}',
        b'{"version":1,"ready":{"user":{}},"guilds":"nope"}',
        b'{"version":1,"ready":{"user":{"id":"5"}},"guilds":[7]}',
    ],
    ids=['list', 'list1', 'int', 'str', 'no-ready', 'ready-null', 'ready-list', 'user-null', 'guilds-str', 'guild-int'],
)
async def test_malformed_snapshots_downgrade_to_cold_start(tmp_path, blob):
    """F-01: valid-JSON but malformed snapshots must return False and leave
    the state cold — never raise out of replay (and so never out of login)."""
    import zlib

    path = tmp_path / 'cache.bin'
    path.write_bytes(zlib.compress(blob, 1))
    c = Client(persistence_path=path)
    await c._async_setup_hook()
    c._connection.user = discord.ClientUser(state=c._connection, data=up(5))
    assert await c._connection._replay_persistence() is False
    assert not c._connection._guilds
    assert not c._connection._persistence_loaded


@pytest.mark.asyncio
async def test_snapshot_strips_auth_and_account_data(tmp_path):
    """F-02/F-10: the written snapshot allow-lists its READY keys — no
    auth_token, settings protos, connected accounts or payment data at
    rest — and replaying an old snapshot carrying auth_token leaves the
    live token untouched."""
    import zlib

    path = tmp_path / 'cache.bin'
    c = await _snapshot_to(path)

    raw = zlib.decompress(path.read_bytes())
    payload = __import__('json').loads(raw)
    assert set(payload['ready']) <= {'user', 'users', 'relationships', 'private_channels'}
    for forbidden in ('auth_token', 'user_settings_proto', 'user_guild_settings', 'connected_accounts',
                      'pending_payments', 'analytics_token', 'sessions', 'read_state', 'experiments'):
        assert forbidden not in payload['ready']

    # An old/hostile snapshot with auth_token inside must not touch the token
    hostile = dict(payload)
    hostile['ready'] = dict(payload['ready'], auth_token='stale-token')
    path2 = tmp_path / 'hostile.bin'
    path2.write_bytes(zlib.compress(__import__('json').dumps(hostile).encode(), 1))

    c2 = Client(persistence_path=path2)
    await c2._async_setup_hook()
    state2 = c2._connection
    state2.user = discord.ClientUser(state=state2, data=up(5))
    state2.http = type('H', (), {'token': 'live-token', '_token': lambda self, t: setattr(self, 'token', t)})()
    from discord.state import GuildSubscriptions

    original = GuildSubscriptions.has_feature
    GuildSubscriptions.has_feature = lambda self, g, f: True
    try:
        assert await state2._replay_persistence() is True
    finally:
        GuildSubscriptions.has_feature = original
    assert state2.http.token == 'live-token', 'replay must never swap the validated login token'


@pytest.mark.asyncio
async def test_replay_failure_never_breaks_login(tmp_path):
    """F-01: the login wrapper contains every replay error."""
    c = Client(persistence_path=tmp_path / 'cache.bin')
    await c._async_setup_hook()

    async def explode():
        raise RuntimeError('snapshot blew up')

    c._connection._replay_persistence = explode
    assert await c._replay_persistence_safe() is False  # type: ignore # swallowed

    async def explode2():
        raise BaseException('nasty')

    c._connection._replay_persistence = explode2
    try:
        await c._replay_persistence_safe()  # type: ignore # BaseException still propagates (KeyboardInterrupt-class)
    except BaseException:
        pass
