# Messages

Sending, uploading, editing, deleting, replying, forwarding, reactions,
history, search, read states, polls and embeds, from a user account: every
parameter, the limits, and what's different from a bot. Channels, threads,
DMs and webhooks are on [Channels and threads](channels-and-threads.md);
buttons, selects, modals and slash commands on
[Interactions and components](interactions-and-components.md).

## Sending

```python
msg = await channel.send('hello')
```

`Messageable.send` works on every channel you can write to (`TextChannel`,
`Thread`, `DMChannel`, `GroupChannel`, `VoiceChannel` text chat,
`PartialMessageable`), on `User` and `Member` (it opens the DM), and on
`commands.Context`.

| Parameter | Effect |
|---|---|
| `content` | The text. Converted with `str()`. Up to 2,000 characters (4,000 with Nitro); not checked by the library. |
| `tts` | Text-to-speech. |
| `file` / `files` | One `File`, or a list of up to 10. Passing both raises `TypeError`; more than 10 raises `ValueError`. |
| `stickers` | Up to 3 `GuildSticker` or `StickerItem` (the limit is Discord's). |
| `reference` | A `Message`, `MessageReference` or `PartialMessage` to reply to. |
| `mention_author` | Whether a reply pings the author (overrides `allowed_mentions.replied_user`). |
| `allowed_mentions` | An `AllowedMentions`, merged over the client's default. |
| `silent` | Send without a push or desktop notification (`@silent`). Mentions still count as mentions. |
| `suppress_embeds` | Don't generate link previews. |
| `poll` | A `Poll` to attach. |
| `delete_after` | Seconds after which the message is deleted in the background. |
| `nonce` | By default a snowflake-shaped nonce is generated, like the official client's. `None` sends none. |

```python
# A reply that doesn't ping the author
await message.reply('noted', mention_author=False)

# Or with send()
await channel.send('noted', reference=message, mention_author=False)

# No mentions at all, whatever the text contains
await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

# Silent, with a file, deleted after a minute
await channel.send('log attached', file=discord.File('run.log'), silent=True, delete_after=60)
```

> **Gotcha: no embeds and no components from `send()`.** User accounts can't
> send rich embeds or components. `send()` has no `embed`, `embeds`,
> `components` or `view` parameter; passing one raises `TypeError`. Webhooks
> can send both (see [Channels and threads](channels-and-threads.md#webhooks)).
> Links you post still get Discord's automatic previews.

> **Gotcha: captchas on DMs.** Opening a DM, or sending in one, can return
> `CaptchaRequired`. Discord does this to curb DM spam, based on the account.
> Both `create_dm` and a send in an existing DM channel can get one. See [Tokens and login](tokens-and-login.md#captchas).

Pace sends at 2.6 s or slower. See
[Rate limits and pacing](rate-limits-and-pacing.md).

### Typing indicator

```python
async with channel.typing():      # shows "... is typing" while the block runs
    result = await slow_work()
await channel.send(result)
```

## Files and uploads

```python
await channel.send(file=discord.File('chart.png'))
await channel.send(files=[discord.File('a.png'), discord.File('b.png', spoiler=True)])

# From memory: wrap bytes in a BytesIO
import io
await channel.send(file=discord.File(io.BytesIO(data), filename='report.csv'))
```

`File(fp, filename=None, *, spoiler=MISSING, description=None, title=None, remix=False)`:

| Argument | Meaning |
|---|---|
| `fp` | A path (`str` or `os.PathLike`), or a readable, seekable binary file object. |
| `filename` | The name Discord shows. Defaults to the path's base name. |
| `spoiler` | Blur it. A name starting with `SPOILER_` also makes it a spoiler (the prefix is removed). |
| `description` | Alt text, for images. |
| `title`, `remix` | The attachment title, and whether the image is a remix. |

Rules:

- **A path is opened and closed by the library.** After the send, the file is
  closed; a `File` made from a path can't be sent again.
- **A file object stays yours.** The library doesn't close it. It must be
  readable and seekable, or `ValueError: File buffer ... must be seekable and readable`.
- **`bytes` are a file path,** as in upstream (a bytes path is valid on
  POSIX); they aren't file contents. For data in memory, use
  `io.BytesIO(data)`. Passing data by mistake raises the usual
  `FileNotFoundError` (or `ValueError` for data with a null byte), and since
  v2.3.4 the message says to use `io.BytesIO`.
- **A `File` is single use.** To send the same file object twice, call
  `file.reset()` (rewinds it) or make a new `File`.
- **Files opened from a path are closed** even when `send()` rejects its
  arguments (more than 10 files, say).

**How uploads work.** Files go through Discord's cloud-upload flow, like the
official client: the library asks for a pre-signed URL, uploads the file there
(retrying on transient errors), then sends the message referring to it. The
upload uses `aiohttp`, imported the first time it's needed; REST calls and the
gateway use curl_cffi. Since v2.3.1 the local path isn't sent to Discord, and
pre-signed URLs are masked in logs.

### Attachments you receive

```python
for attachment in message.attachments:
    data = await attachment.read()                 # bytes
    await attachment.save(f'downloads/{attachment.filename}')   # to a path (written in a thread)
    reupload = await attachment.to_file(spoiler=attachment.is_spoiler())
    await other_channel.send(file=reupload)
```

`save(fp)` returns the number of bytes written. `use_cached=True` uses the
proxy URL, which keeps working for a short while after the message is
deleted.

## Editing and deleting

```python
msg = await msg.edit(content='edited')                  # returns the new message
msg = await msg.edit(attachments=[])                    # remove every attachment
msg = await msg.add_files(discord.File('more.png'))     # keep the existing ones, add one
msg = await msg.remove_attachments(msg.attachments[0])
msg = await msg.edit(suppress=True)                     # hide its link previews

await msg.delete()
await msg.delete(delay=10)   # in the background; errors other than HTTP errors are logged
```

`edit(content=..., attachments=..., suppress=..., delete_after=..., allowed_mentions=...)`:

- Only your own messages can be edited.
- `attachments=[...]` is the full list to keep; new `File`s in it are uploaded
  after the kept ones.
- `suppress` on someone else's message needs Manage Messages.
- `edit()` returns a **new** `Message`; the old object isn't updated in place.

Deleting your own messages needs no permission; anyone else's needs Manage
Messages (or it's a DM, where you can only delete your own).

To act on a message you only know by ID, without fetching it:

```python
partial = channel.get_partial_message(message_id)
await partial.edit(content='fixed')
await partial.delete()
```

## Replies and forwards

```python
await message.reply('answer')                           # a reply with the default mention behaviour
ref = message.to_reference(fail_if_not_exists=False)    # if the message may be deleted meanwhile
await channel.send('answer', reference=ref)

await message.forward(other_channel)                    # forward it (no content of your own)
```

- `message.reference` is set on replies and forwards; for a forward,
  `message.message_snapshots` holds the forwarded content.
- `forward()` doesn't check `message.is_forwardable()` first: system
  messages, polls, calls and activities can't be forwarded, and Discord
  rejects the request. Check `is_forwardable()` yourself.

## Reactions

```python
await message.add_reaction('\N{THUMBS UP SIGN}')        # unicode emoji
await message.add_reaction(guild_emoji)                 # an Emoji, PartialEmoji, or 'name:id'
await message.add_reaction('🔥', boost=True)            # a super reaction (needs Nitro)
await message.remove_reaction('🔥', client.user)
await message.clear_reaction('🔥')                      # Manage Messages
await message.clear_reactions()                         # Manage Messages

for reaction in message.reactions:
    print(reaction.emoji, reaction.count, reaction.normal_count, reaction.burst_count, reaction.me, reaction.me_burst)
    async for user in reaction.users(limit=50):
        ...
```

- Adding a reaction needs Read Message History, plus Add Reactions if nobody
  has used that emoji on the message yet.
- **Super (burst) reactions** update `burst_count` and `me_burst`; normal ones
  `normal_count` and `me`; `count` is the sum. Before the November 2026 audit,
  a super reaction was counted as a normal one.
- Pace reactions at 3.0 s or slower. The reaction bucket is
  shared across channels.

## History, pins and search

### History

```python
async for msg in channel.history(limit=200):              # newest first
    ...

# Oldest first, after a date
since = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)
messages = [m async for m in channel.history(after=since, limit=None)]

# Around a message
context = [m async for m in channel.history(around=message, limit=21)]
```

`history(*, limit=100, before=None, after=None, around=None, oldest_first=None)`:

- `limit=None` fetches everything (pages of 100).
- Newest first by default, **oldest first when `after` is given** (unless you
  pass `oldest_first`).
- `before`, `after` and `around` take a message, an `Object`, or a `datetime`
  (naive datetimes are local time).
- `around` needs a limit of at most 101, and an odd limit is best: Discord
  returns the message plus up to `limit // 2` on each side.

### Pins

```python
pins = [m async for m in channel.pins()]             # newest first
oldest = [m async for m in channel.pins(oldest_first=True)]
```

- Pages of 50, newest first; `oldest_first=True` fetches the whole requested
  range before yielding (Discord only pages newest first).
- `await channel.pins()` (the old list-returning form) still works with a
  `DeprecationWarning`.
- `pin()` and `unpin()` need Manage Messages outside DMs.

### Search

User accounts can use Discord's search:

```python
async for msg in guild.search('deploy failed', authors=[me], has=['file'], limit=50):
    print(msg.jump_url)

async for msg in channel.search(limit=100, mentions=[client.user], after=last_week):
    ...
```

`search(content=..., *, contents=..., slop=..., limit=25, offset=0, before=..., after=..., authors=..., author_types=..., mentions=..., mention_everyone=..., pinned=..., has=..., embed_types=..., embed_providers=..., link_hostnames=..., attachment_filenames=..., attachment_extensions=..., application_command_id=..., application_command_name=..., oldest_first=..., most_relevant=False)`
on channels and DMs; `Guild.search` also takes `channels=[...]` and
`include_nsfw=`.

- `has` takes `'image'`, `'sound'`, `'video'`, `'file'`, `'sticker'`,
  `'embed'` and `'link'`, or the same with a leading `-` to exclude
  (`'-link'`).
- `author_types` takes `'user'`, `'bot'` and `'webhook'` (or `'-bot'` and so
  on).
- Results come in pages of 25.
- Discord's search can time out on its side and return fewer results than
  `limit` even when more exist; rerun later for completeness.
- `most_relevant=True` sorts by relevance (then `oldest_first` is ignored),
  and Discord caps how far you can page.
- `limit` or `offset` below 0 raises `ValueError`.
- Search is a heavy endpoint: pace it like sends.

## Read state

```python
await message.ack()                 # mark the channel read up to this message
await message.ack(manual=True)      # as if you clicked "mark as read"
await message.unack()               # mark it (and later messages) unread
await channel.ack()                 # mark a channel read
```

Acks sync to your other clients (`on_message_ack` fires there). See
[Settings](settings.md#read-states).

## Polls

```python
import datetime

poll = discord.Poll(question='Lunch?', duration=datetime.timedelta(hours=2), multiple=False)
poll.add_answer(text='Pizza').add_answer(text='Sushi', emoji='🍣')
msg = await channel.send(poll=poll)

# Later
await msg.end_poll()                         # or msg.poll.end()
msg = await channel.fetch_message(msg.id)    # re-fetch for the final counts
for answer in msg.poll.answers:
    print(answer.text, answer.vote_count, answer.self_voted, answer.victor)
    async for user in answer.voters(limit=100):
        ...
```

- **Duration is in hours.** Discord keeps only whole hours: a poll sent with
  1 h 20 min comes back as 1 h. The library sends what
  you give it and rounds a received poll's duration to whole hours (ported
  from Rapptz in v2.3.2). Pass whole hours.
- **Limits (Discord's):** a question of up to 300 characters, answers of up to
  55, at most 10 answers. Not checked by the library.
- **Answers can't be added after sending:** `add_answer` on a sent poll raises
  `ClientException`.
- **Ending a poll.** After `end_poll()`, Discord tallies the votes in the
  background. The message `end_poll()` returns isn't finalised yet; fetch it
  again a few seconds later for the final counts.
- **Voting.** There's no method to vote from the library; vote in the
  client.
- **Vote events.** `on_raw_poll_vote_add` / `_remove` fire for every vote;
  `on_poll_vote_add` / `_remove` only when the message and the voter are
  cached. On a poll with several answers allowed, they fire once per answer.
  `PollAnswer.self_voted` only changes for your own votes; before the November
  2026 audit, any vote overwrote it.
- **`Poll.copy()`** keeps `multiple` and `layout_type`.

## Embeds

You'll read embeds on other people's messages (`message.embeds`), and build
them for webhooks:

```python
embed = (
    discord.Embed(title='Build #42', description='All green', colour=discord.Colour.green(),
                  timestamp=discord.utils.utcnow(), url='https://ci.example/42')
    .set_author(name='CI', icon_url='https://ci.example/icon.png')
    .add_field(name='Duration', value='3m12s')
    .add_field(name='Tests', value='831 passed')
    .set_footer(text='master')
)
await webhook.send(embed=embed)

data = embed.to_dict()              # and back: discord.Embed.from_dict(data)
```

- Discord's limits: title 256 characters, description 4,096, 25 fields (name
  256, value 1,024), footer 2,048, author name 256, 6,000 in total
  (`len(embed)`), 10 embeds per message. The library doesn't check them,
  except the 10-embed limit on webhooks; Discord rejects the request.
- `Embed.Empty` no longer exists; use `None`.
- Setters return the embed, so they chain.
- Naive `timestamp` datetimes are treated as UTC in `to_dict()`.

## What a `Message` holds

| Attribute | Notes |
|---|---|
| `content`, `clean_content` | `clean_content` replaces mentions with names. |
| `author` | A `Member` in guilds when possible, else a `User`. |
| `attachments`, `embeds`, `stickers`, `reactions`, `poll` | |
| `reference`, `message_snapshots` | Replies and forwards. |
| `mentions`, `role_mentions`, `channel_mentions`, `mention_everyone` | |
| `flags` | `MessageFlags`: `suppress_notifications` (sent with `silent=True`), `suppress_embeds`, `ephemeral` and more. |
| `components` | Buttons, selects and layout components from bots. See [Interactions and components](interactions-and-components.md). |
| `interaction_metadata` | For a bot's message: the command, button or modal that produced it and who used it. *New in 2.3.2.* Discord sends it instead of the deprecated `interaction` field, which still works. |
| `call` | For a call message in a DM or group: a `CallMessage` with `participants`, `ended_timestamp`, `initiator`, `call_ended` (Rapptz's `is_ended()`) and `duration`. |
| `type` | `MessageType`: default, reply, joins, pins, boosts, thread starters, poll results... |
| `pinned`, `pinned_at` | |
| `jump_url` | A link to the message. |
| `created_at`, `edited_at` | Aware datetimes in UTC. |

A live scan of 621 recent messages in the test accounts' servers found none
carrying interaction metadata, so only unit tests cover parsing it.

## Gotchas

- **`edit()` returns a new object.** `msg = await msg.edit(...)`.
- **`message.author` may be a `User`** in a guild if the member isn't cached
  (webhooks, users who left, eviction). Use `isinstance(author, discord.Member)`
  before reading roles.
- **The message cache is small** (1,000 by default, oldest dropped first). Old
  messages have no `on_message_edit`; use the raw events or raise
  `max_messages`.
- **`delete_after` and `delete(delay=...)` run in the background:** if the
  client closes first, the message stays.
- **Mentions in content aren't validated.** `<@123>` for a user who isn't in
  the guild sends as plain text.
