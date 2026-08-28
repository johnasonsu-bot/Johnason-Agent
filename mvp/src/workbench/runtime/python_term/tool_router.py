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

from workbench.runtime.engine_host.v2 import ToolManifestEntryV2
from workbench.runtime.engine_host.v2.registry import (
    ExecutorAccessV2 as ToolAccess,
    ExecutorFileAccessV2 as FileAccess,
)

from .contracts import (
    PublicToolResult,
    StepContext,
    StepExecutionClaim,
    ToolEffectRecord,
    canonical_digest,
    canonical_json,
    validate_safe_json,
)
from .repository import PythonTermRepository


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


class ExecutorRegistration:
    """Opaque handle whose provenance is owned by the Host control plane."""

    __slots__ = (
        "__descriptor_id",
        "__tool_id",
        "__version",
        "__schema_digest",
        "__capability_digest",
        "__access",
        "__weakref__",
    )

    def __init__(self, *_: object, **__: object) -> None:
        raise ToolRouteError(
            "registration_rejected",
            "Executor registration requires the control-plane composition seam",
        ) from None

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Executor registration is immutable")

    @property
    def descriptor_id(self) -> str:
        return self.__descriptor_id

    @property
    def tool_id(self) -> str:
        return self.__tool_id

    @property
    def version(self) -> str:
        return self.__version

    @property
    def schema_digest(self) -> str:
        return self.__schema_digest

    @property
    def capability_digest(self) -> str:
        return self.__capability_digest

    @property
    def access(self) -> ToolAccess:
        return self.__access

    def __repr__(self) -> str:
        return "ExecutorRegistration(<opaque>)"


@dataclass(frozen=True, slots=True)
class _ExecutorBinding:
    manifest: ToolManifestEntryV2
    executor_handle: str
    access: ToolAccess
    capability_digest: str


@dataclass(frozen=True, slots=True)
class SupervisedExecutionSnapshot:
    execution_id: str
    effect_id: str
    state: Literal["running", "cancelling"]


@dataclass(slots=True)
class _ActiveExecution:
    execution_id: str
    effect_id: str
    task: asyncio.Future[object]
    state: Literal["running", "cancelling"] = "running"


def _registration_metadata(
    manifest: ToolManifestEntryV2,
    *,
    executor_handle: str,
    access: ToolAccess,
) -> tuple[str, str]:
    identifier = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
    if not isinstance(executor_handle, str) or not identifier.fullmatch(
        executor_handle
    ):
        raise ToolRouteError(
            "registration_rejected", "Executor handle must be opaque"
        ) from None
    if type(access) is not ToolAccess:
        raise ToolRouteError(
            "registration_rejected", "Executor access declaration was rejected"
        ) from None
    if len(access.files) != len(
        {(item.argument, item.mode) for item in access.files}
    ):
        raise ToolRouteError(
            "registration_rejected", "Executor access contains duplicates"
        ) from None
    schema_digest = canonical_digest(manifest.schema)
    payload = {
        "tool_id": manifest.tool_id,
        "version": manifest.version,
        "schema_digest": schema_digest,
        "executor_handle": executor_handle,
        "access": {
            "files": tuple(
                {"argument": item.argument, "mode": item.mode}
                for item in access.files
            ),
            "network": access.network,
            "command": access.command,
        },
    }
    return schema_digest, canonical_digest(payload)


