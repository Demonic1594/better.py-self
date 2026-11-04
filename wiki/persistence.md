# Persistence

Persistence writes the account's caches to disk and replays them on the next
start. A restarted process then has a warm cache straight away, without
re-scraping member lists.

```py
client = discord.Client(persistence_path='my_account_cache.bin')
```

[`examples/persistent_client.py`](../examples/persistent_client.py) is a complete version.

## When it writes

- **After READY:** the client writes a snapshot shortly after it becomes ready.
- **On a clean `close()`:** it writes **only if a capture is still pending**, for
  example when the READY write didn't happen. Each READY capture is written
  once, so cache changes after READY aren't saved by `close()`; the next READY
  takes a fresh capture. With `Client.run`, Ctrl+C cancels the run, and its
  `async with` block closes the client. `SIGTERM`, which is what `docker stop`
  and systemd send, isn't handled at all. Under such a supervisor, close the
  client from your own signal handler:

  ```py
  import asyncio, os, signal
  import discord

  token = os.environ['DISCORD_TOKEN']
  client = discord.Client(persistence_path='my_account_cache.bin')

  async def main():
      async with client:
          loop = asyncio.get_running_loop()
          loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(client.close()))
          await client.start(token)

  asyncio.run(main())
  ```

## How it writes

The code is `ConnectionState._write_persistence` in [`discord/state.py`](../discord/state.py).

- **Atomic.** The client writes a temporary file, `fsync`s it, `os.replace`s it
  into place and `fsync`s the directory. A crash halfway leaves the old snapshot,
  never a broken one.
- **Owner-only** (`0600`).
- **Never older over newer.** A generation counter stops a slow write from
  replacing a newer snapshot.
- **Compact.** It's one zlib stream of newline-separated JSON records, with a header
  first and then one record per guild.
- **Bounded.** A snapshot over **256 MiB** decompressed isn't written, and a
  warning is logged, because replay would reject it anyway.

## How it replays

Replay runs one guild at a time and gives other tasks a turn every few
milliseconds. Memory during replay tracks the largest single guild, not the
whole account, and other clients in the same process keep running.

The snapshot is **ignored**, and the client starts cold, when:

- it is older than `persistence_max_age` (14 days by default)
- it belongs to a different account
- it is corrupt or oversized

**It can be stale.** Replay restores the members that were cached at READY time.
A member who joined or left while the client was offline stays wrong until live
events or an explicit fetch correct it. Live traffic usually fixes that within
seconds.

## Measured

- **Live test, v2.3.2 (this fork's test accounts):** a 207 KB snapshot for a
  22-guild account. The next login replayed it and reached READY in 2.4 s with
  all 22 guilds.
- **October 2026 live validation**
  ([`docs/live_validation_2026-10.md`](../docs/live_validation_2026-10.md)):
  8 guilds restored from a 161 KiB snapshot in 20–59 ms. That run also found and
  fixed a crash that had made every warm restart fall back to a cold start.
- **Stress benchmark:**
  - 500k READY-time members gave a 9.2 MiB snapshot, replayed in about 4.5 s with
    a 128 MiB peak.
  - 200k members gave a 3.9 MiB snapshot, replayed in 1.54 s.

## Treat the file as a secret

The snapshot contains part of the gateway READY payload, filtered through
`_PERSIST_READY_KEYS`: your user, the cached users, relationships and private
channels. It also holds every guild's payload. Settings and tokens from READY
aren't written. Keep it out of version control, backups
you share and shared directories.
