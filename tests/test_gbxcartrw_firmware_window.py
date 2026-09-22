"""Hardware-free tests for the GBxCart RW v1.3 updater window."""

from __future__ import annotations

import importlib.util
import struct
import sys
from collections import deque
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import Mock

import pytest

import FlashGBX.hw_GBxCartRW as gbxcartrw

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path


class FakeWidget:
    def __init__(self) -> None:
        self.text = ""
        self.text_history: list[str] = []
        self.enabled = True
        self.checked = False
        self.value = 0

    def setText(self, value: str) -> None:
        self.text = value
        self.text_history.append(value)

    def setEnabled(self, value: bool) -> None:
        self.enabled = value

    def setValue(self, value: int) -> None:
        self.value = value

    def isChecked(self) -> bool:
        return self.checked

    def setChecked(self, value: bool) -> None:
        self.checked = value


class FakeMessageBox:
    Icon = SimpleNamespace(Critical=1, Information=2, Warning=3, Question=4)
    StandardButton = SimpleNamespace(Yes=1, No=2, Ok=4, Cancel=8)
    next_answer = StandardButton.No
    shown: ClassVar[list[FakeMessageBox]] = []

    def __init__(self, icon: int, title: str, text: str, buttons: int, parent: object) -> None:
        self.icon = icon
        self.title = title
        self.text = text
        self.buttons = buttons
        self.parent = parent
        self.default_button: int | None = None
        type(self).shown.append(self)

    def setDefaultButton(self, answer: int) -> None:
        self.default_button = answer

    def exec(self) -> int:
        return type(self).next_answer


class ScriptedSerial:
    """Consume an exact serial script; an unexpected command fails immediately."""

    def __init__(self, events: list[tuple[str, object]], *, waiting: int = 64) -> None:
        self.events = deque(events)
        self.waiting = waiting
        self.writes: list[bytes] = []
        self.reads: list[int] = []
        self.flushes = 0
        self.closes = 0

    @property
    def in_waiting(self) -> int:
        return self.waiting

    def _next(self, action: str) -> object:
        assert self.events, f"Unexpected serial {action}"
        expected_action, value = self.events.popleft()
        assert expected_action == action, f"Expected {expected_action}, got {action}"
        return value

    def write(self, data: bytes | bytearray) -> int:
        payload = bytes(data)
        assert self._next("write") == payload
        self.writes.append(payload)
        return len(payload)

    def read(self, count: int) -> bytes:
        expected_count, payload = self._next("read")
        assert expected_count == count
        self.reads.append(count)
        return payload

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closes += 1

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def assert_finished(self) -> None:
        assert not self.events


class ScriptedFirmwareUpdater:
    """Return finite modern updater results and record each controller call."""

    def __init__(
        self,
        app_path: Path,
        results: list[int],
        events: list[str],
        controls: tuple[FakeWidget, ...],
    ) -> None:
        self.APP_PATH = app_path
        self.results = deque(results)
        self.events = events
        self.controls = controls
        self.calls: list[tuple[Path, object]] = []
        self.control_states: list[tuple[bool, ...]] = []

    def WriteFirmware(self, path: Path, status_callback: object) -> int:
        assert self.results, "Unexpected modern firmware writer retry"
        self.events.append("write")
        self.calls.append((path, status_callback))
        self.control_states.append(tuple(control.enabled for control in self.controls))
        return self.results.popleft()

    def assert_finished(self) -> None:
        assert not self.results


@pytest.fixture(scope="module")
def firmware_module() -> Generator[ModuleType]:
    """Load a separate updater module with inert Qt types and restore imports."""
    fake_pyside = ModuleType("PySide6")
    fake_pyside.QtCore = SimpleNamespace()  # type: ignore[attr-defined]
    fake_pyside.QtGui = SimpleNamespace()  # type: ignore[attr-defined]
    fake_pyside.QtWidgets = SimpleNamespace(QDialog=object, QMessageBox=FakeMessageBox)  # type: ignore[attr-defined]
    original_pyside = sys.modules.get("PySide6")
    alias = "FlashGBX._firmware_window_test_module"
    spec = importlib.util.spec_from_file_location(alias, gbxcartrw.__file__)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    sys.modules["PySide6"] = fake_pyside
    try:
        spec.loader.exec_module(module)
    finally:
        if original_pyside is None:
            sys.modules.pop("PySide6", None)
        else:
            sys.modules["PySide6"] = original_pyside
    try:
        assert hasattr(module, "FirmwareUpdaterWindow")
        assert hasattr(module, "FirmwareUpdaterWindowV13")
        yield module
    finally:
        sys.modules.pop(alias, None)


