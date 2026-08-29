"""Control-plane routing for the real Python Term Host v2 runtime."""

from __future__ import annotations

from pathlib import Path

import pytest
from agents.testing import ScriptedModel

import workbench.main as main
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from workbench.runtime.engine_host.v2.contracts import QueryCommandV2, RunEnvelopeV2
from workbench.runtime.engine_host.v2 import registry as registry_module
from workbench.runtime.engine_host.v2.registry import NoConformantRuntime, RuntimeRegistryV2
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
    assert enabled.state.python_term_runtime_gate_metadata.build_id == (
        enabled.state.python_term_runtime.build_id
    )


@pytest.mark.parametrize("value", ("1", "0", "TRUE", "False", " true", "false "))
def test_python_term_flag_rejects_non_strict_boolean_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Catches truthy environment spellings silently enabling a runtime route."""
    monkeypatch.setenv("WORKBENCH_PYTHON_TERM_RUNTIME_ENABLED", value)

    with pytest.raises(ValueError, match="python term runtime enabled must be true or false"):
        main._settings_from_environment(WorkbenchSettings(runtime_dir=tmp_path))


def test_new_explicit_python_term_query_pins_the_real_runtime_and_build(
    tmp_path: Path,
) -> None:
    """Catches a new explicit query being admitted without a durable Python Term pin."""
    registry, runtime = _routable_python_term(tmp_path)
    envelope = _python_term_envelope(runtime)
    command = QueryCommandV2(type="query.start", command_id=envelope.command_id)

    selected = registry.route_python_term_query(
        command,
        envelope,
        registry_module.RuntimeGateMetadataV2.from_capabilities(runtime.capabilities),
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
    gate_metadata = registry_module.RuntimeGateMetadataV2.from_capabilities(
        runtime.capabilities
    )
    registry.route_python_term_query(command, envelope, gate_metadata)
    registry.disable("python-term")
    registry.register(runtime_capabilities("fallback", query=True, model=True))

    resumed = registry.route_python_term_query(command, envelope, gate_metadata)

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
    registry.route_python_term_query(
        command,
        envelope,
        registry_module.RuntimeGateMetadataV2.from_capabilities(runtime.capabilities),
    )
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
        command,
        envelope,
        registry_module.RuntimeGateMetadataV2.from_capabilities(replacement),
    )

    assert (resumed.runtime_id, resumed.build_id, resumed.state) == (
        "python-term",
        runtime.build_id,
        "pinned",
    )


def test_new_query_fails_closed_without_matching_build_and_capability_metadata(
    tmp_path: Path,
) -> None:
    """Catches Python Term routing without independently verifiable runtime metadata."""
    registry, runtime = _routable_python_term(tmp_path)
    envelope = _python_term_envelope(runtime)
    command = QueryCommandV2(type="query.start", command_id=envelope.command_id)
    metadata = registry_module.RuntimeGateMetadataV2.from_capabilities(
        runtime.capabilities
    ).model_copy(update={"build_id": "other-build"})

    with pytest.raises(NoConformantRuntime, match="metadata"):
        registry.route_python_term_query(command, envelope, metadata)

    assert registry.repository.get_pin(envelope.command_id) is None
