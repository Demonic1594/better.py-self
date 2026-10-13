"""
Gateway regime replay: the session-lifecycle behaviors validated live
(RESUME retains the session id, invalid-session with d=false forces a
fresh IDENTIFY, invalid-session with d=true and op 7 RECONNECT resume
instead) plus keepalive hygiene across repeated HELLOs.
"""

from __future__ import annotations

import asyncio
import datetime
import json

import pytest

import discord.gateway as gateway
from discord.gateway import ReconnectWebSocket


class RecordingSocket:
    closed = False
    close_code = None
    close_reason = ''

    def __init__(self):
        self.sent: list[str] = []

    async def recv(self):
        await asyncio.sleep(30)

    async def send(self, d):
        self.sent.append(d)

    async def close(self, code=None, reason=b''):
        self.closed = True
        self.close_code = code


def make_ws(loop):
    ws = gateway.DiscordWebSocket(RecordingSocket(), loop=loop)
    ws._max_heartbeat_timeout = 60
    ws._dispatch = lambda *a: None
    ws.log_receive = lambda *a: None
    ws._discord_parsers = {}
    ws._headers = type(
        'H',
        (),
        {
            'initialization_timestamp': datetime.datetime.now(),
            'gateway_properties': {},
        },
    )()
    ws._connection = type('C', (), {'_has_voice_client': lambda s: False})()
    ws.token = 'tok'
    return ws


def frame(op=None, t=None, s=None, d=None):
    return json.dumps({'op': op, 't': t, 's': s, 'd': d})


HELLO = frame(op=10, d={'heartbeat_interval': 60000, '_trace': []})


@pytest.mark.asyncio
async def test_ready_then_resume_uses_same_session():
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)

    await ws.received_message(
        frame(
            op=0,
            t='READY',
            s=5,
            d={'session_id': '11cdf886deadbeef', 'resume_gateway_url': 'wss://resume.example', '_trace': []},
        )
    )
    assert ws.session_id == '11cdf886deadbeef'
    assert str(ws.gateway) == 'wss://resume.example'

    await ws.resume()
    resume = json.loads(ws.socket.sent[-1])
    assert resume['op'] == 6
    assert resume['d']['session_id'] == '11cdf886deadbeef'
    assert resume['d']['seq'] == ws.sequence
    print('gateway: READY captured session; RESUME carried the same session id + seq')


@pytest.mark.asyncio
async def test_invalid_session_false_forces_fresh_identify():
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)
    ws.session_id = 'about-to-die'
    ws.sequence = 42

    with pytest.raises(ReconnectWebSocket) as exc_info:
        await ws.received_message(frame(op=9, d=False))
    assert exc_info.value.resume is False
    assert ws.session_id is None
    assert ws.sequence is None
    assert ws.socket.closed
    print('gateway: INVALID_SESSION d=false -> clear session, reconnect without resume')


@pytest.mark.asyncio
async def test_invalid_session_true_keeps_session_for_resume():
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)
    ws.session_id = 'keep-me'

    with pytest.raises(ReconnectWebSocket) as exc_info:
        await ws.received_message(frame(op=9, d=True))
    assert exc_info.value.resume is True
    assert ws.session_id == 'keep-me', 'resumable invalidation must retain the session id'
    print('gateway: INVALID_SESSION d=true -> reconnect WITH resume, session retained')


@pytest.mark.asyncio
async def test_reconnect_op_requests_resume():
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)
    ws.session_id = 'keep-me-too'

    with pytest.raises(ReconnectWebSocket) as exc_info:
        await ws.received_message(frame(op=7, d=None))
    assert exc_info.value.resume is True
    assert ws.session_id == 'keep-me-too'
    assert ws.socket.closed
    print('gateway: op 7 RECONNECT -> closed socket, resume requested')


@pytest.mark.asyncio
async def test_resumed_event_is_benign():
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)
    ws.session_id = 'resumed-session'
    await ws.received_message(frame(op=0, t='RESUMED', s=99, d={'_trace': []}))
    assert ws.sequence == 99
    print('gateway: RESUMED event parsed cleanly, sequence tracked')


@pytest.mark.asyncio
async def test_repeated_hello_swaps_keepalive_without_leak():
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)

    for interval in (60000, 45000, 30000):
        await ws.received_message(frame(op=10, d={'heartbeat_interval': interval, '_trace': []}))
        await asyncio.sleep(0)

    assert ws._keep_alive is not None
    assert ws._keep_alive.interval == 30.0  # the LAST hello won
    # The live keepalive holds one task (its run loop) on 3.12+, plus the
    # wait_for-wrapped Event.wait helper task on <=3.11 (wait_for gained a
    # direct-await implementation in 3.12). The replaced keepalives must be
    # gone entirely — a leak would show up as several extra run tasks.
    import sys

    expected = 1 if sys.version_info >= (3, 12) else 2
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.0
    while True:
        background = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if len(background) <= expected or loop.time() > deadline:
            break
        await asyncio.sleep(0.005)
    assert len(background) <= expected, background
    await ws._keep_alive._join()
    await asyncio.sleep(0.01)
    print(f'gateway: 3x HELLO -> single live keepalive ({expected} task(s) on this Python), no leak')
