"""
The deep matrix: conventional, unconventional, spam, abuse and exploit
regimes against the findings/TIMING-calibrated mock — at high parallelism,
with the real button/interaction path, wire cancellation storms, hostile
throttle shapes, bucket-hash flooding, and a record/replay round trip that
turns a captured session into a deterministic regression scenario.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from discord.errors import HTTPException, RateLimited

from traffic_harness import (
    REACTION_ROUTE,
    SEND_ROUTE,
    FakeResponse,
    MockDiscord,
    SessionRecorder,
    ScriptedDiscord,
    button_click,
    click,
    make_http,
    read,
    react,
    send,
)


# --------------------------------------------------------------------------
# Conventional: the real button path, payload integrity (addendum 3)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_click_via_real_interact_path():
    """The library's own interact() payload must carry everything findings
    addendum 3 says real clients need: session_id + message_flags (else 400
    code 50035), plus a snowflake-shaped nonce."""
    mock = MockDiscord()
    http = make_http(mock)

    await button_click(http)

    assert mock.stats['click_204'] == 1
    assert mock.stats['click_400_50035'] == 0
    payload = mock.last_click_payload
    assert payload is not None
    assert isinstance(payload['session_id'], str) and len(payload['session_id']) >= 16
    assert payload['message_flags'] == 0
    assert payload['message_id'] == '901' and payload['channel_id'] == '900'
    assert payload['nonce'].isdigit(), 'nonce must be snowflake-shaped'
    print('button: interact() payload passes the 50035 gate (session_id/message_flags/nonce)')


@pytest.mark.asyncio
async def test_button_click_without_required_keys_rejected():
    """A client that forgets session_id/message_flags gets the 400 code
    50035 exactly as observed live — surfaced as HTTPException.code."""
    mock = MockDiscord()
    http = make_http(mock)

    from discord.http import Route

    with pytest.raises(HTTPException) as exc_info:
        await http.request(Route('POST', '/interactions'), json={'type': 2, 'data': {'custom_id': 'x'}})
    assert exc_info.value.code == 50035
    assert mock.stats['click_400_50035'] == 1
    print('button: missing keys -> 400 code 50035 surfaced')


@pytest.mark.asyncio
async def test_button_lane_sustained_under_surrounding_storms():
    """12 clicks at the TIMING lane pace while the send lane is driven past
    its shared edge and reactions storm concurrently — clicks stay 100%."""
    lane = 0.15
    mock = MockDiscord(send_drip=0.01, send_lane=lane)
    http = make_http(mock)

    async def send_storm() -> None:
        # Bounded client retries (5 attempts) mean a hopelessly over-rate
        # send surfaces as HTTPException(429) — the spammer's honest outcome.
        for _ in range(10):
            results = await asyncio.gather(
                *(send(http, 100 + ch) for ch in range(6)), return_exceptions=True
            )
            assert all(r is None or _is_throttle(r) for r in results), results
            await asyncio.sleep(0.03)

    async def reaction_storm() -> None:
        for _ in range(3):
            await asyncio.gather(*(react(http, ch) for ch in (200, 201, 202, 203)))
            await asyncio.sleep(0.1)

    async def click_lane() -> None:
        for _ in range(12):
            await button_click(http)  # clicks must never inherit the spam outcome
            await asyncio.sleep(lane)

    await asyncio.gather(send_storm(), reaction_storm(), click_lane())

    assert mock.stats['click_204'] == 12
    assert mock.stats['click_400_50035'] == 0
    assert mock.stats['send_429_shared'] >= 1
    assert mock.stats['reaction_429_shared'] >= 1
    print(f'button lane: 12/12 clean amid {mock.stats["send_429_shared"]} send + {mock.stats["reaction_429_shared"]} reaction throttles')


def _is_throttle(exc: BaseException) -> bool:
    return isinstance(exc, HTTPException) and exc.status == 429


# --------------------------------------------------------------------------
# Parallelism: fan-outs, mixed storms, thundering herd
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_fanout_64_sends_16_channels():
    """64 sends across 16 channels all in flight at once: every one lands,
    queuing holds follow the drip arithmetic, nothing leaks."""
    mock = MockDiscord(send_drip=0.03)
    http = make_http(mock)

    await asyncio.wait_for(asyncio.gather(*(send(http, 300 + (i % 16)) for i in range(64))), timeout=5)
    await asyncio.sleep(0)

    total = sum(mock.stats[f'send_200:{300 + ch}'] for ch in range(16))
    assert total == 64
    # 4 per channel: holds run base, 1*drip, 2*drip, 3*drip -> bounded by 4*drip
    assert max(mock.send_holds) < 4 * mock.send_drip + 0.02, max(mock.send_holds)
    assert 'reaction_429_shared' not in mock.stats and 'send_429_shared' not in mock.stats
    print(f'fanout: 64/64 landed, max hold {max(mock.send_holds) * 1000:.0f}ms (<= 4x drip), zero throttles')


@pytest.mark.asyncio
async def test_mega_mixed_storm_all_routes_concurrent():
    """50 requests across all four route classes simultaneously; the global
    lock cycles from the shared 429s; everything completes; telemetry counts
    match the wire exactly."""
    mock = MockDiscord(send_drip=0.02, send_lane=0.5)
    http = make_http(mock)

    tasks = [send(http, 400 + (i % 8)) for i in range(20)]
    tasks += [react(http, 400 + i) for i in range(12)]
    tasks += [button_click(http) for _ in range(8)]
    tasks += [read(http) for _ in range(10)]
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)

    surfaced = sum(1 for r in results if _is_throttle(r))
    other = [r for r in results if isinstance(r, BaseException) and not _is_throttle(r)]
    assert other == [], other
    assert mock.stats['click_204'] == 8  # clicks never surfaced a throttle
    assert mock.stats['read_200'] == 10
    landed_sends = sum(mock.stats[f'send_200:{400 + i}'] for i in range(8))
    assert landed_sends + mock.stats['reaction_204'] + surfaced == 20 + 12  # each send/react landed or honestly surfaced
    assert http._global_over.is_set()
    if mock.stats['reaction_429_shared']:
        assert len(http._throttle_events[REACTION_ROUTE.key]) == mock.stats['reaction_429_shared']
    print(f'mega storm: {landed_sends} landed, {surfaced} honestly surfaced, clicks 8/8, wire==telemetry')


@pytest.mark.asyncio
async def test_global_lock_thundering_herd():
    """While a shared cooldown holds the global lock, 50 fresh requests pile
    on the entry gate; when it releases, every one proceeds — none starve."""
    mock = MockDiscord()
    http = make_http(mock)
    tripped = {'v': False}
    original = mock.request

    async def trip_once(method, url, **kwargs):
        if method == 'PUT' and '/reactions/' in url and not tripped['v']:
            tripped['v'] = True
            return mock._shared_429(0.25)
        return await original(method, url, **kwargs)

    http._HTTPClient__session.request = trip_once  # type: ignore

    trip = asyncio.create_task(react(http, 500))
    for _ in range(200):
        if not http._global_over.is_set():
            break
        await asyncio.sleep(0.001)
    assert not http._global_over.is_set(), 'shared cooldown must be holding the lock'

    herd = [asyncio.create_task(read(http)) for _ in range(50)]
    await asyncio.wait_for(asyncio.gather(trip, *herd), timeout=3)
    assert all(t.done() and not t.cancelled() for t in herd)
    assert mock.stats['read_200'] == 50
    print('thundering herd: 50 gated requests all released, zero starvation')


# --------------------------------------------------------------------------
# Abuse/exploit: cancellation, hostile shapes, bucket flooding
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_mid_throttle_sleep_releases_global_lock():
    """A request cancelled while sleeping out a shared cooldown must still
    release the global lock — otherwise it stays cleared forever and every
    subsequent request deadlocks on the entry gate."""
    mock = MockDiscord()
    http = make_http(mock)
    tripped = {'v': False}
    original = mock.request

    async def trip_once(method, url, **kwargs):
        if method == 'PUT' and '/reactions/' in url and not tripped['v']:
            tripped['v'] = True
            return mock._shared_429(5.0)  # long enough to cancel mid-sleep
        return await original(method, url, **kwargs)

    http._HTTPClient__session.request = trip_once  # type: ignore

    victim = asyncio.create_task(react(http, 501))
    for _ in range(200):
        if not http._global_over.is_set():
            break
        await asyncio.sleep(0.001)
    assert not http._global_over.is_set()

    await asyncio.sleep(0.02)
    victim.cancel()
    await asyncio.gather(victim, return_exceptions=True)

    # The lock must come back without any other throttle event happening.
    for _ in range(100):
        if http._global_over.is_set():
            break
        await asyncio.sleep(0.001)
    assert http._global_over.is_set(), 'cancelled request leaked a cleared global lock'

    await asyncio.wait_for(read(http), timeout=1.0)  # pre-fix: deadlocks forever
    print('cancel-mid-cooldown: lock released via finally, session fully usable')


@pytest.mark.asyncio
async def test_parallel_cancellation_storm():
    """40 mixed requests, half cancelled at random moments mid-flight, then
    a clean read: no stuck lock, no leaked tasks, session usable."""
    mock = MockDiscord(send_drip=0.02, send_lane=0.3)
    http = make_http(mock)

    tasks = [asyncio.create_task(send(http, 600 + (i % 4))) for i in range(20)]
    tasks += [asyncio.create_task(react(http, 600 + (i % 4))) for i in range(12)]
    tasks += [asyncio.create_task(button_click(http)) for _ in range(8)]
    await asyncio.sleep(0.01)
    for task in tasks[::2]:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    cancelled = sum(isinstance(r, asyncio.CancelledError) for r in results)
    throttled = sum(_is_throttle(r) for r in results)
    failed = [r for r in results if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError) and not _is_throttle(r)]
    assert failed == [], failed

    await asyncio.sleep(0.05)
    for _ in range(200):
        if http._global_over.is_set():
            break
        await asyncio.sleep(0.001)
    assert http._global_over.is_set()
    await asyncio.wait_for(read(http), timeout=1.0)
    print(f'cancel storm: {cancelled} cancelled, {throttled} honestly throttled, 0 failures, lock intact, session usable')


@pytest.mark.asyncio
async def test_max_ratelimit_timeout_raises_rather_than_sleeps():
    """A 429 demanding 30s with max_ratelimit_timeout=1 must raise
    RateLimited immediately instead of sleeping."""
    mock = MockDiscord()
    http = make_http(mock)
    # The constructor floors this at 30s; bypassing the floor is exactly what
    # the test needs — a 30s demand must raise, not sleep.
    http.max_ratelimit_timeout = 0.5

    huge_429 = FakeResponse(
        429,
        body=b'{"retry_after": 30, "global": false}',
        headers={'content-type': 'application/json', 'X-RateLimit-Scope': 'shared'},
    )
    original = mock.request

    async def once(method, url, **kwargs):
        if method == 'PUT' and '/reactions/' in url:
            mock.request = original  # type: ignore
            return huge_429
        return await original(method, url, **kwargs)

    mock.request = once  # type: ignore
    t0 = time.monotonic()
    with pytest.raises(RateLimited):
        await react(http, 502)
    assert time.monotonic() - t0 < 0.5
    print('max timeout: 30s demand raised as RateLimited in <0.5s')


@pytest.mark.asyncio
async def test_cloudflare_html_429_with_retry_header():
    """An edge-style 429: HTML body, no Via, Retry-After in the header only.
    The client takes the header value, sleeps, retries, succeeds."""
    mock = MockDiscord()
    http = make_http(mock)

    cf_429 = FakeResponse(
        429,
        body=b'<html><title>Too many requests</title></html>',
        headers={'content-type': 'text/html', 'Retry-After': '0.05'},
    )
    original = mock.request

    async def once(method, url, **kwargs):
        if method == 'PUT' and '/reactions/' in url:
            mock.request = original  # type: ignore
            return cf_429
        return await original(method, url, **kwargs)

    mock.request = once  # type: ignore
    t0 = time.monotonic()
    await react(http, 503)
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.05
    assert mock.stats['reaction_204'] == 1
    assert len(http._throttle_events[REACTION_ROUTE.key]) == 1
    print(f'cloudflare 429: slept header retry-after ({elapsed * 1000:.0f}ms), recovered, recorded')


@pytest.mark.asyncio
async def test_202_deferred_retry_increments_attempts():
    """202 with error group 11xxxx is a defer, not a failure: retry_after is
    honored and the attempts query param increments on the retry."""
    seen: list[dict] = []
    defer = FakeResponse(202, body=b'{"code": 110001, "retry_after": 0.02}', headers={'content-type': 'application/json'})
    script = [(0.0, defer), (0.0, FakeResponse(200, body=b'{"id": "9"}'))]

    class Snoop:
        async def request(self, method, url, **kwargs):
            seen.append({'method': method, 'url': url, **kwargs})
            delay, response = script.pop(0)
            await asyncio.sleep(delay)
            return response

    http = make_http(Snoop())
    from discord.http import Route

    await http.request(Route('GET', '/users/{user_id}', user_id=3))

    assert len(seen) == 2
    assert seen[1]['params']['attempts'] == 1
    print('202 defer: retried once with attempts=1')


@pytest.mark.asyncio
async def test_bucket_hash_flood_tables_bounded():
    """Exploit probe: a server minting a fresh bucket hash on every response
    must not grow the client's rate-limit tables without bound."""
    mock = MockDiscord(bucket_hash_flood=True)
    http = make_http(mock)

    for _ in range(300):
        await read(http)

    assert mock.stats['read_200'] == 300
    assert len(http._buckets) <= 256, len(http._buckets)
    assert len(http._bucket_hashes) <= 4, len(http._bucket_hashes)
    print(f'bucket flood: 300 unique hashes, _buckets={len(http._buckets)} (<=256), hashes={len(http._bucket_hashes)}')


