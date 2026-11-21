# RemoteTap — Architecture

> Status: **Phase 3 implemented**. This document separates what exists today
> from what is planned.
>
> * **Implemented (Phase 1)**: the asyncio TCP command server, JSON Lines
>   framing, the `PRESS_START` / `PRESS_STOP` / `ACK` / `ERROR` protocol, session
>   ownership, the simulated actuator, and the pytest suite.
> * **Implemented (Phase 2 safety)**: the press lease (`PRESS_RENEW`), the lease
>   timeout, the hard maximum press duration and the watchdog that releases the
>   actuator when either deadline is reached. All three values are configurable
>   in one place.
> * **Implemented (Phase 3 status)**: the UDP heartbeat / status channel — a
>   device-to-monitor, read-only status stream with sequence numbers, a
>   per-run **boot id**, duplicate and reordering handling, restart detection,
>   and staleness detection in the monitoring client. It cannot influence the
>   actuator, the lease or the watchdog.
> * **Implemented (browser demo)**: a local, loopback-only **web bridge**
>   (`remote_tap.bridge`) that serves a dashboard over HTTP, streams state over
>   WebSocket, translates the page's press events into the *existing* TCP command
>   protocol and forwards the *existing* UDP status. It is a client of the
>   device, adds no new safety mechanism, and exists only to demonstrate the
>   interaction until the Expo app replaces it (section 4.3).
> * **Planned (authentication)**: the handshake and shared-secret auth —
>   **not implemented**. Neither channel is authenticated today.
> * **Future (hardware)**: Raspberry Pi GPIO / servo integration — **not
>   implemented**; the service drives a simulator.
>
> Anything marked **[TBD]** is an open question awaiting a decision; see the
> last section.

## 1. Purpose and scope

RemoteTap presses a desktop PC's power button from a distance: the user holds a
button in a mobile app, and a separate device physically holds the PC's power
button down for as long as the user holds theirs.

The desktop PC is never required to be running. The device is powered and on the
network on its own, and it acts on the PC externally.

This document covers the software architecture for the prototype phase, in which
the actuator is **simulated** (no GPIO or servo is driven yet).

Out of scope for this phase: GPIO, servo control, mechanical design,
the mobile UI beyond what is needed to drive the protocol.

## 2. System overview

```
   ┌──────────────────────┐            ┌───────────────────────────┐
   │  Mobile app          │            │  Device                   │
   │  Expo / RN / TS      │            │  Raspberry Pi Zero 2 W    │
   │                      │            │  Linux + Python 3         │
   │  ┌────────────────┐  │   TCP      │  ┌─────────────────────┐  │
   │  │ Hold button UI │  │  commands  │  │ TCP command server  │  │
   │  │  (press/hold)  │──┼───────────>│  └──────────┬──────────┘  │
   │  └────────────────┘  │            │             │             │
   │                      │   UDP      │  ┌──────────▼──────────┐  │
   │                      │  status    │  │ Session + safety    │  │
   │  ┌────────────────┐  │<───────────┼──│ (sole authority)    │  │
   │  │ Status monitor │  │ one-way    │  └──────────┬──────────┘  │
   │  │  (dev tool)    │  │            │             │             │
   │  └────────────────┘  │            │  ┌──────────▼──────────┐  │
   └──────────────────────┘            │  │ Actuator interface  │  │
              │                        │  └──────────┬──────────┘  │
              │                        │             │             │
              │                        │   ┌─────────▼─────────┐   │
              │                        │   │ Simulator (now)   │   │
              │                        │   │ GPIO/servo (later)│   │
              │                        │   └───────────────────┘   │
              │                        └───────────────────────────┘
              │                                      │
              └──────── Tailscale (private) ─────────┘
                                │
                     ┌──────────▼──────────┐
                     │  Desktop PC         │
                     │  (off, no agent)    │
                     │  power button pins  │
                     └─────────────────────┘
```

Key properties:

- Exactly **two** peers talk: the app and the device.
- There is **no cloud relay, no hosted server, and no inbound public
  exposure**. Reachability comes from Tailscale on the device and the phone.
- The desktop PC runs **no software** in this design. It is an electrical
  target, not a participant.
- The **TCP command channel is authoritative**: it is the only way to change the
  actuator, and the only input to the lease and the watchdog.
