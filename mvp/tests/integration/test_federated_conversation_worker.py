from __future__ import annotations

import asyncio
import time
from pathlib import Path
from secrets import token_urlsafe

import pytest

from tests.fixtures.assignment_v2 import admitted_assignment
from tests.fixtures.host_v2 import fake_v2_command, run_envelope, runtime_capabilities
from workbench.api.conversations import (
    ConversationAPI,
    ConversationInterventionRequest,
    RuntimeConversationRoute,
    python_term_command_id,
)
from workbench.conversations.repository import ConversationRepository
from workbench.credentials.service import VaultService
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.v2.contracts import (
    RuntimeMessageInputV2,
    RuntimePromptSectionInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)
from workbench.runtime.engine_host.v2.registry import RuntimeRegistryV2
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.engine_host.v2.supervisor import SidecarSupervisor
from workbench.runtime.federated_conversation import (
    FederatedConversationExecutionError,
    FederatedConversationExecutor,
)
from workbench.runtime.provider_grants import (
    FederatedRuntimeCoordinator,
    ProviderGrantBroker,
    canonical_provider_profile_digest,
)
from workbench.runtime.provider_grants.repository import ProviderGrantRepository
from workbench.settings import RuntimeProcessConfig
from workbench.workflow.event_store import EventStore


class _NoopRunner:
    async def run_turn(self, _command):
        if False:
            yield None


class _PinnedRoute:
    def __init__(self, route: RuntimeConversationRoute) -> None:
        self.route = route

    def route_conversation_query(self, *, selector, admission):
        assert selector == self.route.runtime_id
        assert admission.runtime_command_id == self.route.runtime_command_id
        return self.route


def _runtime_input() -> RuntimeQueryInputV2:
    messages = (
        RuntimeMessageInputV2(
            message_id="message-1",
            role="user",
            content="execute through the federated worker",
        ),
    )
    prompt_sections = (
        RuntimePromptSectionInputV2(
            section_id="section-1", order=0, content="pinned instructions"
        ),
    )
    return RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=(),
        context_snapshot_digest=canonical_runtime_input_digest(()),
        prompt_sections=prompt_sections,
        prompt_manifest_digest=canonical_runtime_input_digest(prompt_sections),
    )


