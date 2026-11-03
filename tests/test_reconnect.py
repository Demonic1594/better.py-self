"""
Reconnect behaviour of Client.connect (gateway) and the voice handshake retry.

The gateway is replaced by scripted fakes and time is driven by a fake clock,
so these tests check the policy (when to sleep, when to resume at once, when
to start a fresh backoff) without any network access.
"""

from __future__ import annotations

import asyncio
import errno as errno_module
import time as real_time
from typing import Any, List, Optional

import pytest

import discord.client as client_module
import discord.voice_state as voice_module
from discord.errors import ConnectionClosed
from discord.gateway import DiscordWebSocket


class FakeClock:
    """Stands in for the time module inside client.py: monotonic() is scripted."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)


class CountingBackoff(client_module.ExponentialBackoff):  # type: ignore[misc]
    instances: List[CountingBackoff] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        CountingBackoff.instances.append(self)


def _reset_error() -> OSError:
    # ECONNRESET is 104 on Linux; the errno set must recognise it
    return OSError(errno_module.ECONNRESET, 'Connection reset by peer')


async def _run_script(monkeypatch, script):
    """Drives Client.connect() through `script`.

    Each entry is ``(seconds_alive, exception)``: the fake websocket lives for
    that many (fake) seconds, then raises the exception. The final entry is
    ``(seconds, None)`` meaning "stop": the client is marked closed and the
    loop exits on the raised error.
    """
    client = client_module.Client()
    await client._async_setup_hook()

    clock = FakeClock()
    monkeypatch.setattr(client_module, 'time', clock)
    monkeypatch.setattr(client_module, 'ExponentialBackoff', CountingBackoff)
    CountingBackoff.instances = []

    sleeps: List[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(client_module.asyncio, 'sleep', fake_sleep)

    steps = list(script)
    from_client_calls: List[int] = []
    from_client_params: List[dict] = []

    class FakeWS:
        sequence = None
        gateway = 'wss://gateway.example'
        session_id = 'session'

        async def poll_event(self) -> None:
            seconds, exc, *rest = steps.pop(0)
            if rest and rest[0]:
                self.session_id = None
            clock.now += seconds
            if exc is None:
                client._closing_task = asyncio.get_running_loop().create_future()
                raise ConnectionClosed(4000)  # reconnectable; is_closed() ends the loop
            raise exc

    async def fake_from_client(_client, **params):
        from_client_calls.append(1)
        from_client_params.append(params)
        return FakeWS()

    monkeypatch.setattr(DiscordWebSocket, 'from_client', staticmethod(fake_from_client))

    await asyncio.wait_for(client.connect(reconnect=True), timeout=5)
    return sleeps, len(from_client_calls), len(CountingBackoff.instances), from_client_params


@pytest.mark.asyncio
async def test_connection_reset_resumes_immediately_once_then_backs_off(monkeypatch):
    script = [
        (1, _reset_error()),  # blip: resume at once
        (1, _reset_error()),  # repeats: must back off, not hammer the gateway
        (1, _reset_error()),  # still repeating: back off again
        (0, None),  # stop
    ]
    sleeps, connects, _, _ = await _run_script(monkeypatch, script)

    assert connects == 4
    assert len(sleeps) == 2, f'expected a backoff after the first immediate resume, got {sleeps}'


@pytest.mark.asyncio
async def test_short_failing_sessions_keep_growing_backoff(monkeypatch):
    script = [(5, asyncio.TimeoutError()), (5, asyncio.TimeoutError()), (5, asyncio.TimeoutError()), (0, None)]
    sleeps, connects, backoffs, _ = await _run_script(monkeypatch, script)

    assert connects == 4
    assert len(sleeps) == 3
    assert backoffs == 1, 'short sessions must continue the same backoff, not restart it'


@pytest.mark.asyncio
async def test_healthy_session_starts_a_fresh_backoff(monkeypatch):
    script = [
        (0, asyncio.TimeoutError()),
        (0, asyncio.TimeoutError()),
        (120, asyncio.TimeoutError()),  # stayed up for two minutes before failing
        (0, None),
    ]
    _, connects, backoffs, _ = await _run_script(monkeypatch, script)

    assert connects == 4
    assert backoffs == 2, 'a healthy session must reset the backoff for its successor'


@pytest.mark.asyncio
async def test_healthy_session_re_enables_the_immediate_resume(monkeypatch):
    script = [
        (1, _reset_error()),  # uses the immediate resume
        (120, _reset_error()),  # healthy session that then resets: immediate resume again
        (1, _reset_error()),  # repeats right after: now it must back off
        (0, None),
    ]
    sleeps, connects, _, _ = await _run_script(monkeypatch, script)

    assert connects == 4
    assert len(sleeps) == 1, f'exactly one backoff expected, got {sleeps}'


class FixedBackoff:
    instances = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        FixedBackoff.instances += 1

    def delay(self) -> float:
        return 0.25


@pytest.mark.asyncio
async def test_voice_handshake_retry_uses_the_jittered_backoff(monkeypatch):
    client = client_module.Client()
    await client._async_setup_hook()

    sleeps: List[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def closed_handshake(self, resume: bool):
        raise ConnectionClosed(4006)

    async def soft_disconnect(self, *args: Any, **kwargs: Any) -> None:
        return None

    conn = voice_module.VoiceConnectionState.__new__(voice_module.VoiceConnectionState)
    conn._state = voice_module.ConnectionFlowState.got_both_voice_updates
    conn.endpoint = 'voice.example'

    FixedBackoff.instances = 0
    monkeypatch.setattr(voice_module, 'ExponentialBackoff', FixedBackoff)
    monkeypatch.setattr(voice_module.asyncio, 'sleep', fake_sleep)
    monkeypatch.setattr(voice_module.VoiceConnectionState, '_voice_connect', noop)
    monkeypatch.setattr(voice_module.VoiceConnectionState, '_wait_for_state', noop)
    monkeypatch.setattr(voice_module.VoiceConnectionState, '_connect_websocket', closed_handshake)
    monkeypatch.setattr(voice_module.VoiceConnectionState, 'disconnect', soft_disconnect)

    with pytest.raises(ConnectionClosed):
        await conn._inner_connect(reconnect=True, self_deaf=False, self_mute=False, self_video=False, resume=False)

    assert sleeps == [0.25] * 4, 'retries must take their delay from the jittered backoff'
    assert FixedBackoff.instances == 1


@pytest.mark.asyncio
async def test_invalid_session_waits_then_identifies_at_the_default_gateway(monkeypatch):
    from discord.gateway import ReconnectWebSocket

    script = [
        (1, _reset_error()),  # a resumable connection (sets a resume gateway)
        (1, ReconnectWebSocket(resume=False, delay=2.5)),  # INVALID_SESSION d=false
        (0, None),
    ]
    sleeps, connects, _, params = await _run_script(monkeypatch, script)

    assert connects == 3
    assert 2.5 in sleeps, 'Discord asks for a wait before re-identifying'
    assert params[1].get('resume') is True and params[1].get('gateway') is not None
    assert params[2]['resume'] is False
    assert 'gateway' not in params[2], 'a dead session must not reuse its resume gateway'


@pytest.mark.asyncio
async def test_invalid_session_resumable_waits_then_resumes(monkeypatch):
    from discord.gateway import ReconnectWebSocket

    script = [
        (1, ReconnectWebSocket(delay=1.5)),  # INVALID_SESSION d=true
        (0, None),
    ]
    sleeps, connects, _, params = await _run_script(monkeypatch, script)

    assert connects == 2 and 1.5 in sleeps
    assert params[1]['resume'] is True and params[1].get('gateway') is not None


@pytest.mark.asyncio
async def test_resume_without_a_session_identifies_instead(monkeypatch):
    script = [
        (1, asyncio.TimeoutError(), True),  # the session id was lost along the way
        (0, None),
    ]
    _, connects, _, params = await _run_script(monkeypatch, script)

    assert connects == 2
    assert params[1]['resume'] is False, 'nothing to RESUME without a session id'
    assert params[1].get('gateway') is None, 'identify uses the default gateway'


# --- gateway level: close codes and INVALID_SESSION through DiscordWebSocket ----------


class _CloseFrameSocket:
    def __init__(self, close_code: Optional[int] = None) -> None:
        self.closed = False
        self.close_code = close_code
        self.close_reason = ''

    async def recv(self):
        from curl_cffi.const import CurlWsFlag

        return None, CurlWsFlag.CLOSE

    async def close(self, code=None, reason=b''):
        self.closed = True


class _MessageSocket(_CloseFrameSocket):
    async def send(self, data):
        return None


def _gateway_ws(socket, *, sequence=42, session_id='sess'):
    from discord.gateway import DiscordWebSocket

    ws = DiscordWebSocket(socket, loop=asyncio.get_running_loop())
    ws._max_heartbeat_timeout = 1.0
    ws._dispatch = lambda *a: None
    ws.log_receive = lambda *a: None
    ws.sequence = sequence
    ws.session_id = session_id
    ws.gateway = DiscordWebSocket.DEFAULT_GATEWAY.with_query(v=10)
    return ws


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [4007, 4009])
async def test_non_resumable_close_codes_reidentify_without_resuming(code):
    from discord.gateway import DiscordWebSocket, ReconnectWebSocket

    ws = _gateway_ws(_CloseFrameSocket(close_code=code))
    with pytest.raises(ReconnectWebSocket) as caught:
        await ws.poll_event()

    assert caught.value.resume is False, f'close {code} cannot be resumed'
    assert ws.sequence is None and ws.session_id is None
    assert ws.gateway == DiscordWebSocket.DEFAULT_GATEWAY


@pytest.mark.asyncio
async def test_resumable_close_code_still_resumes():
    from discord.gateway import ReconnectWebSocket

    ws = _gateway_ws(_CloseFrameSocket(close_code=4000))
    with pytest.raises(ReconnectWebSocket) as caught:
        await ws.poll_event()
    assert caught.value.resume is True and ws.session_id == 'sess'


@pytest.mark.asyncio
async def test_invalid_session_false_clears_state_and_delays_identify():
    import json

    from discord.gateway import ReconnectWebSocket

    ws = _gateway_ws(_MessageSocket())
    with pytest.raises(ReconnectWebSocket) as caught:
        await ws.received_message(json.dumps({'op': 9, 'd': False}))

    assert caught.value.resume is False
    assert 1.0 <= caught.value.delay <= 5.0, 'Discord asks for a random 1-5 second wait'
    assert ws.sequence is None and ws.session_id is None


@pytest.mark.asyncio
async def test_invalid_session_true_keeps_the_session_and_delays_resume():
    import json

    from discord.gateway import ReconnectWebSocket

    ws = _gateway_ws(_MessageSocket())
    with pytest.raises(ReconnectWebSocket) as caught:
        await ws.received_message(json.dumps({'op': 9, 'd': True}))

    assert caught.value.resume is True
    assert 1.0 <= caught.value.delay <= 5.0
    assert ws.session_id == 'sess' and ws.sequence == 42


@pytest.mark.asyncio
async def test_repeated_reconnect_requests_back_off(monkeypatch):
    from discord.gateway import ReconnectWebSocket

    # A gateway (or proxy) that answers every connection with RECONNECT
    script = [(1, ReconnectWebSocket()) for _ in range(6)] + [(0, None)]
    sleeps, connects, _, _ = await _run_script(monkeypatch, script)

    assert connects == 7
    # The first few retries are immediate; after that each one waits
    assert len(sleeps) == 3, f'expected backoff from the 4th consecutive request on, got {sleeps}'


@pytest.mark.asyncio
async def test_a_healthy_session_resets_the_reconnect_streak(monkeypatch):
    from discord.gateway import ReconnectWebSocket

    script = [
        (1, ReconnectWebSocket()),
        (1, ReconnectWebSocket()),
        (1, ReconnectWebSocket()),
        (120, ReconnectWebSocket()),  # a normal long session ends in a RECONNECT
        (1, ReconnectWebSocket()),
        (1, ReconnectWebSocket()),
        (1, ReconnectWebSocket()),
        (0, None),
    ]
    sleeps, _, _, _ = await _run_script(monkeypatch, script)

    assert sleeps == [], f'no backoff expected when no streak builds up, got {sleeps}'
