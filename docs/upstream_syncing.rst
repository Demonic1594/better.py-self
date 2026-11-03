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
    ``wait_for`` futures remove their listener entry when cancelled (e.g.
    by an ``asyncio.wait_for`` timeout), and errors raised by a listener's
    ``result`` callback go to its future instead of out of
    ``received_message``. Decompression contexts return bytes and ``received_message`` parses
    them with ``utils._from_json_frame`` (``debug_log_receive`` decodes
    for ``on_socket_raw_receive``); ``poll_event`` uses a lazily-resolved ``asyncio.timeout`` context
    (with a ``wait_for`` fallback for Python < 3.11); ``close()`` is
    idempotent and joins (with a timeout) the keepalive task, which is
    kept referenced; the keepalive handlers and rate limiter use
    ``__slots__``. **Conflict points:** any upstream change to
    ``poll_event``, ``close``, or the keepalive classes.

Connection state (``discord/state.py``)
    The largest concentration of changes:

    - O(1) message cache via ``_messages_index`` (helpers
      ``_add_message_to_cache`` / ``_remove_message_from_cache``); all
      appends must go through them. Bulk deletes use
      ``_remove_messages_from_cache`` (one in-place pass over the deque).
    - ``GuildSubscriptions``: once a tick task is past its delay it clears
      ``_task`` before flushing, so a concurrent ``_tick()`` schedules a new
      tick instead of cancelling the in-flight send; tick tasks are held by
      ``utils._spawn_background``; the ``_checked_add`` overflow path merges
      into ``_pending`` after the flush instead of overwriting it. The
      15 KiB batch-size check is incremental (``_entry_sizes`` /
      ``_encoded_size``): only the changed entry is re-encoded.
    - Presence updates: ``ClientStatus`` has value equality (no-op
      suppression depends on it); ``Presence`` does not keep the raw
      activity payloads (memory) and only skips re-parsing when both the
      old and new activity lists are empty; presences for absent members
      are dropped. ``_handle_presence_update`` looks the member up once and
      skips the old-presence copy and comparison when ``_has_listener``
      reports no ``presence_update`` listener (``_has_listener`` checks the
      dispatch handler cache first).
    - ``store_user`` keys the user cache on the ``User``'s own id int.
    - Bulk parsers (``parse_ready``, ``parse_ready_supplemental``,
      ``parse_guild_create``, ``parse_guild_members_chunk``) are wrapped
      in ``_gc_paused``; keep the decorator if upstream rewrites them.
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
    - ``Client.dispatch`` handler cache (handler lookup uses ``getattr``
      with a default, never a raised ``AttributeError``) and re-entrancy guards;
      ``_schedule_event`` eager-starts handler tasks when the opt-in
      ``eager_dispatch`` option is set (Python 3.12+, loop thread only), dispatch
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
    ``_update`` only copies on change. Only the usually-``None``
    ``premium_since`` / ``timed_out_until`` keep ``_raw_*`` slots; roles
    and ``joined_at`` are compared parsed (their raw copies cost ~300 B
    per cached member);
    ``guild_permissions`` ORs raw role permission ints;
    ``display_name``/``colour`` avoid building sorted role lists.

Message (``discord/message.py``)
    Handler dispatch via precomputed ``_INIT_HANDLERS`` / ``_HANDLERS``
    with a ``MISSING`` sentinel; module-level mention regexes;
    ``_handle_member`` re-warms evicted authors; ``_handle_mentions``
    re-warms mentioned members.

HTTP (``discord/http.py``, ``discord/tracking.py``, ``discord/client.py``)
    aiohttp is imported lazily (``_load_aiohttp``, ``_ensure_asession``) and
    is not used at login: ``Client.headers_context`` runs the build-number
    lookups over curl_cffi through ``tracking._CurlFetchSession``;
    fire-and-forget tasks go through ``utils._spawn_background``;
    ``json_or_text`` duck-types curl/aiohttp responses; route keys are
    precomputed; the static header template is cached
    (``_get_base_headers``); the timezone header is resolved once; retry
    sleeps have jitter. ``Ratelimit._refresh`` returns early if the bucket's
    window moved on while it queued for ``_sleeping`` (no stale re-sleep or
    second reset). The idle-bucket sweep in
    ``_try_clear_expired_ratelimits`` runs at most every
    ``BUCKET_SWEEP_INTERVAL`` seconds instead of on every new bucket. Telemetry from the live rate studies: per-route
    latency EWMA (``_route_latencies``, exposed as
    ``Client.http_route_latencies``), wire-level throttle records
    (``_throttle_events`` / ``Client.http_throttle_events``) and the
    ``on_http_throttle`` push event, both fed from the 429 handler.
    **Conflict points:** the entire 429 handling block — a shared-scope
    429 engages the global lock (released in a ``finally`` so a cancelled
    request cannot deadlock the client), ``retry_after`` is sanitized
    (non-finite raises ``RateLimited``; strings coerce; negatives clamp),
    and the telemetry + dispatch sit right after the warning log line.

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

Webhooks (``discord/webhook/async_.py``, ``discord/webhook/sync.py``)
    ``AsyncDeferredLock`` / ``DeferredLock`` release in ``finally`` (a
    cancelled deferral sleep used to leave the webhook lock held); the
    final attempt gives up without sleeping; backoff uses
    ``http._retry_delay``; resets use ``utils._CONNECTION_RESET_ERRNOS``
    (also used by ``HTTPClient``). ``HTTPClient`` maps failed responses
    through ``_http_error``. ``utils._parse_ratelimit_header`` returns 0
    instead of raising when both reset headers are missing.

