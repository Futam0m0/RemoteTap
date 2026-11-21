# RemoteTap

RemoteTap is a **remote physical power-button actuator**.

It lets a user press and hold a button in a mobile app, which causes a remotely
located hardware device to physically press and hold a desktop PC's power
button.

The desktop PC does not need to be powered on for RemoteTap to work, because
the actuator sits *outside* the PC and drives the power button (or the
motherboard's power header) directly.

## Current status

**Phase 3 — software only, simulated actuator.**

The hardware layer is deliberately not implemented yet. During this phase we
build and test the networking, session, ownership and safety logic against a
**simulated actuator** that logs what a real actuator would do.

Two transports are used, with different responsibilities: **TCP carries
commands** (the only way to change the actuator, and the only input to the
lease), and **UDP carries status** (a read-only heartbeat the device emits so a
monitoring client can watch it).

Implemented today:

- **TCP command server** (asyncio) speaking newline-delimited JSON (JSON Lines).
- **Protocol**: `PRESS_START`, `PRESS_RENEW`, `PRESS_STOP` commands and `ACK`,
  `ERROR` responses, with `command_id`, `session_id` and a `state` field.
- **Exclusive session ownership**: the connection that starts a press is the only
  connection that may renew or stop it. Another connection cannot control the
  session even if it knows the session id. Disconnecting the owner releases the
  actuator, so reconnecting starts a new session.
- **Press lease (Phase 2 safety)**: a press is a lease the owning connection must
  renew with `PRESS_RENEW` every 250 ms. If no renewal arrives within the
  1000 ms lease timeout, the device releases the button itself. A press is also
  released at 3000 ms regardless of renewals — the maximum duration can never be
  extended by a client. All three values live in [`backend/remote_tap/config.py`](backend/remote_tap/config.py)
  and can be overridden with command-line flags.
- **Idempotent commands**: a retried `(session_id, command_id)` returns its
  cached response without re-running the action; if that cached response is no
  longer true (the press has since ended) the retry gets the current state
  instead, and is never executed again.
- **Error handling** for malformed JSON, unknown commands, missing/invalid
  fields, wrong sessions, expired leases and oversized lines.
- **UDP heartbeat / status channel (Phase 3)**: the device emits a small JSON
  datagram (heartbeat or status) every 1000 ms describing whether it is alive and
  what the actuator is doing. Datagrams carry a version, a device id, a **boot
  id**, an incrementing sequence number and a timestamp, so a receiver can detect
  lost, duplicate and out-of-order packets, tell a **device restart** apart from
  reordering, and decide when the device has gone stale. The channel is
  **outbound-only and read-only**: the device never accepts a UDP command, and
  nothing in a datagram can press, renew, stop or otherwise influence the
  actuator or the lease.
- **UDP monitoring client** (`python -m remote_tap.monitor`): a console tool that
  receives heartbeats and prints the device's status, sequence number, missing
  heartbeats, duplicates, out-of-order datagrams, restarts (a new boot id) and an
  "offline" notice when beats stop. It is a **development and protocol
  demonstration tool**, not part of the mobile product, and there is no graphical
  dashboard.
- **Browser demonstration interface (demo only, Phase 3)**: a local, loopback-only
  web dashboard (`python -m remote_tap.bridge`) with a press-and-hold power
  button, standing in for the future mobile app so the interaction can be shown
  and tested in a browser. A browser cannot open raw TCP or UDP sockets, so a
  small bridge process sits between the page and the device: the page talks to
  the bridge over **HTTP/WebSocket**, and the bridge talks to the device over
  the **existing** TCP command protocol and receives the **existing** UDP
  heartbeats. WebSocket is only the browser-to-bridge transport — it never
  replaces TCP, and every command still goes through the device's own session
  manager, lease and watchdog. It is a development demonstration, not part of
  the mobile product: loopback only, no authentication yet.
- **Simulated actuator**, an **interactive command-line client** and the
  **pytest suite** (226 tests).

Planned, **not implemented** yet (by design):

- **Authentication** ([TBD] in docs/architecture.md). Neither channel is
  authenticated today, and UDP datagrams are not authenticated or secure.
- **Raspberry Pi hardware**: GPIO / servo / mechanical parts.
- **The mobile application.**

The design documents live in [`docs/`](docs/).

## Target hardware (future)

| Component | Role |
| --- | --- |
| Mobile app (Expo / React Native / TypeScript) | User presses and holds the remote button |
| Device: Raspberry Pi Zero 2 W, Linux, Python | Receives commands, enforces safety, drives the actuator |
| Actuator (servo / relay / transistor) | Physically presses the PC power button |
| Tailscale | Private network between app and device, no public exposure |

## Repository layout

```
RemoteTap/
├── README.md              # this file
├── .gitignore             # ignore rules for Python, Node/Expo, secrets
├── requirements.txt       # Python dependencies (backend + tests)
├── LICENSE
├── docs/
│   ├── architecture.md    # components, layering, safety model
│   └── protocol.md        # wire protocol between app and device
├── backend/               # Python 3 asyncio device service
│   ├── pytest.ini         # pytest configuration
│   ├── remote_tap/
│   │   ├── app.py         # server entry point (TCP + UDP)
│   │   ├── client.py      # interactive command-line client (TCP)
│   │   ├── monitor.py     # UDP heartbeat monitor (development tool)
│   │   ├── config.py      # host, port, size limits, press-safety limits, UDP settings
│   │   ├── session.py     # actuator state, session ownership, idempotency, lease wiring
│   │   ├── net/           # tcp_server.py, udp_heartbeat.py
│   │   ├── protocol/      # messages.py, validate.py, udp_messages.py
│   │   ├── safety/        # watchdog.py (lease timer, maximum-duration timer)
│   │   ├── bridge/        # demo web bridge: device_link.py, press.py, status_hub.py, app.py
│   │   ├── web/           # demo dashboard assets: index.html, styles.css, app.js
│   │   └── actuator/      # base.py (interface), simulator.py
│   └── tests/             # pytest suite (protocol, actuator, session, safety, TCP, UDP, bridge)
└── mobile/                # Expo + React Native + TypeScript app (later phase)
```

Phases 1–3 create the backend only. `mobile/` arrives in a later phase, so no
empty placeholder folder is committed.

## Prerequisites

For the prototype phase (backend + tests):

- Python 3.11 or newer
- `pip`
- Git

For later phases:

- Node.js 20+ and Expo CLI (mobile app)
- Tailscale on both the developer machine and the device (needed only when we
  start testing across two real hosts)

## How to run the project

The prototype needs up to four terminals: the device service (TCP commands +
UDP heartbeats), the command client, the optional UDP status monitor, and the
optional browser demo.

> **Run every command from `backend/`, with the virtual environment active.**
> `remote_tap` is a package inside `backend/`, so `python -m remote_tap...` only
> resolves from there. From the repository root — or with the system Python
> instead of the venv — it fails with
> `No module named 'remote_tap'`. In each new terminal:
>
> ```powershell
> cd "path\to\RemoteTap\backend"
> .\.venv\Scripts\activate          # then use `python -m remote_tap...`
> ```
>
> Without activating, call the interpreter directly:
> `.\.venv\Scripts\python.exe -m remote_tap.app ...`.

**Setup (once, from `backend/`):**

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux / macOS
pip install -r ..\requirements.txt
```

**1. Terminal A - the device service** (cwd `backend/`, venv active):

```bash
python -m remote_tap.app --host 127.0.0.1 --port 8765
```

It prints the actuator transitions, the press-safety policy it enforces and the
UDP heartbeat settings:

```
RemoteTap device service
  listening : 127.0.0.1:8765 (TCP)
  actuator  : simulated - no hardware is driven
  lease     : renew every 250 ms, release after 1000 ms without one
  max press : 3000 ms, renewals cannot extend it
  heartbeat : 1000 ms to 127.0.0.1:8766 (UDP, read-only status)
  monitor   : python -m remote_tap.monitor --host 127.0.0.1 --port 8766
  client    : python -m remote_tap.client --host 127.0.0.1 --port 8765
  stop      : Ctrl+C
[actuator:sim] IDLE -> PRESSING
[actuator:sim] PRESSING -> IDLE
```

`--host` is loopback by default. To reach the service from another machine,
pass the address that machine can see (for example the Tailscale address), never
a public interface unless you know the network is private.

The three press timings can be overridden for experimentation, for example:

```bash
python -m remote_tap.app --renew-interval-ms 250 --lease-timeout-ms 1000 --max-press-ms 3000
```

`--lease-timeout-ms` must be greater than `--renew-interval-ms`, and
`--max-press-ms` must be at least `--lease-timeout-ms`; other combinations are
rejected at startup.

The UDP settings are configured **separately** from the TCP port:

```bash
python -m remote_tap.app --udp-dest-host 127.0.0.1 --udp-dest-port 8766 \
                         --heartbeat-interval-ms 1000 --stale-ms 3000 \
                         --device-id remotetap-01
```

`--heartbeat-interval-ms` must be between 1 and 60000 ms, and `--stale-ms` must
be at least twice the heartbeat interval (so one lost heartbeat does not make a
healthy device look offline); invalid combinations are rejected at startup.
Pass `--no-udp` to run TCP only — command handling is unaffected. Nothing here
hardcodes a development machine's address: every address is a flag, and the
device's UDP socket binds an ephemeral source port (`--udp-bind-port 0`, the
default) because it never receives.

**2. Terminal B - the command client** (cwd `backend/`, venv active):

```bash
python -m remote_tap.client --host 127.0.0.1 --port 8765
```

Then type `press`, `renew`, `stop`, `status`, `help` or `quit`. `press` starts a
hold and reports the lease policy the device advertised; the press is then
released by the device unless you keep sending `renew` before the lease timeout,
and it always ends at the maximum duration. `stop` or closing the client releases
the actuator immediately.

**3. Terminal C (optional) - the UDP status monitor** (cwd `backend/`, venv active):

```bash
python -m remote_tap.monitor --host 127.0.0.1 --port 8766
```

It listens for the device's heartbeats and prints each one it accepts, for
example:

```
[monitor] listening on 127.0.0.1:8766 (UDP), stale after 3000 ms
[monitor] seq=1 type=status state=IDLE session_active=no device=remotetap-01 boot=5f3c9a1e from ('127.0.0.1', 54321)
[monitor] seq=2 type=heartbeat state=IDLE session_active=no device=remotetap-01 boot=5f3c9a1e from ('127.0.0.1', 54321)
[monitor] seq=3 type=status state=PRESSING session_active=yes device=remotetap-01 boot=5f3c9a1e from ('127.0.0.1', 54321)
```

Press the button in Terminal B while watching Terminal C to see `state` move to
`PRESSING` and back. If you stop the device service, the monitor reports the
outage once:

```
[monitor] device OFFLINE (no status for 3000 ms; last seq=7)
```

Start the device service again and the monitor notices the restart by the new
boot id, resets that device's sequence tracking and ignores any late packet still
carrying the previous boot id:

```
[monitor] seq=1 type=status state=IDLE session_active=no device=remotetap-01 boot=8a7b6c5d from ('127.0.0.1', 54999)  !! new boot id: the device restarted, sequence tracking reset
[monitor] seq=9 from an earlier boot (boot=5f3c9a1e) ignored; superseded by boot '8a7b6c5d...'
```

Useful flags: `--stale-ms` (offline threshold), `--device-id` (ignore datagrams
from any other device) and `--verbose`. The monitor cannot command the device:
it only reads datagrams. The boot id identifies a run of the service for
correlation only — it is not a credential, and UDP datagrams are not
authenticated (see `docs/protocol.md` section 11).

**4. Terminal D (optional) - the browser demo** (cwd `backend/`, venv active):

```bash
python -m remote_tap.bridge --device-host 127.0.0.1 --device-port 8765
```

Then open **<http://127.0.0.1:8080>**. The bridge announces itself like this:

```
12:00:01 INFO    remote_tap.bridge.app: bridge ready: page on http://127.0.0.1:8080, device 127.0.0.1:8765, heartbeats on 127.0.0.1:8766
```

**Press and hold** the power button on the page: while you hold it, the bridge
keeps the device's press lease renewed over TCP; when you release — or the
pointer is cancelled, the window loses focus, or the tab is hidden — it sends
the stop. The page also shows the device connection, the actuator state
(`IDLE` / `PRESSING`), the heartbeat status (`ONLINE` / `OFFLINE`), the last
heartbeat and the sequence number.

The browser talks to the bridge over **HTTP and WebSocket**; it never opens a
TCP or UDP socket itself, and it is never trusted with the lease. The device's
own lease timeout, disconnect release and maximum press duration are unchanged
and still in force, so a closed page, a killed bridge or a stalled browser
cannot leave the button held down.

Because the bridge listens for heartbeats on the same port as the monitor, run
**either** Terminal C **or** Terminal D, not both. Pass `--no-udp` to run the
bridge without the status channel (commands are unaffected), or move the ports
with `--udp-port` / `--web-port`. Useful flags: `--web-host` (default
`127.0.0.1`; do not expose the demo beyond this machine — there is no
authentication), `--stale-ms` (should match the device's), `--device-id` and
`--verbose`.

**5. Tests, from `backend/`:**

```bash
pytest -q
```

**6. Mobile app (later phase), from `mobile/`:**

```bash
npm install
npx expo start
```

The app will point at the device's Tailscale address (for example
`100.x.y.z`), never at a public IP. During development both the app and the
backend can run on the same machine using the loopback address.

## Safety in one paragraph

The device — not the app — is the authority on how long the button may be held.
A press starts a bounded "hold lease": the app must keep renewing that lease with
`PRESS_RENEW` over TCP. If the renewals stop, the socket closes, or the maximum
press duration is reached, the device releases the button on its own, and the
client is never trusted to release safely. The UDP heartbeat is a **status
channel only** and is deliberately **not** an input to the lease: it is lossy,
unordered and unauthenticated, so it can never hold a press open. See
[architecture.md](docs/architecture.md) and [protocol.md](docs/protocol.md) for
the full model, with each rule marked implemented or planned.

Implemented today: the **disconnect release** (closing the owning connection
while pressing releases the actuator), **exclusive session ownership** (only the
connection that started a press may renew or stop it), the **lease timeout**
(1000 ms without a valid renewal → release), the **maximum press duration**
(3000 ms hard ceiling that no client message can extend), the **watchdog**
enforcing both, and the **read-only UDP status channel** (heartbeats with
sequence numbers and staleness detection). The browser demo does not change any
of this: it is an ordinary TCP client of the device (through the local bridge),
so it inherits the same release-on-disconnect, ownership and lease rules.
Authentication is not implemented, and the UDP datagrams are neither
authenticated nor secure.

## Documentation

- [docs/architecture.md](docs/architecture.md) — components, layering,
  networking topology, the TCP/UDP split and why UDP cannot touch the actuator,
  the safety model, non-goals, open questions. Each layer and safety invariant is
  marked implemented or planned. Section 4.3 explains the local demo bridge and
  why it adds no new safety mechanism.
- [docs/protocol.md](docs/protocol.md) — the wire protocol. Sections 3–10
  document the TCP command channel (`PRESS_START` / `PRESS_RENEW` /
  `PRESS_STOP` / `ACK` / `ERROR`, JSON Lines framing, command and session ids,
  error codes, and the press lease); section 11 documents the **implemented**
  UDP heartbeat datagram (format, sequence numbers, stale detection, why it
  cannot control the actuator, and its limitations); the handshake and
  authentication are marked as **planned and not implemented**.

## License

MIT — see [LICENSE](LICENSE).
