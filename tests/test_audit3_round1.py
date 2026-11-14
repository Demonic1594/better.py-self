"""Audit round 3, fix round 1: regressions for the findings of the full-codebase review."""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
import types
import warnings

import pytest

import discord
from discord import utils
from discord.ext import commands, tasks
from discord.http import handle_message_parameters

from test_abuse3 import msg_payload
from test_persistence import up
from test_user_paths import _client

GUILD_ID = 1
CHANNEL_ID = 501  # guild_payload(1) creates channel 500 + gid
SELF_ID = 5


def _store_message(state, **overrides):
    channel = state.get_channel(CHANNEL_ID)
    message = discord.Message(
        state=state, channel=channel, data=msg_payload(channel_id=str(CHANNEL_ID), guild_id=str(GUILD_ID), **overrides)
    )
    state._messages.append(message)
    index = getattr(state, '_messages_index', None)
    if index is not None:
        index[message.id] = message
    assert state._get_message(message.id) is message
    return message


# --- Gateway events ------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_update_of_an_uncached_message_still_fires_the_raw_event():
    """An embed-only MESSAGE_UPDATE for a message outside the cache raised KeyError
    while building a Message, so on_raw_message_edit never fired."""
    client, state = await _client()
    seen = []

    @client.event
    async def on_raw_message_edit(payload):
        seen.append(payload)

    state.parse_message_update({'id': '4242', 'channel_id': str(CHANNEL_ID), 'guild_id': str(GUILD_ID), 'embeds': []})
    await asyncio.sleep(0)
    assert len(seen) == 1
    raw = seen[0]
    assert (raw.message_id, raw.channel_id, raw.guild_id) == (4242, CHANNEL_ID, GUILD_ID)
    assert raw.message is None and raw.cached_message is None
    await client.close()


def _reaction_event(user_id, *, burst=False, message_id=1000):
    return {
        'user_id': str(user_id),
        'channel_id': str(CHANNEL_ID),
        'guild_id': str(GUILD_ID),
        'message_id': str(message_id),
        'emoji': {'id': None, 'name': '\N{THUMBS UP SIGN}'},
        'burst': burst,
        'type': 1 if burst else 0,
    }


@pytest.mark.asyncio
async def test_super_reactions_are_counted_apart_from_normal_ones():
    """A super (burst) reaction was recorded as a normal one: ``me`` instead of
    ``me_burst``, and ``burst_count`` never moved."""
    client, state = await _client()
    message = _store_message(state)

    state.parse_message_reaction_add(_reaction_event(SELF_ID, burst=True))
    (reaction,) = message.reactions
    assert (reaction.me, reaction.me_burst) == (False, True)
    assert (reaction.count, reaction.normal_count, reaction.burst_count) == (1, 0, 1)

    state.parse_message_reaction_add(_reaction_event(SELF_ID))
    assert (reaction.me, reaction.me_burst) == (True, True)
    assert (reaction.count, reaction.normal_count, reaction.burst_count) == (2, 1, 1)

    # Removing the super reaction leaves our normal one in place
    state.parse_message_reaction_remove(_reaction_event(SELF_ID, burst=True))
    assert (reaction.me, reaction.me_burst) == (True, False)
    assert (reaction.count, reaction.normal_count, reaction.burst_count) == (1, 1, 0)
    await client.close()


def _poll_payload():
    return {
        'question': {'text': 'q'},
        'answers': [{'answer_id': 1, 'poll_media': {'text': 'a'}}, {'answer_id': 2, 'poll_media': {'text': 'b'}}],
        'expiry': '2030-01-01T00:00:00+00:00',
        'allow_multiselect': False,
        'layout_type': 1,
    }


@pytest.mark.asyncio
async def test_poll_self_voted_only_follows_our_own_votes():
    """Every vote event overwrote ``self_voted``: another user's vote cleared it,
    and removing our own vote set it."""
    client, state = await _client()
    message = _store_message(state, poll=_poll_payload())
    answer = message.poll.get_answer(1)

    state._update_poll_counts(message, 1, True, True)
    assert answer.self_voted is True and answer.vote_count == 1
    state._update_poll_counts(message, 1, True, False)  # someone else votes
    assert answer.self_voted is True and answer.vote_count == 2
    state._update_poll_counts(message, 1, False, False)  # and removes it
    assert answer.self_voted is True and answer.vote_count == 1
    state._update_poll_counts(message, 1, False, True)  # we remove ours
    assert answer.self_voted is False and answer.vote_count == 0
    state._update_poll_counts(message, 1, False, False)  # stray removal
    assert answer.vote_count == 0, 'the count never goes negative'
    await client.close()


