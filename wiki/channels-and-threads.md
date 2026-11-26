# Channels and threads

Every kind of channel a user account deals with: guild channels and their
permissions, DMs, group DMs and message requests, threads and forums,
invites, webhooks and cloning. Messages themselves are on [Messages](messages-and-channels.md).

## Channel types

| Class | `type` | What it is |
|---|---|---|
| `TextChannel` | `text`, `news` | A text channel. `is_news()` for announcement channels. |
| `VoiceChannel` | `voice` | Voice, with its own text chat (you can `send()` to it). |
| `StageChannel` | `stage_voice` | A stage: `speakers`, `listeners`, `moderators`, `create_instance()`. |
| `CategoryChannel` | `category` | Groups channels; `text_channels`, `voice_channels`, `create_text_channel()`... |
| `ForumChannel` | `forum`, `media` | Forums and media channels (`is_media()`); posts are threads. There's no separate `MediaChannel` class. |
| `DirectoryChannel` | `directory` | Student hub directories. |
| `Thread` | `public_thread`, `private_thread`, `news_thread` | Threads and forum posts. |
| `DMChannel` | `private` | A one-to-one DM. |
| `GroupChannel` | `group` | A group DM (up to 10 people). |
| `PartialMessageable` | — | Only an ID, for sending without a lookup. |

Getting channels:

```python
channel = client.get_channel(channel_id)            # any cached channel or thread, guild or private
channel = await client.fetch_channel(channel_id)    # REST
channel = guild.get_channel_or_thread(channel_id)   # within a guild
dm = client.get_partial_messageable(channel_id)     # just send to it

for c in guild.text_channels: ...                   # also voice_channels, categories, forums, threads
for c in client.private_channels: ...               # DMs and groups
```

## Guild channels

### Creating, editing, deleting

```python
category = await guild.create_category('Projects')
channel = await guild.create_text_channel('alpha', category=category, topic='Project Alpha', slowmode_delay=5)
voice = await category.create_voice_channel('Alpha voice', user_limit=5, bitrate=64000)

channel = await channel.edit(name='alpha-archive', nsfw=False, position=0)   # returns the updated channel
await channel.delete(reason='project done')
```

All of these need Manage Channels in the guild (or the category).

### Permission overwrites

```python
# Allow one member, deny everyone else
await channel.set_permissions(guild.default_role, read_messages=False)
await channel.set_permissions(member, read_messages=True, send_messages=True)

# With an overwrite object
overwrite = discord.PermissionOverwrite(send_messages=False, add_reactions=False)
await channel.set_permissions(role, overwrite=overwrite)

# Remove an overwrite
await channel.set_permissions(role, overwrite=None)

# Replace them all at once
await channel.edit(overwrites={
    guild.default_role: discord.PermissionOverwrite(view_channel=False),
    member: discord.PermissionOverwrite(view_channel=True),
})
```

- In a `PermissionOverwrite`, `True` allows, `False` denies, and `None`
  inherits (no override). `1` and `0` are accepted as `True` and `False`;
  anything else raises `TypeError`.
- The target must be a `Member` or `Role`; anything else raises `ValueError`.
- Keyword permissions together with `overwrite=` raise `TypeError`.
- `set_permissions` returns when the request succeeds, before the gateway
  update arrives, so `channel.overwrites` can lag for a moment.

**Checking permissions:**

```python
perms = channel.permissions_for(member)     # or channel.permissions_for(guild.me)
if perms.send_messages and perms.attach_files:
    ...
```

The order Discord uses, and the library reproduces: the guild owner has
everything; otherwise `@everyone`'s permissions, plus each role's;
Administrator means everything; then the channel's `@everyone` overwrite,
role overwrites (denies then allows), and finally the member's own overwrite.
A timed-out member keeps only View Channel and Read Message History.

### Cloning

```python
copy = await channel.clone(name='alpha-2', category=other_category, reason='template')
```

