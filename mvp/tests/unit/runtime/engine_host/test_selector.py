from types import SimpleNamespace

import pytest

from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.runtime.engine_host.client import (
    HostAdmissionUnknown,
    HostProtocolError,
    HostUnavailable,
)
from workbench.runtime.engine_host.selector import RunnerSelector, host_run_id_for
from workbench.models.contracts import ModelMessage
from workbench.models.profiles import ProviderProfileRecord


class RecordingRunner:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        fail_after_start: bool = False,
        state: str = "ready",
        profile: ProviderProfileRecord | None = None,
    ) -> None:
        self.calls = 0
        self.commands: list[RunAgentTurn] = []
        self.failure = failure
        self.fail_after_start = fail_after_start
        self.lifecycle: list[str] = []
        self.step_calls: list[tuple[str, str]] = []
        self.status = SimpleNamespace(state=state)
        self.profile = profile
        self.snapshots: dict[str, list[ModelMessage]] = {}

    async def run_turn(self, command: RunAgentTurn):
        self.calls += 1
        self.commands.append(command)
        if self.failure is not None and not self.fail_after_start:
            raise self.failure
        yield AgentEvent(
            kind="turn_started", session_id=command.session_id, run_id=command.run_id
        )
        if self.failure is not None:
            raise self.failure
        yield AgentEvent(
            kind="turn_finished", session_id=command.session_id, run_id=command.run_id
        )

    async def start(self) -> None:
        self.lifecycle.append("start")

    async def aclose(self) -> None:
        self.lifecycle.append("close")

    async def execute_step(self, run_id: str, step_id: str) -> tuple[str, str]:
        self.step_calls.append((run_id, step_id))
        return run_id, step_id

    def _resolve_profile(self, provider_id: str | None = None) -> ProviderProfileRecord:
        if self.profile is None:
            if provider_id == "deepseek":
                return ProviderProfileRecord.deepseek(id="deepseek")
            return ProviderProfileRecord(
                id=provider_id or "lmstudio",
                name="Local",
                protocol=provider_id or "lmstudio",
                base_url="http://127.0.0.1:1234",
            )
        return self.profile

    def _model_messages(self, session_id: str) -> list[ModelMessage]:
        return list(self.snapshots.get(session_id, []))


def _turn(*, runner_mode: str | None) -> RunAgentTurn:
    return RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="hello",
        model="local",
        provider_id="lmstudio",
        runner_mode=runner_mode,
    )


def test_selector_defaults_to_python_when_disabled() -> None:
    selector = RunnerSelector(RecordingRunner(), RecordingRunner(), enabled=False)

    assert selector.mode_for("session-1", "lmstudio", "local") == "python"


def test_selector_routes_only_allowlisted_provider_to_host() -> None:
    selector = RunnerSelector(RecordingRunner(), RecordingRunner(), enabled=True)

    assert selector.mode_for("session-1", "lmstudio", "local") == "engine_host"
    assert selector.mode_for("session-1", "deepseek", "cloud") == "python"


@pytest.mark.asyncio
async def test_persisted_mode_does_not_change_after_flag_change() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner()
    selector = RunnerSelector(python_runner, host_runner, enabled=True)
    command = _turn(runner_mode="engine_host")

    selector.enabled = False
    events = [event async for event in selector.run_turn(command)]

    assert host_runner.calls == 1
    assert python_runner.calls == 0
    assert events[-1].kind == "turn_finished"


@pytest.mark.asyncio
async def test_persisted_host_mode_ignores_later_unavailable_status() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner(state="unavailable")
    selector = RunnerSelector(python_runner, host_runner, enabled=True)

    events = [event async for event in selector.run_turn(_turn(runner_mode="engine_host"))]

    assert host_runner.calls == 1
    assert python_runner.calls == 0
    assert events[-1].kind == "turn_finished"


def test_enqueue_selection_falls_back_before_any_host_write_when_unavailable() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner(
        failure=HostUnavailable("offline"), state="unavailable"
    )
    selector = RunnerSelector(python_runner, host_runner, enabled=True)

    mode = selector.mode_for("session-1", "lmstudio", "local")

    assert mode == "python"
    assert host_runner.calls == 0
    assert python_runner.calls == 0


@pytest.mark.asyncio
async def test_unknown_host_admission_never_falls_back_to_python() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner(failure=HostAdmissionUnknown("unknown"))
    selector = RunnerSelector(python_runner, host_runner, enabled=True)

    with pytest.raises(HostAdmissionUnknown):
        [event async for event in selector.run_turn(_turn(runner_mode="engine_host"))]

    assert python_runner.calls == 0


@pytest.mark.asyncio
async def test_ready_host_failure_before_first_event_never_falls_back_to_python() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner(failure=HostUnavailable("possibly accepted"))
    selector = RunnerSelector(python_runner, host_runner, enabled=True)

    with pytest.raises(HostUnavailable):
        [event async for event in selector.run_turn(_turn(runner_mode="engine_host"))]

    assert host_runner.calls == 1
    assert python_runner.calls == 0


