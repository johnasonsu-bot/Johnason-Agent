import asyncio
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import workbench.main as main
from tests.fixtures.host_v2 import run_envelope
from workbench.api.app import AppSettings, create_app
from workbench.conversations.worker import ConversationTaskWorker
from workbench.conversations.models import ConversationMessage
from workbench.conversations.repository import ConversationRepository
from workbench.models.profiles import ProviderProfileRecord
from workbench.providers.repository import ProviderRepository
from workbench.runtime.engine_host.selector import RunnerSelector
from workbench.settings import RuntimeProcessConfig, WorkbenchSettings


class RecordingListener:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.events.append(("setsockopt", level, option, value))

    def bind(self, address: tuple[str, int]) -> None:
        self.events.append(("bind", address))

    def getsockname(self) -> tuple[str, int]:
        self.events.append(("getsockname",))
        return ("127.0.0.1", 43127)

    def close(self) -> None:
        self.events.append(("close",))


class StartedServer:
    def __init__(self, _config: object) -> None:
        self.config = _config
        self.started = False
        self.sockets: list[RecordingListener] | None = None

    async def serve(self, *, sockets: list[RecordingListener]) -> None:
        self.sockets = sockets
        self.started = True


def _settings(tmp_path: Path) -> WorkbenchSettings:
    return WorkbenchSettings(runtime_dir=tmp_path, host="127.0.0.1", port=0)


async def _serve_with_recording_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> tuple[RecordingListener, StartedServer]:
    listener = RecordingListener()
    server: StartedServer | None = None

    def listener_factory(_family: int, _kind: int) -> RecordingListener:
        return listener

    def server_factory(config: object) -> StartedServer:
        nonlocal server
        server = StartedServer(config)
        return server

    monkeypatch.setattr(main, "os", SimpleNamespace(name=platform), raising=False)
    monkeypatch.setattr(main.socket, "socket", listener_factory)
    monkeypatch.setattr(main.uvicorn, "Server", server_factory)
    monkeypatch.setattr(main, "build_app", lambda *_args, **_kwargs: object())

    await main._serve_electron_backend(_settings(tmp_path), "x" * 43, "instance")

    assert server is not None
    return listener, server


def test_windows_listener_is_exclusive_before_bind_and_reuses_selected_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exclusive = 0xC001
    monkeypatch.setattr(main.socket, "SO_EXCLUSIVEADDRUSE", exclusive, raising=False)

    listener, server = asyncio.run(
        _serve_with_recording_listener(tmp_path, monkeypatch, "nt")
    )

    assert listener.events.index(("setsockopt", socket.SOL_SOCKET, exclusive, 1)) < listener.events.index(
        ("bind", ("127.0.0.1", 0))
    )
    assert ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) not in listener.events
    assert listener.events.count(("getsockname",)) == 1
    assert server.sockets == [listener]
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 43127


def test_posix_listener_reuses_address_without_requesting_windows_exclusivity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exclusive = 0xC001
    monkeypatch.setattr(main.socket, "SO_EXCLUSIVEADDRUSE", exclusive, raising=False)

    listener, server = asyncio.run(
        _serve_with_recording_listener(tmp_path, monkeypatch, "posix")
    )

    assert listener.events.index(("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)) < listener.events.index(
        ("bind", ("127.0.0.1", 0))
    )
    assert ("setsockopt", socket.SOL_SOCKET, exclusive, 1) not in listener.events
    assert listener.events.count(("getsockname",)) == 1
    assert server.sockets == [listener]
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 43127


class NoopRunner:
    async def run_turn(self, command):
        if False:
            yield command

    async def execute_step(self, run_id: str, step_id: str):
        del run_id, step_id
        return None


class RecordingLifecycle:
    def __init__(self, operations: list[str], name: str = "host") -> None:
        self.operations = operations
        self.name = name

    async def start(self) -> None:
        self.operations.append(f"{self.name}.start")

    async def aclose(self) -> None:
        self.operations.append(f"{self.name}.close")


def test_engine_host_is_disabled_without_an_explicit_command(tmp_path: Path) -> None:
    settings = WorkbenchSettings(runtime_dir=tmp_path)

    assert settings.engine_host_enabled is False
    assert settings.engine_host_command == ()
    assert settings.engine_host_provider_allowlist == ("lmstudio",)


def test_engine_host_settings_survive_json_round_trip(tmp_path: Path) -> None:
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        engine_host_enabled=True,
        engine_host_command=("engine-host", "--stdio"),
        engine_host_provider_allowlist=("lmstudio", "local-secondary"),
    )

    restored = WorkbenchSettings.model_validate_json(settings.model_dump_json())

    assert restored.engine_host_enabled is True
    assert restored.engine_host_command == ("engine-host", "--stdio")
    assert restored.engine_host_provider_allowlist == ("lmstudio", "local-secondary")


