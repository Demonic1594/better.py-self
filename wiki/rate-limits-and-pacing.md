# Rate limits and pacing

## How user accounts differ from bots

Bots get `X-RateLimit-*` headers on every response and can pace from them.
User accounts **usually don't**. According to Discord's user-API docs,
user-account responses usually carry only `Retry-After`, `X-RateLimit-Global`
and `X-RateLimit-Scope`, and only on a 429. Discord also often **queues** a
user's requests server-side instead of rejecting them, so heavy use shows up as
rising latency before any 429 appears.

So the library can't pace from headers, and you shouldn't treat "no 429s" as
"safe". The parts below handle what can be handled, and make the rest visible.

## What the HTTP client does

The code is `HTTPClient.request` in [`discord/http.py`](../discord/http.py).

- **Per-route buckets.** Each route keeps a bucket. Until Discord reports a
  limit, the bucket assumes `default_ratelimit_limit` (1), so requests on the
  same route are serialized.
- **429 handling.** The client sleeps for `retry_after` from the body, or the
  `Retry-After` header, then retries.
  - A 429 with no usable value waits **1 second** instead of retrying straight away.
  - A global 429 takes the global lock, which pauses every route.
- **Shared scope.** A 429 with `X-RateLimit-Scope: shared` also takes the global
  lock. Discord's user-API docs describe `shared` as a per-resource limit. This
  library treats it as account-wide because the reaction bucket is shared across
  all channels, so one cooldown has to gate every route.
- **Sub-limits.** A 429 that arrives while the bucket still shows requests
  remaining is logged as a sub-ratelimit, and `retry_after` from the body is used.
- **Cloudflare.** A 429 without a `Via` header is Discord's Cloudflare layer, not
  the API. Discord's user-API docs say that **10,000 invalid requests (401, 403
  or 429) in 10 minutes** trigger a **24-hour IP ban**. Shared-scope 429s don't
  count toward it.
- **Retries.** 5xx responses and connection resets are retried, for at most 5
  attempts per request, and each retry is logged.
- **A cap on waits.** `max_ratelimit_timeout=N` raises `RateLimited` instead of
  waiting longer than `N` seconds for a non-global limit. The minimum is 30.

## Seeing what happened

| API | What it tells you |
|---|---|
| `on_http_throttle(route, retry_after, scope)` | Fires for every 429 the library absorbed. |
| `client.http_throttle_events(window=None)` | Per-route throttle counts and the latest `retry_after`, kept for `throttle_window` seconds (300 by default). |
| `client.http_retry_events(window=None)` | Requests retried after a 5xx or a connection error. |
| `client.http_route_latencies()` | A per-route latency moving average. For user accounts this is the real congestion signal, because queuing pushes latency up before any 429. |

## Pacing that held up live

[`examples/lane_governor.py`](../examples/lane_governor.py) implements a pacing
recipe that held up in live rate studies:

- **One serialized lane per action type per account.** Discord's buckets are
  account-wide, so pacing per channel isn't slow enough.
  - send: **2.6 s**
  - react: **3.0 s**
  - click (interactions): **3.0 s**
- **Jitter.** Add human-like jitter to every interval and round-robin across
  destinations. A channel's slowmode applies on top of its lane.
- **After an absorbed 429:** honour `retry_after` plus some padding, and slow that
  lane down **2×**. A shared-scope 429 pauses **every** lane.
- **Speeding back up.** Lower the interval by at most 10% at a time, only after 2
  minutes without throttles, and never below the base intervals above.
- **Watch the send-route latency.** When the send ceiling is reached, it shows as
  queuing (latency going up), not as 429s.

Live measurements:

- **v2.3.2 test accounts:** sends at 2.6 s, reactions at 3.0 s and short bursts
  at 1.2 s caused **no throttles**. A 13-step message/edit/react/delete sequence
  on 2.6 s pacing raised no rate-limit warnings.
- **October 2026 live validation:**
  - a DM send took 0.41–0.44 s
  - adding and removing a reaction took 0.32–0.37 s
  - there were no 429s at that volume

Each account has its own buckets, so run one governor per client.

## Gateway limits

The gateway has limits of its own (from Discord's user-API docs):

- sending too fast closes the connection with code **4008**
- presence updates are limited to **5 per 20 seconds**
- a payload over **15 KiB** closes the connection with **4002**

The library throttles its own gateway sends with `GatewayRatelimiter`: 110
commands per 60 s, leaving room for heartbeats, which bypass it. When it's
limited, it logs `Gateway is ratelimited, waiting N seconds.`
