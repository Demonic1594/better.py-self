# Architecture

How the library is put together at runtime: the main objects and what each
owns, the path a gateway event takes from the socket to your handler, how the
cache and the models relate, and the concepts (snowflakes, partial objects,
`get_` versus `fetch_`) that explain most of the API's shape. Read this when
the behaviour of something surprises you.

## The big picture

```text
                     your code
        handlers (on_*)    │   API calls (send, fetch_*, edit...)
              ▲            │
              │            ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ Client (client.py)                                           │
 │   dispatch() ──► one asyncio task per handler                │
 │   ├── ConnectionState (state.py)                             │
 │   │     parse_* methods ── caches (guilds, members, messages, │
 │   │                         users, presences, threads, ...)  │
 │   ├── DiscordWebSocket (gateway.py) ◄── gateway (wss://)      │
 │   │     decompress ─► JSON ─► ConnectionState.parse_<event>  │
 │   └── HTTPClient (http.py) ──► REST API (https://)            │
 │         rate limits, retries, captchas, headers              │
 └──────────────────────────────────────────────────────────────┘
```

| Object | Module | Owns |
|---|---|---|
| `Client` | `client.py` | The public entry point. Options, lifecycle (`login`, `connect`, `close`), event dispatch, `wait_for`, and the top-level `get_*` / `fetch_*` methods. One per account. |
| `ConnectionState` | `state.py` | Every cache, and one `parse_<event>` method per gateway event that turns a payload into models and dispatches events. Models keep a reference to it (`_state`) to make API calls. |
| `DiscordWebSocket` | `gateway.py` | The gateway connection: IDENTIFY/RESUME, heartbeats, decompression, decoding, the send rate limiter, and handing each event to the state. Replaced on every reconnect. |
| `HTTPClient` | `http.py` | Every REST call, built on curl_cffi. Builds headers, applies rate limits, retries, captchas, and maps errors to exceptions. |
| Models | `guild.py`, `member.py`, `message.py`, `channel.py`, ... | `Guild`, `Member`, `Message`, `TextChannel` and so on. Built from payloads by the state; methods on them call `_state.http`. |
| `discord.types` | `types/` | `TypedDict`s describing payloads, for type checking only. |

The library is single-threaded and runs entirely on one asyncio event loop.

## The life of a gateway event

Take a message being sent in a channel you can see:

1. **Receive.** The websocket gets a frame. With `zstd-stream` compression
   each frame is one compressed message; with `zlib-stream`, frames are
   buffered until a complete message arrives.
2. **Decode.** The bytes are decompressed and parsed as JSON (orjson if
   installed). A frame that can't be decoded is logged and the session is
   resumed on a new connection; nothing reaches your code.
3. **Route.** For `op 0` (an event), the sequence number is recorded (for
   RESUME) and `ConnectionState.parse_message_create(data)` is called.
4. **Parse.** The parser builds a `Message`, resolving the channel, the author
   (as a `Member` in guilds when possible) and mentions from the caches, adds it
   to the message cache, updates the channel's `last_message_id`, and refreshes
   the author in the member cache.
5. **Dispatch.** `client.dispatch('message', message)`:
   - completes any `wait_for('message', ...)` whose check passes;
   - if you defined `on_message`, schedules it as a **new asyncio task**
     (named `discord.py: on_message`). With `eager_dispatch` on Python 3.12+,
     the task starts running immediately, up to its first `await`.
6. **Run.** Your handler runs concurrently with everything else. An
   exception in it goes to `on_error`; it never reaches the gateway loop.

Consequences:

- **Handlers don't block the gateway,** but they also don't run in order:
  two quick events can have their handlers interleave at every `await`. If
  order matters, serialise with an `asyncio.Lock` or a queue.
- **A blocking call stalls everything,** because the parser, the heartbeat
  and every handler share one thread. `time.sleep`, synchronous HTTP
  libraries and heavy CPU work belong in `asyncio.to_thread`.
- **Parsing happens before your handler runs,** so by the time `on_message`
  runs, the cache already contains the message.
- **Pending handlers are tracked.** `(await client.cache_stats()).dispatch_backlog` is how
  many are running; `on_dispatch_backlog` warns when it grows (see
  [Events](events.md)).

## The cache and the models

Everything the gateway tells the client about is kept in the
`ConnectionState`'s caches, and the models you get from events and `get_*`
methods are **the cached objects themselves**.

- **Models are updated in place.** When a `GUILD_MEMBER_UPDATE` arrives, the
  cached `Member` is changed. The `before` you get in `on_member_update(before, after)`
  is a shallow copy taken just before the update; `after` is the live cached
  object.
