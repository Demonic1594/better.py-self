"""Audit round 2, fix round 1: regressions for the P1 findings."""

from __future__ import annotations

import pytest

import discord
from discord.utils import MISSING

from test_persistence import drive_ready, guild_payload, ready_payload
from test_refactor_fixes import _blank_user_settings
from test_rapptz_ports2 import _presence, _ready_client


@pytest.mark.asyncio
async def test_own_presence_update_does_not_raise(monkeypatch):
    """R2-A1-01: parse_presence_update calls presence._update(data, state, user_id),
    but FakeClientPresence._update only took (data, state): every PRESENCE_UPDATE
    for our own account raised TypeError."""
    client = await _ready_client()
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, n_members=2)]
    await drive_ready(client, ready)
    state = client._connection
    me = state.self_id
    assert state.get_presence(me) is state.client_presence

    guild = client.get_guild(1)
    if guild.get_member(me) is None:  # type: ignore[union-attr]
        member = discord.Member(
            data={'user': {'id': str(me), 'username': 'me', 'discriminator': '0'}, 'roles': []}, guild=guild, state=state
        )  # type: ignore[arg-type]
        guild._add_member(member)  # type: ignore[union-attr]
    calls = []
    fake_update = type(state.client_presence)._update

    def spy(self, data, state_, user_id=0, /):
        calls.append(user_id)
        return fake_update(self, data, state_, user_id)

    monkeypatch.setattr(type(state.client_presence), '_update', spy)
    state.parse_presence_update(_presence(me, 1))  # type: ignore[arg-type]  # raised TypeError
    no_guild = _presence(me, 1)
    del no_guild['guild_id']
    state.parse_presence_update(no_guild)  # type: ignore[arg-type]
    # Both updates reached our own presence, with our id, and it stayed the client presence
    assert calls == [me, me]
    assert state.get_presence(me) is state.client_presence
    await client.close()


def _decoded(settings) -> dict:
    return settings.to_dict()


@pytest.mark.asyncio
async def test_restricted_guilds_does_not_clobber_the_guilds_section():
    """R2-A7-13: the privacy loop reused the name ``guilds``, which is also the
    guilds subsection read back through locals(): a list crashed with TypeError,
    and MISSING silently dropped guild_progress_settings."""
    from discord.settings import GuildProgress

    settings = _blank_user_settings()
    progress = GuildProgress(
        1234, hub_progress=discord.HubProgressFlags(), onboarding_progress=discord.OnboardingProgressFlags()
    )
    guild = discord.Object(id=1234)

    new = await settings.edit(restricted_guilds=[guild], guild_progress_settings=[progress])  # raised TypeError
    data = _decoded(new)
    assert data['privacy']['restricted_guild_ids'] == ['1234']
    assert list(data['guilds']['guilds']) == ['1234']

    new = await settings.edit(restricted_guilds=MISSING, guild_progress_settings=[progress])
    data = _decoded(new)
    assert 'guilds' in data and data['guilds']['guilds'], 'MISSING restricted_guilds dropped the guilds section'


@pytest.mark.asyncio
async def test_guild_progress_settings_missing_resets(monkeypatch):
    """MISSING is documented to reset a field; guild_progress_settings ignored it."""
    from discord.settings import GuildProgress

    settings = _blank_user_settings()
    progress = GuildProgress(
        1234, hub_progress=discord.HubProgressFlags(), onboarding_progress=discord.OnboardingProgressFlags()
    )
    with_progress = await settings.edit(guild_progress_settings=[progress])
    assert _decoded(with_progress).get('guilds', {}).get('guilds')

    sent = []
    encode = type(with_progress).dict_to_base64

    def capture(self, payload):
        sent.append(payload)
        return encode(self, payload)

    monkeypatch.setattr(type(with_progress), 'dict_to_base64', capture)
    await with_progress.edit(guild_progress_settings=MISSING)
    # The guilds section must be sent, with the field removed, so the server resets it;
    # it used to be left out entirely, which keeps the old value server-side
    assert 'guilds' in sent[-1] and 'guilds' not in sent[-1]['guilds']


