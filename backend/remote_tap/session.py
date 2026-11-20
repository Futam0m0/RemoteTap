"""Device-side command handling.

This layer owns the device's view of the world:

* which actuator state the device is in,
* which session currently owns the actuator,
* which commands have already been handled.

It knows nothing about sockets, so it can be tested without any networking.

What it deliberately does **not** do yet: hold time limits, heartbeat leases and
watchdog releases. Those belong to the safety layer, which is a later phase.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from .actuator.base import Actuator, ActuatorState
from .config import MAX_DUPLICATE_COMMAND_IDS
from .protocol.messages import Ack, Command, CommandType, ErrorCode, ErrorMessage, Response

logger = logging.getLogger(__name__)


class DeviceSession:
    """Applies validated commands to the actuator.

    Args:
        actuator: the actuator to drive.
        duplicate_cache_size: how many recent commands to remember so that a
            retried command returns its original response instead of being
            executed a second time.
    """

    def __init__(
        self,
        actuator: Actuator,
        duplicate_cache_size: int = MAX_DUPLICATE_COMMAND_IDS,
    ) -> None:
        self._actuator = actuator
        self._duplicate_cache_size = duplicate_cache_size
        self._active_session_id: str | None = None
        # Maps (session_id, command_id) -> the response that command produced.
        # Scoping by session matters: two clients with different sessions may
        # legitimately pick the same command id, and neither may receive the
        # other's response. Ordered so the oldest entry can be evicted.
        self._responses: OrderedDict[tuple[str, str], Response] = OrderedDict()

    @property
    def state(self) -> ActuatorState:
        """Current actuator state."""
        return self._actuator.state

    @property
    def active_session_id(self) -> str | None:
        """The session that currently owns the actuator, if any."""
        return self._active_session_id

    async def handle(self, command: Command) -> Response:
        """Apply one validated command and return the response to send back.

        Retries are safe: a ``(session_id, command_id)`` pair that has already
        been handled returns the stored response and the actuator is not touched
        again.
        """
        key = (command.session_id, command.command_id)
        cached = self._responses.get(key)
        if cached is not None:
            logger.info(
                "replaying response for duplicate command session_id=%s command_id=%s",
                command.session_id,
                command.command_id,
            )
            return cached

        response = await self._execute(command)
        self._remember(key, response)
        return response

    async def release_if_session(self, session_id: str, reason: str = "release") -> bool:
        """Release the actuator if ``session_id`` currently owns it.

        Used when a client disappears: without this the simulator would stay
        ``PRESSING`` forever after the client is gone.

        Returns:
            ``True`` if a hold was released, ``False`` otherwise.
        """
        if self._actuator.state is not ActuatorState.PRESSING:
            return False
        if self._active_session_id != session_id:
            return False
        await self._actuator.release()
        self._active_session_id = None
        logger.warning("released actuator (session=%s, reason=%s)", session_id, reason)
        return True

    async def _execute(self, command: Command) -> Response:
        """Run a command that has not been seen before."""
        if command.type is CommandType.PRESS_START:
            return await self._press_start(command)
        return await self._press_stop(command)

    async def _press_start(self, command: Command) -> Response:
        """Start (or re-assert) a hold for this session."""
        if self._actuator.state is ActuatorState.PRESSING:
            if self._active_session_id != command.session_id:
                return ErrorMessage(
                    code=ErrorCode.INVALID_SESSION,
                    message=(
                        f"actuator is already pressing under session "
                        f"{self._active_session_id!r}"
                    ),
                    command_id=command.command_id,
                    state=self._actuator.state.value,
                )
            # Same session asking again: answer with the current state, do not
            # actuate twice.
            logger.debug("PRESS_START for the active session is a no-op")
            return Ack(command_id=command.command_id, state=self._actuator.state.value)

        await self._actuator.press()
        self._active_session_id = command.session_id
        logger.info("PRESS_START accepted (session=%s)", command.session_id)
        return Ack(command_id=command.command_id, state=self._actuator.state.value)

    async def _press_stop(self, command: Command) -> Response:
        """End the hold owned by this session."""
        if self._actuator.state is not ActuatorState.PRESSING:
            return ErrorMessage(
                code=ErrorCode.INVALID_SESSION,
                message="there is no active press to stop",
                command_id=command.command_id,
                state=self._actuator.state.value,
            )
        if self._active_session_id != command.session_id:
            return ErrorMessage(
                code=ErrorCode.INVALID_SESSION,
                message=(
                    f"session {command.session_id!r} does not own the active press "
                    f"({self._active_session_id!r})"
                ),
                command_id=command.command_id,
                state=self._actuator.state.value,
            )

        await self._actuator.release()
        self._active_session_id = None
        logger.info("PRESS_STOP accepted (session=%s)", command.session_id)
        return Ack(command_id=command.command_id, state=self._actuator.state.value)

    def _remember(self, key: tuple[str, str], response: Response) -> None:
        """Cache a response so a retry of the same command is idempotent."""
        self._responses[key] = response
        self._responses.move_to_end(key)
        while len(self._responses) > self._duplicate_cache_size:
            self._responses.popitem(last=False)
