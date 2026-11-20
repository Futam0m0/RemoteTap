"""Protocol layer: message definitions and validation.

This package knows nothing about sockets or hardware. It converts between
Python objects and the JSON lines that travel over TCP.
"""

from .messages import Ack, Command, CommandType, ErrorCode, ErrorMessage, encode_line
from .validate import ProtocolError, parse_command

__all__ = [
    "Ack",
    "Command",
    "CommandType",
    "ErrorCode",
    "ErrorMessage",
    "ProtocolError",
    "encode_line",
    "parse_command",
]
