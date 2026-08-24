"""Versioned deterministic Solution Template compiler."""

from __future__ import annotations

from typing import Any

from workbench.orchestration.planning import (
    AgentCatalog,
    ResearchPlanDraft,
    ResearchResources,
    build_research_plan,
    reject_secret_like_fields,
)


class SolutionTemplateCompiler:
    SUPPORTED = {("research-blueprint", "1.0.0")}

    def compile(
        self,
        template_id: str,
        template_version: str,
        inputs: dict[str, Any],
        catalog: AgentCatalog,
        resources: ResearchResources,
    ) -> ResearchPlanDraft:
        if (template_id, template_version) not in self.SUPPORTED:
            raise KeyError((template_id, template_version))
        reject_secret_like_fields(inputs)
        goal = inputs.get("goal")
        if not isinstance(goal, str):
            raise ValueError("research template requires one goal")
        return build_research_plan(
            goal=goal,
            catalog=catalog,
            resources=resources,
            compiler_source="template",
            compiler_ref=f"template.{template_id}.{template_version}",
            identity_material={
                "template_id": template_id,
                "template_version": template_version,
                "inputs": inputs,
            },
        )
