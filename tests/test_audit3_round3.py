"""Audit round 3, follow-ups: the items left open in docs/audit_2026-11.md.

Each test fails on the code before these fixes, unless it says it pins
behaviour that was already right.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import discord
from discord import utils
from discord.ext import tasks
from discord.utils import MISSING

from test_abuse3 import msg_payload
from test_persistence import up
from test_user_paths import _client

GUILD_ID = 1
CHANNEL_ID = 501
SELF_ID = 5


# --- 1. Voice handshake races ---------------------------------------------------


def _udp_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    sock.setblocking(False)
    return sock


def test_socket_reader_follows_a_replaced_socket_at_once():
    """The reader sat in select() on the old socket for up to 30 seconds, so the
    first packets on a new socket (IP discovery) waited that long."""
    from discord.voice_state import SocketReader

    old, new, sender = _udp_socket(), _udp_socket(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    state = SimpleNamespace(socket=old)
    received = threading.Event()
    reader = SocketReader(state, start_paused=False)  # type: ignore[arg-type]
    reader.register(lambda data: received.set() if data == b'probe' else None)
    reader.start()
    try:
        time.sleep(0.1)  # the reader is now waiting on `old`
        state.socket = new
        reader.wake()
        sender.sendto(b'probe', new.getsockname())
        assert received.wait(2), 'the packet on the new socket was not read within 2 seconds'
    finally:
        reader.stop()
        reader.join(2)
        for sock in (old, new, sender):
            sock.close()
    assert not reader.is_alive(), 'stop() must end the reader without waiting out select()'


def test_creating_a_socket_closes_the_previous_one():
    from discord.voice_state import VoiceConnectionState

    conn = VoiceConnectionState.__new__(VoiceConnectionState)
    previous = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    conn.socket = previous
    calls = []
    conn._socket_reader = SimpleNamespace(wake=lambda: calls.append('wake'), resume=lambda: calls.append('resume'))  # type: ignore
    conn._create_socket()
    try:
        assert previous.fileno() == -1, 'the replaced socket was left open'
        assert conn.socket is not previous
        assert calls == ['wake', 'resume']
    finally:
        conn.socket.close()


def _voice_state():
    from discord.voice_state import ConnectionFlowState, VoiceConnectionState

    conn = VoiceConnectionState.__new__(VoiceConnectionState)
    conn._state = ConnectionFlowState.set_guild_voice_state
    conn._state_event = asyncio.Event()
    conn._state_waiters = []
    conn._connected = threading.Event()
    conn._connector = None
    conn._aborted_connector = None
    conn._runner = None
    conn._disconnected = asyncio.Event()
    conn.ws = None
    conn.socket = None
    conn.timeout = 30.0
    conn.voice_client = SimpleNamespace(loop=asyncio.get_running_loop(), stop=lambda: None, cleanup=lambda: None)
    return conn


@pytest.mark.asyncio
async def test_a_state_change_right_after_the_check_is_not_missed():
    """_wait_for_state checked the state, then waited on an Event in a task that
    only started a loop turn later; a change in between came and went unseen."""
    from discord.voice_state import ConnectionFlowState

    conn = _voice_state()
    waiter = asyncio.ensure_future(conn._wait_for_state(ConnectionFlowState.got_both_voice_updates, timeout=1.0))
    await asyncio.sleep(0)  # the waiter has checked the state and is about to wait
    conn.state = ConnectionFlowState.got_both_voice_updates
    await asyncio.wait_for(waiter, 0.5)  # timed out after 1 s before
    assert conn._state_waiters == []


@pytest.mark.asyncio
async def test_waiting_for_a_state_still_times_out_and_cleans_up():
    from discord.voice_state import ConnectionFlowState

    conn = _voice_state()
    with pytest.raises(asyncio.TimeoutError):
        await conn._wait_for_state(ConnectionFlowState.connected, timeout=0.05)
    assert conn._state_waiters == []
    conn.state = ConnectionFlowState.got_voice_state_update  # nothing left to wake


def _connect_kwargs():
    return dict(reconnect=True, timeout=30.0, self_deaf=False, self_mute=False, self_video=False, resume=False)


def _patch_disconnect_steps(conn):
    async def nothing(*args, **kwargs):
        return None

    conn._voice_disconnect = nothing
    conn.soft_disconnect = nothing
    conn._pause_socket_reader = lambda: None
    conn._stop_socket_reader = lambda: None
    conn._inside_runner = lambda: False


@pytest.mark.asyncio
async def test_disconnect_during_the_handshake_stops_it():
    """disconnect() returned early while not yet connected, and VoiceClient then
    unregistered the client while its handshake went on into an untracked connection."""
    conn = _voice_state()
    _patch_disconnect_steps(conn)
    started = asyncio.Event()

    async def slow_connect(*args):
        started.set()
        await asyncio.sleep(3600)

    conn._connect = slow_connect
    connecting = asyncio.ensure_future(conn.connect(**_connect_kwargs()))
    await asyncio.wait_for(started.wait(), 1)
    connector = conn._connector

    await asyncio.wait_for(conn.disconnect(force=False), 1)
    assert connector.cancelled(), 'the handshake was left running'
    with pytest.raises(discord.ClientException, match='while connecting'):
        await asyncio.wait_for(connecting, 1)


@pytest.mark.asyncio
async def test_connect_follows_a_handshake_that_replaced_it():
    """Moved while connecting, the voice state starts a new handshake, which
    cancelled the first one: the caller's connect() raised CancelledError (and,
    since the last round's cleanup, tore the new attempt down)."""
    conn = _voice_state()
    _patch_disconnect_steps(conn)
    attempts = []

    async def fake_connect(*args):
        attempts.append(args)
        if len(attempts) == 1:
            await asyncio.sleep(3600)

    conn._connect = fake_connect
    connecting = asyncio.ensure_future(conn.connect(**_connect_kwargs()))
    await asyncio.sleep(0.01)
    await conn.connect(**_connect_kwargs(), wait=False)  # what the move handling does
    await asyncio.wait_for(connecting, 1)  # returns normally once the new attempt is done
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_cancelling_connect_still_cancels_the_handshake():
    """Pins the old behaviour, which awaiting the connector directly gave."""
    conn = _voice_state()
    _patch_disconnect_steps(conn)

    async def slow_connect(*args):
        await asyncio.sleep(3600)

    conn._connect = slow_connect
    connecting = asyncio.ensure_future(conn.connect(**_connect_kwargs()))
    await asyncio.sleep(0.01)
    connector = conn._connector
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    await asyncio.sleep(0)
    assert connector.cancelled()


