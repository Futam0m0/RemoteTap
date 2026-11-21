"""Configuration values for the RemoteTap device service.

Phase 1 needed only the TCP command channel. Phase 2 adds the press lease: the
renewal interval the client is asked to use, the lease timeout the device
enforces, and the hard maximum press duration. Phase 3 adds the UDP heartbeat /
status channel: where heartbeats are sent, how often, and when a monitoring
client should treat the device as offline. Those values live here, so they are
configurable in one place rather than scattered as literals in the code.
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

# Phase 2 press-safety defaults.
#
# A press is a *lease*: the owning connection must keep renewing it. If it
# stops (a frozen phone, a dead app), the device releases the button on its
# own. Independently, no single press may last longer than the hard maximum,
# however diligently the client renews.
DEFAULT_RENEW_INTERVAL_MS = 250
DEFAULT_LEASE_TIMEOUT_MS = 1000
DEFAULT_MAX_PRESS_MS = 3000

# Upper safety limit on ``max_press_ms``, in milliseconds. A press longer than
# this is far beyond "hold the power button" and is almost always a mistake
# (for example a one-hour value typed into a config), so it is rejected loudly
# instead of being accepted silently. The limit only exists to catch
# misconfiguration; it is not a substitute for the reachable policy value.
MAX_ALLOWED_MAX_PRESS_MS = 10_000


@dataclass(frozen=True)
class ServerConfig:
    """Network settings for the TCP command server."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    max_line_bytes: int = MAX_LINE_BYTES


@dataclass(frozen=True)
class PressLimits:
    """Press-safety limits enforced by the device.

    Args:
        renew_interval_ms: how often the client is asked to renew the lease.
            Advertised to the client; the device does not enforce it directly.
        lease_timeout_ms: how long a press survives without a renewal before the
            device releases it (reason ``lease_timeout``).
        max_press_ms: hard ceiling on one continuous press, reached with or
            without renewals (reason ``max_press_exceeded``).

    Raises:
        ValueError: if the values cannot describe a working lease. Each value
            must be positive, the client must have room to miss at least one
            renewal before the lease lapses (``lease_timeout_ms`` at least twice
            ``renew_interval_ms``), the lease must not outlive the maximum press
            duration, and the maximum press duration must stay within
            :data:`MAX_ALLOWED_MAX_PRESS_MS`.
    """

    renew_interval_ms: int = DEFAULT_RENEW_INTERVAL_MS
    lease_timeout_ms: int = DEFAULT_LEASE_TIMEOUT_MS
    max_press_ms: int = DEFAULT_MAX_PRESS_MS

    def __post_init__(self) -> None:
        # Reject zero and negative values explicitly, one field at a time, so
        # the error names the offending value instead of failing a ratio check.
        if self.renew_interval_ms <= 0:
            raise ValueError("renew_interval_ms must be greater than zero")
        if self.lease_timeout_ms <= 0:
            raise ValueError("lease_timeout_ms must be greater than zero")
        if self.max_press_ms <= 0:
            raise ValueError("max_press_ms must be greater than zero")
        # At least one whole renewal interval of slack: an obedient client that
        # misses a single renewal must still be holding a valid lease.
        if self.lease_timeout_ms < 2 * self.renew_interval_ms:
            raise ValueError(
                "lease_timeout_ms must be at least twice renew_interval_ms, so a "
                "single missed renewal is still tolerated"
            )
        if self.max_press_ms < self.lease_timeout_ms:
            raise ValueError("max_press_ms must be at least lease_timeout_ms")
        if self.max_press_ms > MAX_ALLOWED_MAX_PRESS_MS:
            raise ValueError(
                f"max_press_ms must not exceed {MAX_ALLOWED_MAX_PRESS_MS} ms; "
                "refusing to accept an unreasonably long press"
            )


#: The limits used when nothing else is configured.
DEFAULT_PRESS_LIMITS = PressLimits()


