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


def test_codec_rejects_sensitive_nested_payload_during_encoding() -> None:
    unsafe = HostEnvelope.model_construct(
        message_id="command-sensitive-encode",
        kind="command",
        name="host.hello",
        payload={"accessKey": "redacted"},
    )

    with pytest.raises(HostProtocolError, match="sensitive"):
        encode_frame(unsafe)


def test_codec_rejects_sensitive_nested_payload_during_decoding() -> None:
    with pytest.raises(HostProtocolError, match="sensitive"):
        decode_frame(
            b'{"message_id":"command-sensitive-decode","kind":"command",'
            b'"name":"host.hello","payload":{"access_key":"redacted"}}\n'
        )


def test_codec_rejects_trailing_blank_ndjson_frame() -> None:
    with pytest.raises(HostProtocolError, match="newline terminated"):
        decode_frame(
            b'{"message_id":"command-double-newline","kind":"command",'
            b'"name":"host.hello","payload":{}}\n\n'
        )