Voice (``discord/voice_client.py``, ``discord/voice_media.py``, ``discord/opus.py``)
    ``play()`` stops a paused player before replacing it;
    ``VoiceConnectionState._poll_voice_ws`` wraps ``_poll_voice_ws_loop`` so
    an unexpected error is logged and disconnects cleanly; ``_inner_connect``
    disconnects and re-raises after its fifth failed handshake instead of
    reporting success; the RTP audio
    level is computed with ``array``/``math.sumprod``; ``Encoder.encode`` /
    ``Decoder.decode`` return the ctypes buffers' bytes directly.

Interactions, nonces and chunk requests
    ``utils._generate_nonce`` fills the snowflake's low 22 bits from a
    process-wide counter (same-millisecond nonces collided).
    ``parse_interaction_create`` / the autocomplete response handler pop
    their nonce context; ``_pop_interaction`` also clears
    ``_interaction_cache``. ``query_members`` / ``search_recent_members``
    unregister their ``ChunkRequest`` in ``finally``. Slash command option
    parsing logs unknown option names and maps "false"/"0"/"no" strings to
    ``False`` for booleans. ``Interaction.message`` caches hits only.

Guild / channel / files / settings
    ``Guild.audit_logs`` subtracts ``len(entries)`` from the limit (ported
    from Rapptz/discord.py). ``GuildChannel.move(category=None)`` takes its
    siblings from the uncategorised channels. ``_purge_helper`` skips the
    final delete when nothing is left. ``handle_message_parameters``
    numbers local ``File`` ids by multipart index and cloud files after them;
    ``CloudFile.from_file`` keeps ``spoiler``; ``File.size`` is measured from
    the original position. ``UserSettings.edit`` encodes audio contexts with
    ``AudioContext._to_settings_dict`` and treats ``MISSING`` as a reset for
    the ``*_guilds`` privacy fields and ``soundboard_volume``.
    Persistence: a replayed READY is not re-captured, oversized snapshots are
    not written, and writes are generation-ordered under
    ``_persistence_write_lock``.

Lifecycle, audio and messages
    ``Client.start`` warns and returns on a closed client; ``Client.login``
    rejects an empty token; ``Client.__aexit__`` shields the close task;
    ``Client.change_presence`` raises ``ClientException`` before the gateway
    exists. ``HTTPClient.startup`` is serialised by ``_startup_lock`` (the
    work is in ``_startup``), ``HTTPClient.clear`` resets ``_started``, and
    ``static_login`` restores the previous token on any failure.
    ``utils.setup_logging`` replaces the handler a previous call installed
    (marked ``_discord_setup_logging``); ``utils.parse_time`` accepts a
    trailing ``Z`` on Python 3.10. In ``ext.commands.Bot.reload_extension`` a
    failed rollback is logged and the old module's references are removed.
    ``Message.__init__`` always sets ``guild_id``; ``parse_message_update``
    applies a partial update (no ``type``/``content``) to the cached message.
    ``Thread._from_data`` reads the counts with ``.get``. In ``player.py``
    ``_pipe_writer`` closes stdin when its source raises, ``_pipe_reader``
    stops at EOF, ``_kill_process`` waits at most 5 seconds, and
    ``_probe_codec_fallback`` uses ``subprocess.run`` with a timeout.

Persistence format
    Snapshots are version 2: newline-delimited JSON (a header record, then one
    record per guild) in a single zlib stream, written incrementally by
    ``_write_persistence``. ``_SnapshotReader`` streams records with the
    256 MiB decompression cap; version 1 single-document snapshots are still
    read. ``_replay_snapshot`` releases each guild payload once replayed and
    yields every ``PERSISTENCE_REPLAY_SLICE`` seconds. Upstream has no
    persistence, so this does not conflict.

Extensions
    ``StringView.skip_ws`` / ``get_word`` are bounded index loops (same
    ``str.isspace`` semantics); ``_Semaphore`` counts woken-but-not-resumed
    waiters as active so ``MaxConcurrency(wait=True)`` cannot drop the
    mapping entry and exceed its limit.
    Cooldown mappings use lazy per-key expiry with a size-triggered sweep;
    converters memoize the module remapping; ``Command.signature`` caches
    (invalidated in ``update()``); task loops cache timezone conversions
    and tolerate unhashable tzinfo.

Upstream issue and PR triage (2026-11-03)
    Upstream ``master`` was unchanged since the recorded sync. Open items
    reviewed: #939 (info API params never sent, timeout, hardcoded
    platform) ported into ``tracking.py`` with an unknown-browser guard;
    #907 (member list scraping without a SYNC) now logs a warning in
    ``MemberSidebar.wrapper`` (the scrape already returned, it did not hang
    here); #946 is documented behaviour (``sync_presence`` vs
    ``change_presence(edit_settings=...)``), clarified in the docs. Not taken:
    #937 (the reported capability value would disable
    ``user_settings_proto``/``client_state_v2``/``dedupe_user_objects`` our
    parsers rely on, and the report is internally inconsistent), #945 (voice
    disconnect after hours: no close codes or root cause; needs a live voice
    server), and the unmerged feature PRs #896 (frecency settings) and #934
    (profile editing), which are still in review upstream.

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
