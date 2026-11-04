"""Tests for the soundboard and voice-channel-effect port from Rapptz/discord.py (round 3)."""

from __future__ import annotations

import asyncio

import pytest

import discord
from discord.channel import VoiceChannel
from discord.utils import _bytes_to_base64_data
from test_abuse import make_env
from test_audit_round3 import _ready_client
from test_persistence import drive_ready, guild_payload, ready_payload

SOUND_ID = 1300000000000000001


def _sound(guild_id, sound_id=SOUND_ID, name='honk', **extra):
    return {
        'sound_id': str(sound_id),
        'guild_id': str(guild_id),
        'name': name,
        'volume': 0.5,
        'emoji_id': None,
        'emoji_name': '🦆',
        'available': True,
        'user_id': '7',
        **extra,
    }


@pytest.mark.asyncio
async def test_sounds_are_only_cached_for_requested_guilds():
    """A guild's cache exists only after SOUNDBOARD_SOUNDS (the op 31 reply): a
    create event elsewhere dispatches but does not fake a complete cache."""
    client, state, guild, _ = await make_env()
    created = []
    state.dispatch = lambda event, *args: created.append((event, args)) if event.startswith('soundboard') else None  # type: ignore[method-assign]
    state.parse_guild_soundboard_sound_create(_sound(guild.id))  # type: ignore[arg-type]
    assert created and created[0][0] == 'soundboard_sound_create'
    assert list(guild.soundboard_sounds) == [] and guild._soundboard_sounds is None

    state.parse_soundboard_sounds({'guild_id': str(guild.id), 'soundboard_sounds': [_sound(guild.id)]})  # type: ignore[arg-type]
    sound = guild.get_soundboard_sound(SOUND_ID)
    assert sound is not None and sound.name == 'honk' and sound.emoji is not None and sound.emoji.name == '🦆'
    assert state.get_soundboard_sound(SOUND_ID) is sound and client.soundboard_sounds == [sound]

    state.parse_guild_soundboard_sound_create(_sound(guild.id, sound_id=SOUND_ID + 1, name='quack'))  # type: ignore[arg-type]
    assert {s.name for s in guild.soundboard_sounds} == {'honk', 'quack'}


@pytest.mark.asyncio
async def test_sound_update_and_delete_events():
    client, state, guild, _ = await make_env()
    events = []
    state.dispatch = lambda event, *args: events.append((event, args))  # type: ignore[method-assign]
    state.parse_soundboard_sounds({'guild_id': str(guild.id), 'soundboard_sounds': [_sound(guild.id)]})  # type: ignore[arg-type]

    state.parse_guild_soundboard_sound_update(_sound(guild.id, name='HONK'))  # type: ignore[arg-type]
    event, (before, after) = events[-1]
    assert event == 'soundboard_sound_update' and before.name == 'honk' and after.name == 'HONK'

    state.parse_guild_soundboard_sounds_update(
        {'guild_id': str(guild.id), 'soundboard_sounds': [_sound(guild.id, name='honk!', volume=1.0)]}  # type: ignore[arg-type]
    )
    assert guild.get_soundboard_sound(SOUND_ID).volume == 1.0  # type: ignore[union-attr]

    state.parse_guild_soundboard_sound_delete({'guild_id': str(guild.id), 'sound_id': str(SOUND_ID)})  # type: ignore[arg-type]
    assert events[-1][0] == 'soundboard_sound_delete' and list(guild.soundboard_sounds) == []
    count = len(events)
    state.parse_guild_soundboard_sound_delete({'guild_id': str(guild.id), 'sound_id': str(SOUND_ID)})  # type: ignore[arg-type]
    assert len(events) == count, 'an uncached sound has nothing to dispatch'


@pytest.mark.asyncio
async def test_request_soundboard_sounds_waits_for_each_guild(monkeypatch):
    """Client.request_soundboard_sounds sends op 31 and returns once every guild answered."""
    client = await _ready_client()
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1), guild_payload(2)]
    await drive_ready(client, ready)
    sent = []

    class FakeWS:
        def __init__(self):
            self.listeners = []

        def wait_for(self, event, predicate):
            future = client.loop.create_future()
            self.listeners.append((event, predicate, future))
            return future

        async def request_soundboard_sounds(self, guild_ids):
            sent.append(guild_ids)
            for gid in guild_ids:
                data = {'guild_id': str(gid), 'soundboard_sounds': [_sound(gid, sound_id=SOUND_ID + gid)]}
                client._connection.parse_soundboard_sounds(data)  # type: ignore[arg-type]
                for event, predicate, future in self.listeners:
                    if event == 'SOUNDBOARD_SOUNDS' and predicate(data) and not future.done():
                        future.set_result(data)

    client.ws = FakeWS()  # type: ignore[assignment]
    sounds = await client.request_soundboard_sounds([discord.Object(1), discord.Object(2), discord.Object(1)])
    assert sent == [[1, 2]]
    assert sorted(s.id for s in sounds) == [SOUND_ID + 1, SOUND_ID + 2]


