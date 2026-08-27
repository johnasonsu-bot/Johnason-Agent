"""Narrow, provenance-preserving boundary to the pinned OpenAI Agents SDK."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from agents import (
    Agent,
    Handoff,
    Model,
    RunContextWrapper,
    Runner,
    Session,
    Tool,
    __version__ as AGENTS_SDK_VERSION,
)


PINNED_AGENTS_SDK_REVISION = "e773b15488c491d907d42756d91e470f280a3d7e"


class FrozenSnapshotMutationError(RuntimeError):
    """Raised when SDK Session persistence is attempted against a frozen Host snapshot."""


@dataclass(frozen=True, slots=True)
class AgentsSdkBuildMetadata:
    package: str
    revision: str
    sdk_version: str

    @property
    def build_id(self) -> str:
        return f"{self.package}@{self.sdk_version}+{self.revision}"


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError("FrozenSnapshotSession accepts normalized JSON messages only")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class FrozenSnapshotSession:
    """SDK Session seam backed only by an immutable, normalized Host message snapshot."""

    __slots__ = ("session_id", "_messages")

    session_settings = None

    def __init__(self, session_id: str, messages: Sequence[Mapping[str, object]]) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self.session_id = session_id
        self._messages = tuple(
            cast(Mapping[str, object], _freeze(message))
            for message in messages
        )

    async def get_items(self, limit: int | None = None) -> list[Any]:
        if limit is None:
            messages = self._messages
        elif limit <= 0:
            messages = ()
        else:
            messages = self._messages[-limit:]
        return [cast(Any, _thaw(message)) for message in messages]

    async def add_items(self, items: list[Any]) -> None:
        raise FrozenSnapshotMutationError("frozen snapshot sessions do not accept new items")

    async def pop_item(self) -> Any | None:
        raise FrozenSnapshotMutationError("frozen snapshot sessions do not remove items")

    async def clear_session(self) -> None:
        raise FrozenSnapshotMutationError("frozen snapshot sessions cannot be cleared")

    async def mutate(self, *args: object, **kwargs: object) -> None:
        raise FrozenSnapshotMutationError("frozen snapshot sessions cannot be mutated")


class AgentsSdkFacade:
    """The only Task-1 boundary that exposes Agents SDK execution primitives."""

    Runner = Runner
    RunContext = RunContextWrapper
    Agent = Agent
    Model = Model
    Tool = Tool
    Handoff = Handoff
    Session = Session
    build_metadata = AgentsSdkBuildMetadata(
        package="openai-agents-python",
        revision=PINNED_AGENTS_SDK_REVISION,
        sdk_version=AGENTS_SDK_VERSION,
    )

    async def run(self, agent: Any, input: Any, **kwargs: Any) -> Any:
        """Execute through the pinned SDK runner; this facade is not a Runtime implementation."""
        return await Runner.run(agent, input, **kwargs)
