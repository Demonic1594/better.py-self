better.py-self
================

A heavily optimized, memory-bounded, and hardened fork of `discord.py-self
<https://github.com/dolfies/discord.py-self>`_ — a modern, easy-to-use,
feature-rich, and async-ready API wrapper for Discord's user API written in
Python.

| **Note:**
| Automating user accounts is against the Discord ToS. This library is a proof of concept and I cannot recommend using it. Do so at your own risk.
|

| **Credits:**

- `Demonic1594 <https://github.com/Demonic1594>`_ for this fork's optimization, hardening, and tooling work.
- `dolfies <https://github.com/dolfies>`_ for discord.py-self, which this fork carries forward.
- `Rapptz <https://github.com/Rapptz>`_ for the original library the above is based on. Without it, none of this would exist.

What This Fork Adds
--------------------

Everything below is measured, documented, and locked in by CI on Python
3.10–3.14 (see `docs/cache_tuning.rst
<https://github.com/Demonic1594/better.py-self/blob/master/docs/cache_tuning.rst>`_
for the full tuning guide):

**Performance**

- O(1) message cache lookups (was O(n) scans), no-op suppression for
  member/thread/presence updates (the hottest gateway path runs 4.4x faster
  with zero phantom dispatches), lazy ``MESSAGE_UPDATE`` parsing when nobody
  is listening, cached HTTP route keys/header templates, precompiled
  regular expressions, ``gc.freeze`` after READY
- Importing the library no longer loads ``aiohttp`` — it loads on demand

**Memory**

- Every cache is capped and configurable with LRU eviction:
  ``max_member_cache``, ``max_total_member_cache``, ``max_thread_cache``,
  ``max_total_thread_cache``, ``max_guild_presence_cache``,
  ``max_presence_cache``. You, voice members, joined threads, and active
  members are never evicted; evicted data is re-fetchable and re-warmed
  automatically from live traffic
- A 150k-member account drops from ~100 MB of growing caches to ~38 MB with
  a hard ceiling

**Observability**

- ``Client.cache_stats()`` — live snapshot of every cache's size, caps,
  eviction counters, ``get_member`` hit/miss ratios, per-guild breakdown,
  dispatch backlog, and event loop lag
- ``on_dispatch_backlog`` — slow event handlers become visible instead of
  silently accumulating queued events
- ``Client.http_route_latencies()`` — per-route latency EWMA, the correct
  congestion signal for user accounts (Discord queues sends server-side
  instead of returning 429s; rate headers are never served)
- ``Client.http_throttle_events()`` + ``on_http_throttle`` — every
  wire-level 429 the library absorbed internally (sleep + retry) is
  recorded per route and pushed as an event, so real throttle pressure is
  visible without instrumenting the HTTP session
- ``examples/lane_governor.py`` — a drop-in pacing governor implementing
  the validated three-lane recipe (serialized account-global
  send/react/click lanes, x2 backoff, bounded probe-down, shared-scope
  pauses, EWMA congestion watch)

**Fast restarts**

- Opt-in cache persistence (``persistence_path``): caches are snapshotted
  to disk on ready/close (owner-only permissions, atomic writes,
  size-capped decompression) and replayed on startup — restarts go from
  seconds of member re-scraping to a ~75 ms warm replay (the replay covers
  the READY-time member set; anything that changed while offline is
  reconciled by the next member-list sync and live traffic)

**Rate-limit correctness for user accounts**

- Shared-scope 429s (``x-ratelimit-scope: shared`` — the account-wide
  reaction bucket) now gate all routes via the global lock, not just the
  route that tripped

**Robustness**

- Runtime-audited against hostile input: 2,000+ mutated gateway payloads,
  ReDoS-class regex floods, zlib bombs, hostile persistence snapshots,
  resource-exhaustion storms — no hangs, no base-exception leaks, bounded
  memory (11 abuse-driven defect fixes, each regression-gated)

Key Features
-------------

- Modern Pythonic API using ``async`` and ``await``.
- Proper rate limit handling (including user-account quirks; see above).
- Optimised in both speed and memory — measured and CI-gated.
- Mostly compatible with the upstream ``discord.py`` (see the
  `upstream syncing guide
  <https://github.com/Demonic1594/better.py-self/blob/master/docs/upstream_syncing.rst>`_
  for how merges are handled).
- Prevents user account automation detection.
- Implements vast amounts of the user account-specific API. For a non-exhaustive list:

  * Sessions
  * Read states
  * Connections
  * Relationships
  * Experiments
  * Protobuf user settings
  * Application/team management
  * Store/SKUs/entitlements
  * Billing (e.g. subscriptions, payments, boosts, promotions, etc.)
  * Interactions (slash commands, buttons, etc.)

