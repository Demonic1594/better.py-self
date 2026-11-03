"""Crashes and broken features confirmed by the external audit (round 2)."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

import discord
import discord.client as client_module
from discord import Client
from discord.ext import commands
from discord.ext.commands.help import _add_help_line
from discord.webhook.async_ import _ClientSessionProxy

from test_persistence import drive_ready, guild_payload, ready_payload, up


async def _client():
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(state=state, data=up(5))
    return client, state


@pytest.mark.asyncio
async def test_first_connect_failure_retries_instead_of_crashing(monkeypatch):
    """A1-01: with no websocket yet (network down at startup), the retry path read
    self.ws.session_id and died with AttributeError instead of retrying."""
    client, state = await _client()
    calls = []

    async def from_client(cls, client, **params):
        calls.append(params)
        if len(calls) == 1:
            raise ConnectionRefusedError(111, 'refused')  # an OSError: reconnectable
        raise RuntimeError('stop')

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(client_module.DiscordWebSocket, 'from_client', classmethod(from_client))
    monkeypatch.setattr(client_module.asyncio, 'sleep', no_sleep)
    assert client.ws is None
    with pytest.raises(RuntimeError, match='stop'):
        await client.connect(reconnect=True)
    assert len(calls) == 2, 'the failure was retried'
    assert calls[1]['resume'] is False and calls[1]['session'] is None


@pytest.mark.asyncio
async def test_webhook_from_client_before_any_cdn_request(monkeypatch):
    """A2-01: the client's aiohttp session is created lazily, so webhooks built from
    a client before it existed got no session and could not be used."""
    client, state = await _client()
    webhook = discord.Webhook.from_url('https://discord.com/api/webhooks/123456789012345678/' + 'A' * 68, client=client)
    assert isinstance(webhook.session, _ClientSessionProxy)
    fetched = discord.Webhook.from_state({'id': '2', 'type': 1, 'token': 't'}, state)  # type: ignore[arg-type]
    assert isinstance(fetched.session, _ClientSessionProxy)

    requested = []

    class _CM:
        async def __aenter__(self):
            return 'response'

        async def __aexit__(self, *exc):
            return False

    class _Session:
        closed = False

        def request(self, *args, **kwargs):
            requested.append(args)
            return _CM()

    async def ensure():
        return _Session()

    monkeypatch.setattr(client.http, '_ensure_asession', ensure)
    async with webhook.session.request('POST', 'https://example') as response:
        assert response == 'response'
    assert requested == [('POST', 'https://example')], 'the real session is created on first use'


@pytest.mark.asyncio
async def test_unknown_private_channel_type_does_not_abort_ready(monkeypatch):
    """A4-08: an unknown private channel type made READY_SUPPLEMENTAL raise, which
    aborts the connection; fetch_private_channels raised the same way."""
    client, state = await _client()
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, 1)]
    ready['users'] = list(ready.get('users', [])) + [up(77)]
    ready['private_channels'] = [
        {'id': '900', 'type': 99, 'recipient_ids': ['77']},
        {'id': '901', 'type': 1, 'recipient_ids': ['77'], 'last_message_id': None},
    ]
    await drive_ready(client, ready)  # must not raise
    assert [c.id for c in client.private_channels] == [901]

    async def get_private_channels():
        return [
            {'id': '902', 'type': 99, 'recipients': [up(77)]},
            {'id': '903', 'type': 1, 'recipients': [up(77)], 'last_message_id': None},
        ]

    monkeypatch.setattr(state.http, 'get_private_channels', get_private_channels)
    assert [c.id for c in await client.fetch_private_channels()] == [903]


@pytest.mark.asyncio
async def test_edit_accepts_the_accent_colour_spelling(monkeypatch):
    """A5-02: accent_colour= read .value from the MISSING accent_color instead."""
    client, state = await _client()
    sent = []

    async def edit_current_user(payload):
        sent.append(payload)
        return dict(up(5))

    monkeypatch.setattr(state.http, 'edit_current_user', edit_current_user)
    await state.user.edit(accent_colour=discord.Colour(0x123456))
    assert sent[-1]['accent_color'] == 0x123456


def test_sku_is_released_compares_dates():
    """A7-01: release_date is a date and was compared against a datetime."""
    sku = discord.SKU.__new__(discord.SKU)
    sku.release_date = datetime.date(2000, 1, 1)
    assert sku.is_released() is True
    sku.release_date = datetime.date(2999, 1, 1)
    assert sku.is_released() is False
    sku.release_date = None
    assert sku.is_released() is False


def test_emoji_added_is_a_real_flag():
    """A8-01: the flag lacked @flag_value, so it could not be set and read as a method."""
    assert 'emoji_added' in discord.SystemChannelFlags.VALID_FLAGS
    flags = discord.SystemChannelFlags(emoji_added=False)
    assert flags.emoji_added is False
    assert discord.SystemChannelFlags._from_value(0).emoji_added is True
    assert discord.SystemChannelFlags._from_value(256).emoji_added is False, 'inverted like the others'


def test_private_user_flags_include_the_public_ones():
    """A8-05: PrivateUserFlags extends PublicUserFlags but did not accept its flags."""
    flags = discord.PrivateUserFlags(staff=True)
    assert flags.staff is True
    assert 'staff' in dict(flags)
    assert set(discord.PublicUserFlags.VALID_FLAGS) <= set(discord.PrivateUserFlags.VALID_FLAGS)


@pytest.mark.asyncio
async def test_vanity_invite_uses_the_full_invite(monkeypatch):
    """A3-03: the Invite was built from the vanity payload ({code, uses}) instead of
    the full invite, so its other fields were always None."""
    client, state = await _client()
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, 1)]
    await drive_ready(client, ready)
    guild = client.get_guild(1)

    async def get_vanity_code(guild_id):
        return {'code': 'vanity', 'uses': 5}

    async def get_invite(code, **kwargs):
        return {
            'code': code,
            'type': 0,
            'guild': {'id': '1', 'name': 'G1', 'features': []},
            'channel': {'id': '501', 'name': 'general', 'type': 0},
            'approximate_member_count': 42,
            'approximate_presence_count': 7,
        }

    monkeypatch.setattr(state.http, 'get_vanity_code', get_vanity_code)
    monkeypatch.setattr(state.http, 'get_invite', get_invite)
    invite = await guild.vanity_invite()
    assert invite is not None and invite.code == 'vanity'
    assert invite.uses == 5 and invite.max_age == 0 and invite.temporary is False
    assert invite.approximate_member_count == 42


def test_long_help_lines_are_split_instead_of_raising():
    """A6-01: a single help line longer than a page made !help raise RuntimeError."""
    paginator = commands.Paginator(max_size=200)
    _add_help_line(paginator, 'x' * 1000, empty=True)
    paginator.close_page()
    assert ''.join(paginator.pages).count('x') == 1000
    assert all(len(page) <= 200 for page in paginator.pages)

    @commands.command(help='y' * 5000)
    async def cmd(ctx):
        pass

    help_command = commands.DefaultHelpCommand()
    help_command.context = SimpleNamespace(clean_prefix='!', bot=None)  # type: ignore[assignment]
    help_command.add_command_formatting(cmd)  # must not raise
    help_command.paginator.close_page()
    assert ''.join(help_command.paginator.pages).count('y') == 5000
