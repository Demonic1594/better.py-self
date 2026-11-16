"""Follow-ups to the wiki rewrite: the bugs it documented, fixed."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import discord
from discord.http import Route

from test_rate_limit_behavior import FakeResponse, make_http


def _record_sleeps(monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        if delay:  # the fake session's own zero-length sleeps aren't retries
            sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr('discord.http.asyncio.sleep', fake_sleep)
    return sleeps


# --- The HTTP retry loop doesn't sleep before giving up ---------------------------------------


@pytest.mark.asyncio
async def test_five_transient_5xx_sleep_four_times_then_raise(monkeypatch, caplog):
    """It slept a fifth time (about 9 seconds) before raising, and logged "attempt 6 of 5"."""
    http = make_http([(FakeResponse(502, b'{}'), 0)] * 5)
    sleeps = _record_sleeps(monkeypatch)
    with caplog.at_level(logging.WARNING, logger='discord.http'):
        with pytest.raises(discord.DiscordServerError):
            await http.request(Route('GET', '/users/@me'))
    assert len(sleeps) == 4
    assert 'attempt 6 of 5' not in caplog.text and 'attempt 5 of 5' in caplog.text


@pytest.mark.asyncio
async def test_five_429s_raise_without_a_last_sleep(monkeypatch, caplog):
    body = b'{"retry_after": 3.0, "global": false}'
    http = make_http([(FakeResponse(429, body), 0)] * 5)
    sleeps = _record_sleeps(monkeypatch)
    with caplog.at_level(logging.WARNING, logger='discord.http'):
        with pytest.raises(discord.HTTPException) as info:
            await http.request(Route('GET', '/users/@me'))
    assert info.value.status == 429 and not isinstance(info.value, discord.RateLimited)
    assert sleeps == [3.0] * 4
    assert 'on the last of 5 attempts; giving up' in caplog.text


@pytest.mark.asyncio
async def test_a_global_429_on_the_last_attempt_still_closes_the_gate(monkeypatch):
    """Giving up without sleeping must not let the other requests ignore the cooldown."""
    body = b'{"retry_after": 0.2, "global": true}'
    http = make_http([(FakeResponse(429, body), 0)] * 5)
    sleeps = _record_sleeps(monkeypatch)
    # The first four global 429s sleep (instantly here); the fifth gives up
    with pytest.raises(discord.HTTPException):
        await http.request(Route('GET', '/users/@me'))
    assert len(sleeps) == 4
    assert not http._global_over.is_set(), 'the global gate stays closed for the cooldown'
    await asyncio.wait_for(http._global_over.wait(), 2)


@pytest.mark.asyncio
async def test_five_deferrals_raise_without_a_last_sleep(monkeypatch):
    body = b'{"code": 110000, "retry_after": 2.0}'
    http = make_http([(FakeResponse(202, body), 0)] * 5)
    sleeps = _record_sleeps(monkeypatch)
    with pytest.raises(discord.HTTPException):
        await http.request(Route('GET', '/users/@me'))
    assert sleeps == [2.0] * 4


@pytest.mark.asyncio
async def test_a_retry_that_succeeds_is_unchanged(monkeypatch):
    """Guard: a transient error followed by success still returns the data."""
    http = make_http([(FakeResponse(502, b'{}'), 0), (FakeResponse(200, b'{"ok": true}'), 0)])
    sleeps = _record_sleeps(monkeypatch)
    assert await http.request(Route('GET', '/users/@me')) == {'ok': True}
    assert len(sleeps) == 1


# --- File(bytes) explains itself --------------------------------------------------------------


def test_file_given_bytes_data_says_to_use_bytesio():
    with pytest.raises(FileNotFoundError, match=r'io\.BytesIO') as info:
        discord.File(b'not a path at all')
    assert info.value.errno == 2 and info.value.filename == b'not a path at all', 'errno and filename are kept'
    with pytest.raises(ValueError, match=r'io\.BytesIO'):
        discord.File(b'binary\x00data')


def test_file_from_a_bytes_path_still_works(tmp_path):
    """Guard: bytes are still a file path, as upstream has it."""
    path = tmp_path / 'a.txt'
    path.write_bytes(b'hi')
    f = discord.File(bytes(path))
    try:
        assert f.fp.read() == b'hi'
    finally:
        f.close()


# --- ForumChannel.create_tag finds the new tag ------------------------------------------------


def _forum(existing_tags):
    forum = discord.ForumChannel.__new__(discord.ForumChannel)
    forum.id = 900
    forum._available_tags = {t: None for t in existing_tags}
    return forum


@pytest.mark.asyncio
async def test_create_tag_takes_the_new_tag_wherever_discord_puts_it():
    """It took the last tag in Discord's answer, which isn't always the new one."""
    forum = _forum([1, 2])

    async def create_forum_tag(channel_id, **kwargs):
        return {
            'available_tags': [
                {'id': '1', 'name': 'old'},
                {'id': '55', 'name': 'Solved'},  # the new one, not last
                {'id': '2', 'name': 'older'},
            ]
        }

    forum._state = SimpleNamespace(http=SimpleNamespace(create_forum_tag=create_forum_tag))
    tag = await forum.create_tag(name='Solved', emoji=None)
    assert tag.id == 55


@pytest.mark.asyncio
async def test_create_tag_prefers_the_new_tag_with_its_name():
    """Another tag created at the same moment must not be taken for ours."""
    forum = _forum([1])

    async def create_forum_tag(channel_id, **kwargs):
        return {'available_tags': [{'id': '1', 'name': 'a'}, {'id': '70', 'name': 'Solved'}, {'id': '80', 'name': 'Other'}]}

    forum._state = SimpleNamespace(http=SimpleNamespace(create_forum_tag=create_forum_tag))
    tag = await forum.create_tag(name='Solved', emoji=None)
    assert tag.id == 70


@pytest.mark.asyncio
async def test_create_tag_warns_when_the_tag_is_missing(caplog):
    forum = _forum([1])

    async def create_forum_tag(channel_id, **kwargs):
        return {'available_tags': [{'id': '1', 'name': 'a'}]}

    forum._state = SimpleNamespace(http=SimpleNamespace(create_forum_tag=create_forum_tag))
    with caplog.at_level(logging.WARNING, logger='discord.channel'):
        tag = await forum.create_tag(name='Solved', emoji=None)
    assert tag.id == 0
    assert 'could not find it in Discord' in caplog.text


@pytest.mark.asyncio
async def test_create_tag_without_a_name_match_takes_the_last_new_tag():
    """Discord normalised the name, say: the last tag the forum didn't have is taken."""
    forum = _forum([1])

    async def create_forum_tag(channel_id, **kwargs):
        return {'available_tags': [{'id': '1', 'name': 'a'}, {'id': '90', 'name': 'solved'}]}

    forum._state = SimpleNamespace(http=SimpleNamespace(create_forum_tag=create_forum_tag))
    tag = await forum.create_tag(name='Solved', emoji=None)
    assert tag.id == 90


