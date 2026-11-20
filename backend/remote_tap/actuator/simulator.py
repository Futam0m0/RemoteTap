"""A simulated actuator: no GPIO, no servo, no hardware.

It keeps the state in memory and prints every transition to the terminal so the
whole command path can be observed while developing the networking layer.
"""

from __future__ import annotations

import logging

from .base import Actuator, ActuatorState

logger = logging.getLogger(__name__)


class SimulatedActuator(Actuator):
    """A stand-in for the real hardware actuator.

    Args:
        label: name used in the terminal output, so a log line can be traced
            back to the actuator that produced it.
    """

    def __init__(self, label: str = "sim") -> None:
        self._label = label
        self._state = ActuatorState.IDLE

    @property
    def state(self) -> ActuatorState:
        return self._state

    async def press(self) -> None:
        """Simulate pressing and holding the power button."""
        if self._state is ActuatorState.PRESSING:
            logger.debug("[%s] press() ignored: already pressing", self._label)
            return
        self._transition(ActuatorState.IDLE, ActuatorState.PRESSING)

    async def release(self) -> None:
        """Simulate letting go of the power button."""
        if self._state is ActuatorState.IDLE:
            logger.debug("[%s] release() ignored: already idle", self._label)
            return
        self._transition(ActuatorState.PRESSING, ActuatorState.IDLE)

    def _transition(self, old: ActuatorState, new: ActuatorState) -> None:
        """Apply a state change and print it to the terminal."""
        self._state = new
        print(f"[actuator:{self._label}] {old.value} -> {new.value}", flush=True)
        logger.info("[%s] actuator %s -> %s", self._label, old.value, new.value)