def test_build_app_consumes_v2_runtime_config_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[dict[str, object]] = []
    lifecycle = RecordingLifecycle([], "sidecar")

    def supervisor_factory(**kwargs):
        created.append(kwargs)
        return lifecycle

    captured_settings: list[AppSettings] = []

    def app_factory(settings: AppSettings) -> SimpleNamespace:
        captured_settings.append(settings)
        return SimpleNamespace(state=SimpleNamespace())

    monkeypatch.setattr(main, "SidecarSupervisor", supervisor_factory)
    monkeypatch.setattr(main, "create_app", app_factory)
    runtime = RuntimeProcessConfig(runtime_id="fake-v2", argv=("fake-v2", "--stdio"))

    disabled = WorkbenchSettings(
        runtime_dir=tmp_path / "disabled",
        engine_host_v2_enabled=False,
        engine_host_v2_runtimes=(runtime,),
    )
    main.build_app(disabled, runner=NoopRunner())
    assert created == []
    assert captured_settings[-1].sidecar_lifecycle is None

    enabled = WorkbenchSettings(
        runtime_dir=tmp_path / "enabled",
        engine_host_v2_enabled=True,
        engine_host_v2_runtimes=(runtime,),
    )
    app = main.build_app(
        enabled, service_instance_id="app-instance-1", runner=NoopRunner()
    )

    assert len(created) == 1
    assert created[0]["runtimes"] == (runtime,)
    assert created[0]["app_instance_id"] == "app-instance-1"
    assert captured_settings[-1].sidecar_lifecycle is lifecycle
    assert app.state.sidecar_supervisor is lifecycle


def test_engine_host_settings_are_parsed_from_json_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = r"C:\Program Files\Hermes Engine\engine-host.exe"
    monkeypatch.setenv("WORKBENCH_ENGINE_HOST_ENABLED", "true")
    monkeypatch.setenv(
        "WORKBENCH_ENGINE_HOST_COMMAND_JSON",
        json.dumps([executable, "--stdio", "--label=local host"]),
    )
    monkeypatch.setenv(
        "WORKBENCH_ENGINE_HOST_PROVIDER_ALLOWLIST_JSON",
        json.dumps(["lmstudio", "studio-primary"]),
    )

    settings = main._settings_from_environment(
        WorkbenchSettings(runtime_dir=tmp_path)
    )

    assert settings.engine_host_enabled is True
    assert settings.engine_host_command == (
        executable,
        "--stdio",
        "--label=local host",
    )
    assert settings.engine_host_provider_allowlist == (
        "lmstudio",
        "studio-primary",
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WORKBENCH_ENGINE_HOST_ENABLED", "sometimes"),
        ("WORKBENCH_ENGINE_HOST_COMMAND_JSON", '"engine-host --stdio"'),
        ("WORKBENCH_ENGINE_HOST_PROVIDER_ALLOWLIST_JSON", '["lmstudio", 7]'),
    ],
)
def test_engine_host_environment_rejects_noncanonical_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="engine host"):
        main._settings_from_environment(WorkbenchSettings(runtime_dir=tmp_path))


