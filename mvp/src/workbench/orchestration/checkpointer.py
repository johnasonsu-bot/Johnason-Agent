from __future__ import annotations

import os

os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

import sqlite3
from hashlib import sha256
from pathlib import Path
from threading import Lock
from types import TracebackType

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver


class GraphExecutionFenceBusy(RuntimeError):
    """Raised when another local runtime owns a graph thread execution fence."""


class GraphExecutionFence:
    """An OS-released, no-TTL SQLite transaction fence for one graph thread."""

    def __init__(self, fence_path: Path) -> None:
        self._connection = sqlite3.connect(
            fence_path, timeout=0, isolation_level=None, check_same_thread=False
        )
        self._release_lock = Lock()
        self._released = False
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            self._connection.close()
            raise GraphExecutionFenceBusy("graph thread is already executing") from error

    def release(self) -> None:
        """End the owning transaction exactly once; process death also releases it."""
        with self._release_lock:
            if self._released:
                return
            self._released = True
            try:
                self._connection.rollback()
            finally:
                self._connection.close()

    def __enter__(self) -> GraphExecutionFence:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.release()
        return False


class _ManagedSqliteSaver(SqliteSaver):
    checkpoint_path: Path

    def __init__(self, connection: sqlite3.Connection, *, path: Path, serde: JsonPlusSerializer) -> None:
        super().__init__(connection, serde=serde)
        self.checkpoint_path = path

    def __enter__(self) -> SqliteSaver:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.conn.close()
        return False


def open_graph_checkpointer(path: Path) -> SqliteSaver:
    """Open a locally persisted LangGraph checkpointer with safe serialization."""
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path = path.resolve()
    if str(path) == ":memory:" or str(canonical_path) == ":memory:":
        raise ValueError("graph checkpoint must be a filesystem SQLite path")
    connection = sqlite3.connect(canonical_path, check_same_thread=False)
    serializer = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )
    return _ManagedSqliteSaver(connection, path=canonical_path, serde=serializer)


def acquire_graph_execution_fence(
    checkpointer: SqliteSaver, thread_id: str
) -> GraphExecutionFence:
    """Acquire a nonblocking per-thread fence next to this canonical checkpoint."""
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("graph execution fence requires a nonblank thread ID")
    checkpoint_path = getattr(checkpointer, "checkpoint_path", None)
    if not isinstance(checkpoint_path, Path):
        raise TypeError("graph execution fence requires the local managed checkpointer")
    canonical_path = checkpoint_path.resolve()
    digest = sha256(thread_id.encode("utf-8")).hexdigest()
    fence_directory = canonical_path.parent / f".{canonical_path.name}.fences"
    fence_directory.mkdir(mode=0o700, exist_ok=True)
    return GraphExecutionFence(fence_directory / f"{digest}.sqlite")


def graph_config(graph_run_id: str, max_concurrency: int) -> dict[str, object]:
    """Build LangGraph runtime configuration for one graph run."""
    if not isinstance(graph_run_id, str):
        raise TypeError("graph_run_id must be a string")
    if not graph_run_id.strip():
        raise ValueError("graph_run_id must not be blank")
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int):
        raise TypeError("max_concurrency must be an integer")
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least one")
    return {
        "configurable": {"thread_id": graph_run_id},
        "max_concurrency": max_concurrency,
    }