@pytest.fixture(autouse=True)
def reset_message_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeMessageBox.shown = []
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.No)


def make_window(module: ModuleType) -> object:
    window = object.__new__(module.FirmwareUpdaterWindowV13)
    window.APP = SimpleNamespace(QT_APP=SimpleNamespace(processEvents=Mock()))
    window.PCB_VER = "v1.3"
    window.FW_VER = "R26"
    window.PORT = "mock-port"
    window.DEVICE = object()
    window.lblStatus = FakeWidget()
    window.prgStatus = FakeWidget()
    window.btnUpdate = FakeWidget()
    window.btnClose = FakeWidget()
    window.grpAvailableFwUpdates = FakeWidget()
    window.btnUpdate.enabled = False
    window.btnClose.enabled = False
    window.grpAvailableFwUpdates.enabled = False
    window.reject = Mock()
    return window


def make_modern_window(
    module: ModuleType,
    tmp_path: Path,
    *,
    results: list[int],
) -> tuple[object, ScriptedFirmwareUpdater, list[str]]:
    """Build the modern controller without constructing a Qt dialog."""
    events: list[str] = []
    window = object.__new__(module.FirmwareUpdaterWindow)
    window.APP = SimpleNamespace(
        DisconnectDevice=Mock(side_effect=lambda: events.append("disconnect")),
        QT_APP=SimpleNamespace(processEvents=Mock()),
    )
    window.DEVICE = object()
    window.lblStatus = FakeWidget()
    window.prgStatus = FakeWidget()
    window.btnUpdate = FakeWidget()
    window.btnClose = FakeWidget()
    window.optDevicePCBVer14 = FakeWidget()
    window.optDevicePCBVer14a = FakeWidget()
    controls = (
        window.btnUpdate,
        window.btnClose,
        window.optDevicePCBVer14,
        window.optDevicePCBVer14a,
    )
    writer = ScriptedFirmwareUpdater(tmp_path, results, events, controls)
    window.FWUPD = writer
    window.reject = Mock(side_effect=lambda: events.append("reject"))
    return window, writer, events


def test_modern_update_rejects_missing_pcb_selection_before_disconnect(
    firmware_module: ModuleType,
    tmp_path: Path,
) -> None:
    window, writer, events = make_modern_window(firmware_module, tmp_path, results=[])
    original_device = window.DEVICE

    result = window.UpdateFirmware()

    assert result is False
    assert events == []
    assert writer.calls == []
    writer.assert_finished()
    window.APP.DisconnectDevice.assert_not_called()
    assert window.DEVICE is original_device
    window.reject.assert_not_called()
    assert all(
        control.enabled
        for control in (
            window.btnUpdate,
            window.btnClose,
            window.optDevicePCBVer14,
            window.optDevicePCBVer14a,
        )
    )
    assert len(FakeMessageBox.shown) == 1
    assert FakeMessageBox.shown[0].icon == FakeMessageBox.Icon.Critical
    assert "select the PCB version" in FakeMessageBox.shown[0].text


@pytest.mark.parametrize("pcb_version", ["v1.4", "v1.4a/b/c"])
def test_modern_update_cancellation_stops_before_firmware_write(
    firmware_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pcb_version: str,
) -> None:
    window, writer, events = make_modern_window(firmware_module, tmp_path, results=[])
    window.optDevicePCBVer14.setChecked(pcb_version == "v1.4")
    window.optDevicePCBVer14a.setChecked(pcb_version == "v1.4a/b/c")
    original_device = window.DEVICE
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Cancel)

    result = window.UpdateFirmware()

    assert result is None
    assert events == ["disconnect"]
    writer.assert_finished()
    assert writer.calls == []
    window.APP.DisconnectDevice.assert_called_once_with()
    assert window.DEVICE is original_device
    window.reject.assert_not_called()
    assert all(
        control.enabled
        for control in (
            window.btnUpdate,
            window.btnClose,
            window.optDevicePCBVer14,
            window.optDevicePCBVer14a,
        )
    )
    assert len(FakeMessageBox.shown) == 1
    assert FakeMessageBox.shown[0].icon == FakeMessageBox.Icon.Information
    assert pcb_version in FakeMessageBox.shown[0].text


