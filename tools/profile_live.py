"""Profiles the library against a real account's live gateway traffic.

The client only *listens*: it never sends messages or changes anything. It
runs for a fixed time, then writes a report of aggregate numbers only (event
counts, parse timings, JSON decode time, memory, cache sizes and, with
``--cprofile``, the hottest functions). The report contains no message
content, tokens, IDs or names, so it is safe to share.

Usage::

    DISCORD_TOKEN=... python tools/profile_live.py --minutes 10
    DISCORD_TOKEN=... python tools/profile_live.py --minutes 10 --cprofile

``--cprofile`` profiles every function call on the event loop thread. It
slows the client down (roughly 1.5-2x) but shows where time goes in detail;
run once without it for realistic absolute timings.

Not shipped with the package (``tools/`` is not a package directory).
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import os
import pstats
import resource
import sys
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402
from discord import utils  # noqa: E402


def _rss_mb() -> float:
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    # ru_maxrss is KiB on Linux, bytes on macOS (peak, not current)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == 'darwin' else peak / 1024


class Recorder:
    def __init__(self) -> None:
        self.counts: Dict[str, int] = defaultdict(int)
        self.parse_time: Dict[str, float] = defaultdict(float)
        self.json_frames = 0
        self.json_time = 0.0
        self.json_bytes = 0

    def wrap_parser(self, name: str, func: Callable[[Any], None]) -> Callable[[Any], None]:
        counts, parse_time, clock = self.counts, self.parse_time, time.perf_counter

        def timed(data: Any) -> None:
            start = clock()
            try:
                func(data)
            finally:
                parse_time[name] += clock() - start
                counts[name] += 1

        return timed

    def wrap_json(self, func: Callable[[Any], Any]) -> Callable[[Any], Any]:
        clock = time.perf_counter

        def timed(data: Any) -> Any:
            start = clock()
            try:
                return func(data)
            finally:
                self.json_time += clock() - start
                self.json_frames += 1
                self.json_bytes += len(data)

        return timed


def _report(rec: Recorder, client: discord.Client, elapsed: float, rss: List[float], profile: Any, stats: Any) -> str:
    out = io.StringIO()
    w = out.write
    w(f'discord.py-self {discord.__version__} | Python {sys.version.split()[0]} | {sys.platform}\n')
    w(f'orjson: {"yes" if utils.HAS_ORJSON else "no"} | eager_dispatch: {client._eager_dispatch}\n')
    w(f'duration: {elapsed:.0f}s | guilds: {len(client.guilds)}\n\n')

    total_events = sum(rec.counts.values())
    total_parse = sum(rec.parse_time.values())
    w(f'events: {total_events} ({total_events / max(elapsed, 1):.1f}/s), parse time {total_parse * 1e3:.1f} ms total\n')
    if rec.json_frames:
        w(
            f'json: {rec.json_frames} frames, {rec.json_bytes / 1e6:.1f} MB, '
            f'{rec.json_time * 1e3:.1f} ms total ({rec.json_time / rec.json_frames * 1e6:.1f} us/frame)\n'
        )
    w('\nper event type (sorted by total parse time):\n')
    w(f'{"event":38s} {"count":>8s} {"total ms":>10s} {"avg us":>8s}\n')
    for name in sorted(rec.parse_time, key=rec.parse_time.__getitem__, reverse=True):
        count = rec.counts[name]
        total = rec.parse_time[name]
        w(f'{name:38s} {count:8d} {total * 1e3:10.1f} {total / count * 1e6:8.1f}\n')

    w(f'\nRSS: start {rss[0]:.1f} MB, after ready {rss[1]:.1f} MB, end {rss[2]:.1f} MB\n')
    if stats is not None:
        w('\ncache stats:\n')
        for field in getattr(stats, '__slots__', ()) or getattr(stats, '_fields', ()):
            value = getattr(stats, field, None)
            if isinstance(value, (int, float)):
                w(f'  {field}: {value}\n')

    if profile is not None:
        w('\nhottest functions (cProfile, by own time):\n')
        buf = io.StringIO()
        pstats.Stats(profile, stream=buf).strip_dirs().sort_stats('tottime').print_stats(40)
        w(buf.getvalue())
    return out.getvalue()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--minutes', type=float, default=10.0)
    parser.add_argument('--cprofile', action='store_true', help='profile every call (slower; more detail)')
    parser.add_argument('--eager-dispatch', action='store_true', help='run with Client(eager_dispatch=True)')
    parser.add_argument('--output', default='profile_report.txt')
    args = parser.parse_args()

    token = os.environ.get('DISCORD_TOKEN')
    if not token:
        raise SystemExit('Set DISCORD_TOKEN in the environment (never pass it on the command line).')

    rss = [_rss_mb()]
    client = discord.Client(eager_dispatch=args.eager_dispatch)
    rec = Recorder()

    # Time every gateway parser in place (the gateway reads this same dict)
    state = client._connection
    for name, func in list(state.parsers.items()):
        state.parsers[name] = rec.wrap_parser(name, func)
    # Time JSON decoding of gateway frames
    utils._from_json_frame = rec.wrap_json(utils._from_json_frame)  # type: ignore[assignment]

    ready = asyncio.Event()

    @client.event
    async def on_ready():
        ready.set()

    profile = cProfile.Profile() if args.cprofile else None
    if profile is not None:
        profile.enable()

    runner = asyncio.ensure_future(client.start(token))
    start = time.perf_counter()
    try:
        ready_wait = asyncio.ensure_future(ready.wait())
        done, _ = await asyncio.wait({ready_wait, runner}, timeout=180, return_when=asyncio.FIRST_COMPLETED)
        if runner in done:
            ready_wait.cancel()
            runner.result()  # login failed: surface the error (e.g. LoginFailure)
            raise SystemExit('The client stopped before becoming ready.')
        if not done:
            ready_wait.cancel()
            raise SystemExit('Timed out waiting for READY.')
        rss.append(_rss_mb())
        print(f'Ready. Recording for {args.minutes:g} minutes...', flush=True)
        await asyncio.sleep(args.minutes * 60)
    finally:
        elapsed = time.perf_counter() - start
        if profile is not None:
            profile.disable()
        if len(rss) < 2:
            rss.append(_rss_mb())
        rss.append(_rss_mb())
        try:
            stats = await client.cache_stats()
        except Exception:
            stats = None
        await client.close()
        runner.cancel()

    report = _report(rec, client, elapsed, rss, profile, stats)
    with open(args.output, 'w') as f:
        f.write(report)
    print(report)
    print(f'Report written to {args.output}')


if __name__ == '__main__':
    asyncio.run(main())