@pytest.mark.asyncio
async def test_request_soundboard_sounds_times_out_and_unregisters():
    client = await _ready_client()
    futures = []

    class SilentWS:
        def wait_for(self, event, predicate):
            future = client.loop.create_future()
            futures.append(future)
            return future

        async def request_soundboard_sounds(self, guild_ids):
            pass

    client.ws = SilentWS()  # type: ignore[assignment]
    with pytest.raises(asyncio.TimeoutError):
        await client.request_soundboard_sounds([discord.Object(1)], timeout=0.05)
    assert futures and all(f.cancelled() for f in futures)


@pytest.mark.asyncio
async def test_request_soundboard_sounds_bounds_the_send():
    """The timeout also covers the send, which the gateway rate limiter can delay."""
    client = await _ready_client()
    futures = []

    class RateLimitedWS:
        def wait_for(self, event, predicate):
            future = client.loop.create_future()
            futures.append(future)
            return future

        async def request_soundboard_sounds(self, guild_ids):
            await asyncio.sleep(60)

    client.ws = RateLimitedWS()  # type: ignore[assignment]
    started = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.TimeoutError):
        await client.request_soundboard_sounds([discord.Object(1)], timeout=0.05)
    assert asyncio.get_running_loop().time() - started < 5
    assert futures and all(f.cancelled() for f in futures)


@pytest.mark.asyncio
async def test_voice_channel_effect_in_guild_and_private_call():
    """VOICE_CHANNEL_EFFECT_SEND for a guild channel and for a DM call (no guild_id)."""
    client = await _ready_client()
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, n_members=3)]
    await drive_ready(client, ready)
    state = client._connection
    guild = client.get_guild(1)
    voice = VoiceChannel(
        state=state,
        guild=guild,
        data={'id': '700', 'name': 'v', 'type': 2, 'position': 0, 'bitrate': 64000, 'user_limit': 0},
    )  # type: ignore[arg-type]
    guild._add_channel(voice)  # type: ignore[union-attr]
    effects = []
    state.dispatch = lambda event, *args: effects.append(args[0]) if event == 'voice_channel_effect' else None  # type: ignore[method-assign]

    state.parse_voice_channel_effect_send(
        {'guild_id': '1', 'channel_id': '700', 'user_id': '101', 'sound_id': '3', 'sound_volume': 0.7}  # type: ignore[arg-type]
    )
    effect = effects[-1]
    assert effect.channel is voice and effect.user.id == 101 and effect.is_sound()
    assert effect.sound.is_default() and effect.sound.volume == 0.7

    state.parse_voice_channel_effect_send(
        {'channel_id': '900', 'user_id': '101', 'emoji': {'name': '🔥', 'id': None}, 'animation_type': 1, 'animation_id': 5}  # type: ignore[arg-type]
    )
    effect = effects[-1]
    assert isinstance(effect.channel, discord.DMChannel) and not effect.is_sound()
    assert effect.emoji.name == '🔥' and effect.animation.type is discord.VoiceChannelEffectAnimationType.basic


@pytest.mark.asyncio
async def test_send_sound_routes():
    client, state, guild, _ = await make_env()
    voice = VoiceChannel(
        state=state,
        guild=guild,
        data={'id': '700', 'name': 'v', 'type': 2, 'position': 0, 'bitrate': 64000, 'user_limit': 0},
    )  # type: ignore[arg-type]
    calls = []

    async def send_soundboard_sound(channel_id, **payload):
        calls.append((channel_id, payload))

    state.http.send_soundboard_sound = send_soundboard_sound  # type: ignore[method-assign]
    other_guild = discord.Object(999)
    sound = discord.SoundboardSound(guild=other_guild, state=state, data=_sound(999))  # type: ignore[arg-type]
    await voice.send_sound(sound)
    default = discord.SoundboardDefaultSound(
        state=state, data={'sound_id': '3', 'volume': 1.0, 'name': 'quack', 'emoji_name': '🦆'}
    )
    await voice.send_sound(default)
    assert calls == [(700, {'sound_id': SOUND_ID, 'source_guild_id': 999}), (700, {'sound_id': 3})]


