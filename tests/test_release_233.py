"""v2.3.3: the last audit questions (validated and skipped with a warning) and the
features moved up from the v2.3.4 plan."""

from __future__ import annotations

import asyncio
import logging

import pytest

import discord
from discord import utils
from discord.activity import create_activity

from test_user_paths import _client

GUILD_ID = 1
CHANNEL_ID = 501


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    # Every test sees its warnings, whatever ran before it
    monkeypatch.setattr(utils, '_warned_once', set(), raising=False)


def test_warn_once_logs_each_key_once(caplog):
    logger = logging.getLogger('discord.test')
    with caplog.at_level(logging.WARNING, logger='discord.test'):
        for _ in range(3):
            utils._warn_once(logger, 'key-a', 'first %s', 'a')
        utils._warn_once(logger, 'key-b', 'second')
    assert caplog.text.count('first a') == 1 and caplog.text.count('second') == 1


# --- Spotify.track_url for local files ------------------------------------------------------


def test_spotify_track_url_of_a_local_file_is_empty_and_warns(caplog):
    """It returned https://open.spotify.com/track/None."""
    local = create_activity({'type': 2, 'name': 'Spotify', 'party': {'id': 'spotify:1'}}, None, 5)  # type: ignore
    assert isinstance(local, discord.Spotify) and local.track_id is None
    with caplog.at_level(logging.WARNING, logger='discord.activity'):
        assert local.track_url == ''
        assert local.track_url == ''
    assert caplog.text.count('Spotify.track_url is empty for a local file') == 1

    track = create_activity({'type': 2, 'name': 'Spotify', 'sync_id': 'abc', 'party': {'id': 'spotify:1'}}, None, 5)  # type: ignore
    assert track.track_url == 'https://open.spotify.com/track/abc'  # type: ignore


# --- Custom theme colors ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_custom_theme_color_that_is_not_hex_is_skipped(caplog):
    from discord.settings import _client_theme_fields
    from test_refactor_fixes import _blank_user_settings

    if 'primary_color' in _client_theme_fields():
        pytest.skip('the installed discord_protos still has the old fields')
    settings = await _blank_user_settings().edit(client_theme=(utils.MISSING, 3, utils.MISSING))
    custom = settings.settings.appearance.client_theme_settings.custom_user_theme_settings
    custom.colors.append('rebeccapurple')
    with caplog.at_level(logging.WARNING, logger='discord.settings'):
        assert settings.client_theme == (0, 3, 0.0)
        assert settings.client_theme == (0, 3, 0.0)
    assert caplog.text.count('is not a hex color') == 1


# --- close() with handlers still running ---------------------------------------------------


@pytest.mark.asyncio
async def test_close_warns_about_handlers_that_are_still_running(caplog):
    client = discord.Client()
    await client._async_setup_hook()
    release = asyncio.Event()

    @client.event
    async def on_slow():
        await release.wait()

    client.dispatch('slow')
    await asyncio.sleep(0)
    with caplog.at_level(logging.WARNING, logger='discord.client'):
        await client.close()
    assert '1 event handler(s) were still running' in caplog.text
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_closing_from_a_handler_does_not_count_that_handler(caplog):
    client = discord.Client()
    await client._async_setup_hook()
    done = asyncio.Event()

    @client.event
    async def on_shutdown():
        await client.close()
        done.set()

    with caplog.at_level(logging.WARNING, logger='discord.client'):
        client.dispatch('shutdown')
        await asyncio.wait_for(done.wait(), 2)
    assert 'still running' not in caplog.text


# --- Audit log changes without old_value or new_value -------------------------------------------


@pytest.mark.asyncio
async def test_an_audit_log_change_without_old_value_reads_as_none():
    from test_audit3_round2 import _audit_entry

    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    entry = _audit_entry(
        state,
        guild,
        {
            'id': '9',
            'user_id': '100',
            'target_id': str(CHANNEL_ID),
            'action_type': discord.AuditLogAction.channel_update.value,
            'changes': [{'key': 'name', 'new_value': 'renamed'}],
        },
    )
    assert entry.before.name is None and entry.after.name == 'renamed'
    await client.close()


# --- on_raw_typing --------------------------------------------------------------------------


