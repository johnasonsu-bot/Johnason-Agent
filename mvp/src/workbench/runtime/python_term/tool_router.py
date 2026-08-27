"""Fail-closed routing and durable Effect lifecycle for Python Term tools."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from jsonschema import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
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
    path: str
    mode: Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class ToolAccess:
    files: tuple[FileAccess, ...] = ()
    network: bool = False
    command: bool = False


ToolExecutor = Callable[
    [StepContext, Mapping[str, object]], Awaitable[object]
]
AccessResolver = Callable[[Mapping[str, object]], ToolAccess]


@dataclass(frozen=True, slots=True)
class ExecutorSeam:
    execute: ToolExecutor
    access: ToolAccess | AccessResolver = ToolAccess()

    def resolve_access(self, arguments: Mapping[str, object]) -> ToolAccess:
        resolved = self.access(arguments) if callable(self.access) else self.access
        if not isinstance(resolved, ToolAccess):
            raise TypeError("Tool access resolver must return ToolAccess")
        return resolved


@dataclass(frozen=True, slots=True)
class SdkToolWrapper:
    context: StepContext
    manifest: ToolManifestEntryV2
    executor: ExecutorSeam

    @property
    def tool_id(self) -> str:
        return self.manifest.tool_id


class ToolRouteError(RuntimeError):
    def __init__(self, code: str, message: str, *, effect_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.effect_id = effect_id


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    tool_id: str
    tool_call_id: str
    context_identity_digest: str
    reasons: tuple[Literal["tool", "filesystem", "network", "command"], ...]


ApprovalCallback = Callable[[ApprovalRequest], bool | Awaitable[bool]]


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


class ToolRouter:
    def __init__(
        self,
        repository: PythonTermRepository,
        executors: Mapping[str, ExecutorSeam],
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.repository = repository
        self.executors = dict(executors)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def exposed_tools(self, context: StepContext) -> tuple[SdkToolWrapper, ...]:
        wrappers: list[SdkToolWrapper] = []
        seen: set[str] = set()
        for manifest in context.tool_manifest:
            if manifest.tool_id in seen:
                raise ToolRouteError(
                    "manifest_conflict", "frozen manifest contains a duplicate Tool"
                )
            seen.add(manifest.tool_id)
            executor = self.executors.get(manifest.tool_id)
            if executor is None or not self._scope_allows(context, manifest):
                continue
            wrappers.append(
                SdkToolWrapper(
                    context=context,
                    manifest=manifest,
                    executor=executor,
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
        # Manifest resolution is a read-only preflight needed to select the
        # frozen schema. Authorization is not granted until the second lookup.
        manifest = self._manifest(context, tool_id)
        normalized = self._validate_arguments(manifest, arguments)
        manifest = self._manifest(context, tool_id)
        executor = self.executors.get(tool_id)
        if executor is None:
            raise ToolRouteError(
                "tool_unavailable", "Tool has no registered executor"
            )
        if not self._scope_allows(context, manifest):
            raise ToolRouteError("effect_scope_denied", "Tool is outside Effect scope")

        try:
            access = executor.resolve_access(normalized)
        except Exception:
            raise ToolRouteError(
                "access_rejected", "Tool access requirements are invalid"
            ) from None
        reasons = self._authorize(context, manifest, access)
        if reasons:
            await self._approve(
                context,
                tool_id,
                tool_call_id,
                reasons,
                approval,
            )

        effect_id = self._effect_id(context, tool_call_id)
        request_digest = canonical_digest(
            {
                "arguments": normalized,
                "context_identity_digest": context.identity_digest,
                "manifest": manifest,
                "tool_call_id": tool_call_id,
                "access": {
                    "files": [
                        {"mode": item.mode, "path": item.path}
                        for item in access.files
                    ],
                    "network": access.network,
                    "command": access.command,
                },
            }
        )
        reservation = ToolEffectRecord(
            effect_id=effect_id,
            term_id=context.term_id,
            step_id=context.step_id,
            tool_call_id=tool_call_id,
            request_digest=request_digest,
            status="reserved",
        )
        existing, created = self.repository.reserve_tool_effect(reservation)
        replay = self._replay_decision(manifest, existing, created)
        if replay is not None:
            return replay

        try:
            raw_result = await asyncio.wait_for(
                executor.execute(context, normalized),
                timeout=manifest.timeout_ms / 1000,
            )
        except TimeoutError:
            self._execution_failed(
                manifest,
                reservation,
                code="timeout",
            )
        except Exception:
            self._execution_failed(
                manifest,
                reservation,
                code="execution_failed",
            )

        try:
            result = PublicToolResult.model_validate(raw_result)
            if result.status != "completed":
                raise ValueError("executor returned a non-completed result")
        except (PydanticValidationError, TypeError, ValueError):
            rejected = self._terminal_effect(
                reservation,
                status="rejected",
                code="result_rejected",
            )
            self.repository.save_tool_effect(rejected)
            raise ToolRouteError(
                "result_rejected",
                "Tool result failed the public result boundary",
                effect_id=effect_id,
            ) from None

        committed = reservation.model_copy(
            update={
                "status": "committed",
                "result_digest": canonical_digest(result),
                "public_result": result,
            }
        )
        self.repository.save_tool_effect(committed)
        return result

    @staticmethod
    def _manifest(
        context: StepContext, tool_id: str
    ) -> ToolManifestEntryV2:
        matches = tuple(
            item for item in context.tool_manifest if item.tool_id == tool_id
        )
        if len(matches) != 1:
            raise ToolRouteError(
                "tool_not_manifested",
                "Tool is not uniquely present in the frozen manifest",
            )
        return matches[0]

    @staticmethod
    def _validate_arguments(
        manifest: ToolManifestEntryV2, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        try:
            normalized = validate_safe_json(arguments)
            if not isinstance(normalized, dict):
                raise ValueError("Tool arguments must be an object")
            schema = manifest.model_dump(mode="json")["schema"]
            validator_type = validator_for(schema)
            validator_type.check_schema(schema)
            validator_type(schema).validate(normalized)
        except (
            JsonSchemaValidationError,
            SchemaError,
            PydanticValidationError,
            TypeError,
            ValueError,
        ):
            raise ToolRouteError(
                "schema_rejected", "Tool arguments failed schema validation"
            ) from None
        return cast(Mapping[str, object], _freeze(normalized))

    @staticmethod
    def _scope_allows(
        context: StepContext, manifest: ToolManifestEntryV2
    ) -> bool:
        scope = context.effect_scope
        if scope.allowed_tool_ids and manifest.tool_id not in scope.allowed_tool_ids:
            return False
        return manifest.read_only or scope.write_effects

    def _authorize(
        self,
        context: StepContext,
        manifest: ToolManifestEntryV2,
        access: ToolAccess,
    ) -> tuple[Literal["tool", "filesystem", "network", "command"], ...]:
        if self.clock_ms() >= context.workspace_grant.expires_at_ms:
            raise ToolRouteError("workspace_expired", "Workspace Grant has expired")
        reasons: list[Literal["tool", "filesystem", "network", "command"]] = []
        self._policy(context.permission_policy.tool_policy, "tool", reasons)
        if access.files:
            self._policy(
                context.permission_policy.filesystem_policy,
                "filesystem",
                reasons,
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
            self._workspace_policy(
                context.workspace_grant.network_policy,
                "network",
                reasons,
            )
        if access.command:
            self._workspace_policy(
                context.workspace_grant.command_policy,
                "command",
                reasons,
            )
        return tuple(reasons)

    @staticmethod
    def _policy(
        policy: str,
        reason: Literal["tool", "filesystem", "network", "command"],
        reasons: list[Literal["tool", "filesystem", "network", "command"]],
    ) -> None:
        if policy == "deny":
            raise ToolRouteError("permission_denied", "Tool permission was denied")
        if policy in {"ask", "supervisor_approval"}:
            reasons.append(reason)

    @classmethod
    def _workspace_policy(
        cls,
        policy: str,
        reason: Literal["network", "command"],
        reasons: list[Literal["tool", "filesystem", "network", "command"]],
    ) -> None:
        if policy == "deny":
            raise ToolRouteError(f"{reason}_denied", f"Tool {reason} was denied")
        cls._policy(policy, reason, reasons)

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

    @staticmethod
    async def _approve(
        context: StepContext,
        tool_id: str,
        tool_call_id: str,
        reasons: tuple[Literal["tool", "filesystem", "network", "command"], ...],
        approval: ApprovalCallback | None,
    ) -> None:
        if approval is None:
            raise ToolRouteError(
                "approval_required", "Tool execution requires approval"
            )
        request = ApprovalRequest(
            tool_id=tool_id,
            tool_call_id=tool_call_id,
            context_identity_digest=context.identity_digest,
            reasons=reasons,
        )
        try:
            decision = approval(request)
            approved = await decision if inspect.isawaitable(decision) else decision
        except Exception:
            raise ToolRouteError("approval_denied", "Tool approval failed") from None
        if approved is not True:
            raise ToolRouteError("approval_denied", "Tool approval was denied")

    @staticmethod
    def _effect_id(context: StepContext, tool_call_id: str) -> str:
        return "effect-" + canonical_digest(
            {
                "scope_id": context.effect_scope.scope_id,
                "term_id": context.term_id,
                "step_id": context.step_id,
                "tool_call_id": tool_call_id,
            }
        )

    def _replay_decision(
        self,
        manifest: ToolManifestEntryV2,
        effect: ToolEffectRecord,
        created: bool,
    ) -> PublicToolResult | None:
        if created:
            return None
        if effect.status == "committed":
            if not manifest.read_only or manifest.idempotency == "idempotent":
                if effect.public_result is None or effect.result_digest != canonical_digest(
                    effect.public_result
                ):
                    raise ToolRouteError(
                        "effect_corrupt", "Committed Tool Effect has no authoritative result",
                        effect_id=effect.effect_id,
                    )
                return effect.public_result
            raise ToolRouteError(
                "replay_not_allowed",
                "Manifest does not permit replay of this read Tool",
                effect_id=effect.effect_id,
            )
        if effect.status == "reconciliation_required":
            raise ToolRouteError(
                "reconciliation_required",
                "Tool Effect requires reconciliation",
                effect_id=effect.effect_id,
            )
        if effect.status == "rejected":
            raise ToolRouteError(
                "effect_rejected",
                "Tool Effect was already rejected",
                effect_id=effect.effect_id,
            )
        if not manifest.read_only:
            unknown = self._terminal_effect(
                effect,
                status="reconciliation_required",
                code="write_outcome_unknown",
            )
            self.repository.save_tool_effect(unknown)
            raise ToolRouteError(
                "reconciliation_required",
                "Tool Effect requires reconciliation",
                effect_id=effect.effect_id,
            )
        if manifest.idempotency != "idempotent":
            rejected = self._terminal_effect(
                effect,
                status="rejected",
                code="replay_not_allowed",
            )
            self.repository.save_tool_effect(rejected)
            raise ToolRouteError(
                "replay_not_allowed",
                "Manifest does not permit replay of this read Tool",
                effect_id=effect.effect_id,
            )
        return None

    def _execution_failed(
        self,
        manifest: ToolManifestEntryV2,
        reservation: ToolEffectRecord,
        *,
        code: str,
    ) -> None:
        if manifest.read_only:
            status: Literal["rejected", "reconciliation_required"] = "rejected"
            public_code = code
        else:
            status = "reconciliation_required"
            public_code = "reconciliation_required"
        terminal = self._terminal_effect(
            reservation,
            status=status,
            code=code,
        )
        self.repository.save_tool_effect(terminal)
        raise ToolRouteError(
            public_code,
            "Tool execution did not produce a reusable result",
            effect_id=reservation.effect_id,
        ) from None

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
                "result_digest": canonical_digest({"code": code, "result": result}),
                "public_result": result,
            }
        )


__all__ = [
    "ExecutorSeam",
    "FileAccess",
    "ApprovalRequest",
    "SdkToolWrapper",
    "ToolAccess",
    "ToolRouteError",
    "ToolRouter",
]