def test_audio_mime_detection():
    """MP3 (ID3 or any MPEG frame sync) and OGG; Rapptz's version only knew ID3 and FF FB."""
    assert _bytes_to_base64_data(b'ID3\x04rest', audio=True).startswith('data:audio/mpeg;base64,')
    assert _bytes_to_base64_data(b'\xff\xf3\x00', audio=True).startswith('data:audio/mpeg;')
    assert _bytes_to_base64_data(b'OggS\x00', audio=True).startswith('data:audio/ogg;')
    with pytest.raises(ValueError):
        _bytes_to_base64_data(b'RIFF....WAVE', audio=True)


def test_soundboard_audit_log_actions():
    """Actions 130-132 target the sound; automod's range no longer swallows them."""
    for value in (130, 131, 132):
        assert discord.AuditLogAction(value).target_type == 'soundboard_sound'
    assert discord.AuditLogAction.automod_rule_create.target_type == 'auto_moderation'
    assert discord.AuditLogAction.soundboard_sound_delete.category is discord.AuditLogActionCategory.delete


@pytest.mark.asyncio
async def test_soundboard_sound_converter():
    from types import SimpleNamespace

    from discord.ext import commands

    client, state, guild, _ = await make_env()
    state.parse_soundboard_sounds({'guild_id': str(guild.id), 'soundboard_sounds': [_sound(guild.id)]})  # type: ignore[arg-type]
    ctx = SimpleNamespace(guild=guild, bot=client)
    converter = commands.SoundboardSoundConverter()
    assert (await converter.convert(ctx, 'honk')).id == SOUND_ID  # type: ignore[arg-type]
    assert (await converter.convert(ctx, str(SOUND_ID))).name == 'honk'  # type: ignore[arg-type]
    with pytest.raises(commands.SoundboardSoundNotFound):
        await converter.convert(ctx, 'missing')  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_identical_sound_update_dispatches_nothing():
    """A GUILD_SOUNDBOARD_SOUND(S)_UPDATE that changes nothing dispatches no event."""
    client, state, guild, _ = await make_env()
    events = []
    state.dispatch = lambda event, *args: events.append(event)  # type: ignore[method-assign]
    state.parse_soundboard_sounds({'guild_id': str(guild.id), 'soundboard_sounds': [_sound(guild.id)]})  # type: ignore[arg-type]
    state.parse_guild_soundboard_sound_update(_sound(guild.id))  # type: ignore[arg-type]
    state.parse_guild_soundboard_sounds_update({'guild_id': str(guild.id), 'soundboard_sounds': [_sound(guild.id)]})  # type: ignore[arg-type]
    assert events == []
    state.parse_guild_soundboard_sound_update(_sound(guild.id, emoji_name='🐔'))  # type: ignore[arg-type]
    assert events == ['soundboard_sound_update']


@pytest.mark.asyncio
async def test_guild_payload_sounds_replace_the_cache():
    """Sounds in a guild payload are the full set, so they replace the cache."""
    client, state, guild, _ = await make_env()
    state.parse_soundboard_sounds({'guild_id': str(guild.id), 'soundboard_sounds': [_sound(guild.id)]})  # type: ignore[arg-type]
    payload = guild_payload(guild.id)
    payload['soundboard_sounds'] = [_sound(guild.id, sound_id=SOUND_ID + 5, name='new')]
    guild._from_data(payload)  # type: ignore[arg-type]
    assert [s.name for s in guild.soundboard_sounds] == ['new']


def test_sound_inequality_with_other_types():
    """__ne__ negated NotImplemented, which is deprecated and a TypeError on Python 3.14."""
    import warnings

    sound = discord.SoundboardDefaultSound(state=None, data={'sound_id': '3', 'volume': 1.0, 'name': 'q', 'emoji_name': 'x'})  # type: ignore[arg-type]
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        assert (sound != 1) is True and (sound != sound) is False


def test_sounds_are_hashable():
    """Defining __eq__ without __hash__ made every sound class unhashable."""
    data = {'sound_id': '3', 'volume': 1.0, 'name': 'q', 'emoji_name': 'x'}
    a = discord.SoundboardDefaultSound(state=None, data=data)  # type: ignore[arg-type]
    b = discord.SoundboardDefaultSound(state=None, data=data)  # type: ignore[arg-type]
    assert hash(a) == hash(b) and len({a, b}) == 1
    assert discord.SoundboardSound.__hash__ is not None
    assert discord.VoiceChannelSoundEffect.__hash__ is not None


