"""Configuration values for the RemoteTap device service.

Phase 1 only needs the TCP command channel. The UDP heartbeat timings and the
safety lease described in ``docs/architecture.md`` are intentionally not
implemented yet; only the limits that the TCP server itself needs live here.
"""

from dataclasses import dataclass

# Network defaults.
# The default host is the loopback address on purpose: the service must be
# reachable only when someone opts in (for example by passing the machine's
# Tailscale address), never by accident.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# How many bytes to read from a socket at a time. This is a buffer size only;
# it has nothing to do with message boundaries (TCP is a byte stream).
READ_CHUNK_BYTES = 4096

# Largest single message line we accept. A longer line is rejected rather than
# buffered forever, which also protects the server from an abusive client.
MAX_LINE_BYTES = 4096

# How many recently handled command ids to remember so that a retry of the same
# command can be answered from the cache instead of being executed twice.
MAX_DUPLICATE_COMMAND_IDS = 256


@dataclass(frozen=True)
class ServerConfig:
    """Network settings for the TCP command server."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    max_line_bytes: int = MAX_LINE_BYTES
