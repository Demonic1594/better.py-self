"""
Heavy-load stress benchmark (offline, no network, no token).

Feeds pre-serialised gateway JSON frames through the real
``DiscordWebSocket.received_message`` -> parser -> ``Client.dispatch`` path of
a Client holding a realistically shaped 100-guild account, and reports what
that costs. It measures the library's own CPU and memory, not socket I/O.

Phases (``--phase`` selects, default ``all``):

``throughput``  mixed event stream, with and without user handlers
``soak``        long churning stream; cache caps must hold and RSS must plateau
``backlog``     slow handlers under a burst; backlog must drain and memory recover
``multi``       many accounts in one process; per-account RSS cost, then load
``persist``     snapshot write/replay of a large member cache
``bomb``        malformed / hostile frames at volume; nothing may crash or leak

Usage::

    python tools/stress_bench.py                       # moderate defaults
    python tools/stress_bench.py --heavy               # several million events
    python tools/stress_bench.py --phase soak --events 5000000 --json out.json

Exit status is non-zero when a gate fails (cap exceeded, RSS still growing,
unexpected warnings, backlog not drained). Not collected by pytest.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import gc
import json
import logging
import os
import random
import resource
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import discord  # noqa: E402
import discord.gateway as gateway  # noqa: E402
import memory_bench as mb  # noqa: E402

DISCORD_EPOCH_MS = 1420070400000
PAGE = os.sysconf('SC_PAGE_SIZE')
FAILURES: List[str] = []


def rss_mib() -> float:
    with open('/proc/self/statm') as f:
        return int(f.read().split()[1]) * PAGE / 1048576


def peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def gate(ok: bool, message: str) -> None:
    print(f'  [{"PASS" if ok else "FAIL"}] {message}')
    if not ok:
        FAILURES.append(message)


class WarningCounter(logging.Handler):
    """Counts WARNING+ records from the library, ignoring known fixture noise."""

    IGNORE = ('PyNaCl is not installed', 'davey is not installed', 'User has no sessions')

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.count = 0
        self.samples: List[str] = []
        self.logger = logging.getLogger('discord')
        # The logger must actually let WARNING through, whatever the root level is
        self._old_level = self.logger.level
        self.logger.setLevel(logging.WARNING)
        self.logger.addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(self.IGNORE):
            return
        self.count += 1
        if len(self.samples) < 3:
            self.samples.append(message[:160])

    def detach(self) -> None:
        self.logger.removeHandler(self)
        self.logger.setLevel(self._old_level)


class FakeSocket:
    closed = False
    close_code = None
    close_reason = ''

    async def recv(self):
        await asyncio.sleep(3600)

    async def send(self, data):
        pass

    async def close(self, code=None, reason=b''):
        self.closed = True


# --------------------------------------------------------------------------- #
# Frame generation (never inside a timed region)
# --------------------------------------------------------------------------- #


class Frames:
    """Seeded generator of realistic gateway frames across the first N guilds."""

    MIX = (
        ('MESSAGE_CREATE', 40),
        ('PRESENCE_UPDATE', 25),
        ('TYPING_START', 12),
        ('GUILD_MEMBER_UPDATE', 8),
        ('MESSAGE_UPDATE', 6),
        ('MESSAGE_REACTION_ADD', 5),
        ('MESSAGE_DELETE', 4),
    )

    def __init__(self, guilds: int, user_pool: int, seed: int = 1) -> None:
        self.rng = random.Random(seed)
        self.guilds = guilds
        self.user_pool = user_pool
        self.seq = 0
        self.msg_ms = int((time.time() * 1000) - DISCORD_EPOCH_MS) - 10_000_000
        self.recent: List[tuple] = []
        self.kinds = [k for k, w in self.MIX for _ in range(w)]

    def _channel(self):
        gid = self.rng.randint(1, self.guilds)
        c = self.rng.randrange(1, mb.CHANNELS)
        while c % 10 in (0, 9):
            c = self.rng.randrange(1, mb.CHANNELS)
        return gid, gid * 10_000 + 1000 + c

    def _user(self) -> dict:
        # 10% hot users, 90% long tail: realistic churn for the LRU caps
        r = self.rng
        uid = 1_000_000 + (r.randrange(self.user_pool // 10) if r.random() < 0.1 else r.randrange(self.user_pool))
        return mb._user(uid)

    def _member(self, gid: int, user: dict) -> dict:
        base = gid * 10_000
        return {
            'roles': [str(base + 1 + self.rng.randrange(7))],
            'joined_at': '2023-01-01T00:00:00.123000+00:00',
            'premium_since': None,
            'communication_disabled_until': None,
            'nick': None,
            'pending': False,
            'flags': 0,
            'avatar': None,
            'banner': None,
        }

    def _message(self, gid: int, cid: int) -> dict:
        self.msg_ms += self.rng.randrange(1, 40)
        mid = (self.msg_ms << 22) | (self.seq & 0x3FFFFF)
        user = self._user()
        data = {
            'id': str(mid),
            'channel_id': str(cid),
            'guild_id': str(gid),
            'author': user,
            'member': self._member(gid, user),
            'content': 'hello world ' * self.rng.randrange(1, 12),
            'timestamp': '2025-01-01T00:00:00.000000+00:00',
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
        self.recent.append((gid, cid, mid))
        if len(self.recent) > 2000:
            del self.recent[:1000]
        return data

    def frame(self, kind: Optional[str] = None) -> str:
        r = self.rng
        kind = kind or r.choice(self.kinds)
        self.seq += 1
        gid, cid = self._channel()
        if kind == 'MESSAGE_CREATE':
            d: Any = self._message(gid, cid)
        elif kind == 'PRESENCE_UPDATE':
            u = self._user()
            d = {
                'user': {'id': u['id']},
                'guild_id': str(gid),
                'status': r.choice(('online', 'idle', 'dnd', 'offline')),
                'activities': [],
                'client_status': {'desktop': 'online'},
            }
        elif kind == 'TYPING_START':
            u = self._user()
            d = {
                'channel_id': str(cid),
                'guild_id': str(gid),
                'user_id': u['id'],
                'timestamp': int(time.time()),
                'member': {'user': u, **self._member(gid, u)},
            }
        elif kind == 'GUILD_MEMBER_UPDATE':
            u = self._user()
            d = {'guild_id': str(gid), 'user': u, **self._member(gid, u)}
        elif kind in ('MESSAGE_UPDATE', 'MESSAGE_REACTION_ADD', 'MESSAGE_DELETE'):
            if self.recent:
                gid, cid, mid = r.choice(self.recent)
            else:
                mid = 1
            if kind == 'MESSAGE_UPDATE':
                d = self._message(gid, cid)
                d['id'] = str(mid)
                d['edited_timestamp'] = '2025-01-01T00:00:01.000000+00:00'
            elif kind == 'MESSAGE_REACTION_ADD':
                u = self._user()
                d = {
                    'user_id': u['id'],
                    'channel_id': str(cid),
                    'message_id': str(mid),
                    'guild_id': str(gid),
                    'emoji': {'id': None, 'name': '\U0001f44d'},
                    'burst': False,
                    'type': 0,
                    'member': {'user': u, **self._member(gid, u)},
                }
            else:
                d = {'id': str(mid), 'channel_id': str(cid), 'guild_id': str(gid)}
        else:
            raise ValueError(kind)
        return json.dumps({'op': 0, 't': kind, 's': self.seq, 'd': d})

    def batch(self, n: int, kind: Optional[str] = None) -> List[str]:
        return [self.frame(kind) for _ in range(n)]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class Rig:
    """A Client with a full READY applied and a websocket wired to its parsers."""

    def __init__(self, client: discord.Client, ws: gateway.DiscordWebSocket) -> None:
        self.client = client
        self.ws = ws
        self.state = client._connection

    @classmethod
    async def create(cls, ready: dict, **client_kwargs: Any) -> 'Rig':
        client = discord.Client(**client_kwargs)
        await client._async_setup_hook()
        state = client._connection
        state.user = discord.ClientUser(state=state, data=mb._user(5))
        await mb._drive(client, copy.deepcopy(ready))
        ws = gateway.DiscordWebSocket(FakeSocket(), loop=asyncio.get_running_loop())
        ws._dispatch = client.dispatch
        ws.log_receive = lambda *a: None
        ws._discord_parsers = state.parsers
        ws._dispatch_listeners = []
        ws.token = 'tok'
        client.ws = ws
        return cls(client, ws)

    async def feed(self, frames: List[str], yield_every: int = 64) -> float:
        """Feeds frames; yields to the loop every ``yield_every`` like a real recv loop."""
        recv = self.ws.received_message
        sleep = asyncio.sleep
        t0 = time.perf_counter()
        for i, f in enumerate(frames):
            await recv(f)
            if i % yield_every == 0:
                await sleep(0)
        return time.perf_counter() - t0

    async def close(self) -> None:
        self.state.subscriptions._cancel()
        await self.client.close()


def install_handlers(client: discord.Client, delay: float = 0.0) -> Dict[str, int]:
    counts = {'message': 0, 'typing': 0, 'presence': 0, 'member': 0, 'edit': 0, 'react': 0}

    def make(name: str) -> Callable[..., Any]:
        async def handler(*a: Any) -> None:
            counts[name] += 1
            if delay:
                await asyncio.sleep(delay)

        return handler

    client.on_message = make('message')  # type: ignore[attr-defined]
    client.on_typing = make('typing')  # type: ignore[attr-defined]
    client.on_presence_update = make('presence')  # type: ignore[attr-defined]
    client.on_member_update = make('member')  # type: ignore[attr-defined]
    client.on_message_edit = make('edit')  # type: ignore[attr-defined]
    client.on_reaction_add = make('react')  # type: ignore[attr-defined]
    return counts


def caps_report(stats: Any) -> Dict[str, Any]:
    return {
        'members': stats.members,
        'threads': stats.threads,
        'guild_presences': stats.guild_presences,
        'messages': stats.messages,
        'evicted_members': stats.evicted_members,
        'evicted_presences': stats.evicted_presences,
        'backlog': stats.dispatch_backlog,
        'loop_lag_ms': round(stats.event_loop_lag * 1000, 2),
    }


async def drain(client: discord.Client, timeout: float = 120.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        backlog = client._dispatch_backlog_stats()[0]
        if backlog == 0:
            return True
        await asyncio.sleep(0.05)
    return False


def fmt_rate(n: int, secs: float) -> str:
    return f'{n / secs:,.0f} ev/s ({secs / n * 1e6:.1f} us/ev)'


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #


async def phase_throughput(args: argparse.Namespace, ready: dict) -> dict:
    print('\n== throughput: mixed stream through received_message ==')
    out: dict = {}
    for label, handlers in (('no handlers', False), ('with handlers', True)):
        rig = await Rig.create(ready)
        counts = install_handlers(rig.client) if handlers else {}
        gen = Frames(args.guilds, args.user_pool, seed=7)
        total = args.throughput_events
        chunk = 20_000
        elapsed = 0.0
        lat: List[float] = []
        gc.collect()
        for _ in range(total // chunk):
            frames = gen.batch(chunk)
            dt = await rig.feed(frames)
            elapsed += dt
            lat.append(dt / chunk * 1e6)
        await drain(rig.client)
        lat.sort()
        stats = await rig.client.cache_stats()
        print(
            f'  {label:14s} {fmt_rate(total, elapsed)}  p50 {lat[len(lat) // 2]:.1f} us  '
            f'p99 {lat[int(len(lat) * 0.99) - 1]:.1f} us  handlers={sum(counts.values())}'
        )
        out[label] = {'events_per_s': total / elapsed, 'p50_us': lat[len(lat) // 2], 'p99_us': lat[-1], **caps_report(stats)}
        await rig.close()
    return out


async def phase_soak(args: argparse.Namespace, ready: dict) -> dict:
    print(f'\n== soak: {args.events:,} churning events, caps must hold, RSS must plateau ==')
    counter = WarningCounter()
    rig = await Rig.create(
        ready,
        max_member_cache=args.cap_guild_members,
        max_total_member_cache=args.cap_total_members,
        max_guild_presence_cache=2000,
        max_messages=1000,
    )
    install_handlers(rig.client)
    gen = Frames(args.guilds, args.user_pool, seed=11)
    chunk = 20_000
    chunks = args.events // chunk
    samples: List[tuple] = []  # (events, rss, members, messages, evicted_members)
    peak_members = peak_msgs = peak_pres = 0
    elapsed = 0.0
    gc.collect()
    start_rss = rss_mib()
    for i in range(chunks):
        frames = gen.batch(chunk)
        elapsed += await rig.feed(frames)
        await drain(rig.client)
        if (i + 1) % max(1, chunks // 20) == 0 or i == chunks - 1:
            gc.collect()
            stats = await rig.client.cache_stats()
            peak_members = max(peak_members, stats.members)
            peak_msgs = max(peak_msgs, stats.messages)
            peak_pres = max(peak_pres, stats.guild_presences)
            samples.append(((i + 1) * chunk, rss_mib(), stats.members, stats.messages, stats.evicted_members))
    done = chunks * chunk
    print(f'  fed {done:,} events in {elapsed:.1f}s -> {fmt_rate(done, elapsed)}')
    print('  events      RSS MiB   members  messages')
    for ev, rss, mem, msgs, _ in samples[:: max(1, len(samples) // 8)]:
        print(f'  {ev:10,d}  {rss:8.1f}  {mem:8d}  {msgs:8d}')
    # Judge the plateau only once the caps are saturated (evictions have begun):
    # before that, RSS legitimately grows while the caches fill.
    saturated = [x for x in samples if x[4] > 0]
    window = saturated[len(saturated) // 2 :] if len(saturated) >= 4 else []
    growth = (window[-1][1] - window[0][1]) if window else 0.0
    print(f'  start RSS {start_rss:.1f} MiB, final {samples[-1][1]:.1f} MiB, peak {peak_rss_mib():.1f} MiB')
    gate(
        peak_members <= args.cap_total_members * 1.06,
        f'member cache bounded ({peak_members:,} <= cap {args.cap_total_members:,} + 6% batch slack)',
    )
    gate(peak_msgs <= 1000 + 1, f'message cache bounded ({peak_msgs} <= 1000)')
    gate(peak_pres <= 2000 * args.guilds, f'presence cache bounded ({peak_pres:,})')
    if window:
        gate(
            growth <= args.plateau_mib,
            f'RSS plateau after saturation: {window[0][0]:,}->{window[-1][0]:,} events grew {growth:+.1f} MiB <= {args.plateau_mib} MiB',
        )
    else:
        print('  [SKIP] plateau gate: caches never saturated; raise --events or lower --cap-total-members')
    gate(counter.count == 0, f'no library warnings during soak (saw {counter.count}) {counter.samples}')
    counter.detach()
    stats = await rig.client.cache_stats()
    result = {
        'events': done,
        'events_per_s': done / elapsed,
        'rss_start_mib': start_rss,
        'rss_final_mib': samples[-1][1],
        'rss_peak_mib': peak_rss_mib(),
        'second_half_growth_mib': growth,
        **caps_report(stats),
    }
    await rig.close()
    return result


async def phase_backlog(args: argparse.Namespace, ready: dict) -> dict:
    print('\n== backlog: slow handlers under a burst ==')
    rig = await Rig.create(ready, dispatch_backlog_warning=1000)
    fired: List[int] = []

    async def on_backlog(*a: Any) -> None:
        fired.append(1)

    rig.client.on_dispatch_backlog = on_backlog  # type: ignore[attr-defined]
    counts = install_handlers(rig.client, delay=0.02)
    gen = Frames(args.guilds, args.user_pool, seed=3)
    burst = args.burst
    frames = []
    while len(frames) < burst:
        frames.extend(gen.batch(10_000))
    gc.collect()
    before = rss_mib()
    dt = await rig.feed(frames[:burst], yield_every=256)
    stats = await rig.client.cache_stats()
    peak_backlog = stats.dispatch_backlog
    print(
        f'  fed {burst:,} events in {dt:.1f}s; backlog right after {peak_backlog:,}, oldest {stats.dispatch_oldest_age:.2f}s, lag {stats.event_loop_lag * 1000:.1f} ms'
    )
    drained = await drain(rig.client, timeout=300)
    gc.collect()
    after = rss_mib()
    stats = await rig.client.cache_stats()
    print(f'  drained={drained}; handlers run {sum(counts.values()):,}; RSS {before:.1f} -> {after:.1f} MiB')
    gate(drained, 'backlog fully drained')
    gate(
        stats.dispatch_created == stats.dispatch_completed,
        f'every handler task completed ({stats.dispatch_created:,}/{stats.dispatch_completed:,})',
    )
    gate(bool(fired), 'on_dispatch_backlog fired during the burst')
    gate(after - before < 150, f'RSS after drain bounded (+{after - before:.1f} MiB)')
    await rig.close()
    return {
        'burst': burst,
        'peak_backlog': peak_backlog,
        'drained': drained,
        'rss_delta_mib': after - before,
        'backlog_events_fired': len(fired),
    }


async def phase_multi(args: argparse.Namespace, ready: dict) -> dict:
    print(f'\n== multi: {args.accounts} accounts in one process ==')
    gc.collect()
    base = rss_mib()
    rigs: List[Rig] = []
    for _ in range(args.accounts):
        rigs.append(await Rig.create(ready))
    gc.collect()
    after_ready = rss_mib()
    per_account = (after_ready - base) / args.accounts
    print(f'  baseline {base:.1f} MiB -> {after_ready:.1f} MiB after {args.accounts} READYs = {per_account:.1f} MiB/account')
    for i, r in enumerate(rigs):
        install_handlers(r.client)
    gens = [Frames(args.guilds, args.user_pool, seed=100 + i) for i in range(len(rigs))]
    per = args.multi_events
    t0 = time.perf_counter()

    async def pump(rig: Rig, gen: Frames) -> None:
        for _ in range(per // 5000):
            await rig.feed(gen.batch(5000))

    await asyncio.gather(*(pump(r, g) for r, g in zip(rigs, gens)))
    elapsed = time.perf_counter() - t0
    for r in rigs:
        await drain(r.client)
    gc.collect()
    after_load = rss_mib()
    total = per * len(rigs)
    print(
        f'  {total:,} events across accounts in {elapsed:.1f}s -> {fmt_rate(total, elapsed)}; RSS {after_load:.1f} MiB ({(after_load - base) / args.accounts:.1f} MiB/account)'
    )
    gate(per_account < 30, f'READY cost per account {per_account:.1f} MiB < 30 MiB')
    for r in rigs:
        await r.close()
    return {
        'accounts': args.accounts,
        'mib_per_account_ready': per_account,
        'mib_per_account_loaded': (after_load - base) / args.accounts,
        'events_per_s': total / elapsed,
    }


async def phase_persist(args: argparse.Namespace, ready: dict) -> dict:
    print(f'\n== persist: snapshot/replay with {args.persist_members:,} READY-time members ==')
    # The snapshot holds the READY-time guild payloads (not the live cache), so the
    # members have to arrive inside the guild payloads, as for small/lazy guilds.
    path = os.path.join(tempfile.mkdtemp(prefix='stress-persist-'), 'cache.bin')
    big = copy.deepcopy(ready)
    per_guild = max(1, args.persist_members // len(big['guilds']))
    for g in big['guilds']:
        g['members'] = mb.member_payloads(int(g['id']), per_guild)
    counter = WarningCounter()
    expected = per_guild * len(big['guilds'])

    gc.collect()
    t0 = time.perf_counter()
    rig = await Rig.create(big, persistence_path=path, max_member_cache=None, max_total_member_cache=None)
    for _ in range(600):  # the post-READY write runs in a background task
        if os.path.exists(path) or counter.count:
            break
        await asyncio.sleep(0.1)
    t_ready_write = time.perf_counter() - t0
    del big
    size = os.path.getsize(path) if os.path.exists(path) else 0
    members = sum(len(g._members) for g in rig.state._guilds.values())
    print(f'  READY + snapshot: {t_ready_write:.2f}s, file {size / 1048576:.1f} MiB, live members {members:,}/{expected:,}')
    await rig.close()
    del rig
    gc.collect()

    if not size:
        print(f'  snapshot not written; warnings: {counter.samples}')
        counter.detach()
        gate(False, 'snapshot written (oversized snapshots are refused by design above 256 MiB)')
        return {'expected': expected, 'file_mib': 0, 'warnings': counter.samples}

    base = rss_mib()
    client = discord.Client(persistence_path=path, max_member_cache=None, max_total_member_cache=None)
    await client._async_setup_hook()
    client._connection.user = discord.ClientUser(state=client._connection, data=mb._user(5))
    t0 = time.perf_counter()
    replayed = await client._replay_persistence_safe()
    t_replay = time.perf_counter() - t0
    restored = sum(len(g._members) for g in client._connection._guilds.values())
    print(f'  replayed={replayed}: {restored:,} members in {t_replay:.2f}s (RSS +{rss_mib() - base:.1f} MiB)')
    gate(bool(replayed), 'snapshot replayed')
    gate(restored >= members * 0.99, f'replay restored the member set ({restored:,}/{members:,})')
    gate(counter.count == 0, f'no warnings writing/replaying (saw {counter.count}) {counter.samples}')
    counter.detach()
    client._connection.subscriptions._cancel()
    await client.close()
    return {
        'members': members,
        'file_mib': size / 1048576,
        'ready_plus_write_s': t_ready_write,
        'replay_s': t_replay,
        'restored': restored,
    }


async def phase_bomb(args: argparse.Namespace, ready: dict) -> dict:
    print(f'\n== bomb: {args.bomb_events:,} malformed / hostile frames ==')
    counter = WarningCounter()
    rig = await Rig.create(ready)
    rng = random.Random(5)
    gen = Frames(args.guilds, args.user_pool, seed=9)

    def mutate(frame: str) -> str:
        obj = json.loads(frame)
        d = obj['d']
        mode = rng.randrange(7)
        if isinstance(d, dict) and d:
            key = rng.choice(list(d))
            if mode == 0:
                del d[key]
            elif mode == 1:
                d[key] = None
            elif mode == 2:
                d[key] = 'x' * rng.choice((0, 1, 5000))
            elif mode == 3:
                d[key] = -1
            elif mode == 4:
                d[key] = [d[key]] * 3
            elif mode == 5:
                d[key] = {'nested': {'deep': [1, 2, 3]}}
        if mode == 6:
            return frame[: rng.randrange(1, len(frame))]  # truncated JSON
        return json.dumps(obj)

    gc.collect()
    before = rss_mib()
    errors = 0
    t0 = time.perf_counter()
    done = 0
    while done < args.bomb_events:
        frames = [mutate(f) for f in gen.batch(10_000)]
        for f in frames:
            try:
                await rig.ws.received_message(f)
            except gateway.ReconnectWebSocket:
                errors += 1  # undecodable frame: logged, and the session resumes on a new socket
            except Exception as exc:  # anything else escaping would kill the gateway loop
                gate(False, f'unexpected {type(exc).__name__} escaped received_message: {exc!r}')
                done = args.bomb_events
                break
        done += len(frames)
        await asyncio.sleep(0)
    await drain(rig.client)
    gc.collect()
    dt = time.perf_counter() - t0
    after = rss_mib()
    print(
        f'  {done:,} frames in {dt:.1f}s; undecodable {errors:,}; parser warnings logged {counter.count:,}; RSS {before:.1f} -> {after:.1f} MiB'
    )
    gate(after - before < 100, f'RSS bounded under hostile input (+{after - before:.1f} MiB)')
    counter.detach()
    await rig.close()
    return {'frames': done, 'undecodable': errors, 'warnings': counter.count, 'rss_delta_mib': after - before}


PHASES = {
    'throughput': phase_throughput,
    'soak': phase_soak,
    'backlog': phase_backlog,
    'multi': phase_multi,
    'persist': phase_persist,
    'bomb': phase_bomb,
}


async def amain(args: argparse.Namespace) -> int:
    print(f'python {sys.version.split()[0]}, orjson={"yes" if discord.utils.HAS_ORJSON else "no"}, cpus={os.cpu_count()}')
    ready = mb.ready_payload()
    results: dict = {'python': sys.version.split()[0], 'args': vars(args)}
    if args.phase == 'all':
        # Each phase gets a fresh process so RSS figures are not polluted by the
        # heap freed (but not returned to the OS) by an earlier phase.
        import subprocess

        failed = 0
        for name in PHASES:
            out = tempfile.mktemp(suffix='.json')
            passthrough: List[str] = []
            skip = False
            argv = sys.argv[1:]
            for i, c in enumerate(argv):
                if skip:
                    skip = False
                elif c in ('--phase', '--json'):
                    skip = True
                elif not c.startswith(('--phase=', '--json=')):
                    passthrough.append(c)
            cmd = [sys.executable, os.path.abspath(__file__), *passthrough, '--phase', name, '--json', out]
            rc = subprocess.call(cmd)
            failed |= rc
            try:
                with open(out) as f:
                    results[name] = json.load(f).get(name)
                os.unlink(out)
            except (OSError, ValueError):
                results[name] = None
        if args.json:
            with open(args.json, 'w') as f:
                json.dump(results, f, indent=2, default=str)
        print('\nall gates passed' if not failed else '\nSOME PHASE(S) FAILED')
        return 1 if failed else 0
    for name in [args.phase]:
        results[name] = await PHASES[name](args, ready)
        gc.collect()
    print(f'\npeak RSS for the whole run: {peak_rss_mib():.1f} MiB')
    results['failures'] = FAILURES
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(results, f, indent=2, default=str)
    if FAILURES:
        print(f'\n{len(FAILURES)} gate(s) FAILED:')
        for m in FAILURES:
            print(f'  - {m}')
        return 1
    print('\nall gates passed')
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--phase', default='all', choices=['all', *PHASES])
    p.add_argument('--heavy', action='store_true', help='several million events, 40 accounts, 500k members')
    p.add_argument('--events', type=int, default=1_000_000, help='soak events')
    p.add_argument('--throughput-events', type=int, default=200_000)
    p.add_argument('--burst', type=int, default=100_000)
    p.add_argument('--accounts', type=int, default=10)
    p.add_argument('--multi-events', type=int, default=50_000, help='events per account')
    p.add_argument('--persist-members', type=int, default=200_000)
    p.add_argument('--bomb-events', type=int, default=200_000)
    p.add_argument('--guilds', type=int, default=mb.GUILDS)
    p.add_argument('--user-pool', type=int, default=2_000_000)
    p.add_argument('--cap-guild-members', type=int, default=10_000)
    p.add_argument('--cap-total-members', type=int, default=50_000)
    p.add_argument('--plateau-mib', type=float, default=8.0, help='max RSS growth in the second half of the soak')
    p.add_argument('--json', help='write results to this file')
    args = p.parse_args()
    if args.heavy:
        args.events = max(args.events, 5_000_000)
        args.throughput_events = max(args.throughput_events, 1_000_000)
        args.burst = max(args.burst, 300_000)
        args.accounts = max(args.accounts, 40)
        args.persist_members = max(args.persist_members, 500_000)
        args.bomb_events = max(args.bomb_events, 1_000_000)
    sys.exit(asyncio.run(amain(args)))


if __name__ == '__main__':
    main()
