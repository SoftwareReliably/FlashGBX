"""Tests for device acknowledgements and bounded write recovery."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import pytest

from FlashGBX.hw_GBxCartRW import GbxDevice

if TYPE_CHECKING:
    from collections.abc import Iterable


class RecoverySerial:
    """Record the serial operations used to recover a failed write."""

    def __init__(self, *, timeout: float = 1.0, responses: Iterable[bytes] = ()) -> None:
        self.timeout = timeout
        self.reset_output_calls = 0
        self.reset_input_calls = 0
        self.writes: list[bytes] = []
        self.flush_calls = 0
        self._responses = deque(responses)
        self._read_buffer = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self._read_buffer)

    def reset_output_buffer(self) -> None:
        self.reset_output_calls += 1

    def reset_input_buffer(self) -> None:
        self.reset_input_calls += 1

    def write(self, data: bytes | bytearray) -> int:
        payload = bytes(data)
        self.writes.append(payload)
        if self._responses:
            self._read_buffer.extend(self._responses.popleft())
        return len(payload)

    def flush(self) -> None:
        self.flush_calls += 1

    def read(self, count: int) -> bytes:
        result = bytes(self._read_buffer[:count])
        del self._read_buffer[:count]
        return result


def install_read_script(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    responses: Iterable[int | bool],
) -> deque[int | bool]:
    """Install a finite one-byte read script and return its queue."""
    queue = deque(responses)

    def scripted_read(count: int) -> int | bool:
        assert count == 1
        assert queue, "Unexpected protocol read after the response script was exhausted"
        return queue.popleft()

    monkeypatch.setattr(device, "_read", scripted_read)
    return queue


@pytest.mark.parametrize("acknowledgement", [1, 3])
def test_wait_for_ack_accepts_default_success_values(
    monkeypatch: pytest.MonkeyPatch,
    acknowledgement: int,
) -> None:
    device = GbxDevice()
    queue = install_read_script(monkeypatch, device, [acknowledgement])

    assert device.wait_for_ack() == acknowledgement
    assert device.ERROR is False
    assert device.CANCEL is False
    assert device.WRITE_ERRORS == 0
    assert device.CANCEL_ARGS == {}
    assert not queue


def test_wait_for_ack_accepts_only_the_supplied_custom_values(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    queue = install_read_script(monkeypatch, device, [0x7E])

    assert device.wait_for_ack(values=(0x7E,)) == 0x7E
    assert device.WRITE_ERRORS == 0
    assert not queue


def test_real_write_waits_for_acknowledgement_from_serial() -> None:
    serial_device = RecoverySerial(responses=[b"\x01"])
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]
    device.FW = {"pcb_name": "Test device"}

    assert device._write(b"\x12\x34", wait=True) == 1
    assert serial_device.writes == [b"\x12\x34"]
    assert serial_device.flush_calls == 1
    assert serial_device.in_waiting == 0


def test_real_write_propagates_device_error_acknowledgement() -> None:
    serial_device = RecoverySerial(responses=[b"\x02"])
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]
    device.FW = {"pcb_name": "Test device"}

    assert device._write(b"\xab", wait=True) is False
    assert serial_device.writes == [b"\xab"]
    assert device.ERROR is True
    assert device.CANCEL is True
    assert device.CANCEL_ARGS["info_type"] == "msgbox_critical"
    assert "device reported an error" in str(device.CANCEL_ARGS["info_msg"]).lower()


@pytest.mark.parametrize(
    ("acknowledgement", "message_fragment"),
    [
        (False, "timeout error"),
        (2, "device reported an error"),
        (0x7F, "communication error"),
    ],
)
def test_wait_for_ack_records_structured_failures(
    monkeypatch: pytest.MonkeyPatch,
    acknowledgement: int | bool,
    message_fragment: str,
) -> None:
    device = GbxDevice()
    device.DEVICE = RecoverySerial(timeout=2.5)  # type: ignore[assignment]
    queue = install_read_script(monkeypatch, device, [acknowledgement])

    assert device.wait_for_ack() is False
    assert device.ERROR is True
    assert device.CANCEL is True
    assert device.WRITE_ERRORS == 1
    assert device.WRITE_DELAY is False
    assert device.CANCEL_ARGS["info_type"] == "msgbox_critical"
    assert message_fragment in str(device.CANCEL_ARGS["info_msg"]).lower()
    assert not queue


def test_wait_for_ack_enables_write_delay_on_the_fourth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.WRITE_ERRORS = 3
    queue = install_read_script(monkeypatch, device, [2])

    assert device.wait_for_ack() is False
    assert device.WRITE_ERRORS == 4
    assert device.WRITE_DELAY is True
    assert not queue


def test_wait_for_ack_preserves_user_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    cancellation = {"from_user": True, "reason": "stop now"}
    device.CANCEL_ARGS = cancellation.copy()
    device.CANCEL = True
    queue = install_read_script(monkeypatch, device, [0x7F])

    assert device.wait_for_ack() is False
    assert cancellation == device.CANCEL_ARGS
    assert device.CANCEL is True
    assert device.ERROR is False
    assert device.WRITE_ERRORS == 0
    assert device.WRITE_DELAY is False
    assert not queue


def test_try_write_first_attempt_success_clears_stale_error_state(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    writes: list[tuple[object, bool]] = []

    def write(data: object, wait: bool = False) -> int:
        writes.append((data, wait))
        return 3

    monkeypatch.setattr(device, "_write", write)
    device.ERROR = True
    device.CANCEL = True
    device.CANCEL_ARGS = {"reason": "stale"}

    assert device._try_write(memoryview(b"payload")) == 3
    assert writes == [(memoryview(b"payload"), True)]
    assert device.ERROR is False
    assert device.CANCEL is False
    assert device.CANCEL_ARGS == {}


def test_try_write_retries_payload_after_successful_recovery_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    serial_device = RecoverySerial()
    device.DEVICE = serial_device  # type: ignore[assignment]
    writes: list[tuple[object, bool]] = []
    write_results: deque[int | bool] = deque([False, 1])
    reads = install_read_script(monkeypatch, device, [1])

    def write(data: object, wait: bool = False) -> int | bool:
        writes.append((data, wait))
        assert write_results, "Unexpected payload retry"
        return write_results.popleft()

    monkeypatch.setattr(device, "_write", write)
    device.ERROR = True
    device.CANCEL = True
    device.CANCEL_ARGS = {"reason": "recoverable"}

    assert device._try_write(b"retry-me", retries=2) == 1
    assert writes == [(b"retry-me", True), (b"retry-me", True)]
    assert not write_results
    assert not reads
    assert serial_device.reset_output_calls == 1
    assert serial_device.reset_input_calls == 1
    assert serial_device.writes == [b"\x00"]
    assert serial_device.flush_calls == 1
    assert device.ERROR is False
    assert device.CANCEL is False
    assert device.CANCEL_ARGS == {}


def test_try_write_exhausts_exact_attempt_count(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    serial_device = RecoverySerial()
    device.DEVICE = serial_device  # type: ignore[assignment]
    writes: list[tuple[object, bool]] = []
    reads = install_read_script(monkeypatch, device, [2, 2])

    def failed_write(data: object, wait: bool = False) -> bool:
        writes.append((data, wait))
        return False

    monkeypatch.setattr(device, "_write", failed_write)

    assert device._try_write(bytearray(b"fail"), retries=2) is False
    assert writes == [(bytearray(b"fail"), True), (bytearray(b"fail"), True)]
    assert not reads
    assert serial_device.reset_output_calls == 2
    assert serial_device.reset_input_calls == 2
    assert serial_device.writes == [b"\x00", b"\x00"]
    assert serial_device.flush_calls == 2


def test_try_write_stops_before_recovery_after_user_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    writes: list[tuple[object, bool]] = []

    def canceled_write(data: object, wait: bool = False) -> bool:
        writes.append((data, wait))
        device.CANCEL_ARGS = {"from_user": True, "reason": "requested"}
        return False

    monkeypatch.setattr(device, "_write", canceled_write)

    assert device._try_write(0xA5, retries=5) is False
    assert writes == [(0xA5, True)]
    assert device.CANCEL_ARGS == {"from_user": True, "reason": "requested"}


def test_try_write_rejects_zero_retries_before_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    writes: list[object] = []
    monkeypatch.setattr(device, "_write", lambda data, wait=False: writes.append((data, wait)))

    with pytest.raises(ValueError, match="at least 1"):
        device._try_write(b"unused", retries=0)

    assert writes == []


def test_try_write_bounds_recovery_probe_at_twenty_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    serial_device = RecoverySerial()
    device.DEVICE = serial_device  # type: ignore[assignment]
    reads = install_read_script(monkeypatch, device, [0] * 20)
    writes: list[tuple[object, bool]] = []

    def failed_write(data: object, wait: bool = False) -> bool:
        writes.append((data, wait))
        return False

    monkeypatch.setattr(device, "_write", failed_write)

    assert device._try_write(b"bounded", retries=1) is False
    assert writes == [(b"bounded", True)]
    assert not reads
    assert serial_device.reset_output_calls == 20
    assert serial_device.reset_input_calls == 20
    assert serial_device.writes == [b"\x00"] * 20
    assert serial_device.flush_calls == 20
