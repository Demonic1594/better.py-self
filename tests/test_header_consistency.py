"""The browser version the client advertises (User-Agent, Sec-CH-UA, super
properties) must match the TLS fingerprint curl_cffi presents, as it does in a
real browser."""

from __future__ import annotations

import base64
import json

import pytest

from discord.tracking import HeadersContext

TARGETS = [(124, 'chrome124'), (131, 'chrome131'), (145, 'chrome145'), (152, 'chrome152')]


@pytest.fixture(autouse=True)
def fixed_targets(monkeypatch):
    # Independent of the installed curl_cffi version
    monkeypatch.setattr(HeadersContext, '_tls_chrome_targets', staticmethod(lambda: list(TARGETS)), raising=False)


def _web_properties(version: int, os: str = 'Windows', browser: str = 'Chrome') -> dict:
    platform = {'Windows': 'Windows', 'Mac OS X': 'macOS', 'Linux': 'Linux'}[os]
    brand = 'Edg' if browser == 'Edge' else None
    properties = {
        'os': os,
        'browser': browser,
        'browser_version': f'{version}.0.0.0',
        'browser_user_agent': HeadersContext.format_chromium_user_agent(version, platform, brand),  # type: ignore
        'client_build_number': 681034,
    }
    encoded = base64.b64encode(json.dumps(properties).encode()).decode()
    return {'properties': properties, 'encoded': encoded, 'extra_gateway_properties': {'is_fast_connect': False}}


def _decoded(context: HeadersContext) -> dict:
    return json.loads(base64.b64decode(context.encoded_super_properties))


def _patch_info_api(monkeypatch, answer: dict) -> None:
    async def api(*args, **kwargs):
        return answer

    monkeypatch.setattr(HeadersContext, 'fetch_api_properties', staticmethod(api))


@pytest.mark.asyncio
async def test_info_api_version_newer_than_the_fingerprint_is_lowered(monkeypatch):
    """The info API reported Chrome 159 while curl_cffi presented a Chrome 152
    fingerprint; every header now says 152."""
    _patch_info_api(monkeypatch, _web_properties(159))
    context = await HeadersContext.default(None)  # type: ignore[arg-type]

    assert context.browser_major_version == 152
    assert context.impersonate == 'chrome152'
    assert context.super_properties['browser_version'] == '152.0.0.0'
    assert 'Chrome/152.0.0.0 ' in context.user_agent
    assert '"Google Chrome";v="152"' in context.client_hints['Sec-CH-UA']
    # The encoded header is rebuilt from the edited properties, not the info API's copy
    assert _decoded(context)['browser_version'] == '152.0.0.0'
    assert _decoded(context)['browser_user_agent'] == context.user_agent
    # Everything else from the info API is kept
    assert context.super_properties['client_build_number'] == 681034
    assert context.extra_gateway_properties == {'is_fast_connect': False}


@pytest.mark.asyncio
async def test_info_api_version_within_the_fingerprints_is_kept(monkeypatch):
    answer = _web_properties(145)
    _patch_info_api(monkeypatch, answer)
    context = await HeadersContext.default(None)  # type: ignore[arg-type]

    assert context.browser_major_version == 145
    assert context.impersonate == 'chrome145'
    assert context.encoded_super_properties == answer['encoded']


@pytest.mark.asyncio
async def test_edge_keeps_its_brand_when_lowered(monkeypatch):
    _patch_info_api(monkeypatch, _web_properties(159, browser='Edge'))
    context = await HeadersContext.default(None)  # type: ignore[arg-type]

    assert context.browser_type == 'edge'
    assert context.user_agent.endswith('Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0')
    assert context.impersonate == 'chrome152'


@pytest.mark.asyncio
async def test_mac_os_x_is_reported_as_macos_in_client_hints(monkeypatch):
    """The super properties name the OS "Mac OS X"; Chrome's Sec-CH-UA-Platform says "macOS"."""
    _patch_info_api(monkeypatch, _web_properties(159, os='Mac OS X'))
    context = await HeadersContext.default(None)  # type: ignore[arg-type]

    assert context.platform == 'macOS'
    assert context.client_hints['Sec-CH-UA-Platform'] == '"macOS"'
    assert context.super_properties['os'] == 'Mac OS X'
    assert '(Macintosh; Intel Mac OS X 10_15_7)' in context.user_agent
    assert 'Chrome/152.0.0.0' in context.user_agent


@pytest.mark.asyncio
async def test_manual_fallback_does_not_advertise_a_newer_chrome(monkeypatch):
    async def api(*args, **kwargs):
        raise RuntimeError('info API down')

    async def build_number(*args, **kwargs):
        return 12345

    async def chrome_version(*args, **kwargs):
        return '159.0.8154.34'

    monkeypatch.setattr(HeadersContext, 'fetch_api_properties', staticmethod(api))
    monkeypatch.setattr(HeadersContext, 'scrape_client_build_number', staticmethod(build_number))
    monkeypatch.setattr(HeadersContext, 'fetch_chrome_version', staticmethod(chrome_version))
    context = await HeadersContext.default(None)  # type: ignore[arg-type]

    assert context.super_properties['browser_version'] == '152.0.0.0'
    assert 'Chrome/152.0.0.0 ' in context.user_agent
    assert context.impersonate == 'chrome152'


