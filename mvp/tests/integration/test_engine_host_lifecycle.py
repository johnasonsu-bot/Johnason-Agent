"""Integration coverage for the supervised Engine Host subprocess boundary."""

from __future__ import annotations

from pathlib import Path
import sys
import asyncio
from time import monotonic

import pytest

from workbench.runtime.engine_host.client import EngineHostClient, HostUnavailable
from workbench.runtime.engine_host.contracts import HostFrameTooLarge, HostProtocolError


FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_engine_host.py"


def fake_host_command(mode: str) -> tuple[str, ...]:
    return (sys.executable, str(FIXTURE), mode)


async def wait_for_reap(client: EngineHostClient) -> None:
    async def reaped() -> None:
        while client.returncode is None:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(reaped(), timeout=1.0)


@pytest.mark.asyncio
async def test_client_negotiates_protocol_and_capabilities() -> None:
    client = EngineHostClient(fake_host_command("normal"))
    await client.start()
    try:
        capabilities = await client.capabilities()
        assert client.status.state == "ready"
        assert client.status.protocol == "workbench.engine-host/v1"
        assert capabilities.model is True
        assert capabilities.agui is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_incompatible_protocol_fails_without_marking_ready() -> None:
    client = EngineHostClient(fake_host_command("bad_protocol"))
    with pytest.raises(HostProtocolError, match="incompatible protocol"):
        await client.start()
    assert client.status.state == "unavailable"
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_child_exit_fails_pending_requests_and_reaps_process() -> None:
    client = EngineHostClient(fake_host_command("exit_after_hello"), request_timeout=0.5)
    with pytest.raises(HostUnavailable):
        await client.start()
    await client.aclose()
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_close_forces_host_that_ignores_shutdown() -> None:
    client = EngineHostClient(
        fake_host_command("ignore_shutdown"), shutdown_timeout=0.1
    )
    await client.start()
    await client.aclose()
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_close_is_idempotent_after_a_graceful_shutdown() -> None:
    client = EngineHostClient(fake_host_command("normal"))
    await client.start()
    await client.aclose()
    await client.aclose()
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_close_before_start_permanently_rejects_a_later_start() -> None:
    client = EngineHostClient(fake_host_command("normal"))
    await client.aclose()
    with pytest.raises(HostUnavailable, match="closed"):
        await client.start()
    assert client.status.state == "unavailable"


@pytest.mark.asyncio
async def test_concurrent_start_creates_only_one_host_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TMP", str(tmp_path))
    client = EngineHostClient(fake_host_command("record_starts"))
    await asyncio.gather(client.start(), client.start())
    try:
        assert (tmp_path / "fake_engine_host_starts.txt").read_text().count("\n") == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_response_name_must_match_its_correlated_request() -> None:
    client = EngineHostClient(fake_host_command("wrong_response_name"))
    with pytest.raises(HostProtocolError, match="response name"):
        await client.start()
    assert client.status.state == "unavailable"
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_protocol_error_during_drain_reaps_the_host() -> None:
    client = EngineHostClient(fake_host_command("wrong_drain_response_name"))
    await client.start()
    with pytest.raises(HostProtocolError, match="response name"):
        await client.drain(0.5)
    assert client.status.state == "unavailable"
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_drain_timeout_marks_unavailable_and_reaps_the_host() -> None:
    client = EngineHostClient(
        fake_host_command("ignore_drain"), request_timeout=0.1, shutdown_timeout=0.1
    )
    await client.start()
    with pytest.raises(HostUnavailable, match="drain timed out"):
        await client.drain(0.05)
    assert client.status.state == "unavailable"
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_cancelled_close_still_reaps_host() -> None:
    client = EngineHostClient(
        fake_host_command("ignore_shutdown"), shutdown_timeout=0.1
    )
    await client.start()
    closing = asyncio.create_task(client.aclose())
    await asyncio.sleep(0.01)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await client.aclose()
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_cancelled_close_during_start_still_reaps_the_host() -> None:
    client = EngineHostClient(fake_host_command("delayed_hello"), shutdown_timeout=0.1)
    starting = asyncio.create_task(client.start())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(starting), timeout=0.02)
    closing = asyncio.create_task(client.aclose())
    await asyncio.sleep(0)
    supervised_close = client._close_task
    assert supervised_close is not None
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await client.aclose()
    assert client._close_task is supervised_close
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_close_during_unresponsive_hello_honors_shutdown_budget() -> None:
    client = EngineHostClient(
        fake_host_command("ignore_hello"), request_timeout=0.4, shutdown_timeout=0.05
    )
    starting = asyncio.create_task(client.start())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(starting), timeout=0.02)
    started_at = monotonic()
    await client.aclose()
    assert monotonic() - started_at < 0.2
    assert client.returncode is not None
    with pytest.raises(asyncio.CancelledError):
        await starting


