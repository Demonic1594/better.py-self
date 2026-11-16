# Troubleshooting

Problems by symptom, with the cause and the fix. Search this page for the
exact error or log line you see. Installation problems are on
[Installation](installation.md#troubleshooting-installation).

## Contents

- [Login and tokens](#login-and-tokens)
- [Captcha required](#captcha-required)
- [The account was flagged or disabled](#the-account-was-flagged-or-disabled)
- [Connection problems](#connection-problems)
- [Events are missing](#events-are-missing)
- [The member list is empty or incomplete](#the-member-list-is-empty-or-incomplete)
- [`on_ready` takes a long time](#on_ready-takes-a-long-time)
- [Sending and HTTP errors](#sending-and-http-errors)
- [Rate limits](#rate-limits)
- [Voice close codes](#voice-close-codes)
- [Memory and CPU](#memory-and-cpu)
- [asyncio errors](#asyncio-errors)
- [Log lines explained](#log-lines-explained)
- [Warnings added in v2.3.3](#warnings-added-in-v233)
- [Reporting a bug](#reporting-a-bug)

## Login and tokens

- **`LoginFailure: Improper token has been passed`** means the login request
  got a 401: the token is wrong, expired, or was invalidated by a password
  change or a logout. Copy a fresh one (see
  [Tokens and login](tokens-and-login.md#the-token)).
- **`LoginFailure: Improper token has been passed (the token is empty).`** The
  token string is empty, often an unset environment variable
  (`os.environ.get('DISCORD_TOKEN', '')`). Nothing was sent.
- **`TypeError: expected token to be a str`.** You passed bytes or `None`.
- **Gateway close 4004** (`ConnectionClosed` with code 4004) is the gateway's
  version of an invalid token. The client doesn't reconnect after it.
- **A token stops working right after you use it.** Logging out of the
  session you copied it from invalidates it. Copy it from a private window and
  close the window without logging out.
- **`HTTPException` 403, code 40002** ("You need to verify your account"):
  verify the email or phone in the official client.

## Captcha required

```text
discord.errors.CaptchaRequired: 400 Bad Request (error code: -1): Captcha required
```

Discord's anti-spam sometimes requires an hCaptcha before an action. It's
common on opening or sending DMs, joining guilds and friend requests. In the
v2.3.2 live tests, `create_dm` **and** a send in an existing DM channel both
got one. It's a decision Discord makes about the account (its age,
reputation, recent activity) and the action, not a library bug, and retrying
without solving it won't help.

**Handling it.** Pass `captcha_handler`, an async function that receives the
exception and the client and returns a solved token:

```python
async def solve(exc: discord.CaptchaRequired, client: discord.Client) -> str:
    # exc.service is 'hcaptcha', 'recaptcha' or 'recaptcha_enterprise': pick a
    # solver for it. Also exc.sitekey, exc.rqdata, exc.rqtoken, exc.session_id,
    # exc.should_serve_invisible
    return await my_solver(exc)

client = discord.Client(captcha_handler=solve)
```

The library resends the request with `X-Captcha-Key`, plus
`X-Captcha-Session-Id` and `X-Captcha-Rqtoken` when Discord gave them.
[`examples/captcha.py`](../examples/captcha.py) shows the hCaptcha challenge
to you in a PyQt6 webview, so you can solve it by hand. It supports hCaptcha
only. More in [Tokens and login](tokens-and-login.md#captchas).

**Solver services.** Upstream's maintainer says there are no reliable public
solving services left (dolfies/discord.py-self#517). Reports of a solved token
still being rejected are in #890. Showing the challenge to a person, as the
example does, is the dependable option.

**`Forbidden` instead of `CaptchaRequired`.** A captcha that comes with HTTP
403 is raised as `Forbidden`, and the handler isn't called. Check
`exc.json.get('captcha_key')`.

## The account was flagged or disabled

Upstream's maintainer, in dolfies/discord.py-self#930, says Discord's
suspicious-activity detection reacts to:

- **sudden changes** in a token's IP location or client details;
- **one token in two places**, the desktop app and the library at once.

His advice is to use a dedicated token taken from a private browser session.

What this library does to look like the official client:

- **TLS.** Requests go through curl_cffi with a browser TLS fingerprint
  (dolfies/discord.py-self#500 explains why that matters to Cloudflare).
- **Headers.** API requests carry `X-Super-Properties`, with a live build
  number when the startup lookup succeeds (see
  [Tokens and login](tokens-and-login.md#how-the-client-identifies-itself)).
- **Bot-only endpoints.** It doesn't call them: `Member.fetch_voice` uses the
  cache on user accounts.

On your side:

- pace your actions (see [Rate limits and pacing](rate-limits-and-pacing.md));
- don't spam DMs or friend requests;
- keep one stable IP per account;
- watch for warning signs (more captchas, 40002, 340xxx codes) and back off;
- remember that automating a user account is against Discord's ToS whatever
  you do.

**`Could not retrieve client build number. Falling back to hardcoded value...`
(CRITICAL)** means the client couldn't look up the current web client's
build number and is using `9999`, which doesn't look like a real client.
Usually a network or proxy problem at startup; fix it and restart.

## Connection problems

- **`Heartbeat blocked for more than 10 seconds.`** Something blocked the
  event loop: a synchronous call (`time.sleep`, `requests`, heavy CPU work,
  file I/O on a slow disk) in your code. The log includes the stack of what
  was running. Make it async or move it to `asyncio.to_thread`.
- **`Can't keep up, websocket is X.Xs behind.`** The loop is overloaded;
  events are processed late. Same causes, or far too many handlers.
- **`Gateway has stopped responding. Closing and restarting.`** Nothing
  arrived for `heartbeat_timeout` (60 s). The client resumes by itself. If it
  happens right after login on a huge account, raise `heartbeat_timeout`.
- **`The gateway asked to ... N times in a row; waiting X.Xs first.`** Discord
  keeps closing the connection right after it opens. The client backs off.
  Often a network problem, a proxy that drops websockets, or
  `proxy_gateway=True` with a proxy that can't do websockets.
- **`Attempting a reconnect in X.XXs`** with a traceback: a normal reconnect
  after an error; the traceback says why. Rare ones are expected.
- **The client stops with `ConnectionClosed` code 4004, 4010–4014.** Those
  codes are final (invalid token; the others shouldn't happen on a user
  account). See [The gateway](gateway.md#close-codes).
- **`on_ready` fires again after a disconnect.** The session couldn't be
  resumed, so a new one started and the caches were rebuilt. Make `on_ready`
  idempotent.

## Events are missing

- **No `on_message` in a big guild.** With the default
  `guild_subscriptions=True` the library subscribes to every guild. If you
  turned subscriptions off, guilds of 75,000 members or more send no message
  events until you call `guild.subscribe(...)`. See [Member lists](member-lists.md).
- **No `on_member_update`, `on_message_edit`, `on_reaction_add`.** The member
  or message wasn't cached (the caps evict cold entries, and the message
  cache holds 1,000 messages). Use the raw events (`on_raw_*`).
- **No `on_raw_presence_update` or `on_raw_typing`.** They're opt-in:
  `enable_raw_presences=True`, `enable_raw_typing=True`.
- **A typing event was dropped.** That happens when its channel isn't
  cached, or when its user can't be resolved (for example a DM without a
  recipient). Use `on_raw_typing` with `enable_raw_typing=True`; it fires for
  every typing event.
- **Presences of most members are missing.** User accounts can't get every
  member's presence in big guilds; see [Member lists](member-lists.md#presences).
- **A handler never runs.** Check its name (`on_mesage` registers silently),
  that it's `async def`, and that a `commands.Bot` subclass overriding
  `on_message` calls `process_commands`.
- **Handlers seem to lag.** Watch `on_dispatch_backlog` and
  `(await client.cache_stats()).dispatch_backlog`. A slow handler is holding up the queue.

## The member list is empty or incomplete

- **The sidebar returned nothing.** The log shows "Member list scraping ...
  got no member list from Discord (no SYNC for the requested ranges ...)". Pass
  `channels=` that everyone can view.
- **Offline members are missing.** Without Kick Members, Ban Members or Manage
  Roles, `fetch_members` scrapes the sidebar, which in guilds where
  `member_count + hoisted roles + 2 >= 1000` only lists online members.
- **`ClientException: This guild cannot be chunked`.** No permission for the
  gateway request, and the sidebar hides offline members.
- **`get_member` returns `None`.** The member isn't cached (evicted, or never
  seen). Use `guild.fetch_member(id)`, or raise the caps.
- **`len(guild.members)` is smaller than `guild.member_count`.** Expected:
  only cached members are in `members`.

### The log says "Member chunk request ... got no reply"

A new `Guild.chunk()` or `fetch_members()` call found that the running
request had heard nothing back for 60 seconds (more in guilds above 600,000
members), so that request was failed and a new one was sent. The waiters of
the old request get `InvalidData` (a `ClientException`); calling `chunk()`
again is safe. A `chunk()` that waits alone raises the same `InvalidData`
("got no reply for 60 seconds") after that much silence. See
[Member lists](member-lists.md#timeouts-and-lost-requests).

## `on_ready` takes a long time

`chunk_guilds_at_startup` (on by default) waits until member lists are loaded
for the guilds that need it. Each guild waits up to
`max(5, member_count / 10000)` seconds, in parallel. On an account with many
large guilds:

- set `chunk_guilds_at_startup=False` and load members only where you need
  them (`await guild.chunk()`);
- use [persistence](persistence.md) to restart warm;
- check the log for `Timed out waiting for chunks for guild ID ...`.

## Sending and HTTP errors

| Error | Usual cause |
|---|---|
| `TypeError: send() got an unexpected keyword argument 'embed'` | User accounts can't send embeds; use a webhook. See [Messages](messages-and-channels.md#sending). |
| `Forbidden` 50013 "Missing Permissions" | You lack a permission in that channel; check `channel.permissions_for(guild.me)`. |
| `Forbidden` 50007 "Cannot send messages to this user" | Their privacy settings, a block, or no shared guild. |
| `Forbidden` 50001 "Missing Access" | You can't see the channel. |
| `HTTPException` 50035 "Invalid Form Body" | A value Discord rejects: content too long, a bad emoji, an embed over its limits. `exc.text` lists the field. |
| `HTTPException` 20016 | Slowmode. |
| `HTTPException` 40333 / "Cloudflare access denied" | Cloudflare blocked the request; usually too many invalid requests from the IP. |
| `NotFound` 10003 / 10008 / 10013 | Unknown channel / message / user. A 403 with 10008 is anti-abuse, not a missing message. |
| `HTTPException` 20002 "Only bots can use this endpoint" | A bot-only endpoint. Use the user-account alternative (for voice states, `member.voice`). |
| `ValueError: File buffer ... must be seekable and readable` | Pass a seekable file object, or `io.BytesIO(data)`. |
| `FileNotFoundError` when passing bytes to `discord.File` | Bytes are treated as a path (the message says so since v2.3.4); wrap data in `io.BytesIO`. |
| `ModuleNotFoundError: No module named 'requests'` from `SyncWebhook` | `pip install requests`. |
| `curl_cffi ... SessionClosed` | A request after `client.close()`. Stop background tasks before closing. |

[Discord error codes](error-codes.md) has the full list, including the
340001–340018 codes Discord uses when it restricts an account. Read
`exc.status`, `exc.code` and `exc.text` from any `HTTPException`.

## Rate limits

- **`We are being rate limited. ... Retrying in X seconds.`** A 429 the client
  waited out. Occasional ones are fine; frequent ones mean you're too fast.
- **`RateLimited`** isn't an `HTTPException`: it has no `status` or `code`.
  It's raised when you set `max_ratelimit_timeout` and a wait, global or not,
  would be longer than that, and when Discord sends a `retry_after` that isn't
  a finite number. Otherwise the library waits out rate limits for you.
- **`HTTPException` with `status` 429** after a long time: the request was
  still rate limited after 5 attempts.
- **Everything gets slower without any 429.** Discord queues user-account
  requests before rejecting them. Slow down.

See [Rate limits and pacing](rate-limits-and-pacing.md).

## Voice close codes

- **`davey is not installed, voice will NOT be supported.`** or **`PyNaCl is
  not installed`:** install the `[voice]` extra. Joining voice with
  `change_voice_state` still works without it.
- **`discord.opus.OpusNotLoaded`:** libopus wasn't found; install it or call
  `discord.opus.load_opus(...)`. See [Installation](installation.md#the-voice-extra).
- **`ClientException: ffmpeg was not found.`:** install FFmpeg or pass
  `executable=`.
- **4017, "E2EE/DAVE protocol required":** the voice server requires DAVE.
  Install `davey` (dolfies/discord.py-self#917).
- **4006, or random voice disconnects:** these are known upstream issues
  (dolfies/discord.py-self#870, #877, #945). Reconnect, or choose another
  region with `preferred_rtc_regions`.
- **4021:** rate limited on voice connects; the library disconnects. Wait
  before reconnecting.
- **`send_sound` fails:** check that you're **in** the channel, not server
  muted or suppressed, and have Speak and Use Soundboard. Sounds from another
  guild need Nitro.
- **"Already connected to a voice channel" after a failed `connect()`:** fixed
  in v2.3.3; a failed or cancelled connect now cleans up. On older versions,
  call `disconnect(force=True)` on `guild.voice_client` (or the client's
  `voice_clients`) first. It's also raised, correctly, while the library is
  reconnecting by itself.

See [Soundboard and voice](soundboard-and-voice.md).

## Memory and CPU

- **Memory keeps growing.** Check `await client.cache_stats()`: the capped
  caches stay at their caps; uncapped ones (guilds, channels, DMs,
  relationships) only grow with your account. Your own code is the next
  suspect: dicts of `Member` or `Message` objects keep them alive. Store IDs.
- **Memory after leaving many guilds doesn't drop.** With `gc_freeze=True`,
  objects deleted while frozen are reclaimed at the next reconnect.
- **High CPU on a big account.** Usually presences and member updates.
  Install the `speed` extra, keep `enable_raw_presences` off, and consider
  `guild_subscriptions=False` with explicit subscriptions where you need them.
- **`Event handler backlog is N`.** Handlers are slower than events; see
  [Events](events.md#handlers-falling-behind).

## asyncio errors

- **`RuntimeError: asyncio.run() cannot be called from a running event loop`**
  (Jupyter, inside another async framework): use `await client.start(token)`
  instead of `client.run(token)`.
- **`RuntimeError: Client has not been properly initialised...`**:
  `wait_until_ready()` or `wait_for()` was called before `login()`. Use
  `async with client:` or call it after login.
- **`setup_hook` hangs:** you awaited `wait_until_ready()` or `wait_for()` in
  it; the gateway connects only after it returns. Start a task instead.
- **`start() was called on a closed Client`** (a log line): create a new
  client, or `client.clear()` first.
- **`Task was destroyed but it is pending!`** at exit: a background task
  (yours, or a `delete_after`) was still running when the loop stopped. Cancel
  your tasks before closing.
- **`ClientException: change_presence() needs a connected gateway.`** Called
  before the gateway connected; use the `status=`/`activity=` options or call
  it from `on_ready`.

## Log lines explained

| Log line | Meaning |
|---|---|
| `Ignoring unknown Client option(s): ...` | A keyword `Client` doesn't know, usually a typo. See [Client options](client-options.md). |
| `Found user agent "...", build number N.` | Normal startup. |
| `Info API temporarily down. Falling back to manual retrieval...` | The identity lookup used its fallback; normal if rare. |
| `Gateway sent a frame that could not be decoded ... Resuming the session.` | A corrupt frame; recovered. |
| `The X cache reached its cap and now evicts the least recently used entries...` | Eviction started; expected on big accounts. |
| `Timed out waiting for chunks for guild ID ...` | Startup member loading gave up on a guild; `on_ready` continues. |
| `Cache persistence file ...` | See [Persistence](persistence.md#how-it-replays). |
| `Member.edit(...) only applies to your own member; ignored for member ID ...` | Self-only fields passed for someone else. |
| `Ignoring unknown option(s) ... for command ...` | A slash command option name that doesn't exist. |

[Logging and credentials](logging-and-credentials.md#warnings-worth-knowing)
has more.

## Warnings added in v2.3.3

Each of these is logged once per process. The library keeps working: it
skips the bad value and carries on.

- **"A member update carried a user without 'X'; the cached value was
  kept."** Discord sent a member update whose user object lacked a field.
  Earlier versions reset the cached field (a name to `None`, say). Please
  report it with the field name, since Discord's documentation says the field
  is always sent.
- **"Spotify.track_url is empty for a local file"**. A Spotify activity for a
  local file has no track ID. `track_url` is `''` (it used to be
  `https://open.spotify.com/track/None`). Check `track_id` before using the
  URL.
- **"A custom theme color is not a hex color"**. A custom client theme holds a
  color that is not hex. That color reads as `0`. Set the theme again in the
  Discord client.
- **"An Activity with type ActivityType.unknown cannot be sent to Discord"**.
  You passed an activity of type `unknown` to `change_presence`. It is sent as
  `playing`. Pick a real type.
- **"N event handler(s) were still running when the client closed"**.
  `close()` doesn't cancel handlers, and HTTP requests they make after it fail
  with curl_cffi's `SessionClosed`. Wait for your handlers, or stop them,
  before closing.

## Reporting a bug

Include:

- the full traceback;
- the output of `python -m discord -v` (library, Python, curl_cffi,
  discord-protos and OS versions);
- a minimal script that reproduces it, without your token;
- a DEBUG log. Credentials are redacted (see
  [Logging](logging-and-credentials.md)), but check it for message content
  first.

Never include your token. Report it in the
[issue tracker](https://github.com/Demonic1594/better.py-self/issues).
