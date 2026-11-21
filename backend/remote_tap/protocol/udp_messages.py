"""Datagram messages for the RemoteTap UDP status channel.

The UDP channel is a **status** channel, not a command channel. The device sends
these datagrams and a monitoring client reads them; nothing in a datagram can
press, renew or release anything. Only the TCP command channel changes the
actuator (see ``docs/protocol.md``).

Each datagram is a single small JSON object, encoded as UTF-8. Unlike the TCP
channel there is no newline framing and no acknowledgement: UDP gives neither
delivery, nor ordering, nor uniqueness, so every datagram carries its own
sequence number and the receiver is responsible for spotting losses, duplicates
and stale data. Every datagram also carries a ``boot_id``: the sequence number
restarts at 1 when the device process restarts, so the boot id lets a receiver
tell "the device rebooted" apart from "this packet is out of order" (see
``docs/protocol.md``).

This module is the *only* place that knows the datagram's shape. It has no
socket code, so it can be tested without any networking at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

#: Version of the UDP datagram format. A receiver must reject a datagram whose
#: version it does not understand rather than guess at its fields.
UDP_PROTOCOL_VERSION = 1

#: A periodic liveness report. Sent at the configured interval.
HEARTBEAT = "heartbeat"

#: A status report. Sent when the device notices its own state changed, so a
#: monitoring client sees a press or release sooner than the next heartbeat.
STATUS = "status"

#: The message types a device may send.
MESSAGE_TYPES = frozenset({HEARTBEAT, STATUS})

#: The only service status reported today: the service is running normally.
STATUS_OK = "ok"

#: Largest datagram we will parse. The real datagrams are a fraction of this;
#: the limit just stops a nonsense datagram from being treated as valid.
MAX_DATAGRAM_BYTES = 512


class DatagramErrorCode(str, Enum):
    """Why a datagram was rejected by :func:`parse_datagram`."""

    #: Not valid UTF-8, not valid JSON, or larger than :data:`MAX_DATAGRAM_BYTES`.
    MALFORMED = "MALFORMED"
    #: Valid JSON, but not a single JSON object.
    NOT_AN_OBJECT = "NOT_AN_OBJECT"
    #: A required field is absent or null.
    MISSING_FIELD = "MISSING_FIELD"
    #: A field is present but is not the right type or value.
    INVALID_FIELD = "INVALID_FIELD"
    #: ``v`` names a protocol version this receiver does not understand.
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    #: ``type`` is not a message type this receiver understands.
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    #: ``seq`` is not a non-negative integer.
    INVALID_SEQUENCE = "INVALID_SEQUENCE"


class DatagramError(ValueError):
    """Raised when an inbound datagram is not a valid status message.

    Carries a machine-readable :class:`DatagramErrorCode` and a human-readable
    message. A receiver should drop the datagram and carry on: a bad datagram
    must never stop the monitoring loop.
    """

    def __init__(self, code: DatagramErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StatusDatagram:
    """One UDP heartbeat or status datagram.

    Args:
        type: :data:`HEARTBEAT` or :data:`STATUS`.
        device_id: which device sent this; lets one monitor watch several. It is
            stable across device restarts.
        boot_id: identifies one *run* of the device service. A fresh, unique id
            is generated each time the heartbeat service starts, and it stays
            constant for that run. Because the sequence number resets on a
            restart, a receiver keys its sequence tracking on
            ``(device_id, boot_id)`` so a restarted device is recognised as a
            new instance rather than a flood of out-of-order packets. It is an
            identity for correlation only: it does **not** authenticate the
            datagram.
        sequence: 1-based, incremented for every datagram the device *sends*.
            A receiver uses it to spot losses, duplicates and reordering.
        timestamp_ms: the sender's wall-clock time in milliseconds. For display
            only: a receiver must never use it for staleness, because the two
            clocks are not synchronised.
        state: the actuator state, ``"IDLE"`` or ``"PRESSING"``.
        session_active: whether a press session is active.
        status: the device's own service status, :data:`STATUS_OK` today.
        version: the datagram format version.
    """

    type: str
    device_id: str
    boot_id: str
    sequence: int
    timestamp_ms: int
    state: str
    session_active: bool
    status: str = STATUS_OK
    version: int = UDP_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        """The wire form, using the short field names the protocol defines."""
        return {
            "v": self.version,
            "type": self.type,
            "device_id": self.device_id,
            "boot_id": self.boot_id,
            "seq": self.sequence,
            "ts": self.timestamp_ms,
            "status": self.status,
            "state": self.state,
            "session_active": self.session_active,
        }


def encode_datagram(message: StatusDatagram) -> bytes:
    """Serialise one datagram to UTF-8 JSON bytes (no trailing newline).

    ASCII-safe, like the TCP encoder, so any receiver can decode it as UTF-8
    without surprises.
    """
    return json.dumps(message.to_dict(), separators=(",", ":")).encode("utf-8")


def parse_datagram(data: bytes) -> StatusDatagram:
    """Parse one datagram into a validated :class:`StatusDatagram`.

    Raises:
        DatagramError: if the bytes are not a datagram this receiver understands.
            The caller should log and drop the datagram, never crash.
    """
    if len(data) > MAX_DATAGRAM_BYTES:
        raise DatagramError(
            DatagramErrorCode.MALFORMED,
            f"datagram exceeds {MAX_DATAGRAM_BYTES} bytes",
        )

    payload = _parse_json(data)

    if not isinstance(payload, dict):
        raise DatagramError(
            DatagramErrorCode.NOT_AN_OBJECT,
            f"datagram must be a JSON object, got {type(payload).__name__}",
        )

    version = payload.get("v")
    if version is None:
        raise DatagramError(
            DatagramErrorCode.MISSING_FIELD, "field 'v' is required"
        )
    if version != UDP_PROTOCOL_VERSION:
        raise DatagramError(
            DatagramErrorCode.UNSUPPORTED_VERSION,
            f"unsupported protocol version {version!r}; this receiver speaks "
            f"{UDP_PROTOCOL_VERSION}",
        )

    message_type = payload.get("type")
    if message_type is None:
        raise DatagramError(
            DatagramErrorCode.MISSING_FIELD, "field 'type' is required"
        )
    if message_type not in MESSAGE_TYPES:
        raise DatagramError(
            DatagramErrorCode.UNSUPPORTED_TYPE,
            f"unsupported message type {message_type!r}",
        )

    sequence = payload.get("seq")
    if sequence is None:
        raise DatagramError(
            DatagramErrorCode.MISSING_FIELD, "field 'seq' is required"
        )
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise DatagramError(
            DatagramErrorCode.INVALID_SEQUENCE,
            f"field 'seq' must be a non-negative integer, got {sequence!r}",
        )

    state = payload.get("state")
    if state is None:
        raise DatagramError(
            DatagramErrorCode.MISSING_FIELD, "field 'state' is required"
        )
    if state not in ("IDLE", "PRESSING"):
        raise DatagramError(
            DatagramErrorCode.INVALID_FIELD,
            f"field 'state' must be 'IDLE' or 'PRESSING', got {state!r}",
        )

    session_active = payload.get("session_active")
    if session_active is None:
        raise DatagramError(
            DatagramErrorCode.MISSING_FIELD, "field 'session_active' is required"
        )
    if not isinstance(session_active, bool):
        raise DatagramError(
            DatagramErrorCode.INVALID_FIELD,
            f"field 'session_active' must be a boolean, got {session_active!r}",
        )

    return StatusDatagram(
        type=message_type,
        device_id=_require_text(payload, "device_id"),
        boot_id=_require_text(payload, "boot_id"),
        sequence=sequence,
        timestamp_ms=_require_int(payload, "ts"),
        state=state,
        session_active=session_active,
        status=_optional_text(payload, "status", STATUS_OK),
        version=version,
    )


def _parse_json(data: bytes) -> Any:
    """Decode UTF-8 JSON bytes, reporting every failure as ``MALFORMED``."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DatagramError(
            DatagramErrorCode.MALFORMED, f"datagram is not valid UTF-8: {exc.reason}"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DatagramError(
            DatagramErrorCode.MALFORMED, f"invalid JSON: {exc.msg}"
        ) from exc