- The **UDP status channel is outbound-only** from the device. The device sends
  heartbeats from an ephemeral source port to a configured destination; it binds
  no listening UDP port and accepts no UDP input.
- The **browser demo is a client, not a layer**: a local bridge is an ordinary
  TCP client of the device (plus a UDP status listener) that re-exposes the
  interaction to a page. It cannot bypass the session manager, and it owns no
  safety behaviour of its own. Section 4.3 covers it.

> The status monitor in the diagram is a **development and protocol
> demonstration tool**, not part of the mobile product. No graphical monitoring
> dashboard is planned for the app.

## 3. Layers

The system is split so that each concern can be reasoned about, tested and
explained on its own. Dependencies point in one direction only (top depends on
bottom):

| # | Layer | Responsibility | Status | Must not know about |
| --- | --- | --- | --- | --- |
| 1 | **Transport** (`net/`) | Sockets: accept TCP connections, decode frames; send UDP status datagrams | TCP and UDP implemented | Safety policy, actuator hardware |
| 2 | **Protocol** (`protocol/`) | Message dataclasses, JSON encode/decode, validation of shape and types | Implemented (command lines and UDP datagrams) | Sockets, hardware |
| 3 | **Session / business logic** (`session.py`) | Which connection owns the actuator, current state, request ordering, wiring the lease to the actuator | Implemented | Socket details |
| 4 | **Safety** (`safety/`) | Lease timer, maximum-duration timer, automatic release | **Implemented** (TCP-renewed lease) | Client behaviour — it assumes the client can fail |
| 5 | **Actuator** (`actuator/`) | `press()` / `release()` / `is_pressing()` behind an interface | Interface + simulator implemented; GPIO planned | Networks entirely |

The rule that matters most: **safety never depends on the client.** Layer 4 sits
between the session and the actuator on the device, and it can forcibly end a
press regardless of what the app asks for or fails to do. The session arms it on
`PRESS_START`, the lease is pushed forward only by a valid `PRESS_RENEW`, and the
watchdog releases the actuator when either deadline is reached — including when
the client has stopped talking altogether. (See section 5.1 for the limit of that
guarantee: the watchdog is software, running in the same process as the server.)

`PressWatchdog` deliberately knows nothing about sockets, sessions or the
protocol: it takes an async release callback, the limits, and an injectable
monotonic clock. That is what keeps the timing behaviour testable in
milliseconds instead of seconds.

The UDP heartbeat is a **reader of layer 3**, never a peer of layer 4. It is a
separate service object that reads the session's read-only properties and sends
datagrams; it cannot reach the session's command handler. Section 4.1 covers the
isolation in detail.

## 4. Networking design

Two transports, with clearly separated responsibilities:

| Channel | Transport | Why | Status |
| --- | --- | --- | --- |
| Press / stop commands | **TCP** | Reliable, ordered, connection-oriented. A dropped "stop" is unacceptable, and TCP closes give us a clean "the app is gone" signal. | **Implemented** |
| Device state / liveness heartbeat | **UDP** (device → monitor) | Small, frequent, loss-tolerant datagrams. Missing one heartbeat costs one status update, not correctness. | **Implemented** |

Consequences of this split:

- The **TCP connection is authoritative** for everything that matters: actuator
  state, session ownership, the lease, and the watchdog. Nothing on the UDP side
  can change any of them.
- The **UDP heartbeat exists to make the device observable** without a command
  connection: a monitoring client can see the device is alive and what the
  actuator is doing, and can tell when the device goes quiet.
- The lease is renewed **over TCP only**. UDP is deliberately *not* an input to
  the lease: it is lossy, unordered and unauthenticated, so it could never be
  allowed to hold a safety deadline open.
- Because UDP can be blocked while TCP works, a monitoring client may show the
  device as offline even though the command channel is healthy. That is a
  monitoring limitation, not a safety one.

Implemented press timings (configurable in `backend/remote_tap/config.py`,
overridable with command-line flags):

| Parameter | Implemented value | Rationale |
| --- | --- | --- |
| TCP command port | 8765 | Unprivileged, not a well-known service port |
| Lease renewal interval | 250 ms | Advertised to the client; cheap over a private network |
| Lease timeout | 1000 ms | Four missed renewals before a release: tolerates Wi-Fi jitter without holding the button indefinitely |
| Maximum press duration | 3000 ms | Hard ceiling that no client message can extend |
| Bind address | loopback by default; the operator passes the Tailscale address explicitly | Never expose to the public internet |

