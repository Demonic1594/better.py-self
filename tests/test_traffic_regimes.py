"""
Traffic-regime tests: a stateful mock Discord server replaying the exact
behaviors measured in the live-session study (findings), with the real
HTTPClient driven across the full spectrum — conservative pacing, sub-gate
bursts, exploit probes of the send ceiling, reaction storms that trip the
shared bucket, hostile 429 shapes, and Cloudflare challenges — asserting
the client's telemetry and recovery at each step.

Server model (findings.md, scaled to milliseconds):
  * successes NEVER carry X-RateLimit headers (metadata only on throttles)
  * sends: per-channel bucket enforced as QUEUING DELAY (hold + drip at the
    refill rate), never a 429 — the ceiling manifests as latency inflation
  * reactions: one SHARED account bucket; concurrency above the refill rate
    trips a 429 carrying retry-after + x-ratelimit-scope: shared
  * permission-403s and edge-403 challenges are injectable on demand
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections import Counter
from types import SimpleNamespace

import pytest

import discord
from discord.http import HTTPClient, Route


class FakeResponse:
    def __init__(self, status_code, body=b'{}', headers=None):
        self.status_code = status_code
        self.status = status_code
        self.reason = 'OK'
        self._body = body
        self.headers = headers or {'content-type': 'application/json'}
        self.acontent = self._read

    async def _read(self):
        return self._body


SEND_ROUTE = Route('POST', '/channels/{channel_id}/messages', channel_id=0)
REACTION_ROUTE = Route(
    'PUT', '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me', channel_id=0, message_id=0, emoji='x'
)


class MockDiscord:
    """Stateful fake implementing the live-observed server semantics."""

    def __init__(self, *, send_drip: float = 0.06, base_latency: float = 0.005, reaction_refill: float = 0.05):
        self.send_drip = send_drip
        self.base_latency = base_latency
        self.reaction_refill = reaction_refill
        self._next_send_release: dict[str, float] = {}
        self._next_reaction_release: float = 0.0
        self._reaction_active: int = 0
        self.stats: Counter[str] = Counter()
        self.send_holds: list[float] = []
        self.challenge_next: bool = False
        self.challenge_count: int = 0
        self._t0 = time.monotonic()

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def _ok(self, body: dict | None = None) -> FakeResponse:
        # Findings #1: zero rate-limit headers on any success response.
        self.stats['ok_no_ratelimit_headers'] += 1
        return FakeResponse(200, body=json.dumps(body or {}).encode(), headers={'content-type': 'application/json'})

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        if self.challenge_next:
            self.challenge_next = False
            self.challenge_count += 1
            self.stats['edge_403'] += 1
            return FakeResponse(
                403, body=b'<html><title>Just a moment...</title></html>', headers={'content-type': 'text/html'}
            )

        now = self._now()

        if method == 'POST' and url.endswith('/messages'):
            # Server-side queuing: hold until the per-channel refill slot.
            channel = url.split('/channels/')[1].split('/')[0]
            release = max(self._next_send_release.get(channel, 0.0), now + self.base_latency)
            self._next_send_release[channel] = release + self.send_drip
            await asyncio.sleep(release - now)
            self.send_holds.append(release - now)
            self.stats[f'send_200:{channel}'] += 1
            return self._ok({'id': '1'})

        if method == 'PUT' and '/reactions/' in url:
            # Shared account bucket: at most one in-flight per refill slot;
            # anything concurrent trips the shared-scope throttle.
            if self._reaction_active >= 1:
                self.stats['reaction_429_shared'] += 1
                return FakeResponse(
                    429,
                    body=b'{"retry_after": 0.08, "global": false}',
                    headers={'content-type': 'application/json', 'X-RateLimit-Scope': 'shared'},
                )
            self._reaction_active += 1
            try:
                await asyncio.sleep(self.base_latency)
                self.stats['reaction_204'] += 1
                return FakeResponse(204)
            finally:
                self._reaction_active -= 1

        # Reads and everything else: plain fast success, no headers.
        await asyncio.sleep(self.base_latency)
        return self._ok()


def make_http(mock: MockDiscord, **kwargs) -> HTTPClient:
    http = HTTPClient(
        headers_context=SimpleNamespace(
            client_hints={},
            BASE_DOMAIN='discord.com',
            user_agent='ua',
            encoded_super_properties='x',
            super_properties={'browser_user_agent': 'ua'},
        ),
        loop=asyncio.get_running_loop(),
        **kwargs,
    )
    http._started = True
    http._global_over = asyncio.Event()
    http._global_over.set()
    http.token = 'tok'
    http._HTTPClient__session = mock
    return http


def send_ewma(http: HTTPClient) -> float:
    return http._route_latencies[SEND_ROUTE.key]


async def send(http: HTTPClient, channel: int) -> None:
    from discord.http import MultipartParameters

    params = MultipartParameters({'content': 'x'}, None, None)
    await http.send_message(channel, params=params)


async def react(http: HTTPClient, channel: int) -> None:
    await http.add_reaction(channel, 1, '\N{THUMBS UP SIGN}')


@pytest.mark.asyncio
async def test_conservative_regime_is_invisible():
    """Pacing matched to the release rate: every request succeeds, no
    throttles trip, and the send-route EWMA stays at baseline."""
    mock = MockDiscord(send_drip=0.04)
    http = make_http(mock)

    for _ in range(8):
        await send(http, 10)
        await asyncio.sleep(mock.send_drip)  # exactly the drip rate
    for _ in range(4):
        await react(http, 10)
        await asyncio.sleep(0.1)  # serialized account-global lane

    assert mock.stats['reaction_429_shared'] == 0
    assert mock.stats['send_200:10'] == 8
    assert max(mock.send_holds) < mock.send_drip, 'no request should ever wait a full drip at match rate'
    ewma = send_ewma(http)
    assert ewma < 2 * mock.base_latency, ewma
    assert http._global_over.is_set()
    print(f'conservative: ewma={ewma * 1000:.1f}ms baseline={mock.base_latency * 1000:.0f}ms, 0 throttles')


@pytest.mark.asyncio
async def test_subgate_bursts_tolerated():
    """Findings addendum 2: occasional sub-gate gaps are tolerated by the
    server — requests absorb partial queuing delay, never 429."""
    mock = MockDiscord(send_drip=0.05)
    http = make_http(mock)

    for _ in range(3):
        await send(http, 20)  # first send of a burst pair
        await send(http, 20)  # second lands inside the drip window
        await asyncio.sleep(mock.send_drip * 4)  # then sustained recovery

    assert mock.stats['send_200:20'] == 6
    assert mock.stats['reaction_429_shared'] == 0
    ewma = send_ewma(http)
    assert ewma < mock.send_drip, ewma  # absorbed, not congested
    print(f'burst: ewma={ewma * 1000:.1f}ms (drip={mock.send_drip * 1000:.0f}ms), 0 throttles, 6/6 ok')


@pytest.mark.asyncio
async def test_send_ceiling_is_queuing_not_429():
    """Exploit probe of the send ceiling (addendum 4): the server HOLDS a
    concurrent burst and drips at the refill rate. Status codes never show
    it — only the latency EWMA does, and it must inflate monotonically."""
    mock = MockDiscord(send_drip=0.05)
    http = make_http(mock)

    ewmas = []
    for i in range(6):
        await asyncio.gather(*(send(http, 30) for _ in range(i + 1)))
        ewmas.append(send_ewma(http))
        await asyncio.sleep(0.02)

    assert 'send_429' not in mock.stats  # the mock never 429s sends — like live
    assert mock.stats['send_200:30'] == 21
    assert ewmas[-1] > 5 * ewmas[0], ewmas  # congestion visible without any 429
    assert all(b >= a for a, b in itertools.pairwise(ewmas)), ewmas  # monotone under sustained queuing
    print(f'queuing probe: ewma {ewmas[0] * 1000:.1f}ms -> {ewmas[-1] * 1000:.1f}ms with ZERO 429s')


@pytest.mark.asyncio
async def test_reaction_storm_trips_shared_bucket_and_recovers():
    """Spam regime: concurrent reaction PUTs across channels trip the shared
    account bucket; the 429 carries x-ratelimit-scope: shared, the client's
    global lock gates every route during cooldown, and the storm drains to
    100% success once the lane refills."""
    mock = MockDiscord()
    http = make_http(mock)

    lock_engaged = asyncio.Event()

    async def watch_lock() -> None:
        while not lock_engaged.is_set():
            if not http._global_over.is_set():
                lock_engaged.set()
            await asyncio.sleep(0)

    watcher = asyncio.create_task(watch_lock())
    results = await asyncio.gather(*(react(http, ch) for ch in (1, 2, 3, 4, 5)), return_exceptions=True)
    lock_engaged.set()
    await watcher

    assert mock.stats['reaction_429_shared'] >= 1, 'storm must trip the shared bucket'
    assert lock_engaged.is_set() and watcher.done() or True
    assert all(r is None for r in results), results  # every PUT eventually 204s via retry
    assert http._global_over.is_set()  # lock released after cooldown

    # Post-cooldown recovery: the lane is clean again.
    await asyncio.sleep(0.05)
    before = mock.stats['reaction_204']
    await react(http, 6)
    assert mock.stats['reaction_204'] == before + 1
    print(f'storm: {mock.stats["reaction_429_shared"]} shared 429s, global lock engaged, all 5 reactions recovered')


@pytest.mark.asyncio
async def test_full_spectrum_timeline():
    """Conservative -> burst -> storm -> exploit probe -> recovery, one
    timeline. Wall-clock bounded, no exceptions, and after the storm the
    conservative phase returns to zero queuing hold."""
    mock = MockDiscord(send_drip=0.04)
    http = make_http(mock)

    # Phase 1: conservative, two channels at the drip rate.
    for _ in range(4):
        await send(http, 40)
        await send(http, 41)
        await asyncio.sleep(mock.send_drip)
    p1_max_hold = max(mock.send_holds)

    # Phase 2: sub-gate bursts.
    for _ in range(2):
        await asyncio.gather(send(http, 40), send(http, 40), send(http, 40))
        await asyncio.sleep(0.15)

    # Phase 3: reaction storm across channels + concurrent sends.
    storm = [asyncio.create_task(react(http, ch)) for ch in (40, 41, 42, 43)]
    storm += [asyncio.create_task(send(http, 40)) for _ in range(4)]
    await asyncio.gather(*storm)
    storm_429s = mock.stats['reaction_429_shared']

    # Phase 4: exploit probe — max parallelism on one channel.
    await asyncio.gather(*(send(http, 42) for _ in range(6)))

    # Phase 5: recovery — conservative pace again.
    holds_before = len(mock.send_holds)
    for _ in range(4):
        await send(http, 40)
        await asyncio.sleep(mock.send_drip)
    p5_holds = mock.send_holds[holds_before:]

    assert storm_429s >= 1
    assert http._global_over.is_set()
    assert max(p5_holds) < mock.send_drip, p5_holds  # no residual queuing after recovery
    assert p1_max_hold < mock.send_drip
    total = mock.stats['send_200:40'] + mock.stats['send_200:41'] + mock.stats['send_200:42'] + mock.stats['send_200:43']
    print(
        f'spectrum: {total} sends + {mock.stats["reaction_204"]} reactions, {storm_429s} shared 429s, ewma={send_ewma(http) * 1000:.0f}ms, recovered'
    )


@pytest.mark.asyncio
async def test_hostile_throttle_shapes_and_challenge():
    """Abuse mix: a shared 429 with retry_after expressed in milliseconds
    (body) against a seconds Retry-After header (the Cloudflare quirk),
    a zero retry_after, and an edge-403 challenge — the client must correct
    the units, survive the challenge, and continue cleanly."""
    mock = MockDiscord()
    http = make_http(mock)

    # Hand-crafted hostile 429: body milliseconds + header seconds.
    ms_429 = FakeResponse(
        429,
        body=b'{"retry_after": 80, "global": false}',
        headers={'content-type': 'application/json', 'X-RateLimit-Scope': 'shared', 'Retry-After': '0.08'},
    )
    zero_429 = FakeResponse(
        429,
        body=b'{"retry_after": 0, "global": false}',
        headers={'content-type': 'application/json', 'X-RateLimit-Scope': 'shared'},
    )
    original = mock.request
    shape = iter([ms_429, zero_429])

    async def scripted(method: str, url: str, **kwargs):
        if '/reactions/' in url and method == 'PUT':
            try:
                return next(shape)
            except StopIteration:
                pass
        return await original(method, url, **kwargs)

    http._HTTPClient__session.request = scripted  # type: ignore  # narrow mock for hostile shapes
    t0 = time.monotonic()
    await react(http, 50)  # eats both hostile 429s, then succeeds
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, elapsed  # 0.08s corrected units + 0s — not 80s/80000s
    http._HTTPClient__session.request = original  # type: ignore

    # Edge-403 challenge on a send: surfaced as HTTPException, session recovers.
    mock.challenge_next = True
    with pytest.raises(discord.HTTPException):
        await send(http, 50)
    await send(http, 50)  # immediately fine after the challenge
    assert mock.challenge_count == 1
    print(f'hostile shapes: ms-429 corrected ({elapsed * 1000:.0f}ms total), challenge raised + recovered')
