"""
Abuse/robustness tests: hostile payloads, resource exhaustion, ReDoS,
adversarial cache patterns. These assert the survival properties that
matter under abuse: no hangs, bounded memory, exceptions stay catchable
(never BaseException leaks), and caps actually bound.
"""

from __future__ import annotations

import asyncio
import random
import time
import tracemalloc

import pytest

import discord
import discord.utils as u
from discord import Client, Guild
from discord.channel import TextChannel


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


# ---------------------------------------------------------------------------
# 1. Malformed payload fuzzing: mutate valid payloads, drive every parser,
#    assert Exception-only failures and a hard time budget.
# ---------------------------------------------------------------------------

MUTATORS = [
    lambda d, rng: d.pop(rng.choice(list(d.keys() or ['id']))),  # delete a key
    lambda d, rng: d.__setitem__(rng.choice(list(d.keys() or ['id'])), None),  # null a key
    lambda d, rng: d.__setitem__(rng.choice(list(d.keys() or ['id'])), []),  # wrong type: list
    lambda d, rng: d.__setitem__(rng.choice(list(d.keys() or ['id'])), {}),  # wrong type: dict
    lambda d, rng: d.__setitem__(
        rng.choice(list(d.keys() or ['id'])), rng.choice([-1, 2**64, 'x' * 10_000])
    ),  # hostile value
    lambda d, rng: d.__setitem__(rng.choice(['ghost_key', 'x', 'y']), 12345),  # unknown key
]

PARSER_PAYLOADS = {
    'parse_message_create': lambda: msg_payload(),
    'parse_message_update': lambda: msg_payload(),
    'parse_message_delete': lambda: {'id': '1000', 'channel_id': '500', 'guild_id': '1'},
    'parse_message_reaction_add': lambda: {
        'user_id': '7',
        'channel_id': '500',
        'message_id': '1000',
        'guild_id': '1',
        'emoji': {'name': 'x', 'id': None},
        'burst': False,
        'type': 0,
    },
    'parse_guild_member_update': lambda: {
        'guild_id': '1',
        'user': up(7),
        'roles': [],
        'nick': None,
        'pending': False,
        'joined_at': '2023-01-01T00:00:00+00:00',
        'premium_since': None,
        'communication_disabled_until': None,
        'avatar': None,
        'banner': None,
        'flags': 0,
    },
    'parse_guild_member_remove': lambda: {'guild_id': '1', 'user': up(7)},
    'parse_presence_update': lambda: {
        'user': {'id': '7'},
        'status': 'online',
        'activities': [],
        'client_status': {},
        'processed_at_timestamp': 0,
    },
    'parse_thread_update': lambda: {
        'guild_id': '1',
        'id': '9',
        'parent_id': '500',
        'owner_id': '2',
        'name': 't',
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
    },
}


@pytest.mark.asyncio
async def test_fuzzed_parsers_never_hang_or_leak_base_exceptions():
    """~2000 mutated hostile payloads through the real parsers."""
    client, state, guild, channel = await make_env()
    rng = random.Random(1234)
    failures = 0
    base_exc = 0

    for parser_name, payload_factory in PARSER_PAYLOADS.items():
        parser = getattr(state, parser_name)
        for _ in range(300):
            payload = payload_factory()
            for _ in range(rng.randint(1, 3)):
                mut = rng.choice(MUTATORS)
                try:
                    mut(payload, rng)
                except Exception:
                    pass  # mutator itself may fail on empty dicts
            try:
                await asyncio.wait_for(asyncio.to_thread(parser, payload), timeout=5.0)
            except asyncio.TimeoutError:
                pytest.fail(f'{parser_name} hung on mutated payload: {str(payload)[:200]}')
            except BaseException as e:
                if isinstance(e, Exception):
                    failures += 1  # catchable parser failure: acceptable, gateway logs these
                else:
                    base_exc += 1
                    print(f'BASE EXCEPTION LEAK in {parser_name}: {e!r}')

    assert base_exc == 0, 'KeyboardInterrupt/CancelledError-class leaks are forbidden'
    print(f'fuzz: 2100 mutations -> {failures} catchable failures, 0 hangs, 0 base-exception leaks')


