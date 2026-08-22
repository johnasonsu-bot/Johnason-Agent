"""Versioned contracts for the Engine Host sidecar boundary."""

from .contracts import (
    PROTOCOL_V1,
    HostCapabilities,
    HostEnvelope,
    HostFailurePhase,
    HostFrameTooLarge,
    HostProtocolError,
    HostStatus,
)
from .codec import MAX_FRAME_BYTES, decode_frame, encode_frame
from .client import (
    EngineHostClient,
    HostAdmissionUnknown,
    HostExecutionError,
    HostExecutionUnknown,
    HostRunRejected,
    HostSequenceError,
    HostTerminalError,
    HostUnavailable,
)

__all__ = [
    "PROTOCOL_V1",
    "HostCapabilities",
    "HostEnvelope",
    "HostFailurePhase",
    "HostFrameTooLarge",
    "HostProtocolError",
    "HostStatus",
    "MAX_FRAME_BYTES",
    "decode_frame",
    "encode_frame",
    "EngineHostClient",
    "HostAdmissionUnknown",
    "HostExecutionError",
    "HostExecutionUnknown",
    "HostRunRejected",
    "HostSequenceError",
    "HostTerminalError",
    "HostUnavailable",
]
