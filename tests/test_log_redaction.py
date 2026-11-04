"""Debug logging must not store credentials.

Raw gateway events (READY carries analytics/auth tokens, email and phone), HTTP
request and response bodies (passwords on profile edits, tokens on /users/@me),
webhook and interaction URLs (the token is in the path) and voice payloads
(the stream encryption key) are logged at DEBUG; a DEBUG file sink must not end
up holding any of them.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import discord.gateway as gateway
from discord import utils
from discord.http import Route

from test_gateway_regimes import frame, make_ws
from test_rate_limit_behavior import FakeResponse, make_http

SECRETS = ('ANALYTICS-SECRET', 'AUTH-SECRET', 'me@example.com', '+15550100', 'hunter2', 'NEWPASS', 'HOOKTOKEN')


def _logged(caplog) -> str:
    return '\n'.join(r.getMessage() for r in caplog.records)


def test_redact_helper_covers_dicts_json_text_and_urls():
    payload = {
        'd': {
            'analytics_token': 'ANALYTICS-SECRET',
            'auth_token': 'AUTH-SECRET',
            'user': {'id': '1', 'email': 'me@example.com', 'phone': '+15550100'},
            'secret_key': [1, 2, 3],
            'session_id': 'kept',
        }
    }
    out = utils._redact(payload)
    text = str(out)
    assert not any(secret in text for secret in SECRETS)
    assert "'session_id': 'kept'" in text, 'non-sensitive fields are kept'
    assert out['d']['secret_key'] == '[redacted]'
    assert payload['d']['auth_token'] == 'AUTH-SECRET', 'the original is not modified'
    assert utils._redact('{"password":"hunter2","new_password":"NEWPASS","name":"ok"}') == (
        '{"password":"[redacted]","new_password":"[redacted]","name":"ok"}'
    )
    assert 'HOOKTOKEN' not in utils._redact('https://discord.com/api/v9/webhooks/1/HOOKTOKEN?wait=true')
    assert utils._redact('{"token":null}') == '{"token":null}'


@pytest.mark.asyncio
async def test_gateway_event_debug_log_is_redacted(caplog):
    ws = make_ws(asyncio.get_running_loop())
    data = {'analytics_token': 'ANALYTICS-SECRET', 'user': {'email': 'me@example.com', 'phone': '+15550100'}}
    with caplog.at_level(logging.DEBUG, logger='discord.gateway'):
        await ws.received_message(frame(op=0, t='SESSIONS_REPLACE', s=1, d=data))
    text = _logged(caplog)
    assert 'Gateway event' in text and '[redacted]' in text
    assert not any(secret in text for secret in SECRETS)


@pytest.mark.asyncio
async def test_http_debug_logs_redact_bodies_and_url_tokens(caplog):
    body = b'{"id": "1", "analytics_token": "ANALYTICS-SECRET", "email": "me@example.com"}'
    http = make_http(
        [
            (FakeResponse(200, body), 0.0),
            (FakeResponse(429, b'{"retry_after": 0.01, "global": false}'), 0.0),
            (FakeResponse(200, b'{}'), 0.0),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger='discord.http'):
        await http.request(Route('PATCH', '/users/@me'), json={'password': 'hunter2', 'new_password': 'NEWPASS'})
        await http.request(
            Route('POST', '/webhooks/{webhook_id}/{webhook_token}', webhook_id=1, webhook_token='HOOKTOKEN'), json={}
        )
    text = _logged(caplog)
    assert 'has received' in text and 'rate limited' in text, 'the log lines themselves still appear'
    assert not any(secret in text for secret in SECRETS), text


@pytest.mark.asyncio
async def test_voice_payload_debug_logs_are_redacted(caplog):
    sent = []
    vws = gateway.DiscordVoiceWebSocket.__new__(gateway.DiscordVoiceWebSocket)

    async def _sendstr(data):
        sent.append(data)

    vws._sendstr = _sendstr  # type: ignore[attr-defined]
    with caplog.at_level(logging.DEBUG, logger='discord.gateway'):
        await gateway.DiscordVoiceWebSocket.send_as_json(vws, {'op': 0, 'd': {'token': 'AUTH-SECRET', 'server_id': '1'}})
    assert 'AUTH-SECRET' in sent[0], 'the real payload is still sent unchanged'
    text = _logged(caplog)
    assert 'Voice socket sending' in text and 'AUTH-SECRET' not in text


RAW_NAMES = {'url', 'data', 'msg'}
LOGGING_MODULES = ('discord/http.py', 'discord/gateway.py', 'discord/webhook/async_.py', 'discord/webhook/sync.py')


def test_no_log_call_interpolates_a_raw_url_or_payload():
    """Structural guard: a new or multi-line `_log` call that passes a raw URL or
    payload would leak tokens again; every such argument must be redacted.

    Limits: it checks positional arguments that are bare names (`url`, `data`,
    `msg`) of `_log` calls in these modules only, not keyword arguments,
    attribute access such as `response.url`, or other modules."""
    import ast
    import pathlib

    offenders = []
    for path in LOGGING_MODULES:
        tree = ast.parse((pathlib.Path(__file__).resolve().parents[1] / path).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = node.func.value
            if not (isinstance(target, ast.Name) and target.id == '_log'):
                continue
            for arg in node.args[1:]:
                if isinstance(arg, ast.Name) and arg.id in RAW_NAMES:
                    offenders.append(f'{path}:{node.lineno} passes raw `{arg.id}`')
    assert not offenders, '\n'.join(offenders)


def test_redact_regex_stays_linear_on_adversarial_input():
    import time

    hostile = '"token":[' * 100_000  # unterminated arrays: quadratic without the bound
    start = time.perf_counter()
    utils._redact(hostile)
    assert time.perf_counter() - start < 5, 'redaction must not backtrack quadratically'


def test_presigned_upload_url_signature_is_masked():
    url = (
        'https://discord-attachments-uploads-prd.storage.googleapis.com/a/b/c.png'
        '?upload_id=1&X-Goog-Algorithm=GOOG4&X-Goog-Credential=CRED&X-Goog-Signature=SIGSECRET&X-Goog-Expires=3600'
    )
    text = str(utils._RedactedLog(url))
    assert 'SIGSECRET' not in text and 'CRED' not in text
    assert 'X-Goog-Expires=3600' in text, 'harmless parameters stay readable'


def test_billing_address_token_is_redacted():
    """create_payment_source sends billing_address_token, a reusable billing
    credential; it was missing from _SENSITIVE_KEYS and reached DEBUG logs."""
    out = utils._redact({'billing_address_token': 'BILLTOKEN', 'payment_gateway': 1})
    assert out['billing_address_token'] == '[redacted]' and out['payment_gateway'] == 1
    assert 'BILLTOKEN' not in utils._redact('{"billing_address_token":"BILLTOKEN"}')
