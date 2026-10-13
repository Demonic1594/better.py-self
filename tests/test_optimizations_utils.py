"""
Regression tests for the optimization pass:
markdown/mention utilities, permission masks, flags, embeds, routes, components.
"""

from __future__ import annotations

import pytest

import discord.utils as u
from discord import Embed, Permissions, PermissionOverwrite, SystemChannelFlags
from discord.components import _component_factory, SelectDefaultValue
from discord.http import Route


def test_markdown_all_variants():
    assert u.remove_markdown('**hi** _yo_ ~x~', ignore_links=False) == 'hi yo x'
    assert u.remove_markdown('**hi** _yo_ ~x~') == 'hi yo x'
    assert u.remove_markdown('see http://x.com/a_b', ignore_links=False) == 'see http://x.com/ab'
    assert u.remove_markdown('see http://x.com/a_b') == 'see http://x.com/a_b'
    assert u.escape_markdown('**hi** _yo_', ignore_links=False) == '\\*\\*hi\\*\\* \\_yo\\_'
    assert u.escape_markdown('**hi**') == '\\*\\*hi\\*\\*'
    assert u.escape_markdown('**hi**', as_needed=True) == '\\*\\*hi**'


def test_escape_mentions():
    out = u.escape_mentions('@everyone @here <@123> <@!123456789012345678> <@&111111111111111111>')
    assert out.count('\u200b') == 4


def test_resolve_invite():
    ri = u.resolve_invite('https://discord.gg/abc123?event=456')
    assert ri.code == 'abc123' and ri.event == 456
    assert u.resolve_invite('abc123').code == 'abc123'
    with pytest.raises(ValueError):
        u.resolve_invite('bad invite!')


def test_valid_icon_size():
    assert u.valid_icon_size(16) and u.valid_icon_size(4096) and u.valid_icon_size(20)
    assert not u.valid_icon_size(15) and not u.valid_icon_size(8192)


def test_permission_masks_exact():
    p = Permissions.all()
    p.view_channel = False
    p.read_message_history = False
    assert Permissions._timeout_mask() == ~p.value

    base = Permissions.text()
    base.read_messages = True
    base.send_tts_messages = False
    base.manage_messages = False
    base.pin_messages = True
    base.create_private_threads = False
    base.create_public_threads = False
    base.manage_threads = False
    base.send_messages_in_threads = False
    assert Permissions._dm_permissions().value == base.value

    from discord.channel import _VOICE_DENIED_MASK, _VOICE_DENIED_EDIT_MASK

    assert _VOICE_DENIED_MASK == ~Permissions.voice().value
    assert _VOICE_DENIED_EDIT_MASK == ~(
        Permissions.voice().value | Permissions(manage_channels=True, manage_roles=True).value
    )


def test_permission_overwrite_descriptors():
    po = PermissionOverwrite(view_channel=True, manage_messages=False)
    assert po.view_channel is True and po.manage_messages is False
    po.send_messages = True
    assert po.send_messages is True and po.pair()[0].send_messages
    # Store under the alias key (upstream behavior)
    assert dict(iter(po)).get('read_messages') is True


def test_system_channel_flags_inverted_semantics():
    f = SystemChannelFlags._from_value(0)
    assert f.join_notifications is True  # unset bit = enabled
    f2 = SystemChannelFlags(join_notifications=False)
    assert f2.join_notifications is False and f2.value != 0
    assert dict(iter(f2))['join_notifications'] is False
    f3 = SystemChannelFlags._from_value(0)
    f3.join_notifications = False
    assert f3.join_notifications is False