@pytest.mark.parametrize(
    ("pcb_version", "writer_result", "archive_name", "expected_result", "terminal_icon", "terminal_text"),
    [
        ("v1.4", 1, "fw_GBxCart_RW_v1_4.zip", True, FakeMessageBox.Icon.Information, "update is complete"),
        ("v1.4a/b/c", 2, "fw_GBxCart_RW_v1_4a.zip", False, FakeMessageBox.Icon.Critical, "update has failed"),
        ("v1.4", 3, "fw_GBxCart_RW_v1_4.zip", False, FakeMessageBox.Icon.Critical, "file is corrupted"),
    ],
    ids=["success", "update-failure", "corrupted-firmware"],
)
def test_modern_update_handles_terminal_writer_results_once(
    firmware_module: ModuleType,
    tmp_path: Path,
    pcb_version: str,
    writer_result: int,
    archive_name: str,
    expected_result: bool,
    terminal_icon: int,
    terminal_text: str,
) -> None:
    window, writer, events = make_modern_window(firmware_module, tmp_path, results=[writer_result])
    window.optDevicePCBVer14.setChecked(pcb_version == "v1.4")
    window.optDevicePCBVer14a.setChecked(pcb_version == "v1.4a/b/c")
    original_device = window.DEVICE

    result = window.UpdateFirmware()

    assert result is expected_result
    assert writer.control_states == [(False, False, False, False)]
    assert len(writer.calls) == 1
    archive_path, status_callback = writer.calls[0]
    assert archive_path == tmp_path / "res" / archive_name
    assert status_callback.__self__ is window
    assert status_callback.__func__ is firmware_module.FirmwareUpdaterWindow.SetStatus
    writer.assert_finished()
    window.APP.DisconnectDevice.assert_called_once_with()
    assert events == (["disconnect", "write", "reject"] if writer_result == 1 else ["disconnect", "write"])
    assert all(
        control.enabled
        for control in (
            window.btnUpdate,
            window.btnClose,
            window.optDevicePCBVer14,
            window.optDevicePCBVer14a,
        )
    )
    assert len(FakeMessageBox.shown) == 2
    assert FakeMessageBox.shown[1].icon == terminal_icon
    assert terminal_text in FakeMessageBox.shown[1].text
    if writer_result == 1:
        assert window.DEVICE is None
        window.reject.assert_called_once_with()
    else:
        assert window.DEVICE is original_device
        window.reject.assert_not_called()
        assert all("update is complete" not in message.text for message in FakeMessageBox.shown)


def bootloader_reply(
    *,
    signature: bytes = b"\x1e\x93\x06",
    page_words: int = 32,
    flash_words: int = 3808,
    eeprom_minus_one: int = 511,
    identifier: int = 0xAA,
    version: int = 0x1234,
) -> bytes:
    return (
        struct.pack(
            "<3sHB3sBHHBB",
            b"TSB",
            version,
            0x42,
            signature,
            page_words,
            flash_words,
            eeprom_minus_one,
            0x55,
            identifier,
        )
        + b"\x00"
    )


def advancing_clock(step: float = 0.25) -> Callable[[], float]:
    value = 0.0

    def now() -> float:
        nonlocal value
        value += step
        return value

    return now


def readback_script(image: bytes, *, mismatch: bool = False) -> list[tuple[str, object]]:
    padded = bytearray(image).ljust(0x1DC0, b"\xff")
    if mismatch:
        padded[0] ^= 0xFF
    events: list[tuple[str, object]] = [("write", b"f")]
    for position in range(0, 0x1DC0, 0x40):
        events.extend([("write", b"!"), ("read", (0x40, bytes(padded[position : position + 0x40])))])
    events.append(("read", (1, b"?")))
    if mismatch:
        events.append(("write", b"?"))
    return events


