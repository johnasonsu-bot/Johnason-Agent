from __future__ import annotations

import asyncio

import pytest

from workbench.runtime.async_stream import managed_async_iterator


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_replace_projection_failure() -> None:
    original = ValueError("rejected event")

    async def query():
        try:
            yield "accepted"
        finally:
            raise RuntimeError("cleanup failed")

    with pytest.raises(ValueError) as captured:
        async with managed_async_iterator(query()) as stream:
            await anext(stream)
            raise original
    assert captured.value is original


@pytest.mark.asyncio
async def test_cleanup_failure_is_visible_without_primary_failure() -> None:
    async def query():
        try:
            yield "accepted"
        finally:
            raise RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="cleanup failed"):
        async with managed_async_iterator(query()) as stream:
            await anext(stream)


@pytest.mark.asyncio
async def test_consumer_cancellation_closes_stream_before_propagating() -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def query():
        try:
            yield "accepted"
        finally:
            closed.set()

    held_stream = query()

    async def consume():
        async with managed_async_iterator(held_stream) as stream:
            await anext(stream)
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_cancellation_during_cleanup_is_not_swallowed() -> None:
    closing = asyncio.Event()

    async def query():
        try:
            yield "accepted"
        finally:
            closing.set()
            await asyncio.Event().wait()

    async def consume():
        async with managed_async_iterator(query()) as stream:
            await anext(stream)
            raise ValueError("rejected event")

    task = asyncio.create_task(consume())
    await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
