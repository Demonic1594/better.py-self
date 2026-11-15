# Caching and memory

A user account receives far more than a bot of the same size: member lists,
presences, threads, read states. Upstream discord.py-self caches all of it
with no limit, so memory keeps growing on accounts in large guilds. This fork
caps the big caches and evicts their least recently used entries. This page
explains what is cached, how eviction works, how to size the caches, and how
to watch them. The tuning reference is [`docs/cache_tuning.rst`](../docs/cache_tuning.rst).

## What is cached

| Cache | Capped by | Default | Eviction order | Never evicted |
|---|---|---|---|---|
| Members per guild | `max_member_cache` | 10,000 | Least recently **updated** | You, and members in a voice channel |
| Members, all guilds | `max_total_member_cache` | 50,000 | Fullest guilds first | Same |
| Threads per guild | `max_thread_cache` | 5,000 | Least recently active | Threads you're a member of |
| Threads, all guilds | `max_total_thread_cache` | 25,000 | Fullest guilds first | Same |
| Presences per guild | `max_guild_presence_cache` | 5,000 | Least recently **read** | — |
| Presences outside guilds (friends, DMs) | `max_presence_cache` | 20,000 | Least recently read | — |
| Messages | `max_messages` | 1,000 | **Oldest first** (arrival order) | — |
| Users | — (weak references) | — | Dropped when no member, message or relationship references them | — |

Not capped: guilds and their channels and roles, emojis, stickers, read
states, relationships, private channels (DMs and groups). They grow with what
your account can see, not with traffic.

A few small caches have fixed sizes: call messages (100), recent interactions
(100), interaction nonces (50), autocomplete responses (50) and iframe modals
(32).

### Setting the caps

```python
client = discord.Client(
    max_member_cache=5_000,         # per guild
    max_total_member_cache=30_000,  # across all guilds
    max_messages=2_000,
)
```

