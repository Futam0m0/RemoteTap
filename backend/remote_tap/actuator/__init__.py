"""The actuator interface and its simulated implementation."""

from .base import Actuator, ActuatorState
from .simulator import SimulatedActuator

__all__ = ["Actuator", "ActuatorState", "SimulatedActuator"]
