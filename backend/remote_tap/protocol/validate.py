"""Validation of inbound command lines.

These are pure functions: bytes in, :class:`Command` out. Anything that does not
look like a valid command raises :class:`ProtocolError`, which the network layer
turns into an ``ERROR`` response.

Only the validation this phase needs is implemented. Session ownership and hold
limits belong to the session and safety layers, not here.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .messages import Command, CommandType, ErrorCode


class ProtocolError(Exception):
    """Raised when an inbound line is not a valid command.

    Carries the error code to report, a human-readable message, and the
    ``command_id`` when it could be read (``None`` otherwise).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        command_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.command_id = command_id


def parse_command(line: bytes) -> Command:
    """Parse one newline-stripped line into a validated :class:`Command`.

    Args:
        line: raw message bytes, without the trailing newline.

    Raises:
        ProtocolError: if the line is not valid UTF-8, not valid JSON, not a
            JSON object, or does not describe a known command with the required
            fields.
    """
    text = _decode(line)
    payload = _parse_json(text)

    if not isinstance(payload, dict):
        raise ProtocolError(
            ErrorCode.NOT_AN_OBJECT,
            f"message must be a JSON object, got {type(payload).__name__}",
        )

    # Best-effort extraction of command_id so every later error can be matched
    # to the client's request.
    command_id = _peek_command_id(payload)

    raw_type = payload.get("type")
    if raw_type is None:
        raise ProtocolError(
            ErrorCode.MISSING_FIELD, "field 'type' is required", command_id
        )
    if not isinstance(raw_type, str):
        raise ProtocolError(
            ErrorCode.INVALID_FIELD, "field 'type' must be a string", command_id
        )

    try:
        command_type = CommandType(raw_type)
    except ValueError as exc:
        raise ProtocolError(
            ErrorCode.UNKNOWN_COMMAND,
            f"unknown command type {raw_type!r}",
            command_id,
        ) from exc

    return Command(
        type=command_type,
        command_id=_require_text(payload, "command_id", command_id),
        session_id=_require_text(payload, "session_id", command_id),
    )


def _decode(line: bytes) -> str:
    """Decode raw bytes as UTF-8."""
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(
            ErrorCode.BAD_JSON, f"message is not valid UTF-8: {exc.reason}"
        ) from exc


def _parse_json(text: str) -> Any:
    """Parse JSON text, reporting failures as ``BAD_JSON``."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(ErrorCode.BAD_JSON, f"invalid JSON: {exc.msg}") from exc


def _peek_command_id(payload: Mapping[str, Any]) -> str | None:
    """Return ``command_id`` if it is usable for error correlation."""
    value = payload.get("command_id")
    return value if isinstance(value, str) and value.strip() else None


def _require_text(payload: Mapping[str, Any], field: str, command_id: str | None) -> str:
    """Return a required, non-empty string field or raise ``ProtocolError``."""
    if field not in payload or payload[field] is None:
        raise ProtocolError(
            ErrorCode.MISSING_FIELD, f"field '{field}' is required", command_id
        )
    value = payload[field]
    if not isinstance(value, str):
        raise ProtocolError(
            ErrorCode.INVALID_FIELD, f"field '{field}' must be a string", command_id
        )
    if not value.strip():
        raise ProtocolError(
            ErrorCode.INVALID_FIELD, f"field '{field}' must not be empty", command_id
        )
    return value