class ExecutorBroker:
    __slots__ = ("__weakref__",)

    def __init__(self, *_: object, **__: object) -> None:
        raise ToolRouteError(
            "registration_rejected",
            "Executor registry requires the control-plane composition seam",
        ) from None

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Executor registry is immutable")

    def _runtime_state(self) -> tuple[dict[str, _ActiveExecution], int]:
        from workbench.runtime.engine_host.v2.python_term_control_plane import (
            _broker_runtime_state,
        )

        return _broker_runtime_state(self)

    def verifies(
        self,
        registration: ExecutorRegistration,
        manifest: ToolManifestEntryV2,
    ) -> bool:
        from workbench.runtime.engine_host.v2.python_term_control_plane import (
            _registry_verifies,
        )

        return _registry_verifies(self, registration, manifest)

    async def execute_bounded(
        self,
        registration: ExecutorRegistration,
        context: StepContext,
        arguments: Mapping[str, object],
        *,
        effect_id: str,
        timeout_ms: int,
    ) -> tuple[str, object | None]:
        manifest = next(
            (
                item
                for item in context.tool_manifest
                if item.tool_id == registration.tool_id
            ),
            None,
        )
        if manifest is None or not self.verifies(registration, manifest):
            raise ToolRouteError(
                "registration_rejected", "Executor capability verification failed"
            )
        active_executions, supervisor_capacity = self._runtime_state()
        self._retire_completed_executions()
        if len(active_executions) >= supervisor_capacity:
            return "execution_unavailable", None
        from workbench.runtime.engine_host.v2.python_term_control_plane import (
            _dispatch_registered_executor,
        )

        operation = _dispatch_registered_executor(
            self,
            registration,
            context,
            arguments,
        )
        if not inspect.isawaitable(operation):
            raise TypeError("Executor dispatcher must return an awaitable")
        task = asyncio.ensure_future(operation)
        execution_id = "execution-" + secrets.token_hex(16)
        active = _ActiveExecution(
            execution_id=execution_id,
            effect_id=effect_id,
            task=task,
        )
        active_executions[execution_id] = active
        task.add_done_callback(
            lambda completed, identifier=execution_id: self._retire_execution(
                identifier, completed
            )
        )
        if timeout_ms <= 0:
            await self._cancel_and_observe(active)
            return "timeout", None
        timer = asyncio.create_task(asyncio.sleep(timeout_ms / 1000))
        caller_cancelled = False
        try:
            done, _ = await asyncio.wait(
                {task, timer}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            caller_cancelled = True
            done = set()
        if caller_cancelled:
            timer.cancel()
            await self._cancel_and_observe(active)
            return "cancelled", None
        if timer in done:
            await self._cancel_and_observe(active)
            return "timeout", None
        timer.cancel()
        active_executions.pop(execution_id, None)
        try:
            return "completed", task.result()
        except asyncio.CancelledError:
            return "execution_failed", None
        except Exception:
            return "execution_failed", None

    @property
    def supervisor_capacity(self) -> int:
        return self._runtime_state()[1]

    def supervised_executions(self) -> tuple[SupervisedExecutionSnapshot, ...]:
        self._retire_completed_executions()
        active_executions, _ = self._runtime_state()
        return tuple(
            SupervisedExecutionSnapshot(
                execution_id=active.execution_id,
                effect_id=active.effect_id,
                state=active.state,
            )
            for active in sorted(
                active_executions.values(),
                key=lambda item: item.execution_id,
            )
        )

    async def wait_for_quiescence(self, *, timeout_ms: int) -> bool:
        if type(timeout_ms) is not int or timeout_ms < 0:
            raise ValueError("quiescence timeout must be a non-negative integer")
        active_executions, _ = self._runtime_state()
        self._retire_completed_executions()
        if not active_executions:
            return True
        tasks = {active.task for active in active_executions.values()}
        _, pending = await asyncio.wait(tasks, timeout=timeout_ms / 1000)
        self._retire_completed_executions()
        return not pending and not active_executions

    async def _cancel_and_observe(self, active: _ActiveExecution) -> None:
        active.state = "cancelling"
        active.task.cancel()

        async def observe_grace() -> None:
            await asyncio.wait({active.task}, timeout=0.025)

        observer = asyncio.create_task(observe_grace())
        while not observer.done():
            try:
                await asyncio.shield(observer)
            except asyncio.CancelledError:
                continue
        observer.result()
        self._retire_completed_executions()

    def _retire_completed_executions(self) -> None:
        active_executions, _ = self._runtime_state()
        for execution_id, active in tuple(active_executions.items()):
            if active.task.done():
                self._retire_execution(execution_id, active.task)

    def _retire_execution(
        self, execution_id: str, task: asyncio.Future[object]
    ) -> None:
        active_executions, _ = self._runtime_state()
        active = active_executions.get(execution_id)
        if active is not None and active.task is task:
            active_executions.pop(execution_id, None)
        try:
            task.exception()
        except BaseException:
            pass

    def __repr__(self) -> str:
        return "ExecutorBroker(<trusted-frozen>)"


@dataclass(frozen=True, slots=True)
class _AdmittedTool:
    manifest: ToolManifestEntryV2
    registration: ExecutorRegistration
    validator: object


@dataclass(frozen=True, slots=True)
class SdkToolWrapper:
    manifest: ToolManifestEntryV2
    registration: ExecutorRegistration
    step_claim: StepExecutionClaim

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
_MAX_LOCAL_REF_EDGES = 64
_MAX_LOCAL_REF_EXPANSION_DEPTH = 12
_MAX_LOCAL_REF_EXPANDED_NODES = 512
_LEASE_GRACE_MS = 25
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


def _resolve_local_ref(
    schema: object, reference: str
) -> tuple[tuple[str, ...], object] | None:
    if reference == "#":
        return (), schema
    if not reference.startswith("#/") or len(reference) > 512:
        return None
    target = schema
    pointer: list[str] = []
    for raw_segment in reference[2:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        pointer.append(segment)
        if isinstance(target, Mapping) and segment in target:
            target = target[segment]
        elif isinstance(target, (list, tuple)) and segment.isdecimal():
            index = int(segment)
            if index >= len(target):
                return None
            target = target[index]
        else:
            return None
    if not isinstance(target, Mapping):
        return None
    return tuple(pointer), target


def _local_ref_expansion_is_bounded(schema: object) -> bool:
    expanded_nodes = 0

    def visit(
        value: object,
        *,
        ref_depth: int,
        active_targets: frozenset[tuple[str, ...]],
    ) -> bool:
        nonlocal expanded_nodes
        expanded_nodes += 1
        if expanded_nodes > _MAX_LOCAL_REF_EXPANDED_NODES:
            return False
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if reference is not None:
                if not isinstance(reference, str):
                    return False
                resolved = _resolve_local_ref(schema, reference)
                if resolved is None:
                    return False
                target_pointer, target = resolved
                if (
                    target_pointer in active_targets
                    or ref_depth >= _MAX_LOCAL_REF_EXPANSION_DEPTH
                ):
                    return False
                target_is_bounded = visit(
                    target,
                    ref_depth=ref_depth + 1,
                    active_targets=active_targets | {target_pointer},
                )
                siblings_are_bounded = all(
                    visit(
                        nested,
                        ref_depth=ref_depth,
                        active_targets=active_targets,
                    )
                    for key, nested in value.items()
                    if key not in {"$ref", "$defs", "definitions"}
                )
                return target_is_bounded and siblings_are_bounded
            return all(
                visit(
                    nested,
                    ref_depth=ref_depth,
                    active_targets=active_targets,
                )
                for key, nested in value.items()
                if key not in {"$defs", "definitions"}
            )
        if isinstance(value, (list, tuple)):
            return all(
                visit(
                    item,
                    ref_depth=ref_depth,
                    active_targets=active_targets,
                )
                for item in value
            )
        return True

    return visit(schema, ref_depth=0, active_targets=frozenset())


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
    local_ref_edges = 0
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
                    if key != "$ref":
                        return False
                    local_ref_edges += 1
                    if (
                        local_ref_edges > _MAX_LOCAL_REF_EDGES
                        or not isinstance(nested, str)
                        or _resolve_local_ref(schema, nested) is None
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
    return _local_ref_expansion_is_bounded(schema)


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
        invalid_digest_service = type(request_digests) is not HmacRequestDigestService
        from workbench.runtime.engine_host.v2.python_term_control_plane import (
            _bind_tool_router_registry,
        )

        if invalid_digest_service or not _bind_tool_router_registry(
            self, executor_broker, executors
        ):
            raise ToolRouteError(
                "registration_rejected",
                "Tool executor registration was rejected",
            )
        self.repository = repository
        self.__registrations = cast(Mapping[str, ExecutorRegistration], executors)
        self.__executor_broker = cast(ExecutorBroker, executor_broker)
        self.request_digests = cast(HmacRequestDigestService, request_digests)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._admissions: dict[
            str, tuple[StepExecutionClaim, dict[str, _AdmittedTool]]
        ] = {}

    @property
    def executor_broker(self) -> None:
        """The dispatcher-bearing registry is intentionally not exposed."""
        raise AttributeError("Tool Router does not expose its Executor registry")

    def _validated_executor_broker(self) -> ExecutorBroker:
        from workbench.runtime.engine_host.v2.python_term_control_plane import (
            _tool_router_registry_valid,
        )

        if not _tool_router_registry_valid(
            self, self.__executor_broker, self.__registrations
        ):
            raise ToolRouteError(
                "registration_rejected",
                "Tool executor registry provenance was rejected",
            ) from None
        return self.__executor_broker

    def supervised_executions(self) -> tuple[SupervisedExecutionSnapshot, ...]:
        return self._validated_executor_broker().supervised_executions()

    @property
    def supervisor_capacity(self) -> int:
        return self._validated_executor_broker().supervisor_capacity

    async def wait_for_executor_quiescence(self, *, timeout_ms: int) -> bool:
        return await self._validated_executor_broker().wait_for_quiescence(
            timeout_ms=timeout_ms
        )

    def admit(
        self,
        context: StepContext,
        *,
        step_claim: StepExecutionClaim,
    ) -> None:
        if type(step_claim) is not StepExecutionClaim or (
            step_claim.term_id != context.term_id
            or step_claim.step_id != context.step_id
            or not self.repository.step_claim_is_current(step_claim)
        ):
            raise ToolRouteError(
                "step_claim_lost", "Tool Step execution claim was rejected"
            ) from None
        broker = self._validated_executor_broker()
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
            registration = self.__registrations.get(manifest.tool_id)
            if registration is None:
                continue
            if not broker.verifies(registration, manifest):
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
        self._admissions[context.identity_digest] = (step_claim, admitted)

    def exposed_tools(
        self,
        context: StepContext,
        *,
        step_claim: StepExecutionClaim | None = None,
    ) -> tuple[SdkToolWrapper, ...]:
        broker = self._validated_executor_broker()
        admission_entry = self._admissions.get(context.identity_digest)
        if admission_entry is None:
            raise ToolRouteError(
                "manifest_not_admitted", "Frozen Tool Manifest was not admitted"
            )
        admitted_claim, admission = admission_entry
        if step_claim is not None and step_claim != admitted_claim:
            raise ToolRouteError(
                "step_claim_lost", "Tool Step execution claim changed after admission"
            ) from None
        if not self.repository.step_claim_is_current(admitted_claim):
            raise ToolRouteError(
                "step_claim_lost", "Tool Step execution claim was rejected"
            ) from None
        wrappers: list[SdkToolWrapper] = []
        for manifest in context.tool_manifest:
            admitted = admission.get(manifest.tool_id)
            if admitted is None:
                continue
            if not broker.verifies(admitted.registration, manifest):
                raise ToolRouteError(
                    "registration_rejected",
                    "Executor capability provenance was rejected",
                ) from None
            wrappers.append(
                SdkToolWrapper(
                    manifest=manifest,
                    registration=admitted.registration,
                    step_claim=admitted_claim,
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
        step_claim: StepExecutionClaim | None = None,
    ) -> PublicToolResult:
        started_ms = self.monotonic_ms()
        deadline_at_ms = started_ms + context.deadline_ms
        admitted, bound_step_claim = self._admitted_tool(
            context, tool_id, step_claim=step_claim
        )
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
            record_version=2,
            effect_id=effect_id,
            effect_identity_version="hmac-sha256-v1",
            term_id=context.term_id,
            step_id=context.step_id,
            tool_call_id=tool_call_id,
            request_digest=request_digest,
            request_digest_version="hmac-sha256-v1",
            step_claim_digest=(
                bound_step_claim.identity_digest
            ),
            write_effect=write_effect,
            status="reserved",
        )
        owned, replay, waited_for_ownership = await self._reserve_or_replay(
            admitted.manifest,
            reservation,
            owner_id=owner_id,
            deadline_at_ms=deadline_at_ms,
            step_claim=bound_step_claim,
        )
        if replay is not None:
            return replay
        if owned is None:
            raise ToolRouteError(
                "effect_unavailable", "Tool Effect reservation is unavailable",
                effect_id=effect_id,
            )
        reservation = owned

        await self._reauthorize_owned_effect(
            context,
            admitted.manifest,
            access,
            request_digest=request_digest,
            access_digest=access_digest,
            decision=decision,
            approval=approval,
            tool_call_id=tool_call_id,
            deadline_at_ms=deadline_at_ms,
            reservation=reservation,
            reacquire_approval=waited_for_ownership,
            step_claim=bound_step_claim,
        )

        remaining_ms = min(
            admitted.manifest.timeout_ms,
            self._remaining_ms(deadline_at_ms),
        )
        if remaining_ms <= 0:
            await self._finish_failed_execution(
                reservation,
                write_effect=write_effect,
                code="deadline_exceeded",
                step_claim=bound_step_claim,
            )
        if not self.repository.step_claim_is_current(bound_step_claim):
            await self._finish_failed_execution(
                reservation,
                write_effect=write_effect,
                code="step_claim_lost",
                step_claim=bound_step_claim,
            )
        outcome, raw_result = await self._execute_bounded(
            admitted.registration,
            context,
            normalized,
            effect_id=reservation.effect_id,
            timeout_ms=remaining_ms,
        )
        if outcome == "cancelled":
            terminal = self._terminal_effect(
                reservation,
                status=("reconciliation_required" if write_effect else "rejected"),
                code="cancelled",
            )
            await self._persist_terminal_or_raise(
                terminal, reservation, step_claim=bound_step_claim
            )
            raise asyncio.CancelledError()
        if outcome != "completed":
            await self._finish_failed_execution(
                reservation,
                write_effect=write_effect,
                code=outcome,
                step_claim=bound_step_claim,
            )

        result: PublicToolResult | None = None
        result_invalid = False
        try:
            candidate = PublicToolResult.model_validate(raw_result)
            if candidate.status != "completed":
                raise ValueError("non-completed result")
            result = candidate
        except Exception:
            result_invalid = True
        if result_invalid or result is None:
            terminal = self._terminal_effect(
                reservation,
                status=("reconciliation_required" if write_effect else "rejected"),
                code="result_rejected",
            )
            await self._persist_terminal_or_raise(
                terminal, reservation, step_claim=bound_step_claim
            )
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
        persisted = await self._persist_terminal(
            committed, reservation, step_claim=bound_step_claim
        )
        if persisted is not None:
            return result
        durable = self._safe_get_effect(effect_id)
        if (
            durable is not None
            and durable.status == "committed"
            and durable.result_digest == canonical_digest(result)
            and durable.public_result == result
        ):
            return result
        if durable is not None and durable.status == "reconciliation_required":
            raise ToolRouteError(
                "reconciliation_required",
                "Tool Effect requires reconciliation",
                effect_id=effect_id,
            ) from None
        if durable is not None and durable.status == "rejected":
            raise ToolRouteError(
                "step_claim_lost",
                "Tool Step execution claim was lost",
                effect_id=effect_id,
            ) from None
        if write_effect:
            reconciliation = self._terminal_effect(
                reservation,
                status="reconciliation_required",
                code="commit_persistence_failed",
            )
            reconciled = await self._persist_terminal(
                reconciliation, reservation, step_claim=bound_step_claim
            )
            if reconciled is None:
                raise ToolRouteError(
                    "persistence_failure",
                    "Tool Effect terminal persistence failed",
                    effect_id=effect_id,
                ) from None
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

    def _admitted_tool(
        self,
        context: StepContext,
        tool_id: str,
        *,
        step_claim: StepExecutionClaim | None = None,
    ) -> tuple[_AdmittedTool, StepExecutionClaim]:
        broker = self._validated_executor_broker()
        admission_entry = self._admissions.get(context.identity_digest)
        if admission_entry is None:
            raise ToolRouteError(
                "manifest_not_admitted", "Frozen Tool Manifest was not admitted"
            )
        admitted_claim, admission = admission_entry
        if step_claim is not None and step_claim != admitted_claim:
            raise ToolRouteError(
                "step_claim_lost", "Tool Step execution claim changed after admission"
            ) from None
        if not self.repository.step_claim_is_current(admitted_claim):
            raise ToolRouteError(
                "step_claim_lost", "Tool Step execution claim was rejected"
            ) from None
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
        if not broker.verifies(admitted.registration, matches[0]):
            raise ToolRouteError(
                "registration_rejected",
                "Executor capability provenance was rejected",
            ) from None
        return admitted, admitted_claim

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
        granted = False
        failed = False
        try:
            candidate = Path(path)
            if candidate.is_absolute() and ".." not in candidate.parts:
                canonical = candidate.resolve(strict=False)
                if str(canonical) == path:
                    granted = any(
                        canonical == root or canonical.is_relative_to(root)
                        for root in (
                            Path(value).resolve(strict=False) for value in roots
                        )
                    )
        except Exception:
            failed = True
        return granted and not failed

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

    async def _reauthorize_owned_effect(
        self,
        context: StepContext,
        manifest: ToolManifestEntryV2,
        access: _ResolvedAccess,
        *,
        request_digest: str,
        access_digest: str,
        decision: _AuthorizationDecision,
        approval: ApprovalCallback | None,
        tool_call_id: str,
        deadline_at_ms: int,
        reservation: ToolEffectRecord,
        reacquire_approval: bool,
        step_claim: StepExecutionClaim | None,
    ) -> None:
        failure_code: str | None = None
        cancelled = False
        try:
            rechecked = self._authorize_frozen(
                context,
                manifest,
                access,
                access_digest=access_digest,
                request_digest=request_digest,
                deadline_at_ms=deadline_at_ms,
            )
            if rechecked.decision_digest != decision.decision_digest:
                raise ToolRouteError(
                    "authorization_changed",
                    "Tool authorization changed before execution",
                )
            if reacquire_approval and decision.reasons:
                await self._approve_bounded(
                    context,
                    manifest.tool_id,
                    tool_call_id,
                    request_digest,
                    rechecked,
                    approval,
                    deadline_at_ms=deadline_at_ms,
                )
                final = self._authorize_frozen(
                    context,
                    manifest,
                    access,
                    access_digest=access_digest,
                    request_digest=request_digest,
                    deadline_at_ms=deadline_at_ms,
                )
                if final.decision_digest != decision.decision_digest:
                    raise ToolRouteError(
                        "authorization_changed",
                        "Tool authorization changed after approval",
                    )
        except asyncio.CancelledError:
            cancelled = True
        except ToolRouteError as error:
            failure_code = error.code
        except Exception:
            failure_code = "authorization_failed"
        if not cancelled and failure_code is None:
            return
        rejected = self._terminal_effect(
            reservation,
            status="rejected",
            code="cancelled" if cancelled else failure_code or "authorization_failed",
        )
        await self._persist_terminal_or_raise(
            rejected, reservation, step_claim=step_claim
        )
        if cancelled:
            raise asyncio.CancelledError()
        raise ToolRouteError(
            failure_code or "authorization_failed",
            "Tool authorization failed before execution",
            effect_id=reservation.effect_id,
        ) from None

    async def _execute_bounded(
        self,
        registration: ExecutorRegistration,
        context: StepContext,
        arguments: Mapping[str, object],
        *,
        effect_id: str,
        timeout_ms: int,
    ) -> tuple[str, object | None]:
        outcome: tuple[str, object | None] | None = None
        failed = False
        cancelled = False
        try:
            broker = self._validated_executor_broker()
            outcome = await broker.execute_bounded(
                registration,
                context,
                arguments,
                effect_id=effect_id,
                timeout_ms=timeout_ms,
            )
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            failed = True
        if cancelled:
            return "cancelled", None
        if failed or outcome is None:
            return "execution_failed", None
        return outcome

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
        owner_id: str,
        deadline_at_ms: int,
        step_claim: StepExecutionClaim | None,
    ) -> tuple[ToolEffectRecord | None, PublicToolResult | None, bool]:
        reserve_failed = False
        effect: ToolEffectRecord | None = None
        created = False
        try:
            effect, created = self.repository.reserve_tool_effect(
                reservation,
                execution_owner_id=owner_id,
                lease_duration_ms=manifest.timeout_ms + _LEASE_GRACE_MS,
                step_claim=step_claim,
            )
        except Exception:
            reserve_failed = True
        if reserve_failed or effect is None:
            raise ToolRouteError(
                "effect_reservation_failed",
                "Tool Effect reservation failed",
                effect_id=reservation.effect_id,
            ) from None
        if created:
            return effect, None, False
        while True:
            replay = self._terminal_replay(manifest, effect, reservation.write_effect)
            if replay is not None:
                return None, replay, False
            if effect.status != "reserved":
                self._raise_terminal(effect)
            if (
                effect.execution_owner_id is None
                or effect.fence_token is None
                or effect.fence_generation < 1
            ):
                raise ToolRouteError(
                    "effect_corrupt", "Reserved Tool Effect has no execution fence",
                    effect_id=effect.effect_id,
                )
            takeover_failed = False
            replacement: ToolEffectRecord | None = None
            won = False
            try:
                replacement, won = self.repository.takeover_expired_tool_effect(
                    reservation,
                    expected_owner_id=effect.execution_owner_id,
                    expected_fence_token=effect.fence_token,
                    expected_fence_generation=effect.fence_generation,
                    execution_owner_id=owner_id,
                    lease_duration_ms=manifest.timeout_ms + _LEASE_GRACE_MS,
                    step_claim=step_claim,
                )
            except Exception:
                takeover_failed = True
            if takeover_failed or replacement is None:
                raise ToolRouteError(
                    "effect_takeover_failed",
                    "Tool Effect recovery ownership failed",
                    effect_id=effect.effect_id,
                ) from None
            if won and reservation.write_effect:
                terminal = self._terminal_effect(
                    replacement,
                    status="reconciliation_required",
                    code="write_outcome_unknown",
                )
                if (
                    await self._persist_terminal(
                        terminal, replacement, step_claim=step_claim
                    )
                    is not None
                ):
                    raise ToolRouteError(
                        "reconciliation_required",
                        "Tool Effect requires reconciliation",
                        effect_id=replacement.effect_id,
                    )
                raise ToolRouteError(
                    "persistence_failure", "Tool Effect persistence is unavailable",
                    effect_id=replacement.effect_id,
                )
            if won and manifest.idempotency != "idempotent":
                terminal = self._terminal_effect(
                    replacement, status="rejected", code="replay_not_allowed"
                )
                await self._persist_terminal_or_raise(
                    terminal, replacement, step_claim=step_claim
                )
                raise ToolRouteError(
                    "replay_not_allowed",
                    "Manifest does not permit replay of this read Tool",
                    effect_id=replacement.effect_id,
                )
            if won:
                return replacement, None, True
            effect = replacement
            if self._remaining_ms(deadline_at_ms) <= 0:
                raise ToolRouteError(
                    "deadline_exceeded", "Tool Step deadline elapsed",
                    effect_id=effect.effect_id,
                )
            await asyncio.sleep(
                min(_WAIT_POLL_SECONDS, self._remaining_ms(deadline_at_ms) / 1000)
            )

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
        step_claim: StepExecutionClaim | None,
    ) -> None:
        terminal = self._terminal_effect(
            reservation,
            status=("reconciliation_required" if write_effect else "rejected"),
            code=code,
        )
        await self._persist_terminal_or_raise(
            terminal, reservation, step_claim=step_claim
        )
        raise ToolRouteError(
            "reconciliation_required" if write_effect else code,
            "Tool execution did not produce a reusable result",
            effect_id=reservation.effect_id,
        ) from None

    async def _persist_terminal(
        self,
        effect: ToolEffectRecord,
        reservation: ToolEffectRecord,
        *,
        step_claim: StepExecutionClaim | None,
    ) -> ToolEffectRecord | None:
        async def persist() -> ToolEffectRecord | None:
            durable: ToolEffectRecord | None = None
            try:
                current, finished = self.repository.finish_tool_effect(
                    effect,
                    expected_owner_id=reservation.execution_owner_id or "",
                    expected_fence_token=reservation.fence_token or "",
                    expected_fence_generation=reservation.fence_generation,
                    step_claim=step_claim,
                )
            except Exception:
                return None
            if finished and canonical_json(current) == canonical_json(effect):
                durable = current
            return durable

        task = asyncio.create_task(persist())
        cancelled = False
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            result = task.result()
        if cancelled:
            raise asyncio.CancelledError()
        return result

    async def _persist_terminal_or_raise(
        self,
        effect: ToolEffectRecord,
        reservation: ToolEffectRecord,
        *,
        step_claim: StepExecutionClaim | None,
    ) -> ToolEffectRecord:
        durable = await self._persist_terminal(
            effect, reservation, step_claim=step_claim
        )
        if durable is None:
            raise ToolRouteError(
                "persistence_failure",
                "Tool Effect terminal persistence failed",
                effect_id=effect.effect_id,
            ) from None
        return durable

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
                "result_code": code,
                "result_digest": canonical_digest({"code": code, "result": result}),
                "public_result": result,
            }
        )

__all__ = [
    "ExecutorBroker",
    "ExecutorRegistration",
    "FileAccess",
    "HmacRequestDigestService",
    "SupervisedExecutionSnapshot",
    "ApprovalRequest",
    "SdkToolWrapper",
    "ToolAccess",
    "ToolRouteError",
    "ToolRouter",
]
