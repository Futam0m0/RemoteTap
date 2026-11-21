"""Entry point for the RemoteTap device service.

Phase 3 runs a TCP command server in front of a simulated actuator, plus a UDP
heartbeat / status channel that reports the device's state to a monitoring
client::

    python -m remote_tap.app --host 127.0.0.1 --port 8765

The actuator is released on shutdown, so Ctrl+C can never leave the simulated
button stuck down. A press is a lease the client must renew; the lease timeout
and the maximum press duration enforced by the device are configurable here.

The UDP channel never touches the actuator: it only reports what the TCP-side
session is doing (see ``net/udp_heartbeat.py`` and ``docs/protocol.md``). A UDP
failure is logged and the TCP command server keeps running.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .actuator.simulator import SimulatedActuator
from .config import (
    DEFAULT_DEVICE_ID,
    DEFAULT_HEARTBEAT_INTERVAL_MS,
    DEFAULT_HOST,
    DEFAULT_LEASE_TIMEOUT_MS,
    DEFAULT_MAX_PRESS_MS,
    DEFAULT_PORT,
    DEFAULT_RENEW_INTERVAL_MS,
    DEFAULT_STALE_AFTER_MS,
    DEFAULT_UDP_BIND_HOST,
    DEFAULT_UDP_BIND_PORT,
    DEFAULT_UDP_DEST_HOST,
    DEFAULT_UDP_DEST_PORT,
    MAX_ALLOWED_HEARTBEAT_INTERVAL_MS,
    MAX_ALLOWED_MAX_PRESS_MS,
    PressLimits,
    ServerConfig,
    UdpConfig,
)
from .net.tcp_server import TcpCommandServer
from .net.udp_heartbeat import HeartbeatService
from .session import DeviceSession

logger = logging.getLogger("remote_tap.app")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the device service."""
    parser = argparse.ArgumentParser(
        prog="remote_tap.app",
        description=(
            "RemoteTap device service (Phase 3: simulated actuator, TCP command "
            "channel with a press lease, plus a read-only UDP heartbeat/status "
            "channel)."
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "address to listen on (default: %(default)s). Use the machine's "
            "Tailscale address to reach it from another device; avoid 0.0.0.0 "
            "unless you know the network is private."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="TCP port (default: %(default)s)"
    )
    parser.add_argument(
        "--actuator-label",
        default="sim",
        help="label used when the simulated actuator prints (default: %(default)s)",
    )
    parser.add_argument(
        "--renew-interval-ms",
        type=int,
        default=DEFAULT_RENEW_INTERVAL_MS,
        help=(
            "how often a client is asked to renew an active press, in ms "
            "(default: %(default)s); advertised to clients, not enforced"
        ),
    )
    parser.add_argument(
        "--lease-timeout-ms",
        type=int,
        default=DEFAULT_LEASE_TIMEOUT_MS,
        help=(
            "how long a press survives without a renewal before the device "
            "releases it, in ms (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--max-press-ms",
        type=int,
        default=DEFAULT_MAX_PRESS_MS,
        help=(
            "hard ceiling on one continuous press, in ms (default: %(default)s); "
            "renewals cannot extend it, and values above the safety limit of "
            f"{MAX_ALLOWED_MAX_PRESS_MS} ms are rejected"
        ),
    )
    parser.add_argument(
        "--device-id",
        default=DEFAULT_DEVICE_ID,
        help="identifier reported in every UDP heartbeat datagram (default: %(default)s)",
    )
    parser.add_argument(
        "--no-udp",
        action="store_true",
        help="do not start the UDP heartbeat channel (TCP commands are unaffected)",
    )
    parser.add_argument(
        "--udp-dest-host",
        default=DEFAULT_UDP_DEST_HOST,
        help=(
            "address of the monitoring client that receives heartbeats "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--udp-dest-port",
        type=int,
        default=DEFAULT_UDP_DEST_PORT,
        help=(
            "UDP port the monitoring client listens on (default: %(default)s); "
            "kept separate from the TCP --port"
        ),
    )
    parser.add_argument(
        "--udp-bind-host",
        default=DEFAULT_UDP_BIND_HOST,
        help=(
            "local address for the outbound heartbeat socket (default: "
            "%(default)s)"
        ),
    )
    parser.add_argument(
        "--udp-bind-port",
        type=int,
        default=DEFAULT_UDP_BIND_PORT,
        help=(
            "local UDP source port for heartbeats (default: %(default)s, meaning "
            "an ephemeral port). The socket is outbound-only."
        ),
    )
    parser.add_argument(
        "--heartbeat-interval-ms",
        type=int,
        default=DEFAULT_HEARTBEAT_INTERVAL_MS,
        help=(
            "how often a heartbeat datagram is sent, in ms (default: "
            f"%(default)s); values above {MAX_ALLOWED_HEARTBEAT_INTERVAL_MS} ms "
            "are rejected"
        ),
    )
    parser.add_argument(
        "--stale-ms",
        type=int,
        default=DEFAULT_STALE_AFTER_MS,
        help=(
            "how long a monitoring client may go without a datagram before it "
            "treats the device as offline, in ms (default: %(default)s); must be "
            "at least twice the heartbeat interval"
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true", help="log at DEBUG level"
    )
    return parser


def configure_logging(verbose: bool) -> None:
    """Send logs to the terminal in a readable single-line format."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


async def run(
    config: ServerConfig,
    actuator_label: str,
    limits: PressLimits,
    *,
    udp_config: UdpConfig | None = None,
    device_id: str = DEFAULT_DEVICE_ID,
) -> None:
    """Start the server and serve until interrupted.

    When ``udp_config`` is given, a read-only UDP heartbeat service is started
    alongside the TCP server. It never touches the actuator or the lease; if it
    fails, it is logged and the TCP command channel keeps running.
    """
    session = DeviceSession(
        SimulatedActuator(label=actuator_label), limits=limits
    )
    server = TcpCommandServer(session, config)

    await server.start()
    heartbeat: HeartbeatService | None = None
    if udp_config is not None:
        heartbeat = HeartbeatService(session, udp_config, device_id=device_id)
        try:
            await heartbeat.start()
        except OSError as exc:
            # A UDP socket that cannot even be opened must not stop the device
            # from accepting TCP commands.
            logger.error("could not start the UDP heartbeat channel: %s", exc)
            heartbeat = None

    print()
    print("RemoteTap device service")
    print(f"  listening : {config.host}:{server.port} (TCP)")
    print("  actuator  : simulated - no hardware is driven")
    print(
        f"  lease     : renew every {limits.renew_interval_ms} ms, "
        f"release after {limits.lease_timeout_ms} ms without one"
    )
    print(f"  max press : {limits.max_press_ms} ms, renewals cannot extend it")
    if heartbeat is not None:
        print(
            f"  heartbeat : {udp_config.heartbeat_interval_ms} ms to "
            f"{udp_config.dest_host}:{udp_config.dest_port} (UDP, read-only status)"
        )
    else:
        print("  heartbeat : disabled (TCP commands are unaffected)")
    print("  monitor   : python -m remote_tap.monitor "
          f"--host {udp_config.dest_host if udp_config else DEFAULT_UDP_DEST_HOST} "
          f"--port {udp_config.dest_port if udp_config else DEFAULT_UDP_DEST_PORT}")
    print("  client    : python -m remote_tap.client "
          f"--host {config.host} --port {server.port}")
    print("  stop      : Ctrl+C")
    print()

    try:
        await server.serve_forever()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("shutdown requested")
    finally:
        # Safety first: the actuator is released before anything else is torn
        # down (Phase 2 behaviour). Stopping the UDP channel afterwards cannot
        # change the lease or the watchdog in any way.
        if await session.release(reason="shutdown"):
            logger.warning("actuator released during shutdown")
        if heartbeat is not None:
            await heartbeat.stop()
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the service until it exits."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    config = ServerConfig(host=args.host, port=args.port)

    try:
        limits = PressLimits(
            renew_interval_ms=args.renew_interval_ms,
            lease_timeout_ms=args.lease_timeout_ms,
            max_press_ms=args.max_press_ms,
        )
    except ValueError as exc:
        parser.error(str(exc))
        raise  # unreachable: parser.error() exits

    udp_config: UdpConfig | None = None
    if not args.no_udp:
        try:
            udp_config = UdpConfig(
                bind_host=args.udp_bind_host,
                bind_port=args.udp_bind_port,
                dest_host=args.udp_dest_host,
                dest_port=args.udp_dest_port,
                heartbeat_interval_ms=args.heartbeat_interval_ms,
                stale_after_ms=args.stale_ms,
            )
        except ValueError as exc:
            parser.error(str(exc))
            raise  # unreachable: parser.error() exits

    try:
        asyncio.run(
            run(
                config,
                args.actuator_label,
                limits,
                udp_config=udp_config,
                device_id=args.device_id,
            )
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    except OSError as exc:
        logger.error("could not start the server on %s:%s: %s", config.host, config.port, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
