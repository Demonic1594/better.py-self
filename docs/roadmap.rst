.. currentmodule:: discord

.. _roadmap:

Roadmap
=======

This page lists the work planned for upcoming releases. Plans can change;
:doc:`whats_new` is the record of what actually shipped.

v2.3.3
------

Reserved for fixes found after v2.3.2. No new API is planned for it.

v2.3.4
------

**Rapptz/discord.py port, round 4.** This release takes the last API from
Rapptz/discord.py that is useful to user accounts. The candidates come from
comparing Rapptz ``master`` (``65232c3``) with this fork's public API, leaving out
bot-only areas: application commands, interaction responses, sending
components, intents, sharding, entitlements and SKUs.
dolfies/discord.py-self has no unported commits; its ``master`` is still the
commit recorded in ``.github/LATEST_UPSTREAM_COMMIT.txt``.

Planned:

- ``on_raw_typing`` and ``RawTypingEvent``

  Typing events that :func:`on_typing` drops. Today ``TYPING_START`` is dropped
  when its channel isn't cached, or when no member or user can be resolved:
  a DM without a recipient, a group without that recipient, or a guild member
  who isn't cached and isn't in the payload. ``TYPING_START`` is one of the most
  frequent gateway events, so the raw event will be opt-in, the way
  ``enable_raw_presences`` is for presences, and costs nothing when it is off.

- ``on_raw_thread_update`` and ``RawThreadUpdateEvent``

  Thread updates for threads that are not cached, carrying the raw payload
  and the cached thread when there is one.

- ``ActivityType.unknown``

  The enum value ``-1`` that Rapptz uses for activity types it does not know.
  The port first checks how this fork already handles unknown activity types,
  and only adds the value if nothing covers it.

Every item follows the usual rules:

- It carries ``versionadded`` and an entry in :doc:`whats_new`.
- It has regression tests, plus a live check where an account can trigger it.
- It is recorded in :doc:`upstream_syncing`.
- The memory benchmark and the stress benchmark run whenever a hot path is touched.

Open items
----------

These have no release assigned yet.

- **Stress benchmark soak gate.** The soak phase of ``tools/stress_bench.py``
  fails its RSS-plateau gate by about +10.8 MiB (measured in stress-benchmark
  runs during the v2.3.2 work). It fails the same way on code
  from before the audit rounds, and the count of live objects stays flat, so
  this is allocator behaviour, not a leak. Either the soak runs longer, or the
  gate measures live objects instead of RSS.
- **Audit log typings.** ``old_value`` and ``new_value`` in
  ``discord/types/audit_log.py`` could be ``NotRequired`` in every change
  shape. The change is file-wide and changes typings only.
- **Live check of** :attr:`Message.interaction_metadata`. Unit tests cover the
  parsing, but a live run has not yet found a message that carries it.
- **The** ``request_guilds`` **deprecation warning.** ``request_guilds`` is documented as
  deprecated in favour of ``guild_subscriptions``, but using it doesn't emit a
  ``DeprecationWarning`` yet, as the fork's rules require.

Not planned
-----------

These Rapptz/discord.py features are left out because Discord only offers
them to bot accounts, or because this fork already covers them:

- Bot-only: application commands and hybrid commands, interaction responses,
  ``ui.View``/``ui.Modal`` for sending components, intents, sharding,
  application emojis, entitlements and SKUs, ``Guild.active_threads``,
  ``Guild.self_role``, ``AutoShardedClient``.
- Already in this fork under another name: Rapptz's
  ``CallMessage.is_ended()`` is :attr:`CallMessage.call_ended`, and
  ``MemberFlags.automod_quarantined_username`` is
  :attr:`MemberFlags.automod_quarantined_name` (the same ``1 << 7`` bit, available since 2.1).
- Already covered here in another form: :meth:`Client.fetch_guild_preview`,
  :attr:`DMChannel.recipient`, ``ClientStatus``, and receiving every
  message component type (including the newer layout components).
- Rapptz's cache-based ``User.mutual_guilds`` property: it would clash with
  :attr:`UserProfile.mutual_guilds`. ``UserProfile`` subclasses ``User``, and
  its ``mutual_guilds`` is the list of :class:`MutualGuild` objects that the
  profile endpoint returns. dolfies/discord.py-self keeps that meaning.
- Ported, but bot-only on Discord's side: :meth:`Member.fetch_voice` falls
  back to the cache on user accounts, as described in its documentation.
