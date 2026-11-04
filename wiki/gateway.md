# The gateway

The gateway is the WebSocket that delivers events. This fork connects with
**curl_cffi**, which gives it the TLS fingerprint of a real browser; it doesn't
use aiohttp, because Discord's Cloudflare layer looks at TLS fingerprints. The
code is `DiscordWebSocket` in [`discord/gateway.py`](../discord/gateway.py).

## Connecting

- **URL:** `wss://gateway.discord.gg/?v=9&encoding=json&compress=<type>`, with API
  version 9 (`INTERNAL_API_VERSION` in `http.py`).
- **Compression:** `zstd-stream` when a zstd decoder is available (the
  `zstandard` package from the `[speed]` extra, or the standard library's
  `compression.zstd` on Python 3.14+), and `zlib-stream` otherwise. Both are
  streaming transport compression.
- **IDENTIFY** sends the token, `capabilities`, the client `properties` (the
  [super properties](discord-user-api-notes.md#super-properties)), `presence`,
  `compress: false` and `client_state`, as the official client does. The
  capabilities value is **`0x57fd`**: `lazy_user_notes`, `versioned_read_states`,
  `versioned_user_guild_settings`, `dedupe_user_objects`,
  `prioritized_ready_payload`, `multiple_guild_experiment_populations`,
  `non_channel_read_states`, `auth_token_refresh`, `user_settings_proto`,
  `client_state_v2`, `auto_call_connect` and `passive_guild_update_v2`. You can
  read it as `Capabilities.default()`.
- **After READY**, `READY_SUPPLEMENTAL`, `SESSIONS_REPLACE`, guild subscriptions
  and (with `chunk_guilds_at_startup`) chunking follow. `on_ready` waits for the
  chunking.

## Opcodes this library sends

| Op | Name in `DiscordWebSocket` | Used for |
|---|---|---|
| 1 | `HEARTBEAT` | Heartbeats |
| 2 | `IDENTIFY` | Logging in |
| 3 | `PRESENCE` | `change_presence` |
| 4 | `VOICE_STATE` | Joining, leaving or muting in voice (`change_voice_state`, voice connect) |
| 5 | `VOICE_PING` | Voice region ping |
| 6 | `RESUME` | Resuming a session |
| 8 | `REQUEST_MEMBERS` | `query_members`, and `fetch_members` / chunking when you have the permissions |
| 13 | `CALL_CONNECT` | DM and group calls |
| 18–22 | `STREAM_CREATE`/`DELETE`/`WATCH`/`PING`/`SET_PAUSED` | Go Live streams |
| 31 | `REQUEST_SOUNDBOARD_SOUNDS` | `Client.request_soundboard_sounds` (see [Soundboard and voice](soundboard-and-voice.md)) |
| 35 | `SEARCH_RECENT_MEMBERS` | Searching recent members |
| 37 | `BULK_GUILD_SUBSCRIBE` | Guild subscriptions and member-sidebar scraping |
| 40 | `QOS_HEARTBEAT` | Heartbeats carrying QoS data (version 27), as the official client sends them |
| 41 | `UPDATE_TIME_SPENT_SESSION_ID` | Time-spent tracking, as the official client does |

Received: 0 `DISPATCH`, 7 `RECONNECT`, 9 `INVALIDATE_SESSION`, 10 `HELLO` and 11
`HEARTBEAT_ACK`. Discord's full list is in [Discord user API notes](discord-user-api-notes.md#gateway-opcodes).

## Staying connected

- **Heartbeats.** Heartbeats go out on the interval in `HELLO`. Every message
  received counts as a sign of life, not only `HEARTBEAT_ACK`. If **nothing**
  arrives for `heartbeat_timeout` (60 s), the socket is restarted.
- **Receive timeout.** When nothing arrives within the heartbeat window,
  the stalled socket is **closed with code 4000** before the client reconnects.
  4000 keeps the session resumable, and the old socket isn't left open. (Ported
  from Rapptz in v2.3.2.)
- **Resume.** After a disconnect, the client RESUMEs with its session ID and
  sequence number instead of identifying again. Missed events are then replayed,
  and the caches stay as they were. Measured live:
  - **v2.3.2:** a forced close gave 1 `on_resumed`, no new `on_ready`, the same
    22 guilds and 3,787 cached members.
  - **October 2026:** the session ID stayed the same.
- **Gateway fallback after a 5xx.** If the resume URL refuses the WebSocket
  upgrade with a 5xx, the client falls back to `wss://gateway.discord.gg/` and
  identifies again. curl_cffi reports the refused upgrade as `CurlError` 22,
  and `_refused_upgrade_status` reads the status from it. (Ported from Rapptz in
  v2.3.2, adapted to curl_cffi.)
- **Close codes.** These codes end the connection for good, with no reconnect:
  4004 (authentication failed), 4010, 4011, 4012, 4013 and 4014, and a close
  through `Client.close()`. Any other 1000 leads to a reconnect
  (`_can_handle_close`): one sent by Discord, or one the library sends itself
  before reconnecting after INVALID_SESSION or RECONNECT. Any other
  code, or a connection drop, leads to a reconnect, and to a resume when that's
  possible. A bad token normally fails **before** the gateway: the HTTP login
  gets a 401 and raises `LoginFailure('Improper token has been passed')`. 4004
  is the gateway's version of the same failure. See
  [Troubleshooting](troubleshooting.md#login-failures).
- **Your own sends** are throttled to 110 per 60 s (heartbeats skip the limit).
  According to Discord's user-API docs, Discord closes the connection with 4008
  for clients that send too fast.

## Events and frames

- **Frames** are decompressed and decoded (orjson when installed) in
  `received_message`, then dispatched to `ConnectionState.parse_*`. The
  stress benchmark's baseline ([`docs/cache_tuning.rst`](../docs/cache_tuning.rst)) puts this path at about 37,000 events per second on 4 vCPUs
  without handlers.
- **Hostile input.** A frame that decodes but carries hostile or malformed data
  is handled by the parsers, which log a warning and drop it. The stress
  benchmark's `bomb` phase sends 200k such frames and RSS stays bounded.
  **Known gap:** a frame that fails to decode as JSON raises `ValueError` out of
  the receive loop. `poll_event` doesn't catch it, and the benchmark catches it
  itself. A fix is planned.
- **Raw frames.** With `enable_debug_events=True`, `on_socket_raw_receive` and
  `on_socket_raw_send` get the raw frames. `on_socket_event_type` gets each
  event's type name.
