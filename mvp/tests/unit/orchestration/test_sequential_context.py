from workbench.orchestration.context import ContextResolver, PrivateMessage
from workbench.orchestration.handoffs import HandoffPublisher, NodeResult
from workbench.orchestration.project_context import (
    ProjectContextEntry,
    ProjectContextVersion,
)
from workbench.orchestration.sequential_contracts import (
    AgentBindingSnapshot,
    ReviewDecision,
    SequentialNodeSpec,
)


def node(agent_id: str, name: str, ordinal: int) -> SequentialNodeSpec:
    return SequentialNodeSpec(
        node_id=f"node.{agent_id}",
        ordinal=ordinal,
        kind="worker",
        binding=AgentBindingSnapshot(
            agent_id=agent_id,
            display_name=name,
            role="worker",
            provider_id="lmstudio",
            model="local-agent",
            profile_version=1,
        ),
        instruction="执行分配任务",
    )


def project_context() -> ProjectContextVersion:
    return ProjectContextVersion(
        project_id="project-1",
        version=3,
        created_at=1.0,
        entries=(
            ProjectContextEntry(
                key="requirements",
                value_ref="artifact.requirements-v2",
                source_ref="source.user-approved",
                verification_status="verified",
                visibility="shared",
            ),
            ProjectContextEntry(
                key="architect-rules",
                value_ref="artifact.architecture-rules",
                source_ref="source.architecture-owner",
                verification_status="verified",
                visibility="agent:architect",
            ),
            ProjectContextEntry(
                key="writer-notes",
                value_ref="artifact.writer-notes",
                source_ref="source.writer",
                verification_status="verified",
                visibility="agent:product-manager",
            ),
        ),
    )


def test_architect_context_excludes_other_agent_private_history() -> None:
    writer = node("product-manager", "产品经理", 0)
    architect = node("architect", "架构师", 1)
    handoff = HandoffPublisher().publish(
        writer,
        architect,
        NodeResult(
            objective="将故事交给架构师",
            summary="已发布小说",
            content_refs=("artifact.story",),
            evidence_refs=("evidence.story",),
            output_contract="输出动画 HTML",
        ),
        source_attempt=1,
    )

    package = ContextResolver().build(
        architect,
        project_context(),
        private_messages=(
            PrivateMessage(agent_id="architect", content="架构师私有历史"),
            PrivateMessage(agent_id="product-manager", content="产品经理未发布草稿"),
        ),
        handoffs=(handoff,),
        rework=None,
    )

    assert "架构师私有历史" in package.rendered_prompt
    assert "产品经理未发布草稿" not in package.rendered_prompt
    assert "已发布小说" in package.rendered_prompt
    assert package.project_context_version == 3
    assert package.project_sources == (
        "source.user-approved",
        "source.architecture-owner",
    )
    assert "artifact.writer-notes" not in package.rendered_prompt


def test_context_package_contains_references_not_artifact_bodies() -> None:
    architect = node("architect", "架构师", 1)

    package = ContextResolver().build(
        architect,
        project_context(),
        private_messages=(),
        handoffs=(),
        rework=None,
    )

    assert "artifact.requirements-v2" in package.rendered_prompt
    assert package.agent_id == "architect"


def test_rework_is_visible_only_to_the_reviewed_node() -> None:
    writer = node("product-manager", "产品经理", 0)
    architect = node("architect", "架构师", 1)
    rejection = ReviewDecision(
        reviewer_node_id="node.supervisor",
        reviewed_node_id=writer.node_id,
        reviewed_attempt=1,
        decision="rejected",
        findings=("故事不足200字",),
        evidence_refs=("evidence.word-count",),
        rework_instructions="补足故事",
    )

    writer_context = ContextResolver().build(
        writer, project_context(), (), (), rejection
    )
    architect_context = ContextResolver().build(
        architect, project_context(), (), (), rejection
    )

    assert "补足故事" in writer_context.rendered_prompt
    assert "补足故事" not in architect_context.rendered_prompt


def test_reviewer_context_declares_the_exact_structured_decision_identity() -> None:
    reviewer = SequentialNodeSpec(
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
        instruction="审核故事",
        review_target_id="node.product-manager",
    )

    package = ContextResolver().build(
        reviewer, project_context(), (), (), None, attempt=2
    )

    assert "reviewed_node_id=node.product-manager" in package.rendered_prompt
    assert "reviewed_attempt=2" in package.rendered_prompt
    assert '"decision":"approved|rejected|needs_human"' in package.rendered_prompt
