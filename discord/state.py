"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
from collections import deque, OrderedDict
import copy
import datetime
import functools
import gc
import logging
import os
import tempfile
import threading
import time
from typing import (
    ClassVar,
    Dict,
    Final,
    NamedTuple,
    Optional,
    TYPE_CHECKING,
    Union,
    Callable,
    Any,
    List,
    Mapping,
    TypeVar,
    Coroutine,
    Tuple,
    Deque,
    Literal,
    overload,
    Sequence,
    Set,
    cast,
)
import warnings
import weakref
import zlib
import inspect
from math import ceil

from discord_protos import UserSettingsType

from .errors import ClientException, InvalidData, NotFound
from .guild import Guild
from .activity import BaseActivity, create_activity, Session
from .user import User, ClientUser
from .emoji import Emoji
from .mentions import AllowedMentions
from .partial_emoji import PartialEmoji
from .message import Message
from .channel import *
from .channel import _channel_factory, _private_channel_factory
from .raw_models import *
from .member import Member
from .relationship import Relationship, FriendSuggestion
from .role import Role
from .enums import (
    ChannelType,
    InteractionFailureReason,
    InteractionType,
    Locale,
    MessageType,
    PaymentSourceType,
    ReadStateType,
    RelationshipType,
    RequiredActionType,
    Status,
    StreamDeleteReason,
    StreamType,
    try_enum,
)
from . import utils
from .flags import MemberCacheFlags
from .invite import Invite
from .integrations import _integration_factory
from .scheduled_event import ScheduledEvent
from .stage_instance import StageInstance
from .threads import Thread, ThreadMember
from .sticker import GuildSticker
from .soundboard import SoundboardSound
from .settings import UserSettings, FrecencySettings, GuildSettings, ChannelSettings, TrackingSettings
from .interactions import Interaction
from .permissions import Permissions
from .modal import Modal, IFrameModal
from .member import VoiceState
from .application import CommandApplication, IntegrationApplication, PartialApplication
from .connections import Connection
from .payments import Payment
from .entitlements import Entitlement, Gift
from .guild_premium import PremiumGuildSubscriptionSlot
from .library import LibraryApplication
from .automod import AutoModRule, AutoModAction
from .audit_logs import AuditLogEntry
from .read_state import ReadState
from .tutorial import Tutorial
from .experiment import UserExperiment, GuildExperiment, ApexExperiment
from .stream import Stream, StreamKey, StreamProtocol
from .metadata import Metadata
from .directory import DirectoryEntry
from .member_verification import JoinRequest

if TYPE_CHECKING:
    from typing_extensions import Self

    from .abc import Snowflake as abcSnowflake
    from .activity import ActivityTypes
    from .message import MessageableChannel
    from .guild import Guild, GuildChannel
    from .http import HTTPClient
    from .voice_client import VoiceProtocol
    from .client import Client
    from .gateway import DiscordWebSocket
    from .calls import Call
    from .poll import Poll

    from .types.automod import AutoModerationRule, AutoModerationActionExecution
    from .types.snowflake import Snowflake
    from .types.activity import Activity as ActivityPayload
    from .types.application import (
        IntegrationApplication as IntegrationApplicationPayload,
    )
    from .types.command import CommandApplication as CommandApplicationPayload
    from .types.channel import DMChannel as DMChannelPayload
    from .types.user import User as UserPayload, PartialUser as PartialUserPayload
    from .types.emoji import Emoji as EmojiPayload, PartialEmoji as PartialEmojiPayload
    from .types.sticker import GuildSticker as GuildStickerPayload
    from .types.guild import BaseGuild as BaseGuildPayload, Guild as GuildPayload
    from .types.message import (
        Message as MessagePayload,
        MessageSearchResult as MessageSearchResultPayload,
        PartialMessage as PartialMessagePayload,
    )
    from .types import gateway as gw
    from .types.voice import BaseVoiceState as VoiceStatePayload
    from .types.activity import ClientStatus as ClientStatusPayload

    T = TypeVar('T')
    PrivateChannel = Union[DMChannel, GroupChannel]
    Channel = Union[GuildChannel, PrivateChannel, PartialMessageable]

MISSING = utils.MISSING
_log = logging.getLogger(__name__)

# gc.freeze()/unfreeze() are process-global: with several Clients in one
# process, one client's unfreeze must not thaw the others' graphs. This
# counter tracks how many connection states currently hold a freeze.
_gc_freeze_holders: int = 0

F = TypeVar('F', bound=Callable[..., Any])


