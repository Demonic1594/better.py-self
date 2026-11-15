# Presence and activities

Your status and activities (playing, listening, custom status, rich
presence), how they relate to your settings and your other clients, and
reading other people's presences. The code is `Client.change_presence` in
[`discord/client.py`](../discord/client.py) and
[`discord/activity.py`](../discord/activity.py).

## Setting your presence

At login, with options:

```python
client = discord.Client(status=discord.Status.idle, activity=discord.Game('chess'))
```

Later, from `on_ready` on:

```python
await client.change_presence(status=discord.Status.dnd)
await client.change_presence(activity=discord.Game('chess'))
await client.change_presence(activities=[discord.Game('chess'), discord.CustomActivity('busy', emoji='📚')])
await client.change_presence(activity=None)          # clear all activities (the status stays)
await client.change_presence(status=discord.Status.idle, edit_settings=False)
```

`change_presence(*, activity=..., activities=..., status=..., afk=..., idle_since=..., edit_settings=True)`:

- **Anything you don't pass keeps its current value** (since 2.1).
- `activity` and `activities` together raise `TypeError`; more than one custom
  status raises `ValueError`.
- `status=Status.offline` is sent as **invisible**.
- `afk=True` tells Discord you're away, so your phone gets the push
  notifications.
- `idle_since` is a `datetime`, or `None` for "not idle".
- **`edit_settings=True` (the default) saves the status and custom status to
  your account settings**, so every official client (desktop, phone, browser)
  switches too. Pass `edit_settings=False` to change only this session, for
  example for automatic idle.
- Before the gateway is connected (in `setup_hook`, say) it raises
  `ClientException('change_presence() needs a connected gateway. ...')`.
  Use the `status=` and `activity=` options for the initial presence.
- Discord allows 5 presence updates per 20 seconds per connection; the
  library doesn't throttle them for you.

> **Gotcha: `on_ready` runs more than once.** Calling `change_presence` in
> `on_ready` is common and fine, but it runs again after every new session.
> Make it idempotent, or use the constructor options.

### Status values

| `Status` | Shown as |
|---|---|
| `online` | Online (green). |
| `idle` | Idle (yellow moon). |
| `dnd` (`do_not_disturb`) | Do Not Disturb (red); no notifications in the official clients. |
| `invisible` | Offline to everyone else, but connected. |
| `offline` | Sent as `invisible`. |

### Following your other clients: `sync_presence`

With `sync_presence=True` (the default), the library behaves like an
official client: when you change your status or custom status in the app, the
library's session follows, and the presence you pass at login is saved to
your settings. With `False`, the library's session keeps its own status. It
doesn't affect `change_presence`, which is controlled by `edit_settings`.

## Activities