# --- 2. client_theme reset --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('reset', [MISSING, None])
async def test_client_theme_can_be_reset(monkeypatch, reset):
    """MISSING (documented to reset every setting) and None were both dropped."""
    from test_refactor_fixes import _blank_user_settings

    settings = _blank_user_settings()
    themed = await settings.edit(client_theme=(MISSING, 2, MISSING))
    assert themed.client_theme is not None and themed.client_theme[1] == 2

    sent = []
    encode = type(themed).dict_to_base64

    def capture(self, payload):
        sent.append(payload)
        return encode(self, payload)

    monkeypatch.setattr(type(themed), 'dict_to_base64', capture)
    plain = await themed.edit(client_theme=reset)
    assert 'appearance' in sent[-1] and 'client_theme_settings' not in sent[-1]['appearance']
    assert plain.client_theme is None


@pytest.mark.asyncio
async def test_client_theme_with_the_current_settings_schema(caplog):
    """discord_protos dropped primary_color and background_gradient_angle: setting a
    theme raised ParseError, and reading one raised AttributeError."""
    from discord.settings import _client_theme_fields
    from test_refactor_fixes import _blank_user_settings

    if 'primary_color' in _client_theme_fields():
        pytest.skip('the installed discord_protos still has the old fields')
    settings = _blank_user_settings()
    with caplog.at_level(logging.WARNING, logger='discord.settings'):
        themed = await settings.edit(client_theme=(0x5865F2, 3, 45.0))
    assert 'primary_color or background_gradient_angle' in caplog.text
    assert themed.client_theme == (0, 3, 0.0)

    # Colors and the angle of a custom theme are read from where the schema keeps them
    custom = themed.settings.appearance.client_theme_settings.custom_user_theme_settings
    custom.colors.append('#5865f2')
    custom.gradient_angle = 90
    assert themed.client_theme == (0x5865F2, 3, 90.0)