@pytest.mark.parametrize(
    ("identifier", "jmp_mode", "device_type"),
    [
        (0xAA, "relative", "atmega"),
        (0x00, "relative", "attiny"),
        (0x0C, "absolute", "attiny"),
        (0xFE, "unknown", "unknown"),
    ],
)
def test_parse_bootloader_info_decodes_fields_and_device_type(
    firmware_module: ModuleType,
    identifier: int,
    jmp_mode: str,
    device_type: str,
) -> None:
    info = firmware_module.FirmwareUpdaterWindowV13._ParseBootloaderInfo(bootloader_reply(identifier=identifier))

    assert info == {
        "magic": b"TSB",
        "tsb_version": 0x1234,
        "tsb_status": 0x42,
        "signature": b"\x1e\x93\x06",
        "page_size": 64,
        "flash_size": 7616,
        "eeprom_size": 512,
        "unknown": 0x55,
        "avr_jmp_identifier": identifier,
        "jmp_mode": jmp_mode,
        "device_type": device_type,
        "tsb_timeout": 0,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"signature": b"\x00\x00\x00"},
        {"page_words": 16},
        {"flash_words": 2048},
        {"eeprom_minus_one": 255},
        {"identifier": 0x00},
        {"version": 0x8000},
    ],
)
def test_write_firmware_rejects_incompatible_bootloader_before_any_write(
    firmware_module: ModuleType,
    overrides: dict[str, object],
) -> None:
    window = make_window(firmware_module)
    dev = ScriptedSerial([])
    window._ConnectBootloader = Mock(return_value=(dev, bootloader_reply(**overrides)))

    result = window.WriteFirmware(bytearray(b"firmware"), window.SetStatus)

    assert result == 2
    assert dev.writes == []
    assert dev.closes == 1
    dev.assert_finished()
    assert window.lblStatus.text == "Status: Wrong device detected."
    assert window.btnUpdate.enabled is True
    assert window.btnClose.enabled is True
    assert window.grpAvailableFwUpdates.enabled is True
    assert "Status: Done!" not in window.lblStatus.text_history
    assert FakeMessageBox.shown == []


def test_write_firmware_rejects_short_user_data_before_flash_commands(firmware_module: ModuleType) -> None:
    window = make_window(firmware_module)
    dev = ScriptedSerial([("write", b"c"), ("read", (0x41, b"short"))])
    window._ConnectBootloader = Mock(return_value=(dev, bootloader_reply()))

    result = window.WriteFirmware(bytearray(b"firmware"), window.SetStatus)

    assert result == 2
    assert dev.writes == [b"c"]
    assert dev.closes == 1
    dev.assert_finished()
    assert window.lblStatus.text == "Status: Bootloader error."
    assert window.btnUpdate.enabled is True
    assert window.btnClose.enabled is True
    assert "Status: Done!" not in window.lblStatus.text_history


