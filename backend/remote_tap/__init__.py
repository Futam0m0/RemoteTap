"""RemoteTap device service.

Phase 1: a simulated actuator driven over a TCP command channel.

Planned layers (see docs/architecture.md):

* ``protocol``  - message definitions and validation (no sockets, no hardware)
* ``net``       - asyncio TCP server
* ``session``   - device state and command handling
* ``actuator``  - actuator interface plus the simulated implementation
"""

__version__ = "0.1.0"
