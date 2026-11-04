# Caching and memory

A user account receives far more than a bot of the same size: member lists,
presences, threads, read states. Upstream discord.py-self caches all of it
with no limit, so memory keeps growing on accounts in large guilds. This fork
caps every big cache and evicts the least recently used entries. The full
reference is [`docs/cache_tuning.rst`](../docs/cache_tuning.rst); this page
explains the model.

## The caps

| Cache | Option | Default | Never evicted |
|---|---|---|---|
| Members per guild | `max_member_cache` | 10,000 | You, and members in voice channels |
| Members, all guilds | `max_total_member_cache` | 50,000 | Same as above |
| Threads per guild | `max_thread_cache` | 5,000 | Threads you joined |
| Threads, all guilds | `max_total_thread_cache` | 25,000 | Same as above |
| Presences per guild | `max_guild_presence_cache` | 5,000 | — |
| Presences outside guilds | `max_presence_cache` | 20,000 | — |
| Messages | `max_messages` | 1,000 | — |

Pass `None` for any cap to get upstream's unbounded behaviour. Three small
caches are fixed: interactions (100), call messages (100) and iframe modals (32).

### What "least recently used" means here

- **Write recency, not read recency.** An entry's place in the eviction order moves
  when a gateway event touches it, or when you call `Guild.touch_member`, not
  when you call `guild.get_member`. Lookups stay allocation-free that way. As a
  result, a member you look up constantly but who generates no traffic is
  evicted before an active one. Call `touch_member` if you need to keep them.
- **Kept warm automatically.** Members who send messages or are mentioned are
  re-cached and protected. Online members stay warm because their presence
  updates refresh them.
- **The global cap evicts from the fullest guilds first.** Guilds move toward
  the average size the cap implies, and guilds at or below that average aren't
  touched, so the small guilds you actually use keep their members.
- **Batched.** When a cache goes over its cap, it's trimmed to about 95% of
  the cap in one pass. The cost is paid once for every ~5% of new insertions,
  not on every insert.

### When something isn't in the cache

`guild.get_member(id)` returning `None` only means the member went cold.
Fetch them with `Guild.fetch_member` (REST), or `Guild.query_members`
(gateway, by name prefix or IDs). Threads come back with
`Guild.fetch_channel`. An evicted member's presence is recreated by their
next presence update, usually within 30–60 seconds.

## Sizing

Measured on Python 3.14: a cached member costs about **0.7 KB**, a thread about
**0.8 KB** and a presence about **0.3–0.5 KB**. The 50,000-member global cap
therefore keeps the member cache to about 35 MB.

| Account | Suggested settings |
|---|---|
| Few small guilds (under 10k members in total) | Defaults |
| 30–60 guilds | Defaults |
| Many large guilds (20k+ members each) | `max_total_member_cache=50_000` to `100_000`, `max_total_thread_cache=10_000` to `50_000`, `max_guild_presence_cache=5_000` to `10_000` |
| Hosts with little memory (under 1 GB, several accounts) | `max_total_member_cache=10_000`, `max_total_thread_cache=5_000`, `max_guild_presence_cache=2_000` |

Measured numbers:

- [`tests/memory_bench.py`](../tests/memory_bench.py) `--members 50000`
  (100 guilds, 6,000 channels, 50,000 members) retains **45.27 MiB** at v2.3.2.
- A 150k-member account drops from about 100 MB of ever-growing caches to about
  38 MB with a hard ceiling. This figure is from the README's benchmark.
- On a forum guild with 30,000 threads, the thread cap cuts the thread cache from
  about 24 MB to about 4 MB.
- A process costs 55–60 MB before any account logs in. Running several accounts in
  one process pays that once (see [Getting started](getting-started.md#several-accounts)).

## Watching it

```py
from discord.ext import tasks

@tasks.loop(seconds=60)
async def monitor():
    stats = await client.cache_stats()
    hits = stats.member_hits / max(1, stats.member_hits + stats.member_misses)
    print(stats.members, f'{hits:.0%} hit rate', 'backlog', stats.dispatch_backlog)
```

`Client.cache_stats()` returns a `CacheStats` with:

- each cache's size and cap
- eviction counters
- the `get_member` hit and miss counts
- a per-guild breakdown
- the event handler backlog
- the event loop lag

A member hit rate below about 80% on an account you care about means the caps
are too tight. The first time each cache evicts because it reached its cap, the
library logs one INFO line naming that cache.

## Garbage collection

`gc_freeze=True` (the default) calls `gc.freeze()` shortly after READY, so the
cyclic garbage collector stops rescanning the large, long-lived cache graph.
That cuts GC pauses and background CPU on big accounts. On reconnect the graph
is unfrozen so the old caches can be freed. Guild data deleted while frozen is
only freed at the next reconnect; turn the option off if that matters to you.

## Faster hot paths

Faster hot paths also reduce CPU and memory churn:

- message cache lookups are O(1)
- member, thread and presence updates that change nothing are suppressed
- `MESSAGE_UPDATE` is parsed lazily when nobody listens for it
- HTTP route keys and header templates are cached
- regular expressions are precompiled
- `aiohttp` is imported only when needed

The README lists the measured gains. The stress benchmark's soak phase pushes
millions of churning events through the real parse path to check that the
caps hold (see [Development](development.md#benchmarks)).
