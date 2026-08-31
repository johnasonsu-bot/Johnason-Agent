from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

import workbench.main as main
from tests.fixtures.host_v2 import run_envelope, runtime_capabilities
from workbench.runtime.engine_host.v2.registry import (
    NoConformantRuntime,
    RuntimeRegistryIntegrityError,
    RuntimeRegistryV2,
    RuntimeRequirementsV2,
)
from workbench.runtime.engine_host.v2.repository import RuntimeV2Repository
from workbench.settings import RuntimeProcessConfig, WorkbenchSettings


class _PythonRunner:
    async def execute_step(self, run_id: str, step_id: str) -> None:
        del run_id, step_id

    async def run_turn(self, command):
        if False:
            yield command


def test_registry_selects_only_conformant_runtime(tmp_path: Path) -> None:
    """Catches selection of a preferred runtime that misses a required capability."""
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "state.sqlite"))
    registry.register(
        runtime_capabilities(
            "python", tools=True, skills=True, workspace=True, query=True
        )
    )
    registry.register(
        runtime_capabilities(
            "goose", tools=True, skills=False, workspace=True, query=True
        )
    )

    selected = registry.select(
        RuntimeRequirementsV2(
            preferred_runtime_id="goose",
            tools=True,
            skills=True,
            workspace=True,
            query=True,
        )
    )

    assert selected.runtime_id == "python"
    assert selected.build_id == "python:test"
    assert selected.capabilities == ("query", "tools", "skills", "workspace")


def test_registry_fails_closed_without_conformant_runtime(tmp_path: Path) -> None:
    """Catches a capability fallback that admits a nonconformant runtime."""
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "state.sqlite"))
    registry.register(runtime_capabilities("goose", query=True, tools=False))

    with pytest.raises(NoConformantRuntime):
        registry.select(RuntimeRequirementsV2(query=True, tools=True))


def test_accepted_pin_never_reroutes_when_registry_changes(tmp_path: Path) -> None:
    """Catches a durable command resume being re-selected after admission."""
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "state.sqlite"))
    registry.register(runtime_capabilities("goose", query=True))
    selection = registry.select_and_pin(
        run_envelope(runtime_id="goose", command_id="command-1"),
        RuntimeRequirementsV2(preferred_runtime_id="goose", query=True),
    )

    registry.disable("goose")

    assert registry.resume(selection.command_id).runtime_id == "goose"


def test_resume_uses_the_pinned_capability_snapshot_after_live_registry_changes(
    tmp_path: Path,
) -> None:
    """Catches resume replacing admitted capabilities with a later live snapshot."""
    database = tmp_path / "state.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(runtime_capabilities("goose", query=True, model=True))
    registry.select_and_pin(
        run_envelope(runtime_id="goose", command_id="command-capabilities"),
        RuntimeRequirementsV2(query=True, model=True),
    )

    registry.register(runtime_capabilities("goose", query=False, tools=True))
    resumed = RuntimeRegistryV2(RuntimeV2Repository(database)).resume(
        "command-capabilities"
    )

    assert resumed.runtime_id == "goose"
    assert resumed.capabilities == ("query", "model")


def test_registry_isolates_a_tampered_capability_snapshot(tmp_path: Path) -> None:
    """Catches a tampered registration remaining eligible for admission."""
    database = tmp_path / "state.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(runtime_capabilities("python", query=True))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runtime_v2_registrations SET capabilities_json = ? WHERE runtime_id = ?",
            ('{"runtime_id":"python"}', "python"),
        )

    with pytest.raises(NoConformantRuntime):
        registry.select(RuntimeRequirementsV2(query=True))
    assert registry.snapshot() == ()


def test_corrupt_registration_row_is_isolated_from_healthy_selection(
    tmp_path: Path,
) -> None:
    """Catches one corrupt runtime preventing every healthy runtime from admission."""
    database = tmp_path / "state.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(runtime_capabilities("alpha", query=True))
    registry.register(runtime_capabilities("broken", query=True))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runtime_v2_registrations SET capabilities_json = ? WHERE runtime_id = ?",
            ("{}", "broken"),
        )

    selected = registry.select(RuntimeRequirementsV2(query=True))
    snapshot = registry.snapshot()

    assert selected.runtime_id == "alpha"
    assert [item.runtime_id for item in snapshot] == ["alpha"]


