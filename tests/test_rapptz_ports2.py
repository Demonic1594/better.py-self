"""Regression tests for features ported from Rapptz/discord.py (round 2)."""

from __future__ import annotations

import pytest

import discord
from discord.channel import VoiceChannel
from discord.message import Message, MessageInteractionMetadata
from test_abuse import make_env, msg_payload
from test_audit_round3 import _ready_client
from test_persistence import drive_ready, guild_payload, ready_payload, up

VOICE_STATE = {'session_id': 's', 'deaf': False, 'mute': False, 'self_deaf': False, 'self_mute': True, 'suppress': False}


@pytest.mark.asyncio
async def test_member_fetch_voice_uses_the_voice_state_routes():
    """62e52803: Member.fetch_voice reads the voice state from the API, through
    the @me route for our own member and the user route for others."""
    client, state, guild, _ = await make_env()
    state.user = discord.ClientUser(state=state, data={**up(5), 'bot': True})  # type: ignore[arg-type]
    voice = VoiceChannel(
        state=state,
        guild=guild,
        data={'id': '700', 'name': 'v', 'type': 2, 'position': 0, 'bitrate': 64000, 'user_limit': 0},
    )  # type: ignore[arg-type]
    guild._add_channel(voice)
    calls = []

    async def get_voice_state(guild_id, user_id):
        calls.append(('user', guild_id, user_id))
        return {**VOICE_STATE, 'channel_id': '700', 'user_id': str(user_id)}

    async def get_my_voice_state(guild_id):
        calls.append(('me', guild_id))
        return {**VOICE_STATE, 'channel_id': None, 'user_id': '5'}

    state.http.get_voice_state = get_voice_state  # type: ignore[method-assign]
    state.http.get_my_voice_state = get_my_voice_state  # type: ignore[method-assign]
    member = discord.Member(data={'user': up(42), 'roles': []}, guild=guild, state=state)  # type: ignore[arg-type]
    result = await member.fetch_voice()
    assert calls[-1] == ('user', guild.id, 42)
    assert result.channel is voice and result.self_mute is True
    me = discord.Member(data={'user': up(5), 'roles': []}, guild=guild, state=state)  # type: ignore[arg-type]
    result = await me.fetch_voice()
    assert calls[-1] == ('me', guild.id) and result.channel is None


@pytest.mark.asyncio
async def test_member_fetch_voice_on_a_user_account_uses_the_cache():
    """Live: the voice state routes answer 403 (code 20002, bots only) to user
    accounts. A user account reads the cache instead, or raises a clear error."""
    client, state, guild, _ = await make_env()
    state.user = discord.ClientUser(state=state, data=up(5))  # type: ignore[arg-type]
    voice = VoiceChannel(
        state=state,
        guild=guild,
        data={'id': '700', 'name': 'v', 'type': 2, 'position': 0, 'bitrate': 64000, 'user_limit': 0},
    )  # type: ignore[arg-type]
    guild._add_channel(voice)

    async def refused(*args):
        raise AssertionError('a user account must not call the bot-only voice state routes')

    state.http.get_voice_state = refused  # type: ignore[method-assign]
    state.http.get_my_voice_state = refused  # type: ignore[method-assign]
    member = discord.Member(data={'user': up(42), 'roles': []}, guild=guild, state=state)  # type: ignore[arg-type]
    guild._add_member(member)
    with pytest.raises(discord.ClientException):
        await member.fetch_voice()  # not in voice per the cache
    guild._update_voice_state({**VOICE_STATE, 'channel_id': '700', 'user_id': '42'}, 700)  # type: ignore[arg-type]
    result = await member.fetch_voice()
    assert result.channel is voice
    with pytest.raises(discord.ClientException, match='bot accounts'):
        await member.fetch_voice(fallback_to_cache=False)


@pytest.mark.asyncio
async def test_member_fetch_voice_falls_back_when_discord_refuses_as_bot_only():
    client, state, guild, _ = await make_env()
    state.user = discord.ClientUser(state=state, data={**up(5), 'bot': True})  # type: ignore[arg-type]

    class Resp:
        status = 403
        reason = 'Forbidden'

    async def refused(*args):
        raise discord.Forbidden(Resp(), {'code': 20002, 'message': 'Only bots can use this endpoint'})  # type: ignore[arg-type]

    state.http.get_voice_state = refused  # type: ignore[method-assign]
    member = discord.Member(data={'user': up(42), 'roles': []}, guild=guild, state=state)  # type: ignore[arg-type]
    with pytest.raises(discord.ClientException, match='bot-only'):
        await member.fetch_voice(fallback_to_cache=False)
    with pytest.raises(discord.ClientException, match='not in a voice channel'):
        await member.fetch_voice()


