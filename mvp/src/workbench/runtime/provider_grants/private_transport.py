"""Bounded one-shot Provider Grant transport for a fenced sidecar process."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import math
import os
import socket
import struct
from typing import Any

from .contracts import (
    ProviderGrantAck,
    ProviderGrantBinding,
    canonical_grant_digest,
)


_GRANT_MAGIC = b"JAGTGRN1"
_ACK_MAGIC = b"JAGTACK1"
_WIRE_VERSION = 1
_GRANT_PREFIX = struct.Struct("!8sBII")
_ACK_PREFIX = struct.Struct("!8sBI")
_ACK_FIELDS = frozenset(
    {
        "schema",
        "grant_id",
        "grant_digest",
        "target_instance_digest",
    }
)
_ACK_SCHEMA = "workbench.runtime.provider_grant_ack.v1"
_GRANT_SCHEMA = "workbench.runtime.provider_grant_private.v1"
_DEFAULT_MAX_HEADER_BYTES = 65_536
_DEFAULT_MAX_SECRET_BYTES = 65_536
_DEFAULT_MAX_ACK_BYTES = 8_192


class ProviderGrantTransportError(RuntimeError):
    """The private one-shot transport is unavailable or ambiguous."""


class SocketProviderGrantDelivery:
    """Deliver exactly one binding and secret over one private socket endpoint."""

    def __init__(
        self,
        endpoint: socket.socket,
        *,
        clock: Callable[[], float],
        max_header_bytes: int,
        max_secret_bytes: int,
        max_ack_bytes: int,
    ) -> None:
        self._endpoint = endpoint
        self._endpoint.setblocking(False)
        self._clock = clock
        self._max_header_bytes = _positive_bound(
            max_header_bytes, "header size"
        )
        self._max_secret_bytes = _positive_bound(
            max_secret_bytes, "secret size"
        )
        self._max_ack_bytes = _positive_bound(max_ack_bytes, "ack size")
        self._lock = asyncio.Lock()
        self._attempted = False
        self._closed = False
        self._delivery_task: asyncio.Task[ProviderGrantAck] | None = None

    async def deliver(
        self,
        binding: ProviderGrantBinding,
        secret: memoryview,
    ) -> ProviderGrantAck:
        if not isinstance(binding, ProviderGrantBinding):
            raise TypeError("binding must be a ProviderGrantBinding")
        if not isinstance(secret, memoryview):
            raise TypeError("secret must be a memoryview")
        if secret.ndim != 1 or not secret.contiguous:
            raise ProviderGrantTransportError("provider grant secret is not contiguous")
        secret_bytes = secret.cast("B")
        expected_empty = binding.route.credential_mode == "none"
        if (
            expected_empty and secret_bytes.nbytes != 0
            or not expected_empty
            and not 0 < secret_bytes.nbytes <= self._max_secret_bytes
        ):
            raise ProviderGrantTransportError("provider grant secret size is invalid")

        async with self._lock:
            if self._closed:
                raise ProviderGrantTransportError("provider grant transport is closed")
            if self._attempted:
                raise ProviderGrantTransportError(
                    "provider grant delivery was already attempted"
                )
            self._attempted = True
            current = asyncio.current_task()
            if current is None:
                raise ProviderGrantTransportError(
                    "provider grant delivery has no active task"
                )
            self._delivery_task = current
            try:
                return await self._deliver_once(binding, secret_bytes)
            except asyncio.CancelledError:
                interrupted_by_close = self._closed
                self._close()
                if interrupted_by_close:
                    raise ProviderGrantTransportError(
                        "provider grant transport failed"
                    ) from None
                raise
            except ProviderGrantTransportError:
                self._close()
                raise
            except (ConnectionError, OSError) as error:
                self._close()
                raise ProviderGrantTransportError(
                    "provider grant transport failed"
                ) from error
            finally:
                if self._delivery_task is current:
                    self._delivery_task = None

    async def _deliver_once(
        self,
        binding: ProviderGrantBinding,
        secret: memoryview,
    ) -> ProviderGrantAck:
        digest = canonical_grant_digest(binding)
        header = json.dumps(
            {
                "schema": _GRANT_SCHEMA,
                "binding": binding.model_dump(mode="json"),
                "grant_digest": digest,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(header) > self._max_header_bytes:
            raise ProviderGrantTransportError(
                "provider grant header size is invalid"
            )
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(
            self._endpoint,
            _GRANT_PREFIX.pack(
                _GRANT_MAGIC,
                _WIRE_VERSION,
                len(header),
                secret.nbytes,
            ),
        )
        await loop.sock_sendall(self._endpoint, header)
        await loop.sock_sendall(self._endpoint, secret)

        prefix = await _receive_exact(loop, self._endpoint, _ACK_PREFIX.size)
        magic, version, payload_size = _ACK_PREFIX.unpack(prefix)
        if magic != _ACK_MAGIC or version != _WIRE_VERSION:
            raise ProviderGrantTransportError(
                "provider grant acknowledgement framing is invalid"
            )
        if not 0 < payload_size <= self._max_ack_bytes:
            raise ProviderGrantTransportError(
                "provider grant acknowledgement size is invalid"
            )
        encoded = await _receive_exact(loop, self._endpoint, payload_size)
        try:
            document = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderGrantTransportError(
                "provider grant acknowledgement is invalid"
            ) from None
        if (
            not isinstance(document, dict)
            or set(document) != _ACK_FIELDS
            or document.get("schema") != _ACK_SCHEMA
            or document.get("grant_id") != binding.grant_id
            or document.get("grant_digest") != digest
            or document.get("target_instance_digest")
            != binding.target.instance_id_digest
        ):
            raise ProviderGrantTransportError(
                "provider grant acknowledgement identity is invalid"
            )
        acknowledged_at = self._clock()
        if (
            isinstance(acknowledged_at, bool)
            or not isinstance(acknowledged_at, (int, float))
            or not math.isfinite(float(acknowledged_at))
            or float(acknowledged_at) <= 0
        ):
            raise ProviderGrantTransportError(
                "provider grant acknowledgement time is invalid"
            )
        return ProviderGrantAck(
            grant_id=binding.grant_id,
            grant_digest=digest,
            target_instance_digest=binding.target.instance_id_digest,
            acknowledged_at=float(acknowledged_at),
        )

    async def aclose(self) -> None:
        # Closing the endpoint must interrupt a delivery that is blocked waiting
        # for an acknowledgement.  Waiting for ``_lock`` first would deadlock
        # shutdown because ``deliver`` owns it for the whole one-shot exchange.
        self._close()
        task = self._delivery_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._endpoint.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._endpoint.close()


def open_provider_grant_socketpair(
    *,
    clock: Callable[[], float],
    max_header_bytes: int = _DEFAULT_MAX_HEADER_BYTES,
    max_secret_bytes: int = _DEFAULT_MAX_SECRET_BYTES,
    max_ack_bytes: int = _DEFAULT_MAX_ACK_BYTES,
) -> tuple[SocketProviderGrantDelivery, socket.socket]:
    """Return the parent delivery endpoint and caller-owned sidecar endpoint."""

    if os.name != "posix" or not hasattr(socket, "socketpair"):
        raise ProviderGrantTransportError(
            "provider grant private transport is unavailable on this platform"
        )
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        delivery = SocketProviderGrantDelivery(
            parent,
            clock=clock,
            max_header_bytes=max_header_bytes,
            max_secret_bytes=max_secret_bytes,
            max_ack_bytes=max_ack_bytes,
        )
    except BaseException:
        parent.close()
        child.close()
        raise
    child.set_inheritable(False)
    return delivery, child


async def _receive_exact(
    loop: asyncio.AbstractEventLoop,
    endpoint: socket.socket,
    size: int,
) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = await loop.sock_recv(endpoint, remaining)
        if not chunk:
            raise ProviderGrantTransportError(
                "provider grant acknowledgement channel closed"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _positive_bound(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


__all__ = [
    "ProviderGrantTransportError",
    "SocketProviderGrantDelivery",
    "open_provider_grant_socketpair",
]