# --- 3. Partial presence users ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_presence_keeps_user_fields_it_leaves_out():
    """A presence carries a partial user. Leaving out public_flags or collectibles
    used to clear them (and fire on_user_update); a missing username raised KeyError."""
    client, state = await _client()
    member = state._get_guild(GUILD_ID).get_member(100)
    user = member._user
    user._public_flags = 64
    user._collectibles = {'nameplate': {'asset': 'x'}}
    updates = []

    @client.event
    async def on_user_update(before, after):
        updates.append((before, after))

    state.parse_presence_update(
        {
            'user': {'id': '100', 'avatar': 'abc'},
            'guild_id': str(GUILD_ID),
            'status': 'online',
            'activities': [],
            'client_status': {},
        }
    )
    await asyncio.sleep(0)
    assert user._avatar == 'abc', 'a field the payload carries is still applied'
    assert user._public_flags == 64 and user._collectibles == {'nameplate': {'asset': 'x'}}
    assert user.name == 'u100'
    assert len(updates) == 1 and updates[0][0]._avatar is None

    # A partial user that changes nothing dispatches nothing
    state.parse_presence_update(
        {
            'user': {'id': '100', 'username': 'u100'},
            'guild_id': str(GUILD_ID),
            'status': 'idle',
            'activities': [],
            'client_status': {},
        }
    )
    await asyncio.sleep(0)
    assert len(updates) == 1
    await client.close()


@pytest.mark.asyncio
async def test_a_member_update_still_applies_the_full_user():
    """GUILD_MEMBER_UPDATE carries a full user, so a missing optional key still clears it."""
    client, state = await _client()
    user = state._get_guild(GUILD_ID).get_member(100)._user
    user._public_flags = 64
    state.parse_guild_member_update({'guild_id': str(GUILD_ID), 'user': up(100), 'roles': []})
    assert user._public_flags == 0
    await client.close()


@pytest.mark.asyncio
async def test_own_presence_with_a_partial_user():
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    me = discord.Member(data={'user': up(SELF_ID), 'roles': []}, guild=guild, state=state)
    me._user = state.user
    assert (
        me._presence_update({'status': 'online', 'activities': [], 'client_status': {}}, {'id': '5', 'avatar': 'x'}) is None
    )  # type: ignore
    await client.close()


# --- 4 and 5. Login failure and requests after close ----------------------------------------


class _ClosableSession:
    def __init__(self):
        self._closed = False
        self.closes = 0

    async def close(self):
        self._closed = True
        self.closes += 1


@pytest.mark.asyncio
async def test_a_failed_first_login_closes_the_http_session():
    """Outside `async with` or run(), nothing closed the session a failed login opened."""
    client = discord.Client()
    await client._async_setup_hook()
    http = client.http
    session = _ClosableSession()
    http._HTTPClient__session = session  # type: ignore[attr-defined]
    http._started = True

    async def static_login(token):
        raise discord.LoginFailure('Improper token has been passed')

    http.static_login = static_login  # type: ignore[method-assign]
    with pytest.raises(discord.LoginFailure):
        await client.login('not-a-real-token')
    assert session.closes == 1
    assert http._started is False and not http._HTTPClient__session, 'a later login must build a new session'  # type: ignore[attr-defined]
    await client.close()


@pytest.mark.asyncio
async def test_a_failed_relogin_leaves_the_running_session_alone():
    client = discord.Client()
    await client._async_setup_hook()
    client._connection.user = discord.ClientUser(state=client._connection, data=up(SELF_ID))
    http = client.http
    session = _ClosableSession()
    http._HTTPClient__session = session  # type: ignore[attr-defined]
    http._started = True

    async def static_login(token):
        raise discord.LoginFailure('Improper token has been passed')

    http.static_login = static_login  # type: ignore[method-assign]
    with pytest.raises(discord.LoginFailure):
        await client.login('not-a-real-token')
    assert session.closes == 0
    http._HTTPClient__session = MISSING  # type: ignore[attr-defined]
    await client.close()


@pytest.mark.asyncio
async def test_a_request_after_close_fails_at_once():
    """Pins documented behaviour: a handler still running after close() gets a clear
    error straight away, not a retried request."""
    from curl_cffi import requests as curl_requests

    from discord.http import Route
    from test_rate_limit_behavior import make_http

    http = make_http([])
    session = curl_requests.AsyncSession()
    await session.close()
    http._HTTPClient__session = session  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(curl_requests.exceptions.SessionClosed):
        await http.request(Route('GET', '/users/@me'))
    assert loop.time() - started < 0.5


