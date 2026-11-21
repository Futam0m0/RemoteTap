"""Lease and maximum-duration enforcement for an active press.

The watchdog owns two deadlines that are set when a press starts:

* the **lease deadline**, which the client pushes forward by renewing, and
* the **maximum deadline**, which is fixed and can never be pushed.

A background task sleeps until the nearer deadline. If the lease deadline is
reached first the press is released because the client went quiet
(:data:`LEASE_TIMEOUT`); if the maximum deadline is reached first the press is
released because it has simply lasted too long (:data:`MAX_PRESS_EXCEEDED`).
Either way the callback is invoked and the task ends.

Nothing here knows about sockets, sessions or the protocol: it takes an async
callback and a clock. That keeps it easy to test with very short timings.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Awaitable, Callable, Optional

from ..config import PressLimits

logger = logging.getLogger(__name__)

#: The client stopped renewing the lease in time.
LEASE_TIMEOUT = "lease_timeout"

#: The press reached the hard maximum duration.
MAX_PRESS_EXCEEDED = "max_press_exceeded"


class PressWatchdog:
    """Enforces the lease and the maximum duration of one press.

    Args:
        on_expire: awaited with the release reason when a limit is reached. It
            must release the actuator; the watchdog does not touch hardware.
        limits: the lease renewal interval, lease timeout and maximum duration.
        clock: monotonic clock, injectable so tests can control time.
    """

    def __init__(
        self,
        on_expire: Callable[[str], Awaitable[None]],
        limits: PressLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_expire = on_expire
        self._limits = limits
        self._clock = clock
        self._task: Optional[asyncio.Task[None]] = None
        self._lease_deadline = 0.0
        self._max_deadline = 0.0

    @property
    def limits(self) -> PressLimits:
        """The limits this watchdog enforces."""
        return self._limits

    @property
    def active(self) -> bool:
        """Whether a press is currently being timed."""
        return self._task is not None

    @property
    def lease_deadline(self) -> float:
        """Monotonic time the lease expires, or ``0.0`` when never armed.

        Read-only. The value is only meaningful while :attr:`active` is true;
        it is left in place after :meth:`disarm` rather than reset.
        """
        return self._lease_deadline

    async def arm(self) -> None:
        """Start timing a new press, replacing any previous timers."""
        await self.disarm()
        now = self._clock()
        self._lease_deadline = now + self._limits.lease_timeout_ms / 1000.0
        self._max_deadline = now + self._limits.max_press_ms / 1000.0
        self._task = asyncio.ensure_future(self._run())
        logger.debug(
            "press watchdog armed (lease %.3fs, max %.3fs)",
            self._limits.lease_timeout_ms / 1000.0,
            self._limits.max_press_ms / 1000.0,
        )

    def renew(self) -> None:
        """Push the lease deadline forward.

        A no-op when no press is being timed, so a stray renewal cannot create a
        lease out of nothing. The running task re-reads the deadline each loop,
        so it never has to be restarted.
        """
        if self._task is None:
            return
        self._lease_deadline = self._clock() + self._limits.lease_timeout_ms / 1000.0

    async def disarm(self) -> None:
        """Stop timing. Safe to call when nothing is armed, or from the task."""
        task, self._task = self._task, None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        """Sleep until the nearest deadline, then expire; re-check on each wake."""
        try:
            while True:
                now = self._clock()
                if now >= self._max_deadline:
                    await self._expire(MAX_PRESS_EXCEEDED)
                    return
                if now >= self._lease_deadline:
                    await self._expire(LEASE_TIMEOUT)
                    return
                delay = min(self._max_deadline, self._lease_deadline) - now
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("press watchdog failed; the press may not be released")
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    async def _expire(self, reason: str) -> None:
        """Hand the release reason to the owner of the watchdog."""
        logger.warning("press limit reached (%s): releasing", reason)
        await self._on_expire(reason)
