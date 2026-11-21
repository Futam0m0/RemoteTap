"""Protocol layer: message definitions and validation.

This package knows nothing about sockets or hardware. It converts between
Python objects and the JSON lines that travel over TCP, and between Python
objects and the JSON datagrams used by the UDP status channel.
"""

from .messages import Ack, Command, CommandType, ErrorCode, ErrorMessage, encode_line
from .udp_messages import (
    UDP_PROTOCOL_VERSION,
    DatagramError,
    DatagramErrorCode,
    StatusDatagram,
    encode_datagram,
    parse_datagram,
)
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
    "UDP_PROTOCOL_VERSION",
    "DatagramError",
    "DatagramErrorCode",
    "StatusDatagram",
    "encode_datagram",
    "parse_datagram",
]
