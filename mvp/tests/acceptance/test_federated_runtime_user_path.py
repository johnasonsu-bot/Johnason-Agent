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
from workbench.runtime.agent_loop import AgentEvent
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
LIVE_MARKER = "FEDERATED_RUNTIME_LIVE_OK"
LIVE_RUNTIME_IDS = frozenset({"python-term", "goose", "dsh"})
TEST_BUILD_MAP_JSON = json.dumps(
    {
        "python-term": "python-term:test-build",
        "goose": "goose:test-build",
        "dsh": "dsh:test-build",
    }
)


class _NoopRunner:
    async def run_turn(self, _command: object) -> Any:
        if False:
            yield None


class _CompletingChatRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run_turn(self, command: Any) -> Any:
        self.calls += 1
        yield AgentEvent(
            kind="turn_started", session_id=command.session_id, run_id=command.run_id
        )
        yield AgentEvent(
            kind="text_delta",
            session_id=command.session_id,
            run_id=command.run_id,
            payload={"text": "chat reply"},
        )
        yield AgentEvent(
            kind="turn_finished", session_id=command.session_id, run_id=command.run_id
        )


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

    def __init__(self, *, build_ids: dict[str, str] | None = None) -> None:
        self.build_ids = {} if build_ids is None else dict(build_ids)

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
                "runtime.build_id": self.build_ids.get(
                    selector, f"{selector}:test"
                ),
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


def _service_app(
    database: Path,
    executor: Any,
    *,
    runner: Any | None = None,
    router: _OfflineRuntimeRouter | None = None,
):
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
            runner=_NoopRunner() if runner is None else runner,
            owner_id="runtime-user-path-acceptance",
            runtime_router=_OfflineRuntimeRouter() if router is None else router,
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


def test_chat_mode_omits_runtime_selector_and_uses_chat_runner(tmp_path: Path) -> None:
    """Catches chat being silently rewritten to an explicit Runtime selector."""
    database = tmp_path / "chat.sqlite"
    runner = _CompletingChatRunner()
    runtime = _CompletingRuntime()
    app = _service_app(database, runtime, runner=runner)
    payload = {
        "content": "exercise chat without a Runtime selector",
        "model": "default",
        "provider_id": "provider-a",
    }
    assert "runtime" not in payload

    with TestClient(app) as client:
        _create_session(client, "session-chat")
        accepted = client.post(
            "/api/sessions/session-chat/messages",
            headers={"Idempotency-Key": "message-chat"},
            json=payload,
        )
        assert accepted.status_code == 202, accepted.text
        completed = _wait_for_terminal(
            client, "session-chat", "message-chat", payload
        )
        empty_selector = client.post(
            "/api/sessions/session-chat/messages",
            headers={"Idempotency-Key": "message-empty-runtime"},
            json={**payload, "runtime": ""},
        )

    assert completed["status"] == "completed"
    assert len(_terminal_events(completed)) == 1
    assert empty_selector.status_code == 422
    assert runner.calls == 1
    assert runtime.calls == 0


