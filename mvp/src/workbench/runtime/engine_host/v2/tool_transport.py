"""Bound tool callbacks on the existing Host pipe, not a tool implementation.

Executors own durable effects and workspace access. Cancellation is cooperative:
returning a result acknowledges settlement; raising during a write does not.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any, Literal

from jsonschema.validators import validator_for

from .contracts import RunEnvelopeV2, ToolManifestEntryV2

IDENTITY_FIELDS = (
    "session_id", "run_id", "term_id", "step_id", "command_id", "agent_id", "agent_role"
)
MAX_FRAME_BYTES = 1_048_576


class ToolTransportError(ValueError):
    """A request or result cannot be correlated with this query."""


class ToolWriteUncertain(ToolTransportError):
    """The executor did not confirm a write outcome; automatic replay is unsafe."""


@dataclass(frozen=True)
class PlatformToolResult:
    status: Literal["completed", "failed"]
    output: Any
    effect_id: str | None = None


@dataclass(frozen=True)
class PlatformToolCall:
    envelope: RunEnvelopeV2
    manifest: ToolManifestEntryV2
    tool_call_id: str
    arguments: dict[str, Any]
    cancelled: asyncio.Event


PlatformToolExecutor = Callable[[PlatformToolCall], Awaitable[PlatformToolResult]]


@dataclass
class _PendingCall:
    tool_call_id: str
    tool_id: str
    read_only: bool
    cancelled: asyncio.Event
    task: asyncio.Task[None] | None = None
    settled: bool = False


class PlatformToolBridge:
    """One frozen query's bidirectional tool requests with supervised lifetimes."""

    def __init__(
        self,
        envelope: RunEnvelopeV2,
        execute: PlatformToolExecutor,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        *,
        on_failure: Callable[[Exception], None] | None = None,
    ) -> None:
        self.envelope = envelope
        self._identity = {name: getattr(envelope, name) for name in IDENTITY_FIELDS}
        self._manifest = {item.tool_id: item for item in envelope.tool_manifest}
        self._execute = execute
        self._send = send
        self._on_failure = on_failure
        self._calls: dict[str, _PendingCall] = {}
        self._call_ids: set[str] = set()
        self._failures: list[Exception] = []
        self._closed = False
        self._cancelling = False

    @property
    def has_pending(self) -> bool:
        return any(not call.settled for call in self._calls.values())

    @property
    def has_pending_write(self) -> bool:
        return any(not call.settled and not call.read_only for call in self._calls.values())

    def handle(self, frame: Mapping[str, Any]) -> None:
        if self._closed:
            raise ToolTransportError("tool transport is closed")
        if set(frame) != {"kind", "type", "request_id", "payload"} or frame["kind"] != "request":
            raise ToolTransportError("invalid tool request frame")
        request_id = frame["request_id"]
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise ToolTransportError("invalid tool request id")
        payload = frame["payload"]
        command_type = frame["type"]
        fields = {"identity", "tool_call_id", "tool_id"}
        if command_type == "tool.execute":
            fields.add("arguments")
        elif command_type != "tool.cancel":
            raise ToolTransportError("unknown tool request type")
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ToolTransportError("invalid tool request payload")
        if payload["identity"] != self._identity:
            raise ToolTransportError("tool request identity does not match query")
        call_id, tool_id = payload["tool_call_id"], payload["tool_id"]
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
            raise ToolTransportError("invalid tool call id")
        if not isinstance(tool_id, str) or tool_id not in self._manifest:
            raise ToolTransportError("tool is not in frozen manifest")
        if command_type == "tool.cancel":
            pending = self._calls.get(request_id)
            if (pending is None or pending.tool_call_id != call_id
                    or pending.tool_id != tool_id or pending.settled):
                raise ToolTransportError("tool cancellation has no pending call")
            pending.cancelled.set()
            return
        if self._cancelling:
            raise ToolTransportError("tool query is cancelling")
        if request_id in self._calls or call_id in self._call_ids:
            raise ToolTransportError("tool request or call id was reused")
        if sum(not call.settled for call in self._calls.values()) >= 32:
            raise ToolTransportError("too many pending tool calls")
        arguments = payload["arguments"]
        if not isinstance(arguments, dict):
            raise ToolTransportError("tool arguments must be an object")
        manifest = self._manifest[tool_id]
        schema = manifest.model_dump(mode="json")["schema"]
        try:
            # JSON roundtrip detaches arguments from the caller before execution.
            arguments = json.loads(json.dumps(arguments, allow_nan=False))
            validator_for(schema)(schema).validate(arguments)
        except Exception as error:
            raise ToolTransportError("tool arguments do not match frozen schema") from error
        pending = _PendingCall(call_id, tool_id, manifest.read_only, asyncio.Event())
        self._calls[request_id] = pending
        self._call_ids.add(call_id)
        call = PlatformToolCall(self.envelope, manifest, call_id, arguments, pending.cancelled)
        pending.task = asyncio.create_task(self._run(request_id, pending, call))
        pending.task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            error: Exception = ToolTransportError("tool execution was abandoned")
        else:
            error = task.exception()
        if error is not None:
            self._failures.append(error)
            if self._on_failure is not None:
                self._on_failure(error)

    async def _run(self, request_id: str, pending: _PendingCall, call: PlatformToolCall) -> None:
        timer = asyncio.get_running_loop().call_later(
            call.manifest.timeout_ms / 1000, call.cancelled.set
        )
        try:
            try:
                result = await self._execute(call)
            except Exception as error:
                if not pending.read_only:
                    raise ToolWriteUncertain("platform write outcome is unknown") from error
                result = PlatformToolResult("failed", {"error": "tool_execution_failed"})
            if not isinstance(result, PlatformToolResult) or result.status not in {"completed", "failed"}:
                raise ToolTransportError("invalid platform tool result")
            if not pending.read_only and not result.effect_id:
                raise ToolWriteUncertain("platform write result has no effect confirmation")
            if result.effect_id is not None and (
                not isinstance(result.effect_id, str) or not result.effect_id.strip()
                or len(result.effect_id) > 256
            ):
                raise ToolTransportError("invalid platform effect id")
            frame = {
                "kind": "command", "type": "tool.result", "command_id": request_id,
                "payload": {
                    "identity": self._identity, "tool_call_id": call.tool_call_id,
                    "tool_id": pending.tool_id, "status": result.status,
                    "output": result.output, "effect_id": result.effect_id,
                },
            }
            try:
                encoded = json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf8")
                if len(encoded) + 1 > MAX_FRAME_BYTES:
                    raise ValueError("result exceeds frame")
            except (TypeError, ValueError, RecursionError) as error:
                raise ToolTransportError("platform tool result is not bounded JSON") from error
            await self._send(frame)
            pending.settled = True
        finally:
            timer.cancel()

    def cancel_all(self) -> None:
        self._cancelling = True
        for pending in self._calls.values():
            if not pending.settled:
                pending.cancelled.set()

    async def drain(self) -> None:
        tasks = [call.task for call in self._calls.values() if call.task is not None]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def aclose(self) -> None:
        self.cancel_all()
        try:
            await self.drain()
        finally:
            self._closed = True
