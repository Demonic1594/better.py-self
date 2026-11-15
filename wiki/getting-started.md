# Getting started

From nothing to a running client: installing, the token, a first client in
both styles, how `run`, `start` and `async with` differ, reacting to events,
calling the API, shutting down cleanly, several accounts, and the mistakes
most people make in their first hour.

> **Before you start:** automating a user account is against Discord's Terms
> of Service and can get the account disabled. Use a bot account for anything
> a bot can do, and use a throwaway account while you experiment. See
> [Tokens and login](tokens-and-login.md) for what raises the risk.

## Install

```sh
python3 -m pip install -U "discord.py-self[speed] @ git+https://github.com/Demonic1594/better.py-self.git"
```

Python 3.10 or newer. The package imports as `discord`. It is not on PyPI:
`pip install discord.py-self` installs dolfies' upstream package instead.
[Installation](installation.md) covers release wheels, extras, voice and every
dependency.

## Your token

The library logs in with your account's **token**; it can't log in with an
email and password. The usual way to get it is from the browser's developer
tools: open the Network tab, pick any request to `discord.com/api`, and copy
its `Authorization` header.

Keep the token out of your code:

```sh
export DISCORD_TOKEN='...'        # Windows PowerShell: $env:DISCORD_TOKEN = '...'
```

```python
import os
token = os.environ['DISCORD_TOKEN']
```

The token is a password. Anyone who has it is logged in as you. Changing your
password invalidates every token. [Tokens and login](tokens-and-login.md) has
more, including how to lower the chance of the account being flagged.

## A first client

There are two equivalent styles. Use whichever reads better to you.

### Subclassing `Client`

```python
import os
import discord


class MyClient(discord.Client):
    async def on_ready(self):
        print(f'Logged in as {self.user} (ID {self.user.id}) in {len(self.guilds)} guilds')

    async def on_message(self, message: discord.Message):
        # Only react to your own messages, or the client answers everyone
        if message.author != self.user:
            return
        if message.content == '!ping':
            await message.channel.send(f'pong ({self.latency * 1000:.0f} ms)')


client = MyClient()
client.run(os.environ['DISCORD_TOKEN'])
```

### Decorators

```python
import os
import discord

client = discord.Client()


@client.event
async def on_ready():
    print(f'Logged in as {client.user}')


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user and message.content == '!ping':
        await message.channel.send('pong')


client.run(os.environ['DISCORD_TOKEN'])
```

`@client.event` registers the function under its own name, so the name must be
the event's name (`on_message`, `on_ready`). It must be an `async def`;
anything else raises `TypeError: event registered must be a coroutine function`.
There's one handler per event name: registering a second `on_message`
replaces the first. To have several, use [`commands.Bot`](commands-and-tasks.md)
listeners or call your functions from one handler.

### What you see when it starts

`run()` sets up logging at INFO, so a normal start prints something like:

```text
[INFO    ] discord.client: Logging in using static token.
[INFO    ] discord.http: Found user agent "Mozilla/5.0 ...", build number 4xxxxx.
[INFO    ] discord.http: Found TLS fingerprint target "chrome1xx".
[INFO    ] discord.gateway: Connected to Gateway (Session ID: ...).
Logged in as you (ID ...) in 42 guilds
```

The user agent and build number are fetched at login so the client looks like
the current Discord web client; see [Tokens and login](tokens-and-login.md).

## `run`, `start` and `async with`

| | What it does | Use it when |
|---|---|---|
| `client.run(token)` | Creates an event loop, sets up logging, logs in, connects, and blocks until the client closes. Ctrl+C closes the client cleanly and returns. | A script whose only job is this one client. |
| `await client.start(token)` | `login()` then `connect()`, inside your own event loop. Doesn't touch logging. Returns when the connection ends. | You already run an event loop (a web server, several clients, other tasks). |
| `async with client:` | Prepares the client for the loop on entry and closes it on exit, even on an exception. | Always wrap `start()` in it. |

The `asyncio` form of the first example:

```python
import asyncio
import logging
import os
import discord


async def main():
    discord.utils.setup_logging(level=logging.INFO)   # start() doesn't do this for you
    client = MyClient()
    async with client:
        await client.start(os.environ['DISCORD_TOKEN'])


asyncio.run(main())
```

Gotchas:

- **`run()` inside a running event loop fails** (`asyncio.run() cannot be called
  from a running event loop`), for example in Jupyter. Use `await client.start(...)` there.
- **A failed login raises out of `run()`** as `discord.LoginFailure` (a wrong
  or empty token). Ctrl+C is the only exception `run()` swallows.
- **`run()` configures the `discord` logger**, not the root logger, unless
  you pass `root_logger=True`. Pass `log_handler=None` to configure logging
  yourself. See [Logging and credentials](logging-and-credentials.md).