@pytest.mark.parametrize(
    ("mismatch", "answer", "expected_result"),
    [
        (False, FakeMessageBox.StandardButton.No, None),
        (True, FakeMessageBox.StandardButton.No, 2),
        (True, FakeMessageBox.StandardButton.Yes, 3),
    ],
)
def test_verify_firmware_readback_match_or_retry_decision(
    firmware_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: bool,
    answer: int,
    expected_result: int | None,
) -> None:
    window = make_window(firmware_module)
    image = bytes(range(80))
    dev = ScriptedSerial(readback_script(image, mismatch=mismatch))
    monkeypatch.setattr(firmware_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(FakeMessageBox, "next_answer", answer)

    result = window._VerifyFirmwareWrite(dev, bytearray(image), window.SetStatus)

    assert result == expected_result
    dev.assert_finished()
    assert len(dev.reads) == 120
    if mismatch:
        assert window.lblStatus.text == "Status: Verification Error."
        assert "Status: Done!" not in window.lblStatus.text_history
        assert dev.closes == 1
        assert len(FakeMessageBox.shown) == 1
        assert "Verification Error" in FakeMessageBox.shown[0].text
    else:
        assert window.lblStatus.text == "Status: Verification OK."
        assert window.prgStatus.value == 100
        assert dev.closes == 0
        assert FakeMessageBox.shown == []


def test_verify_firmware_readback_timeout_uses_advancing_clock_and_closes_port(
    firmware_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = make_window(firmware_module)
    dev = ScriptedSerial([("write", b"f"), ("write", b"!")], waiting=0)
    monkeypatch.setattr(firmware_module.time, "monotonic", advancing_clock())
    monkeypatch.setattr(firmware_module.time, "sleep", lambda _seconds: None)

    result = window._VerifyFirmwareWrite(dev, bytearray(b"firmware"), window.SetStatus)

    assert result == 2
    dev.assert_finished()
    assert dev.reads == []
    assert dev.closes == 1
    assert window.lblStatus.text == "Status: Verification Error."
    assert window.btnUpdate.enabled is True
    assert window.btnClose.enabled is True
    assert "Status: Done!" not in window.lblStatus.text_history
    assert FakeMessageBox.shown == []


def test_write_firmware_pads_last_page_and_finishes_after_matching_readback(
    firmware_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = make_window(firmware_module)
    firmware = bytearray(range(65))
    padded = firmware + bytearray(b"\xff" * 63)
    user_data = bytearray(range(65))
    extended_timeout = bytearray(user_data)
    extended_timeout[2] = 254
    restored_timeout = bytearray(user_data)
    restored_timeout[2] = 42
    events: list[tuple[str, object]] = [
        ("write", b"c"),
        ("read", (0x41, bytes(user_data))),
        ("write", b"C"),
        ("read", (1, b"?")),
        ("write", b"!"),
        ("write", bytes(extended_timeout)),
        ("read", (0x41, bytes(extended_timeout))),
        ("write", b"F"),
        ("read", (1, b"?")),
    ]
    for page in (padded[:0x40], padded[0x40:]):
        events.extend([("write", b"!"), ("write", bytes(page)), ("read", (1, b"?"))])
    events.extend([("write", b"?"), ("read", (1, b"?"))])
    events.extend(readback_script(padded))
    events.extend(
        [
            ("write", b"C"),
            ("read", (1, b"?")),
            ("write", b"!"),
            ("write", bytes(restored_timeout)),
            ("read", (0x41, bytes(restored_timeout))),
            ("write", b"?"),
        ],
    )
    dev = ScriptedSerial(events)
    window._ConnectBootloader = Mock(return_value=(dev, bootloader_reply()))
    monkeypatch.setattr(firmware_module.time, "sleep", lambda _seconds: None)

    result = window.WriteFirmware(firmware, window.SetStatus)

    assert result == 1
    dev.assert_finished()
    assert dev.closes == 1
    assert dev.writes.count(b"F") == 1
    assert bytes(padded[:0x40]) in dev.writes
    assert bytes(padded[0x40:]) in dev.writes
    assert firmware == bytearray(range(65))
    assert window.lblStatus.text == "Status: Done!"
    assert "Status: Verification OK." in window.lblStatus.text_history
    assert window.prgStatus.value == 100
    assert window.DEVICE is None
    window.reject.assert_called_once()
    assert len(FakeMessageBox.shown) == 1
    assert "firmware update is complete" in FakeMessageBox.shown[0].text


def test_write_firmware_bootloader_handshake_exhausts_retries_without_success(
    firmware_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = make_window(firmware_module)
    resets: list[float] = []
    window.ResetAVR = lambda delay: resets.append(delay) or True
    attempts: list[ScriptedSerial] = []

    def serial_factory(*_args: object, **kwargs: object) -> ScriptedSerial:
        assert kwargs == {"port": "mock-port", "baudrate": 38400, "timeout": 1}
        dev = ScriptedSerial([("write", b"@@@"), ("read", (0x11, b"")), ("write", b"?")])
        attempts.append(dev)
        return dev

    monkeypatch.setattr(firmware_module.serial, "Serial", serial_factory)
    monkeypatch.setattr(firmware_module.time, "sleep", lambda _seconds: None)

    result = window.WriteFirmware(bytearray(b"firmware"), window.SetStatus)

    assert result == 2
    assert len(attempts) == 11
    assert all(dev.closes == 1 for dev in attempts)
    assert all(not dev.events for dev in attempts)
    assert len(resets) == 12
    assert resets[0] == 0
    assert resets[-1] == pytest.approx(0.55)
    assert window.lblStatus.text == "Status: Bootloader timeout."
    assert window.btnUpdate.enabled is True
    assert window.btnClose.enabled is True
    assert len(FakeMessageBox.shown) == 1
    assert "not responding correctly" in FakeMessageBox.shown[0].text
    assert "Status: Done!" not in window.lblStatus.text_history


def test_connect_bootloader_accepts_expected_handshake_without_closing_port(
    firmware_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = make_window(firmware_module)
    reply = bootloader_reply()
    dev = ScriptedSerial([("write", b"@@@"), ("read", (0x11, reply))])
    serial_factory = Mock(return_value=dev)
    reset_avr = Mock(return_value=True)
    window.ResetAVR = reset_avr
    monkeypatch.setattr(firmware_module.serial, "Serial", serial_factory)
    monkeypatch.setattr(firmware_module.time, "sleep", lambda _seconds: None)

    result = window._ConnectBootloader(window.SetStatus)

    assert result == (dev, reply)
    reset_avr.assert_called_once_with(0.0)
    serial_factory.assert_called_once_with(port="mock-port", baudrate=38400, timeout=1)
    dev.assert_finished()
    assert dev.closes == 0
    assert window.lblStatus.text == "Status: Waiting for bootloader..."
    assert FakeMessageBox.shown == []


def test_write_firmware_stops_when_bootloader_reset_fails_before_serial_open(
    firmware_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = make_window(firmware_module)
    reset_avr = Mock(return_value=False)
    window.ResetAVR = reset_avr
    serial_factory = Mock(side_effect=AssertionError("Serial opened after failed reset"))
    monkeypatch.setattr(firmware_module.serial, "Serial", serial_factory)

    result = window.WriteFirmware(bytearray(b"firmware"), window.SetStatus)

    assert result == 2
    reset_avr.assert_called_once_with(0.0)
    serial_factory.assert_not_called()
    assert window.lblStatus.text == "Status: Bootloader error."
    assert window.btnUpdate.enabled is True
    assert window.prgStatus.value == 0
    assert len(FakeMessageBox.shown) == 1
    assert "Status: Done!" not in window.lblStatus.text_history


@pytest.mark.parametrize(
    ("answer", "expected_result"),
    [(FakeMessageBox.StandardButton.No, 2), (FakeMessageBox.StandardButton.Yes, 3)],
)
def test_write_firmware_failed_page_ack_closes_port_and_respects_retry_choice(
    firmware_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    answer: int,
    expected_result: int,
) -> None:
    window = make_window(firmware_module)
    firmware = bytearray(b"firmware")
    padded_page = bytes(firmware + bytearray(b"\xff" * (0x40 - len(firmware))))
    user_data = bytearray(range(65))
    extended_timeout = bytearray(user_data)
    extended_timeout[2] = 254
    dev = ScriptedSerial(
        [
            ("write", b"c"),
            ("read", (0x41, bytes(user_data))),
            ("write", b"C"),
            ("read", (1, b"?")),
            ("write", b"!"),
            ("write", bytes(extended_timeout)),
            ("read", (0x41, bytes(extended_timeout))),
            ("write", b"F"),
            ("read", (1, b"?")),
            ("write", b"!"),
            ("write", padded_page),
            ("read", (1, b"!")),
            ("write", b"?"),
        ],
    )
    window._ConnectBootloader = Mock(return_value=(dev, bootloader_reply()))
    verification = Mock(side_effect=AssertionError("Verification started after a failed page write"))
    window._VerifyFirmwareWrite = verification
    monkeypatch.setattr(FakeMessageBox, "next_answer", answer)
    monkeypatch.setattr(firmware_module.time, "sleep", lambda _seconds: None)

    result = window.WriteFirmware(firmware, window.SetStatus)

    assert result == expected_result
    dev.assert_finished()
    assert dev.closes == 1
    verification.assert_not_called()
    assert "Write Error" in window.lblStatus.text
    assert window.btnUpdate.enabled is True
    assert "Status: Done!" not in window.lblStatus.text_history
    assert len(FakeMessageBox.shown) == 1
    assert "Write Error" in FakeMessageBox.shown[0].text
