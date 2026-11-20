# RemoteTap — Protocol

> Status: **draft v0**, written before implementation. Every `[TBD]` is an open
> question for review. Numbers here are proposals, not decisions.

## 1. Design principles

1. **Human-readable on the wire.** Newline-delimited JSON (JSON Lines) over TCP
   and single JSON datagrams over UDP. We can debug with `nc` and a log file,
   and there is no binary codec to maintain in two languages.
2. **Two transports, two jobs.** TCP carries commands that must not be lost.
   UDP carries liveness traffic that is cheap and expendable.
3. **Explicitly versioned.** Every message carries `"v": 1`. A mismatched
   version is rejected at handshake rather than misparsed.
4. **Tiny messages.** No payload exceeds a few hundred bytes; nothing is ever
   streamed or chunked.
5. **Safety is not negotiable by the wire.** The client cannot request an
   unlimited hold, and message order cannot be used to defeat the lease.
6. **Forward-compatible parsing.** Unknown fields are ignored, so adding a field
   later does not break an older peer.

## 2. Transports

| Channel | Transport | Port (proposed) | Framing |
| --- | --- | --- | --- |
| Command channel | TCP | 8765 | One JSON object per line, terminated by `\n` (LF) |
| Heartbeat / state | UDP | 8766 | One JSON object per datagram, no delimiter |

Maximum line length: 4 KiB. A longer line is a protocol error and the
connection is closed. Encoding is UTF-8; the JSON must be a single object (not
an array).

Both ports listen on the device only, bound to the tailnet interface. Nothing
is exposed to the public internet.

## 3. Common envelope

Every message is a JSON object with these fields:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `v` | integer | yes | Protocol version. Currently `1`. |
| `type` | string | yes | Message type, see tables below. |
| `id` | string | TCP commands: yes | Client-generated request id, echoed in the reply so responses can be matched. |
| `ts` | integer | no | Sender's Unix timestamp in milliseconds. Informational only — never used for safety decisions. |

Timestamps are never trusted for safety. All safety timing is measured by the
device using its own monotonic clock, so a phone with a wrong clock cannot
extend a hold.

## 4. UDP heartbeat channel

The heartbeat carries two things: "the app is still here" and "here is the
device's current state". It is the input to the safety lease.

### 4.1 App → device: `ping`

```json
{"v":1,"type":"ping","device_id":"remotetap-01","ts":1763612400123,"holding":true}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `device_id` | string | yes | Which device this app is talking to; lets one app target several devices later. |
| `holding` | boolean | no | Whether the app believes it is currently holding the button. Used to cross-check device state, never to decide it. |

Sent every heartbeat interval while the button is held, and at a slower rate
when idle. **[TBD]** the idle rate; 5000 ms proposed.

### 4.2 Device → app: `beat`

```json
{"v":1,"type":"beat","device_id":"remotetap-01","ts":1763612400125,
 "state":"holding","session":"7f3c1a","hold_ms":2000,"max_hold_ms":10000,
 "lease_ms":3000,"busy":false}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `state` | string | `idle`, `holding`, or `fault` |
| `session` | string | Id of the active hold, or `null` when idle |
| `hold_ms` | integer | How long the current hold has lasted, or `0` |
| `max_hold_ms` | integer | The device's hard maximum hold duration |
| `lease_ms` | integer | Remaining lease time before the device auto-releases, or `0` |
| `busy` | boolean | Whether another client currently controls the device |

`beat` is sent as a reply to each `ping` and additionally once per heartbeat
interval while holding, so the app can display the device's state even if its
own packets are missing.

Note: heartbeats are **not** a command channel. Nothing in a UDP message can
change the actuator state. Only the TCP command channel can.

## 5. TCP command channel

### 5.1 Connection lifecycle

```
app                                      device
 │  TCP connect ───────────────────────────>│
 │  {"type":"hello", ...} ─────────────────>│   identify + version check
 │  <───────────────────── {"type":"welcome"}│
 │  {"type":"auth", "token":"..."} ────────>│   shared secret
 │  <─────────────────────── {"type":"ok"}   │   ready
 │  {"type":"press"} ──────────────────────>│
 │  <─────────── {"type":"state", holding}   │
 │  (heartbeats continue over UDP)           │
 │  {"type":"release"} ────────────────────>│
 │  <─────────── {"type":"state", released}  │
 │  close ─────────────────────────────────>│   also forces release if holding
```

Handshake rules:

- `hello` must be the first message; anything else closes the connection.
- If the version is unsupported the device replies `error` (`bad_version`) and
  closes.
