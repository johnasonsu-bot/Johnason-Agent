"""Fail-closed routing and durable Effect lifecycle for Python Term tools."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from pydantic import ValidationError as PydanticValidationError

from workbench.runtime.engine_host.v2 import ToolManifestEntryV2

from .contracts import (
    PublicToolResult,
    StepContext,
    ToolEffectRecord,
    canonical_digest,
    validate_safe_json,
)
from .repository import PythonTermRepository


@dataclass(frozen=True, slots=True)
class FileAccess:
    argument: str
    mode: Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class ToolAccess:
    files: tuple[FileAccess, ...] = ()
    network: bool = False
    command: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedFileAccess:
    path: str
    mode: Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class _ResolvedAccess:
    files: tuple[_ResolvedFileAccess, ...]
    network: bool
    command: bool

    @property
    def has_external_effect(self) -> bool:
        return (
            any(item.mode == "write" for item in self.files)
            or self.network
            or self.command
        )


class HmacRequestDigestService:
    __slots__ = ("__key",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("request digest key must contain at least 32 bytes")
        self.__key = key

    def digest(self, value: object) -> str:
        from .contracts import canonical_json

        return hmac.new(
            self.__key,
            canonical_json(value).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def __repr__(self) -> str:
        return "HmacRequestDigestService(<opaque>)"


@dataclass(frozen=True, slots=True)
class ExecutorRegistration:
    tool_id: str
    version: str
    schema_digest: str
    capability_digest: str
    executor_handle: str
    access: ToolAccess

    def __post_init__(self) -> None:
        identifier = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
        values = (self.tool_id, self.version, self.executor_handle)
        if any(
            not isinstance(value, str) or not identifier.fullmatch(value)
            for value in values
        ):
            raise ValueError("executor registration identifiers must be opaque")
        if type(self.access) is not ToolAccess:
            raise TypeError("executor registration access must be ToolAccess")
        if any(type(item) is not FileAccess for item in self.access.files):
            raise TypeError("executor registration file access must be FileAccess")
        if len(self.access.files) != len(
            {(item.argument, item.mode) for item in self.access.files}
        ):
            raise ValueError("executor registration access contains duplicates")
        if any(not identifier.fullmatch(item.argument) for item in self.access.files):
            raise ValueError("executor file access argument must be opaque")

    @classmethod
    def from_manifest(
        cls,
        manifest: ToolManifestEntryV2,
        *,
        executor_handle: str,
        access: ToolAccess,
    ) -> "ExecutorRegistration":
        schema_digest = canonical_digest(manifest.schema)
        capability_digest = canonical_digest(
            {
                "tool_id": manifest.tool_id,
                "version": manifest.version,
                "schema_digest": schema_digest,
                "executor_handle": executor_handle,
                "access": {
                    "files": [
                        {"argument": item.argument, "mode": item.mode}
                        for item in access.files
                    ],
                    "network": access.network,
                    "command": access.command,
                },
            }
        )
        return cls(
            tool_id=manifest.tool_id,
            version=manifest.version,
            schema_digest=schema_digest,
            capability_digest=capability_digest,
            executor_handle=executor_handle,
            access=access,
        )

    def matches(self, manifest: ToolManifestEntryV2) -> bool:
        expected = type(self).from_manifest(
            manifest,
            executor_handle=self.executor_handle,
            access=self.access,
        )
        return expected == self


@dataclass(frozen=True, slots=True)
class _AdmittedTool:
    manifest: ToolManifestEntryV2
    registration: ExecutorRegistration
    validator: object


@dataclass(frozen=True, slots=True)
class SdkToolWrapper:
    context: StepContext
    manifest: ToolManifestEntryV2
    registration: ExecutorRegistration

    @property
    def tool_id(self) -> str:
        return self.manifest.tool_id


class ToolRouteError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, effect_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.effect_id = effect_id


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    tool_id: str
    tool_call_id: str
    context_identity_digest: str
    request_digest: str
    access_digest: str
    decision_digest: str
    reasons: tuple[Literal["tool", "filesystem", "network", "command"], ...]


@dataclass(frozen=True, slots=True)
class _AuthorizationDecision:
    reasons: tuple[Literal["tool", "filesystem", "network", "command"], ...]
    access_digest: str
    decision_digest: str


ApprovalCallback = Callable[[ApprovalRequest], bool | Awaitable[bool]]


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


_MAX_SCHEMA_BYTES = 16_384
_MAX_SCHEMA_DEPTH = 16
_MAX_SCHEMA_NODES = 256
_MAX_SCHEMA_CONTAINER_ITEMS = 128
_MAX_PATTERN_LENGTH = 256
_MAX_ARGUMENT_BYTES = 65_536
_LEASE_GRACE_MS = 1_000
_WAIT_POLL_SECONDS = 0.01


def _safe_pattern(pattern: str) -> bool:
    if len(pattern) > _MAX_PATTERN_LENGTH or "(?" in pattern:
        return False
    if re.search(r"\\[1-9]", pattern):
        return False
    if re.search(r"\([^)]*[+*{][^)]*\)[+*{]", pattern):
        return False
    if re.search(r"\([^)]*\|[^)]*\)[+*{]", pattern):
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def _resolve_local_ref(schema: object, reference: str) -> bool:
    if reference == "#":
        return True
    if not reference.startswith("#/") or len(reference) > 512:
        return False
    target = schema
    for raw_segment in reference[2:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(target, Mapping) and segment in target:
            target = target[segment]
        elif isinstance(target, (list, tuple)) and segment.isdecimal():
            index = int(segment)
            if index >= len(target):
                return False
            target = target[index]
        else:
            return False
    return True


def _schema_is_bounded(schema: object) -> bool:
    try:
        encoded = json.dumps(
            schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        return False
    if len(encoded) > _MAX_SCHEMA_BYTES:
        return False
    nodes = 0
    stack: list[tuple[object, int]] = [(schema, 0)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_SCHEMA_NODES or depth > _MAX_SCHEMA_DEPTH:
            return False
        if isinstance(value, Mapping):
            if len(value) > _MAX_SCHEMA_CONTAINER_ITEMS:
                return False
            for key, nested in value.items():
                if key in {"$id", "$anchor", "$dynamicAnchor", "$recursiveAnchor"}:
                    return False
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    if not isinstance(nested, str) or not _resolve_local_ref(
                        schema, nested
                    ):
                        return False
                if key == "pattern" and (
                    not isinstance(nested, str) or not _safe_pattern(nested)
                ):
                    return False
                if key == "patternProperties":
                    if not isinstance(nested, Mapping) or any(
                        not isinstance(pattern, str) or not _safe_pattern(pattern)
                        for pattern in nested
                    ):
                        return False
                stack.append((nested, depth + 1))
        elif isinstance(value, (list, tuple)):
            if len(value) > _MAX_SCHEMA_CONTAINER_ITEMS:
                return False
            stack.extend((item, depth + 1) for item in value)
    return True


class ToolRouter:
    def __init__(
        self,
        repository: PythonTermRepository,
        executors: Mapping[str, object],
        *,
        executor_broker: object | None = None,
        request_digests: HmacRequestDigestService | None = None,
        clock_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        invalid_registration = any(
            type(registration) is not ExecutorRegistration
            or key != registration.tool_id
            for key, registration in executors.items()
        )
        invalid_broker = executor_broker is None or not callable(
            getattr(executor_broker, "execute", None)
        )
        invalid_digest_service = type(request_digests) is not HmacRequestDigestService
        if invalid_registration or invalid_broker or invalid_digest_service:
            raise ToolRouteError(
                "registration_rejected",
                "Tool executor registration was rejected",
            )
        self.repository = repository
        self.registrations = cast(
            dict[str, ExecutorRegistration], dict(executors)
        )
        self.executor_broker = executor_broker
        self.request_digests = cast(HmacRequestDigestService, request_digests)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._admissions: dict[str, dict[str, _AdmittedTool]] = {}

    def admit(self, context: StepContext) -> None:
        admitted: dict[str, _AdmittedTool] = {}
        seen: set[str] = set()
        failed_code: str | None = None
        for manifest in context.tool_manifest:
            if manifest.tool_id in seen:
                failed_code = "manifest_conflict"
                break
            seen.add(manifest.tool_id)
            schema = manifest.model_dump(mode="json")["schema"]
            validator: object | None = None
            compile_failed = False
            try:
                if not _schema_is_bounded(schema):
                    raise ValueError("unsafe schema")
                validator_type = validator_for(schema)
                validator_type.check_schema(schema)
                validator = validator_type(
                    schema,
                    format_checker=FormatChecker(),
                )
            except Exception:
                compile_failed = True
            if compile_failed or validator is None:
                failed_code = "schema_rejected"
                break
            registration = self.registrations.get(manifest.tool_id)
            if registration is None:
                continue
            if not registration.matches(manifest):
                failed_code = "registration_rejected"
                break
            admitted[manifest.tool_id] = _AdmittedTool(
                manifest=manifest,
                registration=registration,
                validator=validator,
            )
        if failed_code is not None:
            raise ToolRouteError(
                failed_code,
                "Frozen Tool Manifest admission was rejected",
            ) from None
        self._admissions[context.tool_manifest_digest] = admitted

    def exposed_tools(self, context: StepContext) -> tuple[SdkToolWrapper, ...]:
        admission = self._admissions.get(context.tool_manifest_digest)
        if admission is None:
            raise ToolRouteError(
                "manifest_not_admitted", "Frozen Tool Manifest was not admitted"
            )
        wrappers: list[SdkToolWrapper] = []
        for manifest in context.tool_manifest:
            admitted = admission.get(manifest.tool_id)
            if admitted is None:
                continue
            wrappers.append(
                SdkToolWrapper(
                    context=context,
                    manifest=manifest,
                    registration=admitted.registration,
                )
            )
        return tuple(wrappers)

    async def invoke(
        self,
        context: StepContext,
        tool_id: str,
        arguments: Mapping[str, object],
        *,
        tool_call_id: str,
        approval: ApprovalCallback | None = None,
    ) -> PublicToolResult:
        started_ms = self.monotonic_ms()
        deadline_at_ms = started_ms + context.deadline_ms
        admitted = self._admitted_tool(context, tool_id)
        normalized = self._validate_admitted_arguments(admitted, arguments)
        access = self._resolve_access(admitted.registration, normalized)
        write_effect = self._classify_effect(context, admitted.manifest, access)
        request_payload = {
            "arguments": normalized,
            "context_identity_digest": context.identity_digest,
            "manifest_digest": context.tool_manifest_digest,
            "registration_digest": admitted.registration.capability_digest,
            "tool_call_id": tool_call_id,
            "access": self._access_payload(access),
            "write_effect": write_effect,
        }
        request_digest = self.request_digests.digest(
            {"domain": "tool-request-v1", "payload": request_payload}
        )
        access_digest = self.request_digests.digest(
            {"domain": "tool-access-v1", "access": self._access_payload(access)}
        )
        decision = self._authorize_frozen(
            context,
            admitted.manifest,
            access,
            access_digest=access_digest,
            request_digest=request_digest,
            deadline_at_ms=deadline_at_ms,
        )
        if decision.reasons:
            await self._approve_bounded(
                context,
                tool_id,
                tool_call_id,
                request_digest,
                decision,
                approval,
                deadline_at_ms=deadline_at_ms,
            )
            rechecked = self._authorize_frozen(
                context,
                admitted.manifest,
                access,
                access_digest=access_digest,
                request_digest=request_digest,
                deadline_at_ms=deadline_at_ms,
            )
            if rechecked.decision_digest != decision.decision_digest:
                raise ToolRouteError(
                    "authorization_changed",
                    "Tool authorization changed while awaiting approval",
                )

        owner_id = "owner-" + secrets.token_hex(16)
        effect_id = "effect-" + self.request_digests.digest(
            {
                "domain": "tool-effect-v1",
                "scope_id": context.effect_scope.scope_id,
                "term_id": context.term_id,
                "step_id": context.step_id,
                "tool_call_id": tool_call_id,
            }
        )
        reservation = ToolEffectRecord(
            effect_id=effect_id,
            term_id=context.term_id,
            step_id=context.step_id,
            tool_call_id=tool_call_id,
            request_digest=request_digest,
            write_effect=write_effect,
            execution_owner_id=owner_id,
            lease_expires_at_ms=(
                self.clock_ms() + admitted.manifest.timeout_ms + _LEASE_GRACE_MS
            ),
            status="reserved",
        )
        owned, replay = await self._reserve_or_replay(
            admitted.manifest,
            reservation,
            deadline_at_ms=deadline_at_ms,
        )
        if replay is not None:
            return replay
        if owned is None:
            raise ToolRouteError(
                "effect_unavailable", "Tool Effect reservation is unavailable",
                effect_id=effect_id,
            )
        reservation = owned

        remaining_ms = min(
            admitted.manifest.timeout_ms,
            self._remaining_ms(deadline_at_ms),
        )
        if remaining_ms <= 0:
            await self._finish_failed_execution(
                reservation, write_effect=write_effect, code="deadline_exceeded"
            )
        outcome, raw_result = await self._execute_bounded(
            admitted.registration,
            context,
            normalized,
            timeout_ms=remaining_ms,
        )
        if outcome == "cancelled":
            terminal = self._terminal_effect(
                reservation,
                status=("reconciliation_required" if write_effect else "rejected"),
                code="cancelled",
            )
            await self._persist_terminal(terminal)
            raise asyncio.CancelledError()
        if outcome != "completed":
            await self._finish_failed_execution(
                reservation, write_effect=write_effect, code=outcome
            )

        result: PublicToolResult | None = None
        result_invalid = False
        try:
            candidate = PublicToolResult.model_validate(raw_result)
            if candidate.status != "completed":
                raise ValueError("non-completed result")
            result = candidate
        except (PydanticValidationError, TypeError, ValueError):
            result_invalid = True
        if result_invalid or result is None:
            terminal = self._terminal_effect(
                reservation,
                status=("reconciliation_required" if write_effect else "rejected"),
                code="result_rejected",
            )
            await self._persist_terminal(terminal)
            raise ToolRouteError(
                "reconciliation_required" if write_effect else "result_rejected",
                "Tool result failed the public result boundary",
                effect_id=effect_id,
            ) from None

        committed = reservation.model_copy(
            update={
                "status": "committed",
                "execution_owner_id": None,
                "lease_expires_at_ms": None,
                "result_digest": canonical_digest(result),
                "public_result": result,
            }
        )
        persisted = await self._persist_terminal(committed)
        if persisted:
            return result
        durable = self._safe_get_effect(effect_id)
        if (
            durable is not None
            and durable.status == "committed"
            and durable.result_digest == canonical_digest(result)
            and durable.public_result == result
        ):
            return result
        if write_effect:
            reconciliation = self._terminal_effect(
                reservation,
                status="reconciliation_required",
                code="commit_persistence_failed",
            )
            await self._persist_terminal(reconciliation)
            raise ToolRouteError(
                "reconciliation_required",
                "Tool Effect requires reconciliation",
                effect_id=effect_id,
            ) from None
        raise ToolRouteError(
            "persistence_unavailable",
            "Tool result persistence is unavailable",
            effect_id=effect_id,
        ) from None

    def _admitted_tool(self, context: StepContext, tool_id: str) -> _AdmittedTool:
        admission = self._admissions.get(context.tool_manifest_digest)
        if admission is None:
            raise ToolRouteError(
                "manifest_not_admitted", "Frozen Tool Manifest was not admitted"
            )
        matches = tuple(
            item for item in context.tool_manifest if item.tool_id == tool_id
        )
        if len(matches) != 1:
            raise ToolRouteError(
                "tool_not_manifested",
                "Tool is not uniquely present in the frozen manifest",
            )
        admitted = admission.get(tool_id)
        if admitted is None:
            raise ToolRouteError("tool_unavailable", "Tool has no admitted executor")
        return admitted

    @staticmethod
    def _validate_admitted_arguments(
        admitted: _AdmittedTool, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        normalized: object | None = None
        failed = False
        try:
            normalized = validate_safe_json(arguments)
            if not isinstance(normalized, dict):
                raise ValueError("Tool arguments must be an object")
            if len(
                json.dumps(
                    normalized,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ) > _MAX_ARGUMENT_BYTES:
                raise ValueError("Tool arguments exceed the bounded input")
            cast(object, admitted.validator).validate(  # type: ignore[attr-defined]
                normalized
            )
        except Exception:
            failed = True
        if failed or not isinstance(normalized, dict):
            raise ToolRouteError(
                "schema_rejected", "Tool arguments failed schema validation"
            ) from None
        return cast(Mapping[str, object], _freeze(normalized))

    @staticmethod
    def _resolve_access(
        registration: ExecutorRegistration,
        arguments: Mapping[str, object],
    ) -> _ResolvedAccess:
        files: list[_ResolvedFileAccess] = []
        failed = False
        for rule in registration.access.files:
            value = arguments.get(rule.argument)
            if not isinstance(value, str):
                failed = True
                break
            files.append(_ResolvedFileAccess(path=value, mode=rule.mode))
        if failed:
            raise ToolRouteError(
                "access_rejected", "Tool access requirements are invalid"
            ) from None
        return _ResolvedAccess(
            files=tuple(files),
            network=registration.access.network,
            command=registration.access.command,
        )

    @staticmethod
    def _classify_effect(
        context: StepContext,
        manifest: ToolManifestEntryV2,
        access: _ResolvedAccess,
    ) -> bool:
        if manifest.read_only and access.has_external_effect:
            raise ToolRouteError(
                "manifest_effect_mismatch",
                "Read-only Tool registration declares an external Effect",
            )
        write_effect = not manifest.read_only or access.has_external_effect
        scope = context.effect_scope
        if scope.allowed_tool_ids and manifest.tool_id not in scope.allowed_tool_ids:
            raise ToolRouteError("effect_scope_denied", "Tool is outside Effect scope")
        if write_effect and not scope.write_effects:
            raise ToolRouteError("effect_scope_denied", "Write Effect is outside scope")
        return write_effect

    @staticmethod
    def _access_payload(access: _ResolvedAccess) -> Mapping[str, object]:
        return {
            "files": tuple(
                {"mode": item.mode, "path": item.path} for item in access.files
            ),
            "network": access.network,
            "command": access.command,
        }

    def _remaining_ms(self, deadline_at_ms: int) -> int:
        return deadline_at_ms - self.monotonic_ms()

    def _authorize_frozen(
        self,
        context: StepContext,
        manifest: ToolManifestEntryV2,
        access: _ResolvedAccess,
        *,
        access_digest: str,
        request_digest: str,
        deadline_at_ms: int,
    ) -> _AuthorizationDecision:
        if self._remaining_ms(deadline_at_ms) <= 0:
            raise ToolRouteError("deadline_exceeded", "Tool Step deadline elapsed")
        if self.clock_ms() >= context.workspace_grant.expires_at_ms:
            raise ToolRouteError("workspace_expired", "Workspace Grant has expired")
        reasons: list[Literal["tool", "filesystem", "network", "command"]] = []
        self._collect_policy(
            context.permission_policy.tool_policy, "tool", reasons,
            denied_code="permission_denied",
        )
        if access.files:
            self._collect_policy(
                context.permission_policy.filesystem_policy,
                "filesystem",
                reasons,
                denied_code="permission_denied",
            )
            for item in access.files:
                roots = (
                    context.workspace_grant.readable_paths
                    if item.mode == "read"
                    else context.workspace_grant.writable_paths
                )
                if not self._path_is_granted(item.path, roots):
                    raise ToolRouteError(
                        "workspace_denied", "Tool path is outside Workspace Grant"
                    )
        if access.network:
            self._collect_policy(
                context.workspace_grant.network_policy,
                "network",
                reasons,
                denied_code="network_denied",
            )
        if access.command:
            self._collect_policy(
                context.workspace_grant.command_policy,
                "command",
                reasons,
                denied_code="command_denied",
            )
        reason_tuple = tuple(reasons)
        decision_digest = self.request_digests.digest(
            {
                "domain": "tool-authorization-v1",
                "request_digest": request_digest,
                "access_digest": access_digest,
                "context_identity_digest": context.identity_digest,
                "permission_policy_digest": context.permission_policy_digest,
                "workspace_grant_digest": context.workspace_grant_digest,
                "manifest_version": manifest.version,
                "reasons": reason_tuple,
            }
        )
        return _AuthorizationDecision(
            reasons=reason_tuple,
            access_digest=access_digest,
            decision_digest=decision_digest,
        )

    @staticmethod
    def _collect_policy(
        policy: str,
        reason: Literal["tool", "filesystem", "network", "command"],
        reasons: list[Literal["tool", "filesystem", "network", "command"]],
        *,
        denied_code: str,
    ) -> None:
        if policy == "deny":
            raise ToolRouteError(denied_code, "Tool permission was denied")
        if policy in {"ask", "supervisor_approval"}:
            reasons.append(reason)

    @staticmethod
    def _path_is_granted(path: str, roots: tuple[str, ...]) -> bool:
        candidate = Path(path)
        if not candidate.is_absolute() or ".." in candidate.parts:
            return False
        canonical = candidate.resolve(strict=False)
        if str(canonical) != path:
            return False
        return any(
            canonical == root or canonical.is_relative_to(root)
            for root in (Path(value).resolve(strict=False) for value in roots)
        )

    async def _approve_bounded(
        self,
        context: StepContext,
        tool_id: str,
        tool_call_id: str,
        request_digest: str,
        decision: _AuthorizationDecision,
        approval: ApprovalCallback | None,
        *,
        deadline_at_ms: int,
    ) -> None:
        if approval is None:
            raise ToolRouteError(
                "approval_required", "Tool execution requires approval"
            )
        request = ApprovalRequest(
            tool_id=tool_id,
            tool_call_id=tool_call_id,
            context_identity_digest=context.identity_digest,
            request_digest=request_digest,
            access_digest=decision.access_digest,
            decision_digest=decision.decision_digest,
            reasons=decision.reasons,
        )
        if inspect.iscoroutinefunction(approval):
            operation = cast(Callable[[ApprovalRequest], Awaitable[bool]], approval)(
                request
            )
        else:
            operation = asyncio.to_thread(approval, request)
        outcome, value = await self._await_operation(
            operation,
            timeout_ms=self._remaining_ms(deadline_at_ms),
        )
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        if outcome == "timeout":
            raise ToolRouteError("deadline_exceeded", "Tool approval deadline elapsed")
        if outcome != "completed" or value is not True:
            raise ToolRouteError(
                "approval_denied", "Tool approval was denied"
            ) from None

    async def _execute_bounded(
        self,
        registration: ExecutorRegistration,
        context: StepContext,
        arguments: Mapping[str, object],
        *,
        timeout_ms: int,
    ) -> tuple[str, object | None]:
        operation: object | None = None
        failed = False
        cancelled = False
        try:
            operation = self.executor_broker.execute(  # type: ignore[union-attr]
                registration.executor_handle,
                context,
                arguments,
            )
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            failed = True
        if cancelled:
            return "cancelled", None
        if failed or not inspect.isawaitable(operation):
            return "execution_failed", None
        return await self._await_operation(operation, timeout_ms=timeout_ms)

    @staticmethod
    async def _await_operation(
        operation: Awaitable[object], *, timeout_ms: int
    ) -> tuple[str, object | None]:
        if timeout_ms <= 0:
            return "timeout", None
        task = asyncio.ensure_future(operation)
        timer = asyncio.create_task(asyncio.sleep(timeout_ms / 1000))
        cancelled = False
        try:
            done, _ = await asyncio.wait(
                {task, timer}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            cancelled = True
            done = set()
        if cancelled:
            task.cancel()
            timer.cancel()
            task.add_done_callback(ToolRouter._consume_task)
            return "cancelled", None
        if timer in done:
            task.cancel()
            task.add_done_callback(ToolRouter._consume_task)
            return "timeout", None
        timer.cancel()
        failed = False
        value: object | None = None
        try:
            value = task.result()
        except asyncio.CancelledError:
            failed = True
        except Exception:
            failed = True
        if failed:
            return "execution_failed", None
        return "completed", value

    @staticmethod
    def _consume_task(task: asyncio.Future[object]) -> None:
        try:
            task.exception()
        except BaseException:
            pass

    async def _reserve_or_replay(
        self,
        manifest: ToolManifestEntryV2,
        reservation: ToolEffectRecord,
        *,
        deadline_at_ms: int,
    ) -> tuple[ToolEffectRecord | None, PublicToolResult | None]:
        effect, created = self.repository.reserve_tool_effect(reservation)
        if created:
            return reservation, None
        while True:
            replay = self._terminal_replay(manifest, effect, reservation.write_effect)
            if replay is not None:
                return None, replay
            if effect.status != "reserved":
                self._raise_terminal(effect)
            if effect.execution_owner_id == reservation.execution_owner_id:
                return effect, None
            now_ms = self.clock_ms()
            if (
                effect.lease_expires_at_ms is not None
                and effect.lease_expires_at_ms > now_ms
            ):
                if self._remaining_ms(deadline_at_ms) <= 0:
                    raise ToolRouteError(
                        "deadline_exceeded", "Tool Step deadline elapsed",
                        effect_id=effect.effect_id,
                    )
                await asyncio.sleep(
                    min(_WAIT_POLL_SECONDS, self._remaining_ms(deadline_at_ms) / 1000)
                )
                refreshed = self._safe_get_effect(effect.effect_id)
                if refreshed is None:
                    raise ToolRouteError(
                        "effect_unavailable", "Tool Effect disappeared",
                        effect_id=effect.effect_id,
                    )
                effect = refreshed
                continue
            if reservation.write_effect:
                terminal = self._terminal_effect(
                    effect,
                    status="reconciliation_required",
                    code="write_outcome_unknown",
                )
                if await self._persist_terminal(terminal):
                    raise ToolRouteError(
                        "reconciliation_required",
                        "Tool Effect requires reconciliation",
                        effect_id=effect.effect_id,
                    )
                refreshed = self._safe_get_effect(effect.effect_id)
                if refreshed is not None and refreshed != effect:
                    effect = refreshed
                    continue
                raise ToolRouteError(
                    "persistence_unavailable", "Tool Effect persistence is unavailable",
                    effect_id=effect.effect_id,
                )
            if manifest.idempotency != "idempotent":
                terminal = self._terminal_effect(
                    effect, status="rejected", code="replay_not_allowed"
                )
                await self._persist_terminal(terminal)
                raise ToolRouteError(
                    "replay_not_allowed",
                    "Manifest does not permit replay of this read Tool",
                    effect_id=effect.effect_id,
                )
            replacement = reservation.model_copy(
                update={
                    "lease_expires_at_ms": (
                        now_ms + manifest.timeout_ms + _LEASE_GRACE_MS
                    )
                }
            )
            effect, won = self.repository.takeover_expired_tool_effect(
                replacement,
                expected_owner_id=effect.execution_owner_id,
                now_ms=now_ms,
            )
            if won:
                return replacement, None

    @staticmethod
    def _terminal_replay(
        manifest: ToolManifestEntryV2,
        effect: ToolEffectRecord,
        write_effect: bool,
    ) -> PublicToolResult | None:
        if effect.write_effect != write_effect:
            raise ToolRouteError(
                "effect_conflict", "Tool Effect classification changed",
                effect_id=effect.effect_id,
            )
        if effect.status != "committed":
            return None
        if manifest.read_only and manifest.idempotency != "idempotent":
            raise ToolRouteError(
                "replay_not_allowed",
                "Manifest does not permit replay of this read Tool",
                effect_id=effect.effect_id,
            )
        if effect.public_result is None or effect.result_digest != canonical_digest(
            effect.public_result
        ):
            raise ToolRouteError(
                "effect_corrupt", "Committed Tool Effect has no authoritative result",
                effect_id=effect.effect_id,
            )
        return effect.public_result

    @staticmethod
    def _raise_terminal(effect: ToolEffectRecord) -> None:
        if effect.status == "reconciliation_required":
            raise ToolRouteError(
                "reconciliation_required", "Tool Effect requires reconciliation",
                effect_id=effect.effect_id,
            )
        if effect.status == "rejected":
            raise ToolRouteError(
                "effect_rejected", "Tool Effect was already rejected",
                effect_id=effect.effect_id,
            )

    async def _finish_failed_execution(
        self,
        reservation: ToolEffectRecord,
        *,
        write_effect: bool,
        code: str,
    ) -> None:
        terminal = self._terminal_effect(
            reservation,
            status=("reconciliation_required" if write_effect else "rejected"),
            code=code,
        )
        await self._persist_terminal(terminal)
        raise ToolRouteError(
            "reconciliation_required" if write_effect else code,
            "Tool execution did not produce a reusable result",
            effect_id=reservation.effect_id,
        ) from None

    async def _persist_terminal(self, effect: ToolEffectRecord) -> bool:
        async def persist() -> bool:
            failed = False
            try:
                self.repository.save_tool_effect(effect)
            except Exception:
                failed = True
            return not failed

        task = asyncio.create_task(persist())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            try:
                return await asyncio.shield(task)
            except (asyncio.CancelledError, Exception):
                return False

    def _safe_get_effect(self, effect_id: str) -> ToolEffectRecord | None:
        failed = False
        effect: ToolEffectRecord | None = None
        try:
            effect = self.repository.get_tool_effect(effect_id)
        except Exception:
            failed = True
        return None if failed else effect

    @staticmethod
    def _terminal_effect(
        effect: ToolEffectRecord,
        *,
        status: Literal["rejected", "reconciliation_required"],
        code: str,
    ) -> ToolEffectRecord:
        result = PublicToolResult(
            status="failed",
            summary=(
                "Tool execution requires reconciliation"
                if status == "reconciliation_required"
                else "Tool call was rejected"
            ),
        )
        return effect.model_copy(
            update={
                "status": status,
                "execution_owner_id": None,
                "lease_expires_at_ms": None,
                "result_digest": canonical_digest({"code": code, "result": result}),
                "public_result": result,
            }
        )

__all__ = [
    "ExecutorRegistration",
    "FileAccess",
    "HmacRequestDigestService",
    "ApprovalRequest",
    "SdkToolWrapper",
    "ToolAccess",
    "ToolRouteError",
    "ToolRouter",
]
