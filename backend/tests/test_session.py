"""Tests for device-side command handling and session ownership.

These run without any networking: the session is called directly with an opaque
``client_id``, which is what a connection supplies in the real server.
"""

from __future__ import annotations

import pytest

from remote_tap.actuator.simulator import SimulatedActuator
from remote_tap.protocol.messages import Ack, Command, CommandType, ErrorCode, ErrorMessage
from remote_tap.session import DeviceSession

CLIENT_A = "client-a"
CLIENT_B = "client-b"
SESSION_A = "session-a"
SESSION_B = "session-b"


async def start_press(
    session: DeviceSession,
    *,
    client_id: str = CLIENT_A,
    session_id: str = SESSION_A,
    command_id: str = "c1",
):
    """Send PRESS_START from ``client_id``."""
    return await session.handle(
        Command(CommandType.PRESS_START, command_id, session_id), client_id
    )


async def stop_press(
    session: DeviceSession,
    *,
    client_id: str = CLIENT_A,
    session_id: str = SESSION_A,
    command_id: str = "c2",
):
    """Send PRESS_STOP from ``client_id``."""
    return await session.handle(
        Command(CommandType.PRESS_STOP, command_id, session_id), client_id
    )


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


async def test_press_start_presses_the_actuator_and_records_the_owner(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    response = await start_press(session)

    assert isinstance(response, Ack)
    assert response.state == "PRESSING"
    assert response.command_id == "c1"
    assert actuator.state.value == "PRESSING"
    assert session.active_session_id == SESSION_A
    assert session.owner_client_id == CLIENT_A


async def test_the_owner_can_stop_the_press(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await start_press(session)

    response = await stop_press(session)

    assert isinstance(response, Ack)
    assert response.state == "IDLE"
    assert actuator.state.value == "IDLE"
    assert session.active_session_id is None
    assert session.owner_client_id is None


async def test_press_start_twice_from_the_owner_actuates_once(
    session: DeviceSession,
    actuator: SimulatedActuator,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await start_press(session, command_id="c1")
    capsys.readouterr()

    response = await start_press(session, command_id="c2")

    assert isinstance(response, Ack)
    assert response.state == "PRESSING"
    assert capsys.readouterr().out.count("IDLE -> PRESSING") == 0
    assert actuator.state.value == "PRESSING"


# ----------------------------------------------------------------------
# Ownership
# ----------------------------------------------------------------------


async def test_another_connection_cannot_start_a_press(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A)

    response = await start_press(
        session, client_id=CLIENT_B, session_id=SESSION_B, command_id="b1"
    )

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert response.state == "PRESSING"
    assert actuator.state.value == "PRESSING"
    assert session.owner_client_id == CLIENT_A


async def test_another_connection_cannot_stop_even_with_the_session_id(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A)

    # Same session id, different connection: still not allowed to control it.
    response = await stop_press(
        session, client_id=CLIENT_B, session_id=SESSION_A, command_id="b1"
    )

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert actuator.state.value == "PRESSING"
    assert session.active_session_id == SESSION_A
    assert session.owner_client_id == CLIENT_A


async def test_another_connection_does_not_learn_the_session_id_from_the_error(
    session: DeviceSession,
) -> None:
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A)

    response = await start_press(
        session, client_id=CLIENT_B, session_id=SESSION_B, command_id="b1"
    )

    assert isinstance(response, ErrorMessage)
    assert SESSION_A not in response.message


async def test_the_owner_cannot_relabel_its_own_press_with_a_new_session(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A)

    response = await start_press(
        session, client_id=CLIENT_A, session_id=SESSION_B, command_id="c2"
    )

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert session.active_session_id == SESSION_A
    assert actuator.state.value == "PRESSING"


async def test_the_owner_cannot_stop_with_the_wrong_session_id(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A)

    response = await stop_press(
        session, client_id=CLIENT_A, session_id=SESSION_B, command_id="c2"
    )

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert actuator.state.value == "PRESSING"


async def test_a_new_connection_can_press_after_the_owner_released(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A)
    await stop_press(session, client_id=CLIENT_A, session_id=SESSION_A)

    # Reconnecting means starting a new session, which is allowed.
    response = await start_press(
        session, client_id=CLIENT_B, session_id=SESSION_B, command_id="b1"
    )

    assert isinstance(response, Ack)
    assert response.state == "PRESSING"
    assert actuator.state.value == "PRESSING"
    assert session.owner_client_id == CLIENT_B
    assert session.active_session_id == SESSION_B


async def test_press_stop_without_an_active_press_is_rejected(
    session: DeviceSession,
) -> None:
    response = await stop_press(session)

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert response.state == "IDLE"


# ----------------------------------------------------------------------
# Release on disconnect
# ----------------------------------------------------------------------


async def test_release_if_client_releases_only_the_owning_connection(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A)

    assert await session.release_if_client(CLIENT_B) is False
    assert actuator.state.value == "PRESSING"

    assert await session.release_if_client(CLIENT_A) is True
    assert actuator.state.value == "IDLE"
    assert session.active_session_id is None
    assert session.owner_client_id is None


async def test_release_if_client_is_a_no_op_when_idle(session: DeviceSession) -> None:
    assert await session.release_if_client(CLIENT_A) is False


async def test_release_ignores_ownership(session: DeviceSession, actuator: SimulatedActuator) -> None:
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A)

    assert await session.release(reason="shutdown") is True
    assert actuator.state.value == "IDLE"
    assert await session.release(reason="shutdown") is False


# ----------------------------------------------------------------------
# Duplicate command ids
# ----------------------------------------------------------------------


async def test_duplicate_command_id_from_the_same_connection_replays_the_response(
    session: DeviceSession,
    actuator: SimulatedActuator,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = await start_press(session, session_id=SESSION_A, command_id="c1")
    capsys.readouterr()

    replay = await start_press(session, session_id=SESSION_A, command_id="c1")

    assert replay == first
    assert capsys.readouterr().out == ""  # the actuator was not actuated again
    assert actuator.state.value == "PRESSING"


async def test_duplicate_command_id_replays_errors_too(session: DeviceSession) -> None:
    first = await stop_press(session, command_id="c1")

    replay = await stop_press(session, command_id="c1")

    assert first == replay
    assert isinstance(replay, ErrorMessage)
    assert replay.code is ErrorCode.INVALID_SESSION


async def test_two_sessions_can_safely_use_the_same_command_id(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    # SESSION_A presses and stops using command ids c1/c2.
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A, command_id="c1")
    await stop_press(session, client_id=CLIENT_A, session_id=SESSION_A, command_id="c2")

    # SESSION_B is a different session, so reusing the same command ids must be
    # evaluated on its own merits rather than answered from SESSION_A's cache.
    response = await start_press(
        session, client_id=CLIENT_B, session_id=SESSION_B, command_id="c1"
    )

    assert isinstance(response, Ack)
    assert response.state == "PRESSING"
    assert response.command_id == "c1"
    assert actuator.state.value == "PRESSING"
    assert session.active_session_id == SESSION_B


async def test_a_cached_response_is_never_replayed_to_another_connection(
    session: DeviceSession, actuator: SimulatedActuator
) -> None:
    await start_press(session, client_id=CLIENT_A, session_id=SESSION_A, command_id="c1")

    # Identical (session_id, command_id), different connection: this must be
    # judged as a new request and rejected, not answered with A's cached ACK.
    response = await start_press(
        session, client_id=CLIENT_B, session_id=SESSION_A, command_id="c1"
    )

    assert isinstance(response, ErrorMessage)
    assert response.code is ErrorCode.INVALID_SESSION
    assert actuator.state.value == "PRESSING"
    assert session.owner_client_id == CLIENT_A


async def test_the_duplicate_cache_is_bounded(actuator: SimulatedActuator) -> None:
    session = DeviceSession(actuator, duplicate_cache_size=1)
    await stop_press(session, command_id="c1")  # rejected: nothing is pressing
    await start_press(session, command_id="c2")  # evicts c1 from the cache

    # c1 is no longer cached, so it is executed again - and now it succeeds,
    # because c2 started a press under this session.
    response = await stop_press(session, command_id="c1")

    assert isinstance(response, Ack)
    assert response.state == "IDLE"
    assert actuator.state.value == "IDLE"
