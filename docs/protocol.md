# RemoteTap — Protocol

This document describes the wire protocol between the mobile app and the device.

**Status.** Sections 3 to 10 and section 11 document what is **implemented
today**: a TCP command channel carrying newline-delimited JSON (three commands
and two responses, plus the Phase 2 press lease) and a read-only UDP heartbeat /
status channel. Section 12 onwards describes features that are **planned and not
yet implemented** — the handshake and authentication. Planned material is
labelled as such and must not be relied on by any client today.

**Two transports, two jobs.** TCP is the **only** channel that can change the
actuator: it carries `PRESS_START` / `PRESS_RENEW` / `PRESS_STOP` and is the sole
authority on press state, ownership and safety. UDP is a **status-only** channel:
the device emits heartbeats describing its own state so a monitoring client can
watch it without holding a TCP connection open. **Nothing in a UDP datagram can
press, renew, stop or otherwise influence the actuator, the lease or the
watchdog**, and the device accepts no commands over UDP.

**Not authenticated.** There is no authentication on either channel yet
(section 12). UDP datagrams in particular are plain JSON on the wire: they are
neither authenticated nor encrypted, and anyone able to reach the port could
forge them. A forged heartbeat can at most mislead a *monitor*; it can never
change the actuator, because the UDP path has no route into the session.

---

## 1. Design principles

1. **Human-readable on the wire.** Newline-delimited JSON (JSON Lines). It can
   be debugged with `nc` and a log file, and there is no binary codec to
   maintain in two languages.
2. **Explicitly versioned.** The UDP datagram format carries a version field
   `v` (currently `1`), and a receiver rejects a version it does not understand
   rather than guessing. The TCP command channel has no version field yet; one is
   reserved for the planned handshake (section 12) but is not transmitted today.
3. **Tiny messages.** No payload is expected to exceed a few hundred bytes;
   nothing is streamed or chunked.
4. **Safety is not negotiable by the wire.** A client cannot request an unlimited
   hold, and message order cannot be used to defeat the lease: the device
   measures every deadline with its own clock, and the maximum press duration
   can never be extended by anything the client sends.
5. **Forward-compatible parsing.** Unknown fields are ignored, so adding a field
   later does not break an older peer.

## 2. Transports

| Channel | Transport | Port | Framing | Status |
| --- | --- | --- | --- | --- |
| Command channel | TCP | 8765 (device default) | One JSON object per line, terminated by `\n` (LF) | **Implemented** |
| Heartbeat / status | UDP | 8766 (monitoring-client default) | One JSON object per datagram | **Implemented** (section 11) |

The device binds loopback (`127.0.0.1`) by default. To be reachable from another
host the operator passes the machine's Tailscale address explicitly; nothing is
exposed to the public internet unless the operator chooses to.

The TCP command port and the UDP heartbeat port are **configured separately**
(`--port` versus `--udp-dest-port`) and never share a number.

## 3. Framing — JSON Lines (implemented)

TCP is a byte stream with **no message boundaries**, so the protocol defines one:

* Every message is a single JSON object serialised on one line and terminated
  by a single newline character (`\n`, byte `0x0A`).
* Encoding is UTF-8. The device emits ASCII-safe JSON (non-ASCII characters are
  escaped), so a client may decode as UTF-8 without surprises.
* A line that is empty or only whitespace is ignored as a keep-alive.
* A line longer than **4096 bytes** is rejected with an `ERROR`
  (`LINE_TOO_LONG`) and the connection is then closed: once the boundary is
  untrustworthy the rest of the stream cannot be parsed safely.
* Exactly one JSON object per line. Arrays, strings, numbers and other top-level
  JSON values are rejected with `NOT_AN_OBJECT`.

Because the delimiter is a newline, **a message must never contain a raw
newline**. JSON string escapes (`\n`) are fine; a literal newline inside the
serialised object is not.

## 4. Message envelope

### 4.1 Client → device (commands)

Every command is a JSON object with these fields:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | Command name. One of `PRESS_START`, `PRESS_RENEW`, `PRESS_STOP`. |
| `command_id` | string | yes | Client-generated id for this request. Non-empty. Used to match a response and to make retries idempotent (section 8). |
| `session_id` | string | yes | Client-generated id for this hold. Non-empty. Identifies the press session (section 7). |

Example:

