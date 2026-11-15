# Soundboard and voice

Voice on a user account: joining channels with or without audio, connecting
and playing audio, the handshake and its failure modes, DAVE encryption,
audio sources and FFmpeg, DM and group calls, Go Live streams, the
soundboard, voice channel effects and voice states. Installing the voice
dependencies is covered in [Installation](installation.md#the-voice-extra).

## Two ways to be "in voice"

| | `guild.change_voice_state(channel=...)` | `voice_channel.connect()` |
|---|---|---|
| What it does | Sends a voice state update (gateway op 4). You appear in the channel. | Joins and opens a voice connection (websocket + UDP), returning a `VoiceClient`. |
| Audio | None: nothing is sent or received. | Send audio with `play()`. |
| Needs | Nothing extra. | The `voice` extra (PyNaCl, davey) and libopus; FFmpeg for most sources. |
| Good for | Presence in a channel, soundboard sounds, being "there". | Playing audio. |

```python
await guild.change_voice_state(channel=voice_channel, self_mute=True, self_deaf=False)
await guild.change_voice_state(channel=None)          # leave
```

## Connecting for audio

```python
vc: discord.VoiceClient = await voice_channel.connect(timeout=30.0, reconnect=True, self_deaf=True)
print(vc.channel, vc.latency, vc.average_latency, vc.is_connected())

await vc.move_to(other_channel)    # None disconnects
await vc.disconnect()
```

`connect(*, timeout=30.0, reconnect=True, cls=VoiceClient, self_deaf=False, self_mute=False, self_video=False)`:

- Raises `asyncio.TimeoutError` if the handshake doesn't finish in `timeout`,
  `ClientException('Already connected to a voice channel')` if this guild (or
  call) already has a voice client, `RuntimeError` without PyNaCl or davey,
  and `discord.opus.OpusNotLoaded` without libopus.
- **A failed connect cleans up.** If `connect()` fails for any reason, or is
  cancelled (by `asyncio.wait_for`, say), the voice client is disconnected and
  unregistered before the error is raised. Before the November 2026 audit only
  a timeout did this, and every later `connect()` raised "Already connected to
  a voice channel".
- The handshake is tried up to 5 times within `timeout` when the voice socket
  closes during it (with `reconnect=True`), with jittered backoff; the last
  failure logs `Failed to connect to voice after 5 attempts (close code N).`
- `vc.latency` is `inf` until the first heartbeat ACK; `average_latency`
  averages the last 20.
- `move_to(channel, timeout=30.0)`: if the move doesn't complete in time, it
  logs `Timed out trying to move to channel ...` and returns without raising.
- `client.voice_clients` lists every active voice connection;
  `guild.voice_client` is the one for a guild.

### What can go wrong during the handshake

- **IP discovery times out.** The voice server's IP discovery reply is a
  single UDP packet. If it's lost, the wait ends after 10 seconds instead of
  hanging. What happens next depends on where it was lost:
  - during `connect()`: the call fails and the client disconnects from voice,
    so call `connect()` again;
  - during a resume (close code 4015, a network error without a close code,
    or 4014/4022 after being moved): the client gives up and disconnects
    from voice, so call `connect()` again;
  - during the automatic reconnect after any other close (with
    `reconnect=True`, the default): the library keeps retrying by itself and
    logs `Could not connect to voice... Retrying...` each time. Don't call
    `connect()` then; it would raise "Already connected to a voice channel".
- **Moved while connecting.** If Discord moves the account to another channel
  during the handshake, the library starts a new handshake and `connect()`
  waits for it. It used to raise `asyncio.CancelledError`.
- **Disconnecting while connecting.** `vc.disconnect()` from another task
  during the handshake stops it, and the pending `connect()` raises
  `ClientException('Disconnected from voice while connecting.')`.
- **Voice server changes.** When Discord moves the connection to another
  voice server, the old UDP socket is closed and the reader switches to the
  new one at once (it used to wait up to 30 seconds, which could time out IP
  discovery).
- **No usable encryption mode:** `ClientException('Unable to find a supported voice encryption mode')`.

### Voice close codes

| Code | Meaning | What the library does |
|---|---|---|
| 1000 | Normal close | Disconnects. |
| 4014 | Moved, or disconnected by a moderator | Disconnects if the main gateway already reported it; otherwise tries to reconnect. |
| 4015 | Voice server crashed | Resumes the voice session. |
| 4021 | Rate limited | Logs `We are being rate limited while trying to connect to voice. Disconnecting...` and disconnects. |
| 4022 | The call or channel was terminated | Like 4014. |
| No code (network error) | | Resumes. |
| Anything else | | With `reconnect=True`, reconnects with backoff (`Disconnected from voice... Reconnecting in X.XXs.`). |

Upstream reports 4006 closes in some regions (dolfies/discord.py-self#870)
and random disconnects (#945, #877). See
[Troubleshooting](troubleshooting.md#voice-close-codes).

### Encryption and DAVE

- **Transport encryption** uses PyNaCl. The library picks the first mode the
  server offers from: `aead_xchacha20_poly1305_rtpsize`,
  `xsalsa20_poly1305_lite`, `xsalsa20_poly1305_suffix`, `xsalsa20_poly1305`.
- **DAVE** is Discord's end-to-end encryption for voice, built on MLS and
  implemented by `davey`. The client announces the DAVE version it supports;
  when the server enables it, the library creates a DAVE session and handles
  the DAVE opcodes (21 to 31), and audio is end-to-end encrypted once the
  session is ready. Discord's voice servers increasingly require it; upstream
  reports close code **4017** ("E2EE/DAVE protocol required") without it
  (dolfies/discord.py-self#917).
- The library requires **both** PyNaCl and davey to create a `VoiceClient`,
  and warns at startup when either is missing.

## Playing audio

```python
source = discord.FFmpegPCMAudio('song.mp3')
source = discord.PCMVolumeTransformer(source, volume=0.5)

def after(error: Exception | None):
    # Runs in the audio thread, not the event loop
    if error:
        print('player error:', error)
    asyncio.run_coroutine_threadsafe(channel.send('done'), client.loop)

vc.play(source, after=after)

vc.pause(); vc.resume(); vc.stop()
print(vc.is_playing(), vc.is_paused())
vc.source.volume = 1.5          # with a PCMVolumeTransformer
```

`play(source, *, after=None, application='audio', bitrate=128, fec=True, expected_packet_loss=0.15, bandwidth='full', signal_type='auto')`:

- The encoder options (`application`, `bitrate` 16 to 512 kbps, `fec`,
  `expected_packet_loss`, `bandwidth`, `signal_type`) apply to PCM sources;
  Opus sources are sent as they are. Bad values raise `ValueError`.
- `ClientException('Not connected to voice.')`, `ClientException('Already playing audio.')`,
  `TypeError` for something that isn't an `AudioSource`, `TypeError` for a
  non-callable `after`.
- **`after(error)` runs in the audio thread.** `error` is `None` after a normal
  end or `stop()`, else the exception (FFmpeg failures included, also through
  a `PCMVolumeTransformer`). Use `asyncio.run_coroutine_threadsafe` to call
  async code from it. If `after` raises, `Calling the after function failed.` is
  logged.
- **Disconnected mid-play:** the player waits for the reconnect, logs
  `Not connected, waiting for Ns...`, and stops quietly if it doesn't come back.
- **Changing the source** (`vc.source = new`) doesn't clean up the old one, so
  you can play it again. If you won't, call its `cleanup()`: an FFmpeg source
  with `pipe=True` keeps its process until then. `play()` while something is
  playing stops the old player first (and cleans it up).

### Audio sources

| Source | For |
|---|---|
| `FFmpegPCMAudio(source, *, executable='ffmpeg', pipe=False, stderr=None, before_options=None, options=None)` | Anything FFmpeg can read, decoded to PCM (48 kHz, stereo) and encoded with libopus by the library. Works with `PCMVolumeTransformer`. |
| `FFmpegOpusAudio(source, *, bitrate=None, codec=None, executable='ffmpeg', ...)` | FFmpeg outputs Opus directly. Less CPU, no volume control. `codec='copy'` (or `'opus'`) passes Opus through untouched. |
| `await FFmpegOpusAudio.from_probe(source, *, method=None, **kwargs)` | Probes the input with ffprobe and copies Opus when it already is Opus. |
| `PCMAudio(stream)` | Raw 16-bit 48 kHz stereo PCM from a file object. |
| `PCMVolumeTransformer(original, volume=1.0)` | Volume control for PCM sources (Opus raises `ClientException`). Volume is capped at 2.0. |

```python
# Streams and URLs: reconnect options for FFmpeg
source = discord.FFmpegPCMAudio(
    url,
    before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    options='-vn',
)

# Pipe data you already have
source = discord.FFmpegPCMAudio(io.BytesIO(data), pipe=True)
```

- `before_options` go before `-i`, `options` after; both are split like a
  shell command.
- A missing FFmpeg raises `ClientException('ffmpeg was not found.')`; pass
  `executable='/path/to/ffmpeg'`.
- FFmpeg exiting with an error raises `FFmpegProcessError` with its stderr (the
  first 8 KB) into `after`.
- `from_probe` falls back to `ffmpeg -i` parsing, then to libopus at 128 kbps,
  logging each failed probe. Probing takes up to 20 seconds.

### Opus

```python
import discord.opus

if not discord.opus.is_loaded():
    discord.opus.load_opus('libopus.so.0')      # the full name or path on Linux and macOS
```

Windows uses the DLLs bundled in `discord/bin`. Elsewhere the library looks
for a system libopus with `ctypes.util.find_library('opus')` the first time
it encodes, and raises `OpusNotLoaded` if there's none. See
[Installation](installation.md#the-voice-extra).

## DM and group calls

```python
dm = await user.create_dm()
vc = await dm.connect()            # starts a call and rings them (ring=True)

call = dm.call                     # the PrivateCall, while one is active
print(call.ringing, call.initiator, call.connected, call.members, call.region)
await call.ring(); await call.stop_ringing()
await call.change_region('rotterdam')
await call.leave()

group_call = group.call
await group_call.ring(friend_a)    # GroupCall.ring/stop_ringing take recipients (default: everyone)
```

- `DMChannel.connect(*, timeout=60.0, reconnect=True, ring=True)` starts a call
  and rings; joining an existing call (`call.connect()`) doesn't ring.
- After the call ends, its methods raise `ClientException('Call is over')`.
- Events: `on_call_create`, `on_call_update`, `on_call_delete`. A call message
  in the channel has `message.call` (a `CallMessage`: `participants`,
  `ended_timestamp`, `initiator`, `call_ended`, `duration`).
- An unavailable call (Discord's outage flag) stays in the cache, flagged
  `PrivateCall.unavailable`.

## Go Live streams

`VoiceClient.watch_stream(...)` and `create_stream(...)` handle Go Live
streams (gateway ops 18 to 22); `Stream` objects have `watch()`, `delete()`,
`pause()` and `resume()`, and owner-only methods raise `ClientException` for
others' streams. Video isn't sent or received by `VoiceClient`
(`supports_video()` is `False`); this covers the signalling.
Events: `on_stream_create`, `on_stream_available`, `on_stream_unavailable`,
`on_stream_update`, `on_stream_delete`.

## Voice states

- **Reading:** `member.voice` is the cached `VoiceState` (`channel`,
  `self_mute`, `self_deaf`, `mute`, `deaf`, `self_stream`, `self_video`,
  `suppress`, `requested_to_speak_at`), kept current by the gateway. It was
  correct for both accounts in the live test. `on_voice_state_update(member, before, after)`
  reports changes.
- **`Member.fetch_voice(*, fallback_to_cache=True)`** was ported from Rapptz.
  - Discord serves the voice-state REST endpoints to bot accounts only: it
    returned **403, code 20002, "Only bots can use this endpoint"** for every
    variant tried live.
  - On a user account, `fetch_voice()` doesn't make the request. It returns
    the cached state, or raises `ClientException` if the member isn't in
    voice. `fallback_to_cache=False` raises `ClientException` instead.
  - A bot account still calls the API, and falls back the same way on
    403/20002.
- Members in voice are never evicted from the member cache.

## Soundboard (new in v2.3.2)

Ported from Rapptz/discord.py and adapted to user accounts. The code is in
[`discord/soundboard.py`](../discord/soundboard.py), `Guild` and `VoiceChannel`;
the reference is the "Soundboard" section of [`docs/api.rst`](../docs/api.rst).

### Getting sounds

Bots receive a guild's sounds in `GUILD_CREATE`. **User accounts don't.** The
official client asks for them when you open the soundboard, by sending gateway
op 31 (`REQUEST_SOUNDBOARD_SOUNDS`). Discord replies with one
`SOUNDBOARD_SOUNDS` event per guild.

```python
sounds = await client.request_soundboard_sounds([guild_a, guild_b], timeout=30.0)
guild_a.soundboard_sounds   # now cached
client.get_soundboard_sound(sound_id)
```

- **Only the guilds you name are requested.** The upstream community version
  (dolfies/discord.py-self#929) asks for every guild at login. Asking only for
  the guilds you name matches the official client, and an account in hundreds
  of guilds doesn't cache sounds nobody uses.
- **The timeout must be positive and finite.** It defaults to 30 s. `None`,
  0, negatives, NaN and infinity raise `ValueError`. It covers the send as
  well (the gateway rate limiter can hold that for up to a minute) and the
  replies. Listeners belong to the socket that exists when you call, so after
  a reconnect only the timeout ends the wait. Without a gateway connection it
  raises `ClientException('The client is not connected to the gateway.')`.
- **A complete set or nothing.** A guild's cache is either a complete set or
  `None`. It's filled by a `SOUNDBOARD_SOUNDS` reply, or by a guild payload
  that carries `soundboard_sounds`, and either one replaces it.
  - A create event for a guild you never requested still fires
    `on_soundboard_sound_create`, but isn't cached, so a partial set never
    looks complete.
  - Updates and deletes only change sounds that are cached.
  - An update that changes nothing fires no event.
- **Measured live:** five guilds per account answered in **55 to 70 ms**.

Other calls:

- `await client.fetch_soundboard_default_sounds()` returns Discord's built-in
  sounds, which everyone can use. There were 6 at the time of the live test.
- `await guild.fetch_soundboard_sound(id)` and `await guild.fetch_soundboard_sounds()`
  fetch sounds over REST.

### Creating, editing, deleting

```python
sound = await guild.create_soundboard_sound(name='honk', sound=mp3_bytes, volume=0.5, emoji='🦆')
sound = await sound.edit(name='honk2', volume=0.8, emoji=custom_emoji)  # or a str, a PartialEmoji, or None to clear
await sound.delete()
```

Discord's rules, from its user-API docs and Rapptz's docs:

- **Format:** MP3 or OGG, at most **5.2 seconds** and **512 KiB**. The library
  checks the file's magic bytes (ID3, any MPEG frame sync, or `OggS`) and
  raises `ValueError('Unsupported audio type given (expected MP3 or OGG)')` for
  anything else. Any bytes-like object works, including a `memoryview`. The
  duration and size limits are Discord's; the library doesn't check them.
- **Name:** 2 to 32 characters (not checked by the library).
- **Volume:** 0 to 1, default 1 (not checked by the library).
- **Slots:** a guild has 8, 24, 36 or 48 slots depending on its boost tier, or
  96 with `MORE_SOUNDBOARD`.
- **Permissions:**
  - creating needs Create Expressions;
  - editing and deleting need Manage Expressions;
  - `SoundboardSound.user` is only filled in when you have one of those two.
- **Emoji:** an unsupported `emoji` type raises `TypeError`. An edit sends both
  `emoji_id` and `emoji_name`, so switching between a custom and a unicode
  emoji clears the old one.

### Playing a sound

```python
await guild.change_voice_state(channel=voice_channel, self_mute=True)  # you must be in the channel
await voice_channel.send_sound(sound)   # a SoundboardSound, or a SoundboardDefaultSound
```

- **Your voice state:** you must be **in the voice channel**, and not server
  muted, deafened or suppressed. A voice state update is enough; no
  `VoiceClient` needed.
- **Permissions:** Speak and Use Soundboard.
- **Sounds from another guild** also need Use External Sounds and, on
  Discord's side, **Nitro**. The library sends `source_guild_id` automatically
  when the sound belongs to a different guild.

### Events

- `on_soundboard_sound_create(sound)`, `on_soundboard_sound_update(before, after)`
  and `on_soundboard_sound_delete(sound)`. They follow the cache rules above.
- `on_voice_channel_effect(effect)` fires when someone sends an emoji or sound
  effect in a voice call you're in, in a guild or in a DM or group call.
  Rapptz raises on the private-call case.
  - `effect.is_sound()` says whether it's a sound.
  - `effect.sound` is a `VoiceChannelSoundEffect`, with `is_default()` for
    Discord's built-in sounds.
  - `effect.emoji` holds the emoji, and `effect.animation` the animation
    (`premium` or `basic`).

### Audit log

These actions are `soundboard_sound_create`, `soundboard_sound_update` and
`soundboard_sound_delete`:

- **The target.** Discord sends these entries **without a `target_id`**
  (found in the live test).
  - Create and delete entries take the sound's ID from their changes, so
    `entry.target` is the sound or an `Object`.
  - Update entries carry only the changed fields, so their `target` is
    `None`.
  - Rapptz's version raises `TypeError` here.
- **The emoji.** `AuditLogDiff.emoji` is built from both the `emoji_id` and
  the `emoji_name` change. A custom emoji keeps its ID, and a removed emoji
  reads `None`.

### Tested live (v2.3.2)

Each of these was run on user accounts:

- creating an MP3 sound and an OGG sound (passed as a `memoryview`)
- fetching sounds, one at a time and as a list
- playing a guild sound and a default sound, with the listening account
  receiving `on_voice_channel_effect`
- editing (name, volume, emoji 🦆 → custom → 🎉 → none)
- create, update and delete events reaching another account
- reading the audit log
- deleting the sounds

Not tested live: playing a sound from another guild, which needs Nitro.

## A complete player

[`examples/basic_voice.py`](../examples/basic_voice.py) is a command-based
player (join, play a file or URL, volume, stop) with `PCMVolumeTransformer`
and a `before_invoke` hook that connects to the author's channel.
