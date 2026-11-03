"""Client(eager_dispatch=True): opt-in eager start for event handler tasks."""

from __future__ import annotations

import asyncio
import logging
import sys

import pytest

import discord

needs_312 = pytest.mark.skipif(sys.version_info < (3, 12), reason='eager tasks need Python 3.12+')


async def _client(**kwargs) -> discord.Client:
    client = discord.Client(**kwargs)
    await client._async_setup_hook()
    return client


@pytest.mark.asyncio
async def test_default_handlers_are_deferred():
    client = await _client()
    ran = []

    async def on_thing():
        ran.append(True)

    client.on_thing = on_thing  # type: ignore[attr-defined]
    client.dispatch('thing')
    assert ran == [], 'without eager_dispatch the handler waits for the loop'
    await asyncio.sleep(0)
    assert ran == [True]
    await client.close()


@needs_312
@pytest.mark.asyncio
async def test_eager_handler_runs_inside_dispatch_and_resumes_normally():
    client = await _client(eager_dispatch=True)
    steps = []

    async def on_thing():
        steps.append('start')
        await asyncio.sleep(0)
        steps.append('after-await')

    client.on_thing = on_thing  # type: ignore[attr-defined]
    client.dispatch('thing')
    assert steps == ['start'], 'runs synchronously up to the first await'
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert steps == ['start', 'after-await']
    for _ in range(3):
        await asyncio.sleep(0)
    assert not client._dispatch_pending, 'backlog bookkeeping still drains'
    await client.close()


@needs_312
@pytest.mark.asyncio
async def test_eager_handler_errors_still_reach_on_error():
    client = await _client(eager_dispatch=True)
    errors = []

    async def on_thing():
        raise ValueError('boom')

    async def on_error(event, *args, **kwargs):
        errors.append((event, sys.exc_info()[0]))

    client.on_thing = on_thing  # type: ignore[attr-defined]
    client.on_error = on_error  # type: ignore[method-assign]
    client.dispatch('thing')
    await asyncio.sleep(0)
    assert errors == [('on_thing', ValueError)]
    await client.close()


@needs_312
@pytest.mark.asyncio
async def test_eager_falls_back_off_the_loop_thread(monkeypatch):
    client = await _client(eager_dispatch=True)
    ran = []

    async def on_thing():
        ran.append(True)

    client.on_thing = on_thing  # type: ignore[attr-defined]
    # Simulate a dispatch from a thread that is not running the client's loop
    monkeypatch.setattr(asyncio, '_get_running_loop', lambda: None)
    client.dispatch('thing')
    monkeypatch.undo()
    assert ran == [], 'must not eager-start off the loop thread (CPython gh-155338)'
    await asyncio.sleep(0)
    assert ran == [True]
    await client.close()


def test_eager_dispatch_ignored_with_warning_before_312(monkeypatch, caplog):
    import discord.client as client_module

    monkeypatch.setattr(client_module.sys, 'version_info', (3, 11, 9))
    with caplog.at_level(logging.WARNING, logger='discord.client'):
        client = discord.Client(eager_dispatch=True)
    assert client._eager_dispatch is False
    assert any('eager_dispatch requires Python 3.12' in r.getMessage() for r in caplog.records)
