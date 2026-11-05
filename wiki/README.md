# better.py-self wiki

These are in-depth guides to **better.py-self**, a drop-in fork of
[dolfies/discord.py-self](https://github.com/dolfies/discord.py-self) for
Discord **user accounts**. It keeps the same public API and adds bounded
caches, faster hot paths, credential-safe logging and features ported from
[Rapptz/discord.py](https://github.com/Rapptz/discord.py).

The wiki explains how things work and why. The Sphinx docs in [`docs/`](../docs)
are the API reference: [`api.rst`](../docs/api.rst) and the
[changelog](../docs/whats_new.rst). Every statement here was checked against
this repository's code at **v2.3.2**. Where a fact comes from somewhere else,
such as Discord's behaviour, an upstream project or a live test, the page says
so.

These pages live in the repository's [`wiki/`](../wiki) folder and are
published to the GitHub Wiki on every merge to `master`. To change one, edit
the file in a pull request: edits made on the Wiki tab are overwritten.

> **Before you start:** automating a user account is against Discord's Terms
> of Service and can get the account disabled. Use a bot account for anything
> a bot can do.

## Pages

| Page | What it covers |
|---|---|
| [Getting started](getting-started.md) | Installing, extras, your first client, tokens, multiple accounts |
| [Client options](client-options.md) | Every `Client(...)` option, its default and what it changes |
| [Events](events.md) | Every event, grouped, with the opt-in and raw events explained |
| [Caching and memory](caching-and-memory.md) | Cache caps, eviction, `cache_stats()`, memory numbers |
| [Member lists](member-lists.md) | Guild subscriptions, `fetch_members`, chunking, sidebar scraping |
| [Rate limits and pacing](rate-limits-and-pacing.md) | How 429s are handled for user accounts, throttle visibility, safe pacing |
| [The gateway](gateway.md) | Connecting, opcodes used, compression, resume and reconnect, heartbeats |
| [Persistence](persistence.md) | Snapshotting caches for warm restarts |
| [Logging and credentials](logging-and-credentials.md) | Setting up logging, what is redacted, token hygiene |
| [Soundboard and voice](soundboard-and-voice.md) | Soundboard sounds, voice channel effects, voice states, voice support |
| [Messages and channels](messages-and-channels.md) | Sending, uploads, polls, threads, webhooks, clones, components, interaction metadata |
| [ext.commands and ext.tasks](commands-and-tasks.md) | Self-bot commands, converters, background loops |
| [Differences from upstream](differences-from-upstream.md) | Compared with discord.py-self and with discord.py, and what is bot-only |
| [Troubleshooting](troubleshooting.md) | Captchas, flagged accounts, login failures, 403/20002, empty member lists, voice close codes |
| [Discord error codes](error-codes.md) | The error codes a user account runs into, what they mean, and how to read them from exceptions |
| [Discord user API notes](discord-user-api-notes.md) | Background on Discord's user API: opcodes, close codes, super properties, limits |
| [Development](development.md) | Checks, benchmarks, test harnesses, the release process |

## Version

This wiki describes **v2.3.2**. See the [changelog](../docs/whats_new.rst) for what changed,
and the [roadmap](../docs/roadmap.rst) for what is planned.
