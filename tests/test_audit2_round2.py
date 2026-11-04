"""Audit round 2, fix round 2: regressions for the P2 findings."""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time

import pytest

import discord
from discord import utils
from discord.client import _loop

from test_abuse3 import make_env, up


@pytest.mark.asyncio
async def test_clear_after_close_lets_the_client_start_again():
    """R2-A1-02: close() sets loop to MISSING and clear() left it there, so the
    next login() skipped the setup hook and dispatch silently did nothing."""
    client = discord.Client()
    await client._async_setup_hook()
    await client.close()
    assert client.loop is utils.MISSING
    await client.cache_stats()  # crashed on MISSING.time()

    client.clear()
    assert client.loop is _loop, 'login() runs the setup hook only for the sentinel'
    await client._async_setup_hook()  # what login() now does
    seen = []

    @client.event
    async def on_probe():
        seen.append(1)

    client.dispatch('probe')
    await asyncio.sleep(0)
    assert seen == [1]
    await client.close()


@pytest.mark.asyncio
async def test_cache_stats_after_close_with_a_pending_handler():
    """The backlog age also read loop.time() and crashed on MISSING when a
    dispatched handler was still pending at close()."""
    client = discord.Client()
    await client._async_setup_hook()
    release = asyncio.Event()

    @client.event
    async def on_slow():
        await release.wait()

    client.dispatch('slow')
    await asyncio.sleep(0)
    assert client._dispatch_pending, 'the handler is still running'
    await client.close()
    stats = await client.cache_stats()  # raised AttributeError on MISSING.time()
    assert stats is not None
    release.set()
    await asyncio.sleep(0)


def _member_item(user_id):
    return {'member': {'user': up(user_id), 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}}


@pytest.mark.asyncio
async def test_member_list_sync_honours_member_cache_flags():
    """R2-A3-03: a member-list SYNC with no scrape request cached every member
    even with member_cache_flags.joined off, unlike every other member path."""
    client, state, guild, channel = await make_env()
    state.user = discord.ClientUser(state=state, data=up(5))
    state.member_cache_flags = discord.MemberCacheFlags.none()
    before = set(guild._members)
    payload = {
        'guild_id': '1',
        'member_count': 3,
        'online_count': 3,
        'groups': [{'id': 'online', 'count': 3}],
        'ops': [{'op': 'SYNC', 'range': [0, 99], 'items': [_member_item(5), _member_item(70_001), _member_item(70_002)]}],
    }
    state.parse_guild_member_list_update(payload)  # type: ignore[arg-type]
    added = set(guild._members) - before
    assert 70_001 not in added and 70_002 not in added
    assert guild.get_member(5) is not None, 'our own member is still cached'

    state.member_cache_flags = discord.MemberCacheFlags.all()
    state.parse_guild_member_list_update(payload)  # type: ignore[arg-type]
    assert guild.get_member(70_001) is not None and guild.get_member(70_002) is not None


