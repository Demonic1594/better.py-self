"""
The push signal (on_http_throttle), the drop-in lane governor implementing
the validated pacing recipe (examples/lane_governor.py), and per-account
bucket isolation (TIMING.md: buckets are per-account — N accounts, N lanes).
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from traffic_harness import (
    REACTION_ROUTE,
    MockDiscord,
    button_click,
    make_http,
    react,
    send,
)

_spec = importlib.util.spec_from_file_location(
    'lane_governor', Path(__file__).parent.parent / 'examples' / 'lane_governor.py'
)
lane_governor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lane_governor)
LaneGovernor = lane_governor.LaneGovernor
classify_route = lane_governor.classify_route


# --------------------------------------------------------------------------
# on_http_throttle push event
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_throttle_dispatches_per_wire_event():
    mock = MockDiscord()
    http = make_http(mock)
    fired: list[tuple] = []
    http.client = SimpleNamespace(
        dispatch=lambda event, *args: fired.append((event, *args)),
        _connection=SimpleNamespace(installation_id=None, locale='en-US'),
    )

    await asyncio.gather(*(react(http, ch) for ch in (10, 11, 12, 13)))
    wire = mock.stats['reaction_429_shared']
    throttles = [f for f in fired if f[0] == 'http_throttle']
    assert len(throttles) == wire >= 1
    route, retry_after, scope = throttles[0][1:]
    assert route == REACTION_ROUTE.key
    assert retry_after == 0.08
    assert scope == 'shared'
    print(f'dispatch: {len(throttles)} push events, exact match with wire')


@pytest.mark.asyncio
async def test_dispatch_failure_never_breaks_the_retry():
    mock = MockDiscord()
    http = make_http(mock)

    def exploding(event, *args):
        raise RuntimeError('listener bug')

    http.client = SimpleNamespace(dispatch=exploding, _connection=SimpleNamespace(installation_id=None, locale='en-US'))
    await asyncio.gather(*(react(http, ch) for ch in (20, 21)))  # trips + retries to success
    assert mock.stats['reaction_204'] == 2
    print('dispatch: exploding listener contained, request still completed')


# --------------------------------------------------------------------------
# LaneGovernor
# --------------------------------------------------------------------------


def _governor(**kwargs):
    defaults = dict(
        send=0.06,
        react=0.08,
        click=0.08,
        jitter=(1.0, 1.0),  # deterministic in tests
        padding=0.02,
        backoff=2.0,
        probe_after=0.5,
        probe_step=0.10,
        ewma_factor=3.0,
    )
    defaults.update(kwargs)
    return LaneGovernor(None, **defaults)


@pytest.mark.asyncio
async def test_governor_clean_pace_never_trips():
    """Governor base intervals at/above the server lane: zero throttles,
    zero probes needed, everything lands."""
    mock = MockDiscord(send_drip=0.005, send_lane=0.05)
    http = make_http(mock)
    gov = _governor()

    for i in range(10):
        await gov.run(lambda ch=900 + i: send(http, ch), lane='send')
    for ch in (901, 902):
        await gov.run(lambda ch=ch: react(http, ch), lane='react')
        await asyncio.sleep(0.02)
    await gov.run(lambda: button_click(http), lane='click')

    assert mock.stats['send_200:909'] >= 1
    assert 'send_429_shared' not in mock.stats
    assert 'reaction_429_shared' not in mock.stats
    assert gov.stats == {'actions': 13, 'throttles': 0, 'shared_pauses': 0, 'probes': 0, 'ewma_backoffs': 0}
    assert gov.intervals() == {'send': 0.06, 'react': 0.08, 'click': 0.08}
    print('governor: clean pace, 13 actions, zero throttle pressure')


@pytest.mark.asyncio
async def test_governor_backs_off_on_throttle_and_stabilizes():
    """Base interval under the server lane edge: throttles trip, the
    governor doubles the lane, and the remaining actions run clean."""
    mock = MockDiscord(send_drip=0.005, send_lane=0.25)
    http = make_http(mock)
    gov = _governor(send=0.06)  # 3 completions inside the 0.5s window -> trips

    dispatched: list[tuple] = []
    http.client = SimpleNamespace(
        dispatch=lambda event, *args: dispatched.append((event, *args)) if event == 'http_throttle' else None,
        _connection=SimpleNamespace(installation_id=None, locale='en-US'),
    )

    from discord.errors import HTTPException

    surfaced = 0
    for i in range(12):
        try:
            await gov.run(lambda ch=910 + (i % 3): send(http, ch), lane='send')
        except HTTPException as exc:
            assert exc.status == 429
            surfaced += 1
        for event_tuple in dispatched:
            gov.on_throttle(*event_tuple[1:])
            dispatched.remove(event_tuple)

    assert mock.stats.get('send_429_shared', 0) >= 1
    assert gov.stats['throttles'] >= 1
    assert gov.lanes['send'].current == pytest.approx(0.06 * 2 ** gov.lanes['send'].trips)
    landed = sum(mock.stats[f'send_200:{910 + ch}'] for ch in range(3))
    assert landed + surfaced == 12  # everything landed or honestly surfaced
    print(f'governor: {gov.stats["throttles"]} trips -> send lane now {gov.lanes["send"].current:.3f}s, all 12 landed')


@pytest.mark.asyncio
async def test_governor_shared_scope_pauses_all_lanes():
    gov = _governor()
    gov.on_throttle(REACTION_ROUTE.key, 0.05, 'shared')
    now = time.monotonic()
    assert all(gov._next_at[lane] >= now + 0.05 + gov.padding - 0.001 for lane in lane_governor.LANES)
    assert gov.stats['shared_pauses'] == 1
    # A click immediately after must wait out the pause.
    mock = MockDiscord()
    http = make_http(mock)
    t0 = time.monotonic()
    await asyncio.wait_for(gov.run(lambda: button_click(http), lane='click'), timeout=1.0)
    assert time.monotonic() - t0 >= 0.05
    print('governor: shared scope pauses all three lanes for retry-after + padding')


@pytest.mark.asyncio
async def test_governor_probe_down_bounded():
    gov = _governor(send=0.06)
    gov.lanes['send'].current = 0.12
    gov.lanes['send'].last_changed = time.monotonic() - 10.0  # long clean stretch
    mock = MockDiscord(send_drip=0.005)
    http = make_http(mock)
    await gov.run(lambda: send(http, 920), lane='send')

    assert gov.stats['probes'] == 1
    assert gov.lanes['send'].current == pytest.approx(0.12 * 0.9)  # <=10% decay
    # Probing never crosses below the validated base.
    gov.lanes['send'].current = 0.061
    gov.lanes['send'].last_changed = time.monotonic() - 10.0
    await gov.run(lambda: send(http, 921), lane='send')
    assert gov.lanes['send'].current == 0.06
    # A trip resets the probe clock.
    gov.on_throttle('POST /channels/{channel_id}/messages', 0.01, None)
    assert gov.lanes['send'].current == 0.06 * 2
    print('governor: probe down <=10% after clean stretch, floored at base, reset on trip')


def test_governor_ewma_observe_backs_off_gently():
    gov = _governor()
    gov.observe({'POST /channels/{channel_id}/messages': 0.05})  # baseline
    gov.observe({'POST /channels/{channel_id}/messages': 0.20})  # 4x baseline = queuing
    assert gov.stats['ewma_backoffs'] == 1
    assert gov.lanes['send'].current == pytest.approx(0.06 * 1.25)
    # Unrelated routes are ignored.
    gov.observe({'PUT /channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me': 5.0})
    assert gov.stats['ewma_backoffs'] == 1
    print('governor: EWMA congestion signal backs off the send lane only')


def test_classify_route():
    assert classify_route('PUT /channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me') == 'react'
    assert classify_route('POST /interactions') == 'click'
    assert classify_route('POST /channels/{channel_id}/messages') == 'send'
    assert classify_route('GET /users/{user_id}') is None


# --------------------------------------------------------------------------
# Per-account bucket isolation (TIMING.md: N accounts = N parallel lanes)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_account_buckets_isolate_lanes():
    mock = MockDiscord(send_drip=0.005, send_lane=0.15, per_account_buckets=True)
    http_a = make_http(mock, token='acct-a')
    http_b = make_http(mock, token='acct-b')

    # Account A spams: concurrent reactions + sustained over-rate sends.
    async def spam_react() -> None:
        await asyncio.gather(*(react(http_a, ch) for ch in (930, 931, 932, 933)))

    async def spam_send() -> None:
        for _ in range(8):
            await send(http_a, 930)
            await asyncio.sleep(0.04)

    # Account B paces cleanly on the same channels through its own lanes.
    async def clean_b() -> None:
        for _ in range(4):
            await asyncio.sleep(0.12)
            await react(http_b, 930)
            await send(http_b, 930)
            await asyncio.sleep(0.08)

    await asyncio.wait_for(asyncio.gather(spam_react(), spam_send(), clean_b(), return_exceptions=True), timeout=10)

    assert mock.stats['reaction_429_shared:acct-a'] >= 1
    assert 'reaction_429_shared:acct-b' not in mock.stats
    assert mock.stats['send_429_shared:acct-a'] >= 1
    assert 'send_429_shared:acct-b' not in mock.stats
    assert mock.stats['reaction_204:acct-b'] == 4
    landed_b = mock.stats['send_200:930:acct-b']
    assert landed_b == 4
    print(
        f'isolation: A tripped {mock.stats["reaction_429_shared:acct-a"]} reaction + '
        f'{mock.stats["send_429_shared:acct-a"]} send throttles; B: zero, 4/4 reactions + {landed_b}/4 sends landed'
    )