def _gc_paused(func: F) -> F:
    """Pauses the cyclic GC while a bulk payload builds long-lived objects.

    READY and large guild/member payloads allocate tens of thousands of objects
    that all survive; each generational collection they trigger re-scans the
    growing graph, which costs ~10-15% of the parse. The parse is synchronous,
    so collection simply resumes when it returns (or raises). Left alone when
    the application has disabled the GC itself; nested calls are no-ops.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not gc.isenabled():
            return func(*args, **kwargs)
        gc.disable()
        try:
            return func(*args, **kwargs)
        finally:
            gc.enable()

    return wrapper  # type: ignore


# Cache persistence limits
PERSISTENCE_DEFAULT_MAX_AGE: float = 14 * 24 * 3600  # seconds
PERSISTENCE_MAX_BYTES: int = (
    256 * 1024 * 1024
)  # maximum decompressed snapshot size (256 MiB; ~100x a realistic large snapshot)
# Snapshot format written by this version: newline-delimited JSON records (a
# header, then one record per guild) in a single zlib stream. Version 1 (one
# JSON document) is still read.
PERSISTENCE_FORMAT_VERSION: int = 2
# How long replay may run on the event loop before yielding to other tasks
PERSISTENCE_REPLAY_SLICE: float = 0.01
ST = TypeVar('ST', bound=StreamProtocol)


class _SnapshotReader:
    """Streams JSON records out of a zlib-compressed persistence snapshot.

    Records are separated by newlines, so a version 2 snapshot is decoded one
    guild at a time and never needs the whole account in memory at once. A
    version 1 snapshot is a single JSON document without newlines and comes
    back as one record. The decompressed total is capped at
    :data:`PERSISTENCE_MAX_BYTES` so a hostile file cannot exhaust memory.

    The methods block (file IO, decompression, JSON parsing): run them in a
    worker thread.
    """

    __slots__ = ('_fp', '_decompressor', '_buffer', '_scan', '_total', '_eof')

    # Small windows keep the buffer near one record: decompressing far ahead
    # would hold that much of the snapshot in memory at once
    READ_SIZE = 256 * 1024
    OUTPUT_SIZE = 1024 * 1024

    def __init__(self, path: Union[str, os.PathLike]) -> None:
        self._fp = open(path, 'rb')
        self._decompressor = zlib.decompressobj()
        self._buffer = bytearray()
        self._scan = 0  # buffer offset already searched for a newline
        self._total = 0
        self._eof = False

    def close(self) -> None:
        self._fp.close()

    def _append(self, data: bytes) -> None:
        self._total += len(data)
        if self._total > PERSISTENCE_MAX_BYTES:
            raise ValueError('persistence file exceeds the maximum allowed size')
        self._buffer += data

    def _fill(self) -> None:
        decompressor = self._decompressor
        data = decompressor.unconsumed_tail
        if not data:
            if decompressor.eof:
                # End of the zlib stream; anything after it is ignored
                self._eof = True
                return
            data = self._fp.read(self.READ_SIZE)
            if not data:
                self._append(decompressor.flush())
                self._eof = True
                return
        self._append(decompressor.decompress(data, self.OUTPUT_SIZE))

    def next_record(self) -> Any:
        """Returns the next decoded record, or ``None`` at the end of the snapshot."""
        buffer = self._buffer
        while True:
            index = buffer.find(b'\n', self._scan)
            if index >= 0:
                line = buffer[:index]
                del buffer[: index + 1]
                self._scan = 0
                if line.strip():
                    return utils._from_json(line)
                continue
            self._scan = len(buffer)
            if self._eof:
                if not buffer:
                    return None
                # The last record (or a whole version 1 document): parse the
                # buffer in place rather than copying it
                record = utils._from_json(buffer)
                buffer.clear()
                self._scan = 0
                return record
            self._fill()


class ChunkRequest:
    __slots__ = (
        'guild_id',
        'resolver',
        'loop',
        'limit',
        'remaining',
        'cache',
        'oneshot',
        'nonce',
        'buffer',
        'last_buffer',
        'waiters',
        'reverse',
        'unclaimed',
    )

    def __init__(
        self,
        guild_id: int,
        loop: asyncio.AbstractEventLoop,
        resolver: Callable[[int], Any],
        *,
        limit: Optional[int] = None,
        cache: bool = True,
        oneshot: bool = True,
        nonce: Optional[str] = None,
        reverse: bool = False,
    ) -> None:
        self.guild_id: int = guild_id
        self.resolver: Callable[[int], Any] = resolver
        self.loop: asyncio.AbstractEventLoop = loop
        self.limit: Optional[int] = limit
        self.remaining: int = limit or 0
        self.cache: bool = cache
        self.oneshot: bool = oneshot
        self.nonce: str = nonce or utils._generate_nonce()
        self.buffer: List[Member] = []
        self.last_buffer: Optional[List[Member]] = None
        self.waiters: List[asyncio.Future[List[Member]]] = []
        self.reverse: bool = reverse
        # A result that completed before anyone waited for it. The request is
        # sent before the caller starts waiting, so a fast reply can arrive in
        # between; without this it was dropped and the caller hung until its
        # timeout. Claimed by exactly one later wait.
        self.unclaimed: Optional[List[Member]] = None

    def add_members(self, members: List[Member]) -> None:
        if self.reverse:
            members = members[::-1]

        if self.limit is not None:
            if self.remaining <= 0:
                return

            members = members[: self.remaining]
            self.remaining -= len(members)

        self.buffer.extend(members)

        if self.cache:
            guild = self.resolver(self.guild_id)
            if guild is None:
                return

            for member in members:
                guild._add_member(member)

        if not self.oneshot:
            self.last_buffer = members

    async def wait(self) -> List[Member]:
        unclaimed = self.unclaimed
        if unclaimed is not None:
            self.unclaimed = None
            return unclaimed
        future = self.loop.create_future()
        self.waiters.append(future)
        try:
            return await future
        finally:
            self.waiters.remove(future)

    def get_future(self) -> asyncio.Future[List[Member]]:
        future = self.loop.create_future()
        unclaimed = self.unclaimed
        if unclaimed is not None:
            self.unclaimed = None
            future.set_result(unclaimed)
            return future
        self.waiters.append(future)
        return future

    def done(self) -> None:
        result = self.buffer if self.oneshot else self.last_buffer or self.buffer
        delivered = False
        for future in self.waiters:
            if not future.done():
                future.set_result(result)
                delivered = True
        if not delivered:
            self.unclaimed = result

    def fail(self, exc: BaseException) -> None:
        # Wake everyone waiting on a request that can no longer complete
        for future in list(self.waiters):
            if not future.done():
                future.set_exception(exc)
                future.exception()  # mark retrieved: an unawaited future must not warn at GC


class MemberSidebar:
    __slots__ = (
        'guild',
        'channels',
        'chunk',
        'delay',
        'cache',
        'loop',
        'safe_override',
        '_limit_override',
        'ranges',
        'subscribing',
        'buffer',
        'exception',
        'waiters',
    )

    def __init__(
        self,
        guild: Guild,
        channels: Sequence[abcSnowflake],
        *,
        chunk: bool,
        delay: Union[int, float],
        cache: bool,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.guild = guild
        self.cache = cache
        self.chunk = chunk
        self.delay = delay
        self.loop = loop
        self.safe_override = False  # >.<
        self._limit_override: Optional[int] = None

        if channels:
            self.channels = self.validate_channels(channels)
        else:
            self.channels = self.get_channels(1 if chunk else 5)
        self.ranges = self.get_ranges()
        self.subscribing: bool = False
        self.buffer: List[Member] = []
        self.exception: Optional[Exception] = None
        self.waiters: List[asyncio.Future[Optional[List[Member]]]] = []

    @property
    def manual_override(self) -> bool:
        return self._limit_override is not None

    @property
    def limit(self) -> int:
        if self._limit_override is not None:
            return self._limit_override

        guild = self.guild
        members = guild._presence_count if guild._offline_members_hidden else guild._member_count or 0
        # Ensure groups are accounted for
        return (members or 0) + len([role for role in guild.roles if role.hoist]) + 2

    @property
    def state(self) -> ConnectionState:
        return self.guild._state

    @property
    def safe(self):
        return self.safe_override or (self.guild._member_count or 0) >= 75000

    @staticmethod
    def amalgamate(original: Tuple[int, int], value: Tuple[int, int]) -> Tuple[int, int]:
        return original[0], value[1] - 99

    def get_ranges(self, *, start: int = 0) -> List[Tuple[int, int]]:
        chunk = 100
        end = 99
        amount = self.limit
        if amount is None:
            raise RuntimeError('Member/presence count required to compute ranges')

        ceiling = ceil(amount / chunk) * chunk
        ranges = []
        for i in range(int(start / chunk), int(ceiling / chunk)):
            min = i * chunk
            max = min + end
            ranges.append((min, max))

        return ranges

    def get_current_ranges(self) -> List[Tuple[int, int]]:
        ranges = self.ranges
        ret = []

        for _ in range(3):
            if self.safe:
                try:
                    ret.append(ranges.pop(0))
                except IndexError:
                    break
            else:
                try:
                    current = ranges.pop(0)
                except IndexError:
                    break
                for _ in range(3):
                    try:
                        current = self.amalgamate(current, ranges.pop(0))
                    except IndexError:
                        break
                ret.append(current)

        return ret

    def handle_manual_override(self, group_members: int) -> None:
        # Certain guilds like Midjourney have their member list groups manually set
        # In these cases, the online group is removed, and most online members are not retrievable
        # We must update the limit to the "real" online count, and recalculate the ranges
        self._limit_override = group_members
        if self.ranges:
            self.ranges = self.get_ranges(start=self.ranges[0][0])

    def validate_channels(self, channels: Sequence[abcSnowflake]) -> Sequence[Snowflake]:
        guild = self.guild
        ids = set()

        for channel in channels:
            real_channel = guild.get_channel(channel.id)
            if real_channel is None:
                raise ValueError(f'Channel {channel!r} not found in guild {guild.id}')

            # Attempt to account for member list ID bug
            if real_channel._can_everyone(Permissions.read_messages):
                ids.add('everyone')
            else:
                ids.add(real_channel.member_list_id)

        if len(ids) > 1:
            # This is only a warning because it's not a critical issue and can have false positives
            warnings.warn(
                f'Member list scraping for guild ID {guild.id} with multiple member list IDs may lead to undefined behavior',
                RuntimeWarning,
            )

        return [channel.id for channel in channels]

    def get_channels(self, amount: int) -> Sequence[Snowflake]:
        # Because of a Discord bug, not all of these channels will have the
        # "everyone" member list ID, however they will all functionally have
        # an identical member list
        return [channel.id for channel in self.guild.channels if channel._can_everyone(Permissions.read_messages)][:amount]

    def add_members(self, members: List[Member]) -> None:
        # Order-preserving dedupe so within-batch LRU freshness is deterministic
        members = list(dict.fromkeys(members))
        self.buffer.extend(members)
        if self.cache:
            guild = self.guild
            for member in members:
                guild._add_member(member)

    async def wait(self) -> List[Member]:
        future = self.loop.create_future()
        self.waiters.append(future)
        try:
            return await future
        finally:
            self.waiters.remove(future)

    def get_future(self) -> asyncio.Future[List[Member]]:
        future = self.loop.create_future()
        self.waiters.append(future)
        return future

    def done(self) -> None:
        for future in self.waiters:
            if not future.done():
                if self.exception:
                    future.set_exception(self.exception)
                else:
                    future.set_result(self.buffer)

        try:
            del self.state._scrape_requests[self.guild.id]
        except KeyError:
            pass

    def start(self):
        utils._spawn_background(self.wrapper(), loop=self.loop)

    async def wrapper(self):
        try:
            await self.scrape()
            if not self.buffer and (self.guild._member_count or 0) > 1:
                # Discord answered the subscription without a SYNC for any
                # requested range (seen with INVALIDATE-only replies), so there
                # is nothing to return. Say so rather than return [] silently.
                _log.warning(
                    'Member list scraping for guild ID %s got no member list from Discord (no SYNC for the '
                    'requested ranges in channel(s) %s); returning no members. Retrying later or passing '
                    'channels explicitly may help.',
                    self.guild.id,
                    ', '.join(map(str, self.channels)),
                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _log.warning('Member list scraping failed for guild ID %s (%s).', self.guild.id, exc)
            self.exception = exc
        finally:
            self.done()

    async def scrape(self):
        self.subscribing = True
        delay = self.delay
        channels = self.channels
        guild = self.guild
        state = guild._state

        while self.subscribing:
            requests = {}
            for channel in channels:
                ranges = self.get_current_ranges()
                if not ranges:
                    self.subscribing = False
                    break
                requests[channel] = ranges
            if not self.subscribing and not requests:
                break

            request_values = list(requests.values())
            all_possible_ranges = range(request_values[0][0][0], request_values[-1][-1][1] + 1)

            if not requests:
                raise ClientException('Failed to automatically choose channels; please specify them manually')

            def predicate(data):
                return int(data['guild_id']) == guild.id and any(
                    op['op'] == 'SYNC' and op['range'][0] in all_possible_ranges for op in data['ops']
                )

            await state.subscriptions.subscribe_to_channels(guild, requests, replace=True)

            try:
                await asyncio.wait_for(
                    state.ws.wait_for('GUILD_MEMBER_LIST_UPDATE', predicate), timeout=state._chunk_timeout(guild)
                )
            except asyncio.TimeoutError:
                r = tuple(requests.values())[-1][-1]
                if self.limit in range(r[0], r[1]) or self.limit < r[1]:
                    self.subscribing = False
                    break
                else:
                    if self.safe:
                        raise InvalidData('Did not receive a response from Discord')

                    # Sometimes servers require safe mode (they used to have 75k+ members)
                    # so if we don't get a response we force safe mode and try again
                    _log.debug(
                        'Forcing member list scraping safe mode for guild ID %s (member count: %s).',
                        guild.id,
                        guild._member_count,
                    )
                    self.safe_override = True
                    self.ranges = self.get_ranges()
                    await self.scrape()
                    return

            await asyncio.sleep(delay)

            r = tuple(requests.values())[-1][-1]
            if self.limit in range(r[0], r[1]) or self.limit < r[1]:
                self.subscribing = False
                break

        utils._spawn_background(state.subscriptions.subscribe_to_channels(guild, {}, replace=True), loop=state.loop)


class GuildSubscriptions:
    """A bit of documentation on guild subscriptions:

    Client can subscribe to the following "features":
    - ``typing``: whether the client receives TYPING_START events
    - ``threads``: whether the client receives thread events for threads the user is not in -- probably wrong
      - The client immediately receives a THREAD_LIST_SYNC event with all the threads in the guild
    - ``activities``: not sure what this does yet
    - ``member_updates``: whether the client receives member events (GUILD_MEMBER_ADD, GUILD_MEMBER_UPDATE, GUILD_MEMBER_REMOVE)

    The client is automatically subscribed to all guilds with < 75k members on connect. For guilds the client is not subscribed to,
    it will not receive non-stateful events (e.g. MESSAGE_CREATE, MESSAGE_UPDATE, MESSAGE_DELETE, etc.).
    Additionally, it will receive the PASSIVE_UPDATE_V2 event to keep voice states and channel unreads up-to-date.

    Once a client subscribes to the ``typing`` feature, it is considered subscribed to that guild indefinitely. On the first subscribe,
    it will receive a GUILD_CREATE event (for some reason). If subscribing to other features/members without having subscribed to the
    guild, they will have no effect until the client subscribes to it. This comes with the drawback of not receiving a THREAD_LIST_SYNC
    event if the client is not subscribed to the guild, even if it subscribes to it later. It must first unsubscribe from the ``threads``
    event and then resubscribe to it.

    Clients can also subscribe to specific members within a guild using the ``members`` parameter. This is a list of user IDs that
    the client will receive GUILD_MEMBER_ & PRESENCE_ UPDATEs for. This field has no cap except for the max payload size.

    Clients can also subscribe to specific threads' member lists using the ``thread_member_lists`` parameter. After subscribing to a thread
    member list, the client will immediately receive a THREAD_MEMBER_LIST_UPDATE event (sent regardless of whether the client is subscribed
    to the guild) with all the members in the thread. Note that the members received are guild member objects, not thread member objects.
    There is no way for user accounts to request thread member objects. After subscribing to a thread member list, the client will be
    subscribed to all members in the thread, akin to using ``members``. This field has no cap except for the max payload size.

    The client is automatically subscribed to the members of all friends, implicit relationships, and users with open DMs (that it has a mutual guild with)
    for all guilds with less than 75k members at startup. Additionally, the Gateway will repeat this process and subscribe you to a specific DM recipient every time a new DM is opened.
    The above triggers a GUILD_MEMBER_REMOVE event for every guild you do not share with the DM recipient (this behavior used to exist for the ``members`` parameter too).

    Irrespective of subscribed members, clients will always receive GUILD_MEMBER_UPDATE and GUILD_MEMBER_REMOVE events for actions they perform
    (e.g. changing a user's nickname, kicking a user, banning a user, etc.). Additionally, you will also receive GUILD_MEMBER_UPDATE for members
    with an active voice state. This requires that the client is subscribed to the guild.

    Remember that you get presence updates for the overall user presence for all friends and implicit relationships.

    .. note::

        The following subscription behaviors are *not* confirmed and may not match the above:

        - Joining a new guild: whether the client is subscribed automatically is not verified.
        - Adding a new friend: the client is believed **not** to be subscribed to them in every guild,
          although the overall user presence is still received.
        - Opening a new DM: this is believed to subscribe the client to the recipient universally.
        - Guilds with 75k+ members: behavior here differs from smaller guilds and has not been characterized.
    """

    # I thought it was 4096 bytes, but this is taken from the client
    MAX_PAYLOAD_SIZE: Final[int] = 15360  # 15 KiB
    TICK_TIMEOUT: Final[float] = 0.5

    __slots__ = (
        '_state',
        '_pending',
        '_entry_sizes',
        '_sized_payload',
        '_flush_failures',
        '_task',
        '_blocked',
        '_subscribed',
        '_typing',
        '_threads',
        '_activities',
        '_member_updates',
        '_members',
        '_thread_member_lists',
        '_channels',
    )

    def __init__(self, state: ConnectionState, /) -> None:
        self._state = state
        self._pending: gw.BulkGuildSubscribePayload = {}
        # Encoded size of each pending entry, valid for the dict in _sized_payload
        self._entry_sizes: Dict[str, int] = {}
        self._sized_payload: Any = None
        # Consecutive failed sends: drives the retry backoff and the warning
        self._flush_failures: int = 0
        self._task: Optional[asyncio.Task[None]] = None
        self._blocked: bool = False
        self.clear()

    def clear(self) -> None:
        # Feature subscriptions
        self._subscribed: utils.SnowflakeList = utils.SnowflakeList()
        self._typing: utils.SnowflakeList = utils.SnowflakeList()
        self._threads: utils.SnowflakeList = utils.SnowflakeList()
        # Guilds subscribed with the 'activities' feature (see has_feature)
        self._activities: utils.SnowflakeList = utils.SnowflakeList()
        self._member_updates: utils.SnowflakeList = utils.SnowflakeList()

        # Member subscriptions
        self._members: Dict[int, utils.SnowflakeList] = {}
        self._thread_member_lists: Dict[int, utils.SnowflakeList] = {}
        self._channels: Dict[int, Dict[int, List[Tuple[int, int]]]] = {}

    def _initial_update(self, guilds: List[Guild], /):
        # The client is subscribed to all guilds with < 75k members on connect
        # This function is currently unused because I don't want to rely on this behavior
        for guild in guilds:
            if guild._member_count and guild._member_count < 75000:
                self._subscribed.add(guild.id)

    def _initial_thread_subscription(self, guild_id: int, /):
        # On GUILD_CREATE, the client is given some subscriptions sometimes
        # The field is called "has_threads_subscription", but I'm
        # pretty sure it subscribes you to all the default features
        self._subscribed.add(guild_id)
        self._typing.add(guild_id)
        self._threads.add(guild_id)
        self._activities.add(guild_id)

    def _cancel(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _tick_task(self) -> None:
        # A send that keeps failing is retried less and less often (up to every 30s)
        delay = min(self.TICK_TIMEOUT * (2 ** min(self._flush_failures, 6)), 30.0)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        # Past the delay this task is no longer the pending tick: a concurrent
        # _tick() (any subscribe call) must schedule a new tick rather than
        # cancel this one mid-send, which would drop the in-flight payload
        if self._task is asyncio.current_task():
            self._task = None

        ws = self._state.ws
        if ws is not None and ws.open:
            await self._flush()
        elif self._pending:
            # Nothing to retry for when there is nothing queued: the next
            # subscribe call ticks again
            self._tick()

    def _tick(self) -> None:
        self._cancel()
        if not self._blocked:
            # Strongly referenced until done: once flushing, _task no longer holds it
            self._task = utils._spawn_background(self._tick_task(), loop=self._state.loop)

    @property
    def empty(self) -> bool:
        return not self._subscribed and not self._pending

    @property
    def blocked(self) -> bool:
        return self._blocked

    @blocked.setter
    def blocked(self, value: bool, /) -> None:
        if self._blocked == value:
            return
        self._blocked = value
        if value:
            self._cancel()
        else:
            self._tick()

    async def _requeue_subscriptions(self):
        # We need to send all our subscriptions again if we reconnect
        pending = self._pending
        subscribed = self._subscribed
        typing = self._typing
        threads = self._threads
        activities = self._activities
        member_updates = self._member_updates
        members = self._members
        thread_member_lists = self._thread_member_lists
        channels = self._channels
        self.clear()

        for guild_id in self._state._guilds:
            if guild_id not in subscribed:
                continue

            key = str(guild_id)
            payload: gw.BaseGuildSubscribePayload = {
                # Ensure we are subscribed to the guild
                'typing': guild_id in typing or guild_id in subscribed,
                'threads': guild_id in threads,
                'activities': guild_id in activities,
                'member_updates': guild_id in member_updates,
                'members': list(members.get(guild_id, ())),
                'thread_member_lists': list(thread_member_lists.get(guild_id, ())),
                'channels': channels.get(guild_id, {}),  # type: ignore
            }
            if key in pending:
                payload.update(pending[key])
            if payload:
                await self._checked_add({key: payload})

    def is_subscribed(self, guild: abcSnowflake, /) -> bool:
        return guild.id in self._subscribed

    def _is_pending_subscribe(self, guild_id: int, /) -> bool:
        key = str(guild_id)
        return guild_id in self._subscribed or (key in self._pending and self._pending[key].get('typing') is True)

    def has_feature(
        self, guild: abcSnowflake, feature: Literal['typing', 'threads', 'activities', 'member_updates'], /
    ) -> bool:
        # getattr is less performant
        if feature == 'typing':
            return self._typing.has(guild.id)
        elif feature == 'threads':
            return self._threads.has(guild.id)
        elif feature == 'activities':
            return self._activities.has(guild.id)
        elif feature == 'member_updates':
            return self._member_updates.has(guild.id)

    def _has_feature_pending(
        self, guild_id: int, feature: Literal['typing', 'threads', 'activities', 'member_updates'], /
    ) -> bool:
        key = str(guild_id)
        if feature == 'typing' and guild_id in self._typing:
            return True
        elif feature == 'threads' and guild_id in self._threads:
            return True
        elif feature == 'activities' and guild_id in self._activities:
            return True
        elif feature == 'member_updates' and guild_id in self._member_updates:
            return True
        if key in self._pending and self._pending[key].get(feature) is True:
            return True
        return False

    def members_for(self, guild: abcSnowflake, /) -> Sequence[int]:
        return utils.SequenceProxy(self._members.get(guild.id, ()))

    def has_member(self, guild: abcSnowflake, member: abcSnowflake, /) -> bool:
        members = self._members.get(guild.id)
        if members is None:
            return False
        return members.has(member.id)

    def threads_for(self, guild: abcSnowflake, /) -> Sequence[int]:
        return utils.SequenceProxy(self._thread_member_lists.get(guild.id, ()))

    def has_thread(self, guild: abcSnowflake, thread: abcSnowflake, /) -> bool:
        threads = self._thread_member_lists.get(guild.id)
        if threads is None:
            return False
        return threads.has(thread.id)

    def channels_for(self, guild: abcSnowflake, /) -> Dict[int, List[Tuple[int, int]]]:
        return self._channels.get(guild.id, {}).copy()

    async def _checked_add(self, changes: gw.BulkGuildSubscribePayload, /) -> None:
        # n.b. changes should have a single key
        # We need to check if the new payload is larger than MAX_PAYLOAD_SIZE bytes
        # If it is, we need to flush the old payload and start a new one
        # If there isn't an old payload and the new payload is larger, this is impossible
        EMPTY: Any = {}
        # Bounded: each round that does not fit sends the backlog first. If sends
        # keep failing (the backlog is requeued) or other callers keep refilling it,
        # queue the change anyway instead of recursing without end.
        for attempt in range(3):
            pending = self._pending
            new_payload = pending.copy()
            new_sizes = self._sizes_for(pending).copy()
            for guild_id, subscriptions in changes.items():
                old = cast(Dict[Any, Any], new_payload.get(guild_id, EMPTY))
                updates = cast(Dict[Any, Any], subscriptions)
                new_payload[guild_id] = merged = cast(Any, {**old, **updates})
                # Only the changed entry is re-encoded, not the whole batch
                new_sizes[guild_id] = self._entry_size(guild_id, merged)

            fits = self._encoded_size(new_sizes) <= self.MAX_PAYLOAD_SIZE
            if not fits:
                if len(utils._to_json(changes)) > self.MAX_PAYLOAD_SIZE:
                    raise ValueError('Guild subscription payload too large to send')
                if attempt < 2:
                    await self._flush()
                    # Other subscribe calls may have queued changes during the flush;
                    # the next round merges into them instead of overwriting
                    continue
                _log.warning(
                    'Guild subscription backlog could not be flushed (%d consecutive send failures); '
                    'queueing the change behind it.',
                    self._flush_failures,
                )

            self._pending = new_payload
            self._entry_sizes = new_sizes
            self._sized_payload = new_payload
            break

        self._tick()

    @staticmethod
    def _entry_size(key: str, value: Any) -> int:
        # Length of '"key":value' inside the compact JSON object
        return len(utils._to_json({key: value})) - 2

    @staticmethod
    def _encoded_size(sizes: Dict[str, int]) -> int:
        # len(_to_json(payload)) == braces + entries + commas between them
        return 2 + sum(sizes.values()) + max(len(sizes) - 1, 0)

    def _sizes_for(self, payload: Any) -> Dict[str, int]:
        if self._sized_payload is not payload:
            # _pending was replaced elsewhere (flush, requeue): re-measure once
            self._entry_sizes = {key: self._entry_size(key, value) for key, value in payload.items()}
            self._sized_payload = payload
        return self._entry_sizes

    async def _flush(self) -> None:
        payload = self._pending
        if not payload:
            return
        self._pending = {}

        try:
            # Only keys that are present in the payload are updated on the backend
            await self._state.ws.bulk_guild_subscribe(payload)
        except Exception as exc:
            self._flush_failures += 1
            # Visible, but not on every retry: the first failure and then every 10th
            level = logging.WARNING if self._flush_failures % 10 == 1 else logging.DEBUG
            _log.log(
                level,
                'Bulk guild subscribe failed with %s (%d in a row). Requeuing changes...',
                exc,
                self._flush_failures,
                exc_info=True,
            )
            # This should never raise
            new_payload = self._pending
            self._pending = payload
            await self._checked_add(new_payload)
            return

        self._flush_failures = 0

        for key, subscriptions in payload.items():
            guild_id = int(key)
            if subscriptions.get('typing'):
                self._subscribed.add(guild_id)
            for attr, value in subscriptions.items():
                if isinstance(value, bool):
                    record = getattr(self, f'_{attr}')
                    if value:
                        record.add(guild_id)
                    else:
                        record.discard(guild_id)
                elif attr == 'channels':
                    record = self._channels
                    if value:
                        record[guild_id] = value  # type: ignore
                    else:
                        record.pop(guild_id, None)
                else:
                    record = getattr(self, f'_{attr}')
                    if value:
                        record[guild_id] = utils.SnowflakeList(value)  # type: ignore
                    else:
                        record.pop(guild_id, None)

    async def send(self) -> None:
        should_tick = self._task is not None
        await self._flush()
        if should_tick:
            self._tick()

    async def subscribe_to(
        self,
        guild: abcSnowflake,
        /,
        *,
        typing: bool = MISSING,
        threads: bool = MISSING,
        activities: bool = MISSING,
        member_updates: bool = MISSING,
    ):
        guild_id = guild.id
        _typing = typing if typing is not MISSING else self._has_feature_pending(guild_id, 'typing')
        if not _typing:
            # To allow unsubscribing from a guild entirely, everything else must be gone first
            _threads = threads if threads is not MISSING else self._has_feature_pending(guild_id, 'threads')
            _activities = activities if activities is not MISSING else self._has_feature_pending(guild_id, 'activities')
            _member_updates = (
                member_updates if member_updates is not MISSING else self._has_feature_pending(guild_id, 'member_updates')
            )
            _members = self._members.get(guild_id)
            _thread_member_lists = self._thread_member_lists.get(guild_id)
            _channels = self._channels.get(guild_id)
            if any((_threads, _activities, _member_updates, _members, _thread_member_lists, _channels)):
                raise TypeError('Cannot unsubscribe from guild while other features are still subscribed')

        payload: gw.BaseGuildSubscribePayload = {}
        if typing is not MISSING:
            payload['typing'] = typing
        if threads is not MISSING:
            payload['threads'] = threads
        if activities is not MISSING:
            payload['activities'] = activities
        if member_updates is not MISSING:
            payload['member_updates'] = member_updates

        if payload:
            await self._checked_add({str(guild_id): payload})

    async def subscribe_to_members(self, guild: abcSnowflake, /, *members: abcSnowflake, replace: bool = False) -> None:
        if not replace and not members:
            return

        # Sanity check
        if not self._is_pending_subscribe(guild.id):
            raise TypeError('Cannot subscribe to guild without subscribing to typing')

        payload: gw.BaseGuildSubscribePayload = {}
        values: Set[Snowflake] = {member.id for member in members}
        if not replace:
            existing = self._members.get(guild.id)
            if existing:
                values.update(existing)

        payload['members'] = list(values)
        await self._checked_add({str(guild.id): payload})

    async def unsubscribe_from_members(self, guild: abcSnowflake, /, *members: abcSnowflake) -> None:
        # Sanity check
        if not self._is_pending_subscribe(guild.id):
            return

        payload: gw.BaseGuildSubscribePayload = {}
        existing = self._members.get(guild.id)
        to_remove = [member.id for member in members]

        if existing:
            payload['members'] = [member for member in existing if member not in to_remove]
        await self._checked_add({str(guild.id): payload})

    async def subscribe_to_threads(self, guild: abcSnowflake, /, *threads: abcSnowflake, replace: bool = False) -> None:
        if not replace and not threads:
            return

        # Sanity check
        if not self._is_pending_subscribe(guild.id):
            raise TypeError('Cannot subscribe to guild without subscribing to typing')

        payload: gw.BaseGuildSubscribePayload = {}
        values: set[Snowflake] = {thread.id for thread in threads}
        if not replace:
            existing = self._thread_member_lists.get(guild.id)
            if existing:
                values.update(existing)

        payload['thread_member_lists'] = list(values)
        await self._checked_add({str(guild.id): payload})

    async def unsubscribe_from_threads(self, guild: abcSnowflake, /, *threads: abcSnowflake) -> None:
        # Sanity check
        if not self._is_pending_subscribe(guild.id):
            return

        payload: gw.BaseGuildSubscribePayload = {}
        existing = self._thread_member_lists.get(guild.id)
        to_remove = [thread.id for thread in threads]

        if existing:
            payload['thread_member_lists'] = [thread for thread in existing if thread not in to_remove]
        await self._checked_add({str(guild.id): payload})

    async def subscribe_to_channels(
        self, guild: abcSnowflake, /, channels: Dict[Snowflake, List[Tuple[int, int]]], replace: bool = False
    ) -> None:
        if not replace and not channels:
            return

        # Sanity check
        if not self._is_pending_subscribe(guild.id):
            raise TypeError('Cannot subscribe to guild without subscribing to typing')

        payload: gw.BaseGuildSubscribePayload = {}
        values: Dict[Snowflake, List[Tuple[int, int]]] = {str(channel_id): ranges for channel_id, ranges in channels.items()}
        if not replace:
            existing = self._channels.get(guild.id)
            if existing:
                values = {**existing, **values}

        payload['channels'] = values
        await self._checked_add({str(guild.id): payload})


class ClientStatus:
    __slots__ = ('status', 'desktop', 'mobile', 'web', 'embedded')

    def __init__(self, status: Optional[str] = None, data: Optional[ClientStatusPayload] = None, /) -> None:
        self.status: str = 'offline'
        self.desktop: Optional[str] = None
        self.mobile: Optional[str] = None
        self.web: Optional[str] = None
        self.embedded: Optional[str] = None

        if status is not None or data is not None:
            self._update(status or 'offline', data or {})

    def __repr__(self) -> str:
        attrs = [
            ('status', self.status),
            ('desktop', self.desktop),
            ('mobile', self.mobile),
            ('web', self.web),
            ('embedded', self.embedded),
        ]
        inner = ' '.join('%s=%r' % t for t in attrs)
        return f'<{self.__class__.__name__} {inner}>'

    def _update(self, status: str, data: ClientStatusPayload, /) -> None:
        self.status = status
        self.desktop = data.get('desktop')
        self.mobile = data.get('mobile')
        self.web = data.get('web')
        self.embedded = data.get('embedded')

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ClientStatus):
            return False
        return (
            self.status == other.status
            and self.desktop == other.desktop
            and self.mobile == other.mobile
            and self.web == other.web
            and self.embedded == other.embedded
        )

    def __ne__(self, other: object) -> bool:
        if not isinstance(other, ClientStatus):
            return True
        return not self.__eq__(other)

    @classmethod
    def _copy(cls, client_status: Self, /) -> Self:
        self = cls.__new__(cls)  # bypass __init__
        self.status = client_status.status
        self.desktop = client_status.desktop
        self.mobile = client_status.mobile
        self.web = client_status.web
        self.embedded = client_status.embedded
        return self


class Presence:
    __slots__ = ('client_status', 'activities', 'hidden_activities')

    _OFFLINE: ClassVar[Self] = MISSING

    def __init__(self, data: gw.BasePresenceUpdate, state: ConnectionState, user_id: int, /) -> None:
        self.client_status: ClientStatus = ClientStatus(data['status'], data.get('client_status'))
        self.activities: Tuple[ActivityTypes, ...] = tuple(
            create_activity(d, state, user_id) for d in data.get('activities', [])
        )
        self.hidden_activities: Tuple[ActivityTypes, ...] = tuple(
            create_activity(d, state, user_id) for d in data.get('hidden_activities', [])
        )

    def __repr__(self) -> str:
        attrs = [
            ('client_status', self.client_status),
            ('activities', self.activities),
            ('hidden_activities', self.hidden_activities),
        ]
        inner = ' '.join('%s=%r' % t for t in attrs)
        return f'<{self.__class__.__name__} {inner}>'

    def __eq__(self, other: Any) -> bool:
        if self is other:
            return True
        if not isinstance(other, Presence):
            return False
        return self.client_status == other.client_status and self.activities == other.activities

    def __ne__(self, other: Any) -> bool:
        if self is other:
            return False
        if not isinstance(other, Presence):
            return True
        return self.client_status != other.client_status or self.activities != other.activities

    def _update(self, data: gw.BasePresenceUpdate, state: ConnectionState, user_id: int, /) -> None:
        self.client_status._update(data['status'], data.get('client_status'))
        raw_activities = data['activities']
        # The raw activity dicts are not kept (they would double the memory of
        # every presence with an activity), so re-parse unless both are empty
        if raw_activities or self.activities:
            self.activities = tuple(create_activity(d, state, user_id) for d in raw_activities)

    @classmethod
    def _offline(cls) -> Self:
        if cls._OFFLINE is MISSING:
            self = cls.__new__(cls)  # bypass __init__
            self.client_status = ClientStatus()
            self.activities = ()
            self.hidden_activities = ()
            cls._OFFLINE = self

        return cls._OFFLINE

    @classmethod
    def _copy(cls, presence: Self, /) -> Self:
        self = cls.__new__(cls)  # bypass __init__
        self.client_status = ClientStatus._copy(presence.client_status)
        self.activities = presence.activities
        self.hidden_activities = presence.hidden_activities
        return self


class FakeClientPresence(Presence):
    __slots__ = ('_state',)

    def __init__(self, state: ConnectionState, /) -> None:
        self._state = state

    def _update(self, data: gw.PresenceUpdateEvent, state: ConnectionState, user_id: int = 0, /) -> None:
        # Same signature as Presence._update, which parse_presence_update calls
        # for every presence. Our own presence is derived from the sessions, so
        # there is nothing to store here.
        return

    @property
    def client_status(self) -> ClientStatus:
        state = self._state
        status = str(getattr(state.all_session, 'status', state.ws.status))
        client_status = {str(session.client): str(session.status) for session in state._sessions.values()}
        return ClientStatus(status, client_status)  # type: ignore

    @property
    def activities(self) -> Tuple[ActivityTypes, ...]:
        return getattr(
            self._state.all_session,
            'activities',
            tuple(create_activity(d, self._state, self._state.self_id) for d in self._state.ws.activities),
        )


class CacheStats(NamedTuple):
    """A snapshot of the client's cache utilization and effectiveness.

    Returned by :meth:`Client.cache_stats`.

    .. versionadded:: 2.2
    """

    guilds: int
    """Total number of cached guilds."""
    members: int
    """Total number of cached members across all guilds."""
    member_cache_cap: Optional[int]
    """The configured per-guild member cache cap, if any."""
    total_member_cache_cap: Optional[int]
    """The configured global member cache cap, if any."""
    evicted_members: int
    """Total members evicted from the cache since the last reset."""
    member_hits: int
    """Total :meth:`Guild.get_member` calls that found a cached member."""
    member_misses: int
    """Total :meth:`Guild.get_member` calls that missed."""
    threads: int
    """Total number of cached threads across all guilds."""
    thread_cache_cap: Optional[int]
    """The configured per-guild thread cache cap, if any."""
    total_thread_cache_cap: Optional[int]
    """The configured global thread cache cap, if any."""
    evicted_threads: int
    """Total threads evicted from the cache since the last reset."""
    guild_presences: int
    """Total stored guild-scoped presences across all guilds."""
    guild_presence_cap: Optional[int]
    """The configured per-guild presence cap, if any."""
    dm_presences: int
    """Total stored non-guild presences (e.g. relationships)."""
    dm_presence_cap: Optional[int]
    """The configured non-guild presence cap, if any."""
    evicted_presences: int
    """Total presences evicted since the last reset."""
    messages: int
    """Number of messages currently in the message cache."""
    message_cache_cap: Optional[int]
    """The configured message cache size, if any."""
    dispatch_backlog: int
    """Event handler tasks created but not yet completed."""
    dispatch_oldest_age: float
    """Seconds the oldest pending event handler has been queued."""
    dispatch_created: int
    """Total event handler tasks created since the last reset."""
    dispatch_completed: int
    """Total event handler tasks completed since the last reset."""
    event_loop_lag: float
    """Measured event loop scheduling delay in seconds at snapshot time.

    ``-1.0`` indicates the loop was stalled for over a second.
    """
    per_guild: Dict[int, Dict[str, int]]
    """A per-guild breakdown with the cached ``members``, ``threads`` and
    ``presences`` counts, plus ``evicted_members`` and ``evicted_threads``,
    for each cached guild."""


async def logging_coroutine(coroutine: Coroutine[Any, Any, T], *, info: str) -> Optional[T]:
    try:
        await coroutine
    except Exception:
        _log.exception('Exception occurred during %s.', info)


def _soundboard_sound_key(sound: SoundboardSound) -> Tuple[Any, ...]:
    # The fields a GUILD_SOUNDBOARD_SOUND_UPDATE can change
    emoji = sound.emoji
    return (sound.name, sound.volume, sound.available, emoji.id if emoji else None, emoji.name if emoji else None)


# event name -> 'on_' + event, for ConnectionState._has_listener
_LISTENER_METHOD_NAMES: Dict[str, str] = {}


class ConnectionState:
    # READY keys that are persisted and replayed. Everything else in a live
    # READY payload is account data — settings protos, connected accounts,
    # pending payments, analytics and auth/session identifiers — with no
    # cache-warmth value, and it must not sit at rest in the snapshot.
    _PERSIST_READY_KEYS: ClassVar[frozenset[str]] = frozenset({'user', 'users', 'relationships', 'private_channels'})

    def __init__(
        self,
        *,
        dispatch: Callable[..., Any],
        handlers: Dict[str, Callable[..., Any]],
        hooks: Dict[str, Callable[..., Coroutine[Any, Any, Any]]],
        http: HTTPClient,
        client: Client,
        **options: Any,
    ) -> None:
        # Set later, after Client.login
        self.loop: asyncio.AbstractEventLoop = utils.MISSING
        self.http: HTTPClient = http
        self.client = client
        self.max_messages: Optional[int] = options.get('max_messages', 1000)
        if self.max_messages is not None and self.max_messages <= 0:
            self.max_messages = 1000

        # Per-guild cap on the member cache; evicts least recently used members.
        # ``None`` disables the cap.
        self.max_member_cache: Optional[int] = options.get('max_member_cache', 10_000)
        if self.max_member_cache is not None and self.max_member_cache <= 0:
            self.max_member_cache = 10_000
        # Global cap on the (non-guild) presence cache. ``None`` disables the cap.
        self.max_presence_cache: Optional[int] = options.get('max_presence_cache', 20_000)
        if self.max_presence_cache is not None and self.max_presence_cache <= 0:
            self.max_presence_cache = 20_000
        # Global cap on the member cache across all guilds; evicts from the
        # fullest guilds first. ``None`` disables the cap.
        self.max_total_member_cache: Optional[int] = options.get('max_total_member_cache', 50_000)
        if self.max_total_member_cache is not None and self.max_total_member_cache <= 0:
            self.max_total_member_cache = 50_000
        self._member_add_counter: int = 0
        # Per-guild cap on the thread cache. ``None`` disables the cap.
        self.max_thread_cache: Optional[int] = options.get('max_thread_cache', 5_000)
        if self.max_thread_cache is not None and self.max_thread_cache <= 0:
            self.max_thread_cache = 5_000
        # Global cap on the thread cache across all guilds. ``None`` disables the cap.
        self.max_total_thread_cache: Optional[int] = options.get('max_total_thread_cache', 25_000)
        if self.max_total_thread_cache is not None and self.max_total_thread_cache <= 0:
            self.max_total_thread_cache = 25_000
        self._thread_add_counter: int = 0
        # Per-guild cap on stored (guild-scoped) presences. ``None`` disables the cap.
        self.max_guild_presence_cache: Optional[int] = options.get('max_guild_presence_cache', 5_000)
        if self.max_guild_presence_cache is not None and self.max_guild_presence_cache <= 0:
            self.max_guild_presence_cache = 5_000
        # Whether to freeze the startup object graph after READY so the cyclic
        # garbage collector stops rescanning long-lived objects
        self.gc_freeze: bool = options.get('gc_freeze', True)
        self._gc_freeze_task: Optional[asyncio.Task] = None
        # True only while THIS state contributes to the process-wide freeze count
        self._holds_gc_freeze: bool = False
        # Opt-in cache persistence: snapshot the raw gateway payloads to disk
        # and replay them on the next startup for a warm cache without
        # re-scraping. ``None`` (default) disables it entirely.
        self.persistence_path: Optional[Union[str, os.PathLike]] = options.get('persistence_path', None)
        # Snapshots older than this many seconds are ignored on startup
        self.persistence_max_age: float = float(options.get('persistence_max_age', PERSISTENCE_DEFAULT_MAX_AGE))
        # Strong references to in-flight background writes (asyncio only keeps weak refs to tasks)
        self._persistence_tasks: Set[asyncio.Task] = set()
        # Serialises writes so an older snapshot can never land over a newer one
        self._persistence_lock: asyncio.Lock = asyncio.Lock()
        self._persistence_ready: Optional[gw.ReadyEvent] = None
        self._persistence_guild_payloads: Dict[int, GuildPayload] = {}
        self._persistence_loaded: bool = False
        # Write ordering that survives cancellation: a cancelled to_thread() wait
        # releases _persistence_lock while its thread still runs, so the rename
        # itself is ordered by generation under a thread lock
        self._persistence_generation: int = 0
        self._persistence_written: int = 0
        self._persistence_write_lock: threading.Lock = threading.Lock()
        self._persistence_guild_ids: Set[int] = set()
        # Cache effectiveness accounting (see ConnectionState.cache_stats)
        self._member_hits: int = 0
        self._member_misses: int = 0
        self._presence_evictions: int = 0
        # Caches that have logged their first eviction (logged once per cache, at INFO)
        self._eviction_logged: Set[str] = set()

        self.dispatch: Callable[..., Any] = dispatch
        self.handlers: Dict[str, Callable[..., Any]] = handlers
        self.hooks: Dict[str, Callable[..., Coroutine[Any, Any, Any]]] = hooks
        self._ready_task: Optional[asyncio.Task] = None
        self.heartbeat_timeout: float = options.get('heartbeat_timeout', 60.0)
        # on_raw_presence_update is opt-in: presence updates are the busiest
        # event, and even checking for a listener costs ~13% of their parse time
        self.raw_presence_flag: bool = bool(options.get('enable_raw_presences', False))

        allowed_mentions = options.get('allowed_mentions')
        if allowed_mentions is not None and not isinstance(allowed_mentions, AllowedMentions):
            raise TypeError('allowed_mentions parameter must be AllowedMentions')

        self.allowed_mentions: Optional[AllowedMentions] = allowed_mentions
        self._chunk_requests: Dict[Union[str, int], ChunkRequest] = {}
        self._scrape_requests: Dict[Union[str, int], MemberSidebar] = {}

        activities = options.get('activities', [])
        if not activities:
            activity = options.get('activity')
            if activity is not None:
                activities = [activity]

        if not all(isinstance(activity, BaseActivity) for activity in activities):
            raise TypeError('activity parameter must derive from BaseActivity')
        activities = [activity.to_dict() for activity in activities]

        status = options.get('status', None)
        if status:
            if status is Status.offline:
                status = 'invisible'
            else:
                status = str(status)

        idle_since = options.get('idle_since', None)
        if idle_since:
            if not isinstance(idle_since, datetime.datetime):
                raise TypeError('idle_since parameter must be a datetime.datetime')
            since = int(idle_since.timestamp() * 1000)
        else:
            since = 0

        chunk_guilds = options.get('chunk_guilds_at_startup', MISSING)
        subscribe_guilds = options.get('guild_subscriptions', options.get('request_guilds', True))

        cache_flags = options.get('member_cache_flags', None)
        if cache_flags is None:
            cache_flags = MemberCacheFlags.all()
        else:
            if not isinstance(cache_flags, MemberCacheFlags):
                raise TypeError(f'member_cache_flags parameter must be MemberCacheFlags not {type(cache_flags)!r}')

        if not cache_flags.joined and chunk_guilds:
            raise ClientException('Cannot chunk guilds at startup without a member cache')
        if chunk_guilds and not subscribe_guilds:
            raise ClientException('Cannot chunk guilds at startup without subscribing to them')

        self._chunk_guilds: bool = chunk_guilds if chunk_guilds is not MISSING else (subscribe_guilds and cache_flags.joined)
        self._subscribe_guilds: bool = subscribe_guilds
        self.member_cache_flags: MemberCacheFlags = cache_flags
        self._activities: List[ActivityPayload] = activities
        self._status: Optional[str] = status
        self._afk: bool = options.get('afk', False)
        self._idle_since: int = since
        self.overriden_rtc_regions: Optional[List[str]] = options.get('preferred_rtc_regions', None)
        self.installation_id: Optional[str] = options.get('installation_id', None)

        if cache_flags._empty:
            self.store_user = self.create_user

        self.parsers: Dict[str, Callable[[Any], None]]
        self.parsers = parsers = {}
        for attr, func in inspect.getmembers(self):
            if attr.startswith('parse_'):
                parsers[attr[6:].upper()] = func

        self.clear(full=True)

    def _release_gc_freeze(self) -> None:
        # Cancel a pending freeze so it cannot land on a half-built graph,
        # then give back this state's hold, if it has one. gc.unfreeze() is
        # process-global, so only the LAST holder actually unfreezes.
        global _gc_freeze_holders
        task = self._gc_freeze_task
        if task is not None and not task.done():
            task.cancel()
        self._gc_freeze_task = None
        if self._holds_gc_freeze:
            self._holds_gc_freeze = False
            _gc_freeze_holders = max(0, _gc_freeze_holders - 1)
            if _gc_freeze_holders == 0:
                gc.unfreeze()

    def clear(self, *, full: bool = False) -> None:
        # The frozen startup graph is about to be dropped en masse; return it
        # to the collectable generations so reconnects don't leak it.
        self._release_gc_freeze()
        # Chunk requests belong to the session that sent them; a fresh READY or a
        # close would otherwise leave them registered forever, hanging whoever waits
        self._fail_pending_chunk_requests('The session was reset before the member chunk request finished.')
        self.user: Optional[ClientUser] = None
        self._users: weakref.WeakValueDictionary[int, User] = weakref.WeakValueDictionary()
        self.settings: Optional[UserSettings] = None
        self.frecency_settings: Optional[FrecencySettings] = None
        self.consents: Optional[TrackingSettings] = None
        self.connections: Dict[str, Connection] = {}
        self.pending_payments: Dict[int, Payment] = {}
        self.analytics_token: Optional[str] = None
        self.preferred_rtc_regions: List[str] = []
        self.country_code: Optional[str] = None
        self.api_code_version: int = 0
        self.session_type: Optional[str] = None
        self.auth_session_id: Optional[str] = None
        self.required_action: Optional[RequiredActionType] = None
        self.friend_suggestion_count: int = 0
        self.disclose: List[str] = []
        self._emojis: Dict[int, Emoji] = {}
        self._stickers: Dict[int, GuildSticker] = {}
        self._guilds: Dict[int, Guild] = {}
        self.tutorial: Tutorial = Tutorial.default(self)

        self._read_states: Dict[int, Dict[int, ReadState]] = {}
        self.read_state_version: int = 0

        self.guild_settings: Dict[Optional[int], GuildSettings] = {}
        self.guild_settings_version: int = 0

        self._join_requests: Dict[int, JoinRequest] = {}
        self._calls: Dict[int, Call] = {}
        self._call_message_cache: OrderedDict[int, Message] = OrderedDict()
        self._voice_clients: Dict[int, VoiceProtocol] = {}
        self._voice_states: Dict[int, VoiceState] = {}
        self._streams: Dict[StreamKey, Stream] = {}
        self._stream_clients: Dict[StreamKey, StreamProtocol] = {}
        self._stream_server_updates: Dict[StreamKey, gw.StreamServerUpdateEvent] = {}

        self._interaction_cache: OrderedDict[Union[int, str], Tuple[int, Optional[str], MessageableChannel]] = OrderedDict()
        self._interactions: OrderedDict[Union[int, str], Interaction] = OrderedDict()
        self._application_command_autocomplete_cache: OrderedDict[
            Union[int, str], Tuple[MessageableChannel, Any, Any, Union[str, int, float]]
        ] = OrderedDict()
        self._interaction_iframe_modals: OrderedDict[int, IFrameModal] = OrderedDict()
        self._relationships: Dict[int, Relationship] = {}
        self._private_channels: Dict[int, PrivateChannel] = {}
        self._private_channels_by_user: Dict[int, DMChannel] = {}

        self._guild_presences: Dict[int, OrderedDict[int, Presence]] = {}
        self._presences: OrderedDict[int, Presence] = OrderedDict()
        self._member_add_counter = 0
        self._thread_add_counter = 0
        self._member_hits = 0
        self._member_misses = 0
        self._presence_evictions = 0
        self._persistence_ready = None
        self._persistence_guild_payloads = {}
        self._sessions: Dict[str, Session] = {}

        if self.max_messages is not None:
            self._messages: Optional[Deque[Message]] = deque(maxlen=self.max_messages)
        else:
            self._messages: Optional[Deque[Message]] = None
        self._messages_index: Dict[int, Message] = {}

        self.experiments: Dict[int, UserExperiment] = {}
        self.guild_experiments: Dict[int, GuildExperiment] = {}
        self.apex_experiments: Dict[int, ApexExperiment] = {}

        if full:
            self.subscriptions: GuildSubscriptions = GuildSubscriptions(self)

    def _note_member_add(self) -> None:
        # Amortized global member cache rebalancing; the actual check is
        # spread out so it does not run on every single insertion
        if self.max_total_member_cache is None:
            return
        self._member_add_counter += 1
        if self._member_add_counter >= 1024:
            self._maybe_evict_members_global()

    def _log_first_eviction(self, cache: str) -> None:
        # Eviction is silent below INFO, so a user whose caches fill up would
        # otherwise not know the caps exist; say so once per cache
        logged = self._eviction_logged
        if cache in logged:
            return
        logged.add(cache)
        _log.info(
            'The %s cache reached its cap and now evicts the least recently used entries. '
            'This is expected on large accounts; raise or disable the max_*_cache options if needed '
            '(see Client.cache_stats()). Logged once per cache.',
            cache,
        )

    def _maybe_evict_members_global(self) -> None:
        cap = self.max_total_member_cache
        if cap is None:
            self._member_add_counter = 0
            return

        self._member_add_counter = 0
        guilds = [guild for guild in self._guilds.values() if guild._members]
        total = sum(map(len, (guild._members for guild in guilds)))
        if total <= cap:
            return

        # Converge every guild toward the average guild size implied by the cap.
        # Guilds at or below the average are left untouched, so small guilds
        # the user actually participates in keep their members.
        n = len(guilds)
        average_target = cap // n
        guilds.sort(key=lambda guild: len(guild._members), reverse=True)
        excess = total - cap
        for guild in guilds:
            if excess <= 0:
                break
            before = len(guild._members)
            if before <= average_target:
                continue
            # Evict towards the average, but never more than the excess needs
            guild._evict_members(max(average_target, before - excess))
            excess -= before - len(guild._members)

        _log.debug(
            'Rebalanced the member cache: %d -> %d members (cap: %d).',
            total,
            sum(map(len, (guild._members for guild in guilds))),
            cap,
        )

    def _note_thread_add(self) -> None:
        # Amortized global thread cache rebalancing
        if self.max_total_thread_cache is None:
            return
        self._thread_add_counter += 1
        if self._thread_add_counter >= 1024:
            self._maybe_evict_threads_global()

    def _maybe_evict_threads_global(self) -> None:
        cap = self.max_total_thread_cache
        if cap is None:
            self._thread_add_counter = 0
            return

        self._thread_add_counter = 0
        guilds = [guild for guild in self._guilds.values() if guild._threads]
        total = sum(map(len, (guild._threads for guild in guilds)))
        if total <= cap:
            return

        # Same convergence policy as the member cache
        n = len(guilds)
        average_target = cap // n
        guilds.sort(key=lambda guild: len(guild._threads), reverse=True)
        excess = total - cap
        for guild in guilds:
            if excess <= 0:
                break
            before = len(guild._threads)
            if before <= average_target:
                continue
            guild._evict_threads(max(average_target, before - excess))
            excess -= before - len(guild._threads)

        _log.debug(
            'Rebalanced the thread cache: %d -> %d threads (cap: %d).',
            total,
            sum(map(len, (guild._threads for guild in guilds))),
            cap,
        )

    def _fail_pending_chunk_requests(self, reason: str) -> None:
        requests = self._chunk_requests
        if not requests:
            return
        exc = ClientException(reason)
        for request in list(requests.values()):
            request.fail(exc)
        requests.clear()

    def process_chunk_requests(self, guild_id: int, nonce: Optional[str], members: List[Member], complete: bool) -> None:
        removed = []
        for key, request in self._chunk_requests.items():
            if request.guild_id == guild_id and request.nonce == nonce:
                request.add_members(members)
                if complete:
                    request.done()
                    if request.oneshot:
                        removed.append(key)

        for key in removed:
            del self._chunk_requests[key]

    def call_handlers(self, key: str, *args: Any, **kwargs: Any) -> None:
        try:
            func = self.handlers[key]
        except KeyError:
            pass
        else:
            func(*args, **kwargs)

    async def call_hooks(self, key: str, *args: Any, **kwargs: Any) -> None:
        try:
            coro = self.hooks[key]
        except KeyError:
            pass
        else:
            await coro(*args, **kwargs)

    async def async_setup(self) -> None:
        pass

    @property
    def session_id(self) -> Optional[str]:
        if self.ws:
            return self.ws.session_id

    @property
    def ws(self):
        return self.client.ws

    @property
    def self_id(self) -> int:
        u = self.user
        return u.id if u else None  # type: ignore

    @property
    def locale(self) -> str:
        return str(getattr(self.user, 'locale', 'en-US'))

    @property
    def parsed_locale(self) -> Locale:
        return getattr(self.user, 'locale', Locale.american_english)

    @property
    def voice_clients(self) -> List[VoiceProtocol]:
        return list(self._voice_clients.values())

    def _update_voice_state(
        self, data: VoiceStatePayload, channel_id: Optional[int]
    ) -> Tuple[Optional[User], VoiceState, VoiceState]:
        user_id = int(data['user_id'])
        user = self.get_user(user_id)
        channel: Optional[Union[DMChannel, GroupChannel]] = self._get_private_channel(channel_id)

        try:
            # Check if we should remove the voice state from cache
            if channel is None:
                after = self._voice_states.pop(user_id)
            else:
                after = self._voice_states[user_id]

            before = copy.copy(after)
            after._update(data, channel)
        except KeyError:
            # if we're here then add it into the cache
            after = VoiceState(data=data, channel=channel)
            before = VoiceState(data=data, channel=None)
            # A leave for a user we never saw join has nothing to cache: storing
            # a channel-less state would leak one entry per such event
            if channel is not None:
                self._voice_states[user_id] = after

        return user, before, after

    def _voice_state_for(self, user_id: int) -> Optional[VoiceState]:
        return self._voice_states.get(user_id)

    def _has_voice_client(self) -> bool:
        return bool(self._voice_clients)

    def _get_voice_client(self, guild_id: Optional[int]) -> Optional[VoiceProtocol]:
        # The keys of self._voice_clients are ints
        return self._voice_clients.get(guild_id)  # type: ignore

    def _add_voice_client(self, guild_id: int, voice: VoiceProtocol) -> None:
        self._voice_clients[guild_id] = voice

    def _remove_voice_client(self, guild_id: int) -> None:
        self._voice_clients.pop(guild_id, None)

    def _get_voice_client_for_stream_key(self, stream_key: StreamKey) -> Optional[VoiceProtocol]:
        if stream_key.type is StreamType.guild:
            voice = self._get_voice_client(stream_key.guild_id)
        elif stream_key.type is StreamType.call:
            voice = self._get_voice_client(self.self_id)
        else:
            return None

        if voice is None:
            return None

        if stream_key.channel_id is not None and voice.channel.id != stream_key.channel_id:
            return None
        return voice

    def _stream_key_for_voice_client(self, voice_client: VoiceProtocol) -> StreamKey:
        user: ClientUser = self.user  # type: ignore
        channel = voice_client.channel
        guild = getattr(channel, 'guild', None)
        if guild is not None:
            return StreamKey.from_guild(guild_id=guild.id, channel_id=channel.id, owner_id=user.id)
        return StreamKey.from_call(channel_id=channel.id, owner_id=user.id)

    def _streams_for_voice_client(self, voice_client: VoiceProtocol) -> Tuple[Stream, ...]:
        return tuple(
            stream for stream in self._streams.values() if self._get_voice_client_for_stream_key(stream.key) is voice_client
        )

    def _stream_clients_for_voice_client(self, voice_client: VoiceProtocol) -> Tuple[StreamProtocol, ...]:
        return tuple(stream for stream in self._stream_clients.values() if stream.voice_client is voice_client)

    def get_stream(self, stream_key: StreamKey) -> Optional[Stream]:
        return self._streams.get(stream_key)

    def _store_stream(self, data: gw.StreamEvent) -> Tuple[Optional[Stream], Stream]:
        stream_key = StreamKey.from_value(data['stream_key'])
        stream = self._streams.get(stream_key)
        if stream is None:
            stream = Stream(state=self, data=data)
            self._streams[stream.key] = stream
            return None, stream

        old = copy.copy(stream)
        stream._update(data)
        return old, stream

    def _get_stream_client(self, stream_key: StreamKey) -> Optional[StreamProtocol]:
        return self._stream_clients.get(stream_key)

    def _add_stream_client(self, stream_key: StreamKey, stream: StreamProtocol) -> None:
        self._stream_clients[stream_key] = stream

    def _remove_stream_client(self, stream_key: StreamKey) -> None:
        self._stream_clients.pop(stream_key, None)

    async def _connect_stream(
        self,
        voice_client: VoiceProtocol,
        stream_key: StreamKey,
        request: Callable[[], Coroutine[Any, Any, None]],
        *,
        cls: Callable[[VoiceProtocol, Stream], ST],
        timeout: float,
        reconnect: bool,
    ) -> ST:
        existing = self._get_stream_client(stream_key)
        if existing is not None:
            return cast(ST, existing)

        def same_create(stream: Stream) -> bool:
            return stream.key == stream_key

        def same_delete(stream: Stream, reason: StreamDeleteReason) -> bool:
            return stream.key == stream_key

        create_task = self.loop.create_task(
            self.client.wait_for('stream_create', check=same_create, timeout=timeout),
            name='Stream create waiter',
        )
        delete_task = self.loop.create_task(
            self.client.wait_for('stream_delete', check=same_delete, timeout=timeout),
            name='Stream delete waiter',
        )
        tasks = {create_task, delete_task}
        stream: Optional[Stream] = None

        try:
            await asyncio.sleep(0)
            await request()
            while stream is None:
                done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise asyncio.TimeoutError
                for task in done:
                    tasks.discard(task)
                    if task is delete_task:
                        _, reason = task.result()
                        raise ClientException(f'Stream connection rejected: {reason}')
                    stream = task.result()
        finally:
            for task in tasks:
                task.cancel()

        protocol = cls(voice_client, stream)
        if not isinstance(protocol, StreamProtocol):
            raise TypeError('Type must meet StreamProtocol abstract base class')

        self._add_stream_client(stream.key, protocol)
        try:
            server_update = self._stream_server_updates.get(stream.key)
            if server_update is not None:
                await protocol.on_stream_server_update(server_update)
            await protocol.connect(timeout=timeout, reconnect=reconnect)
        except asyncio.TimeoutError:
            try:
                await protocol.disconnect(force=True)
            except Exception:
                pass
            raise
        except Exception:
            protocol.cleanup()
            raise

        return protocol

    def _get_preferred_regions(self) -> Dict[str, Union[List[str], str]]:
        regions = self.overriden_rtc_regions if self.overriden_rtc_regions is not None else self.client.preferred_rtc_regions
        if regions:
            return {'preferred_regions': regions, 'preferred_region': regions[0]}
        return {}

    def _update_references(self, ws: DiscordWebSocket) -> None:
        for vc in self.voice_clients:
            vc.main_ws = ws  # type: ignore # Silencing the unknown attribute (ok at runtime).

    def _add_interaction(self, interaction: Interaction) -> None:
        interactions = self._interactions
        if interaction.id in interactions:
            interactions.move_to_end(interaction.id)
        interactions[interaction.id] = interaction
        if len(interactions) > 100:
            interactions.popitem(last=False)

    def _pop_interaction(self, interaction: Interaction, data: gw.InteractionEvent) -> None:
        self._interactions.pop(interaction.id, None)
        nonce = data.get('nonce', interaction.nonce)
        if nonce is not None:
            self._application_command_autocomplete_cache.pop(nonce, None)
            # Present when INTERACTION_CREATE never arrived (terminal-event fallback)
            self._interaction_cache.pop(nonce, None)

    def _get_interaction_from_terminal_event(self, data: Mapping[str, Any]) -> Optional[Interaction]:
        interaction_id = int(data['id'])
        interaction = self._interactions.get(interaction_id)
        if interaction is not None:
            return interaction

        nonce = data.get('nonce')
        if nonce is None:
            return None

        cached = self._interaction_cache.get(nonce)
        if cached is not None:
            type, name, channel = cached
            return Interaction._from_self(
                channel,  # pyright: ignore[reportArgumentType]
                id=data['id'],
                type=type,
                nonce=nonce,
                user=self.user,  # type: ignore # self.user is always present here
                name=name,
            )

        autocomplete = self._application_command_autocomplete_cache.get(nonce)
        if autocomplete is not None:
            channel, command, _, _ = autocomplete
            return Interaction._from_self(
                channel,  # pyright: ignore[reportArgumentType]
                id=data['id'],
                type=InteractionType.autocomplete.value,
                nonce=nonce,
                user=self.user,  # type: ignore # self.user is always present here
                name=command.name,
            )

        return None

    def store_user(self, data: Union[UserPayload, PartialUserPayload], *, cache: bool = True) -> User:
        # this way is 300% faster than `dict.setdefault`.
        user_id = int(data['id'])
        try:
            return self._users[user_id]
        except KeyError:
            user = User(state=self, data=data)
            if cache:
                # Key on the user's own id int so each cached user holds one int, not two
                self._users[user.id] = user
            return user

    def create_user(self, data: Union[UserPayload, PartialUserPayload], cache: bool = False) -> User:
        user_id = int(data['id'])
        if user_id == self.self_id:
            return self.user  # type: ignore
        return User(state=self, data=data)

    def get_user(self, id: int) -> Optional[User]:
        return self._users.get(id)

    def store_emoji(self, guild: Guild, data: EmojiPayload) -> Emoji:
        # The id will be present here
        emoji_id = int(data['id'])  # type: ignore
        emoji = Emoji(guild=guild, state=self, data=data)
        if not self.is_guild_evicted(guild):
            self._emojis[emoji_id] = emoji
        return emoji

    def store_sticker(self, guild: Guild, data: GuildStickerPayload) -> GuildSticker:
        sticker_id = int(data['id'])
        sticker = GuildSticker(state=self, data=data)
        if not self.is_guild_evicted(guild):
            self._stickers[sticker_id] = sticker
        return sticker

    @property
    def guilds(self) -> Sequence[Guild]:
        return utils.SequenceProxy(self._guilds.values())

    def _get_guild(self, guild_id: Optional[int], /) -> Optional[Guild]:
        # The keys of self._guilds are ints
        return self._guilds.get(guild_id)  # type: ignore

    def _get_or_create_unavailable_guild(self, guild_id: int, /) -> Guild:
        return self._guilds.get(guild_id) or Guild._create_unavailable(state=self, guild_id=guild_id)

    def _add_guild(self, guild: Guild, /) -> None:
        self._guilds[guild.id] = guild

    def _remove_guild(self, guild: Guild, /) -> None:
        self._guilds.pop(guild.id, None)
        self._guild_presences.pop(guild.id, None)
        self.guild_settings.pop(guild.id, None)

        # Purge read states belonging to this guild's channels
        channel_ids = set(guild._channels)
        channel_ids.update(guild._threads)
        if channel_ids:
            channel_states = self._read_states.get(ReadStateType.channel.value)
            if channel_states:
                for cid in channel_ids:
                    channel_states.pop(cid, None)

        # Nuke guild expressions
        for emoji in guild.emojis:
            self._emojis.pop(emoji.id, None)

        for sticker in guild.stickers:
            self._stickers.pop(sticker.id, None)

        # Streams in this guild's channels can no longer be watched or ended
        guild_id = guild.id
        for streams in (self._streams, self._stream_server_updates):
            stale = [key for key in streams if key.guild_id == guild_id]
            for key in stale:
                del streams[key]

    def create_guild(self, guild: BaseGuildPayload, /) -> Guild:
        return Guild(data=guild, state=self)

    @property
    def emojis(self) -> Sequence[Emoji]:
        return utils.SequenceProxy(self._emojis.values())

    @property
    def stickers(self) -> Sequence[GuildSticker]:
        return utils.SequenceProxy(self._stickers.values())

    def get_emoji(self, emoji_id: Optional[int]) -> Optional[Emoji]:
        # the keys of self._emojis are ints
        return self._emojis.get(emoji_id)  # type: ignore

    def get_sticker(self, sticker_id: Optional[int]) -> Optional[GuildSticker]:
        # the keys of self._stickers are ints
        return self._stickers.get(sticker_id)  # type: ignore

    @property
    def private_channels(self) -> Sequence[PrivateChannel]:
        return utils.SequenceProxy(self._private_channels.values())

    async def call_connect(self, channel_id: int) -> None:
        # This is now no longer needed with the AUTO_CALL_CONNECT capability
        if self.ws is None:
            return

        await self.ws.call_connect(channel_id)

    def _get_private_channel(self, channel_id: Optional[int]) -> Optional[PrivateChannel]:
        # The keys of self._private_channels are ints
        return self._private_channels.get(channel_id)  # type: ignore

    def _get_private_channel_by_user(self, user_id: Optional[int]) -> Optional[DMChannel]:
        # The keys of self._private_channels are ints
        return self._private_channels_by_user.get(user_id)  # type: ignore

    def _add_private_channel(self, channel: PrivateChannel) -> None:
        channel_id = channel.id
        self._private_channels[channel_id] = channel

        if isinstance(channel, DMChannel) and channel.recipient:
            self._private_channels_by_user[channel.recipient.id] = channel

    def add_dm_channel(self, data: DMChannelPayload) -> DMChannel:
        # self.user is *always* cached when this is called
        channel = DMChannel(me=self.user, state=self, data=data)  # type: ignore
        self._add_private_channel(channel)
        return channel

    def _remove_private_channel(self, channel: PrivateChannel) -> None:
        self._private_channels.pop(channel.id, None)
        # A call can outlive its channel when no CALL_DELETE follows the delete
        self._calls.pop(channel.id, None)
        if isinstance(channel, DMChannel):
            recipient = channel.recipient
            if recipient is not None:
                self._private_channels_by_user.pop(recipient.id, None)

    def _get_message(self, msg_id: Optional[int]) -> Optional[Message]:
        if msg_id is None:
            return None
        if self._messages:
            message = self._messages_index.get(msg_id)
            if message is not None:
                return message
        # Call messages (keyed by message id) may live outside the main cache
        return self._call_message_cache.get(msg_id)

    def _add_message_to_cache(self, message: Message) -> None:
        messages = self._messages
        if messages is None:
            return
        index = self._messages_index
        if messages.maxlen is not None and len(messages) == messages.maxlen:
            # The append is about to evict the oldest entry
            oldest = messages[0]
            if index.get(oldest.id) is oldest:
                del index[oldest.id]
        messages.append(message)
        index[message.id] = message

    def _remove_message_from_cache(self, message: Message) -> None:
        messages = self._messages
        if messages is not None:
            try:
                messages.remove(message)
            except ValueError:
                pass  # the message may only live in the call cache
        index = self._messages_index
        if index.get(message.id) is message:
            del index[message.id]
        self._call_message_cache.pop(message.id, None)

    def _remove_messages_from_cache(self, found: List[Message], ids: Set[int]) -> None:
        # Bulk form of _remove_message_from_cache: one pass over the deque
        # instead of one O(n) deque.remove() per deleted message
        messages = self._messages
        if messages is not None:
            kept = [message for message in messages if message.id not in ids]
            if len(kept) != len(messages):
                messages.clear()  # in place: the deque object (and maxlen) stays the same
                messages.extend(kept)
        index = self._messages_index
        call_cache = self._call_message_cache
        for message in found:
            if index.get(message.id) is message:
                del index[message.id]
            call_cache.pop(message.id, None)

    def _add_guild_from_data(self, data: GuildPayload) -> Guild:
        guild = self.create_guild(data)
        self._add_guild(guild)
        return guild

    def _guild_needs_chunking(self, guild: Guild) -> bool:
        return self._chunk_guilds and not guild.chunked and not guild.unavailable

    async def _can_chunk_guild(self, guild: Guild) -> bool:
        if not guild.me:
            await guild.query_members(user_ids=[self.self_id], cache=True)

        return guild.me is not None and any(
            (
                guild.me.guild_permissions.kick_members,
                guild.me.guild_permissions.ban_members,
                guild.me.guild_permissions.manage_roles,
            )
        )

    def _get_guild_channel(
        self, data: PartialMessagePayload, guild_id: Optional[int] = None
    ) -> Tuple[Union[Channel, Thread], Optional[Guild]]:
        channel_id = int(data['channel_id'])
        try:
            guild_id = guild_id or int(data['guild_id'])  # pyright: ignore[reportTypedDictNotRequiredAccess]
            guild = self._get_guild(guild_id)
        except KeyError:
            channel = self.get_channel(channel_id)
            guild = None
        else:
            channel = guild and guild._resolve_channel(channel_id)

        return channel or PartialMessageable(state=self, guild_id=guild_id, id=channel_id), guild

    async def _delete_messages(self, channel_id, messages, reason: Optional[str] = None) -> None:
        delete_message = self.http.delete_message
        for msg in messages:
            try:
                await delete_message(channel_id, msg.id, reason=reason)
            except NotFound as exc:
                if exc.code == 10008:
                    continue  # bulk deletion ignores not found messages, single deletion does not.
                # several other race conditions with deletion should fail without continuing,
                # such as the channel being deleted and not found.
                raise

    def _update_poll_counts(self, message: Message, answer_id: int, added: bool, self_voted: bool = False) -> Optional[Poll]:
        poll = message.poll
        if not poll:
            return
        poll._handle_vote(answer_id, added, self_voted)
        return poll

    def _update_poll_results(self, from_: Message, to: Union[Message, int]) -> None:
        if isinstance(to, Message):
            cached = self._get_message(to.id)
        elif isinstance(to, int):
            cached = self._get_message(to)

            if cached is None:
                return

            to = cached
        else:
            return

        if to.poll is None:
            return

        to.poll._update_results_from_message(from_)

        if cached is not None and cached.poll:
            cached.poll._update_results_from_message(from_)

    def subscribe_guild(
        self, guild: Guild, typing: bool = True, activities: bool = True, threads: bool = True, member_updates: bool = True
    ) -> Coroutine:
        return self.subscriptions.subscribe_to(
            guild, typing=typing, activities=activities, threads=threads, member_updates=member_updates
        )

    def chunker(
        self,
        guild_ids: List[Snowflake],
        query: Optional[str] = '',
        limit: int = 0,
        presences: bool = True,
        *,
        user_ids: Optional[List[Snowflake]] = None,
        nonce: Optional[str] = None,
    ):
        return self.ws.request_chunks(
            guild_ids, query=query, limit=limit, presences=presences, user_ids=user_ids, nonce=nonce
        )

    async def query_members(
        self,
        guild: Guild,
        query: Optional[str],
        limit: int,
        user_ids: Optional[List[Snowflake]],
        cache: bool,
        presences: bool,
    ) -> List[Member]:
        guild_id = guild.id
        request = ChunkRequest(guild.id, self.loop, self._get_guild, cache=cache)
        self._chunk_requests[request.nonce] = request

        try:
            await self.chunker(
                [guild_id], query=query, limit=limit, presences=presences, user_ids=user_ids, nonce=request.nonce
            )
            return await asyncio.wait_for(request.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            _log.warning('Timed out waiting for chunks with query %r and limit %d for guild ID %d.', query, limit, guild_id)
            raise
        finally:
            # Removed on completion already; on a timeout or send error it would
            # otherwise stay registered for the life of the process
            self._chunk_requests.pop(request.nonce, None)

    async def search_recent_members(
        self,
        guild: Guild,
        query: str = '',
        limit: Optional[int] = None,
        cache: bool = False,
    ) -> List[Member]:
        guild_id = guild.id
        request = ChunkRequest(guild.id, self.loop, self._get_guild, limit=limit, cache=cache, oneshot=False, reverse=True)
        self._chunk_requests[request.nonce] = request

        # Unlike query members, this OP is paginated
        try:
            return await self._search_recent_members_pages(guild_id, request, query, limit)
        finally:
            self._chunk_requests.pop(request.nonce, None)

    async def _search_recent_members_pages(
        self, guild_id: int, request: ChunkRequest, query: str, limit: Optional[int]
    ) -> List[Member]:
        old_continuation_token = None
        continuation_token = None
        while True:
            try:
                await self.ws.search_recent_members(guild_id, query=query, nonce=request.nonce, after=continuation_token)
                returned = await asyncio.wait_for(request.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                _log.warning(
                    'Timed out waiting for search chunks with query %r and limit %d for guild ID %d.',
                    query,
                    limit,
                    guild_id,
                )
                raise

            if (limit is not None and request.remaining < 1) or len(returned) < 1:
                break

            # Sort the members by joined_at timestamp and grab the oldest one
            old_continuation_token = continuation_token
            continuation_token = request.buffer[-1].id
            if continuation_token == old_continuation_token:
                break

        return list(request.buffer)

    async def _delay_ready(self) -> None:
        manager = self.subscriptions
        manager.blocked = True

        try:
            # Try to only send one OP each
            member_nonce = utils._generate_nonce()
            states = []
            to_chunk = []

            if not manager.empty:
                await manager._requeue_subscriptions()

            for guild in self._guilds.values():
                if self._subscribe_guilds:
                    await self.subscribe_guild(guild)

                if self._guild_needs_chunking(guild):
                    if await self._can_chunk_guild(guild):
                        future = await self.chunk_guild(guild, wait=False, nonce=member_nonce)
                        to_chunk.append(guild.id)
                        states.append((guild, future))
                    elif not guild._offline_members_hidden:
                        request = MemberSidebar(guild, MISSING, chunk=True, cache=True, loop=self.loop, delay=0)
                        if not request.channels:
                            # Not possible to scrape here
                            continue

                        self._scrape_requests[guild.id] = request
                        request.start()
                        states.append((guild, request.get_future()))

            manager.blocked = False
            await self.chunker(to_chunk, nonce=member_nonce)

            # Every guild's deadline runs from the same start: the requests are all
            # in flight at once, so N stalled guilds cost the longest timeout, not
            # the sum of all of them (which held READY for hours at 1000 guilds)
            started = self.loop.time()
            for guild, future in states:
                timeout = max(0.0, started + self._chunk_timeout(guild) - self.loop.time())

                try:
                    await asyncio.wait_for(future, timeout=timeout)
                except asyncio.TimeoutError:
                    _log.warning('Timed out waiting for chunks for guild ID %s.', guild.id)
                except (ClientException, InvalidData):
                    pass
        except asyncio.CancelledError:
            pass
        except Exception:
            # e.g. the gateway closed while chunks were being requested; the
            # reconnect brings a new READY. Nothing awaits this task, so say why
            # READY did not complete instead of dropping the error silently.
            _log.warning('The ready sequence failed; waiting for the next READY.', exc_info=True)
        else:
            # Dispatch the event
            self.call_handlers('ready')
            self.dispatch('ready')
            self._schedule_gc_freeze()
            if self.persistence_path is not None:
                task = asyncio.create_task(self._write_persistence())
                self._persistence_tasks.add(task)
                task.add_done_callback(self._persistence_tasks.discard)
        finally:
            # Make sure we don't block it forever
            manager.blocked = False
            self._ready_task = None

    def _schedule_gc_freeze(self) -> None:
        # The cyclic GC periodically rescans every tracked object; after READY
        # the cache graph is long-lived and will never form collectable cycles,
        # so freezing it cuts GC pause times and background CPU.
        if not self.gc_freeze:
            return
        if self._gc_freeze_task is not None and not self._gc_freeze_task.done():
            return

        async def freeze_later() -> None:
            # Give ready handlers a moment to finish before freezing the graph
            global _gc_freeze_holders
            await asyncio.sleep(2.0)
            if self._holds_gc_freeze:
                return
            t0 = time.perf_counter()
            gc.collect()
            elapsed = time.perf_counter() - t0
            gc.freeze()
            _gc_freeze_holders += 1
            self._holds_gc_freeze = True
            _log.debug('Froze the startup object graph for the garbage collector (collect took %.1f ms).', elapsed * 1000)

        self._gc_freeze_task = asyncio.create_task(freeze_later())

    @_gc_paused
    def parse_ready(self, data: gw.ReadyEvent) -> None:
        if self._ready_task is not None:
            self._ready_task.cancel()

        if self._persistence_loaded:
            # A replayed cache exists: preserve the guild caches (and their
            # members/presences/threads) across the fresh READY so they don't
            # need to be re-scraped; everything else is re-parsed fresh below
            preserved_guilds = self._guilds
            preserved_guild_presences = self._guild_presences
            self.clear()
            self._guilds = preserved_guilds
            self._guild_presences = preserved_guild_presences
            self._persistence_loaded = False
        else:
            self.clear()

        if self.persistence_path is not None:
            self._persistence_ready = data
        self._ready_data = data

        # Clear the ACK token
        self.http.ack_token = None

        # Self parsing
        self.user = user = ClientUser(state=self, data=data['user'])
        self._users[user.id] = user  # type: ignore

        # Read state parsing
        read_states = data.get('read_state', {})
        for read_state in read_states.get('entries', []):
            item = ReadState(state=self, data=read_state)
            self.store_read_state(item)
        self.read_state_version = read_states.get('version', 0)

        # Guild settings parsing
        guild_settings = data.get('user_guild_settings', {})
        self.guild_settings = {
            utils._get_as_snowflake(entry, 'guild_id'): GuildSettings(data=entry, state=self)
            for entry in guild_settings.get('entries', [])
        }
        self.guild_settings_version = guild_settings.get('version', 0)

        # Experiments
        self.experiments = {exp[0]: UserExperiment(state=self, data=exp) for exp in data.get('experiments', [])}
        self.guild_experiments = {exp[0]: GuildExperiment(state=self, data=exp) for exp in data.get('guild_experiments', [])}
        self.apex_experiments, self.apex_experiment_assignments, installation_id = ApexExperiment.parse(
            self, data.get('apex_experiments')
        )
        if installation_id is not None:
            # If omitted, it means we passed a valid one in IDENTIFY, so don't overwrite it
            self.installation_id = installation_id

        # Extras
        self.analytics_token = data.get('analytics_token')
        self.preferred_rtc_regions = data.get('geo_ordered_rtc_regions', ['us-central'])
        self.settings = UserSettings(self, data.get('user_settings_proto') or '')
        self.consents = TrackingSettings(data=data.get('consents', {}), state=self)
        self.country_code = data.get('country_code', 'US')
        self.api_code_version = data.get('api_code_version', 1)
        self.session_type = data.get('session_type', 'normal')
        self.auth_session_id = data.get('auth_session_id_hash')
        self.connections = {c['id']: Connection(state=self, data=c) for c in data.get('connected_accounts', [])}
        self.pending_payments = {int(p['id']): Payment(state=self, data=p) for p in (data.get('pending_payments') or [])}
        self.required_action = try_enum(RequiredActionType, data['required_action']) if 'required_action' in data else None
        self.friend_suggestion_count = data.get('friend_suggestion_count', 0)

        if 'sessions' in data:
            self.parse_sessions_replace(data['sessions'], from_ready=True)

        if 'auth_token' in data:
            self.http._token(data['auth_token'])

        if 'tutorial' in data and data['tutorial']:
            self.tutorial = Tutorial(state=self, data=data['tutorial'])

        # Before parsing the rest, we wait for READY_SUPPLEMENTAL
        # This has voice state objects as well as an initial member cache

    @_gc_paused
    def parse_ready_supplemental(self, extra_data: gw.ReadySupplementalEvent) -> None:
        data = self._ready_data

        # Temp user parsing
        user = self.user
        temp_users: Dict[int, PartialUserPayload] = {int(data['user']['id']): data['user']}
        for u in data.get('users', []):
            u_id = int(u['id'])
            temp_users[u_id] = u

        # Merge all the fragmented guild data
        for guild_data, guild_extra, merged_members, merged_me, merged_presences in zip(
            data.get('guilds', []),
            extra_data.get('guilds', []),
            extra_data.get('merged_members', []),
            data.get('merged_members', []),
            extra_data['merged_presences'].get('guilds', []),
        ):
            for presence in merged_presences:
                if 'user' not in presence:
                    presence['user'] = {'id': presence['user_id']}

            if 'properties' in guild_data:
                guild_data.update(guild_data.pop('properties'))

            voice_states = guild_data.setdefault('voice_states', [])
            voice_states.extend(guild_extra.get('voice_states', []))
            members = guild_data.setdefault('members', [])
            members.extend(merged_me)
            members.extend(merged_members)
            presences = guild_data.setdefault('presences', [])
            presences.extend(merged_presences)

            for member in members:
                if 'user' not in member:
                    # Payloads without an inline user carry user_id: resolve
                    # against the temp users; a missing/unresolvable id must
                    # skip rather than raise
                    user_id = member.pop('user_id', None)
                    if user_id is None:
                        continue
                    try:
                        member['user'] = temp_users.get(int(user_id))
                    except (TypeError, ValueError):
                        continue

            # Index members by user id once instead of scanning the whole
            # member list per voice state (O(members x voice states) before)
            members_by_id: Dict[Any, Any] = {}
            for member in members:
                user = member.get('user')
                if isinstance(user, dict) and 'id' in user:
                    members_by_id[user['id']] = member
            for voice_state in voice_states:
                if 'member' not in voice_state:
                    member = members_by_id.get(voice_state.get('user_id'))
                    if member is not None:
                        voice_state['member'] = member

        # Guild parsing
        fresh_guild_ids: Set[int] = set()
        for guild_data in data.get('guilds', []):
            guild_id = utils._get_as_snowflake(guild_data, 'id')
            if guild_id is not None:
                fresh_guild_ids.add(guild_id)
            if self.persistence_path is not None and guild_id is not None:
                self._persistence_guild_payloads[guild_id] = guild_data
            existing = self._get_guild(guild_id) if guild_id is not None else None
            if existing is not None and guild_id in self._persistence_guild_ids:
                # Merge fresh data into the replayed guild, keeping its
                # cached members/presences/threads (in-place reparse)
                existing._from_data(guild_data)
            else:
                self._add_guild_from_data(guild_data)

        if self._persistence_guild_ids:
            # Prune replayed guilds the account is no longer a member of
            for guild_id in self._persistence_guild_ids - fresh_guild_ids:
                guild = self._guilds.get(guild_id)
                if guild is not None:
                    self._remove_guild(guild)
                    _log.debug('Pruned stale persisted guild ID %d.', guild_id)
            self._persistence_guild_ids = set()

        # Join request parsing
        for request in data.get('guild_join_requests', []):
            join_request = JoinRequest(data=request, state=self)
            self._join_requests[join_request.guild_id] = join_request

        # Relationship parsing
        for relationship in data.get('relationships', []):
            try:
                r_id = int(relationship['id'])
            except KeyError:
                continue
            else:
                if 'user' not in relationship:
                    relationship['user'] = temp_users[int(relationship.pop('user_id'))]
                self._relationships[r_id] = Relationship(state=self, data=relationship)

        # Relationship presence parsing
        for presence in extra_data['merged_presences'].get('friends', []):
            user_id = int(presence.pop('user_id'))  # type: ignore
            self.store_presence(user_id, self.create_presence(presence, user_id))

        # Private channel parsing
        for pm in data.get('private_channels', []) + extra_data.get('lazy_private_channels', []):
            factory, _ = _private_channel_factory(pm['type'])
            if factory is None:
                # A private channel type this version does not know: skipping it
                # keeps READY_SUPPLEMENTAL (whose errors abort the connection) alive
                _log.debug('Skipping private channel ID %s with unknown type %s in READY.', pm.get('id'), pm['type'])
                continue
            try:
                if 'recipients' not in pm:
                    pm['recipients'] = [temp_users[int(u_id)] for u_id in pm.pop('recipient_ids')]  # type: ignore
                channel = factory(me=user, data=pm, state=self)  # type: ignore
            except (KeyError, IndexError, TypeError, ValueError):
                # A malformed entry (e.g. a DM with no recipient) must not abort
                # READY_SUPPLEMENTAL, whose errors drop the connection
                _log.warning('Skipping malformed private channel ID %s in READY.', pm.get('id'), exc_info=True)
                continue
            self._add_private_channel(channel)

        # Disloses
        self.disclose = data.get('disclose', [])

        # We're done
        del self._ready_data
        self.call_handlers('connect')
        self.dispatch('connect')
        self._ready_task = asyncio.create_task(self._delay_ready())

    async def _write_persistence(self) -> bool:
        """Atomically writes the captured payloads to ``persistence_path``.

        The snapshot contains the raw READY payload, so it is treated as a secret:
        the file is created with owner-only permissions via a uniquely named
        temporary file in the same directory, flushed to disk, then renamed over
        the destination.
        """
        path = self.persistence_path
        if path is None:
            return False

        # Capture, write and consume under the lock: concurrent writers must
        # not each snapshot the same capture, or the consume-on-write below
        # would be defeated and once-per-capture would not hold
        async with self._persistence_lock:
            if self._persistence_ready is None:
                # Without the READY section there is nothing replayable: the
                # guild payloads alone cannot reconstruct the account context
                return False

            payload_ready_source = self._persistence_ready
            self._persistence_generation += 1
            generation = self._persistence_generation
            header = {
                'version': PERSISTENCE_FORMAT_VERSION,
                'saved_at': datetime.datetime.now(datetime.timezone.utc).timestamp(),
                'user_id': self.self_id,
                'ready': {k: v for k, v in payload_ready_source.items() if k in self._PERSIST_READY_KEYS},
            }
            guild_payloads = list(self._persistence_guild_payloads.values())

            def _write() -> Optional[int]:
                # One record per line, encoded and compressed as it goes: the
                # whole snapshot never exists as a single string in memory
                directory = os.path.dirname(os.path.abspath(os.fspath(path)))
                # mkstemp creates the file with mode 0600 and a unique name
                fd, tmp = tempfile.mkstemp(dir=directory, prefix='.persist-', suffix='.tmp')
                oversized: Optional[int] = None
                try:
                    with os.fdopen(fd, 'wb') as fp:
                        compressor = zlib.compressobj(1)
                        total = 0
                        for record in (header, *guild_payloads):
                            line = utils._to_json(record).encode('utf-8') + b'\n'
                            total += len(line)
                            if total > PERSISTENCE_MAX_BYTES:
                                # Replay rejects anything this large: writing it
                                # would only waste IO on every READY
                                oversized = total
                                break
                            fp.write(compressor.compress(line))
                        if oversized is None:
                            fp.write(compressor.flush())
                            fp.flush()
                            os.fsync(fp.fileno())
                    if oversized is not None:
                        os.unlink(tmp)
                        return oversized
                    with self._persistence_write_lock:
                        if generation < self._persistence_written:
                            # A newer snapshot already landed: never replace it
                            os.unlink(tmp)
                            return None
                        os.replace(tmp, path)
                        self._persistence_written = generation
                    # fsync the directory so the rename itself is durable
                    try:
                        dir_fd = os.open(directory, os.O_RDONLY)
                    except OSError:
                        dir_fd = -1
                    if dir_fd >= 0:
                        try:
                            os.fsync(dir_fd)
                        except OSError:
                            pass
                        finally:
                            os.close(dir_fd)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                return None

            try:
                oversized = await asyncio.to_thread(_write)
            except Exception:
                _log.warning('Failed to write the cache persistence file.', exc_info=True)
                return False
            if oversized is not None:
                _log.warning(
                    'Not writing the cache persistence file: the snapshot is over the %.0f MiB limit replay '
                    'accepts (stopped at %.1f MiB). Persistence is effectively disabled for this account.',
                    PERSISTENCE_MAX_BYTES / 2**20,
                    oversized / 2**20,
                )
                return False
            _log.debug('Wrote cache persistence to %s (%d guilds).', path, len(guild_payloads))
            # The raw READY and guild payloads were only kept to be written. Hold
            # no second copy of every guild's members/presences for the process
            # lifetime (eviction could never free it); a later READY re-captures.
            # The close-time write finds nothing pending and is a no-op: the file
            # already holds exactly this snapshot.
            if self._persistence_ready is payload_ready_source:
                self._persistence_ready = None
                self._persistence_guild_payloads = {}
            return True

    async def _replay_persistence(self) -> bool:
        """Loads and replays the persisted payloads into the cache."""
        path = self.persistence_path
        if path is None:
            return False

        try:
            reader = _SnapshotReader(path)
        except FileNotFoundError:
            return False
        except Exception:
            _log.warning('Failed to read the cache persistence file.', exc_info=True)
            return False
        try:
            return await self._replay_snapshot(reader)
        finally:
            reader.close()

    async def _replay_snapshot(self, reader: _SnapshotReader) -> bool:
        try:
            data = await asyncio.to_thread(reader.next_record)
        except Exception:
            _log.warning('Cache persistence file is corrupt; ignoring it.', exc_info=True)
            return False
        if not isinstance(data, dict) or data.get('version') not in (1, PERSISTENCE_FORMAT_VERSION):
            return False
        # Version 2 streams one guild per record after this header; version 1
        # carries every guild inside this one document
        streamed = data['version'] == PERSISTENCE_FORMAT_VERSION

        # Guard against stale snapshots (older than 14 days) and account mismatches
        saved_at = data.get('saved_at', 0)
        if isinstance(saved_at, (int, float)) and time.time() - saved_at > self.persistence_max_age:
            _log.info('Cache persistence file is stale; performing a cold start.')
            return False
        # Shape validation: the file is data at rest and cannot be trusted.
        # A valid-JSON but malformed snapshot must downgrade to a cold start,
        # never crash login.
        ready = data.get('ready')
        if not isinstance(ready, dict) or not isinstance(ready.get('user'), dict):
            _log.info('Cache persistence snapshot has no replayable READY; performing a cold start.')
            return False
        # Strip to the allow-list: snapshots written by older versions (or by
        # anything else) may still carry auth/session identifiers, settings
        # protos and other account data that must not be replayed.
        ready = {k: v for k, v in ready.items() if k in self._PERSIST_READY_KEYS}
        saved_user_id = ready['user'].get('id')
        if self.user is not None and saved_user_id is not None:
            try:
                other_account = int(saved_user_id) != self.user.id
            except (TypeError, ValueError):
                _log.info('Cache persistence snapshot has an invalid user ID; performing a cold start.')
                return False
            if other_account:
                _log.warning('Cache persistence file belongs to a different account; ignoring it.')
                return False

        guild_payloads: List[Any] = []
        if not streamed:
            guild_payloads = data.get('guilds', [])
            if not isinstance(guild_payloads, list):
                guild_payloads = []
        del data

        # Replay through the normal parsing paths; parse_ready on an empty
        # state clears nothing of value and dispatches no events
        try:
            self.parse_ready(ready)  # type: ignore
            # parse_ready captures its READY for persistence; a replayed one must not
            # be: if the client closes before the fresh READY arrives, the close-time
            # write would replace the snapshot with this READY and zero guilds
            self._persistence_ready = None

            # Relationships and private channels (normally parsed in the
            # supplemental). The snapshot is a file on disk: unlike gateway
            # traffic, it cannot be trusted, so every conversion is guarded
            # and malformed entries skipped.
            temp_users: Dict[int, Any] = {}
            for user_payload in ready.get('users', []) if isinstance(ready.get('users'), list) else ():
                try:
                    temp_users[int(user_payload['id'])] = user_payload
                except (KeyError, TypeError, ValueError):
                    continue
            try:
                temp_users[int(ready['user']['id'])] = ready['user']
            except (KeyError, TypeError, ValueError):
                # parse_ready already populated state: drop it so the cold
                # start begins from a clean cache
                self.clear()
                return False
            for relationship in ready.get('relationships', []) if isinstance(ready.get('relationships'), list) else ():
                try:
                    r_id = int(relationship['id'])
                except (KeyError, TypeError, ValueError):
                    continue
                else:
                    if 'user' not in relationship:
                        try:
                            relationship['user'] = temp_users[int(relationship.pop('user_id'))]
                        except (KeyError, TypeError, ValueError, IndexError):
                            continue
                    try:
                        self._relationships[r_id] = Relationship(state=self, data=relationship)
                    except Exception:
                        continue
            for pm in ready.get('private_channels', []) if isinstance(ready.get('private_channels'), list) else ():
                try:
                    factory, _ = _private_channel_factory(pm['type'])
                    if 'recipients' not in pm:
                        pm['recipients'] = [temp_users[int(u_id)] for u_id in pm.pop('recipient_ids')]
                    self._add_private_channel(factory(me=self.user, data=pm, state=self))  # type: ignore
                except Exception:
                    continue

            # Guild caches (the merged payloads with members/presences); malformed
            # guild entries are skipped individually. Each payload is released
            # once its objects exist, and the loop gets a turn every few ms so a
            # large replay cannot stall other clients in the same process.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PERSISTENCE_REPLAY_SLICE
            index = 0
            while True:
                if streamed:
                    guild_data = await asyncio.to_thread(reader.next_record)
                    if guild_data is None:
                        break
                else:
                    if index >= len(guild_payloads):
                        break
                    guild_data = guild_payloads[index]
                    guild_payloads[index] = None
                    index += 1
                try:
                    guild = self._add_guild_from_data(guild_data)
                except Exception:
                    _log.debug('Skipping malformed guild payload in the persistence snapshot.', exc_info=True)
                    guild = None
                del guild_data
                if guild is not None:
                    self._persistence_guild_ids.add(guild.id)
                if loop.time() >= deadline:
                    await asyncio.sleep(0)
                    deadline = loop.time() + PERSISTENCE_REPLAY_SLICE

            self._persistence_loaded = True
            _log.info('Replayed cache persistence: %d guilds loaded.', len(self._persistence_guild_ids))
            return True
        except Exception:
            # Belt for anything the per-entry guards above did not catch:
            # a malformed snapshot downgrades to a cold start, never a crash
            _log.warning('Persistence snapshot failed to replay; performing a cold start.', exc_info=True)
            self.clear()
            return False

    def parse_resumed(self, data: gw.ResumedEvent) -> None:
        self.dispatch('resumed')

    def parse_passive_update_v2(self, data: gw.PassiveUpdateV2Event) -> None:
        # PASSIVE_UPDATE_V2 is sent for large guilds you are not subscribed to
        # in order to keep their members/read and voice states up-to-date; it replaces CHANNEL_UNREADS_UPDATE and PASSIVE_UPDATE_V1
        guild = self._get_guild(int(data['guild_id']))
        if not guild:
            _log.debug('PASSIVE_UPDATE_V2 referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        for user_id in map(int, data.get('removed_voice_states', [])):
            guild._voice_states.pop(user_id, None)

        for channel_data in data.get('updated_channels', []):
            channel = guild.get_channel(int(channel_data['id']))
            if not channel:
                continue
            channel.last_message_id = utils._get_as_snowflake(channel_data, 'last_message_id')  # type: ignore
            if 'last_pin_timestamp' in channel_data and hasattr(channel, 'last_pin_timestamp'):
                channel.last_pin_timestamp = utils.parse_time(channel_data['last_pin_timestamp'])  # type: ignore

        members = {int(m['user']['id']): m for m in data.get('updated_members', [])}

        cache_flags = self.member_cache_flags
        for k, member_data in members.items():
            member = guild.get_member(k)
            if member is not None:
                member._update(member_data)
            else:
                if cache_flags.voice:
                    member = Member(data=member_data, guild=guild, state=self)
                    guild._add_member(member)

        for voice_state in data.get('updated_voice_states', []):
            user_id = int(voice_state['user_id'])
            member_data = members.get(user_id)
            if member_data:
                voice_state['member'] = member_data
            guild._update_voice_state(voice_state, utils._get_as_snowflake(voice_state, 'channel_id'))

    def parse_message_create(self, data: gw.MessageCreateEvent) -> None:
        channel, _ = self._get_guild_channel(data)

        # channel will be the correct type here
        message = Message(channel=channel, data=data, state=self)  # type: ignore
        self.dispatch('message', message)
        if self._messages is not None:
            self._add_message_to_cache(message)
        if message.call is not None:
            call_cache = self._call_message_cache
            call_cache[message.id] = message
            if len(call_cache) > 100:
                call_cache.popitem(last=False)
        if channel:
            channel.last_message_id = message.id  # type: ignore

        read_state = self.get_read_state(channel.id)
        if (
            not message.author.is_blocked()
            and not (channel.type == ChannelType.group and message.type == MessageType.recipient_remove)
            and message._is_self_mentioned()
        ):
            # Increment mention count if applicable
            read_state.badge_count += 1
        if message.author.id == self.self_id and message.type != MessageType.poll_result:
            # Implicitly mark our own messages as read
            read_state.last_acked_id = message.id
            read_state.badge_count = 0

        # Messages keep their thread warm in the cache
        if isinstance(channel, Thread):
            guild = channel.guild
            if guild is not None:
                guild._touch_thread(channel.id)

    def parse_message_delete(self, data: gw.MessageDeleteEvent) -> None:
        raw = RawMessageDeleteEvent(data)
        found = self._get_message(raw.message_id)
        raw.cached_message = found
        self.dispatch('raw_message_delete', raw)
        if found is not None:
            self.dispatch('message_delete', found)
            if self._messages is not None:
                self._remove_message_from_cache(found)
            else:
                self._call_message_cache.pop(found.id, None)

    def parse_message_delete_bulk(self, data: gw.MessageDeleteBulkEvent) -> None:
        raw = RawBulkMessageDeleteEvent(data)
        if self._messages:
            found_messages = [message for message in self._messages if message.id in raw.message_ids]
        else:
            found_messages = []
        raw.cached_messages = found_messages
        self.dispatch('raw_bulk_message_delete', raw)
        if found_messages:
            self.dispatch('bulk_message_delete', found_messages)
            self._remove_messages_from_cache(found_messages, raw.message_ids)

    def _has_listener(self, *events: str) -> bool:
        # Checks whether any wait_for listener or event handler exists for the given events.
        # Runs on hot paths (every PRESENCE_UPDATE checks two events), so the
        # "on_" names are memoised and extra_events is looked up once
        client = self.client
        listeners = client._listeners
        handlers = client._event_handlers
        extra_events = None
        names = _LISTENER_METHOD_NAMES
        for event in events:
            method = names.get(event)
            if method is None:
                method = names[event] = 'on_' + event
            # Handlers seen by dispatch() are cached: the common "yes" is one dict
            # hit. A stale entry only costs a false positive (some extra work)
            if method in handlers or listeners.get(event):
                return True
            if hasattr(client, method):
                return True
            # ext.commands Bot.listen/cog listeners are stored separately
            if extra_events is None:
                extra_events = getattr(client, 'extra_events', None) or {}
            if extra_events and extra_events.get(method):
                return True
        return False

    def parse_message_update(self, data: gw.MessageUpdateEvent) -> None:
        channel, _ = self._get_guild_channel(data)
        cached_message = self._get_message(utils._get_as_snowflake(data, 'id'))

        if cached_message is not None:
            if self._has_listener('message_edit', 'raw_message_edit') and not ('type' in data and 'content' in data):
                # A partial update (an embed-only unfurl, say) cannot build a Message of its own:
                # the constructor needs `type` and `content`, so it raised and dropped the event
                # without even updating the cache. Apply it to the cached message instead.
                older_message = copy.copy(cached_message)
                cached_message._update(data)
                raw = RawMessageUpdateEvent(data=data, message=cached_message)
                raw.cached_message = older_message
                self.dispatch('raw_message_edit', raw)
                self.dispatch('message_edit', older_message, cached_message)
            elif self._has_listener('message_edit', 'raw_message_edit'):
                # channel would be the correct type here
                updated_message = Message(channel=channel, data=data, state=self)  # type: ignore

                raw = RawMessageUpdateEvent(data=data, message=updated_message)
                older_message = copy.copy(cached_message)
                raw.cached_message = older_message
                cached_message._update(data)
                # Coerce the `after` parameter to take the new updated Member
                # ref: #5999
                older_message.author = updated_message.author
                self.dispatch('raw_message_edit', raw)
                self.dispatch('message_edit', older_message, updated_message)
            else:
                cached_message._update(data)
        elif self._has_listener('raw_message_edit'):
            updated_message = Message(channel=channel, data=data, state=self)  # type: ignore
            raw = RawMessageUpdateEvent(data=data, message=updated_message)
            self.dispatch('raw_message_edit', raw)

    def parse_message_ack(self, data: gw.MessageAckEvent) -> None:
        self.read_state_version = data.get('version', self.read_state_version)
        channel_id = int(data['channel_id'])
        channel = self.get_channel(channel_id)
        if channel is None:
            _log.debug('MESSAGE_ACK referencing an unknown channel ID: %s. Discarding.', channel_id)
            return

        raw = RawMessageAckEvent(data)
        message_id = int(data['message_id'])
        message = self._get_message(message_id)
        raw.cached_message = message

        read_state = self.get_read_state(channel_id)
        read_state.last_acked_id = message_id
        if 'mention_count' in data:
            read_state.badge_count = data['mention_count']
        if 'flags' in data and data['flags'] is not None:
            read_state._flags = data['flags']
        if 'last_viewed' in data and data['last_viewed']:
            read_state.last_viewed = read_state.unpack_last_viewed(data['last_viewed'])

        self.dispatch('raw_message_ack', raw)
        if message is not None:
            self.dispatch('message_ack', message, raw.manual)

    def parse_message_reaction_add(self, data: gw.MessageReactionAddEvent) -> None:
        emoji = data['emoji']
        emoji_id = utils._get_as_snowflake(emoji, 'id')
        emoji = PartialEmoji.with_state(self, id=emoji_id, animated=emoji.get('animated', False), name=emoji['name'])  # type: ignore
        raw = RawReactionActionEvent(data, emoji, 'REACTION_ADD')

        member_data = data.get('member')
        if member_data:
            guild = self._get_guild(raw.guild_id)
            if guild is not None:
                raw.member = Member(data=member_data, guild=guild, state=self)
            else:
                raw.member = None
        else:
            raw.member = None
        self.dispatch('raw_reaction_add', raw)

        # rich interface here
        message = self._get_message(raw.message_id)
        if message is not None:
            emoji = self._upgrade_partial_emoji(emoji)
            reaction = message._add_reaction(data, emoji, raw.user_id)
            user = raw.member or self._get_reaction_user(message.channel, raw.user_id)

            if user:
                self.dispatch('reaction_add', reaction, user)

    def parse_message_reaction_remove_all(self, data: gw.MessageReactionRemoveAllEvent) -> None:
        raw = RawReactionClearEvent(data)
        self.dispatch('raw_reaction_clear', raw)

        message = self._get_message(raw.message_id)
        if message is not None:
            old_reactions = message.reactions.copy()
            message.reactions.clear()
            self.dispatch('reaction_clear', message, old_reactions)

    def parse_message_reaction_remove(self, data: gw.MessageReactionRemoveEvent) -> None:
        emoji = PartialEmoji.from_dict(data['emoji'])
        emoji._state = self
        raw = RawReactionActionEvent(data, emoji, 'REACTION_REMOVE')
        self.dispatch('raw_reaction_remove', raw)

        message = self._get_message(raw.message_id)
        if message is not None:
            emoji = self._upgrade_partial_emoji(emoji)
            try:
                reaction = message._remove_reaction(data, emoji, raw.user_id)
            except (AttributeError, ValueError):  # eventual consistency lol
                pass
            else:
                user = self._get_reaction_user(message.channel, raw.user_id)
                if user:
                    self.dispatch('reaction_remove', reaction, user)

    def parse_message_reaction_remove_emoji(self, data: gw.MessageReactionRemoveEmojiEvent) -> None:
        emoji = PartialEmoji.from_dict(data['emoji'])
        emoji._state = self
        raw = RawReactionClearEmojiEvent(data, emoji)
        self.dispatch('raw_reaction_clear_emoji', raw)

        message = self._get_message(raw.message_id)
        if message is not None:
            try:
                reaction = message._clear_emoji(emoji)
            except (AttributeError, ValueError):  # eventual consistency lol
                pass
            else:
                if reaction:
                    self.dispatch('reaction_clear_emoji', reaction)

    def parse_recent_mention_delete(self, data: gw.RecentMentionDeleteEvent) -> None:
        message_id = int(data['message_id'])
        message = self._get_message(message_id)
        if message is not None:
            self.dispatch('recent_mention_delete', message)
        self.dispatch('raw_recent_mention_delete', message_id)

    def parse_presences_replace(self, data: List[gw.UserPresenceUpdate]) -> None:
        for presence in data:
            self.parse_presence_update(presence)

    def _handle_presence_update(
        self, guild: Optional[Guild], data: Union[gw.BasePresenceUpdate, gw.PresenceUpdateEvent]
    ) -> None:
        guild_id = guild.id if guild else None
        user = data['user']  # type: ignore
        if user is None:
            _log.debug('PRESENCE_UPDATE referencing a null user. Discarding.')
            return
        user_id = int(user['id'])

        member = None
        if guild is not None:
            member = guild.get_member(user_id)
            if member is None:
                # Drop any stale presence left over from before the member was
                # evicted; keeping it would leak memory and could serve stale data
                self.remove_presence(user_id, guild_id)
                _log.debug('PRESENCE_UPDATE referencing an unknown member ID: %s. Discarding.', user_id)
                return

        # The old/new comparison only feeds on_presence_update: skip the copy
        # and compare when nothing listens (the cache is updated either way)
        listening = self._has_listener('presence_update')
        presence = self.get_presence(user_id, guild_id)
        if presence is not None:
            old_presence = Presence._copy(presence) if listening else presence
            presence._update(data, self, user_id)
        else:
            old_presence = Presence._offline()
            presence = self.store_presence(user_id, self.create_presence(data, user_id), guild_id)

        if not guild:
            try:
                relationship = self.create_implicit_relationship(self.store_user(user))  # type: ignore
            except (KeyError, ValueError):
                # User object is partial, so we can't continue
                _log.debug('PRESENCE_UPDATE referencing an unknown relationship ID: %s. Discarding.', user_id)
                return

            user_update = relationship.user._update_self(user)  # type: ignore
            if listening and old_presence != presence:
                old_relationship = Relationship._copy(relationship, old_presence)
                self.dispatch('presence_update', old_relationship, relationship)
        else:
            # Presence updates keep flowing for online members, so mark them as
            # recently used to protect them from cache eviction
            guild._touch_member(user_id)

            user_update = member._user._update_self(user)  # type: ignore
            if listening and old_presence != presence:
                old_member = Member._copy(member)  # type: ignore # Member existence was validated above
                old_member._presence = old_presence
                self.dispatch('presence_update', old_member, member)

        if user_update:
            self.dispatch('user_update', user_update[0], user_update[1])

    def parse_presence_update(self, data: gw.PresenceUpdateEvent) -> None:
        guild_id = utils._get_as_snowflake(data, 'guild_id')
        guild = self._get_guild(guild_id)

        # Opt-in via enable_raw_presences (Rapptz/discord.py 418a791, where the
        # default comes from the intents). The raw event covers members the
        # cache does not hold.
        user = data.get('user') if self.raw_presence_flag else None
        if user:
            user_id = int(user['id'])
            raw = RawPresenceUpdateEvent(
                user_id=user_id, guild_id=guild_id, guild=guild, presence=self.create_presence(data, user_id)
            )
            self.dispatch('raw_presence_update', raw)

        if guild_id and not guild:
            _log.debug('PRESENCE_UPDATE referencing an unknown guild ID: %s. Discarding.', guild_id)
            return

        self._handle_presence_update(guild, data)

    def parse_user_update(self, data: gw.UserUpdateEvent) -> None:
        # Clear the ACK token
        self.http.ack_token = None

        user: ClientUser = self.user  # type: ignore
        old_user = copy.copy(user)
        user._full_update(data)
        self.dispatch('user_update', old_user, user)

    def parse_user_note_update(self, data: gw.UserNoteUpdateEvent) -> None:
        # The gateway does not provide note objects on READY with our default capabilities
        # so we cannot have (old, new) event dispatches
        user_id = int(data['id'])
        text = data['note']
        self.dispatch('raw_note_update', user_id, text)

        user = self.get_user(user_id)
        if user:
            self.dispatch('note_update', user, text)

    def parse_user_settings_proto_update(self, data: gw.ProtoSettingsEvent):
        type = UserSettingsType(data['settings']['type'])
        if type == UserSettingsType.preloaded_user_settings:
            settings = self.settings
            if settings:
                old_settings = UserSettings._copy(settings)
                settings._update(data['settings']['proto'], partial=data.get('partial', False))
                self.dispatch('settings_update', old_settings, settings)
                self.dispatch('internal_settings_update', old_settings, settings)
        elif type == UserSettingsType.frecency_user_settings:
            settings = self.frecency_settings
            if settings:
                old_settings = FrecencySettings._copy(settings)
                settings._update(data['settings']['proto'], partial=data.get('partial', False))
                self.dispatch('frecency_settings_update', old_settings, settings)
            else:
                settings = FrecencySettings(self, data['settings']['proto'])
                self.frecency_settings = settings
                self.dispatch('frecency_settings_update', None, settings)
        elif type == UserSettingsType.test_settings:
            pass
        else:
            _log.warning('USER_SETTINGS_PROTO_UPDATE referencing an unknown type: %s', type.value)

    def parse_user_guild_settings_update(self, data: gw.UserGuildSettingsEvent) -> None:
        guild_id = utils._get_as_snowflake(data, 'guild_id')

        settings = self.guild_settings.get(guild_id)
        if settings is not None:
            old_settings = copy.copy(settings)
            settings._update(data)
        else:
            old_settings = None
            settings = GuildSettings(data=data, state=self)
            self.guild_settings[guild_id] = settings
        self.guild_settings_version = data.get('version', self.guild_settings_version)
        self.dispatch('guild_settings_update', old_settings, settings)

    def parse_user_required_action_update(self, data: gw.RequiredActionEvent) -> None:
        required_action = try_enum(RequiredActionType, data['required_action']) if data['required_action'] else None
        self.required_action = required_action
        self.dispatch('required_action_update', required_action)

    def parse_user_non_channel_ack(self, data: gw.NonChannelAckEvent) -> None:
        self.read_state_version = data.get('version', self.read_state_version)

        raw = RawUserFeatureAckEvent(data)
        read_state = self.get_read_state(self.self_id, raw.type)
        read_state.last_acked_id = int(data['entity_id'])
        self.dispatch('user_feature_ack', raw)

    def parse_user_connections_update(self, data: Union[gw.ConnectionEvent, gw.PartialConnectionEvent]) -> None:
        self.dispatch('connections_update')

        id = data.get('id')
        if id is None or 'user_id' in data:
            return

        if id not in self.connections:
            self.connections[id] = connection = Connection(state=self, data=data)
            self.dispatch('connection_create', connection)
        else:
            # Known limitation: a deleted connection can also arrive here, and is
            # reported as an update. Comparing the payload with the previous one
            # would detect it, but certain events also send updates with identical
            # payloads, so that signal is unreliable. Everything is treated as an
            # update until Discord provides a distinguishing field.
            connection = self.connections[id]
            old_connection = copy.copy(connection)
            connection._update(data)
            self.dispatch('connection_update', old_connection, connection)

    def parse_user_connections_link_callback(self, data: gw.ConnectionsLinkCallbackEvent) -> None:
        self.dispatch('connections_link_callback', data['provider'], data['callback_code'], data['callback_state'])

    def parse_user_payment_sources_update(self, data: gw.NoEvent) -> None:
        self.dispatch('payment_sources_update')

    def parse_user_subscriptions_update(self, data: gw.NoEvent) -> None:
        self.dispatch('subscriptions_update')

    def parse_user_payment_client_add(self, data: gw.PaymentClientAddEvent) -> None:
        self.dispatch('payment_client_add', data['purchase_token_hash'], utils.parse_time(data['expires_at']))

    def parse_user_premium_guild_subscription_slot_create(self, data: gw.PremiumGuildSubscriptionSlotEvent) -> None:
        slot = PremiumGuildSubscriptionSlot(state=self, data=data)
        self.dispatch('premium_guild_subscription_slot_create', slot)

    def parse_user_premium_guild_subscription_slot_update(self, data: gw.PremiumGuildSubscriptionSlotEvent) -> None:
        slot = PremiumGuildSubscriptionSlot(state=self, data=data)
        self.dispatch('premium_guild_subscription_slot_update', slot)

    def parse_billing_popup_bridge_callback(self, data: gw.BillingPopupBridgeCallbackEvent) -> None:
        self.dispatch(
            'billing_popup_bridge_callback',
            try_enum(PaymentSourceType, data.get('payment_source_type', 0)),
            data.get('path', ''),
            Metadata(data.get('query', {})),
            data.get('state'),
        )

    def parse_oauth2_token_revoke(self, data: gw.OAuth2TokenRevokeEvent) -> None:
        if 'access_token' not in data or 'application_id' not in data:
            _log.warning('OAUTH2_TOKEN_REVOKE payload has invalid data: %s. Discarding.', list(data.keys()))
            return
        self.dispatch('oauth2_token_revoke', data['access_token'], int(data['application_id']))

    def parse_auth_session_change(self, data: gw.AuthSessionChangeEvent) -> None:
        self.auth_session_id = auth_session_id = data['auth_session_id_hash']
        self.dispatch('auth_session_change', auth_session_id)

    def parse_payment_update(self, data: gw.PaymentUpdateEvent) -> None:
        id = int(data['id'])
        payment = self.pending_payments.get(id)
        if payment is not None:
            payment._update(data)
        else:
            payment = Payment(state=self, data=data)

        self.dispatch('payment_update', payment)

    def parse_library_application_update(self, data: gw.LibraryApplicationUpdateEvent) -> None:
        entry = LibraryApplication(state=self, data=data)
        self.dispatch('library_application_update', entry)

    def parse_sessions_replace(self, payload: gw.SessionsReplaceEvent, *, from_ready: bool = False) -> None:
        # Discord returns a max of 15 sessions, even though the user may have more
        # Unfortunately, this means our own session may not be included in the payload
        data = {s['session_id']: s for s in payload}
        if len([s for s in data if s != 'all']) >= 15:
            _log.warning('User has more than 15 active sessions. Client presence information may be inaccurate.')
        if not data:
            # Not really sure what to do when we receive an empty sessions payload
            # This is an edge case either way, the session is probably dying soon
            _log.warning('User has no sessions (from READY: %s). Discarding.', str(from_ready))
            return

        for session_id, session in data.items():
            existing = self._sessions.get(session_id)
            if existing is not None:
                old = copy.copy(existing)
                existing._update(session)
                if not from_ready and (
                    old.status != existing.status or old.active != existing.active or old.activities != existing.activities
                ):
                    self.dispatch('session_update', old, existing)
            else:
                existing = Session(state=self, data=session)
                self._sessions[session_id] = existing
                if not from_ready:
                    self.dispatch('session_create', existing)

        old_all = None
        if not from_ready:
            removed_sessions = [s for s in self._sessions if s not in data]
            for session_id in removed_sessions:
                if session_id == 'all':
                    old_all = self._sessions.pop('all')
                else:
                    session = self._sessions.pop(session_id)
                    self.dispatch('session_delete', session)

        if 'all' not in self._sessions:
            # The "all" session does not always exist...
            # This happens if there is only a single session (us)
            # or all sessions are the same state
            # In the case it is "removed", we try to update the old one
            # Else, we create a new one with fake data
            fake = data.get(self.session_id) or next(iter(data.values()))  # type: ignore
            if old_all is not None:
                old = copy.copy(old_all)
                old_all._update(fake)
                if old.status != old_all.status or old.active != old_all.active or old.activities != old_all.activities:
                    self.dispatch('session_update', old, old_all)
            else:
                old_all = Session._fake_all(state=self, data=fake)
            self._sessions['all'] = old_all

        # One-time discover our internal state (skipped pre-connect, e.g.
        # during persistence replay where no websocket exists yet)
        if from_ready and self.ws is not None and self.ws.status == 'unknown':
            our_session = self._sessions.get(self.session_id)  # type: ignore
            if our_session is None:
                return
            self.ws.status = our_session.status.value
            self.ws.activities = [a.to_dict() for a in our_session.activities]

    def parse_entitlement_create(self, data: gw.EntitlementEvent) -> None:
        entitlement = Entitlement(state=self, data=data)
        self.dispatch('entitlement_create', entitlement)

    def parse_entitlement_update(self, data: gw.EntitlementEvent) -> None:
        entitlement = Entitlement(state=self, data=data)
        self.dispatch('entitlement_update', entitlement)

    def parse_entitlement_delete(self, data: gw.EntitlementEvent) -> None:
        entitlement = Entitlement(state=self, data=data)
        self.dispatch('entitlement_delete', entitlement)

    # Gift code event payloads are different from the REST API
    # but there's not much else we can do here

    def parse_gift_code_create(self, data: gw.GiftCreateEvent) -> None:
        gift = Gift(state=self, data=data)  # type: ignore
        self.dispatch('gift_create', gift)

    def parse_gift_code_update(self, data: gw.GiftUpdateEvent) -> None:
        gift = Gift(state=self, data=data)  # type: ignore
        self.dispatch('gift_update', gift)

    # Invite events are no longer dispatched for user accounts...
    # Kept here for future proofing

    def parse_invite_create(self, data: gw.InviteCreateEvent) -> None:
        invite = Invite.from_gateway(state=self, data=data)
        self.dispatch('invite_create', invite)

    def parse_invite_delete(self, data: gw.InviteDeleteEvent) -> None:
        invite = Invite.from_gateway(state=self, data=data)
        self.dispatch('invite_delete', invite)

    def parse_channel_delete(self, data: gw.ChannelDeleteEvent) -> None:
        guild = self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
        channel_id = int(data['id'])
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                guild._remove_channel(channel)
                self.dispatch('guild_channel_delete', channel)

                if channel.type in (ChannelType.voice, ChannelType.stage_voice):
                    for s in guild.scheduled_events:
                        if s.channel_id == channel.id:
                            guild._scheduled_events.pop(s.id)
                            self.dispatch('scheduled_event_delete', s)

                threads = guild._remove_threads_by_channel(channel_id)
                for thread in threads:
                    self.dispatch('thread_delete', thread)
                    self.dispatch('raw_thread_delete', RawThreadDeleteEvent._from_thread(thread))
        else:
            channel = self._get_private_channel(channel_id)
            if channel is not None:
                self._remove_private_channel(channel)
                # Private/group channels can carry call voice states; purge them
                self._voice_states = {
                    k: v
                    for k, v in self._voice_states.items()
                    if v is None or v.channel is None or v.channel.id != channel_id
                }
                self.dispatch('private_channel_delete', channel)

        # Nuke read state
        read_state = self.get_read_state(channel_id)
        if read_state is not None:
            self.remove_read_state(read_state)

    def parse_channel_update(self, data: gw.ChannelUpdateEvent) -> None:
        channel_type = try_enum(ChannelType, data.get('type'))
        channel_id = int(data['id'])
        if channel_type in (ChannelType.private, ChannelType.group):
            channel = self._get_private_channel(channel_id)
            if channel is not None:
                old_channel = copy.copy(channel)
                channel._update(data)  # type: ignore # the data payload varies based on the channel type
                self.dispatch('private_channel_update', old_channel, channel)
                return
            else:
                _log.debug('CHANNEL_UPDATE referencing an unknown channel ID: %s. Discarding.', channel_id)

        guild_id = utils._get_as_snowflake(data, 'guild_id')
        guild = self._get_guild(guild_id)
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                old_channel = copy.copy(channel)
                channel._update(guild, data)  # type: ignore # the data payload varies based on the channel type
                self.dispatch('guild_channel_update', old_channel, channel)
            else:
                _log.debug('CHANNEL_UPDATE referencing an unknown channel ID: %s. Discarding.', channel_id)
        else:
            _log.debug('CHANNEL_UPDATE referencing an unknown guild ID: %s. Discarding.', guild_id)

    def parse_channel_create(self, data: gw.ChannelCreateEvent) -> None:
        factory, ch_type = _channel_factory(data['type'])
        if factory is None:
            _log.debug('CHANNEL_CREATE referencing an unknown channel type %s. Discarding.', data['type'])
            return

        if ch_type in (ChannelType.group, ChannelType.private):
            channel_id = int(data['id'])
            if self._get_private_channel(channel_id) is None:
                channel = factory(me=self.user, data=data, state=self)  # type: ignore # user is always present when logged in
                self._add_private_channel(channel)  # type: ignore # channel will always be a private channel
                self.dispatch('private_channel_create', channel)
        else:
            guild_id = utils._get_as_snowflake(data, 'guild_id')
            guild = self._get_guild(guild_id)
            if guild is not None:
                # The factory can't be a DMChannel or GroupChannel here
                channel = factory(guild=guild, state=self, data=data)  # type: ignore
                guild._add_channel(channel)  # type: ignore
                self.dispatch('guild_channel_create', channel)
            else:
                _log.debug('CHANNEL_CREATE referencing an unknown guild ID: %s. Discarding.', guild_id)
                return

    def parse_channel_pins_update(self, data: gw.ChannelPinsUpdateEvent) -> None:
        channel_id = int(data['channel_id'])
        try:
            guild = self._get_guild(int(data['guild_id']))  # pyright: ignore[reportTypedDictNotRequiredAccess]
        except KeyError:
            guild = None
            channel = self._get_private_channel(channel_id)
        else:
            channel = guild and guild._resolve_channel(channel_id)

        if channel is None:
            _log.debug('CHANNEL_PINS_UPDATE referencing an unknown channel ID: %s. Discarding.', channel_id)
            return

        last_pin = utils.parse_time(data.get('last_pin_timestamp'))
        if hasattr(channel, 'last_pin_timestamp'):
            channel.last_pin_timestamp = last_pin  # type: ignore # Handled above

        if guild is None:
            self.dispatch('private_channel_pins_update', channel, last_pin)
        else:
            self.dispatch('guild_channel_pins_update', channel, last_pin)

    def parse_channel_pins_ack(self, data: gw.ChannelPinsAckEvent) -> None:
        self.read_state_version = data.get('version', self.read_state_version)
        channel_id = int(data['channel_id'])
        channel = self.get_channel(channel_id)
        if channel is None:
            _log.debug('CHANNEL_PINS_ACK referencing an unknown channel ID: %s. Discarding.', channel_id)
            return

        read_state = self.get_read_state(channel_id)
        last_pin = utils.parse_time(data.get('last_pin'))
        read_state.acked_pin_timestamp = last_pin

        if channel.guild is None:
            self.dispatch('private_channel_pins_ack', channel, last_pin)
        else:
            self.dispatch('guild_channel_pins_ack', channel, last_pin)

    def parse_channel_recipient_add(self, data: gw.ChannelRecipientEvent) -> None:
        channel = self._get_private_channel(int(data['channel_id']))
        if channel is None:
            _log.debug('CHANNEL_RECIPIENT_ADD referencing an unknown channel ID: %s. Discarding.', data['channel_id'])
            return

        user = self.store_user(data['user'])
        channel.recipients.append(user)  # type: ignore
        if 'nick' in data:
            channel.nicks[user] = data['nick']  # type: ignore
        self.dispatch('group_join', channel, user)

    def parse_channel_recipient_remove(self, data: gw.ChannelRecipientEvent) -> None:
        channel = self._get_private_channel(int(data['channel_id']))
        if channel is None:
            _log.debug('CHANNEL_RECIPIENT_REMOVE referencing an unknown channel ID: %s. Discarding.', data['channel_id'])
            return

        user = self.store_user(data['user'])
        try:
            channel.recipients.remove(user)  # type: ignore
        except ValueError:
            pass
        else:
            self.dispatch('group_remove', channel, user)

    def parse_thread_create(self, data: gw.ThreadCreateEvent) -> None:
        guild_id = int(data['guild_id'])
        guild: Optional[Guild] = self._get_guild(guild_id)
        if guild is None:
            _log.debug('THREAD_CREATE referencing an unknown guild ID: %s. Discarding.', guild_id)
            return

        existing = guild.get_thread(int(data['id']))
        if existing is not None:  # Shouldn't happen
            guild._touch_thread(existing.id)
            old = existing._update(data)
            if old is not None:
                self.dispatch('thread_update', old, existing)
        else:
            thread = Thread(guild=guild, state=self, data=data)
            guild._add_thread(thread)
            if data.get('newly_created', False):
                if (
                    thread.parent
                    and thread.parent.type in (ChannelType.forum, ChannelType.media)
                    and thread.owner_id == self.self_id
                ):
                    # Implicitly mark our own threads as read
                    read_state = self.get_read_state(thread.parent_id)
                    read_state.last_acked_id = thread.id
                    read_state.badge_count = 0
                self.dispatch('thread_create', thread)
            else:
                self.dispatch('thread_join', thread)

    def parse_thread_update(self, data: gw.ThreadUpdateEvent) -> None:
        guild_id = int(data['guild_id'])
        guild = self._get_guild(guild_id)
        if guild is None:
            _log.debug('THREAD_UPDATE referencing an unknown guild ID: %s. Discarding', guild_id)
            return

        existing = guild.get_thread(int(data['id']))
        if existing is not None:
            guild._touch_thread(existing.id)
            old = existing._update(data)
            if existing.archived:
                guild._remove_thread(existing)
            if old is not None:
                self.dispatch('thread_update', old, existing)
        else:  # Shouldn't happen
            thread = Thread(guild=guild, state=self, data=data)
            guild._add_thread(thread)

    def parse_thread_delete(self, data: gw.ThreadDeleteEvent) -> None:
        guild_id = int(data['guild_id'])
        guild = self._get_guild(guild_id)
        if guild is None:
            _log.debug('THREAD_DELETE referencing an unknown guild ID: %s. Discarding', guild_id)
            return

        raw = RawThreadDeleteEvent(data)
        raw.thread = thread = guild.get_thread(raw.thread_id)
        self.dispatch('raw_thread_delete', raw)

        if thread is not None:
            guild._remove_thread(thread)
            self.dispatch('thread_delete', thread)

        # Nuke read state
        read_state = self.get_read_state(raw.thread_id)
        if read_state is not None:
            self.remove_read_state(read_state)

    def parse_thread_list_sync(self, data: gw.ThreadListSyncEvent) -> None:
        guild_id = int(data['guild_id'])
        guild: Optional[Guild] = self._get_guild(guild_id)
        if guild is None:
            _log.debug('THREAD_LIST_SYNC referencing an unknown guild ID: %s. Discarding.', guild_id)
            return

        try:
            channel_ids = {int(i) for i in data['channel_ids']}  # pyright: ignore[reportTypedDictNotRequiredAccess]
        except KeyError:
            channel_ids = None
            threads = guild._threads.copy()
        else:
            threads = guild._filter_threads(channel_ids)

        new_threads = {}
        for d in data.get('threads', []):
            thread = threads.pop(int(d['id']), None)
            if thread is not None:
                old = thread._update(d)
                if old is not None:
                    self.dispatch('thread_update', old, thread)  # Honestly not sure if this is right
            else:
                thread = Thread(guild=guild, state=self, data=d)
                new_threads[thread.id] = thread
        old_threads = [t for t in threads.values() if t.id not in new_threads]

        for member in data.get('members', []):
            try:  # Note: member['id'] is the thread_id
                thread = threads[int(member['id'])]
            except KeyError:
                continue
            else:
                thread._add_member(ThreadMember(thread, member))

        for k in new_threads.values():
            guild._add_thread(k)

        for k in old_threads:
            guild._threads.pop(k.id, None)  # may have been evicted mid-sync
            self.dispatch('thread_delete', k)  # Again, not sure

        for message in data.get('most_recent_messages', []):
            guild_id = utils._get_as_snowflake(message, 'guild_id')
            channel, _ = self._get_guild_channel(message)

            # channel will be the correct type here
            message = Message(channel=channel, data=message, state=self)  # type: ignore
            if self._messages is not None:
                self._add_message_to_cache(message)

    def parse_thread_member_update(self, data: gw.ThreadMemberUpdate) -> None:
        guild_id = int(data['guild_id'])
        guild: Optional[Guild] = self._get_guild(guild_id)
        if guild is None:
            _log.debug('THREAD_MEMBER_UPDATE referencing an unknown guild ID: %s. Discarding.', guild_id)
            return

        thread_id = int(data['id'])
        thread: Optional[Thread] = guild.get_thread(thread_id)
        if thread is None:
            _log.debug('THREAD_MEMBER_UPDATE referencing an unknown thread ID: %s. Discarding.', thread_id)
            return

        member = ThreadMember(thread, data)
        thread.me = member
        # Member activity keeps the thread warm in the cache
        guild._touch_thread(thread_id)

    def parse_thread_members_update(self, data: gw.ThreadMembersUpdate) -> None:
        guild_id = int(data['guild_id'])
        guild: Optional[Guild] = self._get_guild(guild_id)
        if guild is None:
            _log.debug('THREAD_MEMBERS_UPDATE referencing an unknown guild ID: %s. Discarding.', guild_id)
            return

        thread_id = int(data['id'])
        thread: Optional[Thread] = guild.get_thread(thread_id)
        raw = RawThreadMembersUpdate(data)
        if thread is None:
            _log.debug('THREAD_MEMBERS_UPDATE referencing an unknown thread ID: %s. Discarding.', thread_id)
            return

        # Member activity keeps the thread warm in the cache
        guild._touch_thread(thread_id)

        added_members = [ThreadMember(thread, d) for d in data.get('added_members', [])]
        removed_member_ids = [int(x) for x in data.get('removed_member_ids', [])]
        self_id = self.self_id
        for member in added_members:
            if member.id != self_id:
                thread._add_member(member)
                self.dispatch('thread_member_join', member)
            else:
                thread.me = member
                self.dispatch('thread_join', thread)

        for member_id in removed_member_ids:
            member = thread._pop_member(member_id)
            if member_id != self_id:
                self.dispatch('raw_thread_member_remove', raw)
                if member is not None:
                    self.dispatch('thread_member_remove', member)
            else:
                self.dispatch('thread_remove', thread)

    def parse_thread_member_list_update(self, data: gw.ThreadMemberListUpdateEvent) -> None:
        guild_id = int(data['guild_id'])
        guild: Optional[Guild] = self._get_guild(guild_id)
        if guild is None:
            _log.debug('THREAD_MEMBER_LIST_UPDATE referencing an unknown guild ID: %s. Discarding.', guild_id)
            return

        thread_id = int(data['thread_id'])
        thread: Optional[Thread] = guild.get_thread(thread_id)
        if thread is None:
            _log.debug('THREAD_MEMBER_LIST_UPDATE referencing an unknown thread ID: %s. Discarding.', thread_id)
            return

        members = [ThreadMember(thread, member) for member in data['members']]
        for m in members:
            thread._add_member(m)

    def parse_guild_member_add(self, data: gw.GuildMemberAddEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_MEMBER_ADD referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        # A repeated GUILD_MEMBER_ADD for a member already cached would count
        # them twice in member_count and dispatch on_member_join again
        # (Rapptz/discord.py 22ade98). The cache is read directly so the check
        # does not count as a member-cache hit or miss.
        member_id = int(data['user']['id'])
        if member_id in guild._members:
            _log.debug('GUILD_MEMBER_ADD referencing an already cached member ID: %s. Discarding.', member_id)
            return

        member = Member(guild=guild, data=data, state=self)
        presence = None
        if 'presence' in data:
            presence = self.create_presence(data['presence'], member._user.id)

        if self.member_cache_flags.joined or member.id == self.self_id:
            if presence is not None:
                self.store_presence(member.id, presence, guild.id)
            guild._add_member(member)
        else:
            member._presence = presence  # Save the presence

        if guild._member_count is not None:
            guild._member_count += 1

        self.dispatch('member_join', member)

    def parse_guild_member_remove(self, data: gw.GuildMemberRemoveEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_MEMBER_REMOVE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        user_data = data['user']
        if len(user_data) <= 1:
            # Fake event here, so no event
            member = guild.get_member(int(user_data['id']))
            if member is not None:
                guild._remove_member(member)
            else:
                _log.debug('GUILD_MEMBER_REMOVE referencing an unknown member ID: %s. Discarding.', user_data['id'])
            return

        try:
            user = self.store_user(user_data)  # type: ignore
            raw = RawMemberRemoveEvent(data, user)
        except KeyError:
            _log.debug('GUILD_MEMBER_REMOVE referencing an unknown user ID: %s. Discarding.', data['user']['id'])
            return

        if guild._member_count is not None:
            guild._member_count -= 1

        member = guild.get_member(user.id)
        if member is not None:
            raw.user = member
            guild._remove_member(member)
            self.dispatch('member_remove', member)

        self.dispatch('raw_member_remove', raw)

    def _handle_member_update(self, guild: Guild, data: Union[gw.GuildMemberUpdateEvent, gw.MemberWithUser]):
        user = data['user']
        user_id = int(user['id'])
        member = guild.get_member(user_id)
        if member is not None:
            old_member = member._update(data)
            if old_member is not None:
                self.dispatch('member_update', old_member, member)
        else:
            if self.member_cache_flags.joined:
                member = Member(data=data, guild=guild, state=self)  # type: ignore # The data is close enough
                guild._add_member(member)
            _log.debug('GUILD_MEMBER_UPDATE referencing an unknown member ID: %s.', user_id)

        if member is not None:
            # Force an update on the inner user if necessary
            user_update = member._user._update_self(user)
            if user_update:
                self.dispatch('user_update', user_update[0], user_update[1])

    def parse_guild_member_update(self, data: gw.GuildMemberUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_MEMBER_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        self._handle_member_update(guild, data)

    def parse_guild_member_list_update(self, data: gw.GuildMemberListUpdateEvent) -> None:
        self.dispatch('raw_member_list_update', data)
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_MEMBER_LIST_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        request = self._scrape_requests.get(guild.id)

        if data['member_count'] > 0:
            guild._member_count = data['member_count']
        if data['online_count'] > 0:
            guild._presence_count = data['online_count']
        group_ids = {group['id'] for group in data['groups']}
        guild._true_online_count = sum(group['count'] for group in data['groups'] if group['id'] != 'offline')

        empty_tuple = tuple()
        members = []
        ops = data['ops']

        for opdata in ops:
            # The OPs are as follows:
            # SYNC: Provides member/presence data for a 100 member range of the member list
            # UPDATE: Dispatched when a member is updated and stays in the same range
            # INSERT: Dispatched when a member is inserted into a range
            # DELETE: Dispatched when a member is removed from a range
            # INVALIDATE: Sent when you're unsubscribed from a range

            if opdata['op'] == 'SYNC':
                for item in opdata['items']:
                    if 'group' in item:  # Hoisted role
                        continue

                    mdata = item['member']
                    member = Member(data=mdata, guild=guild, state=self)
                    if mdata.get('presence') is not None:
                        member._presence_update(mdata['presence'], empty_tuple)

                    members.append(member)

        if request:
            if request.chunk and not ('offline' in group_ids or data['member_count'] == data['online_count']):
                # The guild has offline members hidden
                _log.debug(f'Detected guild {guild} with erroneous offline members.')
                return
            request.add_members(members)

            # Attempt to detect Discord overriding the member list
            if (
                not request.chunk
                and not request.manual_override
                and data['online_count'] > 0
                and guild._true_online_count < data['online_count']
                and 'online' not in group_ids
            ):
                _log.debug(
                    f'Detected guild {guild} with manually overriden member list groups: online members not cached by the Gateway.'
                )
                request.handle_manual_override(guild._true_online_count)
        else:
            for member in members:
                guild._add_member(member)

    def parse_guild_application_command_index_update(self, data: gw.GuildApplicationCommandIndexUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug(
                'GUILD_APPLICATION_COMMAND_INDEX_UPDATE referencing an unknown guild ID: %s. Discarding.',
                data['guild_id'],
            )
            return

        self.dispatch('application_command_index_update', guild)

    def parse_guild_emojis_update(self, data: gw.GuildEmojisUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_EMOJIS_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        before_emojis = guild.emojis
        for emoji in before_emojis:
            self._emojis.pop(emoji.id, None)
        guild.emojis = tuple(map(lambda d: self.store_emoji(guild, d), data['emojis']))
        self.dispatch('guild_emojis_update', guild, before_emojis, guild.emojis)

    def parse_soundboard_sounds(self, data: gw.SoundboardSoundsEvent) -> None:
        # The reply to request_soundboard_sounds: the guild's full set, so it
        # replaces whatever was cached
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('SOUNDBOARD_SOUNDS referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return
        guild._soundboard_sounds = {}
        for raw_sound in data['soundboard_sounds']:
            guild._add_soundboard_sound(SoundboardSound(guild=guild, state=self, data=raw_sound))

    def parse_guild_soundboard_sound_create(self, data: gw.GuildSoundBoardSoundCreateEvent) -> None:
        guild = self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
        if guild is None:
            _log.debug(
                'GUILD_SOUNDBOARD_SOUND_CREATE referencing an unknown guild ID: %s. Discarding.', data.get('guild_id')
            )
            return
        sound = SoundboardSound(guild=guild, state=self, data=data)
        # Only a guild whose sounds were requested has a cache to add to; a
        # one-sound cache would look like the guild's complete set
        if guild._soundboard_sounds is not None:
            guild._add_soundboard_sound(sound)
        self.dispatch('soundboard_sound_create', sound)

    def _update_soundboard_sound(self, guild: Guild, data: gw.GuildSoundBoardSoundUpdateEvent, event: str) -> None:
        sound_id = int(data['sound_id'])
        sound = guild.get_soundboard_sound(sound_id)
        if sound is None:
            # Not cached: user accounts only hold sounds of requested guilds
            _log.debug('%s referencing an uncached sound ID: %s. Discarding.', event, sound_id)
            return
        old_sound = copy.copy(sound)
        sound._update(data)
        # An update that changes nothing dispatches nothing (as for other models)
        if _soundboard_sound_key(old_sound) == _soundboard_sound_key(sound):
            return
        self.dispatch('soundboard_sound_update', old_sound, sound)

    def parse_guild_soundboard_sound_update(self, data: gw.GuildSoundBoardSoundUpdateEvent) -> None:
        guild = self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
        if guild is None:
            _log.debug(
                'GUILD_SOUNDBOARD_SOUND_UPDATE referencing an unknown guild ID: %s. Discarding.', data.get('guild_id')
            )
            return
        self._update_soundboard_sound(guild, data, 'GUILD_SOUNDBOARD_SOUND_UPDATE')

    def parse_guild_soundboard_sounds_update(self, data: gw.GuildSoundBoardSoundsUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_SOUNDBOARD_SOUNDS_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return
        for raw_sound in data['soundboard_sounds']:
            self._update_soundboard_sound(guild, raw_sound, 'GUILD_SOUNDBOARD_SOUNDS_UPDATE')

    def parse_guild_soundboard_sound_delete(self, data: gw.GuildSoundBoardSoundDeleteEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_SOUNDBOARD_SOUND_DELETE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return
        sound = guild.get_soundboard_sound(int(data['sound_id']))
        if sound is None:
            _log.debug('GUILD_SOUNDBOARD_SOUND_DELETE referencing an uncached sound ID: %s. Discarding.', data['sound_id'])
            return
        guild._remove_soundboard_sound(sound)
        self.dispatch('soundboard_sound_delete', sound)

    def get_soundboard_sound(self, sound_id: Optional[int]) -> Optional[SoundboardSound]:
        if sound_id is None:
            return None
        for guild in self._guilds.values():
            sound = guild.get_soundboard_sound(sound_id)
            if sound is not None:
                return sound
        return None

    @property
    def soundboard_sounds(self) -> List[SoundboardSound]:
        return [sound for guild in self._guilds.values() for sound in guild.soundboard_sounds]

    def parse_guild_stickers_update(self, data: gw.GuildStickersUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_STICKERS_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        before_stickers = guild.stickers
        for emoji in before_stickers:
            self._stickers.pop(emoji.id, None)
        guild.stickers = tuple(map(lambda d: self.store_sticker(guild, d), data['stickers']))
        self.dispatch('guild_stickers_update', guild, before_stickers, guild.stickers)

    def parse_guild_audit_log_entry_create(self, data: gw.GuildAuditLogEntryCreate) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_AUDIT_LOG_ENTRY_CREATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        entry = AuditLogEntry(
            users=self._users,
            integrations={},
            application_commands={},
            automod_rules={},
            webhooks={},
            data=data,
            guild=guild,
        )
        self.dispatch('audit_log_entry_create', entry)

    # AutoMod events are not actually dispatched for user accounts...
    # Kept here for future proofing

    def parse_auto_moderation_rule_create(self, data: AutoModerationRule) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('AUTO_MODERATION_RULE_CREATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        rule = AutoModRule(data=data, guild=guild, state=self)
        self.dispatch('automod_rule_create', rule)

    def parse_auto_moderation_rule_update(self, data: AutoModerationRule) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('AUTO_MODERATION_RULE_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        rule = AutoModRule(data=data, guild=guild, state=self)
        self.dispatch('automod_rule_update', rule)

    def parse_auto_moderation_rule_delete(self, data: AutoModerationRule) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('AUTO_MODERATION_RULE_DELETE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        rule = AutoModRule(data=data, guild=guild, state=self)
        self.dispatch('automod_rule_delete', rule)

    def parse_auto_moderation_action_execution(self, data: AutoModerationActionExecution) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('AUTO_MODERATION_ACTION_EXECUTION referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        execution = AutoModAction(data=data, state=self)
        self.dispatch('automod_action', execution)

    def _get_create_guild(self, data: gw.GuildCreateEvent) -> Optional[Guild]:
        guild = self._get_guild(int(data['id']))
        unavailable = data.get('unavailable')

        # Discord being Discord sends a GUILD_CREATE after subscribing to a large guild
        # In this case, we just update it and return None to avoid a double dispatch
        if guild is not None:
            guild._from_data(data)
            if unavailable is not False:
                return

        return guild or self._add_guild_from_data(data)

    def _chunk_timeout(self, guild: Guild) -> float:
        return max(5.0, (guild._member_count or 0) / 10000)

    def is_guild_evicted(self, guild: Guild) -> bool:
        return guild.id not in self._guilds

    async def assert_guild_presence_count(self, guild: Guild):
        if not guild._offline_members_hidden or guild._presence_count:
            return

        channel = None
        for channel in guild.channels:
            if channel.permissions_for(guild.default_role).read_messages and channel.permissions_for(guild.me).read_messages:  # type: ignore
                break
        else:
            raise RuntimeError('No channels viewable')

        requests: Dict[Snowflake, List[Tuple[int, int]]] = {str(channel.id): [(0, 99)]}

        def predicate(data):
            return int(data['guild_id']) == guild.id

        await self.subscriptions.subscribe_to_channels(guild, requests)

        try:
            await asyncio.wait_for(self.ws.wait_for('GUILD_MEMBER_LIST_UPDATE', predicate), timeout=10)
        except asyncio.TimeoutError:
            pass

        if not guild._presence_count:
            data = await self.http.get_guild_preview(guild.id)
            guild._presence_count = data['approximate_presence_count']

    @overload
    async def scrape_guild(
        self,
        guild: Guild,
        *,
        wait: Literal[True] = ...,
        cache: bool,
        force_scraping: bool = ...,
        chunk: bool = ...,
        channels: Sequence[abcSnowflake] = ...,
        delay: Union[int, float] = ...,
    ) -> List[Member]: ...

    @overload
    async def scrape_guild(
        self,
        guild: Guild,
        *,
        wait: Literal[False] = ...,
        cache: bool,
        force_scraping: bool = ...,
        chunk: bool = ...,
        channels: Sequence[abcSnowflake] = ...,
        delay: Union[int, float] = ...,
    ) -> asyncio.Future[List[Member]]: ...

    async def scrape_guild(
        self,
        guild: Guild,
        *,
        wait: bool = True,
        cache: bool,
        force_scraping: bool = False,
        chunk: bool = False,
        channels: Sequence[abcSnowflake] = MISSING,
        delay: Union[int, float] = MISSING,
    ) -> Union[List[Member], asyncio.Future[List[Member]]]:
        if not guild.me:
            await guild.query_members(user_ids=[self.self_id], cache=True)

        if (
            not chunk
            and not force_scraping
            and guild.me
            and any(
                {
                    guild.me.guild_permissions.kick_members,
                    guild.me.guild_permissions.ban_members,
                    guild.me.guild_permissions.manage_roles,
                }
            )
        ):
            request = self._chunk_requests.get(guild.id)
            if request is None:
                self._chunk_requests[guild.id] = request = ChunkRequest(guild.id, self.loop, self._get_guild, cache=cache)
                try:
                    await self.chunker([guild.id], nonce=request.nonce)
                except BaseException as exc:
                    if self._chunk_requests.get(guild.id) is request:
                        del self._chunk_requests[guild.id]
                    request.fail(exc if isinstance(exc, Exception) else ClientException('Member chunk request cancelled.'))
                    raise
        else:
            if not chunk:
                await self.assert_guild_presence_count(guild)

            request = self._scrape_requests.get(guild.id)
            if request is None:
                self._scrape_requests[guild.id] = request = MemberSidebar(
                    guild, channels, chunk=chunk, cache=cache, loop=self.loop, delay=delay or 0
                )
                if not request.channels:
                    del self._scrape_requests[guild.id]
                    if chunk:
                        raise ClientException('Guild cannot be chunked: no channels viewable by @everyone')
                    raise ClientException('Failed to automatically choose channels; please specify them manually')
                request.start()

        if wait:
            return await request.wait()
        return request.get_future()

    @overload
    async def chunk_guild(
        self, guild: Guild, *, nonce: Optional[str] = ..., wait: Literal[True] = ..., cache: Optional[bool] = ...
    ) -> List[Member]: ...

    @overload
    async def chunk_guild(
        self, guild: Guild, *, nonce: Optional[str] = ..., wait: Literal[False] = ..., cache: Optional[bool] = ...
    ) -> asyncio.Future[List[Member]]: ...

    async def chunk_guild(
        self, guild: Guild, *, nonce: Optional[str] = None, wait: bool = True, cache: Optional[bool] = None
    ) -> Union[List[Member], asyncio.Future[List[Member]]]:
        cache = cache or self.member_cache_flags.joined
        request = self._chunk_requests.get(guild.id)
        if request is None:
            self._chunk_requests[guild.id] = request = ChunkRequest(
                guild.id, self.loop, self._get_guild, cache=cache, nonce=nonce
            )
            if not nonce:
                try:
                    await self.chunker([guild.id], nonce=request.nonce)
                except BaseException as exc:
                    # The request was never sent: leaving it registered made every
                    # later chunk() join it and wait forever
                    if self._chunk_requests.get(guild.id) is request:
                        del self._chunk_requests[guild.id]
                    request.fail(exc if isinstance(exc, Exception) else ClientException('Member chunk request cancelled.'))
                    raise

        if wait:
            return await request.wait()
        return request.get_future()

    async def _chunk_and_dispatch(self, guild: Guild, chunk: bool, unavailable: Optional[bool]) -> None:
        timeout = self._chunk_timeout(guild)

        if chunk:
            coro = None
            if await self._can_chunk_guild(guild):
                coro = self.chunk_guild(guild)
            elif not guild._offline_members_hidden:
                try:
                    coro = await self.scrape_guild(guild, wait=False, cache=True, chunk=True)
                except ClientException:
                    pass

            if coro is not None:
                try:
                    await asyncio.wait_for(coro, timeout=timeout)
                except asyncio.TimeoutError:
                    _log.warning('Somehow timed out waiting for chunks for guild %s.', guild.id)
                except (ClientException, InvalidData):
                    pass

        if unavailable is False:
            self.dispatch('guild_available', guild)
        else:
            self.dispatch('guild_join', guild)

    @_gc_paused
    def parse_guild_create(self, data: gw.GuildCreateEvent):
        if 'properties' in data:
            data.update(data.pop('properties'))  # type: ignore

        guild = self._get_create_guild(data)
        if guild is None:
            return

        if data.get('has_threads_subscription'):
            self.subscriptions._initial_thread_subscription(guild.id)

        if self._subscribe_guilds and not guild.unavailable:
            utils._spawn_background(self.subscribe_guild(guild), loop=self.loop)

        # Chunk if needed
        needs_chunking = self._guild_needs_chunking(guild)
        utils._spawn_background(self._chunk_and_dispatch(guild, needs_chunking, data.get('unavailable')), loop=self.loop)

    def parse_guild_update(self, data: gw.GuildUpdateEvent) -> None:
        guild = self._get_guild(int(data['id']))
        if guild is not None:
            old_guild = copy.copy(guild)
            guild._from_data(data)
            self.dispatch('guild_update', old_guild, guild)
        else:
            _log.debug('GUILD_UPDATE referencing an unknown guild ID: %s. Discarding.', data['id'])

    def parse_guild_delete(self, data: gw.GuildDeleteEvent) -> None:
        guild = self._get_guild(int(data['id']))
        if guild is None:
            _log.debug('GUILD_DELETE referencing an unknown guild ID: %s. Discarding.', data['id'])
            return

        if data.get('unavailable', False):
            # GUILD_DELETE with unavailable being True means that the
            # guild that was available is now currently unavailable
            guild.unavailable = True
            self.dispatch('guild_unavailable', guild)
            return

        # Cleanup the message cache
        if self._messages is not None:
            self._messages: Optional[Deque[Message]] = deque(
                (msg for msg in self._messages if msg.guild != guild), maxlen=self.max_messages
            )
            self._messages_index = {msg.id: msg for msg in self._messages}

        self._remove_guild(guild)
        self.dispatch('guild_remove', guild)

    def parse_guild_feature_ack(self, data: gw.NonChannelAckEvent) -> None:
        self.read_state_version = data.get('version', self.read_state_version)
        guild = self._get_guild(int(data['resource_id']))
        if guild is None:
            _log.debug('GUILD_FEATURE_ACK referencing an unknown guild ID: %s. Discarding.', data['resource_id'])
            return

        raw = RawGuildFeatureAckEvent(data, self)
        read_state = self.get_read_state(guild.id, raw.type)
        read_state.last_acked_id = int(data['entity_id'])
        self.dispatch('guild_feature_ack', raw)

        # Rich events here
        if read_state.type == ReadStateType.scheduled_events:
            event = guild.get_scheduled_event(read_state.last_acked_id)
            if event is not None:
                self.dispatch('scheduled_event_ack', event)

    def parse_guild_ban_add(self, data: gw.GuildBanAddEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            try:
                user = User(data=data['user'], state=self)
            except KeyError:
                pass
            else:
                member = guild.get_member(user.id) or user
                self.dispatch('member_ban', guild, member)

    def parse_guild_ban_remove(self, data: gw.GuildBanRemoveEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None and 'user' in data:
            user = self.store_user(data['user'])
            self.dispatch('member_unban', guild, user)

    def parse_guild_role_create(self, data: gw.GuildRoleCreateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_ROLE_CREATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        role_data = data['role']
        role = Role(guild=guild, data=role_data, state=self)
        guild._add_role(role)
        self.dispatch('guild_role_create', role)

    def parse_guild_role_delete(self, data: gw.GuildRoleDeleteEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            role_id = int(data['role_id'])
            try:
                role = guild._remove_role(role_id)
            except KeyError:
                return
            else:
                self.dispatch('guild_role_delete', role)
        else:
            _log.debug('GUILD_ROLE_DELETE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_role_update(self, data: gw.GuildRoleUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            role_data = data['role']
            role_id = int(role_data['id'])
            role = guild.get_role(role_id)
            if role is not None:
                old_role = copy.copy(role)
                role._update(role_data)
                self.dispatch('guild_role_update', old_role, role)
        else:
            _log.debug('GUILD_ROLE_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])

    @_gc_paused
    def parse_guild_members_chunk(self, data: gw.GuildMembersChunkEvent) -> None:
        guild_id = int(data['guild_id'])
        guild = self._get_guild(guild_id)
        presences = data.get('presences', [])

        if guild is None:
            return

        members = []
        for member_data in data.get('members', []):
            try:
                members.append(Member(guild=guild, data=member_data, state=self))
            except (KeyError, TypeError, ValueError):
                # One malformed entry must not lose the whole chunk (and leave
                # its request waiting for the timeout)
                _log.warning('Skipping a malformed member in a chunk for guild ID %s.', guild_id, exc_info=True)
        _log.debug('Processed a chunk for %s members in guild ID %s.', len(members), guild_id)

        if presences:
            empty_tuple = ()
            member_dict: Dict[Snowflake, Member] = {str(member.id): member for member in members}
            for presence in presences:
                user = presence['user']
                member_id = user['id']
                member = member_dict.get(member_id)
                if member is not None:
                    member._presence_update(presence, empty_tuple)

        complete = data.get('chunk_index', 0) + 1 == data.get('chunk_count')
        self.process_chunk_requests(guild_id, data.get('nonce'), members, complete)

    def parse_guild_integrations_update(self, data: gw.GuildIntegrationsUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            self.dispatch('guild_integrations_update', guild)
        else:
            _log.debug('GUILD_INTEGRATIONS_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_integration_create(self, data: gw.IntegrationCreateEvent) -> None:
        guild_id = int(data['guild_id'])
        guild = self._get_guild(guild_id)
        if guild is not None:
            cls, _ = _integration_factory(data['type'])
            integration = cls(data=data, guild=guild)
            self.dispatch('integration_create', integration)
        else:
            _log.debug('INTEGRATION_CREATE referencing an unknown guild ID: %s. Discarding.', guild_id)

    def parse_integration_update(self, data: gw.IntegrationUpdateEvent) -> None:
        guild_id = int(data['guild_id'])
        guild = self._get_guild(guild_id)
        if guild is not None:
            cls, _ = _integration_factory(data['type'])
            integration = cls(data=data, guild=guild)
            self.dispatch('integration_update', integration)
        else:
            _log.debug('INTEGRATION_UPDATE referencing an unknown guild ID: %s. Discarding.', guild_id)

    def parse_integration_delete(self, data: gw.IntegrationDeleteEvent) -> None:
        guild_id = int(data['guild_id'])
        guild = self._get_guild(guild_id)
        if guild is not None:
            raw = RawIntegrationDeleteEvent(data)
            self.dispatch('raw_integration_delete', raw)
        else:
            _log.debug('INTEGRATION_DELETE referencing an unknown guild ID: %s. Discarding.', guild_id)

    def parse_webhooks_update(self, data: gw.WebhooksUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('WEBHOOKS_UPDATE referencing an unknown guild ID: %s. Discarding', data['guild_id'])
            return

        channel_id = utils._get_as_snowflake(data, 'channel_id')
        channel = guild.get_channel(channel_id)  # type: ignore # None is okay here
        if channel is not None:
            self.dispatch('webhooks_update', channel)
        else:
            _log.debug('WEBHOOKS_UPDATE referencing an unknown channel ID: %s. Discarding.', data['channel_id'])

    def parse_stage_instance_create(self, data: gw.StageInstanceCreateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            stage_instance = StageInstance(guild=guild, state=self, data=data)
            guild._stage_instances[stage_instance.id] = stage_instance
            self.dispatch('stage_instance_create', stage_instance)
        else:
            _log.debug('STAGE_INSTANCE_CREATE referencing unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_stage_instance_update(self, data: gw.StageInstanceUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            stage_instance = guild._stage_instances.get(int(data['id']))
            if stage_instance is not None:
                old_stage_instance = copy.copy(stage_instance)
                stage_instance._update(data)
                self.dispatch('stage_instance_update', old_stage_instance, stage_instance)
            else:
                _log.debug('STAGE_INSTANCE_UPDATE referencing unknown stage instance ID: %s. Discarding.', data['id'])
        else:
            _log.debug('STAGE_INSTANCE_UPDATE referencing unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_stage_instance_delete(self, data: gw.StageInstanceDeleteEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            try:
                stage_instance = guild._stage_instances.pop(int(data['id']))
            except KeyError:
                pass
            else:
                self.dispatch('stage_instance_delete', stage_instance)
        else:
            _log.debug('STAGE_INSTANCE_DELETE referencing unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_scheduled_event_create(self, data: gw.GuildScheduledEventCreateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            scheduled_event = ScheduledEvent(state=self, data=data)
            guild._scheduled_events[scheduled_event.id] = scheduled_event
            self.dispatch('scheduled_event_create', scheduled_event)

            read_state = self.get_read_state(guild.id, ReadStateType.scheduled_events)
            if not guild.notification_settings.mute_scheduled_events:
                # Increment badge count if we're not muted
                read_state.badge_count += 1
            if scheduled_event.creator_id == self.self_id:
                # Implicitly ack created events
                read_state.last_acked_id = scheduled_event.id
                read_state.badge_count = 0
        else:
            _log.debug('SCHEDULED_EVENT_CREATE referencing unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_scheduled_event_update(self, data: gw.GuildScheduledEventUpdateEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            scheduled_event = guild._scheduled_events.get(int(data['id']))
            if scheduled_event is not None:
                old_scheduled_event = copy.copy(scheduled_event)
                scheduled_event._update(data)
                self.dispatch('scheduled_event_update', old_scheduled_event, scheduled_event)
            else:
                _log.debug('SCHEDULED_EVENT_UPDATE referencing unknown scheduled event ID: %s. Discarding.', data['id'])
        else:
            _log.debug('SCHEDULED_EVENT_UPDATE referencing unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_scheduled_event_delete(self, data: gw.GuildScheduledEventDeleteEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            scheduled_event = guild._scheduled_events.pop(int(data['id']), ScheduledEvent(state=self, data=data))
            self.dispatch('scheduled_event_delete', scheduled_event)
        else:
            _log.debug('SCHEDULED_EVENT_DELETE referencing unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_scheduled_event_user_add(self, data: gw.GuildScheduledEventUserAdd) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            scheduled_event = guild._scheduled_events.get(int(data['guild_scheduled_event_id']))
            if scheduled_event is not None:
                user_id = int(data['user_id'])
                user = self.get_user(user_id)
                if user is not None:
                    scheduled_event._add_user(user)
                    self.dispatch('scheduled_event_user_add', scheduled_event, user)
                self.dispatch('raw_scheduled_event_user_add', scheduled_event, user_id)
            else:
                _log.debug(
                    'SCHEDULED_EVENT_USER_ADD referencing unknown scheduled event ID: %s. Discarding.',
                    data['guild_scheduled_event_id'],
                )
        else:
            _log.debug('SCHEDULED_EVENT_USER_ADD referencing unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_scheduled_event_user_remove(self, data: gw.GuildScheduledEventUserRemove) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            scheduled_event = guild._scheduled_events.get(int(data['guild_scheduled_event_id']))
            if scheduled_event is not None:
                user_id = int(data['user_id'])
                user = self.get_user(user_id)
                if user is not None:
                    scheduled_event._pop_user(user.id)
                    self.dispatch('scheduled_event_user_remove', scheduled_event, user)
                self.dispatch('raw_scheduled_event_user_remove', scheduled_event, user_id)
            else:
                _log.debug(
                    'SCHEDULED_EVENT_USER_REMOVE referencing unknown scheduled event ID: %s. Discarding.',
                    data['guild_scheduled_event_id'],
                )
        else:
            _log.debug('SCHEDULED_EVENT_USER_REMOVE referencing unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_join_request_create(self, data: gw.GuildJoinRequestCreateEvent) -> None:
        request = JoinRequest(data=data['request'], state=self)
        # The event is also received as a moderator, but only our own requests are cached
        # n.b. this may be doubly received (i.e. for a moderator w/ a join request) in some rare cases but currently we don't care
        if request.user_id == self.self_id:
            self._join_requests[request.guild_id] = request
        self.dispatch('join_request_create', request)

    def parse_guild_join_request_update(self, data: gw.GuildJoinRequestUpdateEvent) -> None:
        request = JoinRequest(data=data['request'], state=self)
        if request.user_id == self.self_id:
            self._join_requests[request.guild_id] = request
        self.dispatch('join_request_update', request)

    def parse_guild_join_request_delete(self, data: gw.GuildJoinRequestDeleteEvent) -> None:
        raw = RawJoinRequestDeleteEvent(data, self)
        if raw.user_id == self.self_id:
            self._join_requests.pop(raw.guild_id, None)
        self.dispatch('raw_join_request_delete', raw)

        user = self.get_user(raw.user_id)
        if user is not None:
            self.dispatch('join_request_delete', raw.guild, user)

    def parse_guild_directory_entry_create(self, data: gw.DirectoryEntryEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_DIRECTORY_ENTRY_CREATE referencing unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        channel = guild.get_channel(int(data['directory_channel_id']))
        if channel is None:
            _log.debug(
                'GUILD_DIRECTORY_ENTRY_CREATE referencing unknown channel ID: %s. Discarding.', data['directory_channel_id']
            )
            return

        entry = DirectoryEntry(data=data, state=self, channel=channel)  # type: ignore # channel should always be a DirectoryChannel
        self.dispatch('directory_entry_create', entry)

    def parse_guild_directory_entry_update(self, data: gw.DirectoryEntryEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_DIRECTORY_ENTRY_UPDATE referencing unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        channel = guild.get_channel(int(data['directory_channel_id']))
        if channel is None:
            _log.debug(
                'GUILD_DIRECTORY_ENTRY_UPDATE referencing unknown channel ID: %s. Discarding.', data['directory_channel_id']
            )
            return

        entry = DirectoryEntry(data=data, state=self, channel=channel)  # type: ignore # channel should always be a DirectoryChannel
        self.dispatch('directory_entry_update', entry)

    def parse_guild_directory_entry_delete(self, data: gw.DirectoryEntryDeleteEvent) -> None:
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            _log.debug('GUILD_DIRECTORY_ENTRY_DELETE referencing unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        channel = guild.get_channel(int(data['directory_channel_id']))
        if channel is None:
            _log.debug(
                'GUILD_DIRECTORY_ENTRY_DELETE referencing unknown channel ID: %s. Discarding.', data['directory_channel_id']
            )
            return

        entry = DirectoryEntry(data=data, state=self, channel=channel)  # type: ignore # channel should always be a DirectoryChannel
        self.dispatch('directory_entry_delete', entry)

    def parse_call_create(self, data: gw.CallCreateEvent) -> None:
        channel_id = int(data['channel_id'])
        channel = self._get_private_channel(channel_id)
        if channel is None:
            _log.debug('CALL_CREATE referencing unknown channel ID: %s. Discarding.', data['channel_id'])
            return

        call = self._calls.get(channel_id)
        if call is not None:
            # Should only happen for unavailable calls
            old_call = copy.copy(call)
            call._update(data)
            self.dispatch('call_update', old_call, call)

        message = self._get_message(int(data['message_id']))
        call = channel._add_call(data=data, state=self, message=message, channel=channel)
        self._calls[channel.id] = call
        self.dispatch('call_create', call)

    def parse_call_update(self, data: gw.CallUpdateEvent) -> None:
        call = self._calls.get(int(data['channel_id']))
        if call is None:
            _log.debug('CALL_UPDATE referencing unknown call (channel ID: %s). Discarding.', data['channel_id'])
            return
        old_call = copy.copy(call)
        call._update(data)
        self.dispatch('call_update', old_call, call)

    def parse_call_delete(self, data: gw.CallDeleteEvent) -> None:
        call = self._calls.pop(int(data['channel_id']), None)
        if call is not None:
            if data.get('unavailable'):
                old_call = copy.copy(call)
                call.unavailable = True
                self.dispatch('call_update', old_call, call)
                return

            call._delete()
            self._call_message_cache.pop(call._message_id, None)
            # Purge any voice states still pointing at the deleted call's channel
            channel_id = call.channel.id
            self._voice_states = {
                k: v for k, v in self._voice_states.items() if v is None or v.channel is None or v.channel.id != channel_id
            }
            self.dispatch('call_delete', call)

    def parse_voice_state_update(self, data: gw.VoiceStateUpdateEvent) -> None:
        guild_id = utils._get_as_snowflake(data, 'guild_id')
        guild = self._get_guild(guild_id)
        channel_id = utils._get_as_snowflake(data, 'channel_id')
        flags = self.member_cache_flags
        self_id = self.self_id

        if guild_id is not None and guild is None:
            _log.debug('VOICE_STATE_UPDATE referencing unknown guild ID: %s. Discarding.', guild_id)
            return

        if int(data['user_id']) == self_id:
            voice = self._get_voice_client(guild.id if guild else self_id)
            if voice is not None:
                coro = voice.on_voice_state_update(data)
                utils._spawn_background(logging_coroutine(coro, info='Voice Protocol voice state update handler'))

        if guild is not None:
            member, before, after = guild._update_voice_state(data, channel_id)
            if member is not None:
                if flags.voice:
                    if channel_id is None and flags._voice_only and member.id != self_id:
                        guild._remove_member(member)
                    elif channel_id is not None:
                        guild._add_member(member)

                self.dispatch('voice_state_update', member, before, after)
            else:
                _log.debug('VOICE_STATE_UPDATE referencing an unknown member ID: %s. Discarding.', data['user_id'])
        else:
            user, before, after = self._update_voice_state(data, channel_id)
            self.dispatch('voice_state_update', user, before, after)

    def parse_voice_channel_effect_send(self, data: gw.VoiceChannelEffectSendEvent) -> None:
        # Effects arrive for guild voice channels and, for user accounts, for
        # private calls (no guild_id), which the Rapptz original did not handle
        channel_id = int(data['channel_id'])
        user_id = int(data['user_id'])
        guild_id = utils._get_as_snowflake(data, 'guild_id')
        if guild_id is not None:
            guild = self._get_guild(guild_id)
            if guild is None:
                _log.debug('VOICE_CHANNEL_EFFECT_SEND referencing an unknown guild ID: %s. Discarding.', guild_id)
                return
            channel = guild.get_channel(channel_id)
            user = guild.get_member(user_id)
        else:
            channel = self._get_private_channel(channel_id)
            user = self.get_user(user_id)
        if channel is None:
            _log.debug('VOICE_CHANNEL_EFFECT_SEND referencing an unknown channel ID: %s. Discarding.', channel_id)
            return
        effect = VoiceChannelEffect(state=self, data=data, channel=channel, user=user)  # type: ignore # a voice channel
        self.dispatch('voice_channel_effect', effect)

    def parse_voice_server_update(self, data: gw.VoiceServerUpdateEvent) -> None:
        key_id = utils._get_as_snowflake(data, 'guild_id')
        if key_id is None:
            key_id = self.self_id

        vc = self._get_voice_client(key_id)
        if vc is not None:
            coro = vc.on_voice_server_update(data)
            utils._spawn_background(logging_coroutine(coro, info='Voice Protocol voice server update handler'))

    def _dispatch_stream_protocol_event(self, stream_key: StreamKey, handler: str, *args: Any, info: str) -> None:
        stream = self._get_stream_client(stream_key)
        if stream is not None:
            coro = getattr(stream, handler)(*args)
            utils._spawn_background(logging_coroutine(coro, info=info))

    def parse_stream_create(self, data: gw.StreamCreateEvent) -> None:
        old, stream = self._store_stream(data)
        if old is not None and old.unavailable:
            stream.unavailable = False
            self._dispatch_stream_protocol_event(
                stream.key,
                'on_stream_available',
                stream,
                info='Stream Protocol stream available handler',
            )
            self.dispatch('stream_available', stream)
            return

        self._dispatch_stream_protocol_event(
            stream.key,
            'on_stream_create',
            stream,
            info='Stream Protocol stream create handler',
        )
        self.dispatch('stream_create', stream)

    def parse_stream_server_update(self, data: gw.StreamServerUpdateEvent) -> None:
        stream_key = StreamKey.from_value(data['stream_key'])
        self._stream_server_updates[stream_key] = data
        self._dispatch_stream_protocol_event(
            stream_key,
            'on_stream_server_update',
            data,
            info='Stream Protocol stream server update handler',
        )

    def parse_stream_update(self, data: gw.StreamUpdateEvent) -> None:
        stream_key = StreamKey.from_value(data['stream_key'])
        stream = self._streams.get(stream_key)
        if stream is None:
            _log.debug('STREAM_UPDATE referencing unknown stream %s. Discarding.', stream_key)
            return

        old = copy.copy(stream)
        stream._update(data)
        self._dispatch_stream_protocol_event(
            stream.key,
            'on_stream_update',
            old,
            stream,
            info='Stream Protocol stream update handler',
        )
        self.dispatch('stream_update', old, stream)

    def parse_stream_delete(self, data: gw.StreamDeleteEvent) -> None:
        stream_key = StreamKey.from_value(data['stream_key'])
        reason = try_enum(StreamDeleteReason, data['reason'])
        if data.get('unavailable'):
            stream = self._streams.get(stream_key)
            if stream is None:
                stream = Stream(state=self, data=data)
                self._streams[stream.key] = stream
            was_unavailable = stream.unavailable
            stream.unavailable = True
            if was_unavailable:
                return
            self._dispatch_stream_protocol_event(
                stream.key,
                'on_stream_unavailable',
                stream,
                info='Stream Protocol stream unavailable handler',
            )
            self.dispatch('stream_unavailable', stream)
            return

        stream = self._streams.pop(stream_key, None)
        if stream is None:
            stream = Stream(state=self, data=data)
        self._stream_server_updates.pop(stream_key, None)
        self._dispatch_stream_protocol_event(
            stream.key,
            'on_stream_delete',
            stream,
            reason,
            info='Stream Protocol stream delete handler',
        )
        self.dispatch('stream_delete', stream, reason)

    def parse_typing_start(self, data: gw.TypingStartEvent) -> None:
        channel, guild = self._get_guild_channel(data)
        if channel is not None:
            member = None
            user_id = int(data['user_id'])
            if isinstance(channel, DMChannel):
                member = channel.recipient

            elif isinstance(channel, (Thread, TextChannel)) and guild is not None:
                member = guild.get_member(user_id)

                if member is None:
                    member_data = data.get('member')
                    if member_data:
                        member = Member(data=member_data, state=self, guild=guild)

            elif isinstance(channel, GroupChannel):
                member = utils.find(lambda x: x.id == user_id, channel.recipients)

            if member is not None:
                timestamp = datetime.datetime.fromtimestamp(data['timestamp'], tz=datetime.timezone.utc)
                self.dispatch('typing', channel, member, timestamp)

    def parse_relationship_add(self, data: gw.RelationshipAddEvent) -> None:
        key = int(data['id'])
        new = self._relationships.get(key)
        if new is None:
            relationship = Relationship(state=self, data=data)
            self._relationships[key] = relationship
            self.dispatch('relationship_add', relationship)
        else:
            old = copy.copy(new)
            new._update(data)
            self.dispatch('relationship_update', old, new)

    def parse_relationship_remove(self, data: gw.RelationshipEvent) -> None:
        key = int(data['id'])
        try:
            old = self._relationships.pop(key)
        except KeyError:
            _log.warning('RELATIONSHIP_REMOVE referencing unknown relationship ID: %s. Discarding.', key)
        else:
            self.dispatch('relationship_remove', old)

    def parse_relationship_update(self, data: gw.RelationshipEvent) -> None:
        key = int(data['id'])
        new = self._relationships.get(key)
        if new is None:
            relationship = Relationship(state=self, data=data)  # type: ignore
            self._relationships[key] = relationship
        else:
            old = copy.copy(new)
            new._update(data)
            self.dispatch('relationship_update', old, new)

    def parse_friend_suggestion_create(self, data: gw.FriendSuggestionCreateEvent):
        self.friend_suggestion_count += 1
        self.dispatch('friend_suggestion_add', FriendSuggestion(state=self, data=data))

    def parse_friend_suggestion_delete(self, data: gw.FriendSuggestionDeleteEvent):
        self.friend_suggestion_count -= 1
        user_id = int(data['suggested_user_id'])
        user = self.get_user(user_id)
        if user:
            self.dispatch('friend_suggestion_remove', user)
        self.dispatch('raw_friend_suggestion_remove', user_id)

    def parse_interaction_create(self, data: gw.InteractionEvent) -> None:
        if 'nonce' not in data:
            # Most likely user is messing around with the raw API
            return

        # Each nonce gets one INTERACTION_CREATE: pop it so the bounded cache only
        # ever holds in-flight interactions (it used to keep the last 50, so a
        # burst could evict entries that were still waiting for this event)
        context = self._interaction_cache.pop(data['nonce'], None)
        if context is None:
            _log.debug('INTERACTION_CREATE for an unknown nonce %s; channel and name are unavailable.', data['nonce'])
            context = (0, None, None)
        type, name, channel = context
        i = Interaction._from_self(channel, type=type, user=self.user, name=name, **data)  # type: ignore # self.user is always present here
        self._add_interaction(i)
        self.dispatch('interaction', i)

    def parse_interaction_success(self, data: gw.InteractionEvent) -> None:
        id = int(data['id'])
        i = self._get_interaction_from_terminal_event(data)
        if i is None:
            _log.warning('INTERACTION_SUCCESS referencing an unknown interaction ID: %s. Discarding.', id)
            return

        i.successful = True
        self.dispatch('interaction_finish', i)
        self._pop_interaction(i, data)

    def parse_interaction_failure(self, data: gw.InteractionFailureEvent) -> None:
        id = int(data['id'])
        i = self._get_interaction_from_terminal_event(data)
        if i is None:
            _log.warning('INTERACTION_FAILURE referencing an unknown interaction ID: %s. Discarding.', id)
            return

        i.successful = False
        i.reason_code = try_enum(InteractionFailureReason, data['reason_code'])
        self.dispatch('interaction_finish', i)
        self._pop_interaction(i, data)

    def parse_application_command_autocomplete_response(self, data: gw.ApplicationCommandAutocompleteEvent) -> None:
        from .commands import ApplicationCommandAutocomplete

        nonce = data['nonce']
        # One response per request: pop so the bounded cache holds only pending ones
        context = self._application_command_autocomplete_cache.pop(nonce, None)
        if context is None:
            _log.warning('APPLICATION_COMMAND_AUTOCOMPLETE_RESPONSE referencing an unknown nonce: %s. Discarding.', nonce)
            return

        response = ApplicationCommandAutocomplete(state=self, data=data, context=context)
        self.dispatch('application_command_autocomplete_response', response)

    def parse_interaction_modal_create(self, data: gw.InteractionModalCreateEvent) -> None:
        id = int(data['id'])
        interaction = self._get_interaction_from_terminal_event(data)
        if interaction is None:
            _log.warning('INTERACTION_MODAL_CREATE referencing an unknown interaction ID: %s. Discarding.', id)
            return

        modal = Modal(data=data, interaction=interaction)
        interaction.modal = modal
        self._add_interaction(interaction)
        self.dispatch('modal', modal)

    def parse_interaction_iframe_modal_create(self, data: gw.InteractionIframeModalCreateEvent) -> None:
        interaction = None
        channel = None
        try:
            interaction = self._interactions.get(int(data['id']), None)
        except KeyError:
            pass
        else:
            if interaction is not None:
                channel = interaction.channel

        nonce = data.get('nonce')
        if interaction is None and nonce is not None:
            type, name, channel = self._interaction_cache.get(nonce, (0, None, None))
            if channel is not None:
                interaction = Interaction._from_self(
                    channel,  # type: ignore
                    id=data['id'],
                    type=type,
                    nonce=nonce,
                    user=self.user,  # type: ignore # self.user is always present here
                    name=name,
                )
                self._add_interaction(interaction)

        if channel is None:
            channel = self._get_or_create_partial_messageable(int(data['channel_id']))
        if channel is None:
            _log.warning(
                'INTERACTION_IFRAME_MODAL_CREATE referencing an unknown channel ID: %s. Discarding.', data['channel_id']
            )
            return

        modal = IFrameModal(data=data, state=self, channel=channel, interaction=interaction)  # type: ignore
        modals = self._interaction_iframe_modals
        modals[modal.application.id] = modal
        if len(modals) > 32:
            # Dropped CLOSE events would otherwise strand entries until reconnect
            modals.popitem(last=False)
        self.dispatch('iframe_modal', modal)

    def parse_interaction_iframe_modal_close(self, data: gw.InteractionIframeModalCloseEvent) -> None:
        application_id = int(data['application_id'])
        modal = self._interaction_iframe_modals.pop(application_id, None)
        if modal is None:
            _log.warning(
                'INTERACTION_IFRAME_MODAL_CLOSE referencing an unknown application ID: %s. Discarding.', application_id
            )
            return

        self.dispatch('iframe_modal_close', modal)

    # Silence "unknown event" warnings for events parsed elsewhere
    parse_nothing = lambda *_: None
    parse_guild_application_commands_update = parse_nothing  # Dead event
    parse_user_settings_update = parse_nothing  # Dead event

    def parse_message_poll_vote_add(self, data: gw.PollVoteActionEvent) -> None:
        raw = RawPollVoteActionEvent(data)

        self.dispatch('raw_poll_vote_add', raw)

        message = self._get_message(raw.message_id)
        guild = self._get_guild(raw.guild_id)

        if guild:
            user = guild.get_member(raw.user_id)
        else:
            user = self.get_user(raw.user_id)

        if message and user:
            poll = self._update_poll_counts(message, raw.answer_id, True, raw.user_id == self.self_id)
            if poll:
                self.dispatch('poll_vote_add', user, poll.get_answer(raw.answer_id))

    def parse_message_poll_vote_remove(self, data: gw.PollVoteActionEvent) -> None:
        raw = RawPollVoteActionEvent(data)

        self.dispatch('raw_poll_vote_remove', raw)

        message = self._get_message(raw.message_id)
        guild = self._get_guild(raw.guild_id)

        if guild:
            user = guild.get_member(raw.user_id)
        else:
            user = self.get_user(raw.user_id)

        if message and user:
            poll = self._update_poll_counts(message, raw.answer_id, False, raw.user_id == self.self_id)
            if poll:
                self.dispatch('poll_vote_remove', user, poll.get_answer(raw.answer_id))

    def _get_reaction_user(self, channel: MessageableChannel, user_id: int) -> Optional[Union[User, Member]]:
        if isinstance(channel, (TextChannel, Thread, VoiceChannel, StageChannel)):
            return channel.guild.get_member(user_id)
        return self.get_user(user_id)

    def get_emoji_from_partial_payload(self, data: PartialEmojiPayload) -> Union[Emoji, PartialEmoji, str]:
        emoji_id = utils._get_as_snowflake(data, 'id')

        if not emoji_id:
            # the name key will be a str
            return data['name']  # type: ignore

        try:
            return self._emojis[emoji_id]
        except KeyError:
            return PartialEmoji.with_state(
                self,
                animated=data.get('animated', False),
                id=emoji_id,
                name=data['name'],  # type: ignore
            )

    def _upgrade_partial_emoji(self, emoji: PartialEmoji) -> Union[Emoji, PartialEmoji, str]:
        emoji_id = emoji.id
        if not emoji_id:
            return emoji.name
        try:
            return self._emojis[emoji_id]
        except KeyError:
            return emoji

    def get_channel(self, id: Optional[int]) -> Optional[Union[Channel, Thread]]:
        if id is None:
            return None

        pm = self._get_private_channel(id)
        if pm is not None:
            return pm

        for guild in self.guilds:
            channel = guild._resolve_channel(id)
            if channel is not None:
                return channel

    def _get_or_create_partial_messageable(self, id: Optional[int]) -> Optional[Union[Channel, Thread]]:
        if id is None:
            return None

        return self.get_channel(id) or PartialMessageable(state=self, id=id)

    def create_message(
        self,
        *,
        channel: MessageableChannel,
        data: MessagePayload,
        search_result: Optional[MessageSearchResultPayload] = None,
    ) -> Message:
        return Message(state=self, channel=channel, data=data, search_result=search_result)

    def _update_message_references(self) -> None:
        # self._messages won't be None when this is called
        for msg in self._messages:  # type: ignore
            if not msg.guild:
                continue

            new_guild = self._get_guild(msg.guild.id)
            if new_guild is not None and new_guild is not msg.guild:
                channel_id = msg.channel.id
                channel = new_guild._resolve_channel(channel_id) or PartialMessageable(
                    state=self, id=channel_id, guild_id=new_guild.id
                )
                msg._rebind_cached_references(new_guild, channel)

    def create_integration_application(self, data: IntegrationApplicationPayload) -> IntegrationApplication:
        return IntegrationApplication(state=self, data=data)

    def create_command_application(
        self,
        data: CommandApplicationPayload,
        *,
        guild: Optional[Guild] = None,
    ) -> CommandApplication:
        return CommandApplication(state=self, data=data, guild=guild)

    def default_guild_settings(self, guild_id: Optional[int]) -> GuildSettings:
        return GuildSettings(data={'guild_id': guild_id}, state=self)  # type: ignore

    def default_channel_settings(self, guild_id: Optional[int], channel_id: int) -> ChannelSettings:
        return ChannelSettings(guild_id, data={'channel_id': channel_id}, state=self)  # type: ignore

    def create_implicit_relationship(self, user: User) -> Relationship:
        relationship = self._relationships.get(user.id)
        if relationship is not None:
            if relationship.type.value == 0:
                relationship.type = RelationshipType.implicit
        else:
            relationship = Relationship._from_implicit(state=self, user=user)
            self._relationships[relationship.id] = relationship
        return relationship

    @property
    def all_session(self) -> Optional[Session]:
        return self._sessions.get('all')

    @property
    def current_session(self) -> Optional[Session]:
        return self._sessions.get(self.session_id)  # type: ignore

    @utils.cached_property
    def client_presence(self) -> FakeClientPresence:
        return FakeClientPresence(self)

    def _build_cache_stats(self, event_loop_lag: float = 0.0) -> CacheStats:
        """Gathers a cache utilization snapshot. See :meth:`Client.cache_stats`."""
        guilds = self._guilds
        per_guild: Dict[int, Dict[str, int]] = {}
        members = 0
        threads = 0
        evicted_members = 0
        evicted_threads = 0
        guild_presences = 0
        for guild_id, guild in guilds.items():
            member_count = len(guild._members)
            thread_count = len(guild._threads)
            presence_count = len(self._guild_presences.get(guild_id, ()))
            evicted_members += guild._evicted_members
            evicted_threads += guild._evicted_threads
            members += member_count
            threads += thread_count
            guild_presences += presence_count
            if member_count or thread_count or presence_count or guild._evicted_members or guild._evicted_threads:
                per_guild[guild_id] = {
                    'members': member_count,
                    'threads': thread_count,
                    'presences': presence_count,
                    'evicted_members': guild._evicted_members,
                    'evicted_threads': guild._evicted_threads,
                    'tracked_evicted_ids': len(guild._evicted_member_ids),
                }

        client = self.client
        if client is not None:
            backlog, oldest_age, created, completed = client._dispatch_backlog_stats()
        else:
            backlog = oldest_age = created = completed = 0

        return CacheStats(
            guilds=len(guilds),
            members=members,
            member_cache_cap=self.max_member_cache,
            total_member_cache_cap=self.max_total_member_cache,
            evicted_members=evicted_members,
            member_hits=self._member_hits,
            member_misses=self._member_misses,
            threads=threads,
            thread_cache_cap=self.max_thread_cache,
            total_thread_cache_cap=self.max_total_thread_cache,
            evicted_threads=evicted_threads,
            guild_presences=guild_presences,
            guild_presence_cap=self.max_guild_presence_cache,
            dm_presences=len(self._presences),
            dm_presence_cap=self.max_presence_cache,
            evicted_presences=self._presence_evictions,
            messages=len(self._messages) if self._messages is not None else 0,
            message_cache_cap=self.max_messages,
            dispatch_backlog=backlog,
            dispatch_oldest_age=oldest_age,
            dispatch_created=created,
            dispatch_completed=completed,
            event_loop_lag=event_loop_lag,
            per_guild=per_guild,
        )

    def create_presence(self, data: gw.BasePresenceUpdate, user_id: int) -> Presence:
        return Presence(data, self, user_id)

    def create_offline_presence(self) -> Presence:
        return Presence._offline()

    def get_presence(self, user_id: int, guild_id: Optional[int] = None) -> Optional[Presence]:
        if user_id == self.self_id:
            # Our own presence is unified
            return self.client_presence

        if guild_id is not None:
            guild = self._guild_presences.get(guild_id)
            if guild is not None:
                presence = guild.get(user_id)
                if presence is not None:
                    # Mark as most recently used
                    guild.move_to_end(user_id)
                return presence
            return
        presences = self._presences
        presence = presences.get(user_id)
        if presence is not None:
            # Mark as most recently used
            presences.move_to_end(user_id)
        return presence

    def remove_presence(self, user_id: int, guild_id: Optional[int] = None) -> None:
        if guild_id is not None:
            guild = self._guild_presences.get(guild_id)
            if guild is not None:
                guild.pop(user_id, None)
        else:
            self._presences.pop(user_id, None)

    def store_presence(self, user_id: int, presence: Presence, guild_id: Optional[int] = None) -> Presence:
        if (
            presence.client_status.status == Status.offline.value
            and not presence.activities
            and not presence.hidden_activities
        ):
            # We don't store empty presences
            self.remove_presence(user_id, guild_id)
            return presence

        if user_id == self.self_id:
            # We don't store our own presence
            return presence

        if guild_id is not None:
            guild = self._guild_presences.get(guild_id)
            if guild is None:
                guild = self._guild_presences[guild_id] = OrderedDict()
            if user_id in guild:
                guild.move_to_end(user_id)
            guild[user_id] = presence
            cap = self.max_guild_presence_cache
            if cap is not None:
                evicted = 0
                while len(guild) > cap:
                    guild.popitem(last=False)
                    evicted += 1
                if evicted:
                    self._presence_evictions += evicted
                    self._log_first_eviction('presence')
        else:
            presences = self._presences
            if user_id in presences:
                presences.move_to_end(user_id)
            presences[user_id] = presence
            cap = self.max_presence_cache
            if cap is not None:
                evicted = 0
                while len(presences) > cap:
                    presences.popitem(last=False)
                    evicted += 1
                if evicted:
                    self._presence_evictions += evicted
                    self._log_first_eviction('presence')
        return presence

    @overload
    def get_read_state(self, id: int, type: ReadStateType = ..., *, if_exists: Literal[False] = ...) -> ReadState: ...

    @overload
    def get_read_state(self, id: int, type: ReadStateType = ..., *, if_exists: Literal[True]) -> Optional[ReadState]: ...

    def get_read_state(
        self, id: int, type: ReadStateType = ReadStateType.channel, *, if_exists: bool = False
    ) -> Optional[ReadState]:
        try:
            return self._read_states[type.value][id]
        except KeyError:
            if not if_exists:
                # Create and store a default read state
                state = ReadState.default(id, type, state=self)
                self.store_read_state(state)
                return state

    def remove_read_state(self, read_state: ReadState) -> None:
        try:
            group = self._read_states[read_state.type.value]
        except KeyError:
            return
        group.pop(read_state.id, None)

    def store_read_state(self, read_state: ReadState):
        try:
            group = self._read_states[read_state.type.value]
        except KeyError:
            group = self._read_states[read_state.type.value] = {}
        group[read_state.id] = read_state

    @utils.cached_property
    def premium_subscriptions_application(self) -> PartialApplication:
        # Hardcoded application for premium subscriptions, highly unlikely to change
        return PartialApplication(
            state=self,
            data={
                'id': 521842831262875670,
                'name': 'Nitro',
                'icon': None,
                'description': '',
                'type': None,
                'is_monetized': False,
                'is_verified': False,
                'is_discoverable': False,
                'hook': True,
                'storefront_available': False,
                'verify_key': '93661a9eefe452d12f51e129e8d9340e7ca53a770158c0ec7970e701534b7420',
                'flags': 0,
            },
        )

    @utils.cached_property
    def premium_subscriptions_sku_ids(self) -> Dict[str, Snowflake]:
        return {
            'none': 628379670982688768,
            'basic': 978380684370378762,
            'legacy': 521842865731534868,
            'classic': 521846918637420545,
            'full': 521847234246082599,
            'guild': 590663762298667008,
        }
