"""
Regressions for the fork-owned internals refactor: guild subscription
batching must never drop a payload, gateway waiters must be cleaned up,
and rate-limit bucket sweeps must stay amortized.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from discord.state import GuildSubscriptions


class SlowWS:
    """bulk_guild_subscribe blocks until released, like a rate-limited send."""

    open = True

    def __init__(self) -> None:
        self.sent: list = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def bulk_guild_subscribe(self, payload) -> None:
        self.started.set()
        await self.release.wait()
        self.sent.append(payload)


def make_subscriptions(ws, monkeypatch) -> GuildSubscriptions:
    monkeypatch.setattr(GuildSubscriptions, 'TICK_TIMEOUT', 0.01)
    state = SimpleNamespace(ws=ws, loop=asyncio.get_running_loop())
    return GuildSubscriptions(state)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_subscribe_during_inflight_flush_is_not_dropped(monkeypatch):
    ws = SlowWS()
    subs = make_subscriptions(ws, monkeypatch)

    await subs.subscribe_to(SimpleNamespace(id=1), typing=True)  # type: ignore[arg-type]
    await asyncio.wait_for(ws.started.wait(), 1)  # first flush is now blocked mid-send

    # A subscribe arriving mid-send used to cancel the flushing task, losing guild 1
    await subs.subscribe_to(SimpleNamespace(id=2), typing=True)  # type: ignore[arg-type]
    ws.release.set()

    for _ in range(100):
        if len(ws.sent) == 2:
            break
        await asyncio.sleep(0.01)

    assert ws.sent == [{'1': {'typing': True}}, {'2': {'typing': True}}]
    assert 1 in subs._subscribed and 2 in subs._subscribed, 'local bookkeeping must reflect both sends'


@pytest.mark.asyncio
async def test_overflow_flush_merges_changes_queued_during_the_flush(monkeypatch):
    from discord import utils

    ws = SlowWS()
    ws.release.set()
    subs = make_subscriptions(ws, monkeypatch)
    changes = {'2': {'typing': True, 'threads': True}}
    queued_meanwhile = {'3': {'typing': True}}
    # Big enough for the merged result, too small for the original batch + changes
    monkeypatch.setattr(GuildSubscriptions, 'MAX_PAYLOAD_SIZE', len(utils._to_json({**queued_meanwhile, **changes})))
    subs._pending = {'1': {'typing': True, 'threads': True, 'activities': True}}  # type: ignore[typeddict-item]

    real_flush = GuildSubscriptions._flush
    fired = []

    async def flush_with_concurrent_add(self):
        await real_flush(self)
        if not fired:  # another subscriber queues something once, during the await
            fired.append(True)
            self._pending = dict(queued_meanwhile)

    monkeypatch.setattr(GuildSubscriptions, '_flush', flush_with_concurrent_add)
    await subs._checked_add(changes)  # type: ignore[arg-type]

    assert ws.sent == [{'1': {'typing': True, 'threads': True, 'activities': True}}]
    assert subs._pending == {**queued_meanwhile, **changes}, 'neither batch may be overwritten'
    subs._cancel()


@pytest.mark.asyncio
async def test_tick_before_gateway_connects_retries_instead_of_crashing(monkeypatch):
    subs = make_subscriptions(None, monkeypatch)
    subs._pending = {'1': {'typing': True}}  # type: ignore[typeddict-item]
    subs._tick()
    first = subs._task
    await asyncio.sleep(0.05)
    assert first is not None and first.done() and first.exception() is None
    assert subs._task is not None and not subs._task.done(), 'it keeps re-ticking until the ws is up'
    subs._cancel()


def _gateway_ws():
    from discord import gateway

    ws = gateway.DiscordWebSocket.__new__(gateway.DiscordWebSocket)
    ws.loop = asyncio.get_running_loop()
    ws._dispatch_listeners = []
    return ws


@pytest.mark.asyncio
async def test_gateway_wait_for_timeout_removes_listener():
    ws = _gateway_ws()
    future = ws.wait_for('THREAD_MEMBER_LIST_UPDATE', lambda d: True)
    assert len(ws._dispatch_listeners) == 1

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(future, timeout=0.01)
    await asyncio.sleep(0)  # done callbacks run on the next loop iteration

    assert ws._dispatch_listeners == [], 'a timed-out waiter must not linger'


@pytest.mark.asyncio
async def test_bulk_delete_removes_in_one_pass_and_keeps_deque():
    from collections import deque

    from discord.state import ConnectionState

    state = ConnectionState.__new__(ConnectionState)
    state._messages = deque(maxlen=10)
    state._messages_index = {}
    state._call_message_cache = {}

    msgs = [SimpleNamespace(id=i) for i in range(10)]
    for m in msgs:
        state._add_message_to_cache(m)  # type: ignore[arg-type]
    state._call_message_cache[3] = msgs[3]
    original = state._messages

    found = [msgs[1], msgs[3], msgs[7]]
    state._remove_messages_from_cache(found, {1, 3, 7, 999})  # type: ignore[arg-type]

    assert state._messages is original and original.maxlen == 10
    assert [m.id for m in state._messages] == [0, 2, 4, 5, 6, 8, 9]
    assert sorted(state._messages_index) == [0, 2, 4, 5, 6, 8, 9]
    assert 3 not in state._call_message_cache


@pytest.mark.parametrize('use_orjson', [False, True])
def test_incremental_subscription_size_matches_full_encoding(monkeypatch, use_orjson):
    import json
    import random

    from discord import utils

    if use_orjson:
        orjson = pytest.importorskip('orjson')
        monkeypatch.setattr(utils, '_to_json', lambda obj: orjson.dumps(obj).decode('utf-8'))
    else:
        monkeypatch.setattr(utils, '_to_json', lambda obj: json.dumps(obj, separators=(',', ':'), ensure_ascii=True))

    rng = random.Random(7)
    sizes: dict = {}
    payload: dict = {}
    for _ in range(300):
        key = str(rng.randrange(10**17, 10**17 + 40))
        value = {
            'typing': rng.random() < 0.5,
            'members': [str(rng.randrange(10**17, 10**18)) for _ in range(rng.randrange(0, 6))],
            'channels': {str(rng.randrange(10**17, 10**18)): [[0, 99]]} if rng.random() < 0.5 else {},
        }
        payload[key] = value
        sizes[key] = GuildSubscriptions._entry_size(key, value)
        assert GuildSubscriptions._encoded_size(sizes) == len(utils._to_json(payload))
    assert GuildSubscriptions._encoded_size({}) == len(utils._to_json({}))


@pytest.mark.asyncio
async def test_bucket_sweep_is_throttled_but_still_evicts(monkeypatch):
    import discord.http as http_module
    from traffic_harness import make_http

    http = make_http(None)
    clock = [1000.0]
    monkeypatch.setattr(http_module.time, 'monotonic', lambda: clock[0])
    scans = []
    real_inactive = http_module.Ratelimit.is_inactive

    def counting_inactive(self):
        scans.append(1)
        return real_inactive(self)

    monkeypatch.setattr(http_module.Ratelimit, 'is_inactive', counting_inactive)

    for i in range(400):
        http.get_ratelimit(f'route:{i}')
    # One sweep when the table reached 256, then none until the interval passes
    assert len(scans) == 256, len(scans)

    # Age every bucket past the 300s idle threshold, then pass the interval
    for bucket in http._buckets.values():
        bucket._last_request -= 301
    clock[0] += http.BUCKET_SWEEP_INTERVAL
    http.get_ratelimit('route:new')
    assert list(http._buckets) == ['route:new'], 'idle buckets are still evicted'


@pytest.mark.asyncio
async def test_dispatch_handler_add_replace_remove_and_none():
    import discord

    client = discord.Client()
    await client._async_setup_hook()
    calls = []

    async def first(*args):
        calls.append('first')

    async def second(*args):
        calls.append('second')

    client.dispatch('thing')  # no handler yet: a no-op
    client.on_thing = first  # type: ignore[attr-defined]
    client.dispatch('thing')
    client.on_thing = second  # type: ignore[attr-defined]
    client.dispatch('thing')
    client.on_thing = None  # type: ignore[attr-defined]
    client.dispatch('thing')  # used to cache None and crash scheduling it
    del client.on_thing  # type: ignore[attr-defined]
    client.dispatch('thing')
    await asyncio.sleep(0.05)

    assert calls == ['first', 'second']
    await client.close()


async def _presence_env():
    import discord
    from discord import Client, Guild

    client = Client()
    await client._async_setup_hook()
    state = client._connection
    guild = Guild(state=state, data={'id': '1', 'roles': [{'id': '1', 'name': '@everyone'}], 'member_count': 5})
    state._guilds[1] = guild
    user = {'id': '7', 'username': 'u', 'discriminator': '0001', 'global_name': None, 'avatar': None}
    guild._add_member(
        discord.Member(data={'user': user, 'roles': [], 'joined_at': '2023-01-01T00:00:00+00:00'}, guild=guild, state=state)
    )
    return client, state, guild


def _presence(status):
    return {'user': {'id': '7'}, 'status': status, 'activities': [], 'client_status': {'desktop': status}}


@pytest.mark.asyncio
async def test_presence_cache_updates_without_listener():
    import discord

    client, state, guild = await _presence_env()
    state._handle_presence_update(guild, _presence('online'))
    state._handle_presence_update(guild, _presence('dnd'))
    assert guild.get_member(7).status is discord.Status.dnd
    await client.close()


@pytest.mark.asyncio
async def test_presence_wait_for_only_listener_gets_before_and_after():
    import discord

    client, state, guild = await _presence_env()
    state._handle_presence_update(guild, _presence('online'))

    waiter = asyncio.ensure_future(client.wait_for('presence_update', timeout=1))
    await asyncio.sleep(0)
    state._handle_presence_update(guild, _presence('idle'))
    before, after = await waiter

    assert before.status is discord.Status.online and after.status is discord.Status.idle
    await client.close()


@pytest.mark.asyncio
async def test_webhook_lock_released_when_cancelled_during_deferral():
    from discord.webhook.async_ import AsyncDeferredLock

    lock = asyncio.Lock()

    async def holder():
        async with AsyncDeferredLock(lock) as deferred:
            deferred.delay_by(30)  # bucket exhausted: defer the release

    task = asyncio.ensure_future(holder())
    await asyncio.sleep(0.01)
    assert lock.locked()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked(), 'later senders to this webhook would hang forever'


def test_sync_webhook_lock_released_when_interrupted(monkeypatch):
    import threading

    import discord.webhook.sync as sync_module

    def interrupted_sleep(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(sync_module.time, 'sleep', interrupted_sleep)
    lock = threading.Lock()
    with pytest.raises(KeyboardInterrupt):
        with sync_module.DeferredLock(lock) as deferred:
            deferred.delay_by(5)
    assert not lock.locked()


def test_connection_reset_errnos_cover_this_platform():
    import errno

    from discord import utils

    assert errno.ECONNRESET in utils._CONNECTION_RESET_ERRNOS  # 104 on Linux
    assert {54, 10054} <= utils._CONNECTION_RESET_ERRNOS


def test_ratelimit_header_without_reset_fields_does_not_raise():
    from discord import utils

    response = SimpleNamespace(headers={'X-Ratelimit-Remaining': '0'})
    assert utils._parse_ratelimit_header(response) == 0.0
    assert utils._parse_ratelimit_header(SimpleNamespace(headers={'X-Ratelimit-Reset-After': '1.5'})) == 1.5


@pytest.mark.asyncio
async def test_webhook_gives_up_without_a_final_sleep(monkeypatch):
    import discord.webhook.async_ as async_module
    from discord.errors import DiscordServerError
    from discord.http import Route

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(async_module.asyncio, 'sleep', fake_sleep)

    class Response:
        status = 503
        headers: dict = {}
        reason = 'Service Unavailable'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def text(self):
            return 'down'

        async def read(self):
            return b'down'

    class Session:
        def request(self, *args, **kwargs):
            return Response()

    adapter = async_module.AsyncWebhookAdapter()
    route = Route('POST', '/webhooks/{webhook_id}/{webhook_token}', webhook_id=1, webhook_token='t')
    with pytest.raises(DiscordServerError):
        await adapter.request(route, session=Session())  # type: ignore[arg-type]

    assert len(sleeps) == 4, f'5 attempts need 4 backoffs, not 5: {sleeps}'


@pytest.mark.asyncio
async def test_max_concurrency_wait_never_exceeds_limit():
    from discord.ext.commands import BucketType
    from discord.ext.commands.cooldowns import MaxConcurrency

    mc = MaxConcurrency(1, per=BucketType.default, wait=True)
    running = 0
    peak = 0

    async def job(release_first: asyncio.Event | None = None):
        nonlocal running, peak
        await mc.acquire(None)  # type: ignore[arg-type]
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        await mc.release(None)  # type: ignore[arg-type]

    await mc.acquire(None)  # A holds the slot
    b = asyncio.ensure_future(job())  # B waits
    await asyncio.sleep(0)
    await mc.release(None)  # A releases: B is woken but has not resumed yet
    c = asyncio.ensure_future(job())  # C arrives in that window
    await asyncio.gather(b, c)

    assert peak == 1, 'two holders ran at once with max_concurrency(1)'
    assert mc._mapping == {}, 'every slot returned and the entry cleaned up'


@pytest.mark.asyncio
async def test_max_concurrency_cancelled_woken_waiter_passes_slot_on():
    from discord.ext.commands import BucketType
    from discord.ext.commands.cooldowns import MaxConcurrency

    mc = MaxConcurrency(1, per=BucketType.default, wait=True)
    await mc.acquire(None)  # type: ignore[arg-type]
    b = asyncio.ensure_future(mc.acquire(None))  # type: ignore[arg-type]
    c = asyncio.ensure_future(mc.acquire(None))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    await mc.release(None)  # type: ignore[arg-type]  # wakes b
    b.cancel()  # cancelled after being woken, before resuming
    await asyncio.sleep(0)
    await asyncio.wait_for(c, 1)  # c must get the slot b gave up
    await mc.release(None)  # type: ignore[arg-type]
    assert mc._mapping == {}


def test_stringview_scan_equivalence():
    import random

    from discord.ext.commands.view import StringView

    rng = random.Random(3)
    alphabet = ['a', ' ', '\t', '\n', '　', '\x1c', 'é', '"']
    for _ in range(2000):
        text = ''.join(rng.choice(alphabet) for _ in range(rng.randrange(0, 12)))
        view = StringView(text)
        words = []
        while not view.eof:
            view.skip_ws()
            word = view.get_word()
            if word:
                words.append(word)
        assert words == text.split(), text


def test_audio_level_matches_reference():
    import math
    import random
    import struct

    from discord.voice_media import RTP_AUDIO_LEVEL_SILENCE as SILENCE, _audio_level_from_pcm

    def reference(data: bytes) -> int:  # the original per-sample implementation
        sample_bytes = len(data) & ~1
        if sample_bytes == 0:
            return SILENCE
        square_sum = count = 0
        for (sample,) in struct.iter_unpack('<h', data[:sample_bytes]):
            square_sum += sample * sample
            count += 1
        if square_sum == 0:
            return SILENCE
        ratio = min(math.sqrt(square_sum / count) / 32767.0, 1.0)
        if ratio <= 0:
            return SILENCE
        return min(SILENCE, max(0, round(-20.0 * math.log10(ratio))))

    rng = random.Random(5)
    for _ in range(500):
        size = rng.choice([0, 1, 2, 3, 100, 3840, 3841])
        amp = rng.choice([0, 1, 50, 3000, 32767])
        data = (
            bytes(rng.randrange(256) for _ in range(size))
            if amp == 32767
            else b''.join(struct.pack('<h', rng.randint(-amp, amp)) for _ in range(size // 2))
        )
        assert _audio_level_from_pcm(data) == reference(data)


def test_opus_roundtrip_bytes_unchanged():
    import array
    import ctypes

    from discord import opus

    try:
        opus._load_default()
    except Exception:
        pass
    if not opus.is_loaded():
        pytest.skip('libopus not available')

    encoder = opus.Encoder()
    decoder = opus.Decoder()
    pcm = bytes(range(256)) * (encoder.FRAME_SIZE * 4 // 256)  # one 20 ms stereo frame
    packet = encoder.encode(pcm, encoder.SAMPLES_PER_FRAME)
    assert type(packet) is bytes and 0 < len(packet) < len(pcm)

    decoded = decoder.decode(packet)
    assert type(decoded) is bytes and len(decoded) == encoder.FRAME_SIZE
    # Same bytes the old list -> array('h') -> tobytes() route produced
    as_shorts = array.array('h', decoded)
    assert array.array('h', list(as_shorts)).tobytes() == decoded
    assert ctypes.sizeof(ctypes.c_int16) == 2


def test_play_while_paused_stops_the_parked_player(monkeypatch):
    import discord.voice_client as vc_module
    from discord.player import AudioSource

    stopped = []

    class ParkedPlayer:
        def stop(self):
            stopped.append(True)

    class FakePlayer:
        def __init__(self, source, client, *, after=None):
            self.source = source

        def start(self):
            pass

    class OpusSource(AudioSource):
        def read(self):
            return b''

        def is_opus(self):
            return True

    monkeypatch.setattr(vc_module, 'AudioPlayer', FakePlayer)
    client = vc_module.VoiceClient.__new__(vc_module.VoiceClient)
    client._player = ParkedPlayer()  # paused: is_playing() is False
    monkeypatch.setattr(vc_module.VoiceClient, 'is_connected', lambda self: True)
    monkeypatch.setattr(vc_module.VoiceClient, 'is_playing', lambda self: False)

    client.play(OpusSource())

    assert stopped == [True], 'the paused player thread would be orphaned'
    assert isinstance(client._player, FakePlayer)


@pytest.mark.asyncio
async def test_concurrent_ratelimit_refreshes_sleep_the_window_once():
    from discord.http import Ratelimit

    bucket = Ratelimit(None, 1)
    loop = asyncio.get_running_loop()
    bucket.remaining = 0
    bucket.reset_after = 0.05
    bucket.expires = loop.time() + bucket.reset_after
    resets = []
    real_reset = Ratelimit.reset

    def counting_reset(self):
        resets.append(1)
        real_reset(self)

    Ratelimit.reset = counting_reset  # type: ignore[method-assign]
    try:
        start = loop.time()
        await asyncio.gather(*(bucket._refresh() for _ in range(5)))
        elapsed = loop.time() - start
    finally:
        Ratelimit.reset = real_reset  # type: ignore[method-assign]

    assert len(resets) == 1, 'queued refreshes must not re-reset an already refreshed bucket'
    assert elapsed < 0.05 * 2, f'5 queued refreshes slept {elapsed:.2f}s (stale windows re-slept)'


@pytest.mark.asyncio
async def test_voice_poller_logs_and_disconnects_on_unexpected_error(monkeypatch, caplog):
    import logging

    from discord.voice_state import ConnectionFlowState, VoiceConnectionState

    conn = VoiceConnectionState.__new__(VoiceConnectionState)
    conn._state = ConnectionFlowState.connected
    disconnects = []

    async def failing_loop(self, reconnect):
        raise RuntimeError('voice server returned garbage')

    async def fake_disconnect(self, *args, **kwargs):
        disconnects.append(True)

    monkeypatch.setattr(VoiceConnectionState, '_poll_voice_ws_loop', failing_loop)
    monkeypatch.setattr(VoiceConnectionState, 'disconnect', fake_disconnect)

    with caplog.at_level(logging.ERROR, logger='discord.voice_state'):
        await conn._poll_voice_ws(True)  # must not raise out of the (never awaited) task

    assert disconnects == [True]
    assert any('poller stopped on an unexpected error' in r.getMessage() for r in caplog.records)


def test_nonces_are_unique_within_a_millisecond():
    from discord import utils

    nonces = [utils._generate_nonce() for _ in range(5000)]
    assert len(set(nonces)) == len(nonces), 'nonces made in the same ms used to collide'
    # Still snowflake-shaped: the timestamp part decodes to now
    created = utils.snowflake_time(int(nonces[-1]))
    assert abs((utils.utcnow() - created).total_seconds()) < 5


@pytest.mark.asyncio
async def test_interaction_create_pops_its_nonce_context():
    import discord

    client = discord.Client()
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(
        state=state, data={'id': '5', 'username': 'me', 'discriminator': '0', 'global_name': None, 'avatar': None}
    )
    channel = object()
    state._interaction_cache['n1'] = (2, 'ping', channel)

    seen = []
    client.on_interaction = lambda i: seen.append(i) or asyncio.sleep(0)  # type: ignore[attr-defined]
    state.parse_interaction_create({'id': '77', 'nonce': 'n1'})  # type: ignore[arg-type]

    assert 'n1' not in state._interaction_cache, 'the cache must only hold in-flight interactions'
    interaction = state._interactions[77]
    assert interaction.channel is channel and interaction.name == 'ping'
    await client.close()


def test_boolean_option_strings_and_unknown_options(caplog):
    import logging
    from types import SimpleNamespace

    from discord.commands import SlashMixin
    from discord.enums import ApplicationCommandOptionType

    opt = SimpleNamespace(name='flag', type=ApplicationCommandOptionType.boolean)
    cmd = SimpleNamespace(options=[opt], name='cmd')
    with caplog.at_level(logging.WARNING, logger='discord.commands'):
        options, _, _ = SlashMixin._parse_kwargs(cmd, {'flag': 'false', 'flgg': True})  # type: ignore[arg-type]
    assert options == [{'type': ApplicationCommandOptionType.boolean.value, 'name': 'flag', 'value': False}]
    assert any("'flgg'" in r.getMessage() for r in caplog.records), 'unknown options must not vanish silently'


def test_mixed_cloud_and_local_attachments_match_multipart_parts():
    import io

    from discord.file import CloudFile, File
    from discord.http import handle_message_parameters

    cloud = CloudFile('https://up/1', 'cloud.png', 'uploads/cloud.png', state=None)  # type: ignore[arg-type]
    local = File(io.BytesIO(b'abc'), filename='local.txt')
    params = handle_message_parameters(content='hi', files=[cloud, local])

    form_names = [part['name'] for part in params.multipart]
    payload = __import__('json').loads(params.multipart[0]['data'])
    by_name = {a['filename']: a for a in payload['attachments']}
    # The local file's id must name its multipart part; ids stay unique
    assert f'files[{by_name["local.txt"]["id"]}]' in form_names
    assert by_name['cloud.png']['uploaded_filename'] == 'uploads/cloud.png'
    assert len({a['id'] for a in payload['attachments']}) == 2
    params.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_cloud_file_from_file_keeps_spoiler():
    import io

    from discord.file import CloudFile, File

    uploaded = []

    async def upload_to_cloud(url, file):
        uploaded.append(url)

    state = SimpleNamespace(http=SimpleNamespace(upload_to_cloud=upload_to_cloud))
    file = File(io.BytesIO(b'x'), filename='secret.png', spoiler=True)
    cloud = await CloudFile.from_file(
        file=file,
        state=state,  # type: ignore[arg-type]
        data={'upload_url': 'https://up/2', 'upload_filename': 'uploads/secret.png', 'id': 0},  # type: ignore[typeddict-item]
    )
    assert cloud.spoiler is True and cloud.filename == 'SPOILER_secret.png'


def test_file_size_counts_from_the_original_position():
    import io

    from discord.file import File

    buffer = io.BytesIO(b'0123456789')
    buffer.seek(4)
    file = File(buffer, filename='tail.bin')
    assert file.size == 6, 'only bytes 4..10 are uploaded'
    assert buffer.tell() == 4, 'position restored'


async def _guild_env():
    import discord
    from discord import Guild

    client = discord.Client()
    await client._async_setup_hook()
    state = client._connection
    state.user = discord.ClientUser(
        state=state, data={'id': '5', 'username': 'me', 'discriminator': '0', 'global_name': None, 'avatar': None}
    )
    channels = [
        {'id': '10', 'type': 4, 'name': 'cat', 'position': 0},
        {'id': '11', 'type': 0, 'name': 'top-a', 'position': 0},
        {'id': '12', 'type': 0, 'name': 'top-b', 'position': 1},
        {'id': '13', 'type': 0, 'name': 'child-a', 'position': 0, 'parent_id': '10'},
        {'id': '14', 'type': 0, 'name': 'child-b', 'position': 1, 'parent_id': '10'},
    ]
    guild = Guild(
        state=state,
        data={'id': '1', 'name': 'g', 'roles': [{'id': '1', 'name': '@everyone'}], 'channels': channels, 'member_count': 1},
    )
    state._guilds[1] = guild
    return client, state, guild


@pytest.mark.asyncio
async def test_audit_logs_limit_counts_entries():
    client, state, guild = await _guild_env()
    calls = []

    async def get_audit_logs(guild_id, *, limit, before=None, after=None, user_id=None, action_type=None):
        calls.append(limit)
        start = 10_000 - len(calls) * 100
        entries = [{'id': str(start - i), 'action_type': 1, 'user_id': None, 'target_id': None} for i in range(limit)]
        return {
            'audit_log_entries': entries,
            'users': [],
            'webhooks': [],
            'integrations': [],
            'application_commands': [],
            'auto_moderation_rules': [],
            'threads': [],
        }

    state.http.get_audit_logs = get_audit_logs  # type: ignore[method-assign]
    got = [entry async for entry in guild.audit_logs(limit=150)]
    assert len(got) == 150, 'the limit used to be reduced by the number of response keys'
    assert calls == [100, 50]
    await client.close()


@pytest.mark.asyncio
async def test_purge_does_not_redelete_flushed_batches():
    from discord.abc import _purge_helper

    deleted_batches = []

    class Msg:
        def __init__(self, i):
            self.id = i
            self.type = SimpleNamespace(is_deletable=lambda: True)

    async def history(**kwargs):
        for i in range(60):
            yield Msg(i)

    async def delete_messages(channel_id, messages, reason=None):
        deleted_batches.append([m.id for m in messages])

    channel = SimpleNamespace(id=1, history=history, _state=SimpleNamespace(_delete_messages=delete_messages))
    # Messages 50..59 do not match: the 50 matches are flushed and nothing remains
    result = await _purge_helper(channel, limit=None, check=lambda m: m.id < 50)  # type: ignore[arg-type]

    assert len(result) == 50
    assert deleted_batches == [list(range(50))], f'already-deleted messages were sent again: {deleted_batches}'


@pytest.mark.asyncio
async def test_move_to_top_level_uses_top_level_siblings():
    client, state, guild = await _guild_env()
    sent = []

    async def bulk_channel_update(guild_id, payload, *, reason=None):
        sent.extend(payload)

    state.http.bulk_channel_update = bulk_channel_update  # type: ignore[method-assign]
    child = guild.get_channel(13)
    await child.move(category=None, end=True)  # type: ignore[union-attr]

    ids = [entry['id'] for entry in sent]
    assert ids == [11, 12, 13], f'positions must be computed among top-level channels, got {sent}'
    assert sent[-1].get('parent_id', 'unset') is None
    await client.close()


@pytest.mark.asyncio
async def test_query_members_timeout_unregisters_request(monkeypatch):
    import discord.state as state_module

    client, state, guild = await _guild_env()

    async def chunker(*args, **kwargs):
        return None

    async def instant_timeout(aw, timeout):
        aw.close()
        raise asyncio.TimeoutError

    state.chunker = chunker  # type: ignore[assignment]
    monkeypatch.setattr(state_module.asyncio, 'wait_for', instant_timeout)
    with pytest.raises(asyncio.TimeoutError):
        await state.query_members(guild, 'x', 5, None, False, False)
    assert state._chunk_requests == {}, 'a timed-out request must not stay registered'
    await client.close()


class _EchoSettingsHTTP:
    """edit() returns UserSettings parsed from exactly what would have been sent."""

    async def edit_proto_settings(self, type, data, required_data_version=None):
        return {'settings': data}


def _blank_user_settings():
    from discord.settings import UserSettings

    return UserSettings(SimpleNamespace(http=_EchoSettingsHTTP()), '')  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_user_audio_settings_edit_roundtrips():
    from discord.settings import AudioContext

    settings = _blank_user_settings()
    ctx = AudioContext(123, muted=True, volume=55.0)
    new = await settings.edit(user_audio_settings=[ctx])  # raised AttributeError (s.id) before

    (got,) = new.user_audio_settings
    assert (got.user_id, got.muted, got.volume) == (123, True, 55.0)
    assert abs((got.modified_at - ctx.modified_at).total_seconds()) < 1


@pytest.mark.asyncio
async def test_settings_missing_resets_instead_of_failing():
    settings = _blank_user_settings()
    reset = await settings.edit(restricted_guilds=[], soundboard_volume=MISSING_VALUE())  # type: ignore[arg-type]
    assert not reset.settings.voice_and_video.HasField('soundboard_settings'), 'MISSING must reset, not set volume 0'
    assert reset.soundboard_volume == 100.0
    await settings.edit(restricted_guilds=MISSING_VALUE())  # used to raise TypeError


def MISSING_VALUE():
    from discord.utils import MISSING

    return MISSING


def test_guild_progress_default_collapsed_channels():
    from discord.flags import HubProgressFlags, OnboardingProgressFlags
    from discord.settings import GuildProgress

    progress = GuildProgress(1, hub_progress=HubProgressFlags(), onboarding_progress=OnboardingProgressFlags())
    assert progress._collapsed_channel_ids == []


def test_holdout_bucket_zero_is_kept():
    from discord.experiment import UserExperiment

    holdout = UserExperiment.from_holdout(None, 1, 'exp', 1, 0)  # type: ignore[arg-type]
    assert holdout.assignment == 0
    assert UserExperiment.from_holdout(None, 1, 'exp', 1, None).assignment == -1  # type: ignore[arg-type]


def test_mp4_is_detected_at_its_real_offset():
    from discord import utils

    mp4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41' + b'\x00' * 32
    assert utils._get_mime_type_for_image(mp4, with_video=True) == 'video/mp4'
    assert utils._get_mime_type_for_image(b'\x00\x00\x00\x1cftypmp42' + b'\x00' * 32, with_video=True) == 'video/mp4'
    with pytest.raises(ValueError):
        utils._get_mime_type_for_image(mp4, with_video=False)


@pytest.mark.asyncio
async def test_voice_connect_fails_after_exhausting_retries(monkeypatch):
    import discord.voice_state as vs
    from discord.errors import ConnectionClosed
    from discord.voice_state import ConnectionFlowState, VoiceConnectionState

    conn = VoiceConnectionState.__new__(VoiceConnectionState)
    conn._state = ConnectionFlowState.got_both_voice_updates
    conn.endpoint = 'voice.example'
    attempts, disconnects = [], []

    async def noop(*args, **kwargs):
        return None

    async def failing_ws(self, resume):
        attempts.append(resume)
        raise ConnectionClosed(4006)

    async def record_disconnect(self, *, force=True, cleanup=True, wait=False):
        disconnects.append(cleanup)

    monkeypatch.setattr(VoiceConnectionState, '_voice_connect', noop)
    monkeypatch.setattr(VoiceConnectionState, '_wait_for_state', noop)
    monkeypatch.setattr(VoiceConnectionState, '_connect_websocket', failing_ws)
    monkeypatch.setattr(VoiceConnectionState, 'disconnect', record_disconnect)
    monkeypatch.setattr(vs.asyncio, 'sleep', noop)

    with pytest.raises(ConnectionClosed):
        await conn._inner_connect(reconnect=True, self_deaf=False, self_mute=False, self_video=False, resume=False)

    assert len(attempts) == 5
    assert disconnects == [False, False, False, False, True], 'retries soft-disconnect; the final failure cleans up'
