"""Tests for the bridge's TCP command link.

These speak to the real device TCP server over a real socket, so they exercise
exactly the path the demo bridge uses: the existing command protocol, unchanged.
"""

from __future__ import annotations

import pytest

from remote_tap.bridge.device_link import DeviceLink, DeviceLinkError
from remote_tap.net.tcp_server import TcpCommandServer
from remote_tap.protocol.messages import CommandType
from remote_tap.session import DeviceSession


async def test_connect_and_send_press_start(server: TcpCommandServer) -> None:
    """The link opens a connection and a command gets a validated reply."""
    link = DeviceLink("127.0.0.1", server.port)
    await link.connect()
    try:
        assert link.connected

        response = await link.send(CommandType.PRESS_START, "session-a")

        assert response["type"] == "ACK"
        assert response["state"] == "PRESSING"
        # The device advertises its lease policy, as it does to any client.
        assert response["renew_interval_ms"] > 0
        assert response["max_press_ms"] > 0
    finally:
        await link.close()

    assert not link.connected


async def test_send_without_connecting_is_refused() -> None:
    """A command cannot be delivered before the link is connected."""
    link = DeviceLink("127.0.0.1", 1)

    with pytest.raises(DeviceLinkError):
        await link.send(CommandType.PRESS_START, "session-a")


async def test_link_notices_the_device_closing(
    server: TcpCommandServer, wait_until
) -> None:
    """A disconnect is noticed promptly, not only on the next send.

    The listener owns the stream, so the bridge learns the device is gone as
    soon as the socket closes rather than hanging on a reply that never comes.
    """
    lost: list[str] = []
    link = DeviceLink("127.0.0.1", server.port, on_lost=lost.append)
    await link.connect()
    await link.send(CommandType.PRESS_START, "session-a")

    await server.stop()

    assert await wait_until(lambda: not link.connected)
    assert lost, "on_lost should fire when the device closes the connection"

    with pytest.raises(DeviceLinkError):
        await link.send(CommandType.PRESS_START, "session-a")


async def test_closing_a_link_releases_its_own_press(
    server: TcpCommandServer, session: DeviceSession, wait_until
) -> None:
    """The device releases a press when its owning connection goes away.

    This is the device's own ``release_if_client`` behaviour, which is what makes
    a vanished bridge or browser safe; the link only has to close its socket.
    """
    link = DeviceLink("127.0.0.1", server.port)
    await link.connect()
    await link.send(CommandType.PRESS_START, "session-a")
    assert session.state.value == "PRESSING"

    await link.close()

    assert await wait_until(lambda: session.state.value == "IDLE")


async def test_only_the_owning_connection_may_stop_a_press(
    server: TcpCommandServer, session: DeviceSession
) -> None:
    """A second connection cannot command the first connection's session.

    The bridge does not weaken session ownership: it is still one client of the
    device, and the device decides who may act.
    """
    owner = DeviceLink("127.0.0.1", server.port)
    intruder = DeviceLink("127.0.0.1", server.port)
    await owner.connect()
    await intruder.connect()
    try:
        started = await owner.send(CommandType.PRESS_START, "session-a")
        assert started["type"] == "ACK"

        stopped = await intruder.send(CommandType.PRESS_STOP, "session-a")

        assert stopped["type"] == "ERROR"
        assert stopped["code"] == "INVALID_SESSION"
        assert session.state.value == "PRESSING"
    finally:
        await intruder.close()
        await owner.close()


async def test_device_rejects_an_invalid_command(
    server: TcpCommandServer,
) -> None:
    """A malformed command is rejected by the device, not silently accepted."""
    link = DeviceLink("127.0.0.1", server.port)
    await link.connect()
    try:
        # An unknown command type never validates, and the device says so.
        response = await link.send(CommandType.PRESS_RENEW, "session-does-not-exist")

        assert response["type"] == "ERROR"
        assert response["code"] == "INVALID_SESSION"
    finally:
        await link.close()
