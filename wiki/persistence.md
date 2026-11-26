# Persistence

Persistence writes the account's READY-time state to disk and replays it on
the next start. A restarted process then has its guilds, channels, members and
DMs in the cache before the gateway has even connected, without waiting for
member lists to be scraped again. It's off by default.

```python
client = discord.Client(persistence_path='cache/my_account.bin')
```

[`examples/persistent_client.py`](../examples/persistent_client.py) is a
complete version. The options are `persistence_path` (no default location)
and `persistence_max_age` (14 days).

## When it writes

- **After READY.** Once startup is complete (after member lists are loaded),
  the client writes a snapshot of what READY delivered.
- **On demand.** `client.write_persistence()` writes the pending capture now;
  it returns `False` when there's nothing pending.
- **On `close()`, only if a capture is still pending,** for example when the
  READY write didn't happen yet. Each READY capture is written once, so cache
  changes made after READY are **not** saved by `close()`; the next READY takes
  a fresh capture.

With `Client.run`, Ctrl+C closes the client. `SIGTERM`, which is what
`docker stop` and systemd send, isn't handled at all. Under such a supervisor,
close the client from your own signal handler:

```python
import asyncio, os, signal
import discord

client = discord.Client(persistence_path='cache/my_account.bin')


async def main():
    async with client:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(client.close()))
        await client.start(os.environ['DISCORD_TOKEN'])


asyncio.run(main())
```

(`loop.add_signal_handler` isn't available on Windows; there, rely on Ctrl+C.)

## What is in the file

- A **header**: the format version (`2`), when it was saved, your user ID,
  and the parts of READY listed in `_PERSIST_READY_KEYS`: `user`, `users`,
  `relationships` and `private_channels`.
- One **record per guild**: the guild's READY-time payload (channels, roles,
  emojis, the members and presences that came with it, threads).

Not in it: settings, read states, experiments, sessions, the auth token, and
anything that changed after READY.

The file is one zlib stream (level 1) of newline-separated JSON records.

## How it writes

The code is `ConnectionState._write_persistence` in
[`discord/state.py`](../discord/state.py).

- **Atomic.** The client writes a temporary file (`.persist-*` in the same
  directory), `fsync`s it, `os.replace`s it into place and `fsync`s the
  directory. A crash halfway leaves the old snapshot, never a broken one.
- **Owner-only** (`0600`).
- **Never older over newer.** A generation counter stops a slow write from
  replacing a newer snapshot.
- **Bounded.** A snapshot over **256 MiB** decompressed isn't written, and the
  client logs `Not writing the cache persistence file: the snapshot is over the 256 MiB limit replay accepts ... Persistence is effectively disabled for this account.`
  Raising the caps doesn't help here; the snapshot holds READY data, which
  depends on the account.

## How it replays

Replay runs inside `login()`, after the token is verified and **before**
`setup_hook`, so `setup_hook` already sees the cached guilds.

- **Streamed.** The file is read and replayed one guild at a time, and the
  client yields to other tasks every 10 ms. Memory during replay tracks the
  largest single guild, not the whole account, and other clients in the same
  process keep running.
- **Reconciled.** When the fresh READY arrives, guilds that are in both are
  updated in place, and replayed guilds that READY doesn't list (you left
  them while offline) are removed.
- **Not re-saved.** A replayed READY isn't captured again, so closing during
  replay can't overwrite a good snapshot with an empty one.

What you see in the logs:

| Log | Level | Meaning |
|---|---|---|
| `Replayed cache persistence: N guilds loaded.` | INFO | It worked. |
| `Cache persistence file is stale; performing a cold start.` | INFO | Older than `persistence_max_age`. |
| `Cache persistence file belongs to a different account; ignoring it.` | WARNING | Another account's file at that path. Give each account its own path. |
| `Cache persistence file is corrupt; ignoring it.` | WARNING | Unreadable, truncated, or over the size limit. It will be overwritten after the next READY. |
| `Persistence snapshot failed to replay; performing a cold start.` | WARNING | Replay raised partway; the client starts cold. |
| (nothing) | | A snapshot from an incompatible format version is ignored silently, and replaced after the next READY. |

## Staleness

Replay restores what was true at the last READY. A member who joined or left,
a role that changed, or a channel created while the client was offline stays
wrong until live events or an explicit fetch correct it. The fresh READY fixes
the guild list and the guild-level data within seconds of connecting;
membership details catch up as events arrive. Don't use replayed data for
decisions that must be exact at startup.

## Several accounts

Each account needs its own file. With many accounts in one process:

```python
def make_client(account: str) -> discord.Client:
    return discord.Client(persistence_path=f'cache/{account}.bin', persistence_max_age=3 * 24 * 3600)
```

A file that belongs to another account is ignored (with a warning) rather
than loaded into the wrong client.

## Measured

- **A 22-guild account:** a 207 KB snapshot. The next login replayed it and
  reached READY in 2.4 s with all 22 guilds.
- **October 2026 live validation**
  ([`docs/live_validation_2026-10.md`](../docs/live_validation_2026-10.md)):
  8 guilds restored from a 161 KiB snapshot in 20 to 59 ms. That run also
  found and fixed a crash that had made every warm restart fall back to a cold
  start.
- **Stress benchmark:**
  - 500k READY-time members gave a 9.2 MiB snapshot, replayed in about 4.5 s
    with a 128 MiB peak.
  - 200k members gave a 3.9 MiB snapshot, replayed in about 1.5 s.

## Treat the file as a secret

The snapshot holds your user object (email and phone included if READY carried
them; the allow-list filters top-level READY keys, not the fields inside
`user`), your relationships, your DMs and every guild's member data. It has no
token. Keep it out of version control, out of backups you share and out of
shared directories, and delete it when you stop using an account.
