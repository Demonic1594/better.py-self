"""
Performance regression smoke tests.

Timing assertions are unstable on shared CI runners, so these tests assert
*allocation counts* (via ``sys.getallocatedblocks``) and operation counts
instead. Ceilings are expressed relative to the interpreter's measured
allocation noise floor, with headroom, so they hold across Python versions.
A regression that re-introduces per-call object churn in a hot path (for
example re-computing the permission masks, or rebuilding an embed through
a dict roundtrip) will blow through the ceiling.
"""

from __future__ import annotations

import gc
import sys

import discord
import discord.utils as u
from discord import Embed, Permissions
from discord.components import _component_factory
from discord.http import Route


def alloc_delta(func, *args, **kwargs) -> int:
    """Runs func and returns the fewest blocks allocated over 5 runs."""
    results = []
    for _ in range(5):
        gc.collect()
        before = sys.getallocatedblocks()
        func(*args, **kwargs)
        results.append(sys.getallocatedblocks() - before)
    return min(results)


def noise_floor() -> int:
    return alloc_delta(lambda: None)


def test_permission_masks_alloc_free():
    noise = noise_floor()
    # Both masks are precomputed constants: reading them costs at most the
    # bound-classmethod object (and, for _dm_permissions, the wrapper)
    assert alloc_delta(Permissions._timeout_mask) <= noise + 1
    assert alloc_delta(Permissions._dm_permissions) <= noise + 4


def test_escape_mentions_allocations():
    noise = noise_floor()
    text = 'hi @everyone <@123456789012345678> and <@!876543210987654321>'
    # Result string + regex sub machinery only; the uncompiled-regex version
    # allocated the pattern cache lookup and more per call
    assert alloc_delta(u.escape_mentions, text) <= noise + 8


def test_snowflake_time_allocations():
    noise = noise_floor()
    assert alloc_delta(u.snowflake_time, 1300000000000000000) <= noise + 6


def test_embed_to_dict_allocations():
    noise = noise_floor()
    e = Embed(title='t', description='d', colour=1)
    e.add_field(name='a', value='b')
    e.set_footer(text='f')
    assert alloc_delta(e.to_dict) <= noise + 12


def test_embed_copy_allocations():
    noise = noise_floor()
    e = Embed(title='t', description='d', colour=1)
    e.add_field(name='a', value='b')
    e.set_footer(text='f')
    e.set_author(name='au')
    # slot-copy with one-level dict copies; the old from_dict(to_dict())
    # roundtrip allocated several times more
    assert alloc_delta(e.copy) <= noise + 30


def test_component_factory_allocation_stable():
    payload = {'type': 2, 'style': 1, 'label': 'x', 'custom_id': 'c'}
    first = alloc_delta(_component_factory, payload)
    # Re-running must be allocation-stable (dict dispatch, no string churn)
    assert alloc_delta(_component_factory, payload) <= first + 2


def test_route_precomputed_alloc_free():
    noise = noise_floor()
    route = Route('GET', '/channels/{channel_id}/messages', channel_id=1)
    # key and major_parameters are computed at construction: reading them
    # costs at most the tuple this test builds
    assert alloc_delta(lambda: (route.key, route.major_parameters)) <= noise + 1


def test_flags_iteration_allocations():
    from discord import ApplicationFlags

    noise = noise_floor()
    f = ApplicationFlags(embedded=True, gateway_presence=True)
    # One tuple per item from __iter__ is expected; the old per-entry
    # class-dict scan with isinstance checks allocated far more
    n = len(ApplicationFlags._ITER_FLAGS)
    assert alloc_delta(lambda: list(iter(f))) <= noise + n * 3


