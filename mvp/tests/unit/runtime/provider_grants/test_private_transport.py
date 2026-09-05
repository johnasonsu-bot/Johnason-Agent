from __future__ import annotations

import asyncio
import json
import socket
import struct
from typing import Any

import pytest

from workbench.runtime.provider_grants.contracts import (
    ProviderGrantBinding,
    ProviderGrantRouteV1,
    ProviderGrantTarget,
    canonical_grant_digest,
)
from workbench.runtime.provider_grants.private_transport import (
    ProviderGrantTransportError,
    open_provider_grant_socketpair,
)


_GRANT_PREFIX = struct.Struct("!8sBII")
_ACK_PREFIX = struct.Struct("!8sBI")


def _binding() -> ProviderGrantBinding:
    target = ProviderGrantTarget(
        runtime_id="goose",
        build_id="goose-build-001",
        lease_id="lease-001",
        instance_id_digest="1" * 64,
        instance_nonce_digest="2" * 64,
        host_generation="7",
        lease_generation_seq=3,
        expires_at=200.0,
    )
    return ProviderGrantBinding(
        grant_id="grant-001",
        target=target,
        session_id="session-001",
        command_id="command-001",
        run_id="run-001",
        term_id="term-001",
        step_id="step-001",
        provider_id="deepseek-primary",
        provider_profile_digest="4" * 64,
        route=ProviderGrantRouteV1(
            protocol="deepseek",
            base_url="https://api.deepseek.com",
            metadata_headers=(),
            thinking_enabled=True,
            reasoning_effort="high",
        ),
        model="deepseek-chat",
        scopes=("inference",),
        issued_at=100.0,
        expires_at=150.0,
        grant_nonce_digest="3" * 64,
    )


