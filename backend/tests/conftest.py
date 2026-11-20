"""Shared test fixtures.

The tests speak the protocol over a real TCP socket, so they exercise the same
path a client uses: bytes in, JSON lines out.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Awaitable, Callable

import pytest

from remote_tap.actuator.simulator import SimulatedActuator
from remote_tap.config import ServerConfig
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
def session(actuator: SimulatedActuator) -> DeviceSession:
    """Device state on top of the simulated actuator."""
    return DeviceSession(actuator)


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
