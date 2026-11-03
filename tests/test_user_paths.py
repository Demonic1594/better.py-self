"""User-facing paths: sending, pagination, commands and task loops."""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
from types import SimpleNamespace

import pytest

import discord
from discord import Client
from discord.ext import commands, tasks
from discord.ext.commands.view import StringView
from discord.http import handle_message_parameters
from discord.state import ChunkRequest

from test_persistence import drive_ready, guild_payload, ready_payload, up


async def _client():
    client = Client()
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(state=state, data=up(5))
    ready = ready_payload()
    ready['guilds'] = [guild_payload(1, 1)]
    await drive_ready(client, ready)
    return client, state


# --- Sending -------------------------------------------------------------------


def test_file_from_an_open_file_sends_only_its_basename(tmp_path):
    path = tmp_path / 'private' / 'report.txt'
    path.parent.mkdir()
    path.write_bytes(b'x')
    with open(path, 'rb') as fp:
        assert discord.File(fp).filename == 'report.txt', 'the local directory must not be uploaded'
    assert discord.File(io.BytesIO(b'x')).filename == 'untitled'
    assert discord.File(str(path)).filename == 'report.txt'


@pytest.mark.asyncio
async def test_greet_mention_author_leaves_the_callers_allowed_mentions_alone(monkeypatch):
    client, state = await _client()
    channel = client.get_channel(501)
    caller = discord.AllowedMentions(everyone=False)
    sent = {}

    async def send_greet(channel_id, sticker_id, *, message_reference, allowed_mentions):
        sent['allowed_mentions'] = allowed_mentions
        raise RuntimeError('stop here')

    monkeypatch.setattr(state.http, 'send_greet', send_greet)
    sticker = SimpleNamespace(id=1)
    with pytest.raises(RuntimeError):
        await channel.greet(sticker, allowed_mentions=caller, mention_author=False)
    assert caller.replied_user is not False, 'the caller object must not be modified'
    assert sent['allowed_mentions'].replied_user is False and sent['allowed_mentions'].everyone is False


def test_falsy_nonce_is_still_sent():
    with handle_message_parameters(content='x', nonce=0) as params:
        assert params.payload['nonce'] == 0
    with handle_message_parameters(content='x', nonce=None) as params:
        assert 'nonce' not in params.payload


def test_embed_from_dict_does_not_modify_the_input():
    data = {'title': 't', 'fields': [{'name': 'a', 'value': 'b', 'inline': True}]}
    embed = discord.Embed.from_dict(data)
    embed.add_field(name='c', value='d')
    assert len(data['fields']) == 1
    assert len(embed.fields) == 2


@pytest.mark.asyncio
async def test_delayed_delete_logs_unexpected_errors(monkeypatch, caplog):
    client, state = await _client()
    channel = client.get_channel(501)
    message = discord.PartialMessage(channel=channel, id=99)

    async def delete_message(channel_id, message_id, reason=None):
        raise RuntimeError('network went away')

    monkeypatch.setattr(state.http, 'delete_message', delete_message)
    with caplog.at_level(logging.WARNING, logger='discord.message'):
        await discord.Message.delete(message, delay=0)  # type: ignore[arg-type]
        for _ in range(5):
            await asyncio.sleep(0)
    assert any('after a delay failed' in r.getMessage() for r in caplog.records)


# --- Pagination ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_event_users_with_before_and_after(monkeypatch):
    """The endpoint returns top-level user objects with string ids; the
    before/after predicates used ['user']['id'] and compared str to int."""
    client, state = await _client()
    event = discord.ScheduledEvent(
        state=state,
        data={
            'id': '7',
            'guild_id': '1',
            'name': 'e',
            'scheduled_start_time': '2026-01-01T00:00:00+00:00',
            'privacy_level': 2,
            'status': 1,
            'entity_type': 3,
            'entity_metadata': {'location': 'x'},
            'user_count': 1,  # a stale snapshot must not cap the iteration
        },
    )
    pages = [[up(400), up(300), up(200)], []]

    async def get_scheduled_event_users(guild_id, event_id, *, limit, with_member, before=None, after=None):
        return {'users': pages.pop(0) if pages else []}

    monkeypatch.setattr(state.http, 'get_scheduled_event_users', get_scheduled_event_users)
    users = [u.id async for u in event.users(before=discord.Object(id=350), after=discord.Object(id=100))]
    assert sorted(users) == [200, 300]
    pages[:] = [[up(400), up(300), up(200)], []]
    assert len([u async for u in event.users()]) == 3, 'limit=None returns every user'