def test_member_noop_update_dispatch_free():
    """A no-op member update must not build an old copy (the pre-optimization
    behavior allocated a full Member copy per update)."""
    import asyncio
    from discord import Client, Guild

    async def main():
        client = Client()
        await client._async_setup_hook()
        state = client._connection
        guild = Guild(state=state, data={'id': '1', 'roles': [{'id': '1', 'name': '@everyone'}], 'member_count': 5})
        state._guilds[1] = guild
        member = discord.Member(
            data={
                'user': {'id': '7', 'username': 'u', 'discriminator': '0001', 'global_name': None, 'avatar': None},
                'roles': [],
                'joined_at': '2023-01-01T00:00:00+00:00',
            },
            guild=guild,
            state=state,
        )
        payload = {
            'user': {'id': '7'},
            'roles': [],
            'nick': None,
            'pending': False,
            'premium_since': None,
            'communication_disabled_until': None,
            'avatar': None,
            'banner': None,
            'flags': 0,
        }
        updates = [member._update(payload) for _ in range(50)]
        assert not any(up is not None for up in updates), 'no-op updates must return None (no dispatch)'
        # A real change still dispatches
        changed = member._update({**payload, 'nick': 'new'})
        assert changed is not None

    asyncio.run(main())


def test_presence_noop_update_single_dispatch():
    import asyncio
    from discord import Client, Guild

    async def main():
        client = Client()
        await client._async_setup_hook()
        state = client._connection
        guild = Guild(state=state, data={'id': '1', 'roles': [{'id': '1', 'name': '@everyone'}], 'member_count': 5})
        state._guilds[1] = guild
        guild._add_member(
            discord.Member(
                data={
                    'user': {'id': '7', 'username': 'u', 'discriminator': '0001', 'global_name': None, 'avatar': None},
                    'roles': [],
                    'joined_at': '2023-01-01T00:00:00+00:00',
                },
                guild=guild,
                state=state,
            )
        )
        seen = []

        @client.event
        async def on_presence_update(before, after):
            seen.append(1)

        payload = {
            'user': {'id': '7'},
            'status': 'online',
            'activities': [],
            'client_status': {},
            'processed_at_timestamp': 0,
        }
        for _ in range(50):
            state._handle_presence_update(guild, payload)
        await asyncio.sleep(0)
        assert len(seen) == 1, 'identical presence updates must dispatch exactly once'

    asyncio.run(main())


def test_message_index_o1_lookup():
    """Cache lookup after warmup must not scan (allocation-free dict hit)."""
    import asyncio
    from discord import Client, Guild
    from discord.channel import TextChannel

    async def main():
        client = Client()
        await client._async_setup_hook()
        state = client._connection
        guild = Guild(state=state, data={'id': '1', 'roles': [{'id': '1', 'name': '@everyone'}], 'member_count': 5})
        state._guilds[1] = guild
        channel = TextChannel(state=state, guild=guild, data={'id': '500', 'name': 'c', 'type': 0, 'position': 0})
        guild._add_channel(channel)
        payload = {
            'id': '1000',
            'channel_id': '500',
            'guild_id': '1',
            'author': {'id': '7', 'username': 'u', 'discriminator': '0001', 'global_name': None, 'avatar': None},
            'content': 'hi',
            'timestamp': '2024-01-01T00:00:00+00:00',
            'edited_timestamp': None,
            'tts': False,
            'mention_everyone': False,
            'mentions': [],
            'mention_roles': [],
            'attachments': [],
            'embeds': [],
            'pinned': False,
            'type': 0,
            'flags': 0,
        }
        for i in range(200):
            payload['id'] = str(1000 + i)
            state.parse_message_create(payload)
        noise = noise_floor()
        # Warm the method cache, then a hit must allocate nothing beyond noise
        state._get_message(1100)
        assert alloc_delta(state._get_message, 1100) <= noise

    asyncio.run(main())


def test_throttle_telemetry_allocations():
    """The throttle record (deque append) and the Client summary build must
    stay allocation-light: they sit on the 429 path and get polled by
    pacing hosts."""
    import asyncio
    from collections import deque

    async def main():
        client = discord.Client()
        http = client.http
        events = http._throttle_events['PUT x'] = deque(maxlen=64)
        noise = noise_floor()

        def record() -> None:
            events.append((0.0, 1.0))

        assert alloc_delta(record) <= noise + 2

        for i in range(10):
            events.append((float(i), 0.5))
        http._throttle_events['POST y'] = deque((t, 0.1) for t in range(10))

        def summarize() -> None:
            http_summary = client.http_throttle_events()
            assert len(http_summary) == 2

        assert alloc_delta(summarize) <= noise + 16

        await asyncio.sleep(0)

    asyncio.run(main())
