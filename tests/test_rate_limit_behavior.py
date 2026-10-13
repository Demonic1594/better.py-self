"""
Rate-limit behavior informed by the live-session user-account study:
no X-RateLimit headers are served (buckets only surface on 429s),
reactions share an account-wide bucket (x-ratelimit-scope: shared), and
sends are queued server-side (latency, not 429, is the signal).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from discord import Client
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


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        response, delay = self.script.pop(0)
        await asyncio.sleep(delay)
        return response


def make_http(script, **kwargs):
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
    http._HTTPClient__session = FakeSession(script)
    return http


@pytest.mark.asyncio
async def test_shared_scope_429_engages_global_lock():
    """A 429 carrying x-ratelimit-scope: shared (the account-wide reaction
    bucket) must gate ALL routes via the global lock, not just the tripped
    route — per-route pacing under-paces a shared limit."""
    body = b'{"retry_after": 0.05, "global": false}'
    script = [
        (
            FakeResponse(
                429,
                body,
                headers={
                    'content-type': 'application/json',
                    'X-RateLimit-Scope': 'shared',
                    'X-Ratelimit-Remaining': '0',
                },
            ),
            0.0,
        ),
        (FakeResponse(200, b'{"ok":1}'), 0.0),
    ]
    http = make_http(script)
    route = Route(
        'PUT', '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me', channel_id=1, message_id=2, emoji='x'
    )
    other = Route('POST', '/channels/{channel_id}/messages', channel_id=999)

    # Script ordering: the 429 first, then one response for the retry and
    # one for the gated request (it will pop whichever comes first after)
    http._HTTPClient__session.script.append((FakeResponse(200, b'{}'), 0.0))

    main = asyncio.ensure_future(http.request(route))
    # Wait for the 429's retry window to open the cooldown
    for _ in range(200):
        await asyncio.sleep(0.005)
        if not http._global_over.is_set():
            break
    assert not http._global_over.is_set(), 'shared-scope cooldown must engage the global lock'

    # A different route arriving mid-cooldown must be gated by the global lock
    gate = asyncio.ensure_future(http.request(other, json={'x': 1}))
    await asyncio.sleep(0.02)
    assert not gate.done(), 'other route must be blocked while the shared cooldown holds'

    result = await asyncio.wait_for(main, timeout=5)
    assert isinstance(result, dict)  # retry succeeded after the cooldown
    await asyncio.wait_for(gate, timeout=5)
    assert http._global_over.is_set()
    print('shared-scope 429: global lock engaged during cooldown, released after; other route gated')


@pytest.mark.asyncio
async def test_local_scope_429_stays_per_route():
    body = b'{"retry_after": 0.02, "global": false}'
    script = [(FakeResponse(429, body, headers={'content-type': 'application/json'}), 0.0), (FakeResponse(200, b'{}'), 0.0)]
    http = make_http(script)
    route = Route('POST', '/channels/{channel_id}/messages', channel_id=1)
    await asyncio.wait_for(http.request(route), timeout=5)
    assert http._global_over.is_set(), 'route-local 429 must not engage the global lock'
    print('route-local 429 (no scope header): global lock untouched')


@pytest.mark.asyncio
async def test_route_latency_ewma_tracks_congestion():
    """Latency EWMA per route: the queuing-congestion signal for accounts
    that never receive rate headers."""
    # One-off spikes barely move a 1/8 EWMA; sustained queuing converges
    script = [(FakeResponse(200), 0.00), (FakeResponse(200), 0.30)] + [(FakeResponse(200), 0.30)] * 8
    http = make_http(script)
    route = Route('POST', '/channels/{channel_id}/messages', channel_id=1)
    ewmas = []
    for _ in range(10):
        await http.request(route)
        ewmas.append(http._route_latencies[route.key])
    ewma = ewmas[-1]
    assert ewma > 0.2, ewma  # converged toward the sustained 0.30s latency
    assert ewmas[1] < 0.1  # a single spike barely moved it (denoised)
    assert ewmas[-1] > ewmas[3] > ewmas[1]  # monotone rise under sustained queuing
    other = Route('GET', '/users/@me')
    http._HTTPClient__session.script.append((FakeResponse(200), 0.0))
    await http.request(other)
    assert other.key in http._route_latencies
    assert http._route_latencies[other.key] < 0.05
    print(f'latency EWMA: congested route at {ewma:.2f}s, clean route at {http._route_latencies[other.key]:.3f}s')


@pytest.mark.asyncio
async def test_client_exposes_route_latencies():
    client = Client()
    await client._async_setup_hook()
    latencies = client.http_route_latencies()
    assert isinstance(latencies, dict)
    # The returned dict is a copy: mutating it must not touch the tracker
    latencies['bogus'] = 1.0
    assert 'bogus' not in client.http._route_latencies
    print('Client.http_route_latencies(): copy semantics verified')
