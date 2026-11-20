# RemoteTap

RemoteTap is a **remote physical power-button actuator**.

It lets a user press and hold a button in a mobile app, which causes a remotely
located hardware device to physically press and hold a desktop PC's power
button.

The desktop PC does not need to be powered on for RemoteTap to work, because
the actuator sits *outside* the PC and drives the power button (or the
motherboard's power header) directly.

## Current status

**Prototype phase — software only.**

The hardware layer is deliberately not implemented yet. During this phase we
build and test the complete networking, session and safety logic against a
**simulated actuator** that logs what a real actuator would do.

Not implemented yet (by design):

- Raspberry Pi GPIO / servo / mechanical parts
- The mobile application
- UDP heartbeat, the safety lease and the watchdog (Phase 2)
- Authentication ([TBD] in docs/architecture.md)

What exists today: **Phase 1** — an asyncio TCP command server, a simulated
actuator, an interactive command-line client and the pytest suite. The design
documents live in [`docs/`](docs/).

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
│   │   ├── app.py         # server entry point
│   │   ├── client.py      # interactive command-line client
│   │   ├── config.py      # host, port, size limits
│   │   ├── session.py     # device state and command handling
│   │   ├── net/           # tcp_server.py
│   │   ├── protocol/      # messages.py, validate.py
│   │   └── actuator/      # base.py (interface), simulator.py
│   └── tests/             # pytest suite (protocol, actuator, session, TCP)
└── mobile/                # Expo + React Native + TypeScript app (later phase)
```

Phase 1 creates the backend only. `mobile/` arrives in a later phase, so no
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

Phase 1 needs two terminals: one for the device service, one for the client.
Both commands are run from `backend/`.

**Setup (once):**

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux / macOS
pip install -r ..\requirements.txt
```

**1. Terminal A - the device service:**

```bash
python -m remote_tap.app --host 127.0.0.1 --port 8765
```

It prints the actuator transitions, so every press and release is visible:

```
RemoteTap device service
  listening : 127.0.0.1:8765 (TCP)
  actuator  : simulated - no hardware is driven
[actuator:sim] IDLE -> PRESSING
[actuator:sim] PRESSING -> IDLE
```

`--host` is loopback by default. To reach the service from another machine,
pass the address that machine can see (for example the Tailscale address), never
a public interface unless you know the network is private.

**2. Terminal B - the client:**

```bash
python -m remote_tap.client --host 127.0.0.1 --port 8765
```

Then type `press`, `stop`, `status`, `help` or `quit`. Presses are held until you
send `stop` or close the client; closing the client releases the actuator.

**3. Tests, from `backend/`:**

```bash
pytest -q
```

**4. Mobile app (later phase), from `mobile/`:**

```bash
npm install
npx expo start
```

The app will point at the device's Tailscale address (for example
`100.x.y.z`), never at a public IP. During development both the app and the
backend can run on the same machine using the loopback address.

## Safety in one paragraph

The device — not the app — is the authority on how long the button may be held.
A press starts a bounded "hold lease"; the app must keep renewing that lease
with heartbeats; if the heartbeat stops, the socket closes, or the maximum hold
time is reached, the device releases the button on its own. The client is never
trusted to release safely. See
[architecture.md](docs/architecture.md) for the full model.

Of that model, **Phase 1 implements only the disconnect release**: closing the
client connection while pressing releases the actuator. The lease, the maximum
hold time and the watchdog arrive with the UDP heartbeat in a later phase, so
today a client can hold the button for as long as it likes.

## Documentation

- [docs/architecture.md](docs/architecture.md) — components, layering,
  networking topology, safety model, non-goals, open questions.
- [docs/protocol.md](docs/protocol.md) — the draft wire protocol. Note that the
  message names in Phase 1 differ from that draft (`PRESS_START` /
  `PRESS_STOP` / `ACK` / `ERROR` instead of `press` / `release` / `state`);
  the draft has not been updated to match yet.

## License

MIT — see [LICENSE](LICENSE).
