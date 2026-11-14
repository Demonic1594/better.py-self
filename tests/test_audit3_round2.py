"""Audit round 3, fix round 1 (continued): the smaller fixes, one regression each."""

from __future__ import annotations

import asyncio
import io
import logging
import types

import pytest

import discord

from test_persistence import ready_payload, supplemental_for, up
from test_user_paths import _client

GUILD_ID = 1


# --- Members and roles --------------------------------------------------------


class _RecordingHTTP:
    """Records the calls Member.edit / Role.edit make and answers with nothing."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return method


@pytest.mark.asyncio
async def test_own_nickname_is_sent_once():
    """It went to both "modify current member" and "modify member"; the second
    raised Forbidden without manage_nicknames, after the nickname had changed."""
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    me = discord.Member(data={'user': up(5), 'roles': []}, guild=guild, state=state)
    state.http = http = _RecordingHTTP()  # type: ignore
    await me.edit(nick='x')
    assert [name for name, _, _ in http.calls] == ['edit_me']
    assert http.calls[0][2] == {'nick': 'x'}

    # Somebody else's nickname still goes through "modify member"
    http.calls.clear()
    await guild.get_member(100).edit(nick='y')
    assert [name for name, _, _ in http.calls] == ['edit_member']
    assert http.calls[0][2]['nick'] == 'y'
    await client.close()


@pytest.mark.asyncio
async def test_self_only_fields_on_another_member_are_reported(caplog):
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    state.http = http = _RecordingHTTP()  # type: ignore
    with caplog.at_level(logging.WARNING, logger='discord.member'):
        await guild.get_member(100).edit(bio='hello', pronouns='they')
    assert http.calls == [], 'nothing is sent, as before'
    assert 'bio' in caplog.text and 'pronouns' in caplog.text and '100' in caplog.text
    await client.close()


@pytest.mark.asyncio
async def test_role_colours_accept_none_and_zero():
    """``secondary_colour or secondary_color`` dropped None (clear) and 0 (black)."""
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    role = guild.default_role
    sent = {}

    async def edit_role(guild_id, role_id, *, reason=None, **fields):
        sent.clear()
        sent.update(fields)
        return {'id': str(role_id), 'name': 'r'}

    async def create_role(guild_id, *, reason=None, **fields):
        sent.clear()
        sent.update(fields)
        return {'id': '77', 'name': 'r'}

    state.http.edit_role = edit_role  # type: ignore
    state.http.create_role = create_role  # type: ignore

    await role.edit(secondary_colour=None, tertiary_colour=0)
    assert sent['colors'] == {'secondary_color': None, 'tertiary_color': 0}
    await role.edit(secondary_color=5)  # the American spelling still works
    assert sent['colors'] == {'secondary_color': 5}

    await guild.create_role(name='r', secondary_colour=0)
    assert sent['colors']['secondary_color'] == 0
    await client.close()


# --- Client and state ---------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_settings_flags_and_empty_lists():
    """``friend_source_flags.to_dict()`` does not exist, and empty values (how
    these settings are cleared) were dropped by truthiness checks."""
    client, state = await _client()
    sent = {}

    async def edit_settings(**payload):
        sent.update(payload)
        raise RuntimeError('stop')

    state.http.edit_settings = edit_settings  # type: ignore
    with pytest.raises(RuntimeError, match='stop'):
        await client.edit_legacy_settings(
            friend_source_flags=discord.FriendSourceFlags.none(),
            friend_discovery_flags=discord.FriendDiscoveryFlags.none(),
            restricted_guilds=[],
        )
    assert sent['friend_source_flags'] == {'mutual_friends': False, 'mutual_guilds': False, 'all': False}
    assert sent['friend_discovery_flags'] == 0
    assert sent['restricted_guilds'] == []
    await client.close()


@pytest.mark.asyncio
async def test_ready_supplemental_skips_a_relationship_with_an_unknown_user(caplog):
    client, state = await _client()
    ready = ready_payload()
    supplemental = supplemental_for(ready)
    state.parsers['READY'](ready)
    with caplog.at_level(logging.DEBUG, logger='discord.state'):
        state._ready_data['relationships'] = [
            {'id': '200', 'type': 1, 'user_id': '100'},
            {'id': '999', 'type': 1, 'user_id': '424242'},  # not among the users
        ]
        state.parsers['READY_SUPPLEMENTAL'](supplemental)  # raised KeyError
    assert 200 in state._relationships and 999 not in state._relationships
    assert 'unknown user' in caplog.text
    if state._ready_task is not None:
        state._ready_task.cancel()
    await asyncio.sleep(0)
    await client.close()


@pytest.mark.asyncio
async def test_cancelled_ready_task_keeps_its_replacement():
    """The ``finally`` of a cancelled _delay_ready cleared ``_ready_task`` even
    when a new READY had already stored the next task there."""
    client, state = await _client()
    entered = asyncio.Event()

    async def chunker(*args, **kwargs):
        entered.set()
        await asyncio.sleep(3600)

    state.chunker = chunker
    state._ready_data = {'guilds': []}
    first = asyncio.ensure_future(state._delay_ready())
    state._ready_task = first
    await asyncio.wait_for(entered.wait(), timeout=2)
    replacement = asyncio.ensure_future(asyncio.sleep(3600))
    state._ready_task = replacement  # what the next READY does
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert state._ready_task is replacement
    replacement.cancel()
    await client.close()


@pytest.mark.asyncio
async def test_desktop_properties_without_super_properties_are_invalid_data(monkeypatch):
    async def fetch_api_properties(cls, session, *args, **kwargs):
        return {'metadata': {'native_chrome_version': '130.0.0.0'}}  # no "properties"

    monkeypatch.setattr(discord.HeadersContext, 'fetch_api_properties', classmethod(fetch_api_properties))
    with pytest.raises(discord.InvalidData):  # was KeyError
        await discord.HeadersContext.desktop(None)  # type: ignore


# --- Models -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('saver', ['attachment', 'asset'])
async def test_save_writes_to_an_unbuffered_stream(tmp_path, saver):
    """``open(path, 'wb', buffering=0)`` is a RawIOBase, not a BufferedIOBase, so
    it was handed to open() as if it were a path."""
    base = discord.Attachment if saver == 'attachment' else discord.Asset

    class Target(base):  # type: ignore
        __slots__ = ()

        async def read(self, *args, **kwargs):
            return b'payload'

    target = Target.__new__(Target)
    path = tmp_path / 'out.bin'
    with open(path, 'wb', buffering=0) as fp:
        assert isinstance(fp, io.RawIOBase)
        assert await target.save(fp) == 7
    assert path.read_bytes() == b'payload'


def _audit_entry(state, guild, data):
    from discord.audit_logs import AuditLogEntry

    return AuditLogEntry(
        users={}, integrations={}, application_commands={}, automod_rules={}, webhooks={}, data=data, guild=guild
    )


@pytest.mark.asyncio
async def test_audit_log_entries_with_partial_data():
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)

    # invite_update lists only the changed fields
    entry = _audit_entry(
        state,
        guild,
        {
            'id': '1',
            'user_id': '100',
            'target_id': None,
            'action_type': discord.AuditLogAction.invite_update.value,
            'changes': [{'key': 'max_uses', 'old_value': 0, 'new_value': 5}],
        },
    )
    invite = entry.target  # raised AttributeError
    assert invite.max_uses == 5 and invite.max_age == 0

    # An AutoMod entry without the rule name or trigger type
    entry = _audit_entry(
        state,
        guild,
        {
            'id': '2',
            'user_id': '100',
            'target_id': '100',
            'action_type': discord.AuditLogAction.automod_block_message.value,
            'options': {'channel_id': ''},
        },
    )
    assert entry.extra.automod_rule_name is None

    # Role colours without a tertiary colour
    entry = _audit_entry(
        state,
        guild,
        {
            'id': '3',
            'user_id': '100',
            'target_id': str(GUILD_ID),
            'action_type': discord.AuditLogAction.role_update.value,
            'changes': [{'key': 'colors', 'old_value': {'primary_color': 1}, 'new_value': {'primary_color': 2}}],
        },
    )
    assert entry.after.colour.value == 2 and entry.after.tertiary_colour is None
    await client.close()


@pytest.mark.asyncio
async def test_connection_integration_without_a_guild():
    """``Integration`` read the state from its guild, which a connection's
    integration does not always have."""
    from discord.connections import Connection

    client, state = await _client()
    data = {
        'id': '1',
        'name': 'n',
        'type': 'twitch',
        'verified': True,
        'integrations': [{'id': '9', 'type': 'twitch', 'account': {'id': '1', 'name': 'acct'}}],
    }
    connection = Connection(data=data, state=state)  # type: ignore # raised AttributeError
    (integration,) = connection.integrations
    assert integration._state is state and integration.guild is None
    await client.close()


@pytest.mark.asyncio
async def test_application_bot_is_an_application_bot():
    """The parent class stores a plain User first; the subclass then kept it
    instead of building the ApplicationBot it documents."""
    from discord.application import Application, ApplicationBot

    client, state = await _client()
    data = {
        'id': '1',
        'name': 'app',
        'description': '',
        'summary': '',
        'icon': None,
        'verify_key': 'k',
        'flags': 0,
        'verification_state': None,
        'bot_public': True,
        'owner': up(5),
        'bot': up(300) | {'bot': True},
    }
    app = Application(state=state, data=data)
    assert isinstance(app.bot, ApplicationBot)
    first = app.bot
    app._update(data)
    assert isinstance(app.bot, ApplicationBot) and app.bot.id == first.id
    await client.close()


# --- Audio and voice ----------------------------------------------------------


def test_opus_encoder_pads_a_short_buffer():
    from discord import opus

    try:
        opus._load_default()
    except Exception:
        pass
    if not opus.is_loaded():
        pytest.skip('libopus not available')
    frame = opus.Encoder.SAMPLES_PER_FRAME
    padded = b'\x01\x02' * 5 + b'\x00' * (opus.Encoder.FRAME_SIZE - 10)
    # 10 bytes used to be handed to libopus as a full frame, which read past the
    # end of the buffer. They now encode like the same bytes followed by silence
    assert opus.Encoder().encode(b'\x01\x02' * 5, frame) == opus.Encoder().encode(padded, frame)


@pytest.mark.asyncio
async def test_voice_ip_discovery_times_out(monkeypatch):
    """The reply is one UDP datagram; if it was lost the await never finished."""
    from discord.gateway import DiscordVoiceWebSocket

    ws = DiscordVoiceWebSocket.__new__(DiscordVoiceWebSocket)
    loop = asyncio.get_running_loop()
    listeners = []
    ws._connection = types.SimpleNamespace(
        ssrc=1,
        socket=object(),
        add_socket_listener=listeners.append,
        remove_socket_listener=listeners.remove,
    )

    class Loop:
        def create_future(self):
            return loop.create_future()

        async def sock_sendall(self, sock, data):
            return None

        def call_soon_threadsafe(self, *args):
            return loop.call_soon_threadsafe(*args)

    ws.loop = Loop()  # type: ignore
    real_wait_for = asyncio.wait_for
    seen = []

    async def wait_for(awaitable, timeout):
        seen.append(timeout)
        return await real_wait_for(awaitable, 0.01)

    monkeypatch.setattr(asyncio, 'wait_for', wait_for)
    with pytest.raises(asyncio.TimeoutError):
        # The outer bound only matters on the old code, which waited forever
        await real_wait_for(ws.discover_ip(), 2)
    assert seen == [10.0]
    await asyncio.sleep(0)
    assert listeners == [], 'the socket listener is removed again'
