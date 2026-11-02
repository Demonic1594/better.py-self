"""
Offline memory benchmark for a logged-in account.

Builds a synthetic but realistically shaped user-account READY payload
(many guilds with roles/channels/emojis/stickers, friends, DMs, read states,
guild settings), drives it through a real :class:`discord.Client` without a
network, and reports what stays allocated afterwards.

Usage::

    python tests/memory_bench.py            # summary
    python tests/memory_bench.py --top 25   # plus top allocation sites

Not collected by pytest (no ``test_`` prefix); ``test_memory_budget`` in
``test_memory.py`` imports :func:`measure` for a regression bound.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import gc
import os
import sys
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402

GUILDS = 100
ROLES = 30
CHANNELS = 60
EMOJIS = 40
STICKERS = 5
FRIENDS = 300
DMS = 200
READ_STATES = 2000

AVATAR = 'a_' + '0123456789abcdef' * 2


def _user(uid: int) -> dict:
    return {
        'id': str(uid),
        'username': f'user{uid}',
        'discriminator': '0',
        'global_name': f'User {uid}',
        'avatar': AVATAR,
        'avatar_decoration_data': None,
        'public_flags': 0,
        'primary_guild': None,
        'clan': None,
        'collectibles': None,
        'display_name_styles': None,
    }


def _guild(gid: int) -> dict:
    base = gid * 10_000
    everyone = {'id': str(gid), 'name': '@everyone', 'permissions': '1071698660929', 'position': 0, 'color': 0}
    roles = [everyone] + [
        {
            'id': str(base + r),
            'name': f'Role {r}',
            'permissions': '1071698660929',
            'position': r,
            'color': 0x99AAB5,
            'colors': {'primary_color': 0x99AAB5, 'secondary_color': None, 'tertiary_color': None},
            'hoist': r % 3 == 0,
            'managed': False,
            'mentionable': False,
            'icon': None,
            'unicode_emoji': None,
            'flags': 0,
            'description': None,
            'tags': {},
        }
        for r in range(1, ROLES)
    ]
    overwrites = [
        {'id': str(gid), 'type': 0, 'allow': '0', 'deny': '1024'},
        {'id': str(base + 1), 'type': 0, 'allow': '1024', 'deny': '0'},
    ]
    channels = []
    for c in range(CHANNELS):
        ctype = 4 if c % 10 == 0 else (2 if c % 10 == 9 else 0)
        channels.append(
            {
                'id': str(base + 1000 + c),
                'type': ctype,
                'name': f'channel-{c}',
                'position': c,
                'parent_id': None if ctype == 4 else str(base + 1000 + (c // 10) * 10),
                'permission_overwrites': overwrites,
                'topic': 'A reasonably sized channel topic used for memory benchmarking.' if ctype == 0 else None,
                'nsfw': False,
                'rate_limit_per_user': 0,
                'last_message_id': str(base + 50_000 + c),
                'flags': 0,
                'bitrate': 64000,
                'user_limit': 0,
            }
        )
    emojis = [
        {
            'id': str(base + 3000 + e),
            'name': f'emoji_{e}',
            'roles': [],
            'require_colons': True,
            'managed': False,
            'animated': e % 4 == 0,
            'available': True,
        }
        for e in range(EMOJIS)
    ]
    stickers = [
        {
            'id': str(base + 4000 + s),
            'name': f'sticker {s}',
            'tags': 'smile',
            'type': 2,
            'format_type': 1,
            'description': 'A sticker',
            'available': True,
            'guild_id': str(gid),
        }
        for s in range(STICKERS)
    ]
    return {
        'id': str(gid),
        'properties': {
            'id': str(gid),
            'name': f'Guild number {gid}',
            'icon': AVATAR,
            'splash': None,
            'banner': None,
            'description': None,
            'owner_id': str(base + 9),
            'features': ['COMMUNITY', 'NEWS', 'ANIMATED_ICON', 'INVITE_SPLASH', 'ROLE_ICONS'],
            'verification_level': 1,
            'default_message_notifications': 1,
            'explicit_content_filter': 2,
            'mfa_level': 0,
            'nsfw_level': 0,
            'premium_tier': 1,
            'preferred_locale': 'en-US',
            'system_channel_id': str(base + 1001),
            'rules_channel_id': None,
            'afk_timeout': 300,
        },
        'member_count': 5000,
        'premium_subscription_count': 3,
        'joined_at': '2023-01-01T00:00:00+00:00',
        'large': True,
        'lazy': True,
        'roles': roles,
        'channels': channels,
        'emojis': emojis,
        'stickers': stickers,
        'threads': [],
        'guild_scheduled_events': [],
        'stage_instances': [],
        'soundboard_sounds': [],
        'data_mode': 'full',
        'version': 1,
    }


def ready_payload(user_id: int = 5) -> dict:
    friends = [100 + i for i in range(FRIENDS)]
    users = [_user(u) for u in friends]
    guilds = [_guild(g) for g in range(1, GUILDS + 1)]
    read_states = [
        {'id': str(10_000_000 + i), 'last_message_id': str(20_000_000 + i), 'mention_count': 0, 'last_pin_timestamp': None}
        for i in range(READ_STATES)
    ]
    guild_settings = [
        {
            'guild_id': str(g),
            'muted': False,
            'message_notifications': 1,
            'suppress_everyone': False,
            'suppress_roles': False,
            'mobile_push': True,
            'hide_muted_channels': False,
            'flags': 0,
            'notify_highlights': 0,
            'channel_overrides': [
                {'channel_id': str(g * 10_000 + 1000 + c), 'muted': True, 'message_notifications': 3, 'collapsed': False}
                for c in range(5)
            ],
            'version': 1,
        }
        for g in range(1, GUILDS + 1)
    ]
    return {
        'v': 9,
        'user': _user(user_id) | {'email': 'x@x', 'verified': True, 'purchased_flags': 0, 'flags': 0, 'premium_type': 0},
        'users': users,
        'guilds': guilds,
        'relationships': [{'id': str(u), 'type': 1, 'user_id': str(u), 'nickname': None} for u in friends],
        'private_channels': [
            {'id': str(900_000 + i), 'type': 1, 'recipient_ids': [str(friends[i % FRIENDS])], 'last_message_id': '1'}
            for i in range(DMS)
        ],
        'sessions': [],
        'user_settings_proto': '',
        'required_action': None,
        'auth_token': None,
        'read_state': {'version': 0, 'partial': False, 'entries': read_states},
        'user_guild_settings': {'version': 0, 'partial': False, 'entries': guild_settings},
        'experiments': [],
        'guild_experiments': [],
        'friend_suggestion_count': 0,
        'connected_accounts': [],
        'consents': {},
        'country_code': 'US',
        'api_code_version': 1,
        'session_type': 'normal',
    }


def supplemental_for(ready: dict) -> dict:
    guilds = ready.get('guilds', [])
    return {
        'guilds': [{'id': g['id'], 'voice_states': [], 'embedded_activities': []} for g in guilds],
        'merged_members': [[] for _ in guilds],
        'merged_presences': {'guilds': [[] for _ in guilds], 'friends': []},
        'lazy_private_channels': [],
    }


async def _drive(client: discord.Client, ready: dict) -> None:
    state = client._connection
    state._subscribe_guilds = False
    state._chunk_guilds = False
    client._handlers['connect'] = lambda: None
    client._handlers['ready'] = lambda: None

    async def _noop_chunker(*args, **kwargs):
        return None

    state.chunker = _noop_chunker  # type: ignore[assignment]
    state.parsers['READY'](ready)
    state.parsers['READY_SUPPLEMENTAL'](supplemental_for(ready))
    if state._ready_task is not None:
        try:
            await asyncio.wait_for(state._ready_task, timeout=5)
        except asyncio.TimeoutError:
            pass
    await asyncio.sleep(0)


def member_payloads(gid: int, count: int) -> list:
    base = gid * 10_000
    return [
        {
            'user': _user(1_000_000 + i),
            'roles': [str(base + 1 + i % 7), str(base + 8 + i % 5), str(base + 15 + i % 3)],
            'joined_at': '2023-01-01T00:00:00.123000+00:00',
            'premium_since': None,
            'communication_disabled_until': None,
            'nick': None,
            'pending': False,
            'flags': 0,
            'avatar': None,
            'banner': None,
        }
        for i in range(count)
    ]


async def measure(*, top: int = 0, members: int = 0, **client_kwargs) -> dict:
    """Returns retained bytes after a full READY (the payload itself is freed).

    ``members`` additionally caches that many members in the first guild, as
    member-list chunking of a large guild would.
    """
    payload = ready_payload()
    gc.collect()
    tracemalloc.start(1)
    before = tracemalloc.take_snapshot()

    client = discord.Client(**client_kwargs)
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(state=state, data=_user(5))  # static_login stand-in
    await _drive(client, copy.deepcopy(payload))
    del payload
    if members:
        guild = next(iter(state._guilds.values()))
        for data in member_payloads(guild.id, members):
            guild._add_member(discord.Member(data=data, guild=guild, state=state))
        state.subscriptions._cancel()  # offline: no websocket for the subscription tick
    gc.collect()

    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    filt = [tracemalloc.Filter(False, tracemalloc.__file__), tracemalloc.Filter(False, '<frozen importlib._bootstrap*>')]
    stats = after.filter_traces(filt).compare_to(before.filter_traces(filt), 'lineno')
    retained = sum(s.size_diff for s in stats)
    result = {
        'retained_bytes': retained,
        'guilds': len(state._guilds),
        'channels': sum(len(g._channels) for g in state._guilds.values()),
        'users': len(state._users),
        'members': sum(len(g._members) for g in state._guilds.values()),
    }
    if top:
        result['top'] = [
            (f'{s.traceback[0].filename.split("better.py-self/")[-1]}:{s.traceback[0].lineno}', s.size_diff, s.count_diff)
            for s in sorted(stats, key=lambda s: -s.size_diff)[:top]
        ]
    await client.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--top', type=int, default=0)
    parser.add_argument('--members', type=int, default=0)
    args = parser.parse_args()
    res = asyncio.run(measure(top=args.top, members=args.members, max_member_cache=None, max_total_member_cache=None))
    print(
        f'retained {res["retained_bytes"] / 1024 / 1024:.2f} MiB '
        f'({res["guilds"]} guilds, {res["channels"]} channels, {res["users"]} users, {res["members"]} members)'
    )
    for site, size, count in res.get('top', []):
        print(f'{size / 1024:9.1f} KiB {count:8d}  {site}')


if __name__ == '__main__':
    main()
