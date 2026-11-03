.. currentmodule:: discord

.. _cache_tuning:

Cache Tuning Guide
====================

The client maintains several in-memory caches (members, presences, threads,
messages). For accounts in large or numerous guilds these caches dominate
memory usage, so every cache is capped and configurable. This page explains
the knobs, the defaults, and how to tune them for your workload.

All options are :class:`Client` constructor parameters. Passing ``None``
disables a cap entirely (the cache then grows without bound, like older
versions of the library).

Quick recommendations
----------------------

**Few small guilds** (under 10k members total):
    The defaults are fine everywhere.

**Medium accounts** (30-60 guilds):
    The defaults are fine everywhere.

**Many large guilds** (20k+ members each):
    ``max_total_member_cache=50000`` to ``100000``,
    ``max_total_thread_cache=10000`` to ``50000``,
    ``max_guild_presence_cache=5000`` to ``10000``.

**Memory-constrained hosts** (under 1 GB, many accounts):
    ``max_total_member_cache=10000``, ``max_total_thread_cache=5000``,
    ``max_guild_presence_cache=2000``.

Rough per-object costs, measured on Python 3.14: a cached member is about
0.7 KB, a thread about 0.8 KB, and a presence about 0.3-0.5 KB. A 50,000
member global cap therefore bounds the member cache at roughly 35 MB.

Member caches
--------------

``max_member_cache`` (default ``10000``)
    Per-guild cap. When exceeded, the least recently used members are
    evicted. You (the client user) and members in voice channels are never
    evicted. Members that send messages or are mentioned are re-cached
    automatically, and online members stay warm because presence updates
    refresh their recency.

``max_total_member_cache`` (default ``50000``)
    Global cap across all guilds. When exceeded, guilds converge toward the
    average size implied by the cap; guilds at or below the average are left
    untouched, so small guilds you actually use keep their members.

Evicted members can always be retrieved with :meth:`Guild.fetch_member`
or :meth:`Guild.query_members`; ``guild.get_member`` returning ``None``
just means the member went cold. Use :meth:`Client.cache_stats` to watch
the hit/miss ratio — a ratio below ~80% on an account you care about means
the caps are too tight.

The member cache is *write-recency* ordered: LRU positions advance on
gateway events and explicit touches, not on ``get_member`` reads, so the
hot path stays allocation-free. A member you query constantly but who
generates no traffic is evicted before an actively-updating one; if that
matters for your workload, touch members on use (:meth:`Guild.touch_member`).

Eviction runs in amortized batches: when a cache crosses its cap it is
trimmed to ~95% of the cap in one pass, so the cost is paid once per ~5%
of the cache size in new insertions rather than on every insert.

Thread caches
--------------

``max_thread_cache`` (default ``5000``) and ``max_total_thread_cache``
(default ``25000``) behave like their member counterparts. Threads you have
joined are never evicted; threads with recent messages or member activity
stay warm. Evicted threads are fetchable via :meth:`Guild.fetch_channel`.
On a 30,000-thread forum guild, the cap reduces the thread cache from
~24 MB to ~4 MB.

Presence caches
----------------

``max_guild_presence_cache`` (default ``5000``) caps the per-guild presence
store; ``max_presence_cache`` (default ``20000``) caps the store used for
non-guild presences (relationships, DMs). Presences are transient — an
evicted member's presence is recreated by their next presence update,
typically within 30-60 seconds.

Other caches
-------------

``max_messages`` (default ``1000``) is the message cache. The interaction
cache (100), call message cache (100), and iframe modal cache (32) are
small and currently fixed.

Runtime observability
----------------------

:meth:`Client.cache_stats` returns a :class:`CacheStats` snapshot with every
cache's size, cap, eviction counters, ``get_member`` hit/miss ratio, a
per-guild breakdown, the event handler backlog, and the event loop lag.
Poll it periodically (for example from a ``tasks.loop``) to watch memory
behavior and tune caps empirically.

The ``dispatch_backlog_warning`` option (default ``1000``) dispatches
:func:`on_dispatch_backlog` when event handlers queue up faster than they
complete — the single most common silent failure mode for long-running
accounts.

Stress baseline
----------------

