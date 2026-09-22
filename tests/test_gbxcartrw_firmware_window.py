"""Hardware-free tests for the GBxCart RW v1.3 updater window."""

from __future__ import annotations

import datetime
import importlib.util
import struct
import sys
import zipfile
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

    def isEnabled(self) -> bool:
        return self.enabled

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


class FakeFileDialog:
    next_path = ""
    calls: ClassVar[list[tuple[object, str, str, str]]] = []

    @classmethod
    def getOpenFileName(
        cls,
        parent: object,
        title: str,
        directory: str,
        file_filter: str,
    ) -> tuple[str, str]:
        cls.calls.append((parent, title, directory, file_filter))
        return cls.next_path, ""


class FakeSettings:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = {} if values is None else dict(values)
        self.writes: list[tuple[str, object]] = []

    def value(self, key: str) -> object | None:
        return self.values.get(key)

    def setValue(self, key: str, value: object) -> None:
        self.values[key] = value
        self.writes.append((key, value))


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


class ScriptedV13Transport:
    """Return a finite V13 writer script and preserve every retry payload."""

    def __init__(
        self,
        results: list[int],
        events: list[str],
        controls: tuple[FakeWidget, ...],
    ) -> None:
        self.results = deque(results)
        self.events = events
        self.controls = controls
        self.payloads: list[bytearray] = []
        self.callbacks: list[object] = []
        self.control_states: list[tuple[bool, ...]] = []

    def __call__(self, payload: bytearray, status_callback: object) -> int:
        assert self.results, "Unexpected V13 firmware writer retry"
        self.events.append("write")
        self.payloads.append(payload.copy())
        self.callbacks.append(status_callback)
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
    fake_pyside.QtWidgets = SimpleNamespace(  # type: ignore[attr-defined]
        QDialog=object,
        QMessageBox=FakeMessageBox,
        QFileDialog=FakeFileDialog,
    )
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
    FakeFileDialog.calls = []
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.No)
    monkeypatch.setattr(FakeFileDialog, "next_path", "")


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
    window.lblDeviceFWVer2Result = FakeWidget()
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


def make_v13_update_window(
    module: ModuleType,
    tmp_path: Path,
    *,
    results: list[int],
) -> tuple[object, ScriptedV13Transport, FakeSettings, list[str]]:
    """Build an inert V13 controller with a finite transport script."""
    events: list[str] = []
    settings = FakeSettings({"LastDirFirmwareUpdate": str(tmp_path / "remembered")})
    window = object.__new__(module.FirmwareUpdaterWindowV13)
    window.APP = SimpleNamespace(
        SETTINGS=settings,
        DisconnectDevice=Mock(side_effect=lambda: events.append("disconnect")),
        QT_APP=SimpleNamespace(processEvents=Mock()),
    )
    window.APP_PATH = tmp_path
    window.PCB_VER = "v1.3"
    window.CFW_VER = "CFW test"
    window.OFW_VER = "OFW test"
    window.DEVICE = object()
    window.lblStatus = FakeWidget()
    window.prgStatus = FakeWidget()
    window.btnUpdate = FakeWidget()
    window.btnClose = FakeWidget()
    window.grpAvailableFwUpdates = FakeWidget()
    window.optCFW = FakeWidget()
    window.optOFW = FakeWidget()
    window.optExternal = FakeWidget()
    controls = (window.btnUpdate, window.btnClose, window.grpAvailableFwUpdates)
    transport = ScriptedV13Transport(results, events, controls)
    window.WriteFirmware = transport
    return window, transport, settings, events


def intel_hex_record(address: int, record_type: int, data: bytes = b"") -> str:
    """Encode one valid Intel HEX record."""
    record = bytearray([len(data), address >> 8, address & 0xFF, record_type])
    record.extend(data)
    record.append((-sum(record)) & 0xFF)
    return ":" + record.hex().upper()


def intel_hex_image(data: bytes, *, address: int = 0) -> str:
    return "\n".join((intel_hex_record(address, 0, data), intel_hex_record(0, 1))) + "\n"


def write_v13_archive(tmp_path: Path, members: dict[str, str | bytes]) -> Path:
    archive_path = tmp_path / "res" / "fw_GBxCart_RW_v1_3.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
    return archive_path


def write_modern_metadata_archive(
    tmp_path: Path,
    archive_name: str,
    *,
    version: str,
    build_timestamp: int,
    description: str,
) -> Path:
    archive_path = tmp_path / "res" / archive_name
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    contents = f"[Firmware]\nfw_ver = {version}\nfw_buildts = {build_timestamp}\nfw_text = {description}\n"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("fw.ini", contents)
    return archive_path


