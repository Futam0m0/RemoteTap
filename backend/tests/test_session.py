"""Tests for device-side command handling.

These run without any networking: the session is called directly with
already-validated commands.
"""

from __future__ import annotations

import pytest

from remote_tap.actuator.simulator import SimulatedActuator
from remote_tap.protocol.messages import Ack, Command, CommandType, ErrorCode, ErrorMessage
from remote_tap.session import DeviceSession

SESSION_A = "session-a"
SESSION_B = "session-b"


def press_start(command_id: str, session_id: str = SESSION_A) -> Command:
    return Command(CommandType.PRESS_START, command_id, session_id)


def press_stop(command_id: str, session_id: str = SESSION_A) -> Command:
    return Command(CommandType.PRESS_STOP, command_id, session_id)


async def test_press_start_presses_the_actuator(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    response = await session.handle(press_start("c1"))

    assert isinstance(response, Ack)
    assert response.state == "PRESSING"
    assert response.command_id == "c1"
    assert actuator.state.value == "PRESSING"
    assert session.active_session_id == SESSION_A


async def test_press_stop_releases_the_actuator(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await session.handle(press_start("c1"))

    response = await session.handle(press_stop("c2"))

    assert isinstance(response, Ack)
    assert response.state == "IDLE"
    assert actuator.state.value == "IDLE"
    assert session.active_session_id is None


async def test_press_start_twice_for_the_same_session_actuates_once(
    session: DeviceSession, actuator: SimulatedActuator, capsys: pytest.CaptureFixture[str]
) -> None:
    await session.handle(press_start("c1"))
    capsys.readouterr()

    response = await session.handle(press_start("c2"))

    assert isinstance(response, Ack)
    assert response.state == "PRESSING"
    assert capsys.readouterr().out.count("IDLE -> PRESSING") == 0
    assert actuator.state.value == "PRESSING"


async def test_a_second_session_cannot_press_while_busy(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await session.handle(press_start("c1", SESSION_A))

    response = await session.handle(press_start("c2", SESSION_B))

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert response.state == "PRESSING"
    assert session.active_session_id == SESSION_A
    assert actuator.state.value == "PRESSING"


async def test_press_stop_from_another_session_is_rejected(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await session.handle(press_start("c1", SESSION_A))

    response = await session.handle(press_stop("c2", SESSION_B))

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert actuator.state.value == "PRESSING"


async def test_press_stop_without_an_active_press_is_rejected(
    session: DeviceSession,
) -> None:
    response = await session.handle(press_stop("c1"))

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert response.state == "IDLE"


async def test_duplicate_command_id_replays_the_first_response(
    session: DeviceSession, actuator: SimulatedActuator, capsys: pytest.CaptureFixture[str]
) -> None:
    first = await session.handle(press_start("c1"))
    capsys.readouterr()

    replay = await session.handle(press_start("c1"))

    assert replay == first
    assert capsys.readouterr().out == ""  # the actuator was not actuated again


async def test_duplicate_command_id_replays_errors_too(
    session: DeviceSession,
) -> None:
    first = await session.handle(press_stop("c1"))

    replay = await session.handle(press_stop("c1"))

    assert first == replay
    assert isinstance(replay, ErrorMessage)


async def test_the_same_command_id_in_another_session_is_not_a_duplicate(
    session: DeviceSession,
) -> None:
    await session.handle(press_start("c1", SESSION_A))

    # SESSION_B reuses SESSION_A's command id: that must not be treated as a
    # retry, so the request is evaluated on its own merits and rejected.
    response = await session.handle(press_start("c1", SESSION_B))

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION


async def test_the_duplicate_cache_is_bounded(actuator: SimulatedActuator) -> None:
    session = DeviceSession(actuator, duplicate_cache_size=1)
    await session.handle(press_stop("c1"))  # rejected: nothing is pressing
    await session.handle(press_start("c2"))  # evicts c1 from the cache

    # c1 is no longer cached, so it is executed again - and now it succeeds.
    response = await session.handle(press_stop("c1"))

    assert isinstance(response, Ack)
    assert response.state == "IDLE"


async def test_release_if_session_releases_only_the_owning_session(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await session.handle(press_start("c1", SESSION_A))

    assert await session.release_if_session(SESSION_B) is False
    assert actuator.state.value == "PRESSING"

    assert await session.release_if_session(SESSION_A) is True
    assert actuator.state.value == "IDLE"
    assert session.active_session_id is None


async def test_release_if_session_is_a_no_op_when_idle(session: DeviceSession) -> None:
    assert await session.release_if_session(SESSION_A) is False
