import discord


class MyClient(discord.Client):
    async def on_ready(self):
        # After a restart with a valid snapshot this prints the full member
        # count almost immediately; a cold start would still be scraping.
        guild = self.get_guild(123456789012345678)  # guild ID goes here
        if guild is not None:
            print(f'{guild.name}: {len(guild.members)} members cached')

    async def on_message(self, message):
        # Caches may be briefly stale right after a restart (for example a
        # member who left while offline); live events reconcile within
        # seconds as Discord sends updates.
        print(f'{message.author}: {message.content}')


# Persist this account's caches to disk. On the next startup the client
# replays them, giving you a warm cache immediately: guilds, members,
# threads and DMs are all available right after login without waiting for
# member list scraping.
#
# Snapshots are written automatically when the client becomes ready and
# again on a clean shutdown. Snapshots older than 14 days or belonging to
# a different account are ignored automatically.
client = MyClient(persistence_path='my_account_cache.bin')
client.run('token')
