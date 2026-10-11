"""
Shared harness for the traffic-regime tests (not collected by pytest):
a stateful mock Discord server, a wire-level session recorder, a scripted
replayer, and request helpers driving the real HTTPClient.

The mock implements the server semantics measured live (findings.md +
TIMING.md); the recorder captures any session (mock or a real one wrapped
by hosts running their own live traffic) into a JSONL fixture; the
replayer turns a fixture back into a deterministic script so recorded
regimes become regression tests.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

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


SEND_ROUTE = Route('POST', '/channels/{channel_id}/messages', channel_id=0)
REACTION_ROUTE = Route(
    'PUT', '/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me', channel_id=0, message_id=0, emoji='x'
)
CLICK_ROUTE = Route('POST', '/interactions')


class MockDiscord:
    """Stateful fake implementing the live-observed server semantics."""

    def __init__(
        self,
        *,
        send_drip: float = 0.06,
        base_latency: float = 0.005,
        reaction_refill: float = 0.05,
        send_lane: float | None = None,
        require_interaction_keys: bool = True,
        click_concurrency: int | None = None,
        bucket_hash_flood: bool = False,
    ):
        self.send_drip = send_drip
        self.base_latency = base_latency
        self.reaction_refill = reaction_refill
        # TIMING.md: sends are also an account-global shared bucket. Sustained
        # intervals >= send_lane are clean; below ~2/3 of it, throttles begin.
        self.send_lane = send_lane
        # findings.md addendum 3: interactions without session_id and
        # message_flags are rejected as malformed (400 code 50035).
        self.require_interaction_keys = require_interaction_keys
        self.click_concurrency = click_concurrency
        # Exploit probe: serve a unique bucket hash on every response.
        self.bucket_hash_flood = bucket_hash_flood
        self._next_send_release: dict[str, float] = {}
        self._send_completions: list[float] = []
        self._next_reaction_release: float = 0.0
        self._reaction_active: int = 0
        self._click_active: int = 0
        self._flood_seq = 0
        self.last_click_payload: dict[str, Any] | None = None
        self.stats: Counter[str] = Counter()
        self.send_holds: list[float] = []
        self.challenge_next: bool = False
        self.challenge_count: int = 0
        self._t0 = time.monotonic()

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def _shared_429(self, retry_after: float) -> FakeResponse:
        return FakeResponse(
            429,
            body=json.dumps({'retry_after': retry_after, 'global': False}).encode(),
            headers={'content-type': 'application/json', 'X-RateLimit-Scope': 'shared'},
        )

    def _ok(self, body: dict | None = None) -> FakeResponse:
        self.stats['ok_no_ratelimit_headers'] += 1
        headers = {'content-type': 'application/json'}
        if self.bucket_hash_flood:
            # Unique hash with an already-expired window: the client must
            # bound its bucket tables despite endless "new" buckets.
            self._flood_seq += 1
            headers.update(
                {
                    'X-RateLimit-Bucket': f'flood{self._flood_seq}',
                    'X-RateLimit-Limit': '5',
                    'X-RateLimit-Remaining': '4',
                    'X-RateLimit-Reset-After': '0',
                    'X-RateLimit-Reset': str(time.time() - 10),
                }
            )
        return FakeResponse(200, body=json.dumps(body or {}).encode(), headers=headers)

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        if self.challenge_next:
            self.challenge_next = False
            self.challenge_count += 1
            self.stats['edge_403'] += 1
            return FakeResponse(
                403, body=b'<html><title>Just a moment...</title></html>', headers={'content-type': 'text/html'}
            )

        now = self._now()

        if method == 'POST' and url.endswith('/interactions'):
            payload = kwargs.get('json')
            if payload is None:
                raw = kwargs.get('data')
                if isinstance(raw, (str, bytes)):
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError):
                        payload = {}
            payload = payload or {}
            if self.require_interaction_keys and ('session_id' not in payload or 'message_flags' not in payload):
                self.stats['click_400_50035'] += 1
                return FakeResponse(
                    400,
                    body=b'{"code": 50035, "message": "Invalid Form Body"}',
                    headers={'content-type': 'application/json'},
                )
            if self.click_concurrency is not None and self._click_active >= self.click_concurrency:
                self.stats['click_429_shared'] += 1
                return self._shared_429(0.06)
            self._click_active += 1
            try:
                await asyncio.sleep(self.base_latency)
                self.stats['click_204'] += 1
                self.last_click_payload = payload
                return FakeResponse(204)
            finally:
                self._click_active -= 1

        if method == 'POST' and url.endswith('/messages'):
            # Sustained account-global lane: more than 2 completions inside
            # a 2x-lane window means the sender is outrunning the refill.
            if self.send_lane is not None:
                window = self._now() - 2 * self.send_lane
                recent = sum(1 for t in self._send_completions if t >= window)
                if recent >= 3:
                    self.stats['send_429_shared'] += 1
                    return self._shared_429(0.06)

            # Server-side queuing: hold until the per-channel refill slot.
            channel = url.split('/channels/')[1].split('/')[0]
            release = max(self._next_send_release.get(channel, 0.0), now + self.base_latency)
            self._next_send_release[channel] = release + self.send_drip
            await asyncio.sleep(release - now)
            if self.send_lane is not None:
                self._send_completions.append(self._now())
            self.send_holds.append(release - now)
            self.stats[f'send_200:{channel}'] += 1
            return self._ok({'id': '1'})

        if method == 'PUT' and '/reactions/' in url:
            # Shared account bucket: at most one in-flight per refill slot;
            # anything concurrent trips the shared-scope throttle.
            if self._reaction_active >= 1:
                self.stats['reaction_429_shared'] += 1
                return self._shared_429(0.08)
            self._reaction_active += 1
            try:
                await asyncio.sleep(self.base_latency)
                self.stats['reaction_204'] += 1
                return FakeResponse(204)
            finally:
                self._reaction_active -= 1

        # Reads and everything else: plain fast success, no headers.
        await asyncio.sleep(self.base_latency)
        self.stats['read_200'] += 1
        return self._ok()


class SessionRecorder:
    """Transparent wire recorder. Wrap any session-like object (the mock in
    tests, or a real session during a host-run live capture) and every
    request/response pair is logged as a JSONL event, throttles included:

        http._HTTPClient__session = SessionRecorder(real_session, path)

    The fixture is the bridge between authentic traffic and the offline
    suite: ScriptedDiscord replays the recorded outcome schedule.
    """

    def __init__(self, inner: Any, path: Optional[Path] = None):
        self.inner = inner
        self.path = path
        self.events: list[dict[str, Any]] = []
        self._t0 = time.monotonic()

    async def request(self, method: str, url: str, **kwargs) -> Any:
        t0 = time.monotonic()
        response = await self.inner.request(method, url, **kwargs)
        elapsed = time.monotonic() - t0
        body = b''
        raw = getattr(response, '_body', None)
        if raw is None:
            read = getattr(response, 'acontent', None) or getattr(response, 'read', None)
            if read is not None:
                try:
                    body = await read()
                except Exception:
                    body = b''
        else:
            body = raw
        event: dict[str, Any] = {
            't': round(t0 - self._t0, 6),
            'method': method,
            'url': url,
            'status': getattr(response, 'status_code', 0),
            'latency': round(elapsed, 6),
        }
        if event['status'] == 429:
            retry_after = None
            try:
                retry_after = json.loads(body).get('retry_after')
            except Exception:
                pass
            event['retry_after'] = retry_after
            event['scope'] = response.headers.get('X-RateLimit-Scope')
        self.events.append(event)
        return response

    def save(self, path: Path | None = None) -> Path:
        path = Path(path or self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as fp:
            for event in self.events:
                fp.write(json.dumps(event) + '\n')
        return path

    @staticmethod
    def load(path: Path) -> list[dict[str, Any]]:
        with open(path) as fp:
            return [json.loads(line) for line in fp if line.strip()]


class ScriptedDiscord:
    """Deterministic replayer: consumes a script of (delay, response-spec)
    pairs. Build one from a recorded fixture to replay the exact outcome
    schedule (statuses, throttles, retry-afters) with optional time scaling.
    """

    def __init__(self, script: list[tuple[float, FakeResponse]], scale: float = 1.0):
        self.script = list(script)
        self.scale = scale
        self.consumed = 0
        self.served: list[int] = []

    @classmethod
    def from_events(cls, events: list[dict[str, Any]], scale: float = 1.0) -> ScriptedDiscord:
        script: list[tuple[float, FakeResponse]] = []
        prev_t = 0.0
        for event in events:
            delay = max(0.0, event['t'] - prev_t)
            prev_t = event['t']
            status = event['status']
            if status == 429:
                retry_after = event.get('retry_after') or 0.05
                headers = {'content-type': 'application/json'}
                if event.get('scope'):
                    headers['X-RateLimit-Scope'] = event['scope']
                body = json.dumps({'retry_after': retry_after, 'global': False}).encode()
                script.append((delay, FakeResponse(status, body=body, headers=headers)))
            else:
                script.append((delay, FakeResponse(status)))
        return cls(script, scale)

    async def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        if not self.script:
            return FakeResponse(204)
        delay, response = self.script.pop(0)
        self.consumed += 1
        self.served.append(response.status_code)
        await asyncio.sleep(delay * self.scale)
        return response


def make_http(session: Any, **kwargs) -> HTTPClient:
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
    http._HTTPClient__session = session
    return http


def send_ewma(http: HTTPClient) -> float:
    return http._route_latencies[SEND_ROUTE.key]


async def send(http: HTTPClient, channel: int) -> None:
    from discord.http import MultipartParameters

    params = MultipartParameters({'content': 'x'}, None, None)
    await http.send_message(channel, params=params)


async def react(http: HTTPClient, channel: int) -> None:
    await http.add_reaction(channel, 1, '\N{THUMBS UP SIGN}')


async def click(http: HTTPClient) -> None:
    await http.request(CLICK_ROUTE, json={'session_id': 'x' * 32, 'message_flags': 0})


async def read(http: HTTPClient, user_id: int = 2) -> None:
    await http.get_user(user_id)


async def button_click(http: HTTPClient) -> None:
    """Drives the library's real interaction path (HTTPClient.interact):
    the payload it builds must carry the keys findings.md addendum 3 says
    real clients need (session_id + message_flags), or Discord 400s."""
    channel = SimpleNamespace(id=900, _state=SimpleNamespace(session_id=None), guild=None)
    message = SimpleNamespace(
        id=901,
        application_id=910,
        author=SimpleNamespace(id=910),
        flags=SimpleNamespace(value=0),
        guild=None,
    )
    data = {'component_type': 2, 'custom_id': 'button-1'}
    interaction_type = SimpleNamespace(value=2)
    await http.interact(interaction_type, data, channel, message)
