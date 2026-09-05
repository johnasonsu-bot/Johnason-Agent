"""Federated Runtime user-path acceptance.

The deterministic tests exercise the real Conversation HTTP service and its
durable store while replacing only the external Runtime process.  They are
contract/regression evidence, never live Runtime GO evidence.

The live test talks to an already-running, user-operated Workbench instance.
It is skipped unless explicitly enabled and never reads a Vault, credential,
Runtime directory, or evidence file directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from tests.fixtures.host_v2 import run_envelope
from workbench.api.app import AppSettings, create_app
from workbench.api.conversations import (
    RuntimeConversationRoute,
    python_term_admission_identity,
    python_term_command_id,
)
from workbench.conversations.repository import ConversationRepository
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.v2.contracts import (
    RuntimeEventV2,
    RuntimeMessageInputV2,
    RuntimePromptSectionInputV2,
    RuntimeQueryInputV2,
    canonical_runtime_input_digest,
)
from workbench.runtime.provider_grants import canonical_provider_profile_digest


LIVE_OPT_IN = os.environ.get("WORKBENCH_RUN_LIVE_RUNTIME_ACCEPTANCE") == "1"
TERMINAL_NAMES = frozenset({"turn_finished", "turn_failed"})


class _NoopRunner:
    async def run_turn(self, _command: object) -> Any:
        if False:
            yield None


def _runtime_input(content: str) -> RuntimeQueryInputV2:
    messages = (
        RuntimeMessageInputV2(
            message_id="message-1",
            role="user",
            content=content,
        ),
    )
    prompt_sections = (
        RuntimePromptSectionInputV2(
            section_id="section-1",
            order=0,
            content="Follow the accepted user request.",
        ),
    )
    return RuntimeQueryInputV2(
        messages=messages,
        message_snapshot_digest=canonical_runtime_input_digest(messages),
        context_items=(),
        context_snapshot_digest=canonical_runtime_input_digest(()),
        prompt_sections=prompt_sections,
        prompt_manifest_digest=canonical_runtime_input_digest(prompt_sections),
    )


class _OfflineRuntimeRouter:
    """Materialize the same frozen route shape used by the production router."""

    def route_conversation_query(self, *, selector: str, admission: Any) -> RuntimeConversationRoute:
        runtime_command_id = python_term_command_id(
            admission.session_id, admission.command_id
        )
        runtime_input = _runtime_input(admission.messages[-1].content)
        admission_identity_digest = hashlib.sha256(
            json.dumps(
                python_term_admission_identity(admission),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        profile_digest = canonical_provider_profile_digest(admission.provider)
        envelope = run_envelope(
            runtime_id=selector,
            command_id=runtime_command_id,
            overrides={
                "session_id": f"conversation-session:{admission.session_id}",
                "run_id": f"run-{admission.command_id}",
                "term_id": f"term-{admission.command_id}",
                "step_id": f"step-{admission.command_id}",
                "provider_ref": f"provider-profile:{admission.provider.id}",
                "model": admission.model,
                "message_snapshot_digest": runtime_input.message_snapshot_digest,
                "context.snapshot_digest": runtime_input.context_snapshot_digest,
                "prompt_manifest_digest": runtime_input.prompt_manifest_digest,
                "extensions": {
                    "admission_identity_digest": admission_identity_digest,
                    "provider_profile_digest": profile_digest,
                    "resolved_model": admission.model,
                },
            },
        )
        snapshot = {
            "selector": selector,
            "runtime_id": selector,
            "build_id": envelope.runtime.build_id,
            "provider_profile_digest": profile_digest,
            "resolved_model": admission.model,
            "envelope": envelope.model_dump(mode="json"),
            "runtime_input": runtime_input.model_dump(mode="json"),
        }
        return RuntimeConversationRoute(
            runtime_id=selector,
            build_id=envelope.runtime.build_id,
            runtime_command_id=runtime_command_id,
            execution_snapshot=snapshot,
        )


def _runtime_event(
    snapshot: dict[str, Any], event_type: str, cursor: int, payload: dict[str, Any]
) -> RuntimeEventV2:
    envelope = snapshot["envelope"]
    return RuntimeEventV2.model_validate(
        {
            "event_id": f"event-{cursor}",
            "run_id": envelope["run_id"],
            "term_id": envelope["term_id"],
            "step_id": envelope["step_id"],
            "cursor": cursor,
            "type": event_type,
            "payload": payload,
            "required": False,
        }
    )


class _CompletingRuntime:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, snapshot: dict[str, Any]) -> Any:
        self.calls += 1
        yield _runtime_event(snapshot, "runtime.status", 1, {"status": "running"})
        yield _runtime_event(snapshot, "assistant.delta", 2, {"text": "hello "})
        yield _runtime_event(
            snapshot, "assistant.message", 3, {"content": "hello runtime"}
        )
        yield _runtime_event(snapshot, "runtime.status", 4, {"status": "completed"})


class _ForbiddenRuntime:
    def __init__(self) -> None:
        self.called = False

    async def execute(self, _snapshot: dict[str, Any]) -> Any:
        self.called = True
        raise AssertionError("a durable terminal must not re-execute after restart")
        yield


class _CancellableRuntime:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancel_signal: asyncio.Event | None = None
        self.command_id: str | None = None
        self.cancelled_commands: list[str] = []

    async def execute(self, snapshot: dict[str, Any]) -> Any:
        self.command_id = snapshot["envelope"]["command_id"]
        self.cancel_signal = asyncio.Event()
        self.started.set()
        yield _runtime_event(snapshot, "runtime.status", 1, {"status": "running"})
        await self.cancel_signal.wait()
        yield _runtime_event(snapshot, "runtime.status", 2, {"status": "cancelled"})

    def active_command(self, session_id: str) -> str | None:
        assert session_id == "session-cancel"
        return self.command_id

    async def cancel(self, command_id: str) -> bool:
        assert command_id == self.command_id
        assert self.cancel_signal is not None
        self.cancelled_commands.append(command_id)
        self.cancel_signal.set()
        return True


class _IsolatingRuntime:
    async def execute(self, snapshot: dict[str, Any]) -> Any:
        status = "failed" if snapshot["runtime_id"] == "goose" else "completed"
        yield _runtime_event(snapshot, "runtime.status", 1, {"status": "running"})
        yield _runtime_event(snapshot, "runtime.status", 2, {"status": status})


def _service_app(database: Path, executor: Any):
    providers = ProviderRepository(database)
    for provider_id in ("provider-a", "provider-b"):
        providers.upsert(
            ProviderProfileRecord(
                id=provider_id,
                name=provider_id,
                protocol="lmstudio",
                base_url="http://127.0.0.1:1234",
                credential_mode="none",
                model_aliases={"default": "configured-model"},
            )
        )
    return create_app(
        AppSettings(
            database=database,
            runner=_NoopRunner(),
            owner_id="runtime-user-path-acceptance",
            runtime_router=_OfflineRuntimeRouter(),
            federated_executor=executor,
        )
    )


def _create_session(client: TestClient, session_id: str) -> None:
    response = client.post("/api/sessions", json={"session_id": session_id})
    assert response.status_code == 200, response.text


def _message_payload(runtime_id: str, *, provider_id: str = "provider-a") -> dict[str, str]:
    return {
        "content": "exercise the federated user path",
        "model": "default",
        "provider_id": provider_id,
        "runtime": runtime_id,
    }


def _wait_for_terminal(
    client: TestClient,
    session_id: str,
    command_id: str,
    payload: dict[str, str],
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers={"Idempotency-Key": command_id},
            json=payload,
        )
        assert response.status_code in {200, 202}, response.text
        value = response.json()
        if response.status_code == 200:
            return value
        time.sleep(0.01)
    raise AssertionError(f"turn {session_id}/{command_id} did not become terminal")


def _terminal_events(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in response.get("events", [])
        if event.get("name") in TERMINAL_NAMES
    ]


@pytest.mark.parametrize("runtime_id", ["goose", "dsh"])
def test_service_message_path_is_idempotent_conflict_safe_and_restart_durable(
    tmp_path: Path, runtime_id: str
) -> None:
    """Catches reruns, mutable command identity, and duplicate recovery output."""
    database = tmp_path / f"{runtime_id}.sqlite"
    first_executor = _CompletingRuntime()
    session_id = f"session-{runtime_id}"
    command_id = "message-1"
    payload = _message_payload(runtime_id)

    with TestClient(_service_app(database, first_executor)) as client:
        _create_session(client, session_id)
        accepted = client.post(
            f"/api/sessions/{session_id}/messages",
            headers={"Idempotency-Key": command_id},
            json=payload,
        )
        assert accepted.status_code == 202, accepted.text
        completed = _wait_for_terminal(client, session_id, command_id, payload)
        duplicate = _wait_for_terminal(client, session_id, command_id, payload)

        assert completed == duplicate
        assert completed["command_id"] == command_id
        assert completed["status"] == "completed"
        assert len(_terminal_events(completed)) == 1

        for changed in (
            {**payload, "runtime": "dsh" if runtime_id == "goose" else "goose"},
            {**payload, "provider_id": "provider-b"},
            {**payload, "model": "changed-model"},
        ):
            conflict = client.post(
                f"/api/sessions/{session_id}/messages",
                headers={"Idempotency-Key": command_id},
                json=changed,
            )
            assert conflict.status_code == 409

    forbidden = _ForbiddenRuntime()
    with TestClient(_service_app(database, forbidden)) as restarted:
        recovered = _wait_for_terminal(restarted, session_id, command_id, payload)
        replay = restarted.get(f"/api/sessions/{session_id}/events")

    repository = ConversationRepository(database)
    messages = repository.list_messages(session_id)
    assert recovered == completed
    assert forbidden.called is False
    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.content for message in messages] == [
        payload["content"],
        "hello runtime",
    ]
    assert replay.text.count('"name": "turn_finished"') == 1
    assert first_executor.calls == 1


def test_service_cancel_path_has_one_cancelled_terminal(tmp_path: Path) -> None:
    """Catches cancellation that misses the active command or seals twice."""
    database = tmp_path / "cancel.sqlite"
    executor = _CancellableRuntime()
    payload = _message_payload("dsh")
    with TestClient(_service_app(database, executor)) as client:
        _create_session(client, "session-cancel")
        accepted = client.post(
            "/api/sessions/session-cancel/messages",
            headers={"Idempotency-Key": "message-cancel"},
            json=payload,
        )
        assert accepted.status_code == 202, accepted.text
        assert executor.started.wait(timeout=2.0)

        cancelled = client.post(
            "/api/sessions/session-cancel/interventions",
            headers={"Idempotency-Key": "cancel-1"},
            json={"kind": "cancel", "content": "stop"},
        )
        assert cancelled.status_code == 200, cancelled.text
        terminal = _wait_for_terminal(
            client, "session-cancel", "message-cancel", payload
        )

    assert terminal["status"] == "cancelled"
    assert len(_terminal_events(terminal)) == 1
    assert _terminal_events(terminal)[0]["name"] == "turn_failed"
    assert executor.cancelled_commands == [
        python_term_command_id("session-cancel", "message-cancel")
    ]


def test_failed_runtime_does_not_block_another_runtime(tmp_path: Path) -> None:
    """Catches one federated Runtime failure poisoning another session/lane."""
    database = tmp_path / "isolation.sqlite"
    with TestClient(_service_app(database, _IsolatingRuntime())) as client:
        _create_session(client, "session-goose")
        _create_session(client, "session-dsh")
        goose_payload = _message_payload("goose")
        dsh_payload = _message_payload("dsh")
        for session_id, payload in (
            ("session-goose", goose_payload),
            ("session-dsh", dsh_payload),
        ):
            accepted = client.post(
                f"/api/sessions/{session_id}/messages",
                headers={"Idempotency-Key": "message-1"},
                json=payload,
            )
            assert accepted.status_code == 202, accepted.text

        goose = _wait_for_terminal(
            client, "session-goose", "message-1", goose_payload
        )
        dsh = _wait_for_terminal(client, "session-dsh", "message-1", dsh_payload)

    assert goose["status"] == "failed"
    assert dsh["status"] == "completed"
    assert len(_terminal_events(goose)) == 1
    assert len(_terminal_events(dsh)) == 1


class _LiveWorkbenchClient:
    """HTTP-only adapter for an already unlocked, user-operated Workbench."""

    def __init__(self) -> None:
        base_url = os.environ.get("WORKBENCH_LIVE_ACCEPTANCE_BASE_URL")
        provider_id = os.environ.get("WORKBENCH_LIVE_PROVIDER_PROFILE_ID")
        model = os.environ.get("WORKBENCH_LIVE_MODEL")
        missing = [
            name
            for name, value in (
                ("WORKBENCH_LIVE_ACCEPTANCE_BASE_URL", base_url),
                ("WORKBENCH_LIVE_PROVIDER_PROFILE_ID", provider_id),
                ("WORKBENCH_LIVE_MODEL", model),
            )
            if not value
        ]
        if missing:
            raise AssertionError("live acceptance configuration missing: " + ", ".join(missing))
        headers: dict[str, str] = {}
        capability = os.environ.get("WORKBENCH_LIVE_CAPABILITY")
        if capability:
            headers["X-Workbench-Capability"] = capability
        self.provider_id = str(provider_id)
        self.model = str(model)
        self.client = httpx.Client(
            base_url=str(base_url).rstrip("/"), headers=headers, timeout=30.0
        )

    def __enter__(self) -> "_LiveWorkbenchClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.client.close()

    def _json(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        response = self.client.request(method, path, **kwargs)
        try:
            value = response.json()
        except ValueError as exc:
            raise AssertionError(f"{method} {path} did not return JSON") from exc
        if not isinstance(value, dict):
            raise AssertionError(f"{method} {path} did not return an object")
        return response.status_code, value

    def run(self, runtime_id: str) -> dict[str, Any]:
        nonce = uuid4().hex
        session_id = f"live-runtime-{runtime_id}-{nonce}"
        command_id = f"live-message-{nonce}"
        prompt = "Reply with exactly: FEDERATED_RUNTIME_LIVE_OK"
        payload = {
            "content": prompt,
            "model": self.model,
            "provider_id": self.provider_id,
            "runtime": runtime_id,
        }
        status, _ = self._json("POST", "/api/sessions", json={"session_id": session_id})
        assert status == 200
        status, accepted = self._json(
            "POST",
            f"/api/sessions/{session_id}/messages",
            headers={"Idempotency-Key": command_id},
            json=payload,
        )
        assert status == 202, f"live {runtime_id} message was not accepted ({status})"
        deadline = time.monotonic() + float(
            os.environ.get("WORKBENCH_LIVE_ACCEPTANCE_TIMEOUT_SECONDS", "120")
        )
        terminal = accepted
        while time.monotonic() < deadline:
            status, terminal = self._json(
                "POST",
                f"/api/sessions/{session_id}/messages",
                headers={"Idempotency-Key": command_id},
                json=payload,
            )
            if status == 200:
                break
            assert status == 202, f"live {runtime_id} poll failed ({status})"
            time.sleep(0.25)
        else:
            raise AssertionError(f"live {runtime_id} did not reach a terminal state")

        duplicate_status, duplicate = self._json(
            "POST",
            f"/api/sessions/{session_id}/messages",
            headers={"Idempotency-Key": command_id},
            json=payload,
        )
        admission_status, admission = self._json(
            "GET",
            f"/api/sessions/{session_id}/runtime-admissions/{command_id}",
        )
        alternate_runtime = "dsh" if runtime_id != "dsh" else "goose"
        conflict_statuses = []
        for changed in (
            {**payload, "runtime": alternate_runtime},
            {**payload, "provider_id": self.provider_id + "-identity-conflict"},
            {**payload, "model": self.model + "-identity-conflict"},
        ):
            conflict_status, _ = self._json(
                "POST",
                f"/api/sessions/{session_id}/messages",
                headers={"Idempotency-Key": command_id},
                json=changed,
            )
            conflict_statuses.append(conflict_status)
        return {
            "command_id": command_id,
            "terminal": terminal,
            "duplicate_status": duplicate_status,
            "duplicate": duplicate,
            "admission_status": admission_status,
            "admission": admission,
            "conflict_statuses": conflict_statuses,
        }


@pytest.mark.skipif(not LIVE_OPT_IN, reason="live endpoint opt-in required")
@pytest.mark.parametrize("runtime_id", ["python-term", "goose", "dsh"])
def test_live_runtime_user_path_uses_saved_provider_and_unique_terminal(
    runtime_id: str,
) -> None:
    """Real endpoint proof: no fixture client and no direct credential access."""
    with _LiveWorkbenchClient() as live:
        result = live.run(runtime_id)

    terminal = result["terminal"]
    admission = result["admission"]
    assert terminal["status"] == "completed"
    assert terminal["command_id"] == result["command_id"]
    assert len(_terminal_events(terminal)) == 1
    assert any(
        event.get("type") == "TEXT_MESSAGE_CONTENT" and event.get("delta")
        for event in terminal["events"]
    )
    assert result["duplicate_status"] == 200
    assert result["duplicate"] == terminal
    assert result["conflict_statuses"] == [409, 409, 409]
    assert result["admission_status"] == 200
    assert admission["selector"] == runtime_id
    assert admission["runtime_id"] == runtime_id
    assert admission["state"] == "ready"
    assert admission["trust_status"] in {"DEV_UNTRUSTED", "PRODUCTION_TRUSTED"}
    assert "fake" not in str(admission["build_id"]).lower()
