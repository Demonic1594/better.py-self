# Logging and credentials

How the library logs, how to configure it, what each level tells you, the
warnings worth knowing, what is redacted from logs (and what isn't), and how
to look after your token and the other secrets the library handles.

## Loggers

Everything goes through the standard `logging` module under the `discord`
logger, with one child logger per module:

| Logger | What it logs |
|---|---|
| `discord.client` | Login, the connect loop, reconnects and backoff, handler errors (`Ignoring exception in ...`), the backlog warning, the close-time warning. |
| `discord.gateway` | Connecting, HELLO/READY problems, heartbeats (`Can't keep up`, `Heartbeat blocked`), undecodable frames, every event at DEBUG. |
| `discord.http` | The user agent and fingerprint at startup, 429s and retries, every request and response at DEBUG. |
| `discord.state` | Parser warnings (malformed or unexpected payloads), cache eviction notices, chunking and scraping, persistence. |
| `discord.voice_state`, `discord.voice_client`, `discord.player` | Voice handshakes, reconnects, audio player errors. |
| `discord.ext.commands`, `discord.ext.tasks` | Command and loop errors. |

## Setting it up

`Client.run` installs a handler for you: coloured output on stderr at INFO
(colour when the stream is a terminal, or inside Docker). Its parameters:

```python
import logging

client.run(token)                                            # default: INFO to stderr, discord loggers only

handler = logging.FileHandler('discord.log', encoding='utf-8', mode='w')
client.run(token, log_handler=handler, log_level=logging.DEBUG)

client.run(token, log_handler=handler, root_logger=True)    # configure the root logger (your loggers too)

client.run(token, log_handler=None)                          # don't touch logging; configure it yourself
```

`log_formatter=` takes a `logging.Formatter`. Without `run()` (with `start()`),
nothing is configured for you: call `discord.utils.setup_logging()` yourself,
or configure `logging` directly. If logging isn't configured at all, the
library's warnings and errors go nowhere.

```python
import logging
import discord

discord.utils.setup_logging(level=logging.INFO, root=False)   # same defaults as run()
```

`setup_logging(handler=..., formatter=..., level=..., root=True)` defaults to
the **root** logger when you call it directly, while `run()` passes
`root=False`.

### Recipes

**A rotating file at DEBUG, the console at INFO:**

```python
import logging
import logging.handlers

logger = logging.getLogger('discord')
logger.setLevel(logging.DEBUG)

file = logging.handlers.RotatingFileHandler('discord.log', maxBytes=32 * 1024 * 1024, backupCount=5, encoding='utf-8')
file.setFormatter(logging.Formatter('[{asctime}] [{levelname:<8}] {name}: {message}', '%Y-%m-%d %H:%M:%S', style='{'))
logger.addHandler(file)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
logger.addHandler(console)

client.run(token, log_handler=None)
```

**Quiet the chatty loggers:**

```python
logging.getLogger('discord.http').setLevel(logging.INFO)      # no per-request DEBUG lines
logging.getLogger('discord.gateway').setLevel(logging.INFO)   # no per-event DEBUG lines
```

**Show deprecation warnings** (they go through `warnings`, not `logging`, and
Python hides `DeprecationWarning` by default):

```sh
python -W default::DeprecationWarning bot.py
```

or `logging.captureWarnings(True)` to route them into your logging setup.

See [`docs/logging.rst`](../docs/logging.rst) for more.

## What each level tells you

- **DEBUG:** every gateway event and HTTP request and response (redacted),
  heartbeat details, cache decisions. Very large on a busy account: tens of
  megabytes per hour.
- **INFO:** the startup lines (`Logging in using static token.`, the user
  agent and build number, the TLS fingerprint target, the gateway session),
  reconnects and resumes, the first eviction of each cache, persistence replay.
- **WARNING:** something the library worked around: a 429, a retried 5xx, a
  malformed payload it skipped, a value it couldn't use. Worth reading.
- **ERROR / CRITICAL:** handler exceptions (`Ignoring exception in on_message`),
  failures it couldn't work around. `Could not retrieve client build number`
  is CRITICAL because a fallback build number is conspicuous.

### Warnings worth knowing

| Warning | Meaning |
|---|---|
| `Ignoring unknown Client option(s): ...` | A misspelt or foreign (`intents`) option. |
| `PyNaCl is not installed, voice will NOT be supported.` / `davey is not installed...` | Install the `voice` extra if you need voice. |
| `We are being rate limited. ... Retrying in X seconds.` | A 429 absorbed; see [Rate limits and pacing](rate-limits-and-pacing.md). Frequent ones mean you're going too fast. |
| `Gateway has stopped responding. Closing and restarting.` | Nothing arrived for `heartbeat_timeout`; the client resumes. |
| `Heartbeat blocked for more than 10 seconds.` | Your code blocked the event loop. The log includes the stack. |
| `Can't keep up, websocket is X.Xs behind.` | The loop is overloaded; events are processed late. |
| `Event handler backlog is N (oldest Xs old).` | Handlers are slower than events. See [Events](events.md#handlers-falling-behind). |
| `Gateway sent a frame that could not be decoded ... Resuming the session.` | A corrupt frame; the client recovered. |
| `N event handler(s) were still running when the client closed.` | Requests they make now fail with `SessionClosed`. |
| `A member update carried a user without 'X'; the cached value was kept.` | Shown once per field. Please report it with the field name. |
| `Spotify.track_url is empty for a local file...` | Check `track_id` before building a URL. |
| `A custom theme color is not a hex color ...` | Reads as `0`; set the theme again in the client. |
| `An Activity with type ActivityType.unknown cannot be sent to Discord...` | It was sent as "Playing". |
| `Member list scraping for guild ID ... got no member list from Discord` | See [Member lists](member-lists.md#when-the-sidebar-returns-nothing). |
| `Not writing the cache persistence file: the snapshot is over the 256 MiB limit` | Persistence can't work for this account. |

The ones marked "once" in [Troubleshooting](troubleshooting.md#warnings-added-in-v233)
are logged once per process, whatever the number of occurrences.

## What's redacted, and what isn't

**DEBUG logs redact every known credential field.** Before v2.3.2, DEBUG
logging to a file recorded the READY payload's tokens, the passwords sent when
you edit your profile, and the voice encryption key. Now everything logged
goes through `utils._redact` and `utils._RedactedLog` first. The code is in
[`discord/utils.py`](../discord/utils.py).

- **Redacted keys.** The values of these JSON keys become `[redacted]`
  wherever they appear: `token`, `auth_token`, `analytics_token`,
  `access_token`, `refresh_token`, `secret`, `secret_key`, `backup_codes`,
  `password`, `new_password`, `email`, `phone`, `ticket`, `captcha_key`,
  `payment_source_token`, `billing_address_token`, `billing_address`, `bank`,
  `purchase_token` and `two_way_link_code`.
- **Where.** Raw gateway events, HTTP request and response bodies, and voice
  payloads.
- **Headers aren't logged at all,** so the `Authorization` header never
  appears.
- **URLs.** Webhook and interaction tokens are masked in logged URLs
  (`/webhooks/<id>/[redacted]`, `/interactions/<id>/[redacted]`), including in
  the 429 and deferred-request warnings and the rate-limit bucket keys.
  Signed query parameters of pre-signed upload URLs (`X-Goog-Signature`,
  `X-Amz-Signature`, `Signature`, `sig`, `token`...) are masked too.
- **Lazy.** Redaction runs only when a DEBUG line is actually emitted, so it
  costs nothing when DEBUG is off.

Limits:

- **Only those keys.** A credential Discord adds under a new key name isn't
  caught until it's added to the list.
- **Nested values in request bodies.** Request bodies are logged as JSON text
  and redacted with a pattern that handles strings, numbers, flat arrays and
  flat objects under a sensitive key. A sensitive key whose value is a deeper
  nested structure isn't fully masked. Responses and gateway events (parsed
  objects) are redacted at any depth.
- **Your handlers get raw data.** `on_socket_raw_receive` and
  `on_socket_raw_send` (behind `enable_debug_events`) receive the **raw,
  unredacted** frames.
- **Message content isn't a credential.** DEBUG logs contain messages, names
  and IDs. Read them before sharing.

Checked live at v2.3.2:

- About **50 MB of DEBUG logs** from four test accounts held **no fragment**
  of any account token.
- A webhook created during the test never had its token appear in the logs,
  even with `Webhook.from_url` and message edits.

## Other secrets the library handles

| Secret | Where | Protection |
|---|---|---|
| Your token | In memory; sent in `Authorization`. | Never logged. |
| The [persistence](persistence.md) snapshot | The file you configure. | Written `0600`; contains your user object, relationships, DMs and guild member data, no token. Treat it as a secret. |
| Voice encryption keys | In memory during a voice connection. | Redacted in logs (`secret_key`). |
| Captcha solutions | Sent once in `X-Captcha-Key`. | Not logged. |
| Webhook URLs | Whatever you store. | Masked in logs; a webhook URL is a credential, keep it like a token. |

## Looking after your token

- **Keep it out of your code.** Read it from an environment variable or a
  file outside the repository. Never commit it, paste it into an issue, or
  post it in a log.
- **Rotate it if it leaks.** Changing your password invalidates every token.
- **Don't share a token between the app and the library.** Upstream's
  maintainer has seen accounts flagged when one token is used in the desktop
  app and in the library at once, or when the token's IP or client details
  change quickly. Take a dedicated token from a private browser session
  (dolfies/discord.py-self#930). See [Tokens and login](tokens-and-login.md).
- **Report bugs without the token.** Include the DEBUG log; the library's own
  output is safe to share as far as credentials go, but check it for message
  content first.