@pytest.mark.asyncio
async def test_cancelling_only_start_waiter_reaps_the_host_without_aclose() -> None:
    client = EngineHostClient(fake_host_command("ignore_hello"), shutdown_timeout=0.1)
    starting = asyncio.create_task(client.start())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(starting), timeout=0.02)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    await wait_for_reap(client)


@pytest.mark.asyncio
async def test_cancelling_one_of_two_start_waiters_keeps_handshake_alive() -> None:
    client = EngineHostClient(fake_host_command("delayed_hello"))
    first = asyncio.create_task(client.start())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(first), timeout=0.02)
    second = asyncio.create_task(client.start())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert client.status.state == "ready"
    await client.aclose()


@pytest.mark.asyncio
async def test_reader_error_reaps_when_ready_precedes_start_task_completion() -> None:
    client = EngineHostClient(
        fake_host_command("delayed_close_stdout_after_ready"), shutdown_timeout=0.1
    )
    await client.start()

    async def complete_after_reader_error() -> None:
        await asyncio.sleep(0.2)

    client._start_task = asyncio.create_task(complete_after_reader_error())
    await wait_for_reap(client)
    assert client.status.state == "unavailable"


@pytest.mark.asyncio
async def test_concurrent_close_preserves_incompatible_protocol_error() -> None:
    client = EngineHostClient(fake_host_command("bad_protocol"), shutdown_timeout=0.1)
    starting = asyncio.create_task(client.start())
    while client.status.state != "unavailable":
        await asyncio.sleep(0)
    closing = asyncio.create_task(client.aclose())
    with pytest.raises(HostProtocolError, match="incompatible protocol"):
        await starting
    await closing
    assert client.returncode is not None


@pytest.mark.asyncio
async def test_reader_eof_after_handshake_reaps_the_still_running_host() -> None:
    client = EngineHostClient(
        fake_host_command("close_stdout_after_ready"), shutdown_timeout=0.1
    )
    await client.start()
    await wait_for_reap(client)
    assert client.status.state == "unavailable"


@pytest.mark.asyncio
async def test_capabilities_waits_for_delayed_hello_without_an_early_command() -> None:
    client = EngineHostClient(fake_host_command("delayed_hello"))
    starting = asyncio.create_task(client.start())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(starting), timeout=0.02)
    capabilities = await client.capabilities()
    assert capabilities.model is True
    await starting
    await client.aclose()


@pytest.mark.asyncio
async def test_drain_before_ready_does_not_write_to_the_host() -> None:
    client = EngineHostClient(fake_host_command("delayed_hello"))
    starting = asyncio.create_task(client.start())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(starting), timeout=0.02)
    with pytest.raises(HostUnavailable, match="ready"):
        await client.drain(0.1)
    await starting
    await client.aclose()


@pytest.mark.asyncio
async def test_environment_allowlist_omits_parent_secret_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENGINE_HOST_TEST_SECRET", "must-not-reach-child")
    client = EngineHostClient(fake_host_command("environment_guard"))
    await client.start()
    await client.aclose()


@pytest.mark.asyncio
async def test_oversized_host_frame_fails_start_and_never_marks_ready() -> None:
    client = EngineHostClient(fake_host_command("oversized"))
    with pytest.raises(HostFrameTooLarge):
        await client.start()
    assert client.status.state == "unavailable"
    await client.aclose()


@pytest.mark.asyncio
async def test_host_stderr_diagnostic_cannot_include_parent_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENGINE_HOST_TEST_SECRET", "must-not-reach-child")
    client = EngineHostClient(fake_host_command("diagnostic"))
    await client.start()
    try:
        assert client.diagnostics == "engine-host emitted diagnostics"
        assert "must-not-reach-child" not in client.diagnostics
        assert "ENGINE_HOST_TEST_SECRET" not in client.diagnostics
    finally:
        await client.aclose()
