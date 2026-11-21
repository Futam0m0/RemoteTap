"""Tests for the UDP heartbeat service and its isolation from the TCP path.

The heartbeat is driven against a real UDP socket recorded by ``UdpRecorder``,
so the tests see the same bytes a monitoring client would. Timing is either
polled (``wait_for``) or asserted with generous slack, never with a fixed sleep
that has to guess how slow the machine is.
"""

from __future__ import annotations

import asyncio
import socket
from typing import AsyncIterator, Awaitable, Callable

import pytest

from remote_tap.actuator.base import ActuatorState
from remote_tap.config import DEFAULT_DEVICE_ID, UdpConfig
from remote_tap.monitor import StatusMonitor
from remote_tap.net.udp_heartbeat import HeartbeatService
from remote_tap.protocol.messages import Ack, Command, CommandType, ErrorMessage
from remote_tap.protocol.udp_messages import (
    HEARTBEAT,
    STATUS,
    DatagramError,
    StatusDatagram,
    parse_datagram,
)
from remote_tap.session import DeviceSession

from conftest import RawClient, UdpRecorder

SESSION_ID = "session-1"
CLIENT_ID = "connection-1"

#: Builds a heartbeat service bound to the shared session.
HeartbeatFactory = Callable[..., Awaitable[HeartbeatService]]
#: Opens another TCP connection to the running test server.
ClientFactory = Callable[[], Awaitable[RawClient]]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def heartbeat_tasks() -> list[asyncio.Task]:
    """Every heartbeat background task still alive on the running loop.

    ``asyncio.all_tasks`` returns only tasks that have not finished, so an empty
    list really does mean the service left nothing running behind it.
    """
    alive = []
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        if getattr(coro, "__qualname__", "").startswith("HeartbeatService._run"):
            alive.append(task)
    return alive


def make_config(
    recorder: UdpRecorder, *, interval_ms: int = 30, stale_ms: int | None = None
) -> UdpConfig:
    """UDP settings pointed at a recorder listening on loopback."""
    return UdpConfig(
        dest_host="127.0.0.1",
        dest_port=recorder.port,
        heartbeat_interval_ms=interval_ms,
        stale_after_ms=stale_ms if stale_ms is not None else max(2 * interval_ms, 100),
    )


async def wait_for_datagram(
    recorder: UdpRecorder,
    predicate: Callable[[StatusDatagram], bool],
    timeout: float = 2.0,
) -> StatusDatagram:
    """Wait for a datagram matching ``predicate``, polling rather than sleeping."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for data in recorder.datagrams:
            try:
                message = parse_datagram(data)
            except DatagramError:  # pragma: no cover - the device only sends valid ones
                continue
            if predicate(message):
                return message
        await asyncio.sleep(0.005)
    raise AssertionError("the expected datagram never arrived")


def send_udp(host: str, port: int, payload: bytes) -> None:
    """Send one datagram from a throwaway socket, as an outsider would."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(payload, (host, port))


async def tcp_command(
    session: DeviceSession,
    command_type: CommandType,
    *,
    session_id: str = SESSION_ID,
    command_id: str = "cmd-1",
    client_id: str = CLIENT_ID,
) -> Ack | ErrorMessage:
    """Apply a command through the TCP session, as a connection would."""
    return await session.handle(
        Command(command_type, command_id, session_id), client_id
    )


class _BrokenTransport:
    """A datagram transport whose sends always fail.

    Swapped in for the real transport to exercise the failure path without
    touching the network.
    """

    def __init__(self) -> None:
        self.closed = False

    def sendto(self, data: bytes, addr: object) -> None:
        raise OSError("simulated network failure")

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default


@pytest.fixture
async def make_heartbeat(
    session: DeviceSession,
) -> AsyncIterator[Callable[..., Awaitable[HeartbeatService]]]:
    """Factory for heartbeat services on the shared session, all stopped after."""
    created: list[HeartbeatService] = []

    async def build(recorder: UdpRecorder, **kwargs: int) -> HeartbeatService:
        service = HeartbeatService(session, make_config(recorder, **kwargs))
        created.append(service)
        return service

    try:
        yield build
    finally:
        for service in created:
            await service.stop()


