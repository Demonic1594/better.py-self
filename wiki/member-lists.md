# Member lists

Bots ask for members with intents. User accounts can't, and Discord doesn't
push a user client the full member list of a large guild: it sends what the
official app's UI would show. This page explains what you get by default, how
to load whole member lists (gateway requests or sidebar scraping), searching
for members, the timeouts and failure modes, and how to manage subscriptions
yourself.

The code is `Guild.subscribe`, `Guild.subscribe_to`, `Guild.chunk`,
`Guild.fetch_members` and `Guild.query_members` in
[`discord/guild.py`](../discord/guild.py), and the `MemberSidebar` and
chunking code in [`discord/state.py`](../discord/state.py).
[`docs/guild_subscriptions.rst`](../docs/guild_subscriptions.rst) is
upstream's description of Discord's side.

## What you get by default

- **Members at login.** READY and READY_SUPPLEMENTAL carry a small part of
  each guild's members (you, and members Discord chooses to include, such as
  those in voice). `guild.members` starts with those.
- **Members from events.** Everyone who sends a message, reacts, joins voice
  or appears in an event while you're connected is cached as they appear
  (subject to the [cache caps](caching-and-memory.md)).
- **Guild subscriptions.** With `guild_subscriptions=True` (the default), the
  library subscribes to **every** guild after READY, with typing, activities,
  threads and member updates. That's what makes Discord send member join,
  update and remove events, the thread list, and in guilds of 75,000 members
  or more, any message events at all. See [The gateway](gateway.md#guild-subscriptions).
- **Startup loading.** With `chunk_guilds_at_startup` (on by default, unless
  subscriptions or the `joined` member cache flag are off), `on_ready` waits
  until each guild that isn't fully loaded has been loaded with one of the
  methods below.

Whatever your subscriptions, you always get events for actions **you** take
(nicknaming, kicking or banning someone). Raw events like
`on_raw_member_remove` are always dispatched; cached ones like
`on_member_update` only when the member is cached.

> **Gotcha: `len(guild.members)` is not the member count.** It's the number of
> *cached* members. `guild.member_count` is Discord's number, and
> `guild.online_count` the online count when the sidebar reported it.

## Loading the whole member list

Two methods exist, and the library picks by permission:

| | Gateway request (op 8) | Sidebar scraping (op 37) |
|---|---|---|
| **Needs** | Kick Members, Ban Members or Manage Roles in the guild | A channel that `@everyone` can read |
| **Returns** | Every member, offline included | What the member sidebar shows: **only online members** in guilds where the sidebar hides offline ones |
| **Speed** | Fast: chunks of 1,000 | Slower: ranges of 100 rows |
| **Used by** | `chunk()`, `fetch_members()` | `chunk()`, `fetch_members()` when the account lacks those permissions, or `force_scraping=True` |

The sidebar hides offline members when
`member_count + hoisted roles + 2 >= 1000` (the official client's rule, which
may be slightly off because Discord caches member list groups).

### `Guild.chunk()`

```python
members = await guild.chunk()           # loads and caches every member it can get
print(len(members), 'of', guild.member_count)
```

- Raises `ClientException('This guild is not subscribed to')` if the guild
  isn't subscribed, and `ClientException('This guild cannot be chunked')` when
  neither method works (no permission, and the sidebar hides offline members).
- Uses the gateway request when you have the permissions, otherwise scrapes.
- Caches the members if the `joined` member cache flag is on (it is by
  default), even with `cache=False`.
- A second `chunk()` while one is running joins it.
- `guild.chunked` tells you whether the guild is fully loaded.

### `Guild.fetch_members()`

```python
members = await guild.fetch_members(
    channels=[guild.get_channel(rules_channel_id)],   # where to read the sidebar from
    cache=False,                                      # don't add them to the cache
    force_scraping=False,                             # scrape even with permissions
    delay=0,                                          # seconds between requests
)
```

- Returns the members without caching them unless `cache=True` (and that
  cache isn't kept up to date afterwards).
- `channels`: the channels whose sidebar to read. By default the library picks
  up to 5 channels `@everyone` can read (1 when chunking); more channels make
  big lists faster. An unknown channel raises
  `ValueError('Channel ... not found in guild ...')`.
- No usable channel raises `ClientException('Failed to automatically choose channels; please specify them manually')`.
- Returns `[]` for a guild the client has evicted.

### How scraping works

1. The library subscribes to the chosen channels' member lists with op 37,
   asking for ranges of 100 rows (`[0, 99]`, `[100, 199]`, ...), up to four
   ranges per request.
2. Discord answers with `GUILD_MEMBER_LIST_UPDATE` events containing a `SYNC`
   for each range; the members in them are collected (and cached if asked).
3. It continues until the list (members plus group headers) is covered.

In guilds of 75,000 members or more, or after a range times out, scraping
switches to **safe mode**: one range at a time. If a range still gets no
answer, it raises `InvalidData('Did not receive a response from Discord')`.

Scraping is a snapshot: later `UPDATE`, `INSERT` and `DELETE` operations on
the list are ignored, and scraping dispatches no member join or remove
events. `on_raw_member_list_update(data)` gives you each update raw, before
the library processes it, if you want to keep your own live sidebar.

## Timeouts and lost requests

- Each request (gateway chunk or scrape range) waits up to
  `max(5, member_count / 10000)` seconds; startup loading runs all guilds in
  parallel with those timeouts.
- A chunk request that gets **no reply at all** for 60 seconds (one second per
  10,000 members above 600,000) is given up: its waiters get `InvalidData`
  (`The member chunk request for guild ID ... got no reply for 60 seconds (N members received).`).
  Every chunk received restarts the clock, so a slow but progressing request
  on a huge guild isn't cut off.
- A new `chunk()` or `fetch_members()` that finds the running request dead
  logs `Member chunk request for guild ID ... got no reply; sending a new one.`
  and sends a fresh one. Before v2.3.3, a lost reply (for example on a
  connection that died and resumed) made `chunk()` wait forever.

```python
try:
    members = await guild.chunk()
except discord.InvalidData:
    # Discord didn't answer; it's safe to try again later
    members = list(guild.members)
```

### When the sidebar returns nothing

The library logs, for example:

```text
Member list scraping for guild ID ... got no member list from Discord (no SYNC
for the requested ranges in channel(s) ...); returning no members. Retrying
later or passing channels explicitly may help.
```

Discord sent no member list for the channels tried. That happens when the
channels can't be seen by everyone, or the guild hides its member list. Pass
`channels=` that everyone can view. See also dolfies/discord.py-self#907,
where `fetch_members` works in one guild and not another.

## Searching for members

```python
# By username or nickname prefix (gateway, op 8)
found = await guild.query_members(query='ali', limit=10)

# By IDs (gateway, op 8)
found = await guild.query_members(user_ids=[1111, 2222], presences=True)

# Among the 10,000 most recently joined members (gateway, op 35)
recent = await guild.query_recent_members(query='ali', limit=50)

# One member by ID (REST)
member = await guild.fetch_member(1111)
```

| Method | Limits | Notes |
|---|---|---|
| `query_members(query=..., limit=5)` | At most 100 results | Pass exactly one of `query` or `user_ids` (`TypeError` otherwise). Cached by default (`cache=True`). According to Discord's user-API docs, listing everyone with an empty query needs one of the three permissions above. `subscribe=True` also subscribes you to their presence updates. |
| `query_recent_members(query=None, limit=1000)` | The 10,000 most recently joined; up to 10,000 results | Paged by the library, 30 seconds per page. No presences. |
| `fetch_member(user_id)` | One member | REST; not added to the cache. Raises `NotFound` if they aren't in the guild. |

## Managing subscriptions yourself

Set `guild_subscriptions=False` to save bandwidth and memory on accounts in
many busy guilds, and subscribe only where you need events:

```python
client = discord.Client(guild_subscriptions=False)   # chunk_guilds_at_startup is then off too

@client.event
async def on_ready():
    guild = client.get_guild(WATCHED_GUILD_ID)
    await guild.subscribe(typing=True, member_updates=True)
    # Presence and member updates for specific people
    await guild.subscribe_to(members=[discord.Object(id=uid) for uid in WATCHED_USERS])
```

Without subscriptions:

- large guilds send no message events at all;
- `guild.threads` holds only the threads you joined;
- member join, update and remove events stop;
- most `on_user_update` events stop;
- the member and user caches stay mostly empty;
- `chunk_guilds_at_startup=True` raises `ClientException`.

Voice states and channel unreads are kept up to date passively either way;
there are no real-time events for them.

## Presences

There's no way to get presences for **every** member of a big guild on a user
account. You receive presences for:

- friends, implicit relationships, and people you have an open DM (and a
  mutual guild) with;
- members of guilds you're subscribed to with `activities`, as Discord decides;
- members you subscribed to with `subscribe_to(members=...)`;
- members returned by `query_members(presences=True)`.

## Live numbers

- **v2.3.2:** `fetch_members(cache=True)` on a 694-member guild returned all
  694 members in 0.6 seconds.