Implemented UDP status values:

| Parameter | Implemented value | Rationale |
| --- | --- | --- |
| Heartbeat interval | 1000 ms | Frequent enough to spot an outage quickly, light enough to be negligible |
| Stale / offline timeout | 3000 ms | Three missed heartbeats before a monitor calls the device offline; must be at least twice the interval |
| Monitoring-client port | 8766 | The monitor listens here; the device sends from an ephemeral source port |
| Device bind | `127.0.0.1`, port `0` (ephemeral) | Outbound-only socket: no listening UDP port, no UDP input accepted |
| Device id | `remotetap-01` | Lets one monitor watch several devices later; **stable across restarts** |
| Boot id | A fresh 32-character hex string per service run (stdlib `uuid.uuid4().hex`) | Names this *run* of the service; a new value tells the monitor the device restarted and its sequence counter reset |

The **boot id** is the answer to a specific problem: the sequence number restarts
at `1` on every process start, so a monitor that tracked sequence alone would
read a restarted device's first datagram as "out of order" (it previously saw
`500`). The device therefore stamps every datagram with a boot id that is
constant for the lifetime of the heartbeat service and changes only when the
process (re)starts. The monitor keys its sequence state on
`(device_id, boot_id)`, so a new boot id starts a fresh sequence stream rather
than colliding with the old one. See section 4.2.

### 4.1 Why the UDP channel cannot affect the actuator

The separation is structural rather than a rule to remember:

1. The heartbeat service is a **separate object** (`net/udp_heartbeat.py`) from
   the TCP command server, constructed in `app.py` next to it and stopped next
   to it.
2. It holds a reference to the session **only to read** its read-only
   properties (`state`, `active_session_id`). It never calls
   `DeviceSession.handle`, `release` or `release_if_client`.
3. The device's UDP socket has **no command path**: `_HeartbeatProtocol`
   receives nothing meaningful (the socket is outbound-only) and a received
   datagram is logged and dropped.
4. A send failure (`OSError`) is caught, counted and logged; it can never
   propagate into the TCP server.
5. The heartbeat task re-raises only `CancelledError`; any other exception is
   logged and swallowed, so a status-channel bug cannot take down the command
   channel.

What this guarantees, matching the Phase 3 requirements:

* UDP can never start, renew, extend or end a press.
* UDP can never move the press lease or the watchdog deadline.
* Losing UDP packets cannot interrupt or extend a press.
* Duplicate or out-of-order datagrams cannot corrupt the displayed status (the
  monitor ignores them).
* A UDP failure cannot disable TCP command handling, and a broken UDP channel
  does not affect the shutdown release order (the actuator is still released
  before the TCP server stops).

### 4.2 Recognising a restarted device

Sequence numbers are only meaningful *within one run* of the device. Because the
counter restarts at `1`, the monitor cannot compare a datagram against the last
one it saw without first knowing whether the two came from the same run. The
**boot id** supplies that context.

**Device side.** `HeartbeatService` (`net/udp_heartbeat.py`) generates one boot id
when it is constructed — a stdlib `uuid.uuid4().hex` — stores it on the instance,
and includes it in the `boot_id` field of every heartbeat and status datagram.
It is never regenerated for the lifetime of the service, so every datagram from
one run carries the same boot id. The `device_id` is deliberately left unchanged:
it names the *device*, while the boot id names the *run*. The `ts` timestamp is
**not** used as a boot identity — a clock can jump backwards or forwards, and two
runs can share a timestamp.

**Monitor side.** `StatusMonitor` (`monitor.py`) keeps a small tracker per
`(device_id, boot_id)` pair and a `_current_boot` map from device to its live
boot id. When a datagram arrives:

1. It is parsed and filtered against the expected device ids (unchanged from
   Phase 3: an unexpected device is ignored).
2. If the `(device_id, boot_id)` pair has no tracker, this is the **first
   datagram of a run**. If the monitor already held a *different* boot id for
   that device, it has just seen a **restart**: the old boot's tracker is marked
   `superseded`, a restart is counted, and the reported line is flagged. A
   brand-new device (no previous boot) is just a first sighting, not a restart.
   Either way, a fresh tracker is created with the new sequence.
3. If a tracker exists but is `superseded`, the datagram is dropped as
   `SUPERSEDED` regardless of its sequence number. This is what stops an old
   boot's late packet from overwriting the current status or keeping the device
   "alive".
