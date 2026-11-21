"""Tests for the UDP datagram format and the monitoring client's logic.

Nothing here opens a socket: the datagram codec and the monitor are both
socket-free on purpose, so ordering, duplicates and staleness can be checked
exactly, with an injected clock instead of a sleep.
"""

from __future__ import annotations

import json

import pytest

from remote_tap.monitor import DatagramKind, StatusMonitor, format_event
from remote_tap.protocol.udp_messages import (
    HEARTBEAT,
    MAX_DATAGRAM_BYTES,
    STATUS,
    STATUS_OK,
    UDP_PROTOCOL_VERSION,
    DatagramError,
    DatagramErrorCode,
    StatusDatagram,
    encode_datagram,
    parse_datagram,
)


def heartbeat(**overrides: object) -> StatusDatagram:
    """A valid heartbeat, with individual fields overridable."""
    fields: dict[str, object] = {
        "type": HEARTBEAT,
        "device_id": "remotetap-01",
        "boot_id": "boot-a",
        "sequence": 1,
        "timestamp_ms": 1_700_000_000_000,
        "state": "IDLE",
        "session_active": False,
    }
    fields.update(overrides)
    return StatusDatagram(**fields)  # type: ignore[arg-type]


def wire(**overrides: object) -> bytes:
    """Encode a valid heartbeat, then mutate its JSON, for reject tests."""
    payload = heartbeat().to_dict()
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


# ----------------------------------------------------------------------
# Encoding and parsing
# ----------------------------------------------------------------------


def test_a_heartbeat_round_trips_through_the_wire_format() -> None:
    original = heartbeat(type=HEARTBEAT, sequence=7, state="PRESSING", session_active=True)
    parsed = parse_datagram(encode_datagram(original))
    assert parsed == original


def test_the_wire_format_uses_the_documented_short_field_names() -> None:
    payload = json.loads(encode_datagram(heartbeat()))
    assert payload == {
        "v": UDP_PROTOCOL_VERSION,
        "type": HEARTBEAT,
        "device_id": "remotetap-01",
        "boot_id": "boot-a",
        "seq": 1,
        "ts": 1_700_000_000_000,
        "status": STATUS_OK,
        "state": "IDLE",
        "session_active": False,
    }


def test_a_datagram_is_a_single_json_object_with_no_newline() -> None:
    encoded = encode_datagram(heartbeat())
    assert b"\n" not in encoded
    assert encoded.startswith(b"{") and encoded.endswith(b"}")


def test_a_datagram_stays_small() -> None:
    # The receiver accepts at most MAX_DATAGRAM_BYTES; a real datagram should be
    # far smaller, because UDP fragments or drops anything that does not fit.
    # A real 32-character hex boot id is used, not a short test stub.
    datagram = encode_datagram(
        heartbeat(boot_id="9f2c4d1e5a6b7c8d9e0f1a2b3c4d5e6f")
    )
    assert len(datagram) < 256
    assert len(datagram) < MAX_DATAGRAM_BYTES


def test_both_a_heartbeat_and_a_status_report_parse() -> None:
    assert parse_datagram(encode_datagram(heartbeat(type=HEARTBEAT))).type == HEARTBEAT
    assert parse_datagram(encode_datagram(heartbeat(type=STATUS))).type == STATUS


# ----------------------------------------------------------------------
# Malformed and unsupported datagrams
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [b"", b"not json", b"{\"v\": 1,", b"\xff\xfe\x00"],
    ids=["empty", "not-json", "truncated", "invalid-utf8"],
)
def test_invalid_json_is_rejected_as_malformed(data: bytes) -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(data)
    assert caught.value.code is DatagramErrorCode.MALFORMED


@pytest.mark.parametrize(
    "data", [b"[1, 2, 3]", b"\"a string\"", b"42", b"null", b"true"]
)
def test_json_that_is_not_an_object_is_rejected(data: bytes) -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(data)
    assert caught.value.code is DatagramErrorCode.NOT_AN_OBJECT


