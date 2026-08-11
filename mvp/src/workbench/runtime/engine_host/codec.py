"""Bounded NDJSON serialization for Engine Host messages."""

from pydantic import ValidationError

from .contracts import HostEnvelope, HostFrameTooLarge, HostProtocolError


MAX_FRAME_BYTES = 1_048_576


def encode_frame(envelope: HostEnvelope) -> bytes:
    """Encode one Engine Host envelope as a bounded UTF-8 NDJSON frame."""
    try:
        validated = HostEnvelope.model_validate(envelope.model_dump())
    except (UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise HostProtocolError(
            "engine-host frame contains sensitive or invalid payload"
        ) from exc
    encoded = validated.model_dump_json(exclude_none=True).encode("utf-8") + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise HostFrameTooLarge("engine-host frame exceeds 1 MiB")
    return encoded


def decode_frame(value: bytes) -> HostEnvelope:
    """Decode one bounded, newline-terminated Engine Host NDJSON frame."""
    if len(value) > MAX_FRAME_BYTES:
        raise HostFrameTooLarge("engine-host frame exceeds 1 MiB")
    if not value.endswith(b"\n") or value[:-1].endswith(b"\n"):
        raise HostProtocolError("engine-host frame is not newline terminated")
    try:
        return HostEnvelope.model_validate_json(value[:-1])
    except (UnicodeDecodeError, ValidationError, ValueError) as exc:
        if "sensitive" in str(exc).casefold():
            raise HostProtocolError("engine-host frame contains sensitive payload") from exc
        raise HostProtocolError("invalid engine-host frame") from exc