```json
{"type":"PRESS_START","command_id":"c1","session_id":"session-a"}
```

### 4.2 Device → client (responses)

Every response has a `type` of `ACK` or `ERROR` (section 6). Responses are
written on the same connection the command arrived on.

## 5. Commands (implemented)

| `type` | Meaning | Success reply |
| --- | --- | --- |
| `PRESS_START` | Ask the device to press and hold the actuator. Creates the session if none is active, and makes this connection the session's exclusive owner. | `ACK` with `state: "PRESSING"` and the lease policy |
| `PRESS_RENEW` | Keep the active press alive for another lease timeout. Only the owning connection may do this. | `ACK` with `state: "PRESSING"` |
| `PRESS_STOP` | Ask the device to release the actuator. Only the owning connection may do this. | `ACK` with `state: "IDLE"` |

`PRESS_START` semantics:

* If the actuator is idle, the device presses it, records `session_id`, makes
  this connection the owner, and starts the lease and maximum-duration timers.
  Reply: `ACK`, `state: "PRESSING"`.
* If the actuator is **already pressing**, the device does **not** actuate twice.
  Whether the reply is an `ACK` or an `ERROR` depends on ownership (section 7).
* Re-sending `PRESS_START` for a press this connection already owns is a no-op: it
  answers with the current state and the lease policy. It is deliberately **not**
  a renewal and does **not** restart the timers, so a client cannot hold the
  button down indefinitely by spamming `PRESS_START` instead of renewing.
* The requested hold duration is **not** a client parameter. The device decides
  it: the lease (section 10.2) and the maximum press duration (section 10.3) are
  configured on the device and cannot be changed over the wire.

`PRESS_RENEW` semantics:

* Valid only while this connection owns the active session and the `session_id`
  matches. It pushes the lease deadline forward by the lease timeout; it can
  **never** extend the maximum press duration.
* Every renewal must use a **fresh `command_id`**. A repeated `command_id` is
  treated as a retry and answered from the cache (section 8); it does **not**
  extend the lease a second time.
* If there is no active press, or this connection does not own it, or the
  `session_id` does not match, the device replies `ERROR` and changes nothing.
* If this connection's own press already ended because a limit was reached, the
  reply is `ERROR` with `LEASE_EXPIRED` (section 10.4).
* A **lost** renewal acknowledgement does not necessarily mean the renewal
  failed: the device may have applied it and only the reply was lost. Retry the
  **same** `command_id` once to learn the outcome, then carry on with fresh ids.
* If a renewal is answered with `LEASE_EXPIRED` or `INVALID_SESSION`, the client
  must consider the press **ended**: stop renewing and release the UI.

`PRESS_STOP` semantics:

* If this connection owns the active session and the `session_id` matches, the
  device releases the actuator, stops the timers, clears the session, and replies
  `ACK` with `state: "IDLE"`.
