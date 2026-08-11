import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import workbench.main as main
from workbench.api.app import AppSettings, create_app
from workbench.conversations.worker import ConversationTaskWorker
from workbench.settings import WorkbenchSettings


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
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations

    async def start(self) -> None:
        self.operations.append("host.start")

    async def aclose(self) -> None:
        self.operations.append("host.close")


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
