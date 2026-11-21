"""The bridge's TCP command link to the device service.

A browser cannot open a raw TCP socket, so the bridge holds **one** ordinary TCP
connection to the RemoteTap device service and speaks the **existing** command
protocol on it (see :mod:`remote_tap.protocol.messages`). Nothing about that
protocol changes here: the same ``PRESS_START`` / ``PRESS_RENEW`` /
``PRESS_STOP`` commands are sent and the same ``ACK`` / ``ERROR`` objects come
back.

Why one connection is enough — and why it matters
-------------------------------------------------

The device's session manager makes the *connection* the owner of a press: a
``client_id`` is minted per TCP connection and only that connection may renew or
stop the session it started. By using exactly one connection and never
inventing a second one, the bridge inherits that ownership model unchanged. It
cannot bypass the session manager, and a browser request can only ever do what
the protocol itself allows.

Deliberately **not** reusing :class:`remote_tap.client.RemoteTapClient`: that is
an interactive CLI helper. It prints every message to stdout and keeps its own
copy of the session id, which would duplicate the bookkeeping the press
controller owns. This link only moves bytes and decodes replies.

Disconnect handling
-------------------

Responses are read by one background task that owns the stream, so the link
notices the device closing the connection *promptly* rather than only on the
next send. When that happens:

* any command in flight fails with :class:`DeviceLinkError` instead of hanging;
* :attr:`connected` becomes ``False``;
* the optional ``on_lost`` callback fires, so the bridge can tell the page and
  drop its press.

The device's own safety behaviour is what actually protects the actuator here:
when this connection goes away the device runs ``release_if_client`` and the
press lease stops being renewed, so a press cannot survive the bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any, Callable, Optional

from ..protocol.messages import CommandType, encode_line

logger = logging.getLogger("remote_tap.bridge.link")


class DeviceLinkError(RuntimeError):
    """The command could not be delivered, or no reply could be read.

    Raised when the bridge is not connected, the device service closed the
    connection, or a reply could not be decoded. The caller should treat the
    press as over rather than retrying blindly.
    """


class DeviceLink:
    """A single, reconnecting TCP connection speaking the command protocol.

    Args:
        host: device service address.
        port: device service TCP port.
        connect_timeout: how long to wait for the TCP connection to open.
        on_lost: called with a short reason whenever an established connection
            goes away. Used by the bridge to update the page and end a press.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_timeout: float = 5.0,
        on_lost: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._on_lost = on_lost

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        # One command is in flight at a time, so one waiter is enough. The lock
        # makes that explicit: a renewal and a stop cannot interleave on the
        # wire, and each reply is matched to the command that asked for it.
        self._lock = asyncio.Lock()
        self._waiter: Optional[asyncio.Future[dict[str, Any]]] = None
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether an established, usable connection to the device exists."""
        return self._writer is not None

    @property
    def host(self) -> str:
        """The device address this link talks to."""
        return self._host

    @property
    def port(self) -> int:
        """The device TCP port this link talks to."""
        return self._port

    @property
    def last_error(self) -> Optional[str]:
        """A short description of the last disconnect, if any."""
        return self._last_error

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the TCP connection if it is not already open.

        Raises:
            OSError: if the connection cannot be established. The caller is
                expected to retry later; the bridge must still serve the page
                while the device is down.
        """
        if self._writer is not None:
            return
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            self._connect_timeout,
        )
        self._reader = reader
        self._writer = writer
        self._last_error = None
        self._reader_task = asyncio.ensure_future(self._read_loop())
        logger.info("connected to the device service at %s:%s", self._host, self._port)

    async def close(self) -> None:
        """Close the connection cleanly and stop reading.

        Closing the socket is what makes the device release a press this link
        owns, so this is deliberately the last thing a shutdown does.
        """
        await self._teardown("closed by the bridge")

    async def _teardown(self, reason: str) -> None:
        """Drop the socket, the reader task and any in-flight command."""
        task, self._reader_task = self._reader_task, None
        writer, self._writer = self._writer, None
        self._reader = None

        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
                await writer.wait_closed()

        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_exception(DeviceLinkError(reason))

        self._last_error = reason
        logger.info("disconnected from the device service: %s", reason)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self, command_type: CommandType, session_id: str
    ) -> dict[str, Any]:
        """Send one command and return the decoded reply.

        Each call gets a fresh ``command_id``, exactly as the protocol expects:
        a repeated renewal must be a *new* command, or the device treats it as a
        retry and does not extend the lease again.

        Args:
            command_type: which command to send.
            session_id: the press session the command refers to.

        Returns:
            The raw decoded reply (``{"type": "ACK", ...}`` or
            ``{"type": "ERROR", ...}``). The caller decides what it means; the
            device has already validated it.

        Raises:
            DeviceLinkError: if the bridge is not connected, the connection is
                lost, or the reply cannot be decoded. The link is left
                disconnected so the caller can react to the device being gone.
        """
        async with self._lock:
            if self._writer is None or self._reader is None:
                raise DeviceLinkError("not connected to the device service")

            command = {
                "type": command_type.value,
                "command_id": uuid.uuid4().hex,
                "session_id": session_id,
            }

            loop = asyncio.get_running_loop()
            self._waiter = loop.create_future()
            try:
                self._writer.write(encode_line(command))
                await self._writer.drain()
                return await self._waiter
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                await self._teardown(f"connection error: {exc}")
                raise DeviceLinkError(f"connection error: {exc}") from exc
            finally:
                self._waiter = None

    async def _read_loop(self) -> None:
        """Read replies until the connection ends.

        Exactly one reply arrives per command, and commands are serialised, so
        each complete line is handed to the command currently waiting for it.
        """
        reason = "connection closed by the device service"
        try:
            reader = self._reader
            assert reader is not None
            while True:
                line = await reader.readuntil(b"\n")
                if not line:
                    break
                try:
                    response = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    # An unreadable reply cannot be matched to a command, so the
                    # link is treated as unusable rather than guessing.
                    reason = f"undecodable reply from the device service: {exc}"
                    logger.warning("%s", reason)
                    break
                waiter = self._waiter
                if waiter is not None and not waiter.done():
                    waiter.set_result(response)
                else:
                    logger.warning("dropping unsolicited reply from the device service")
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError) as exc:
            reason = f"connection lost: {exc}"
        finally:
            if self._reader_task is asyncio.current_task():
                self._reader_task = None
                writer, self._writer = self._writer, None
                self._reader = None
                waiter, self._waiter = self._waiter, None
                if waiter is not None and not waiter.done():
                    waiter.set_exception(DeviceLinkError(reason))
                self._last_error = reason
                logger.warning("device connection lost: %s", reason)
                # Report the loss before closing the socket: ``connected`` is
                # already ``False`` at this point, so anything watching the link
                # must not be able to see it go down without ``on_lost`` having
                # fired. Closing the writer below yields to the loop.
                if self._on_lost is not None:
                    self._on_lost(reason)
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(
                        ConnectionResetError, BrokenPipeError, OSError
                    ):
                        await writer.wait_closed()