@pytest.mark.parametrize(
    ("pcb_version", "archive_name", "version", "build_timestamp", "description"),
    [
        ("v1.4", "fw_GBxCart_RW_v1_4.zip", "R40+L14", 1_700_000_000, "v1.4 release"),
        ("v1.4a/b/c", "fw_GBxCart_RW_v1_4a.zip", "R41+L15", 1_750_000_000, "v1.4a/b/c release"),
    ],
)
def test_modern_pcb_selection_loads_and_displays_firmware_metadata(
    firmware_module: ModuleType,
    tmp_path: Path,
    pcb_version: str,
    archive_name: str,
    version: str,
    build_timestamp: int,
    description: str,
) -> None:
    window, _writer, _events = make_modern_window(firmware_module, tmp_path, results=[])
    write_modern_metadata_archive(
        tmp_path,
        archive_name,
        version=version,
        build_timestamp=build_timestamp,
        description=description,
    )
    window.optDevicePCBVer14.setChecked(pcb_version == "v1.4")
    window.optDevicePCBVer14a.setChecked(pcb_version == "v1.4a/b/c")

    window.SetPCBVersion()

    assert version == window.OFW_VER
    assert build_timestamp == window.OFW_BUILDTS
    assert description == window.OFW_TEXT
    assert window.lblDeviceFWVer2Result.text.startswith(f"{version} (")
    displayed_timestamp = window.lblDeviceFWVer2Result.text.removeprefix(f"{version} (").removesuffix(")")
    displayed_datetime = datetime.datetime.fromisoformat(displayed_timestamp)
    assert displayed_datetime.microsecond == 0
    assert displayed_datetime.timestamp() == build_timestamp
    assert window.optDevicePCBVer14.isChecked() is (pcb_version == "v1.4")
    assert window.optDevicePCBVer14a.isChecked() is (pcb_version == "v1.4a/b/c")


def test_modern_pcb_selection_ignores_missing_selection(
    firmware_module: ModuleType,
    tmp_path: Path,
) -> None:
    window, _writer, _events = make_modern_window(firmware_module, tmp_path, results=[])
    window.lblDeviceFWVer2Result.setText("Choose a PCB")

    assert window.SetPCBVersion() is None

    assert window.lblDeviceFWVer2Result.text == "Choose a PCB"
    assert not hasattr(window, "OFW_VER")


@pytest.mark.parametrize(
    ("set_progress", "enable_ui", "expected_progress"),
    [(None, False, 17), (12.34, True, 123)],
    ids=["text-only", "progress-and-enable"],
)
def test_modern_status_updates_text_progress_and_controls(
    firmware_module: ModuleType,
    tmp_path: Path,
    set_progress: float | None,
    enable_ui: bool,
    expected_progress: int,
) -> None:
    window, _writer, _events = make_modern_window(firmware_module, tmp_path, results=[])
    window.prgStatus.setValue(17)
    for control in (
        window.btnUpdate,
        window.btnClose,
        window.optDevicePCBVer14,
        window.optDevicePCBVer14a,
    ):
        control.setEnabled(False)

    window.SetStatus("Writing", enableUI=enable_ui, setProgress=set_progress)

    assert window.lblStatus.text == "Status: Writing"
    assert window.prgStatus.value == expected_progress
    assert all(
        control.enabled is enable_ui
        for control in (
            window.btnUpdate,
            window.btnClose,
            window.optDevicePCBVer14,
            window.optDevicePCBVer14a,
        )
    )
    window.APP.QT_APP.processEvents.assert_called_once_with()


@pytest.mark.parametrize(
    ("set_progress", "enable_ui", "expected_progress"),
    [(None, False, 17), (12.6, True, 13)],
    ids=["text-only", "progress-and-enable"],
)
def test_v13_status_updates_text_progress_and_controls(
    firmware_module: ModuleType,
    set_progress: float | None,
    enable_ui: bool,
    expected_progress: int,
) -> None:
    window = make_window(firmware_module)
    window.prgStatus.setValue(17)

    window.SetStatus("Writing", enableUI=enable_ui, setProgress=set_progress)

    assert window.lblStatus.text == "Status: Writing"
    assert window.prgStatus.value == expected_progress
    assert window.btnUpdate.enabled is enable_ui
    assert window.btnClose.enabled is enable_ui
    assert window.grpAvailableFwUpdates.enabled is enable_ui
    window.APP.QT_APP.processEvents.assert_not_called()


