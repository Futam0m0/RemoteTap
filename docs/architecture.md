# RemoteTap — Architecture

> Status: draft v0, written before any implementation.
> Anything marked **[TBD]** is an open question awaiting a decision; see the
> last section.

## 1. Purpose and scope

RemoteTap presses a desktop PC's power button from a distance: the user holds a
button in a mobile app, and a separate device physically holds the PC's power
button down for as long as the user holds theirs.

The desktop PC is never required to be running. The device is powered and on the
network on its own, and it acts on the PC externally.

This document covers the software architecture for the prototype phase, in which
the actuator is **simulated**.

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
   │  ┌────────────────┐  │   UDP      │  ┌──────────▼──────────┐  │
   │  │ Heartbeat      │<─┼───────────>│  │ Session + safety    │  │
   │  │  keep-alive    │  │ heartbeats │  └──────────┬──────────┘  │
   │  └────────────────┘  │            │             │             │
   └──────────────────────┘            │  ┌──────────▼──────────┐  │
              │                        │  │ Actuator interface  │  │
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

## 3. Layers

The system is split so that each concern can be reasoned about, tested and
explained on its own. Dependencies point in one direction only (top depends on
bottom):

| # | Layer | Responsibility | Must not know about |
| --- | --- | --- | --- |
| 1 | **Transport** (`net/`) | Sockets: accept TCP connections, decode frames, send/receive UDP datagrams | Safety policy, actuator hardware |
| 2 | **Protocol** (`protocol/`) | Message dataclasses, JSON encode/decode, validation of shape and types | Sockets, hardware |
| 3 | **Session / business logic** | Which client is in control, current state, request ordering | Socket details |
| 4 | **Safety** (`safety/`) | Hold limits, lease/watchdog, automatic release, audit log | Client behaviour — it assumes the client can fail |
| 5 | **Actuator** (`actuator/`) | `press()` / `release()` / `is_pressed()` behind an interface | Networks entirely |

The rule that matters most: **safety never depends on the client.** Layer 4 sits
between the session and the actuator on the device, and it can veto or forcibly
end an action regardless of what the app asks for or fails to do.

## 4. Networking design

| Channel | Transport | Why |
| --- | --- | --- |
| Press / release commands | **TCP** | Reliable, ordered, connection-oriented. A dropped "release" is unacceptable, and TCP closes give us a clean "the app is gone" signal. |
| Heartbeat / liveness | **UDP** | Small, frequent, loss-tolerant datagrams. Missing one heartbeat is fine; missing several is a safety input. |
| Device state / liveness echo | **UDP** | Lets the app show whether the device is reachable without holding a TCP connection open. |

Consequences of this split:

- The safety lease is driven by **UDP heartbeats**, not by TCP. A TCP connection
  that stays open but goes silent (a frozen phone) must still result in a
  release, so liveness is measured on the heartbeat channel and is *also*
  released on TCP close.
- Because UDP can be blocked while TCP works, the safety layer treats "no
  heartbeat" as a release trigger; the app is responsible for sending heartbeats
  promptly while the button is held.

Proposed defaults for the prototype (**[TBD]** — confirm values):

| Parameter | Proposed value | Rationale |
| --- | --- | --- |
| TCP command port | 8765 | Unprivileged, not a well-known service port |
| UDP heartbeat port | 8766 | Same |
| Heartbeat interval while holding | 1000 ms | Frequent enough to detect failure quickly, cheap on a Pi Zero |
| Lease timeout (no heartbeat) | 3000 ms = 3 missed beats | Tolerates brief Wi-Fi hiccups without holding the button indefinitely |
| Maximum hold duration | 10000 ms | Hard ceiling that a client cannot override |
| Bind address | Tailscale interface / `0.0.0.0` on the tailnet only | Never expose to the public internet |

## 5. Safety model

The device is authoritative. The invariants:

1. **Bounded hold.** Every press starts a hold with a hard maximum duration.
   When it expires, the device releases and reports that it did so
   (reason: `max_hold_exceeded`). Presses longer than the maximum are clamped,
   not rejected silently.
2. **Deadman lease.** While the actuator is held, the device requires a fresh
   heartbeat at least every lease-timeout. Silence past the timeout → release
   (reason: `lease_timeout`).
3. **Release on disconnect.** Closing the TCP connection, either side, triggers
   a release (reason: `connection_lost`).
