# Interactions and components

A user account can't create buttons or slash commands, but it can **use**
bots' ones, as the official client does: run slash and context-menu commands,
click buttons, pick select options and submit modals. This page covers how,
what you get back, and the events that tell you what happened. The code is in
[`discord/commands.py`](../discord/commands.py),
[`discord/components.py`](../discord/components.py),
[`discord/modal.py`](../discord/modal.py) and
[`discord/interactions.py`](../discord/interactions.py).

> Automating interactions with other people's bots is exactly what Discord's
> anti-automation looks for. Pace clicks and commands (3.0 s lanes; see
> [Rate limits and pacing](rate-limits-and-pacing.md)).

## How interactions work here

1. You trigger something (a slash command, a click).
2. The library sends an interaction request with a nonce and returns as soon
   as Discord accepts it. **None of these methods wait for the bot's
   answer**, and none return it.
3. Discord dispatches events as the bot responds:
   - `on_interaction(interaction)` when your interaction is created;
   - `on_interaction_finish(interaction)` when the bot acknowledges it;
   - `on_modal(modal)` if the bot answers with a modal;
   - the bot's reply arrives as a normal message (`on_message`), with
     `message.interaction_metadata` pointing at your interaction.

To act on the result, wait for the event:

```python
def from_bot(m: discord.Message) -> bool:
    return m.channel == channel and m.author.id == BOT_ID

await command(channel, query='weather in paris')
reply = await client.wait_for('message', check=from_bot, timeout=15)
```

## Slash and context-menu commands

```python
commands = await channel.application_commands()      # what you can use in this channel
for cmd in commands:
    print(cmd.type, cmd.name, cmd.application_id, cmd.description)

ping = discord.utils.get(commands, name='ping', application_id=BOT_ID)
await ping(channel)                                   # /ping

weather = discord.utils.get(commands, name='weather')
await weather(channel, city='Paris', units='metric')  # options as keyword arguments
```

- `application_commands()` returns `SlashCommand`, `UserCommand`,
  `MessageCommand` and `PrimaryEntryPointCommand` objects for the commands
  you're allowed to use there. In guild channels it uses the guild's command
  index; in DMs, the bot's commands; in group DMs and DMs with people, `[]`.
- **It's heavily rate limited.** Cache the result and reuse it rather than
  calling it before every command.
- `on_application_command_index_update(guild)` fires when a guild's commands
  change.

### Options

Pass options as keyword arguments named after them:

| Option type | Pass |
|---|---|
| string, integer, number | The value; it's converted (`str`, `int`, `float`). |
| boolean | `True`/`False`, or the strings `'true'`, `'1'`, `'yes'` (anything else is false). |
| user, channel, role, mentionable | The object (or a `discord.Object`); its ID is sent. |
| attachment | A `discord.File`; it's uploaded with the command. |
| an option with choices | The choice's value, or its name (mapped to the value). |

An option name the command doesn't have is dropped with a warning:
`Ignoring unknown option(s) colour for command 'weather' (valid: city, units).`

### Subcommands

A command with subcommands is a group; calling the group itself raises
`TypeError('Cannot use a group')`. Call the subcommand:

```python
tag = discord.utils.get(commands, name='tag')
show = discord.utils.get(tag.children, name='show')   # /tag show
await show(channel, name='rules')
```

### Context-menu commands

```python
user_cmd = next(c for c in commands if isinstance(c, discord.UserCommand))
await user_cmd(member, channel=channel)           # right-click a user → Apps → command

msg_cmd = next(c for c in commands if isinstance(c, discord.MessageCommand))
await msg_cmd(message, channel=channel)           # right-click a message → Apps → command
```

For slash commands the channel is the first positional argument; for
context-menu commands the target is, and the channel is the `channel=`
keyword. The channel can be left out when the command has a `target_channel`;
otherwise the call raises
`TypeError("__call__() missing 1 required argument: 'channel'")`.

## Buttons

```python
for row in message.components:                    # ActionRow → children
    for component in row.children:
        if isinstance(component, discord.Button):
            print(component.label, component.style, component.custom_id, component.url, component.disabled)

button = message.components[0].children[0]
await button.click()
```

- A **link button** returns its URL from `click()` and sends nothing.
- A premium (SKU) button returns `None` and sends nothing.
- A button not attached to a message raises `TypeError`.
- Otherwise `click()` sends the interaction and returns `None` (since 2.2 the
  interaction itself isn't returned; use the events).
- Disabled buttons can still be "clicked" by the library; the bot will
  usually reject it. Check `button.disabled`.

## Select menus

```python
select = message.components[1].children[0]        # a SelectMenu
print(select.placeholder, select.min_values, select.max_values, [o.label for o in select.options])

option = discord.utils.get(select.options, label='Red')
await select.choose(option)                       # one or more SelectOption objects
```

- `choose(*options)` sends the interaction with those values. For a select
  on a message, the number of options isn't checked by the library; respect
  `min_values` and `max_values` yourself, or the bot will reject it.
- For a select inside a modal, `choose()` records the answer (like
  `answer()`) and checks the count: outside the allowed range raises
  `ValueError`.

## Modals

When a bot answers with a modal, `on_modal` fires with it:

```python
@client.event
async def on_modal(modal: discord.Modal):
    for row in modal.components:
        for field in row.children:
            if isinstance(field, discord.TextInput):
                if field.label == 'Name':
                    field.answer('Sam')
                elif field.label == 'Reason':
                    field.answer('Testing')
    await modal.submit()
```

- Answer each component (`TextInput.answer(value)`, `SelectMenu.answer(*values)`,
  file uploads with `File` objects), then `submit()`.
- A required component without an answer, or a value outside its length
  limits, raises `ValueError`.
- `modal.interaction` is the interaction that opened it.
- Newer modal layouts (labels wrapping components) are supported; a label
  around a component type this version doesn't know is submitted unchanged,
  with a warning.

## Layout components (Components V2)

Bots can send rich layouts: sections, text displays, thumbnails, media
galleries, files, separators, containers, labels, file uploads, radio groups,
checkbox groups and checkboxes. User accounts **receive and parse all of
them** into their classes in `discord/components.py`, matching Rapptz's set:

```python
def walk(components, depth=0):
    for c in components:
        print('  ' * depth + type(c).__name__)
        walk(getattr(c, 'children', []) or getattr(c, 'components', []), depth + 1)

walk(message.components)
```

**Sending** layouts isn't possible from `send()` (no `components`
parameter); webhooks can send them (`webhook.send(components=[...])`).
A message can't mix V2 components with content, embeds, stickers or polls:
that raises `ValueError` before sending. Before the November 2026 audit,
sending V2 components raised `NameError`, and passing your own `flags`
modified that `MessageFlags` object.

## Reading interaction metadata

```python
if message.interaction_metadata is not None:
    meta = message.interaction_metadata
    print(meta.type, meta.user, meta.id)       # who used which command, button or modal
```

`Message.interaction_metadata` (new in v2.3.2) replaces the deprecated
`Message.interaction`, which still works. It also says, for context-menu
commands, which user or message was targeted, and for follow-ups, the
original response.

## Gotchas

- **Nothing waits for the bot.** `click()`, `choose()`, `submit()` and command
  calls return once Discord accepts the request. Use `wait_for` on the
  events or the reply message.
- **`application_commands()` is rate limited.** Cache it.
- **Components can change.** Bots edit messages to update or disable buttons;
  re-read `message.components` (or fetch the message) before clicking.
- **Interactions expire.** A component on a very old message may no longer
  be handled by the bot.