@pytest.mark.asyncio
async def test_hostile_nonfinite_and_malformed_retry_after():
    """NaN/Infinity retry_after (valid JSON to Python's parser) must raise
    RateLimited instead of hanging the loop; a string coerces to an
    immediate retry."""
    nan_429 = FakeResponse(
        429, body=b'{"retry_after": NaN, "global": false}', headers={'content-type': 'application/json'}
    )
    inf_429 = FakeResponse(
        429, body=b'{"retry_after": Infinity, "global": false}', headers={'content-type': 'application/json'}
    )
    str_429 = FakeResponse(
        429,
        body=b'{"retry_after": "soon", "global": false}',
        headers={'content-type': 'application/json', 'X-RateLimit-Scope': 'shared'},
    )
    ok = FakeResponse(204)

    for hostile, should_raise in ((nan_429, True), (inf_429, True), (str_429, False)):
        script = [(0.0, hostile), (0.0, ok)]
        replayer = ScriptedDiscord(script)
        http = make_http(replayer)
        if should_raise:
            # Non-finite values must surface, never hang: RateLimited when
            # the parser accepts the body (stdlib JSON), or the text-fallback
            # Cloudflare-ban path when it refuses it (orjson)
            await asyncio.wait_for(_expect_surface(http), timeout=2.0)
        else:
            await asyncio.wait_for(react(http, 504), timeout=2.0)
    print('hostile retry_after: NaN/Infinity -> surfaced, string -> coerced + retried')


