# Guilds, members and roles

Working with guilds (servers) from a user account: what's cached and what
must be fetched, joining and leaving, members and editing them (yourself
versus others), roles, permissions, bans, audit logs, emojis, stickers,
scheduled events and templates. Member *lists* (loading every member) have
their own page: [Member lists](member-lists.md).

## Getting guilds

```python
guild = client.get_guild(guild_id)                 # cache: every guild you're in, after READY
guild = await client.fetch_guild(guild_id)         # REST: no channels, no members
print(len(client.guilds))
```

- `client.guilds` and `get_guild` hold every guild you're in, with channels,
  roles, emojis and the cached members.
- `fetch_guild` returns a guild **without** `channels` or `members`; it's for
  guilds you aren't in (previews) or for fresh counts (`with_counts=True`
  fills `approximate_member_count` and `approximate_presence_count`).
- `guild.member_count` is Discord's count; `len(guild.members)` is only the
  cached members. Both counts can lag behind joins and leaves.

### Joining and leaving

```python
invite = await client.fetch_invite('abcdef')
guild = await client.accept_invite(invite)        # join (watch for captchas)
await guild.leave()                               # the owner can't leave; use guild.delete()
```

`on_guild_join(guild)` fires when you join; `on_guild_remove(guild)` when you
leave, are kicked or banned, or the guild is deleted.

### Useful guild attributes