* If the connection does not own the press, or there is no press, the device does
  nothing and replies `ERROR` (`INVALID_SESSION`, or `LEASE_EXPIRED` when this
  connection's own press was already ended by a safety limit). Idle is safe.

## 6. Responses (implemented)

A response is either an `ACK` or an `ERROR`. Both carry `type`; the remaining
fields differ.

### 6.1 `ACK`

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | string | `"ACK"` |
| `command_id` | string | Echo of the command's `command_id`. |
| `state` | string | Actuator state **after** the command was applied: `"IDLE"` or `"PRESSING"`. |
| `renew_interval_ms` | integer | Present on a successful `PRESS_START` or `PRESS_RENEW`: how often the device expects a renewal. |
| `max_press_ms` | integer | Present on a successful `PRESS_START` or `PRESS_RENEW`: the hard maximum press duration. |

The two lease fields are omitted from replies that end a press, where no lease is
running any more.

```json
{"type":"ACK","command_id":"c1","state":"PRESSING","renew_interval_ms":250,"max_press_ms":3000}
```

### 6.2 `ERROR`

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | string | `"ERROR"` |
| `code` | string | Machine-readable error code (section 9). |
| `message` | string | Human-readable explanation. Not stable for parsing. |
| `command_id` | string or null | Echo of the request's `command_id` when it could be read, otherwise `null`. |
| `state` | string | Present when the device could determine it: the unchanged actuator state, `"IDLE"` or `"PRESSING"`. Omitted for message-level errors detected before the state is known. |

```json
{"type":"ERROR","code":"INVALID_SESSION","message":"this connection does not own the active press","command_id":"c2","state":"PRESSING"}
```

### 6.3 The `state` field

`state` always reports the device's own view, never the client's intent. It is
`"IDLE"` or `"PRESSING"` (the two values of the actuator state machine). A
client should treat `state` as authoritative and update its UI from it rather
than assuming its command succeeded.

There is no unsolicited `state` push yet: every response is a reply to a command
on the same connection, **including** the releases the device performs by itself.
A client whose press is released by a limit learns about it from its next
`PRESS_RENEW` or `PRESS_STOP` (section 10.4), or by noticing that its lease is
gone. Device-initiated notifications are planned (section 13).

## 7. Session ids and connection ownership (implemented)

A **press session** is the period during which the actuator is held. It is
identified by the client-supplied `session_id`.

Ownership rules, all enforced by the device:

1. The connection that creates a session (the one whose `PRESS_START` pressed
   the actuator) is the session's **exclusive owner**.
2. Only the owning connection may send `PRESS_STOP` for that session.
3. Only the owning connection may renew that session's lease with `PRESS_RENEW`.
4. A **different connection cannot control an existing session, even if it knows
   the `session_id`.** Such a request is rejected with `INVALID_SESSION`, and
   the error deliberately does **not** reveal the active `session_id` or the
   reason another connection's press ended.
5. When the owning connection disconnects, the device releases the actuator
   (reason `client_disconnected`). Reconnecting therefore requires a **new**
   session: the released session is gone.

Consequences for a client:

* Do not treat `session_id` as a bearer token; it grants nothing on its own.
* After reconnecting, always start a fresh session with a new `session_id`.
* Keep the owning connection open for as long as you want to hold: closing it
  releases the press immediately, before any lease timer matters.

## 8. Command ids and idempotency (implemented)

`command_id` exists so a client can safely retry a command whose response it did
not see.

* The device caches each response keyed by **(session_id, command_id)**.
* If the **same connection** sends the **same `(session_id, command_id)`** pair
  again, the device returns the cached response **without executing the action
  again**. This applies to stored errors too.
* Two different sessions may safely reuse the same `command_id`: because the key
  includes `session_id`, their entries do not collide.
* A cached response is **never** replayed to another connection. Replay requires
  that the original sender is the same connection.
* The cache is bounded (256 entries by default) and evicts the oldest entry
  first. After eviction a repeated command is executed again.
* Renewals are ordinary commands: give each `PRESS_RENEW` its own `command_id`,
  otherwise a repeated one is answered from the cache and does **not** extend the
  lease.
* A cached response is replayed only while it still tells the **truth**. Once a
  press has ended — a safety limit released it, or the owning connection went
  away — a cached `state: "PRESSING"` is stale. The device then answers a retry
  of that old command with the **current** state instead of the cached reply:
  `LEASE_EXPIRED` for the connection whose own press the device ended, and
  `INVALID_SESSION` for anything else (which reveals nothing about another
  connection's press). A cached response must never claim that an expired
  session is still active.
* A stale command is **never executed again**. Retrying an old `PRESS_START`
  therefore cannot start a second press, and retrying an old `PRESS_RENEW`
  cannot resurrect an expired press or report one as active. A cached response
  from an old session is never used to describe a newer one.
* A client that wants to act again — press, renew or stop — must use a **new
  `command_id`**. Reusing an old one after the state has moved on returns the
  current-state error above, not an action.

## 9. Error codes (implemented)

| Code | Raised when | Connection after |
| --- | --- | --- |
| `BAD_JSON` | The line is not valid UTF-8 or not valid JSON. | open |
| `NOT_AN_OBJECT` | The JSON is valid but not a single object. | open |
| `MISSING_FIELD` | A required field (`type`, `command_id`, `session_id`) is absent. | open |
| `INVALID_FIELD` | A field is present but the wrong type, or empty. | open |
| `UNKNOWN_COMMAND` | `type` is not a known command. | open |
| `LINE_TOO_LONG` | A line exceeded 4096 bytes. | **closed** |
| `INVALID_SESSION` | The command refers to a session this connection does not own, or there is no active press to act on. | open |
| `LEASE_EXPIRED` | The owning connection sent a renewal or a stop for a press that the device had already ended because a lease or maximum-duration limit was reached (section 10.4). | open |
| `INTERNAL` | An unexpected device-side failure while handling a valid command. | open |

"Connection after: open" means the device replies with the `ERROR` and then
continues reading the same connection, so a client can correct itself and carry
on. Only `LINE_TOO_LONG` ends the connection.

`INVALID_SESSION` and `LEASE_EXPIRED` can also answer a **retry** of a command
the device already handled. When the cached reply for a replayed
`(session_id, command_id)` is no longer true — most often because a safety limit
has since released the press — the device answers with the *current* state
instead of the stale reply: `LEASE_EXPIRED` for the connection whose own press
the device ended, `INVALID_SESSION` for anything else. The stale command is never
executed again (section 8).

## 10. The press lease (implemented)

A press is a **lease**, not a promise: the device holds the button down only for
as long as the owning connection keeps asking it to. Nothing the client sends can
make a press last longer than the device's own limits.

### 10.1 The three limits

| Limit | Default | Meaning |
| --- | --- | --- |
| Renewal interval | 250 ms | How often the client should send `PRESS_RENEW`. Advertised to the client; **not** enforced by the device. |
| Lease timeout | 1000 ms | How long a press survives without a renewal. Silence past this deadline → the device releases. |
| Maximum press duration | 3000 ms | Hard ceiling on one continuous press, reached with or without renewals. |

All three are configured on the device (`--renew-interval-ms`,
`--lease-timeout-ms`, `--max-press-ms`; see `backend/remote_tap/config.py`) and
the advertised values are returned in every successful `PRESS_START` and
`PRESS_RENEW` ACK. They are never taken from the client.

There is also a hard upper bound on the maximum press duration: a value above
**10000 ms** is rejected at startup (`MAX_ALLOWED_MAX_PRESS_MS`), so a
misconfiguration such as a one-hour press is refused loudly rather than accepted
silently.

The device validates all three at startup, and refuses to run if any rule fails:

1. each value must be **greater than zero** (a zero or negative value is
   rejected explicitly, naming the offending field);
2. the lease must tolerate **at least one whole missed renewal**:
   `lease_timeout_ms >= 2 * renew_interval_ms` (with the defaults, the client is
   asked to renew every 250 ms and the lease survives 1000 ms, i.e. four missed
   renewals);
3. the lease may not outlive the maximum: `lease_timeout_ms <= max_press_ms`;
4. the maximum may not exceed the 10000 ms safety bound above.

### 10.2 How the lease works

* `PRESS_START` (accepted) arms two deadlines measured by the device's own
  monotonic clock: the **lease deadline** (`now + lease_timeout_ms`) and the
  **maximum deadline** (`now + max_press_ms`).
* Each accepted `PRESS_RENEW` moves the **lease deadline** to
  `now + lease_timeout_ms`. It never touches the maximum deadline.
* Re-sending `PRESS_START` for the same session is **not** a renewal: it neither
  extends the lease nor restarts the maximum. A client that wants to keep holding
  must renew.
* `PRESS_STOP`, a disconnect of the owning connection, and device shutdown all
  clear both deadlines.

### 10.3 What the device does when a deadline is reached

The device releases the actuator itself, clears the session, and logs the reason:

| Reason | Cause |
| --- | --- |
| `lease_timeout` | The lease deadline passed: the owning connection stopped renewing in time. |
| `max_press_exceeded` | The maximum deadline passed, however diligently the client renewed. |

If both deadlines fall due at the same moment, the maximum duration wins. There
is no message the client can send to prevent this, and no way to re-arm a press
that has already ended: the session is gone and the actuator is idle.

### 10.4 How a client learns its press ended

There is no unsolicited notification yet. The owner finds out from the next
command it sends:

* it gets `ERROR` with code `LEASE_EXPIRED`, whose `message` names the reason
  (`lease_timeout` or `max_press_exceeded`), and `state: "IDLE"`;
* a *different* connection asking about the same session gets the ordinary
  `INVALID_SESSION`, so it learns nothing about someone else's press.

A client that has not sent anything since its press expired will simply see no
further `ACK`s until it tries to renew or stop.

### 10.5 What a client must do

* After `PRESS_START`, remember `renew_interval_ms` and `max_press_ms` from the
  ACK instead of hardcoding them.
* Send `PRESS_RENEW` (with a **fresh `command_id`** each time) more often than
  `renew_interval_ms` while the user holds the button.
* Expect the press to end at `max_press_ms` even if renewals are perfect, and
  treat `LEASE_EXPIRED` as "the hold is over, release the UI".
* Replaying a **duplicate** renewal (the same `command_id` again) does **not**
  extend the lease: a periodic renewal must always carry a new `command_id`.
* A **lost renewal acknowledgement** does not necessarily mean the renewal
  failed. Retry the **same** `command_id` once to learn the outcome, then carry
  on with fresh ids. Do not send a new `command_id` to "retry" a renewal you are
  unsure about — that would extend the lease twice if the first one landed.
* If a renewal is answered with `LEASE_EXPIRED` or `INVALID_SESSION`, the client
  **must consider the press ended**: stop renewing and release the UI. Never
  assume a press is still active because a cached reply once said `PRESSING`.

---

## 11. UDP heartbeat / status channel (implemented)

The device periodically emits a small JSON datagram describing its own state, so
a monitoring client can see that the device is alive and what the actuator is
doing **without** holding a TCP command connection open.

This channel is **read-only and one-directional**:

* the device is the only sender; the monitoring client is the only receiver;
* the device's UDP socket is outbound-only and **binds no listening port by
  default** (`bind_port = 0`), so it always sends from an ephemeral source port;
* any datagram that does arrive at the device socket is logged and dropped
  (section 11.4);
* the heartbeat reads the session's read-only properties to build each snapshot
  and holds no actuator state of its own.

### 11.1 Why UDP at all

TCP exists because a command must not be lost. UDP exists because a *status
update* does not have to be reliable: it is small, frequent, and worthless once
it is superseded. Sending a heartbeat over UDP means the device can report its
state with no connection, no handshake and no per-message acknowledgement, and a
dropped heartbeat simply costs one update. The two channels demonstrate two
transports with genuinely different responsibilities rather than one transport
used twice.

### 11.2 Datagram format

One JSON object per datagram, UTF-8, no newline and no framing of any kind.

```json
{"v":1,"type":"heartbeat","device_id":"remotetap-01","boot_id":"5f3c9a1e7b2d4c8f9a0e1b2c3d4e5f60","seq":42,"ts":1763612400123,"status":"ok","state":"IDLE","session_active":false}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `v` | integer | Protocol version of this datagram format. Currently `1`. |
| `type` | string | `"heartbeat"` (a periodic liveness tick) or `"status"` (the device noticed its own state changed). |
| `device_id` | string | Which device sent this; lets one monitor watch several. Configured with `--device-id` (default `remotetap-01`). **Stable across restarts.** |
| `boot_id` | string | Identifies one **run** of the device service (32 hex characters). Generated fresh when the service starts and constant for that run, so it changes after a device restart. See section 11.3. |
| `seq` | integer | 1-based sequence number, incremented for every datagram the device actually sends. **Restarts at `1`** when the service restarts. |
| `ts` | integer | Sender's wall-clock time in milliseconds. **Display only** — see section 11.5. |
| `status` | string | The device's own service status. Today the only value is `"ok"`. |
| `state` | string | Actuator state: `"IDLE"` or `"PRESSING"`. |
| `session_active` | boolean | Whether a press session is currently active. |

`device_id` names the *device*; `boot_id` names the *run*. The device id is the
same every time the device starts, which is exactly why the boot id is needed:
without it, a restarted device's fresh `seq = 1` is indistinguishable from a very
old packet (section 11.3). The `ts` is **not** used as a substitute for boot
identity — clocks are unsynchronised (section 11.5).

`type` distinguishes the two message types:

* **`heartbeat`** — a routine liveness tick, sent at the configured interval.
  The device state is unchanged since the previous datagram.
* **`status`** — sent when the device notices its own state moved (a press
  started, a press was released by a command, a lease or the maximum duration).
  It is also the type of the **first** datagram after startup, because the
  monitor has seen nothing yet. This makes a press visible one interval sooner
  than an ordinary heartbeat would.

Both types carry the same fields and the same meaning; `type` only tells the
receiver *why* it was sent.

### 11.3 Sequence numbers, boot ids and freshness

* `seq` starts at `1` **for each run of the service** and increases by exactly
  one for each datagram the device hands to the OS. A send that **fails** is not
  counted, so a failed send does not leave a gap that looks like packet loss.
* `boot_id` identifies that run. It is generated once when the service starts —
  a fresh 32-character hex id — and never changes while the process lives, so a
  restarted device is always a *different* boot id (section 11.2).
* A receiver keys its sequence bookkeeping on the pair **`(device_id, boot_id)`**,
  not on the device id alone. For a datagram that arrives:
  * **same boot, `seq > last`** → **accepted**; any numbers skipped are counted
    as missing (`missing = seq - last - 1`), because UDP may have dropped or
    reordered them;
  * **same boot, `seq == last`** → **duplicate**, ignored so it cannot overwrite
    the displayed status with an older copy;
  * **same boot, `seq < last`** → **out of order** (a late datagram), ignored;
  * **a boot id not seen before for that device** → the device **restarted**. The
    receiver starts fresh sequence tracking for the new boot, so the restarted
    device's `seq = 1` is **accepted** rather than misread as a flood of
    out-of-order packets. The boot it was watching before is marked
    **superseded**: any late datagram still carrying that old boot id is ignored
    outright — whatever its sequence number — so a previous process's packets
    cannot overwrite the current device status.

  The boot-id rule is the fix for the restart problem: a monitor that remembered
  only "device `remotetap-01` is at sequence 500" would reject the restarted
  device's whole fresh sequence. Keying on `(device_id, boot_id)` makes a restart
  a clean, recognisable event instead.

* Receiving a duplicate or out-of-order datagram from the **current** boot still
  proves the device is alive, so the "last seen" timer is reset even when the
  payload is ignored. A **superseded** datagram does not, because the boot that
  sent it has already gone.

**Stale detection is measured by the receiver, with its own monotonic clock.**
The datagram's `ts` is never used for it (section 11.5). If nothing valid has
arrived for longer than the stale timeout the monitor shows the device as
**offline**; the notice is printed once per outage, and cleared by the next
valid datagram. The stale timeout defaults to 3000 ms and must be at least twice
the heartbeat interval, so a single lost heartbeat cannot make a healthy device
look offline.

**What is guaranteed, and what is not.** Without authentication (section 12) the
boot id is a *correlation* identifier, not a credential:

* Within a single observed run the receiver reliably tells a restart from
  reordering, and it ignores late packets from a boot it has already replaced —
  that does not depend on the sender being honest.
* But a boot id the receiver has **never seen** cannot be distinguished from a
  restart. A forged or long-delayed datagram that carries an unknown boot id is
  treated as a new instance and may become the currently displayed status. This
  is unavoidable while datagrams are unauthenticated, and is why the status
  channel must never be trusted for anything that matters — it never influences
  the actuator in any case (section 11.4).

### 11.4 Why UDP cannot control the actuator

The isolation is structural, not a policy that could be forgotten:

* the heartbeat service is a **separate object** from the TCP command server and
  shares only the session's *read-only* properties (`state`,
  `active_session_id`);
* it never calls `DeviceSession.handle`, and no code path exists from an inbound
  datagram to a command — `_HeartbeatProtocol.datagram_received` only logs and
  drops;
* it never presses, renews or releases, and never touches the lease or the
  watchdog;
* a send failure is caught and logged, so a broken UDP channel cannot disturb
  the TCP command channel.

Consequently: losing every UDP packet cannot interrupt or extend a press, a
forged datagram cannot start or stop one, and the TCP session manager remains
the sole authority on actuator state. Only `PRESS_START` / `PRESS_RENEW` /
`PRESS_STOP` over TCP change the actuator, and only TCP may move the lease
deadline or the watchdog deadline.

### 11.5 Known UDP limitations

* **No delivery, ordering or uniqueness guarantee.** Datagrams may be lost,
  duplicated or reordered, and the protocol assumes all three. Sequence numbers
  and receiver-side timers are the only defence.
* **Clocks are not synchronised.** The two peers are different machines with
  different clocks, so `ts` cannot be used to judge staleness; a receiver must
  time datagrams itself.
* **No datagram is acknowledged or retried.** A heartbeat is deliberately
  fire-and-forget; there is no TCP-style acknowledgement or retransmission
  machinery for routine beats.
* **Blocked UDP is silent.** A firewall that drops UDP looks exactly like a dead
  device; the monitor will report the device as offline even though TCP works.
* **Boot ids identify runs, not identities.** A `boot_id` lets a receiver spot a
  restarted device and ignore its previous run's late packets, but it is not a
  credential: a receiver cannot prove a datagram really came from the boot it
  names, and a datagram carrying an unknown boot id is indistinguishable from a
  restart (section 11.3).
* **Size.** A datagram is a few hundred bytes at most. The receiver rejects
  anything above 512 bytes as malformed rather than trying to reassemble it.
* **No authentication or encryption.** See the note at the top of this document
  and section 12.

### 11.6 Monitoring client (development tool)

A small CLI client demonstrates the channel. It is a **development and protocol
demonstration tool, not a product interface**, and it deliberately has no GUI:

```
python -m remote_tap.monitor --host 0.0.0.0 --port 8766
```

It prints each datagram it accepts (sequence number, type, actuator state,
session flag, device id, a short form of the boot id and the sender), reports
gaps in the sequence, names duplicate, out-of-order and superseded datagrams, and
prints a single "device OFFLINE" line when heartbeats stop arriving. When a
datagram arrives with a boot id it has not seen before for that device, it notes
that the **device restarted** and restarts that device's sequence tracking. It
cannot command the device.

---

*Everything below this line is **planned** and has **not been implemented**.
It is recorded so the future design is unambiguous; no client today may depend
on it.*

---

## 12. Planned: handshake and authentication (not implemented)

A `hello` / `welcome` / `auth` handshake is planned so that the device can
version-check the client and require a shared secret before any control message.
Until it exists, the TCP command channel is reachable by anyone who can reach
the port; the only protection is the private network (Tailscale) and the
loopback-by-default bind address.

A version field (integer, currently `1`) is reserved for this handshake on the
**TCP** channel. It is defined in the code as `PROTOCOL_VERSION` but is **not**
enforced or transmitted on TCP today. (The UDP datagram format has its own,
separate `v` field, which *is* transmitted and checked — section 11.2.)

## 13. Planned: notifications and remaining release reasons (not implemented)

The lease, the maximum press duration and the disconnect release **are**
implemented (sections 7 and 10). What remains for a later phase:

1. **Device-initiated notifications over TCP.** The device will push a `state`
   message when it changes state **without** being asked (for example when a
   limit releases the button), so the app learns about a `lease_timeout` or
   `max_press_exceeded` release immediately instead of on its next command.
   Today there are no unsolicited TCP messages. (The UDP `status` datagram of
   section 11.2 reports state changes, but it is a status channel a monitor
   reads — it is not a command or notification channel the app may act on.)
2. **Structured release reasons in the logs.** `lease_timeout`,
   `max_press_exceeded`, `client_disconnected` and `shutdown` are logged today;
   `client_release` and `fault` are the planned names for the remaining paths.

**Not planned: a UDP-fed lease.** The UDP channel will *not* be used to renew or
extend the press lease. Heartbeats are lossy, ordering-free and unauthenticated,
so allowing them to influence a safety deadline would weaken the guarantee the
lease exists to provide. The lease stays renewed over TCP only, and losing UDP
packets can neither interrupt nor extend a press.

## 14. Open questions

1. **UDP ports** — the heartbeat uses 8766 by default (the monitoring client
   listens there; the device sends from an ephemeral port). Acceptable, and is a
   separate configuration enough?
2. **Heartbeat rate** — 1000 ms implemented. Should the device also send an
   immediate `status` when a press ends *and* at a different idle rate?
3. **Auth model** — one shared `token` in an `auth` message. Enough, or does the
   handshake need a nonce/challenge so the token is not sent in cleartext
   repeatedly (it is inside Tailscale, but still on the wire)?
4. **`session` semantics on reconnect** — today any reconnect requires a new
   session. Should a future reconnect while still holding be able to resume
   ownership, or is forcing a new session the right permanent rule?
5. **Multiple devices** — `device_id` is implemented in the heartbeat, and the
   monitor can filter on it. Confirm the prototype only ever has one device and
   this is just future-proofing.
6. **Lease timings** — 250 / 1000 / 3000 ms are now the implemented defaults. Are
   they right for a real press (long enough for Wi-Fi jitter, short enough that a
   frozen phone cannot hold the button for long)?
