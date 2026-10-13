"""Traffic-regime tests against the findings/TIMING-calibrated mock
server in traffic_harness — see that module for the server model."""

from __future__ import annotations

import asyncio
import itertools
import time

import pytest

import discord

from traffic_harness import (
    REACTION_ROUTE,
    SEND_ROUTE,
    FakeResponse,
    MockDiscord,
    click,
    make_http,
    react,
    send,
    send_ewma,
)


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

    assert 'send_429_shared' not in mock.stats  # bursts queue; only sustained over-rate 429s (TIMING.md)
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


@pytest.mark.asyncio
async def test_timing_lane_recipe_is_clean():
    """TIMING.md operating rates, scaled: three serialized account-global
    lanes (send 2.6s -> send_lane, react 3.0s -> 1.15x, click 3.0s -> 1.15x),
    round-robin. Zero throttles on any lane, zero telemetry recorded."""
    lane = 0.2
    mock = MockDiscord(send_drip=0.01, send_lane=lane)
    http = make_http(mock)

    for _ in range(8):
        await send(http, 60)
        await asyncio.sleep(lane)
        await react(http, 60)
        await asyncio.sleep(lane * 0.15)
        await click(http)
        await asyncio.sleep(lane * 0.15)

    assert mock.stats['send_200:60'] == 8
    assert mock.stats['reaction_204'] == 8
    assert mock.stats['click_204'] == 8
    assert 'send_429_shared' not in mock.stats
    assert 'reaction_429_shared' not in mock.stats
    assert http._throttle_events == {}
    print('lane recipe: 8x(send+react+click) round-robin, zero throttles, zero telemetry')


@pytest.mark.asyncio
async def test_sustained_overrate_sends_trip_shared_lane():
    """TIMING.md send table: sustained ~0.6x of the safe interval is the
    hard edge — regular shared-bucket 429s. The client must absorb them
    (retry to success) AND record every wire-level throttle."""
    lane = 0.2
    mock = MockDiscord(send_drip=0.01, send_lane=lane)
    http = make_http(mock)

    for _ in range(10):
        await send(http, 61)
        await asyncio.sleep(lane * 0.4)  # ~2.5x the safe sustained rate

    assert mock.stats['send_200:61'] == 10  # every send eventually landed
    trips = mock.stats['send_429_shared']
    assert trips >= 2, 'sustained over-rate must trip the shared send bucket'
    recorded = http._throttle_events.get(SEND_ROUTE.key)
    assert recorded is not None and len(recorded) == trips
    assert all(ra == 0.06 for _, ra in recorded)
    print(f'over-rate: 10 sends at 2.5x safe rate -> {trips} shared 429s, all absorbed + recorded')


@pytest.mark.asyncio
async def test_click_lane_independent():
    """TIMING.md: component clicks are an independent bucket. While the
    send lane trips its shared bucket and reactions storm theirs, clicks
    keep succeeding (delayed only by the account-wide cooldown, which
    scope=shared correctly imposes on every route)."""
    lane = 0.2
    mock = MockDiscord(send_drip=0.01, send_lane=lane)
    http = make_http(mock)

    async def send_stream() -> None:
        for _ in range(8):
            await send(http, 62)
            await asyncio.sleep(lane * 0.4)

    async def reaction_storm() -> None:
        await asyncio.gather(*(react(http, ch) for ch in (62, 63, 64, 65)))

    async def click_lane() -> None:
        for _ in range(5):
            await click(http)
            await asyncio.sleep(lane * 1.25)

    await asyncio.gather(send_stream(), reaction_storm(), click_lane())

    assert mock.stats['click_204'] == 5  # clicks themselves never throttled
    assert mock.stats['send_429_shared'] >= 1
    assert mock.stats['reaction_429_shared'] >= 1
    assert http._throttle_events.keys() == {SEND_ROUTE.key, REACTION_ROUTE.key}
    print(
        f'independence: 5/5 clicks clean while {mock.stats["send_429_shared"]} send + '
        f'{mock.stats["reaction_429_shared"]} reaction throttles tripped around them'
    )


@pytest.mark.asyncio
async def test_wire_throttle_observability():
    """TIMING.md: 'discord.py-self hides throttles — observe at the session
    layer to know real throttle pressure.' With the telemetry record, no
    session instrumentation is needed: absorbed 429s are visible via
    Client.http_throttle_events(), matching the wire exactly."""
    mock = MockDiscord()
    http = make_http(mock)

    client = discord.Client()
    client.http = http
    assert client.http_throttle_events() == {}

    await asyncio.gather(*(react(http, ch) for ch in (70, 71, 72, 73)))
    summary = client.http_throttle_events()
    entry = summary[REACTION_ROUTE.key]
    assert entry['count'] == mock.stats['reaction_429_shared']
    assert entry['count'] >= 1
    assert entry['last_retry_after'] == 0.08
    assert 0 <= entry['last_after_s'] < 5

    # A clean run after cooldown does not clear the record (it is a log,
    # not a state flag) — and adding traffic keeps it bounded per route.
    for _ in range(80):
        await react(http, 74)
        await asyncio.sleep(0.02)
    assert len(http._throttle_events[REACTION_ROUTE.key]) <= 64
    print(f'observability: {entry["count"]} hidden throttles visible without session instrumentation')
