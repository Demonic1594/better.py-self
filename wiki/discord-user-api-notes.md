# Discord user API notes

This is background on Discord's user API, the API the official client uses.
Discord doesn't publish documentation for it. The facts here come from the
community project **discord-userdoccers** ([docs.discord.food](https://docs.discord.food)),
cross-checked against what this library does. Where the two differ, the page
says so. Discord can change any of this without notice.

## Super properties

Every request to Discord's main API carries `X-Super-Properties`, a base64-encoded JSON object
describing the client: OS, browser, browser version, `client_build_number`,
release channel, locale and so on. The same object is sent as `properties` in
gateway IDENTIFY. According to userdoccers, it's "not required" but "highly
recommended due to its significance in anti-abuse systems". Many experiments
also need a recent `client_build_number`, and `browser_user_agent` should match
the `User-Agent` header.

This library builds the object to match the official web client. At startup it
tries to fetch a current build number and browser version, and falls back to
built-in values if those lookups fail (`HeadersContext`). The gateway handshake,
CDN and cloud-upload requests and the webhook adapter use their own headers,
without super properties.

The October 2026 live validation
([`docs/live_validation_2026-10.md`](../docs/live_validation_2026-10.md)) caught
a regression where every request had been sending `client_build_number=9999`.
No real client sends that value. It was fixed and verified at build 634304 with
Chrome 156.

For the same reason, the parts of requests that copy the official client are
deliberately kept as they are: the headers, the super properties, and
parameters such as `with_expiration=true`. They aren't up for removal as
"unnecessary".

## Gateway opcodes

From userdoccers, the opcodes a client can send:

| Op | Name | Op | Name |
|---|---|---|---|
| 1 | Heartbeat | 22 | Stream Set Paused |
| 2 | Identify | 28 | Request Forum Unreads |
| 3 | Presence Update | 29 | Remote Command |
| 4 | Voice State Update | 30 | Request Deleted Entity IDs |
| 5 | Voice Server Ping | 31 | Request Soundboard Sounds |
| 6 | Resume | 32/33 | Speed Test Create/Delete (deprecated) |
| 8 | Request Guild Members | 34 | Request Last Messages |
| 13 | Call Connect | 35 | Search Recent Members |
| 14 | Guild Subscriptions (deprecated) | 36 | Request Channel Statuses (deprecated, see 43) |
| 17 | Lobby Voice States | 37 | Guild Subscriptions Bulk |
| 18 | Stream Create | 38 | Guild Channels Resync |
| 19 | Stream Delete | 39 | Request Channel Member Count |
| 20 | Stream Watch | 40 | QoS Heartbeat |
| 21 | Stream Ping | 41 | Update Time Spent Session ID |
|  |  | 42/43 | Lobby Voice Server Ping / Request Channel Info |

Discord sends: 0 Dispatch, 7 Reconnect, 9 Invalid Session, 10 Hello and 11
Heartbeat ACK. Op 12 (Guild Sync) is gone.

[The gateway](gateway.md#opcodes-this-library-sends) lists the opcodes this
library sends.

### IDENTIFY

From userdoccers:

- `token`, `properties` (the super properties), `presence` (only useful when
  re-identifying), `compress`, `capabilities` and `client_state`
- `large_threshold`, which defaults to 250 for users
- for user accounts, all intents are on by default

The library sends capabilities **`0x57fd`**, which userdoccers' examples also use.

### Close codes

| Code | Meaning (userdoccers) | What this library does |
|---|---|---|
| 4000 | Unknown error | Reconnects and resumes. The library itself also closes a stalled socket with 4000. |
| 4001 / 4002 / 4003 / 4005 | Unknown opcode / decode error (or a payload over 15 KiB) / not authenticated / already authenticated | Reconnects |
| **4004** | Authentication failed | **Stops.** The token is bad. |
| 4007 / 4009 | Invalid sequence / session timed out | Forgets the session and identifies again straight away (`_NON_RESUMABLE_CLOSE_CODES`), with no RESUME attempt. |
| 4008 | Rate limited (sending too fast) | Reconnects. The library throttles its own sends to avoid this. |
| **4010 – 4014** | Invalid shard, sharding required, invalid API version, invalid or disallowed intents | **Stops** |
| 4015 | Too many user sessions | Reconnects (close other sessions) |
| 1000 / 1001 | Normal close | Only a close through `Client.close()` stops the client. A 1000 or 1001 from Discord leads to a reconnect that **tries to RESUME**. If Discord refuses, an INVALID_SESSION makes the client identify again. (The library also sends a 1000 itself on INVALID_SESSION or RECONNECT, then reconnects.) |

This is Discord's side, according to userdoccers. When a client closes with 1000
or 1001, Discord ends the session. Any other code, or a plain TCP drop, keeps it
resumable for a few minutes. That's why the library closes a stalled socket with
4000.

### Transport compression

`?compress=zlib-stream` or `zstd-stream`. The library uses zstd when a decoder
is available. Payload compression (`compress: true` in IDENTIFY) is only used when
there is no transport compression, and the two can't be combined.

## Rate limits

From userdoccers:

- **Global limit:** 50 requests per second per user.
- **Headers:** user requests usually get only `Retry-After`, `X-RateLimit-Global`
  and `X-RateLimit-Scope` (`user`, `global` or `shared`), and only on a 429.
- **Cloudflare:** 10,000 invalid requests (401, 403 or 429) in 10 minutes bring a
  **24-hour IP ban**. Shared-scope 429s don't count.
- **Gateway:**
  - presence updates: 5 per 20 s
  - Request Guild Members (op 8): 1 per guild per session per 30 s
  - payloads: at most 15 KiB

[Rate limits and pacing](rate-limits-and-pacing.md) covers how the library
handles these, and the pacing that held up live.

## Member lists

From userdoccers:

- **Op 8:** listing a whole guild needs `MANAGE_ROLES`, `KICK_MEMBERS` or
  `BAN_MEMBERS`. A prefix query returns at most 100 members, and each chunk holds
  up to 1,000.
- **Op 35** searches the 10,000 most recently joined members.
- **Op 14** is deprecated in favour of **op 37** bulk subscriptions. The
  library uses op 37.

See [Member lists](member-lists.md).

## Soundboard

From userdoccers:

- **Op 31** `{"guild_ids": [...]}` brings one Soundboard Sounds event per guild.
- **Sending a sound:**
  - **Endpoint:** `POST /channels/{id}/send-soundboard-sound` with `sound_id` and
    an optional `source_guild_id`. It returns 204.
  - **Voice state:** you must be connected, and not server muted, deafened or suppressed.
  - **Default sounds:** everyone can use them.
  - **Guild sounds:** usable in their own guild. Nitro subscribers can use them
    in every guild.
- **Upload limits:** 512 KiB and 5.2 s. Names are 2–32 characters, volume 0–1.

See [Soundboard and voice](soundboard-and-voice.md).

## Things that are bot-only

Found live with this library:

- **`GET /guilds/{id}/voice-states/@me` and `/voice-states/{user_id}`** answer
  403, code 20002, to user accounts.

From Discord's design:

- application command registration, interaction responses, sending components,
  entitlements and SKUs, and sharding

## Error codes worth knowing

[Discord error codes](error-codes.md) covers many more, with links to the full
community-maintained list.

| Code | Meaning |
|---|---|
| 20002 | Only bots can use this endpoint |
| 40333 | Blocked by Cloudflare |
| 10008 with HTTP 403 | Blocked by anti-abuse, despite the "Unknown Message" text |
| -1 with `captcha_key` | Captcha required. The library raises `CaptchaRequired`. |

## What userdoccers doesn't cover

At the time of the research, userdoccers had **no page on the captcha flow**
(`/resources/captcha` returned 404), so the captcha section of
[Troubleshooting](troubleshooting.md#captcha-required) comes from this library's
code and the upstream issue tracker instead.
