# Events

Register an event by defining an `on_<name>` coroutine on a `Client` subclass,
or with the `@client.event` decorator. For a one-off wait, use
`await client.wait_for('name', check=..., timeout=...)`. The full signatures and
payload classes are in [`docs/api.rst`](../docs/api.rst) under "Event
Reference". This page groups the events and explains the ones that behave
specially.

## Cached and raw events

Most events come in two forms:

- **Cached events** (`on_message_edit`, `on_reaction_add`, `on_member_update`,
  and so on) give you full objects. They only fire when the related object is in
  the cache. With bounded caches (see [Caching and memory](caching-and-memory.md)),
  an evicted member or an old message means no cached event.
- **Raw events** (`on_raw_message_edit`, `on_raw_reaction_add`,
  `on_raw_member_remove`, and so on) don't need the object to be cached. They
  pass a payload object holding the IDs, the raw data and the cached object if
  there is one. They can still be skipped when the payload is unusable, for
  example `on_raw_member_remove` for a guild the client doesn't know.

Use the raw form when you must not miss an event because of the cache.

### Opt-in events

Some events are off by default because they are frequent, or only useful for debugging:

| Event | Turn on with | Why it's off |
|---|---|---|
| `on_raw_presence_update(payload)` | `enable_raw_presences=True` | Presence updates are the most frequent gateway event, and building the payload for each one costs time. When it's off, only `on_presence_update` fires, for cached members. |
| `on_socket_raw_receive(msg)`, `on_socket_raw_send(payload)` | `enable_debug_events=True` | These handlers get raw frames, **without redaction**. Don't log them anywhere public. |

`on_raw_typing` and `on_raw_thread_update` are planned for v2.3.4 (see the
[roadmap](../docs/roadmap.rst)). Until then, a typing event is dropped when its
channel isn't cached, or when no member or user can be resolved. In guild text
channels and threads, a member who isn't cached is built from the event's
payload, so those still fire.

## Event groups

These are taken from the Event Reference in [`docs/api.rst`](../docs/api.rst).

- **Connection:** `on_connect`, `on_disconnect`, `on_ready`, `on_resumed`,
  `on_error`, `on_socket_event_type`
- **Fork additions:**
  - `on_dispatch_backlog(pending, oldest_age)`: event handlers are falling behind.
  - `on_http_throttle(route, retry_after, scope)`: a 429 was absorbed. See [Rate limits](rate-limits-and-pacing.md).
- **Messages:**
  - `on_message`, `on_message_edit`, `on_message_delete`,
    `on_bulk_message_delete`, `on_message_ack`, `on_raw_message_edit`,
    `on_raw_message_delete`, `on_raw_bulk_message_delete`,
    `on_raw_message_ack`
  - Recent mentions: `on_recent_mention_delete`, `on_raw_recent_mention_delete`
  - Typing: `on_typing`
- **Reactions:** `on_reaction_add`, `on_reaction_remove`, `on_reaction_clear`,
  `on_reaction_clear_emoji`, and their `on_raw_*` forms
- **Polls:** `on_poll_vote_add`, `on_poll_vote_remove`, `on_raw_poll_vote_add`,
  `on_raw_poll_vote_remove`. On a poll that allows several answers, these fire
  once for each vote added or removed.
- **Guilds:** `on_guild_join`, `on_guild_remove`, `on_guild_available`,
  `on_guild_unavailable`, `on_guild_update`, `on_guild_emojis_update`,
  `on_guild_stickers_update`, `on_guild_feature_ack`,
  `on_application_command_index_update`, `on_audit_log_entry_create`
- **Channels:**
  - `on_guild_channel_create`, `on_guild_channel_delete`,
    `on_guild_channel_update`, `on_guild_channel_pins_update`
  - Private channels: `on_private_channel_create`, `on_private_channel_delete`,
    `on_private_channel_update`, `on_private_channel_pins_update`
  - Groups: `on_group_join`, `on_group_remove`
  - `on_webhooks_update`
