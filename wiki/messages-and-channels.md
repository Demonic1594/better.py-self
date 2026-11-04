# Messages and channels

These work as in discord.py-self. This page covers user-account specifics,
the changes this fork made, and what was confirmed live at v2.3.2.

## Sending

```py
msg = await channel.send('hello', file=discord.File('image.png'), allowed_mentions=discord.AllowedMentions.none())
msg = await msg.edit(content='edited')
await msg.add_reaction('🦆')
await msg.delete()
```

- **Pacing.** Pace sends and reactions yourself; see
  [Rate limits and pacing](rate-limits-and-pacing.md). Lanes of 2.6 s for sends
  and 3.0 s for reactions caused no throttles in live tests.
- **Nonces.** Messages carry snowflake-shaped nonces, as the official client's do.
- **Captcha on DMs.** Opening a DM, or sending in one, can return **"Captcha
  required"**. Discord does this to curb DM spam, and it applies to the account,
  not the library. In the v2.3.2 live tests both `create_dm` and a send in an
  existing DM channel got it. See
  [Troubleshooting](troubleshooting.md#captcha-required).

### Uploads

Files go through Discord's cloud-upload flow: the library asks for a
pre-signed URL, uploads the file there, then sends the message referring to it.
The upload uses `aiohttp`, which is imported only when an upload or CDN request
needs it. REST calls and the gateway use curl_cffi.

- Since v2.3.1, the local file path is no longer sent to Discord.
- Pre-signed upload URLs are masked in logs.
- Live test: a 1.6 KB text file was uploaded, read back with
  `Attachment.read()` and came back byte-for-byte identical.

## Polls

```py
poll = discord.Poll(question='Lunch?', duration=datetime.timedelta(hours=2))
poll.add_answer(text='Pizza').add_answer(text='Sushi', emoji='🍣')
msg = await channel.send(poll=poll)
await msg.end_poll()
```

- **Duration is in hours.** Since v2.3.2 the library rounds it to whole hours
  (ported from Rapptz), because Discord accepts nothing else. In the live test,
  1 h 20 min became 1 h.
- **Questions** can be up to 300 characters.
- **Ending a poll.** After `end_poll()`, Discord tallies the votes in the background.
  The message `end_poll()` returns isn't marked finalised yet; fetched again 5
  seconds later, it was (live test). Re-fetch the message if you need the final
  counts.
- **Vote events.** `on_poll_vote_add`/`on_poll_vote_remove` and their raw forms
  fire once for each vote added or removed.

## Threads

- **Creating:** `await text_channel.create_thread(name=..., type=discord.ChannelType.public_thread)`,
  or pass `message=` to start the thread from a message.
- **Managing:** `await thread.edit(archived=True)` and `await thread.delete()`.
  `await thread.fetch_members()` lists the thread's members.
- **Caching.** Threads are capped like members (see
  [Caching and memory](caching-and-memory.md)). An evicted thread comes back with
  `Guild.fetch_channel`.
- **Live test:** create, send, archive and delete all worked.

## Webhooks

```py
hook = await channel.create_webhook(name='hook')
await hook.send('hi', username='someone', wait=True)

partial = discord.Webhook.from_url(hook.url, client=client)
m = await partial.send('via url', wait=True)
await partial.edit_message(m.id, content='edited')
await partial.delete_message(m.id)
await hook.delete()
```

Webhook tokens are masked in every logged URL; the live test found no webhook
token in 12 MB of DEBUG logs. The webhook adapter uses `aiohttp`. Webhooks bound
to a client share its aiohttp session, which keeps cookies. That's why the
Rapptz change that turns cookies off (`DummyCookieJar`) wasn't ported: without
cookies, clients get 403 responses from Cloudflare.

## Cloning channels

`await channel.clone(name=..., category=..., reason=...)` creates a copy with
the same settings, now including the fields Rapptz fixed in v2.3.2. Voice and
stage channels take a `category=` argument.

Live test: text and voice clones had the same topic, NSFW flag, slowmode,
category, overwrites, bitrate and user limit as their originals.

## Components and interaction metadata

**User accounts receive every component type that bots send:**

- action rows, buttons, selects and text inputs
- the newer layout components: section, text display, thumbnail, media gallery,
  file, separator, container, label, file upload, radio group, checkbox group
  and checkbox

All of them are parsed into their classes in `discord/components.py`, matching
Rapptz's set. A user account can also click buttons and choose from selects
(`Button.click()`, `SelectMenu.choose()`) and submit modals, as the official
client does. Pace clicks on the "click" lane (3.0 s).

**`Message.interaction_metadata`** (new in v2.3.2) describes the interaction that
produced a bot's message: which command, button or modal it was, who used it
(`user`), the original response message, the message that was interacted with,
and, for context-menu commands, the target user or message. `Message.interaction`
also keeps working; in this fork it isn't deprecated.

A live scan of 621 recent messages in the test accounts' servers found none
carrying interaction metadata, so only unit tests cover parsing it.

## Calls and DMs

- **DMs:** `user.create_dm()` and `DMChannel.recipient`. Group DMs are
  `GroupChannel`, with `recipients`.
- **Call messages:** a DM or group call message has a `CallMessage`
  (`discord/calls.py`) with `participants`, `ended_timestamp`, `initiator` and
  `call_ended`, which says whether the call has ended. `call_ended` is what
  Rapptz calls `is_ended()`.
- **Calls:** `on_call_create`, `on_call_update` and `on_call_delete` track
  ringing calls.
