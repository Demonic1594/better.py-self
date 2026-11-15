# Client options

Every keyword argument `discord.Client(...)` accepts: its default, what it
changes, how it interacts with the others, and the quirks. Ready-made
configurations for common setups are at the [end](#ready-made-configurations).
The source is the `Client` docstring and `Client.__init__` in
[`discord/client.py`](../discord/client.py), and where `ConnectionState` and
`HTTPClient` read each option.

`commands.Bot` takes all of these, plus the [command options](#extcommandsbot-only).

## How options are checked

- **Keyword only.** `Client(max_messages=500)`, never positional.
- **Unknown names are logged, not ignored silently.** A typo logs
  `Ignoring unknown Client option(s): max_member_cahce. Check them for typos.`
  at WARNING and the client starts anyway. The known names are
  `_KNOWN_CLIENT_OPTIONS`, and `tests/test_audit_round6.py` keeps it in sync
  with the typed option dictionaries.
- **Wrong types raise `TypeError`** for `member_cache_flags`,
  `allowed_mentions`, `activity`/`activities` and `idle_since`.
- **Contradictory combinations raise `ClientException`** at construction
  (the chunking ones [below](#gateway-and-startup)).
- **Cache caps: `0` or a negative number silently means the default.** Pass
  `None` to remove a cap. `max_messages=None` is the exception: it turns the
  message cache **off**.

```python
client = discord.Client(max_member_cache=0)      # same as the default, 10,000 (no warning)
client = discord.Client(max_member_cache=None)   # no per-guild cap: every member is kept
client = discord.Client(max_messages=None)       # no message cache at all
```

## Caches and memory

| Option | Default | Effect |
|---|---|---|
| `max_messages` | `1000` | Size of the message cache, across all channels. Messages are kept in arrival order and the oldest is dropped first (reading a message doesn't refresh it). `None` disables the cache: `on_message_edit`, `on_message_delete` and `on_reaction_add` then only fire as their raw versions. |
| `max_member_cache` | `10000` | Members kept **per guild**. Least recently *updated* members are evicted first (an update, a presence change, sending a message or being mentioned refreshes a member; `get_member` doesn't). Your own member and members in voice are never evicted. Eviction trims the guild to about 95% of the cap at once. `None` = no cap. |
| `max_total_member_cache` | `50000` | Members kept **across all guilds**. Checked every 1,024 additions; when over, the fullest guilds are trimmed toward the average share, and smaller guilds are untouched. `None` = no cap. |
| `max_presence_cache` | `20000` | Presences outside guilds: friends, other relationships and DM recipients. |
| `max_guild_presence_cache` | `5000` | Presences **per guild**. Least recently *read* are evicted first (`member.status` refreshes it). An evicted presence returns with the member's next presence update. |
| `max_thread_cache` | `5000` | Threads per guild. Threads you're a member of are never evicted; activity refreshes a thread. |
| `max_total_thread_cache` | `25000` | Threads across all guilds, rebalanced like members. |
| `member_cache_flags` | `MemberCacheFlags.all()` | Which members to cache at all; see [below](#member_cache_flags). A non-`MemberCacheFlags` value raises `TypeError`. |
| `gc_freeze` | `True` | About 2 seconds after READY, runs a full garbage collection and then `gc.freeze()`, which moves everything alive into a permanent generation the collector no longer scans. This removes most GC pauses on big accounts. It is undone on reconnect and close. Objects deleted while frozen (guilds you left, say) are only reclaimed after the next reconnect. The collection itself blocks the loop briefly (its duration is logged at DEBUG). |

The first time a cache reaches its cap, the client logs one INFO line
(`The member cache reached its cap and now evicts the least recently used entries...`)
so you know eviction has started. [Caching and memory](caching-and-memory.md)
explains how to size the caches and read `cache_stats()`.

### `member_cache_flags`

| Flag | Default | Caches |
|---|---|---|
| `joined` (alias `other`) | on | Members who joined while you were online, were chunked or scraped, or appeared in an event. |
| `voice` | on | Members in a voice channel. |

Your own member is always cached.

```python
# Only members in voice (a voice-focused client); they are dropped when they leave voice
flags = discord.MemberCacheFlags.none()
flags.voice = True
client = discord.Client(member_cache_flags=flags)

# No members at all: users are created on the fly and never cached
client = discord.Client(member_cache_flags=discord.MemberCacheFlags.none())
```

> **Gotcha: the constructor starts with everything on.**
> `MemberCacheFlags(voice=True)` keeps `joined` on as well. For voice only,
> start from `MemberCacheFlags.none()` (as above) or pass `joined=False`.

With `joined` off, the default for `chunk_guilds_at_startup` becomes `False`;
passing `chunk_guilds_at_startup=True` raises
`ClientException: Cannot chunk guilds at startup without a member cache`.

## Gateway and startup

| Option | Default | Effect |
|---|---|---|
| `guild_subscriptions` | `True` | Subscribes to every guild after READY (typing, activities, threads and member updates). Without it, Discord sends far less: no member events, few presences, and in guilds of 75,000 members or more no message, reaction or typing events. The member and thread caches stay mostly empty. See [Member lists](member-lists.md). |
| `request_guilds` | — | **Deprecated** alias of `guild_subscriptions`. Emits a `DeprecationWarning` (hidden by Python's default filters unless you run with `-W default`). Only read when `guild_subscriptions` isn't given. |
| `chunk_guilds_at_startup` | `guild_subscriptions` and `member_cache_flags.joined` | Before `on_ready`, loads the member lists of guilds that aren't fully loaded: with the gateway member request where you have Kick, Ban or Manage Roles, and by scraping the member sidebar elsewhere. `on_ready` waits for all of them; each guild has a timeout of `max(5, member_count / 10000)` seconds, and they run in parallel. Set it to `False` on accounts with many or large guilds. Passing `True` together with `guild_subscriptions=False` raises `ClientException: Cannot chunk guilds at startup without subscribing to them`. |
| `heartbeat_timeout` | `60.0` | Seconds without **any** message from the gateway before the socket is closed (code 4000) and the session resumed. Raise it if processing the first payloads on a huge account takes longer than that. |

## Events

| Option | Default | Effect |
|---|---|---|
| `enable_raw_presences` | `False` | Dispatches `on_raw_presence_update`, which also fires for members not in the cache. Off by default because presence updates are the most frequent event, and even checking for a listener cost about 13% of their parse time. *New in 2.3.2.* |
| `enable_raw_typing` | `False` | Dispatches `on_raw_typing`, which fires for every typing event, even when `on_typing` can't resolve the channel or user. *New in 2.3.3.* |
| `enable_debug_events` | `False` | Dispatches `on_socket_raw_receive(msg)` and `on_socket_raw_send(payload)`. They get the raw frames **without redaction**, tokens included. |
| `eager_dispatch` | `False` | Python 3.12+: starts each event handler eagerly, so it runs synchronously inside the dispatch up to its first `await`. This lowers per-event overhead, but changes ordering: a handler that doesn't await runs before the parser continues. Below 3.12 it is turned off with the warning `eager_dispatch requires Python 3.12 or newer; event handlers will be scheduled normally.` |
| `dispatch_backlog_warning` | `1000` | When this many handler tasks are pending, logs `Event handler backlog is N (oldest Xs old).` and dispatches `on_dispatch_backlog(pending, oldest_age)`, at most once every 30 seconds. `0` disables it. |

```python
class Monitor(discord.Client):
    async def on_dispatch_backlog(self, pending: int, oldest_age: float):
        # Handlers are piling up: something is slow (a blocking call, a slow API)
        print(f'{pending} handlers pending, oldest started {oldest_age:.1f}s ago')


client = Monitor(dispatch_backlog_warning=500)
```

## Presence on login

| Option | Default | Effect |
|---|---|---|
| `status` | your saved status | The `Status` to connect with. `Status.offline` is sent as `invisible` (offline is not a status you can set). |
| `activity` / `activities` | — | An activity, or a list of them, to connect with. If both are given, `activity` is **ignored without a warning**; pass one. A value that isn't an activity raises `TypeError: activity parameter must derive from BaseActivity`. |
| `afk` | `False` | Connect as AFK, which tells Discord to send push notifications to your phone instead. |
| `idle_since` | `None` | A `datetime` to report as idle since. Anything else raises `TypeError`. |
| `sync_presence` | `True` | Follow status and custom-status changes made on your other clients (the app, the browser), as the official client does, and save the initial presence to your settings. |

The initial presence is sent when the gateway connects, and only if one of
these options is set. To change it later, use `change_presence` (from
`on_ready` on, not in `setup_hook`):

```python
client = discord.Client(status=discord.Status.dnd, activity=discord.Game('chess'))

@client.event
async def on_ready():
    # edit_settings=False: change this session's presence without saving it
    # to your account settings (so your other clients don't follow)
    await client.change_presence(status=discord.Status.idle, edit_settings=False)
```

See [Presence and activities](presence-and-activities.md).

## HTTP and rate limits

| Option | Default | Effect |
|---|---|---|
| `max_ratelimit_timeout` | `None` (wait as long as Discord says) | The longest rate-limit wait, in seconds, the client accepts. A longer one raises `RateLimited` instead of sleeping, both for per-route and global limits, and even before the request is sent when the bucket's reset is already known. **Values below 30 are raised to 30 silently**, and `0` means no limit. |
| `default_ratelimit_limit` | `1` | Requests per bucket assumed until Discord reports a limit. User accounts usually get no rate-limit headers, so with `1` each route runs one request at a time. |
| `throttle_window` | `300.0` | How long, in seconds, a 429 stays in `client.http_throttle_events()`. `0` keeps none; a negative value raises `ValueError`. |
| `assume_unsync_clock` | `True` | Use Discord's `X-Ratelimit-Reset-After` (a duration) instead of comparing its reset timestamp with your system clock. Leave it on unless your clock is NTP-synced and you want slightly tighter timing. |
| `allowed_mentions` | everything allowed | Default `AllowedMentions` for every message you send; a per-message value is merged over it. Wrong type raises `TypeError`. |
| `captcha_handler` | `None` | `async def handler(exc: CaptchaRequired, client) -> str` returning a solved captcha token. Without it, `CaptchaRequired` is raised to your code. See [Tokens and login](tokens-and-login.md#captchas). |

```python
# Fail fast instead of sleeping through long rate limits
client = discord.Client(max_ratelimit_timeout=60)

@client.event
async def on_message(message):
    try:
        await message.add_reaction('\N{EYES}')
    except discord.RateLimited as exc:
        print(f'would have waited {exc.retry_after:.0f}s, skipping')
```

[Rate limits and pacing](rate-limits-and-pacing.md) explains the retry loop,
`http_throttle_events()`, and how to pace a user account.

## Proxies

| Option | Default | Effect |
|---|---|---|
| `proxy` | `None` | Proxy URL for REST requests, CDN downloads and (unless `proxy_gateway=False`) the gateway. HTTP, HTTPS and SOCKS URLs are passed to curl (`socks5h://...` resolves DNS through the proxy). |
| `proxy_auth` | `None` | `aiohttp.BasicAuth(login, password)` for the proxy. It is converted to curl's `(login, password)`. Credentials in the URL (`http://user:pass@host:port`) work too. |
| `proxy_gateway` | `True` | Also route the gateway websocket through `proxy`. `False` keeps only the websocket direct; REST requests still use the proxy. |
| `rpc_proxy` | `None` | Value for the `X-RPC-Proxy` header sent with every request. Despite the name, nothing is routed through it. |

```python
client = discord.Client(proxy='socks5h://127.0.0.1:1080')
client = discord.Client(proxy='http://proxy.example:3128', proxy_auth=aiohttp.BasicAuth('user', 'pass'))
```

> **Gotcha:** an account's IP address changing often, or many accounts behind
> one IP, is a common reason for verification prompts and flags. Keep one
> stable proxy per account. See [Tokens and login](tokens-and-login.md).

Voice audio is sent over UDP and doesn't go through the proxy.

## Client identity

These shape what Discord sees. Matching the official client matters; see
[Discord user API notes](discord-user-api-notes.md#super-properties).

| Option | Default | Effect |
|---|---|---|
| `timezone` | your system time zone | The `Region/City` name sent in `X-Discord-Timezone`. By default it comes from `tzlocal`; if that fails once, the header is left out for the rest of the session. |
| `installation_id` | none | Sent as `X-Installation-ID` and in IDENTIFY. Pass the same value across sessions to look like one installation. |
| `preferred_rtc_regions` | from READY | Voice regions to prefer, in order. |
| `debug_options` | `['bugReporterEnabled']` | The list sent in `X-Debug-Options`, as the official client sends it. |
| `canary` | `False` | Adds `canary` to `X-Debug-Options`, which asks Discord to serve requests from its canary API. The host and the release channel don't change. |
| `apm_tracing` (alias `trace`) | `False` | Adds `trace` to `X-Debug-Options` and sends an `X-Client-Trace-ID`, which turns on Discord's APM tracing; a trace URL is logged for each request at DEBUG. |

The super properties (user agent, browser version, build number) aren't
options: they are fetched once before the first request so they match the
current web client. To control them yourself, override the
`Client.headers_context()` method; see [Tokens and login](tokens-and-login.md#how-the-client-identifies-itself).

## Persistence

| Option | Default | Effect |
|---|---|---|
| `persistence_path` | `None` (off) | File to write a snapshot of the READY data to, and to replay on the next start so caches are warm before the gateway connects. There's no default location. See [Persistence](persistence.md). |
| `persistence_max_age` | `1209600` (14 days) | Snapshots older than this are ignored (cold start). |

## `ext.commands.Bot` only

| Option | Default | Effect |
|---|---|---|
| `command_prefix` | required | A string, a list of strings, or a callable `(bot, message) -> str or list`. With a list, the first one in list order that matches wins, so put longer prefixes first (`['!!', '!']`). `commands.when_mentioned_or('!')` also works. |
| `self_bot` | `False` | Process commands **only** from your own account. |
| `user_bot` | `False` | Process commands from **everyone, including** your own account. You can't set both; with neither (the default), commands from everyone **except** your own account are processed. |
| `owner_id` / `owner_ids` | — | Who passes `is_owner()` checks. Not fetched automatically for user accounts. |
| `case_insensitive` | `False` | Case-insensitive command names. |
| `strip_after_prefix` | `False` | Allow whitespace after the prefix (`! ping`). |
| `help_command` | the default help | `None` removes it. |
| `description` | `''` | Shown in the default help. |

See [ext.commands and ext.tasks](commands-and-tasks.md).

## Ready-made configurations

These start from the defaults, which suit an ordinary account well. Measure
with `await client.cache_stats()` before and after.

**A personal account (few guilds, a few thousand members):** the defaults.

```python
client = discord.Client()
```

**A large account that mostly reads messages** (hundreds of guilds, big
servers): don't wait for member lists at startup, keep fewer members and
presences, keep more messages.

```python
client = discord.Client(
    chunk_guilds_at_startup=False,
    max_member_cache=2_000,
    max_total_member_cache=50_000,
    max_guild_presence_cache=1_000,
    max_messages=5_000,
)
```

**Many accounts in one process on a small server:** tight caps, warm restarts.

```python
def make_client(name: str) -> discord.Client:
    return discord.Client(
        chunk_guilds_at_startup=False,
        max_member_cache=1_000,
        max_total_member_cache=10_000,
        max_total_thread_cache=5_000,
        max_guild_presence_cache=500,
        max_presence_cache=2_000,
        max_messages=200,
        persistence_path=f'cache/{name}.bin',
    )
```

**Everything, unbounded (upstream behaviour):** only for small accounts.

```python
client = discord.Client(
    max_member_cache=None,
    max_total_member_cache=None,
    max_presence_cache=None,
    max_guild_presence_cache=None,
    max_thread_cache=None,
    max_total_thread_cache=None,
    gc_freeze=False,
)
```

**A voice client** that only needs members in voice:

```python
flags = discord.MemberCacheFlags.none()
flags.voice = True
client = discord.Client(member_cache_flags=flags)   # chunk_guilds_at_startup defaults to False here
```