def test_poll_copy_keeps_multiple_and_layout():
    poll = discord.Poll('q', datetime.timedelta(hours=1), multiple=True)
    copied = poll.copy()
    assert copied.multiple is True and copied.layout_type is poll.layout_type


@pytest.mark.asyncio
async def test_unavailable_call_stays_cached():
    """CALL_DELETE with ``unavailable`` popped the call, so the channel lost it
    and the CALL_CREATE that brings it back could not update it."""
    client, state = await _client()
    channel = state._get_private_channel(900)
    assert channel is not None
    state.parse_call_create({'channel_id': '900', 'message_id': '1', 'region': 'x', 'ringing': [], 'voice_states': []})
    call = state._calls[900]

    state.parse_call_delete({'channel_id': '900', 'unavailable': True})
    assert state._calls.get(900) is call and call.unavailable is True

    state.parse_call_delete({'channel_id': '900'})
    assert 900 not in state._calls
    await client.close()


def test_call_region_is_never_unset():
    from discord.calls import PrivateCall

    call = PrivateCall.__new__(PrivateCall)
    call.channel = types.SimpleNamespace(recipients=[], id=1)
    call._state = types.SimpleNamespace(get_user=lambda _id: None)
    try:
        call._update({'message_id': '1', 'channel_id': '1', 'ringing': []})  # no region
    except Exception:
        pass  # the stub state is minimal; only the slot matters here
    assert call.region is None


@pytest.mark.asyncio
async def test_thread_list_sync_keeps_the_thread_members():
    """The members of a THREAD_LIST_SYNC were looked up in a dict that the loop
    above had just emptied, so every one of them was dropped."""
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)

    def thread(thread_id):
        return {
            'id': str(thread_id),
            'guild_id': str(GUILD_ID),
            'parent_id': str(CHANNEL_ID),
            'owner_id': '100',
            'name': 't',
            'type': 11,
            'message_count': 0,
            'member_count': 1,
            'thread_metadata': {
                'archived': False,
                'auto_archive_duration': 60,
                'archive_timestamp': '2024-01-01T00:00:00+00:00',
            },
        }

    def member(thread_id, user_id):
        return {'id': str(thread_id), 'user_id': str(user_id), 'join_timestamp': '2024-01-01T00:00:00+00:00', 'flags': 0}

    state.parse_thread_list_sync({'guild_id': str(GUILD_ID), 'threads': [thread(700)], 'members': [member(700, 100)]})
    assert 100 in guild.get_thread(700)._members, 'a new thread keeps its member'

    # The same for a thread that is already cached
    guild.get_thread(700)._members.clear()
    state.parse_thread_list_sync({'guild_id': str(GUILD_ID), 'threads': [thread(700)], 'members': [member(700, 101)]})
    assert 101 in guild.get_thread(700)._members
    await client.close()


@pytest.mark.asyncio
async def test_member_update_tracks_the_avatar_decoration():
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    member = guild.get_member(100)
    decoration = {'asset': 'a_1', 'sku_id': '9'}
    old = member._update({'user': up(100), 'roles': [], 'avatar_decoration_data': decoration})
    assert old is not None and old._avatar_decoration_data is None
    assert member._avatar_decoration_data == decoration
    # A payload without the key changes nothing
    assert member._update({'user': up(100), 'roles': []}) is None
    assert member._avatar_decoration_data == decoration
    await client.close()


# --- State and lifecycle ------------------------------------------------------


@pytest.mark.asyncio
async def test_own_status_and_activities_before_connect():
    """``guild.me.status`` and ``.activities`` read ``state.ws`` as a getattr default,
    which is evaluated eagerly and is None until the client connects."""
    client, state = await _client()
    state._sessions.clear()
    assert client.ws is None
    presence = state.client_presence
    assert str(presence.client_status.status) == 'unknown'
    assert presence.activities == ()
    await client.close()


