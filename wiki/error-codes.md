# Discord error codes

When a request fails, Discord usually returns a JSON error with a numeric `code`
and a `message`. Cloudflare and some other failures return plain text instead.
The library raises an `HTTPException`, or one of its subclasses `Forbidden` (403),
`NotFound` (404), `DiscordServerError` (5xx) or `CaptchaRequired`. On every one of
them:

- `exc.status` is the HTTP status
- `exc.code` is Discord's error code (`0` if there was none, for example a text response)
- `exc.text` is the message, with field errors appended, or the text body
- `exc.json` is the JSON error body. For a text response it's a stand-in with
  code `0`, not a real body.

`RateLimited` is separate: it isn't an `HTTPException` and has none of these
attributes (see [Troubleshooting](troubleshooting.md#other-discord-error-codes)).

```py
try:
    await channel.send('hi')
except discord.Forbidden as exc:
    if exc.code == 50007:       # Cannot send messages to this user
        ...
    elif exc.code == 340002:    # sending DMs has been limited for this account
        ...
```

The tables below are the codes a **user account** is most likely to see. They
are a curated subset of the community-maintained
[Discord error codes list](https://gist.github.com/Dziurwa14/de2498e5ee28d2089f095aa037957cbb),
which is collected from Discord's client and from
[userdoccers](https://docs.discord.food/datamining/errors) and covers every known
code across about 40 categories. Discord adds and changes codes without notice,
so check the full list when you meet one that isn't here.

## Account and anti-abuse

These are the ones to watch on an automated account. Several mean Discord
has restricted the account.

| Code | Message | What it means for you |
|---|---|---|
| -1 (with `captcha_key`) | Captcha required | Raised as `CaptchaRequired`. See [Troubleshooting](troubleshooting.md#captcha-required). |
| 40001 | Unauthorized | The token is invalid. At login, this is `LoginFailure`. |
| 40002 | You need to verify your account in order to perform this action | The account needs a verified email or phone number. |
| 40003 | You are opening direct messages too fast | Slow down opening DMs. |
| 40013 | This account must be claimed. | The account has no email and password set. |
| 50178 | The user account must first be verified | As 40002. |
| 60003 | Two factor is required for this operation | The action needs 2FA. Moderating in guilds that require 2FA also needs it. |
| 70004 | This phone number was recently used on a different account | Phone verification is blocked. |
| 20024 | Under minimum age | The account is age-restricted. |
| 340001–340018 | Access to sending messages / DMs / attachments / friend requests / joining or creating servers ... has been limited for the user | **The account is restricted.** Each code names what is limited, for example 340002 for DMs, 340007 for friend requests and 340015 for joining servers. Stop the automation that triggered it. |
| 40333 | Cloudflare access denied (code: 1020) | The IP was blocked by Cloudflare. See [Rate limits](rate-limits-and-pacing.md). |
| 10008 with HTTP 403 | Unknown Message | According to userdoccers, a 403 with this code means Discord's anti-abuse system blocked the request, despite the text. |
| 20002 | Only bots can use this endpoint | A bot-only endpoint, such as the voice-state REST routes. `Member.fetch_voice` falls back to the cache for this reason. |
| 20001 | Bots cannot use this endpoint | The reverse case. You'd only see it with a bot token. |

## Rate limits and slowmode

| Code | Message | Notes |
|---|---|---|
| 31001 | You are being rate limited | Usually comes with a 429, which the library waits out. |
| 31002 | The resource is being rate limited | |
| 20016 | This action cannot be performed due to slowmode rate limit | The channel's slowmode. Pace per channel as well as per lane. |
| 20028 | The write action you are performing on the channel has hit the write rate limit. | A per-channel write limit. |
| 40062 | Service resource is being rate limited. | |
| 30046 | Maximum number of edits to messages older than 1 hour reached. | A cap on editing old messages. |

## Messages, DMs and relationships

| Code | Message |
|---|---|
| 50007 | Cannot send messages to this user (DMs closed, or no mutual server) |
| 50033 | Invalid Recipient(s) |
| 50006 | Cannot send an empty message |
| 50005 | Cannot edit a message authored by another user |
| 50008 | Cannot send messages in a non-text channel |
| 50016 | Provided too few or too many messages to delete. Must provide at least 2 and fewer than 100 messages to delete. |
| 20009 | Explicit content cannot be sent to the desired recipient(s) |
| 240000 | Message blocked by harmful links filter |
| 30010 | Maximum number of reactions reached (20) |
| 90001 | Reaction was blocked |
| 80001 | Friend request blocked |
| 80003 | Cannot send friend request to self |
| 80004 | No users with DiscordTag exist |

## Permissions and access

| Code | Message |
|---|---|
| 50001 | Missing Access (you can't see the channel or guild) |
| 50013 | Missing Permissions |
| 50024 | Cannot execute action on this channel type |
| 50038 | You cannot perform this action on yourself |
| 50095 | This server is not available in your location |
| 40007 | The user is banned from this guild |
| 50009 | Channel verification level is too high |

## Limits

| Code | Message |
|---|---|
| 30001 | Maximum number of guilds reached (100). The limit is higher with Nitro. |
| 30003 | Maximum number of pins reached for the channel (250) |
| 30007 | Maximum number of webhooks reached (10) |
| 30013 | Maximum number of guild channels reached (500) |
| 30015 | Maximum number of attachments in a message reached (10) |
| 50045 | File uploaded exceeds the maximum size |
| 40005 | Request entity too large |

## Unknown objects

These usually mean an ID is wrong or the object was deleted:

| Code | Message |
|---|---|
| 10003 | Unknown Channel |
| 10004 | Unknown Guild |
| 10006 | Unknown Invite |
| 10008 | Unknown Message (but see the 403 case above) |
| 10011 | Unknown Role |
| 10013 | Unknown User |
| 10014 | Unknown Emoji |
| 10015 | Unknown Webhook |
| 10026 | Unknown Ban |
| 10070 | Unknown Guild Scheduled Event |
| 10077 | Endpoint not available |
| 10097 | Unknown sound (soundboard) |

## Request problems

| Code | Message | Notes |
|---|---|---|
| 50035 | Invalid Form Body | `exc.text` lists the fields that failed and why. |
| 50109 | The request body contains invalid JSON. | |
| 50041 | Invalid API version provided | The library uses API v9. |
| 40006 | This feature has been temporarily disabled | Discord's side. |
| 40016 | Service is currently unavailable. | Discord's side. |
