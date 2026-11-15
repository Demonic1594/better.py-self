# better.py-self wiki

In-depth guides to **better.py-self**, a drop-in fork of
[dolfies/discord.py-self](https://github.com/dolfies/discord.py-self) for
Discord **user accounts**. It keeps the same public API and adds bounded
caches, faster hot paths, credential-safe logging, hardening against broken
input, and features ported from [Rapptz/discord.py](https://github.com/Rapptz/discord.py).

The wiki explains how things work, why, and what to watch out for, with
examples you can run. The Sphinx docs in [`docs/`](../docs) are the API
reference: [`api.rst`](../docs/api.rst) for every class and method, and the
[changelog](../docs/whats_new.rst). Every statement here was checked against
this repository's code at **v2.3.3**. Where a fact comes from somewhere else,
such as Discord's behaviour, an upstream project or a live test, the page says
so.

These pages live in the repository's [`wiki/`](../wiki) folder and are
published to the GitHub Wiki on every merge to `master`. To change one, edit
the file in a pull request: edits made on the Wiki tab are overwritten.

> **Before you start:** automating a user account is against Discord's Terms
> of Service and can get the account disabled. Use a bot account for anything
> a bot can do, and a throwaway account while you experiment.

## Start here

| Page | What it covers |
|---|---|
| [Installation](installation.md) | Python versions, install methods, every dependency and extra, version and platform quirks, upgrading |
| [Getting started](getting-started.md) | Your token, a first client, `run` vs `start`, events, calling the API, shutting down, common first mistakes |
| [Tokens and login](tokens-and-login.md) | What login does step by step, how the client identifies itself, captchas, login errors, what gets accounts flagged |
| [Client options](client-options.md) | Every `Client(...)` option, its default, its quirks, and ready-made configurations |
| [Recipes](recipes.md) | Complete programs: message logs, keyword alerts, backups, member exports, multi-account runners and more |

## How it works

| Page | What it covers |
|---|---|
| [Architecture](architecture.md) | The main objects, the life of a gateway event, cache and models, `get_` vs `fetch_`, snowflakes, threads |
| [Events](events.md) | Registering handlers, ordering, cached vs raw events, opt-in events, every event with its arguments, `wait_for`, errors |
| [The gateway](gateway.md) | Connecting, compression, heartbeats, resume vs new session, close codes, opcodes, guild subscriptions |
| [Caching and memory](caching-and-memory.md) | What is cached, eviction order, sizing, `cache_stats()`, garbage collection |
| [Member lists](member-lists.md) | Getting members on a user account: chunking, sidebar scraping, searching, presences |
| [Persistence](persistence.md) | Snapshotting READY for warm restarts: format, replay, staleness |
| [Rate limits and pacing](rate-limits-and-pacing.md) | The retry loop, which exceptions you get, throttle visibility, pacing a user account |
| [Logging and credentials](logging-and-credentials.md) | Loggers and setup, what each level means, warnings worth knowing, redaction |

## Using the API

| Page | What it covers |
|---|---|
| [Messages](messages-and-channels.md) | Sending, files, editing, replies and forwards, reactions, history, search, read state, polls, embeds |
| [Channels and threads](channels-and-threads.md) | Channel types, permissions and overwrites, cloning, invites, DMs, group DMs, threads, forums, webhooks |
| [Guilds, members and roles](guilds-members-and-roles.md) | Guilds, members (yourself vs others), roles, permissions, bans, audit logs, emojis, scheduled events |
| [Users and relationships](users-and-relationships.md) | Users, editing your account, profiles, friends and requests, blocks, notes, connections |
| [Presence and activities](presence-and-activities.md) | Status, custom status, rich presence, Spotify, reading others' presences |
| [Settings](settings.md) | Protobuf user settings, client themes and `discord_protos`, notifications, read states |
| [Interactions and components](interactions-and-components.md) | Using bots' slash commands, buttons, selects and modals |
| [Soundboard and voice](soundboard-and-voice.md) | Voice connections, playback, FFmpeg, DAVE, calls, Go Live, the soundboard |
| [ext.commands and ext.tasks](commands-and-tasks.md) | Self-bot commands, converters, checks, cooldowns, errors, cogs, background loops |
| [Applications and billing](applications-and-billing.md) | Developer apps and teams, OAuth2, Nitro and payments, boosts, entitlements, experiments |

## Reference

| Page | What it covers |
|---|---|
| [Troubleshooting](troubleshooting.md) | Problems by symptom: login, captchas, flagged accounts, connections, missing events, errors, voice, memory |
| [Discord error codes](error-codes.md) | The exceptions the library raises, and the error codes a user account runs into |
| [Discord user API notes](discord-user-api-notes.md) | Background on Discord's user API: super properties, opcodes, close codes, limits |
| [Differences from upstream](differences-from-upstream.md) | Compared with discord.py-self and discord.py, porting bot code, what was ported and why not |
| [Development](development.md) | Repository layout, checks, tests, benchmarks, rules for changes, the wiki, releasing |

## Version

This wiki describes **v2.3.3**. See the [changelog](../docs/whats_new.rst) for
what changed, and the [roadmap](../docs/roadmap.rst) for what is planned.