@pytest.mark.asyncio
async def test_stalled_chunk_request_is_replaced():
    """A chunk request whose reply never came stayed registered, so every later
    Guild.chunk() joined it and waited forever."""
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    sent = []

    async def chunker(guild_ids, *, nonce=None, **kwargs):
        sent.append(nonce)

    state.chunker = chunker
    first = await state.chunk_guild(guild, wait=False)
    stale = state._chunk_requests[guild.id]
    waiter = asyncio.ensure_future(stale.wait())
    await asyncio.sleep(0)

    # Still fresh: a second caller joins it
    await state.chunk_guild(guild, wait=False)
    assert state._chunk_requests[guild.id] is stale and len(sent) == 1

    stale.last_activity -= 3600
    await state.chunk_guild(guild, wait=False)
    assert state._chunk_requests[guild.id] is not stale and len(sent) == 2
    with pytest.raises(discord.ClientException):
        await waiter
    first.cancel()
    await client.close()


@pytest.mark.asyncio
async def test_old_member_sidebar_leaves_a_newer_registration_alone():
    client, state = await _client()
    guild = state._get_guild(GUILD_ID)
    from discord.state import MemberSidebar

    old = MemberSidebar.__new__(MemberSidebar)
    old.guild, old.waiters, old.exception, old.buffer = guild, [], None, []
    newer = object()
    state._scrape_requests[guild.id] = newer
    old.done()
    assert state._scrape_requests.get(guild.id) is newer
    del state._scrape_requests[guild.id]
    await client.close()


def test_request_guilds_warns_and_still_works():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        client = discord.Client(request_guilds=False)
    assert any(issubclass(w.category, DeprecationWarning) and 'request_guilds' in str(w.message) for w in caught)
    assert client._connection._subscribe_guilds is False
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        discord.Client(guild_subscriptions=False)  # the replacement does not warn


# --- HTTP ----------------------------------------------------------------------


def _recording_http():
    http = discord.Client().http
    calls = []

    def request(route, **kwargs):
        calls.append((route, kwargs))
        return None

    http.request = request  # type: ignore
    return http, calls


def test_entitlement_filters_are_sent():
    """The query was passed to Route() instead of request(), so ``exclude_consumed``
    and ``sku_ids`` never left the client."""
    http, calls = _recording_http()
    http.get_user_app_entitlements(1, sku_ids=[2], exclude_consumed=True)
    route, kwargs = calls[0]
    assert kwargs['params']['sku_ids'] == [2] and 'exclude_consumed' in kwargs['params']


def test_validation_error_closes_owned_files(tmp_path):
    path = tmp_path / 'f.txt'
    path.write_bytes(b'x')
    files = [discord.File(str(path)) for _ in range(11)]
    with pytest.raises(ValueError):
        handle_message_parameters(files=files)
    assert all(f.fp.closed for f in files)
    # A stream the caller opened is still the caller's to close
    stream = io.BytesIO(b'x')
    with pytest.raises(ValueError):
        handle_message_parameters(files=[discord.File(stream, 'a.txt') for _ in range(11)])
    assert not stream.closed


def test_components_v2_does_not_modify_the_callers_flags():
    from discord.components import TextDisplay

    component = TextDisplay({'type': 10, 'content': 'x'})  # type: ignore
    flags = discord.MessageFlags()
    with handle_message_parameters(components=[component], flags=flags) as params:
        assert params.payload['flags'] & discord.MessageFlags(components_v2=True).value
    assert flags.value == 0
    # Without flags it raised NameError: the class was imported for annotations only
    with handle_message_parameters(components=[component]) as params:
        assert params.payload['flags'] == discord.MessageFlags(components_v2=True).value


def test_billing_address_is_redacted_in_logs():
    body = '{"billing_address":{"name":"Ann Lee","line_1":"1 Main St"},"bank":"DE00"}'
    redacted = utils._redact(body)
    assert 'Ann Lee' not in redacted and 'Main St' not in redacted and 'DE00' not in redacted
    assert utils._redact({'billing_address': {'name': 'Ann'}})['billing_address'] == '[redacted]'


def test_unparsable_rate_limit_headers_do_not_raise():
    request = types.SimpleNamespace(headers={'X-Ratelimit-Reset-After': 'abc'})
    assert utils._parse_ratelimit_header(request) == 0.0
    request = types.SimpleNamespace(headers={'X-Ratelimit-Reset': 'abc'})
    assert utils._parse_ratelimit_header(request, use_clock=True) == 0.0


