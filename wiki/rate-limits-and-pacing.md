# Rate limits and pacing

How the HTTP client handles rate limits, retries and server errors, which
exceptions reach your code and when, how to see what's happening, and how to
pace a user account so it stays out of trouble. The code is
`HTTPClient.request` in [`discord/http.py`](../discord/http.py).

## How user accounts differ from bots

Bots get `X-RateLimit-*` headers on every response and can pace from them.
User accounts **usually don't**. According to Discord's user-API docs,
user-account responses usually carry only `Retry-After`, `X-RateLimit-Global`
and `X-RateLimit-Scope`, and only on a 429. Discord also often **queues** a
user's requests server-side instead of rejecting them, so heavy use shows up
as rising latency before any 429 appears.

So the library can't pace from headers, and "no 429s" doesn't mean "safe".
The parts below handle what can be handled, and make the rest visible.

## What happens to a request

1. **Wait for any global cooldown** (a global or shared-scope 429 earlier).
2. **Take the route's bucket.** Each route (`POST /channels/{id}/messages`
   for one channel ID, say) has a bucket. Until Discord reports a limit, a
   bucket allows `default_ratelimit_limit` (1) request at a time, so requests
   on the same route are serialised. If the bucket is known to reset later than
   `max_ratelimit_timeout`, `RateLimited` is raised before anything is sent.
3. **Send it,** with up to **5 attempts**. Each of these uses one attempt and
   is retried:

   | Response | What the client does |
   |---|---|
   | 429 with a JSON body | Sleeps `retry_after` (body, then `Retry-After` header, then **1 s** if neither is usable), then retries. A global or shared-scope 429 pauses every route during the wait. |
   | 202 with a code in 110000–119999 (Discord deferring the request) | Sleeps the body's `retry_after` (default **5 s**), adds an `attempts` query parameter, retries. |
   | 502, 504, 507, 522, 523, 524 | Sleeps `1 + 2 × attempt + random(0–1)` seconds (about 1, 3, 5, 7, 9 s), retries. |
   | curl errors 23, 28, 35 (write error, timeout, TLS handshake) or a connection reset | Same delay, retried on the first four attempts. |

   Later attempts carry `X-Failed-Requests` with the number of transport
   failures so far, as the official client does.
