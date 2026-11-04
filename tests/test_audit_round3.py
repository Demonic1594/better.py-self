"""Regression tests for audit round 3: compatibility, rate limits and HTTP."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

import discord
from discord import Client
from discord.http import Route
from test_persistence import drive_ready, guild_payload, ready_payload, up
from test_rate_limit_behavior import FakeResponse, make_http


@pytest.mark.asyncio
async def test_http_edit_profile_alias():
    """B4-02: upstream's HTTPClient.edit_profile (renamed edit_current_user here) still works."""
    http = make_http([(FakeResponse(200, b'{"id":"1"}'), 0.0)])
    assert await http.edit_profile({'bio': 'x'}) == {'id': '1'}
    method, url, kwargs = http._HTTPClient__session.requests[0]
    assert method == 'PATCH' and url.endswith('/users/@me')
    assert kwargs['data'] == '{"bio":"x"}'


async def _ready_client(**options):
    client = Client(**options)
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(state=state, data=up(5))  # static_login stand-in
    return client


@pytest.mark.asyncio
async def test_member_collectibles_is_both_attribute_and_method():
    """B4-03: upstream calls member.collectibles() (a method, as on User); ours is a property."""
    client = await _ready_client()
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, n_members=2)]
    await drive_ready(client, ready)
    member = client.get_guild(1).get_member(100)
    assert member.collectibles == []
    assert member.collectibles() == []
    assert member.collectibles() is not None and list(member.collectibles) == member.collectibles()


@pytest.mark.asyncio
async def test_retry_waits_for_a_global_cooldown_engaged_meanwhile():
    """A2-02: the global lock was checked only before the first attempt, so a retry
    after a 5xx ignored a global cooldown that began during its backoff."""
    http = make_http([(FakeResponse(502, b'{}'), 0.0), (FakeResponse(200, b'{}'), 0.0)])
    http._record_retry = lambda *args: 0.05
    task = asyncio.ensure_future(http.request(Route('GET', '/users/@me')))
    requests = http._HTTPClient__session.requests
    for _ in range(100):
        await asyncio.sleep(0)
        if requests:
            break
    http._global_over.clear()  # another request hit a global 429 during the backoff
    await asyncio.sleep(0.2)
    assert len(requests) == 1 and not task.done(), 'the retry must wait for the global cooldown'
    http._global_over.set()
    assert await asyncio.wait_for(task, timeout=2) == {}
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_429_without_retry_after_waits_instead_of_spinning(caplog):
    """A2-03: a JSON 429 with no retry_after (and no Retry-After header) retried
    immediately, burning all five attempts in a tight loop."""
    script = [
        (
            FakeResponse(429, b'{"message":"slow down"}', headers={'content-type': 'application/json', 'Via': '1.1 google'}),
            0.0,
        )
    ]
    script.append((FakeResponse(200, b'{}'), 0.0))
    http = make_http(script)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger='discord.http'):
        assert await http.request(Route('GET', '/users/@me')) == {}
    assert time.monotonic() - started >= 0.9
    assert any('Retrying in 1.00 seconds' in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_per_call_headers_override_client_wide_headers():
    """A2-07: client-wide extra_headers overwrote headers passed to a single request."""
    http = make_http([(FakeResponse(200, b'{}'), 0.0)])
    http.extra_headers = {'X-Test': 'client', 'X-Only-Client': '1'}
    await http.request(Route('GET', '/users/@me'), headers={'X-Test': 'call'})
    headers = http._HTTPClient__session.requests[0][2]['headers']
    assert headers['X-Test'] == 'call'
    assert headers['X-Only-Client'] == '1'


@pytest.mark.asyncio
async def test_first_cache_eviction_is_logged_once_at_info(caplog):
    """B4-01: cache caps evicted silently below DEBUG; the first eviction of each
    cache is now logged once at INFO."""
    client = await _ready_client(max_member_cache=3)
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, n_members=10), guild_payload(2, n_members=10)]
    with caplog.at_level(logging.INFO, logger='discord.state'):
        await drive_ready(client, ready)
        guild = client.get_guild(1)
        guild._evict_members(1)
    assert len(guild._members) <= 3
    notices = [r for r in caplog.records if r.levelno == logging.INFO and 'member cache reached its cap' in r.getMessage()]
    assert len(notices) == 1
