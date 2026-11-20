"""The actuator abstraction.

Everything above this layer talks to an :class:`Actuator`; it never knows
whether the actuator is a simulator or a Raspberry Pi. That is what allows the
simulator to be replaced later without touching the protocol, session or
networking code.
"""

from __future__ import annotations

import abc
from enum import Enum


class ActuatorState(str, Enum):
    """The only two states a power-button actuator needs."""

    IDLE = "IDLE"
    PRESSING = "PRESSING"


class Actuator(abc.ABC):
    """Something that can hold a physical button down."""

    @property
    @abc.abstractmethod
    def state(self) -> ActuatorState:
        """The actuator's current state."""

    @abc.abstractmethod
    async def press(self) -> None:
        """Move to :attr:`ActuatorState.PRESSING`.

        Idempotent: pressing while already pressing does nothing.
        """

    @abc.abstractmethod
    async def release(self) -> None:
        """Return to :attr:`ActuatorState.IDLE`.

        Idempotent: releasing while already idle does nothing.
        """

    @property
    def is_pressing(self) -> bool:
        """Whether the button is currently being held down."""
        return self.state is ActuatorState.PRESSING