- **Threads:** `on_thread_create`, `on_thread_join`, `on_thread_update`,
  `on_thread_remove`, `on_thread_delete`, `on_raw_thread_delete`,
  `on_thread_member_join`, `on_thread_member_remove`,
  `on_raw_thread_member_remove`
- **Members and users:**
  - `on_member_join`, `on_member_remove`, `on_raw_member_remove`,
    `on_member_update`, `on_user_update`, `on_member_ban`, `on_member_unban`
  - Presences: `on_presence_update`, `on_raw_presence_update`
  - Member list: `on_raw_member_list_update`. This is the member sidebar; see [Member lists](member-lists.md).
- **Roles:** `on_guild_role_create`, `on_guild_role_update`, `on_guild_role_delete`
- **Scheduled events and stages:**
  - `on_scheduled_event_create`, `on_scheduled_event_update`,
    `on_scheduled_event_delete`, `on_scheduled_event_user_add`,
    `on_scheduled_event_user_remove`, their `on_raw_*` forms, and
    `on_scheduled_event_ack`
  - `on_stage_instance_create`, `on_stage_instance_update`,
    `on_stage_instance_delete`
- **Voice:** `on_voice_state_update`, `on_voice_channel_effect` (new in 2.3.2)
- **Soundboard (new in 2.3.2):** `on_soundboard_sound_create`,
  `on_soundboard_sound_update`, `on_soundboard_sound_delete`. See
  [Soundboard and voice](soundboard-and-voice.md) for when these fire.
- **Calls and streams:** `on_call_create`, `on_call_update`, `on_call_delete`,
  `on_stream_create`, `on_stream_available`, `on_stream_unavailable`,
  `on_stream_update`, `on_stream_delete`
- **Relationships and social:** `on_relationship_add`,
  `on_relationship_update`, `on_relationship_remove`,
  `on_friend_suggestion_add`, `on_friend_suggestion_remove`,
  `on_raw_friend_suggestion_remove`, `on_note_update`, `on_raw_note_update`
- **Settings and account:** `on_settings_update`, `on_frecency_settings_update`,
  `on_guild_settings_update`, `on_required_action_update`,
  `on_user_feature_ack`, `on_connections_update`, `on_connection_create`,
  `on_connection_update`, `on_connections_link_callback`,
  `on_oauth2_token_revoke`
- **Billing and store:** `on_payment_sources_update`,
  `on_subscriptions_update`, `on_payment_client_add`, `on_payment_update`,
  `on_premium_guild_subscription_slot_create`,
  `on_premium_guild_subscription_slot_update`,
  `on_billing_popup_bridge_callback`, `on_library_application_update`,
  `on_entitlement_create`, `on_entitlement_update`, `on_entitlement_delete`,
  `on_gift_create`, `on_gift_update`
- **Guild join requests and directories:**
  - `on_join_request_create`, `on_join_request_update`,
    `on_join_request_delete`, `on_raw_join_request_delete`
  - `on_directory_entry_create`, `on_directory_entry_update`,
    `on_directory_entry_delete`
- **Integrations and interactions:**
  - `on_integration_create`, `on_integration_update`,
    `on_guild_integrations_update`, `on_raw_integration_delete`
  - `on_interaction`, `on_interaction_finish`, `on_modal`,
    `on_application_command_autocomplete_response`

## Dispatched, but not in the API reference

These events are dispatched by [`discord/state.py`](../discord/state.py) but
aren't documented in `api.rst`. Upstream doesn't document them either. They
work, but their arguments are only defined by the code shown here:

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

## Handlers and the backlog

Each event handler runs as its own task. If handlers are slower than the
events arriving, tasks pile up. At `dispatch_backlog_warning` pending tasks
(1000 by default), `on_dispatch_backlog(pending, oldest_age)` fires, at most
once every 30 s. `Client.cache_stats()` also reports `dispatch_backlog` and the
event loop lag.
