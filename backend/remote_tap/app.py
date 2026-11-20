"""Entry point for the RemoteTap device service.

Phase 1 runs a single TCP command server in front of a simulated actuator::

    python -m remote_tap.app --host 127.0.0.1 --port 8765

The actuator is released on shutdown, so Ctrl+C can never leave the simulated
button stuck down.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .actuator.simulator import SimulatedActuator
from .config import DEFAULT_HOST, DEFAULT_PORT, ServerConfig
from .net.tcp_server import TcpCommandServer
from .session import DeviceSession

logger = logging.getLogger("remote_tap.app")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the device service."""
    parser = argparse.ArgumentParser(
        prog="remote_tap.app",
        description="RemoteTap device service (Phase 1: simulated actuator, TCP only).",
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


async def run(config: ServerConfig, actuator_label: str) -> None:
    """Start the server and serve until interrupted."""
    session = DeviceSession(SimulatedActuator(label=actuator_label))
    server = TcpCommandServer(session, config)

    await server.start()
    print()
    print("RemoteTap device service")
    print(f"  listening : {config.host}:{server.port} (TCP)")
    print("  actuator  : simulated - no hardware is driven")
    print("  client    : python -m remote_tap.client "
          f"--host {config.host} --port {server.port}")
    print("  stop      : Ctrl+C")
    print()

    try:
        await server.serve_forever()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("shutdown requested")
    finally:
        released = await session.release_if_session(
            session.active_session_id or "", reason="shutdown"
        )
        if released:
            logger.warning("actuator released during shutdown")
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the service until it exits."""
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    config = ServerConfig(host=args.host, port=args.port)

    try:
        asyncio.run(run(config, args.actuator_label))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    except OSError as exc:
        logger.error("could not start the server on %s:%s: %s", config.host, config.port, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