@pytest.mark.parametrize(
    'version, target',
    [(152, 'chrome152'), (160, 'chrome152'), (145, 'chrome145'), (130, 'chrome131'), (138, 'chrome131'), (100, 'chrome124')],
)
def test_impersonate_picks_the_closest_fingerprint(version, target):
    context = HeadersContext(super_properties={'browser_user_agent': 'ua'}, browser_major_version=version)
    assert context.impersonate == target


def test_electron_desktop_client_uses_its_chromium_version():
    context = HeadersContext(
        super_properties={'browser_user_agent': 'ua'}, browser_type='electron', browser_major_version=134
    )
    assert context.impersonate == 'chrome131'


def test_without_targets_the_default_fingerprint_is_used(monkeypatch):
    from curl_cffi import requests

    monkeypatch.setattr(HeadersContext, '_tls_chrome_targets', staticmethod(lambda: []))
    context = HeadersContext(super_properties={'browser_user_agent': 'ua'}, browser_major_version=152)
    assert context.impersonate == requests.impersonate.DEFAULT_CHROME


def test_api_requests_use_the_fetch_priority():
    """Chrome sends u=0 only for page navigations; fetch() calls are u=1."""
    context = HeadersContext(super_properties={'browser_user_agent': 'ua'}, browser_major_version=152)
    assert context.client_hints['Priority'] == 'u=1, i'


# --- Demonic1594/better.py-self#39: contexts built by hand --------------------------------

ANDROID_TARGETS = [(99, 'chrome99_android'), (131, 'chrome131_android')]


def test_ios_contexts_send_webkit_headers():
    """Every iOS browser is WebKit: Safari's TLS fingerprint went out with
    Chromium's Sec-CH-UA client hints, a combination no real client sends."""
    from curl_cffi import requests

    for browser in ('chrome', 'edge', 'firefox'):
        context = HeadersContext(
            platform='iOS',
            browser_type=browser,  # type: ignore[arg-type]
            browser_major_version=152,
            super_properties={'browser_user_agent': 'ua'},
        )
        assert context.impersonate == requests.impersonate.DEFAULT_SAFARI_IOS
        assert context.client_hints == {'Priority': 'u=3, i'}


@pytest.mark.parametrize(
    'version, target', [(159, 'chrome131_android'), (131, 'chrome131_android'), (100, 'chrome99_android')]
)
def test_android_contexts_use_the_closest_android_fingerprint(monkeypatch, version, target):
    monkeypatch.setattr(
        HeadersContext, '_tls_chrome_android_targets', staticmethod(lambda: list(ANDROID_TARGETS)), raising=False
    )
    context = HeadersContext(
        platform='Android',
        browser_major_version=version,
        super_properties={'browser_user_agent': HeadersContext.format_chromium_user_agent(version, 'Android')},
    )
    assert context.impersonate == target


def test_a_user_agent_for_another_platform_is_logged(caplog):
    """A Windows user agent with platform='Android' went out silently: Sec-CH-UA-Mobile ?1
    and Sec-CH-UA-Platform "Android" next to "Windows NT 10.0"."""
    with caplog.at_level('WARNING', logger='discord.tracking'):
        HeadersContext(
            platform='Android',
            browser_major_version=152,
            super_properties={'browser_user_agent': HeadersContext.format_chromium_user_agent(152)},
        )
    assert "platform is 'Android' but its user agent describes 'Windows'" in caplog.text


def test_a_user_agent_with_another_version_is_logged(caplog):
    with caplog.at_level('WARNING', logger='discord.tracking'):
        HeadersContext(
            browser_major_version=152,
            super_properties={'browser_user_agent': HeadersContext.format_chromium_user_agent(159)},
        )
    assert 'browser_major_version is 152 but its user agent says 159' in caplog.text


@pytest.mark.parametrize(
    'platform, user_agent',
    [
        ('Windows', HeadersContext.format_chromium_user_agent(152)),
        ('macOS', HeadersContext.format_chromium_user_agent(152, 'macOS')),
        ('Linux', HeadersContext.format_chromium_user_agent(152, 'Linux')),
        ('Android', HeadersContext.format_chromium_user_agent(152, 'Android')),
        ('Windows', 'a user agent naming no platform'),
        # iPadOS requests desktop sites with a Mac user agent
        (
            'iOS',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15',
        ),
    ],
)
def test_consistent_contexts_log_nothing(caplog, platform, user_agent):
    with caplog.at_level('WARNING', logger='discord.tracking'):
        HeadersContext(platform=platform, browser_major_version=152, super_properties={'browser_user_agent': user_agent})
    assert caplog.text == ''