4. **Release on shutdown.** Process exit, SIGINT/SIGTERM, or an unexpected
   exception in a handler releases the actuator, so the button cannot be left
   stuck down.
5. **Single controller.** At most one client controls the actuator at a time;
   a second concurrent press is rejected (`busy`). A later connection may take
   over the *idle* state but never inherits an active hold.
6. **Idempotent, explicit transitions.** Duplicate presses while already holding
   return the current state rather than re-triggering hardware; a release when
   already released succeeds without touching hardware.
7. **Authenticated, not just reachable.** Being on the tailnet is necessary but
   not sufficient: the client presents a shared secret at handshake time.
   **[TBD]** how the secret is provisioned (config file vs. environment
   variable) and whether a pre-shared key is enough for the prototype.
8. **Auditable.** Every accepted command, release reason and actuator transition
   is logged with a timestamp, so we can explain after the fact exactly why the
   button was or was not held.

The failure story we design for: *the phone dies mid-hold.* The TCP connection
breaks and heartbeats stop. Within the lease timeout, the device releases the
button on its own and records why. No user interaction is required for safety.

## 6. Actuator abstraction

The only layer that will differ between the simulator and real hardware.

```python
class Actuator(Protocol):
    async def press(self) -> None: ...
    async def release(self) -> None: ...
    def is_pressed(self) -> bool: ...
```

- `SimulatedActuator` (prototype): logs transitions, keeps an in-memory state,
  optionally with an artificial delay so timing behaviour is realistic.
- `GpioActuator` (later): drives a servo or transistor on the Pi. Same
  interface; the layers above do not change.

`press()` and `release()` are idempotent and state-checked, so a repeated call
never produces a second physical actuation.

## 7. Planned code layout

```
backend/
  remote_tap/
    config.py        # ports, timings, limits, token (single source of truth)
    app.py           # wiring + entry point, signal handling
    protocol/
      messages.py    # dataclasses + JSON codec
      validate.py    # shape/type validation, version checks
    net/
      tcp_server.py  # asyncio command server
      heartbeat.py   # UDP sender/receiver
    session.py       # who controls the actuator; state machine
    safety/
      watchdog.py    # lease timer, max-hold timer, forced release
      audit.py       # structured event log
    actuator/
      base.py        # Actuator interface
      simulator.py   # SimulatedActuator
  tests/
    test_protocol.py
    test_safety.py
    test_session.py
    test_tcp_integration.py

mobile/
  src/
    screens/         # the hold button
    services/        # transport client, heartbeat, connection state
    types/           # protocol types mirroring docs/protocol.md
```

## 8. Non-goals for this phase

- No GPIO, servo, relay or mechanical implementation.
- No mobile UI beyond the single hold button and a connection indicator.
- No relay server, no accounts, no database, no device registry.
- No OTA updates, no Docker, no CI beyond running `pytest` locally.
- No multi-PC, multi-user, or scheduled-press features.

## 9. Testing strategy

- **Protocol**: encode/decode round-trips, malformed frames, unknown types,
  wrong protocol version.
- **Safety** (the priority): lease expiry releases the actuator, max hold clamps,
  TCP close releases, duplicate presses are idempotent, a second client gets
  `busy`, shutdown always releases.
- **Session**: state transitions and release reasons.
- **Integration**: a real asyncio TCP client + UDP heartbeat against the server
  on loopback, including the "client goes silent mid-hold" scenario.
- Timing tests use short, injected lease/limit values rather than real sleeps
  where possible.

## 10. Open questions

Decisions I need from you before implementing:

1. **Ports and timing defaults** — accept the table in §4, or different values?
2. **Authentication** — shared secret in a config file is my suggestion for the
   prototype. Acceptable, or do you want something else?
3. **Ownership of the button state** — should the app show a live "device is
   holding / released" state pushed over UDP, or is a purely command-driven app
   enough for the first version?
4. **Maximum hold duration** — 10 s proposed. Is that the right ceiling for the
   behaviour you want (a short tap vs. a deliberate multi-second hold)?
5. **Backend entry point package name** — `remote_tap` under `backend/` is my
   suggestion. Fine, or prefer `server/`, `device/`, or a src-layout?
6. **Test dependency** — `pytest-asyncio` is included for async server tests.
   Keep it, or avoid it and use `asyncio.run()` inside plain pytest tests?