def test_build_app_rejects_enabled_engine_host_without_command(tmp_path: Path) -> None:
    settings = WorkbenchSettings(runtime_dir=tmp_path, engine_host_enabled=True)

    with pytest.raises(ValueError, match="engine host command is required when enabled"):
        main.build_app(settings, runner=NoopRunner())


def test_build_app_composes_enabled_host_and_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    host = object()
    selector = object()

    def host_factory(command: tuple[str, ...]) -> object:
        captured["command"] = command
        return host

    def selector_factory(
        python_runner: object,
        host_runner: object,
        enabled: bool,
        provider_allowlist: tuple[str, ...],
    ) -> object:
        captured.update(
            python_runner=python_runner,
            host_runner=host_runner,
            enabled=enabled,
            provider_allowlist=provider_allowlist,
        )
        return selector

    def app_factory(settings: AppSettings) -> SimpleNamespace:
        captured["app_settings"] = settings
        return SimpleNamespace(state=SimpleNamespace())

    python_runner = NoopRunner()
    monkeypatch.setattr(main, "EngineHostClient", host_factory)
    monkeypatch.setattr(main, "RunnerSelector", selector_factory)
    monkeypatch.setattr(main, "create_app", app_factory)
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        engine_host_enabled=True,
        engine_host_command=("engine-host", "--stdio"),
        engine_host_provider_allowlist=("lmstudio", "local-secondary"),
    )

    main.build_app(settings, runner=python_runner)

    assert captured["command"] == ("engine-host", "--stdio")
    assert captured["python_runner"] is python_runner
    assert captured["host_runner"] is host
    assert captured["enabled"] is True
    assert captured["provider_allowlist"] == ("lmstudio", "local-secondary")
    app_settings = captured["app_settings"]
    assert isinstance(app_settings, AppSettings)
    assert app_settings.runner is selector
    assert app_settings.runner_lifecycle is selector


def test_real_build_app_selector_resolves_default_deepseek_and_context(
    tmp_path: Path,
) -> None:
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        engine_host_enabled=True,
        engine_host_command=("engine-host", "--stdio"),
        engine_host_provider_allowlist=("lmstudio",),
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    providers = ProviderRepository(settings.database)
    providers.save(
        ProviderProfileRecord(
            id="studio-primary",
            name="Local",
            protocol="lmstudio",
            base_url="http://127.0.0.1:1234",
        )
    )
    providers.save(
        ProviderProfileRecord.deepseek(
            id="deepseek-primary",
            secret_id="provider/" + "a" * 32,
        )
    )
    conversations = ConversationRepository(settings.database)
    conversations.create_session("session-1")
    conversations.append_message(
        ConversationMessage(
            session_id="session-1",
            command_id="earlier:user",
            role="user",
            content="earlier",
        )
    )

    app = main.build_app(settings)
    selector = app.state.execution_runner
    selector.host_runner._status = SimpleNamespace(state="ready")

    assert isinstance(selector, RunnerSelector)
    assert selector.resolve_profile(None).id == "studio-primary"
    assert selector.mode_for("session-1", "studio-primary", "local") == "engine_host"
    assert selector.resolve_profile("deepseek").id == "deepseek-primary"
    assert selector.mode_for("session-1", "deepseek-primary", "cloud") == "python"
    assert [message.content for message in selector.model_messages("session-1")] == [
        "earlier"
    ]


def test_each_build_app_host_instance_gets_a_new_comparable_generation(
    tmp_path: Path,
) -> None:
    settings = WorkbenchSettings(
        runtime_dir=tmp_path,
        engine_host_enabled=True,
        engine_host_command=("engine-host", "--stdio"),
    )

    first = main.build_app(settings, runner=NoopRunner()).state.execution_runner
    second = main.build_app(settings, runner=NoopRunner()).state.execution_runner

    assert UUID(first.host_generation)
    assert UUID(second.host_generation)
    assert first.host_generation != second.host_generation


