from __future__ import annotations

import pytest

import workbench.runtime.deepseek_harness as deepseek_harness
from workbench.runtime.deepseek_harness.prompt_sections import (
    DeepSeekPromptSection,
    PromptSectionBridge,
    PromptSectionBridgeError,
)


def _section(
    section_id: str,
    *,
    namespace: str = "host",
    priority: int = 0,
    stable_order: int = 0,
    content: str | None = None,
    content_reference: str | None = None,
    visibility: str = "model",
    mutable: bool = False,
) -> DeepSeekPromptSection:
    body = content if content is not None else f"content for {section_id}"
    return DeepSeekPromptSection(
        section_id=section_id,
        namespace=namespace,
        priority=priority,
        stable_order=stable_order,
        content=body if content_reference is None else None,
        content_reference=content_reference,
        visibility=visibility,
        mutable=mutable,
        source_digest="a" * 64,
    )


def test_prompt_section_bridge_is_available_from_the_runtime_package() -> None:
    assert deepseek_harness.DeepSeekPromptSection is DeepSeekPromptSection
    assert deepseek_harness.PromptSectionBridge is PromptSectionBridge
    assert deepseek_harness.PromptSectionBridgeError is PromptSectionBridgeError


def test_bridge_maps_normalized_sections_to_dsh_registration_order() -> None:
    bridge = PromptSectionBridge()

    assembly = bridge.assemble(
        (
            _section("tooling", priority=100, stable_order=0),
            _section("goal", priority=0, stable_order=20),
            _section("policy-b", priority=0, stable_order=10),
            _section("policy-a", priority=0, stable_order=10),
        )
    )

    assert assembly.registrations == (
        {"name": "host:policy-a", "order": 0, "text": "content for policy-a"},
        {"name": "host:policy-b", "order": 0, "text": "content for policy-b"},
        {"name": "host:goal", "order": 0, "text": "content for goal"},
        {"name": "host:tooling", "order": 100, "text": "content for tooling"},
    )
    assert assembly.evidence.section_order == (
        "host:policy-a",
        "host:policy-b",
        "host:goal",
        "host:tooling",
    )


def test_prompt_digest_is_stable_for_equivalent_input_order() -> None:
    bridge = PromptSectionBridge()
    first = _section("first", priority=-100, stable_order=5)
    second = _section(
        "second",
        namespace="project",
        priority=0,
        stable_order=7,
        visibility="private",
        mutable=True,
    )

    forward = bridge.assemble((first, second))
    reverse = bridge.assemble((second, first))

    assert forward.evidence == reverse.evidence
    assert len(forward.evidence.prompt_digest) == 64


def test_namespace_breaks_an_otherwise_identical_ordering_tie() -> None:
    bridge = PromptSectionBridge()
    agent = _section("goal", namespace="agent", stable_order=10)
    project = _section("goal", namespace="project", stable_order=10)

    forward = bridge.assemble((agent, project))
    reverse = bridge.assemble((project, agent))

    assert forward.evidence == reverse.evidence
    assert forward.evidence.section_order == ("agent:goal", "project:goal")


def test_prompt_digest_changes_when_model_facing_content_changes() -> None:
    bridge = PromptSectionBridge()

    before = bridge.assemble((_section("goal", content="Build the artifact."),))
    after = bridge.assemble((_section("goal", content="Review the artifact."),))

    assert before.evidence.prompt_digest == (
        "1df1d37b5fc3d1726c7b0e846f67adfbe3bd7c2c9048d0bfb97105399cadaf36"
    )
    assert before.evidence.prompt_digest != after.evidence.prompt_digest


def test_registration_snapshot_cannot_drift_after_digesting() -> None:
    """Catches mutable DSH registrations diverging from retained evidence."""
    assembly = PromptSectionBridge().assemble((_section("goal"),))

    with pytest.raises(TypeError):
        assembly.registrations[0]["text"] = "mutated after digest"  # type: ignore[index]


def test_bridge_rejects_duplicate_dsh_section_names() -> None:
    bridge = PromptSectionBridge()

    with pytest.raises(PromptSectionBridgeError, match="duplicate prompt section"):
        bridge.assemble(
            (
                _section("goal", namespace="agent"),
                _section("goal", namespace="agent", stable_order=1),
            )
        )


def test_bridge_rejects_unresolved_content_references() -> None:
    bridge = PromptSectionBridge()

    with pytest.raises(PromptSectionBridgeError, match="content reference"):
        bridge.assemble(
            (_section("workspace", content_reference="context://workspace/current"),)
        )
