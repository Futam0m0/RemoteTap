"""Tests for the press lease: renewal, lease timeout and maximum duration.

The device, not the client, owns these limits, so every test here checks what
the backend does on its own when a client is slow, silent or lying.

The timings are in milliseconds rather than the production 250/1000/3000 ms, so
the whole file still runs quickly; they are far enough apart to absorb ordinary
scheduler jitter.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator, Callable

import pytest

from remote_tap import app
from remote_tap.actuator.base import ActuatorState
from remote_tap.actuator.simulator import SimulatedActuator
from remote_tap.config import PressLimits, ServerConfig
from remote_tap.net.tcp_server import TcpCommandServer
from remote_tap.protocol.messages import Ack, Command, CommandType, ErrorCode, ErrorMessage
from remote_tap.safety import LEASE_TIMEOUT, MAX_PRESS_EXCEEDED, PressWatchdog
from remote_tap.session import DeviceSession

from conftest import RawClient

CLIENT_A = "client-a"
CLIENT_B = "client-b"
SESSION_A = "session-a"
SESSION_B = "session-b"

#: A press that must be renewed about five times over its lifetime.
FAST_LIMITS = PressLimits(renew_interval_ms=10, lease_timeout_ms=60, max_press_ms=600)

#: One renewal period, plus slack for the event loop.
TICK = 0.02


async def send(
    session: DeviceSession,
    command_type: CommandType,
    *,
    client_id: str = CLIENT_A,
    session_id: str = SESSION_A,
    command_id: str = "c1",
):
    """Send one command to the session, as a connection would."""
    return await session.handle(
        Command(command_type, command_id, session_id), client_id
    )


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Wait for a device-side condition, to avoid fixed sleeps in tests."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def running_watchdog_tasks() -> list[asyncio.Task]:
    """Every watchdog task still alive on the running loop.

    Used to prove that a press which has ended leaves no background task behind.
    ``asyncio.all_tasks`` only returns tasks that have not finished, so an empty
    list means the timers really are gone.
    """
    alive = []
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        if getattr(coro, "__qualname__", "").startswith("PressWatchdog._run"):
            alive.append(task)
    return alive


@pytest.fixture
async def make_session() -> AsyncIterator[Callable[..., DeviceSession]]:
    """Factory for sessions with custom limits, all released after the test."""
    created: list[DeviceSession] = []

    def build(limits: PressLimits = FAST_LIMITS) -> DeviceSession:
        device = DeviceSession(SimulatedActuator(label="lease"), limits=limits)
        created.append(device)
        return device

    yield build

    for device in created:
        await device.release(reason="test_teardown")


# ----------------------------------------------------------------------
# The lease the owner has to renew
# ----------------------------------------------------------------------


async def test_a_press_that_is_never_renewed_is_released_by_the_device(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START)
    assert session.state is ActuatorState.PRESSING

    # The client is still connected but silent: the device lets go anyway.
    assert await wait_until(lambda: session.state is ActuatorState.IDLE)
    assert session.active_session_id is None
    assert session.owner_client_id is None


async def test_renewals_keep_the_press_alive_past_the_lease_timeout(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START)

    # Five lease timeouts' worth of renewals: the press must survive them all.
    for index in range(15):
        await asyncio.sleep(TICK)
        response = await send(session, CommandType.PRESS_RENEW, command_id=f"r{index}")
        assert isinstance(response, Ack)
        assert response.state == "PRESSING"

    assert session.state is ActuatorState.PRESSING
    assert session.active_session_id == SESSION_A


async def test_a_renewal_after_the_lease_expired_reports_lease_expired(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START, command_id="c1")
    assert await wait_until(lambda: session.state is ActuatorState.IDLE)

    response = await send(session, CommandType.PRESS_RENEW, command_id="c2")

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.LEASE_EXPIRED
    assert response.state == "IDLE"


async def test_a_stop_after_the_lease_expired_reports_lease_expired(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START, command_id="c1")
    assert await wait_until(lambda: session.state is ActuatorState.IDLE)

    response = await send(session, CommandType.PRESS_STOP, command_id="c2")

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.LEASE_EXPIRED


# ----------------------------------------------------------------------
# Who may renew
# ----------------------------------------------------------------------


async def test_only_the_owning_connection_may_renew(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START, client_id=CLIENT_A)

    response = await send(
        session,
        CommandType.PRESS_RENEW,
        client_id=CLIENT_B,
        session_id=SESSION_A,
        command_id="b1",
    )

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert response.state == "PRESSING"
    assert session.owner_client_id == CLIENT_A


async def test_a_renewal_with_the_wrong_session_id_is_refused(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START, session_id=SESSION_A)

    response = await send(
        session,
        CommandType.PRESS_RENEW,
        session_id=SESSION_B,
        command_id="c2",
    )

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert session.state is ActuatorState.PRESSING


async def test_a_renewal_without_any_press_is_not_a_lease_mistake(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()

    # Nothing has expired here, so this is an unknown session, not a late
    # renewal: the two must not be conflated.
    response = await send(session, CommandType.PRESS_RENEW)

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION


async def test_a_stranger_is_not_told_why_the_press_ended(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START, client_id=CLIENT_A)
    assert await wait_until(lambda: session.state is ActuatorState.IDLE)

    response = await send(
        session,
        CommandType.PRESS_RENEW,
        client_id=CLIENT_B,
        session_id=SESSION_A,
        command_id="b1",
    )

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION


# ----------------------------------------------------------------------
# The hard maximum duration
# ----------------------------------------------------------------------


async def test_the_maximum_duration_ends_a_press_that_is_renewed_diligently(
    make_session: Callable[..., DeviceSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    limits = PressLimits(renew_interval_ms=10, lease_timeout_ms=100, max_press_ms=140)
    session = make_session(limits)
    await send(session, CommandType.PRESS_START)

    with caplog.at_level(logging.WARNING):
        for index in range(20):
            await asyncio.sleep(TICK)
            if session.state is not ActuatorState.PRESSING:
                break
            # Renewed five times as often as the lease needs, so a lease timeout
            # can only mean these renewals were not accepted at all.
            await send(session, CommandType.PRESS_RENEW, command_id=f"r{index}")

    # 20 ticks of 20 ms against a 140 ms ceiling: it must have ended early, and
    # for the maximum-duration reason rather than a missed renewal.
    assert index < 19
    assert session.state is ActuatorState.IDLE
    assert session.active_session_id is None
    assert MAX_PRESS_EXCEEDED in caplog.text
    assert LEASE_TIMEOUT not in caplog.text


async def test_repeated_press_start_does_not_extend_the_maximum_duration(
    make_session: Callable[..., DeviceSession],
) -> None:
    # Spamming PRESS_START must not be a way to hold the button down forever,
    # so the press has to end inside the 24-tick window without any renewal.
    limits = PressLimits(renew_interval_ms=100, lease_timeout_ms=300, max_press_ms=300)
    session = make_session(limits)
    await send(session, CommandType.PRESS_START, command_id="c0")

    for index in range(1, 25):
        await asyncio.sleep(TICK)
        if session.state is not ActuatorState.PRESSING:
            break
        await send(session, CommandType.PRESS_START, command_id=f"s{index}")

    assert index < 24
    assert session.state is ActuatorState.IDLE


async def test_the_press_start_ack_advertises_the_lease_policy(
    make_session: Callable[..., DeviceSession],
) -> None:
    limits = PressLimits(renew_interval_ms=20, lease_timeout_ms=200, max_press_ms=900)
    session = make_session(limits)

    response = await send(session, CommandType.PRESS_START)

    assert isinstance(response, Ack)
    assert response.renew_interval_ms == 20
    assert response.max_press_ms == 900


# ----------------------------------------------------------------------
# The watchdog on its own
# ----------------------------------------------------------------------


def recording_watchdog(limits: PressLimits) -> tuple[PressWatchdog, list[str]]:
    """A watchdog that records the reasons it expired with."""
    reasons: list[str] = []

    async def on_expire(reason: str) -> None:
        reasons.append(reason)

    return PressWatchdog(on_expire, limits), reasons


async def test_the_watchdog_reports_a_missed_renewal_as_a_lease_timeout() -> None:
    watchdog, reasons = recording_watchdog(
        PressLimits(renew_interval_ms=10, lease_timeout_ms=40, max_press_ms=400)
    )
    await watchdog.arm()

    assert await wait_until(lambda: bool(reasons))

    assert reasons == [LEASE_TIMEOUT]
    assert watchdog.active is False


async def test_a_renewal_without_an_armed_press_creates_no_lease() -> None:
    watchdog, reasons = recording_watchdog(FAST_LIMITS)

    watchdog.renew()  # a stray renewal must not start timing anything

    assert watchdog.active is False
    await asyncio.sleep(0.1)
    assert reasons == []


async def test_disarming_stops_the_timers() -> None:
    watchdog, reasons = recording_watchdog(FAST_LIMITS)
    await watchdog.arm()
    assert watchdog.active is True

    await watchdog.disarm()

    assert watchdog.active is False
    await asyncio.sleep(0.1)
    assert reasons == []


async def test_disarming_an_unarmed_watchdog_is_safe() -> None:
    watchdog, _ = recording_watchdog(FAST_LIMITS)

    await watchdog.disarm()

    assert watchdog.active is False


# ----------------------------------------------------------------------
# The limits themselves
# ----------------------------------------------------------------------


def test_the_client_must_be_able_to_renew_before_the_lease_lapses() -> None:
    with pytest.raises(ValueError):
        PressLimits(renew_interval_ms=500, lease_timeout_ms=500)


def test_the_lease_may_not_outlive_the_maximum_press_duration() -> None:
    with pytest.raises(ValueError):
        PressLimits(renew_interval_ms=250, lease_timeout_ms=1000, max_press_ms=900)


def test_the_default_limits_are_the_documented_ones() -> None:
    limits = PressLimits()

    assert (
        limits.renew_interval_ms,
        limits.lease_timeout_ms,
        limits.max_press_ms,
    ) == (250, 1000, 3000)


def test_zero_and_negative_values_are_rejected_explicitly() -> None:
    # Each field is checked on its own, so a zero or negative value fails even
    # when the ratio rules would otherwise not notice it.
    with pytest.raises(ValueError):
        PressLimits(renew_interval_ms=0)
    with pytest.raises(ValueError):
        PressLimits(renew_interval_ms=-10)
    with pytest.raises(ValueError):
        PressLimits(lease_timeout_ms=0)
    with pytest.raises(ValueError):
        PressLimits(lease_timeout_ms=-1000)
    with pytest.raises(ValueError):
        PressLimits(max_press_ms=0)
    with pytest.raises(ValueError):
        PressLimits(max_press_ms=-3000)


def test_one_missed_renewal_must_still_be_tolerated() -> None:
    # A lease only 1 ms longer than the renewal interval leaves no room for an
    # obedient client to miss a single renewal, so it is refused.
    with pytest.raises(ValueError):
        PressLimits(renew_interval_ms=999, lease_timeout_ms=1000)

    # Twice the renewal interval is the accepted minimum.
    limits = PressLimits(renew_interval_ms=500, lease_timeout_ms=1000)
    assert limits.lease_timeout_ms == 2 * limits.renew_interval_ms


def test_an_unreasonably_long_press_is_rejected() -> None:
    # An hour-long press is almost certainly a mistake and must not be accepted
    # silently, even though it satisfies the ordering rules.
    with pytest.raises(ValueError):
        PressLimits(renew_interval_ms=250, lease_timeout_ms=1000, max_press_ms=3_600_000)


# ----------------------------------------------------------------------
# M1 — a stale cached response must never describe an ended press
# ----------------------------------------------------------------------


async def test_a_stale_cached_press_start_does_not_claim_a_press(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    first = await send(session, CommandType.PRESS_START, command_id="c1")
    assert isinstance(first, Ack)
    assert first.state == "PRESSING"

    # The device releases the press on its own when the lease lapses.
    assert await wait_until(lambda: session.state is ActuatorState.IDLE)

    # The client retries its original command id, not knowing the press ended.
    retry = await send(session, CommandType.PRESS_START, command_id="c1")

    assert isinstance(retry, ErrorMessage)
    assert retry.code is ErrorCode.LEASE_EXPIRED
    assert retry.state == "IDLE"
    assert session.state is ActuatorState.IDLE


async def test_a_stale_cached_renewal_does_not_claim_a_press(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START, command_id="c1")
    renewal = await send(session, CommandType.PRESS_RENEW, command_id="r1")
    assert isinstance(renewal, Ack)
    assert renewal.state == "PRESSING"

    assert await wait_until(lambda: session.state is ActuatorState.IDLE)

    # Replaying the cached renewal ACK would falsely report PRESSING.
    retry = await send(session, CommandType.PRESS_RENEW, command_id="r1")

    assert isinstance(retry, ErrorMessage)
    assert retry.code is ErrorCode.LEASE_EXPIRED
    assert retry.state == "IDLE"
    assert session.state is ActuatorState.IDLE


async def test_retrying_a_stale_press_start_never_starts_a_new_press(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START, command_id="c1")
    assert await wait_until(lambda: session.state is ActuatorState.IDLE)

    # Retrying the same command id repeatedly must never press the actuator
    # again: only a genuinely new command id may start a new press.
    for _ in range(5):
        retry = await send(session, CommandType.PRESS_START, command_id="c1")
        assert isinstance(retry, ErrorMessage)
        assert session.state is ActuatorState.IDLE
        assert session.active_session_id is None
        assert session.watchdog_active is False

    fresh = await send(session, CommandType.PRESS_START, command_id="c2")
    assert isinstance(fresh, Ack)
    assert fresh.state == "PRESSING"


async def test_a_cached_response_from_an_old_session_cannot_describe_a_newer_one(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(
        session,
        CommandType.PRESS_START,
        client_id=CLIENT_A,
        session_id=SESSION_A,
        command_id="a1",
    )
    assert await wait_until(lambda: session.state is ActuatorState.IDLE)

    # A different connection starts a fresh press on the now-idle device.
    started = await send(
        session,
        CommandType.PRESS_START,
        client_id=CLIENT_B,
        session_id=SESSION_B,
        command_id="b1",
    )
    assert isinstance(started, Ack)

    # A retries its old command: it must not be told its own session is
    # pressing (it is not) and must not learn B's session id.
    stale = await send(
        session,
        CommandType.PRESS_START,
        client_id=CLIENT_A,
        session_id=SESSION_A,
        command_id="a1",
    )

    assert isinstance(stale, ErrorMessage)
    assert SESSION_B not in stale.message
    assert session.owner_client_id == CLIENT_B
    assert session.state is ActuatorState.PRESSING


# ----------------------------------------------------------------------
# M2 — a new press after an automatic expiry
# ----------------------------------------------------------------------


async def test_a_new_press_after_an_expiry_owns_the_device(
    make_session: Callable[..., DeviceSession],
) -> None:
    limits = PressLimits(renew_interval_ms=20, lease_timeout_ms=100, max_press_ms=3000)
    session = make_session(limits)

    # A presses and lets the lease lapse.
    await send(
        session,
        CommandType.PRESS_START,
        client_id=CLIENT_A,
        session_id=SESSION_A,
        command_id="a1",
    )
    assert await wait_until(lambda: session.state is ActuatorState.IDLE)

    # B starts a new press, and becomes the owner.
    started = await send(
        session,
        CommandType.PRESS_START,
        client_id=CLIENT_B,
        session_id=SESSION_B,
        command_id="b1",
    )
    assert isinstance(started, Ack)
    assert session.owner_client_id == CLIENT_B
    assert session.active_session_id == SESSION_B

    # A cannot renew or stop B's press, even with its own session id.
    renew = await send(
        session,
        CommandType.PRESS_RENEW,
        client_id=CLIENT_A,
        session_id=SESSION_A,
        command_id="a2",
    )
    assert isinstance(renew, ErrorMessage)
    assert renew.code is ErrorCode.INVALID_SESSION

    stop = await send(
        session,
        CommandType.PRESS_STOP,
        client_id=CLIENT_A,
        session_id=SESSION_A,
        command_id="a3",
    )
    assert isinstance(stop, ErrorMessage)
    assert stop.code is ErrorCode.INVALID_SESSION
    assert session.owner_client_id == CLIENT_B

    # A's watchdog is gone: only B's remains, and B keeps renewing well past the
    # point where A's old lease would have expired again.
    assert len(running_watchdog_tasks()) == 1
    for index in range(6):
        await asyncio.sleep(TICK)
        assert session.state is ActuatorState.PRESSING
        assert session.owner_client_id == CLIENT_B
        renewed = await send(
            session,
            CommandType.PRESS_RENEW,
            client_id=CLIENT_B,
            session_id=SESSION_B,
            command_id=f"br{index}",
        )
        assert isinstance(renewed, Ack)


# ----------------------------------------------------------------------
# M5 — shutdown during a press
# ----------------------------------------------------------------------


async def test_shutdown_releases_the_press_and_leaves_no_watchdog(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START, command_id="c1")
    assert session.state is ActuatorState.PRESSING
    assert session.watchdog_active is True
    assert len(running_watchdog_tasks()) == 1

    released = await session.release(reason="shutdown")

    assert released is True
    assert session.state is ActuatorState.IDLE
    assert session.active_session_id is None
    assert session.owner_client_id is None
    assert session.watchdog_active is False
    await asyncio.sleep(0)  # let any task's finally clause run
    assert running_watchdog_tasks() == []


async def test_the_service_releases_the_actuator_before_stopping_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app-level shutdown order: release the actuator, then stop the server.

    Stops the actuator being left held if the process is interrupted while a
    press is active: the release has to happen before the listening socket and
    the client connections are torn down.
    """
    events: list[str] = []
    servers: list[TcpCommandServer] = []

    original_release = SimulatedActuator.release

    async def recording_release(self: SimulatedActuator) -> None:
        if self.state is ActuatorState.PRESSING:
            events.append("release")
        await original_release(self)

    original_start = TcpCommandServer.start

    async def recording_start(self: TcpCommandServer) -> None:
        await original_start(self)
        servers.append(self)

    original_stop = TcpCommandServer.stop

    async def recording_stop(self: TcpCommandServer) -> None:
        events.append("stop")
        await original_stop(self)

    monkeypatch.setattr(SimulatedActuator, "release", recording_release)
    monkeypatch.setattr(TcpCommandServer, "start", recording_start)
    monkeypatch.setattr(TcpCommandServer, "stop", recording_stop)

    limits = PressLimits(renew_interval_ms=10, lease_timeout_ms=200, max_press_ms=2000)
    running = asyncio.ensure_future(
        app.run(ServerConfig(host="127.0.0.1", port=0), "app-test", limits)
    )
    client: RawClient | None = None
    try:
        assert await wait_until(lambda: bool(servers), timeout=3.0)
        port = servers[0].port
        assert port is not None

        client = await RawClient.connect("127.0.0.1", port)
        await client.send_json(
            {"type": "PRESS_START", "command_id": "c1", "session_id": SESSION_A}
        )
        assert (await client.read_json())["state"] == "PRESSING"

        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
    finally:
        if client is not None:
            await client.close()
        await asyncio.sleep(0.1)  # let the connection handler wind down

    assert events[:2] == ["release", "stop"]


