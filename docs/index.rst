
.. currentmodule:: discord

better.py-self
================

.. raw:: html

    <div class="bp-hero">
      <img class="bp-hero-logo" src="_static/logo.svg" alt="" />
      <div class="bp-hero-body">
        <p class="bp-hero-tagline">
          A Python API wrapper for Discord user accounts. A drop-in fork of
          discord.py-self: the same public API, with bounded caches, visible
          rate limits, warm restarts and credential-safe logging.
        </p>
        <ul class="bp-pills">
          <li>Python 3.10+</li>
          <li>async / await</li>
          <li>Drop-in for discord.py-self</li>
          <li>MIT licensed</li>
        </ul>
        <div class="bp-install"><span>python3 -m pip install -U <wbr><span class="bp-nowrap">git+https://github.com/</span><wbr><span class="bp-nowrap">Demonic1594/</span><wbr><span class="bp-nowrap">better.py-self.git</span></span></div>
        <div class="bp-actions">
          <a class="bp-button bp-primary" href="quickstart.html">Quickstart</a>
          <a class="bp-button" href="api.html">API reference</a>
          <a class="bp-button" href="https://github.com/Demonic1594/better.py-self/wiki">Wiki</a>
        </div>
      </div>
    </div>

.. warning::

    Automating a user account is against Discord's Terms of Service and can get
    the account disabled. If a bot account can do what you need, use one.

Why better.py-self
--------------------

Everything discord.py-self offers, plus:

.. raw:: html

    <div class="bp-grid">
      <a class="bp-card" href="cache_tuning.html">
        <span class="bp-card-title">Bounded caches</span>
        <span class="bp-card-text">Members, presences, threads and messages are capped with LRU eviction, so memory has a ceiling you choose.</span>
        <span class="bp-card-link">Cache tuning guide &rarr;</span>
      </a>
      <a class="bp-card" href="api.html#discord.Client.http_throttle_events">
        <span class="bp-card-title">Visible rate limits</span>
        <span class="bp-card-text">Every 429 the library waits out fires <code>on_http_throttle</code>, and throttles and retries are summarised per route.</span>
        <span class="bp-card-link">Throttle events &rarr;</span>
      </a>
      <a class="bp-card" href="api.html#discord.Client">
        <span class="bp-card-title">Warm restarts</span>
        <span class="bp-card-text">Set <code>persistence_path</code> to snapshot the caches after login and restore them on the next start.</span>
        <span class="bp-card-link">Client options &rarr;</span>
      </a>
      <a class="bp-card" href="logging.html">
        <span class="bp-card-title">Credential-safe logs</span>
        <span class="bp-card-text">DEBUG logs redact tokens, passwords and other known credential fields before anything is written.</span>
        <span class="bp-card-link">Setting up logging &rarr;</span>
      </a>
      <a class="bp-card" href="api.html#discord.Client.cache_stats">
        <span class="bp-card-title">Observable internals</span>
        <span class="bp-card-text">Cache sizes, hit and miss counts, evictions, the handler backlog and event-loop lag, in one call.</span>
        <span class="bp-card-link">cache_stats() &rarr;</span>
      </a>
      <a class="bp-card" href="whats_new.html">
        <span class="bp-card-title">Ported from discord.py</span>
        <span class="bp-card-text">Soundboard, voice channel effects, interaction metadata, raw presence events and gateway fixes.</span>
        <span class="bp-card-link">Changelog &rarr;</span>
      </a>
    </div>

A first client
----------------

.. code-block:: python3

    import os
    import discord

    class MyClient(discord.Client):
        async def on_ready(self):
            print(f'Logged in as {self.user}')

        async def on_message(self, message):
            # Only answer commands you type yourself
            if message.author == self.user and message.content == '!ping':
                await message.channel.send(f'Pong! {self.latency * 1000:.0f} ms')

    client = MyClient()
    client.run(os.environ['DISCORD_TOKEN'])

The :doc:`quickstart` walks through this example line by line, and
:doc:`authenticating` explains where the token comes from.

Where to go next
------------------

.. raw:: html

    <div class="bp-grid">
      <a class="bp-card" href="intro.html">
        <span class="bp-card-title">Guides</span>
        <span class="bp-card-text">Installation, a first client, tokens, logging, guild subscriptions and cache tuning.</span>
        <span class="bp-card-link">Start with the introduction &rarr;</span>
      </a>
      <a class="bp-card" href="api.html">
        <span class="bp-card-title">API reference</span>
        <span class="bp-card-text">Every class, method, attribute and event, generated from the code.</span>
        <span class="bp-card-link">Browse the reference &rarr;</span>
      </a>
      <a class="bp-card" href="https://github.com/Demonic1594/better.py-self/wiki">
        <span class="bp-card-title">Wiki</span>
        <span class="bp-card-text">In-depth explanations with runnable examples: events, member lists, pacing, persistence, troubleshooting.</span>
        <span class="bp-card-link">Open the wiki &rarr;</span>
      </a>
      <a class="bp-card" href="https://github.com/Demonic1594/better.py-self/tree/master/examples">
        <span class="bp-card-title">Examples</span>
        <span class="bp-card-text">Complete scripts: commands, background tasks, presence, voice, persistence and pacing.</span>
        <span class="bp-card-link">See the examples &rarr;</span>
      </a>
    </div>

Getting help
--------------

- Try the :doc:`faq` and the wiki's
  `troubleshooting page <https://github.com/Demonic1594/better.py-self/wiki/troubleshooting>`_.
- Looking for something specific? Use the :ref:`index <genindex>` or :ref:`search <search>`.
- Report bugs in the :resource:`issue tracker <issues>`.
- For general questions about the library, the upstream
  :resource:`discord.py-self discussions <discussions>` are the place to ask.

Contents
----------

.. toctree::
  :caption: Guides
  :maxdepth: 1

  intro
  quickstart
  authenticating
  logging
  guild_subscriptions
  cache_tuning
  faq

.. toctree::
  :caption: Extensions
  :maxdepth: 1

  ext/commands/index.rst
  ext/tasks/index.rst

External extension: `discord.ext.native_voice <https://discord-ext-native-voice.readthedocs.io/en/latest/>`_
(native voice support).

.. toctree::
  :caption: API reference
  :maxdepth: 1

  api
  discord.ext.commands API Reference <ext/commands/api.rst>
  discord.ext.tasks API Reference <ext/tasks/index.rst>

.. toctree::
  :caption: Project
  :maxdepth: 1

  whats_new
  roadmap
  version_guarantees
  migrating_from_dpy
  migrating
  upstream_syncing