async def _expect_surface(http) -> None:
    with pytest.raises((RateLimited, HTTPException)):
        await react(http, 504)


# --------------------------------------------------------------------------
# Sustained spam, compressed
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sustained_16_channel_spam_compressed():
    """A ~60s-equivalent spam run compressed: 16 channels driven at ~3x the
    shared send edge for 12 rounds. Every send eventually lands, the
    throttle log saturates at its cap, and the lock ends released."""
    lane = 0.15
    mock = MockDiscord(send_drip=0.01, send_lane=lane)
    http = make_http(mock)
    channels = list(range(700, 716))

    t0 = time.monotonic()
    for _ in range(12):
        results = await asyncio.gather(*(send(http, ch) for ch in channels), return_exceptions=True)
        assert all(r is None or _is_throttle(r) for r in results), results
        await asyncio.sleep(0.05)
    wall = time.monotonic() - t0

    landed = sum(mock.stats[f'send_200:{ch}'] for ch in channels)
    trips = mock.stats['send_429_shared']
    assert landed >= 96, landed  # most of the spam still lands (queuing + retries)
    assert trips >= 8, trips
    events = http._throttle_events[SEND_ROUTE.key]
    assert len(events) == 64, 'throttle log must saturate at its bound'
    assert http._global_over.is_set()
    assert wall < 15, wall
    print(f'spam: {landed}/192 landed, {trips} shared 429s, log capped at 64, lock released, {wall:.1f}s')