# --- 7. Task loops --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_during_the_wait_runs_no_further_iteration():
    """stop() during the wait between iterations let one more iteration run, and
    the loop only ended once the interval was over."""
    runs = []
    after = []

    @tasks.loop(seconds=1)
    async def job():
        runs.append(1)

    @job.after_loop
    async def finished():
        after.append(1)

    started = time.perf_counter()
    job.start()
    await asyncio.sleep(0.2)
    job.stop()
    await asyncio.wait_for(job._task, 2)
    assert runs == [1]
    assert after == [1]
    assert time.perf_counter() - started < 0.6, 'the loop waited out its interval before stopping'


@pytest.mark.asyncio
async def test_stop_while_waiting_for_a_scheduled_time():
    import datetime

    runs = []
    soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)).time()

    @tasks.loop(time=soon)
    async def job():
        runs.append(1)

    job.start()
    await asyncio.sleep(0.05)
    job.stop()
    await asyncio.wait_for(job._task, 1)  # waited until the scheduled time before
    assert runs == []


@pytest.mark.asyncio
async def test_stop_inside_the_body_still_finishes_that_iteration():
    """Pins documented behaviour: the running iteration finishes, then the loop ends."""
    runs = []

    @tasks.loop(seconds=0.05)
    async def job():
        runs.append(1)
        job.stop()
        await asyncio.sleep(0.01)
        runs.append(2)

    job.start()
    await asyncio.wait_for(job._task, 1)
    assert runs == [1, 2]


@pytest.mark.asyncio
async def test_restart_twice_does_not_error(caplog):
    """Both restart callbacks called start(); the second raised RuntimeError."""
    loop = asyncio.get_running_loop()
    errors = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    @tasks.loop(seconds=0.2)
    async def job():
        pass

    try:
        job.start()
        await asyncio.sleep(0.05)
        job.restart()
        job.restart()
        await asyncio.sleep(0.1)
        assert job.is_running()
        assert errors == [], errors
    finally:
        loop.set_exception_handler(previous)
        job.cancel()
        await asyncio.sleep(0)


# --- 8. Rate limit buckets ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_request_that_empties_a_bucket_returns_at_once():
    """__aexit__ waited out the bucket reset before returning, so the last request
    of a window took up to reset_after longer than it needed to."""
    from discord.http import Ratelimit

    loop = asyncio.get_running_loop()
    bucket = Ratelimit(None, 2)
    bucket.remaining = 1
    bucket.reset_after = 0.3
    bucket.expires = loop.time() + bucket.reset_after

    started = loop.time()
    async with bucket:
        pass
    assert loop.time() - started < 0.1, 'the emptying request waited for the reset'

    # The next request still waits for the reset
    started = loop.time()
    async with bucket:
        pass
    assert loop.time() - started >= 0.25


@pytest.mark.asyncio
async def test_requests_queued_on_an_empty_bucket_all_get_through():
    from discord.http import Ratelimit

    loop = asyncio.get_running_loop()
    bucket = Ratelimit(None, 3)
    bucket.remaining = 1
    bucket.reset_after = 0.2
    bucket.expires = loop.time() + bucket.reset_after
    done = []

    async def one(i):
        async with bucket:
            done.append((i, loop.time()))

    started = loop.time()
    await asyncio.wait_for(asyncio.gather(*(one(i) for i in range(3))), 2)
    assert len(done) == 3
    assert done[0][1] - started < 0.1
    assert all(t - started >= 0.15 for _, t in done[1:])


@pytest.mark.asyncio
async def test_a_reset_beyond_max_ratelimit_timeout_is_still_refused():
    """Pins the max_ratelimit_timeout behaviour through the background refresh: a
    request facing a longer wait raises RateLimited instead of queueing."""
    from discord.http import Ratelimit

    loop = asyncio.get_running_loop()
    bucket = Ratelimit(1.0, 1)
    bucket.remaining = 0
    bucket.reset_after = 60.0
    bucket.expires = loop.time() + 60.0
    with pytest.raises(discord.RateLimited):
        await asyncio.wait_for(bucket.acquire(), 1)


# --- 9. Guild subscriptions -------------------------------------------------------------


class _RecordingWS:
    open = True

    def __init__(self):
        self.payloads = []

    async def bulk_guild_subscribe(self, payload):
        self.payloads.append(payload)