def test_flag_iteration_and_operators():
    from discord import ApplicationFlags, MemberCacheFlags, MessageFlags

    af = ApplicationFlags(embedded=True, gateway_presence=True)
    d = dict(af)
    assert d['embedded'] is True and d['gateway_presence'] is True
    assert (~ApplicationFlags()).value == ApplicationFlags._INVERT_MASK
    m = MessageFlags._from_value(0)
    assert not m.ephemeral and not bool(m)
    assert MemberCacheFlags.all().joined and MemberCacheFlags.all().voice
    f = ApplicationFlags(embedded=True)
    g = ApplicationFlags(embedded=True, gateway_presence=True)
    assert (f | g).value == g.value and (f & g).value == f.value and (f ^ g).value == g.value & ~f.value
    with pytest.raises(TypeError):
        MessageFlags(bogus_flag=True)


def test_embed_copy_and_to_dict():
    e = Embed(title='t', description='d', colour=1, url='http://x')
    e.add_field(name='a', value='b')
    e.add_field(name='c', value='d', inline=False)
    e.set_footer(text='f', icon_url='http://f')
    e.set_author(name='au')
    c = e.copy()
    assert c == e and c.to_dict() == e.to_dict()
    c._fields[0]['name'] = 'MUT'
    assert e._fields[0]['name'] == 'a'
    c._footer['text'] = 'MUT'
    assert e._footer['text'] == 'f'
    assert len(e) == len(e.title) + len(e.description) + 4 + len('f') + len('au')
    assert bool(e) and not bool(Embed())


def test_route_precomputed_keys():
    r = Route('GET', '/channels/{channel_id}/messages/{message_id}', channel_id=1, message_id=2)
    assert r.key == 'GET /channels/{channel_id}/messages/{message_id}'
    assert r.major_parameters == '1'
    assert r.url.endswith('/channels/1/messages/2')
    m = Route('GET', '/channels/{channel_id}/messages', channel_id=1, metadata='sub')
    assert m.key == 'GET /channels/{channel_id}/messages:sub'
    assert Route('GET', '/users/@me').major_parameters == ''


COMPONENT_PAYLOADS = {
    1: {'type': 1, 'components': []},
    2: {'type': 2, 'style': 1, 'label': 'x'},
    4: {'type': 4, 'style': 1, 'label': 'x', 'custom_id': 'c'},
    3: {'type': 3, 'options': [{'label': 'x', 'value': 'v'}], 'custom_id': 'c'},
    5: {'type': 5, 'custom_id': 'c'},
    6: {'type': 6, 'custom_id': 'c'},
    7: {'type': 7, 'custom_id': 'c'},
    8: {'type': 8, 'custom_id': 'c'},
    9: {'type': 9, 'components': [{'type': 10, 'content': 'hi'}], 'accessory': {'type': 2, 'style': 2, 'label': 'x'}},
    10: {'type': 10, 'content': 'hi'},
    11: {'type': 11, 'media': {'url': 'attachment://x'}},
    12: {'type': 12, 'items': []},
    13: {'type': 13, 'file': {'url': 'attachment://x'}},
    14: {'type': 14},
    17: {'type': 17, 'components': []},
    18: {'type': 18, 'label': 'x', 'component': {'type': 10, 'content': 'hi'}},
    19: {'type': 19, 'custom_id': 'c'},
    21: {'type': 21, 'options': [{'label': 'x', 'value': 'v'}], 'custom_id': 'c'},
    22: {'type': 22, 'options': [{'label': 'x', 'value': 'v'}], 'custom_id': 'c'},
    23: {'type': 23, 'label': 'x', 'style': 1, 'custom_id': 'c'},
}


def test_component_factory_all_types():
    for ctype, payload in COMPONENT_PAYLOADS.items():
        comp = _component_factory(payload)
        assert comp is not None, ctype
        # Roundtrip: to_dict -> factory -> to_dict is stable
        assert _component_factory(comp.to_dict()).to_dict() == comp.to_dict()
    assert _component_factory({'type': 99}) is None


def test_select_default_value_slots():
    sdv = SelectDefaultValue.from_dict({'id': '5', 'type': 0})
    assert sdv.id == 5 and not hasattr(sdv, '__dict__')
    assert sdv.type.value == 0
    assert sdv.to_dict() == {'id': 5, 'type': 0}
