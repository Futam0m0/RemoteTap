"""The bridge's UDP status listener — the only one on the heartbeat port.

The device service broadcasts its state as UDP heartbeat/status datagrams on a
fixed port. A browser cannot open a UDP socket, so the bridge listens on the
page's behalf. Crucially, it is the **sole** listener: the page, the bridge and
any other process must not each bind the same port, which the OS would not allow
anyway and which would split the stream unpredictably.

Rather than re-implement datagram handling, this module reuses the existing
:class:`remote_tap.monitor.StatusMonitor` unchanged. That monitor already knows
everything the demo needs and nothing here duplicates it:

* sequence tracking, duplicates and out-of-order datagrams,
* the ``(device_id, boot_id)`` keying that recognises a restarted device,
* staleness, i.e. deciding when the device is offline.

This hub only adds the two things the page needs on top: a JSON snapshot of the
monitor's current view, and a callback fired whenever that view changes. It
never writes anything to the network — the UDP channel stays read-only, exactly
as it is for the shipped ``python -m remote_tap.monitor`` tool.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from ..config import (
    DEFAULT_DEVICE_ID,
    DEFAULT_STALE_AFTER_MS,
    DEFAULT_UDP_BIND_HOST,
    DEFAULT_UDP_DEST_PORT,
)
from ..monitor import MonitorEvent, StatusMonitor

logger = logging.getLogger("remote_tap.bridge.status")


class _HubProtocol(asyncio.DatagramProtocol):
    """Feeds received datagrams to the shared monitor and reports changes."""

    def __init__(self, hub: "StatusHub") -> None:
        self._hub = hub

    def datagram_received(self, data: bytes, addr: object) -> None:
        self._hub._observe(data)
        self._hub._publish()

    def error_received(self, exc: Exception) -> None:
        # A broken status channel must never disturb the command channel.
        logger.warning("UDP receive error: %s", exc)


class StatusHub:
    """Listens for device heartbeats and exposes them as plain JSON.

    Args:
        bind_host: local address to bind the listening socket to.
        port: local UDP port to listen on (the device's heartbeat destination).
        stale_after_ms: offline threshold, matching the device's own setting.
        expected_device_id: if given, datagrams from any other device are
            rejected, so the demo cannot be confused by unrelated traffic.
        on_update: called (without arguments) whenever the monitor's view
            changes — a new datagram, or the device going offline. The bridge
            uses it to push fresh state to the page.
        check_interval_s: how often to test for staleness; defaults to a quarter
            of the stale timeout.
    """

    def __init__(
        self,
        *,
        bind_host: str = DEFAULT_UDP_BIND_HOST,
        port: int = DEFAULT_UDP_DEST_PORT,
        stale_after_ms: int = DEFAULT_STALE_AFTER_MS,
        expected_device_id: Optional[str] = DEFAULT_DEVICE_ID,
        on_update: Optional[Callable[[], None]] = None,
        check_interval_s: Optional[float] = None,
    ) -> None:
        self._bind_host = bind_host
        self._port = port
        self._on_update = on_update
        self._monitor = StatusMonitor(
            stale_after_ms, expected_device_id=expected_device_id
        )
        self._interval = check_interval_s or max(stale_after_ms / 4000.0, 0.05)
        self._stale_after_ms = stale_after_ms
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._watch_task: Optional[asyncio.Task[None]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def monitor(self) -> StatusMonitor:
        """The underlying monitor, exposed for inspection and tests."""
        return self._monitor

    @property
    def listening(self) -> bool:
        """Whether the UDP socket is bound and the staleness timer is running."""
        return self._transport is not None

    @property
    def port(self) -> int:
        """The port actually bound (differs from the request when ``0``)."""
        transport = self._transport
        if transport is None:
            return self._port
        sockname = transport.get_extra_info("sockname")
        return sockname[1] if sockname else self._port

    async def start(self) -> None:
        """Bind the UDP socket and begin watching for heartbeats.

        Raises:
            OSError: if the port is already in use. The caller may treat that as
                "someone else is already monitoring" and continue without UDP.
        """
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _HubProtocol(self),
            local_addr=(self._bind_host, self._port),
        )
        self._transport = transport
        self._watch_task = asyncio.ensure_future(self._watch_offline())
        logger.info(
            "listening for heartbeats on %s:%s (UDP), offline after %s ms",
            self._bind_host,
            self.port,
            self._stale_after_ms,
        )

    async def stop(self) -> None:
        """Close the UDP socket and stop the staleness timer."""
        task, self._watch_task = self._watch_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
        logger.info("stopped listening for heartbeats")

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """A JSON-friendly view of the monitor's current knowledge."""
        monitor = self._monitor
        age = monitor.age()
        status = monitor.last_status
        return {
            "online": not monitor.is_offline(),
            "listening": self.listening,
            "device_id": monitor.last_device_id,
            "boot_id": monitor.last_boot_id,
            "state": status.state if status is not None else None,
            "session_active": status.session_active if status is not None else None,
            "message_type": status.type if status is not None else None,
            "sequence": monitor.last_sequence,
            "age_ms": None if age is None else round(age * 1000),
            # Display-only: the sender's wall clock, never used for staleness.
            "last_heartbeat_ms": status.timestamp_ms if status is not None else None,
            "counters": {
                "accepted": monitor.accepted,
                "duplicates": monitor.duplicates,
                "out_of_order": monitor.out_of_order,
                "superseded": monitor.superseded,
                "restarts": monitor.restarts,
                "rejected": monitor.rejected,
                "missing": monitor.missing,
            },
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _observe(self, data: bytes) -> MonitorEvent:
        """Hand one datagram to the shared monitor and return its verdict."""
        event = self._monitor.observe(data)
        if event.restarted:
            logger.warning(
                "device %s restarted (new boot id); sequence tracking reset",
                self._monitor.last_device_id,
            )
        return event

    async def _watch_offline(self) -> None:
        """Notice the device falling silent and report it once per outage.

        :meth:`StatusMonitor.check_offline` already reports an outage only the
        first time it is seen, so this publishes exactly once per outage and
        picks the device up again when a datagram arrives.
        """
        try:
            while True:
                await asyncio.sleep(self._interval)
                if self._monitor.check_offline():
                    logger.warning(
                        "device is offline: no heartbeat within the threshold"
                    )
                    self._publish()
        except asyncio.CancelledError:
            raise

    def _publish(self) -> None:
        """Tell the bridge the status view changed."""
        if self._on_update is not None:
            self._on_update()
