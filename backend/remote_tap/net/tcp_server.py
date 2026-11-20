"""Asyncio TCP server implementing the RemoteTap command channel.

Responsibilities, and nothing else:

* accept connections and read bytes from the socket,
* split that byte stream into newline-delimited messages,
* validate each message, then hand it to the device session,
* write JSON responses back on the same connection,
* release the actuator if a client that was pressing disappears.

The server does not decide what a command means; that is the session's job. It
does not know what an actuator is; that is the actuator's job.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from typing import Optional

from ..config import READ_CHUNK_BYTES, ServerConfig
from ..protocol.messages import ErrorCode, ErrorMessage, Response, encode_response
from ..protocol.validate import ProtocolError, parse_command
from ..session import DeviceSession

logger = logging.getLogger(__name__)


@dataclass
class ClientContext:
    """Per-connection bookkeeping.

    Nothing here is shared between connections: it exists so that a disconnect
    can be handled, and logged, without touching the shared session state.
    """

    client_id: str
    peer: str
    owned_session_id: str | None = None


class TcpCommandServer:
    """An asyncio TCP server for the command channel.

    Args:
        session: the device session that applies commands to the actuator.
        config: host, port and message size limits.
        name: label used in log output.
    """

    def __init__(
        self,
        session: DeviceSession,
        config: ServerConfig,
        name: str = "remotetap",
    ) -> None:
        self._session = session
        self._config = config
        self._name = name
        self._server: Optional[asyncio.AbstractServer] = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._client_counter = itertools.count(1)

    @property
    def port(self) -> int | None:
        """The port actually bound.

        Differs from the configured port when ``0`` was requested, which is how
        the tests get a free ephemeral port.
        """
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    @property
    def is_running(self) -> bool:
        """Whether the listening socket is open."""
        return self._server is not None

    async def start(self) -> None:
        """Bind the listening socket and start accepting connections."""
        self._server = await asyncio.start_server(
            self._on_client, self._config.host, self._config.port
        )
        logger.info(
            "[%s] listening on %s:%s", self._name, self._config.host, self.port
        )

    async def serve_forever(self) -> None:
        """Serve until the server is stopped or the task is cancelled."""
        if self._server is None:
            raise RuntimeError("start() must be awaited before serve_forever()")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop accepting connections and close every open client socket."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        writers = list(self._writers)
        for writer in writers:
            writer.close()
        for writer in writers:
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
        self._writers.clear()
        logger.info("[%s] stopped", self._name)

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one client connection from accept to disconnect."""
        context = ClientContext(
            client_id=f"c{next(self._client_counter)}", peer=self._peer_name(writer)
        )
        self._writers.add(writer)
        logger.info("[%s] connected from %s", context.client_id, context.peer)

        try:
            await self._read_loop(reader, writer, context)
        except (ConnectionResetError, ConnectionAbortedError) as exc:
            logger.info("[%s] connection lost: %s", context.client_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive, keeps other clients alive
            logger.exception("[%s] unexpected error", context.client_id)
        finally:
            await self._release_owned_session(context)
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            logger.info("[%s] disconnected", context.client_id)

    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        context: ClientContext,
    ) -> None:
        """Read bytes and dispatch every complete newline-delimited message.

        A single read call may return half a message, several messages, or a
        message split across reads, so incoming bytes are buffered and only
        complete lines are dispatched.
        """
        buffer = bytearray()
        while True:
            chunk = await reader.read(READ_CHUNK_BYTES)
            if not chunk:
                return  # the client closed the connection

            buffer.extend(chunk)

            while True:
                newline = buffer.find(b"\n")
                if newline == -1:
                    if len(buffer) > self._config.max_line_bytes:
                        await self._reject_oversized_line(writer)
                        return
                    break

                line = bytes(buffer[:newline])
                del buffer[: newline + 1]

                if not line.strip():
                    continue  # tolerate empty keep-alive lines

                if len(line) > self._config.max_line_bytes:
                    await self._reject_oversized_line(writer)
                    return

                await self._handle_line(line, context, writer)

    async def _handle_line(
        self, line: bytes, context: ClientContext, writer: asyncio.StreamWriter
    ) -> None:
        """Validate one line, apply it, and write the response."""
        try:
            command = parse_command(line)
        except ProtocolError as exc:
            logger.warning(
                "[%s] rejected message: %s (%s)", context.client_id, exc.message, exc.code.value
            )
            await self._send_error(writer, exc.code, exc.message, exc.command_id)
            return

        try:
            response = await self._session.handle(command)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "[%s] failed to handle %s", context.client_id, command.type.value
            )
            await self._send_error(
                writer,
                ErrorCode.INTERNAL,
                "internal server error",
                command.command_id,
            )
            return

        # Remember which session this connection is responsible for, so that a
        # disconnect only releases the hold it actually owns.
        active = self._session.active_session_id
        context.owned_session_id = (
            command.session_id if active == command.session_id else None
        )

        logger.info(
            "[%s] %s command_id=%s session_id=%s -> %s",
            context.client_id,
            command.type.value,
            command.command_id,
            command.session_id,
            response.to_dict().get("type"),
        )
        await self._send(writer, response)

    async def _release_owned_session(self, context: ClientContext) -> None:
        """Release the actuator if this connection owned the active hold."""
        if context.owned_session_id is None:
            return
        released = await self._session.release_if_session(
            context.owned_session_id, reason="client_disconnected"
        )
        if released:
            logger.warning(
                "[%s] released the actuator because the client disconnected",
                context.client_id,
            )
        context.owned_session_id = None

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def _send(self, writer: asyncio.StreamWriter, response: Response) -> None:
        """Write one response line to the client."""
        writer.write(encode_response(response))
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            logger.info("client went away before the response could be flushed")

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        code: ErrorCode,
        message: str,
        command_id: str | None,
    ) -> None:
        """Report a rejection, including the current state when it is safe to."""
        error = ErrorMessage(
            code=code,
            message=message,
            command_id=command_id,
            state=self._session.state.value,
        )
        await self._send(writer, error)

    async def _reject_oversized_line(self, writer: asyncio.StreamWriter) -> None:
        """Reject a line that exceeded the configured maximum, then disconnect.

        Closing is deliberate: the remaining bytes in the stream cannot be
        trusted to be a message boundary any more.
        """
        logger.warning("rejecting oversized message (> %s bytes)", self._config.max_line_bytes)
        await self._send_error(
            writer,
            ErrorCode.LINE_TOO_LONG,
            f"message exceeds {self._config.max_line_bytes} bytes",
            None,
        )

    @staticmethod
    def _peer_name(writer: asyncio.StreamWriter) -> str:
        """Describe the remote endpoint for logging."""
        peer = writer.get_extra_info("peername")
        if isinstance(peer, tuple) and len(peer) >= 2:
            return f"{peer[0]}:{peer[1]}"
        return str(peer)