@pytest.mark.asyncio
async def test_create_and_edit_sound_validate_input():
    """Unsupported emoji types raise TypeError instead of being ignored, and any
    bytes-like sound works (a memoryview failed MIME detection)."""
    client, state, guild, _ = await make_env()
    payloads = []

    async def create_soundboard_sound(guild_id, *, reason=None, **payload):
        payloads.append(payload)
        return _sound(guild_id)

    async def edit_soundboard_sound(guild_id, sound_id, *, reason=None, **payload):
        raise AssertionError('edit_soundboard_sound must not be called for an unsupported emoji')

    state.http.create_soundboard_sound = create_soundboard_sound  # type: ignore[method-assign]
    state.http.edit_soundboard_sound = edit_soundboard_sound  # type: ignore[method-assign]
    await guild.create_soundboard_sound(name='honk', sound=memoryview(b'ID3\x04data'), emoji='🦆')  # type: ignore[arg-type]
    assert payloads[-1]['sound'].startswith('data:audio/mpeg;base64,') and payloads[-1]['emoji_name'] == '🦆'
    with pytest.raises(TypeError):
        await guild.create_soundboard_sound(name='honk', sound=b'ID3', emoji=42)  # type: ignore[arg-type]
    assert len(payloads) == 1
    sound = discord.SoundboardSound(guild=guild, state=state, data=_sound(guild.id))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await sound.edit(emoji=42)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_request_soundboard_sounds_requires_a_timeout():
    """With no bound, a reconnect mid-request would leave the wait pending forever."""
    client = await _ready_client()
    with pytest.raises(ValueError):
        await client.request_soundboard_sounds([discord.Object(1)], timeout=None)  # type: ignore[arg-type]
    for bad in (0, -1.0, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            await client.request_soundboard_sounds([discord.Object(1)], timeout=bad)


def test_soundboard_audit_log_emoji_keeps_custom_emoji_id():
    """emoji_id and emoji_name arrive as separate changes; a custom emoji kept
    only a nameless PartialEmoji in AuditLogDiff.emoji."""
    from discord.audit_logs import AuditLogChanges

    class Entry:
        action = discord.AuditLogAction.soundboard_sound_update
        _state = None
        guild = None

    changes = AuditLogChanges(
        Entry(),  # type: ignore[arg-type]
        [
            {'key': 'emoji_id', 'old_value': None, 'new_value': '123456789012345678'},
            {'key': 'emoji_name', 'old_value': '\U0001f986', 'new_value': None},
        ],  # type: ignore[list-item]
    )
    assert changes.before.emoji.name == '\U0001f986' and changes.before.emoji.id is None
    assert changes.after.emoji.id == 123456789012345678

    # Only the changed key is sent: custom to custom, then custom to none
    changes = AuditLogChanges(Entry(), [{'key': 'emoji_id', 'old_value': '1', 'new_value': '2'}])  # type: ignore[arg-type,list-item]
    assert (changes.before.emoji.id, changes.after.emoji.id) == (1, 2)
    changes = AuditLogChanges(Entry(), [{'key': 'emoji_id', 'old_value': '5', 'new_value': None}])  # type: ignore[arg-type,list-item]
    assert changes.before.emoji.id == 5 and changes.after.emoji is None
    # A unicode emoji removed: only emoji_name changes, to null
    changes = AuditLogChanges(Entry(), [{'key': 'emoji_name', 'old_value': 'x', 'new_value': None}])  # type: ignore[arg-type,list-item]
    assert changes.before.emoji.name == 'x' and changes.after.emoji is None


@pytest.mark.asyncio
async def test_edit_sound_emoji_clears_the_other_field():
    """Switching between a custom and a unicode emoji must clear the old one."""
    client, state, guild, _ = await make_env()
    payloads = []

    async def edit_soundboard_sound(guild_id, sound_id, *, reason=None, **payload):
        payloads.append(payload)
        return _sound(guild_id)

    state.http.edit_soundboard_sound = edit_soundboard_sound  # type: ignore[method-assign]
    sound = discord.SoundboardSound(guild=guild, state=state, data=_sound(guild.id))  # type: ignore[arg-type]
    await sound.edit(emoji='\U0001f986')
    await sound.edit(emoji=discord.PartialEmoji(name='party', id=123))
    assert payloads == [{'emoji_id': None, 'emoji_name': '\U0001f986'}, {'emoji_id': 123, 'emoji_name': None}]
