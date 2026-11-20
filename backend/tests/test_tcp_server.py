"""End-to-end tests over a real TCP socket.

The server runs in-process on an ephemeral loopback port, so these tests cover
the whole path: socket bytes -> validation -> session -> actuator -> JSON reply.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

import pytest

from remote_tap.actuator.simulator import SimulatedActuator
from remote_tap.net.tcp_server import TcpCommandServer
from remote_tap.session import DeviceSession

from conftest import RawClient

SESSION_A = "session-a"
SESSION_B = "session-b"


def command(command_type: str, command_id: str, session_id: str = SESSION_A) -> dict:
    """Build a command payload."""
    return {
        "type": command_type,
        "command_id": command_id,
        "session_id": session_id,
    }


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    """Wait for a server-side condition, to avoid fixed sleeps in tests."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_a_client_can_connect(server: TcpCommandServer, client: RawClient) -> None:
    assert server.is_running is True
    assert server.port is not None


async def test_press_start_is_acknowledged(
    client: RawClient, actuator: SimulatedActuator
) -> None:
    await client.send_json(command("PRESS_START", "c1"))

    response = await client.read_json()

    assert response == {"type": "ACK", "command_id": "c1", "state": "PRESSING"}
    assert actuator.state.value == "PRESSING"


async def test_press_stop_is_acknowledged(
    client: RawClient, actuator: SimulatedActuator
) -> None:
    await client.send_json(command("PRESS_START", "c1"))
    await client.read_json()

    await client.send_json(command("PRESS_STOP", "c2"))
    response = await client.read_json()

    assert response == {"type": "ACK", "command_id": "c2", "state": "IDLE"}
    assert actuator.state.value == "IDLE"


async def test_press_and_release_print_actuator_transitions(
    client: RawClient, capsys: pytest.CaptureFixture[str]
) -> None:
    await client.send_json(command("PRESS_START", "c1"))
    await client.read_json()
    await client.send_json(command("PRESS_STOP", "c2"))
    await client.read_json()

    printed = capsys.readouterr().out
    assert "IDLE -> PRESSING" in printed
    assert "PRESSING -> IDLE" in printed


async def test_several_messages_in_one_write_are_all_handled(
    client: RawClient, actuator: SimulatedActuator
) -> None:
    # One write, two messages: the server must split on the newline delimiter
    # instead of treating the whole chunk as a single message.
    payload = (
        json.dumps(command("PRESS_START", "c1")) + "\n"
        + json.dumps(command("PRESS_STOP", "c2")) + "\n"
    )
    await client.send_bytes(payload.encode("utf-8"))

    first = await client.read_json()
    second = await client.read_json()

    assert first["command_id"] == "c1"
    assert first["state"] == "PRESSING"
    assert second["command_id"] == "c2"
    assert second["state"] == "IDLE"
    assert actuator.state.value == "IDLE"


async def test_a_message_split_across_writes_is_handled_once_complete(
    client: RawClient, session: DeviceSession
) -> None:
    message = json.dumps(command("PRESS_START", "c1")).encode("utf-8")

    await client.send_bytes(message[:12])
    await asyncio.sleep(0.05)

    assert session.state.value == "IDLE", "a partial line must not be executed"

    await client.send_bytes(message[12:] + b"\n")
    response = await client.read_json()

    assert response["type"] == "ACK"
    assert response["state"] == "PRESSING"


async def test_invalid_json_returns_an_error_and_keeps_the_connection(
    client: RawClient, session: DeviceSession
) -> None:
    await client.send_bytes(b"this is not json\n")

    error = await client.read_json()

    assert error["type"] == "ERROR"
    assert error["code"] == "BAD_JSON"
    assert error["command_id"] is None
    assert error["state"] == "IDLE"

    # The connection is still usable afterwards.
    await client.send_json(command("PRESS_START", "c1"))
    assert (await client.read_json())["type"] == "ACK"


async def test_unknown_command_returns_an_error(client: RawClient) -> None:
    await client.send_json(command("LAUNCH_ROCKET", "c1"))

    error = await client.read_json()

    assert error["type"] == "ERROR"
    assert error["code"] == "UNKNOWN_COMMAND"
    assert error["command_id"] == "c1"


async def test_missing_field_returns_an_error(client: RawClient) -> None:
    await client.send_json({"type": "PRESS_START", "command_id": "c1"})

    error = await client.read_json()

    assert error["code"] == "MISSING_FIELD"
    assert error["command_id"] == "c1"


async def test_oversized_message_is_rejected_and_the_connection_closed(
    client: RawClient,
) -> None:
    oversized = b'{"type":"PRESS_START","padding":"' + b"x" * 5000

    await client.send_bytes(oversized)

    error = await client.read_json()
    assert error["code"] == "LINE_TOO_LONG"

    with pytest.raises(asyncio.IncompleteReadError):
        await client.read_json()


async def test_duplicate_command_id_is_not_executed_twice(
    client: RawClient, actuator: SimulatedActuator, capsys: pytest.CaptureFixture[str]
) -> None:
    await client.send_json(command("PRESS_START", "c1"))
    first = await client.read_json()
    capsys.readouterr()

    await client.send_json(command("PRESS_START", "c1"))
    second = await client.read_json()

    assert first == second
    assert capsys.readouterr().out == ""
    assert actuator.state.value == "PRESSING"


async def test_rejected_session_does_not_change_the_actuator(
    client: RawClient, actuator: SimulatedActuator
) -> None:
    await client.send_json(command("PRESS_STOP", "c1", SESSION_B))

    error = await client.read_json()

    assert error["code"] == "INVALID_SESSION"
    assert error["state"] == "IDLE"
    assert actuator.state.value == "IDLE"


async def test_a_second_client_cannot_take_over_an_active_press(
    client: RawClient,
    make_client: Callable[[], Awaitable[RawClient]],
    actuator: SimulatedActuator,
) -> None:
    await client.send_json(command("PRESS_START", "c1", SESSION_A))
    assert (await client.read_json())["state"] == "PRESSING"

    other = await make_client()
    await other.send_json(command("PRESS_START", "c1", SESSION_B))
    error = await other.read_json()

    assert error["code"] == "INVALID_SESSION"
    assert actuator.state.value == "PRESSING"


async def test_disconnect_releases_the_actuator(
    client: RawClient,
    session: DeviceSession,
    actuator: SimulatedActuator,
) -> None:
    await client.send_json(command("PRESS_START", "c1"))
    assert (await client.read_json())["state"] == "PRESSING"

    await client.close()

    assert await wait_until(lambda: actuator.state.value == "IDLE")
    assert session.active_session_id is None


async def test_the_server_keeps_serving_after_a_client_disconnects(
    client: RawClient,
    make_client: Callable[[], Awaitable[RawClient]],
) -> None:
    await client.send_json(command("PRESS_START", "c1"))
    await client.read_json()
    await client.close()

    reconnect = await make_client()
    await reconnect.send_json(command("PRESS_START", "c2"))

    response = await reconnect.read_json()

    assert response["type"] == "ACK"
    assert response["state"] == "PRESSING"


async def test_state_is_shared_across_connections(
    client: RawClient,
    make_client: Callable[[], Awaitable[RawClient]],
    session: DeviceSession,
) -> None:
    other = await make_client()

    await client.send_json(command("PRESS_START", "c1", SESSION_A))
    await client.read_json()

    assert session.state.value == "PRESSING"
    # The second connection sees the same device state, without asking.
    await other.send_json(command("PRESS_STOP", "c2", SESSION_A))
    assert (await other.read_json())["state"] == "IDLE"