# Phase 3 UDP heartbeat / status defaults.
#
# The UDP channel is a *status* channel only: the device sends small heartbeat
# datagrams and a monitoring client listens. It is never a command channel and
# never touches the lease (see docs/protocol.md).
#
# The device's UDP socket is outbound-only, so it binds an *ephemeral* source
# port by default (0). That lets the monitoring client use a fixed, well-known
# port (8766) even when both run on one development machine.
DEFAULT_DEVICE_ID = "remotetap-01"
DEFAULT_UDP_BIND_HOST = "127.0.0.1"
DEFAULT_UDP_BIND_PORT = 0
DEFAULT_UDP_DEST_HOST = "127.0.0.1"
DEFAULT_UDP_DEST_PORT = 8766
DEFAULT_HEARTBEAT_INTERVAL_MS = 1000
DEFAULT_STALE_AFTER_MS = 3000

#: Upper bound on the heartbeat interval, in milliseconds. A heartbeat slower
#: than this is almost never intended (it would make the device look offline for
#: a minute at a time), so a larger value is rejected rather than accepted
#: silently.
MAX_ALLOWED_HEARTBEAT_INTERVAL_MS = 60_000


@dataclass(frozen=True)
class UdpConfig:
    """Settings for the UDP heartbeat / status channel.

    Args:
        bind_host: local address the device's heartbeat socket binds to.
        bind_port: local UDP source port; ``0`` means "let the OS choose an
            ephemeral port", which is the default for an outbound-only socket.
        dest_host: address of the monitoring client that receives heartbeats.
        dest_port: UDP port the monitoring client listens on.
        heartbeat_interval_ms: how often a heartbeat datagram is sent.
        stale_after_ms: how long a monitoring client may go without a datagram
            before it should treat the device as offline.

    Raises:
        ValueError: if a port is out of range, the heartbeat interval is not
            positive, or the stale timeout is too short to tolerate a single
            lost heartbeat.
    """

    bind_host: str = DEFAULT_UDP_BIND_HOST
    bind_port: int = DEFAULT_UDP_BIND_PORT
    dest_host: str = DEFAULT_UDP_DEST_HOST
    dest_port: int = DEFAULT_UDP_DEST_PORT
    heartbeat_interval_ms: int = DEFAULT_HEARTBEAT_INTERVAL_MS
    stale_after_ms: int = DEFAULT_STALE_AFTER_MS

    def __post_init__(self) -> None:
        if not self.bind_host.strip():
            raise ValueError("udp bind_host must not be empty")
        if not self.dest_host.strip():
            raise ValueError("udp dest_host must not be empty")
        # bind_port 0 is allowed (ephemeral source port); the destination is
        # where someone is listening, so it must be a real port.
        if not 0 <= self.bind_port <= 65535:
            raise ValueError("udp bind_port must be between 0 and 65535")
        if not 1 <= self.dest_port <= 65535:
            raise ValueError("udp dest_port must be between 1 and 65535")
        if self.heartbeat_interval_ms <= 0:
            raise ValueError("heartbeat_interval_ms must be greater than zero")
        if self.heartbeat_interval_ms > MAX_ALLOWED_HEARTBEAT_INTERVAL_MS:
            raise ValueError(
                f"heartbeat_interval_ms must not exceed "
                f"{MAX_ALLOWED_HEARTBEAT_INTERVAL_MS} ms; refusing a heartbeat "
                "so slow the device looks offline between beats"
            )
        if self.stale_after_ms <= 0:
            raise ValueError("stale_after_ms must be greater than zero")
        # At least one whole heartbeat of slack, so a single lost datagram does
        # not make a healthy device look offline.
        if self.stale_after_ms < 2 * self.heartbeat_interval_ms:
            raise ValueError(
                "stale_after_ms must be at least twice heartbeat_interval_ms, "
                "so a single lost heartbeat is still tolerated"
            )


#: The UDP settings used when nothing else is configured.
DEFAULT_UDP_CONFIG = UdpConfig()
