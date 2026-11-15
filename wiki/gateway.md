# The gateway

The gateway is the websocket that delivers events. This page covers how the
client connects and identifies, compression, heartbeats and liveness, when it
resumes and when it starts over, every close code, the opcodes it sends,
guild subscriptions, and how broken input is handled. The code is
`DiscordWebSocket` in [`discord/gateway.py`](../discord/gateway.py) and the
connect loop in `Client.connect` ([`discord/client.py`](../discord/client.py)).

This fork connects with **curl_cffi**, which gives the websocket the TLS
fingerprint of a real browser. It doesn't use aiohttp for the gateway, because
Discord's Cloudflare layer looks at TLS fingerprints.

## Connecting

- **URL:** `wss://gateway.discord.gg/?v=9&encoding=json&compress=<type>`.
  API version 9 (`INTERNAL_API_VERSION` in `http.py`), JSON encoding.
- **Timeout:** each connection attempt (socket plus HELLO plus
  IDENTIFY/RESUME) has 60 seconds.
- **HELLO** gives the heartbeat interval. A HELLO without a usable
  `heartbeat_interval` logs `Gateway sent an unusable HELLO (...). Reconnecting.`
- **IDENTIFY (op 2)** sends what the official web client sends:
  - `token`
  - `capabilities`: **`0x57fd`** (`Capabilities.default()`): `lazy_user_notes`,
    `versioned_read_states`, `versioned_user_guild_settings`,
    `dedupe_user_objects`, `prioritized_ready_payload`,
    `multiple_guild_experiment_populations`, `non_channel_read_states`,
    `auth_token_refresh`, `user_settings_proto`, `client_state_v2`,
    `auto_call_connect` and `passive_guild_update_v2`
  - `properties`: the [super properties](discord-user-api-notes.md#super-properties),
    plus `installation_id` when you set one
  - `presence`: your initial status and activities
  - `compress: false` (the transport is already compressed, below)
  - `client_state: {"guild_versions": {}}`
- **RESUME (op 6)** sends only `token`, `session_id` and `seq`.

### Compression

| Mode | When | How frames arrive |
|---|---|---|
| `zstd-stream` | A zstd decoder is available: the `zstandard` package (the `speed` extra, Python 3.13 and older) or the stdlib `compression.zstd` (Python 3.14+). `zstandard` is preferred when both exist. | Every websocket message is one complete compressed message. |
| `zlib-stream` | Otherwise. | Messages are split across frames; the client buffers until the `00 00 FF FF` flush marker. |

Both are streaming transport compression: one decompression context for the
whole connection.

## After IDENTIFY: READY and startup

1. **READY** arrives. The client cancels any pending startup from an earlier
   session, **clears the caches** (unless a persistence snapshot was just
   replayed), and parses your user, settings, read states, guild settings,
   experiments, connected accounts, sessions and so on. A READY without
   `session_id` or `resume_gateway_url` logs
   `Gateway sent an unusable READY (...). Reconnecting.`
2. **READY_SUPPLEMENTAL** arrives with members, presences and voice states for
   your guilds, and the client loads private channels.
   `on_connect` fires here.
3. **Startup work:** guild subscriptions are sent (see [below](#guild-subscriptions)),
   and with `chunk_guilds_at_startup`, member lists are loaded. Each guild has a
   timeout of `max(5, member_count / 10000)` seconds, all running at once; one
   that times out logs `Timed out waiting for chunks for guild ID ...` and
   startup continues.
4. **`on_ready`** fires.

An exception while parsing READY or READY_SUPPLEMENTAL is raised (it would
leave the caches half-built), while an exception in any other event's parser
is logged (`Parsing event X encountered an exception. Please open an issue with this traceback:`)
and the event is dropped.

## Staying connected

### Heartbeats

- The first heartbeat goes out as soon as HELLO arrives, then one every
  interval.
- The main gateway sends **QoS heartbeats (op 40)** like the official client:
  `{"qos": {"ver": 27, "active": true, "reasons": ["foregrounded"]}, "seq": ...}`,
  with `"rtc_connected"` added while a voice client is connected. When Discord
  asks for a heartbeat (op 1), the client answers with the same op 40.
- **Time spent (op 41)** is reported right after READY and every 30 minutes,
  as the web client does.
- `client.latency` is the time between the last heartbeat and its ACK, in
  seconds. It's `nan` before the first connection and `inf` until the first
  ACK. If it goes over 10 seconds, the client logs
  `Can't keep up, websocket is X.Xs behind.`
- If sending a heartbeat is blocked for more than 10 seconds (the event loop is
  busy), the client logs `Heartbeat blocked for more than 10 seconds.` with the
  stack of what was running, every 10 seconds. That's almost always a blocking
  call in your code.

### Liveness

**Any** message from the gateway counts as a sign of life, not only a
heartbeat ACK. If **nothing** arrives for `heartbeat_timeout` (60 seconds by
default):

- the heartbeat checker logs `Gateway has stopped responding. Closing and restarting.`
- the stalled socket is closed with code **4000** (which keeps the session
  resumable), and the client resumes on a new socket.

The receive loop applies the same timeout to each read, so a socket that
silently stops delivering is closed too. (Ported from Rapptz in v2.3.2.)

## Reconnecting

### Resume or start over

| Situation | What the client does |
|---|---|
| The connection drops, the receive times out, or Discord closes with a resumable code | **RESUME** the session: same session ID, missed events are replayed, caches stay, `on_resumed` fires. |
| `RECONNECT` (op 7) from Discord | Close with 4000, then RESUME at once. |
| `INVALID_SESSION` with `d: true` (resumable) | Close with 4000, wait 1 to 5 seconds (random), then RESUME. |
| `INVALID_SESSION` with `d: false` | Forget the session, close with 1000, wait 1 to 5 seconds, then IDENTIFY: a new session, caches rebuilt, `on_ready` fires again. |
| Close code 4007 (invalid sequence) or 4009 (session timed out) | IDENTIFY at once on the default gateway URL. |
| The resume URL refuses the upgrade with a 5xx | Switch to `wss://gateway.discord.gg/` and IDENTIFY. (Ported from Rapptz in v2.3.2; curl_cffi reports it as `CurlError` 22.) |
| A connection reset (`ECONNRESET`) | RESUME immediately once, without backoff. |

Measured live:

- **v2.3.2:** a forced close gave 1 `on_resumed`, no new `on_ready`, the same
  22 guilds and 3,787 cached members.
- **October 2026:** the session ID stayed the same across a resume.

### Backoff

- Before every reconnect, `on_disconnect` fires.
- A session that lasted at least 60 seconds counts as healthy and resets the
  backoff.
- After more than 3 quick reconnects in a row, the client waits before trying
  again and logs `The gateway asked to ... N times in a row; waiting X.Xs first.`
- Failures in between use exponential backoff: a random delay between 0 and
  2^n seconds, n growing with each failure up to 10 (so at most 1,024 seconds),
  and reset after 2,048 seconds without a failure. Each one is logged as
  `Attempting a reconnect in X.XXs` with the reason.

### Close codes

| Code | Meaning (Discord's) | What the client does |
|---|---|---|
| 1000 sent by Discord | Normal close | Reconnect and resume. |
| 1000 sent by `Client.close()` | You closed the client | Stop. |
| 4000 | Unknown error (also what the library sends when it restarts a socket) | Resume. |
| 4001, 4002, 4003, 4005 | Unknown opcode, decode error, not authenticated, already authenticated | Resume. |
| 4004 | Authentication failed | **Stop** and raise `ConnectionClosed`. The token is invalid. |
| 4007 | Invalid sequence | Identify again. |
| 4008 | Rate limited (you sent too fast) | Resume. |
| 4009 | Session timed out | Identify again. |
| 4010, 4011, 4012, 4013, 4014 | Invalid shard, sharding required, invalid API version, invalid or disallowed intents | **Stop** and raise `ConnectionClosed`. These shouldn't happen with a user account. |
| Anything else, or a dropped TCP connection | | Resume when possible. |

`client.connect(reconnect=False)` doesn't reconnect at all: it closes the
client and re-raises, except for a 1000 close, which returns quietly.

A bad token normally fails **before** the gateway: the HTTP login gets a 401
and raises `LoginFailure('Improper token has been passed')`. 4004 is the
gateway's version of the same failure. See [Tokens and login](tokens-and-login.md).

## Opcodes this library sends

| Op | Name in `DiscordWebSocket` | Used for |
|---|---|---|
| 2 | `IDENTIFY` | Logging in. |
| 3 | `PRESENCE` | `change_presence`, and the initial presence. |
| 4 | `VOICE_STATE` | Joining, leaving, moving or muting in voice (`change_voice_state`, voice connect). Also sent once after every READY. |
| 5 | `VOICE_PING` | Voice region pings. |
| 6 | `RESUME` | Resuming a session. |
| 8 | `REQUEST_MEMBERS` | `Guild.query_members`, and `Guild.chunk` / `fetch_members` when you have Kick, Ban or Manage Roles. |
| 13 | `CALL_CONNECT` | DM and group calls. |
| 14 | `GUILD_SUBSCRIBE` | Defined (`guild_subscribe`), but the library uses op 37 instead. |
| 18–22 | `STREAM_CREATE` / `DELETE` / `WATCH` / `PING` / `SET_PAUSED` | Go Live streams. |
| 31 | `REQUEST_SOUNDBOARD_SOUNDS` | `Client.request_soundboard_sounds` (see [Soundboard and voice](soundboard-and-voice.md)). |
| 35 | `SEARCH_RECENT_MEMBERS` | `Guild.query_recent_members`, paged with a continuation token, 30 seconds per page. |
| 37 | `BULK_GUILD_SUBSCRIBE` | Guild subscriptions and member-sidebar scraping. |
| 40 | `QOS_HEARTBEAT` | Every heartbeat, as above. |
| 41 | `UPDATE_TIME_SPENT_SESSION_ID` | Time-spent reports. |

Received: 0 `DISPATCH`, 1 `HEARTBEAT` (a request), 7 `RECONNECT`, 9
`INVALIDATE_SESSION`, 10 `HELLO` and 11 `HEARTBEAT_ACK`. Discord's full list is
in [Discord user API notes](discord-user-api-notes.md#gateway-opcodes).

**Send rate limit.** Everything except heartbeats goes through a limiter of
110 messages per 60 seconds, IDENTIFY and RESUME included. Above it, sends
wait. According to Discord's user-API docs, Discord closes the connection
with 4008 for clients that send too fast. The library has no separate client
side limit for presence updates or member requests; Discord applies its own.

## Guild subscriptions

On a user account, Discord only sends some events for a guild while the
client is **subscribed** to it. According to upstream's documentation
([`docs/guild_subscriptions.rst`](../docs/guild_subscriptions.rst)), Discord
subscribes a client to guilds under 75,000 members by itself; in a guild the
client isn't subscribed to, no "non-stateful" events arrive (`on_message`,
`on_message_edit`, `on_message_delete`, reactions...). With
`guild_subscriptions=True` (the default), the library subscribes to **every**
guild after READY, whatever its size, with these features:

| Feature | `Guild.subscribe(...)` flag | What you get |
|---|---|---|
| Typing | `typing` | `on_typing` events. Subscribing to any feature also subscribes the guild itself. |
| Activities | `activities` | Activity updates for the guild. There's no way to get presences for **every** member of a guild; see [Member lists](member-lists.md). |
| Threads | `threads` | The full thread list and thread member updates. |
| Member updates | `member_updates` | `on_member_join`, `on_member_update` and `on_member_remove`. |

Events for actions your own account performs (kicks, bans, nickname changes)
always arrive, subscribed or not.

Subscriptions are sent in batches with op 37 (`BULK_GUILD_SUBSCRIBE`):

- changes are collected for 0.5 seconds and sent together;
- a batch is at most 15,360 bytes (15 KiB); a single change bigger than that
  raises `ValueError('Guild subscription payload too large to send')`;
- a failed send is retried with a delay that doubles up to 30 seconds, and
  logged (`Bulk guild subscribe failed with ... Requeuing changes...`) on the
  first failure and every tenth.

To change a guild's subscription yourself:

```python
await guild.subscribe(typing=True, activities=True, threads=True, member_updates=True)
await guild.subscribe(typing=False)     # unsubscribe from the guild entirely
print(guild.is_subscribed())
```

- `guild.subscribe()` with no arguments subscribes to typing.
- Turning `typing` off while another feature is on raises
  `TypeError('Cannot unsubscribe from guild while other features are still subscribed')`:
  typing is what keeps the guild subscribed at all.
- `Guild.subscribe_to(members=..., threads=...)` and
  `Guild.unsubscribe_from(...)` manage subscriptions to specific members (you
  then get their member and presence updates) or specific threads' member
  lists.
- Changes are debounced, so they take up to half a second to go out.

[Member lists](member-lists.md) covers the member sidebar, which uses the same
opcode.

## Broken input

The gateway is treated as untrusted input:

- **A frame that can't be decoded** (corrupt compressed data, invalid JSON,
  or an array or scalar instead of an object) logs
  `Gateway sent a frame that could not be decoded (N bytes, Error: ...). Resuming the session.`,
  closes the socket with 4000 and resumes on a new one; Discord replays the
  events after the last sequence. Repeated failures back off like any other
  reconnect. In 2.3.2 and earlier, such a frame stopped the client.
- **Event data that is hostile or malformed** but valid JSON is handled by
  the parsers, which log a warning and drop that event. The stress
  benchmark's `bomb` phase sends 200,000 such frames, and memory stays
  bounded.
- **A failed handshake** (sending IDENTIFY or RESUME fails, or the connect
  timeout cancels it) closes the new socket with 4000 instead of leaving it
  open.

## Performance

Frames are decompressed and decoded (orjson when installed) in
`received_message`, then handed to `ConnectionState.parse_*`. The stress
benchmark's baseline ([`docs/cache_tuning.rst`](../docs/cache_tuning.rst))
puts this path at about 37,000 events per second on 4 vCPUs without handlers.
`(await client.cache_stats()).event_loop_lag` tells you whether your handlers keep up.

## Debugging the gateway

```python
import logging
discord.utils.setup_logging(level=logging.DEBUG)   # every event name and payload, redacted
```

- At DEBUG the gateway logs every event (`Gateway event: ...`) with credentials
  redacted.
- `on_socket_event_type(event_type)` fires for every event with its name.
- `Client(enable_debug_events=True)` dispatches `on_socket_raw_receive(msg)`
  and `on_socket_raw_send(payload)` with the raw frames, **unredacted**.

```python
from collections import Counter

counts = Counter()

@client.event
async def on_socket_event_type(event_type):
    counts[event_type] += 1      # what is this account actually receiving?
```