def test_systemic_registration_store_corruption_fails_closed(tmp_path: Path) -> None:
    """Catches a missing registry table being mistaken for no eligible runtime."""
    database = tmp_path / "state.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(runtime_capabilities("alpha", query=True))
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE runtime_v2_registrations")

    with pytest.raises(RuntimeRegistryIntegrityError, match="store"):
        registry.select(RuntimeRequirementsV2(query=True))


def test_registry_snapshot_is_deterministic_and_safe(tmp_path: Path) -> None:
    """Catches unstable diagnostics or leakage of executable runtime configuration."""
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "state.sqlite"))
    registry.register(runtime_capabilities("zeta", query=True, checkpoints=True))
    registry.register(runtime_capabilities("alpha", tools=True))

    snapshot = registry.snapshot()

    assert [item.runtime_id for item in snapshot] == ["alpha", "zeta"]
    assert snapshot[1].capabilities == ("query", "checkpoints")
    assert set(snapshot[1].__dataclass_fields__) == {
        "runtime_id",
        "build_id",
        "state",
        "capabilities",
        "command_id",
    }


def test_v2_runtime_process_settings_accept_only_structured_argv(tmp_path: Path) -> None:
    """Catches shell command strings entering the v2 runtime configuration."""
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        engine_host_v2_enabled=True,
        engine_host_v2_runtimes=(
            RuntimeProcessConfig(runtime_id="fake-v2", argv=("fake-v2", "--stdio")),
        ),
    )

    assert settings.engine_host_v2_runtimes[0].argv == ("fake-v2", "--stdio")
    with pytest.raises(ValidationError):
        RuntimeProcessConfig(runtime_id="fake-v2", argv="fake-v2 --stdio")  # type: ignore[arg-type]


def test_v2_runtime_process_settings_reject_duplicate_runtime_ids(
    tmp_path: Path,
) -> None:
    """Catches two process slots racing to advertise the same runtime identity."""
    with pytest.raises(ValidationError, match="runtime_id.*unique"):
        WorkbenchSettings(
            runtime_dir=tmp_path,
            engine_host_v2_enabled=True,
            engine_host_v2_runtimes=(
                RuntimeProcessConfig(runtime_id="fake-v2", argv=("first",)),
                RuntimeProcessConfig(runtime_id="fake-v2", argv=("second",)),
            ),
        )


def test_withdraw_is_transient_and_register_preserves_manual_disable(
    tmp_path: Path,
) -> None:
    """Catches a crash/restart silently clearing a persisted operator disable."""
    registry = RuntimeRegistryV2(RuntimeV2Repository(tmp_path / "state.sqlite"))
    capabilities = runtime_capabilities("goose", query=True)
    registry.register(capabilities)

    withdrawn = registry.withdraw("goose")

    assert withdrawn is not None
    assert withdrawn.runtime_id == "goose"
    assert withdrawn.state == "unavailable"
    assert registry.withdraw("goose") is None
    assert registry.snapshot()[0].state == "unavailable"

    registry.register(capabilities)
    registry.disable("goose")
    registry.withdraw("goose")
    restored = registry.register(capabilities)

    assert restored.state == "disabled"
    assert registry.snapshot()[0].state == "disabled"


@pytest.mark.parametrize(
    "unsafe_argument",
    [
        "--api-key",
        "--token=abcdefghijklmnopqrstuvwx",
        "OPENAI_API_KEY=abcdefghijklmnopqrstuvwx",
        "s" + "k" + "-" + "proj-" + "abcdefghijklmnopqrstuvwx",
        "Bear" + "er" + " " + "eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "bad\x00argument",
        "bad\nargument",
        "bad\x7fargument",
    ],
)
def test_v2_runtime_process_settings_reject_sensitive_or_control_argv(
    unsafe_argument: str,
) -> None:
    """Catches credentials or control bytes crossing the settings argv boundary."""
    with pytest.raises(ValidationError, match="sensitive|control"):
        RuntimeProcessConfig(
            runtime_id="fake-v2", argv=("fake-v2", unsafe_argument)
        )


