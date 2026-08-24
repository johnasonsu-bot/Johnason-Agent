import json

import pytest

from workbench.orchestration.research_graph import LocalReviewDecision
from workbench.orchestration.review import (
    InvalidReviewDecision,
    ResearchDecisionParser,
)


def test_research_parser_accepts_one_owned_structured_decision() -> None:
    value = ResearchDecisionParser().parse_local(
        json.dumps(
            {
                "reviewed_branch_id": "fact_check",
                "reviewed_attempt": 2,
                "decision": "approved",
                "evidence_refs": ["evidence:fact-check:2"],
            }
        ),
        branch="fact_check",
        attempt=2,
    )

    assert isinstance(value, LocalReviewDecision)
    assert value.decision == "approved"


def test_research_parser_rejects_prose_or_stale_attempt() -> None:
    payload = json.dumps(
        {
            "reviewed_branch_id": "fact_check",
            "reviewed_attempt": 1,
            "decision": "approved",
            "evidence_refs": ["evidence:fact-check:1"],
        }
    )
    with pytest.raises(InvalidReviewDecision):
        ResearchDecisionParser().parse_local(
            f"结果如下：{payload}", branch="fact_check", attempt=1
        )
    with pytest.raises(InvalidReviewDecision):
        ResearchDecisionParser().parse_local(
            payload, branch="fact_check", attempt=2
        )
