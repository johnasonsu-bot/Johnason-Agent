from __future__ import annotations

import pytest
from pydantic import ValidationError

from workbench.orchestration.compiler import (
    MentionSequenceCompiler,
    SolutionTemplateCompiler,
    UnknownAgentMention,
)
from workbench.orchestration.sequential_contracts import (
    AgentBindingSnapshot,
    ExecutionPlanDraft,
    ProgressReport,
    ReviewDecision,
)


EXACT_PROMPT = (
    "@产品经理 写一篇200字小说 "
    "@Supervisor 审核小说是否约200字且故事完整，不通过则打回产品经理 "
    "@架构师 改写成一个动画html "
    "@Verifier 验证HTML可独立打开且包含可见动画，不通过则打回架构师"
)


def binding(
    agent_id: str,
    display_name: str,
    role: str,
    *,
    enabled: bool = True,
    profile_version: int = 1,
    runtime_id: str | None = None,
) -> AgentBindingSnapshot:
    values = dict(
        agent_id=agent_id,
        display_name=display_name,
        role=role,
        provider_id="lmstudio" if role == "worker" else "deepseek-primary",
        model="local-agent" if role == "worker" else "deepseek-v4-flash",
        profile_version=profile_version,
        enabled=enabled,
    )
    if runtime_id is not None:
        values["runtime_id"] = runtime_id
    return AgentBindingSnapshot(**values)


@pytest.fixture
def bindings() -> tuple[AgentBindingSnapshot, ...]:
    return (
        binding("product-manager", "产品经理", "worker"),
        binding("supervisor", "Supervisor", "supervisor"),
        binding("architect", "架构师", "worker"),
        binding("verifier", "Verifier", "verifier"),
    )


def test_compiles_exact_sequence_and_review_targets(bindings) -> None:
    plan = MentionSequenceCompiler().compile(EXACT_PROMPT, bindings)

    assert [node.kind for node in plan.nodes] == [
        "worker",
        "supervisor",
        "worker",
        "verifier",
    ]
    assert [node.binding.agent_id for node in plan.nodes] == [
        "product-manager",
        "supervisor",
        "architect",
        "verifier",
    ]
    assert plan.nodes[1].review_target_id == plan.nodes[0].node_id
    assert plan.nodes[3].review_target_id == plan.nodes[2].node_id
    assert plan.nodes[0].instruction == "写一篇200字小说"
    assert plan.nodes[2].instruction == "改写成一个动画html"


def test_aliases_resolve_to_canonical_reviewer_bindings() -> None:
    bindings = (
        binding("writer", "作者", "worker"),
        binding("supervisor", "Supervisor", "supervisor"),
        binding("verifier", "Verifier", "verifier"),
    )

    plan = MentionSequenceCompiler().compile(
        "@作者 写故事 @监督者 审核作者 @验证者 核验作者",
        bindings,
    )

    assert [node.binding.agent_id for node in plan.nodes] == [
        "writer",
        "supervisor",
        "verifier",
    ]
    assert plan.nodes[1].review_target_id == plan.nodes[0].node_id
    assert plan.nodes[2].review_target_id == plan.nodes[0].node_id


def test_longest_display_name_wins() -> None:
    bindings = (
        binding("product", "产品", "worker"),
        binding("product-manager", "产品经理", "worker"),
    )

    plan = MentionSequenceCompiler().compile("@产品经理 写验收标准", bindings)

    assert [node.binding.agent_id for node in plan.nodes] == ["product-manager"]


@pytest.mark.parametrize(
    ("content", "bindings"),
    [
        ("@不存在 执行任务", (binding("worker", "执行者", "worker"),)),
        (
            "@执行者 执行任务",
            (binding("worker", "执行者", "worker", enabled=False),),
        ),
    ],
)
def test_rejects_unknown_or_disabled_mentions(content, bindings) -> None:
    with pytest.raises(UnknownAgentMention):
        MentionSequenceCompiler().compile(content, bindings)


def test_compiler_is_deterministic_and_binding_version_changes_identity(bindings) -> None:
    compiler = MentionSequenceCompiler()

    first = compiler.compile(EXACT_PROMPT, bindings)
    replay = compiler.compile(EXACT_PROMPT, bindings)
    changed = compiler.compile(
        EXACT_PROMPT,
        tuple(
            item.model_copy(update={"profile_version": 2})
            if item.agent_id == "architect"
            else item
            for item in bindings
        ),
    )

    assert replay == first
    assert changed.plan_id != first.plan_id
    assert [node.node_id for node in changed.nodes] != [
        node.node_id for node in first.nodes
    ]


