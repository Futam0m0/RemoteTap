"""End-to-end tests for the demo bridge.

These start the real FastAPI app under a real uvicorn server and talk to it over
real HTTP and a real WebSocket, against the real device TCP server. The browser
side is played by an HTTP/WebSocket client, so the tests cover the same path a
page uses — but through the bridge, never by talking to the device directly.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Callable, Optional

import httpx
import pytest
import uvicorn
import websockets

from remote_tap.bridge.app import BridgeConfig, BridgeRuntime, create_app
from remote_tap.config import DEFAULT_DEVICE_ID
from remote_tap.net.tcp_server import TcpCommandServer
from remote_tap.protocol.udp_messages import HEARTBEAT, StatusDatagram, encode_datagram
from remote_tap.session import DeviceSession


class LiveBridge:
    """A running bridge plus the URLs and objects its tests need."""

    def __init__(self, app, server: uvicorn.Server, task: asyncio.Task, port: int) -> None:
        self.app = app
        self.server = server
        self.task = task
        self.port = port

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    @property
    def runtime(self) -> BridgeRuntime:
        return self.app.state.bridge


@pytest.fixture
async def bridge(
    server: TcpCommandServer, wait_until: Callable[..., object]
) -> AsyncIterator[LiveBridge]:
    """A bridge serving an ephemeral port, pointed at the test device server."""
    config = BridgeConfig(
        device_host="127.0.0.1",
        device_port=server.port,
        udp_host="127.0.0.1",
        udp_port=0,
        web_host="127.0.0.1",
        web_port=0,
        expected_device_id=None,
    )
    app = create_app(config)
    uv_config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
    )
    uv = uvicorn.Server(uv_config)
    task = asyncio.ensure_future(uv.serve())
    try:
        started = await wait_until(lambda: getattr(uv, "started", False), timeout=10.0)
        assert started, "the bridge did not start in time"
        port = uv.servers[0].sockets[0].getsockname()[1]
        yield LiveBridge(app, uv, task, port)
    finally:
        uv.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            task.cancel()


async def _send_udp(port: int, message: StatusDatagram) -> None:
    """Send one datagram to the bridge's status listener."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, remote_addr=("127.0.0.1", port)
    )
    try:
        transport.sendto(encode_datagram(message))
    finally:
        transport.close()


def _heartbeat(sequence: int = 1) -> StatusDatagram:
    """A plausible device heartbeat."""
    return StatusDatagram(
        type=HEARTBEAT,
        device_id=DEFAULT_DEVICE_ID,
        boot_id="boot-live",
        sequence=sequence,
        timestamp_ms=1_700_000_000_000 + sequence,
        state="IDLE",
        session_active=False,
    )


async def _next(websocket, predicate: Optional[Callable] = None, timeout: float = 3.0):
    """Read messages until one satisfies ``predicate``."""

    async def _loop():
        while True:
            message = json.loads(await websocket.recv())
            if predicate is None or predicate(message):
                return message

    return await asyncio.wait_for(_loop(), timeout)


def _is_state(websocket_message: dict) -> bool:
    """Whether a message is a state snapshot."""
    return websocket_message.get("type") == "state"


def _pressed(websocket_message: dict) -> bool:
    """A state snapshot in which the bridge is holding a press."""
    return _is_state(websocket_message) and bool(
        websocket_message.get("press", {}).get("active")
    )


def _released(websocket_message: dict) -> bool:
    """A state snapshot in which nothing is pressed."""
    return _is_state(websocket_message) and not websocket_message.get(
        "press", {}
    ).get("active")


# --------------------------------------------------------------------- HTTP


async def test_serves_the_dashboard(bridge: LiveBridge) -> None:
    """The page and its assets are served from the bridge."""
    async with httpx.AsyncClient(base_url=bridge.http_url) as client:
        page = await client.get("/")
        script = await client.get("/static/app.js")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "RemoteTap" in page.text
    assert script.status_code == 200


async def test_state_endpoint_reports_the_device_link(bridge: LiveBridge) -> None:
    """The JSON snapshot shows a live device connection and an idle actuator."""
    async with httpx.AsyncClient(base_url=bridge.http_url) as client:
        response = await client.get("/api/state")

    assert response.status_code == 200
    body = response.json()
    assert body["device"]["connected"] is True
    assert body["actuator"]["state"] == "IDLE"
    assert body["udp"]["listening"] is True


# ---------------------------------------------------------------- WebSocket


async def test_websocket_greets_with_state(bridge: LiveBridge) -> None:
    """A connecting page is sent the current state straight away."""
    async with websockets.connect(bridge.ws_url) as websocket:
        message = await _next(websocket)

    assert message["type"] == "state"
    assert message["device"]["connected"] is True


async def test_press_and_release_over_websocket(
    bridge: LiveBridge, session: DeviceSession
) -> None:
    """A press request drives the device, and a stop releases it."""
    async with websockets.connect(bridge.ws_url) as websocket:
        await _next(websocket)

        await websocket.send(json.dumps({"action": "press_start"}))
        pressed = await _next(websocket, _pressed)
        assert pressed["actuator"]["state"] == "PRESSING"
        assert session.state.value == "PRESSING"

        await websocket.send(json.dumps({"action": "press_stop"}))
        released = await _next(websocket, _released)
        assert released["actuator"]["state"] == "IDLE"
        assert session.state.value == "IDLE"


