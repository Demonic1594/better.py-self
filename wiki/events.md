# Events

Everything about events: how to register handlers, the order things happen
in, cached versus raw events, the opt-in events, what the connection events
really mean, a catalogue of every event with its arguments, `wait_for`
patterns, errors, and the backlog warning. The full signatures and payload
classes are in [`docs/api.rst`](../docs/api.rst) under "Event Reference".

## Registering handlers

Three ways, all equivalent for the client:

```python
import discord
from discord.ext import commands


# 1. A method on a Client subclass
class MyClient(discord.Client):
    async def on_message(self, message):
        ...


# 2. The @client.event decorator (uses the function's name)
client = discord.Client()

@client.event
async def on_message(message):
    ...


# 3. commands.Bot listeners: any number per event, any function name
bot = commands.Bot(command_prefix='!', self_bot=True)

@bot.listen('on_message')
async def log_messages(message):
    ...

@bot.listen()
async def on_message_delete(message):   # name taken from the function
    ...
```

Rules:

- The handler must be `async def`; `@client.event` raises
  `TypeError: event registered must be a coroutine function` otherwise.
- A `Client` has **one** handler per event name. Registering `on_message`
  twice keeps the last one. Use `commands.Bot` and `listen()` (or
  `bot.add_listener(func, 'on_message')`) for several. A `Bot`'s own
  `on_message` method processes commands; if you override it, call
  `await bot.process_commands(message)` yourself.
- Names are checked only by convention: `@client.event` on a function named
  `on_mesage` registers a handler for an event that never fires, without an
  error.

## When handlers run

For every gateway event:

1. The parser updates the cache (a new message is already in
   `client.cached_messages` when `on_message` runs; an updated member is
   already updated in `on_member_update`).
2. `client.dispatch(name, *args)` resolves waiting `wait_for` calls, then
   schedules your handler as a **new task** (named `discord.py: on_<name>`).
3. Your handler runs concurrently with the next events.

So:

- **Handlers interleave.** Two quick messages run two `on_message` tasks at
  once, switching at every `await`. If they share state, protect it:

  ```python
  lock = asyncio.Lock()

  @client.event
  async def on_message(message):
      async with lock:              # one at a time
          await update_counter(message)
  ```

- **No handler blocks the gateway,** but a blocking call (not awaited) blocks
  everything.
- **`eager_dispatch=True`** (Python 3.12+) runs each handler synchronously up to
  its first `await` before the parser continues. It saves the scheduling cost
  per event, at the price of that ordering change.
- **Events before `login()` or after `close()` are dropped silently.**

## Cached and raw events

Most events come in two forms:

- **Cached events** (`on_message_edit`, `on_reaction_add`, `on_member_update`
  and so on) give you full objects. They fire **only when the related object
  is in the cache**. With bounded caches (see
  [Caching and memory](caching-and-memory.md)), an evicted member or an old
  message means no cached event.
- **Raw events** (`on_raw_message_edit`, `on_raw_reaction_add` and so on) fire
  whether or not the object is cached. They pass a payload object with the
  IDs, the raw `data` where useful, and the cached object if there is one.