def _audit_log_http(entry_ids, calls):
    """A get_audit_logs stand-in that returns newest first, as Discord does."""

    async def get_audit_logs(guild_id, limit=100, before=None, after=None, user_id=None, action_type=None):
        calls.append({'before': before, 'after': after, 'limit': limit})
        ids = sorted(entry_ids, reverse=True)
        if before is not None:
            ids = [i for i in ids if i < before]
        if after is not None:
            # Either possible semantics would do here; the fixed code never sends after
            ids = [i for i in ids if i > after]
        page = ids[:limit]
        entries = [{'id': str(i), 'action_type': 1, 'user_id': None, 'target_id': None, 'changes': []} for i in page]
        return {'audit_log_entries': entries, 'users': []}

    return get_audit_logs


@pytest.mark.asyncio
async def test_audit_logs_oldest_first_yields_each_entry_once_in_order():
    """A3-01: oldest_first paged up with after=entries[-1], which re-fetched
    the same entries page after page. It now walks down with before and
    yields the buffered entries in reverse."""
    client = await _ready_client()
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, n_members=2)]
    await drive_ready(client, ready)
    guild = client.get_guild(1)
    assert guild is not None
    ids = list(range(1000, 1250))  # 250 entries: three pages
    calls = []
    client.http.get_audit_logs = _audit_log_http(ids, calls)  # type: ignore[method-assign]

    got = [e.id async for e in guild.audit_logs(limit=None, oldest_first=True)]
    assert got == ids, 'every entry exactly once, oldest first'
    assert all(c['after'] is None for c in calls) and len(calls) == 3

    got = [e.id async for e in guild.audit_logs(limit=10, oldest_first=True, after=discord.Object(id=1100))]
    assert got == list(range(1101, 1111))

    got = [
        e.id
        async for e in guild.audit_logs(
            limit=None, oldest_first=True, after=discord.Object(id=1100), before=discord.Object(id=1105)
        )
    ]
    assert got == [1101, 1102, 1103, 1104]

    # The default newest-first path is unchanged
    got = [e.id async for e in guild.audit_logs(limit=5)]
    assert got == [1249, 1248, 1247, 1246, 1245]

    await client.close()


@pytest.mark.asyncio
async def test_guild_update_without_optional_keys_keeps_cached_values():
    """A3-02: Guild._from_data also runs for GUILD_UPDATE and read joined_at,
    emojis, premium_subscription_count and max_presences with .get(), so an
    update that left them out wiped them."""
    client = await _ready_client()
    ready = ready_payload()
    data = guild_payload(1, n_members=2)
    data.update(
        {
            'joined_at': '2024-01-02T03:04:05+00:00',
            'emojis': [{'id': '55', 'name': 'blob', 'animated': False, 'available': True}],
            'premium_subscription_count': 7,
            'max_presences': 25000,
        }
    )
    ready['guilds'] = [data]
    await drive_ready(client, ready)
    guild = client.get_guild(1)
    assert guild is not None
    joined_at = guild.joined_at
    assert joined_at is not None and [e.id for e in guild.emojis] == [55]

    client._connection.parse_guild_update({'id': '1', 'name': 'renamed'})  # type: ignore[arg-type]
    assert guild.name == 'renamed'
    assert guild.joined_at == joined_at
    assert [e.id for e in guild.emojis] == [55]
    assert guild.premium_subscription_count == 7 and guild.max_presences == 25000

    # Keys that are present still apply, including empty and null values
    client._connection.parse_guild_update(
        {'id': '1', 'name': 'renamed', 'emojis': [], 'premium_subscription_count': 0, 'max_presences': None}  # type: ignore[arg-type]
    )
    assert guild.emojis == () and guild.premium_subscription_count == 0 and guild.max_presences is None
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'frame', ['{"broken": ', 'not json', '[]', '"a string"', 'null', b'\xff' * 64 + b'\x00\x00\xff\xff']
)
async def test_undecodable_frame_resumes_instead_of_crashing(frame, caplog):
    """A frame that failed to decode raised ValueError (or AttributeError for a
    non-object) out of poll_event, which Client.connect doesn't handle, so the
    client stopped. It now closes the socket with 4000 and asks for a RESUME."""
    import asyncio
    import logging

    from discord.gateway import ReconnectWebSocket
    from test_abuse2 import make_ws

    ws = make_ws(asyncio.get_running_loop())
    with caplog.at_level(logging.WARNING, logger='discord.gateway'):
        with pytest.raises(ReconnectWebSocket) as info:
            await ws.received_message(frame)
    assert info.value.resume is True
    assert ws.socket.closed
    assert any('could not be decoded' in r.getMessage() for r in caplog.records)
