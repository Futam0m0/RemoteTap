"""Tests for the bridge's single-press controller.

The controller is exercised against the real device server, so the lease renewal
it drives is the real protocol interaction — not a simulation of one.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from remote_tap.actuator.simulator import SimulatedActuator
from remote_tap.bridge.device_link import DeviceLink
from remote_tap.bridge.press import IDLE, PRESSING, PressController
from remote_tap.config import PressLimits, ServerConfig
from remote_tap.net.tcp_server import TcpCommandServer
from remote_tap.session import DeviceSession


@pytest.fixture
async def bridge_press(server: TcpCommandServer) -> AsyncIterator[tuple]:
    """A connected link and a press controller, cleaned up afterwards."""
    link = DeviceLink("127.0.0.1", server.port)
    await link.connect()
    controller = PressController(link)
    try:
        yield link, controller
    finally:
        await controller.shutdown()
        await link.close()


@pytest.fixture
async def fast_press() -> AsyncIterator[tuple]:
    """A device with very short safety limits, for timing-sensitive tests.

    Renew every 50 ms, release after 120 ms without one, and never exceed 600 ms
    in one press — fast enough to observe a lease and a maximum in a test.
    """
    limits = PressLimits(renew_interval_ms=50, lease_timeout_ms=120, max_press_ms=600)
    device = DeviceSession(SimulatedActuator(label="fast"), limits=limits)
    server = TcpCommandServer(
        device, ServerConfig(host="127.0.0.1", port=0), name="fast-server"
    )
    await server.start()
    link = DeviceLink("127.0.0.1", server.port)
    await link.connect()
    controller = PressController(link)
    try:
        yield link, controller, device
    finally:
        await controller.shutdown()
        await link.close()
        await device.release(reason="test_teardown")
        await server.stop()


async def test_press_and_release(
    bridge_press: tuple, session: DeviceSession
) -> None:
    """A press starts the device, and a stop releases it."""
    _, press = bridge_press

    started = await press.start("ws1")

    assert started.ok
    assert press.active
    assert press.state == PRESSING
    assert session.state.value == "PRESSING"
    assert press.renew_interval_ms is not None

    stopped = await press.stop("ws1")

    assert stopped.ok
    assert not press.active
    assert press.state == IDLE
    assert session.state.value == "IDLE"


async def test_bridge_renews_the_lease_while_held(fast_press: tuple) -> None:
    """The bridge — not the page — keeps the press alive.

    The lease lasts 120 ms here. Without the bridge's own renewals the device
    would release the press long before the test checks, so surviving well past
    the lease proves the renewals are happening.
    """
    _, press, device = fast_press

    await press.start("ws1")
    assert press.active

    # More than four lease timeouts; only the bridge's renewals can explain the
    # device still pressing at the end of it.
    await asyncio.sleep(0.5)

    assert device.state.value == "PRESSING"
    assert press.active


async def test_device_enforces_the_maximum_press_duration(
    fast_press: tuple, wait_until
) -> None:
    """The device ends a long press even though the bridge keeps renewing.

    This is the rule the page must never own: the bridge renews obediently, and
    the device still releases the button at its own ceiling. The bridge simply
    notices and stops.
    """
    _, press, device = fast_press

    await press.start("ws1")
    assert press.active

    assert await wait_until(lambda: not press.active, timeout=3.0)
    assert device.state.value == "IDLE"
    assert press.last_error, "the bridge should report why the press ended"


async def test_second_browser_cannot_stop_the_press(
    bridge_press: tuple, session: DeviceSession
) -> None:
    """Only the connection that started the press may stop it."""
    _, press = bridge_press
    await press.start("ws1")

    refused = await press.stop("ws2")

    assert not refused.ok
    assert refused.code == "NOT_OWNER"
    assert press.active
    assert session.state.value == "PRESSING"


async def test_release_if_owner_releases_only_for_the_owner(
    bridge_press: tuple, session: DeviceSession
) -> None:
    """A browser that did not start the press cannot release it by leaving."""
    _, press = bridge_press
    await press.start("ws1")

    assert await press.release_if_owner("ws2", reason="other browser left") is False
    assert press.active

    assert await press.release_if_owner("ws1", reason="browser left") is True
    assert not press.active
    assert session.state.value == "IDLE"


async def test_repeated_press_start_does_not_restart_the_press(
    bridge_press: tuple, session: DeviceSession
) -> None:
    """A duplicated press request is a no-op, never a second press.

    Re-sending PRESS_START with a fresh session would reach the device as a
    conflicting command; the controller must not do that. The session id is
    unchanged, so the device's maximum-duration timer is not restarted either.
    """
    _, press = bridge_press
    first = await press.start("ws1")
    session_id = press.session_id
    assert first.ok

    second = await press.start("ws1")

    assert second.ok
    assert press.session_id == session_id
    assert session.state.value == "PRESSING"


async def test_stopping_when_idle_is_a_no_op(bridge_press: tuple) -> None:
    """A stop with nothing pressing is harmless, not an error."""
    _, press = bridge_press

    outcome = await press.stop("ws1")

    assert outcome.ok
    assert not press.active


async def test_press_without_a_device_link_reports_an_error() -> None:
    """The page is told the device is unreachable instead of hanging."""
    link = DeviceLink("127.0.0.1", 1)
    press = PressController(link)

    outcome = await press.start("ws1")

    assert not outcome.ok
    assert not press.active
    assert "not connected" in outcome.message


async def test_lost_link_clears_the_bridge_press(bridge_press: tuple) -> None:
    """When the device connection drops, the bridge stops claiming a press.

    The device has already released the actuator by this point; this only keeps
    the page's view honest.
    """
    link, press = bridge_press
    await press.start("ws1")
    assert press.active

    await link.close()
    await press.handle_link_lost("connection closed")

    assert not press.active
    assert press.owner is None
