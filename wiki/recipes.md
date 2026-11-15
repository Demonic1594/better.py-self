# Recipes

Complete, runnable programs for common jobs. Each reads the token from the
`DISCORD_TOKEN` environment variable and uses only the APIs described on the
other pages. Adapt the IDs, and keep the pacing: these run on your account.

- [Log deleted and edited messages](#log-deleted-and-edited-messages)
- [Keyword alerts to a webhook](#keyword-alerts-to-a-webhook)
- [A self-bot with paced commands](#a-self-bot-with-paced-commands)
- [Back up a channel to JSON Lines](#back-up-a-channel-to-json-lines)
- [Download every attachment in a channel](#download-every-attachment-in-a-channel)
- [Export a guild's member list to CSV](#export-a-guilds-member-list-to-csv)
- [Track friends' status changes](#track-friends-status-changes)
- [A rotating custom status](#a-rotating-custom-status)
- [Several accounts with warm restarts](#several-accounts-with-warm-restarts)
- [A health endpoint next to the client](#a-health-endpoint-next-to-the-client)
- [Graceful shutdown under Docker or systemd](#graceful-shutdown-under-docker-or-systemd)

The [`examples/`](../examples) folder has more, including a
[pacing governor](../examples/lane_governor.py), a
[cache monitor](../examples/cache_stats_monitor.py),
[rich presence](../examples/rich_presence.py) and
[voice playback](../examples/basic_voice.py).

## Log deleted and edited messages

Uses raw events so it works for messages the cache no longer holds, and a
bigger message cache so most deletes still have their content.

```python
import logging
import os

import discord

log = logging.getLogger('audit')


class Watcher(discord.Client):
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        msg = payload.cached_message
        if msg is None:
            log.info('deleted uncached message %s in channel %s', payload.message_id, payload.channel_id)
        else:
            log.info('deleted %s by %s in #%s: %r', msg.id, msg.author, msg.channel, msg.content)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        before = payload.cached_message
        after = payload.message
        if after is None or before is None or before.content == after.content:
            return  # an embed-only update, or nothing to compare with
        log.info('edited %s by %s: %r -> %r', after.id, after.author, before.content, after.content)


discord.utils.setup_logging(level=logging.INFO, root=True)
Watcher(max_messages=20_000).run(os.environ['DISCORD_TOKEN'], log_handler=None)
```

## Keyword alerts to a webhook

Watches every channel you can see and posts matches, with an embed, to a
webhook of your own (user accounts can't send embeds themselves).

```python
import os
import re

import discord

PATTERN = re.compile(r'\b(outage|incident|deploy failed)\b', re.IGNORECASE)
WEBHOOK_URL = os.environ['ALERT_WEBHOOK']


class Alerts(discord.Client):
    async def setup_hook(self):
        self.webhook = discord.Webhook.from_url(WEBHOOK_URL, client=self)

    async def on_message(self, message: discord.Message):
        if message.author == self.user or not message.content or not PATTERN.search(message.content):
            return
        embed = discord.Embed(description=message.content[:4000], timestamp=message.created_at, url=message.jump_url)
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        where = f'#{message.channel}' + (f' in {message.guild.name}' if message.guild else '')
        embed.set_footer(text=where)
        await self.webhook.send(embed=embed, username='Keyword alerts')


Alerts().run(os.environ['DISCORD_TOKEN'])
```

Webhooks have their own rate limits; for very busy keywords, batch matches
and send one message per interval.

## A self-bot with paced commands

Commands you type yourself, with every outgoing message going through one
lane so bursts never happen.

```python
import asyncio
import os
import random
import time

import discord
from discord.ext import commands


class Lane:
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def run(self, coro_fn, *args, **kwargs):
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await coro_fn(*args, **kwargs)
            finally:
                self._next = time.monotonic() + self.interval * random.uniform(1.0, 1.3)


bot = commands.Bot(command_prefix='.', self_bot=True, help_command=None)
send_lane = Lane(2.6)


@bot.command()
async def say(ctx: commands.Context, times: commands.Range[int, 1, 5], *, text: str):
    for _ in range(times):
        await send_lane.run(ctx.send, text)


@bot.command()
async def purge_mine(ctx: commands.Context, limit: commands.Range[int, 1, 50] = 10):
    """Delete your own last N messages in this channel."""
    mine = [m async for m in ctx.channel.history(limit=200) if m.author == bot.user][:limit]
    for message in mine:
        await send_lane.run(message.delete)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await ctx.message.add_reaction('\N{CROSS MARK}')
    elif not isinstance(error, commands.CommandNotFound):
        raise error


bot.run(os.environ['DISCORD_TOKEN'])
```

## Back up a channel to JSON Lines

Oldest first, one message per line, resumable from the last saved ID.

```python
import asyncio
import json
import os
import pathlib

import discord

CHANNEL_ID = 123456789012345678
OUT = pathlib.Path(f'backup-{CHANNEL_ID}.jsonl')


def last_saved_id() -> int | None:
    if not OUT.exists():
        return None
    last = None
    with OUT.open(encoding='utf-8') as f:
        for line in f:
            last = json.loads(line)['id']
    return last


async def main():
    async with discord.Client(chunk_guilds_at_startup=False) as client:
        await client.login(os.environ['DISCORD_TOKEN'])   # REST only: no gateway needed
        channel = await client.fetch_channel(CHANNEL_ID)
        after = last_saved_id()
        count = 0
        with OUT.open('a', encoding='utf-8') as f:
            async for m in channel.history(limit=None, after=discord.Object(id=after) if after else None, oldest_first=True):
                f.write(json.dumps({
                    'id': m.id,
                    'author': {'id': m.author.id, 'name': m.author.name},
                    'created_at': m.created_at.isoformat(),
                    'edited_at': m.edited_at.isoformat() if m.edited_at else None,
                    'content': m.content,
                    'attachments': [a.url for a in m.attachments],
                    'reply_to': m.reference.message_id if m.reference else None,
                }, ensure_ascii=False) + '\n')
                count += 1
        print(f'saved {count} new messages to {OUT}')


asyncio.run(main())
```

`history()` pages 100 messages per request and the HTTP client waits out any
rate limit, so a large channel simply takes a while.

## Download every attachment in a channel

```python
import asyncio
import os
import pathlib

import discord

CHANNEL_ID = 123456789012345678
DEST = pathlib.Path('attachments')


async def main():
    DEST.mkdir(exist_ok=True)
    async with discord.Client() as client:
        await client.login(os.environ['DISCORD_TOKEN'])
        channel = await client.fetch_channel(CHANNEL_ID)
        async for message in channel.history(limit=None):
            for attachment in message.attachments:
                target = DEST / f'{message.id}-{attachment.filename}'
                if not target.exists():
                    await attachment.save(target)
                    await asyncio.sleep(0.5)     # be gentle with the CDN


asyncio.run(main())
```

## Export a guild's member list to CSV

Uses the gateway member request if you have the permissions, sidebar scraping
otherwise (online members only in big guilds). See [Member lists](member-lists.md).

```python
import csv
import os

import discord

GUILD_ID = 123456789012345678


class Exporter(discord.Client):
    async def on_ready(self):
        guild = self.get_guild(GUILD_ID)
        try:
            members = await guild.chunk()
        except (discord.ClientException, discord.InvalidData) as exc:
            print('chunking failed, falling back to the sidebar:', exc)
            members = await guild.fetch_members(cache=False)

        with open(f'members-{GUILD_ID}.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'username', 'display_name', 'joined_at', 'roles'])
            for m in members:
                writer.writerow([m.id, m.name, m.display_name, m.joined_at.isoformat() if m.joined_at else '',
                                 ';'.join(r.name for r in m.roles[1:])])
        print(f'{len(members)} of {guild.member_count} members exported')
        await self.close()


Exporter(chunk_guilds_at_startup=False).run(os.environ['DISCORD_TOKEN'])
```

## Track friends' status changes

Friends' presences always arrive, so this works without any guild
subscription. Outside guilds, `on_presence_update` gets `Relationship`
objects (with `status`, `activities` and `user`) instead of members.

```python
import datetime
import os

import discord


class FriendStatus(discord.Client):
    async def on_presence_update(self, before, after):
        if not isinstance(after, discord.Relationship) or after.type is not discord.RelationshipType.friend:
            return  # a guild member, or a non-friend relationship
        if before.status != after.status:
            now = datetime.datetime.now().strftime('%H:%M:%S')
            print(f'[{now}] {after.user}: {before.status} -> {after.status}')

    async def on_relationship_update(self, before: discord.Relationship, after: discord.Relationship):
        if after.type is discord.RelationshipType.friend and before.type is not after.type:
            print(f'{after.user} is now a friend')


FriendStatus().run(os.environ['DISCORD_TOKEN'])
```

## A rotating custom status

Changes only this session's presence (`edit_settings=False`), far below
Discord's 5 updates per 20 seconds.

```python
import itertools
import os

import discord
from discord.ext import tasks

STATUSES = itertools.cycle(['Coding', 'Reviewing PRs', 'On a break'])


class Rotator(discord.Client):
    async def setup_hook(self):
        self.rotate.start()

    @tasks.loop(minutes=10)
    async def rotate(self):
        await self.change_presence(activity=discord.CustomActivity(next(STATUSES)), edit_settings=False)

    @rotate.before_loop
    async def before_rotate(self):
        await self.wait_until_ready()


Rotator().run(os.environ['DISCORD_TOKEN'])
```

## Several accounts with warm restarts

One process, one client per account, each with its own persistence file and
tight caches.

```python
import asyncio
import logging
import os
import pathlib

import discord

pathlib.Path('cache').mkdir(exist_ok=True)


class AccountClient(discord.Client):
    async def on_ready(self):
        stats = await self.cache_stats()
        print(f'{self.user}: {len(self.guilds)} guilds, {stats.members} members cached')


async def run_account(index: int, token: str):
    client = AccountClient(
        chunk_guilds_at_startup=False,
        max_member_cache=1_000,
        max_total_member_cache=10_000,
        max_messages=200,
        persistence_path=f'cache/account-{index}.bin',
    )
    async with client:
        await client.start(token)


async def main():
    discord.utils.setup_logging(level=logging.INFO)
    tokens = [t for t in os.environ['DISCORD_TOKENS'].split(',') if t]
    results = await asyncio.gather(*(run_account(i, t) for i, t in enumerate(tokens)), return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            print(f'account {i} stopped: {result!r}')


asyncio.run(main())
```

## A health endpoint next to the client

Runs an aiohttp web server and the client in the same event loop.

```python
import asyncio
import os

import discord
from aiohttp import web

client = discord.Client()


async def health(request: web.Request) -> web.Response:
    if not client.is_ready():
        return web.json_response({'ready': False}, status=503)
    stats = await client.cache_stats()
    return web.json_response({
        'ready': True,
        'user': str(client.user),
        'latency_ms': round(client.latency * 1000),
        'guilds': stats.guilds,
        'backlog': stats.dispatch_backlog,
        'loop_lag_ms': round(stats.event_loop_lag * 1000),
    })


async def main():
    app = web.Application()
    app.router.add_get('/health', health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 8080).start()
    try:
        async with client:
            await client.start(os.environ['DISCORD_TOKEN'])
    finally:
        await runner.cleanup()


asyncio.run(main())
```

## Graceful shutdown under Docker or systemd

`docker stop` and systemd send `SIGTERM`, which Python doesn't turn into a
clean close by default. Close the client yourself so voice disconnects, a
pending persistence snapshot is written, and the gateway closes cleanly.

```python
import asyncio
import os
import signal

import discord

client = discord.Client(persistence_path='cache/main.bin')


async def main():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(client.close()))
    async with client:
        await client.start(os.environ['DISCORD_TOKEN'])


asyncio.run(main())
```

(`add_signal_handler` isn't available on Windows; there, Ctrl+C under
`client.run()` already closes cleanly.)