| Cached event | Fires only if... | Raw event | Raw payload |
|---|---|---|---|
| `on_message_edit(before, after)` | the message is cached | `on_raw_message_edit(payload)` | `RawMessageUpdateEvent`: `message_id`, `channel_id`, `guild_id`, `data`, `cached_message` (the old version), `message` (the new one, or `None` for a partial update of an uncached message) |
| `on_message_delete(message)` | the message is cached | `on_raw_message_delete(payload)` | `RawMessageDeleteEvent`: `message_id`, `channel_id`, `guild_id`, `cached_message` |
| `on_bulk_message_delete(messages)` | at least one is cached (gets only the cached ones) | `on_raw_bulk_message_delete(payload)` | `RawBulkMessageDeleteEvent`: `message_ids`, `channel_id`, `guild_id`, `cached_messages` |
| `on_reaction_add(reaction, user)`, `on_reaction_remove` | the message is cached | `on_raw_reaction_add(payload)`, `on_raw_reaction_remove` | `RawReactionActionEvent`: `message_id`, `channel_id`, `guild_id`, `user_id`, `emoji`, `member` (adds only), `message_author_id`, `burst`, `burst_colours`, `type`, `event_type` |
| `on_reaction_clear(message, reactions)` | the message is cached | `on_raw_reaction_clear(payload)` | `RawReactionClearEvent` |
| `on_reaction_clear_emoji(reaction)` | the message is cached | `on_raw_reaction_clear_emoji(payload)` | `RawReactionClearEmojiEvent` |
| `on_member_remove(member)` | the member is cached | `on_raw_member_remove(payload)` | `RawMemberRemoveEvent`: `user`, `guild_id` |
| `on_presence_update(before, after)` | the member is cached | `on_raw_presence_update(payload)` (opt-in) | `RawPresenceUpdateEvent` |
| `on_typing(channel, user, when)` | the channel is cached and the user resolves | `on_raw_typing(payload)` (opt-in) | `RawTypingEvent`: `channel_id`, `user_id`, `guild_id`, `timestamp`, `user` |
| `on_thread_update(before, after)` | the thread is cached | `on_raw_thread_update(payload)` | `RawThreadUpdateEvent`: `thread_id`, `thread_type`, `guild_id`, `parent_id`, `data`, `thread` |
| `on_thread_delete(thread)` | the thread is cached | `on_raw_thread_delete(payload)` | `RawThreadDeleteEvent` |
| `on_thread_member_remove(member)` | the thread and member are cached | `on_raw_thread_member_remove(payload)` | `RawThreadMembersUpdate`, once per member who left |
| `on_poll_vote_add(user, answer)`, `on_poll_vote_remove` | the message and user are cached | `on_raw_poll_vote_add(payload)`, `on_raw_poll_vote_remove` | `RawPollVoteActionEvent` |
| `on_message_ack(message, manual)` | the message is cached | `on_raw_message_ack(payload)` | `RawMessageAckEvent` |

Use the raw form when you must not miss an event. A raw event can still be
skipped when its payload is unusable, for example `on_raw_member_remove` for
a guild the client doesn't know.

```python
@client.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.cached_message is not None:
        print('deleted:', payload.cached_message.content)
    else:
        print('deleted an uncached message', payload.message_id, 'in', payload.channel_id)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == client.user.id:
        return
    channel = client.get_partial_messageable(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)   # if you really need it
    print(payload.emoji, 'on', message.jump_url)
```

> **Gotcha: `on_raw_message_edit` with `message=None`.** Discord sends partial
> updates (for example only the embeds when a link unfurls). For an uncached
> message there's nothing to build a `Message` from, so `payload.message` is
> `None`; the IDs and `payload.data` are set. Before v2.3.3 that update raised
> in the parser and the event was lost.

## Opt-in events

Some events are off by default because they're frequent, or only for
debugging:

| Event | Turn on with | Why it's off |
|---|---|---|
| `on_raw_presence_update(payload)` | `enable_raw_presences=True` | Presence updates are the most frequent gateway event, and even checking for a listener cost about 13% of their parse time. When it's off, only `on_presence_update` fires, for cached members. |
| `on_raw_typing(payload)` | `enable_raw_typing=True` | Typing events are frequent, and most clients only need `on_typing`. *New in 2.3.3.* |
| `on_socket_raw_receive(msg)`, `on_socket_raw_send(payload)` | `enable_debug_events=True` | Every frame, raw and **unredacted** (tokens included). Don't log them anywhere public. |

`on_typing` is dropped when the typing event's channel isn't cached, or when
no member or user can be resolved. In guild text channels and threads, a
member who isn't cached is built from the event's member data, so those still
fire. `on_raw_typing` fires for every typing event; its `user` is the resolved
member or user (built from the event's member data when the channel isn't
cached but the guild is), or `None`.

## Connection events

