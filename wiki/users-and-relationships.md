# Users and relationships

Users, your own account (`ClientUser`) and editing it, profiles, friends,
friend requests, blocks, friend suggestions, notes and connected accounts.
Presence and activities are on [Presence and activities](presence-and-activities.md);
settings on [Settings](settings.md).

## Users

```python
user = client.get_user(user_id)                # cache (weakly held: may be None)
user = await client.fetch_user(user_id)        # REST

print(user.name, user.global_name, user.display_name, user.id, user.created_at)
print(user.avatar, user.display_avatar.url, user.banner, user.accent_colour)
```

| Attribute | Meaning |
|---|---|
| `name` | The username (unique, lowercase since the 2023 migration). |
| `discriminator` | `'0'` for migrated ("pomelo") accounts, the old 4 digits otherwise. `is_pomelo()` tells you which. `str(user)` is `name` for pomelo accounts and `name#1234` otherwise. |
| `global_name` | The display name the user chose, or `None`. |
| `display_name` | `global_name` if set, else `name`. On a `Member`, the guild nickname comes first. |
| `avatar`, `display_avatar` | `avatar` is `None` without a custom avatar; `display_avatar` falls back to the default one. |
| `banner`, `accent_colour` | Only filled on fetched users and profiles; the gateway doesn't send them everywhere. |
| `avatar_decoration`, `collectibles`, `primary_guild`, `display_name_style` | Profile cosmetics: decorations, nameplates, the guild tag shown next to the name, and the name's font, effect and colours. `primary_guild` is never `None` (check `primary_guild.identity_enabled`). |
| `public_flags` | Badges (`staff`, `partner`, `verified_bot_developer`...). |
| `bot`, `system` | |

- `User` objects are shared: one object per user, referenced by every
  `Member` and `Message` for that person, and dropped when nothing references
  it. That's why `client.get_user` can return `None` for someone you saw
  earlier.
- Users compare and hash by ID; `member == user` is `True` for the same
  person.

## Your account: `ClientUser`

`client.user` is a `ClientUser`, set by `login()`:

```python
me = client.user
print(me.email, me.verified, me.mfa_enabled, me.premium_type, me.locale, me.phone)
```

### Editing your account

```python
me = await client.user.edit(global_name='Sam', bio='hello', pronouns='they/them')
me = await client.user.edit(avatar=open('me.png', 'rb').read())
me = await client.user.edit(avatar=None)                                  # remove it
me = await client.user.edit(username='new_name', password=current_password)
me = await client.user.edit(new_password='...', password=current_password)
```

- **A password is required** only to change `username`, `discriminator`,
  `email` or the password itself (`new_password`); otherwise
  `ValueError('Password is required')`. Everything else needs no password.
- **Changing the password returns a new token**, which the library stores and
  uses from then on. Old tokens stop working, so update wherever you keep it.
- `avatar` takes image bytes, a `RecentAvatar` (one of your recent avatars), or
  `None`. `avatar_description` adds alt text to an upload.
- `banner` (Nitro), `accent_colour` (a `Colour`, or `None` to clear),
  `bio` (`None` clears it), `date_of_birth` (a `datetime.date`).
- `house` changes your HypeSquad house (`None` leaves it).
- `primary_guild` sets the guild tag shown next to your name (a guild, or
  `None` to hide it).
- `display_name_font`, `display_name_effect`, `display_name_colours`: the
  name style (Nitro).
- `avatar_decoration_sku_id`, `nameplate_sku_id`: collectibles you own.
  `avatar_decoration=` still works with a `DeprecationWarning`.
- `pomelo=True` with `username=...` migrates a legacy `name#1234` account to
  a unique username. It can't be undone.

Profile-only fields have their own method, which returns the profile metadata:

```python
meta = await client.user.edit_profile(bio='hi', pronouns='she/her', theme_colours=[discord.Colour.red(), discord.Colour.blue()])
```

Discord's limits: bios up to 190 characters, pronouns up to 40.

> Changing your username, email or password is the kind of action Discord
> asks to verify (captcha, email, 2FA). Do it in the official client.

## Profiles

```python
profile = await client.fetch_user_profile(user_id, with_mutual_guilds=True, with_mutual_friends=True)
print(profile.bio, profile.metadata.banner, profile.metadata.accent_colour)
print(profile.premium_type, profile.premium_since, profile.badges)
print([g.guild.name for g in profile.mutual_guilds or []], len(profile.mutual_friends or []))
for connection in profile.connections:
    print(connection.type, connection.name, connection.url)

member_profile = await guild.fetch_member_profile(user_id)   # with the guild-specific parts
```