def test_same_command_keeps_build_a_when_environment_moves_to_build_b(
    tmp_path: Path,
) -> None:
    """Catches a completed command being silently re-routed to a newer build."""
    database = tmp_path / "build-freeze.sqlite"
    router = _OfflineRuntimeRouter(build_ids={"goose": "goose:build-a"})
    payload = _message_payload("goose")
    with TestClient(
        _service_app(database, _CompletingRuntime(), router=router)
    ) as client:
        _create_session(client, "session-build")
        accepted = client.post(
            "/api/sessions/session-build/messages",
            headers={"Idempotency-Key": "message-build"},
            json=payload,
        )
        assert accepted.status_code == 202, accepted.text
        first = _wait_for_terminal(
            client, "session-build", "message-build", payload
        )

        router.build_ids["goose"] = "goose:build-b"
        replay = _wait_for_terminal(
            client, "session-build", "message-build", payload
        )
        next_payload = {**payload, "content": "new command uses current build"}
        next_accepted = client.post(
            "/api/sessions/session-build/messages",
            headers={"Idempotency-Key": "message-next-build"},
            json=next_payload,
        )
        assert next_accepted.status_code == 202, next_accepted.text
        _wait_for_terminal(
            client, "session-build", "message-next-build", next_payload
        )

    repository = ConversationRepository(database)
    frozen = repository.load_turn_status("session-build", "message-build")
    current = repository.load_turn_status("session-build", "message-next-build")
    assert replay == first
    assert frozen is not None
    assert frozen.state["runtime_build_id"] == "goose:build-a"
    assert frozen.state["runtime_execution"]["build_id"] == "goose:build-a"
    assert current is not None
    assert current.state["runtime_build_id"] == "goose:build-b"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:43127",
        "http://example.com:43127",
        "http://localhost.example:43127",
        "http://user@127.0.0.1:43127",
        "http://127.0.0.1:43127/api",
        "http://127.0.0.1:43127/?target=remote",
    ],
)
def test_live_client_rejects_non_loopback_or_ambiguous_base_urls(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """Catches a capability-bearing client accepting a larger trust boundary."""
    monkeypatch.setenv("WORKBENCH_LIVE_ACCEPTANCE_BASE_URL", base_url)
    monkeypatch.setenv("WORKBENCH_LIVE_PROVIDER_PROFILE_ID", "provider-a")
    monkeypatch.setenv("WORKBENCH_LIVE_MODEL", "model-a")
    monkeypatch.setenv("WORKBENCH_LIVE_CAPABILITY", "sensitive-capability-value")
    monkeypatch.setenv(
        "WORKBENCH_LIVE_EXPECTED_BUILD_IDS_JSON", TEST_BUILD_MAP_JSON
    )

    with pytest.raises(AssertionError, match="loopback HTTP origin"):
        _LiveWorkbenchClient()


def test_live_client_rejects_redirect_without_forwarding_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a redirect moving the live request and capability off loopback."""
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://example.com/collect"},
            request=request,
        )

    monkeypatch.setenv(
        "WORKBENCH_LIVE_ACCEPTANCE_BASE_URL", "http://127.0.0.1:43127"
    )
    monkeypatch.setenv("WORKBENCH_LIVE_PROVIDER_PROFILE_ID", "provider-a")
    monkeypatch.setenv("WORKBENCH_LIVE_MODEL", "model-a")
    monkeypatch.setenv("WORKBENCH_LIVE_CAPABILITY", "sensitive-capability-value")
    monkeypatch.setenv(
        "WORKBENCH_LIVE_EXPECTED_BUILD_IDS_JSON", TEST_BUILD_MAP_JSON
    )
    with _LiveWorkbenchClient(transport=httpx.MockTransport(redirect)) as live:
        with pytest.raises(AssertionError, match="redirect"):
            live._json("GET", "/api/sessions/example/events")

    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"


@pytest.mark.parametrize(
    "build_ids",
    [
        "not-json",
        "{}",
        '{"python-term":"python:build","goose":"goose:build"}',
        '{"python-term":"","goose":"goose:build","dsh":"dsh:build"}',
        '{"python-term":"python:build","goose":"goose:build","dsh":"dsh:build","other":"x"}',
    ],
)
def test_live_client_requires_exact_expected_build_map(
    monkeypatch: pytest.MonkeyPatch, build_ids: str
) -> None:
    """Catches arbitrary ready builds satisfying the limited completion check."""
    monkeypatch.setenv(
        "WORKBENCH_LIVE_ACCEPTANCE_BASE_URL", "http://127.0.0.1:43127"
    )
    monkeypatch.setenv("WORKBENCH_LIVE_PROVIDER_PROFILE_ID", "provider-a")
    monkeypatch.setenv("WORKBENCH_LIVE_MODEL", "model-a")
    monkeypatch.setenv("WORKBENCH_LIVE_EXPECTED_BUILD_IDS_JSON", build_ids)

    with pytest.raises(AssertionError, match="expected build map"):
        _LiveWorkbenchClient()


def test_limited_live_completion_contract_requires_marker_and_expected_build() -> None:
    """Catches arbitrary text or an unexpected admitted build being called a pass."""
    result = {
        "command_id": "command-1",
        "terminal": {
            "command_id": "command-1",
            "status": "completed",
            "events": [
                {"type": "TEXT_MESSAGE_CONTENT", "delta": "FEDERATED_RUNTIME_LIVE_OK"},
                {"type": "CUSTOM", "name": "turn_finished"},
            ],
        },
        "duplicate_status": 200,
        "duplicate": None,
        "conflict_statuses": [409, 409, 409],
        "admission_status": 200,
        "admission": {
            "selector": "dsh",
            "runtime_id": "dsh",
            "build_id": "dsh:expected-build",
            "state": "ready",
            "trust_status": "DEV_UNTRUSTED",
        },
    }
    result["duplicate"] = result["terminal"]

    _assert_limited_live_completion(
        result, runtime_id="dsh", expected_build_id="dsh:expected-build"
    )
    wrong_marker = {**result, "terminal": {**result["terminal"], "events": [
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "arbitrary answer"},
        {"type": "CUSTOM", "name": "turn_finished"},
    ]}}
    with pytest.raises(AssertionError, match="exact marker"):
        _assert_limited_live_completion(
            wrong_marker, runtime_id="dsh", expected_build_id="dsh:expected-build"
        )
    with pytest.raises(AssertionError, match="expected build"):
        _assert_limited_live_completion(
            result, runtime_id="dsh", expected_build_id="dsh:other-build"
        )


class _LiveWorkbenchClient:
    """HTTP-only adapter for an already unlocked, user-operated Workbench."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        base_url = os.environ.get("WORKBENCH_LIVE_ACCEPTANCE_BASE_URL")
        provider_id = os.environ.get("WORKBENCH_LIVE_PROVIDER_PROFILE_ID")
        model = os.environ.get("WORKBENCH_LIVE_MODEL")
        expected_build_ids_json = os.environ.get(
            "WORKBENCH_LIVE_EXPECTED_BUILD_IDS_JSON"
        )
        missing = [
            name
            for name, value in (
                ("WORKBENCH_LIVE_ACCEPTANCE_BASE_URL", base_url),
                ("WORKBENCH_LIVE_PROVIDER_PROFILE_ID", provider_id),
                ("WORKBENCH_LIVE_MODEL", model),
                (
                    "WORKBENCH_LIVE_EXPECTED_BUILD_IDS_JSON",
                    expected_build_ids_json,
                ),
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
        try:
            expected_build_ids = json.loads(str(expected_build_ids_json))
        except (TypeError, ValueError):
            raise AssertionError("live acceptance requires an exact expected build map") from None
        if (
            not isinstance(expected_build_ids, dict)
            or set(expected_build_ids) != LIVE_RUNTIME_IDS
            or any(
                not isinstance(value, str) or not value
                for value in expected_build_ids.values()
            )
        ):
            raise AssertionError("live acceptance requires an exact expected build map")
        self.expected_build_ids = expected_build_ids
        url = httpx.URL(str(base_url))
        if (
            url.scheme != "http"
            or url.host not in {"127.0.0.1", "localhost", "::1"}
            or url.userinfo != b""
            or url.raw_path != b"/"
            or url.query != b""
            or url.fragment != ""
        ):
            raise AssertionError("live acceptance requires a loopback HTTP origin")
        self.client = httpx.Client(
            base_url=str(url).rstrip("/"),
            headers=headers,
            timeout=30.0,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> "_LiveWorkbenchClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.client.close()

    def _json(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        response = self.client.request(method, path, **kwargs)
        if 300 <= response.status_code < 400:
            raise AssertionError("live acceptance redirect rejected")
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
        prompt = f"Reply with exactly: {LIVE_MARKER}"
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


def _assert_limited_live_completion(
    result: dict[str, Any], *, runtime_id: str, expected_build_id: str
) -> None:
    """Verify only HTTP completion/identity facts exposed by the public API."""
    terminal = result["terminal"]
    admission = result["admission"]
    rendered_text = "".join(
        str(event.get("delta", ""))
        for event in terminal["events"]
        if event.get("type") == "TEXT_MESSAGE_CONTENT"
    )
    assert rendered_text == LIVE_MARKER, "live completion did not return exact marker"
    assert terminal["status"] == "completed"
    assert terminal["command_id"] == result["command_id"]
    assert len(_terminal_events(terminal)) == 1
    assert result["duplicate_status"] == 200
    assert result["duplicate"] == terminal
    assert result["conflict_statuses"] == [409, 409, 409]
    assert result["admission_status"] == 200
    assert admission["selector"] == runtime_id
    assert admission["runtime_id"] == runtime_id
    assert admission["state"] == "ready"
    assert admission["trust_status"] in {"DEV_UNTRUSTED", "PRODUCTION_TRUSTED"}
    assert admission["build_id"] == expected_build_id, (
        "live completion did not use the expected build"
    )


@pytest.mark.skipif(not LIVE_OPT_IN, reason="live endpoint opt-in required")
@pytest.mark.parametrize("runtime_id", ["python-term", "goose", "dsh"])
def test_live_runtime_limited_completion_uses_marker_and_expected_build(
    runtime_id: str,
) -> None:
    """Finite live check; Provider/Model proof remains a separate manual Gate."""
    with _LiveWorkbenchClient() as live:
        result = live.run(runtime_id)

    _assert_limited_live_completion(
        result,
        runtime_id=runtime_id,
        expected_build_id=live.expected_build_ids[runtime_id],
    )
