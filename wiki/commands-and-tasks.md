# ext.commands and ext.tasks

The command framework (`discord.ext.commands`) and background loops
(`discord.ext.tasks`) on a user account: who a self-bot answers, prefixes,
arguments and converters, checks, cooldowns, errors, help, cogs and
extensions, and loops with their scheduling, recovery and stopping rules.
The full reference is [`docs/ext/commands`](../docs/ext/commands) and
[`docs/ext/tasks`](../docs/ext/tasks).

## A self-bot

```python
import os
import discord
from discord.ext import commands

bot = commands.Bot(command_prefix='>', self_bot=True, help_command=None)


@bot.command()
async def echo(ctx: commands.Context, times: int, *, text: str):
    for _ in range(times):
        await ctx.send(text)


@bot.command(aliases=['p'])
async def ping(ctx: commands.Context):
    await ctx.message.edit(content=f'pong: {bot.latency * 1000:.0f} ms')   # edit your own command message


bot.run(os.environ['DISCORD_TOKEN'])
```

`commands.Bot(command_prefix, help_command=<default>, description=None, **options)`
takes every [Client option](client-options.md) plus the command options.

### Who the bot answers

| Option | Processes commands from |
|---|---|
| neither (default) | everyone **except** your own account |
| `self_bot=True` | **only** your own account. The usual choice: you type the commands. |
| `user_bot=True` | everyone, **including** your own account |

Setting both raises `TypeError`. Messages from bots are never processed. The
code is `BotBase.__init__` and `_skip_check` in
[`discord/ext/commands/bot.py`](../discord/ext/commands/bot.py).

> **Gotcha: answering other people's commands is risky.** With the default or
> `user_bot=True`, anyone can make your account send messages. Add checks, and
> pace the replies.

### Prefixes

```python
commands.Bot(command_prefix='!')
commands.Bot(command_prefix=['!!', '!'])                  # the first match in list order wins
commands.Bot(command_prefix=commands.when_mentioned_or('!'))

async def get_prefix(bot, message):
    return '?' if message.guild and message.guild.id == SPECIAL_GUILD else '!'

commands.Bot(command_prefix=get_prefix)
```

- `strip_after_prefix=True` allows `! ping`.
- `case_insensitive=True` makes command names case-insensitive.
- A list must contain only strings.

### Overriding `on_message`

The bot's own `on_message` runs the commands. If you override it, call
`process_commands` yourself, or use a listener instead:

```python
@bot.event
async def on_message(message):
    if message.content.startswith('hello'):
        ...
    await bot.process_commands(message)


@bot.listen('on_message')          # doesn't replace command processing
async def log_messages(message):
    ...
```

## Arguments and converters

```python
@bot.command()
async def info(ctx, member: discord.Member, channel: discord.TextChannel = None, *, reason: str = 'none'):
    channel = channel or ctx.channel
    await ctx.send(f'{member.display_name} in {channel.mention}: {reason}')


@bot.command()
async def roll(ctx, sides: commands.Range[int, 2, 100] = 6):
    ...


@bot.command()
async def ban_many(ctx, members: commands.Greedy[discord.Member], *, reason: str):
    ...
```

- `*, rest: str` takes everything after as one string.
- Annotated types are converted; a failed conversion raises a `BadArgument`
  subclass (`MemberNotFound`, `ChannelNotFound`...).

### Converters

In [`converter.py`](../discord/ext/commands/converter.py):

- **Objects and users:** `ObjectConverter`, `MemberConverter`, `UserConverter`
- **Messages:** `MessageConverter`, `PartialMessageConverter`
- **Channels:** `GuildChannelConverter`, `TextChannelConverter`,
  `VoiceChannelConverter`, `StageChannelConverter`,
  `CategoryChannelConverter`, `ForumChannelConverter`, `ThreadConverter`
- **Guild things:** `RoleConverter`, `GuildConverter`, `InviteConverter`,
  `ScheduledEventConverter`
- **Emoji and media:** `EmojiConverter`, `PartialEmojiConverter`,
  `GuildStickerConverter`, `SoundboardSoundConverter` (new in v2.3.2)
- **Other:** `ColourConverter` / `ColorConverter`, `GameConverter`
- **Used the same way:** `Greedy`, `Range`, `clean_content`,
  `FlagConverter` and `typing.Annotated`

How they resolve an argument:

- **The order.** ID-or-name converters try an ID, then a mention (and, for
  channels, a channel URL), then a name.
