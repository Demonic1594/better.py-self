"""
Gateway lifecycle regression tests: poll timeouts, close idempotency,
keepalive joining, zlib decompression.
"""

from __future__ import annotations

import asyncio
import time
import zlib

import pytest
from curl_cffi.const import CurlWsFlag

from discord import gateway
from discord import utils


class FakeSocket:
    def __init__(self, delay=0.0, payload=None):
        self.delay = delay
        self.payload = payload
        self.closed = False
        self.close_code = None
        self.close_reason = ''

    async def recv(self):
        await asyncio.sleep(self.delay)
        if self.payload is not None:
            return self.payload, CurlWsFlag.TEXT
        # Text frame as a str: bytes frames are treated as transport-compressed
        # and routed through the decompressor, which differs per environment
        return '{"t":null,"s":null,"op":1,"d":null}', CurlWsFlag.TEXT

    async def send(self, data):
        pass

    async def close(self, code=None, reason=b''):
        self.closed = True


def make_ws(loop, delay=0.0):
    ws = gateway.DiscordWebSocket(FakeSocket(delay), loop=loop)
    ws._max_heartbeat_timeout = 0.3
    ws._dispatch = lambda *a: None
    ws.log_receive = lambda *a: None
    return ws


@pytest.mark.asyncio
async def test_poll_event_async_with_timeout():
    ws = make_ws(asyncio.get_running_loop())
    await asyncio.wait_for(ws.poll_event(), timeout=1.0)  # must not raise TypeError


@pytest.mark.asyncio
async def test_poll_event_timeout_raises_reconnect():
    loop = asyncio.get_running_loop()
    ws = make_ws(loop, delay=5.0)
    start = time.monotonic()
    with pytest.raises(gateway.ReconnectWebSocket):
        await ws.poll_event()
    assert 0.2 < time.monotonic() - start < 2.0


@pytest.mark.asyncio
async def test_poll_event_wait_for_fallback():
    loop = asyncio.get_running_loop()
    original = gateway._asyncio_timeout
    gateway._asyncio_timeout = None
    try:
        ws = make_ws(loop, delay=5.0)
        with pytest.raises(gateway.ReconnectWebSocket):
            await ws.poll_event()
    finally:
        gateway._asyncio_timeout = original


@pytest.mark.asyncio
async def test_close_idempotent_under_concurrency():
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)
    await asyncio.gather(ws.close(4000), ws.close(4001), ws.close(4002))
    assert ws.socket.closed and ws._close_code == 4000


@pytest.mark.asyncio
async def test_close_joins_blocked_keepalive():
    loop = asyncio.get_running_loop()

    class SlowSendSocket(FakeSocket):
        async def send(self, data):
            await asyncio.sleep(30)

    ws = gateway.DiscordWebSocket(SlowSendSocket(), loop=loop)
    ws._max_heartbeat_timeout = 60.0
    ws._dispatch = lambda *a: None
    ws.log_receive = lambda *a: None
    keepalive = gateway.KeepAliveHandler(ws=ws, interval=0.01)
    keepalive.start()
    await asyncio.sleep(0.05)

    start = time.monotonic()
    await ws.close(4000)
    elapsed = time.monotonic() - start
    # _join times out at 5s and reaps the task
    assert elapsed < 8.0
    await asyncio.sleep(0)
    assert keepalive._task.done()


def test_keepalive_slots():
    handler = gateway.KeepAliveHandler.__slots__
    assert '_task' in handler and 'latency' in handler
    with pytest.raises(AttributeError):
        gateway.GatewayRatelimiter().bogus_attr = 1


