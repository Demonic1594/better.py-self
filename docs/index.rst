Welcome to discord.py-self
===========================

.. image:: /images/snake.svg
.. image:: /images/snake_dark.svg

better.py-self is a drop-in fork of discord.py-self, a modern, easy to use,
feature-rich and async-ready API wrapper for the Discord user APIs. It keeps
the same public API and adds bounded, tunable caches, faster hot paths,
credential-safe logging and features ported from Rapptz/discord.py.

**Features:**

- Modern Pythonic API using ``async``\/``await`` syntax
- Rate limit handling that accounts for user-account behaviour, with every
  absorbed 429 and retry visible (:meth:`discord.Client.http_throttle_events`,
  :meth:`discord.Client.http_retry_events`)
- Every cache is capped, with LRU eviction, and observable through
  :meth:`discord.Client.cache_stats` (see :doc:`cache_tuning`)
- Opt-in cache persistence for fast restarts (``persistence_path``)
- Soundboard, voice channel effects, interaction metadata and raw presence
  events (new in v2.3.2; see :doc:`whats_new`)
- DEBUG logs that never contain credentials (see :doc:`logging`)
- A command extension for self-bots, and an extension for background tasks
- Prevents detection of user account automation

Getting started
-----------------

Is this your first time using the library? This is the place to get started!

- **Migrating from discord.py:** :doc:`migrating_from_dpy`
- **First steps:** :doc:`intro` | :doc:`quickstart` | :doc:`logging`
- **Working with Discord:** :doc:`authenticating` | :doc:`guild_subscriptions`
- **Memory and caches:** :doc:`cache_tuning`
- **What's planned:** :doc:`roadmap`
- **In-depth guides:** the :resource:`wiki <wiki>`
- **Examples:** Many examples are available in the :resource:`repository <examples>`

| **Obligatory note:**
| Automating user accounts is against the Discord ToS. If what you are trying to do is accomplishable with a bot account, please use one.

Getting help
--------------

If you're having trouble with something, these resources might help.

- Try the :doc:`faq` first, it's got answers to all common questions.
- If you're looking for something specific, try the :ref:`index <genindex>` or :ref:`searching <search>`.
- Report bugs in the :resource:`issue tracker <issues>`.
- Ask in our :resource:`GitHub discussions page <discussions>`.

Extensions
------------

These extensions help you during development when it comes to common tasks.

.. toctree::
  :maxdepth: 1

  ext/commands/index.rst
  ext/tasks/index.rst

External extensions:

- **Native voice:** `discord.ext.native_voice <https://discord-ext-native-voice.readthedocs.io/en/latest/>`_

Manuals
----------

These pages go into great detail about everything the API can do.

.. toctree::
  :maxdepth: 1

  api
  discord.ext.commands API Reference <ext/commands/api.rst>
  discord.ext.tasks API Reference <ext/tasks/index.rst>
  cache_tuning

Meta
------

If you're looking for something related to the project itself, it's here.

.. toctree::
  :maxdepth: 1

  whats_new
  roadmap
  version_guarantees
  migrating_from_dpy
  migrating
  upstream_syncing