def test_v2_runtime_process_settings_allow_safe_business_argv() -> None:
    """Catches argv validation rejecting ordinary token metrics or reset commands."""
    config = RuntimeProcessConfig(
        runtime_id="fake-v2",
        argv=("password-reset-helper", "--token-count=32"),
    )

    assert config.argv == ("password-reset-helper", "--token-count=32")


def test_build_app_assembles_v2_registry_without_replacing_v1_runner(
    tmp_path: Path,
) -> None:
    """Catches the v2 feature flag changing the established Python runner route."""
    runner = _PythonRunner()
    app = main.build_app(
        WorkbenchSettings(
            runtime_dir=tmp_path,
            engine_host_v2_enabled=True,
            engine_host_v2_runtimes=(
                RuntimeProcessConfig(runtime_id="fake-v2", argv=("fake-v2", "--stdio")),
            ),
        ),
        runner=runner,
    )

    assert isinstance(app.state.runtime_registry_v2, RuntimeRegistryV2)
    assert app.state.execution_runner is runner
    with TestClient(app) as client:
        assert client.get("/api/v1/engine-host").json() == {
            "v2": {
                "enabled": True,
                "protocol": "2.0",
                "runtimes": [
                    {
                        "runtime_id": "fake-v2",
                        "state": "unavailable",
                        "capabilities": [],
                        "selector": "fake-v2",
                        "selectable_for_new_commands": False,
                        "admission_state": "unavailable",
                        "admission_reason": "runtime_unavailable",
                        "supervisor_state": "unavailable",
                        "host_generation": 1,
                        "restart_count": 0,
                        "active": False,
                        "last_error_category": "start_failed",
                    }
                ],
            }
        }


def test_v2_runtime_settings_are_parsed_from_structured_json_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an environment loader that treats a shell string as process argv."""
    monkeypatch.setenv("WORKBENCH_ENGINE_HOST_V2_ENABLED", "true")
    monkeypatch.setenv(
        "WORKBENCH_ENGINE_HOST_V2_RUNTIMES_JSON",
        '[{"runtime_id":"fake-v2","argv":["fake-v2","--stdio"]}]',
    )

    settings = main._settings_from_environment(WorkbenchSettings(runtime_dir=tmp_path))

    assert settings.engine_host_v2_enabled is True
    assert settings.engine_host_v2_runtimes[0].argv == ("fake-v2", "--stdio")


def test_atomic_admission_blocks_cross_registry_disable_until_command_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches disable committing in the former select-to-pin admission gap."""
    database = tmp_path / "state.sqlite"
    first = RuntimeRegistryV2(RuntimeV2Repository(database))
    second = RuntimeRegistryV2(RuntimeV2Repository(database))
    capabilities = runtime_capabilities("goose", query=True)
    first.register(capabilities)
    second.register(capabilities)
    selected = threading.Event()
    release_pin = threading.Event()
    disable_finished = threading.Event()
    outcomes: dict[str, object] = {}
    original = first._select_in_connection

    def pause_after_selection(requirements, connection):
        outcome = original(requirements, connection)
        selected.set()
        assert release_pin.wait(timeout=2)
        return outcome

    monkeypatch.setattr(first, "_select_in_connection", pause_after_selection)

    def admit() -> None:
        outcomes["selection"] = first.select_and_pin(
            run_envelope(runtime_id="goose", command_id="command-atomic"),
            RuntimeRequirementsV2(query=True),
        )

    def disable() -> None:
        outcomes["disabled"] = second.disable("goose")
        disable_finished.set()

    admission = threading.Thread(target=admit)
    admission.start()
    assert selected.wait(timeout=2)
    disabler = threading.Thread(target=disable)
    disabler.start()
    assert not disable_finished.wait(timeout=0.1)
    release_pin.set()
    admission.join(timeout=2)
    disabler.join(timeout=2)

    assert not admission.is_alive()
    assert not disabler.is_alive()
    assert first.repository.get_pin("command-atomic") is not None
    assert first.resume("command-atomic").runtime_id == "goose"


