from __future__ import annotations

import pytest

from workbench.api.conversations import PythonTermConversationAdmission
from workbench.conversations.models import ConversationMessage
from workbench.models.profiles import ProviderProfileRecord
from workbench.runtime.conversation_execution import (
    build_runtime_execution_snapshot,
    read_runtime_execution,
    runtime_input_context_items,
)
from workbench.runtime.engine_host.v2.contracts import (
    QueryCommandV2,
    RunEnvelopeV2,
    RuntimeMessageInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)


def _admission() -> PythonTermConversationAdmission:
    return PythonTermConversationAdmission(
        session_id="session-1",
        command_id="command-1",
        runtime_command_id="runtime-command-1",
        provider=ProviderProfileRecord(
            id="provider-1",
            name="Provider",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
            model_aliases={"default": "configured-model"},
        ),
        model="configured-model",
        agent_profiles=(),
        project_context=None,
        messages=(
            ConversationMessage(
                message_id="message-1",
                session_id="session-1",
                command_id="command-1:user",
                sequence=1,
                role="user",
                content="hello",
            ),
        ),
    )


def _envelope() -> RunEnvelopeV2:
    messages = (
        RuntimeMessageInputV2(
            message_id="message-1", role="user", content="hello"
        ),
    )
    context_items = runtime_input_context_items(_admission())
    return RunEnvelopeV2.model_validate(
        {
            "runtime": {
                "runtime_id": "goose",
                "build_id": "goose-test",
                "config_digest": "a" * 64,
                "host_generation": "conversation-control-plane-v2",
            },
            "session_id": "conversation-session:session-1",
            "run_id": "conversation-run-1",
            "term_id": "conversation-term-1",
            "step_id": "conversation-step-1",
            "command_id": "runtime-command-1",
            "attempt": 0,
            "agent_id": "conversation-default-agent",
            "agent_role": "worker",
            "provider_ref": "provider-profile:provider-1",
            "model": "configured-model",
            "model_options_digest": "b" * 64,
            "message_snapshot_digest": canonical_runtime_input_digest(messages),
            "context": {
                "snapshot_ref": "conversation-session:session-1",
                "snapshot_digest": canonical_runtime_input_digest(context_items),
                "version": 0,
            },
            "context_budget": {
                "max_input_tokens": 4096,
                "reserved_output_tokens": 0,
                "protected_message_ids": (),
                "protected_prompt_section_ids": (),
                "compaction_policy": "none",
                "summary_ref": None,
            },
            "tool_manifest": (),
            "tool_manifest_digest": "e" * 64,
            "skill_pins": (),
            "skill_manifest_digest": "f" * 64,
            "plugin_pins": (),
            "plugin_manifest_digest": "1" * 64,
            "permission_policy_digest": "2" * 64,
            "workspace_grant": {
                "grant_id": "conversation-grant-1",
                "workspace_snapshot_ref": "empty-workspace-1",
                "readable_paths": (),
                "writable_paths": (),
                "command_policy": "deny",
                "network_policy": "deny",
                "expires_at_ms": 4_102_444_800_000,
            },
            "checkpoint_cursor": 0,
            "deadline_ms": 60_000,
            "traceparent": "conversation-trace-1",
            "extensions": {},
        }
    )


def test_runtime_execution_snapshot_materializes_query_input() -> None:
    snapshot = build_runtime_execution_snapshot(
        _admission(), QueryCommandV2(type="query.start", command_id="runtime-command-1"), _envelope()
    )

    runtime_input = RuntimeQueryInputV2.model_validate(snapshot["runtime_input"])
    envelope = RunEnvelopeV2.model_validate(snapshot["envelope"])

    assert runtime_input.messages[-1].content == "hello"
    assert runtime_input.message_snapshot_digest == envelope.message_snapshot_digest
    assert runtime_input.context_snapshot_digest == envelope.context.snapshot_digest
    assert runtime_input.prompt_manifest_digest == envelope.prompt_manifest_digest
    assert snapshot["selector"] == "goose"


def test_old_python_term_execution_is_read_only_compatible() -> None:
    assert read_runtime_execution(
        {"python_term_execution": {"envelope": {"command_id": "c1"}}}
    ) == {"envelope": {"command_id": "c1"}}


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("message_snapshot_digest", "c" * 64, "message snapshot digest"),
        ("prompt_manifest_digest", "e" * 64, "prompt manifest digest"),
    ],
)
def test_runtime_execution_snapshot_rejects_an_unbound_input_digest(
    field: str, value: str, match: str
) -> None:
    envelope = _envelope().model_copy(update={field: value})

    with pytest.raises(ValueError, match=match):
        build_runtime_execution_snapshot(
            _admission(),
            QueryCommandV2(type="query.start", command_id="runtime-command-1"),
            envelope,
        )


def test_runtime_execution_snapshot_rejects_an_unbound_context_digest() -> None:
    envelope = _envelope().model_copy(
        update={"context": _envelope().context.model_copy(update={"snapshot_digest": "d" * 64})}
    )

    with pytest.raises(ValueError, match="context snapshot digest"):
        build_runtime_execution_snapshot(
            _admission(),
            QueryCommandV2(type="query.start", command_id="runtime-command-1"),
            envelope,
        )