Creates a channel with the same settings (topic, NSFW flag, slowmode,
overwrites, bitrate, user limit...), including the fields Rapptz fixed in
v2.3.2. Voice and stage channels take `category=`. A category's `clone()` only
copies its name, overwrites and NSFW flag.

A clone keeps the original's topic, NSFW flag, slowmode, category,
permission overwrites, bitrate and user limit.

### Invites

```python
invite = await channel.create_invite(max_age=3600, max_uses=5, unique=True)
print(invite.url)

invites = await channel.invites()        # needs Manage Channels
invite = await client.fetch_invite('abcdef')
guild = await client.accept_invite(invite)   # join the guild
```

- `max_age=0` (the default) never expires; `max_uses=0` is unlimited.
- `GroupChannel.create_invite()` defaults to 24 hours (`max_age=86400`) and
  doesn't support `0`.
- Joining guilds from invites is an action Discord watches for spam; expect
  captchas on new accounts.

## DMs

```python
dm = await user.create_dm()           # or await client.create_dm(user)
await dm.send('hi')
await user.send('hi')                 # opens the DM for you

print(dm.recipient)                   # the other person

await dm.close()                      # "close" the DM in the UI; history comes back if reopened
```

- Opening a DM with someone you share no guild or friendship with can fail
  (`Forbidden`, code 50007 "Cannot send messages to this user") or ask for a
  captcha.
- **Message requests:** a DM from a stranger can arrive as a request.
  `dm.accept()` and `dm.decline()` handle it.
- `DMChannel.add_recipients(*users)` turns a DM into a group DM.

## Group DMs

```python
group = await client.create_group(friend_a, friend_b)
await group.add_recipients(friend_c)
await group.remove_recipients(friend_c)          # you must be the owner
await group.edit(name='Weekend', icon=png_bytes, owner=friend_a)
await group.leave()                              # deletes the group if you're the last member
await group.leave(silent=True)                   # without the "left the group" message
```

