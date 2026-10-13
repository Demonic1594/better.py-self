import discord
from discord.ext import tasks


class MyClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Cache caps tuned for an account in many large guilds.
        # See the cache tuning guide for other workload profiles.
        # (These can also be passed as regular kwargs instead.)

    async def setup_hook(self) -> None:
        # Start the monitoring loop
        self.cache_monitor.start()

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

    @tasks.loop(seconds=60)
    async def cache_monitor(self):
        # A live snapshot of every cache: sizes, caps, eviction counters,
        # get_member hit/miss ratio, per-guild breakdown, dispatch backlog
        # and event loop lag.
        stats = await self.cache_stats()

        hit_rate = stats.member_hits / max(1, stats.member_hits + stats.member_misses)
        print(
            f'guilds={stats.guilds} members={stats.members} '
            f'(evicted={stats.evicted_members}, hit rate={hit_rate:.1%}) '
            f'threads={stats.threads} presences={stats.guild_presences} '
            f'backlog={stats.dispatch_backlog} loop_lag={stats.event_loop_lag * 1000:.1f}ms'
        )

        # A low hit rate means the member cache caps are too tight for this
        # account and members keep going cold between uses.
        if stats.member_hits + stats.member_misses > 1000 and hit_rate < 0.8:
            print('  -> member cache hit rate is low; consider raising max_member_cache')

        # A large backlog means event handlers are slower than the events
        # arriving. on_dispatch_backlog also fires automatically on its own.
        if stats.dispatch_backlog > 500:
            oldest = stats.dispatch_oldest_age
            print(f'  -> handlers are behind by {stats.dispatch_backlog} events (oldest {oldest:.1f}s old)')

    @cache_monitor.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()

    async def on_dispatch_backlog(self, pending, oldest_age):
        # Fired automatically when pending handlers exceed
        # dispatch_backlog_warning (default 1000, at most once per 30s)
        print(f'WARNING: event handler backlog ({pending} pending, oldest {oldest_age:.1f}s)')


client = MyClient()
client.run('token')