@pytest.mark.asyncio
async def test_info_api_answer_without_a_user_agent_is_not_used():
    """Every request reads the user agent from the super properties; an answer
    without one used to be accepted and then failed login with KeyError."""
    from test_upstream_triage import _PROPERTIES, _RecordingSession

    properties = {k: v for k, v in _PROPERTIES['properties'].items() if k != 'browser_user_agent'}
    session = _RecordingSession({**_PROPERTIES, 'properties': properties})
    ctx = await discord.HeadersContext.default(session)  # type: ignore
    assert isinstance(ctx.user_agent, str) and ctx.user_agent


# --- Channels, messages, models -----------------------------------------------


@pytest.mark.asyncio
async def test_channel_edit_with_overwrites_sends_a_serialisable_payload():
    """The ``overwrites`` mapping (keyed by Role/Member) was sent along with the
    payload built from it, and failed to serialise."""
    client, state = await _client()
    channel = state.get_channel(CHANNEL_ID)
    sent = {}

    async def edit_channel(channel_id, *, reason=None, **options):
        sent.update(options)
        utils._to_json(options)
        return None

    state.http.edit_channel = edit_channel  # type: ignore
    role = channel.guild.default_role
    await channel._edit({'overwrites': {role: discord.PermissionOverwrite(send_messages=False)}}, reason=None)
    assert 'overwrites' not in sent
    assert sent['permission_overwrites'][0]['id'] == role.id
    await client.close()


@pytest.mark.asyncio
async def test_pins_oldest_first_is_ordered_across_pages():
    client, state = await _client()
    channel = state.get_channel(CHANNEL_ID)
    stamps = [f'2024-01-{day:02d}T00:00:00+00:00' for day in range(1, 29)]
    items = [
        {'pinned_at': stamp, 'message': msg_payload(id=str(2000 + i), channel_id=str(CHANNEL_ID))}
        for i, stamp in enumerate(stamps)
    ]
    newest_first = items[::-1]

    async def pins_from(*, channel_id, limit, before=None):
        pool = [i for i in newest_first if before is None or i['pinned_at'] < before]
        page = pool[:50] if len(pool) <= 50 else pool[:50]
        return {'items': page, 'has_more': len(pool) > len(page)}

    # Two pages of the real page size
    many = [
        {
            'pinned_at': f'2024-01-01T00:{m:02d}:{s:02d}+00:00',
            'message': msg_payload(id=str(3000 + m * 60 + s), channel_id=str(CHANNEL_ID)),
        }
        for m in range(2)
        for s in range(40)
    ]
    newest_first = many[::-1]
    state.http.pins_from = pins_from  # type: ignore
    got = [m.id async for m in channel.pins(limit=None, oldest_first=True)]
    assert got == [int(i['message']['id']) for i in many]
    newest = [m.id async for m in channel.pins(limit=None)]
    assert newest == got[::-1]
    await client.close()


@pytest.mark.asyncio
async def test_failed_voice_connect_unregisters_the_client():
    """Only a timeout cleaned up; any other failure (or a cancellation) left the
    voice client registered and every later connect() raised "Already connected"."""
    client, state = await _client()

    class Failing(discord.VoiceProtocol):
        async def connect(self, **kwargs):
            raise RuntimeError('handshake failed')

        async def disconnect(self, *, force=False):
            raise RuntimeError('nothing to disconnect')

    channel = types.SimpleNamespace(id=1, guild=state._get_guild(GUILD_ID))

    class Target(discord.abc.Connectable):
        _state = state

        def _get_voice_client_key(self):
            return 77, 'guild_id'

        def _get_voice_state_pair(self):
            return 77, 1

        async def _get_channel(self):
            return channel

    with pytest.raises(RuntimeError, match='handshake failed'):
        await Target().connect(cls=Failing)
    assert not state._has_voice_client()
    await client.close()


def test_embed_set_field_at_leaves_the_source_payload_alone():
    data = {'fields': [{'name': 'a', 'value': 'b', 'inline': True}]}
    embed = discord.Embed.from_dict(data)
    embed.set_field_at(0, name='x', value='y')
    assert data['fields'][0] == {'name': 'a', 'value': 'b', 'inline': True}
    assert embed.fields[0].name == 'x'


