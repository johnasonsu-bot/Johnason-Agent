from __future__ import annotations

from pathlib import Path
from secrets import token_urlsafe

import pytest

from tests.fixtures.assignment_v2 import admitted_assignment
from tests.fixtures.host_v2 import fake_v2_command, run_envelope, runtime_capabilities
from workbench.api.conversations import (
    ConversationAPI,
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
from workbench.runtime.federated_conversation import FederatedConversationExecutor
from workbench.runtime.provider_grants import (
    FederatedRuntimeCoordinator,
    ProviderGrantBroker,
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
    envelope = run_envelope(
        command_id=runtime_command_id,
        host_generation="1",
        overrides={
            "provider_ref": "provider-profile:deepseek-primary",
            "model": "deepseek-v4-flash",
            "message_snapshot_digest": runtime_input.message_snapshot_digest,
            "context.snapshot_digest": runtime_input.context_snapshot_digest,
            "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
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
    providers = ProviderRepository(database)
    _, profile = providers.upsert(
        ProviderProfileRecord.deepseek(id="deepseek-primary")
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
        await api.process_queued_turn(session_id, public_command_id)
    finally:
        await supervisor.aclose()

    turn = repository.load_turn_status(session_id, public_command_id)
    assert turn is not None
    assert turn.status == "completed"
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