@pytest.mark.asyncio
async def test_unsubscribing_from_a_guild_survives_a_reconnect():
    """typing=False unsubscribes from the guild, but the guild stayed marked as
    subscribed: is_subscribed() kept returning True and a reconnect resubscribed."""
    from discord.state import GuildSubscriptions

    ws = _RecordingWS()
    guild = discord.Object(id=1234)
    subs = GuildSubscriptions(SimpleNamespace(ws=ws, loop=asyncio.get_running_loop(), _guilds={1234: guild}))  # type: ignore[arg-type]
    subs._blocked = True

    await subs.subscribe_to(guild, typing=True)
    await subs._flush()
    assert subs.is_subscribed(guild)

    await subs.subscribe_to(guild, typing=False)
    await subs._flush()
    assert not subs.is_subscribed(guild)

    await subs._requeue_subscriptions()
    assert '1234' not in subs._pending, 'the reconnect would subscribe to the guild again'


# --- 10. Thread member removals for uncached threads -----------------------------------------


@pytest.mark.asyncio
async def test_raw_thread_member_remove_fires_for_an_uncached_thread():
    client, state = await _client()
    seen = []

    @client.event
    async def on_raw_thread_member_remove(payload):
        seen.append(payload)

    state.parse_thread_members_update(
        {'id': '777', 'guild_id': str(GUILD_ID), 'member_count': 1, 'removed_member_ids': ['100', str(SELF_ID)]}
    )
    await asyncio.sleep(0)
    assert len(seen) == 1, 'one event per removed member other than us'
    assert seen[0].thread_id == 777 and seen[0].data['removed_member_ids'] == ['100', str(SELF_ID)]
    await client.close()


# --- 11. Python 3.14 annotations --------------------------------------------------------


_DEFINED_FIRST = """
from discord.ext import commands

class Before(commands.Converter):
    async def convert(self, ctx, argument):
        return argument

@commands.command()
async def probe(ctx, value: Before, count: int = 1, *, rest: "str"):
    pass
"""

_DEFINED_LATER = """
from discord.ext import commands

@commands.command()
async def probe(ctx, value: LaterDefined):
    pass

class LaterDefined:
    pass
"""


def test_command_annotations_resolve_the_same_on_every_python():
    """Checked for 3.14 (deferred annotations): annotations resolve as before, and a
    name defined only after the command fails at decoration, as it always has with
    string annotations. CI runs this on 3.10 to 3.14."""
    namespace: dict = {}
    exec(compile(_DEFINED_FIRST, '<commands>', 'exec'), namespace)
    params = namespace['probe'].clean_params
    assert params['value'].annotation is namespace['Before']
    assert params['count'].annotation is int and params['rest'].annotation is str

    for prefix in ('', 'from __future__ import annotations\n'):
        with pytest.raises(NameError):
            exec(compile(prefix + _DEFINED_LATER, '<commands>', 'exec'), {})


# --- 12. Markdown helpers ---------------------------------------------------------------


@pytest.mark.parametrize(
    'text',
    ['\n' * 80000, '[a](' * 20000, '<a' * 40000, '<a:/' * 20000, '[a](<a:/\n\n' * 8000],
    ids=['blank lines', 'open links', 'angle brackets', 'angle URLs', 'mixed'],
)
def test_markdown_helpers_are_linear(text):
    """These took seconds (17 s for '<a:/' * 20000) because each attempt rescanned
    the rest of the text."""
    started = time.perf_counter()
    utils.escape_markdown(text)
    utils.escape_markdown(text, as_needed=True)
    utils.escape_markdown(text, ignore_links=False)
    utils.remove_markdown(text)
    assert time.perf_counter() - started < 2.0


def test_a_list_after_a_blank_line_is_escaped_on_its_own_line():
    """The backslash went on the blank line, leaving the list marker active, and
    remove_markdown deleted the newline instead of the '-'."""
    assert utils.escape_markdown('hello\n\n- item') == 'hello\n\n\\- item'
    assert utils.escape_markdown('hello\n\n- item', as_needed=True) == 'hello\n\n\\- item'
    assert utils.remove_markdown('hello\n\n- item') == 'hello\n\n item'
    assert utils.escape_markdown('  - item') == '\\  - item', 'indentation on the same line is unchanged'


