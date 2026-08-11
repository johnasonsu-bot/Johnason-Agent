import pytest

from workbench.runtime.engine_host.codec import (
    MAX_FRAME_BYTES,
    HostFrameTooLarge,
    HostProtocolError,
    decode_frame,
    encode_frame,
)
from workbench.runtime.engine_host.contracts import HostEnvelope


def test_codec_round_trips_one_utf8_ndjson_frame() -> None:
    frame = HostEnvelope(
        message_id="event-1",
        kind="event",
        name="agent.message.delta",
        run_id="run-1",
        sequence=1,
        payload={"content": "中文"},
    )
    assert decode_frame(encode_frame(frame)) == frame


def test_codec_rejects_oversized_frame_before_json_parse() -> None:
    with pytest.raises(HostFrameTooLarge):
        decode_frame(b"{" + b"x" * MAX_FRAME_BYTES + b"}\n")


def test_codec_rejects_frame_without_newline_terminator() -> None:
    with pytest.raises(HostProtocolError, match="not newline terminated"):
        decode_frame(b'{"message_id":"command-1"}')


def test_codec_wraps_invalid_json_as_protocol_error() -> None:
    with pytest.raises(HostProtocolError, match="invalid engine-host frame"):
        decode_frame(b"{not-json}\n")