@pytest.mark.asyncio
async def test_worker_runs_supervisor_grant_host_v2_into_conversation_store(
    tmp_path: Path,
) -> None:
    database = tmp_path / "federated-conversation.sqlite"
    session_id = "session-1"
    public_command_id = "turn-1"
    runtime_command_id = python_term_command_id(session_id, public_command_id)
    runtime_input = _runtime_input()
    providers = ProviderRepository(database)
    _, profile = providers.upsert(
        ProviderProfileRecord.deepseek(id="deepseek-primary")
    )
    envelope = run_envelope(
        command_id=runtime_command_id,
        host_generation="1",
        overrides={
            "provider_ref": "provider-profile:deepseek-primary",
            "model": "deepseek-v4-flash",
            "deadline_ms": 60_000,
            "message_snapshot_digest": runtime_input.message_snapshot_digest,
            "context.snapshot_digest": runtime_input.context_snapshot_digest,
            "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
            "extensions": {
                "provider_profile_digest": canonical_provider_profile_digest(profile),
                "resolved_model": "deepseek-v4-flash",
            },
        },
    )
    capabilities = runtime_capabilities(
        "fake-v2",
        build_id="python:test-build",
        query=True,
        model=True,
        tools=True,
        skills=True,
        plugins=True,
        workspace=True,
        interventions=True,
        pause_resume=True,
        compaction=True,
        checkpoints=True,
        streaming=True,
        plan=True,
        todo=True,
        prompt_sections=True,
        tool_interceptors=True,
        event_cursor=True,
    )
    assignments, _ = admitted_assignment(database, envelope, capabilities)
    supervisor = SidecarSupervisor(
        runtimes=(
            RuntimeProcessConfig(
                runtime_id="fake-v2",
                argv=fake_v2_command("provider_grant_query"),
            ),
        ),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="federated-conversation-test",
    )
    vault = VaultService(tmp_path / "federated-conversation.vault")
    vault.create(token_urlsafe(24))
    assert profile.secret_id is not None
    vault.put(profile.secret_id, token_urlsafe(32))
    broker = ProviderGrantBroker(
        database=database,
        providers=providers,
        vault=vault,
        authority=supervisor,
    )
    executor = FederatedConversationExecutor(
        assignments=assignments,
        supervisor=supervisor,
        coordinator=FederatedRuntimeCoordinator(broker),
    )
    route = RuntimeConversationRoute(
        runtime_id="fake-v2",
        build_id="python:test-build",
        runtime_command_id=runtime_command_id,
        execution_snapshot={
            "selector": "fake-v2",
            "runtime_id": "fake-v2",
            "build_id": "python:test-build",
            "provider_profile_digest": canonical_provider_profile_digest(profile),
            "resolved_model": "deepseek-v4-flash",
            "envelope": envelope.model_dump(mode="json"),
            "runtime_input": runtime_input.model_dump(mode="json"),
        },
    )
    repository = ConversationRepository(database)
    api = ConversationAPI(
        conversations=repository,
        events=EventStore(database),
        runner=_NoopRunner(),
        providers=providers,
        runtime_router=_PinnedRoute(route),
        federated_executor=executor,
    )
    api.create_session(session_id)

    await supervisor.start()
    try:
        await api.enqueue_message(
            session_id=session_id,
            command_id=public_command_id,
            content="execute through the federated worker",
            model="default",
            provider_id="deepseek-primary",
            runtime="fake-v2",
        )
        assert (
            repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)
            is not None
        )
        providers.upsert(
            profile.model_copy(
                update={"base_url": "https://api.deepseek.com/v2"}
            )
        )
        await api.process_queued_turn(session_id, public_command_id)
        retryable = repository.load_turn_status(session_id, public_command_id)
        assert retryable is not None and retryable.status == "retryable"
        assert retryable.state["reason"] == "provider_unavailable"
        assert not any(
            event.event_type.startswith("runtime.")
            for event in api.events.read_stream(f"run:{session_id}")
        )
        providers.upsert(profile)
        assert canonical_provider_profile_digest(
            providers.get("deepseek-primary")
        ) == envelope.extensions["provider_profile_digest"]
        retry_not_before = retryable.state["retry_not_before"]
        await asyncio.sleep(max(0.0, retry_not_before - time.time()) + 0.01)
        assert (
            repository.claim_next_turn(owner_id="worker-2", lease_seconds=30)
            is not None
        )
        await api.process_queued_turn(session_id, public_command_id)
    finally:
        await supervisor.aclose()

    turn = repository.load_turn_status(session_id, public_command_id)
    assert turn is not None
    assert turn.status == "completed", turn.state
    assert turn.state["runtime_projected_cursor"] == 2
    event_types = [
        event.event_type for event in api.events.read_stream(f"run:{session_id}")
    ]
    assert event_types[-3:] == [
        "runtime.status.changed",
        "runtime.status.changed",
        "conversation.turn.finished",
    ]
    grant_repository = ProviderGrantRepository(database)
    with grant_repository.store.connect() as connection:
        rows = connection.execute(
            "SELECT grant_id FROM provider_grants_private"
        ).fetchall()
    assert len(rows) == 1
    assert grant_repository.get(rows[0]["grant_id"]).state == "consumed"