def test_masked_links_are_still_escaped():
    """Pins the safety property: a masked link whose URL has a '[' is still escaped."""
    assert utils.escape_markdown('[click](https://example.invalid/[x)') == '\\[click](https://example.invalid/[x)'
    assert utils.escape_markdown('[a](b)') == '\\[a](b)'


_OLD_COMMON = r'^>(?:>>)?\s|\[[^\[\]]+\]\([^)]*\)|^#{1,3}|^\s*-'
_OLD_URL = r'(?P<url><[a-zA-Z][^: >]*:\/[^ >]+>|(?:https?|steam):\/\/[^\s<]+[^<.,:;\"\'\]\s])'


def test_markdown_output_only_changes_where_intended(monkeypatch):
    """Differential check against the previous regexes: every input whose output
    changed has a list marker after a blank line or a '<' inside an angle-bracket
    URL, the two cases the change targets. (The full run in the PR used 400,000
    inputs.)"""
    old_stock = re.compile(rf'(?P<markdown>[_\\~|\*`]|{_OLD_COMMON})', re.MULTILINE)
    old = {
        '_MARKDOWN_ESCAPE_REGEX': re.compile(
            rf'(?P<markdown>{utils._MARKDOWN_ESCAPE_SUBREGEX}|{_OLD_COMMON})', re.MULTILINE
        ),
        '_MARKDOWN_STOCK_REGEX': old_stock,
        '_MARKDOWN_STOCK_REMOVE_REGEX': re.compile(f'(?:{_OLD_URL}|{old_stock.pattern})', re.MULTILINE),
    }
    new = {name: getattr(utils, name) for name in old}

    def outputs(regexes, text):
        for name, regex in regexes.items():
            monkeypatch.setattr(utils, name, regex)
        return (
            utils.escape_markdown(text),
            utils.escape_markdown(text, ignore_links=False),
            utils.escape_markdown(text, as_needed=True),
            utils.remove_markdown(text),
            utils.remove_markdown(text, ignore_links=False),
        )

    def intended(text):
        for regex in old.values():
            for match in regex.finditer(text):
                found = match.group(0)
                if '\n' in found and found.endswith('-'):
                    return True
                if found.startswith('<') and '<' in found[1:]:
                    return True
        return False

    alphabet = list('ab-*_~|`>#[]()<>:/ \n\t\\.') + ['http://', '<a:/', '[x](', ')', '\n- ', '> ', '## ']
    rng = random.Random(2026)
    changed = 0
    for _ in range(4000):
        text = ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        before, after = outputs(old, text), outputs(new, text)
        if before != after:
            changed += 1
            assert intended(text), f'unexpected change for {text!r}: {before} -> {after}'
    assert changed, 'the sample should exercise both changes'


# --- 13. A lone chunk request that gets no reply ------------------------------------------


@pytest.mark.asyncio
async def test_a_chunk_request_without_a_reply_gives_up(monkeypatch):
    """A single chunk() whose reply was lost waited forever; Guild.chunk documents
    InvalidData for it."""
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)

    async def chunker(*args, **kwargs):
        return None

    state.chunker = chunker
    monkeypatch.setattr(state, '_chunk_stall_after', lambda guild: 0.2)
    started = asyncio.get_running_loop().time()
    with pytest.raises(discord.InvalidData, match='got no reply'):
        await asyncio.wait_for(state.chunk_guild(guild), 2)
    assert asyncio.get_running_loop().time() - started < 1
    assert guild.id not in state._chunk_requests, 'the dead request must not be joined again'
    await client.close()


@pytest.mark.asyncio
async def test_a_slow_but_progressing_chunk_request_is_not_cut_off(monkeypatch):
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)

    async def chunker(*args, **kwargs):
        return None

    state.chunker = chunker
    monkeypatch.setattr(state, '_chunk_stall_after', lambda guild: 0.2)
    waiting = asyncio.ensure_future(state.chunk_guild(guild))
    await asyncio.sleep(0)
    request = state._chunk_requests[guild.id]
    for _ in range(5):  # 0.5 s in all, more than twice the stall time
        await asyncio.sleep(0.1)
        request.add_members([])
    request.done()
    assert await asyncio.wait_for(waiting, 1) == []
    await client.close()