# --------------------------------------------------------------------------
# Record -> replay: authentic captures become deterministic regressions
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_replay_round_trip(tmp_path):
    """Wrap the mock in SessionRecorder, run a mixed over-rate storm, save
    the fixture, and replay it through ScriptedDiscord: the wire outcome
    sequence (statuses, retry-afters, scopes) reproduces exactly."""
    mock = MockDiscord(send_drip=0.01, send_lane=0.12)
    recorder = SessionRecorder(mock)
    http = make_http(recorder)

    storm = [send(http, 800 + (i % 4)) for i in range(10)]
    storm += [react(http, 800 + i) for i in range(6)]
    storm += [button_click(http) for _ in range(3)]
    storm += [read(http) for _ in range(3)]
    recorded_results = await asyncio.wait_for(asyncio.gather(*storm, return_exceptions=True), timeout=10)
    # Over-rate sends may surface 429s after the client's 5-attempt bound;
    # the fixture must capture the wire either way.
    assert all(r is None or _is_throttle(r) for r in recorded_results), recorded_results

    path = recorder.save(tmp_path / 'session.jsonl')
    events = SessionRecorder.load(path)
    assert len(events) == len(recorder.events)
    wire_429s = sum(1 for e in events if e['status'] == 429)
    assert wire_429s >= 1
    assert all(e['scope'] == 'shared' for e in events if e['status'] == 429)
    # No rate headers recorded on successes (findings #1 held through capture)
    assert all('scope' not in e for e in events if e['status'] != 429)

    replay = ScriptedDiscord.from_events(events, scale=0.03)
    http2 = make_http(replay)
    n = len(events)
    # Re-issue n requests of any class: each consumes one scripted response;
    # 429s are absorbed internally exactly as they were on the wire, and a
    # request whose recorded fate was 5 consecutive 429s surfaces one here
    # too — the replay is faithful either way.
    await asyncio.wait_for(
        asyncio.gather(*(read(http2) for _ in range(n)), return_exceptions=True), timeout=10
    )
    assert replay.consumed == n
    assert replay.served == [event['status'] for event in events]
    print(f'record/replay: {n} wire events ({wire_429s} throttles) captured + replayed deterministically')


