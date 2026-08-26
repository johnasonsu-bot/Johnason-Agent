import pytest

from workbench.orchestration.planning import PlanValidationError, PlanValidator
from workbench.orchestration.templates import SolutionTemplateCompiler

from tests.unit.orchestration.test_planning import catalog, resources


def test_research_template_is_deterministic() -> None:
    compiler = SolutionTemplateCompiler()
    inputs = {"goal": "形成竞争分析", "scope": "公开资料"}

    first = compiler.compile(
        "research-blueprint", "1.0.0", inputs, catalog(), resources()
    )
    second = compiler.compile(
        "research-blueprint", "1.0.0", inputs, catalog(), resources()
    )

    assert first.model_dump() == second.model_dump()
    assert PlanValidator().validate(first).parallel_worker_count == 4


def test_template_rejects_unknown_version_and_secret_like_inputs() -> None:
    compiler = SolutionTemplateCompiler()
    with pytest.raises(KeyError):
        compiler.compile(
            "research-blueprint", "2.0.0", {"goal": "分析"}, catalog(), resources()
        )
    with pytest.raises(PlanValidationError, match="secret-like"):
        compiler.compile(
            "research-blueprint",
            "1.0.0",
            {"goal": "分析", "api_key": "must-not-persist"},
            catalog(),
            resources(),
        )
