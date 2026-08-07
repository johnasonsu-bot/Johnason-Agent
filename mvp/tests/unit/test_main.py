import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import workbench.main as main
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
