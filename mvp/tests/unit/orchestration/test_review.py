import pytest

from workbench.orchestration.review import InvalidReviewDecision, ReviewDecisionParser
from workbench.orchestration.sequential_contracts import (
    AgentBindingSnapshot,
    SequentialNodeSpec,
)


def reviewer() -> SequentialNodeSpec:
    return SequentialNodeSpec(
        node_id="node.supervisor",
        ordinal=1,
        kind="supervisor",
        binding=AgentBindingSnapshot(
            agent_id="supervisor",
            display_name="Supervisor",
            role="supervisor",
            provider_id="deepseek-primary",
            model="deepseek-v4-flash",
            profile_version=1,
        ),
        instruction="审核小说",
        review_target_id="node.writer",
    )


def test_parser_accepts_one_fenced_structured_rejection() -> None:
    decision = ReviewDecisionParser().parse(
        """```json
        {"reviewed_node_id":"node.writer","reviewed_attempt":1,
         "decision":"rejected","findings":["不足200字"],
         "evidence_refs":["evidence.word-count"],
         "rework_instructions":"补足故事"}
        ```""",
        reviewer(),
        attempt=1,
    )

    assert decision.reviewer_node_id == "node.supervisor"
    assert decision.decision == "rejected"


@pytest.mark.parametrize(
    "text",
    [
        '{"reviewed_node_id":"node.writer","reviewed_attempt":1,"decision":"rejected"}',
        '{"reviewed_node_id":"node.other","reviewed_attempt":1,"decision":"approved","evidence_refs":["evidence.ok"]}',
        '{"reviewed_node_id":"node.writer","reviewed_attempt":2,"decision":"approved","evidence_refs":["evidence.ok"]}',
        '{} trailing text',
    ],
)
def test_parser_rejects_incomplete_mismatched_or_ambiguous_decision(text: str) -> None:
    with pytest.raises(InvalidReviewDecision):
        ReviewDecisionParser().parse(text, reviewer(), attempt=1)
