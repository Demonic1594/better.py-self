# Settings

Your account's settings as the official clients store them: the protobuf
`UserSettings`, frecency settings, per-guild and per-channel notification
settings, the deprecated legacy settings, read states, and how
`discord_protos` versions affect them. The code is
[`discord/settings.py`](../discord/settings.py).

## User settings (protobuf)

Modern Discord clients store settings as a protobuf message, synced across
your devices. The library parses it with the `discord_protos` package.

```python
settings = client.settings                       # cached, from READY; None before
settings = await client.fetch_settings()         # fresh from the API

print(settings.status, settings.custom_activity, settings.locale, settings.theme)
print(settings.developer_mode, settings.afk_timeout, settings.guild_folders)

settings = await settings.edit(theme=discord.Theme.dark, developer_mode=True)
```

- `edit()` returns a **new** `UserSettings`; the object you called it on isn't
  changed. `client.settings` is updated when Discord confirms the change
  (`on_settings_update(before, after)` fires on every session).
- `edit()` with no arguments raises `TypeError`.

Commonly used settings:

| Setting | Type | Notes |
|---|---|---|
| `status` | `Status` | Your saved status (what `change_presence(edit_settings=True)` writes). |
| `custom_activity` | `CustomActivity` or `None` | Your custom status. |
| `locale` | `Locale` | The client language. |
| `theme` | `Theme` | `Theme.light` or `Theme.dark`. |
| `client_theme` | `(primary, gradient_preset_id, angle)` | Nitro client themes. See [below](#client-themes-and-discord_protos-versions). |
| `guild_folders`, `guild_positions` | lists | Server folders and order in the sidebar. |
| `restricted_guilds` | guilds | Guilds whose members can't DM you. |
| `activity_restricted_guilds`, `message_request_restricted_guilds` | guilds | Per-guild activity status and message-request privacy. |
| `developer_mode` | `bool` | Shows "Copy ID" in the client. |
| `afk_timeout` | seconds | When the client counts you as idle. |
| `dismissed_contents` | list of ints | Dismissed tips and banners. |
| `inbox_tab`, `drops`, `home_auto_navigation` | | |

The full list is in [`docs/api.rst`](../docs/api.rst) under `UserSettings`.

### How edits work: whole sections and versions

The settings message has sections (appearance, privacy, status...). An edit
sends each section you touch **as a whole**, built from the object you called
`edit()` on. So:

- **Edit from a fresh object.** Editing from an old `UserSettings` resets the
  other fields of the touched section to what that old object had, undoing
  changes made in the meantime (on another device, say).
- **`require_version=True`** makes the edit fail if the settings changed since
  your object was fetched (it sends the object's `data_version`); pass an int
  to require a specific version.

```python
settings = await client.fetch_settings()
settings = await settings.edit(require_version=True, developer_mode=True)
```

**`MISSING` resets a setting** to Discord's default (the key is removed from
the payload), while `None` means "none" for settings that can be empty:

```python
from discord.utils import MISSING

await settings.edit(custom_activity=None)       # clear the custom status
await settings.edit(client_theme=MISSING)       # reset the client theme to the default
```

## Client themes and `discord_protos` versions

`client_theme` is `(primary_colour, gradient_preset_id, gradient_angle)`:

```python
primary, preset, angle = client.settings.client_theme
await client.settings.edit(client_theme=(0x5865F2, 3, 45.0))
await client.settings.edit(client_theme=None)      # no client theme
```

Discord changed how themes are stored: recent `discord_protos` versions no
longer have the `primary_color` and `background_gradient_angle` fields, and
keep colours in `custom_user_theme_settings` instead. The library reads the
schema at runtime and works with **both**:

- **Reading** takes the old fields when the installed schema has them, the new
  ones otherwise. (Before v2.3.3, reading a set theme raised `AttributeError`
  with recent `discord_protos`.)
- **Writing** sets what the schema supports and logs, for a value it can't
  store: `client_theme: the settings schema (discord_protos) has no <field> field any more; that value was not sent.`
  (Before, the whole edit failed with a protobuf `ParseError`.)
- **A colour that isn't hex** in the new storage reads as `0`, with a warning
  once: `A custom theme color is not a hex color (...); client_theme reports 0 for it.`

`python -m discord -v` shows the installed `discord-protos` version.

## Frecency settings

Frecency settings (recently and frequently used emojis, GIFs, stickers,
application commands...) are a separate protobuf:

```python
frecency = client.frecency_settings                  # cached, from READY
frecency = await client.fetch_frecency_settings()    # fresh from the API
```

They follow the same rules (`require_version`, whole sections, `MISSING`).
`on_frecency_settings_update(before, after)` fires on changes.

## Notification settings

Per guild and per channel:

```python
gs = guild.notification_settings
print(gs.level, gs.muted, gs.suppress_everyone, gs.suppress_roles, gs.mobile_push)

await gs.edit(level=discord.NotificationLevel.only_mentions, suppress_everyone=True)
await gs.edit(muted_until=True)                                     # mute indefinitely
await gs.edit(muted_until=discord.utils.utcnow() + datetime.timedelta(hours=8))
await gs.edit(muted_until=False)                                    # unmute

cs = channel.notification_settings
await cs.edit(muted_until=True, level=discord.NotificationLevel.nothing)
```

`GuildSettings.edit` takes `muted_until`, `level`, `suppress_everyone`,
`suppress_roles`, `mobile_push`, `hide_muted_channels`,
`mute_scheduled_events` and `notify_highlights`; `ChannelSettings.edit`
takes `muted_until`, `collapsed` and `level`. `on_guild_settings_update`
fires when they change.

Tracking (consent) settings: `await client.fetch_tracking_settings()`
returns a `TrackingSettings` with `edit(**kwargs)`.

## Legacy settings (deprecated)

`client.edit_legacy_settings(**kwargs)` and `LegacyUserSettings` use the old
JSON settings endpoint. They still work, with a `DeprecationWarning`
(`edit_legacy_settings is deprecated, use UserSettings.edit instead.`). Use
`UserSettings.edit`. (Earlier versions' warning named `Client.edit_settings`,
which doesn't exist.)

## Read states

A read state is "how far you've read" in a channel, or for a feature (guild
events, onboarding, message requests). Acknowledging (acking) moves it.

```python
await message.ack()                       # read up to this message
await message.ack(manual=True)            # as an explicit "mark as read"
await message.unack()                     # mark this message and later ones unread
await channel.ack()
await guild.ack()                         # the whole guild

for rs in client.read_states:             # every read state the client knows
    print(rs.type, rs.id, rs.last_acked_id, rs.badge_count)
```

- `ReadStateType`: `channel`, `scheduled_events`, `notification_center`,
  `guild_home`, `onboarding`, `message_requests`.
- `ReadState.ack(entity_id, *, manual=False, mention_count=None, last_viewed=...)`
  is the low-level form. `manual`, `mention_count` and `last_viewed` only apply
  to channel read states; for others they raise `ValueError`.
- Acks sync to your other clients (`on_message_ack` there). Acking everything
  constantly isn't what a person does; ack what you actually "read".
- Read states for a guild are dropped when you leave it.

## Gotchas

- **`edit()` returns a new object.** Keep using the return value, or re-read
  `client.settings` after `on_settings_update`.
- **Stale objects reset fields.** Fetch before editing, or use
  `require_version=True`.
- **Settings sync to every device.** A theme or status change shows on your
  phone too.
- **`discord_protos` updates can drop fields.** The library copes for the
  client theme; for other fields, a value the schema no longer has is
  ignored. Pin `discord_protos` if you depend on an exact field set.
