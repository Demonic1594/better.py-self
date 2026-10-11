"""
LaneGovernor — pacing for Discord user accounts, implementing the operating
recipe validated by live rate studies (three serialized account-global
lanes, backoff on throttles, bounded probe-down, latency as the congestion
signal). It exists because user accounts receive no rate-limit headers:
pacing must come from conservative intervals + the throttle/latency signals
the library exposes (``on_http_throttle``, ``http_route_latencies()``).

Recipe (validated rates in parentheses):
  * one serialized lane per action class per account — the buckets are
    account-global, so per-channel pacing under-paces them:
    send (2.6s), react (3.0s), click (3.0s)
  * human jitter on every interval; round-robin across destinations;
    per-channel slowmode stacks on top of the lane
  * on an absorbed 429: honor retry-after + padding, that lane backs
    off x2; a shared-scope 429 pauses ALL lanes (account-wide cooldown)
  * backoff decays only by probing down <=10%, and only after >=2 min
    clean — never below the validated base interval
  * watch the send-route latency EWMA: queuing inflation (not 429s) is
    how the send ceiling actually manifests

Wire-up::

    import asyncio
    from discord import Client
    from lane_governor import LaneGovernor

    client = Client()
    governor = LaneGovernor(client)

    @client.event
    async def on_http_throttle(route, retry_after, scope):
        governor.on_throttle(route, retry_after, scope)

    async def main():
        asyncio.create_task(governor.auto_observe())  # EWMA congestion watch
        ...
        await governor.run(
            lambda: channel.send('hello'),
            lane='send',
            channel=channel.id,
            slowmode=channel.slowmode_delay or 0,
        )

Multi-account: buckets are per-account — run one governor per client.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable

LANES = ('send', 'react', 'click')


# Route classification: which lane does a throttled route belong to?
def classify_route(route: str) -> str | None:
    """Maps an HTTP route key to its lane, or None if unrecognized."""
    if '/reactions/' in route:
        return 'react'
    if '/interactions' in route:
        return 'click'
    if 'POST' in route and '/messages' in route:
        return 'send'
    return None


class Lane:
    """A single serialized pacing lane with bounded probe-down."""

    __slots__ = ('base', 'current', 'last_changed', 'trips')

    def __init__(self, base: float) -> None:
        self.base = base
        self.current = base
        self.last_changed = time.monotonic()
        self.trips = 0

    def backoff(self, factor: float) -> None:
        self.current *= factor
        self.last_changed = time.monotonic()
        self.trips += 1

    def maybe_probe(self, now: float, probe_after: float, probe_step: float) -> bool:
        """Decays the interval by <= probe_step, only after probe_after
        seconds without a trip, never below the base interval."""
        if self.current <= self.base or now - self.last_changed < probe_after:
            return False
        self.current = max(self.base, self.current * (1.0 - probe_step))
        self.last_changed = now
        return True


class LaneGovernor:
    def __init__(
        self,
        client: Any = None,
        *,
        send: float = 2.6,
        react: float = 3.0,
        click: float = 3.0,
        jitter: tuple[float, float] = (0.85, 1.25),
        padding: float = 1.5,
        backoff: float = 2.0,
        probe_after: float = 120.0,
        probe_step: float = 0.10,
        ewma_factor: float = 3.0,
        max_channel_slots: int = 512,
    ) -> None:
        self.client = client
        self.jitter = jitter
        self.padding = padding
        self.backoff = backoff
        self.probe_after = probe_after
        self.probe_step = probe_step
        self.ewma_factor = ewma_factor
        self.lanes: dict[str, Lane] = {'send': Lane(send), 'react': Lane(react), 'click': Lane(click)}
        self._locks: dict[str, asyncio.Lock] = {lane: asyncio.Lock() for lane in LANES}
        self._next_at: dict[str, float] = {lane: 0.0 for lane in LANES}
        # (lane, channel_id) -> next allowed time; per-channel slowmode
        # stacks on top of the account-global lane interval
        self._channel_next: dict[tuple[str, Any], float] = {}
        self._max_channel_slots = max_channel_slots
        self.stats = {'actions': 0, 'throttles': 0, 'shared_pauses': 0, 'probes': 0, 'ewma_backoffs': 0}
        self._baseline_ewma: float | None = None

    # -- pacing ------------------------------------------------------------

    async def run(
        self, factory: Callable[[], Awaitable[Any]], lane: str, *, channel: Any = None, slowmode: float = 0.0
    ) -> Any:
        """Serializes `factory` through `lane`, waiting for the lane (and
        the channel's slowmode slot) before awaiting it."""
        if lane not in self._locks:
            raise ValueError(f'unknown lane {lane!r}; expected one of {LANES}')
        async with self._locks[lane]:
            now = time.monotonic()
            if self.lanes[lane].maybe_probe(now, self.probe_after, self.probe_step):
                self.stats['probes'] += 1
            wait = self._next_at[lane] - now
            if wait > 0:
                await asyncio.sleep(wait)
            if channel is not None and slowmode > 0:
                key = (lane, channel)
                channel_wait = self._channel_next.get(key, 0.0) - time.monotonic()
                if channel_wait > 0:
                    await asyncio.sleep(channel_wait)
                if len(self._channel_next) >= self._max_channel_slots:
                    self._channel_next.clear()  # bounded: cold-start is safe
                self._channel_next[key] = time.monotonic() + slowmode
            self._next_at[lane] = time.monotonic() + self.lanes[lane].current * random.uniform(*self.jitter)
            self.stats['actions'] += 1
            return await factory()

    # -- signals ------------------------------------------------------------

    def on_throttle(self, route: str, retry_after: float, scope: str | None) -> None:
        """Wire to ``on_http_throttle``. A shared-scope throttle is an
        account-wide cooldown: pause every lane. Otherwise back off the
        lane that tripped, on top of honoring retry-after + padding."""
        self.stats['throttles'] += 1
        hold = max(0.0, retry_after) + self.padding
        now = time.monotonic()
        lane = classify_route(route)
        targets = LANES if (scope == 'shared' or lane is None) else (lane,)
        if scope == 'shared':
            self.stats['shared_pauses'] += 1
        for name in targets:
            self._next_at[name] = max(self._next_at[name], now + hold)
            if scope == 'shared' or lane is None:
                # pausing every lane is a full account cooldown; doubling
                # each interval would over-react, so only pad the clock
                continue
            self.lanes[name].backoff(self.backoff)

    async def auto_observe(self, poll: float = 30.0) -> None:
        """Long-running task: watches the send-route latency EWMA. Queuing
        inflation without 429s is the real send ceiling — slow the lane."""
        if self.client is None:
            raise RuntimeError('auto_observe needs a client')
        while True:
            await asyncio.sleep(poll)
            self.observe(self.client.http_route_latencies())

    def observe(self, latencies: dict[str, float]) -> None:
        for route, ewma in latencies.items():
            if 'POST' not in route or '/messages' not in route or '/reactions/' in route:
                continue
            if self._baseline_ewma is None or ewma < self._baseline_ewma:
                self._baseline_ewma = ewma
            elif ewma > self._baseline_ewma * self.ewma_factor:
                lane = self.lanes['send']
                lane.backoff(1.25)  # gentle: congestion, not a hard trip
                self.stats['ewma_backoffs'] += 1

    # -- introspection ------------------------------------------------------

    def intervals(self) -> dict[str, float]:
        return {name: lane.current for name, lane in self.lanes.items()}