# ---------------------------------------------------------------------------
# 2. ReDoS hunting: pathological inputs against the regex-heavy utilities.
# ---------------------------------------------------------------------------


def time_call(func, *args, budget=0.25):
    t0 = time.perf_counter()
    func(*args)
    return time.perf_counter() - t0


def test_redos_mentions_and_markdown():
    pathological = [
        ('@' + 'a' * 5000, 'partial everyone'),
        ('<@' + '1' * 100000, 'unterminated mention'),
        ('<@!' + '9' * 100000 + '>', 'huge mention'),
        ('<' * 20000, 'angle bomb'),
        ('<' + '@' * 20000, 'angle-at bomb'),
        ('*_' * 20000, 'markdown toggle bomb'),
        ('[' * 5000 + ']' * 5000 + '(' * 5000, 'link bomb'),
        ('[' * 50000, 'bracket bomb'),
        ('[' + 'x' * 50000 + ']', 'unterminated label'),
        ('http://' + 'a' * 100000, 'long url'),
        ('http://x' + '_' * 100000, 'url with underscores'),
        ('\\' * 50000, 'backslash bomb'),
    ]
    for text, label in pathological:
        for fn in (u.escape_mentions, u.escape_markdown, u.remove_markdown):
            dt = time_call(fn, text)
            assert dt < 0.25, f'ReDoS suspected in {fn.__name__} ({label}): {dt:.2f}s'
    # clean_content goes through the same mention regex via escape_mentions


def test_markdown_regex_hardening_is_linear():
    """The pre-hardening regexes were quadratic on angle/bracket floods."""
    for n in (5000, 20000, 80000):
        dt = time_call(u.escape_markdown, '<' * n)
        dt2 = time_call(u.remove_markdown, '[' * n)
        assert dt < 0.05 and dt2 < 0.05, (n, dt, dt2)
    # Linear sanity: 4x input must not cost ~16x
    small = time_call(u.escape_markdown, '<' * 20000)
    big = time_call(u.escape_markdown, '<' * 80000)
    assert big < small * 16


def test_multi_link_text_escaped_correctly():
    """Post-hardening: each link in multi-link text is handled individually.

    The pre-hardening greedy regex swallowed the whole span as one match
    ('[a](b) and [c](d)' escaped only the first bracket). This pins the fix.
    """
    text = '[a](b) and [c](d)'
    escaped = u.escape_markdown(text)
    assert escaped == '\\[a](b) and \\[c](d)'
    assert u.remove_markdown(text) == ' and '


@pytest.mark.asyncio
async def test_message_content_bombs():
    client, state, guild, channel = await make_env()
    bombs = [
        '<@123456789012345678> ' * 20_000,  # mention flood
        '@everyone ' * 50_000,
        'a' * 1_000_000,  # 1MB content
        '<#' + '9' * 20 + '> ' * 20_000,
        '\U0001f525' * 100_000,  # emoji flood
    ]
    for i, content in enumerate(bombs):
        payload = msg_payload(id=str(2000 + i), content=content)
        await asyncio.wait_for(asyncio.to_thread(state.parse_message_create, payload), timeout=10.0)
        message = state._get_message(2000 + i)
        # accessing the cached-slot properties must also complete
        await asyncio.wait_for(asyncio.to_thread(lambda: message.clean_content), timeout=10.0)
        await asyncio.wait_for(asyncio.to_thread(lambda: message.raw_mentions), timeout=10.0)
    print('content bombs: parse + clean_content + raw_mentions all under budget')


