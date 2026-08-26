import json

from workbench.agui.mapper import map_domain_event
from workbench.protocol.events import DomainEvent


def test_research_projection_excludes_prompts_and_credentials() -> None:
    event = DomainEvent.new(
        "research.branch.progress",
        "test",
        {
            "graph_run_id": "research-run.1",
            "node_id": "node.research",
            "branch_id": "research",
            "attempt": 1,
            "stage": "worker",
            "status": "completed",
            "evidence_refs": ["evidence:public:1"],
            "private_prompt": "private prompt",
            "api_key": "must-not-project",
        },
        run_id="research-run.1",
    )

    projected = map_domain_event(event)
    text = json.dumps(projected)

    assert projected[0]["name"] == "research.branch.progress"
    assert "evidence:public:1" in text
    assert "private prompt" not in text
    assert "must-not-project" not in text
