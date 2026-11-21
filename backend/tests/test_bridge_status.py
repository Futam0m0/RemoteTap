"""Tests for the bridge's UDP status hub.

The hub must be the only listener on the heartbeat port and must reuse the
shipped monitor rather than reimplementing sequence handling. These tests send
real datagrams to a real socket and read the hub's view back.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from remote_tap.bridge.status_hub import StatusHub
from remote_tap.config import DEFAULT_DEVICE_ID
from remote_tap.protocol.udp_messages import HEARTBEAT, StatusDatagram, encode_datagram


async def _send(port: int, message: StatusDatagram) -> None:
    """Send one datagram from a throwaway socket, as the device would."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, remote_addr=("127.0.0.1", port)
    )
    try:
        transport.sendto(encode_datagram(message))
    finally:
        transport.close()


def _datagram(
    *,
    sequence: int = 1,
    boot_id: str = "boot-a",
    device_id: str = DEFAULT_DEVICE_ID,
    state: str = "PRESSING",
    session_active: bool = True,
) -> StatusDatagram:
    """Build a plausible device datagram."""
    return StatusDatagram(
        type=HEARTBEAT,
        device_id=device_id,
        boot_id=boot_id,
        sequence=sequence,
        timestamp_ms=1_700_000_000_000 + sequence,
        state=state,
        session_active=session_active,
    )


@pytest.fixture
async def hub() -> AsyncIterator[StatusHub]:
    """A hub on an ephemeral loopback port that accepts any device id."""
    running = StatusHub(port=0, expected_device_id=None)
    await running.start()
    try:
        yield running
    finally:
        await running.stop()


async def test_forwards_a_heartbeat_to_the_page(wait_until) -> None:
    """A datagram becomes the status the page is shown."""
    updates: list[int] = []
    hub = StatusHub(port=0, expected_device_id=None, on_update=lambda: updates.append(1))
    await hub.start()
    try:
        await _send(hub.port, _datagram())

        assert await wait_until(lambda: hub.snapshot()["sequence"] == 1)
        snapshot = hub.snapshot()
        assert snapshot["online"] is True
        assert snapshot["state"] == "PRESSING"
        assert snapshot["session_active"] is True
        assert snapshot["device_id"] == DEFAULT_DEVICE_ID
        assert snapshot["boot_id"] == "boot-a"
        assert updates, "the hub should have told the bridge something changed"
    finally:
        await hub.stop()


async def test_rejects_a_malformed_datagram(hub: StatusHub, wait_until) -> None:
    """A bad datagram is counted and dropped, never treated as status."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, remote_addr=("127.0.0.1", hub.port)
    )
    try:
        transport.sendto(b"this is not a RemoteTap datagram")
    finally:
        transport.close()

    assert await wait_until(lambda: hub.snapshot()["counters"]["rejected"] == 1)
    snapshot = hub.snapshot()
    assert snapshot["sequence"] is None
    assert snapshot["online"] is False


async def test_ignores_datagrams_from_another_device(wait_until) -> None:
    """An expected-device filter keeps unrelated traffic out of the demo."""
    running = StatusHub(port=0, expected_device_id=DEFAULT_DEVICE_ID)
    await running.start()
    try:
        await _send(running.port, _datagram(device_id="some-other-device"))

        assert await wait_until(
            lambda: running.snapshot()["counters"]["rejected"] == 1
        )
        assert running.snapshot()["sequence"] is None
    finally:
        await running.stop()


async def test_recognises_a_restarted_device(hub: StatusHub, wait_until) -> None:
    """A new boot id is a restart, so its sequence 1 is accepted, not dropped.

    This is the existing monitor's restart detection, reused unchanged: the demo
    does not need its own copy of that logic.
    """
    await _send(hub.port, _datagram(sequence=9, boot_id="boot-a"))
    assert await wait_until(lambda: hub.snapshot()["sequence"] == 9)

    await _send(hub.port, _datagram(sequence=1, boot_id="boot-b"))

    assert await wait_until(lambda: hub.snapshot()["counters"]["restarts"] == 1)
    snapshot = hub.snapshot()
    assert snapshot["boot_id"] == "boot-b"
    assert snapshot["sequence"] == 1
    # A restarted device is not a flood of out-of-order packets.
    assert snapshot["counters"]["out_of_order"] == 0


async def test_reports_the_device_offline_after_silence(wait_until) -> None:
    """No heartbeat within the threshold shows as offline."""
    running = StatusHub(
        port=0, expected_device_id=None, stale_after_ms=150, check_interval_s=0.02
    )
    await running.start()
    try:
        await _send(running.port, _datagram())
        assert await wait_until(lambda: running.snapshot()["online"] is True)

        assert await wait_until(lambda: running.snapshot()["online"] is False)
    finally:
        await running.stop()


async def test_hub_is_the_only_listener_on_its_port(hub: StatusHub) -> None:
    """A second receiver cannot bind the same port.

    Binding the heartbeat port exactly once is what makes the bridge the sole
    consumer of the status stream, with no split or competing receiver.
    """
    second = StatusHub(port=hub.port, expected_device_id=None)

    with pytest.raises(OSError):
        await second.start()
