"""
Robustness fixes: malformed server data, unusual input and cache states must
produce a correct result or a clear error, never a confusing different one.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from types import SimpleNamespace

import pytest

from discord.errors import HTTPException, RateLimited
from discord.http import Route, handle_message_parameters, json_or_text

from traffic_harness import FakeResponse, make_http


def _response(status=400):
    return SimpleNamespace(status=status, reason='Bad Request')


# --- HTTPException construction ------------------------------------------------------


@pytest.mark.parametrize(
    'payload',
    [
        {'code': 50035, 'message': 'Invalid Form Body', 'errors': ['unexpected', 'list']},
        {'code': 50035, 'message': 'Invalid Form Body', 'errors': 'just a string'},
        {'code': 50035, 'message': 'Invalid Form Body', 'errors': {'_errors': None}},
        {'code': 50035, 'message': 'Invalid Form Body', 'errors': {'name': {'_errors': 'oops'}}},
        {'code': 50035, 'message': 'Invalid Form Body', 'errors': {'name': {'_errors': [None, 1, {'message': 'ok'}]}}},
        {'code': 1, 'message': 'x', 'payment_id': 'not-a-number'},
        {'code': 1, 'message': None},
        {'code': 1, 'message': 12345},
    ],
)
def test_http_exception_survives_malformed_payloads(payload):
    exc = HTTPException(_response(), payload)  # used to raise AttributeError/TypeError/ValueError
    assert exc.status == 400
    assert isinstance(exc.text, str)
    assert str(exc).startswith('400 Bad Request')


def test_http_exception_non_string_message():
    assert HTTPException(_response(), ['a', 'list']).text == "['a', 'list']"
    assert HTTPException(_response(), None).text == ''


def test_http_exception_still_formats_normal_errors():
    exc = HTTPException(
        _response(),
        {'code': 50035, 'message': 'Invalid Form Body', 'errors': {'name': {'_errors': [{'message': 'Too long'}]}}},
    )
    assert 'In name: Too long' in exc.text


# --- json_or_text ----------------------------------------------------------------------


class _Body:
    def __init__(self, content: bytes, content_type: str, charset=None):
        self.headers = {'content-type': content_type}
        self.charset = charset
        self._content = content

    async def read(self):
        return self._content


@pytest.mark.asyncio
async def test_json_with_charset_parameter_is_parsed():
    result = await json_or_text(_Body(b'{"id": "1"}', 'application/json; charset=utf-8'))
    assert result == {'id': '1'}
    assert await json_or_text(_Body(b'{"id": "1"}', 'Application/JSON')) == {'id': '1'}


@pytest.mark.asyncio
async def test_unknown_charset_does_not_hide_the_response():
    result = await json_or_text(_Body('café'.encode(), 'text/html', charset='no-such-charset'))
    assert result == 'café'


# --- handle_message_parameters ---------------------------------------------------------------


def test_too_many_attachments_fail_fast():
    with pytest.raises(ValueError, match='attachments has a maximum of 10'):
        handle_message_parameters(attachments=[object()] * 11)  # type: ignore[list-item]


# --- Route ----------------------------------------------------------------------------------


def test_route_warns_when_an_id_is_none(caplog):
    with caplog.at_level(logging.WARNING, logger='discord.http'):
        route = Route('GET', '/channels/{channel_id}/messages', channel_id=None)
    assert 'channel_id' in caplog.text, 'a None id must be diagnosable'
    assert route.url.endswith('/channels/None/messages'), 'behaviour is unchanged: it only warns'


def test_route_ignores_none_for_unused_parameters(caplog):
    with caplog.at_level(logging.WARNING, logger='discord.http'):
        Route('GET', '/users/@me', guild_id=None)
    assert not caplog.records


# --- 429 / 202 handling ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_with_a_non_object_json_body_does_not_crash():
    script = [
        FakeResponse(429, body=b'[1, 2]', headers={'content-type': 'application/json', 'Retry-After': '120', 'Via': '1.1'}),
    ]

    class Session:
        async def request(self, method, url, **kwargs):
            return script.pop(0)

    http = make_http(Session(), max_ratelimit_timeout=30.0)
    with pytest.raises(RateLimited):  # used to be AttributeError from data.get on a list
        await http.request(Route('GET', '/users/{user_id}', user_id=3))


@pytest.mark.asyncio
async def test_deferred_202_respects_the_ratelimit_cap():
    body = b'{"code": 110001, "retry_after": 100000}'
    script = [FakeResponse(202, body=body, headers={'content-type': 'application/json'})]

    class Session:
        async def request(self, method, url, **kwargs):
            return script.pop(0)

    http = make_http(Session(), max_ratelimit_timeout=30.0)
    with pytest.raises(RateLimited):  # used to sleep ~28 hours
        await asyncio.wait_for(http.request(Route('GET', '/users/{user_id}', user_id=3)), timeout=5)


# --- Member.top_role -------------------------------------------------------------------------


@functools.total_ordering
class _Role:
    def __init__(self, position):
        self.position = position

    def __eq__(self, other):
        return self.position == other.position

    def __lt__(self, other):
        return self.position < other.position


def _member(roles, cached):
    from discord import Member, utils

    member = Member.__new__(Member)
    member._roles = utils.SnowflakeList(roles)
    member.guild = SimpleNamespace(get_role=lambda rid: cached.get(rid), default_role=None)
    return member


def test_top_role_with_the_default_role_uncached():
    high = _Role(5)
    member = _member([10, 11, 12], {10: _Role(1), 11: high})  # 12 is not cached, and neither is @everyone
    assert member.top_role is high  # used to raise TypeError comparing None with a Role


def test_top_role_with_no_resolvable_role():
    assert _member([10], {}).top_role is None  # falls back to default_role, which is None here


# --- converters ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_range_rejects_nan():
    from discord.ext import commands

    ctx = SimpleNamespace(current_parameter=SimpleNamespace(name='amount'))
    converter = commands.Range[float, 0.0, 10.0]
    with pytest.raises(commands.RangeError):
        await converter.convert(ctx, 'nan')  # type: ignore[arg-type]
    assert await converter.convert(ctx, '2.5') == 2.5  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_timestamp_converter_overflow_is_a_bad_argument():
    from discord.ext import commands

    with pytest.raises(commands.BadArgument):
        await commands.converter.Timestamp().convert(None, '<t:99999999999999999999999>')  # type: ignore[arg-type]
    assert (await commands.converter.Timestamp().convert(None, '<t:0>')).year == 1970  # type: ignore[arg-type]
