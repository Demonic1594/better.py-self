.. currentmodule:: discord

.. _quickstart:

Quickstart
============

This page walks you through a first client, then the same client built with the
commands extension. It assumes the library is installed; if it isn't, see
:ref:`installing`.

.. warning::

    Automating a user account is against Discord's Terms of Service. Use a bot
    account for anything a bot can do.

A minimal client
------------------

.. code-block:: python3

    import os
    import discord

    client = discord.Client()

    @client.event
    async def on_ready():
        print(f'Logged in as {client.user} ({len(client.guilds)} guilds)')

    @client.event
    async def on_message(message):
        # Only answer commands typed from this account
        if message.author != client.user:
            return

        if message.content == '!ping':
            await message.channel.send(f'Pong! {client.latency * 1000:.0f} ms')

    client.run(os.environ['DISCORD_TOKEN'])

Save it as ``example_client.py``. Don't name it ``discord.py``: the file would
shadow the library.

Step by step
~~~~~~~~~~~~~~

1. ``import discord`` imports the library. If this raises
   :exc:`ModuleNotFoundError`, see :ref:`installing`.
2. :class:`Client` is the connection to Discord. Its keyword arguments tune
   caching, presence and networking; the defaults suit most accounts.
3. :meth:`Client.event` registers a coroutine as an event handler, named after
   the event it handles. :func:`on_ready` runs once the account's data has
   loaded; :func:`on_message` runs for every message the account can see.
4. Because :func:`on_message` sees *every* message, the handler returns early
   unless :attr:`Message.author` is :attr:`Client.user`. Without that check,
   anyone could make your account reply.
5. :attr:`Client.latency` is the gateway heartbeat latency, in seconds.
6. :meth:`Client.run` logs in, sets up :doc:`logging <logging>` and runs until
   you press Ctrl+C. Reading the token from an environment variable keeps it out
   of your code; :doc:`authenticating` explains where to find it.

Running it
~~~~~~~~~~~~

.. code-block:: shell

    $ export DISCORD_TOKEN='your token'
    $ python3 example_client.py

On Windows, use ``set DISCORD_TOKEN=your token`` and ``py -3 example_client.py``.
Type ``!ping`` in any channel from the same account to see the reply.

The same client with commands
-------------------------------

The :doc:`commands extension <ext/commands/index>` parses messages into commands
with typed arguments, so you don't have to:

.. code-block:: python3

    import os
    from discord.ext import commands

    bot = commands.Bot(command_prefix='!', self_bot=True)

    @bot.command()
    async def ping(ctx):
        await ctx.send(f'Pong! {bot.latency * 1000:.0f} ms')

    @bot.command()
    async def repeat(ctx, times: int, *, text: str):
        for _ in range(min(times, 5)):
            await ctx.send(text)

    bot.run(os.environ['DISCORD_TOKEN'])

``self_bot=True`` makes the bot respond only to your own messages. The
annotations convert arguments: ``!repeat 3 hello world`` calls
``repeat(ctx, 3, 'hello world')``.

Next steps
------------

- :ref:`Events <discord-api-events>` lists everything a client can react to.
- :doc:`guild_subscriptions` explains which events and members a user account
  receives, and how to get more.
- :doc:`cache_tuning` sets memory limits for accounts in many or large guilds.
- The `wiki <https://github.com/Demonic1594/better.py-self/wiki>`_ has
  in-depth guides with runnable examples, including rate limits and pacing.
