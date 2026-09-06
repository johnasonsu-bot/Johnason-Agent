"""Explicit ownership of delegated async streams, independent of finalization."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypeVar


_T = TypeVar("_T")


@asynccontextmanager
async def managed_async_iterator(
    stream: AsyncIterator[_T],
) -> AsyncIterator[AsyncIterator[_T]]:
    """Close a delegated iterator before returning control to its consumer.

    Cleanup runs in the consuming task; the resource owner supplies its cleanup
    deadline. A secondary cleanup failure must not replace a projection/query
    failure, while cancellation remains observable.
    """
    close = getattr(stream, "aclose", None)
    try:
        yield stream
    except BaseException:
        if callable(close):
            try:
                await close()
            except Exception:
                pass
        raise
    else:
        if callable(close):
            await close()