Installing
----------

**Python 3.10 or higher is required.**

This fork is private and is not published to PyPI. Install it from the
repository, or from the `v2.2.0 release
<https://github.com/Demonic1594/better.py-self/releases/tag/v2.2.0>`_
wheel:

.. code:: sh

    # From the repository
    python3 -m pip install -U git+https://github.com/Demonic1594/better.py-self.git

    # From the release wheel
    python3 -m pip install -U https://github.com/Demonic1594/better.py-self/releases/download/v2.2.0/discord_py_self-2.2.0-py3-none-any.whl

For voice support, add the ``[voice]`` extra:

.. code:: sh

    python3 -m pip install -U "discord.py-self[voice] @ git+https://github.com/Demonic1594/better.py-self.git"

Optional Packages
~~~~~~~~~~~~~~~~~~

* `PyNaCl <https://pypi.org/project/PyNaCl/>`__ and ``davey`` (for voice support, via the ``[voice]`` extra)
* The ``[speed]`` extra — `orjson <https://pypi.org/project/orjson/>`__, `mmh3 <https://pypi.org/project/mmh3/>`__, `zstandard <https://pypi.org/project/zstandard/>`__, `Brotli <https://pypi.org/project/Brotli/>`__ and `aiodns <https://pypi.org/project/aiodns/>`__ — for the fast JSON/hash/compression/DNS paths exercised by the CI speed legs

Please note that on Linux installing voice you must install the following packages via your favourite package manager (e.g. ``apt``, ``dnf``, etc) before running the above commands:

* libffi-dev (or ``libffi-devel`` on some systems)
* python-dev (e.g. ``python3.12-dev`` for Python 3.12)

Differences From Upstream
--------------------------

- The package still installs as ``discord`` (import ``discord`` as usual);
  the distribution name on the v2.2.0 wheel is ``discord_py_self``.
- Upstream's ``selfcord.py`` renamed build (the ``renamed`` branch) is not
  carried by this fork; it was removed together with the destructive
  rename/publish workflows.
- The cache caps have sensible defaults (see the tuning guide) — pass
  ``None`` to any of them for unbounded, upstream-style behavior.

Quick Example
--------------

.. code:: py

    import discord

    class MyClient(discord.Client):
        async def on_ready(self):
            stats = await self.cache_stats()
            print(f'Logged on as {self.user} — {stats.members} members cached')

        async def on_message(self, message):
            # only respond to ourselves
            if message.author != self.user:
                return

            if message.content == 'ping':
                await message.channel.send('pong')

    client = MyClient(
        # Tuned for an account in many large guilds; see the cache tuning guide
        max_total_member_cache=50000,
        persistence_path='my_account_cache.bin',  # instant warm restarts
    )
    client.run('token')

Bot Example
~~~~~~~~~~~~~

.. code:: py

    import discord
    from discord.ext import commands

    bot = commands.Bot(command_prefix='>', self_bot=True)

    @bot.command()
    async def ping(ctx):
        await ctx.send('pong')

    bot.run('token')

Monitoring Example
~~~~~~~~~~~~~~~~~~~

.. code:: py

    import discord
    from discord.ext import tasks

    class MyClient(discord.Client):
        @tasks.loop(seconds=60)
        async def monitor(self):
            stats = await self.cache_stats()
            hit_rate = stats.member_hits / max(1, stats.member_hits + stats.member_misses)
            latencies = self.http_route_latencies()
            print(f'{stats.members} members ({hit_rate:.0%} hit rate), '
                  f'backlog={stats.dispatch_backlog}, send latency={latencies.get("POST /channels/{channel_id}/messages", 0):.2f}s')

        @monitor.before_loop
        async def before(self):
            await self.wait_until_ready()

        async def setup_hook(self):
            self.monitor.start()

    client = MyClient()
    client.run('token')

You can find more examples in the examples directory, including
``cache_stats_monitor.py`` and ``persistent_client.py``.

Contributing
-------------

This is a private fork. If you have access, contributions are welcome —
read the `contribution guide
<https://github.com/Demonic1594/better.py-self/blob/master/.github/CONTRIBUTING.md>`_
first. For merging upstream changes, follow the
`upstream syncing guide
<https://github.com/Demonic1594/better.py-self/blob/master/docs/upstream_syncing.rst>`_.

Links
------

- `Fork releases <https://github.com/Demonic1594/better.py-self/releases>`_
- `Cache tuning guide <https://github.com/Demonic1594/better.py-self/blob/master/docs/cache_tuning.rst>`_
- `Upstream (dolfies/discord.py-self) <https://github.com/dolfies/discord.py-self>`_
- `Upstream documentation <https://discordpy-self.readthedocs.io/en/latest/index.html>`_
