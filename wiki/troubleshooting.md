# Troubleshooting

## Captcha required

`discord.CaptchaRequired: 400 Bad Request (error code: -1): Captcha required`

Discord's anti-spam sometimes requires an hCaptcha before an action. It's
common on opening or sending DMs, joining guilds and friend requests. In the
v2.3.2 live tests, `create_dm` **and** a send in an existing DM channel both
got one. It's a decision Discord makes about the account (its age,
reputation, recent activity), not a library bug, and retrying won't help.

**Handling it.** Pass `captcha_handler`, an async function that receives the
exception and the client and returns a solved token:

```py
async def solve(exc: discord.CaptchaRequired, client: discord.Client) -> str:
    # exc.service ('hcaptcha'), exc.sitekey, exc.rqdata, exc.rqtoken,
    # exc.session_id, exc.should_serve_invisible
    return await my_solver(exc)

client = discord.Client(captcha_handler=solve)
```

The library resends the request with `X-Captcha-Key`, plus `X-Captcha-Session-Id`
and `X-Captcha-Rqtoken` when Discord gave them.
[`examples/captcha.py`](../examples/captcha.py) shows the hCaptcha challenge to
you in a PyQt6 webview, so you can solve it by hand.

**Solver services.** Upstream's maintainer says there are no reliable public
solving services left (dolfies/discord.py-self#517). Reports of a solved token
still being rejected are in #890. Showing the challenge to a person, as the
example does, is the dependable option.

## Login failures

- **`LoginFailure: Improper token has been passed`** means the HTTP login got a
  401: the token is wrong, expired, or was invalidated by a password change.
  Copy a fresh `Authorization` header (see [Getting started](getting-started.md#your-token)).
  An empty token raises `LoginFailure('Improper token has been passed (the token
  is empty).')` before any request is made.
- **Gateway close 4004** (authentication failed) is the gateway's version of the
  same failure, and the client doesn't reconnect after it.
- **4015 (too many sessions)** means the account has more gateway sessions than
  Discord allows for a user account. Close the extra sessions.

## The account was flagged or disabled

Upstream's maintainer, in dolfies/discord.py-self#930, says Discord's
suspicious-activity detection reacts to:

- **sudden changes** in a token's IP location or client details
- **one token in two places**, the desktop app and the library at once

His advice is to use a dedicated token taken from a private browser session.

What this library does to look like the official client:

- **TLS.** Requests go through curl_cffi with a browser TLS fingerprint
  (dolfies/discord.py-self#500 explains why that matters to Cloudflare).
- **Headers.** It sends current `X-Super-Properties` with a live build number
  (see [Discord user API notes](discord-user-api-notes.md#super-properties)).
- **Bot-only endpoints.** It doesn't call them: `Member.fetch_voice` uses the
  cache on user accounts.

On your side:

- pace your actions (see [Rate limits and pacing](rate-limits-and-pacing.md))
- don't spam DMs or friend requests
- remember that automating a user account is against Discord's ToS whatever
  you do

## 403 with code 20002, "Only bots can use this endpoint"

You called something Discord reserves for bot accounts. The voice-state REST
routes are one example. Use the user-account alternative; for voice states
that's `member.voice`, or `Member.fetch_voice()`, which falls back to it.

## Other Discord error codes

From Discord's user-API docs:

- **40333:** blocked by Cloudflare. Slow down. After 10,000 invalid requests
  (401, 403 or 429) in 10 minutes, Discord bans the IP for 24 hours.
- **403 with code 10008 ("Unknown Message"):** despite the message, this means
  Discord's anti-abuse system blocked the request.
- **`RateLimited`** is raised only when you set `max_ratelimit_timeout` and a wait
  would be longer than that. Otherwise the library waits out rate limits for you.

## Events are missing

- **No `on_message` in a big guild.** Guilds of 75,000 members or more aren't
  subscribed automatically. Call `guild.subscribe(...)`. See
  [Member lists](member-lists.md).
- **No `on_member_update` or `on_message_edit`.** The member or message wasn't
  cached (the caps evict cold entries). Use the raw events (`on_raw_*`).
- **No `on_raw_presence_update`.** It's opt-in: pass `enable_raw_presences=True`.
- **A typing event was dropped.** That happens when its channel isn't cached,
  or when its user can't be resolved (for example a DM without a recipient).
  `on_raw_typing` is planned for v2.3.4.
- **Handlers seem to lag.** Watch `on_dispatch_backlog` and
  `cache_stats().dispatch_backlog`. A slow handler is holding up the queue.

## The member list is empty or incomplete

- **The sidebar returned nothing.** The log shows "Member list scraping ... got
  no member list from Discord (no SYNC for the requested ranges ...)". Pass
  `channels=` that everyone can view.
- **Offline members are missing.** Without `kick_members`, `ban_members` or
  `manage_roles`, `fetch_members` scrapes the sidebar, which in guilds over
  1,000 members only lists online members.
- **`get_member` returns `None`.** The member was evicted. Use
  `guild.fetch_member(id)`, or raise the caps.

## `on_ready` takes a long time

`chunk_guilds_at_startup=True` (the default) waits until the guilds that can be
chunked have been chunked. On an account with many large guilds, set it to
`False` and fetch members only where you need them, or use
[persistence](persistence.md) to restart warm.

## Voice close codes

- **`davey is not installed, voice will NOT be supported.`** or **`PyNaCl is not
  installed`:** install the `[voice]` extra. Joining voice with
  `change_voice_state` still works without it.
- **4017, "E2EE/DAVE protocol required":** the voice server requires DAVE.
  Install `davey` (dolfies/discord.py-self#917).
- **4006, or random voice disconnects:** these are known upstream issues
  (dolfies/discord.py-self#870, #877, #945). Reconnect, or choose another region
  with `preferred_rtc_regions`.
- **`send_sound` fails:** check that you're **in** the channel, not server muted or
  suppressed, and have `speak` and `use_soundboard`. Sounds from another guild
  need Nitro.

## The log says "Ignoring unknown Client option(s)"

You passed a keyword `Client` doesn't know, usually a typo. See
[Client options](client-options.md).

## Reporting a bug

Include:

- the full traceback
- the library version (`discord.__version__`)
- your Python version and OS
- a DEBUG log. Credentials are redacted (see [Logging](logging-and-credentials.md)),
  but check it for message content first.

Never include your token. Report it in the
[issue tracker](https://github.com/Demonic1594/better.py-self/issues).
