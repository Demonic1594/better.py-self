# Tokens and login

How a user account logs in: getting and protecting the token, what happens
step by step when you call `run()` or `login()`, how the client makes itself
look like the Discord web client, captchas, the errors you can get, and what
makes Discord flag an account.

> Automating a user account is against Discord's Terms of Service, and nothing
> on this page changes that or makes it risk-free.

## The token

A user token is a long string of three dot-separated parts. Discord's own
clients send it in the `Authorization` header of every request, and the
library does the same. There's no login with an email and password: you give
the library a token you already have.

**Getting it from the browser:**

1. Log in at <https://discord.com/app> (a private window is best, see
   [below](#what-gets-accounts-flagged)).
2. Open the developer tools (Ctrl+Shift+I, or Cmd+Option+I on macOS) and the
   **Network** tab.
3. Click around so a few requests appear, filter on `api`, and select one.
4. In its request headers, copy the value of `Authorization`.

[`docs/authenticating.rst`](../docs/authenticating.rst) also has a console
snippet that prints the token.

**Keeping it safe:**

- It's equivalent to your password plus your 2FA: whoever has it is logged in
  as you, without a code.
- Read it from an environment variable or a file outside your repository, never
  from source code:

  ```python
  import os
  token = os.environ['DISCORD_TOKEN']
  ```

- Don't print it, and don't paste tracebacks or logs that contain it. The
  library redacts tokens and other credentials in its own logs (see
  [Logging and credentials](logging-and-credentials.md)), but not in yours.
  `enable_debug_events=True` handlers get raw, unredacted frames.
- **To revoke a token,** change your password (every token is invalidated), or
  log out of that session in Discord's settings (Devices).

**Token lifetime.** A token stays valid until you log out of that session,
change your password, or Discord invalidates it (for example after flagging
the account). Logging out of the browser tab you copied it from invalidates
it; closing the tab doesn't. That's why a private window works well: copy the
token, then close the window without logging out.

## What happens when you log in

`client.run(token)` and `await client.start(token)` do this, in order:

1. **Check the token locally.** A non-string raises `TypeError`. The token is
   stripped of whitespace, and an empty one raises
   `LoginFailure('Improper token has been passed (the token is empty).')`
   before any request is made. The log says `Logging in using static token.`
2. **Work out the client identity** (once per client): the super properties,
   user agent and browser fingerprint, as described in
   [the next section](#how-the-client-identifies-itself).
3. **Verify the token** with `GET /users/@me`. A 401 raises
   `LoginFailure('Improper token has been passed')`. Any other error is raised
   as the `HTTPException` it is.
4. **Replay the persistence snapshot**, if `persistence_path` is set and the
   snapshot matches this account (see [Persistence](persistence.md)).
5. **Run `setup_hook()`**, once.
6. **Connect to the gateway** and send IDENTIFY (or RESUME), with a 60-second
   timeout for the handshake. See [The gateway](gateway.md).
7. **Receive READY and READY_SUPPLEMENTAL,** fill the caches, subscribe to
   guilds, load member lists if `chunk_guilds_at_startup` is on, then dispatch
   `on_connect` and finally `on_ready`.

If login fails in step 1 to 3, the client closes its HTTP session again, so you
can retry with another token on the same client.

```python
import asyncio
import discord


async def try_token(token: str) -> bool:
    client = discord.Client()
    try:
        await client.login(token)   # steps 1 to 5, no gateway connection
        print('valid token for', client.user)
        return True
    except discord.LoginFailure:
        print('invalid token')
        return False
    finally:
        await client.close()


asyncio.run(try_token('...'))
```

> **Gotcha:** `client.user` is set by `login()`, but the caches (`guilds`,
> `private_channels`, `relationships`) are only filled by READY, after
> `connect()`.

## How the client identifies itself

Discord's anti-abuse systems look at how a client presents itself. The
official web client sends a set of **super properties** (operating system,
browser and version, client build number, and so on) as the base64-encoded
`X-Super-Properties` header on every request and in IDENTIFY, and its TLS
handshake has a recognisable browser fingerprint. The library reproduces both.

### The TLS fingerprint

Every request and the gateway websocket go through
[curl_cffi](https://github.com/lexiforest/curl_cffi), which uses a build of
libcurl that reproduces a real browser's TLS and HTTP/2 handshake. The library
picks the curl_cffi profile that matches the browser in the super properties
(the newest Chrome profile by default). The startup log names it:
`Found TLS fingerprint target "chrome1xx".` A plain Python HTTP client is
easy to tell apart from a browser at this level, which is one reason
Cloudflare used to block upstream's aiohttp-based requests.

### The super properties

Before the first request, `Client.headers_context()` builds a
`HeadersContext`. By default (`HeadersContext.default`) it impersonates the
**Chrome web client on Windows**:

1. It asks the **info API** at `https://cordapi.dolfi.es/api/v2/properties/web`
   for the current web client's properties. This service is run by
   discord.py-self's upstream maintainer. The request carries no token or
   account data. It times out after 10 seconds.
2. If that fails, it logs `Info API temporarily down. Falling back to manual retrieval...`
   at INFO and builds the properties itself:
   - **The build number** is scraped from `https://discord.com/login`. If that
     fails too, it logs **CRITICAL** `Could not retrieve client build number. Falling back to hardcoded value...`
     and uses `9999`. A wrong build number is conspicuous, so treat this log as
     a reason to stop and investigate (usually a network or proxy problem).
   - **The Chrome version** comes from Google's version history API, then
     from curl_cffi's newest Chrome profile, and as a last resort `136`
     (logged at WARNING).

The result is logged once: `Found user agent "Mozilla/5.0 ...", build number 4xxxxx.`

The lookups use the client's `proxy`, so they leave from the same IP as the
account's other traffic.

### Choosing another identity

Override `headers_context` to identify as something else. The desktop client
needs its full property set from the info API and has no fallback, so it
raises if the API is down:

```python
import aiohttp
import discord


class DesktopClient(discord.Client):
    async def headers_context(self):
        async with aiohttp.ClientSession() as session:
            return await discord.HeadersContext.desktop(session)
```

> **Warning:** building a `HeadersContext` from scratch with made-up
> properties is the quickest way to look unlike any real client. Stick to the
> factories (`default`, `desktop`) unless you know exactly what the official
> client sends.

Other headers sent with every request: `X-Discord-Locale` (`en-US`),
`X-Discord-Timezone` (from your system, or the `timezone` option),
`X-Debug-Options`, `Referer`, `Origin` and the browser's `Sec-Fetch-*` and
client-hint headers. Some actions (joining a guild from an invite, opening a
DM, adding a friend) also send `X-Context-Properties` describing where in the
UI the action came from, as the official client does. See
[Discord user API notes](discord-user-api-notes.md#super-properties).

## Captchas

Discord sometimes requires an hCaptcha before an action. It's common on
opening or sending DMs, joining guilds and friend requests, and on new or
low-reputation accounts. It's Discord's decision about the account and the
action, not a library bug, and retrying without solving it doesn't help.

The request then fails with:

```text
discord.errors.CaptchaRequired: 400 Bad Request (error code: -1): Captcha required
```

`CaptchaRequired` is an `HTTPException` with these attributes:

| Attribute | Meaning |
|---|---|
| `service` | Which captcha: usually `'hcaptcha'`; `'recaptcha'` (the default when Discord doesn't say) or `'recaptcha_enterprise'` are possible. |
| `sitekey` | The site key to load the captcha with. |
| `rqdata`, `rqtoken` | Extra challenge data some captchas need. |
| `session_id` | The captcha session, sent back with the solution. |
| `should_serve_invisible` | Whether Discord asked for the invisible variant. |
| `errors` | The `captcha_key` error strings Discord returned. |

### Handling them

Pass `captcha_handler`, an async function that receives the exception and the
client and returns the solved token. The library then resends the same
request with `X-Captcha-Key` (the solved token), plus `X-Captcha-Session-Id` and
`X-Captcha-Rqtoken` when Discord gave them:

```python
import discord


async def solve(exc: discord.CaptchaRequired, client: discord.Client) -> str:
    if exc.service != 'hcaptcha':
        raise exc                     # give up: the original request raises it
    print(f'captcha needed: sitekey={exc.sitekey}')
    return await ask_a_human(exc)     # your code: show it to a person and return the token


client = discord.Client(captcha_handler=solve)
```

Without a handler, `CaptchaRequired` is raised to the code that made the
request. With one, the handler is called for each captcha the same request
gets, at most four times; a fifth captcha on that request is raised. What you return isn't checked: a wrong token just produces another
captcha.

[`examples/captcha.py`](../examples/captcha.py) shows the hCaptcha challenge
to you in a PyQt6 webview so you can solve it by hand. It supports hCaptcha
only.

**Solver services.** Upstream's maintainer says there are no reliable public
solving services left (dolfies/discord.py-self#517), and there are reports of
solved tokens still being rejected (#890). Showing the challenge to a person,
as the example does, is the dependable option.

> **Gotcha:** a captcha that comes with HTTP 403 is raised as `Forbidden`
> (403 is checked first), not `CaptchaRequired`, so the handler isn't called
> for it. Check `exc.json` for `captcha_key` if you catch `Forbidden` on an
> action that may need a captcha.

## Errors at login

| Error | Meaning | What to do |
|---|---|---|
| `TypeError: expected token to be a str, received ...` | You passed something other than a string. | Pass the token string. |
| `LoginFailure: Improper token has been passed (the token is empty).` | Empty or whitespace-only token. | Check where you read it from (an unset environment variable?). |
| `LoginFailure: Improper token has been passed` | `GET /users/@me` returned 401: wrong, expired, or invalidated token. | Copy a fresh token. |
| `HTTPException` 403 with code `40002` | The account must verify its email or phone (Discord's own message says which). | Verify the account in the official client. |
| `CaptchaRequired` at login | Rare; Discord wants a captcha even for this. | Log in once in a browser, solve it there. |
| `ConnectionClosed` with code `4004` | The gateway rejected the token. The client doesn't reconnect. | Copy a fresh token. |
| `ConnectionClosed` with code `4013`/`4014` | Invalid or disallowed capabilities or intents; shouldn't happen with this library. | Report it. |
| `RuntimeError: Client has not been properly initialised...` | `wait_until_ready()` called before `login()` or outside `async with`. | Call it after login, or inside `async with client:`. |
| `start() was called on a closed Client` (a log line; `start()` returns) | The client was closed before. | Create a new client, or call `client.clear()`. |

## Sessions

Every connected client is a **session** of your account, like a browser tab
or the desktop app. Discord shows them under Settings → Devices.

- The library and your app running at once with the same token look like two
  sessions from one login, which is unusual (see below).
- `sync_presence=True` (the default) makes the library follow status changes
  made on your other sessions, as the official clients do.
- `client.sessions` lists the account's gateway sessions as Discord reports
  them: the library's own, the others, and an "all" session that represents
  your overall presence.

## What gets accounts flagged

Upstream's maintainer, in dolfies/discord.py-self#930, says Discord's
suspicious-activity detection reacts to:

- **Sudden changes** in a token's IP location or client details (a token
  copied from your desktop app suddenly used from a VPS in another country, with
  a different client).
- **One token in two places**, the desktop app and the library at once.

His advice is to use a **dedicated token taken from a private browser
session**, used only by the library, from one stable IP.

What this library does to look like the official client:

- **TLS:** a browser fingerprint through curl_cffi.
- **Headers:** `X-Super-Properties` with a live build number when the lookup
  succeeds, and the browser's other headers.
- **Gateway:** the same IDENTIFY properties and capabilities as the web client,
  QoS heartbeats and the periodic "time spent" reports the web client sends.
- **No bot-only endpoints:** for example, `Member.fetch_voice` uses the cache
  on user accounts instead of an endpoint that returns `20002`.

What you control:

- **Pace your actions,** especially sends, reactions, DMs and friend
  requests. See [Rate limits and pacing](rate-limits-and-pacing.md).
- **Don't mass-DM or mass-friend.** That's what anti-spam is for.
- **One account per IP**, ideally a residential one, and stable over time.
  Many accounts behind one datacenter IP stand out.
- **Watch the warning signs:** captchas becoming frequent, phone verification
  requests (`40002`), `40333` (Cloudflare), or 340xxx codes (account
  restrictions, see [Discord error codes](error-codes.md)). Stop and slow down
  when they appear.