def _read_exact(peer: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = peer.recv(remaining)
        if not chunk:
            raise AssertionError("private transport closed before the frame completed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive_grant_and_ack(
    peer: socket.socket,
    *,
    ack_updates: dict[str, object] | None = None,
) -> tuple[dict[str, Any], bytes]:
    magic, version, header_size, secret_size = _GRANT_PREFIX.unpack(
        _read_exact(peer, _GRANT_PREFIX.size)
    )
    assert magic == b"JAGTGRN1"
    assert version == 1
    header = json.loads(_read_exact(peer, header_size))
    secret = _read_exact(peer, secret_size)
    ack: dict[str, object] = {
        "schema": "workbench.runtime.provider_grant_ack.v1",
        "grant_id": header["binding"]["grant_id"],
        "grant_digest": header["grant_digest"],
        "target_instance_digest": header["binding"]["target"][
            "instance_id_digest"
        ],
    }
    if ack_updates:
        ack.update(ack_updates)
    encoded = json.dumps(
        ack, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    peer.sendall(_ACK_PREFIX.pack(b"JAGTACK1", 1, len(encoded)) + encoded)
    return header, secret


@pytest.mark.skipif(not hasattr(socket, "socketpair"), reason="socketpair unavailable")
@pytest.mark.asyncio
async def test_socket_delivery_sends_secret_separately_and_returns_bound_ack() -> None:
    delivery, peer = open_provider_grant_socketpair(clock=lambda: 120.0)
    binding = _binding()
    secret = bytearray(b"private-provider-value")
    try:
        receiver = asyncio.create_task(asyncio.to_thread(_receive_grant_and_ack, peer))

        ack = await delivery.deliver(binding, memoryview(secret))
        header, observed_secret = await receiver

        assert observed_secret == secret
        assert header == {
            "schema": "workbench.runtime.provider_grant_private.v1",
            "binding": binding.model_dump(mode="json"),
            "grant_digest": canonical_grant_digest(binding),
        }
        assert bytes(secret) not in json.dumps(header, sort_keys=True).encode()
        assert ack.grant_id == binding.grant_id
        assert ack.grant_digest == canonical_grant_digest(binding)
        assert ack.target_instance_digest == binding.target.instance_id_digest
        assert ack.acknowledged_at == 120.0
    finally:
        peer.close()
        await delivery.aclose()


@pytest.mark.skipif(not hasattr(socket, "socketpair"), reason="socketpair unavailable")
@pytest.mark.asyncio
async def test_no_credential_route_sends_an_explicit_zero_length_payload() -> None:
    delivery, peer = open_provider_grant_socketpair(clock=lambda: 120.0)
    binding = _binding().model_copy(
        update={
            "route": ProviderGrantRouteV1(
                protocol="lmstudio",
                base_url="http://127.0.0.1:1234/v1",
                credential_mode="none",
                metadata_headers=(),
                thinking_enabled=False,
                reasoning_effort="high",
            )
        }
    )
    secret = bytearray()
    try:
        receiver = asyncio.create_task(asyncio.to_thread(_receive_grant_and_ack, peer))
        ack = await delivery.deliver(binding, memoryview(secret))
        header, observed_secret = await receiver
        assert header["binding"]["route"]["credential_mode"] == "none"
        assert observed_secret == b""
        assert ack.grant_digest == canonical_grant_digest(binding)
    finally:
        peer.close()
        await delivery.aclose()


@pytest.mark.skipif(not hasattr(socket, "socketpair"), reason="socketpair unavailable")
@pytest.mark.asyncio
async def test_invalid_ack_fails_closed_and_delivery_cannot_be_retried() -> None:
    delivery, peer = open_provider_grant_socketpair(clock=lambda: 120.0)
    binding = _binding()
    secret = bytearray(b"private-provider-value")
    try:
        receiver = asyncio.create_task(
            asyncio.to_thread(
                _receive_grant_and_ack,
                peer,
                ack_updates={"grant_digest": "9" * 64},
            )
        )
        with pytest.raises(ProviderGrantTransportError, match="acknowledgement"):
            await delivery.deliver(binding, memoryview(secret))
        await receiver

        with pytest.raises(ProviderGrantTransportError, match="closed"):
            await delivery.deliver(binding, memoryview(secret))
    finally:
        peer.close()
        await delivery.aclose()


@pytest.mark.skipif(not hasattr(socket, "socketpair"), reason="socketpair unavailable")
@pytest.mark.asyncio
async def test_cancelled_delivery_closes_ambiguous_transport() -> None:
    delivery, peer = open_provider_grant_socketpair(clock=lambda: 120.0)
    binding = _binding()
    secret = bytearray(b"private-provider-value")
    started = asyncio.Event()

    async def hold_ack() -> None:
        await asyncio.to_thread(_read_exact, peer, _GRANT_PREFIX.size)
        started.set()
        await asyncio.sleep(10)

    receiver = asyncio.create_task(hold_ack())
    sending = asyncio.create_task(delivery.deliver(binding, memoryview(secret)))
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        with pytest.raises(ProviderGrantTransportError, match="closed"):
            await delivery.deliver(binding, memoryview(secret))
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        peer.close()
        await delivery.aclose()


@pytest.mark.skipif(not hasattr(socket, "socketpair"), reason="socketpair unavailable")
@pytest.mark.asyncio
async def test_close_interrupts_delivery_waiting_for_ack() -> None:
    delivery, peer = open_provider_grant_socketpair(clock=lambda: 120.0)
    binding = _binding()
    secret = bytearray(b"private-provider-value")
    prefix_received = asyncio.Event()

    async def receive_prefix_without_ack() -> None:
        await asyncio.to_thread(_read_exact, peer, _GRANT_PREFIX.size)
        prefix_received.set()
        await asyncio.sleep(10)

    receiver = asyncio.create_task(receive_prefix_without_ack())
    sending = asyncio.create_task(delivery.deliver(binding, memoryview(secret)))
    try:
        await asyncio.wait_for(prefix_received.wait(), timeout=1.0)
        await asyncio.wait_for(delivery.aclose(), timeout=1.0)
        with pytest.raises(ProviderGrantTransportError, match="transport failed"):
            await asyncio.wait_for(sending, timeout=1.0)
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        peer.close()
        await delivery.aclose()


@pytest.mark.skipif(not hasattr(socket, "socketpair"), reason="socketpair unavailable")
@pytest.mark.asyncio
async def test_oversized_secret_is_rejected_before_any_wire_write() -> None:
    delivery, peer = open_provider_grant_socketpair(
        clock=lambda: 120.0,
        max_secret_bytes=8,
    )
    peer.setblocking(False)
    try:
        with pytest.raises(ProviderGrantTransportError, match="secret size"):
            await delivery.deliver(_binding(), memoryview(bytearray(b"123456789")))
        with pytest.raises(BlockingIOError):
            peer.recv(1)
    finally:
        peer.close()
        await delivery.aclose()