| Event | Fires | Doesn't fire |
|---|---|---|
| `on_connect()` | After READY and READY_SUPPLEMENTAL are processed, before member lists are loaded and before `on_ready`. | On a resume. |
| `on_ready()` | When the caches are ready after a **new session**: after READY, and after `chunk_guilds_at_startup` loading (each guild waits at most `max(5, members / 10000)` seconds). | On a resume. It **does** fire again after a reconnect that needed a new session. |
| `on_resumed()` | When a dropped connection resumed the same session. The caches are kept and missed events are replayed. | |
| `on_disconnect()` | Before every reconnect, resumable or not, and on close. | |
| `on_error(event, *args, **kwargs)` | When a handler raises. The default logs `Ignoring exception in <event>` with the traceback. | |
| `on_socket_event_type(event_type)` | For every gateway event, with its name (`'MESSAGE_CREATE'`). Always on; cheap when no handler exists. | |

A new session (a fresh IDENTIFY) **clears the caches** and rebuilds them from
READY, so objects you held from before are stale. A resume keeps everything.

```python
class MyClient(discord.Client):
    async def on_connect(self):
        print('connected; caches filled, member lists still loading')

    async def on_ready(self):
        print('ready:', len(self.guilds), 'guilds')

    async def on_resumed(self):
        print('resumed: nothing was lost')

    async def on_disconnect(self):
        print('disconnected; reconnecting')
```

See [The gateway](gateway.md) for when the client resumes and when it starts
a new session.

## Fork additions