def test_an_oversized_datagram_is_rejected_without_being_parsed() -> None:
    oversized = b'{"v":1,"pad":"' + b"x" * MAX_DATAGRAM_BYTES + b'"}'
    with pytest.raises(DatagramError) as caught:
        parse_datagram(oversized)
    assert caught.value.code is DatagramErrorCode.MALFORMED


@pytest.mark.parametrize(
    "field", ["v", "type", "seq", "state", "session_active", "boot_id"]
)
def test_every_required_field_is_required(field: str) -> None:
    payload = heartbeat().to_dict()
    del payload[field]
    with pytest.raises(DatagramError) as caught:
        parse_datagram(json.dumps(payload).encode("utf-8"))
    assert caught.value.code is DatagramErrorCode.MISSING_FIELD


def test_a_required_field_present_but_null_is_missing() -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(wire(**{"seq": None}))
    assert caught.value.code is DatagramErrorCode.MISSING_FIELD


@pytest.mark.parametrize("version", [0, 2, 99, "1"])
def test_an_unsupported_protocol_version_is_rejected(version: object) -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(wire(**{"v": version}))
    assert caught.value.code is DatagramErrorCode.UNSUPPORTED_VERSION


def test_an_unsupported_message_type_is_rejected() -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(wire(**{"type": "press_start"}))
    assert caught.value.code is DatagramErrorCode.UNSUPPORTED_TYPE


@pytest.mark.parametrize("sequence", [-1, "3", 1.5, True, None])
def test_an_invalid_sequence_number_is_rejected(sequence: object) -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(wire(**{"seq": sequence}))
    assert caught.value.code in (
        DatagramErrorCode.INVALID_SEQUENCE,
        DatagramErrorCode.MISSING_FIELD,
    )


@pytest.mark.parametrize("state", ["pressing", "DOWN", 1, None])
def test_an_unknown_actuator_state_is_rejected(state: object) -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(wire(**{"state": state}))
    assert caught.value.code in (
        DatagramErrorCode.INVALID_FIELD,
        DatagramErrorCode.MISSING_FIELD,
    )


def test_a_non_boolean_session_flag_is_rejected() -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(wire(**{"session_active": "yes"}))
    assert caught.value.code is DatagramErrorCode.INVALID_FIELD


def test_a_blank_device_id_is_rejected() -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(wire(**{"device_id": "   "}))
    assert caught.value.code is DatagramErrorCode.INVALID_FIELD


@pytest.mark.parametrize("boot_id", ["", "   ", 5, None, ["b"]])
def test_an_invalid_boot_id_is_rejected(boot_id: object) -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(wire(**{"boot_id": boot_id}))
    assert caught.value.code in (
        DatagramErrorCode.INVALID_FIELD,
        DatagramErrorCode.MISSING_FIELD,
    )


def test_a_boot_id_round_trips_unchanged() -> None:
    original = heartbeat(boot_id="9f2c4d1e5a6b7c8d9e0f1a2b3c4d5e6f")
    assert parse_datagram(encode_datagram(original)).boot_id == original.boot_id


def test_an_optional_status_field_falls_back_to_ok() -> None:
    payload = heartbeat().to_dict()
    del payload["status"]
    assert parse_datagram(json.dumps(payload).encode("utf-8")).status == STATUS_OK


def test_a_datagram_error_carries_a_message_for_the_operator() -> None:
    with pytest.raises(DatagramError) as caught:
        parse_datagram(b"nonsense")
    assert str(caught.value) and caught.value.message
    assert isinstance(caught.value, ValueError)


# ----------------------------------------------------------------------
# The monitoring client's classification logic
# ----------------------------------------------------------------------