@pytest.mark.asyncio
async def test_search_offset_applies_to_the_first_page_only(monkeypatch):
    client, state = await _client()
    channel = client.get_channel(501)
    sent = []

    def msg(i):
        return {
            'id': str(i),
            'channel_id': '501',
            'author': up(9),
            'content': 'x',
            'timestamp': '2026-01-01T00:00:00+00:00',
            'edited_timestamp': None,
            'tts': False,
            'mention_everyone': False,
            'mentions': [],
            'mention_roles': [],
            'attachments': [],
            'embeds': [],
            'pinned': False,
            'type': 0,
            'flags': 0,
        }

    pages = [
        {'messages': [[msg(30 + i)] for i in range(25)] + [[]], 'total_results': 60},
        {'messages': [[msg(10)]], 'total_results': 60},
    ]

    async def search(entity_id, payload):
        sent.append(dict(payload))
        return pages.pop(0) if pages else {'messages': [], 'total_results': 60}

    # Guild channels search through the guild endpoint
    monkeypatch.setattr(state.http, 'search_guild', search)
    monkeypatch.setattr(state.http, 'search_channel', search)
    found = [m async for m in channel.search('x', offset=10, limit=30)]
    assert found, 'an empty trailing entry must not break the cursor'
    assert sent[0].get('offset') == 10
    assert len(sent) > 1 and 'offset' not in sent[1], 'later pages are positioned by the cursor'


@pytest.mark.asyncio
async def test_archived_threads_treat_naive_datetimes_as_local_time(monkeypatch):
    client, state = await _client()
    channel = client.get_channel(501)
    seen = {}

    async def get_public_archived_threads(channel_id, before=None, limit=50):
        seen['before'] = before
        return {'threads': [], 'members': [], 'has_more': False}

    monkeypatch.setattr(state.http, 'get_public_archived_threads', get_public_archived_threads)
    naive = datetime.datetime(2024, 1, 1, 12, 0)
    [t async for t in channel.archived_threads(before=naive)]
    expected = naive.astimezone(datetime.timezone.utc).isoformat()
    assert seen['before'] == expected, 'same instant as history(before=naive)'
    assert seen['before'].endswith('+00:00')


# --- Member requests -----------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_result_that_arrives_before_the_waiter_is_kept():
    request = ChunkRequest(1, asyncio.get_running_loop(), lambda gid: None, cache=False)
    request.done()  # the reply landed while the request was still being sent
    assert await asyncio.wait_for(request.wait(), timeout=1) == []
    # Claimed once: the next wait is a fresh one
    pending = asyncio.ensure_future(request.wait())
    await asyncio.sleep(0)
    assert not pending.done()
    request.done()
    assert await asyncio.wait_for(pending, timeout=1) == []


# --- Commands and tasks --------------------------------------------------------


@pytest.mark.asyncio
async def test_greedy_ignores_trailing_whitespace():
    @commands.command()
    async def cmd(ctx, numbers: commands.Greedy[int]):
        pass

    ctx = SimpleNamespace(view=StringView('1 2   '), current_argument=None, bot=None, command=cmd)
    param = cmd.clean_params['numbers']
    result = await cmd._transform_greedy_pos(ctx, param, False, int)  # type: ignore[arg-type]
    assert result == [1, 2], 'no phantom argument from the trailing spaces'
    assert ctx.view.eof


def test_task_loop_accepts_times_in_different_zones():
    from zoneinfo import ZoneInfo

    paris = datetime.time(9, tzinfo=ZoneInfo('Europe/Paris'))
    utc = datetime.time(7, tzinfo=datetime.timezone.utc)

    @tasks.loop(time=[paris, utc])
    async def job():
        pass

    assert set(job._time) == {paris, utc}