def test_scheduled_event_privacy_level_and_object_channel():
    from discord.scheduled_event import ScheduledEvent

    data = {
        'id': '1',
        'guild_id': '1',
        'name': 'e',
        'entity_type': 2,
        'scheduled_start_time': '2030-01-01T00:00:00+00:00',
        'privacy_level': 2,
        'status': 3,  # was read as the privacy level
    }
    event = ScheduledEvent(state=types.SimpleNamespace(store_user=lambda u: u), data=data)  # type: ignore
    assert event.privacy_level is discord.PrivacyLevel.guild_only and event.status is discord.EventStatus.completed


@pytest.mark.asyncio
async def test_scheduled_event_edit_accepts_an_object_channel():
    """``Object.type is VoiceChannel`` named a class imported for type checking
    only, so it raised NameError."""
    from discord.scheduled_event import ScheduledEvent

    event = ScheduledEvent.__new__(ScheduledEvent)
    event.id, event.guild_id, event.entity_type = 1, 1, discord.EntityType.voice
    sent = {}

    async def edit_scheduled_event(guild_id, event_id, *, reason=None, **payload):
        sent.update(payload)
        raise RuntimeError('stop')

    event._state = types.SimpleNamespace(http=types.SimpleNamespace(edit_scheduled_event=edit_scheduled_event))  # type: ignore
    with pytest.raises(RuntimeError, match='stop'):
        await event.edit(channel=discord.Object(id=5, type=discord.StageChannel))
    assert sent['channel_id'] == 5


def test_read_state_of_a_guild_without_events():
    from discord.read_state import ReadState

    read_state = ReadState.__new__(ReadState)
    read_state._last_entity_id = None
    read_state.type = discord.ReadStateType.scheduled_events
    read_state.id = 1
    guild = types.SimpleNamespace(scheduled_events=[])
    read_state._state = types.SimpleNamespace(_get_or_create_unavailable_guild=lambda _id: guild)  # type: ignore
    assert read_state.last_entity_id == 0


def test_friend_suggestion_is_hashable():
    from discord.relationship import FriendSuggestion

    suggestion = FriendSuggestion.__new__(FriendSuggestion)
    suggestion.user = types.SimpleNamespace(id=42)
    assert suggestion.id == 42 and hash(suggestion) == hash(suggestion) and suggestion == suggestion


def test_array_flags_support_the_bitwise_operators():
    """``|``, ``&``, ``^`` and ``~`` built the result with a constructor that takes
    a list of positions and raised TypeError."""
    from discord.flags import ApplicationCommandContext

    guild = ApplicationCommandContext(guild=True)
    dm = ApplicationCommandContext(dm_channel=True)
    both = guild | dm
    assert both.guild and both.dm_channel
    assert (both & guild) == guild and (both ^ guild) == dm
    assert not (~guild).guild
    assert ApplicationCommandContext._from_value([0, 1]) == both, 'the list constructor is unchanged'


def test_permission_overwrite_stores_real_booleans():
    overwrite = discord.PermissionOverwrite(send_messages=1, read_messages=0)
    allow, deny = overwrite.pair()
    assert allow.send_messages and deny.read_messages


def test_sequence_proxy_copies_once():
    """The cached copy was stored under a name-mangled attribute nothing read, so
    every index, iteration and membership test copied the collection again."""
    proxy = utils.SequenceProxy({i: i for i in range(10)}.values())
    assert proxy[3] == 3 and 4 in proxy
    first = proxy._SequenceProxy__copied  # type: ignore
    assert proxy._SequenceProxy__copied is first  # type: ignore
    assert list(utils.SequenceProxy([3, 1, 2], sorted=True)) == [1, 2, 3]


def test_activities_are_hashable_and_copyable():
    activity = discord.Activity(name='x', type=discord.ActivityType.playing)
    assert hash(activity) == hash(discord.Activity(name='x', type=discord.ActivityType.playing))
    assert activity in {activity}
    state = object()
    custom = discord.CustomActivity.from_dict({'state': 'hi', 'type': 4, 'name': 'Custom Status'}, state=state)  # type: ignore
    assert custom._state is state
    hang = discord.HangActivity.from_dict({'state': 'gaming', 'type': 6, 'name': 'Hang Status'}, state=state)  # type: ignore
    assert hang._state is state


def test_store_and_billing_fixes():
    from discord.billing import BillingAddress
    from discord.store import SKUPrice

    address = BillingAddress(name='n', address='a', city='c', country='US')
    assert hash(address) == hash(BillingAddress(name='n', address='a', city='c', country='US'))

    price = SKUPrice.__new__(SKUPrice)
    price.amount, price.sale_amount = 999, 0
    assert price.discounts == 999
    price.sale_amount = None
    assert price.discounts == 0