@pytest.mark.parametrize("window_kind", ["modern", "v13"])
@pytest.mark.parametrize(
    ("close_enabled", "answer", "expected"),
    [
        (True, FakeMessageBox.StandardButton.No, True),
        (False, FakeMessageBox.StandardButton.No, False),
        (False, FakeMessageBox.StandardButton.Yes, True),
    ],
    ids=["enabled", "disabled-declined", "disabled-confirmed"],
)
def test_firmware_window_close_safeguards(
    firmware_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    window_kind: str,
    close_enabled: bool,
    answer: int,
    expected: bool,
) -> None:
    if window_kind == "modern":
        window, _writer, _events = make_modern_window(firmware_module, tmp_path, results=[])
    else:
        window = make_window(firmware_module)
    window.btnClose.setEnabled(close_enabled)
    monkeypatch.setattr(FakeMessageBox, "next_answer", answer)

    result = window.CloseDialog()

    assert result is expected
    if close_enabled:
        assert FakeMessageBox.shown == []
    else:
        assert len(FakeMessageBox.shown) == 1
        assert FakeMessageBox.shown[0].icon == FakeMessageBox.Icon.Warning
        assert FakeMessageBox.shown[0].default_button == FakeMessageBox.StandardButton.No


@pytest.mark.parametrize("pcb_version", [5, 6])
def test_firmware_updater_class_routes_modern_pcb_versions(pcb_version: int) -> None:
    device = gbxcartrw.GbxDevice()
    device.FW = {"pcb_ver": pcb_version}  # type: ignore[assignment]

    assert device.GetFirmwareUpdaterClass() == (gbxcartrw.FirmwareUpdater, gbxcartrw.FirmwareUpdaterWindow)


@pytest.mark.parametrize("pcb_version", [2, 4, 90, 100, 101])
def test_firmware_updater_class_routes_legacy_pcb_versions(pcb_version: int) -> None:
    device = gbxcartrw.GbxDevice()
    device.FW = {"pcb_ver": pcb_version}  # type: ignore[assignment]

    assert device.GetFirmwareUpdaterClass() == (None, gbxcartrw.FirmwareUpdaterWindowV13)


def test_firmware_updater_class_routes_missing_device_to_modern_updater() -> None:
    assert gbxcartrw.GbxDevice.GetFirmwareUpdaterClass(None) == (
        gbxcartrw.FirmwareUpdater,
        gbxcartrw.FirmwareUpdaterWindow,
    )


def test_firmware_updater_class_rejects_unsupported_pcb_version() -> None:
    device = gbxcartrw.GbxDevice()
    device.FW = {"pcb_ver": 255}  # type: ignore[assignment]

    assert device.GetFirmwareUpdaterClass() is None


@pytest.mark.parametrize(
    ("pcb_version", "missing_name"),
    [(5, "FirmwareUpdaterWindow"), (4, "FirmwareUpdaterWindowV13")],
)
def test_firmware_updater_class_handles_unavailable_optional_qt_window(
    monkeypatch: pytest.MonkeyPatch,
    pcb_version: int,
    missing_name: str,
) -> None:
    device = gbxcartrw.GbxDevice()
    device.FW = {"pcb_ver": pcb_version}  # type: ignore[assignment]
    monkeypatch.delattr(gbxcartrw, missing_name)

    assert device.GetFirmwareUpdaterClass() is None


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


@pytest.mark.parametrize(
    ("source", "writer_results", "expected_result", "expected_payload"),
    [
        ("custom", [1], True, bytearray(b"\x10\x11")),
        ("official", [2], False, bytearray(b"\x20\x21\x22")),
        ("external", [3, 1], True, bytearray(b"\xff\xff\x30\x31")),
    ],
    ids=["custom-success", "official-failure", "external-retry-success"],
)
def test_v13_update_loads_selected_hex_and_honors_writer_script(
    firmware_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    writer_results: list[int],
    expected_result: bool,
    expected_payload: bytearray,
) -> None:
    window, transport, settings, events = make_v13_update_window(
        firmware_module,
        tmp_path,
        results=writer_results,
    )
    custom_hex = intel_hex_image(b"\x10\x11")
    official_hex = intel_hex_image(b"\x20\x21\x22")
    write_v13_archive(tmp_path, {"cfw.hex": custom_hex, "ofw.hex": official_hex})
    window.optCFW.setChecked(source == "custom")
    window.optOFW.setChecked(source == "official")
    window.optExternal.setChecked(source == "external")
    if source == "external":
        external_path = tmp_path / "gbxcart_rw_test_pcb_r13.hex"
        external_path.write_text(intel_hex_image(b"\x30\x31", address=2), encoding="ascii")
        monkeypatch.setattr(FakeFileDialog, "next_path", str(external_path))
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Yes)

    result = window.UpdateFirmware()

    assert result is expected_result
    assert transport.payloads == [expected_payload] * len(writer_results)
    assert transport.control_states == [(False, False, False)] * len(writer_results)
    assert all(callback.__self__ is window for callback in transport.callbacks)
    assert all(
        callback.__func__ is firmware_module.FirmwareUpdaterWindowV13.SetStatus for callback in transport.callbacks
    )
    transport.assert_finished()
    window.APP.DisconnectDevice.assert_called_once_with()
    assert events == ["disconnect", *(["write"] * len(writer_results))]
    assert len(FakeMessageBox.shown) == 1
    assert FakeMessageBox.shown[0].icon == FakeMessageBox.Icon.Question
    if source == "external":
        assert settings.writes == [("LastDirFirmwareUpdate", str(tmp_path))]
        assert len(FakeFileDialog.calls) == 1
        assert FakeFileDialog.calls[0][0] is window
        assert FakeFileDialog.calls[0][2] == str(tmp_path / "remembered")
    else:
        assert settings.writes == []
        assert FakeFileDialog.calls == []


