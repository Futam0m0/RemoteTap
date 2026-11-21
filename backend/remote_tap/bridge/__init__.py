"""Local web bridge for the browser demonstration interface.

A browser cannot open a raw TCP or UDP socket, so this package sits between the
dashboard page and the existing device service:

* :mod:`~remote_tap.bridge.device_link` speaks the **existing TCP command
  protocol** as a client, so the fleet keeps one authority for sessions, the
  lease and the watchdog;
* :mod:`~remote_tap.bridge.press` owns the single press the bridge is holding
  and drives the lease renewals;
* :mod:`~remote_tap.bridge.status_hub` is the **sole** UDP listener for the
  heartbeat channel and reuses the existing
  :class:`~remote_tap.monitor.StatusMonitor`, so there is no second competing
  receiver and no duplicated ordering logic;
* :mod:`~remote_tap.bridge.app` exposes both to the page over localhost HTTP and
  WebSocket.

Nothing here changes the TCP or UDP protocols, the watchdog or the session
manager. The bridge is a client of them, not a replacement.
"""

from .app import BridgeConfig, create_app

__all__ = ["BridgeConfig", "create_app"]