@pytest.mark.asyncio
async def test_host_failure_after_first_event_never_falls_back_to_python() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner(
        failure=HostUnavailable("interrupted"), fail_after_start=True
    )
    selector = RunnerSelector(python_runner, host_runner, enabled=True)

    with pytest.raises(HostUnavailable):
        [event async for event in selector.run_turn(_turn(runner_mode="engine_host"))]

    assert python_runner.calls == 0


@pytest.mark.asyncio
async def test_selector_lifecycle_delegates_only_to_host() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner()
    selector = RunnerSelector(python_runner, host_runner, enabled=True)

    await selector.start()
    await selector.aclose()

    assert python_runner.lifecycle == []
    assert host_runner.lifecycle == ["start", "close"]


@pytest.mark.asyncio
async def test_workflow_steps_remain_on_python_runner() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner()
    selector = RunnerSelector(python_runner, host_runner, enabled=True)

    result = await selector.execute_step("run-1", "step-1")

    assert result == ("run-1", "step-1")
    assert python_runner.step_calls == [("run-1", "step-1")]
    assert host_runner.step_calls == []


@pytest.mark.asyncio
async def test_host_execution_uses_stable_per_turn_run_ids() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner()
    selector = RunnerSelector(python_runner, host_runner, enabled=True)
    first = _turn(runner_mode="engine_host")
    second = first.model_copy(update={"command_id": "turn-2"})

    [event async for event in selector.run_turn(first)]
    [event async for event in selector.run_turn(second)]
    [event async for event in selector.run_turn(first)]

    assert host_runner.commands[0].run_id == "run-1"
    assert host_runner.commands[1].run_id == "run-1"
    assert host_runner.commands[0].host_run_id != host_runner.commands[1].host_run_id
    assert host_runner.commands[0].host_run_id == host_runner.commands[2].host_run_id
    assert host_runner.commands[0].host_run_id == host_run_id_for("session-1", "turn-1")


def test_selector_routes_resolved_no_secret_lmstudio_profile() -> None:
    profile = ProviderProfileRecord(
        id="studio-primary",
        name="Custom LM Studio",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
    )
    selector = RunnerSelector(
        RecordingRunner(profile=profile),
        RecordingRunner(),
        enabled=True,
        provider_allowlist=("lmstudio",),
    )

    assert selector.resolve_profile("studio-primary") == profile
    assert selector.mode_for("session-1", "studio-primary", "local") == "engine_host"


@pytest.mark.asyncio
async def test_selector_sends_resolved_protocol_and_message_snapshot_to_host() -> None:
    profile = ProviderProfileRecord(
        id="studio-primary",
        name="Custom LM Studio",
        protocol="lmstudio",
        base_url="http://127.0.0.1:1234",
    )
    host_runner = RecordingRunner()
    selector = RunnerSelector(
        RecordingRunner(profile=profile),
        host_runner,
        enabled=True,
    )
    command = _turn(runner_mode="engine_host").model_copy(
        update={
            "provider_id": "studio-primary",
            "message_snapshot": (
                ModelMessage(role="user", content="first"),
                ModelMessage(role="assistant", content="answer"),
                ModelMessage(role="user", content="next"),
            ),
        }
    )

    [event async for event in selector.run_turn(command)]

    assert host_runner.commands[0].provider_id == "lmstudio"
    assert [message.content for message in host_runner.commands[0].message_snapshot] == [
        "first",
        "answer",
        "next",
    ]


@pytest.mark.asyncio
async def test_protocol_failure_on_persisted_host_mode_never_calls_python() -> None:
    python_runner = RecordingRunner()
    selector = RunnerSelector(
        python_runner,
        RecordingRunner(failure=HostProtocolError("bad frame")),
        enabled=True,
    )

    with pytest.raises(HostProtocolError):
        [event async for event in selector.run_turn(_turn(runner_mode="engine_host"))]

    assert python_runner.calls == 0


def test_selector_never_routes_secret_or_non_lmstudio_profile_to_host() -> None:
    deepseek = ProviderProfileRecord(
        id="deepseek-primary",
        name="DeepSeek",
        protocol="deepseek",
        base_url="https://api.deepseek.com",
        secret_id="vault-secret",
        thinking_enabled=True,
    )
    selector = RunnerSelector(
        RecordingRunner(profile=deepseek),
        RecordingRunner(),
        enabled=True,
        provider_allowlist=("deepseek-primary",),
    )

    assert selector.mode_for("session-1", "deepseek-primary", "cloud") == "python"


def test_selector_delegates_canonical_message_snapshot() -> None:
    python_runner = RecordingRunner()
    python_runner.snapshots["session-1"] = [
        ModelMessage(role="user", content="first"),
        ModelMessage(role="assistant", content="answer"),
    ]
    selector = RunnerSelector(python_runner, RecordingRunner(), enabled=True)

    assert selector.model_messages("session-1") == python_runner.snapshots["session-1"]
