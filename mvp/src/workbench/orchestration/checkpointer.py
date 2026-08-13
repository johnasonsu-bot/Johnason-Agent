from __future__ import annotations

import os

os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

import sqlite3
from pathlib import Path
from types import TracebackType

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver


class _ManagedSqliteSaver(SqliteSaver):
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
    connection = sqlite3.connect(path, check_same_thread=False)
    serializer = JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )
    return _ManagedSqliteSaver(connection, serde=serializer)


def graph_config(graph_run_id: str, max_concurrency: int) -> dict[str, object]:
    """Build LangGraph runtime configuration for one graph run."""
    if not graph_run_id.strip():
        raise ValueError("graph_run_id must not be blank")
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least one")
    return {
        "configurable": {"thread_id": graph_run_id},
        "max_concurrency": max_concurrency,
    }