| Attribute | Notes |
|---|---|
| `me` | Your own `Member` in the guild. Always cached. |
| `owner_id`, `owner` | `owner` may be `None` if not cached. |
| `roles`, `default_role` | `default_role` is `@everyone`. |
| `channels`, `text_channels`, `voice_channels`, `categories`, `forums`, `threads` | |
| `emojis`, `stickers`, `soundboard_sounds` | `soundboard_sounds` is empty on user accounts until `client.request_soundboard_sounds([guild])` (see [Soundboard and voice](soundboard-and-voice.md)). |
| `premium_tier`, `premium_subscription_count` | Boosts. |
| `features` | `'COMMUNITY'`, `'DISCOVERABLE'`... |
| `large` | `member_count >= 75000` here (Discord's own "large" threshold for subscriptions). |
| `chunked` | Whether every member is cached. |
| `vanity_url` | If the guild has a vanity code; `await guild.vanity_invite()` returns the `Invite` or `None`. |

```python
await guild.ack()      # mark the whole guild read
```

## Members

```python
member = guild.get_member(user_id) or await guild.fetch_member(user_id)

print(member.display_name, member.nick, member.global_name, member.name)
print([r.name for r in member.roles], member.top_role, member.guild_permissions)
print(member.joined_at, member.premium_since, member.timed_out_until, member.voice)
```

- `member.roles` starts with `@everyone`, sorted by position.
- `member.voice` is their `VoiceState` (channel, mute, deaf, streaming) or
  `None`.
- A `Member` isn't a `User` subclass, but `member == user` works when the IDs
  match, and the user's attributes (`name`, `avatar`, `bot`...) are available
  on the member.

### Editing members

```python
await member.edit(nick='Newbie', roles=[role_a, role_b], reason='onboarding')
await member.add_roles(role_c)
await member.remove_roles(role_a, reason='expired')
await member.timeout(datetime.timedelta(hours=1), reason='spam')   # or a datetime; None removes
await member.move_to(voice_channel)                                # None disconnects them
await member.kick(reason='...')
await member.ban(reason='...', delete_message_seconds=3600)
```

| Action | Permission |
|---|---|
| `nick` (someone else) | Manage Nicknames |
| `roles`, `add_roles`, `remove_roles` | Manage Roles, and the roles must be below your top role |
| `timeout` | Moderate Members (up to 28 days, Discord's limit) |
| `move_to`, `mute`, `deafen` | Move Members / Mute Members / Deafen Members |
| `kick` / `ban` | Kick Members / Ban Members |

- `add_roles` and `remove_roles` with `atomic=True` (the default) send one
  request per role, which is safe against concurrent changes; `atomic=False`
  sends one `edit(roles=...)`.
- `timeout()` takes a `timedelta` (from now) or an aware `datetime`; a naive
  datetime raises `TypeError`.

### Editing your own member

Your own member has extra, per-guild profile fields:

```python
await guild.me.edit(
    nick='Me here',
    avatar=png_bytes,             # a per-guild avatar (Nitro)
    banner=png_bytes,             # a per-guild banner (Nitro)
    bio='Hello',
    pronouns='they/them',
)
```

> **Gotcha: self-only fields on other members are dropped.** `banner`, `bio`,
> `avatar`, `pronouns`, the display-name font, effect and colours, the avatar
> decoration and the nameplate only apply to your own member. Passed for
> someone else, they're ignored with the warning
> `Member.edit(bio, ...) only applies to your own member; ignored for member ID ...`.

Changing your own nickname uses the "modify current member" request only.
Before the November 2026 audit it sent the nickname twice, and the second
request failed with `Forbidden` on accounts without Manage Nicknames.

### Voice states

`Member.fetch_voice()` asks Discord for a member's voice state. Discord only
serves that endpoint to bot accounts, so on a user account the library
doesn't make the request: it returns the cached `member.voice`, or raises
`ClientException` with `fallback_to_cache=False`.

### Mutual guilds and profiles

There's no `Member.mutual_guilds`. Use the profile endpoints:

```python
profile = await guild.fetch_member_profile(user_id, with_mutual_guilds=True)
profile = await client.fetch_user_profile(user_id, with_mutual_guilds=True, with_mutual_friends=True)
```

See [Users and relationships](users-and-relationships.md).

## Roles

```python
role = await guild.create_role(
    name='Helpers',
    permissions=discord.Permissions(manage_messages=True, kick_members=True),
    colour=discord.Colour.blue(),
    hoist=True,
    mentionable=True,
)
role = await role.edit(name='Helpers+', secondary_colour=discord.Colour.purple())   # a gradient (enhanced role styles)
await role.edit(position=3)
await role.move(above=other_role)
await role.delete()

roles = await guild.fetch_roles()
counts = await guild.role_member_counts()      # {role_id: count}
```

- **Colours:** `colour` (or `color`), plus `secondary_colour` and
  `tertiary_colour` for gradients and holographic roles. `None` clears a
  secondary or tertiary colour, and `Colour(0)` (black) is a valid value.
  Before the November 2026 audit, `None` and `0` were ignored.
- **Icons:** `display_icon` (bytes or a unicode emoji), or `icon` and
  `unicode_emoji` separately. Setting `display_icon` together with `icon` or
  `unicode_emoji` raises `ValueError`. A custom emoji as `emoji` raises
  `ValueError('emoji only supports unicode emojis')`.
- **Positions:** Discord changes positions through its own endpoint, so
  `edit(position=...)` sends two requests: the move, then the other fields.
  The other arguments are checked first, so invalid ones raise before the
  role moves (before v2.3.4 the role was moved first). If the second request
  itself fails, the position has already changed. `move(beginning=..., end=..., above=..., below=..., offset=...)`
  takes exactly one anchor. The default role can't be moved, and nothing can
  go to position 0.
- `role.add_members(*members)` and `role.remove_members(*members)` change many
  members at once (Discord allows up to 30 per call).
- `role.fetch_members()` returns up to 100 of the role's first members.

## Permissions

```python
perms = member.guild_permissions                 # guild-wide
perms = channel.permissions_for(member)          # in a channel, with overwrites
perms = channel.permissions_for(guild.me)        # what YOU can do there

if perms.manage_messages: ...
print([name for name, value in perms if value])

p = discord.Permissions(send_messages=True, embed_links=True)
p.update(attach_files=True)
p |= discord.Permissions.voice()
```

- Presets: `Permissions.all()`, `none()`, `general()`, `text()`, `voice()`,
  `stage()`, `membership()`, `elevated()`, `advanced()`, `events()`, `apps()`,
  `all_channel()`.
- Aliases: `read_messages` is `view_channel`, `manage_roles` is
  `manage_permissions`, `external_emojis` is `use_external_emojis`,
  `send_polls` is `create_polls`, `manage_expressions` covers emojis and
  stickers.
- `Permissions.update(**kwargs)` ignores unknown names silently; check your
  spelling.
- `permissions_for` follows Discord's order (owner, roles, Administrator,
  `@everyone` overwrite, role overwrites, member overwrite), and a timed-out
  member keeps only View Channel and Read Message History. Passed a `Role`,
  member overwrites aren't computed.

## Bans

```python
await guild.ban(user, reason='spam', delete_message_seconds=86400)  # also works with discord.Object(id=...)
await guild.unban(user)
ban = await guild.fetch_ban(user)                                   # NotFound if not banned
async for entry in guild.bans(limit=None):
    print(entry.user, entry.reason)

result = await guild.bulk_ban([user_a, user_b], delete_message_seconds=0)
```

- `delete_message_seconds` defaults to one day; `delete_message_days` still
  works with a `DeprecationWarning`, and mixing the two raises `TypeError`.
- `bans()` takes `before` or `after`, not both.
- `bulk_ban` needs Ban Members and Manage Guild; Discord allows up to 200
  users per call.

## Audit logs

```python
async for entry in guild.audit_logs(limit=50, action=discord.AuditLogAction.member_role_update):
    print(entry.created_at, entry.user, entry.target, entry.reason)
    print(entry.before.roles, '->', entry.after.roles)

# Oldest first, within a range
async for entry in guild.audit_logs(after=since, oldest_first=True, limit=None):
    ...
```

- Needs View Audit Log.
- `entry.before` and `entry.after` hold the changed fields; a field Discord
  didn't send an old or new value for reads as `None`.
- `entry.target` is the changed object when it's cached, else a
  `discord.Object` with the ID.
- **`oldest_first=True`** walks the range newest to oldest (Discord's order) and
  buffers it before yielding the first entry, so a small `limit` still fetches
  the whole range first. Pass `after` to bound it.
- Unknown action types come through as raw values instead of failing.
- `on_audit_log_entry_create(entry)` delivers new entries live.

## Emojis and stickers

```python
emoji = await guild.create_custom_emoji(name='party', image=png_bytes, roles=[vip_role])
await emoji.edit(name='party2')
await emoji.delete()

sticker = await guild.create_sticker(name='wave', description='hi', emoji='👋', file=discord.File('wave.png'))
```

- Needs Manage Expressions.
- Slots depend on the boost tier (50 static and 50 animated at tier 0).
- `emoji.is_usable()` is `False` for a role-locked emoji while your own member
  isn't cached.

## Scheduled events

```python
event = await guild.create_scheduled_event(
    name='Game night',
    start_time=discord.utils.utcnow() + datetime.timedelta(days=1),
    entity_type=discord.EntityType.voice,
    channel=voice_channel,
    privacy_level=discord.PrivacyLevel.guild_only,
)
await event.start()
await event.end()
async for user in event.users():
    ...
```

- `start_time` and `end_time` must be timezone-aware.
- `external` events need `location` and `end_time`; voice and stage events need
  `channel` and reject `location`.

## Templates, widgets and the rest

| Feature | Methods |
|---|---|
| Templates | `guild.templates()`, `guild.create_template(name=...)`, `client.fetch_template(code)`, `template.create_guild(name)` |
| Widget | `guild.widget()` (the widget must be enabled), `guild.edit_widget(...)` |
| Welcome screen, onboarding | `guild.welcome_screen()`, `guild.onboarding()` |
| Integrations | `guild.integrations()` |
| AutoMod | `guild.automod_rules()`, `guild.create_automod_rule(...)` |
| Stage instances | `stage_channel.create_instance(topic=...)` |

## Gotchas

- **Most reads are cache-only.** `get_guild`, `guild.members`,
  `guild.threads`, `member.voice` reflect what the gateway told the client;
  `fetch_*` calls hit the API and count against rate limits.
- **`fetch_guild` has no channels or members.**
- **`Role.edit(position=...)` is two requests;** a failure in the second leaves the role moved.
- **Self-only member fields are dropped for others** (with a warning).
- **Moderation actions are visible.** Everything you do appears in the audit
  log with your account's name.
