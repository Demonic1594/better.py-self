# Soundboard and voice

## Soundboard (new in v2.3.2)

This was ported from Rapptz/discord.py and adapted to user accounts. The code is in
[`discord/soundboard.py`](../discord/soundboard.py), `Guild` and `VoiceChannel`;
the reference is the "Soundboard" section of [`docs/api.rst`](../docs/api.rst).

### Getting sounds

Bots receive a guild's sounds in `GUILD_CREATE`. **User accounts don't.** The
official client asks for them when you open the soundboard, by sending gateway
op 31 (`REQUEST_SOUNDBOARD_SOUNDS`). Discord replies with one
`SOUNDBOARD_SOUNDS` event per guild.

```py
sounds = await client.request_soundboard_sounds([guild_a, guild_b], timeout=30.0)
guild_a.soundboard_sounds   # now cached
client.get_soundboard_sound(sound_id)
```

- **Only the guilds you name are requested.** The upstream community version
  (dolfies/discord.py-self#929) asks for every guild at login. Asking only for
  the guilds you name matches the official client, and an account in hundreds
  of guilds doesn't cache sounds nobody uses.
- **The timeout must be positive and finite.** It defaults to 30 s. `None`, 0,
  negatives, NaN and infinity raise `ValueError`. It covers the send as well,
  because the gateway rate limiter can hold that for up to a minute, and the
  replies. Listeners belong to the socket that exists when you call, so after a
  reconnect only the timeout ends the wait.
- **A complete set or nothing.** A guild's cache is either a complete set or
  `None`. It's filled by a `SOUNDBOARD_SOUNDS` reply, or by a guild payload that
  carries `soundboard_sounds`, and either one replaces it.
  - A create event for a guild you never requested still fires
    `on_soundboard_sound_create`, but isn't cached, so a partial set never
    looks complete.
  - Updates and deletes only change sounds that are cached.
  - An update that changes nothing fires no event.
- **Measured live:** five guilds per account answered in **55–70 ms**.

Other calls:

- `await client.fetch_soundboard_default_sounds()` returns Discord's built-in
  sounds, which everyone can use. There were 6 at the time of the live test.
- `await guild.fetch_soundboard_sound(id)` and `await guild.fetch_soundboard_sounds()`
  fetch sounds over REST.

### Creating, editing, deleting

```py
sound = await guild.create_soundboard_sound(name='honk', sound=mp3_bytes, volume=0.5, emoji='🦆')
sound = await sound.edit(name='honk2', volume=0.8, emoji=custom_emoji)  # or a str, a PartialEmoji, or None to clear
await sound.delete()
```

Discord's rules, from its user-API docs and Rapptz's docs:

- **Format:** MP3 or OGG, at most **5.2 seconds** and **512 KiB**. The library
  checks the file's magic bytes (ID3, any MPEG frame sync, or `OggS`) and raises
  `ValueError` for anything else. Any bytes-like object works, including a
  `memoryview`.
- **Name:** 2–32 characters.
- **Volume:** 0 to 1, default 1.
- **Slots:** a guild has 8, 24, 36 or 48 slots depending on its boost tier, or
  96 with `MORE_SOUNDBOARD`.
- **Permissions:**
  - creating needs `create_expressions`
  - editing and deleting need `manage_expressions`
  - `SoundboardSound.user` is only filled in when you have one of those two
- **Emoji:** an unsupported `emoji` type raises `TypeError`. An edit sends both
  `emoji_id` and `emoji_name`, so switching between a custom and a unicode emoji
  clears the old one.

### Playing a sound

```py
await guild.change_voice_state(channel=voice_channel, self_mute=True)  # you must be in the channel
await voice_channel.send_sound(sound)   # a SoundboardSound, or a SoundboardDefaultSound
```

- **Your voice state:** you must be **in the voice channel**, and not server
  muted, deafened or suppressed.
- **Permissions:** `speak` and `use_soundboard`.
- **Sounds from another guild** also need `use_external_sounds` and, on
  Discord's side, **Nitro**. The library sends `source_guild_id` automatically
  when the sound belongs to a different guild.

### Events

- `on_soundboard_sound_create(sound)`, `on_soundboard_sound_update(before, after)`
  and `on_soundboard_sound_delete(sound)`. They follow the cache rules above.
- `on_voice_channel_effect(effect)` fires when someone sends an emoji or sound
  effect in a voice call you're in, in a guild or in a DM or group call. Rapptz
  raises on the private-call case.
  - `effect.is_sound()` says whether it's a sound.
  - `effect.sound` is a `VoiceChannelSoundEffect`, with `is_default()` for
    Discord's built-in sounds.
  - `effect.emoji` holds the emoji, and `effect.animation` the animation (`premium` or
    `basic`).

### Audit log

These actions are `soundboard_sound_create`, `soundboard_sound_update` and
`soundboard_sound_delete`:

- **The target.** Discord sends these entries **without a `target_id`** (found
  in the live test).
  - Create and delete entries take the sound's ID from their changes, so
    `entry.target` is the sound or an `Object`.
  - Update entries carry only the changed fields, so their `target` is `None`.
  - Rapptz's version raises `TypeError` here.
- **The emoji.** `AuditLogDiff.emoji` is built from both the `emoji_id` and the
  `emoji_name` change. A custom emoji keeps its ID, and a removed emoji reads `None`.

### Tested live (v2.3.2)

Each of these was run on user accounts:

- creating an MP3 sound and an OGG sound (passed as a `memoryview`)
- fetching sounds, one at a time and as a list
- playing a guild sound and a default sound, with the listening account receiving `on_voice_channel_effect`
- editing (name, volume, emoji 🦆 → custom → 🎉 → none)
- create, update and delete events reaching another account
- reading the audit log
- deleting the sounds

Not tested live: playing a sound from another guild, which needs Nitro.

## Voice states

- **Joining.** `await guild.change_voice_state(channel=..., self_mute=..., self_deaf=..., self_video=...)`
  joins, moves or leaves (`channel=None`) over gateway op 4. It works **without**
  voice support installed. You show up in the channel, but no audio flows.
- **Reading.** `member.voice` reads the cached voice state, which the gateway
  keeps current. It was correct for both accounts in the live test.
- **`Member.fetch_voice(*, fallback_to_cache=True)`** was ported from Rapptz.
  - Discord serves the voice-state REST endpoints to bot accounts only: it
    returned **403, code 20002, "Only bots can use this endpoint"** for every
    variant tried live.
  - On a user account, `fetch_voice()` doesn't make the request. It returns the
    cached state, or raises `ClientException` if the member isn't in voice.
    `fallback_to_cache=False` raises `ClientException` instead.
  - A bot account still calls the API, and falls back the same way on 403/20002.

## Voice audio

For audio, install the `[voice]` extra (`PyNaCl`, `davey`) and connect with
`await voice_channel.connect()`, which returns a `VoiceClient`.

**Encryption.** Discord's voice servers now require **DAVE**, Discord's
end-to-end encryption. `davey` implements it, and the voice gateway handles the
DAVE opcodes 21–25. Without it, connections fail; upstream reports close code
**4017** ("E2EE/DAVE protocol required"), dolfies/discord.py-self#917. The client
warns at startup when `davey` is missing.

**Known upstream voice issues** (open or recent at dolfies/discord.py-self):

- random disconnects (#945, #877)
- 4006 closes in some regions (#870)

See [Troubleshooting](troubleshooting.md#voice-close-codes).

[`examples/basic_voice.py`](../examples/basic_voice.py) shows playback.