4. **Raise or return:**

   | Outcome | Exception |
   |---|---|
   | 2xx | Returns the data. |
   | 403 | `Forbidden` |
   | 404 | `NotFound` |
   | 500, 503 and other 5xx not retried above | `DiscordServerError` |
   | A body with `captcha_key` (other statuses) | `CaptchaRequired`, or the captcha handler is called and the request retried (see [Tokens and login](tokens-and-login.md#captchas)) |
   | Other 4xx | `HTTPException` |
   | A wait longer than `max_ratelimit_timeout` | `RateLimited` |
   | 429 with a text body and no `Retry-After` (a Cloudflare ban page) | `HTTPException('Cloudflare ban (code: ...)')` at once, no retry |
   | Still 429, 202 or 5xx after the 5th attempt | `HTTPException` with that `status` (`DiscordServerError` for 5xx) |
   | The client was closed | curl_cffi's `SessionClosed`, not wrapped |

> **Gotcha: a request that stays rate limited raises `HTTPException`, not
> `RateLimited`.** `RateLimited` only means "the wait was longer than your
> `max_ratelimit_timeout`". After five 429s in a row, the request fails with
> `HTTPException` whose `status` is 429. Catch both if you handle rate limits
> yourself.

The fifth attempt raises as soon as its answer arrives: it doesn't sleep
first. (Before v2.3.4 it slept the computed delay, about 9 seconds after a
5xx, before raising, and logged "attempt 6 of 5".) A global 429 on the last
attempt still closes the global gate for its `retry_after`, so the other
requests wait it out. The log says
`... responded with 429 on the last of 5 attempts; giving up.`

### The window's last request returns at once

When a response empties its bucket, that request returns straight away and
the wait for the reset happens in the background; the next request on the
bucket waits for it. Before the November 2026 audit, the request that emptied
the bucket waited out the reset itself.

### Global, shared and sub-limits

- **Global** (`X-RateLimit-Global` or `"global": true`): the global lock pauses
  every route for `retry_after`. Logged as
  `Global rate limit has been hit. Retrying in X.XX seconds.`
- **Shared scope** (`X-RateLimit-Scope: shared`): also takes the global lock.
  Discord's user-API docs describe `shared` as a per-resource limit; this
  library treats it as account-wide because the reaction bucket is shared
  across all channels, so one cooldown has to gate every route.
- **Sub-limits:** a 429 while the bucket still shows requests remaining is
  logged at DEBUG as a sub-ratelimit, and `retry_after` from the body is used.
- **Cloudflare:** a 429 without a `Via` header comes from Discord's Cloudflare
  layer, not the API (`Cloudflare rate limit has been hit. Retrying in ...`).
  Discord's user-API docs say **10,000 invalid requests (401, 403 or 429) in
  10 minutes** trigger a **24-hour IP ban**. Shared-scope 429s don't count
  toward it. Some Cloudflare responses give `Retry-After` in milliseconds; the
  client detects that and converts it.

### `max_ratelimit_timeout`

By default the client waits however long Discord says. With
`max_ratelimit_timeout=N`, any wait longer than `N` seconds raises
`RateLimited` instead: per-route, global and deferred (202) waits alike.
Values below 30 are raised to 30 silently; `0` means no limit.

```python
client = discord.Client(max_ratelimit_timeout=60)

async def react_safely(message: discord.Message, emoji: str) -> bool:
    try:
        await message.add_reaction(emoji)
        return True
    except discord.RateLimited as exc:
        print(f'skipped: would have waited {exc.retry_after:.0f}s (cloudflare={exc.cloudflare})')
    except discord.HTTPException as exc:
        if exc.status == 429:
            print('still rate limited after 5 attempts')
        else:
            raise
    return False
```

`RateLimited` is **not** an `HTTPException` (it can be raised before any
response): it has `retry_after` and `cloudflare`, no `status` or `code`.

## Seeing what happened

| API | What it tells you |
|---|---|
| `on_http_throttle(route, retry_after, scope)` | Fires for every 429 the library absorbed, before it sleeps. |
| `client.http_throttle_events(window=None)` | Per-route throttle counts and the latest `retry_after`, kept for `throttle_window` seconds (300 by default). |
| `client.http_retry_events(window=None)` | Requests retried after a 5xx or a connection error. |
| `client.http_route_latencies()` | A per-route latency moving average. For user accounts this is the real congestion signal: queuing pushes latency up before any 429. |

Logs (logger `discord.http`):

- `We are being rate limited. POST /channels/{channel_id}/messages responded with 429. Retrying in 2.10 seconds.` (WARNING)
- `... Timeout of X was too long, erroring instead.` before a `RateLimited`
- `POST ... failed (HTTP 502); retrying in 3.40 seconds (attempt 2 of 5).`

```python
class Watched(discord.Client):
    async def on_http_throttle(self, route, retry_after, scope):
        print(f'429 on {route}: waiting {retry_after:.1f}s (scope={scope})')

    async def on_ready(self):
        print(self.http_route_latencies())
```

## Pacing a user account

The library waits out the 429s it gets, but avoiding them, and the queuing
before them, is up to you.
[`examples/lane_governor.py`](../examples/lane_governor.py) implements a
recipe that held up in live rate studies:

- **One serialised lane per action type per account.** Discord's buckets are
  account-wide for user accounts, so pacing per channel isn't slow enough.
  - send: **2.6 s**
  - react: **3.0 s**
  - click (interactions): **3.0 s**
- **Jitter.** Add random, human-like jitter to every interval and round-robin
  across destinations. A channel's slowmode applies on top of its lane.
- **After an absorbed 429:** honour `retry_after` plus some padding, and slow
  that lane down **2×**. A shared-scope 429 pauses **every** lane.
- **Speeding back up.** Lower the interval by at most 10% at a time, only after
  2 minutes without throttles, and never below the base intervals above.
- **Watch the send-route latency.** When the send ceiling is reached, it shows
  as queuing (latency going up), not as 429s.

A minimal lane, if you don't need the whole governor:

```python
import asyncio
import random
import time


class Lane:
    """Runs one action at a time, at least `interval` seconds apart, with jitter."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def run(self, coro_fn, *args, **kwargs):
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await coro_fn(*args, **kwargs)
            finally:
                self._next = time.monotonic() + self.interval * random.uniform(1.0, 1.3)


send_lane = Lane(2.6)
react_lane = Lane(3.0)

# usage
await send_lane.run(channel.send, 'hello')
await react_lane.run(message.add_reaction, '\N{THUMBS UP SIGN}')
```

Each account has its own buckets, so run one set of lanes per client.

Live measurements:

- **v2.3.2 test accounts:** sends at 2.6 s, reactions at 3.0 s and short
  bursts at 1.2 s caused **no throttles**. A 13-step
  message/edit/react/delete sequence on 2.6 s pacing raised no rate-limit
  warnings.
- **October 2026 live validation:**
  - a DM send took 0.41 to 0.44 s
  - adding and removing a reaction took 0.32 to 0.37 s
  - there were no 429s at that volume

## Gateway limits

The gateway has limits of its own (from Discord's user-API docs):

- sending too fast closes the connection with code **4008**;
- presence updates are limited to **5 per 20 seconds**;
- a payload over **15 KiB** closes the connection with **4002**.

The library throttles its own gateway sends to 110 per 60 seconds, leaving
room for heartbeats, which bypass the limiter. When it's limited, it logs
`Gateway is ratelimited, waiting N seconds.` Guild subscription batches are
kept under 15 KiB. The library doesn't limit presence updates for you: don't
call `change_presence` in a loop.

## Things that look like rate limits but aren't

| Symptom | Actually |
|---|---|
| `CaptchaRequired` on sends, DMs, friend requests | Anti-spam, not a rate limit. Slow down and see [Tokens and login](tokens-and-login.md#captchas). |
| `HTTPException` 403 with code `40333` | Cloudflare blocked the request (`Cloudflare access denied (code: 1020)`). Usually too many invalid requests from the IP. |
| 403 with code `10008` ("Unknown Message") | Discord's anti-abuse blocked the request, despite the text. |
| `HTTPException` with code `20016` | Slowmode: the channel's per-user rate limit. |
| Codes `340xxx` | Account restrictions. See [Discord error codes](error-codes.md). |
| Sends getting slower and slower, no 429 | Server-side queuing. Slow down. |
