# Member lists

Bots ask for members with intents. User accounts can't, and Discord doesn't
push them the full member list of a large guild. It sends a user client what its
UI would show, through **guild subscriptions**. The full list can be fetched on
request (`fetch_members`, `chunk`): through the gateway when you have the right
permissions, or by scraping the member sidebar, which can return everyone in a
small enough guild with a channel `@everyone` can see. This page explains what
you get and how to get more.

The reference is [`docs/guild_subscriptions.rst`](../docs/guild_subscriptions.rst);
the code is `Guild.subscribe`, `Guild.subscribe_to`, `Guild.fetch_members` and
`Guild.chunk` in [`discord/guild.py`](../discord/guild.py).

## What you get automatically

With `guild_subscriptions=True` (the default), on connect:

- **Guilds under 75,000 members:** the client subscribes to them automatically.
  You get their message, reaction and typing events, member updates and the
  thread list. You are also subscribed to your friends, implicit relationships
  and the people you have open DMs with.
- **Guilds of 75,000 members or more:** you get **no non-stateful events**
  (`on_message`, `on_message_edit`, `on_message_delete`, and so on) until you
  subscribe yourself with `Guild.subscribe()`.

Whatever your subscriptions, you always get events for actions **you** take,
such as nicknaming, kicking or banning someone. Raw events like
`on_raw_member_remove` are always dispatched. Cached events like
`on_member_update` only fire when the member is cached.

Voice states and channel unreads are kept up to date passively; there are no
real-time events for them.

## Managing subscriptions yourself

Set `guild_subscriptions=False` to save bandwidth and memory. You then
subscribe with `Guild.subscribe(...)` (typing, threads, member updates) and
`Guild.subscribe_to(members=..., threads=...)` /
`Guild.unsubscribe_from(...)` for specific members or thread member lists.
`chunk_guilds_at_startup` doesn't work in this mode, because it depends on
guild subscriptions.

Subscription changes are debounced, so they can take up to about half a second
to apply.

Without subscriptions:

- large guilds may send no message events at all
- `Guild.threads` only holds the threads you joined
- member events stop
- most `on_user_update` events stop
- the member and user caches stay mostly empty

## Getting the whole member list

`await guild.fetch_members(channels=..., cache=False, force_scraping=False, delay=0)`
picks its method by permission:

- **With `kick_members`, `ban_members` or `manage_roles`:** it requests members
  normally over the gateway (op 8), which includes offline members.
- **Otherwise it scrapes the member sidebar.** That's the same list the official
  client shows, read through `GUILD_MEMBER_LIST_UPDATE` (op 37 bulk
  subscriptions). It is slower, and in large guilds it **only returns online
  members**. "Large" is decided by `member_count + hoisted roles + 2 >= 1000`
  (`Guild._offline_members_hidden` in `guild.py`), not by the member count alone.

`channels` takes up to 5 channels to read the sidebar from; more channels make
it faster. `delay` waits between requests. `force_scraping=True` scrapes even when
you have the permissions. `cache=True` also caches the members, but that cache
isn't kept up to date afterwards.

`Guild.chunk()` caches the full list on the same rules. A guild can be chunked
if you have one of the three permissions, or if it isn't large by the same rule
(`member_count + hoisted roles + 2` under 1,000) and has a channel everyone can see.

### Live numbers (v2.3.2)

`fetch_members(cache=True)` on a 694-member guild returned all 694 members in
0.6 s.

### When the sidebar returns nothing

The library logs, for example: `Member list scraping for guild ID ... got no
member list from Discord (no SYNC for the requested ranges in channel(s) ...);
returning no members. Retrying later or passing channels explicitly may help.`

It means Discord sent no member-list sync for the channels tried. That happens
when the channels picked can't be seen by everyone, or the guild hides its
member list. Pass `channels=` that everyone can view. See also
dolfies/discord.py-self#907, where `fetch_members` works in one guild and not
another.

## Single members and searches

- `Guild.fetch_member(user_id)` fetches one member over REST.
- `Guild.query_members(query=..., user_ids=..., limit=...)` searches over the
  gateway (op 8). According to Discord's user-API docs, a user account needs
  one of the three permissions above to list everyone. A prefix query returns
  at most 100 members.
- Discord also has a "search recent members" opcode (35) for the 10,000 most
  recently joined members. The library defines it as `SEARCH_RECENT_MEMBERS`.

## Raw member-list updates

`on_raw_member_list_update(data)` gives you each `GUILD_MEMBER_LIST_UPDATE`
before the library processes it, for building your own sidebar view.