# ----------------------------------------------------------------------
# M6 — a duplicate renewal is inert
# ----------------------------------------------------------------------


async def test_a_duplicate_renewal_does_not_extend_the_lease(
    make_session: Callable[..., DeviceSession],
) -> None:
    session = make_session()
    await send(session, CommandType.PRESS_START, command_id="c1")
    await asyncio.sleep(TICK)

    await send(session, CommandType.PRESS_RENEW, command_id="r1")
    first_deadline = session.lease_deadline
    assert first_deadline > 0.0

    # The exact same command again — same command id — is answered from the
    # cache and must not push the lease forward a second time.
    await asyncio.sleep(TICK)
    replay = await send(session, CommandType.PRESS_RENEW, command_id="r1")

    assert isinstance(replay, Ack)
    assert session.lease_deadline == first_deadline


async def test_a_burst_of_renewals_does_not_bank_extra_lease_time(
    make_session: Callable[..., DeviceSession],
) -> None:
    limits = PressLimits(renew_interval_ms=10, lease_timeout_ms=100, max_press_ms=1000)
    session = make_session(limits)
    await send(session, CommandType.PRESS_START, command_id="c1")

    # Ten renewals back to back: each moves the deadline to 'now + 100 ms', it
    # does not stack, so the lease can never accumulate ten seconds' worth.
    for index in range(10):
        await send(session, CommandType.PRESS_RENEW, command_id=f"burst{index}")

    await asyncio.sleep(0.25)  # longer than one lease, shorter than ten

    assert session.state is ActuatorState.IDLE