- You can only fetch the profile of someone you share a guild with, are
  friends with, or have a pending request from; otherwise `NotFound`.
- `mutual_guilds` and `mutual_friends` are `None` unless you asked for them.
- `profile.metadata` holds the banner, accent and theme colours, and profile
  effect.
- Profiles are a heavy endpoint for the official client too: don't fetch them
  in a loop.

## Relationships

A **relationship** is Discord's word for friends, friend requests and blocks.

```python
for rel in client.relationships:          # every relationship, from the cache
    print(rel.user, rel.type, rel.nick)

friends = client.friends                  # just friends
blocked = client.blocked

rels = await client.fetch_relationships() # from the API
```

| `RelationshipType` | Meaning |
|---|---|
| `friend` | Friends. |
| `incoming_request` | Someone sent you a request. |
| `outgoing_request` | You sent one. |
| `blocked` | You blocked them. |
| `implicit` | Someone you interact with often but aren't friends with (affects presences you get). |

### Friend requests

```python
await client.send_friend_request(user)              # a User or Member
await client.send_friend_request('some_username')   # by username
await client.send_friend_request('name', '1234')    # a legacy name and discriminator
await user.send_friend_request()

# Incoming
for rel in client.relationships:
    if rel.type is discord.RelationshipType.incoming_request:
        await rel.accept()                          # or await rel.delete() to ignore it

await relationship.delete()   # unfriend, unblock, ignore an incoming or cancel an outgoing request, by type
await friend_relationship.edit(nick='Bestie')       # a friend nickname; None clears it

await user.block()
await user.unblock()
await user.remove_friend()
```

- Requests from people you share nothing with ("strangers") need
  `rel.accept(confirm_stranger_request=True)`; Discord rejects them otherwise.
- **Friend requests are one of the most spam-sensitive actions.** Expect
  captchas, and pace them far more slowly than messages.

```python
@client.event
async def on_relationship_add(rel: discord.Relationship):
    if rel.type is discord.RelationshipType.incoming_request:
        print('friend request from', rel.user)

@client.event
async def on_relationship_update(before, after):
    if before.type is discord.RelationshipType.outgoing_request and after.type is discord.RelationshipType.friend:
        print(after.user, 'accepted')
```

### Friend suggestions

```python
for suggestion in await client.friend_suggestions():
    print(suggestion.id, suggestion.user, [r.type for r in suggestion.reasons], suggestion.from_user_contacts)
    await suggestion.delete()      # dismiss; suggestion.accept() sends a friend request
```

`FriendSuggestion.id` (the suggested user's ID) was added in v2.3.3, so
suggestions can be hashed and compared. Events: `on_friend_suggestion_add`,
`on_friend_suggestion_remove`, `on_raw_friend_suggestion_remove`.

## Notes

Private notes on users, synced across your clients:

```python
note = await user.fetch_note()           # str or None
await user.edit_note('met at the conference')
await user.delete_note()

all_notes = await client.fetch_notes()   # {user_id: note}
```

`on_note_update(user, note)` fires when a note changes on any of your
sessions.

## Connected accounts

```python
for connection in await client.fetch_connections():
    print(connection.type, connection.name, connection.verified, connection.visible)
    await connection.edit(visible=False, show_activity=False)
```

- `Connection` (yours) has `edit`, `refresh`, `delete` and
  `fetch_access_token`; `PartialConnection` (on profiles) is read-only and has
  `url`.
- `on_connections_update`, `on_connection_create`, `on_connection_update`
  fire on changes.

## Affinities

`await client.user_affinities()` returns Discord's ranking of the users you
interact with most (it drives friend suggestions and "frequently contacted").
Guild and channel affinities exist too.

## Gotchas

- **`client.get_user` returns `None`** for users nothing references anymore.
  Fetch them, or keep an ID.
- **Banners and accent colours** are only on fetched users and profiles.
- **`display_name` on a `User`** is the global name; on a `Member`, the
  nickname wins.
- **The token changes when the password does.** The library switches to the
  new one; anything else using the old token is logged out.
- **A member update missing a user field** keeps the cached value and logs a
  warning once per field (since v2.3.3). Before, the field was reset.
