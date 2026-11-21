"""Command-line entry point for the demo bridge.

Run it from the ``backend`` directory, with the device service already running::

    python -m remote_tap.bridge

Then open http://127.0.0.1:8080 in a browser.

The bridge is a client of the device service: it opens one TCP connection for
commands and listens for the UDP heartbeats the device already sends. It never
replaces either protocol, and it never listens on anything but the address given
to ``--web-host`` (loopback by default), so the demo stays on this machine.
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import (
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    BridgeConfig,
    create_app,
)
from ..config import (
    DEFAULT_DEVICE_ID,
    DEFAULT_PORT,
    DEFAULT_STALE_AFTER_MS,
    DEFAULT_UDP_BIND_HOST,
    DEFAULT_UDP_DEST_PORT,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the demo bridge."""
    parser = argparse.ArgumentParser(
        prog="remote_tap.bridge",
        description=(
            "Local RemoteTap demo bridge: serves the browser dashboard and "
            "translates its press events into the existing TCP command "
            "protocol. It also listens for the device's UDP heartbeats."
        ),
    )
    parser.add_argument(
        "--device-host",
        default="127.0.0.1",
        help="device service address (default: %(default)s)",
    )
    parser.add_argument(
        "--device-port",
        type=int,
        default=DEFAULT_PORT,
        help="device service TCP command port (default: %(default)s)",
    )
    parser.add_argument(
        "--udp-host",
        default=DEFAULT_UDP_BIND_HOST,
        help=(
            "local address to listen for heartbeats on (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=DEFAULT_UDP_DEST_PORT,
        help=(
            "local UDP port heartbeats arrive on; the device's --udp-dest-port "
            "(default: %(default)s). The bridge is the only listener on it."
        ),
    )
    parser.add_argument(
        "--no-udp",
        action="store_true",
        help="do not listen for heartbeats (TCP commands are unaffected)",
    )
    parser.add_argument(
        "--stale-ms",
        type=int,
        default=DEFAULT_STALE_AFTER_MS,
        help=(
            "show the device as offline after this long without a heartbeat, in "
            "ms (default: %(default)s); should match the device's --stale-ms"
        ),
    )
    parser.add_argument(
        "--web-host",
        default=DEFAULT_WEB_HOST,
        help=(
            "address to serve the dashboard on (default: %(default)s). Keep it "
            "on loopback: there is no authentication yet."
        ),
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help="port to serve the dashboard on (default: %(default)s)",
    )
    parser.add_argument(
        "--device-id",
        default=DEFAULT_DEVICE_ID,
        help=(
            "ignore heartbeats from any device other than this id "
            f"(default: %(default)s); pass an empty string to accept any device"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="log at DEBUG level")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the bridge until interrupted."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    config = BridgeConfig(
        device_host=args.device_host,
        device_port=args.device_port,
        udp_host=args.udp_host,
        udp_port=args.udp_port,
        web_host=args.web_host,
        web_port=args.web_port,
        stale_after_ms=args.stale_ms,
        expected_device_id=args.device_id or None,
        use_udp=not args.no_udp,
    )

    try:
        uvicorn.run(
            create_app(config),
            host=config.web_host,
            port=config.web_port,
            log_level="debug" if args.verbose else "info",
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
