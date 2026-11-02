"""
Memory regressions: per-member cache footprint, presences not duplicating
raw activity payloads, and the login header lookup not importing aiohttp.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

import discord
from discord.state import Presence
from discord.tracking import _CurlFetchSession, _resolve_proxy_auth

from memory_bench import measure


@pytest.mark.asyncio
async def test_member_cache_bytes_per_member_budget():
    count = 10_000
    kwargs = {'max_member_cache': None, 'max_total_member_cache': None}
    base = await measure(**kwargs)
    full = await measure(members=count, **kwargs)
    assert full['members'] == count
    per_member = (full['retained_bytes'] - base['retained_bytes']) / count
    # ~810 B after dropping the raw role/joined_at copies (was ~1015 B);
    # the bound leaves headroom for interpreter differences
    assert per_member < 900, f'{per_member:.0f} B per cached member'


def test_member_does_not_keep_raw_payload_copies():
    assert '_raw_roles' not in discord.Member.__slots__
    assert '_raw_joined_at' not in discord.Member.__slots__
    assert '_raw_activities' not in Presence.__slots__


def test_login_header_lookup_does_not_import_aiohttp():
    # Fresh interpreter: the network fetch is stubbed, everything else is real
    code = textwrap.dedent(
        """
        import asyncio, sys
        import discord
        from discord import client as client_module

        seen = {}

        async def fake_default(session, proxy=None, proxy_auth=None):
            seen['session'] = type(session).__name__
            return 'ctx'

        client_module.HeadersContext.default = fake_default

        async def main():
            c = discord.Client()
            assert await c.headers_context() == 'ctx'

        asyncio.run(main())
        print(seen['session'], 'aiohttp' in sys.modules)
        """
    )
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ['_CurlFetchSession', 'False']


class _FakeCurlResponse:
    status_code = 200
    reason = 'OK'
    ok = True
    headers = {'x': '1'}
    text = 'body'

    def json(self):
        return {'a': 1}


class _FakeCurlSession:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _FakeCurlResponse()


@pytest.mark.asyncio
async def test_curl_fetch_session_mimics_aiohttp_response_api():
    raw = _FakeCurlSession()
    session = _CurlFetchSession(raw)  # type: ignore[arg-type]

    async with session.get('https://example', proxy=None, proxy_auth=('u', 'p'), headers={'h': 'v'}) as resp:
        assert (resp.status, resp.reason, resp.ok) == (200, 'OK', True)
        assert await resp.text() == 'body'
        assert await resp.json() == {'a': 1}

    class BasicAuthLike:
        login, password = 'l', 'pw'

    async with session.post('https://example', proxy='http://p', proxy_auth=BasicAuthLike()):
        pass

    assert raw.calls[0] == ('GET', 'https://example', {'proxy_auth': ('u', 'p'), 'headers': {'h': 'v'}})
    assert raw.calls[1] == ('POST', 'https://example', {'proxy': 'http://p', 'proxy_auth': ('l', 'pw')})


def test_resolve_proxy_auth_tuple_forms():
    # Regression: this raised NameError (BasicAuth was a TYPE_CHECKING-only
    # import), silently forcing the fallback build number for proxied logins
    assert _resolve_proxy_auth(None) is None
    session = _CurlFetchSession(_FakeCurlSession())  # type: ignore[arg-type]
    assert _resolve_proxy_auth(('u', 'p'), session) == ('u', 'p')

    import aiohttp

    auth = _resolve_proxy_auth(('u', 'p'))
    assert isinstance(auth, aiohttp.BasicAuth) and auth.login == 'u'
    assert _resolve_proxy_auth(auth) is auth
