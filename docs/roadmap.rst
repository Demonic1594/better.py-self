.. currentmodule:: discord

.. _roadmap:

Roadmap
=======

This page lists the work planned for upcoming releases. Plans can change;
:doc:`whats_new` is the record of what actually shipped.

v2.3.4 (released)
-----------------

The four bugs the wiki rewrite documented (the retry loop's final sleep,
``File(bytes)``, ``ForumChannel.create_tag`` and ``Role.edit(position=...)``),
the in-depth wiki, and corrected docs. See :doc:`whats_new`.

v2.3.3 (released)
-----------------

The November 2026 audit and its follow-ups, the Rapptz/discord.py round-4 port
(:func:`on_raw_typing`, :func:`on_raw_thread_update`,
:attr:`ActivityType.unknown`), the stress benchmark's soak gate on live objects,
and the audit log typings. See :doc:`whats_new`.

Next
----

No release is planned yet. Candidates are the open items below, and anything
new in dolfies/discord.py-self or Rapptz/discord.py that applies to user
accounts.

Open items
----------

These have no release assigned yet.

- **Live check of** :attr:`Message.interaction_metadata`. Unit tests cover the
  parsing, but a live run has not yet found a message that carries it.
- **Questions the v2.3.3 warnings will answer.** Two payload shapes can only be
  confirmed on a live account: whether a member update ever leaves out a user
  field that is still set, and the color format of custom themes. Both now keep
  the data and log a warning (once) when they meet the unexpected shape, so a
  report of that warning settles each question.

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
