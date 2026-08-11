from types import SimpleNamespace

import pytest

from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.runtime.engine_host.client import HostAdmissionUnknown, HostUnavailable
from workbench.runtime.engine_host.selector import RunnerSelector


class RecordingRunner:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        fail_after_start: bool = False,
        state: str = "ready",
    ) -> None:
        self.calls = 0
        self.commands: list[RunAgentTurn] = []
        self.failure = failure
        self.fail_after_start = fail_after_start
        self.lifecycle: list[str] = []
        self.step_calls: list[tuple[str, str]] = []
        self.status = SimpleNamespace(state=state)

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
async def test_host_unavailable_before_admission_falls_back_to_python() -> None:
    python_runner = RecordingRunner()
    host_runner = RecordingRunner(
        failure=HostUnavailable("offline"), state="unavailable"
    )
    selector = RunnerSelector(python_runner, host_runner, enabled=True)

    events = [event async for event in selector.run_turn(_turn(runner_mode="engine_host"))]

    assert host_runner.calls == 0
    assert python_runner.calls == 1
    assert events[-1].kind == "turn_finished"


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
