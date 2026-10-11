"""
Fixture auto-discovery: every ``tests/fixtures/*.jsonl`` produced by
SessionRecorder (from the mock, or from a host's own live capture) is
replayed through ScriptedDiscord and must reproduce its recorded wire
outcome sequence exactly. Drop a new fixture in the directory and it is a
regression test immediately — no wiring needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from traffic_harness import SessionRecorder, ScriptedDiscord, fixture_report, make_http

FIXTURE_DIR = Path(__file__).parent / 'fixtures'
FIXTURES = sorted(FIXTURE_DIR.glob('*.jsonl'))


@pytest.mark.skipif(not FIXTURES, reason='no recorded fixtures present')
@pytest.mark.parametrize('fixture', FIXTURES, ids=lambda p: p.name)
@pytest.mark.asyncio
async def test_fixture_replays_deterministically(fixture: Path):
    events = SessionRecorder.load(fixture)
    assert events, f'{fixture.name} is empty'

    replay = ScriptedDiscord.from_events(events, scale=0.02)
    http = make_http(replay)
    n = len(events)
    # Re-issue n requests of any class; each consumes one scripted response.
    # Recorded 429s are absorbed exactly as they were on the wire, and a
    # request whose recorded fate was 5 consecutive 429s surfaces one here
    # too — the replay is faithful either way.
    await asyncio.wait_for(asyncio.gather(*(http.get_user(2) for _ in range(n)), return_exceptions=True), timeout=30)

    assert replay.consumed == n
    assert replay.served == [event['status'] for event in events]


@pytest.mark.skipif(not FIXTURES, reason='no recorded fixtures present')
def test_fixtures_are_sane():
    """Every committed fixture must be non-degenerate: real traffic mix,
    bounded latency, and throttles that carry a scope when present."""
    for fixture in FIXTURES:
        events = SessionRecorder.load(fixture)
        report = fixture_report(events)
        assert report['events'] >= 10, (fixture.name, report)
        assert report['statuses'], (fixture.name, report)
        if report['throttle_ratio']:
            assert report['throttle_scopes'], (fixture.name, report)
        assert report['latency']['max_s'] < 60, (fixture.name, report)


def test_fixture_report_shape():
    events = [
        {'t': 0.0, 'method': 'POST', 'url': 'https://discord.com/api/v9/channels/1/messages', 'status': 200, 'latency': 0.4},
        {'t': 1.2, 'method': 'PUT', 'url': 'https://discord.com/api/v9/channels/1/messages/2/reactions/x/@me', 'status': 429, 'latency': 0.1, 'retry_after': 1, 'scope': 'shared'},
        {'t': 3.0, 'method': 'POST', 'url': 'https://discord.com/api/v9/interactions', 'status': 204, 'latency': 0.2},
    ]
    report = fixture_report(events)
    assert report['events'] == 3
    assert report['statuses'] == {'200': 1, '204': 1, '429': 1}
    assert report['throttle_ratio'] == round(1 / 3, 4)
    assert report['throttle_scopes'] == {'shared': 1}
    assert report['routes']['POST channels/{id}/messages'] == 1
    assert report['routes']['PUT channels/{id}/messages/{id}/reactions'] == 1
    assert report['latency'] == {'p50_s': 0.2, 'max_s': 0.4}