4. Otherwise the sequence is compared **within the same boot**: equal →
   `DUPLICATE`, lower → `OUT_OF_ORDER` (the displayed status is never rewound),
   higher → `ACCEPTED`, with the gap counted as missing heartbeats.

So a restarted device's sequence `1` is `ACCEPTED` (a new boot), whereas a
sequence below the current boot's high-water mark is `OUT_OF_ORDER`, and a repeat
of the latest sequence is a `DUPLICATE`. Staleness and the once-per-outage
offline notice are unchanged: they still run off the receiver's own monotonic
clock, never the sender's `ts`.

**What this does and does not guarantee.** Restart detection is a *correlation*
mechanism, not a security one. Without authentication, "a boot id I have not seen
before" is indistinguishable from a forged datagram or a first contact, so the
monitor treats an unknown boot id as a new run. Nothing stops an attacker who can
reach the UDP port from sending a datagram with a fresh boot id and a high
sequence; at most this misleads a *monitor*'s display. It cannot touch the
actuator, the lease or the watchdog, because the UDP path has no route into the
session (section 4.1). Authenticating datagrams is deferred to the
authentication phase.

### 4.3 The local demo bridge (a stand-in for the mobile app)

The Expo app does not exist yet, so `remote_tap.bridge` is a small,
**loopback-only** process that lets a browser demonstrate the press-and-hold
interaction. It exists for one reason: a web page cannot open a raw TCP or UDP
socket, so something has to sit between the browser and the device and speak the
device's protocols on the page's behalf.

```
  Browser (page)            Bridge (this machine)                Device service
  ┌───────────────┐  HTTP   ┌────────────────────┐   TCP (existing)  ┌───────────────┐
  │  dashboard    │◄────────│ FastAPI + uvicorn  │──────────────────►│ TCP command   │
  │  press/hold   │  WS     │  • DeviceLink      │  PRESS_START/…    │ server        │
  │  status chips │◄───────►│  • PressController  │                  │ session +     │
  └───────────────┘         │  • StatusHub        │◄─── UDP (existing)│ lease/watchdog│
                            └────────────────────┘   heartbeat/status └───────────────┘
```

**It is a client of the device, in exactly the sense any other client is:**

- `bridge/device_link.py` opens **one** TCP connection and speaks the existing
  command protocol on it (`PRESS_START` / `PRESS_RENEW` / `PRESS_STOP`, `ACK` /
  `ERROR`). Because it is a single ordinary connection, it inherits the device's
  ownership model unchanged — it cannot bypass the session manager, and a
  browser request can only ever do what the protocol itself allows.
