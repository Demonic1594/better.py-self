"""Button clicks and the interaction request path (offline).

Live runs measured clicks at ~620 ms p50 with a rare multi-second tail. Our side
of a click is one POST /interactions: these tests pin its payload and nonce
bookkeeping, and make sure a slow click caused by a retried request can be
diagnosed from the client (429s and transport retries are both recorded).
"""

from __future__ import annotations

import json
import logging

import pytest

import discord
import discord.http as http_module
from discord import Client
from discord.enums import InteractionType
from discord.http import Route

from test_persistence import drive_ready, guild_payload, ready_payload, up
from test_rate_limit_behavior import FakeResponse, make_http


def _message_payload(components):
    return {
        'id': '900',
        'channel_id': '501',
        'guild_id': '1',
        'author': dict(up(777), bot=True),
        'content': 'pick one',
        'timestamp': '2026-01-01T00:00:00+00:00',
        'edited_timestamp': None,
        'tts': False,
        'mention_everyone': False,
        'mentions': [],
        'mention_roles': [],
        'attachments': [],
        'embeds': [],
        'pinned': False,
        'type': 0,
        'flags': 0,
        'application_id': '777',
        'components': components,
    }


BUTTONS = [
    {
        'type': 1,
        'components': [
            {'type': 2, 'style': 1, 'label': 'Go', 'custom_id': 'go'},
            {'type': 2, 'style': 5, 'label': 'Docs', 'url': 'https://example.com'},
            {'type': 2, 'style': 6, 'sku_id': '42'},
        ],
    }
]


async def _message(components=BUTTONS):
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(state=state, data=up(5))
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, 1)]
    await drive_ready(client, ready)
    channel = client.get_channel(501)
    message = discord.Message(state=state, channel=channel, data=_message_payload(components))  # type: ignore
    return client, state, message


def _buttons(message):
    go, link, sku = message.components[0].children
    return go, link, sku


@pytest.mark.asyncio
async def test_button_click_sends_one_component_interaction(monkeypatch):
    client, state, message = await _message()
    go, _, _ = _buttons(message)
    calls = []

    async def interact(type, data, channel, message=None, *, nonce, **kwargs):
        calls.append((type, data, channel, message, nonce))

    monkeypatch.setattr(state.http, 'interact', interact)
    assert await go.click() is None
    assert len(calls) == 1, 'a click is exactly one request'
    type, data, channel, sent_message, nonce = calls[0]
    assert type is InteractionType.component
    assert data['custom_id'] == 'go' and data['component_type'] == 2
    assert channel.id == 501 and sent_message is message
    assert state._interaction_cache[nonce][0] == InteractionType.component.value, 'nonce kept to match the reply'


@pytest.mark.asyncio
async def test_failed_click_forgets_its_nonce(monkeypatch):
    client, state, message = await _message()
    go, _, _ = _buttons(message)

    async def interact(*args, nonce, **kwargs):
        raise discord.HTTPException(FakeResponse(400), 'nope')

    monkeypatch.setattr(state.http, 'interact', interact)
    with pytest.raises(discord.HTTPException):
        await go.click()
    assert not state._interaction_cache, 'a failed click must not leave a pending nonce behind'


@pytest.mark.asyncio
async def test_link_and_sku_buttons_send_nothing(monkeypatch):
    client, state, message = await _message()
    _, link, sku = _buttons(message)

    async def interact(*args, **kwargs):
        raise AssertionError('no request expected')

    monkeypatch.setattr(state.http, 'interact', interact)
    assert await link.click() == 'https://example.com'
    assert await sku.click() is None


@pytest.mark.asyncio
async def test_interaction_payload_shape():
    client, state, message = await _message()
    http = make_http([(FakeResponse(204, b''), 0.0)])
    go, _, _ = _buttons(message)
    await http.interact(InteractionType.component, go.to_interaction_dict(), message.channel, message, nonce='123')
    _, url, kwargs = http._HTTPClient__session.requests[0]
    assert url.endswith('/interactions')
    body = kwargs.get('json') or json.loads(kwargs.get('data') or '{}')
    assert body['type'] == InteractionType.component.value
    assert body['application_id'] == '777' and body['message_id'] == '900'
    assert body['channel_id'] == '501' and body['guild_id'] == '1' and body['nonce'] == '123'
    assert body['data']['custom_id'] == 'go'


@pytest.mark.asyncio
async def test_retried_5xx_on_a_click_is_logged_and_recorded(monkeypatch, caplog):
    """A slow click with no 429 behind it used to be undiagnosable: 5xx and
    connection-error retries slept silently. They are now logged and counted."""
    monkeypatch.setattr(http_module, '_retry_delay', lambda tries: 0.0)
    http = make_http([(FakeResponse(502, b'bad gateway'), 0.0), (FakeResponse(204, b''), 0.0)])
    client = Client()
    client.http = http
    with caplog.at_level(logging.WARNING, logger='discord.http'):
        await http.request(Route('POST', '/interactions'), json={'x': 1})
    assert len(http._HTTPClient__session.requests) == 2, 'retried once'
    assert any('HTTP 502' in r.getMessage() and 'retrying' in r.getMessage() for r in caplog.records)
    events = client.http_retry_events()
    ((route, summary),) = events.items()
    assert 'interactions' in route
    assert summary['count'] == 1 and summary['last_reason'] == 'HTTP 502'
    assert client.http_throttle_events() == {}, 'a 5xx is not a 429 throttle'


@pytest.mark.asyncio
async def test_429_on_a_click_is_recorded_as_a_throttle(monkeypatch):
    body = b'{"retry_after": 0.01, "global": false}'
    http = make_http([(FakeResponse(429, body), 0.0), (FakeResponse(204, b''), 0.0)])
    client = Client()
    client.http = http
    await http.request(Route('POST', '/interactions'), json={'x': 1})
    ((route, summary),) = client.http_throttle_events().items()
    assert 'interactions' in route and summary['count'] == 1
    assert client.http_retry_events() == {}
