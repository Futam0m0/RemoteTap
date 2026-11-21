"""The Phase 2 safety layer.

Once a press starts, the device enforces two limits on its own, without asking
any client:

* a **lease**: the owning connection must keep renewing, or the press is
  released;
* a **hard maximum duration**: no single press may last longer than the
  configured ceiling, however diligently the client renews.

This is what makes the device — not the app — the authority on how long the
button may be held. The app is never trusted to release safely.
"""

from .watchdog import LEASE_TIMEOUT, MAX_PRESS_EXCEEDED, PressWatchdog

__all__ = ["PressWatchdog", "LEASE_TIMEOUT", "MAX_PRESS_EXCEEDED"]
