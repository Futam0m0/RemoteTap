"""UDP monitoring client for the RemoteTap heartbeat / status channel.

This is a **development and protocol demonstration** tool, not a product
interface. It listens for the device's heartbeat datagrams and prints what it
sees, so the status channel can be exercised without a TCP command connection
and without any mobile UI.

It shows:

* the latest device status and actuator state,
* the latest sequence number,
* missing heartbeats (gaps in the sequence),
* duplicate and out-of-order datagrams,
* when the last valid datagram arrived,
* and the device going **offline** when heartbeats stop.

Sequence tracking is keyed on ``(device_id, boot_id)`` rather than the device
alone. The device's sequence number restarts at 1 whenever its process restarts,
so a monitor that remembered only "device X was at sequence 500" would misread a
restarted device's fresh sequence as a flood of out-of-order packets. The boot id
marks each run of the service: when a known device reports a new boot id, the
monitor recognises a restart, resets that device's sequence tracking and ignores
any late datagram still carrying the *previous* boot id.

It deliberately reads nothing but the datagram: this client cannot command the
device, and a malformed or unsupported datagram is reported and dropped rather
than crashing the loop.

Run it with::

    python -m remote_tap.monitor --host 0.0.0.0 --port 8766
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .config import (
    DEFAULT_DEVICE_ID,
    DEFAULT_STALE_AFTER_MS,
    DEFAULT_UDP_BIND_HOST,
    DEFAULT_UDP_DEST_PORT,
)
from .protocol.udp_messages import DatagramError, StatusDatagram, parse_datagram

logger = logging.getLogger("remote_tap.monitor")


class DatagramKind(str, Enum):
    """How an inbound datagram was classified."""

    #: A new, in-order datagram; the displayed status is updated.
    ACCEPTED = "accepted"
    #: The same sequence number as the datagram already displayed.
    DUPLICATE = "duplicate"
    #: An older sequence number than the one already displayed.
    OUT_OF_ORDER = "out_of_order"
    #: From a boot that a newer boot has already replaced; ignored.
    SUPERSEDED = "superseded"
    #: Malformed, or a message this client does not understand.
    REJECTED = "rejected"


@dataclass(frozen=True)
class MonitorEvent:
    """The outcome of observing one datagram."""

    kind: DatagramKind
    sequence: int | None = None
    #: How many datagrams were missing immediately before an accepted one.
    missing: int = 0
    message: str = ""
    #: The boot id the datagram carried, when it could be parsed.
    boot_id: str | None = None
    #: True when this datagram came from a boot never seen for its device, i.e.
    #: the device looks like it restarted.
    restarted: bool = False


@dataclass
class _InstanceTracker:
    """Sequence bookkeeping for one ``(device_id, boot_id)`` run of a device."""

    last_sequence: int
    last_status: StatusDatagram
    #: Set once a *newer* boot for the same device has been accepted, so late
    #: datagrams still carrying this old boot id can be ignored outright.
    superseded: bool = False


class StatusMonitor:
    """Tracks the newest status seen and whether the device is still sending.

    Kept free of sockets so its logic (ordering, duplicates, restarts,
    staleness) can be tested with an injected clock and hand-built datagrams.

    Sequence state is kept per ``(device_id, boot_id)``, so several devices — and
    several runs of one device — are tracked independently.

    Args:
        stale_after_ms: how long without a datagram before the device is
            considered offline.
        clock: monotonic clock, injectable for deterministic staleness tests.
        expected_device_id: if given, datagrams from any other device are
            rejected.
    """

    def __init__(
        self,
        stale_after_ms: int = DEFAULT_STALE_AFTER_MS,
        *,
        clock: Callable[[], float] = time.monotonic,
        expected_device_id: str | None = None,
    ) -> None:
        self._stale_after = stale_after_ms / 1000.0
        self._clock = clock
        self._expected_device_id = expected_device_id
        #: The most recently accepted datagram and its sequence — the status the
        #: monitor is currently displaying.
        self.last_status: StatusDatagram | None = None
        self.last_sequence: int | None = None
        self.last_seen: float | None = None
        self.last_device_id: str | None = None
        self.last_boot_id: str | None = None
        self.accepted = 0
        self.duplicates = 0
        self.out_of_order = 0
        self.superseded = 0
        self.restarts = 0
        self.rejected = 0
        self.missing = 0
        self._offline_reported = False
        #: Sequence state per ``(device_id, boot_id)``.
        self._instances: dict[tuple[str, str], _InstanceTracker] = {}
        #: The boot id currently believed to be the live run of each device.
        self._current_boot: dict[str, str] = {}

    def observe(self, data: bytes, *, now: float | None = None) -> MonitorEvent:
        """Classify one inbound datagram and update the displayed status."""
        now = self._clock() if now is None else now

        try:
            message = parse_datagram(data)
        except DatagramError as exc:
            self.rejected += 1
            return MonitorEvent(DatagramKind.REJECTED, message=exc.message)

        if (
            self._expected_device_id is not None
            and message.device_id != self._expected_device_id
        ):
            self.rejected += 1
            return MonitorEvent(
                DatagramKind.REJECTED,
                sequence=message.sequence,
                boot_id=message.boot_id,
                message=f"unexpected device id {message.device_id!r}",
            )

        key = (message.device_id, message.boot_id)
        tracker = self._instances.get(key)

        # A datagram from a boot that a newer one has already replaced is stale
        # by definition: it is dropped before it can touch liveness or status.
        # This is what stops a previous process's late packets — whatever their
        # sequence number — from overwriting the current device status.
        if tracker is not None and tracker.superseded:
            self.superseded += 1
            return MonitorEvent(
                DatagramKind.SUPERSEDED,
                sequence=message.sequence,
                boot_id=message.boot_id,
                message=f"superseded by boot {self._current_boot.get(message.device_id)!r}",
            )

        # Whatever the sequence says, the device is clearly still alive, so the
        # offline timer restarts even for a duplicate or a late datagram.
        self.last_seen = now
        self._offline_reported = False

        if tracker is None:
            return self._accept_new_instance(message)

        if message.sequence == tracker.last_sequence:
            self.duplicates += 1
            return self._ignored(DatagramKind.DUPLICATE, message)
        if message.sequence < tracker.last_sequence:
            self.out_of_order += 1
            return self._ignored(
                DatagramKind.OUT_OF_ORDER,
                message,
                detail=f"newest is {tracker.last_sequence}",
            )

        missing = message.sequence - tracker.last_sequence - 1
        tracker.last_sequence = message.sequence
        tracker.last_status = message
        return self._accept(message, missing=missing)

    def _accept_new_instance(self, message: StatusDatagram) -> MonitorEvent:
        """Register a first-seen boot for a device and display its datagram.

        A boot id the monitor has not seen for this device is treated as a fresh
        run: its sequence tracking starts here, so its sequence 1 is accepted
        rather than mistaken for an out-of-order packet from the previous run.
        """
        previous_boot = self._current_boot.get(message.device_id)
        restarted = previous_boot is not None and previous_boot != message.boot_id
        if restarted:
            previous = self._instances.get((message.device_id, previous_boot))
            if previous is not None:
                previous.superseded = True
            self.restarts += 1
        self._current_boot[message.device_id] = message.boot_id
        self._instances[(message.device_id, message.boot_id)] = _InstanceTracker(
            last_sequence=message.sequence, last_status=message
        )
        return self._accept(message, missing=0, restarted=restarted)

    def _accept(
        self,
        message: StatusDatagram,
        *,
        missing: int,
        restarted: bool = False,
    ) -> MonitorEvent:
        """Display ``message`` as the newest status and count the acceptance."""
        self.missing += missing
        self.last_sequence = message.sequence
        self.last_status = message
        self.last_device_id = message.device_id
        self.last_boot_id = message.boot_id
        self.accepted += 1
        return MonitorEvent(
            DatagramKind.ACCEPTED,
            sequence=message.sequence,
            missing=missing,
            boot_id=message.boot_id,
            restarted=restarted,
        )

    @staticmethod
    def _ignored(
        kind: DatagramKind, message: StatusDatagram, *, detail: str = ""
    ) -> MonitorEvent:
        """Build an event for a datagram that is not allowed to change status."""
        return MonitorEvent(
            kind, sequence=message.sequence, message=detail, boot_id=message.boot_id
        )

    def age(self, *, now: float | None = None) -> float | None:
        """Seconds since the last datagram, or ``None`` if there has been none."""
        if self.last_seen is None:
            return None
        return (self._clock() if now is None else now) - self.last_seen

    def is_offline(self, *, now: float | None = None) -> bool:
        """Whether the device should be shown as offline."""
        if self.last_seen is None:
            return True
        return self.age(now=now) > self._stale_after

    def check_offline(self, *, now: float | None = None) -> bool:
        """Return ``True`` only the first time the device is seen to be offline.

        Lets the caller print the offline notice once per outage instead of on
        every poll.
        """
        if not self.is_offline(now=now):
            return False
        if self._offline_reported:
            return False
        self._offline_reported = True
        return True


class _MonitorProtocol(asyncio.DatagramProtocol):
    """Feeds received datagrams to a :class:`StatusMonitor` and reports them."""

    def __init__(
        self, monitor: StatusMonitor, reporter: Callable[[object, MonitorEvent], None]
    ) -> None:
        self._monitor = monitor
        self._reporter = reporter

    def datagram_received(self, data: bytes, addr: object) -> None:
        event = self._monitor.observe(data)
        self._reporter(addr, event)

    def error_received(self, exc: Exception) -> None:
        logger.warning("UDP receive error: %s", exc)


def _short_boot(boot_id: str | None) -> str:
    """Abbreviate a boot id for console output (the full value is long)."""
    if not boot_id:
        return "unknown"
    return boot_id[:8]


def format_event(addr: object, event: MonitorEvent, monitor: StatusMonitor) -> str:
    """Render one observed datagram as a line of console output."""
    if event.kind is DatagramKind.REJECTED:
        return f"[monitor] rejected datagram from {addr}: {event.message}"

    if event.kind is DatagramKind.SUPERSEDED:
        return (
            f"[monitor] seq={event.sequence} from an earlier boot "
            f"(boot={_short_boot(event.boot_id)}) ignored; {event.message}"
        )

    if event.kind is DatagramKind.DUPLICATE:
        return (
            f"[monitor] duplicate seq={event.sequence} ignored "
            f"(newest is {monitor.last_sequence})"
        )

    if event.kind is DatagramKind.OUT_OF_ORDER:
        return (
            f"[monitor] out-of-order seq={event.sequence} ignored "
            f"({event.message})"
        )

    status = monitor.last_status
    assert status is not None
    line = (
        f"[monitor] seq={status.sequence} type={status.type} state={status.state} "
        f"session_active={'yes' if status.session_active else 'no'} "
        f"device={status.device_id} boot={_short_boot(status.boot_id)} from {addr}"
    )
    if event.restarted:
        line += "  !! new boot id: the device restarted, sequence tracking reset"
    if event.missing:
        line += f"  !! {event.missing} heartbeat(s) missing before this one"
    return line


async def run_monitor(
    host: str,
    port: int,
    stale_after_ms: int = DEFAULT_STALE_AFTER_MS,
    *,
    expected_device_id: str | None = None,
    check_interval_s: float | None = None,
    reporter: Callable[[object, MonitorEvent], None] = format_event,
) -> None:
    """Listen for heartbeats and report them until cancelled.

    Args:
        host: local address to bind the listening UDP socket to.
        port: local UDP port to listen on.
        stale_after_ms: offline threshold, in milliseconds.
        expected_device_id: if given, ignore datagrams from other devices.
        check_interval_s: how often to check for staleness; defaults to a
            quarter of the stale timeout.
        reporter: called with ``(addr, event)`` for every datagram.
    """
    monitor = StatusMonitor(stale_after_ms, expected_device_id=expected_device_id)
    interval = check_interval_s or max(stale_after_ms / 4000.0, 0.05)

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _MonitorProtocol(monitor, reporter),
        local_addr=(host, port),
    )
    print(f"[monitor] listening on {host}:{port} (UDP), stale after {stale_after_ms} ms")
    try:
        while True:
            await asyncio.sleep(interval)
            if monitor.check_offline():
                age = monitor.age() or 0.0
                print(
                    f"[monitor] device OFFLINE (no status for {age * 1000:.0f} ms; "
                    f"last seq={monitor.last_sequence})"
                )
    finally:
        transport.close()


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the monitoring client."""
    parser = argparse.ArgumentParser(
        prog="remote_tap.monitor",
        description=(
            "RemoteTap UDP heartbeat monitor (Phase 3). Listens for device "
            "status datagrams; it cannot command the device."
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_UDP_BIND_HOST,
        help=(
            "local address to listen on (default: %(default)s). Use 0.0.0.0 to "
            "accept heartbeats from another machine on a private network."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_UDP_DEST_PORT, help="UDP port (default: %(default)s)"
    )
    parser.add_argument(
        "--stale-ms",
        type=int,
        default=DEFAULT_STALE_AFTER_MS,
        help="show the device as offline after this long without a heartbeat (default: %(default)s)",
    )
    parser.add_argument(
        "--device-id",
        default=None,
        help=(
            "ignore datagrams from any device other than this id "
            f"(default: accept any; the device default is {DEFAULT_DEVICE_ID})"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="log at DEBUG level")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the monitoring client until interrupted."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(
            run_monitor(
                args.host,
                args.port,
                args.stale_ms,
                expected_device_id=args.device_id,
            )
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print()
    except OSError as exc:
        print(f"[monitor] could not listen on {args.host}:{args.port}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