@pytest.mark.parametrize("cancel_phase", ["grant", "query"])
@pytest.mark.asyncio
async def test_cancel_intervention_reaches_federated_lease_and_seals_once(
    tmp_path: Path, cancel_phase: str,
) -> None:
    database = tmp_path / "federated-cancel.sqlite"
    session_id = "session-cancel"
    public_command_id = "turn-cancel"
    runtime_command_id = python_term_command_id(session_id, public_command_id)
    runtime_input = _runtime_input()
    providers = ProviderRepository(database)
    _, profile = providers.upsert(
        ProviderProfileRecord.deepseek(id="deepseek-primary")
    )
    profile_digest = canonical_provider_profile_digest(profile)
    envelope = run_envelope(
        command_id=runtime_command_id,
        host_generation="1",
        overrides={
            "session_id": f"conversation-session:{session_id}",
            "deadline_ms": 60_000,
            "provider_ref": "provider-profile:deepseek-primary",
            "model": "deepseek-v4-flash",
            "message_snapshot_digest": runtime_input.message_snapshot_digest,
            "context.snapshot_digest": runtime_input.context_snapshot_digest,
            "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
            "extensions": {
                "provider_profile_digest": profile_digest,
                "resolved_model": "deepseek-v4-flash",
            },
        },
    )
    capabilities = runtime_capabilities(
        "fake-v2",
        build_id="python:test-build",
        query=True,
        model=True,
        tools=True,
        skills=True,
        plugins=True,
        workspace=True,
        interventions=True,
        pause_resume=True,
        compaction=True,
        checkpoints=True,
        streaming=True,
        plan=True,
        todo=True,
        prompt_sections=True,
        tool_interceptors=True,
        event_cursor=True,
    )
    assignments, _ = admitted_assignment(database, envelope, capabilities)
    supervisor = SidecarSupervisor(
        runtimes=(
            RuntimeProcessConfig(
                runtime_id="fake-v2",
                argv=fake_v2_command("provider_grant_blocking_query"),
            ),
        ),
        registry=RuntimeRegistryV2(RuntimeV2Repository(database)),
        assignments=assignments,
        runtime_dir=tmp_path,
        app_instance_id="federated-cancel-test",
    )
    vault = VaultService(tmp_path / "federated-cancel.vault")
    vault.create(token_urlsafe(24))
    assert profile.secret_id is not None
    vault.put(profile.secret_id, token_urlsafe(32))
    broker = ProviderGrantBroker(
        database=database,
        providers=providers,
        vault=vault,
        authority=supervisor,
    )
    grant_delivery_started = asyncio.Event()
    if cancel_phase == "grant":
        original_deliver = broker.deliver

        async def block_grant_delivery(*args, **kwargs):
            actual_delivery = kwargs["delivery"]

            class BlockedAckDelivery:
                async def deliver(self, binding, secret):
                    grant_delivery_started.set()
                    await asyncio.Event().wait()
                    return await actual_delivery.deliver(binding, secret)

            return await original_deliver(
                *args, **{**kwargs, "delivery": BlockedAckDelivery()}
            )

        broker.deliver = block_grant_delivery
    executor = FederatedConversationExecutor(
        assignments=assignments,
        supervisor=supervisor,
        coordinator=FederatedRuntimeCoordinator(broker),
    )
    route = RuntimeConversationRoute(
        runtime_id="fake-v2",
        build_id="python:test-build",
        runtime_command_id=runtime_command_id,
        execution_snapshot={
            "selector": "fake-v2",
            "runtime_id": "fake-v2",
            "build_id": "python:test-build",
            "provider_profile_digest": profile_digest,
            "resolved_model": "deepseek-v4-flash",
            "envelope": envelope.model_dump(mode="json"),
            "runtime_input": runtime_input.model_dump(mode="json"),
        },
    )
    repository = ConversationRepository(database)
    api = ConversationAPI(
        conversations=repository,
        events=EventStore(database),
        runner=_NoopRunner(),
        providers=providers,
        runtime_router=_PinnedRoute(route),
        federated_executor=executor,
    )
    api.create_session(session_id)

    await supervisor.start()
    try:
        await api.enqueue_message(
            session_id=session_id,
            command_id=public_command_id,
            content="cancel the federated worker",
            model="default",
            provider_id="deepseek-primary",
            runtime="fake-v2",
        )
        assert repository.claim_next_turn(owner_id="worker-1", lease_seconds=30)
        processing = asyncio.create_task(
            api.process_queued_turn(session_id, public_command_id)
        )
        if cancel_phase == "grant":
            await asyncio.wait_for(grant_delivery_started.wait(), timeout=2.0)

            async def collect_duplicate_execution():
                return [
                    event
                    async for event in executor.execute(route.execution_snapshot)
                ]

            with pytest.raises(FederatedConversationExecutionError) as duplicate:
                await asyncio.wait_for(
                    collect_duplicate_execution(), timeout=1.0
                )
            assert duplicate.value.category == "runtime_unavailable"
            assert duplicate.value.retryable is True
        else:
            for _ in range(500):
                if any(
                    event.event_type == "runtime.status.changed"
                    and event.payload.get("status") == "running"
                    for event in api.events.read_stream(f"run:{session_id}")
                ):
                    break
                if processing.done():
                    await processing
                    current = repository.load_turn_status(
                        session_id, public_command_id
                    )
                    raise AssertionError(
                        f"Host v2 query ended before acceptance: {current}"
                    )
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("Host v2 did not accept the query")
        await api.queue_intervention(
            session_id=session_id,
            command_id="cancel-command",
            payload=ConversationInterventionRequest(
                kind="cancel", content="stop", context_version=0
            ),
        )
        await asyncio.wait_for(processing, timeout=2.0)
    finally:
        if "processing" in locals() and not processing.done():
            processing.cancel()
            await asyncio.gather(processing, return_exceptions=True)
        await supervisor.aclose()

    turn = repository.load_turn_status(session_id, public_command_id)
    assert turn is not None and turn.state["reason"] == "runtime_cancelled"
    terminals = [
        event
        for event in api.events.read_stream(f"run:{session_id}")
        if event.event_type == "conversation.turn.failed"
    ]
    assert len(terminals) == 1
    assert terminals[0].payload["response_status"] == "cancelled"
    if cancel_phase == "grant":
        with ProviderGrantRepository(database).store.connect() as connection:
            grant = connection.execute(
                "SELECT grant_id FROM provider_grants_private"
            ).fetchone()
        assert grant is not None
        record = ProviderGrantRepository(database).get(grant["grant_id"])
        assert record.state == "revoked"
        assert record.reason == "query_cancelled"
