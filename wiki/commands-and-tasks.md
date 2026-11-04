# ext.commands and ext.tasks

## Self-bot commands

```py
from discord.ext import commands

bot = commands.Bot(command_prefix='>', self_bot=True)

@bot.command()
async def echo(ctx, times: int, *, text: str):
    for _ in range(times):
        await ctx.send(text)

bot.run('token')
```

Who the bot answers depends on two options:

| Option | Responds to |
|---|---|
| neither (default) | everyone **except** your own account |
| `self_bot=True` | **only** your own account. This is the usual choice: you type the commands. |
| `user_bot=True` | everyone, **including** your own account |

Setting both raises `TypeError`. The code is `BotBase.__init__` in
[`discord/ext/commands/bot.py`](../discord/ext/commands/bot.py).

- **Prefixes.** `command_prefix` can be a string, a list of strings, or a
  callable; `commands.when_mentioned_or(...)` works too.
- **Other options.** `case_insensitive`, `strip_after_prefix` and
  `owner_id`/`owner_ids` work as in discord.py.
- **Overriding `on_message`.** If you override it, call
  `await bot.process_commands(message)` or your commands stop working.
- **Not supported.** App commands, hybrid commands and `GroupCog` aren't
  available, because only bots can register application commands. You can
  still **use** other bots' slash commands from a user account through the
  `application_commands` and interaction APIs; see `docs/api.rst`.

**Live test (v2.3.2):** with `command_prefix='bpy!'` and `self_bot=True`, sending `bpy!echo 7 hello world`
parsed to `(7, 'hello world')`, and the `Member` converter resolved a user ID.

### Converters

These converters are in [`converter.py`](../discord/ext/commands/converter.py):

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
- **Not converters, but used the same way:** `Greedy`, `Range`,
  `clean_content`, `FlagConverter` and `typing.Annotated`

How they resolve an argument:

- **The order.** ID-or-name converters try an ID, then a mention (and, for
  channels, a channel URL), then a name.
- **A number that looks like an ID** is resolved as an ID only, with no name
  fallback. Every ID-or-name converter does this. `SoundboardSoundConverter`
  follows the same rule; Rapptz's version does too.
- **Sounds** come from the local guild's cache, or, in DMs, from the caches of
  every guild. A guild's sounds are cached after
  `Client.request_soundboard_sounds`, or when a guild payload includes them.
  When nothing matches, the converter raises `SoundboardSoundNotFound`.

Each converter raises its own documented `BadArgument` subclass
(`MemberNotFound`, `ChannelNotFound` and so on) when it fails.

## Background tasks

```py
from discord.ext import tasks

@tasks.loop(seconds=60)
async def heartbeat():
    print('alive')

@heartbeat.before_loop
async def before():
    await client.wait_until_ready()

heartbeat.start()
```

- **Scheduling.** `tasks.loop(seconds=..., minutes=..., hours=..., time=...,
  count=None, reconnect=True, name=None)`.
  - `time=` takes one or more `datetime.time` values. They default to UTC, and
    can't be combined with the relative arguments.
  - `count` limits the number of iterations; `None` runs forever.
- **Recovery.** `reconnect=True` restarts the loop with exponential backoff
  after errors the loop can handle. `add_exception_type(...)` adds more.
- **Stopping.** `stop()` lets the current iteration finish; `cancel()` stops now.
- **Timezones.** Since v2.3.1, loops whose `time=` values mix timezones run correctly.

**Live test (v2.3.2):** `tasks.loop(seconds=0.5, count=3)` ran exactly 3 times
and then stopped.
