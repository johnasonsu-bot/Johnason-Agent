"""Integration coverage for the supervised Engine Host subprocess boundary."""

from __future__ import annotations

from pathlib import Path
import sys
import asyncio

import pytest

from workbench.runtime.engine_host.client import EngineHostClient, HostUnavailable
from workbench.runtime.engine_host.contracts import HostFrameTooLarge, HostProtocolError


FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_engine_host.py"


def fake_host_command(mode: str) -> tuple[str, ...]:
    return (sys.executable, str(FIXTURE), mode)


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
    await asyncio.sleep(0.25)
    assert client.returncode is not None


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
