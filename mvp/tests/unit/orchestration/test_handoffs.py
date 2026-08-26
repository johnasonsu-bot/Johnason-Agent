import pytest
from pydantic import ValidationError

from workbench.orchestration.handoffs import HandoffPublisher, NodeResult
from workbench.orchestration.sequential_contracts import (
    AgentBindingSnapshot,
    SequentialNodeSpec,
)


def node(agent_id: str, ordinal: int) -> SequentialNodeSpec:
    return SequentialNodeSpec(
        node_id=f"node.{agent_id}",
        ordinal=ordinal,
        kind="worker",
        binding=AgentBindingSnapshot(
            agent_id=agent_id,
            display_name=agent_id,
            role="worker",
            provider_id="lmstudio",
            model="local-agent",
            profile_version=1,
        ),
        instruction="执行任务",
    )


def test_handoff_contains_only_structured_public_result() -> None:
    handoff = HandoffPublisher().publish(
        node("writer", 0),
        node("architect", 1),
        NodeResult(
            objective="交付故事",
            summary="故事已完成",
            content_refs=("artifact.story",),
            evidence_refs=("evidence.word-count",),
            output_contract="生成动画 HTML",
        ),
        source_attempt=2,
    )

    assert handoff.source_node_id == "node.writer"
    assert handoff.target_node_id == "node.architect"
    assert handoff.source_attempt == 2
    assert not hasattr(handoff, "private_context")


def test_handoff_rejects_same_source_and_target() -> None:
    same = node("writer", 0)
    with pytest.raises(ValidationError):
        HandoffPublisher().publish(
            same,
            same,
            NodeResult(
                objective="交付故事",
                summary="故事已完成",
                output_contract="生成动画 HTML",
            ),
            source_attempt=1,
        )