- `bridge/press.py` owns at most one press and, while it is held, renews the
  lease over that same TCP connection on a timer. The interval is taken from the
  `renew_interval_ms` the device advertises in its `ACK`, and every renewal is a
  **new** command, because a repeated `command_id` is treated as a retry and
  would not extend the lease. A repeated pointer-down from the page that started
  the press is reported as the press already in progress rather than restarted
  (restarting would reset the device's maximum-duration timer); a second page
  that presses while another holds the button is refused before any command
  reaches the device.
- `bridge/status_hub.py` is the **only** UDP listener on the heartbeat port and
  reuses the shipped `StatusMonitor` to interpret datagrams. It does not open a
  second receiver competing with `remote_tap.monitor`: the two are alternatives,
  not companions, on the same port.
- `bridge/app.py` serves the page and its assets, streams state snapshots over
  one WebSocket, and applies the page's requests through the press controller.

**WebSocket does not replace TCP.** It is only the browser-to-bridge transport,
because that is all a page has. The command path from the bridge to the device
is the original newline-delimited JSON over TCP, unchanged, and the status path
is the original heartbeat datagram, unchanged. No protocol was rewritten to
accommodate the browser, and no browser request reaches the device without going
through the device's own validation, ownership check and lease.

**What it deliberately does not do.** It does not reimplement the session
manager, the lease, the watchdog or the actuator; it adds no safety rule of its
own; it does not expose the service publicly (the page binds loopback by
default); and it does not enforce the lease or the maximum press duration. The
page's own release handling is a convenience layered on top of the device's
rules, never the mechanism that protects the actuator.

**Safety on disconnect**, using only device behaviour that already existed:

| What goes away | What releases the actuator |
| --- | --- |
| The page (tab closed, socket dropped) | The bridge sees the WebSocket close and sends `PRESS_STOP` for the press that connection owned. If that cannot be sent, the next row covers it. |
| The bridge process, or its TCP connection | The device runs its own `release_if_client` for that connection, and the lease stops being renewed. |
| The page stalls but stays connected | Nothing new is required: the bridge renews only while it holds a press, so the device's lease timeout still ends it. |
| The bridge stalls but its socket stays open | The device's **lease timeout** fires within 1000 ms, and the **maximum press duration** (3000 ms) still caps the hold. |

So the page holds no safety-critical role: it cannot extend a press past the
lease, and it cannot keep a press alive after it has gone.

## 5. Safety model

The device is authoritative. The invariants below describe the target behaviour;
the arrow under each one says what exists today.

1. **Bounded hold.** Every press starts a hold with a hard maximum duration
   (3000 ms by default). When it expires the device releases the actuator and
   records the reason (reason: `max_press_exceeded`), and the owning connection
   is told why on its next `PRESS_RENEW` or `PRESS_STOP`. No client message can
   extend the maximum: a repeated `PRESS_START` is not a renewal and does not
   restart the timer.
   → **Implemented.**
2. **Deadman lease.** While the actuator is held, the device requires a valid
   `PRESS_RENEW` at least every lease timeout (1000 ms by default; the client is
   asked to renew every 250 ms). Silence past the timeout → release (reason:
   `lease_timeout`).
   → **Implemented.** Renewals are accepted only from the owning connection and
   only for the active session id, and are validated before the lease extends.
3. **Release on disconnect.** Closing the TCP connection, either side, triggers
   a release (reason: `client_disconnected`).
   → **Implemented.** Released only if the disconnecting connection owned the
   active session.
4. **Release on shutdown.** Process exit, SIGINT/SIGTERM, or an unexpected
   exception in a handler releases the actuator, so the button cannot be left
   stuck down.
   → **Implemented** for the service interrupt/exit path (unconditional release
   on shutdown). Per-handler exception recovery is defensive rather than a
   dedicated release path.
5. **Single controller.** At most one client controls the actuator at a time; a
   second concurrent press is rejected (`INVALID_SESSION`). A later connection
   may take over the *idle* state but never inherits an active hold.
   → **Implemented.** The connection that created the session is its exclusive
   owner; a different connection cannot control it even with the session id.
6. **Idempotent, explicit transitions.** Duplicate presses while already holding
   return the current state rather than re-triggering hardware; a stop when
   already idle is rejected without touching hardware. Retries of the same
   `(session_id, command_id)` return the cached response.
   → **Implemented.**
7. **Authenticated, not just reachable.** Being on the tailnet is necessary but
   not sufficient: the client will present a shared secret at handshake time.
   **[TBD]** how the secret is provisioned (config file vs. environment
   variable) and whether a pre-shared key is enough for the prototype.
   → **Planned. Not implemented;** today reachability on the private network is
   the only barrier.
8. **Auditable.** Every accepted command, release reason and actuator transition
   is logged with a timestamp, so we can explain after the fact exactly why the
   button was or was not held.
   → **Partially implemented:** commands, ownership decisions, connect/disconnect
   and actuator transitions are logged, and both safety releases log their reason
   (`lease_timeout`, `max_press_exceeded`). A structured event log
   (`safety/audit.py`) is still planned.

The failure story we design for: *the phone dies mid-hold.* The TCP connection
breaks and renewals stop. **Both halves are now covered**: the disconnect
releases the actuator immediately if the dead connection owned the press, and if
the connection somehow lingers silently the lease timeout releases it within
1000 ms — and in every case the press ends at 3000 ms at the latest.

### 5.1 Where the watchdog runs — and what it cannot guarantee

`PressWatchdog` runs in the **same process and the same asyncio event loop** as
the TCP command server. It is not a separate thread, a separate process, or a
hardware timer: it is a coroutine (`asyncio.ensure_future(self._run())`) that
sleeps until the nearest deadline on the loop that also serves the sockets.

This has a direct consequence for the safety argument. The watchdog can only run
when the event loop is able to run it. Any of the following **prevents the
software watchdog from executing**, and the actuator can then stay pressed past
both deadlines:

* a **blocked event loop** — a long synchronous call in any handler (or in a
  library on the same loop) stalls the loop, so the watchdog coroutine does not
  get scheduled;
* a **process crash** — an unhandled fault, `SIGKILL`, or the interpreter
  exiting takes the watchdog with it;
* **OS suspension / sleep / hibernation** — if the Pi suspends, no Python code
  runs until it wakes;
* **forced termination** — `kill -9`, a power cut, or an OOM kill stops the
  process without running the shutdown release path.

So the lease and maximum-duration timers protect against a *misbehaving or
disappearing client*, which is what Phase 2 set out to do. They do **not** make
the device fail-safe against the watchdog's own host failing.

**Before any physical actuator is connected** (GPIO/servo integration, a later
phase), a **hardware-level fail-safe** will be required in addition to this
software watchdog — for example a hardware timer or dead-man circuit that
de-energises the actuator independently of the Pi's software. A press that can
only be ended by the same process that could crash is not an acceptable safety
boundary for real hardware.

## 6. Actuator abstraction

The only layer that will differ between the simulator and real hardware.

```python
class Actuator(ABC):
    @property
    def state(self) -> ActuatorState: ...      # IDLE or PRESSING
    async def press(self) -> None: ...
    async def release(self) -> None: ...
    def is_pressing(self) -> bool: ...
```

- `SimulatedActuator` (**implemented**): keeps an in-memory state and prints
  every transition (`IDLE -> PRESSING` / `PRESSING -> IDLE`) so a run is
  observable without hardware.
- `GpioActuator` (**future hardware, not implemented**): will drive a servo or
  transistor on the Pi Zero 2 W. Same interface; the layers above do not change.

`press()` and `release()` are state-checked, so a repeated call never produces a
second actuation: pressing while already pressing, or releasing while already
idle, is a no-op.

## 7. Code layout

What exists today, and what is still to come:

```
backend/
  remote_tap/
    config.py        # implemented: host, port, size limits, press limits, UDP settings
    app.py           # implemented: wiring + entry point, shutdown release, UDP start/stop
    monitor.py       # implemented: UDP monitoring client (development tool)
    protocol/
      messages.py    # implemented: command dataclasses + JSON codec
      validate.py    # implemented: shape/type validation
      udp_messages.py  # implemented: datagram codec + validation (socket-free)
    net/
      tcp_server.py  # implemented: asyncio command server
      udp_heartbeat.py  # implemented: outbound-only UDP heartbeat service
    session.py       # implemented: who owns the actuator; state machine; lease wiring
    safety/
      watchdog.py    # implemented: lease timer, max-press timer, forced release
      audit.py       # PLANNED: structured event log
    actuator/
      base.py        # implemented: Actuator interface
      simulator.py   # implemented: SimulatedActuator
    bridge/          # implemented: local demo bridge (browser interface, section 4.3)
      device_link.py   # implemented: one TCP connection speaking the command protocol
      press.py         # implemented: the single press + its lease renewals
      status_hub.py    # implemented: sole UDP listener; reuses monitor.StatusMonitor
      app.py           # implemented: FastAPI app (HTTP + WebSocket), state fan-out
      __main__.py      # implemented: `python -m remote_tap.bridge` entry point
    web/             # implemented: demo dashboard assets (no build step, no bundler)
      index.html       # implemented: dashboard markup
      styles.css       # implemented: dark-first responsive theme
      app.js           # implemented: WebSocket client + pointer-event hold
  tests/
    test_protocol.py       # implemented
    test_actuator.py       # implemented
    test_session.py        # implemented
    test_tcp_server.py     # implemented (integration over a real socket)
    test_safety.py         # implemented (lease expiry, maximum duration, watchdog)
    test_udp_protocol.py   # implemented (datagram codec + monitor logic, no sockets)
    test_udp_heartbeat.py  # implemented (real UDP socket, isolation from TCP)
    test_bridge_link.py    # implemented (bridge TCP link vs. the real command server)
    test_bridge_press.py   # implemented (press, renewals, ownership, device limits)
    test_bridge_status.py  # implemented (UDP forwarding, offline, port exclusivity)
    test_bridge_app.py     # implemented (real uvicorn HTTP + WebSocket, end to end)

mobile/                  # PLANNED (later phase) - does not exist yet
  src/
    screens/         # the hold button
    services/        # transport client, connection state
    types/           # protocol types mirroring docs/protocol.md
```

## 8. Non-goals for this phase

- No GPIO, servo, relay or mechanical implementation.
- No mobile UI beyond the single hold button and a connection indicator.
- No relay server, no accounts, no database, no device registry.
- No OTA updates, no Docker, no CI beyond running `pytest` locally.
- No multi-PC, multi-user, or scheduled-press features.
- **No graphical monitoring dashboard, in the app or elsewhere.** The UDP
  monitor is a console tool for developing and demonstrating the protocol. The
  browser demo (section 4.3) is a temporary stand-in for the mobile app's hold
  button — a demonstration of the interaction, not a product dashboard — and is
  replaced by the Expo app.
- **No new safety mechanism in the demo.** The bridge reuses the existing lease,
  disconnect release and watchdog; it does not add or replace any of them.
- No authentication, and no Tailscale automation: both are a later phase.

## 9. Testing strategy

Implemented tests (226, all passing) cover Phase 1, the Phase 2 lease, the
Phase 3 UDP status channel and the browser demo bridge:

- **Protocol** (`tests/test_protocol.py`): encode/decode round-trips, malformed
  frames, unknown types, missing/empty fields, non-object payloads, size limits,
  and the `PRESS_RENEW` command and lease fields on `ACK`.
- **Actuator** (`tests/test_actuator.py`): simulator state, transitions, and
  no-op behaviour on repeated calls.
- **Session** (`tests/test_session.py`): happy-path transitions, **exclusive
  connection ownership** (only the owner may stop; a stranger is refused even
  with the session id; the error does not reveal the session id), release on
  disconnect, and duplicate-`command_id` idempotency keyed by
  `(session_id, command_id)`.
- **Safety** (`tests/test_safety.py`): lease expiry releases the actuator; a
  renewed press outlives the lease timeout; only the owner may renew and only for
  the active session id; renewal at the wrong time yields `LEASE_EXPIRED` (owner)
  or `INVALID_SESSION` (stranger); the maximum duration ends even a diligently
  renewed press; a repeated `PRESS_START` does not extend the maximum;
  `PressLimits` rejects inconsistent timings, zero/negative values and an
  unreasonably long maximum; a **stale cached response is never replayed as if
  the press were still active** and a stale retry never starts a new press; a
  new press after an automatic expiry is owned by the new connection while the
  old one cannot renew, stop or release it; shutdown releases the actuator
  before the TCP server stops and leaves no watchdog task behind; a duplicate
  renewal is inert; and a burst of renewals banks no extra lease time.
- **Integration** (`tests/test_tcp_server.py`): a real asyncio TCP client against
  the server on loopback — framing (multiple messages per write; one message
  split across writes), error recovery, oversize rejection, duplicate commands,
  ownership across two connections, release on disconnect, and the lease expiring
  or being kept alive over a real socket.
- **UDP protocol and monitor** (`tests/test_udp_protocol.py`): datagram
  round-trip and exact wire fields; invalid JSON, non-object payloads,
  oversized datagrams, every missing/invalid field, an unsupported version and an
  unsupported type; then the monitor's logic with an **injected clock** —
  in-order acceptance, gap counting, duplicate and out-of-order rejection that
  never rewinds the displayed status, rejection of another device's datagrams,
  staleness at the timeout boundary, the once-per-outage offline notice, and that
  staleness uses the receiver's clock rather than the sender's `ts`. The restart
  cases use a pinned boot id: a first sighting is not a restart, a new boot id is
  recognised as one, a restarted device's sequence `1` is accepted, an old
  sequence within the *current* boot is still out of order, duplicates are
  duplicates within one boot, a late datagram from a superseded boot is ignored
  (and does not keep the device alive), and two devices are tracked
  independently — plus a boot id that must be a non-empty string and must
  round-trip unchanged.
- **UDP heartbeat and isolation** (`tests/test_udp_heartbeat.py`): against a real
  UDP socket — heartbeats sent at the configured interval (and a long interval
  honoured after the immediate first beat), sequence numbers incrementing by one,
  the reported state read from the authoritative session (idle, pressing, and
  back to idle), `status` versus `heartbeat` selection, the heartbeat stopping on
  shutdown with **no background task left**, the socket released and a repeat
  `stop()` safe; a failed send counted and consuming no sequence number, and a
  failed send not killing the task. Then the isolation proofs: datagrams sent
  straight at the device socket cannot initiate, stop or renew a press, cannot
  move the lease deadline (while a TCP renewal can), stopping the UDP channel
  does not release a press, and breaking the UDP channel mid-run does not stop
  TCP command handling. The boot id is checked here too: it stays constant for
  the life of one service instance, a second instance generates a different one,
  and a real stop/start pair of services is recognised by the monitor as a single
  restart with no out-of-order or superseded events.
- **Bridge: TCP link** (`tests/test_bridge_link.py`): the bridge's link against a
  real command server — a `PRESS_START` `ACK` carrying the lease policy, sending
  before connecting raising, a disconnect noticed *promptly* (the `on_lost`
  callback fires) rather than only on the next send, closing a link releasing a
  press it owns, a second connection being refused (`INVALID_SESSION`) while the
  first still holds the session, and a bad session renewal being rejected.
- **Bridge: press controller** (`tests/test_bridge_press.py`): press and release
  through the controller; the bridge **renewing the lease while the button is
  held** (the press outlives several lease timeouts); the device's maximum
  duration ending the press on its own and the reason being recorded; a second
  browser being refused (`NOT_OWNER`); `release_if_owner` acting only for the
  true owner; a repeated `press_start` **not** restarting the press (the session
  id is unchanged — a stale or duplicated request cannot reset the device's
  timer); stopping while idle being a no-op; a press with no link reporting an
  error; and a lost link clearing the bridge's press.
- **Bridge: UDP status** (`tests/test_bridge_status.py`): a heartbeat forwarded to
  the page and firing the update callback; a malformed datagram counted as
  `rejected` and leaving no status; another device's datagrams ignored when a
  device id is expected; a new boot id recognised as a restart (its sequence `1`
  accepted, not out of order); the device going offline after silence; and a
  **second listener being unable to bind the same port**, which is what proves
  the bridge is the sole UDP receiver rather than a competitor.
- **Bridge: end to end** (`tests/test_bridge_app.py`): the real FastAPI app under
  a real uvicorn server, driven over real HTTP and a real WebSocket against the
  real device TCP server — the dashboard and its assets served; `/api/state`
  reporting a live link and an idle actuator; a new WebSocket greeted with state;
  press and release driving the device session; **closing the page releasing the
  press**, so the actuator is never left held; an unknown action and a non-JSON
  message refused as notices without killing the connection; a second page unable
  to stop another page's press; a UDP heartbeat reaching the page with its
  sequence number; `BridgeRuntime` shutdown releasing the press; and a bridge
  pointed at a dead port still serving the page and reporting the device as
  disconnected.

The bridge tests drive the same HTTP/WebSocket interface the page uses; the
page's own JavaScript is not executed by the suite. Pointer-cancellation,
lost-capture and visibility handling live in `web/app.js` and are exercised
manually in a browser; what the tests pin down is the equivalent backend
guarantee — that a browser going away releases the press — plus the message the
page sends for every release path.

Timing tests use short, injected `PressLimits` / `UdpConfig` and an injectable
monotonic clock rather than real sleeps, so the whole suite runs in a few
seconds. Where a real interval must be observed, the tests poll for a condition
with a timeout instead of sleeping a fixed amount. The default `PressLimits`
(1000 ms lease) is used everywhere else, so a test that never touches the timings
cannot be released by the watchdog mid-assertion.

Planned tests (later phases, **not written yet**):

- The handshake and authentication path, and any Tailscale-specific behaviour.

## 10. Open questions

Decisions still needed for the phases after Phase 3:

1. **Heartbeat rate** — 1000 ms implemented, stale after 3000 ms. Should the
   device beat faster while pressing, or slow down when idle to save power?
2. **Authentication** — shared secret in a config file is my suggestion for the
   prototype. Acceptable, or do you want something else? And should the UDP
   status channel be authenticated at all, or is it acceptable for status to be
   forgeable as long as it cannot command anything?
3. **Ownership of the button state** — the app currently learns the device's
   state from `ACK` replies. Should it also consume the UDP status stream, or is
   a purely command-driven app enough for the first version?
4. **Maximum hold duration** — implemented at **3000 ms** as a hard ceiling. Is
   that the right value for the behaviour you want (a short tap vs. a deliberate
   multi-second hold)?
5. **Session resume on reconnect** — Phase 1 forces a new session after any
   disconnect, and Phase 2 keeps that rule. Keep it permanently, or allow
   resuming an active hold in a later phase?
