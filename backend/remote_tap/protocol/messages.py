"""Message definitions for the RemoteTap TCP command protocol.

The client sends three commands and the device answers with two responses:

* ``PRESS_START`` / ``PRESS_RENEW`` / ``PRESS_STOP`` sent by the client
* ``ACK`` / ``ERROR`` sent by the device

``PRESS_RENEW`` is the Phase 2 addition: a press is a *lease*, and the owning
connection keeps it alive by renewing it before the lease times out.

Each message is a single JSON object serialised on one line and terminated by a
newline character. The newline is the only framing the protocol has, which is
why every message must fit on one line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Union

#: Protocol version, for future compatibility checks. Not yet enforced.
PROTOCOL_VERSION = 1


class CommandType(str, Enum):
    """Commands a client may send."""

    PRESS_START = "PRESS_START"
    PRESS_RENEW = "PRESS_RENEW"
    PRESS_STOP = "PRESS_STOP"


class ErrorCode(str, Enum):
    """Machine-readable reasons why a message was rejected."""

    # Message-level problems (detected by the protocol layer).
    BAD_JSON = "BAD_JSON"
    NOT_AN_OBJECT = "NOT_AN_OBJECT"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_FIELD = "INVALID_FIELD"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    LINE_TOO_LONG = "LINE_TOO_LONG"

    # State-level problems (detected by the session layer).
    INVALID_SESSION = "INVALID_SESSION"
    #: A renewal or stop arrived for a press the device already ended because a
    #: safety limit was reached.
    LEASE_EXPIRED = "LEASE_EXPIRED"

    # Anything unexpected on our side.
    INTERNAL = "INTERNAL"


@dataclass(frozen=True)
class Command:
    """A command that passed validation."""

    type: CommandType
    command_id: str
    session_id: str


@dataclass(frozen=True)
class Ack:
    """A successful response.

    ``state`` is the actuator state *after* the command was applied, so the
    client can always tell what the device believes is true.

    ``renew_interval_ms`` and ``max_press_ms`` carry the device's lease policy
    on a successful press start or renewal, so the client knows how often to
    renew and how long a press can possibly last. They are omitted from replies
    that end a press, where no lease is running any more.
    """

    command_id: str
    state: str
    renew_interval_ms: int | None = None
    max_press_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "ACK",
            "command_id": self.command_id,
            "state": self.state,
        }
        if self.renew_interval_ms is not None:
            payload["renew_interval_ms"] = self.renew_interval_ms
        if self.max_press_ms is not None:
            payload["max_press_ms"] = self.max_press_ms
        return payload


@dataclass(frozen=True)
class ErrorMessage:
    """A rejection.

    ``command_id`` is echoed when we could read it, so the client can match the
    error to its request. ``state`` is the unchanged actuator state, when known.
    """

    code: ErrorCode
    message: str
    command_id: str | None = None
    state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "ERROR",
            "code": self.code.value,
            "message": self.message,
            "command_id": self.command_id,
        }
        if self.state is not None:
            payload["state"] = self.state
        return payload


#: Everything the device can send back.
Response = Union[Ack, ErrorMessage]


def encode_line(payload: Mapping[str, Any]) -> bytes:
    """Serialise a message into a single newline-terminated UTF-8 line.

    The newline is what lets the receiving side find message boundaries in a
    byte stream.
    """
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def encode_response(response: Response) -> bytes:
    """Serialise an :class:`Ack` or :class:`ErrorMessage`."""
    return encode_line(response.to_dict())