- Groups hold up to 10 people, and you can only add your friends (Discord's
  rules; the library doesn't check).
- `edit(name=None)` or `icon=None` clears them.
- `on_group_join(channel, user)` and `on_group_remove(channel, user)` fire as
  people come and go.

## Threads

### Creating

```python
# From a message: a public thread
thread = await message.create_thread(name='Discussion', auto_archive_duration=1440)

# Without a message: a PRIVATE thread by default
thread = await channel.create_thread(name='mods only')

# A public thread without a message
thread = await channel.create_thread(name='open', type=discord.ChannelType.public_thread)
```

- **Without `message`, `create_thread` makes a private thread** unless you pass
  `type=discord.ChannelType.public_thread`. `invitable=False` stops
  non-moderators adding people to a private thread.
- With `message`, `type` is ignored (it's always public).
- `auto_archive_duration` must be 60, 1440, 4320 or 10080 minutes; unset uses
  the channel's default.

### Managing

```python
await thread.edit(name='renamed', archived=True, locked=True, slowmode_delay=10)
await thread.join()
await thread.add_user(member)
await thread.remove_user(member)
await thread.leave()
await thread.delete()

members = await thread.fetch_members()
```

- A thread must be unarchived to be edited (except unarchiving it).
- Joining a private thread needs Manage Threads (or an invitation).
- `fetch_members()` asks over the gateway and waits up to 15 seconds for the
  answer; on timeout it raises `InvalidData`.

### Archived threads and the thread cache

```python
async for thread in channel.archived_threads(limit=None):
    ...
async for thread in channel.archived_threads(private=True, joined=True):
    ...
```

- `joined=True` only works with `private=True` (`ValueError` otherwise).
- Active threads come from the gateway and are cached (bounded per guild and
  in total, see [Caching and memory](caching-and-memory.md)). Threads you're a
  member of are never evicted. An evicted thread comes back with
  `await guild.fetch_channel(thread_id)`.
- Without `guild_subscriptions`, `guild.threads` holds only the threads you
  joined.

### Thread events

`on_thread_create`, `on_thread_join` (for you), `on_thread_update`,
`on_thread_remove`, `on_thread_delete`, `on_thread_member_join`,
`on_thread_member_remove`, and the raw `on_raw_thread_update`,
`on_raw_thread_delete` and `on_raw_thread_member_remove` (which fire for
uncached threads too). See [Events](events.md).

## Forums

```python
forum: discord.ForumChannel = guild.get_channel(forum_id)

# A post is a thread plus its first message
tag = discord.utils.get(forum.available_tags, name='Question')
post = await forum.create_thread(
    name='How do I...?',
    content='Details here',
    files=[discord.File('screenshot.png')],
    applied_tags=[tag],
)
print(post.thread.jump_url, post.message.id)

await post.thread.add_tags(another_tag)
await post.thread.edit(archived=True)

new_tag = await forum.create_tag(name='Solved', emoji='\N{WHITE HEAVY CHECK MARK}', moderated=True)
```

- `create_thread` returns a `ThreadWithMessage` (`thread`, `message`).
- Forum posts are always public threads.
- `applied_tags` takes `ForumTag` objects; Discord allows up to 5.
- `create_tag` takes the new tag's ID from Discord's answer: the tag the
  forum didn't have before, preferring the one with your name (so a tag
  someone else created at the same moment isn't taken for yours). If it
  can't find it, `new_tag.id` stays `0` and a warning is logged; re-read
  `forum.available_tags`. (Before v2.3.4 it assumed the last tag in the
  answer, and stayed at 0 silently.)

## Webhooks

Webhooks are the one way a user account can post **embeds and components**:

```python
hook = await channel.create_webhook(name='Reports')          # needs Manage Webhooks
await hook.send('hi', username='Reporter', avatar_url='https://example.com/a.png')
msg = await hook.send(embed=discord.Embed(title='Build passed'), wait=True)  # wait=True returns the message
await hook.edit_message(msg.id, content='edited')
await hook.delete_message(msg.id)
await hook.delete()

# From a URL
partial = discord.Webhook.from_url(url, client=client)   # uses the client's session and state
await partial.send('via url')

# In a forum or thread
await partial.send('new post', thread_name='Daily report')
await partial.send('reply', thread=discord.Object(id=thread_id))
```

- `send()` on a webhook takes `embed`/`embeds`, `components`, `thread`,
  `thread_name`, `applied_tags`, `poll`, `username`, `avatar_url` and `wait`.
  `thread` and `thread_name` together raise `TypeError`.
- `wait=False` (the default) returns `None`.
- `Webhook.from_url` needs `session=` (an `aiohttp.ClientSession`) or
  `client=` (a logged-in client); an invalid URL raises `ValueError`.
- `SyncWebhook` is the blocking version, for scripts without an event loop.
  It uses the `requests` package, which isn't one of this library's
  dependencies: `pip install requests` first, or `SyncWebhook.from_url` fails
  with `ModuleNotFoundError`.
- **Rate limits:** webhook sends retry up to 5 times on 429s; a 429 from
  Cloudflare (no `Via` header) raises at once.
- **A webhook URL is a credential.** Anyone with it can post. Tokens are
  masked in every logged URL.

The webhook adapter uses `aiohttp`. Webhooks bound to a client share its
aiohttp session, which keeps cookies. That's why the Rapptz change that turns
cookies off (`DummyCookieJar`) wasn't ported: without cookies, clients get 403
responses from Cloudflare.

## Followed (news) channels

```python
await news_channel.follow(destination=my_channel)   # crosspost its messages into my_channel
await message.publish()                              # publish a message in a news channel you manage
```

## Gotchas

- **`channel.edit()` returns a new object.** Use the return value.
- **`get_channel` doesn't see uncached threads.** Use
  `guild.fetch_channel(id)` for those.
- **Private thread by default.** `channel.create_thread(name=...)` without a
  message creates a private thread.
- **Group invites expire.** 24 hours by default, unlike guild invites.
- **Overwrites lag briefly** after `set_permissions` until the gateway update
  arrives.