| Class | `ActivityType` | Example |
|---|---|---|
| `Game` | `playing` | `discord.Game('chess')` |
| `Streaming` | `streaming` | `discord.Streaming(name='Live', url='https://twitch.tv/...')` |
| `Activity` | any | `discord.Activity(type=discord.ActivityType.watching, name='the logs')` |
| `CustomActivity` | `custom` | `discord.CustomActivity('In a meeting', emoji='📅')` |
| `Spotify` | `listening` | See [below](#spotify). |
| `HangActivity` | `hang` | "Hanging out" statuses (`HangStatusType`). |

`ActivityType` values: `unknown` (-1), `playing` (0), `streaming` (1),
`listening` (2), `watching` (3), `custom` (4), `competing` (5), `hang` (6).

### Custom status

```python
await client.change_presence(activity=discord.CustomActivity(
    'Back at 3pm',
    emoji='🕒',
    expires_at=discord.utils.utcnow() + datetime.timedelta(hours=2),
))
```

- The text is `name` (or `state` if given), the emoji a unicode string or a
  `PartialEmoji`.
- With `edit_settings=True` it's saved to your settings, where the official
  clients show it. It can also be set through settings directly:
  `await client.settings.edit(custom_activity=discord.CustomActivity('...'))`.

### Rich presence

```python
activity = discord.Activity(
    type=discord.ActivityType.playing,
    name='My Game',
    application_id=123456789012345678,          # an application you own, for the assets
    details='Ranked match',
    state='In queue',
    timestamps={'start': int(time.time() * 1000)},
    assets={'large_image': 'logo', 'large_text': 'My Game'},
    buttons=['Website'],
)
await client.change_presence(activity=activity)
```

[`examples/rich_presence.py`](../examples/rich_presence.py) is a complete
version. Images (`assets`) come from the application's art assets; buttons
are labels (their URLs come from the metadata your application provides).

### Spotify

```python
listening = discord.Spotify(
    title='Song',
    artists=['Artist'],
    album='Album',
    track_id='4cOdK2wGLETKBW3PvgPWqT',
    duration=datetime.timedelta(minutes=3, seconds=30),
    party_owner_id=client.user.id,
)
await client.change_presence(activity=listening)
```

Reading someone's Spotify activity:

```python
@client.event
async def on_presence_update(before, after):
    for activity in after.activities:
        if isinstance(activity, discord.Spotify):
            print(after, 'is listening to', activity.title, 'by', activity.artist)
            if activity.track_id:
                print(activity.track_url)
```

- `track_id` is `None` for a **local file**, which has no Spotify ID. Since
  v2.3.3, `track_url` is then `''` and a warning is logged once
  (`Spotify.track_url is empty for a local file ...`); before, it was a URL
  ending in `/None`. Check `track_id` first.
- `start`, `end` and `duration` give the playback position;
  `album_cover_url` the cover.

[`examples/spotify_presence.py`](../examples/spotify_presence.py) shows the
whole thing.

### `ActivityType.unknown`

Discord sometimes sends activity types this version doesn't know. A missing
type still means `playing`. `ActivityType.unknown` (`-1`) exists since v2.3.3
(it was documented before but missing). Discord doesn't accept `-1`, so an
`Activity` with type `unknown` is **sent as playing**, with a warning once:
`An Activity with type ActivityType.unknown cannot be sent to Discord; it is sent as "playing" instead.`

## Reading other people's presences

```python
member = guild.get_member(user_id)
print(member.status, member.raw_status, member.activities, member.activity)
print(member.desktop_status, member.mobile_status, member.web_status, member.is_on_mobile())

@client.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if before.status != after.status:
        print(after, before.status, '->', after.status)
```

What you can see on a user account:

- **Friends, implicit relationships and people you have an open DM (and a
  mutual guild) with:** always.
- **Guild members:** only the presences Discord sends for subscribed guilds,
  and for members you subscribed to with `guild.subscribe_to(members=[...])`
  or fetched with `query_members(presences=True)`. There's no way to get
  presences for **every** member of a big guild. See
  [Member lists](member-lists.md#presences).
- Presences are cached per guild (`max_guild_presence_cache`, 5,000) and
  outside guilds (`max_presence_cache`, 20,000). An evicted presence comes back
  with the member's next update.
- In guilds, `on_presence_update(before, after)` gets `Member`s and needs the
  member cached; for friends and other relationships it gets `Relationship`
  objects (with `status`, `activities` and `user`). `on_raw_presence_update`
  (opt in with `enable_raw_presences=True`) fires for everyone Discord sends.

## Your sessions

`client.sessions` lists your account's gateway sessions as Discord reports
them (the library's, your apps', and an "all" session for your overall
presence), with each session's client info, status and activities.
`on_session_create`, `on_session_update` and `on_session_delete` fire as they
change.

## Gotchas

- **Your presence changes everywhere** with `edit_settings=True`. Use `False`
  for anything automatic.
- **`change_presence` in `setup_hook` fails;** the gateway isn't up yet.
- **Status `offline` becomes `invisible`.** There's no "offline but
  connected" other than invisible.
- **Don't loop `change_presence`.** 5 per 20 seconds is Discord's limit, and
  constantly changing presences look automated.