def test_atomic_admission_pins_the_selected_capability_snapshot_before_reregister(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches capability persistence occurring after the admission transaction."""
    database = tmp_path / "state.sqlite"
    admitting = RuntimeRegistryV2(RuntimeV2Repository(database))
    updating = RuntimeRegistryV2(RuntimeV2Repository(database))
    admitting.register(runtime_capabilities("goose", query=True, model=True))
    selected = threading.Event()
    release_pin = threading.Event()
    update_finished = threading.Event()
    original = admitting.repository._pin_command_in_transaction

    def pause_before_pin(connection, envelope):
        selected.set()
        assert release_pin.wait(timeout=2)
        return original(connection, envelope)

    monkeypatch.setattr(
        admitting.repository, "_pin_command_in_transaction", pause_before_pin
    )
    outcomes: dict[str, object] = {}

    def admit() -> None:
        outcomes["selection"] = admitting.select_and_pin(
            run_envelope(runtime_id="goose", command_id="command-snapshot"),
            RuntimeRequirementsV2(query=True, model=True),
        )

    def update() -> None:
        outcomes["updated"] = updating.register(
            runtime_capabilities("goose", tools=True)
        )
        update_finished.set()

    admission = threading.Thread(target=admit)
    admission.start()
    assert selected.wait(timeout=2)
    updater = threading.Thread(target=update)
    updater.start()
    assert not update_finished.wait(timeout=0.1)
    release_pin.set()
    admission.join(timeout=2)
    updater.join(timeout=2)

    assert not admission.is_alive()
    assert not updater.is_alive()
    assert admitting.resume("command-snapshot").capabilities == ("query", "model")


def test_reopened_registry_marks_unadvertised_runtime_unavailable_not_corrupt(
    tmp_path: Path,
) -> None:
    """Catches a normal process restart turning durable history into an integrity error."""
    database = tmp_path / "state.sqlite"
    RuntimeRegistryV2(RuntimeV2Repository(database)).register(
        runtime_capabilities("goose", query=True)
    )
    reopened = RuntimeRegistryV2(RuntimeV2Repository(database))

    snapshot = reopened.snapshot()

    assert snapshot[0].runtime_id == "goose"
    assert snapshot[0].state == "unavailable"
    with pytest.raises(NoConformantRuntime):
        reopened.select(RuntimeRequirementsV2(query=True))


def test_reopened_disabled_runtime_remains_disabled_and_unselectable(
    tmp_path: Path,
) -> None:
    """Catches a disabled durable registration becoming ambiguous after restart."""
    database = tmp_path / "state.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(runtime_capabilities("goose", query=True))
    registry.disable("goose")

    reopened = RuntimeRegistryV2(RuntimeV2Repository(database))

    assert reopened.snapshot()[0].state == "disabled"
    with pytest.raises(NoConformantRuntime):
        reopened.select(RuntimeRequirementsV2(query=True))


def test_reopened_registry_resumes_durable_pin_without_live_advertisement(
    tmp_path: Path,
) -> None:
    """Catches resume consulting the new process's live selection instead of its pin."""
    database = tmp_path / "state.sqlite"
    registry = RuntimeRegistryV2(RuntimeV2Repository(database))
    registry.register(runtime_capabilities("goose", query=True))
    registry.select_and_pin(
        run_envelope(runtime_id="goose", command_id="command-reopen"),
        RuntimeRequirementsV2(query=True),
    )

    resumed = RuntimeRegistryV2(RuntimeV2Repository(database)).resume("command-reopen")

    assert resumed.runtime_id == "goose"
    assert resumed.build_id == "goose:test"
