"""Regression tests for audit round 5: state cleanup and malformed payloads."""

from __future__ import annotations

import asyncio
import logging

import pytest

from discord.application import ManifestLabel
from discord.commands import OptionChoice
from discord.emoji import Emoji
from discord.enums import ApplicationCommandOptionType
from discord.message import Message
from discord.store import SKUPrice
from discord.stream import StreamKey
from discord.tracking import HeadersContext
from discord.utils import sane_wait_for
from test_abuse import make_env, msg_payload
from test_audit_round3 import _ready_client
from test_persistence import drive_ready, ready_payload, up


@pytest.mark.asyncio
async def test_guild_removal_drops_its_streams():
    """A1-02: removing a guild left its streams and stream server updates cached."""
    client, state, guild, channel = await make_env()
    mine = StreamKey.from_guild(guild_id=guild.id, channel_id=500, owner_id=7)
    other = StreamKey.from_guild(guild_id=guild.id + 1, channel_id=600, owner_id=7)
    for key in (mine, other):
        state._streams[key] = object()  # type: ignore[assignment]
        state._stream_server_updates[key] = {}  # type: ignore[assignment]
    state._remove_guild(guild)
    assert mine not in state._streams and mine not in state._stream_server_updates
    assert other in state._streams and other in state._stream_server_updates


@pytest.mark.asyncio
async def test_voice_leave_for_unknown_user_is_not_cached():
    """A1-05: a leave event for a user never seen joining stored a channel-less state."""
    client, state, guild, channel = await make_env()
    data = {'user_id': '777', 'channel_id': None, 'session_id': 's', 'deaf': False, 'mute': False,
            'self_deaf': False, 'self_mute': False, 'suppress': False}  # fmt: skip
    guild._update_voice_state(data, None)  # type: ignore[arg-type]
    assert 777 not in guild._voice_states
    state._update_voice_state(data, None)  # type: ignore[arg-type]
    assert 777 not in state._voice_states


@pytest.mark.asyncio
async def test_deleting_a_dm_drops_its_call():
    """A4-02: a DM's call outlived the channel when no CALL_DELETE followed."""
    client = await _ready_client()
    await drive_ready(client, ready_payload())
    state = client._connection
    channel = state._get_private_channel(900)
    assert channel is not None
    state._calls[900] = object()  # type: ignore[assignment]
    state._remove_private_channel(channel)
    assert 900 not in state._calls


@pytest.mark.asyncio
async def test_malformed_dm_in_ready_is_skipped():
    """A DM with no recipient raised IndexError in READY_SUPPLEMENTAL, dropping the connection."""
    client = await _ready_client()
    ready = ready_payload()
    ready['private_channels'].append({'id': '901', 'type': 1, 'recipient_ids': [], 'last_message_id': '1'})
    await drive_ready(client, ready)
    state = client._connection
    assert state._get_private_channel(900) is not None, 'the valid DM after the bad one must still load'
    assert state._get_private_channel(901) is None
    assert not hasattr(state, '_ready_data'), 'READY_SUPPLEMENTAL must finish'


@pytest.mark.asyncio
async def test_sane_wait_for_cancels_its_own_waiters_on_timeout():
    """C3-04: each timeout left the wrapped coroutine's task pending forever."""
    event = asyncio.Event()
    caller_future = asyncio.get_running_loop().create_future()
    before = asyncio.all_tasks()
    with pytest.raises(asyncio.TimeoutError):
        await sane_wait_for([event.wait(), caller_future], timeout=0.01)
    await asyncio.sleep(0)
    leftover = [t for t in asyncio.all_tasks() - before if not t.done()]
    assert leftover == []
    assert not caller_future.cancelled(), "a future the caller passed in is the caller's to manage"


@pytest.mark.asyncio
async def test_role_locked_emoji_without_own_member_is_not_usable():
    """A3-04: is_usable raised AttributeError when our own member was not cached."""
    client, state, guild, channel = await make_env()
    assert guild.me is None
    emoji = Emoji(guild=guild, state=state, data={'id': '5', 'name': 'x', 'roles': ['1'], 'available': True})
    assert emoji.is_usable() is False


@pytest.mark.asyncio
async def test_malformed_member_does_not_lose_the_chunk(caplog):
    """A3-05: one malformed member raised before the chunk reached its request."""
    client, state, guild, channel = await make_env()
    seen = []
    state.process_chunk_requests = lambda guild_id, nonce, members, complete: seen.append(members)  # type: ignore[method-assign]
    good = {'user': up(301), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}
    with caplog.at_level(logging.WARNING, logger='discord.state'):
        state.parse_guild_members_chunk(
            {'guild_id': str(guild.id), 'members': [{'user': up(300)}, good], 'chunk_index': 0, 'chunk_count': 1}  # type: ignore[arg-type]
        )
    assert [m.id for m in seen[0]] == [301]
    assert any('malformed member' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_poll_result_without_embeds_has_system_content():
    """A5-04: a poll_result message whose embeds were cleared raised IndexError."""
    client, state, guild, channel = await make_env()
    message = Message(state=state, channel=channel, data=msg_payload(type=46, embeds=[]))  # type: ignore[arg-type]
    assert message.system_content.endswith('poll has closed.')


def test_option_choice_of_another_type_keeps_its_value():
    """A7-04: a choice for a type other than string/integer/number had no value slot."""
    choice = OptionChoice({'name': 'yes', 'value': True}, ApplicationCommandOptionType.boolean)  # type: ignore[arg-type]
    assert choice.value is True
    assert 'yes' in repr(choice)


def test_free_sku_on_sale_has_no_division_by_zero():
    """A7-05: a zero-price SKU with a sale price raised ZeroDivisionError."""
    price = SKUPrice.from_private({'price_tier': 0, 'sale_price_tier': 0})  # type: ignore[arg-type]
    assert price.amount == 0 and price.sale_percentage == 0


def test_manifest_label_does_not_modify_the_payload():
    """A7-12: ManifestLabel wrote application_id into the caller's dict."""
    data = {'id': '1', 'name': 'label'}
    label = ManifestLabel(data=data, application_id=5)  # type: ignore[arg-type]
    assert isinstance(label, ManifestLabel) and label.application_id == 5
    assert data == {'id': '1', 'name': 'label'}


@pytest.mark.asyncio
async def test_info_api_without_browser_version_falls_back(monkeypatch):
    """A7-10: an info API answer without browser_version made HeadersContext.default raise."""

    async def api(*args, **kwargs):
        return {'properties': {'os': 'Windows', 'browser': 'Chrome'}}

    async def build_number(*args, **kwargs):
        return 12345

    async def chrome_version(*args, **kwargs):
        return '130.0.0.0'

    monkeypatch.setattr(HeadersContext, 'fetch_api_properties', staticmethod(api))
    monkeypatch.setattr(HeadersContext, 'scrape_client_build_number', staticmethod(build_number))
    monkeypatch.setattr(HeadersContext, 'fetch_chrome_version', staticmethod(chrome_version))
    context = await HeadersContext.default(None)  # type: ignore[arg-type]
    assert context.super_properties['browser_version'] == '130.0.0.0'