@pytest.mark.asyncio
async def test_unknown_user_settings_type_is_logged_not_raised(caplog):
    """R2-A7-14: UserSettingsType(unknown) raised ValueError before the
    'unknown type' warning could run."""
    client, state, guild, channel = await make_env()
    with caplog.at_level(logging.WARNING, logger='discord.state'):
        state.parse_user_settings_proto_update({'settings': {'type': 99, 'proto': ''}})  # type: ignore[arg-type]
    assert any('unknown type' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_message_snapshot_without_edited_timestamp():
    """R2-A5-01: a forwarded message's snapshot may leave out edited_timestamp."""
    from discord.message import MessageReference, MessageSnapshot

    client, state, guild, channel = await make_env()
    reference = MessageReference(message_id=123, channel_id=channel.id, guild_id=guild.id)
    snapshot = {
        'type': 0,
        'content': 'forwarded',
        'embeds': [],
        'attachments': [],
        'timestamp': '2024-01-01T00:00:00+00:00',
    }
    (parsed,) = MessageSnapshot._from_value(state, [{'message': snapshot}], reference)  # type: ignore[arg-type]
    assert parsed.content == 'forwarded' and parsed.edited_at is None


def test_components_with_an_unknown_child_round_trip():
    """R2-A5-02: an accessory or label child of an unknown type became None,
    and to_dict() then raised AttributeError. It is now sent back unchanged."""
    from discord.components import LabelComponent, SectionComponent

    unknown = {'type': 999, 'future_field': True}
    section = SectionComponent({'type': 9, 'components': [{'type': 10, 'content': 'hi'}], 'accessory': unknown})  # type: ignore[arg-type]
    assert section.accessory is None
    assert section.to_dict()['accessory'] == unknown

    label = LabelComponent({'type': 18, 'label': 'L', 'component': unknown})  # type: ignore[arg-type]
    assert label.to_dict()['component'] == unknown
    assert label.to_submit_dict()['component'] == unknown

    known = SectionComponent(
        {'type': 9, 'components': [], 'accessory': {'type': 11, 'media': {'url': 'https://x/y.png'}}}  # type: ignore[arg-type]
    )
    assert known.to_dict()['accessory']['type'] == 11


@pytest.mark.asyncio
async def test_zero_interval_loop_warns(caplog):
    """R2-A6-01: @tasks.loop() runs back to back. It stays valid, as upstream,
    but now says so in the log; with a count it is clearly intentional."""
    from discord.ext import tasks

    runs = []

    @tasks.loop()
    async def spin():
        runs.append(1)
        if len(runs) >= 3:
            spin.stop()

    with caplog.at_level(logging.WARNING, logger='discord.ext.tasks'):
        await spin.start()
        runs.clear()
        await spin.start()  # a restart does not repeat it
    assert len(runs) == 3
    assert sum('zero interval' in r.getMessage() for r in caplog.records) == 1

    caplog.clear()

    @tasks.loop(count=2)
    async def burst():
        pass

    with caplog.at_level(logging.WARNING, logger='discord.ext.tasks'):
        await burst.start()
    assert not any('zero interval' in r.getMessage() for r in caplog.records)

    @tasks.loop(seconds=0.01, count=1)
    async def paced():
        pass

    with caplog.at_level(logging.WARNING, logger='discord.ext.tasks'):
        await paced.start()
    assert not any('zero interval' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_past_due_subscription_without_grace_period(caplog):
    """R2-A7-15: remaining computed None - utcnow() when Discord left out the
    grace period expiry of a past-due subscription."""
    from discord.subscriptions import Subscription

    client, state, guild, channel = await make_env()
    end = utils.utcnow() + datetime.timedelta(days=2)
    data = {
        'id': '1',
        'type': 1,
        'status': 2,  # past_due
        'current_period_start': (end - datetime.timedelta(days=30)).isoformat(),
        'current_period_end': end.isoformat(),
        'items': [],
    }
    sub = Subscription(data=data, state=state)  # type: ignore[arg-type]
    assert sub.status == discord.SubscriptionStatus.past_due
    assert sub.grace_period_expires_at is None
    remaining = sub.remaining
    assert datetime.timedelta(days=1) < remaining <= datetime.timedelta(days=2)

    # Neither date: zero, and said so in the log
    sub.current_period_end = None  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger='discord.subscriptions'):
        assert sub.remaining == datetime.timedelta()
    assert any('no period end either' in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_forum_clone_without_category():
    """R2-A4-01: ForumChannel.clone() had no default for category, so calling
    it the documented way raised TypeError."""
    client, state, guild, channel = await make_env()
    forum = discord.ForumChannel(
        state=state,
        guild=guild,
        data={'id': '600', 'name': 'forum', 'type': 15, 'position': 1},  # type: ignore[arg-type]
    )
    guild._add_channel(forum)
    sent = {}

    async def create_channel(guild_id, channel_type, *, reason=None, **options):
        sent.update(options, type=channel_type)
        return {'id': '601', 'name': options['name'], 'type': channel_type, 'position': 2}

    state.http.create_channel = create_channel  # type: ignore[method-assign]
    clone = await forum.clone()
    assert isinstance(clone, discord.ForumChannel) and clone.name == 'forum'
    assert sent['type'] == 15 and sent['parent_id'] == forum.category_id


_OLD_SUB = '|'.join(r'\{0}(?=([\s\S]*((?<!\{0})\{0})))'.format(c) for c in ('*', '`', '_', '~', '|'))
_OLD_AS_NEEDED = re.compile(rf'(?P<markdown>{_OLD_SUB}|{utils._MARKDOWN_ESCAPE_COMMON})', re.MULTILINE)


def _old_as_needed(text):
    return _OLD_AS_NEEDED.sub(r'\\\1', utils._BACKSLASH_REGEX.sub(r'\\\\', text))


@pytest.mark.parametrize(
    'text',
    [
        '',
        '**hello**',
        '**# > _',
        '*a* `b` _c_ ~~d~~ ||e||',
        '> quote *x*\n# head _y_\n - item ~z~',
        '[link *t*](http://x/_y_) **',
        'a\\*b\\*c',
        '***___```~~~|||',
        '__init__ and *args',
    ],
)
def test_escape_markdown_as_needed_matches_the_old_regex(text):
    """R2-C2-03: same output as upstream's lookahead regex (a 200k-string fuzz
    run agreed too)."""
    assert utils.escape_markdown(text, as_needed=True) == _old_as_needed(text)


def test_escape_markdown_as_needed_is_linear():
    """R2-C2-03: the per-character lookahead made as_needed O(n^2); 20k
    asterisks took several seconds."""
    start = time.perf_counter()
    out = utils.escape_markdown('*' * 20_000, as_needed=True)
    assert time.perf_counter() - start < 0.5
    assert out == '*' * 20_000  # one run: nothing after it, so nothing escaped
    assert utils.escape_markdown('*' * 5000 + 'x*', as_needed=True) == '\\*' * 5000 + 'x*'


@pytest.mark.asyncio
async def test_settings_bytes_fields_survive_protobuf_json(monkeypatch):
    """dismissed_contents and GuildProgress.dismissed_contents went into
    ParseDict as raw bytes, which protobuf JSON reads as base64: [1] became
    b'' and the edit silently lost the value."""
    from google.protobuf.json_format import ParseDict

    from discord.settings import GuildProgress
    from test_refactor_fixes import _blank_user_settings

    settings = _blank_user_settings()
    cls = type(settings)
    sent = []
    encode = cls.dict_to_base64

    def capture(self, payload):
        sent.append(payload)
        return encode(self, payload)

    monkeypatch.setattr(cls, 'dict_to_base64', capture)
    progress = GuildProgress(
        1234,
        hub_progress=discord.HubProgressFlags(),
        onboarding_progress=discord.OnboardingProgressFlags(),
        dismissed_contents=[1, 7],
    )
    await settings.edit(guild_progress_settings=[progress], dismissed_contents=[3, 4])
    message = ParseDict(sent[-1], cls.PROTOBUF_CLS())
    assert message.guilds.guilds[1234].dismissed_guild_content == b'\x01\x07'
    assert message.user_content.dismissed_contents == b'\x03\x04'
    # GuildProgress.to_dict() itself keeps its raw-bytes shape
    assert progress.to_dict()['dismissed_guild_content'] == b'\x01\x07'
