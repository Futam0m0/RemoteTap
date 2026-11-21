"""Device-side command handling.

This layer owns the device's view of the world:

* which actuator state the device is in,
* which session owns the actuator, and which connection owns that session,
* which commands have already been handled.

It knows nothing about sockets: a ``client_id`` is an opaque token supplied by
the caller, which is what keeps this layer testable without any networking.

Ownership model
---------------

* A press session belongs to exactly one connection: the connection that created
  it is its exclusive owner.
* Only the owner may stop the press, and only the owner may renew its lease.
* A different connection cannot control an existing session, even if it knows
  the session id.
* When the owning connection disappears the actuator is released, so the next
  connection has to start a new session.

Lease model
-----------

A press is a *lease*, enforced by the device rather than trusted to the client.
Once the actuator is pressing, a :class:`~remote_tap.safety.PressWatchdog` is
armed and the owning connection must keep the press alive by renewing it with
``PRESS_RENEW``. Two things can end a press without the client asking:

* the lease expires because the client went quiet, or
* the hard maximum press duration is reached, however diligently the client
  renews.

In both cases the device releases the actuator itself: a phone that has frozen
must never be able to hold the button down indefinitely.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

from .actuator.base import Actuator, ActuatorState
from .config import DEFAULT_PRESS_LIMITS, MAX_DUPLICATE_COMMAND_IDS, PressLimits
from .protocol.messages import Ack, Command, CommandType, ErrorCode, ErrorMessage, Response
from .safety import LEASE_TIMEOUT, MAX_PRESS_EXCEEDED, PressWatchdog

logger = logging.getLogger(__name__)

#: Why the device released a press on its own, in words a client can read.
_EXPIRY_MESSAGES = {
    LEASE_TIMEOUT: "the press was released because the lease was not renewed in time",
    MAX_PRESS_EXCEEDED: "the press was released because it reached the maximum duration",
}


@dataclass(frozen=True)
class _CachedResponse:
    """A response kept so a retry can be answered without acting again.

    The connection that produced it is recorded, because a response may only be
    replayed to the connection that asked for it.
    """

    client_id: str
    response: Response


class DeviceSession:
    """Applies validated commands to the actuator.

    Args:
        actuator: the actuator to drive.
        duplicate_cache_size: how many recent commands to remember so that a
            retried command returns its original response instead of being
            executed a second time.
        limits: the press-safety limits (renewal interval, lease timeout,
            maximum press duration) the device enforces.
    """

    def __init__(
        self,
        actuator: Actuator,
        duplicate_cache_size: int = MAX_DUPLICATE_COMMAND_IDS,
        limits: PressLimits = DEFAULT_PRESS_LIMITS,
    ) -> None:
        self._actuator = actuator
        self._duplicate_cache_size = duplicate_cache_size
        self._limits = limits
        self._active_session_id: str | None = None
        self._owner_client_id: str | None = None
        # Set when the device ended a press itself, so the owner's late renewal
        # or stop can be told why its press is gone instead of a bare "no press".
        self._expired_session_id: str | None = None
        self._expired_owner_client_id: str | None = None
        self._expired_reason: str | None = None
        # Maps (session_id, command_id) -> cached response. Ordered so the
        # oldest entry can be evicted once the cache is full.
        self._responses: OrderedDict[tuple[str, str], _CachedResponse] = OrderedDict()
        self._watchdog = PressWatchdog(self._on_press_expired, limits)

    @property
    def state(self) -> ActuatorState:
        """Current actuator state."""
        return self._actuator.state

    @property
    def press_limits(self) -> PressLimits:
        """The press-safety limits this device enforces."""
        return self._limits

    @property
    def active_session_id(self) -> str | None:
        """The session that currently owns the actuator, if any."""
        return self._active_session_id

    @property
    def owner_client_id(self) -> str | None:
        """The connection that owns the active session, if any."""
        return self._owner_client_id

    @property
    def watchdog_active(self) -> bool:
        """Whether a press is currently being timed by the safety watchdog."""
        return self._watchdog.active

    @property
    def lease_deadline(self) -> float:
        """Monotonic time at which the active press's lease expires, or ``0.0``.

        Read-only, and only meaningful while :attr:`watchdog_active` is true.
        """
        return self._watchdog.lease_deadline

    async def handle(self, command: Command, client_id: str) -> Response:
        """Apply one validated command from ``client_id`` and return the reply.

        Retries are safe: a ``(session_id, command_id)`` pair that this same
        connection has already sent is answered from the cache, and the actuator
        is not touched again.

        A cached response is replayed only while it still describes the device as
        it is *now*. Once a press has ended — a safety limit released it, or the
        owning connection went away — the cached ``PRESSING`` reply is no longer
        the truth, and a cached reply from an old session must never be used to
        describe a newer one. A stale response is therefore replaced by a
        current-state error, and the command is **not** executed again: retrying
        an old ``command_id`` can therefore never start a second press.
        """
        key = (command.session_id, command.command_id)
        cached = self._responses.get(key)
        if cached is not None and cached.client_id == client_id:
            if self._cached_response_is_current(command, client_id, cached.response):
                logger.info(
                    "replaying response for duplicate command session_id=%s command_id=%s",
                    command.session_id,
                    command.command_id,
                )
                return cached.response
            logger.info(
                "not replaying stale response for duplicate command session_id=%s "
                "command_id=%s",
                command.session_id,
                command.command_id,
            )
            return self._stale_duplicate_error(command, client_id)

        response = await self._execute(command, client_id)
        self._remember(key, client_id, response)
        return response

    def _cached_response_is_current(
        self, command: Command, client_id: str, response: Response
    ) -> bool:
        """Whether a cached response still describes the device as it is now.

        Only the ``state`` a reply advertises can go out of date, and comparing
        it with the actuator state alone is not enough: a *different* session may
        be pressing now, and that press must not be reported as this session's.
        The check is therefore per-session:

        * a reply claiming ``PRESSING`` is current only while this very session,
          owned by this very connection, is the one holding the actuator;
        * a reply claiming ``IDLE`` is current only while nothing is pressing —
          once someone else holds the button, answering ``IDLE`` would misstate
          the device.
        """
        claimed = response.state
        if claimed == ActuatorState.PRESSING.value:
            return (
                self._actuator.state is ActuatorState.PRESSING
                and self._active_session_id == command.session_id
                and self._owner_client_id == client_id
            )
        if claimed == ActuatorState.IDLE.value:
            return self._actuator.state is not ActuatorState.PRESSING
        return True

    def _stale_duplicate_error(self, command: Command, client_id: str) -> ErrorMessage:
        """Answer a retried command whose cached response is no longer true.

        The command is deliberately **not** executed: a retried ``PRESS_START``
        whose original press already ended must never press the actuator again.
        The reply is what a fresh command would be told about the current state —
        ``LEASE_EXPIRED`` for the connection whose own press the device ended,
        and a plain ``INVALID_SESSION`` otherwise, which reveals nothing about
        another connection's session or why its press finished.
        """
        return self._no_press_error(
            command,
            client_id,
            "the press this command refers to is no longer active",
        )

    async def release(self, reason: str = "release") -> bool:
        """Release the actuator unconditionally.

        Used when the whole service stops, and by the safety watchdog, which
        must be able to release without asking any client. The lease timer is
        stopped first, so a released press can never be expired afterwards.

        Returns:
            ``True`` if a hold was released, ``False`` otherwise.
        """
        await self._watchdog.disarm()
        if self._actuator.state is not ActuatorState.PRESSING:
            return False
        session_id = self._active_session_id
        await self._actuator.release()
        await self._clear_session()
        logger.warning("released actuator (session=%s, reason=%s)", session_id, reason)
        return True

    async def release_if_client(self, client_id: str, reason: str = "release") -> bool:
        """Release the actuator if ``client_id`` owns the active session.

        Used when a connection disappears: without this the simulator would stay
        ``PRESSING`` forever after its owner is gone. A connection that does not
        own the session cannot release it.

        Returns:
            ``True`` if a hold was released, ``False`` otherwise.
        """
        if self._actuator.state is not ActuatorState.PRESSING:
            return False
        if self._owner_client_id != client_id:
            return False
        return await self.release(reason)

    async def _execute(self, command: Command, client_id: str) -> Response:
        """Run a command that has not been seen from this connection before."""
        if command.type is CommandType.PRESS_START:
            return await self._press_start(command, client_id)
        if command.type is CommandType.PRESS_RENEW:
            return await self._press_renew(command, client_id)
        return await self._press_stop(command, client_id)

    async def _press_start(self, command: Command, client_id: str) -> Response:
        """Start (or re-assert) a hold owned by ``client_id``."""
        if self._actuator.state is ActuatorState.PRESSING:
            if self._owner_client_id != client_id:
                # Do not name the session: a stranger does not get to learn it.
                return ErrorMessage(
                    code=ErrorCode.INVALID_SESSION,
                    message="the actuator is already pressing under another connection",
                    command_id=command.command_id,
                    state=self._actuator.state.value,
                )
            if self._active_session_id != command.session_id:
                return ErrorMessage(
                    code=ErrorCode.INVALID_SESSION,
                    message=(
                        "this connection already owns an active press under a "
                        "different session id"
                    ),
                    command_id=command.command_id,
                    state=self._actuator.state.value,
                )
            # The owner asking again with the same session: answer with the
            # current state, do not actuate twice. Deliberately *not* treated as
            # a renewal and deliberately not re-armed: re-arming would restart
            # the maximum-duration timer and let a client hold forever by
            # re-sending PRESS_START instead of renewing the lease.
            logger.debug("PRESS_START for the owned session is a no-op")
            return self._lease_ack(command.command_id)

        await self._actuator.press()
        self._active_session_id = command.session_id
        self._owner_client_id = client_id
        self._forget_expired()
        await self._watchdog.arm()
        logger.info(
            "PRESS_START accepted (session=%s, owner=%s, lease=%sms, max=%sms)",
            command.session_id,
            client_id,
            self._limits.lease_timeout_ms,
            self._limits.max_press_ms,
        )
        return self._lease_ack(command.command_id)

    async def _press_renew(self, command: Command, client_id: str) -> Response:
        """Push the lease deadline of the press owned by ``client_id`` forward.

        A renewal is only honoured for the connection that owns the press and
        for the session id that press was started under. It extends the lease;
        it can never extend the maximum press duration.
        """
        if self._actuator.state is not ActuatorState.PRESSING:
            return self._no_press_error(
                command, client_id, "there is no active press to renew"
            )
        if self._owner_client_id != client_id:
            return ErrorMessage(
                code=ErrorCode.INVALID_SESSION,
                message="this connection does not own the active press",
                command_id=command.command_id,
                state=self._actuator.state.value,
            )
        if self._active_session_id != command.session_id:
            return ErrorMessage(
                code=ErrorCode.INVALID_SESSION,
                message="session id does not match the active press",
                command_id=command.command_id,
                state=self._actuator.state.value,
            )

        self._watchdog.renew()
        logger.debug("PRESS_RENEW accepted (session=%s)", command.session_id)
        return self._lease_ack(command.command_id)

    async def _press_stop(self, command: Command, client_id: str) -> Response:
        """End the hold, if this connection owns it."""
        if self._actuator.state is not ActuatorState.PRESSING:
            return self._no_press_error(
                command, client_id, "there is no active press to stop"
            )
        if self._owner_client_id != client_id:
            return ErrorMessage(
                code=ErrorCode.INVALID_SESSION,
                message="this connection does not own the active press",
                command_id=command.command_id,
                state=self._actuator.state.value,
            )
        if self._active_session_id != command.session_id:
            return ErrorMessage(
                code=ErrorCode.INVALID_SESSION,
                message="session id does not match the active press",
                command_id=command.command_id,
                state=self._actuator.state.value,
            )

        session_id = self._active_session_id
        await self._actuator.release()
        await self._clear_session()
        logger.info("PRESS_STOP accepted (session=%s)", session_id)
        return Ack(command_id=command.command_id, state=self._actuator.state.value)

    async def _on_press_expired(self, reason: str) -> None:
        """Release the actuator because the watchdog reached a limit.

        Called by :class:`~remote_tap.safety.PressWatchdog` with ``lease_timeout``
        or ``max_press_exceeded``. The reason is remembered so that the owner's
        next ``PRESS_RENEW`` or ``PRESS_STOP`` is answered with ``LEASE_EXPIRED``
        rather than a misleading "there is no active press".
        """
        session_id = self._active_session_id
        owner_client_id = self._owner_client_id
        if self._actuator.state is ActuatorState.PRESSING:
            await self._actuator.release()
        await self._clear_session()
        self._expired_session_id = session_id
        self._expired_owner_client_id = owner_client_id
        self._expired_reason = _EXPIRY_MESSAGES.get(reason, reason)
        logger.warning(
            "released actuator (session=%s, reason=%s)", session_id, reason
        )

    def _lease_ack(self, command_id: str) -> Ack:
        """Acknowledge a press and advertise the lease policy it runs under.

        The client is told how often to renew and how long a press can last at
        most, so it never has to guess the device's timings.
        """
        return Ack(
            command_id=command_id,
            state=self._actuator.state.value,
            renew_interval_ms=self._limits.renew_interval_ms,
            max_press_ms=self._limits.max_press_ms,
        )

    def _no_press_error(
        self, command: Command, client_id: str, message: str
    ) -> ErrorMessage:
        """Explain that there is no press to act on.

        If this connection's own press was ended by a safety limit, say so:
        otherwise the client cannot tell "you were too slow" from "you are not
        the owner". The reason is only given to the connection that owned the
        ended press.
        """
        if (
            self._expired_session_id is not None
            and command.session_id == self._expired_session_id
            and client_id == self._expired_owner_client_id
        ):
            return ErrorMessage(
                code=ErrorCode.LEASE_EXPIRED,
                message=self._expired_reason or "the press already ended",
                command_id=command.command_id,
                state=self._actuator.state.value,
            )
        return ErrorMessage(
            code=ErrorCode.INVALID_SESSION,
            message=message,
            command_id=command.command_id,
            state=self._actuator.state.value,
        )

    async def _clear_session(self) -> None:
        """Forget the active session, its owner, and stop timing its lease."""
        self._active_session_id = None
        self._owner_client_id = None
        await self._watchdog.disarm()

    def _forget_expired(self) -> None:
        """Drop the record of a press the device ended by itself."""
        self._expired_session_id = None
        self._expired_owner_client_id = None
        self._expired_reason = None

    def _remember(self, key: tuple[str, str], client_id: str, response: Response) -> None:
        """Cache a response so a retry from the same connection is idempotent.

        The key is ``(session_id, command_id)``. If that key is already held for
        a different connection, the new entry replaces it: session ids belong to
        a single connection, so the older entry can no longer be retried by
        anyone who is allowed to act.
        """
        self._responses[key] = _CachedResponse(client_id=client_id, response=response)
        self._responses.move_to_end(key)
        while len(self._responses) > self._duplicate_cache_size:
            self._responses.popitem(last=False)
