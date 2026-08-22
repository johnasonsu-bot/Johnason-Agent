"""Offline acceptance for the durable Engine Host contract."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from workbench.api.conversations import ConversationAPI
from workbench.conversations.repository import ConversationRepository
from workbench.models.contracts import ModelMessage
from workbench.models.profiles import ProviderProfileRecord
from workbench.runtime.agent_loop import AgentEvent, RunAgentTurn
from workbench.runtime.engine_host.client import (
    EngineHostClient,
    HostAdmissionUnknown,
    HostExecutionError,
    HostExecutionUnknown,
)
from workbench.runtime.engine_host.contracts import HostProtocolError
from workbench.runtime.engine_host.selector import RunnerSelector
from workbench.workflow.event_store import EventStore


FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_engine_host.py"


def fake_host_command(mode: str, capture: Path | None = None) -> tuple[str, ...]:
    command = [sys.executable, str(FIXTURE), mode]
    if capture is not None:
        command.append(str(capture))
    return tuple(command)


def turn() -> RunAgentTurn:
    return RunAgentTurn(
        session_id="session-1",
        run_id="run-1",
        command_id="turn-1",
        prompt="offline contract",
        model="local-agent",
        provider_id="lmstudio",
        owner_id="worker-1",
        runner_mode="engine_host",
        host_run_id="host-run-1",
        message_snapshot=(ModelMessage(role="user", content="offline contract"),),
    )


class OfflinePythonRunner:
    def __init__(self) -> None:
        self.calls = 0

    def resolve_profile(self, provider_id: str | None = None) -> ProviderProfileRecord:
        return ProviderProfileRecord(
            id=provider_id or "lmstudio",
            name="Offline Local",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
        )

    def model_messages(self, _session_id: str) -> list[ModelMessage]:
        return []

    async def run_turn(self, command: RunAgentTurn):
        self.calls += 1
        yield AgentEvent(
            kind="turn_started", session_id=command.session_id, run_id=command.run_id
        )
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": "python offline"},
        )
        yield AgentEvent(
            kind="turn_finished", session_id=command.session_id, run_id=command.run_id
        )


async def collect_failure(mode: str) -> Exception:
    client = EngineHostClient(
        fake_host_command(mode), request_timeout=0.2, shutdown_timeout=0.05
    )
    await client.start()
    try:
        with pytest.raises(Exception) as failure:
            _ = [event async for event in client.run_turn(turn())]
        return failure.value
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    (
        "mode",
        "phase",
        "retryable",
        "reconciliation_required",
        "compatibility_type",
        "safe_summary",
    ),
    [
        (
            "exit_before_accept",
            "pre_start",
            True,
            False,
            HostAdmissionUnknown,
            "engine-host run admission is unknown",
        ),
        (
            "exit_after_accept",
            "accepted_before_tool",
            True,
            False,
            HostExecutionUnknown,
            "engine-host run execution is unknown",
        ),
        (
            "exit_after_read_tool",
            "read_only_effect",
            True,
            False,
            HostExecutionUnknown,
            "engine-host run execution is unknown",
        ),
        (
            "exit_during_write_tool",
            "unknown_write_effect",
            False,
            True,
            HostExecutionError,
            "engine-host write effect requires reconciliation",
        ),
        (
            "unknown_event",
            "protocol",
            True,
            False,
            HostProtocolError,
            "engine-host emitted an unknown run event",
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_failure_has_one_safe_classification(
    mode: str,
    phase: str,
    retryable: bool,
    reconciliation_required: bool,
    compatibility_type: type[Exception],
    safe_summary: str,
) -> None:
    failure = await collect_failure(mode)

    assert isinstance(failure, HostExecutionError)
    assert isinstance(failure, compatibility_type)
    assert failure.phase == phase  # type: ignore[attr-defined]
    assert failure.retryable is retryable  # type: ignore[attr-defined]
    assert (  # type: ignore[attr-defined]
        failure.reconciliation_required is reconciliation_required
    )
    assert str(failure) == safe_summary
    assert "offline contract" not in str(failure)


async def run_host_scenario(database: Path, mode: str) -> tuple[object, int]:
    host = EngineHostClient(
        fake_host_command(mode), request_timeout=0.2, shutdown_timeout=0.05
    )
    await host.start()
    python_runner = OfflinePythonRunner()
    selector = RunnerSelector(
        python_runner,
        host,
        enabled=True,
        provider_allowlist=("lmstudio",),
        host_generation="generation-1",
    )
    repository = ConversationRepository(database, host_generation="generation-1")
    api = ConversationAPI(repository, EventStore(database), selector)
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="offline contract",
        model="local-agent",
        provider_id="lmstudio",
    )
    claimed = repository.claim_next_turn(owner_id="worker-1")
    assert claimed is not None
    try:
        await api.process_queued_turn("session-1", "turn-1")
    finally:
        await host.aclose()
    persisted = repository.load_turn_status("session-1", "turn-1")
    assert persisted is not None
    return persisted, python_runner.calls


async def run_cancel_scenario(database: Path, mode: str) -> tuple[Exception, object]:
    host = EngineHostClient(
        fake_host_command(mode), request_timeout=0.05, shutdown_timeout=0.05
    )
    await host.start()
    python_runner = OfflinePythonRunner()
    selector = RunnerSelector(
        python_runner,
        host,
        enabled=True,
        provider_allowlist=("lmstudio",),
        host_generation="generation-1",
    )
    repository = ConversationRepository(database, host_generation="generation-1")
    api = ConversationAPI(repository, EventStore(database), selector)
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="offline contract",
        model="local-agent",
        provider_id="lmstudio",
    )
    claimed = repository.claim_next_turn(owner_id="worker-1")
    assert claimed is not None
    processing = asyncio.create_task(api.process_queued_turn("session-1", "turn-1"))

    async def write_effect_is_pending() -> str:
        while True:
            for run_id, stream in host._active_runs.items():
                if stream.unfinished_write_tools:
                    return run_id
            await asyncio.sleep(0)

    try:
        run_id = await asyncio.wait_for(write_effect_is_pending(), timeout=1.0)
        with pytest.raises(Exception) as failure:
            await host.cancel(run_id, "user_requested")
        await asyncio.wait_for(processing, timeout=1.0)
    finally:
        processing.cancel()
        await asyncio.gather(processing, return_exceptions=True)
        await host.aclose()

    persisted = repository.load_turn_status("session-1", "turn-1")
    assert persisted is not None
    assert python_runner.calls == 0
    return failure.value, persisted


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        ("exit_before_accept", "retryable"),
        ("exit_after_accept", "retryable"),
        ("exit_after_read_tool", "retryable"),
        ("exit_during_write_tool", "reconciliation_required"),
        ("unknown_event", "retryable"),
    ],
)
@pytest.mark.asyncio
async def test_host_crash_maps_to_durable_turn_status(
    tmp_path: Path, mode: str, expected_status: str
) -> None:
    turn_status, python_calls = await run_host_scenario(
        tmp_path / f"{mode}.sqlite", mode
    )

    assert turn_status.status == expected_status  # type: ignore[attr-defined]
    assert turn_status.owner_id is None  # type: ignore[attr-defined]
    assert turn_status.lease_expires_at == 0  # type: ignore[attr-defined]
    assert turn_status.state["runner_mode"] == "engine_host"  # type: ignore[attr-defined]
    assert python_calls == 0


@pytest.mark.parametrize(
    "mode",
    [
        "write_then_run_completed",
        "write_then_run_failed",
        "write_then_run_cancelled",
    ],
)
@pytest.mark.asyncio
async def test_run_terminal_cannot_hide_an_unfinished_write_effect(
    tmp_path: Path, mode: str
) -> None:
    persisted, python_calls = await run_host_scenario(tmp_path / f"{mode}.sqlite", mode)

    assert persisted.status == "reconciliation_required"  # type: ignore[attr-defined]
    assert persisted.state["host_failure_phase"] == "unknown_write_effect"  # type: ignore[attr-defined]
    terminal_outcomes = [  # type: ignore[attr-defined]
        item
        for item in persisted.result
        if item.get("name") in {"turn_finished", "turn_failed"}
    ]
    assert len(terminal_outcomes) == 1
    assert terminal_outcomes[0]["name"] == "turn_failed"
    assert terminal_outcomes[0]["value"]["reason"] == (
        "engine_host_unknown_write_effect"
    )
    assert python_calls == 0


@pytest.mark.parametrize(
    "mode",
    [
        "write_then_ignore_cancel",
        "write_then_ack_without_terminal",
        "write_then_cancel_protocol_error",
    ],
)
@pytest.mark.asyncio
async def test_cancel_failure_preserves_unknown_write_precedence(
    tmp_path: Path, mode: str
) -> None:
    failure, persisted = await run_cancel_scenario(
        tmp_path / f"{mode}.sqlite", mode
    )

    assert isinstance(failure, HostExecutionError)
    assert failure.phase == "unknown_write_effect"  # type: ignore[attr-defined]
    assert failure.reconciliation_required is True  # type: ignore[attr-defined]
    assert persisted.status == "reconciliation_required"  # type: ignore[attr-defined]
    assert persisted.state["runner_mode"] == "engine_host"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_first_valid_terminal_is_the_only_durable_outcome(tmp_path: Path) -> None:
    database = tmp_path / "terminal-sealed.sqlite"
    persisted, python_calls = await run_host_scenario(
        database, "immediate_event_after_terminal"
    )
    outcomes = [
        event.event_type
        for event in EventStore(database).read_stream("run:session-1")
        if event.event_type
        in {
            "conversation.turn.finished",
            "conversation.turn.failed",
            "conversation.turn.retryable",
        }
    ]

    assert persisted.status == "completed"  # type: ignore[attr-defined]
    assert outcomes == ["conversation.turn.finished"]
    assert python_calls == 0


@pytest.mark.asyncio
async def test_unknown_write_effect_is_not_claimed_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "unknown-write.sqlite"
    persisted, _ = await run_host_scenario(database, "exit_during_write_tool")

    restarted = ConversationRepository(database, host_generation="generation-2")
    assert persisted.status == "reconciliation_required"  # type: ignore[attr-defined]
    assert restarted.claim_next_turn(owner_id="worker-2") is None


@pytest.mark.asyncio
async def test_retryable_host_turn_replays_only_after_new_generation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart.sqlite"
    persisted, _ = await run_host_scenario(database, "exit_after_read_tool")
    assert persisted.status == "retryable"  # type: ignore[attr-defined]
    assert (
        ConversationRepository(database, host_generation="generation-1").claim_next_turn(
            owner_id="same-generation"
        )
        is None
    )

    host = EngineHostClient(fake_host_command("normal"), shutdown_timeout=0.05)
    await host.start()
    python_runner = OfflinePythonRunner()
    restarted = ConversationRepository(database, host_generation="generation-2")
    selector = RunnerSelector(
        python_runner,
        host,
        enabled=True,
        host_generation="generation-2",
    )
    api = ConversationAPI(restarted, EventStore(database), selector)
    claimed = restarted.claim_next_turn(owner_id="worker-2")
    assert claimed is not None
    try:
        await api.process_queued_turn("session-1", "turn-1")
    finally:
        await host.aclose()

    completed = restarted.load_turn_status("session-1", "turn-1")
    assert completed is not None and completed.status == "completed"
    assert python_runner.calls == 0
    assert [message.role for message in restarted.list_messages("session-1")] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_feature_flag_off_runs_python_without_host_admission(tmp_path: Path) -> None:
    database = tmp_path / "flag-off.sqlite"
    host = EngineHostClient(("definitely-not-an-engine-host",))
    python_runner = OfflinePythonRunner()
    selector = RunnerSelector(python_runner, host, enabled=False)
    repository = ConversationRepository(database)
    api = ConversationAPI(repository, EventStore(database), selector)
    api.create_session("session-1")
    await api.enqueue_message(
        session_id="session-1",
        command_id="turn-1",
        content="offline contract",
        model="local-agent",
        provider_id="lmstudio",
    )
    claimed = repository.claim_next_turn(owner_id="worker-1")
    assert claimed is not None

    await api.process_queued_turn("session-1", "turn-1")

    persisted = repository.load_turn_status("session-1", "turn-1")
    assert persisted is not None and persisted.status == "completed"
    assert persisted.state["runner_mode"] == "python"
    assert python_runner.calls == 1
    assert host.returncode is None


@pytest.mark.asyncio
async def test_protocol_lifecycle_cancel_drain_and_forced_shutdown() -> None:
    client = EngineHostClient(
        fake_host_command("blocking_run"),
        request_timeout=0.2,
        shutdown_timeout=0.05,
    )
    await client.start()
    stream = client.run_turn(turn())
    assert (await anext(stream)).kind == "turn_started"
    consumer = asyncio.create_task(anext(stream))
    await client.cancel("host-run-1", "user_requested")
    terminal = await consumer
    assert terminal.kind == "turn_failed"
    await stream.aclose()
    await client.drain(0.2)
    await client.aclose()
    assert client.returncode is not None

    forced = EngineHostClient(
        fake_host_command("ignore_shutdown"),
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    await forced.start()
    await forced.aclose()
    assert forced.returncode is not None


@pytest.mark.asyncio
async def test_protocol_artifacts_do_not_contain_sensitive_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "sensitive-sentinel-value"
    capture = tmp_path / "wire-metadata.ndjson"
    monkeypatch.setenv("TEST_SECRET", sentinel)
    client = EngineHostClient(
        fake_host_command("capture_metadata", capture), shutdown_timeout=0.05
    )
    await client.start()
    try:
        events = [event async for event in client.run_turn(turn())]
    finally:
        await client.aclose()

    event_text = json.dumps(
        [{"kind": event.kind, "payload": event.payload} for event in events],
        sort_keys=True,
    )
    scanned = "\n".join(
        [
            repr(client.command),
            capture.read_text(encoding="utf-8"),
            client.diagnostics,
            event_text,
            str(HostAdmissionUnknown(sentinel)),
            str(HostExecutionUnknown(sentinel)),
            "\n".join(
                path.read_text(encoding="utf-8")
                for path in (tmp_path / "artifacts").glob("**/*")
                if path.is_file()
            )
            if (tmp_path / "artifacts").exists()
            else "",
        ]
    )
    lowered = scanned.lower()
    assert sentinel not in scanned
    assert "reasoning_content" not in scanned
    assert "api_key" not in lowered
    assert "password" not in lowered
    assert os.environ["TEST_SECRET"] == sentinel
