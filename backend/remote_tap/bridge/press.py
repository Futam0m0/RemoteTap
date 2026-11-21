"""The bridge's single press, and the lease renewals that keep it alive.

The page asks the bridge to hold the button down; the bridge is what actually
owns the press on the device. Exactly one press can exist at a time, and it
belongs to the one browser connection that started it.

Why the bridge — not the page — renews the lease
------------------------------------------------

A press on the device is a **lease**: the owning TCP connection must keep
renewing it or the device releases the button by itself. The page cannot renew
anything (it has no TCP socket) and, more importantly, must never be trusted to
keep a press safe. So the renewal timer lives here, next to the TCP connection
the device already measures the lease against. The page contributes nothing
safety-critical: if it stalls, crashes or is closed, this controller either stops
renewing or sends a stop, and the device's own lease and maximum-press limits are
unchanged and still in force.

What this class deliberately does **not** do
--------------------------------------------

* It does not decide how long a press may last, or when a lease has lapsed —
  those are the device's rules. It only obeys the ``renew_interval_ms`` the
  device advertises in its acknowledgement.
* It does not invent session ids for the device to trust blindly: a press always
  starts with a fresh ``PRESS_START`` the device validates itself.
* It does not retry a failed renewal with the *same* command id, which the
  device would treat as a duplicate and would not extend the lease. Every
  renewal is a new command, as the protocol requires.

Ownership mirrors the device's own model: a browser connection that did not
start the press cannot stop or renew it, and when the owning connection goes
away the bridge releases the press on its behalf.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from ..protocol.messages import CommandType
from .device_link import DeviceLink, DeviceLinkError

logger = logging.getLogger("remote_tap.bridge.press")

#: Actuator states the bridge reports. The device itself only ever has these
#: two; the page adds its own transient RELEASING/ERROR display on top.
IDLE = "IDLE"
PRESSING = "PRESSING"


@dataclass(frozen=True)
class PressOutcome:
    """The result of a press or stop request from the page."""

    ok: bool
    state: str
    code: Optional[str] = None
    message: str = ""


class PressController:
    """Owns the one press the bridge is holding and drives its lease renewals.

    Args:
        link: the TCP command link to the device service.
        on_change: called (without arguments) whenever the press state changes,
            so the bridge can push fresh state to the page.
    """

    def __init__(
        self,
        link: DeviceLink,
        *,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self._link = link
        self._on_change = on_change
        self._owner: Optional[str] = None
        self._session_id: Optional[str] = None
        self._state = IDLE
        self._renew_interval_ms: Optional[int] = None
        self._max_press_ms: Optional[int] = None
        self._renew_task: Optional[asyncio.Task[None]] = None
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """Whether the bridge is holding a press on the device."""
        return self._state == PRESSING

    @property
    def owner(self) -> Optional[str]:
        """The browser connection that owns the press, if any."""
        return self._owner

    @property
    def state(self) -> str:
        """``PRESSING`` while the bridge holds a press, ``IDLE`` otherwise."""
        return self._state

    @property
    def session_id(self) -> Optional[str]:
        """The press session id currently in use, if any."""
        return self._session_id

    @property
    def renew_interval_ms(self) -> Optional[int]:
        """How often the device asked the press to be renewed."""
        return self._renew_interval_ms

    @property
    def max_press_ms(self) -> Optional[int]:
        """The device's hard maximum press duration, as it advertised it."""
        return self._max_press_ms

    @property
    def last_error(self) -> Optional[str]:
        """The most recent rejection or failure, for display."""
        return self._last_error

    def summary(self) -> dict[str, object]:
        """A small snapshot of the press state for the page."""
        return {
            "active": self.active,
            "state": self._state,
            "session_id": self._session_id,
            "renew_interval_ms": self._renew_interval_ms,
            "max_press_ms": self._max_press_ms,
            "error": self._last_error,
        }

    # ------------------------------------------------------------------
    # Commands from the page
    # ------------------------------------------------------------------

    async def start(self, owner: str) -> PressOutcome:
        """Start (or re-assert) a press for ``owner``.

        Only one press exists at a time. A second browser that tries to press
        while another holds the button is refused here — before any command
        reaches the device — mirroring the device's own "already pressing under
        another connection" rule.
        """
        if self.active:
            if self._owner == owner:
                # A repeated pointer-down from the same connection: report the
                # press already in progress instead of restarting it, which
                # would reset the device's maximum-duration timer.
                return PressOutcome(ok=True, state=self._state)
            return PressOutcome(
                ok=False,
                state=self._state,
                code="BUSY",
                message="another browser is already holding the press",
            )

        if not self._link.connected:
            return self._outcome_error(
                "the bridge is not connected to the device service"
            )

        session_id = uuid.uuid4().hex
        try:
            response = await self._link.send(CommandType.PRESS_START, session_id)
        except DeviceLinkError as exc:
            return self._outcome_error(f"the device service is unreachable: {exc}")

        if response.get("type") != "ACK":
            return self._outcome_error(
                str(response.get("message") or "the device rejected the press"),
                code=response.get("code"),
                state=response.get("state") or IDLE,
            )

        self._owner = owner
        self._session_id = session_id
        self._state = PRESSING
        self._last_error = None
        self._renew_interval_ms = self._as_int(response.get("renew_interval_ms"))
        self._max_press_ms = self._as_int(response.get("max_press_ms"))
        logger.info(
            "press started (owner=%s, session=%s, renew=%sms, max=%sms)",
            owner,
            session_id,
            self._renew_interval_ms,
            self._max_press_ms,
        )
        self._start_renewing()
        self._changed()
        return PressOutcome(ok=True, state=self._state)

    async def stop(self, owner: str) -> PressOutcome:
        """End the press owned by ``owner``.

        A stop from a connection that does not own the press is refused; a stop
        when nothing is pressing is a harmless no-op (the page can safely send
        one after a release it did not observe).
        """
        if not self.active:
            return PressOutcome(ok=True, state=IDLE)
        if self._owner != owner:
            return PressOutcome(
                ok=False,
                state=self._state,
                code="NOT_OWNER",
                message="this connection does not own the press",
            )
        return await self._end_press(owner=owner)

    async def release_if_owner(self, owner: str, *, reason: str) -> bool:
        """Release the press only if ``owner`` owns it.

        Used when a browser connection disappears. Returns whether a press was
        actually ended.
        """
        if not self.active or self._owner != owner:
            return False
        logger.warning("releasing the press because its owner went away (%s)", reason)
        await self._end_press(owner=None, reason=reason)
        return True

    async def handle_link_lost(self, reason: str) -> None:
        """React to the device connection going away.

        The device has already released the actuator through
        ``release_if_client``; this only tidies the bridge's own view so the
        page stops showing a press the device no longer has.
        """
        if not self.active and self._owner is None:
            return
        logger.warning("press ended because the device link was lost (%s)", reason)
        await self._cancel_renewing()
        self._clear("the device connection was lost; it released the press")
        self._changed()

    async def shutdown(self) -> None:
        """Best-effort release of the press, then stop renewing.

        Called while the bridge is shutting down. If the stop cannot be sent,
        the device's own disconnect release covers it when the socket closes.
        """
        if self.active:
            with contextlib.suppress(DeviceLinkError):
                await self._link.send(CommandType.PRESS_STOP, self._session_id or "")
        await self._cancel_renewing()
        self._clear(None)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _end_press(
        self, *, owner: Optional[str], reason: Optional[str] = None
    ) -> PressOutcome:
        """Stop renewing and send ``PRESS_STOP`` for the current press."""
        await self._cancel_renewing()
        session_id = self._session_id or ""
        try:
            response = await self._link.send(CommandType.PRESS_STOP, session_id)
        except DeviceLinkError as exc:
            self._clear(f"the device connection was lost: {exc}")
            self._changed()
            return PressOutcome(
                ok=False, state=self._state, code="LINK_LOST", message=str(exc)
            )

        if response.get("type") != "ACK":
            # The device refused the stop — usually because a safety limit
            # already ended the press. Either way it is no longer ours.
            code = response.get("code")
            message = str(response.get("message") or "the press was already over")
            self._clear(message)
            self._changed()
            return PressOutcome(ok=False, state=IDLE, code=code, message=message)

        self._clear(reason)
        self._changed()
        return PressOutcome(ok=True, state=IDLE)

    def _start_renewing(self) -> None:
        """Begin renewing the lease at the interval the device advertised."""
        # The device always advertises the interval on a successful press, but
        # fall back to a conservative quarter of the lease if it ever did not.
        interval_ms = self._renew_interval_ms or 250
        self._renew_task = asyncio.ensure_future(self._renew_loop(interval_ms / 1000.0))

    async def _renew_loop(self, interval_s: float) -> None:
        """Send ``PRESS_RENEW`` until the press ends or the device refuses it.

        Every iteration sends a *new* command (fresh command id), as the
        protocol requires: re-sending an identical renewal would be treated as a
        retry and would not extend the lease.
        """
        try:
            while True:
                await asyncio.sleep(interval_s)
                session_id = self._session_id
                if session_id is None:
                    return
                try:
                    response = await self._link.send(
                        CommandType.PRESS_RENEW, session_id
                    )
                except DeviceLinkError as exc:
                    logger.warning("renewal failed: %s", exc)
                    self._clear(
                        "the device connection was lost; the press was released"
                    )
                    self._changed()
                    return

                if response.get("type") != "ACK":
                    message = str(
                        response.get("message") or "the device ended the press"
                    )
                    logger.warning("renewal rejected: %s (%s)", message, response.get("code"))
                    self._clear(message)
                    self._changed()
                    return
        except asyncio.CancelledError:
            raise
        finally:
            if self._renew_task is asyncio.current_task():
                self._renew_task = None

    async def _cancel_renewing(self) -> None:
        """Stop the renewal timer, if one is running."""
        task, self._renew_task = self._renew_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _clear(self, error: Optional[str]) -> None:
        """Forget the press; record ``error`` for display when given."""
        self._owner = None
        self._session_id = None
        self._state = IDLE
        self._renew_interval_ms = None
        self._max_press_ms = None
        self._last_error = error

    def _outcome_error(
        self, message: str, *, code: Optional[str] = None, state: str = IDLE
    ) -> PressOutcome:
        """Record and return a failed press request."""
        self._last_error = message
        logger.warning("press rejected: %s", message)
        self._changed()
        return PressOutcome(
            ok=False, state=state, code=code or "ERROR", message=message
        )

    def _changed(self) -> None:
        """Notify the bridge that the press state moved."""
        if self._on_change is not None:
            self._on_change()

    @staticmethod
    def _as_int(value: object) -> Optional[int]:
        """Return ``value`` when it is a real integer, else ``None``."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value