### `setup_hook`

`setup_hook` runs once, after the token is accepted and before the gateway
connects. It's the place for async setup: loading a database, starting
background tasks, loading extensions.

```python
class MyClient(discord.Client):
    async def setup_hook(self):
        self.started_at = discord.utils.utcnow()
        self.bg = asyncio.create_task(self.heartbeat())

    async def heartbeat(self):
        await self.wait_until_ready()
        while not self.is_closed():
            print('still alive, latency', self.latency)
            await asyncio.sleep(60)
```

> **Gotcha:** don't `await self.wait_until_ready()` or `self.wait_for(...)`
> *inside* `setup_hook` itself: the gateway connects only after `setup_hook`
> returns, so it would wait forever. Start a task that waits, as above.

## Events

Handlers are named `on_<event>`. The common ones:

```python
class MyClient(discord.Client):
    async def on_ready(self):                       # caches are filled; fires again after a full reconnect
        ...

    async def on_message(self, message):            # every message you can see
        ...

    async def on_message_edit(self, before, after): # only for cached messages
        ...

    async def on_raw_message_edit(self, payload):   # every edit, cached or not
        ...

    async def on_reaction_add(self, reaction, user):
        ...

    async def on_relationship_add(self, relationship):  # a friend request or a new friend
        ...
```

**Cached and raw events.** Most events need the object in the cache; the
`on_raw_*` versions fire either way and give you IDs plus whatever is cached.
The cache is bounded by default (see [Caching and memory](caching-and-memory.md)),
so use raw events when you must not miss one. [Events](events.md) lists every
event.

**`on_ready` can fire more than once.** It fires after every *new* session.
A brief disconnect usually *resumes* the session instead (`on_resumed`,
no new `on_ready`), but a long one starts a new session. Guard one-time setup:

```python
async def on_ready(self):
    if getattr(self, '_did_setup', False):
        return
    self._did_setup = True
    ...
```

Or put it in `setup_hook`, which runs once.

### Waiting for something

`wait_for` waits for the next event that passes a check:

```python
@client.event
async def on_message(message):
    if message.author != client.user or message.content != '!confirm':
        return
    await message.channel.send('Reply "yes" within 30 seconds.')

    def check(m: discord.Message) -> bool:
        return m.channel == message.channel and m.author == message.author and m.content.lower() == 'yes'

    try:
        reply = await client.wait_for('message', check=check, timeout=30)
    except asyncio.TimeoutError:
        await message.channel.send('Timed out.')
    else:
        await reply.add_reaction('\N{WHITE HEAVY CHECK MARK}')
```

The event name has no `on_` prefix. For events with several arguments,
`check` gets all of them and `wait_for` returns them as a tuple:
`before, after = await client.wait_for('message_edit', check=lambda b, a: a.id == msg_id)`.

### Errors in handlers

An exception in a handler doesn't stop the client. It goes to `on_error`, which
by default logs `Ignoring exception in on_message` with the traceback, through
the `discord.client` logger. Override it to report errors elsewhere:

```python
import sys, traceback

class MyClient(discord.Client):
    async def on_error(self, event_method, /, *args, **kwargs):
        exc = sys.exc_info()[1]
        print(f'{event_method} failed: {exc!r}')
        traceback.print_exc()
```

## Calling the API

Two families of methods:

- **`get_*`** (`get_channel`, `get_guild`, `get_user`, `guild.get_member`) look
  in the cache. They are synchronous, free, and return `None` when the object
  isn't cached.
- **`fetch_*`** (`fetch_channel`, `fetch_user`, `guild.fetch_member`) make an
  HTTP request. They are `async`, always up to date, raise `NotFound` or
  `Forbidden`, and count against rate limits.

```python
async def send_to(client: discord.Client, channel_id: int, text: str):
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    try:
        return await channel.send(text)
    except discord.Forbidden as exc:
        print('no permission:', exc.code, exc.text)
    except discord.HTTPException as exc:
        print('failed:', exc.status, exc.code, exc.text)
```

To send to a channel by ID without looking it up at all, use
`client.get_partial_messageable(channel_id).send(...)`; the request fails if
the ID is wrong.

> **Gotcha: the cache is empty before `on_ready`.** `get_channel` returns
> `None` until READY has been processed, and even after it, a member or
> message may have been evicted. Code that "works on my small account" can
> fail on a big one; fall back to `fetch_*` where it matters.

Pace what you send. User accounts are rate-limited harder than bots, and
bursts are what gets accounts flagged; see
[Rate limits and pacing](rate-limits-and-pacing.md).

## Configuring the client

Every option is a keyword argument to `Client(...)`:

```python
client = MyClient(
    status=discord.Status.idle,               # presence at login
    max_messages=5000,                        # bigger message cache
    max_member_cache=2000,                    # fewer members per guild
    chunk_guilds_at_startup=False,            # faster on_ready on big accounts
    enable_raw_typing=True,                   # opt in to on_raw_typing
)
```

A misspelt option isn't silently ignored: it logs
`Ignoring unknown Client option(s): max_member_cahce. Check them for typos.`
[Client options](client-options.md) lists every option.

> **Gotcha: code written for discord.py.** `Client(intents=...)` is a bot
> concept. User accounts have no intents, so this fork logs
> `Ignoring unknown Client option(s): intents` and carries on. Remove it.

## Shutting down

- **Ctrl+C** under `run()` closes the client cleanly.
- **From code:** `await client.close()`. It disconnects voice, writes the
  persistence snapshot if one is configured, closes the gateway with code 1000
  and closes the HTTP session. Calling it twice is safe.
- **Handlers still running are not cancelled.** If any are, `close()` logs
  `N event handler(s) were still running when the client closed.` HTTP requests
  they make afterwards fail with curl_cffi's `SessionClosed`. Stop your own
  background tasks before closing.

```python
class MyClient(discord.Client):
    async def on_message(self, message):
        if message.author == self.user and message.content == '!quit':
            await message.channel.send('bye')
            await self.close()   # the handler calling close() isn't counted as "still running"
```

A closed client can't `start()` again: create a new one, or call
`client.clear()` first.

## Several accounts

Run them in **one process**. Each Python process costs a fixed 55 to 60 MB for
the interpreter and the imports; in one process, each extra account only costs
its own caches (about 16 MB for a 100-guild account with 10,000 cached members,
against 85 to 100 MB as a separate process).

```python
import asyncio
import logging
import os
import discord


class AccountClient(discord.Client):
    async def on_ready(self):
        print('ready:', self.user)


async def run_account(token: str):
    async with AccountClient() as client:
        await client.start(token)


async def main(tokens):
    discord.utils.setup_logging(level=logging.INFO)
    # return_exceptions=True keeps the other accounts running if one fails
    results = await asyncio.gather(*(run_account(t) for t in tokens), return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            print(f'account {i} stopped: {result!r}')


asyncio.run(main(os.environ['DISCORD_TOKENS'].split(',')))
```

Each client has its own HTTP session, gateway connection, caches and rate
limit state. They share only process-wide things: the one-time voice warnings
and the garbage-collector freeze (which is released when the last client
releases it). [`examples/multiple_accounts.py`](../examples/multiple_accounts.py)
is a complete version.

## Common first mistakes

| Mistake | What happens | Fix |
|---|---|---|
| Not checking `message.author` | The client replies to everyone, or to itself in a loop. | Return early unless `message.author == self.user` (or your allow-list). |
| `time.sleep()` or `requests.get()` in a handler | The whole client freezes: no events, heartbeats stop, Discord drops the connection. | `await asyncio.sleep()`; use an async HTTP client, or `await asyncio.to_thread(blocking_fn)`. |
| `change_presence()` before the gateway is up (in `setup_hook`) | `ClientException: change_presence() needs a connected gateway...` | Pass `status=` / `activity=` to `Client(...)`, or call it in `on_ready`. |
| `client.get_channel()` at import time or in `setup_hook` | Returns `None`: nothing is cached yet. | Use it after `on_ready`, or `fetch_channel`. |
| Passing both `activity=` and `activities=` | `activity` is silently ignored at login (and `change_presence` raises `TypeError`). | Pass one. |
| Writing `def on_message` (no `async`) | `TypeError: event registered must be a coroutine function` | `async def`. |
| `intents=discord.Intents...` | A warning about an unknown option. | Remove it. |
| Sending in a tight loop | 429s, captchas, then a flagged account. | Pace sends; see [Rate limits and pacing](rate-limits-and-pacing.md). |
| Sharing your token in a bug report or log | Account takeover. | The library redacts tokens in its logs; check your own prints. |

## Next steps

| To... | Read |
|---|---|
| Understand how the pieces fit | [Architecture](architecture.md) |
| Know every event | [Events](events.md) |
| Send messages, files, embeds and polls | [Messages](messages-and-channels.md) |
| Work with channels, threads and DMs | [Channels and threads](channels-and-threads.md) |
| Write commands | [ext.commands and ext.tasks](commands-and-tasks.md) |
| Run on a big account | [Caching and memory](caching-and-memory.md), [Member lists](member-lists.md) |
| Avoid getting flagged | [Rate limits and pacing](rate-limits-and-pacing.md), [Tokens and login](tokens-and-login.md) |
| See complete programs | [Recipes](recipes.md) and [`examples/`](../examples) |
| Fix something | [Troubleshooting](troubleshooting.md) |