# --- 14. Smaller model gaps ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_webhook_message_in_a_thread_has_the_thread_as_channel():
    """SyncWebhook ignored `thread` and reported the parent channel."""
    client, state = await _client()
    webhook = discord.SyncWebhook(
        {'id': '42', 'type': 1, 'channel_id': str(CHANNEL_ID), 'guild_id': str(GUILD_ID), 'token': 'tok'},  # type: ignore
        session=None,  # type: ignore
        state=state,
    )
    assert webhook.channel is not None and webhook.channel.id == CHANNEL_ID
    message = webhook._create_message(msg_payload(channel_id='999', guild_id=str(GUILD_ID)), thread=discord.Object(id=999))  # type: ignore
    assert message.channel.id == 999
    # A new forum post (thread_name=, no thread) is in its own channel too
    message = webhook._create_message(msg_payload(channel_id='998', guild_id=str(GUILD_ID)))  # type: ignore
    assert message.channel.id == 998
    await client.close()


def _group(state):
    data = {
        'id': '901',
        'type': 3,
        'owner_id': str(SELF_ID),
        'recipients': [up(100), up(101)],
        'nicks': [{'id': '100', 'nick': 'hundred'}],
    }
    channel = discord.GroupChannel(me=state.user, state=state, data=data)  # type: ignore
    state._add_private_channel(channel)
    return channel


@pytest.mark.asyncio
async def test_group_update_without_recipients_keeps_them():
    client, state = await _client()
    channel = _group(state)
    channel._update({'id': '901', 'type': 3, 'owner_id': str(SELF_ID), 'name': 'renamed'})  # type: ignore
    assert channel.name == 'renamed'
    assert [u.id for u in channel.recipients] == [100, 101]
    await client.close()


@pytest.mark.asyncio
async def test_group_recipient_events_keep_the_list_consistent():
    client, state = await _client()
    channel = _group(state)
    event = {'channel_id': '901', 'user': up(102)}
    state.parse_channel_recipient_add(event)  # type: ignore
    state.parse_channel_recipient_add(event)  # type: ignore # a repeated event
    assert [u.id for u in channel.recipients] == [100, 101, 102]

    hundred = state.get_user(100)
    state.parse_channel_recipient_remove({'channel_id': '901', 'user': up(100)})  # type: ignore
    assert hundred not in channel.nicks, 'the nickname of a member who left was kept'
    await client.close()


def test_widgets_are_hashable():
    from discord.widget import Widget

    first, second = Widget.__new__(Widget), Widget.__new__(Widget)
    first.id = second.id = 7
    assert first == second and hash(first) == hash(second) and len({first, second}) == 1


def test_entitlements_have_no_unset_payment_slot():
    from discord.entitlements import Entitlement

    assert 'payment' not in Entitlement.__slots__


@pytest.mark.asyncio
async def test_leaving_a_guild_drops_its_guild_read_states():
    from discord.read_state import ReadState

    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    for type in (discord.ReadStateType.scheduled_events, discord.ReadStateType.guild_home):
        state.store_read_state(ReadState.default(GUILD_ID, type, state=state))
    state._remove_guild(guild)
    for type in (discord.ReadStateType.scheduled_events, discord.ReadStateType.guild_home):
        assert state.get_read_state(GUILD_ID, type, if_exists=True) is None
    await client.close()


@pytest.mark.asyncio
async def test_newest_first_entitlements_stop_once_past_after():
    """With after= and oldest_first=False the paginator walked back to the very
    first entitlement, one request per 100, filtering everything out."""
    from discord.application import Application

    calls = []

    async def get_app_entitlements(app_id, limit=100, before=None, after=None, **kwargs):
        calls.append(before)
        ids = [i for i in range(1000, 0, -1) if before is None or i < before]
        return [{'id': str(i), 'type': 1, 'sku_id': '1', 'application_id': '9', 'user_id': '5'} for i in ids[:limit]]

    app = Application.__new__(Application)
    app.id = 9
    app._state = SimpleNamespace(http=SimpleNamespace(get_app_entitlements=get_app_entitlements), self_id=5)  # type: ignore
    got = [e.id async for e in app.entitlements(after=discord.Object(id=950), oldest_first=False, limit=None)]
    assert got == list(range(1000, 950, -1))
    assert len(calls) == 1, f'{len(calls)} requests'


def test_activity_with_a_null_party_id():
    """A party with "id": null raised AttributeError while building the activity."""
    from discord.activity import create_activity

    activity = create_activity({'type': 0, 'name': 'game', 'party': {'id': None}}, None, 5)  # type: ignore
    assert activity is not None and activity.name == 'game'