# ----------------------------------------------------------------------
# Watchdog supersession and stale expiry attribution
# ----------------------------------------------------------------------


async def test_arming_again_safely_supersedes_the_previous_watchdog() -> None:
    limits = PressLimits(renew_interval_ms=10, lease_timeout_ms=80, max_press_ms=800)
    watchdog, reasons = recording_watchdog(limits)

    await watchdog.arm()
    await asyncio.sleep(0.05)  # part-way to the first lease deadline
    await watchdog.arm()  # supersedes it: the timers restart
    await asyncio.sleep(0.05)  # the first deadline has now passed, the second not

    # Had the first watchdog survived, it would have fired by now.
    assert reasons == []
    assert watchdog.active is True
    assert len(running_watchdog_tasks()) == 1

    await watchdog.disarm()


async def test_the_maximum_duration_wins_a_simultaneous_lease_expiry() -> None:
    # The lease and maximum deadlines coincide; the maximum-duration reason must
    # win so a diligent client cannot be blamed for a missed renewal.
    limits = PressLimits(renew_interval_ms=10, lease_timeout_ms=50, max_press_ms=50)
    watchdog, reasons = recording_watchdog(limits)
    await watchdog.arm()

    assert await wait_until(lambda: bool(reasons))

    assert reasons == [MAX_PRESS_EXCEEDED]


async def test_an_expired_session_is_not_attributed_to_a_newer_session(
    make_session: Callable[..., DeviceSession],
) -> None:
    limits = PressLimits(renew_interval_ms=20, lease_timeout_ms=100, max_press_ms=3000)
    session = make_session(limits)

    # A's press expires, and the device remembers why *for A*.
    await send(
        session,
        CommandType.PRESS_START,
        client_id=CLIENT_A,
        session_id=SESSION_A,
        command_id="a1",
    )
    assert await wait_until(lambda: session.state is ActuatorState.IDLE)

    # B starts a new press. A's ended press must not be blamed on B, and A's
    # expiry reason must not be handed to B.
    await send(
        session,
        CommandType.PRESS_START,
        client_id=CLIENT_B,
        session_id=SESSION_B,
        command_id="b1",
    )
    assert session.owner_client_id == CLIENT_B

    response = await send(
        session,
        CommandType.PRESS_RENEW,
        client_id=CLIENT_B,
        session_id=SESSION_A,
        command_id="b2",
    )

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert SESSION_A not in response.message
    assert SESSION_B not in response.message
    assert session.owner_client_id == CLIENT_B