- `auth` is required before any control message. Failure → `error`
  (`auth_failed`) and close.
- A client that already holds the actuator keeps it; a *new* connection's press
  is rejected with `busy` while the hold is active.

### 5.2 Command messages

| `type` | Fields | Reply |
| --- | --- | --- |
| `hello` | `device_id`, `client` (name), `v` | `welcome` `{device_id, session:null, max_hold_ms, lease_ms}` |
| `auth` | `token` | `ok` `{role:"controller"}` or `error` |
| `press` | `session` (optional, echo of a previous session) | `state` `{holding:true, session, max_hold_ms}` |
| `release` | `reason` (optional, client-side label) | `state` `{holding:false}` |
| `status` | — | `state` (current device state, no change) |
| `ping` | — | `ok` (TCP-level keep-alive; distinct from the UDP heartbeat) |

`press` semantics:

- On success the device presses the actuator, starts a session id, and starts
  both the max-hold timer and the lease timer.
- If already holding, the device returns the current `state` unchanged
  (idempotent, no extra hardware action).
- The requested hold duration is **not** a client parameter. The only hold
  length is the device's `max_hold_ms`.

`release` semantics:

- Releases the actuator if held, stops both timers, returns `state`.
- If already released, returns success. Releasing is always safe and always
  allowed, even for a client that does not own the hold.

### 5.3 Device-initiated messages

| `type` | Fields | When |
| --- | --- | --- |
| `welcome` | `device_id`, `v`, limits | In reply to `hello` |
| `ok` | `id` (echo), optional detail | Generic success reply |
| `state` | `holding`, `session`, `hold_ms`, `reason` | After every state change, and as a reply to `press`/`release`/`status` |
| `error` | `code`, `message`, `id` (echo when known) | Any failure; see §6 |

`state` is pushed to the controlling client when the device changes state
**without** being asked — for example when the watchdog releases the button.
That push is how the app learns about a `lease_timeout` or `max_hold_exceeded`
release.

## 6. Error codes

| Code | Meaning | Connection after |
| --- | --- | --- |
| `bad_envelope` | Not a JSON object, missing `v`/`type`, or line too long | closed |
| `bad_version` | Unsupported `v` | closed |
| `unauthorized` | Control message before a successful `auth` | closed |
| `auth_failed` | Wrong token | closed |
| `busy` | Another session currently holds the actuator | open |
| `not_holding` | A release arrived when nothing was held — informational | open |
| `bad_field` | Field present but wrong type or out of range | open (ignored) |
| `internal` | Unexpected device-side failure | closed |

## 7. Safety behaviour on the wire

These are device rules, stated here so the app's expectations are unambiguous:

1. A hold is limited to `max_hold_ms`. At that point the device releases and
   sends `state` with `reason: "max_hold_exceeded"`.
2. If no UDP `ping` arrives for `lease_ms` while holding, the device releases
   and sends `state` with `reason: "lease_timeout"`.
3. If the TCP control connection closes while holding, the device releases
   (`reason: "connection_lost"`).
4. On device shutdown the actuator is released (`reason: "shutdown"`).
5. The app's release message is a *request*, not a guarantee. It is one of
   several ways a hold can end, and never the only one.

Release reasons: `client_release`, `max_hold_exceeded`, `lease_timeout`,
`connection_lost`, `shutdown`, `fault`.

## 8. Versioning

- `v` is an integer and increments only for incompatible changes.
- Adding an optional field is **not** a version change; receivers must ignore
  unknown fields.
- The device advertises its version in `welcome`. The app is expected to check
  compatibility itself and show a clear error rather than sending unknown
  messages.

## 9. Open questions

1. **Ports** 8765/8766 proposed — acceptable, or do you prefer others?
2. **Framing** newline-delimited JSON proposed. Acceptable for the prototype, or
   would you rather length-prefixed frames?
3. **Auth model** one shared `token` in the `auth` message. Enough for the
   prototype, or does the handshake need a nonce/challenge so the token is not
   sent in cleartext repeatedly (it is inside Tailscale, but still on the wire)?
4. **Idle ping rate** 5000 ms proposed. Fine, or do you want the heartbeat only
   while holding?
5. **`session` semantics on reconnect** — if the app reconnects while the device
   is still holding (heartbeats were still arriving), should the app be able to
   resume ownership, or should any reconnect force a release for simplicity?
6. **Multiple devices** — `device_id` is in the protocol already; confirm that
   the prototype only ever has one device and this is just future-proofing.