def test_build_app_composes_one_shared_provider_grant_broker(
    tmp_path: Path,
) -> None:
    from workbench.runtime.provider_grants import (
        ProviderGrantBroker,
        ProviderGrantTarget,
        ProviderGrantUnavailable,
    )

    settings = WorkbenchSettings(runtime_dir=tmp_path)

    app = main.build_app(settings, runner=NoopRunner())

    assert isinstance(app.state.provider_grant_broker, ProviderGrantBroker)
    assert not hasattr(app.state.provider_grant_broker, "grants")
    assert all(
        "provider-grant" not in getattr(route, "path", "") for route in app.routes
    )
    envelope = run_envelope(
        overrides={
            "runtime": {
                "runtime_id": "goose",
                "build_id": "goose-build-001",
                "config_digest": "1" * 64,
                "host_generation": "7",
            }
        }
    )
    target = ProviderGrantTarget(
        runtime_id="goose",
        build_id="goose-build-001",
        lease_id="lease-without-supervisor",
        instance_id_digest="1" * 64,
        instance_nonce_digest="2" * 64,
        host_generation="7",
        lease_generation_seq=1,
        expires_at=4_102_444_800.0,
    )
    with pytest.raises(ProviderGrantUnavailable, match="runtime target"):
        app.state.provider_grant_broker.issue(envelope, target=target)


def test_create_app_passes_host_generation_to_worker_repository(
    tmp_path: Path,
) -> None:
    app = create_app(
        AppSettings(
            database=tmp_path / "generation.sqlite",
            runner=NoopRunner(),
            owner_id="api",
            host_generation="generation-1",
        )
    )

    with TestClient(app):
        assert (
            app.state.conversation_worker.repository.host_generation
            == "generation-1"
        )


def test_app_starts_host_before_worker_and_closes_after_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[str] = []
    lifecycle = RecordingLifecycle(operations)

    async def worker_start(self) -> None:
        operations.append("worker.start")

    async def worker_stop(self) -> None:
        operations.append("worker.stop")

    monkeypatch.setattr(ConversationTaskWorker, "start", worker_start)
    monkeypatch.setattr(ConversationTaskWorker, "stop", worker_stop)
    app = create_app(
        AppSettings(
            database=tmp_path / "app.sqlite",
            runner=NoopRunner(),
            owner_id="api",
            runner_lifecycle=lifecycle,
        )
    )

    with TestClient(app):
        assert operations[:2] == ["host.start", "worker.start"]

    assert operations[-2:] == ["worker.stop", "host.close"]


def test_app_starts_sidecar_before_worker_and_closes_it_after_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[str] = []
    runner_lifecycle = RecordingLifecycle(operations, "v1")
    sidecar_lifecycle = RecordingLifecycle(operations, "sidecar")

    async def worker_start(self) -> None:
        operations.append("worker.start")

    async def worker_stop(self) -> None:
        operations.append("worker.stop")

    monkeypatch.setattr(ConversationTaskWorker, "start", worker_start)
    monkeypatch.setattr(ConversationTaskWorker, "stop", worker_stop)
    app = create_app(
        AppSettings(
            database=tmp_path / "app.sqlite",
            runner=NoopRunner(),
            owner_id="api",
            runner_lifecycle=runner_lifecycle,
            sidecar_lifecycle=sidecar_lifecycle,
        )
    )

    assert app.state.sidecar_supervisor is sidecar_lifecycle
    with TestClient(app):
        assert operations[:3] == ["v1.start", "sidecar.start", "worker.start"]

    assert operations[-3:] == ["worker.stop", "sidecar.close", "v1.close"]