# ----------------------------------------------------------------------
# Heartbeat behaviour
# ----------------------------------------------------------------------


async def test_heartbeats_are_transmitted_at_the_configured_interval(
    udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    interval = 0.1
    service = await make_heartbeat(udp_recorder, interval_ms=100)
    await service.start()

    assert await udp_recorder.wait_for(3), "the device did not send three heartbeats"
    times = udp_recorder.arrival_times()[:3]
    gaps = [b - a for a, b in zip(times, times[1:])]
    # The first heartbeat is sent immediately, then one per interval. Only the
    # lower bound is asserted: a loaded machine may run late, never early.
    assert all(gap >= interval * 0.6 for gap in gaps), gaps


async def test_a_long_interval_is_respected_after_the_first_immediate_beat(
    udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder, interval_ms=5000, stale_ms=10000)
    await service.start()

    assert await udp_recorder.wait_for(1), "no first heartbeat"
    await asyncio.sleep(0.3)  # Far shorter than the 5 s interval.
    assert len(udp_recorder.datagrams) == 1


async def test_sequence_numbers_increment_by_one(
    udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder)
    await service.start()

    assert await udp_recorder.wait_for(3)
    sequences = [parse_datagram(data).sequence for data in udp_recorder.datagrams[:3]]
    assert sequences == [1, 2, 3]
    assert service.last_sequence == 3
    assert service.datagrams_sent == 3


async def test_a_heartbeat_reports_the_idle_state_when_nothing_is_pressed(
    session: DeviceSession, udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder)
    await service.start()

    assert await udp_recorder.wait_for(1)
    message = parse_datagram(udp_recorder.datagrams[0])
    assert message.state == ActuatorState.IDLE.value
    assert message.session_active is False
    assert message.device_id  # non-empty


async def test_the_reported_state_comes_from_the_authoritative_session_manager(
    session: DeviceSession, udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder)
    await service.start()
    assert await udp_recorder.wait_for(1)

    # The press is started on the TCP side only. The heartbeat must notice by
    # reading the session, because it holds no state of its own.
    reply = await tcp_command(session, CommandType.PRESS_START)
    assert isinstance(reply, Ack) and reply.state == ActuatorState.PRESSING.value

    message = await wait_for_datagram(udp_recorder, lambda m: m.session_active)
    assert message.state == ActuatorState.PRESSING.value

    # ...and back to idle once the press is stopped.
    reply = await tcp_command(session, CommandType.PRESS_STOP, command_id="cmd-2")
    assert isinstance(reply, Ack) and reply.state == ActuatorState.IDLE.value

    message = await wait_for_datagram(udp_recorder, lambda m: not m.session_active)
    assert message.state == ActuatorState.IDLE.value


async def test_the_first_datagram_is_a_status_and_an_unchanged_tick_is_a_heartbeat(
    udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder)
    await service.start()

    assert await udp_recorder.wait_for(2)
    first, second = (parse_datagram(d) for d in udp_recorder.datagrams[:2])
    # The first datagram carries a fresh snapshot, so it is a status report.
    assert first.type == STATUS
    # Nothing changed in between, so the next one is an ordinary heartbeat.
    assert second.type == HEARTBEAT


async def test_a_state_change_is_reported_immediately_as_a_status(
    session: DeviceSession, udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder)
    await service.start()
    assert await udp_recorder.wait_for(1)

    await tcp_command(session, CommandType.PRESS_START)
    changed = await wait_for_datagram(udp_recorder, lambda m: m.session_active)
    assert changed.type == STATUS

    # The next unchanged tick goes back to being a heartbeat.
    following = await wait_for_datagram(udp_recorder, lambda m: m.sequence > changed.sequence)
    assert following.type == HEARTBEAT


async def test_the_heartbeat_stops_when_the_service_shuts_down(
    udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder, interval_ms=20)
    await service.start()
    assert await udp_recorder.wait_for(2)

    await service.stop()
    sent = len(udp_recorder.datagrams)
    await asyncio.sleep(0.15)  # Several intervals' worth of silence.
    assert len(udp_recorder.datagrams) == sent


async def test_no_heartbeat_task_remains_after_shutdown(
    udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder)
    await service.start()
    assert await udp_recorder.wait_for(1)
    assert len(heartbeat_tasks()) == 1

    await service.stop()
    assert heartbeat_tasks() == []


async def test_stopping_releases_the_socket_and_is_safe_to_repeat(
    udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder)
    assert service.is_running is False
    assert service.local_address is None

    await service.start()
    assert service.is_running is True
    address = service.local_address
    assert address is not None and address[1] > 0

    await service.stop()
    assert service.is_running is False
    assert service.local_address is None
    # Shutting down twice must not raise.
    await service.stop()


async def test_the_monitoring_client_interprets_a_real_heartbeat_stream(
    udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder)
    await service.start()
    assert await udp_recorder.wait_for(3)

    monitor = StatusMonitor(stale_after_ms=5000, expected_device_id=DEFAULT_DEVICE_ID)
    for data in udp_recorder.datagrams:
        monitor.observe(data)

    assert monitor.accepted == 3
    assert monitor.last_sequence == 3
    assert monitor.is_offline() is False


# ----------------------------------------------------------------------
# Failure handling
# ----------------------------------------------------------------------


async def test_send_once_without_a_socket_reports_failure(
    session: DeviceSession, udp_recorder: UdpRecorder
) -> None:
    service = HeartbeatService(session, make_config(udp_recorder))
    assert await service.send_once() is False
    assert service.datagrams_sent == 0
    assert service.send_failures == 0


async def test_a_failed_send_is_counted_and_consumes_no_sequence_number(
    session: DeviceSession, udp_recorder: UdpRecorder
) -> None:
    service = HeartbeatService(session, make_config(udp_recorder))
    await service.start()
    assert await udp_recorder.wait_for(1)
    sequence_before = service.last_sequence

    # Break the socket underneath the already-running service.
    service._transport = _BrokenTransport()  # type: ignore[assignment]
    assert await service.send_once() is False

    assert service.send_failures == 1
    assert service.last_sequence == sequence_before
    assert service.datagrams_sent == 1


async def test_a_send_failure_does_not_kill_the_heartbeat_task(
    session: DeviceSession, udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder, interval_ms=20)
    await service.start()
    assert await udp_recorder.wait_for(1)

    service._transport = _BrokenTransport()  # type: ignore[assignment]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1.0
    while service.send_failures < 2 and loop.time() < deadline:
        await asyncio.sleep(0.005)

    assert service.send_failures >= 2, "the task stopped sending after a failure"
    assert len(heartbeat_tasks()) == 1, "the heartbeat task died on a send error"


# ----------------------------------------------------------------------
# Isolation: UDP must not be able to touch the actuator
# ----------------------------------------------------------------------


async def test_udp_traffic_cannot_initiate_a_press(
    session: DeviceSession, udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    service = await make_heartbeat(udp_recorder)
    await service.start()
    assert await udp_recorder.wait_for(1)

    device = service.local_address
    assert device is not None

    # Every plausible attempt at a command, sent straight at the device socket.
    for payload in (
        b'{"type":"PRESS_START","command_id":"x","session_id":"s"}',
        b'{"type":"press_start"}',
        b"PRESS_START",
        b"\x00\xff\x00",
    ):
        send_udp(device[0], device[1], payload)

    await asyncio.sleep(0.05)
    assert session.state is ActuatorState.IDLE
    assert session.active_session_id is None
    assert session.watchdog_active is False


async def test_udp_traffic_cannot_stop_a_press(
    session: DeviceSession, udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    reply = await tcp_command(session, CommandType.PRESS_START)
    assert isinstance(reply, Ack)
    assert session.state is ActuatorState.PRESSING

    service = await make_heartbeat(udp_recorder)
    await service.start()
    assert await udp_recorder.wait_for(1)
    device = service.local_address
    assert device is not None

    send_udp(device[0], device[1], b'{"type":"PRESS_STOP","command_id":"x","session_id":"s"}')
    send_udp(device[0], device[1], b'{"type":"PRESS_RENEW","command_id":"x","session_id":"s"}')
    await asyncio.sleep(0.05)

    assert session.state is ActuatorState.PRESSING
    assert session.active_session_id == SESSION_ID
    assert session.owner_client_id == CLIENT_ID


async def test_udp_traffic_does_not_move_the_tcp_watchdog_deadline(
    session: DeviceSession, udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    reply = await tcp_command(session, CommandType.PRESS_START)
    assert isinstance(reply, Ack)

    service = await make_heartbeat(udp_recorder)
    await service.start()
    assert await udp_recorder.wait_for(1)
    device = service.local_address
    assert device is not None

    deadline_before = session.lease_deadline
    assert deadline_before > 0.0

    # Losing every UDP packet (or flooding the socket with renewals) must leave
    # the lease exactly where it was.
    for _ in range(10):
        send_udp(device[0], device[1], b'{"type":"PRESS_RENEW","session_id":"s"}')
    await asyncio.sleep(0.05)
    assert session.lease_deadline == deadline_before

    # Only a TCP renewal may move the deadline, which proves the previous
    # assertion is not trivially true.
    reply = await tcp_command(session, CommandType.PRESS_RENEW, command_id="cmd-2")
    assert isinstance(reply, Ack)
    assert session.lease_deadline > deadline_before


async def test_stopping_the_udp_channel_does_not_release_a_press(
    session: DeviceSession, udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    reply = await tcp_command(session, CommandType.PRESS_START)
    assert isinstance(reply, Ack)

    service = await make_heartbeat(udp_recorder)
    await service.start()
    assert await udp_recorder.wait_for(1)

    await service.stop()

    assert session.state is ActuatorState.PRESSING
    assert session.active_session_id == SESSION_ID
    assert session.watchdog_active is True


async def test_a_broken_udp_channel_does_not_stop_tcp_command_handling(
    session: DeviceSession,
    udp_recorder: UdpRecorder,
    make_heartbeat: HeartbeatFactory,
    make_client: ClientFactory,
) -> None:
    client = await make_client()
    await client.send_json(
        {"type": "PRESS_START", "command_id": "c1", "session_id": "s1"}
    )
    assert (await client.read_json())["state"] == ActuatorState.PRESSING.value

    service = await make_heartbeat(udp_recorder, interval_ms=20)
    await service.start()
    assert await udp_recorder.wait_for(1)

    # Take the UDP channel down while it is running.
    service._transport = _BrokenTransport()  # type: ignore[assignment]
    await asyncio.sleep(0.1)

    # The TCP command channel is untouched: it still accepts commands.
    await client.send_json(
        {"type": "PRESS_STOP", "command_id": "c2", "session_id": "s1"}
    )
    response = await client.read_json()
    assert response["type"] == "ACK"
    assert response["state"] == ActuatorState.IDLE.value
    assert session.state is ActuatorState.IDLE


async def test_udp_cannot_be_used_as_a_command_channel_for_another_connection(
    session: DeviceSession, udp_recorder: UdpRecorder, make_heartbeat: HeartbeatFactory
) -> None:
    # Even a datagram that copies a valid session id and owner gets nowhere: the
    # UDP socket has no path into DeviceSession.handle at all.
    reply = await tcp_command(session, CommandType.PRESS_START)
    assert isinstance(reply, Ack)

    service = await make_heartbeat(udp_recorder)
    await service.start()
    assert await udp_recorder.wait_for(1)
    device = service.local_address
    assert device is not None

    send_udp(
        device[0],
        device[1],
        b'{"type":"PRESS_STOP","command_id":"c1","session_id":"session-1"}',
    )
    await asyncio.sleep(0.05)
    assert session.state is ActuatorState.PRESSING