def test_install_url_prefers_the_custom_url_without_install_params():
    from discord.application import PartialApplication

    app = PartialApplication.__new__(PartialApplication)
    app.custom_install_url, app.install_params = 'https://example.invalid/install', None
    assert app.install_url == 'https://example.invalid/install'
    app.custom_install_url = None
    assert app.install_url is None


@pytest.mark.asyncio
async def test_plan_preview_uses_the_sku_id():
    from discord.store import SubscriptionPlan

    plan = SubscriptionPlan.__new__(SubscriptionPlan)
    plan.id, plan.sku_id = 111, 222
    seen = []

    async def preview_sku_purchase(*args, **kwargs):
        seen.append(args)
        raise RuntimeError('stop')

    plan._state = types.SimpleNamespace(http=types.SimpleNamespace(preview_sku_purchase=preview_sku_purchase))  # type: ignore
    with pytest.raises(RuntimeError, match='stop'):
        await plan.preview_purchase()
    assert seen[0][0] == 222 and seen[0][2] == 111


def test_connection_keeps_the_access_token_from_its_payload():
    from discord.connections import Connection

    data = {'id': '1', 'name': 'n', 'type': 'twitch', 'verified': True, 'access_token': 'value-from-payload'}
    connection = Connection(data=data, state=types.SimpleNamespace())  # type: ignore
    assert connection.access_token == 'value-from-payload'


def test_sync_webhook_reports_a_server_error_after_the_last_attempt(monkeypatch):
    """``if response:`` is False for a requests.Response with an error status, so
    five 503s ended in RuntimeError('Unreachable code') instead of DiscordServerError."""
    from discord.webhook import sync as sync_webhook

    class Response:
        status_code = 503
        text = 'unavailable'
        headers = {'Content-Type': 'text/plain'}
        reason = 'Service Unavailable'

        def __bool__(self):
            return False  # what requests.Response does for a status >= 400

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Session:
        def request(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(sync_webhook.time, 'sleep', lambda _s: None)
    adapter = sync_webhook.WebhookAdapter()
    route = sync_webhook.Route('GET', '/webhooks/{webhook_id}', webhook_id=1)
    with pytest.raises(discord.DiscordServerError):
        adapter.request(route, Session())  # type: ignore


# --- Audio ---------------------------------------------------------------------


def test_volume_transformer_exposes_the_source_error():
    class Source(discord.AudioSource):
        _current_error = RuntimeError('ffmpeg exited with 1')

        def read(self):
            return b''

    wrapped = discord.PCMVolumeTransformer(Source())
    assert getattr(wrapped, '_current_error', None) is Source._current_error


def test_ffmpeg_source_that_failed_to_construct_is_collected_quietly():
    from discord.player import FFmpegAudio

    source = FFmpegAudio.__new__(FFmpegAudio)
    source._check_process_returncode()  # raised AttributeError, reported from __del__


# --- Extensions ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_concurrency_releases_the_slot_it_took():
    """The bucket key comes from live state (the author's top role); when it
    changed while the command ran, the release missed and the slot leaked."""
    from discord.ext.commands.cooldowns import MaxConcurrency

    concurrency = MaxConcurrency(1, per=commands.BucketType.role, wait=False)
    author = types.SimpleNamespace(top_role=types.SimpleNamespace(id=1))
    message = types.SimpleNamespace(id=10, author=author, channel=types.SimpleNamespace(id=2))
    ctx = types.SimpleNamespace(message=message, author=author, channel=message.channel)

    await concurrency.acquire(ctx)  # type: ignore
    author.top_role = types.SimpleNamespace(id=99)  # promoted mid-command
    await concurrency.release(message)  # type: ignore
    assert not concurrency._mapping and not concurrency._held

    author.top_role = types.SimpleNamespace(id=1)
    await concurrency.acquire(ctx)  # type: ignore # raised MaxConcurrencyReached
    await concurrency.release(ctx)  # type: ignore


def test_flag_value_touching_the_next_flag_is_not_truncated():
    class Flags(commands.FlagConverter, prefix='-', delimiter=':'):
        name: str
        age: int

    assert Flags.parse_flags('-name:foo-age:3') == {'name': ['foo'], 'age': ['3']}  # was ['fo']
    assert Flags.parse_flags('-name:foo -age:3') == {'name': ['foo'], 'age': ['3']}
    assert Flags.parse_flags('-name:foo  -age:3') == {'name': ['foo '], 'age': ['3']}, 'only one separator is dropped'


def test_loop_instances_inherit_added_exception_types():
    class Custom(Exception):
        pass

    class Holder:
        @tasks.loop(seconds=1)
        async def job(self):
            pass

    Holder.job.add_exception_type(Custom)
    assert Custom in Holder().job._valid_exception


def test_command_check_order_survives_copies():
    def first(ctx):
        return True

    def second(ctx):
        return True

    @commands.check(first)
    @commands.check(second)
    @commands.command()
    async def probe(ctx):
        pass

    @commands.check(first)
    @commands.check(second)
    async def callback(ctx):
        pass

    command = commands.Command(callback)
    order = [check.__name__ for check in command.checks]
    for _ in range(3):
        assert [check.__name__ for check in command.copy().checks] == order
    assert [check.__name__ for check in commands.Command(callback).checks] == order


# --- Gateway -------------------------------------------------------------------


class _Socket:
    closed = False

    async def close(self, *args, **kwargs):
        self.closed = True


def _bare_websocket():
    from discord.gateway import DiscordWebSocket

    ws = DiscordWebSocket.__new__(DiscordWebSocket)
    ws.socket = _Socket()
    ws._keep_alive = None
    ws._dispatch = lambda *args, **kwargs: None
    ws.log_receive = lambda _msg: None
    ws._decompressor = None
    ws.sequence = None
    ws.session_id = None
    ws._close_code = None
    return ws


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'frame, resume',
    [
        ('{"op":10,"d":{}}', True),  # HELLO without heartbeat_interval
        ('{"op":10,"d":null}', True),
        ('{"op":0,"t":"READY","s":1,"d":{"resume_gateway_url":"wss://x.invalid"}}', False),  # no session_id
    ],
)
async def test_unusable_hello_or_ready_reconnects(frame, resume, caplog):
    """These raised KeyError/AttributeError out of the receive loop, which is not
    a reconnectable error, so the client stopped."""
    from discord.gateway import ReconnectWebSocket

    ws = _bare_websocket()
    with caplog.at_level(logging.WARNING, logger='discord.gateway'):
        with pytest.raises(ReconnectWebSocket) as info:
            await ws.received_message(frame)
    assert info.value.resume is resume
    assert ws.socket.closed
    assert 'unusable' in caplog.text


