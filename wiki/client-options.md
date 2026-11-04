# Client options

Every keyword `discord.Client(...)` accepts, taken from the `Client` docstring
and `Client.__init__` in [`discord/client.py`](../discord/client.py). An option
the client doesn't know is **not silently ignored**: it logs
`Ignoring unknown Client option(s): ... Check them for typos.` The set of known
names is `_KNOWN_CLIENT_OPTIONS`, and a test keeps it in sync with the typed option
dictionaries.

`commands.Bot` takes all of these, plus the [command options](#extcommandsbot-only) at the end.

## Caches and memory

| Option | Default | Effect |
|---|---|---|
| `max_messages` | `1000` | Size of the message cache. `None` disables it. |
| `max_member_cache` | `10000` | Members cached **per guild**. `None` means no cap. The least recently used members are evicted first. You and members in voice are never evicted; members who send messages or are mentioned are re-cached and protected. |
| `max_total_member_cache` | `50000` | Members cached **across all guilds**. Evicts from the fullest guilds first, with the same protections. |
| `max_presence_cache` | `20000` | Presences outside guilds (for example relationships). |
| `max_guild_presence_cache` | `5000` | Presences per guild. An evicted presence comes back with that member's next presence update. |
| `max_thread_cache` | `5000` | Threads per guild. Threads you joined are never evicted, and recently active ones are kept warm. |
| `max_total_thread_cache` | `25000` | Threads across all guilds. |
| `member_cache_flags` | cache as much as possible | A `MemberCacheFlags` for finer control over which members are cached. |
| `gc_freeze` | `True` | Freezes the startup object graph for the garbage collector shortly after READY, which cuts GC pauses on big accounts. The graph is unfrozen on reconnect. Guild data deleted while frozen is only reclaimed at the next reconnect. |

See [Caching and memory](caching-and-memory.md) for how to tune these.

## Gateway and startup

| Option | Default | Effect |
|---|---|---|
| `guild_subscriptions` | `True` | Subscribes to every guild at startup. Without it, large guilds (over 75,000 members) may send no message, reaction or typing events, member events stop, and the member and thread caches stay mostly empty. See [Member lists](member-lists.md). |
| `request_guilds` | — | **Deprecated** alias of `guild_subscriptions`. |
| `chunk_guilds_at_startup` | `True` | Delays `on_ready` until the guilds that can be chunked have been chunked. This is slow on accounts with many guilds. |
| `heartbeat_timeout` | `60.0` | Seconds without a `HEARTBEAT_ACK` before the WebSocket is restarted. |
| `enable_debug_events` | `False` | Dispatches `on_socket_raw_receive` and `on_socket_raw_send`. These handlers get the raw, unredacted data. |
| `enable_raw_presences` | `False` | Dispatches `on_raw_presence_update`, which also fires for members not in the cache. Off by default because presence updates are the most frequent event. *New in 2.3.2.* |
| `eager_dispatch` | `False` | Starts event handlers eagerly (Python 3.12+), so a handler runs inside the dispatch up to its first `await`. This lowers per-event overhead, but changes the order in which handlers and the dispatching code run. It is ignored, with a warning, before 3.12. |
| `dispatch_backlog_warning` | `1000` | Number of pending handler tasks at which `on_dispatch_backlog` fires, at most once every 30 s. `0` disables it. |

## Presence on login

| Option | Default | Effect |
|---|---|---|
| `status` | — | `Status` to log in with. |
| `activity` / `activities` | — | Activity, or list of activities, to log in with. Don't pass both. |
| `afk` | `False` | Start the session as AFK. |
| `idle_since` | `None` | A `datetime` to report as idle since. |
| `sync_presence` | `True` | Follow the status and custom-status changes made on your other clients, as the official client does, and save the initial presence to your settings. It doesn't affect `change_presence`, which updates your settings unless you pass `edit_settings=False`. |

## HTTP, rate limits and proxies

| Option | Default | Effect |
|---|---|---|
| `max_ratelimit_timeout` | `None` (no limit) | Longest wait, in seconds, for a non-global rate limit before raising `RateLimited`. Values below **30.0** are raised to 30. |
| `throttle_window` | `300.0` | Seconds a throttle event stays visible in `http_throttle_events()`. `0` keeps only current ones; a negative value raises `ValueError`. |
| `assume_unsync_clock` | `True` | Use Discord's reset time for rate-limit buckets instead of your system clock. |
| `default_ratelimit_limit` | `1` | Bucket limit assumed until Discord reports one. User accounts usually get no rate-limit headers at all. |
| `proxy` | `None` | Proxy URL for HTTP requests. |
| `proxy_auth` | `None` | `aiohttp.BasicAuth` for the proxy. |
| `proxy_gateway` | `True` | Also route the gateway WebSocket through `proxy`. |
| `rpc_proxy` | `None` | Value for the `X-RPC-Proxy` header sent with every request. Despite the name, nothing is routed through it. |
| `allowed_mentions` | — | Default `AllowedMentions` for every message you send. |
| `captcha_handler` | `None` | `async def handler(exc: CaptchaRequired, client) -> str` returning a solved captcha token. See [Troubleshooting](troubleshooting.md#captcha-required). |

## Client identity

These shape what Discord sees, and matching the official client matters (see
[Discord user API notes](discord-user-api-notes.md#super-properties)).

| Option | Default | Effect |
|---|---|---|
| `timezone` | system timezone | `Region/City` timezone announced to Discord. |
| `installation_id` | per session | Installation ID used for experiment tracking. Passing one keeps it across sessions. |
| `preferred_rtc_regions` | Discord's list | Voice regions to prefer. |
| `canary` | `False` | Adds `canary` to the `X-Debug-Options` header, which asks Discord to serve requests from its canary API. The host stays the same, and the release channel doesn't change. |
| `apm_tracing` (alias `trace`) | `False` | Adds `trace` to `X-Debug-Options`, which turns on Discord's APM tracing, and logs a trace URL for each request at DEBUG. |
| `debug_options` | `['bugReporterEnabled']` | The list sent in the `X-Debug-Options` header, the way the official client sends it. `canary` and `apm_tracing` prepend their own entries. |

## Persistence

| Option | Default | Effect |
|---|---|---|
| `persistence_path` | `None` | File to snapshot the caches to after READY and on a clean `close()`, then replay on the next start. See [Persistence](persistence.md). |
| `persistence_max_age` | `1209600` (14 days) | Snapshots older than this cause a cold start instead of a replay. |

## `ext.commands.Bot` only

| Option | Default | Effect |
|---|---|---|
| `command_prefix` | required | A prefix string, a list of them, or a callable returning one. |
| `self_bot` | `False` | Respond **only** to messages sent by your own account. |
| `user_bot` | `False` | Respond to **everyone, including** your own account. You can't set both, and with neither set (the default) the bot responds to everyone **except** your own account. |
| `owner_id` / `owner_ids` | — | The owners for `is_owner()` checks. |
| `case_insensitive` | `False` | Case-insensitive command names. |
| `strip_after_prefix` | `False` | Strip whitespace after the prefix. |