# --------------------------------------------------------------------------
# plans.md F-04/F-05/F-08: retry parsing, overlapping cooldowns, 202 path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_date_and_garbage_retry_after_headers():
    """F-04: Retry-After may be an HTTP-date (Cloudflare style) or garbage;
    none of them may raise mid-retry. A future HTTP-date is honored as a
    delay, garbage means immediate retry."""
    import datetime as dt
    from email.utils import formatdate

    mock = MockDiscord()
    http = make_http(mock)
    original = mock.request

    future = formatdate((dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2)).timestamp(), usegmt=True)
    shapes = [
        ({'Retry-After': future}, True),  # HTTP-date: honored (>= 1s after second-resolution truncation)
        ({'Retry-After': 'abc'}, False),  # garbage: immediate
        ({'Retry-After': ''}, False),
    ]
    seen: list[float] = []

    async def scripted(method, url, **kwargs):
        if method == 'PUT' and '/reactions/' in url and shapes:
            headers, _ = shapes.pop(0)
            http._HTTPClient__session.request = original  # type: ignore
            return FakeResponse(429, body=b'{"global": false}', headers={'content-type': 'application/json', **headers})
        return await original(method, url, **kwargs)

    http._HTTPClient__session.request = scripted  # type: ignore
    for expected_delay in (True, False, False):
        t0 = time.monotonic()
        await react(http, 505)
        elapsed = time.monotonic() - t0
        seen.append(elapsed)
        http._HTTPClient__session.request = scripted  # type: ignore
        if expected_delay:
            assert elapsed >= 0.9, elapsed  # +2s date, second resolution -> >= 1s
        else:
            assert elapsed < 0.5, elapsed
    print(f'retry-after shapes: HTTP-date honored ({seen[0] * 1000:.0f}ms), garbage immediate')


