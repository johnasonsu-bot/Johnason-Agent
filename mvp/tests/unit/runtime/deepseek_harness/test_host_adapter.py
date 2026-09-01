from __future__ import annotations

import pytest

from tests.fixtures.host_v2 import run_envelope
from workbench.runtime.deepseek_harness.host_adapter import (
    DeepSeekHarnessHostAdapter,
    DeepSeekHostAdapterError,
    DeepSeekPreparedQuery,
)
from workbench.runtime.deepseek_harness.prompt_sections import DeepSeekPromptSection


def _section(
    section_id: str,
    *,
    priority: int,
    stable_order: int,
    content: str,
) -> DeepSeekPromptSection:
    return DeepSeekPromptSection(
        section_id=section_id,
        namespace="host",
        priority=priority,
        stable_order=stable_order,
        content=content,
        content_reference=None,
        visibility="model",
        mutable=False,
        source_digest="a" * 64,
    )


def _adapter() -> DeepSeekHarnessHostAdapter:
    return DeepSeekHarnessHostAdapter(
        runtime_id="deepseek-harness",
        build_id="deepseek-harness:test",
    )


def _envelope():
    return run_envelope(runtime_id="deepseek-harness")


def test_prepare_creates_one_deterministic_secret_free_query() -> None:
    """Fails if preparation stops retaining a stable, secret-free query snapshot."""
    adapter = _adapter()
    envelope = _envelope()
    sections = (
        _section("tools", priority=100, stable_order=0, content="Use tools safely."),
        _section("goal", priority=0, stable_order=1, content="Answer the user."),
    )

    first = adapter.prepare(envelope, sections)
    second = adapter.prepare(envelope, sections)

    assert isinstance(first, DeepSeekPreparedQuery)
    assert first == second
    assert first.provider_ref == "provider-1"
    assert first.model == "test-model"
    assert first.context_digest == "a" * 64
    assert first.tool_manifest_digest == "b" * 64
    assert first.skill_manifest_digest == "2" * 64
    assert first.plugin_manifest_digest == "4" * 64
    assert first.prompt_digest == "95499911eb678cf449dd8e3379360734e888d263d3ee084864da2309d79924bf"
    assert first.evidence_digest == "3b64bed28af9c1083d406d3931655c3fc99b68c347d2d1af61ad69f12ea7cfba"
    assert first.command_identity == {
        "protocol_version": "2.0",
        "runtime_id": "deepseek-harness",
        "build_id": "deepseek-harness:test",
        "host_generation": "host-a",
        "session_id": "session-1",
        "run_id": "run-1",
        "term_id": "term-1",
        "step_id": "step-1",
        "command_id": "command-1",
        "attempt": 0,
    }
    assert "secret" not in repr(first).casefold()
    assert "token" not in repr(first).casefold()


def test_prepare_preserves_the_bridge_registration_order_and_freezes_it() -> None:
    """Fails if DSH sees input order or a mutable registration after preparation."""
    prepared = _adapter().prepare(
        _envelope(),
        (
            _section("tools", priority=100, stable_order=0, content="Use tools safely."),
            _section("goal", priority=0, stable_order=20, content="Answer the user."),
            _section("policy", priority=0, stable_order=10, content="Follow policy."),
        ),
    )

    assert prepared.prompt_registrations == (
        {"name": "host:policy", "order": 0, "text": "Follow policy."},
        {"name": "host:goal", "order": 0, "text": "Answer the user."},
        {"name": "host:tools", "order": 100, "text": "Use tools safely."},
    )
    with pytest.raises(TypeError):
        prepared.prompt_registrations[0]["text"] = "drift"  # type: ignore[index]
    with pytest.raises(TypeError):
        prepared.command_identity["command_id"] = "drift"  # type: ignore[index]


def test_prepare_rejects_a_runtime_or_build_other_than_its_bound_host() -> None:
    """Fails if a prepared query can cross a runtime/build identity boundary."""
    adapter = _adapter()
    wrong_runtime = run_envelope(runtime_id="another-runtime")
    wrong_build = _envelope().model_copy(
        update={
            "runtime": {
                "runtime_id": "deepseek-harness",
                "build_id": "deepseek-harness:other-build",
                "config_digest": "c" * 64,
                "host_generation": "host-a",
            }
        }
    )

    with pytest.raises(DeepSeekHostAdapterError, match="runtime/build"):
        adapter.prepare(wrong_runtime, ())
    with pytest.raises(DeepSeekHostAdapterError, match="runtime/build"):
        adapter.prepare(wrong_build, ())


def test_prepare_preserves_an_unresolved_provider_reference_without_credentials() -> None:
    """Fails if preparation resolves provider secrets or rewrites opaque references."""
    envelope = _envelope().model_copy(
        update={"provider_ref": "provider-profile:not-yet-resolved"}
    )

    prepared = _adapter().prepare(envelope, ())

    assert prepared.provider_ref == "provider-profile:not-yet-resolved"
    assert "credential" not in prepared.__dataclass_fields__
    assert "api_key" not in prepared.__dataclass_fields__
    assert "authorization" not in prepared.__dataclass_fields__


def test_prepare_accepts_only_a_validated_envelope_and_normalized_sections() -> None:
    """Fails if unknown raw inputs can bypass Host v2 and PromptSection validation."""
    adapter = _adapter()

    with pytest.raises(DeepSeekHostAdapterError, match="RunEnvelopeV2"):
        adapter.prepare({"unknown": "input"}, ())  # type: ignore[arg-type]
    with pytest.raises(DeepSeekHostAdapterError, match="normalized"):
        adapter.prepare(_envelope(), ({"unknown": "section"},))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        adapter.prepare(_envelope(), (), unknown=True)  # type: ignore[call-arg]