@pytest.mark.parametrize(
    ("boundary", "expected_messages"),
    [("file-picker-cancel", 0), ("unexpected-filename", 1), ("declined-confirmation", 1)],
)
def test_v13_update_stops_before_disconnect_at_selection_boundaries(
    firmware_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    expected_messages: int,
) -> None:
    window, transport, settings, events = make_v13_update_window(firmware_module, tmp_path, results=[])
    window.optCFW.setChecked(boundary == "declined-confirmation")
    window.optExternal.setChecked(boundary != "declined-confirmation")
    if boundary == "unexpected-filename":
        monkeypatch.setattr(FakeFileDialog, "next_path", str(tmp_path / "firmware.hex"))
    if boundary == "declined-confirmation":
        monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.No)

    result = window.UpdateFirmware()

    assert result is None
    assert events == []
    assert transport.payloads == []
    transport.assert_finished()
    window.APP.DisconnectDevice.assert_not_called()
    assert settings.writes == []
    assert all(control.enabled for control in (window.btnUpdate, window.btnClose, window.grpAvailableFwUpdates))
    assert len(FakeMessageBox.shown) == expected_messages
    if boundary == "unexpected-filename":
        assert FakeMessageBox.shown[0].icon == FakeMessageBox.Icon.Critical
        assert "expected filename" in FakeMessageBox.shown[0].text
    elif boundary == "declined-confirmation":
        assert FakeMessageBox.shown[0].icon == FakeMessageBox.Icon.Question


@pytest.mark.parametrize(
    ("invalid_input", "expected_status"),
    [
        ("malformed-hex", "Firmware checksum error."),
        ("oversized-image", "Firmware file is too large."),
        ("non-ascii", "Firmware checksum error."),
        ("missing-member", "Firmware checksum error."),
        ("missing-custom-file", "Firmware checksum error."),
        ("unreadable-custom-file", "Firmware checksum error."),
    ],
)
def test_v13_update_rejects_invalid_input_and_restores_controls(
    firmware_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_input: str,
    expected_status: str,
) -> None:
    window, transport, settings, events = make_v13_update_window(firmware_module, tmp_path, results=[])
    window.prgStatus.setValue(73)
    if invalid_input in ("missing-custom-file", "unreadable-custom-file"):
        window.optExternal.setChecked(True)
        external_path = tmp_path / "gbxcart_rw_invalid_pcb_r13.hex"
        if invalid_input == "unreadable-custom-file":
            external_path.mkdir()
        monkeypatch.setattr(FakeFileDialog, "next_path", str(external_path))
    else:
        window.optCFW.setChecked(True)
        if invalid_input == "malformed-hex":
            members: dict[str, str | bytes] = {"cfw.hex": ":0100000001FF\n:00000001FF\n"}
        elif invalid_input == "oversized-image":
            members = {"cfw.hex": intel_hex_image(b"X", address=0x1BFF)}
        elif invalid_input == "non-ascii":
            members = {"cfw.hex": b"\xff"}
        else:
            members = {"ofw.hex": intel_hex_image(b"OFW")}
        write_v13_archive(tmp_path, members)
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Yes)

    result = window.UpdateFirmware()

    assert result is False
    assert events == []
    assert transport.payloads == []
    transport.assert_finished()
    window.APP.DisconnectDevice.assert_not_called()
    assert window.lblStatus.text == f"Status: {expected_status}"
    assert window.prgStatus.value == 0
    assert all(control.enabled for control in (window.btnUpdate, window.btnClose, window.grpAvailableFwUpdates))
    assert len(FakeMessageBox.shown) == 1
    assert FakeMessageBox.shown[0].icon == FakeMessageBox.Icon.Question
    if "custom" in invalid_input:
        assert settings.writes == [("LastDirFirmwareUpdate", str(tmp_path))]
    else:
        assert settings.writes == []


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
