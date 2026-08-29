"""Control-plane routing for the real Python Term Host v2 runtime."""

from __future__ import annotations

from pathlib import Path

import pytest
from agents.testing import ScriptedModel

import workbench.main as main
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from workbench.runtime.engine_host.v2.contracts import QueryCommandV2, RunEnvelopeV2
from workbench.runtime.engine_host.v2 import registry as registry_module
from workbench.runtime.engine_host.v2.registry import (
    NoConformantRuntime,
    RuntimeRegistryV2,
)
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.runtime.python_term.repository import PythonTermRepository
from workbench.runtime.python_term.runtime import PythonTermRuntime
from workbench.runtime.python_term.sdk_adapter import FixedModelProvider
from workbench.settings import WorkbenchSettings


def _python_term_envelope(
    runtime: PythonTermRuntime, *, command_id: str = "python-term-command"
) -> RunEnvelopeV2:
    """Build a query identity that asks for no capability Python Term lacks."""
    document = run_envelope(runtime_id="python-term", command_id=command_id).model_dump(
        mode="json"
    )
    document["runtime"]["build_id"] = runtime.build_id
    document["tool_manifest"] = []
    document["tool_manifest_digest"] = "a" * 64
    document["skill_pins"] = []
    document["skill_manifest_digest"] = "b" * 64
    document["plugin_pins"] = []
    document["plugin_manifest_digest"] = "c" * 64
    document["context_budget"]["compaction_policy"] = "none"
    document["context_budget"]["protected_prompt_section_ids"] = []
    document["workspace_grant"]["readable_paths"] = []
    document["workspace_grant"]["writable_paths"] = []
    document["workspace_grant"]["command_policy"] = "deny"
    document["workspace_grant"]["network_policy"] = "deny"
    return RunEnvelopeV2.model_validate(document)


def _routable_python_term(tmp_path: Path) -> tuple[RuntimeRegistryV2, PythonTermRuntime]:
    database = tmp_path / "routing.sqlite"
    runtime = PythonTermRuntime(
        PythonTermRepository(database),
        model_provider=FixedModelProvider(
            {("provider-1", "test-model"): ScriptedModel([])}
        ),
    )
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    runtime.register(registry)
    return registry, runtime


def test_python_term_flag_defaults_to_disabled_and_registers_the_real_runtime_only_when_enabled(
    tmp_path: Path,
) -> None:
    """Catches default startup making Python Term routable without an explicit flag."""
    disabled = main.build_app(WorkbenchSettings(runtime_dir=tmp_path / "disabled"))
    enabled = main.build_app(
        WorkbenchSettings(
            runtime_dir=tmp_path / "enabled",
            engine_host_v2_enabled=True,
            python_term_runtime_enabled=True,
        )
    )

    assert disabled.state.runtime_registry_v2 is None
    assert enabled.state.runtime_registry_v2 is not None
    registered = enabled.state.runtime_registry_v2.snapshot()
    assert [(item.runtime_id, item.capabilities) for item in registered] == [
        ("python-term", ("checkpoints", "event_cursor"))
    ]
    assert enabled.state.python_term_runtime.runtime_id == "python-term"
    assert not hasattr(enabled.state, "python_term_runtime_gate_metadata")
    assert not hasattr(registry_module, "RuntimeGateMetadataV2")


@pytest.mark.parametrize("value", ("1", "0", "TRUE", "False", " true", "false "))
def test_python_term_flag_rejects_non_strict_boolean_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Catches truthy environment spellings silently enabling a runtime route."""
    monkeypatch.setenv("WORKBENCH_PYTHON_TERM_RUNTIME_ENABLED", value)

    with pytest.raises(ValueError, match="python term runtime enabled must be true or false"):
        main._settings_from_environment(WorkbenchSettings(runtime_dir=tmp_path))


def _task7_gate_proof(runtime: PythonTermRuntime):
    """Private deterministic fixture standing in for Task 7's fixed verifier."""
    return registry_module._issue_python_term_gate_proof_for_task7(  # type: ignore[attr-defined]
        source_revision="task7-source-r1",
        capabilities=runtime.capabilities,
        gate_result_digest="7" * 64,
    )


def test_public_lookalike_metadata_cannot_admit_or_pin_python_term(
    tmp_path: Path,
) -> None:
    """A caller-shaped record is not a Task 7 gate proof and cannot pin."""
    registry, runtime = _routable_python_term(tmp_path)
    envelope = _python_term_envelope(runtime)
    command = QueryCommandV2(type="query.start", command_id=envelope.command_id)

    class LookalikeGateMetadata:
        runtime_id = "python-term"
        build_id = runtime.build_id
        protocol_version = "2.0"
        capability_digest = "0" * 64
        source_revision = "caller-controlled"
        gate_result_digest = "1" * 64

    with pytest.raises(NoConformantRuntime):
        registry.route_python_term_query(
            command, envelope, gate_proof=LookalikeGateMetadata()
        )

    assert registry.repository.get_pin(envelope.command_id) is None