- `None` removes a cap (upstream's unbounded behaviour)...
- ...except `max_messages=None`, which turns the message cache **off**.
- `0` or a negative number silently means the default.

## How eviction works

### Members: write recency

A member's place in the eviction order moves when the gateway **updates**
them, not when you read them:

- a member update, a presence update, sending a message, or being mentioned
  moves a member to the "recent" end;
- `guild.get_member(id)` doesn't (lookups stay allocation-free);
- `guild.touch_member(user_id)` moves one explicitly (it does nothing if the
  member isn't cached).

So a member you look up constantly but who generates no traffic is evicted
before an active one. If you need specific members to stay, call
`touch_member` on them, or keep their IDs and fetch on demand.

```python
VIPS = {111111111111111111, 222222222222222222}

@tasks.loop(minutes=5)
async def keep_vips_warm():
    for guild in client.guilds:
        for user_id in VIPS:
            guild.touch_member(user_id)
```

**The global cap evicts from the fullest guilds first.** Every 1,024 member
additions, the client checks the total; when it's over, guilds above the
average share (`cap / number of guilds`) are trimmed toward it, and guilds at
or below it aren't touched. The small guilds you actually use keep their
members while a 500,000-member guild absorbs the cuts.

**Eviction is batched.** When a guild goes over its cap, it's trimmed to about
95% of the cap in one pass, so the cost is paid once per ~5% of new
insertions, not on every insert. Evicting a member also drops their presence.

### Presences: read recency

Presences are the opposite: reading one (`member.status`, `member.activities`)
moves it to the recent end. A presence that's evicted comes back with the
member's next presence update from Discord.

### Messages: arrival order

The message cache is a fixed-size queue across all channels: the oldest
message received is dropped first, and reading a message doesn't keep it. A
busy channel can push a quiet channel's messages out quickly. If you need edits
and deletes for older messages, raise `max_messages` or use the raw events.

### The first eviction

The first time each cache reaches its cap, the client logs one INFO line:
`The member cache reached its cap and now evicts the least recently used entries...`.
That's expected on big accounts, and a hint to check the sizes if it appears
on a small one.

## When something isn't in the cache

`guild.get_member(id)` returning `None` means the member isn't cached. They
may have been evicted, or never cached because of `member_cache_flags` or
`guild_subscriptions=False`. Options, cheapest first:

| Need | Use |
|---|---|
| One member, by ID | `await guild.fetch_member(id)` (REST; not added to the cache) |
| Members by name prefix or a list of IDs | `await guild.query_members(query='ab', limit=20)` or `query_members(user_ids=[...])` (gateway; cached by default) |
| A thread | `await guild.fetch_channel(thread_id)` |
| A message | `await channel.fetch_message(message_id)` |
| Whole member lists | [Member lists](member-lists.md) |

For events, use the [raw events](events.md#cached-and-raw-events): they fire
for uncached objects.

## Member cache flags

`member_cache_flags` decides which members are cached at all:

```python
# Cache members only while they're in voice
flags = discord.MemberCacheFlags.none()
flags.voice = True
client = discord.Client(member_cache_flags=flags)
```

| Flag | Caches |
|---|---|
| `joined` (alias `other`) | Members who appear in events, joined while you're online, or were chunked or scraped. |
| `voice` | Members in voice channels. In voice-only mode (`joined` off) they're removed when they leave voice. |

Your own member is always cached. With every flag off, users aren't cached
either: each event creates fresh `User` objects. `MemberCacheFlags(voice=True)`
keeps `joined` on (the constructor starts from "all"); start from `.none()`
as above. See [Client options](client-options.md#member_cache_flags).

## Sizing

Measured on Python 3.14: a cached member costs about **0.7 KB**, a thread about
**0.8 KB** and a presence about **0.3 to 0.5 KB**. The 50,000-member global cap
therefore keeps the member cache to about 35 MB.

| Account | Suggested settings |
|---|---|
| Few small guilds (under 10,000 members in total) | Defaults |
| 30 to 60 guilds | Defaults |
| Many large guilds (20,000+ members each) | `max_total_member_cache=50_000` to `100_000`, `max_total_thread_cache=10_000` to `50_000`, `max_guild_presence_cache=5_000` to `10_000` |
| Hosts with little memory (under 1 GB, several accounts) | `max_total_member_cache=10_000`, `max_total_thread_cache=5_000`, `max_guild_presence_cache=2_000` |

Measured numbers:

- [`tests/memory_bench.py`](../tests/memory_bench.py) `--members 50000`
  (100 guilds, 6,000 channels, 50,000 members) retains **45.27 MiB**, the same
  at v2.3.2 and v2.3.3.
- A 150k-member account drops from about 100 MB of ever-growing caches to
  about 38 MB with a hard ceiling (the README's benchmark).
- On a forum guild with 30,000 threads, the thread cap cuts the thread cache
  from about 24 MB to about 4 MB.
- A process costs 55 to 60 MB before any account logs in. Running several
  accounts in one process pays that once (see
  [Getting started](getting-started.md#several-accounts)).
- The stress benchmark's `multi` phase measures about 7 MB of READY state per
  account.

## Watching it: `cache_stats()`

```python
from discord.ext import tasks


@tasks.loop(seconds=60)
async def monitor():
    s = await client.cache_stats()
    hit_rate = s.member_hits / max(1, s.member_hits + s.member_misses)
    print(
        f'members {s.members}/{s.total_member_cache_cap} (evicted {s.evicted_members}, hit rate {hit_rate:.0%}), '
        f'threads {s.threads}, presences {s.guild_presences}+{s.dm_presences}, messages {s.messages}, '
        f'backlog {s.dispatch_backlog}, loop lag {s.event_loop_lag * 1000:.0f} ms'
    )


@client.event
async def on_ready():
    if not monitor.is_running():
        monitor.start()
```

`await client.cache_stats()` returns a `CacheStats` named tuple:

| Field | Meaning |
|---|---|
| `guilds` | Guilds cached. |
| `members`, `member_cache_cap`, `total_member_cache_cap` | Cached members, and the per-guild and global caps. |
| `evicted_members` | Members evicted so far. |
| `member_hits`, `member_misses` | `Guild.get_member` calls that found or didn't find the member. Internal lookups (resolving mentions, authors) count too. |
| `threads`, `thread_cache_cap`, `total_thread_cache_cap`, `evicted_threads` | The same for threads. |
| `guild_presences`, `guild_presence_cap`, `dm_presences`, `dm_presence_cap`, `evicted_presences` | Presences in guilds and outside them. |
| `messages`, `message_cache_cap` | The message cache. |
| `dispatch_backlog`, `dispatch_oldest_age` | Handler tasks running now, and how long the oldest has run. |
| `dispatch_created`, `dispatch_completed` | Handler tasks started and finished since the connection began. |
| `event_loop_lag` | How late a callback scheduled now runs, in seconds; `-1.0` if the loop was stalled for over a second. |
| `per_guild` | `{guild_id: {'members', 'threads', 'presences', 'evicted_members', 'evicted_threads', ...}}` |

Most counters reset on reconnect. A member hit rate below about 80% on an
account you care about means the caps are too tight for how you use it.
[`examples/cache_stats_monitor.py`](../examples/cache_stats_monitor.py) is a
complete monitor.

## Garbage collection

Two mechanisms keep Python's cyclic garbage collector from scanning huge
cache graphs over and over:

- **During big parses** (READY, READY_SUPPLEMENTAL, large member chunks) the
  collector is paused, then restored.
- **`gc_freeze=True`** (the default): about 2 seconds after READY, the client
  runs one full collection and calls `gc.freeze()`, which moves everything
  alive into a permanent generation the collector ignores. That removes most
  GC pauses and background CPU on big accounts.

Details of the freeze:

- It's undone on reconnect and close, so the old caches can be freed.
- Objects deleted while frozen (guilds you left, say) are only reclaimed
  after the next reconnect. Turn the option off if that matters to you.
- `gc.freeze()` is process-wide. With several clients in one process, the
  freeze is released only when the last client releases it.
- The one full collection blocks the event loop briefly; its duration is
  logged at DEBUG.

## Faster hot paths

The same work that bounds memory also reduces CPU and churn:

- message cache lookups are O(1) (an index next to the queue);
- member, thread and presence updates that change nothing are suppressed;
- `MESSAGE_UPDATE` is parsed lazily when nobody listens for it;
- HTTP route keys and header templates are cached;
- regular expressions are precompiled;
- `aiohttp` is imported only when needed.

The README lists the measured gains. The stress benchmark's soak phase pushes
a million churning events through the real parse path and checks that the
caps hold and live objects plateau (see [Development](development.md#benchmarks)).

## Gotchas

- **Holding a reference keeps an object alive, not current.** An evicted
  member you stored somewhere stops receiving updates. Store IDs.
- **`len(guild.members)` isn't the member count** on a user account. Use
  `guild.member_count` (Discord's number) and see [Member lists](member-lists.md).
- **Cached events need the cache.** `on_message_edit` for a message pushed out
  of the message cache doesn't fire; `on_raw_message_edit` does.
- **Tight caps cost requests.** Every miss you fill with `fetch_*` is an API
  call. Raise the caps before adding fetches in hot paths.
