# Getting started

## Requirements

- Python **3.10 or newer**. CI tests 3.10, 3.11, 3.12, 3.13 and 3.14.
- The package installs as `discord`, so you `import discord` exactly as with
  discord.py-self. Its distribution name is `discord_py_self`.
- This fork is **not on PyPI**. `pip install discord.py-self` installs
  dolfies' upstream package instead.

## Installing

```sh
# From the repository (latest master)
python3 -m pip install -U git+https://github.com/Demonic1594/better.py-self.git

# From a release wheel
python3 -m pip install -U https://github.com/Demonic1594/better.py-self/releases/download/v2.3.2/discord_py_self-2.3.2-py3-none-any.whl
```

### Extras

| Extra | Installs | Why |
|---|---|---|
| `[speed]` (recommended) | `orjson`, `mmh3`, `Brotli`, plus `zstandard` on Python 3.13 and older (3.14 has `compression.zstd` built in) and `aiodns` everywhere except Windows | Faster JSON, hashing, decompression and DNS. With orjson, gateway events are handled about 20% faster overall, and up to about 40% faster for small events such as typing and reactions. |
| `[voice]` | `PyNaCl`, `davey` | Voice support. `davey` provides DAVE, Discord's end-to-end voice encryption, which voice servers now require (see [Troubleshooting](troubleshooting.md#voice-close-codes)). |

```sh
python3 -m pip install -U "discord.py-self[voice,speed] @ git+https://github.com/Demonic1594/better.py-self.git"
```

Both extras are optional. Some of their packages have no prebuilt wheels on a
few platforms, such as Termux/Android, 32-bit ARM and PyPy, and would need a
compiler there. The library works the same without them: JSON falls back to
the stdlib `json` module, and CI runs the whole test suite both ways.

On Linux, voice also needs `libffi-dev` (or `libffi-devel`) and the Python
headers (`python3-dev`) before you install.

If voice support is missing, the client logs `PyNaCl is not installed, voice
will NOT be supported.` or `davey is not installed, voice will NOT be
supported.` at startup. Without voice support you can still join a voice
channel for presence (see [Soundboard and voice](soundboard-and-voice.md)), but
you can't send or receive audio.

## Your first client

```py
import discord

class MyClient(discord.Client):
    async def on_ready(self):
        stats = await self.cache_stats()
        print(f'Logged in as {self.user} ({len(self.guilds)} guilds, {stats.members} members cached)')

    async def on_message(self, message):
        if message.author != self.user:
            return
        if message.content == 'ping':
            await message.channel.send('pong')

client = MyClient()
client.run('token')
```

`Client.run` sets up logging for you (see [Logging and credentials](logging-and-credentials.md))
and closes the client cleanly on Ctrl+C. To manage the event loop yourself, use
`async with client:` and `await client.start(token)`.

### Your token

The library logs in with your account's token; you can't log in with an email
and password. The usual way to get the token is from the browser's developer
tools: open the Network tab, pick any request to `discord.com/api`, and copy its
`Authorization` header.

Treat the token like a password, because it is one:

- Don't paste it into code you commit. Read it from an environment variable or
  a file outside the repository.
- Upstream's maintainer has noticed that accounts get flagged more often when one
  token is used both in the desktop app and in the library at the same time, or
  when the token's IP address or client details change quickly. He recommends
  taking a fresh token from a private (incognito) browser session for the
  library (dolfies/discord.py-self#930).
- If a token leaks, change your password. That invalidates every existing token.

## Several accounts

Run them in **one process**. Each Python process pays a fixed 55–60 MB for the
interpreter and the imports. In one process, each extra account only costs
its own caches: about 16 MB for a 100-guild account with 10,000 cached
members, against 85–100 MB as a separate process.

```py
import asyncio
import discord

async def run_account(token):
    async with discord.Client() as client:
        await client.start(token)

async def main(tokens):
    # return_exceptions=True keeps the other accounts running if one fails
    results = await asyncio.gather(*(run_account(t) for t in tokens), return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            print(f'account {i} stopped: {result!r}')

asyncio.run(main(['token1', 'token2']))
```

[`examples/multiple_accounts.py`](../examples/multiple_accounts.py) is a complete
version. The stress benchmark's `multi` phase runs 10 accounts in one process.

## Examples

The [`examples/`](../examples) folder includes:

- `basic_bot.py`, `background_task.py` and `converters.py` for the extensions
- `cache_stats_monitor.py` for watching caches and backlog
- `persistent_client.py` for [persistence](persistence.md)
- `lane_governor.py` for [pacing](rate-limits-and-pacing.md)
- `captcha.py` for a [captcha handler](troubleshooting.md#captcha-required)
- `multiple_accounts.py`
- presence examples (`rich_presence.py`, `spotify_presence.py`)
- voice (`basic_voice.py`)