def _require_text(payload: Mapping[str, Any], field: str) -> str:
    """Return a required, non-empty string field or raise ``DatagramError``."""
    if field not in payload or payload[field] is None:
        raise DatagramError(
            DatagramErrorCode.MISSING_FIELD, f"field '{field}' is required"
        )
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise DatagramError(
            DatagramErrorCode.INVALID_FIELD,
            f"field '{field}' must be a non-empty string, got {value!r}",
        )
    return value


def _require_int(payload: Mapping[str, Any], field: str) -> int:
    """Return a required non-negative integer field or raise ``DatagramError``."""
    if field not in payload or payload[field] is None:
        raise DatagramError(
            DatagramErrorCode.MISSING_FIELD, f"field '{field}' is required"
        )
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatagramError(
            DatagramErrorCode.INVALID_FIELD,
            f"field '{field}' must be a non-negative integer, got {value!r}",
        )
    return value


def _optional_text(payload: Mapping[str, Any], field: str, default: str) -> str:
    """Return an optional string field, falling back to ``default``."""
    value = payload.get(field)
    if value is None:
        return default
    if not isinstance(value, str):
        raise DatagramError(
            DatagramErrorCode.INVALID_FIELD,
            f"field '{field}' must be a string, got {value!r}",
        )
    return value