# --- The last-attempt 429 is still reported ----------------------------------------------------


@pytest.mark.asyncio
async def test_every_429_dispatches_http_throttle_including_the_last(monkeypatch):
    body = b'{"retry_after": 1.0, "global": false}'
    http = make_http([(FakeResponse(429, body), 0)] * 5)
    seen = []
    http.client = SimpleNamespace(
        dispatch=lambda name, *args: seen.append(name),
        _connection=SimpleNamespace(installation_id=None, locale='en-US'),
    )
    _record_sleeps(monkeypatch)
    with pytest.raises(discord.HTTPException):
        await http.request(Route('GET', '/users/@me'))
    assert seen.count('http_throttle') == 5
    assert sum(len(v) for v in http._throttle_events.values()) == 5


@pytest.mark.asyncio
async def test_a_cooldown_release_from_before_a_restart_is_ignored():
    """A release scheduled before the client restarted must not reopen the new gate."""
    http = make_http([])
    http._engage_global_cooldown(0.05)
    # Simulate a restart: the state a fresh _startup() creates
    http._global_generation += 1
    http._global_over = asyncio.Event()
    http._global_holders = 1  # a new cooldown is running
    await asyncio.sleep(0.1)  # the old release fires now
    assert http._global_holders == 1 and not http._global_over.is_set()


# --- Role.edit validates before moving ---------------------------------------------------------


@pytest.mark.asyncio
async def test_role_edit_with_an_invalid_argument_does_not_move_the_role(monkeypatch):
    """The role used to be moved first, then the bad argument raised."""
    calls = []

    async def move_role_position(*args, **kwargs):
        calls.append('move')

    async def edit_role(*args, **kwargs):
        calls.append('edit')
        return {}

    role = discord.Role.__new__(discord.Role)
    role.id = 10
    role.guild = SimpleNamespace(id=1)
    role._state = SimpleNamespace(http=SimpleNamespace(move_role_position=move_role_position, edit_role=edit_role))

    async def fake_move(self, position, reason):
        calls.append('move')

    monkeypatch.setattr(discord.Role, '_move', fake_move)
    with pytest.raises(AttributeError):
        await role.edit(position=3, permissions='not permissions')  # type: ignore
    assert calls == []