def test_binding_and_plan_are_frozen(bindings) -> None:
    plan = MentionSequenceCompiler().compile(EXACT_PROMPT, bindings)

    with pytest.raises(ValidationError):
        plan.nodes[0].binding.model = "changed"
    with pytest.raises(ValidationError):
        plan.nodes += plan.nodes


def test_compiler_freezes_the_explicit_runtime_in_each_node_binding() -> None:
    source = binding("writer", "作者", "worker", runtime_id="goose")

    plan = MentionSequenceCompiler().compile("@作者 写故事", (source,))
    later_configuration = source.model_copy(update={"runtime_id": "dsh"})
    unspecified = MentionSequenceCompiler().compile(
        "@作者 写故事", (binding("writer", "作者", "worker"),)
    )

    assert plan.nodes[0].binding.runtime_id == "goose"
    assert later_configuration.runtime_id == "dsh"
    assert plan.nodes[0].binding is not later_configuration
    assert plan.plan_id != unspecified.plan_id


def test_unspecified_runtime_preserves_the_pre_runtime_binding_identity() -> None:
    plan = MentionSequenceCompiler().compile(
        "@作者 写故事", (binding("writer", "作者", "worker"),)
    )

    assert plan.plan_id == "plan.3dd3f9ea572916376b64672d2737e70c"
    assert plan.nodes[0].node_id == "node.e7358a62-5870-5295-b61b-264a7ecac71b"


def test_unspecified_runtime_keeps_the_legacy_binding_serialization_shape() -> None:
    unspecified = binding("writer", "作者", "worker")
    explicit = binding("writer", "作者", "worker", runtime_id="goose")

    assert "runtime_id" not in unspecified.model_dump(mode="json")
    assert '"runtime_id"' not in unspecified.model_dump_json()
    assert explicit.model_dump(mode="json")["runtime_id"] == "goose"


def test_historical_plan_without_runtime_id_loads_as_unspecified() -> None:
    plan = MentionSequenceCompiler().compile(
        "@作者 写故事", (binding("writer", "作者", "worker"),)
    )
    historical_document = plan.model_dump(mode="json")

    restored = ExecutionPlanDraft.model_validate(historical_document)

    assert restored.plan_id == plan.plan_id
    assert restored.nodes[0].binding.runtime_id is None


def test_plan_rejects_an_unknown_review_target_with_contract_error(bindings) -> None:
    plan = MentionSequenceCompiler().compile(EXACT_PROMPT, bindings)
    nodes = list(plan.nodes)
    nodes[1] = nodes[1].model_copy(update={"review_target_id": "node.missing"})

    with pytest.raises(ValidationError, match="review target must be a preceding"):
        ExecutionPlanDraft(
            plan_id=plan.plan_id,
            goal=plan.goal,
            nodes=tuple(nodes),
        )


def test_rejected_review_requires_evidence_findings_and_rework() -> None:
    with pytest.raises(ValidationError):
        ReviewDecision(
            reviewer_node_id="reviewer-1",
            reviewed_node_id="worker-1",
            reviewed_attempt=1,
            decision="rejected",
        )

    decision = ReviewDecision(
        reviewer_node_id="reviewer-1",
        reviewed_node_id="worker-1",
        reviewed_attempt=1,
        decision="rejected",
        findings=("故事不足200字",),
        evidence_refs=("artifact.story-check",),
        rework_instructions="补足故事正文",
    )
    assert decision.decision == "rejected"


def test_progress_percentage_requires_exact_deterministic_units() -> None:
    with pytest.raises(ValidationError):
        ProgressReport(
            graph_run_id="run-1",
            node_id="worker-1",
            agent_id="agent-1",
            attempt=1,
            stage="model_execution",
            status="running",
            label="生成故事",
            sequence=1,
            percentage=50,
        )

    report = ProgressReport(
        graph_run_id="run-1",
        node_id="worker-1",
        agent_id="agent-1",
        attempt=1,
        stage="model_execution",
        status="running",
        label="生成故事",
        sequence=1,
        completed_units=1,
        total_units=4,
        percentage=25,
    )
    assert report.percentage == 25


class _Template:
    def compile_intent(
        self,
        intent: str,
        template_id: str,
        template_version: str,
        bindings: tuple[AgentBindingSnapshot, ...],
    ) -> ExecutionPlanDraft:
        return MentionSequenceCompiler().compile(intent, bindings)


def test_solution_template_protocol_has_the_same_plan_return_type(bindings) -> None:
    template = _Template()

    assert isinstance(template, SolutionTemplateCompiler)
    assert isinstance(
        template.compile_intent(
            EXACT_PROMPT,
            "story-animation",
            "1.0.0",
            bindings,
        ),
        ExecutionPlanDraft,
    )