- **Caches are bounded.** Members, presences, threads and messages have caps,
  and the least recently used entries are evicted (see
  [Caching and memory](caching-and-memory.md)). Guilds, channels, roles,
  emojis, relationships and DMs aren't capped.
- **Holding a reference keeps an object alive, not up to date.** If you keep a
  `Member` in your own dict and it is later evicted from the cache, your
  object stays in memory but stops receiving updates. Store IDs, and look the
  object up again when you need it.
- **Users are shared.** `User` objects live in a weak dictionary: the same
  user in many guilds is one `User` object, referenced by each `Member`, and it
  disappears when nothing references it.
- **Models use `__slots__`,** so you can't add your own attributes to them
  (`member.score = 3` raises `AttributeError`). Keep your data in a dict keyed
  by ID.

```python
# Keep your own data keyed by ID, not on the models
scores: dict[int, int] = {}

@client.event
async def on_message(message):
    scores[message.author.id] = scores.get(message.author.id, 0) + 1
```

## `get_*` versus `fetch_*`

| | `get_*` | `fetch_*` |
|---|---|---|
| Source | The cache | A REST request |
| Sync or async | Synchronous | `async` (await it) |
| Cost | A dictionary lookup | A request, counted against rate limits |
| When missing | Returns `None` | Raises `NotFound` (or `Forbidden`) |
| Freshness | As fresh as the last event | Current |
| Caches the result? | — | Usually not (a fetched member isn't added to the member cache, for example) |

Typical pattern:

```python
channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
member = guild.get_member(user_id) or await guild.fetch_member(user_id)
```

Prefer `get_*` in hot paths (event handlers) and fall back to `fetch_*` only
when `None` would be a real problem. Fetching in a loop over many IDs is a
fast way to hit rate limits.

## Partial objects

Many API calls only need an ID. Partial objects let you make them without
fetching or caching anything:

```python
# Send to a channel by ID; nothing is looked up
channel = client.get_partial_messageable(channel_id)
await channel.send('hello')

# Act on a message by ID without fetching it
partial = channel.get_partial_message(message_id)
await partial.add_reaction('\N{THUMBS UP SIGN}')
await partial.delete()

# Anything that takes "an object with an id" (snowflake) accepts discord.Object
await guild.ban(discord.Object(id=user_id))
messages = [m async for m in channel.history(limit=100, before=discord.Object(id=message_id))]
```

If the ID is wrong, the request fails with `NotFound` when it's made, not when
the partial object is created.

## Snowflakes

Every ID is a **snowflake**, a 64-bit integer that encodes its creation time.
The library always uses `int` IDs; Discord sends them as strings and the
library converts them.

```python
created = discord.utils.snowflake_time(message.id)        # an aware datetime in UTC
message.created_at                                          # the same, as a property

# A snowflake for a point in time, to page history from there
since = discord.utils.time_snowflake(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
async for m in channel.history(after=discord.Object(id=since), limit=None):
    ...
```

Models compare equal and hash by ID within their type, so they work as dict
keys and in sets, and a cached object equals a fetched copy of the same thing.
A `Member` and the `User` of the same person are different types; compare
`member.id == user.id`, or use `message.author == client.user` which handles
both.

## Datetimes

All datetimes the library returns are **timezone-aware, in UTC**. Datetimes
you pass in (`before=`, `after=`, `until=`) can be aware (any zone) or naive;
naive ones are treated as **local time**, as in upstream.
`discord.utils.utcnow()` gives the current time aware.

## Threads, the event loop and other frameworks

- **The library isn't thread-safe.** Call it only from the event loop's
  thread. From another thread, hand work to the loop:

  ```python
  future = asyncio.run_coroutine_threadsafe(channel.send('from a thread'), client.loop)
  future.result(timeout=10)
  ```

- **Running alongside a web server** (aiohttp, FastAPI, Quart): run both in the
  same loop. Start the client as a task with `await client.start(token)` inside
  the server's startup, and close it in the shutdown hook.
- **`client.loop`** is the running loop after `login()`; before that it's a
  placeholder that raises if used.

## Where things are configured

| What | Where |
|---|---|
| Cache sizes, presence, proxies, startup behaviour | [Client options](client-options.md) |
| Which events exist and when they fire | [Events](events.md) |
| Connection, reconnects, heartbeats | [The gateway](gateway.md) |
| Rate limits and retries | [Rate limits and pacing](rate-limits-and-pacing.md) |
| What is cached and for how long | [Caching and memory](caching-and-memory.md) |
| Member lists on user accounts | [Member lists](member-lists.md) |
