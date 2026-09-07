"""Platform Tool calls adapted to the existing durable Tool Router."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from workbench.runtime.python_term.contracts import PublicToolResult, StepContext
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.tool_router import ToolDispatchPermit, ToolRouter

from .contracts import RunEnvelopeV2
from .tool_transport import (
    PlatformToolCall,
    PlatformToolResult,
    ToolWriteUncertain,
)


FrozenPlatformContextFactory = Callable[[RunEnvelopeV2], StepContext]
_MAX_STEP_CLAIM_LEASE_MS = 86_400_000
_STEP_CLAIM_GRACE_MS = 5_000


class PlatformToolRouterAdapter:
    def __init__(
        self,
        *,
        runtime_id: Literal["goose", "dsh"],
        router: ToolRouter,
        repository: PythonTermRepository,
        context_factory: FrozenPlatformContextFactory,
        owner_id: str,
    ) -> None:
        if runtime_id not in {"goose", "dsh"}:
            raise ValueError("platform runtime identity is unsupported")
        if not isinstance(router, ToolRouter):
            raise TypeError("router must be a ToolRouter")
        if not isinstance(repository, PythonTermRepository):
            raise TypeError("repository must be a PythonTermRepository")
        if router.repository is not repository:
            raise ValueError("adapter and Tool Router must share one repository")
        if not callable(context_factory):
            raise TypeError("context_factory must be callable")
        if not isinstance(owner_id, str) or not owner_id:
            raise ValueError("owner_id is required")
        self._runtime_id = runtime_id
        self._router = router
        self._repository = repository
        self._context_factory = context_factory
        self._owner_id = owner_id
        self._claim_lock = asyncio.Lock()

    async def __call__(self, call: PlatformToolCall) -> PlatformToolResult:
        if not isinstance(call, PlatformToolCall):
            raise TypeError("call must be a PlatformToolCall")
        context = self._context_factory(call.envelope)
        if not isinstance(context, StepContext):
            raise TypeError("context_factory must return a StepContext")
        if (
            call.envelope.runtime.runtime_id != self._runtime_id
            or context.runtime_id != self._runtime_id
            or tuple(
                item for item in call.envelope.tool_manifest
                if item.tool_id == call.manifest.tool_id
            ) != (call.manifest,)
        ):
            raise ValueError("platform Tool identity does not match the frozen query")
        lease_window_ms = (
            max(call.manifest.timeout_ms, context.deadline_ms)
            + _STEP_CLAIM_GRACE_MS
        )
        if lease_window_ms > _MAX_STEP_CLAIM_LEASE_MS:
            raise ValueError("platform Tool exceeds the durable Step claim lease")
        async with self._claim_lock:
            invocation, permit = await self._start_invocation(
                call,
                context,
                lease_seconds=lease_window_ms / 1_000,
            )
        cancelled_task = asyncio.create_task(call.cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {invocation, cancelled_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if invocation in done:
                cancelled_task.cancel()
                return self._map_result(
                    await invocation,
                    effect_id=permit.effect_id,
                )
            invocation.cancel()
            await self._settle(invocation)
            effect = self._repository.get_tool_effect(permit.effect_id)
            if effect is not None and effect.status == "committed":
                return self._map_result(
                    effect.public_result,
                    effect_id=effect.effect_id,
                )
            if call.manifest.read_only:
                return PlatformToolResult(
                    status="failed",
                    output={"error": "tool_cancelled"},
                    effect_id=permit.effect_id,
                )
            raise ToolWriteUncertain(
                "platform write cancellation requires reconciliation"
            )
        except asyncio.CancelledError:
            if not invocation.done():
                invocation.cancel()
            await self._settle(invocation)
            raise
        except BaseException:
            if not invocation.done():
                invocation.cancel()
            await self._settle(invocation)
            raise
        finally:
            if not cancelled_task.done():
                cancelled_task.cancel()

    async def _start_invocation(
        self,
        call: PlatformToolCall,
        context: StepContext,
        *,
        lease_seconds: float,
    ) -> tuple[asyncio.Task[PublicToolResult], ToolDispatchPermit]:
        term = context.to_term_record(call.envelope)
        step = context.to_step_record()
        self._repository.save_aggregate(term, (step,))
        claim = self._repository.claim_step(
            context.term_id,
            context.step_id,
            owner_id=self._owner_id,
            lease_seconds=lease_seconds,
        )
        if claim is None:
            raise RuntimeError("platform Tool Step is unavailable")
        self._router.admit(context, step_claim=claim)
        invocation = asyncio.create_task(
            self._router.invoke(
                context,
                call.manifest.tool_id,
                call.arguments,
                tool_call_id=call.tool_call_id,
                step_claim=claim,
            )
        )
        gate_task = asyncio.create_task(
            self._router.await_dispatch_gate(
                context_identity_digest=context.identity_digest,
                tool_call_id=call.tool_call_id,
                timeout_ms=min(context.deadline_ms, call.manifest.timeout_ms),
            )
        )
        cancelled_task = asyncio.create_task(call.cancelled.wait())
        try:
            done, _ = await asyncio.wait(
                {invocation, gate_task, cancelled_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled_task in done:
                gate_task.cancel()
                invocation.cancel()
                await self._settle(invocation)
                raise asyncio.CancelledError()
            if invocation in done:
                gate_task.cancel()
                await invocation
                raise RuntimeError("Tool invocation ended without a dispatch gate")
            permit = gate_task.result()
            if call.cancelled.is_set():
                invocation.cancel()
                await self._settle(invocation)
                raise asyncio.CancelledError()
            self._router.release_dispatch_gate(permit)
            return invocation, permit
        except BaseException:
            if not invocation.done():
                invocation.cancel()
            await self._settle(invocation)
            raise
        finally:
            if not gate_task.done():
                gate_task.cancel()
            if not cancelled_task.done():
                cancelled_task.cancel()

    @staticmethod
    def _map_result(
        result: PublicToolResult | None,
        *,
        effect_id: str | None,
    ) -> PlatformToolResult:
        if not isinstance(result, PublicToolResult):
            raise TypeError("Tool Router returned an invalid public result")
        return PlatformToolResult(
            status=result.status,
            output=result.model_dump(mode="json", exclude_none=True),
            effect_id=effect_id,
        )

    @staticmethod
    async def _settle(task: asyncio.Task[PublicToolResult]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done():
            try:
                task.exception()
            except BaseException:
                pass


__all__ = ["FrozenPlatformContextFactory", "PlatformToolRouterAdapter"]