def test_fixed_task7_gate_verifier_can_admit_and_pin_the_real_runtime(
    tmp_path: Path,
) -> None:
    """Only the private fixed issuer/verifier seam can produce a durable pin."""
    registry, runtime = _routable_python_term(tmp_path)
    envelope = _python_term_envelope(runtime)
    command = QueryCommandV2(type="query.start", command_id=envelope.command_id)

    selected = registry.route_python_term_query(
        command, envelope, gate_proof=_task7_gate_proof(runtime)
    )

    assert selected.runtime_id == "python-term"
    assert selected.build_id == runtime.build_id
    assert selected.command_id == envelope.command_id
    pin = registry.repository.get_pin(envelope.command_id)
    assert pin is not None
    assert (pin.runtime_id, pin.runtime_build_id) == ("python-term", runtime.build_id)


def test_accepted_python_term_query_never_falls_back_when_the_live_runtime_disappears(
    tmp_path: Path,
) -> None:
    """Catches an accepted command being rerouted to a later fallback runtime."""
    registry, runtime = _routable_python_term(tmp_path)
    envelope = _python_term_envelope(runtime)
    command = QueryCommandV2(type="query.start", command_id=envelope.command_id)
    proof = _task7_gate_proof(runtime)
    registry.route_python_term_query(command, envelope, gate_proof=proof)
    registry.disable("python-term")
    registry.register(runtime_capabilities("fallback", query=True, model=True))

    resumed = registry.route_python_term_query(command, envelope, gate_proof=proof)

    assert (resumed.runtime_id, resumed.build_id, resumed.state) == (
        "python-term",
        runtime.build_id,
        "pinned",
    )


def test_accepted_python_term_query_recovers_its_pin_after_live_build_metadata_changes(
    tmp_path: Path,
) -> None:
    """Catches resume consulting current gate metadata instead of the durable build pin."""
    registry, runtime = _routable_python_term(tmp_path)
    envelope = _python_term_envelope(runtime)
    command = QueryCommandV2(type="query.start", command_id=envelope.command_id)
    registry.route_python_term_query(command, envelope, gate_proof=_task7_gate_proof(runtime))
    replacement = runtime_capabilities(
        "python-term",
        build_id="python-term:replacement",
        query=True,
        model=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )
    registry.register(replacement)

    resumed = registry.route_python_term_query(
        command, envelope, gate_proof=_task7_gate_proof(runtime)
    )

    assert (resumed.runtime_id, resumed.build_id, resumed.state) == (
        "python-term",
        runtime.build_id,
        "pinned",
    )


@pytest.mark.parametrize(
    ("overrides", "required_capability"),
    (
        ({"context_budget.compaction_policy": "summarize"}, "compaction"),
        ({"context_budget.protected_prompt_section_ids": ("section-1",)}, "prompt_sections"),
        (
            (
                {
                    "plugin_pins": (
                        {
                            "package_id": "plugin-1",
                            "version": "1",
                            "source_revision": "revision-1",
                            "digest": "3" * 64,
                            "capabilities": (),
                            "order": 0,
                        },
                    ),
                    "plugin_manifest_digest": "4" * 64,
                }
            ),
            "plugins",
        ),
        ({"workspace_grant.readable_paths": ("/workspace/read",)}, "workspace"),
    ),
)
def test_complete_envelope_requirements_fail_closed_before_pin(
    tmp_path: Path,
    overrides: dict[str, object],
    required_capability: str,
) -> None:
    """Every used normalized envelope feature must be advertised before pinning."""
    database = tmp_path / "requirements.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    capabilities = runtime_capabilities(
        "python-term",
        query=True,
        model=True,
        checkpoints=True,
        streaming=True,
        event_cursor=True,
    )
    registry.register(capabilities)
    document = _python_term_envelope(
        PythonTermRuntime(PythonTermRepository(database)),
        command_id=f"required-{required_capability}",
    ).model_dump(mode="json")
    document["runtime"]["build_id"] = capabilities.build_id
    for path, value in overrides.items():
        target = document
        *parents, leaf = path.split(".")
        for parent in parents:
            target = target[parent]  # type: ignore[assignment,index]
        target[leaf] = value  # type: ignore[index]
    envelope = RunEnvelopeV2.model_validate(document)
    command = QueryCommandV2(type="query.start", command_id=envelope.command_id)
    proof = registry_module._issue_python_term_gate_proof_for_task7(  # type: ignore[attr-defined]
        source_revision="task7-source-r1",
        capabilities=capabilities,
        gate_result_digest="7" * 64,
    )

    with pytest.raises(NoConformantRuntime):
        registry.route_python_term_query(command, envelope, gate_proof=proof)

    assert registry.repository.get_pin(envelope.command_id) is None