async def test_browser_disconnect_while_pressing_releases_the_press(
    bridge: LiveBridge, session: DeviceSession, wait_until
) -> None:
    """Closing the page must not leave the actuator pressing.

    The bridge releases on the connection's behalf; the device's own disconnect
    release remains the backstop behind it.
    """
    websocket = await websockets.connect(bridge.ws_url)
    await _next(websocket)
    await websocket.send(json.dumps({"action": "press_start"}))
    await _next(websocket, _pressed)
    assert session.state.value == "PRESSING"

    await websocket.close()

    # The device releases as soon as it handles the stop, which is slightly
    # before the bridge reads the acknowledgement and clears its own view.
    assert await wait_until(lambda: session.state.value == "IDLE")
    assert await wait_until(lambda: not bridge.runtime.press.active)


async def test_invalid_commands_are_rejected_and_the_page_survives(
    bridge: LiveBridge, session: DeviceSession
) -> None:
    """An unknown action and a non-JSON message are refused, not fatal."""
    async with websockets.connect(bridge.ws_url) as websocket:
        await _next(websocket)

        await websocket.send(json.dumps({"action": "launch_missiles"}))
        unknown = await _next(
            websocket, lambda m: m.get("type") == "notice"
        )
        assert unknown["level"] == "error"
        assert "unknown action" in unknown["message"]

        await websocket.send("definitely not json")
        broken = await _next(websocket, lambda m: m.get("type") == "notice")
        assert broken["level"] == "error"

        # The connection is still usable after both rejections.
        await websocket.send(json.dumps({"action": "press_start"}))
        assert (await _next(websocket, _pressed))["press"]["active"] is True
        await websocket.send(json.dumps({"action": "press_stop"}))
        await _next(websocket, _released)


async def test_only_the_owning_browser_may_stop_the_press(
    bridge: LiveBridge, session: DeviceSession
) -> None:
    """A second page cannot act on a press it did not start."""
    async with websockets.connect(bridge.ws_url) as owner, websockets.connect(
        bridge.ws_url
    ) as other:
        await _next(owner)
        await _next(other)

        await owner.send(json.dumps({"action": "press_start"}))
        await _next(owner, _pressed)

        await other.send(json.dumps({"action": "press_stop"}))
        refusal = await _next(other, lambda m: m.get("type") == "notice")

        assert refusal["level"] == "error"
        assert session.state.value == "PRESSING"

        await owner.send(json.dumps({"action": "press_stop"}))
        await _next(owner, _released)


async def test_udp_status_is_forwarded_to_the_page(
    bridge: LiveBridge, wait_until
) -> None:
    """Heartbeats the device sends over UDP reach the page through the bridge."""
    hub = bridge.runtime.hub
    assert hub is not None

    async with websockets.connect(bridge.ws_url) as websocket:
        await _next(websocket)

        await _send_udp(hub.port, _heartbeat(sequence=7))

        snapshot = await _next(
            websocket,
            lambda m: _is_state(m) and m.get("udp", {}).get("sequence") == 7,
        )

    assert snapshot["udp"]["online"] is True
    assert snapshot["udp"]["state"] == "IDLE"
    assert snapshot["udp"]["device_id"] == DEFAULT_DEVICE_ID


# -------------------------------------------------------------- safety/lifecycle


async def test_runtime_shutdown_releases_the_press(
    server: TcpCommandServer, session: DeviceSession
) -> None:
    """Stopping the bridge stops its press, so nothing is left held down."""
    runtime = BridgeRuntime(
        BridgeConfig(
            device_host="127.0.0.1",
            device_port=server.port,
            udp_port=0,
            expected_device_id=None,
            use_udp=False,
        )
    )
    await runtime.start()
    try:
        outcome = await runtime.press.start("ws1")
        assert outcome.ok
        assert session.state.value == "PRESSING"
    finally:
        await runtime.stop()

    assert session.state.value == "IDLE"


async def test_bridge_without_a_device_still_serves_the_page(
    server: TcpCommandServer, wait_until
) -> None:
    """A device that is down is reported, instead of preventing the page load."""
    # Point at a port nothing is listening on: the bridge must still come up.
    config = BridgeConfig(
        device_host="127.0.0.1",
        device_port=1,
        udp_port=0,
        expected_device_id=None,
    )
    app = create_app(config)
    uv_config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
    )
    uv = uvicorn.Server(uv_config)
    task = asyncio.ensure_future(uv.serve())
    try:
        assert await wait_until(lambda: getattr(uv, "started", False), timeout=10.0)
        port = uv.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            response = await client.get("/api/state")
        body = response.json()
        assert body["device"]["connected"] is False
        assert body["actuator"]["state"] == "IDLE"
    finally:
        uv.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            task.cancel()