def _metadata(**extra):
    return {
        'id': '1300000000000000000',
        'type': 2,
        'user': up(7),
        'authorizing_integration_owners': {'0': '1', '1': '7'},
        **extra,
    }


@pytest.mark.asyncio
async def test_message_interaction_metadata_is_parsed():
    """963bb085 / 814ce3c8: Message.interaction_metadata, including the context
    menu targets and the interaction behind a submitted modal."""
    client, state, guild, channel = await make_env()
    data = msg_payload(interaction_metadata=_metadata(target_user=up(9), target_message_id='1200'))
    message = Message(state=state, channel=channel, data=data)  # type: ignore[arg-type]
    meta = message.interaction_metadata
    assert isinstance(meta, MessageInteractionMetadata)
    assert meta.id == 1300000000000000000 and meta.type is discord.InteractionType.application_command
    assert meta.user.id == 7 and meta.target_user is not None and meta.target_user.id == 9
    assert meta.target_message_id == 1200 and meta.modal_interaction is None
    assert meta.is_guild_integration() and meta.is_user_integration()

    modal = _metadata(type=5, triggering_interaction_metadata=_metadata(type=3, interacted_message_id='1100'))
    message = Message(state=state, channel=channel, data=msg_payload(interaction_metadata=modal))  # type: ignore[arg-type]
    inner = message.interaction_metadata.modal_interaction  # type: ignore[union-attr]
    assert inner is not None and inner.type is discord.InteractionType.component and inner.interacted_message_id == 1100

    plain = Message(state=state, channel=channel, data=msg_payload())  # type: ignore[arg-type]
    assert plain.interaction_metadata is None


@pytest.mark.asyncio
async def test_message_update_sets_interaction_metadata():
    """The update path (MESSAGE_UPDATE) fills interaction_metadata too."""
    client, state, guild, channel = await make_env()
    message = Message(state=state, channel=channel, data=msg_payload())  # type: ignore[arg-type]
    message._update({'id': '1000', 'channel_id': '500', 'interaction_metadata': _metadata()})  # type: ignore[arg-type]
    assert message.interaction_metadata is not None and message.interaction_metadata.user.id == 7


def _presence(user_id, guild_id):
    return {
        'user': {'id': str(user_id)},
        'guild_id': str(guild_id),
        'status': 'dnd',
        'client_status': {'desktop': 'dnd', 'mobile': 'idle'},
        'activities': [{'type': 0, 'name': 'a game'}],
    }


@pytest.mark.asyncio
async def test_raw_presence_update_covers_uncached_members():
    """418a7915: on_raw_presence_update fires even when the member is not cached,
    which on_presence_update cannot (here: an evicted or never-cached member)."""
    client = await _ready_client(enable_raw_presences=True)
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, n_members=2)]
    await drive_ready(client, ready)
    seen = []

    async def on_raw_presence_update(payload):
        seen.append(payload)

    client.event(on_raw_presence_update)
    client._connection.parse_presence_update(_presence(999, 1))  # type: ignore[arg-type]
    await discord.utils.asyncio.sleep(0)
    assert len(seen) == 1
    raw = seen[0]
    assert isinstance(raw, discord.RawPresenceUpdateEvent)
    assert raw.user_id == 999 and raw.guild_id == 1 and raw.guild is client.get_guild(1)
    assert raw.status is discord.Status.dnd and raw.mobile_status is discord.Status.idle
    assert raw.web_status is discord.Status.offline
    assert [a.name for a in raw.activities] == ['a game']


@pytest.mark.asyncio
async def test_raw_presence_update_is_opt_in(monkeypatch):
    """Without enable_raw_presences nothing is built or dispatched, even with a listener."""
    client = await _ready_client()
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, n_members=2)]
    await drive_ready(client, ready)
    state = client._connection
    built = []
    original = state.create_presence
    monkeypatch.setattr(state, 'create_presence', lambda data, user_id: built.append(user_id) or original(data, user_id))
    seen = []

    async def on_raw_presence_update(payload):
        seen.append(payload)

    client.event(on_raw_presence_update)
    state.parse_presence_update(_presence(999, 1))  # type: ignore[arg-type]
    await discord.utils.asyncio.sleep(0)
    assert built == [] and seen == []
