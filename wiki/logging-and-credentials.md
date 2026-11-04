# Logging and credentials

## Setting up logging

The library logs through the standard `logging` module, under the `discord`
logger. Each module has its own child logger: `discord.client`,
`discord.gateway`, `discord.http`, `discord.state`, and so on.

`Client.run` installs a default handler for you: coloured output on stderr at
INFO. You can change it:

```py
import logging

handler = logging.FileHandler('discord.log', encoding='utf-8', mode='w')
client.run(token, log_handler=handler, log_level=logging.DEBUG)

# root_logger=True applies the configuration to every logger, not just discord's
client.run(token, log_handler=handler, root_logger=True)

# log_handler=None turns the built-in setup off; configure logging yourself
client.run(token, log_handler=None)
```

Without `Client.run`, call `discord.utils.setup_logging(...)` yourself, or configure
`logging` directly. If logging isn't configured at all, the library's warnings
and errors go nowhere.

`discord.http` is very chatty at DEBUG. A common setup keeps it at INFO:
`logging.getLogger('discord.http').setLevel(logging.INFO)`. See
[`docs/logging.rst`](../docs/logging.rst) for more.

## What's logged, and what's kept out

**DEBUG logs redact every known credential field.** Before v2.3.2, enabling DEBUG
logging to a file recorded the READY payload's tokens, the passwords sent when
you edit your profile, and the voice encryption key. Now everything logged goes
through `utils._redact` and `utils._RedactedLog` first. The code is in
[`discord/utils.py`](../discord/utils.py).

- **Redacted keys.** The values of these JSON keys become `[redacted]`
  wherever they appear: `token`, `auth_token`, `analytics_token`,
  `access_token`, `refresh_token`, `secret`, `secret_key`, `backup_codes`,
  `password`, `new_password`, `email`, `phone`, `ticket`, `captcha_key`,
  `payment_source_token`, `billing_address_token`, `purchase_token` and
  `two_way_link_code`.
- **Only those keys.** Redaction works on that list. A credential Discord adds
  under a new key name isn't caught until it's added, so still treat DEBUG logs
  as sensitive, and don't post them publicly without reading them first.
- **Where.** This applies to raw gateway events, HTTP request and response bodies,
  and voice payloads.
- **URLs.** Webhook and interaction tokens are masked in logged URLs
  (`/webhooks/<id>/<token>` and `/interactions/<id>/<token>`), including the
  429 and deferred-request warnings and the rate-limit bucket keys. Pre-signed
  upload URLs are masked too.
- **Not redacted.** `on_socket_raw_receive` / `on_socket_raw_send` (behind
  `enable_debug_events`) still pass the **raw, unredacted** data to your own
  handler. Don't log that anywhere public.

Checked live at v2.3.2:

- About **50 MB of DEBUG logs** from four test accounts held **no fragment** of
  any account token.
- A webhook created during the test never had its token appear in the logs,
  even with `Webhook.from_url` and message edits.

## Other secrets the library writes

The [persistence](persistence.md) snapshot contains part of the READY payload
(your user, cached users, relationships and private channels) and the guilds. It's
written with owner-only (`0600`) permissions, but treat it as a secret anyway.

## Looking after your token

- **Keep it out of your code.** Read the token from an environment variable or a
  file outside the repository. Never commit it, paste it into an issue, or post
  it in a log.
- **Rotate it if it leaks.** Changing your password invalidates every existing
  token.
- **Don't share a token between the app and the library.** Upstream's
  maintainer has seen accounts flagged when one token is used in the desktop app
  and in the library at once, or when the token's IP or client details change
  quickly. Take a dedicated token from a private browser session
  (dolfies/discord.py-self#930).
- **Report bugs without the token.** When reporting a bug, include the DEBUG log.
  It's now safe to share, but check it for message content first.