- **A number that looks like an ID** is resolved as an ID only, with no name
  fallback.
- **Members** are looked up in the cache, then over the gateway
  (`query_members`) when not cached.
- **Sounds** come from the local guild's cache, or, in DMs, from every guild's
  cache. A guild's sounds are cached after `Client.request_soundboard_sounds`
  or when a guild payload includes them. When nothing matches, the converter
  raises `SoundboardSoundNotFound`.

### Flags

```python
class SearchFlags(commands.FlagConverter, prefix='--', delimiter=' '):
    query: str
    limit: int = 10
    authors: list[discord.Member] = []
    nsfw: bool = False


@bot.command()
async def search(ctx, *, flags: SearchFlags):
    # >search --query deploy failed --limit 5 --authors @a @b
    ...
```

- Class options: `prefix`, `delimiter`, `case_insensitive`.
- `commands.flag(name=..., aliases=[...], default=..., max_args=..., override=..., converter=..., description=..., positional=...)`
  customises one flag.
- `Optional[str]` defaults to `None`; `list[...]` takes any number of values;
  `dict[K, V]` takes key-value pairs.
- Duplicate names, two positional flags, or unsupported annotations raise
  `TypeError` when the class is defined.
- Fixed in the November 2026 audit: a value that touches the next flag
  (`-name:foo-age:3` with `prefix='-'`) keeps its last character.

## Context

`ctx` gives you everything about the invocation:

```python
ctx.message, ctx.author, ctx.channel, ctx.guild, ctx.me
ctx.command, ctx.invoked_with, ctx.prefix, ctx.args, ctx.kwargs
await ctx.send('text')            # same parameters as Messageable.send
await ctx.reply('text')           # a reply to the command message
```

A custom `Context` subclass (`bot.get_context(message, cls=MyContext)`) is
shown in [`examples/custom_context.py`](../examples/custom_context.py).

## Checks

```python
@bot.command()
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, limit: int = 20):
    await ctx.channel.purge(limit=limit)


def in_channel(channel_id: int):
    async def predicate(ctx):
        return ctx.channel.id == channel_id
    return commands.check(predicate)


@bot.check                      # a global check for every command
async def only_me(ctx):
    return ctx.author.id == bot.user.id
```

- Checks added with decorators run **top decorator first**. Fixed in the
  November 2026 audit: every copy of a command (including the one each cog
  instance makes) used to reverse the order again.
- Order of evaluation: disabled command, global checks, the cog's
  `cog_check`, then the command's checks. A failure raises `CheckFailure` (or
  a subclass like `MissingPermissions`).
- **`is_owner()` and `@commands.is_owner()` need `owner_id` or `owner_ids`.**
  On a user account the owner isn't looked up automatically; without them,
  `bot.is_owner(user)` raises `AttributeError: Owners aren't set.` Set
  `owner_id=your_user_id`.

## Cooldowns and concurrency

```python
@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)              # once per 30 s per user
@commands.max_concurrency(1, commands.BucketType.channel, wait=False)
async def heavy(ctx):
    ...
```

- `cooldown(rate, per, type)` is a token bucket; over the limit raises
  `CommandOnCooldown` (with `retry_after`).
- `dynamic_cooldown(func, type)` computes the cooldown per message.
- `max_concurrency(n, per, wait=False)` limits simultaneous runs; over the
  limit raises `MaxConcurrencyReached`, or waits with `wait=True`.
- `BucketType`: `default`, `user`, `guild`, `channel`, `member`,
  `category`, `role`.
