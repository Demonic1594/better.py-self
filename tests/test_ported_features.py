"""Features ported from open upstream PRs (dolfies/discord.py-self #896, #934)."""

from __future__ import annotations

import base64
import warnings

import pytest
from discord_protos import FrecencyUserSettings

import discord
from discord import Client

from test_persistence import drive_ready, guild_payload, ready_payload, up


def _frecency_b64(**fields) -> str:
    proto = FrecencyUserSettings()
    proto.favorite_emojis.emojis.extend(fields.get('emojis', []))
    proto.favorite_stickers.sticker_ids.extend(fields.get('stickers', []))
    proto.versions.data_version = fields.get('version', 1)
    return base64.b64encode(proto.SerializeToString()).decode()


async def _client():
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(state=state, data=up(5))
    return client, state


@pytest.mark.asyncio
async def test_frecency_settings_parse_and_fetch(monkeypatch):
    client, state = await _client()
    assert client.frecency_settings is None, 'not fetched yet'

    async def get_proto_settings(type):
        assert type == 2
        return {'settings': _frecency_b64(emojis=['smile', 'wave'], stickers=[11, 12])}

    monkeypatch.setattr(state.http, 'get_proto_settings', get_proto_settings)
    settings = await client.fetch_frecency_settings()
    assert client.frecency_settings is settings
    assert settings.favorite_emojis == ['smile', 'wave']
    assert settings.favorite_sticker_ids == [11, 12]


@pytest.mark.asyncio
async def test_frecency_settings_gateway_update_dispatches():
    client, state = await _client()
    seen = []
    state.dispatch = lambda event, *args: seen.append(args) if event == 'frecency_settings_update' else None
    data = {'settings': {'type': 2, 'proto': _frecency_b64(emojis=['a'])}, 'partial': False}
    state.parse_user_settings_proto_update(data)
    assert seen and seen[0][0] is None and seen[0][1].favorite_emojis == ['a']
    data = {'settings': {'type': 2, 'proto': _frecency_b64(emojis=['b'], version=2)}, 'partial': False}
    state.parse_user_settings_proto_update(data)
    old, new = seen[-1]
    assert old.favorite_emojis == ['a'] and new.favorite_emojis == ['b']
    assert new is state.frecency_settings


@pytest.mark.asyncio
async def test_frecency_edit_sends_only_the_edited_favorites(monkeypatch):
    client, state = await _client()
    settings = discord.FrecencySettings(state, _frecency_b64(emojis=['keep'], stickers=[1], version=7))
    sent = {}

    async def edit_proto_settings(type, payload, required_data_version=None):
        sent.update(type=type, payload=payload, version=required_data_version)
        return {'settings': _frecency_b64(emojis=['keep'], stickers=[2, 3], version=8)}

    monkeypatch.setattr(state.http, 'edit_proto_settings', edit_proto_settings)
    new = await settings.edit(favorite_sticker_ids=[2, 3])
    assert sent['type'] == 2 and sent['version'] == 7, 'edits are guarded by the current data version'
    # Only the edited sub-setting is sent; Discord merges it into the rest
    sent_proto = FrecencyUserSettings.FromString(base64.b64decode(sent['payload']))
    assert list(sent_proto.favorite_stickers.sticker_ids) == [2, 3]
    assert not sent_proto.HasField('favorite_emojis'), 'unedited favorites are not overwritten'
    assert new.favorite_sticker_ids == [2, 3] and new.favorite_emojis == ['keep']


@pytest.mark.asyncio
async def test_member_display_name_style_and_collectibles_change_detection():
    client, state = await _client()
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, 1)]
    await drive_ready(client, ready)
    guild = client.get_guild(1)
    member = guild.get_member(100)
    base = {'user': up(100), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}
    assert member._update(dict(base)) is None, 'a no-op update stays a no-op'

    styled = dict(base, display_name_styles={'font_id': 1, 'effect_id': 2, 'colors': [0xFF0000]})
    old = member._update(styled)
    assert old is not None, 'a display name style change is a member update'
    assert old._display_name_style is None and member._display_name_style == styled['display_name_styles']
    assert member._update(dict(styled)) is None


@pytest.mark.asyncio
async def test_client_user_edit_keeps_deprecated_avatar_decoration(monkeypatch):
    """#934 dropped avatar_decoration; we keep it working (deprecated) since
    removing a public parameter would break existing callers."""
    client, state = await _client()
    calls = []

    async def edit_current_user(payload):
        calls.append(payload)
        return dict(up(5))

    monkeypatch.setattr(state.http, 'edit_current_user', edit_current_user)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        await state.user.edit(avatar_decoration=None)
    assert calls[-1].get('avatar_decoration', 'missing') is None
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)

    await state.user.edit(avatar_decoration_sku_id=42, pronouns='they/them')
    assert calls[-1]['avatar_decoration_sku_id'] == 42
    assert calls[-1]['pronouns'] == 'they/them'
