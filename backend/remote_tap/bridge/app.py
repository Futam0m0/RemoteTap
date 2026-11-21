"""The local web bridge: HTTP + WebSocket on one side, TCP + UDP on the other.

The browser cannot open raw TCP or UDP sockets, so this module is the small,
localhost-only process that stands between the page and the existing device
service. It is a **client** of RemoteTap, never a replacement for it.

Two transports, for two different jobs
--------------------------------------

* **Browser <-> bridge: HTTP and WebSocket.** Ordinary web transports, because
  that is all a page has. Used to serve the dashboard, hand it a state snapshot,
  and carry "the button went down / came up" events.
* **Bridge <-> device: the existing protocols.** The command channel stays the
  original newline-delimited JSON over TCP, and the status channel stays the
  original UDP heartbeat datagrams. WebSocket never carries a command to the
  device, and the bridge never invents a new command format. See
  :mod:`remote_tap.bridge.device_link`, :mod:`remote_tap.bridge.press` and
  :mod:`remote_tap.bridge.status_hub`.

What the bridge is careful *not* to do
--------------------------------------

* It does not reimplement the session manager, the lease, the watchdog or the
  actuator. Every command goes through the device's own session manager, so the
  bridge inherits the same ownership, idempotency and safety rules as any other
  client — a browser request cannot bypass them.
* It does not enforce any safety rule itself. The page's release handling is a
  convenience; the lease and the maximum press duration on the device are what
  actually protect the actuator, and they are untouched.
* It does not open a second UDP receiver. The bridge is the *only* listener on
  the heartbeat port and it reuses the shipped monitor to interpret it.

Safety on disconnection
-----------------------

Both ways the page or the bridge can vanish are covered by existing device
behaviour, not by anything new:

* the page disappears -> the socket closes, and the bridge releases the press,
  sending the same ``PRESS_STOP`` any client would;
* the bridge disappears (or its TCP connection drops) -> the device runs its own
  ``release_if_client`` for that connection, and the lease stops being renewed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import (
    DEFAULT_DEVICE_ID,
    DEFAULT_PORT,
    DEFAULT_STALE_AFTER_MS,
    DEFAULT_UDP_BIND_HOST,
    DEFAULT_UDP_DEST_PORT,
)
from .device_link import DeviceLink
from .press import PressController
from .status_hub import StatusHub

logger = logging.getLogger("remote_tap.bridge")

#: Static dashboard assets, next to the device package.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

#: Default port for the demo page. Loopback only: the demo is not a product
#: surface and must never be reachable from another machine.
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080


@dataclass(frozen=True)
class BridgeConfig:
    """Where the bridge listens and which device service it talks to.

    Args:
        device_host: device service address (the bridge is its TCP client).
        device_port: device service TCP command port.
        udp_host: local address to listen for heartbeats on.
        udp_port: local UDP port hearts are received on — the device's
            ``--udp-dest-port``.
        web_host: address the demo page is served on. Loopback by default.
        web_port: port the demo page is served on.
        stale_after_ms: offline threshold for the status display; should match
            the device's own ``--stale-ms``.
        expected_device_id: if set, heartbeats from any other device are
            ignored. ``None`` accepts any device, like ``remote_tap.monitor``.
        use_udp: set ``False`` to run without the status channel (TCP commands
            are unaffected).
        reconnect_interval_s: how often to retry the device connection while it
            is down.
    """

    device_host: str = "127.0.0.1"
    device_port: int = DEFAULT_PORT
    udp_host: str = DEFAULT_UDP_BIND_HOST
    udp_port: int = DEFAULT_UDP_DEST_PORT
    web_host: str = DEFAULT_WEB_HOST
    web_port: int = DEFAULT_WEB_PORT
    stale_after_ms: int = DEFAULT_STALE_AFTER_MS
    expected_device_id: Optional[str] = DEFAULT_DEVICE_ID
    use_udp: bool = True
    reconnect_interval_s: float = 2.0


class BridgeRuntime:
    """Owns the bridge's link, press and status hub, and fans state to the page.

    Kept separate from the HTTP/WebSocket layer so the whole thing can be driven
    and tested without a browser.

    Args:
        config: addresses and timings.
    """

    def __init__(self, config: BridgeConfig) -> None:
        self._config = config
        self._queues: set[asyncio.Queue[dict[str, object]]] = set()
        self._connection_ids = 0
        self._supervisor: Optional[asyncio.Task[None]] = None
        self._started_at = time.monotonic()

        self.link = DeviceLink(
            config.device_host,
            config.device_port,
            on_lost=self._on_link_lost,
        )
        self.press = PressController(self.link, on_change=self.broadcast)
        self.hub: Optional[StatusHub] = None
        if config.use_udp:
            self.hub = StatusHub(
                bind_host=config.udp_host,
                port=config.udp_port,
                stale_after_ms=config.stale_after_ms,
                expected_device_id=config.expected_device_id,
                on_update=self.broadcast,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def config(self) -> BridgeConfig:
        """The settings this runtime was built with."""
        return self._config

    async def start(self) -> None:
        """Start the status hub, connect to the device, and begin supervising.

        Neither failure is fatal: the page must still load and explain that the
        device or its status channel is unavailable, rather than refusing to
        serve at all.
        """
        if self.hub is not None:
            try:
                await self.hub.start()
            except OSError as exc:
                logger.error("could not listen for heartbeats: %s", exc)
                self.hub = None
        try:
            await self.link.connect()
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "device service at %s:%s is not reachable yet: %s",
                self._config.device_host,
                self._config.device_port,
                exc,
            )
        self._supervisor = asyncio.ensure_future(self._supervise())

    async def stop(self) -> None:
        """Release the press, then close everything.

        The order matters: the press is stopped (or, failing that, the socket is
        closed so the device's own disconnect release fires) before the process
        exits, so a browser that is mid-hold can never leave the actuator down.
        """
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None and supervisor is not asyncio.current_task():
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor

        await self.press.shutdown()
        # Closing the TCP connection is what makes the device release anything
        # this link still owns.
        await self.link.close()
        if self.hub is not None:
            await self.hub.stop()

    async def _supervise(self) -> None:
        """Keep trying to reach the device service while it is unavailable."""
        try:
            while True:
                if not self.link.connected:
                    try:
                        await self.link.connect()
                        self.broadcast()
                    except (OSError, asyncio.TimeoutError) as exc:
                        logger.debug("device service still unreachable: %s", exc)
                await asyncio.sleep(self._config.reconnect_interval_s)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # State fan-out
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """The complete state the page renders, as plain JSON-friendly data."""
        udp = (
            self.hub.snapshot()
            if self.hub is not None
            else {
                "online": False,
                "listening": False,
                "device_id": None,
                "boot_id": None,
                "state": None,
                "session_active": None,
                "message_type": None,
                "sequence": None,
                "age_ms": None,
                "last_heartbeat_ms": None,
                "counters": {},
            }
        )
        return {
            "type": "state",
            "device": {
                "connected": self.link.connected,
                "host": self._config.device_host,
                "port": self._config.device_port,
                "error": self.link.last_error,
            },
            # The actuator state as the bridge knows it. The device only ever has
            # IDLE or PRESSING; the page adds its own transient display states.
            "actuator": {"state": self.press.state},
            "press": self.press.summary(),
            "udp": udp,
            "bridge": {
                "uptime_ms": round((time.monotonic() - self._started_at) * 1000),
                "viewers": len(self._queues),
            },
        }

    def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        """Register a viewer and prime it with the current state."""
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=64)
        self._queues.add(queue)
        queue.put_nowait(self.snapshot())
        self.broadcast()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        """Stop sending state to a viewer."""
        self._queues.discard(queue)

    def broadcast(self) -> None:
        """Push the current state to every viewer, dropping stale queued items.

        A slow or stalled viewer must never block the device-facing tasks, so a
        full queue simply drops its oldest snapshot.
        """
        snapshot = self.snapshot()
        for queue in list(self._queues):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(snapshot)

    # ------------------------------------------------------------------
    # Browser connections
    # ------------------------------------------------------------------

    def register_connection(self) -> str:
        """Mint an id for a newly connected browser."""
        self._connection_ids += 1
        return f"ws{self._connection_ids}"

    async def handle_browser_message(
        self, connection_id: str, message: object
    ) -> Optional[dict[str, object]]:
        """Apply one browser request and return an optional reply to that page.

        The reply is a ``notice`` sent only to the requesting connection; the
        resulting state change reaches every viewer through :meth:`broadcast`.
        """
        if not isinstance(message, dict):
            return notice("error", "messages must be JSON objects")
        action = message.get("action")
        if action == "press_start":
            outcome = await self.press.start(connection_id)
        elif action == "press_stop":
            outcome = await self.press.stop(connection_id)
        elif action == "ping":
            return None
        else:
            return notice("error", f"unknown action {action!r}")
        if not outcome.ok:
            return notice("error", outcome.message, code=outcome.code)
        return None

    async def drop_connection(self, connection_id: str) -> bool:
        """Handle a browser going away, releasing its press if it owned one."""
        return await self.press.release_if_owner(
            connection_id, reason="the browser disconnected"
        )

    def _on_link_lost(self, reason: str) -> None:
        """React to the device connection dropping.

        Called from the link's reader task. The device has already released the
        actuator; the bridge only needs to refresh the page and clear its own
        idea of the press.
        """
        self.broadcast()
        asyncio.ensure_future(self.press.handle_link_lost(reason))


def notice(
    level: str, message: str, *, code: Optional[str] = None
) -> dict[str, object]:
    """Build a one-off message for the page (a rejection, or a hint)."""
    payload: dict[str, object] = {"type": "notice", "level": level, "message": message}
    if code is not None:
        payload["code"] = code
    return payload


def create_app(config: Optional[BridgeConfig] = None) -> FastAPI:
    """Build the FastAPI application serving the dashboard and the bridge.

    Args:
        config: bridge settings; defaults to a loopback-only demo pointing at a
            device service on the standard ports.
    """
    config = config or BridgeConfig()
    runtime = BridgeRuntime(config)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        logger.info(
            "bridge ready: page on http://%s:%s, device %s:%s, heartbeats on %s:%s",
            config.web_host,
            config.web_port,
            config.device_host,
            config.device_port,
            config.udp_host,
            config.udp_port,
        )
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="RemoteTap demo bridge", version="0.1.0", lifespan=lifespan)
    app.state.bridge = runtime
    app.state.config = config

    if (WEB_DIR / "index.html").is_file():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    else:  # pragma: no cover - the assets ship with the package
        logger.warning("dashboard assets are missing from %s", WEB_DIR)

    @app.get("/")
    async def index() -> FileResponse:
        """Serve the dashboard page."""
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/state")
    async def state() -> dict[str, object]:
        """The same snapshot the WebSocket streams, for scripted checks."""
        return runtime.snapshot()

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Stream state to a page and apply the press events it sends."""
        await _browser_session(websocket, runtime)

    return app


async def _browser_session(websocket: WebSocket, runtime: BridgeRuntime) -> None:
    """Run one browser connection from accept to disconnect."""
    await websocket.accept()
    connection_id = runtime.register_connection()
    queue = runtime.subscribe()
    logger.info("browser %s connected", connection_id)

    pump = asyncio.ensure_future(_pump_state(websocket, queue))
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(notice("error", "messages must be valid JSON"))
                continue
            reply = await runtime.handle_browser_message(connection_id, message)
            if reply is not None:
                await websocket.send_json(reply)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        runtime.unsubscribe(queue)
        released = await runtime.drop_connection(connection_id)
        logger.info(
            "browser %s disconnected%s",
            connection_id,
            " - released its press" if released else "",
        )


async def _pump_state(
    websocket: WebSocket, queue: asyncio.Queue[dict[str, object]]
) -> None:
    """Send every queued snapshot to the page, until the page goes away."""
    try:
        while True:
            snapshot = await queue.get()
            await websocket.send_json(snapshot)
    except WebSocketDisconnect:
        return
    except RuntimeError:
        # The sockets application raises this when the connection is already
        # closed; there is nothing left to send to.
        return
