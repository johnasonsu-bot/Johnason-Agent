from pathlib import Path

from workbench.artifacts.store import ArtifactStore
from workbench.orchestration.artifacts import (
    ResearchReportIdentifiers,
    ResearchReportPublisher,
)
from workbench.orchestration.research_graph import ClaimEvidence, MergeResult


def test_research_report_maps_every_claim_to_evidence_and_keeps_metadata_narrow(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "workbench.sqlite", tmp_path / "artifacts")
    artifact = ResearchReportPublisher(store).publish(
        "分析公开市场",
        MergeResult(
            summary="形成经过审核的结论",
            claims=(
                ClaimEvidence(
                    claim="公开市场保持增长",
                    evidence_refs=("evidence:market:1",),
                ),
            ),
            exclusions=("未核验预测",),
            limitations=("仅使用公开资料",),
            open_questions=("下一期数据",),
            artifact_ref="artifact:pending",
        ),
        ResearchReportIdentifiers(
            graph_run_id="research-run.1", plan_id="research-plan.1", version=1
        ),
    )

    opened = store.open(artifact.artifact_id)
    text = (opened.content or b"").decode()
    assert opened.media_type == "text/markdown"
    assert "公开市场保持增长" in text
    assert "evidence:market:1" in text
    assert set(opened.metadata) == {
        "graph_run_id",
        "plan_id",
        "version",
        "artifact_kind",
    }
