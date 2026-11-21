"""Shared test fixtures.

The tests speak the protocol over real TCP and UDP sockets, so they exercise
the same path a client uses: bytes in, JSON messages out.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Awaitable, Callable

import pytest

from remote_tap.actuator.simulator import SimulatedActuator
from remote_tap.config import PressLimits, ServerConfig
from remote_tap.net.tcp_server import TcpCommandServer
from remote_tap.session import DeviceSession


class RawClient:
    """A minimal raw TCP client, independent of the shipped CLI client."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(cls, host: str, port: int) -> "RawClient":
        """Open a connection to the server."""
        reader, writer = await asyncio.open_connection(host, port)
        return cls(reader, writer)

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Send one JSON message, newline terminated."""
        await self.send_bytes((json.dumps(payload) + "\n").encode("utf-8"))

    async def send_bytes(self, data: bytes) -> None:
        """Send raw bytes, to test framing behaviour."""
        self._writer.write(data)
        await self._writer.drain()

    async def read_json(self) -> dict[str, Any]:
        """Read exactly one JSON message from the server."""
        line = await self._reader.readuntil(b"\n")
        return json.loads(line.decode("utf-8"))

    async def close(self) -> None:
        """Disconnect."""
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass


@pytest.fixture
def actuator() -> SimulatedActuator:
    """A simulated actuator, isolated per test."""
    return SimulatedActuator(label="test")


@pytest.fixture
def press_limits() -> PressLimits:
    """The production press-safety limits.

    They are slow enough (lease 1000 ms) that a test which never touches the
    timings cannot be released by the watchdog mid-assertion; lease tests build
    their own fast limits instead.
    """
    return PressLimits()


@pytest.fixture
async def session(
    actuator: SimulatedActuator, press_limits: PressLimits
) -> AsyncIterator[DeviceSession]:
    """Device state on top of the simulated actuator.

    The press is always released afterwards, which both stops the lease timer
    (so no watchdog task outlives the test) and keeps the loop clean.
    """
    device = DeviceSession(actuator, limits=press_limits)
    try:
        yield device
    finally:
        await device.release(reason="test_teardown")


@pytest.fixture
async def server(session: DeviceSession) -> AsyncIterator[TcpCommandServer]:
    """A running TCP server on an ephemeral loopback port."""
    config = ServerConfig(host="127.0.0.1", port=0)
    running = TcpCommandServer(session, config, name="test-server")
    await running.start()
    try:
        yield running
    finally:
        await running.stop()


@pytest.fixture
async def make_client(
    server: TcpCommandServer,
) -> AsyncIterator[Callable[[], Awaitable[RawClient]]]:
    """Factory for extra connections; every client is closed after the test."""
    clients: list[RawClient] = []

    async def connect() -> RawClient:
        assert server.port is not None, "server did not bind a port"
        client = await RawClient.connect("127.0.0.1", server.port)
        clients.append(client)
        return client

    yield connect

    for client in clients:
        await client.close()


@pytest.fixture
async def client(
    make_client: Callable[[], Awaitable[RawClient]],
) -> AsyncIterator[RawClient]:
    """A connected client, closed after the test."""
    connected = await make_client()
    yield connected


class UdpRecorder(asyncio.DatagramProtocol):
    """A listening UDP endpoint that records the datagrams it receives.

    Used to stand in for the monitoring client, so tests can inspect the real
    bytes the device puts on the wire without racing a fixed sleep.
    """

    def __init__(self) -> None:
        self.received: list[tuple[bytes, object, float]] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: object) -> None:
        self.received.append((data, addr, asyncio.get_running_loop().time()))

    @property
    def port(self) -> int:
        """The port this recorder bound to."""
        assert self.transport is not None, "recorder is not listening"
        return self.transport.get_extra_info("sockname")[1]

    @property
    def datagrams(self) -> list[bytes]:
        """Just the payloads, in arrival order."""
        return [data for data, _, _ in self.received]

    def arrival_times(self) -> list[float]:
        """Monotonic arrival time of each datagram, in order."""
        return [when for _, _, when in self.received]

    async def wait_for(self, count: int, timeout: float = 2.0) -> bool:
        """Wait until at least ``count`` datagrams have arrived.

        Polls rather than sleeping a fixed amount, so the tests stay quick and
        do not depend on an arbitrary delay being long enough.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(self.received) >= count:
                return True
            await asyncio.sleep(0.005)
        return len(self.received) >= count


@pytest.fixture
async def udp_recorder() -> AsyncIterator[UdpRecorder]:
    """A listening UDP endpoint on an ephemeral loopback port."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        UdpRecorder, local_addr=("127.0.0.1", 0)
    )
    try:
        yield protocol
    finally:
        transport.close()


@pytest.fixture
def wait_until() -> Callable[..., Awaitable[bool]]:
    """Poll a predicate until it holds, instead of sleeping a fixed amount.

    Keeps tests that depend on a background task (a lease renewal, a heartbeat
    arriving, a connection closing) quick and free of arbitrary delays.
    """

    async def _wait_until(
        predicate: Callable[[], bool], timeout: float = 2.0, interval: float = 0.005
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return True
            await asyncio.sleep(interval)
        return predicate()

    return _wait_until
