"""
Runtime-only features: lazy aiohttp imports, gc freeze plumbing,
cache_stats snapshot, dispatch backpressure.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import discord
from discord import Client, Guild
from discord.channel import TextChannel


def up(uid):
    return {'id': str(uid), 'username': f'u{uid}', 'discriminator': '0001', 'global_name': None, 'avatar': None}


def run_py(code: str) -> str:
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_import_discord_does_not_load_aiohttp():
    out = run_py("import sys; import discord, discord.webhook;print('aiohttp' in sys.modules)")
    assert out.strip() == 'False'


def test_load_aiohttp_applies_hdrs_patch():
    out = run_py('import discord.http as h; a = h._load_aiohttp(); print(a.hdrs.WEBSOCKET)')
    assert out.strip() == 'websocket'


def test_gc_freeze_option_plumbing():
    out = run_py(
        'import asyncio\n'
        'from discord import Client\n'
        'async def m():\n'
        '    c = Client(); await c._async_setup_hook()\n'
        '    c2 = Client(gc_freeze=False); await c2._async_setup_hook()\n'
        '    print(c._connection.gc_freeze, c2._connection.gc_freeze)\n'
        'asyncio.run(m())'
    )
    assert out.strip() == 'True False'


def test_gc_freeze_semantics():
    # Run in a subprocess so freezing never affects the pytest process
    out = run_py(
        'import gc\n'
        'def cycle():\n'
        "    a = {}; b = {'a': a}; a['b'] = b; return a\n"
        'c = cycle(); gc.collect(); gc.freeze(); del c\n'
        'retained = gc.collect()\n'
        'gc.unfreeze(); reclaimed = gc.collect()\n'
        'fresh = cycle(); del fresh\n'
        'post = gc.collect()\n'
        'print(retained, reclaimed, post)\n'
    )
    retained, reclaimed, post = (int(x) for x in out.split())
    assert reclaimed > retained  # frozen cycles reclaimed after unfreeze
    assert post >= 1  # post-freeze cycles still collected


def member(state, guild, uid):
    return discord.Member(
        data={'user': up(uid), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}, guild=guild, state=state
    )


def thread_payload(i):
    return {
        'id': str(1000 + i),
        'parent_id': '500',
        'owner_id': '2',
        'name': f't{i}',
        'type': 11,
        'message_count': 1,
        'member_count': 1,
        'rate_limit_per_user': 0,
        'flags': 0,
        'thread_metadata': {
            'archived': False,
            'auto_archive_duration': 1440,
            'archive_timestamp': '2024-01-01T00:00:00+00:00',
            'locked': False,
            'invitable': True,
        },
    }


@pytest.mark.asyncio
async def test_cache_stats_snapshot():
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    guild = Guild(state=state, data={'id': '100', 'roles': [{'id': '100', 'name': '@everyone'}], 'member_count': 50})
    state._guilds[100] = guild
    channel = TextChannel(state=state, guild=guild, data={'id': '500', 'name': 'c', 'type': 0, 'position': 0})
    guild._add_channel(channel)

    state.max_member_cache = 5
    for i in range(10):
        guild._add_member(member(state, guild, i))
    guild.get_member(7)  # hit (0-4 evicted)
    guild.get_member(8)  # hit
    guild.get_member(99)  # miss
    for i in range(5):
        guild._add_thread(discord.Thread(guild=guild, state=state, data=thread_payload(i)))
    state.store_presence(
        7,
        state.create_presence(
            {'user': {'id': '7'}, 'status': 'online', 'activities': [], 'client_status': {}, 'processed_at_timestamp': 0}, 7
        ),
        100,
    )

    stats = await client.cache_stats()
    assert stats.guilds == 1
    # Batched eviction (hysteresis to ~95% of cap): 10 adds against cap 5
    # land at 4 live / 6 evicted
    assert stats.members == 4 and stats.evicted_members == 6
    assert stats.member_hits == 2 and stats.member_misses == 1
    assert stats.threads == 5 and stats.evicted_threads == 0
    assert stats.guild_presences == 1
    assert stats.member_cache_cap == 5 and stats.thread_cache_cap == 5000
    assert stats.event_loop_lag >= 0
    assert stats.per_guild[100]['members'] == 4
    assert stats.per_guild[100]['evicted_members'] == 6
    # NamedTuple conveniences
    assert discord.CacheStats is not None


@pytest.mark.asyncio
async def test_dispatch_backpressure_tracking_and_warning():
    client = Client(dispatch_backlog_warning=5)
    await client._async_setup_hook()
    warnings = []

    @client.event
    async def on_dispatch_backlog(pending, oldest_age):
        warnings.append((pending, oldest_age))

    release = asyncio.Event()

    async def slow_handler(x):
        await release.wait()

    client.on_burst = slow_handler
    for i in range(20):
        client.dispatch('burst', i)
    await asyncio.sleep(0.1)

    backlog, oldest_age, created, completed = client._dispatch_backlog_stats()
    assert backlog >= 15
    assert oldest_age > 0.05
    assert created >= 21  # 20 burst + the backlog warning dispatch
    assert warnings and warnings[0][0] >= 5

    release.set()
    await asyncio.sleep(0.2)
    backlog2, _, created2, completed2 = client._dispatch_backlog_stats()
    assert backlog2 == 0
    assert completed2 == created2


@pytest.mark.asyncio
async def test_dispatch_backpressure_disabled():
    client = Client(dispatch_backlog_warning=0)
    await client._async_setup_hook()

    async def handler(x):
        await asyncio.sleep(0.2)

    client.on_burst = handler
    for i in range(10):
        client.dispatch('burst', i)
    await asyncio.sleep(0.05)
    assert client._dispatch_backlog_stats()[0] >= 9
    # No dispatch_backlog handler was registered and none should have fired
    assert 'on_dispatch_backlog' not in client._event_handlers
    await asyncio.sleep(0.3)
