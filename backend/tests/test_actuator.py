"""Tests for the simulated actuator."""

from __future__ import annotations

import pytest

from remote_tap.actuator.simulator import SimulatedActuator


async def test_starts_idle(actuator: SimulatedActuator) -> None:
    assert actuator.state.value == "IDLE"
    assert actuator.is_pressing is False


async def test_press_changes_state_and_prints_the_transition(
    actuator: SimulatedActuator, capsys: pytest.CaptureFixture[str]
) -> None:
    await actuator.press()

    assert actuator.state.value == "PRESSING"
    assert actuator.is_pressing is True
    assert "IDLE -> PRESSING" in capsys.readouterr().out


async def test_release_changes_state_and_prints_the_transition(
    actuator: SimulatedActuator, capsys: pytest.CaptureFixture[str]
) -> None:
    await actuator.press()
    capsys.readouterr()

    await actuator.release()

    assert actuator.state.value == "IDLE"
    assert "PRESSING -> IDLE" in capsys.readouterr().out


async def test_pressing_twice_actuates_only_once(
    actuator: SimulatedActuator, capsys: pytest.CaptureFixture[str]
) -> None:
    await actuator.press()
    first = capsys.readouterr().out

    await actuator.press()
    second = capsys.readouterr().out

    assert first.count("IDLE -> PRESSING") == 1
    assert second == ""
    assert actuator.state.value == "PRESSING"


async def test_releasing_while_idle_is_a_no_op(
    actuator: SimulatedActuator, capsys: pytest.CaptureFixture[str]
) -> None:
    await actuator.release()

    assert actuator.state.value == "IDLE"
    assert capsys.readouterr().out == ""