def _typing(channel_id=CHANNEL_ID, user_id=100, **extra):
    return {
        'channel_id': str(channel_id),
        'guild_id': str(GUILD_ID),
        'user_id': str(user_id),
        'timestamp': 1_700_000_000,
        **extra,
    }


def test_enable_raw_typing_is_a_client_option():
    assert discord.Client(enable_raw_typing=True)._connection.raw_typing_flag is True
    assert discord.Client()._connection.raw_typing_flag is False


@pytest.mark.asyncio
async def test_raw_typing_is_off_by_default():
    client, state = await _client()
    seen = []

    @client.event
    async def on_raw_typing(payload):
        seen.append(payload)

    state.parse_typing_start(_typing())  # type: ignore
    await asyncio.sleep(0)
    assert seen == [], 'opt-in: nothing is built or dispatched without enable_raw_typing'
    await client.close()


@pytest.mark.asyncio
async def test_raw_typing_covers_what_on_typing_drops():
    client, state = await _client()
    state.raw_typing_flag = True
    raw, typing = [], []

    @client.event
    async def on_raw_typing(payload):
        raw.append(payload)

    @client.event
    async def on_typing(channel, user, when):
        typing.append(user)

    state.parse_typing_start(_typing())  # type: ignore # a cached channel and member
    state.parse_typing_start(_typing(channel_id=9999, user_id=424242))  # type: ignore # neither is cached
    await asyncio.sleep(0)

    assert len(typing) == 1, 'on_typing only fires when it can resolve both'
    assert len(raw) == 2
    first, second = raw
    assert first.user is not None and first.user.id == 100 and first.guild_id == GUILD_ID
    assert first.timestamp == discord.utils.parse_timestamp(1_700_000_000, ms=False)
    assert (second.channel_id, second.user_id, second.user) == (9999, 424242, None)
    await client.close()


# --- on_raw_thread_update ------------------------------------------------------------------


def _thread(thread_id=700, name='t'):
    return {
        'id': str(thread_id),
        'guild_id': str(GUILD_ID),
        'parent_id': str(CHANNEL_ID),
        'owner_id': '100',
        'name': name,
        'type': 11,
        'message_count': 0,
        'member_count': 1,
        'thread_metadata': {
            'archived': False,
            'auto_archive_duration': 60,
            'archive_timestamp': '2024-01-01T00:00:00+00:00',
        },
    }


@pytest.mark.asyncio
async def test_raw_thread_update_fires_for_cached_and_uncached_threads():
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    raw, updates = [], []

    @client.event
    async def on_raw_thread_update(payload):
        raw.append(payload)

    @client.event
    async def on_thread_update(before, after):
        updates.append((before.name, after.name))

    guild._add_thread(discord.Thread(guild=guild, state=state, data=_thread(700)))  # type: ignore
    state.parse_thread_update(_thread(700, name='renamed'))  # type: ignore
    state.parse_thread_update(_thread(701, name='evicted'))  # type: ignore # not in the cache
    await asyncio.sleep(0)

    assert [(p.thread_id, p.thread is not None) for p in raw] == [(700, True), (701, False)]
    assert raw[0].parent_id == CHANNEL_ID and raw[0].thread_type is discord.ChannelType.public_thread
    assert raw[1].data['name'] == 'evicted'
    assert updates == [('t', 'renamed')], 'on_thread_update is unchanged'
    await client.close()


# --- ActivityType.unknown -----------------------------------------------------------------


def test_activity_type_unknown_exists():
    """It was documented but missing, so ActivityType.unknown raised AttributeError."""
    from discord.enums import try_enum

    assert discord.ActivityType.unknown.value == -1
    assert try_enum(discord.ActivityType, -1) is discord.ActivityType.unknown
    assert try_enum(discord.ActivityType, 9).name == 'unknown_9', 'other unknown values are unchanged'
    assert create_activity({'name': 'x'}, None, 5).type is discord.ActivityType.playing, 'a missing type still means playing'  # type: ignore


def test_an_unknown_activity_type_is_sent_as_playing(caplog):
    activity = discord.Activity(name='x', type=discord.ActivityType.unknown)
    with caplog.at_level(logging.WARNING, logger='discord.activity'):
        assert activity.to_dict()['type'] == 0
        assert activity.to_dict()['type'] == 0
    assert caplog.text.count('cannot be sent to Discord') == 1
    assert discord.Activity(name='x', type=discord.ActivityType.watching).to_dict()['type'] == 3
