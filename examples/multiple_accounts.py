"""Runs several accounts in one process.

Every process pays a fixed ~55-60 MB for Python and the library's imports
before any account logs in. Running each account in its own process pays
that cost once per account; running them in one process pays it once in
total, so each extra account only costs its own caches (~15-20 MB for a
typical account, measured with tests/memory_bench.py scenarios).

Tokens are read from the DISCORD_TOKENS environment variable,
comma-separated. Never hard-code tokens in source files.
"""

import asyncio
import logging
import os

import discord


class AccountClient(discord.Client):
    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')

    async def on_message(self, message: discord.Message):
        if message.author == self.user and message.content == '!ping':
            await message.channel.send(f'pong from {self.user}')


async def run_account(token: str) -> None:
    # Each Client has its own HTTP session, gateway connection and caches;
    # `async with` closes them cleanly when the account stops
    async with AccountClient() as client:
        await client.start(token)


async def main() -> None:
    tokens = [t.strip() for t in os.environ.get('DISCORD_TOKENS', '').split(',') if t.strip()]
    if not tokens:
        raise SystemExit('Set DISCORD_TOKENS to a comma-separated list of tokens.')

    # return_exceptions=True keeps the other accounts running if one fails
    # (for example, an invalid token raises LoginFailure)
    results = await asyncio.gather(*(run_account(token) for token in tokens), return_exceptions=True)
    for index, result in enumerate(results):
        if isinstance(result, BaseException):
            print(f'Account #{index + 1} stopped: {result!r}')


if __name__ == '__main__':
    # Client.run() would normally set up logging; with start() we do it here
    discord.utils.setup_logging(level=logging.INFO)
    asyncio.run(main())
