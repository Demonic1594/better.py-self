"""
Regressions for the round-5 review: fire-and-forget tasks are strongly
referenced, file downloads don't block the event loop, and an explicit
``require_version=1`` isn't mistaken for ``True``.
"""

from __future__ import annotations

import asyncio
import gc
import threading
from types import SimpleNamespace

import pytest

from discord import utils
from discord.settings import UserSettings


@pytest.mark.asyncio
async def test_spawn_background_survives_gc_and_releases_reference():
    started = asyncio.Event()
    release = asyncio.Event()

    async def worker() -> str:
        started.set()
        await release.wait()
        return 'done'

    task = utils._spawn_background(worker(), name='bg-test')
    task_id = id(task)
    del task
    gc.collect()

    await started.wait()
    held = [t for t in utils._background_tasks if id(t) == task_id]
    assert len(held) == 1, 'the helper must keep a strong reference while the task runs'

    release.set()
    assert await held[0] == 'done'
    await asyncio.sleep(0)  # let the done callback run
    assert held[0] not in utils._background_tasks


@pytest.mark.asyncio
async def test_attachment_save_writes_off_the_event_loop(tmp_path, monkeypatch):
    from discord.message import Attachment

    loop_thread = threading.get_ident()
    writer_threads: list[int] = []
    real_write = utils._write_bytes

    def spy(fp, data):
        writer_threads.append(threading.get_ident())
        return real_write(fp, data)

    monkeypatch.setattr(utils, '_write_bytes', spy)

    async def read(self, *, use_cached: bool = False) -> bytes:
        return b'payload'

    monkeypatch.setattr(Attachment, 'read', read)
    attachment = Attachment.__new__(Attachment)

    target = tmp_path / 'out.bin'
    written = await attachment.save(target)

    assert written == len(b'payload')
    assert target.read_bytes() == b'payload'
    assert writer_threads and writer_threads[0] != loop_thread


class _RecordingHTTP:
    def __init__(self) -> None:
        self.versions: list[object] = []

    async def edit_proto_settings(self, type: int, data: str, required_data_version=None):
        self.versions.append(required_data_version)
        return {'settings': data}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('require_version', 'expected'),
    [(1, 1), (True, 'data_version'), (False, None), (7, 7)],
)
async def test_settings_require_version_int_one_is_not_true(require_version, expected):
    http = _RecordingHTTP()
    state = SimpleNamespace(http=http)
    settings = UserSettings(state, '')  # type: ignore[arg-type]
    settings.settings.versions.data_version = 42

    await settings.edit(require_version=require_version, inbox_tutorial_viewed=True)

    assert http.versions == [42 if expected == 'data_version' else expected]