- Fixed in the November 2026 audit: `max_concurrency` releases its slot even
  when the bucket key changes during the command (`BucketType.role` when the
  author's top role changes, `category` when the channel moves); the slot
  used to leak.

These are per-command limits for *your* users. They don't pace your account
against Discord's rate limits; see [Rate limits and pacing](rate-limits-and-pacing.md).

## Errors

```python
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(f'slow down: {error.retry_after:.0f}s')
    if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        return await ctx.send(f'usage: {ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}')
    raise error   # let the default handler log it


@heavy.error                      # a handler for one command
async def heavy_error(ctx, error):
    ...
```

- The default handler logs `Ignoring exception in command <name>` with the
  traceback, at ERROR.
- An exception inside a command is wrapped in `CommandInvokeError`
  (`error.original` is the real one).
- Events: `on_command(ctx)`, `on_command_completion(ctx)`,
  `on_command_error(ctx, error)`.

## Help

The default help command (`DefaultHelpCommand`) replies in the channel.
`help_command=None` removes it. For a custom one, subclass
`commands.HelpCommand` (or `MinimalHelpCommand`) and pass an instance;
anything else raises `TypeError`. `show_hidden=False` and
`verify_checks=True` are the defaults.

## Cogs and extensions

```python
class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command()
    async def uptime(self, ctx):
        ...

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        ...


async def setup(bot: commands.Bot):          # in an extension module
    await bot.add_cog(Utility(bot))


class MyBot(commands.Bot):
    async def setup_hook(self):
        await self.load_extension('cogs.utility')
```

- `add_cog`, `load_extension`, `unload_extension` and `reload_extension` are
  coroutines; call them from `setup_hook` or another async context.
- `python -m discord newcog <name>` generates a cog file.

### What's not available

App commands (`discord.app_commands`), hybrid commands and `GroupCog`
aren't available: only bots can register application commands. You can
still **use** other bots' slash commands; see
[Interactions and components](interactions-and-components.md).

## Background loops: `ext.tasks`

```python
from discord.ext import tasks


class MyClient(discord.Client):
    async def setup_hook(self):
        self.heartbeat.start()

    @tasks.loop(seconds=60)
    async def heartbeat(self):
        print('alive', self.latency)

    @heartbeat.before_loop
    async def before_heartbeat(self):
        await self.wait_until_ready()     # fine here: this runs in the loop's task, not in setup_hook
```

`tasks.loop(*, seconds=..., minutes=..., hours=..., time=..., count=None, reconnect=True, name=None)`:

- **Relative intervals:** `seconds`, `minutes`, `hours` add up.
- **Times of day:** `time=` takes a `datetime.time` or a list of them, and
  can't be combined with the relative arguments (`TypeError`). Naive times
  are UTC; give a `tzinfo` for local times. Since v2.3.1, times in different
  zones work together.
- **`count`** limits the iterations (`None` forever; 0 or less raises
  `ValueError`).
- **No interval at all** (`@tasks.loop()`) runs back to back; the loop logs a
  warning once (`Loop ... has a zero interval and no count; ...`). Give it an
  interval or a `count`.

```python
import datetime
from zoneinfo import ZoneInfo

@tasks.loop(time=[datetime.time(9, 0, tzinfo=ZoneInfo('Europe/Paris')), datetime.time(18, 0, tzinfo=ZoneInfo('Europe/Paris'))])
async def twice_a_day():
    ...
```

### Starting and stopping

| Method | Effect |
|---|---|
| `start(*args)` | Starts the loop. Starting a running loop raises `RuntimeError('Task is already launched and is not completed.')`. |
| `stop()` | Finishes the current iteration, then stops. If the loop is waiting for its next run, it ends at once and no further iteration runs (before the November 2026 audit, one more ran). Called in `before_loop`, the first run is skipped. |
| `cancel()` | Stops now (cancels the task). |
| `restart(*args)` | Cancels and starts again. Calling it twice in a row no longer raises. |
| `change_interval(seconds=..., minutes=..., hours=..., time=...)` | Takes effect after the current wait. |
| `is_running()`, `next_iteration`, `current_loop`, `get_task()` | Inspection. |

Hooks: `@loop.before_loop`, `@loop.after_loop`, `@loop.error`.

### Errors and recovery

- With `reconnect=True` (the default), these exceptions are handled by backing
  off and retrying, logged as `Handling exception in internal background task ... Retrying in X.XXs`:
  `OSError`, `discord.GatewayNotFound`, `discord.ConnectionClosed`,
  `aiohttp.ClientError`, `asyncio.TimeoutError`.
- `add_exception_type(*types)`, `remove_exception_type(*types)` and
  `clear_exception_types()` change that list. Since the November 2026 audit,
  types added on a cog's class-level loop (`MyCog.my_loop.add_exception_type(...)`)
  also apply to each cog instance's loop, as long as they're added before
  the instance first uses the loop.
- Any other exception runs the `error` hook (the default logs it at ERROR)
  and **ends the loop**. Restart it from the hook if you want it to continue.

```python
@heartbeat.error
async def heartbeat_error(self, error):
    print('loop failed:', error)
    self.heartbeat.restart()
```

> **Gotcha: `ext.tasks` imports aiohttp.** Unlike `import discord`, importing
> `discord.ext.tasks` loads aiohttp (for its exception list), about 10 MB per
> process.