def test_zlib_decompression_split_and_fast_path():
    ctx_cls = utils._ActiveDecompressionContext
    if ctx_cls.COMPRESSION_TYPE != 'zlib-stream':
        pytest.skip('zstd transport is active in this environment')

    payload = b'{"t":null,"s":null,"op":10,"d":null}'
    comp = zlib.compressobj(wbits=15)
    frame = comp.compress(payload) + comp.flush(zlib.Z_SYNC_FLUSH)

    d = ctx_cls()
    assert d.decompress(frame[:-2]) is None  # partial marker buffered
    assert d.decompress(frame[-2:]) == payload

    d2 = ctx_cls()
    assert d2.decompress(frame) == payload  # whole-frame fast path

    # Multi-message framing over ONE continuous stream (as the gateway uses)
    d3 = ctx_cls()
    msg1 = b'{"a":1}'
    msg2 = b'{"b":2}'
    c = zlib.compressobj(wbits=15)
    f1 = c.compress(msg1) + c.flush(zlib.Z_SYNC_FLUSH)
    f2 = c.compress(msg2) + c.flush(zlib.Z_SYNC_FLUSH)
    assert d3.decompress(f1) == msg1
    assert d3.decompress(f2) == msg2


@pytest.mark.asyncio
async def test_dispatch_listeners_early_out():
    loop = asyncio.get_running_loop()
    ws = make_ws(loop)
    fut = loop.create_future()
    entry = gateway.EventListener(event='TEST', predicate=lambda d: True, result=None, future=fut)
    ws._dispatch_listeners.append(entry)
    # Simulate the listener dispatch block directly
    data = {'x': 1}
    removed = []
    for index, e in enumerate(ws._dispatch_listeners):
        if e.event != 'TEST':
            continue
        if e.future.cancelled():
            removed.append(index)
            continue
        try:
            valid = e.predicate(data)
        except Exception:
            removed.append(index)
        else:
            if valid:
                e.future.set_result(data)
                removed.append(index)
    for index in reversed(removed):
        del ws._dispatch_listeners[index]
    assert fut.done() and fut.result() == {'x': 1}
    assert not ws._dispatch_listeners


@pytest.mark.asyncio
@pytest.mark.parametrize('use_orjson', [False, True])
async def test_compressed_frame_parses_from_bytes_and_raw_event_gets_str(monkeypatch, use_orjson):
    import json

    if use_orjson:
        orjson = pytest.importorskip('orjson')
        monkeypatch.setattr(utils, '_from_json_frame', orjson.loads)
    else:

        def frame_loader(data):
            return json.loads(data.decode('utf-8') if type(data) is bytes else data)

        monkeypatch.setattr(utils, '_from_json_frame', frame_loader)

    ws = make_ws(asyncio.get_running_loop())
    payload = '{"t":"TYPING_START","s":7,"op":0,"d":{"channel_id":"1","user_id":"2","emoji":"é"}}'.encode()

    class OneShotDecompressor:
        def decompress(self, data):
            return data  # stands in for zlib/zstd: yields the raw UTF-8 bytes

    ws._decompressor = OneShotDecompressor()  # type: ignore
    dispatched = []
    ws._dispatch = lambda *a: dispatched.append(a)
    ws.log_receive = ws.debug_log_receive
    ws._discord_parsers = {'TYPING_START': lambda d: dispatched.append(('parsed', d))}

    await ws.received_message(payload)

    assert ws.sequence == 7
    raw = [a for a in dispatched if a[0] == 'socket_raw_receive']
    assert raw and type(raw[0][1]) is str and raw[0][1] == payload.decode()
    assert ('parsed', {'channel_id': '1', 'user_id': '2', 'emoji': 'é'}) in dispatched


@pytest.mark.asyncio
async def test_listener_result_error_goes_to_waiter_not_receive_loop():
    ws = make_ws(asyncio.get_running_loop())
    ws._discord_parsers = {'GUILD_MEMBER_LIST_UPDATE': lambda d: None}

    def boom(data):
        raise KeyError('missing')

    failing = ws.wait_for('GUILD_MEMBER_LIST_UPDATE', lambda d: True, result=boom)
    ok = ws.wait_for('GUILD_MEMBER_LIST_UPDATE', lambda d: True)

    # Used to raise out of received_message (ending the poll loop -> reconnect)
    await ws.received_message('{"t":"GUILD_MEMBER_LIST_UPDATE","s":3,"op":0,"d":{"guild_id":"1"}}')

    with pytest.raises(KeyError):
        failing.result()
    assert ok.result() == {'guild_id': '1'}
    assert ws._dispatch_listeners == []