| Event | Arguments | Fires when |
|---|---|---|
| `on_dispatch_backlog` | `pending: int, oldest_age: float` | At least `dispatch_backlog_warning` (1000) handler tasks are pending, at most once every 30 seconds. See [below](#handlers-falling-behind). |
| `on_http_throttle` | `route: str, retry_after: float, scope: Optional[str]` | The HTTP client absorbed a 429 and is about to wait. `scope` is the `X-RateLimit-Scope` header (`'user'`, `'shared'`, `'global'` or `None`). See [Rate limits and pacing](rate-limits-and-pacing.md). |
| `on_raw_typing` | `payload: RawTypingEvent` | Opt-in, above. *2.3.3* |
| `on_raw_thread_update` | `payload: RawThreadUpdateEvent` | Every thread update. *2.3.3* |

## The catalogue

Every event in the Event Reference, grouped. "Cache" means the event needs the
object(s) in the cache; the raw forms are listed in the table above.

### Messages

| Event | Arguments | Notes |
|---|---|---|
| `on_message` | `message` | Every message you can see, including your own and DMs. |
| `on_message_edit` | `before, after` | Cache. Also fires when Discord adds embeds to a message (a link unfurling), with `before.content == after.content`. |
| `on_message_delete` | `message` | Cache. |
| `on_bulk_message_delete` | `messages` | Cache (the cached ones). |
| `on_message_ack` | `message, manual` | A message was marked read on one of your sessions; `manual` is whether it was done explicitly. |
| `on_recent_mention_delete` | `message` | A message was removed from your recent mentions. Raw: `on_raw_recent_mention_delete(message_id)`. |
| `on_typing` | `channel, user, when` | See [above](#opt-in-events). |

### Reactions and polls

| Event | Arguments | Notes |
|---|---|---|
| `on_reaction_add`, `on_reaction_remove` | `reaction, user` | Cache. `reaction.burst` tells a super reaction apart. |
| `on_reaction_clear` | `message, reactions` | Cache. |
| `on_reaction_clear_emoji` | `reaction` | Cache. |
| `on_poll_vote_add`, `on_poll_vote_remove` | `user, answer` | On a poll that allows several answers, fires once per answer added or removed. |

### Guilds

| Event | Arguments | Notes |
|---|---|---|
| `on_guild_join` | `guild` | You joined or created a guild. |
| `on_guild_remove` | `guild` | You left, were kicked or banned, or the guild was deleted. |
| `on_guild_available`, `on_guild_unavailable` | `guild` | A guild came back from or went into an outage. |
| `on_guild_update` | `before, after` | Name, icon, settings and so on. |
| `on_guild_emojis_update` | `guild, before, after` | Lists of emojis. |
| `on_guild_stickers_update` | `guild, before, after` | Lists of stickers. |
| `on_audit_log_entry_create` | `entry` | Needs View Audit Log in that guild. |
| `on_guild_feature_ack` | `payload` | A guild feature (onboarding, highlights...) was acknowledged on one of your sessions. |
| `on_application_command_index_update` | `guild` | The guild's slash command index changed. |
| `on_guild_settings_update` | `before, after` | Your notification settings for a guild changed. |

### Channels

| Event | Arguments | Notes |
|---|---|---|
| `on_guild_channel_create`, `on_guild_channel_delete` | `channel` | |
| `on_guild_channel_update` | `before, after` | |
| `on_guild_channel_pins_update` | `channel, last_pin` | `last_pin` is an aware `datetime` or `None`. |
| `on_private_channel_create`, `on_private_channel_delete` | `channel` | DMs and groups. |
| `on_private_channel_update` | `before, after` | Groups: name, icon, owner. |
| `on_private_channel_pins_update` | `channel, last_pin` | |
| `on_group_join`, `on_group_remove` | `channel, user` | Someone joined or left a group DM. |
| `on_webhooks_update` | `channel` | A channel's webhooks changed. |

### Threads

| Event | Arguments | Notes |
|---|---|---|
| `on_thread_create` | `thread` | A thread was created (or you gained access to one). |
| `on_thread_join` | `thread` | You joined a thread, or it became visible to you. |
| `on_thread_update` | `before, after` | Cache. Raw: `on_raw_thread_update`. |
| `on_thread_remove` | `thread` | The thread left your cache (you lost access). Not the same as deleted. |
| `on_thread_delete` | `thread` | Cache. Raw: `on_raw_thread_delete`. |
| `on_thread_member_join`, `on_thread_member_remove` | `member` (a `ThreadMember`) | |

### Members, users and presences

| Event | Arguments | Notes |
|---|---|---|
| `on_member_join` | `member` | Needs a guild subscription (member updates) for big guilds; see [Member lists](member-lists.md). |
| `on_member_remove` | `member` | Cache. Raw: `on_raw_member_remove`. |
| `on_member_update` | `before, after` | Cache. Nickname, roles, timeout, avatar, flags. |
| `on_user_update` | `before, after` | A user's name, avatar or other profile fields changed. A member update that leaves out a user field keeps the cached value and logs a warning once (2.3.3). |
| `on_member_ban`, `on_member_unban` | `guild, user` | Needs Ban Members to receive. |
| `on_presence_update` | `before, after` | Cache. Status and activities. |
| `on_raw_member_list_update` | `data` | The member sidebar; see [Member lists](member-lists.md). |

### Relationships and social

| Event | Arguments | Notes |
|---|---|---|
| `on_relationship_add` | `relationship` | A friend request (incoming or outgoing), a new friend, or a block. Check `relationship.type`. |
| `on_relationship_update` | `before, after` | For example an incoming request becoming a friendship, or a nickname change. |
| `on_relationship_remove` | `relationship` | |
| `on_friend_suggestion_add` | `friend_suggestion` | |
| `on_friend_suggestion_remove` | `user` | Raw: `on_raw_friend_suggestion_remove(user_id)`. |
| `on_note_update` | `user, note` | Your note on a user changed. Raw: `on_raw_note_update(user_id, note)`. |

### Roles, scheduled events, stages

| Event | Arguments | Notes |
|---|---|---|
| `on_guild_role_create`, `on_guild_role_delete` | `role` | |
| `on_guild_role_update` | `before, after` | |
| `on_scheduled_event_create`, `on_scheduled_event_delete` | `event` | |
| `on_scheduled_event_update` | `before, after` | |
| `on_scheduled_event_user_add`, `on_scheduled_event_user_remove` | `event, user` | Raw: `on_raw_scheduled_event_user_add(event, user_id)` and `..._remove`. |
| `on_scheduled_event_ack` | `event` | |
| `on_stage_instance_create`, `on_stage_instance_delete` | `stage_instance` | |
| `on_stage_instance_update` | `before, after` | |

### Voice, calls, streams, soundboard

| Event | Arguments | Notes |
|---|---|---|
| `on_voice_state_update` | `member, before, after` | Joining, leaving, moving, muting, deafening, streaming. `before.channel` is `None` on join, `after.channel` is `None` on leave. Not dispatched for a member Discord sends no member data for and who isn't cached. |
| `on_voice_channel_effect` | `effect` | Emoji and soundboard effects in voice channels and private calls. *2.3.2* |
| `on_call_create`, `on_call_delete` | `call` | A DM or group call started or ended. |
| `on_call_update` | `before, after` | Ringing, region, participants. |
| `on_stream_create`, `on_stream_available`, `on_stream_unavailable` | `stream` | Go Live streams you're watching or broadcasting. |
| `on_stream_update` | `before, after` | |
| `on_stream_delete` | `stream, reason` | |
| `on_soundboard_sound_create`, `on_soundboard_sound_delete` | `sound` | *2.3.2* See [Soundboard and voice](soundboard-and-voice.md). |
| `on_soundboard_sound_update` | `before, after` | |

### Settings and account

| Event | Arguments | Notes |
|---|---|---|
| `on_settings_update` | `before, after` | Your user settings changed, on any session. See [Settings](settings.md). |
| `on_frecency_settings_update` | `before, after` | Frecency (recently used emojis, GIFs...) changed. |
| `on_required_action_update` | `action` | Discord requires an action (verify email or phone, accept terms). |
| `on_user_feature_ack` | `payload` | A user-level feature was acknowledged. |
| `on_connections_update` | — | Your connected accounts changed. |
| `on_connection_create` | `connection` | |
| `on_connection_update` | `before, after` | |
| `on_connections_link_callback` | `provider, code, state` | The OAuth callback when linking a connection. |
| `on_oauth2_token_revoke` | `token, application_id` | An app's access was revoked. |

### Billing, store, entitlements

| Event | Arguments |
|---|---|
| `on_payment_sources_update`, `on_subscriptions_update` | — |
| `on_payment_client_add` | `purchase_token_hash, expires_at` |
| `on_payment_update` | `payment` |
| `on_premium_guild_subscription_slot_create`, `..._update` | `slot` |
| `on_billing_popup_bridge_callback` | `payment_source_type, path, query, state` |
| `on_library_application_update` | `application` |
| `on_entitlement_create`, `on_entitlement_update`, `on_entitlement_delete` | `entitlement` |
| `on_gift_create`, `on_gift_update` | `gift` |

### Guild join requests and directories

| Event | Arguments |
|---|---|
| `on_join_request_create`, `on_join_request_update` | `join_request` |
| `on_join_request_delete` | `guild, user` (raw: `on_raw_join_request_delete(payload)`) |
| `on_directory_entry_create`, `on_directory_entry_update`, `on_directory_entry_delete` | `directory_entry` |

### Integrations and interactions

| Event | Arguments | Notes |
|---|---|---|
| `on_integration_create`, `on_integration_update` | `integration` | |
| `on_guild_integrations_update` | `guild` | |
| `on_raw_integration_delete` | `payload` | |
| `on_interaction` | `interaction` | You triggered an interaction (a slash command, a button click). See [Interactions and components](interactions-and-components.md). |
| `on_interaction_finish` | `interaction` | The bot acknowledged it. |
| `on_modal` | `modal` | A bot answered your interaction with a modal. |
| `on_application_command_autocomplete_response` | `response` | |

### Dispatched, but not in the API reference

These are dispatched by [`discord/state.py`](../discord/state.py) but aren't
documented in `api.rst`. Upstream doesn't document them either. They work,
but their arguments are only defined by the code:

| Event | Arguments |
|---|---|
| `on_automod_rule_create`, `on_automod_rule_update`, `on_automod_rule_delete` | `rule` |
| `on_automod_action` | `execution` |
| `on_invite_create`, `on_invite_delete` | `invite` |
| `on_session_create` | `session` |
| `on_session_update` | `before, after` |
| `on_session_delete` | `session` |
| `on_auth_session_change` | `auth_session_id` |
| `on_guild_channel_pins_ack`, `on_private_channel_pins_ack` | `channel, last_pin` |
| `on_internal_settings_update` | `before, after` |
| `on_iframe_modal`, `on_iframe_modal_close` | `modal` |

Discord doesn't send every AutoMod gateway event to user accounts, as
upstream's migration guide notes.

## Waiting for events: `wait_for`

```python
# The next message in a channel from a given person
msg = await client.wait_for('message', check=lambda m: m.channel.id == cid and m.author.id == uid, timeout=60)

# Multi-argument events return a tuple
reaction, user = await client.wait_for('reaction_add', check=lambda r, u: r.message.id == mid and u.id == uid)

# Events with no arguments return None
await client.wait_for('resumed')

# Wait for the first of several things
tasks = [
    asyncio.create_task(client.wait_for('message', check=check_msg)),
    asyncio.create_task(client.wait_for('reaction_add', check=check_reaction)),
]
done, pending = await asyncio.wait(tasks, timeout=60, return_when=asyncio.FIRST_COMPLETED)
for task in pending:
    task.cancel()
```

- The name has no `on_` prefix and is case-insensitive.
- `timeout` raises `asyncio.TimeoutError`. Without one, it can wait forever.
- If `check` raises, that `wait_for` fails with the exception.
- `wait_for` works without any `on_*` handler for the event.
- Inside `setup_hook`, `wait_for` deadlocks (the gateway isn't connected yet).

## Errors in handlers

An exception in a handler goes to `on_error(event_method, *args, **kwargs)`
with the exception active (`sys.exc_info()`). The default logs
`Ignoring exception in on_message` and the traceback through the
`discord.client` logger, and the client carries on. If `on_error` itself
raises, that is logged too (`Ignoring exception raised by on_error while handling ...`).
`asyncio.CancelledError` in a handler is not an error and isn't reported.

```python
import sys

class MyClient(discord.Client):
    async def on_error(self, event_method, /, *args, **kwargs):
        exc_type, exc, tb = sys.exc_info()
        await report_to_my_webhook(f'{event_method}: {exc!r}')
```

`commands.Bot` has its own `on_command_error` for errors in commands; see
[ext.commands and ext.tasks](commands-and-tasks.md).

## Handlers falling behind

Each handler runs as its own task. If handlers are slower than events arrive,
tasks pile up and memory grows. At `dispatch_backlog_warning` pending tasks
(1000 by default), the client logs
`Event handler backlog is N (oldest Xs old).` and dispatches
`on_dispatch_backlog(pending, oldest_age)`, at most once every 30 seconds:

```python
class MyClient(discord.Client):
    async def on_dispatch_backlog(self, pending, oldest_age):
        stats = await self.cache_stats()
        print(f'{pending} handlers pending; event loop lag {stats.event_loop_lag * 1000:.0f} ms')
```

Common causes: a blocking call in a handler, awaiting something slow (an API,
a database) for every event, or waiting on a lock most handlers need. Move slow
work to a queue with a fixed number of workers. `cache_stats()` also reports
`dispatch_backlog`, `dispatch_oldest_age`, `dispatch_created`,
`dispatch_completed` and `event_loop_lag` (`-1.0` if the loop was stalled for
over a second).

## Gotchas

- **No intents.** User accounts receive what Discord decides to send, which
  depends on guild subscriptions, not intents. Missing member or presence
  events in a big guild usually mean it isn't subscribed; see
  [Member lists](member-lists.md).
- **Your own actions fire events too.** Sending a message fires `on_message`
  for it; editing your profile fires `on_user_update`. Check the author.
- **`on_ready` isn't "once".** It fires after every new session.
- **`before` may be a copy, `after` is live.** Don't keep `after` and expect it
  to stay as it was.
- **Events about objects you've never seen.** With bounded caches, a cached
  event can be skipped for an evicted object; use the raw form when it
  matters.