class FakeClock:
    """A monotonic clock a test can move by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def beat(
    sequence: int,
    *,
    state: str = "IDLE",
    active: bool = False,
    boot_id: str = "boot-a",
    device_id: str = "remotetap-01",
) -> bytes:
    """Encode a heartbeat datagram with the given sequence number."""
    return encode_datagram(
        heartbeat(
            type=HEARTBEAT,
            sequence=sequence,
            state=state,
            session_active=active,
            boot_id=boot_id,
            device_id=device_id,
        )
    )


def test_in_order_heartbeats_are_accepted_and_update_the_status() -> None:
    clock = FakeClock()
    monitor = StatusMonitor(3000, clock=clock)

    first = monitor.observe(beat(1), now=clock())
    assert first.kind is DatagramKind.ACCEPTED
    assert first.missing == 0
    assert monitor.last_sequence == 1
    assert monitor.last_status is not None

    clock.advance(1.0)
    second = monitor.observe(beat(2, state="PRESSING", active=True), now=clock())
    assert second.kind is DatagramKind.ACCEPTED
    assert monitor.last_status.state == "PRESSING"
    assert monitor.last_status.session_active is True
    assert monitor.accepted == 2


def test_a_gap_in_the_sequence_is_reported_as_missing_heartbeats() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    monitor.observe(beat(1))
    event = monitor.observe(beat(4))
    assert event.kind is DatagramKind.ACCEPTED
    # 2 and 3 never arrived.
    assert event.missing == 2
    assert monitor.missing == 2
    assert monitor.last_sequence == 4


def test_a_repeated_sequence_number_is_a_duplicate_and_is_ignored() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    monitor.observe(beat(1))
    monitor.observe(beat(2, state="PRESSING", active=True))

    event = monitor.observe(beat(2, state="IDLE", active=False))
    assert event.kind is DatagramKind.DUPLICATE
    assert monitor.duplicates == 1
    # The displayed status must not be corrupted by the late copy.
    assert monitor.last_status is not None
    assert monitor.last_status.state == "PRESSING"
    assert monitor.last_sequence == 2


def test_an_older_sequence_number_is_out_of_order_and_is_ignored() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    monitor.observe(beat(1))
    monitor.observe(beat(2))
    monitor.observe(beat(3))

    event = monitor.observe(beat(2))
    assert event.kind is DatagramKind.OUT_OF_ORDER
    assert monitor.out_of_order == 1
    assert monitor.last_sequence == 3
    assert "newest is 3" in event.message


def test_duplicates_and_reordering_never_move_the_displayed_status_backwards() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    for sequence in range(1, 6):
        monitor.observe(beat(sequence, state="PRESSING", active=True))

    for sequence in (5, 2, 4, 1):
        assert monitor.observe(beat(sequence)).kind in (
            DatagramKind.DUPLICATE,
            DatagramKind.OUT_OF_ORDER,
        )

    assert monitor.last_sequence == 5
    assert monitor.last_status is not None
    assert monitor.last_status.state == "PRESSING"
    assert monitor.accepted == 5
    assert monitor.duplicates + monitor.out_of_order == 4


def test_a_malformed_datagram_is_rejected_without_touching_the_status() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    monitor.observe(beat(1))

    event = monitor.observe(b"\x00 not a datagram")
    assert event.kind is DatagramKind.REJECTED
    assert monitor.rejected == 1
    assert monitor.last_sequence == 1


def test_a_datagram_from_another_device_is_rejected_when_one_is_expected() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock(), expected_device_id="remotetap-01")
    good = encode_datagram(heartbeat(sequence=1))
    other = encode_datagram(heartbeat(sequence=1, device_id="someone-else"))

    assert monitor.observe(other).kind is DatagramKind.REJECTED
    assert monitor.last_sequence is None
    assert monitor.observe(good).kind is DatagramKind.ACCEPTED


def test_a_duplicate_still_proves_the_device_is_alive() -> None:
    clock = FakeClock()
    monitor = StatusMonitor(1000, clock=clock)
    monitor.observe(beat(1), now=clock())

    clock.advance(0.9)  # Nearly stale.
    monitor.observe(beat(1), now=clock())  # A duplicate arrives.
    assert monitor.is_offline(now=clock()) is False


# ----------------------------------------------------------------------
# Restart detection: sequence tracking is keyed by (device_id, boot_id)
# ----------------------------------------------------------------------


def test_the_first_datagram_from_a_device_is_not_a_restart() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    event = monitor.observe(beat(1))
    assert event.kind is DatagramKind.ACCEPTED
    assert event.restarted is False
    assert monitor.restarts == 0


def test_a_restart_is_recognised_by_a_new_boot_id() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    for sequence in range(1, 501):
        monitor.observe(beat(sequence))
    assert monitor.last_sequence == 500
    assert monitor.accepted == 500

    # The device restarts: the device id is unchanged, but the boot id is fresh
    # and the sequence number is back to 1.
    event = monitor.observe(beat(1, boot_id="boot-b"))
    assert event.kind is DatagramKind.ACCEPTED
    assert event.restarted is True
    assert event.missing == 0
    assert monitor.last_sequence == 1
    assert monitor.last_boot_id == "boot-b"
    assert monitor.restarts == 1


def test_a_restarted_device_sequence_one_is_not_out_of_order() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    monitor.observe(beat(500))
    event = monitor.observe(beat(1, boot_id="boot-b"))
    assert event.kind is DatagramKind.ACCEPTED
    assert monitor.out_of_order == 0


def test_a_late_datagram_from_the_replaced_boot_is_ignored() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    for sequence in (1, 2, 3):
        monitor.observe(beat(sequence, state="PRESSING", active=True))
    # The device restarts; the new boot reports the actuator idle.
    monitor.observe(beat(1, boot_id="boot-b"))

    # A datagram from the old boot arrives late. Even a higher sequence number
    # must not overwrite the current status.
    event = monitor.observe(beat(4, boot_id="boot-a", state="PRESSING", active=True))
    assert event.kind is DatagramKind.SUPERSEDED
    assert monitor.superseded == 1
    assert monitor.last_boot_id == "boot-b"
    assert monitor.last_sequence == 1
    assert monitor.last_status is not None
    assert monitor.last_status.state == "IDLE"


def test_a_superseded_datagram_does_not_keep_the_device_alive() -> None:
    clock = FakeClock()
    monitor = StatusMonitor(1000, clock=clock)
    monitor.observe(beat(1, boot_id="boot-a"), now=clock())
    monitor.observe(beat(1, boot_id="boot-b"), now=clock())  # A restart.

    clock.advance(1.5)  # The live boot has gone quiet: past the stale timeout.
    assert monitor.is_offline(now=clock()) is True

    # A late packet from the dead boot must not clear the outage.
    event = monitor.observe(beat(9, boot_id="boot-a"), now=clock())
    assert event.kind is DatagramKind.SUPERSEDED
    assert monitor.is_offline(now=clock()) is True


def test_old_sequence_numbers_within_the_current_boot_stay_out_of_order() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    for sequence in (1, 2, 3):
        monitor.observe(beat(sequence, boot_id="boot-b"))
    event = monitor.observe(beat(2, boot_id="boot-b"))
    assert event.kind is DatagramKind.OUT_OF_ORDER
    assert monitor.out_of_order == 1
    assert monitor.last_sequence == 3


def test_duplicates_are_counted_within_the_same_boot() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    monitor.observe(beat(1, boot_id="boot-b"))
    monitor.observe(beat(2, boot_id="boot-b"))
    event = monitor.observe(beat(2, boot_id="boot-b"))
    assert event.kind is DatagramKind.DUPLICATE
    assert monitor.duplicates == 1
    assert monitor.last_sequence == 2


def test_two_devices_are_tracked_independently() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    for sequence in (1, 2, 3):
        event = monitor.observe(beat(sequence, device_id="device-a", boot_id="a1"))
        assert event.kind is DatagramKind.ACCEPTED

    # A second device starts at sequence 1; it must not look out of order even
    # though the first device is already at 3.
    event = monitor.observe(beat(1, device_id="device-b", boot_id="b1"))
    assert event.kind is DatagramKind.ACCEPTED
    assert event.missing == 0
    assert event.restarted is False  # A different device, not a reboot.
    assert monitor.out_of_order == 0

    # Both continue independently, each keeping its own sequence.
    a_next = monitor.observe(beat(4, device_id="device-a", boot_id="a1"))
    b_next = monitor.observe(beat(2, device_id="device-b", boot_id="b1"))
    assert a_next.kind is DatagramKind.ACCEPTED
    assert b_next.kind is DatagramKind.ACCEPTED
    assert monitor.accepted == 6
    assert monitor.missing == 0
    assert monitor.restarts == 0


# ----------------------------------------------------------------------
# Staleness and offline detection
# ----------------------------------------------------------------------


def test_status_is_fresh_until_the_stale_timeout_is_crossed() -> None:
    clock = FakeClock()
    monitor = StatusMonitor(1000, clock=clock)
    monitor.observe(beat(1), now=clock())

    clock.advance(0.999)
    assert monitor.age(now=clock()) == pytest.approx(0.999)
    assert monitor.is_offline(now=clock()) is False

    clock.advance(0.002)
    assert monitor.is_offline(now=clock()) is True


def test_a_monitor_that_has_never_seen_a_datagram_is_offline() -> None:
    monitor = StatusMonitor(1000, clock=FakeClock())
    assert monitor.age() is None
    assert monitor.is_offline() is True


def test_the_device_offline_notice_is_reported_only_once_per_outage() -> None:
    clock = FakeClock()
    monitor = StatusMonitor(1000, clock=clock)
    monitor.observe(beat(1), now=clock())

    clock.advance(1.5)
    assert monitor.check_offline(now=clock()) is True
    # Still offline, but the notice has already been given.
    assert monitor.check_offline(now=clock()) is False

    # A single datagram clears the outage, so the next one is reported again.
    monitor.observe(beat(2), now=clock())
    assert monitor.is_offline(now=clock()) is False
    clock.advance(1.5)
    assert monitor.check_offline(now=clock()) is True


def test_staleness_uses_its_own_clock_not_the_sender_timestamp() -> None:
    # A device with a wildly wrong wall clock must still look fresh, because the
    # receiver times datagrams with its own monotonic clock.
    clock = FakeClock()
    monitor = StatusMonitor(1000, clock=clock)
    ancient = encode_datagram(
        heartbeat(sequence=1, timestamp_ms=1)  # 1970
    )
    monitor.observe(ancient, now=clock())
    assert monitor.age(now=clock()) == 0.0
    assert monitor.is_offline(now=clock()) is False


# ----------------------------------------------------------------------
# Console output
# ----------------------------------------------------------------------


def test_the_reported_line_shows_the_state_the_sequence_and_any_gap() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    monitor.observe(beat(1))
    event = monitor.observe(beat(3, state="PRESSING", active=True))

    line = format_event(("127.0.0.1", 5555), event, monitor)
    assert "seq=3" in line
    assert "state=PRESSING" in line
    assert "session_active=yes" in line
    assert "1 heartbeat(s) missing" in line


def test_the_reported_lines_name_duplicates_reordering_and_rejection() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    monitor.observe(beat(1))
    monitor.observe(beat(2))

    duplicate = format_event("addr", monitor.observe(beat(2)), monitor)
    out_of_order = format_event("addr", monitor.observe(beat(1)), monitor)
    rejected = format_event("addr", monitor.observe(b"garbage"), monitor)

    assert "duplicate" in duplicate
    assert "out-of-order" in out_of_order
    assert "rejected" in rejected


def test_the_reported_line_names_a_restart_and_a_superseded_boot() -> None:
    monitor = StatusMonitor(3000, clock=FakeClock())
    monitor.observe(beat(1, boot_id="boot-a"))

    restart = format_event("addr", monitor.observe(beat(1, boot_id="boot-b")), monitor)
    superseded = format_event(
        "addr", monitor.observe(beat(2, boot_id="boot-a")), monitor
    )

    assert "boot=boot-b" in restart
    assert "restarted" in restart
    assert "earlier boot" in superseded
    assert "boot=boot-a" in superseded