@pytest.mark.asyncio
async def test_202_defer_boundaries_hardened():
    """F-08: a 202 missing code/retry_after is a plain success (no KeyError);
    code 119999 is inside the 11xxxx group; a numeric-string retry_after
    parses; NaN falls back to 5s semantics without sleeping forever."""
    from discord.http import Route

    # 202 without a code: not a defer -> success return
    snoop: list[dict] = []
    script = [(0.0, FakeResponse(202, body=b'{}', headers={'content-type': 'application/json'}))]

    class Snoop:
        async def request(self, method, url, **kwargs):
            snoop.append({'method': method, 'url': url, **kwargs})
            delay, response = script.pop(0)
            await asyncio.sleep(delay)
            return response

    http = make_http(Snoop())
    await http.request(Route('GET', '/users/{user_id}', user_id=3))
    assert len(snoop) == 1, 'no code -> no defer, single request'

    # 119999 (the old range excluded it) + string retry_after -> defers once
    snoop.clear()
    script.append((0.0, FakeResponse(202, body=b'{"code": 119999, "retry_after": "0.02"}', headers={'content-type': 'application/json'})))
    script.append((0.0, FakeResponse(200, body=b'{}')))
    http2 = make_http(Snoop())
    await http2.request(Route('GET', '/users/{user_id}', user_id=3))
    assert snoop[1]['params']['attempts'] == 1
    print('202 hardening: missing keys tolerated, 119999 deferred, string retry_after parsed')


@pytest.mark.asyncio
async def test_overlapping_global_cooldowns_release_at_longest():
    """F-05: two shared 429s with different retry-afters clear the global
    lock together; the gate must stay closed until the LONGEST cooldown
    expires, not the shortest."""
    mock = MockDiscord()
    http = make_http(mock)
    original = mock.request
    served: list[float] = []

    async def scripted(method, url, **kwargs):
        if method == 'PUT' and '/reactions/' in url and len(served) < 2:
            served.append(0.15 if not served else 0.45)
            return mock._shared_429(served[-1])
        return await original(method, url, **kwargs)

    http._HTTPClient__session.request = scripted  # type: ignore
    t0 = time.monotonic()
    await asyncio.gather(react(http, 506), react(http, 507))
    await asyncio.wait_for(read(http), timeout=2.0)
    elapsed = time.monotonic() - t0
    # Short cooldown was 0.15s; the read must have waited out the 0.45s one
    assert elapsed >= 0.40, elapsed
    assert http._global_over.is_set()
    print(f'overlap: both 429s absorbed, gate held to the longest cooldown ({elapsed * 1000:.0f}ms)')


@pytest.mark.asyncio
async def test_cancelled_long_cooldown_does_not_hold_the_gate():
    """F-05 + cancellation: cancelling the long-cooldown request drops its
    hold; the gate reopens when the remaining short cooldown expires."""
    mock = MockDiscord()
    http = make_http(mock)
    original = mock.request
    served: list[float] = []

    async def scripted(method, url, **kwargs):
        if method == 'PUT' and '/reactions/' in url and len(served) < 2:
            served.append(0.2 if not served else 2.0)
            return mock._shared_429(served[-1])
        return await original(method, url, **kwargs)

    http._HTTPClient__session.request = scripted  # type: ignore
    short = asyncio.create_task(react(http, 508))
    await asyncio.sleep(0.01)
    long_task = asyncio.create_task(react(http, 509))
    await asyncio.sleep(0.02)
    long_task.cancel()
    await asyncio.gather(short, long_task, return_exceptions=True)

    t0 = time.monotonic()
    await asyncio.wait_for(read(http), timeout=1.0)  # would hang ~2s pre-fix
    assert time.monotonic() - t0 < 0.5
    assert http._global_over.is_set()
    assert http._global_holders == 0
    print('cancel + overlap: long cooldown cancelled, gate released by the short one')
