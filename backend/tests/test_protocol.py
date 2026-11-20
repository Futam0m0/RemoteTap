"""Tests for message encoding and inbound validation."""

from __future__ import annotations

import json

import pytest

from remote_tap.protocol.messages import (
    Ack,
    CommandType,
    ErrorCode,
    ErrorMessage,
    encode_line,
    encode_response,
)
from remote_tap.protocol.validate import ProtocolError, parse_command


def test_parses_a_valid_press_start() -> None:
    line = b'{"type":"PRESS_START","command_id":"c1","session_id":"s1"}'

    command = parse_command(line)

    assert command.type is CommandType.PRESS_START
    assert command.command_id == "c1"
    assert command.session_id == "s1"


def test_parses_a_valid_press_stop() -> None:
    line = b'{"type": "PRESS_STOP", "command_id": "c2", "session_id": "s1"}'

    assert parse_command(line).type is CommandType.PRESS_STOP


def test_tolerates_crlf_line_endings() -> None:
    line = b'{"type":"PRESS_START","command_id":"c1","session_id":"s1"}\r'

    assert parse_command(line).command_id == "c1"


@pytest.mark.parametrize(
    ("line", "expected_code"),
    [
        (b"not json at all", ErrorCode.BAD_JSON),
        (b'{"type":"PRESS_START",', ErrorCode.BAD_JSON),
        (b"\xff\xfe\x00", ErrorCode.BAD_JSON),
        (b"[1, 2, 3]", ErrorCode.NOT_AN_OBJECT),
        (b'"PRESS_START"', ErrorCode.NOT_AN_OBJECT),
        (b'{"command_id":"c1","session_id":"s1"}', ErrorCode.MISSING_FIELD),
        (b'{"type":42,"command_id":"c1","session_id":"s1"}', ErrorCode.INVALID_FIELD),
        (b'{"type":"REBOOT","command_id":"c1","session_id":"s1"}', ErrorCode.UNKNOWN_COMMAND),
        (b'{"type":"PRESS_START","session_id":"s1"}', ErrorCode.MISSING_FIELD),
        (b'{"type":"PRESS_START","command_id":"c1"}', ErrorCode.MISSING_FIELD),
        (b'{"type":"PRESS_START","command_id":7,"session_id":"s1"}', ErrorCode.INVALID_FIELD),
        (b'{"type":"PRESS_START","command_id":"c1","session_id":""}', ErrorCode.INVALID_FIELD),
        (b'{"type":"PRESS_START","command_id":"c1","session_id":"   "}', ErrorCode.INVALID_FIELD),
        (b'{"type":"PRESS_START","command_id":null,"session_id":"s1"}', ErrorCode.MISSING_FIELD),
    ],
)
def test_rejects_invalid_messages(line: bytes, expected_code: ErrorCode) -> None:
    with pytest.raises(ProtocolError) as excinfo:
        parse_command(line)

    assert excinfo.value.code is expected_code


def test_unknown_command_error_carries_the_command_id() -> None:
    with pytest.raises(ProtocolError) as excinfo:
        parse_command(b'{"type":"FLY","command_id":"c9","session_id":"s1"}')

    assert excinfo.value.command_id == "c9"
    assert "FLY" in excinfo.value.message


def test_error_before_a_readable_command_id_has_none() -> None:
    with pytest.raises(ProtocolError) as excinfo:
        parse_command(b'{"type":"PRESS_START","session_id":"s1"}')

    assert excinfo.value.command_id is None


def test_ack_serialises_to_a_single_newline_terminated_line() -> None:
    encoded = encode_response(Ack(command_id="c1", state="PRESSING"))

    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert json.loads(encoded.decode("utf-8")) == {
        "type": "ACK",
        "command_id": "c1",
        "state": "PRESSING",
    }


def test_error_serialises_with_code_message_and_state() -> None:
    payload = ErrorMessage(
        code=ErrorCode.INVALID_SESSION,
        message="nope",
        command_id="c1",
        state="IDLE",
    ).to_dict()

    assert payload == {
        "type": "ERROR",
        "code": "INVALID_SESSION",
        "message": "nope",
        "command_id": "c1",
        "state": "IDLE",
    }


def test_error_without_a_known_state_omits_the_field() -> None:
    payload = ErrorMessage(
        code=ErrorCode.BAD_JSON, message="bad", command_id=None
    ).to_dict()

    assert "state" not in payload
    assert payload["command_id"] is None


def test_encode_line_is_compact_ascii_safe_json() -> None:
    encoded = encode_line({"type": "ACK", "note": "\u00e9"})

    # Non-ASCII is escaped, so the wire format is plain ASCII and cannot be
    # mangled by a transport that is not UTF-8 aware.
    assert encoded == b'{"type":"ACK","note":"\\u00e9"}\n'
    assert json.loads(encoded.decode("ascii"))["note"] == "\u00e9"