def test_stage_instance_from_an_invite_with_members(monkeypatch):
    """``Member`` was imported for type checking only, so a stage invite that
    lists members raised NameError."""
    import discord.member
    from discord.stage_instance import StageInstance

    built = []
    monkeypatch.setattr(discord.member, 'Member', lambda **kwargs: built.append(kwargs) or kwargs['data']['user']['id'])
    guild = types.SimpleNamespace(id=1, _state=None)
    invite = types.SimpleNamespace(
        _state=types.SimpleNamespace(), guild=guild, channel=types.SimpleNamespace(id=2), code='abc', scheduled_event=None
    )
    data = {'topic': 't', 'participant_count': 1, 'speaker_count': 0, 'members': [{'user': up(100), 'roles': []}]}
    instance = StageInstance.from_invite(invite, data)  # type: ignore
    assert instance._members == ['100'] and len(built) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('send failed'), asyncio.CancelledError()])
async def test_failed_handshake_closes_its_socket(monkeypatch, error):
    """The socket belongs to nobody until IDENTIFY/RESUME has been sent; a
    failure or the connect timeout's cancellation left it open."""
    from discord.gateway import DiscordWebSocket
    from test_rapptz_ports import RESUME_URL, _FakeSocket, _ready_client

    client = await _ready_client()
    socket = _FakeSocket()

    async def ws_connect(url, **kwargs):
        return socket

    async def resume(self):
        raise error

    monkeypatch.setattr(client.http, 'ws_connect', ws_connect)
    monkeypatch.setattr(DiscordWebSocket, 'resume', resume)
    with pytest.raises(type(error)):
        await DiscordWebSocket.from_client(client, gateway=RESUME_URL, session='abc', sequence=5, resume=True)
    assert socket.close_calls == [4000], 'closed with a code that keeps the session resumable'