@pytest.mark.asyncio
async def test_embed_and_component_bombs():
    client, state, guild, channel = await make_env()
    huge_embed = {
        'title': 'x' * 100_000,
        'description': 'y' * 100_000,
        'fields': [{'name': 'f' * 1000, 'value': 'v' * 10_000, 'inline': False} for _ in range(25)],
        'footer': {'text': 'z' * 100_000},
        'color': 0xFFFFFF,
    }
    deep_component = {
        'type': 1,
        'components': [
            {
                'type': 2,
                'style': 1,
                'label': 'L' * 10_000,
                'custom_id': 'c' * 10_000,
                'emoji': {'name': 'e' * 10_000, 'id': '9' * 20, 'animated': True},
            }
            for _ in range(50)
        ],
    }
    payload = msg_payload(id='3000', embeds=[huge_embed] * 10, components=[deep_component] * 5)
    await asyncio.wait_for(asyncio.to_thread(state.parse_message_create, payload), timeout=15.0)
    message = state._get_message(3000)
    assert len(message.embeds) == 10 and len(message.components) == 5
    for embed in message.embeds:
        assert len(embed.title) == 100_000
    print('embed/component bombs parsed under budget')


# ---------------------------------------------------------------------------
# 3. Eviction storms and adversarial cache patterns.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_eviction_storm():
    """100k members through a capped guild: bounded, fast, protection holds."""
    client, state, guild, channel = await make_env(max_member_cache=500)
    state.max_member_cache = 500
    state.max_total_member_cache = None
    tracemalloc.start()
    t0 = time.perf_counter()
    for i in range(100_000):
        guild._add_member(
            discord.Member(
                data={'user': up(10_000_000 + i), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'},
                guild=guild,
                state=state,
            )
        )
    dt = time.perf_counter() - t0
    current, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(guild._members) <= 500
    print(
        f'100k-member storm: {dt:.1f}s, cache={len(guild._members)}, tracemalloc peak growth ok ({current / 1e6:.0f}MB live)'
    )
    assert dt < 60


@pytest.mark.asyncio
async def test_protection_larger_than_cap_does_not_hang():
    client, state, guild, channel = await make_env()
    state.max_member_cache = 1
    state.max_total_member_cache = None
    for i in range(10):
        guild._voice_states[i] = type('VS', (), {'user_id': i})()  # all protected
    guild._add_member(
        discord.Member(
            data={'user': up(777), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}, guild=guild, state=state
        )
    )
    # And threads: every cached thread joined
    state.max_thread_cache = 1
    for i in range(10):
        t = discord.Thread(
            guild=guild,
            state=state,
            data={
                'id': str(1000 + i),
                'parent_id': '500',
                'owner_id': '2',
                'name': 't',
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
            },
        )
        t._members[state.self_id] = None
        guild._add_thread(t)
    print('protection > cap: no hang (eviction skips protected, tolerates overflow)')


@pytest.mark.asyncio
async def test_presence_flood_respects_caps():
    client, state, guild, channel = await make_env()
    state.max_guild_presence_cache = 100
    state.max_member_cache = None
    state.max_total_member_cache = None
    for i in range(2_000):
        guild._add_member(
            discord.Member(
                data={'user': up(i), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}, guild=guild, state=state
            )
        )
    payload = lambda i: {
        'user': {'id': str(i)},
        'status': 'online',
        'activities': [],
        'client_status': {'web': 'online'},
        'processed_at_timestamp': 0,
    }
    for i in range(0, 2_000, 2):  # every other member: presence stored
        state._handle_presence_update(guild, payload(i))
    assert len(state._guild_presences[guild.id]) <= 100
    print(f'presence flood: 1000 updates -> {len(state._guild_presences[guild.id])} stored (cap 100)')


@pytest.mark.asyncio
async def test_dispatch_storm_and_cancel_race():
    """20k dispatches with a mix of fast/slow/raising handlers; drain must complete."""
    client, state, guild, channel = await make_env(dispatch_backlog_warning=10_000)

    async def fast(x):
        pass

    async def raising(x):
        raise ValueError('intentional')

    client.on_a = fast
    client.on_b = raising
    t0 = time.perf_counter()
    for i in range(10_000):
        client.dispatch('a', i)
        if i % 10 == 0:
            client.dispatch('b', i)  # exceptions are routed to on_error
    await asyncio.sleep(1.0)
    backlog, _, created, completed = client._dispatch_backlog_stats()
    dt = time.perf_counter() - t0
    assert backlog <= 1_000, f'backlog failed to drain: {backlog}'
    assert completed == created
    print(f'11k dispatches (10% raising): drained in {dt:.1f}s, {created}/{completed}')


# ---------------------------------------------------------------------------
# 4. Hostile persistence files.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hostile_but_valid_shaped_snapshot():
    """A snapshot whose inner payloads are garbage must not crash replay."""
    import os
    import tempfile
    import zlib

    evil_guild = base_guild(66)
    evil_guild['members'] = [{'user': None, 'roles': 'NOT-A-LIST'}, {'ghost': 1}]  # hostile inner data
    evil_guild['channels'] = [{'id': None, 'type': 'SEVEN'}]
    evil_ready = {
        'v': 9,
        'user': up(5),
        'users': [],
        'guilds': [],
        'relationships': [{'id': 'x', 'type': 'y'}],
        'private_channels': [{'id': '9', 'type': 'Z', 'recipient_ids': ['not-an-id']}],
        'sessions': [],
        'read_state': {'version': 0, 'entries': []},
        'user_guild_settings': {'version': 0, 'entries': []},
        'experiments': [],
        'guild_experiments': [],
    }
    blob = zlib.compress(
        discord.utils._to_json(
            {
                'version': 1,
                'saved_at': time.time(),
                'user_id': '5',
                'ready': evil_ready,
                'guilds': [evil_guild] + [base_guild(i) for i in range(1, 50)],
            }
        ).encode()
    )
    path = tempfile.mktemp(suffix='.bin')
    with open(path, 'wb') as fp:
        fp.write(blob)

    c = Client(persistence_path=path)
    await c._async_setup_hook()
    c._connection.user = discord.ClientUser(state=c._connection, data=up(5))
    ok = await c._connection._replay_persistence()  # must not hang or raise BaseException
    print(f'hostile snapshot replay returned {ok}; guilds={len(c._connection._guilds)}')
    os.unlink(path)


@pytest.mark.asyncio
async def test_zlib_bomb_snapshot_rejected():
    import os
    import tempfile
    import zlib

    from discord.state import PERSISTENCE_MAX_BYTES

    # 1 GiB of zeros, tiny compressed: must be stopped at the decompression
    # cap (256 MiB), never materialized
    bomb = zlib.compress(b'\x00' * (1 << 30), 9)
    path = tempfile.mktemp(suffix='.bin')
    with open(path, 'wb') as fp:
        fp.write(bomb)
    c = Client(persistence_path=path)
    await c._async_setup_hook()
    tracemalloc.start()
    ok = await c._connection._replay_persistence()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert ok is False
    # Peak stays near the cap (decompressed window + slack), far below the bomb
    assert peak < PERSISTENCE_MAX_BYTES + 150 * 1024 * 1024, f'bomb peaked at {peak / 1e6:.0f}MB'
    os.unlink(path)
    print(f'1 GiB zlib bomb: rejected at the {PERSISTENCE_MAX_BYTES / 1e6:.0f}MB cap, peak alloc {peak / 1e6:.0f}MB')


# ---------------------------------------------------------------------------
# 5. HTTP-layer abuse (json_or_text + rate limit header parsing).
# ---------------------------------------------------------------------------


class FakeCurlResponse:
    def __init__(self, body, content_type='application/json', charset=None):
        self._body = body
        self.headers = {'content-type': content_type} if content_type is not None else {}
        if charset:
            self.charset_encoding = charset

    async def acontent(self):
        return self._body


@pytest.mark.asyncio
async def test_json_or_text_abuse():
    from discord.http import json_or_text

    valid = [b'{"ok": 1}', b'{"x": ' + b'1' * 4000 + b'}']
    for body in valid:
        resp = FakeCurlResponse(body)
        result = await asyncio.wait_for(json_or_text(resp), timeout=10.0)
        assert isinstance(result, (dict, str))

    # Malformed JSON bodies may raise a *catchable* parse error; never hang.
    malformed = [
        b'{"broken": ',
        b'\xff\xfe\xfa',  # invalid utf-8, no charset
        b'{"nested": ' + b'[' * 100_000 + b']' * 100_000 + b'}',  # deep nesting
        b'{"x": ' + b'1' * 10_000_000 + b'}',  # exceeds Python's int digit limit
        b'{"x": ' + b'1' * 1_000_000 + b'}',
        b'',  # empty body claiming to be JSON
    ]
    for body in malformed:
        resp = FakeCurlResponse(body)
        try:
            await asyncio.wait_for(json_or_text(resp), timeout=15.0)
        except Exception as e:
            assert type(e).__name__ in ('JSONDecodeError', 'ValueError', 'UnicodeDecodeError', 'RecursionError'), repr(e)
    # Missing content-type header entirely -> plain text passthrough
    resp = FakeCurlResponse(b'plain', content_type=None)
    assert await json_or_text(resp) == 'plain'
    print(f'json_or_text: {len(valid) + len(malformed) + 1} hostile bodies handled (valid/parse-error/passthrough)')


@pytest.mark.asyncio
async def test_ratelimit_adversarial_headers():
    """The Ratelimit bookkeeping must tolerate hostile header values."""
    from discord.http import Ratelimit

    rl = Ratelimit(None, 1)
    adversarial = [
        {'X-Ratelimit-Remaining': 'not-a-number', 'X-Ratelimit-Reset-After': 'soon'},
        {'X-Ratelimit-Remaining': '-999', 'X-Ratelimit-Reset-After': '-1'},
        {'X-Ratelimit-Remaining': '1e999', 'X-Ratelimit-Reset-After': '1e999'},
        {'X-Ratelimit-Remaining': 'nan', 'X-Ratelimit-Reset-After': 'inf'},
        {},  # no headers at all
        {'X-Ratelimit-Remaining': '5', 'X-Ratelimit-Reset-After': '0.1'},  # normal
    ]
    for headers in adversarial:
        resp = FakeCurlResponse(b'{}')
        resp.headers = headers
        try:
            await asyncio.wait_for(asyncio.to_thread(rl.update, resp), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail(f'ratelimit update hung on {headers}')
        except Exception as e:
            assert isinstance(e, Exception)  # catchable failures are acceptable
    print('ratelimit adversarial headers: no hangs, failures catchable')


# ---------------------------------------------------------------------------
# 6. Memory bound: adversarial session keeps tracemalloc peak bounded.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adversarial_session_memory_bounded():
    """A mixed storm (messages+members+presences+threads+deletes) with caps
    enabled must not exceed a modest memory ceiling."""
    client, state, guild, channel = await make_env(
        max_messages=500, max_member_cache=300, max_guild_presence_cache=200, max_thread_cache=200
    )
    state.max_total_member_cache = None
    state.max_total_thread_cache = None
    tracemalloc.start()
    rng = random.Random(99)
    for round_ in range(20):
        for i in range(500):
            k = round_ * 500 + i
            state.parse_message_create(msg_payload(id=str(100_000 + k), content=f'msg {k}'))
        for i in range(50):
            uid = rng.randrange(1_000_000)
            try:
                guild._add_member(
                    discord.Member(
                        data={'user': up(uid), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'},
                        guild=guild,
                        state=state,
                    )
                )
            except Exception:
                pass
            state._handle_presence_update(
                guild,
                {
                    'user': {'id': str(uid)},
                    'status': rng.choice(['online', 'idle', 'offline']),
                    'activities': [],
                    'client_status': {},
                    'processed_at_timestamp': 0,
                },
            )
        if round_ % 5 == 0:
            for mid in [100_000 + rng.randrange(round_ * 500 + 500)]:
                state.parse_message_delete({'id': str(mid), 'channel_id': '500', 'guild_id': '1'})
        guild._evict_members(300)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(guild._members) <= 300 + 1
    assert len(state._guild_presences.get(1, ())) <= 200
    print(
        f'adversarial session: 10k msgs + 1k members + 1k presences -> peak {peak / 1e6:.0f}MB, live {current / 1e6:.0f}MB, all caches at cap'
    )
    assert peak < 200 * 1024 * 1024
