.. currentmodule:: discord

.. _upstream_syncing:

Syncing With Upstream
=======================

This repository is a fork of `dolfies/discord.py-self
<https://github.com/dolfies/discord.py-self>`_ that carries a set of
performance, memory and feature changes. Upstream moves quickly, so
periodically merging it in is normal maintenance. This page documents the
procedure and, more importantly, the places our changes live so merge
conflicts can be resolved deliberately instead of by guesswork.

Procedure
----------

.. code-block:: bash

    git fetch upstream
    git merge upstream/master
    # resolve conflicts (see the map below), then:
    python -m compileall -q discord/
    python -m pytest tests/ -q
    python -m pyright          # must stay at 0 errors
    ruff format --check
    python -m sphinx -b html -W docs /tmp/docsbuild

The 250+ tests include regression suites for every optimization described
below, plus allocation-count performance smoke tests
(``tests/test_perf_smoke.py``). If a merge makes them fail, the map below
tells you which optimization was affected.

Where our changes live
-----------------------

Gateway and websocket (``discord/gateway.py``)
    ``poll_event`` uses a lazily-resolved ``asyncio.timeout`` context
    (with a ``wait_for`` fallback for Python < 3.11); ``close()`` is
    idempotent and joins (with a timeout) the keepalive task, which is
    kept referenced; the keepalive handlers and rate limiter use
    ``__slots__``. **Conflict points:** any upstream change to
    ``poll_event``, ``close``, or the keepalive classes.

Connection state (``discord/state.py``)
    The largest concentration of changes:

    - O(1) message cache via ``_messages_index`` (helpers
      ``_add_message_to_cache`` / ``_remove_message_from_cache``); all
      appends must go through them.
    - Presence updates: ``ClientStatus`` has value equality (no-op
      suppression depends on it); ``Presence`` compares raw activity
      payloads before reparsing; presences for absent members are dropped.
    - ``parse_message_update`` only constructs a ``Message`` when
      ``_has_listener`` (which also checks ``Bot.extra_events``) says
      someone is listening.
    - Cache caps: ``max_member_cache`` / ``max_total_member_cache`` /
      ``max_thread_cache`` / ``max_total_thread_cache`` /
      ``max_guild_presence_cache`` / ``max_presence_cache`` options,
      ``_note_member_add`` / ``_maybe_evict_members_global`` (and the
      thread equivalents) amortized rebalancing.
    - Persistence: capture in ``parse_ready`` / ``parse_ready_supplemental``,
      soft-clear + merge in ``parse_ready`` / the supplemental guild loop,
      ``_write_persistence`` / ``_replay_persistence``, and the
      ``Client.login`` / ``Client.close`` hooks.
    - ``Client.dispatch`` handler cache and re-entrancy guards, dispatch
      backlog tracking (``_track_dispatch``), ``_schedule_gc_freeze``,
      and ``CacheStats`` / ``_build_cache_stats``.

    **Conflict points:** upstream rewrites of the READY parsers or the
    presence path will conflict; resolve by re-applying our hooks to their
    new structure rather than taking either side wholesale.

Guild (``discord/guild.py``)
    Member and thread caches with LRU ordering, eviction, protection
    (self, voice, joined threads), ``_members_seen`` chunked accounting,
    presence cleanup on eviction, and the per-guild eviction counters used
    by ``cache_stats``. ``chunked`` is ``count <= _members_seen and ...``.

Member / user (``discord/member.py``, ``discord/user.py``)
    ``_update`` compares raw payload fields before parsing and only copies
    on change; raw fields are cached in ``_raw_*`` slots;
    ``guild_permissions`` ORs raw role permission ints;
    ``display_name``/``colour`` avoid building sorted role lists.

Message (``discord/message.py``)
    Handler dispatch via precomputed ``_INIT_HANDLERS`` / ``_HANDLERS``
    with a ``MISSING`` sentinel; module-level mention regexes;
    ``_handle_member`` re-warms evicted authors; ``_handle_mentions``
    re-warms mentioned members.

HTTP (``discord/http.py``)
    aiohttp is imported lazily (``_load_aiohttp``, ``_ensure_asession``);
    ``json_or_text`` duck-types curl/aiohttp responses; route keys are
    precomputed; the static header template is cached
    (``_get_base_headers``); the timezone header is resolved once; retry
    sleeps have jitter.

Flags and permissions (``discord/flags.py``, ``discord/permissions.py``)
    Precomputed ``_ITER_FLAGS`` / ``_INVERT_MASK`` (note:
    ``flag_value.__get__`` and ``__iter__`` must keep delegating to
    ``_has_flag`` — SystemChannelFlags overrides it with inverted
    semantics); precomputed ``_TIMEOUT_PERMISSIONS_MASK`` /
    ``_DM_PERMISSIONS_VALUE`` and the voice masks in ``channel.py``.

Threads / embeds / components (``discord/threads.py``, ...)
    Fast-path no-op update detection with ``_raw_metadata`` (excluded
    from dispatch attrs); slot-copied ``Embed.copy``; dict-dispatched
    ``_COMPONENT_CLASSES`` component factory; ``__slots__`` on small
    high-cardinality classes.

Extensions
    Cooldown mappings use lazy per-key expiry with a size-triggered sweep;
    converters memoize the module remapping; ``Command.signature`` caches
    (invalidated in ``update()``); task loops cache timezone conversions
    and tolerate unhashable tzinfo.

Post-merge checklist
---------------------

1. All tests pass (``python -m pytest tests/ -q``), including
   ``test_optimizations_*``, ``test_runtime_features``,
   ``test_persistence`` and ``test_perf_smoke``.
2. ``pyright`` reports 0 errors and ``ruff format --check`` is clean.
3. The docs build with warnings-as-errors.
4. Sanity-run the memory simulation profiles if the guild/member parsers
   changed shape (see the cache tuning guide for the expected numbers).
5. Update this page if an optimization moved or was replaced by an
   upstream equivalent.