``tools/stress_bench.py`` feeds pre-serialised gateway frames through the real
receive, parse and dispatch path of a 100-guild account, offline. It measures
the library's own CPU and memory, not socket I/O or Discord's latency. Run it
with ``--heavy`` for the full profile below, or ``--phase <name>`` for one
phase (each phase runs in its own process so RSS figures are not polluted by
earlier phases). It exits non-zero when a gate fails: a cache over its cap,
memory still growing after the caches are full, a backlog that does not drain,
or a library warning on a well-formed stream.

Baseline from ``--heavy`` on a 4-vCPU cloud container, Python 3.13, with the
``[speed]`` extra (orjson) installed, default cache caps. Treat the absolute
numbers as relative to that machine; compare runs on the same host.

.. list-table::
   :header-rows: 1
   :widths: 28 72

   * - Phase
     - Result
   * - Throughput, 1M mixed events
     - 37,200 events/s with no handlers (27 us each, p99 37 us); 30,200
       events/s with six async handlers attached (about 595k handler runs).
   * - Soak, 5M churning events
     - 22,400 events/s including cache eviction. Members held at about 50.5k
       (cap 50k plus batch slack), messages at 1,000, presences under 20. RSS
       reached 172 MiB by 1.2M events and 185 MiB at 5M; the last million
       events added 0.7 MiB. No library warnings.
   * - Slow-handler burst, 300k events
     - Handlers sleeping 20 ms each. The backlog drained completely, every
       handler task completed, ``on_dispatch_backlog`` fired, memory after the
       drain was +58 MiB.
   * - 40 accounts, one process
     - About 6.6 MiB per account after READY and about 36 MiB per account after
       50k events each (cache caps filling), on top of about 68 MiB of
       interpreter and imports paid once. Aggregate 14,900 events/s on one
       core.
   * - Persistence, 500k READY-time members
     - 9.2 MiB snapshot (synthetic payloads compress well; real ones will be
       larger). Replayed in a fresh process with the default caps (55k of the
       500k members kept), the process peaked at 128 MiB and replay took about
       4.5 s. The previous single-document format peaked near 2 GiB on the same
       data, because the whole snapshot was decoded before the caps trimmed it;
       snapshots in that format still load, at that cost, until the next write
       converts them.
   * - 1M malformed or truncated frames
     - No exception escaped the receive path. 143k frames were undecodable and
       dropped; the parsers logged 378k warnings and RSS grew 80 MiB.

Open questions this baseline does not answer: real-account traffic shape
(use ``tools/profile_live.py``), voice, and socket-level behaviour.

Garbage collection
-------------------

``gc_freeze`` (default ``True``) freezes the startup object graph shortly
after READY so the cyclic garbage collector stops rescanning long-lived
caches, reducing GC pauses on large accounts. On reconnect the graph is
unfrozen so reset caches are reclaimed. Guild data deleted while frozen is
only reclaimed on the next reconnect; disable this if that matters.

Startup persistence
--------------------

``persistence_path`` persists the account's caches to disk when the client
becomes ready (and on a clean :meth:`Client.close`), then replays them on
the next startup. A restarted process starts with a warm cache and skips
member list re-scraping entirely — restarts go from seconds to effectively
instant. Snapshots older than ``persistence_max_age`` (14 days by default) or
belonging to a different account are ignored automatically.

The snapshot stores one guild per record, so replay decodes and caches one
guild at a time and gives other tasks a turn every few milliseconds. Memory
during replay tracks the largest single guild rather than the whole account,
and a long replay does not stall other clients running in the same process.

The snapshot contains the raw gateway ``READY`` payload, so treat it as a
secret. It is written atomically with owner-only (``0600``) permissions; keep
it out of version control and shared directories. A snapshot larger than
256 MiB (decompressed) is not written at all, and a warning is logged, since
replay would reject it on every startup anyway. Oversized or corrupt files
that already exist are ignored in favour of a cold start.

The final snapshot is written by a clean :meth:`Client.close`. :meth:`Client.run`
handles ``KeyboardInterrupt`` (Ctrl+C) but not ``SIGTERM``, which is what
``docker stop`` and systemd send, so a process stopped that way exits without
closing the client and skips that last write. If you run under such a
supervisor, close the client from your own signal handler:

.. code-block:: python3

    import asyncio
    import signal

    async def main():
        async with client:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(client.close()))
            await client.start(token)

    asyncio.run(main())
