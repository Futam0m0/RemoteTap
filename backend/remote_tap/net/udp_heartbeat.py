"""UDP heartbeat / status channel.

The device sends a small JSON datagram at a fixed interval describing its own
state, so a monitoring client can see that the device is alive and what the
actuator is doing **without** holding a TCP command connection open.

Isolation from the safety-critical command path
-----------------------------------------------

This service is deliberately one-directional and read-only:

* it only ever **reads** the session, through the session's read-only
  properties, to build a status snapshot;
* it never calls :meth:`DeviceSession.handle`, never presses, renews or releases
  the actuator, and never touches the watchdog, the lease or the disappearance
  of a session;
* a send failure is logged and swallowed, so a broken or blocked UDP channel can
  never take the TCP command channel down with it.

In other words, UDP *describes* the device; it never *drives* it. Only the TCP
command channel changes actuator state (see ``docs/protocol.md``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Callable, Optional

from ..actuator.base import ActuatorState
from ..config import DEFAULT_DEVICE_ID, UdpConfig
from ..protocol.udp_messages import HEARTBEAT, STATUS, StatusDatagram, encode_datagram
from ..session import DeviceSession

logger = logging.getLogger(__name__)


class _HeartbeatProtocol(asyncio.DatagramProtocol):
    """Sink for anything sent to the device's UDP socket.

    The device accepts no commands over UDP, so an inbound datagram is logged
    and dropped. Having a protocol here (rather than a bare socket) also drains
    the receive buffer, so it cannot fill up with unsolicited traffic.
    """

    def datagram_received(self, data: bytes, addr: object) -> None:
        logger.debug(
            "ignoring inbound UDP datagram from %s (%d bytes): the UDP channel "
            "is status-only",
            addr,
            len(data),
        )

    def error_received(self, exc: Exception) -> None:
        # A previous send may fail asynchronously on some platforms. It is not
        # the command channel's problem, so it is only logged.
        logger.warning("UDP socket error: %s", exc)


class HeartbeatService:
    """Periodically sends a status datagram to a monitoring client.

    Args:
        session: the device session, read (never written) for status.
        config: UDP bind/destination addresses, heartbeat interval and stale
            timeout.
        device_id: identifier put in every datagram.
        name: label used in log output.
        clock: monotonic clock, used only to time the sends.
        wall_clock: wall clock, used only for the display-only ``ts`` field.
    """

    def __init__(
        self,
        session: DeviceSession,
        config: UdpConfig,
        *,
        device_id: str = DEFAULT_DEVICE_ID,
        name: str = "remotetap",
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._session = session
        self._config = config
        self._device_id = device_id
        self._name = name
        self._clock = clock
        self._wall_clock = wall_clock
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._sequence = 0
        self._sent = 0
        self._send_failures = 0
        # The snapshot last put on the wire, so a change can be reported as a
        # STATUS datagram instead of an ordinary HEARTBEAT.
        self._last_snapshot: Optional[tuple[str, bool]] = None

    @property
    def config(self) -> UdpConfig:
        """The settings this service sends under."""
        return self._config

    @property
    def is_running(self) -> bool:
        """Whether the UDP socket is open and the heartbeat task is live."""
        return self._transport is not None

    @property
    def local_address(self) -> Optional[tuple[str, int]]:
        """The socket the heartbeats are actually sent from, or ``None``.

        Useful when ``bind_port`` is 0 and the OS chose the port. This is the
        source address, never a listening one: the device accepts no UDP input.
        """
        transport = self._transport
        if transport is None:
            return None
        sockname = transport.get_extra_info("sockname")
        if not sockname:
            return None
        return sockname[0], sockname[1]

    @property
    def datagrams_sent(self) -> int:
        """How many datagrams have been handed to the OS successfully."""
        return self._sent

    @property
    def last_sequence(self) -> int:
        """Sequence number of the most recent datagram sent, or ``0``."""
        return self._sequence

    @property
    def send_failures(self) -> int:
        """How many send attempts failed since the service started."""
        return self._send_failures

    async def start(self) -> None:
        """Open the UDP socket and begin sending heartbeats."""
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            _HeartbeatProtocol,
            local_addr=(self._config.bind_host, self._config.bind_port),
        )
        self._transport = transport
        self._task = asyncio.ensure_future(self._run())
        logger.info(
            "[%s] UDP heartbeat sending to %s:%s every %s ms",
            self._name,
            self._config.dest_host,
            self._config.dest_port,
            self._config.heartbeat_interval_ms,
        )

    async def stop(self) -> None:
        """Cancel the heartbeat task and release the UDP socket."""
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
        logger.info(
            "[%s] UDP heartbeat stopped (%d datagrams sent, %d failed)",
            self._name,
            self._sent,
            self._send_failures,
        )

    def build_datagram(self) -> StatusDatagram:
        """Build the status datagram that would be sent next.

        Reads the session's state through its read-only properties, so the
        heartbeat always reflects the authoritative session manager and never
        keeps a second copy of the actuator state.

        The next sequence number is ``last_sequence + 1``; :meth:`send_once`
        only commits it once the datagram is really sent, so a failed send does
        not leave a gap the receiver would read as a lost packet.
        """
        state = self._session.state.value
        session_active = self._session.active_session_id is not None
        snapshot = (state, session_active)
        # First datagram, or the state moved since the last one: report it as a
        # STATUS. An unchanged tick is an ordinary HEARTBEAT.
        message_type = (
            HEARTBEAT
            if self._last_snapshot is not None and snapshot == self._last_snapshot
            else STATUS
        )
        return StatusDatagram(
            type=message_type,
            device_id=self._device_id,
            sequence=self._sequence + 1,
            timestamp_ms=int(self._wall_clock() * 1000),
            state=state,
            session_active=session_active,
        )

    async def send_once(self) -> bool:
        """Send one datagram now.

        Returns:
            ``True`` if the datagram was handed to the OS, ``False`` if there is
            no socket or the send failed. A failure is logged and swallowed: the
            status channel must never disturb the command channel.
        """
        if self._transport is None:
            return False

        message = self.build_datagram()
        try:
            self._transport.sendto(
                encode_datagram(message),
                (self._config.dest_host, self._config.dest_port),
            )
        except OSError as exc:
            self._send_failures += 1
            logger.warning("UDP heartbeat could not be sent: %s", exc)
            return False

        self._sequence = message.sequence
        self._sent += 1
        self._last_snapshot = (message.state, message.session_active)
        logger.debug(
            "sent %s seq=%s state=%s session_active=%s",
            message.type,
            message.sequence,
            message.state,
            message.session_active,
        )
        return True

    async def _run(self) -> None:
        """Send an immediate beat, then one every interval until cancelled."""
        interval = self._config.heartbeat_interval_ms / 1000.0
        try:
            await self.send_once()
            while True:
                await asyncio.sleep(interval)
                await self.send_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            # Never let a status-channel bug propagate: the TCP command server
            # must keep running even if UDP is broken.
            logger.exception(
                "UDP heartbeat task failed; the TCP command channel is unaffected"
            )
        finally:
            if self._task is asyncio.current_task():
                self._task = None
