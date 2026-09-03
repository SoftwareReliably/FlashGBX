"""Platform-independent tests for the PySide compatibility helpers."""

from __future__ import annotations

import ctypes
import importlib
import importlib.util
import platform
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PIL import Image

PYSIDE_PATH = Path(importlib.import_module("FlashGBX.pyside").__file__)
_MISSING = object()


class FakeStyleHints:
    def __init__(self, scheme: object) -> None:
        self.scheme = scheme

    def colorScheme(self) -> object:
        if isinstance(self.scheme, Exception):
            raise self.scheme
        return self.scheme


class FakeGuiApplication:
    style_hints: FakeStyleHints = FakeStyleHints(1)
    desktop_name: object = ""

    @classmethod
    def styleHints(cls) -> FakeStyleHints:
        return cls.style_hints

    @classmethod
    def desktopFileName(cls) -> object:
        if isinstance(cls.desktop_name, Exception):
            raise cls.desktop_name
        return cls.desktop_name


class FakeApplication:
    current: object = None

    @classmethod
    def instance(cls) -> object:
        return cls.current


class FakePixmap:
    def __init__(self, image: object) -> None:
        self.image = image
        self.device_pixel_ratio: int | None = None

    @classmethod
    def fromImage(cls, image: object) -> FakePixmap:
        return cls(image)

    def setDevicePixelRatio(self, ratio: int) -> None:
        self.device_pixel_ratio = ratio


class FakeDBusMessage:
    def __init__(self, path: str, interface: str, name: str) -> None:
        self.signal = (path, interface, name)
        self.arguments: list[object] = []

    def setArguments(self, arguments: list[object]) -> None:
        self.arguments = arguments


class FakeBus:
    def __init__(self, *, connected: bool = True, send_result: object = True) -> None:
        self.connected = connected
        self.send_result = send_result
        self.messages: list[FakeDBusMessage] = []

    def isConnected(self) -> bool:
        return self.connected

    def send(self, message: FakeDBusMessage) -> bool:
        if isinstance(self.send_result, Exception):
            raise self.send_result
        self.messages.append(message)
        return bool(self.send_result)


def fake_qt_dbus(bus_result: object) -> object:
    class Connection:
        @staticmethod
        def sessionBus() -> object:
            if isinstance(bus_result, Exception):
                raise bus_result
            return bus_result

    return SimpleNamespace(
        QDBusConnection=Connection,
        QDBusMessage=SimpleNamespace(
            createSignal=FakeDBusMessage,
        ),
    )


def fake_pyside(qt_dbus: object = _MISSING) -> ModuleType:
    qt_core = SimpleNamespace(Qt=SimpleNamespace(ColorScheme=SimpleNamespace(Dark=2)))
    qt_gui = SimpleNamespace(QGuiApplication=FakeGuiApplication, QPixmap=FakePixmap)
    qt_widgets = SimpleNamespace(QApplication=FakeApplication)
    module = ModuleType("PySide6")
    module.QtCore = qt_core  # type: ignore[attr-defined]
    module.QtGui = qt_gui  # type: ignore[attr-defined]
    module.QtWidgets = qt_widgets  # type: ignore[attr-defined]
    if qt_dbus is not _MISSING:
        module.QtDBus = qt_dbus  # type: ignore[attr-defined]
    return module


def load_pyside(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    *,
    qt_dbus: object = _MISSING,
) -> ModuleType:
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setitem(sys.modules, "PySide6", fake_pyside(qt_dbus))
    if system == "Windows":
        monkeypatch.setattr(ctypes, "WINFUNCTYPE", lambda *_args: object, raising=False)

    module_name = f"FlashGBX._pyside_test_{system.lower()}_{id(qt_dbus)}"
    spec = importlib.util.spec_from_file_location(module_name, PYSIDE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_noop_backend_and_shared_progress_state(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_pyside(monkeypatch, "Darwin")
    progress = module._TaskbarProgressBase()

    progress.setRange("2", 8)
    progress.setValue("5")
    progress.setVisible(1)
    progress.setPaused(1)

    assert (progress._minimum, progress._maximum, progress._value) == (2, 8, 5)
    assert progress._visible is True
    assert progress._paused is True

    button = module.QtWinExtras.QWinTaskbarButton()
    assert isinstance(button.progress(), module._NoopTaskbarProgress)
    button.setWindow(object())
    module.QtWinExtras.QtWin.setCurrentProcessExplicitAppUserModelID("org.flashgbx")

    namespace = module._QtWinExtrasNamespace(module._NoopTaskbarButton, module._NoopQtWin)
    assert namespace.QWinTaskbarButton is module._NoopTaskbarButton
    assert namespace.QtWin is module._NoopQtWin


def test_hresult_and_optional_logging_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_pyside(monkeypatch, "Darwin")
    module._check_hresult(0, "success")
    module._check_hresult(1, "success")
    with pytest.raises(OSError, match="0xFFFFFFFF"):
        module._check_hresult(-1, "negative")
    with pytest.raises(OSError, match="0x80000000"):
        module._check_hresult(0x80000000, "unsigned")

    monkeypatch.delitem(sys.modules, "FlashGBX.Logging", raising=False)
    module._debug_print("not initialized")
    module._log_exception("not initialized")

    debug = Mock()
    exception = Mock()
    logging_module = SimpleNamespace(dprint=debug, logger=SimpleNamespace(exception=exception))
    monkeypatch.setitem(sys.modules, "FlashGBX.Logging", logging_module)
    module._debug_print("message", value=3)
    module._log_exception("failure")
    debug.assert_called_once_with("message", value=3)
    exception.assert_called_once_with("failure")

    logging_module.dprint = None
    logging_module.logger = None
    module._debug_print("ignored")
    module._log_exception("ignored")


def test_desktop_file_resolution_uses_each_available_source(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_pyside(monkeypatch, "Darwin")

    monkeypatch.setenv("FLASHGBX_DESKTOP_FILE", "/apps/custom.desktop")
    assert module._application_desktop_file() == "custom.desktop"

    monkeypatch.delenv("FLASHGBX_DESKTOP_FILE")
    FakeApplication.current = SimpleNamespace(applicationName=lambda: "Reader App")
    assert module._application_desktop_file() == "Reader App.desktop"

    FakeApplication.current = None
    FakeGuiApplication.desktop_name = "org.flashgbx.desktop"
    assert module._application_desktop_file() == "org.flashgbx.desktop"

    FakeGuiApplication.desktop_name = RuntimeError("Qt unavailable")
    assert module._application_desktop_file() == "flashgbx.desktop"

    FakeGuiApplication.desktopFileName = cast("Any", None)
    assert module._application_desktop_file() == "flashgbx.desktop"

    monkeypatch.setenv("FLASHGBX_DESKTOP_FILE", "/")
    assert module._application_desktop_file() == "flashgbx.desktop"


def test_dark_mode_detection_and_failure_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_pyside(monkeypatch, "Darwin")
    log_exception = Mock()
    monkeypatch.setattr(module, "_log_exception", log_exception)

    FakeGuiApplication.style_hints = FakeStyleHints(module.QtCore.Qt.ColorScheme.Dark)
    assert module.IsDarkMode() is True
    FakeGuiApplication.style_hints = FakeStyleHints(1)
    assert module.IsDarkMode() is False
    FakeGuiApplication.style_hints = FakeStyleHints(RuntimeError("style failure"))
    assert module.IsDarkMode() is False
    log_exception.assert_called_once_with("Failed to determine the Qt color scheme")


def test_bitmap_conversion_scales_pixels_and_handles_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_pyside(monkeypatch, "Darwin")
    image_qt_module = ModuleType("PIL.ImageQt")
    image_qt_module.ImageQt = lambda image: SimpleNamespace(image=image)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PIL.ImageQt", image_qt_module)

    pixmap = module.bitmap2pixmap(Image.new("RGB", (2, 3)), scale_factor=3)

    assert isinstance(pixmap, FakePixmap)
    assert pixmap.image.image.size == (6, 9)
    assert pixmap.device_pixel_ratio == 3
    with pytest.raises(ValueError, match="greater than zero"):
        module.bitmap2pixmap(Image.new("RGB", (1, 1)), scale_factor=0)

    debug = Mock()
    monkeypatch.setattr(module, "_debug_print", debug)
    assert module.bitmap2pixmap(object()) is False
    assert "convert bitmap" in debug.call_args.args[0]


def test_linux_progress_sends_clamped_deduplicated_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus()
    module = load_pyside(monkeypatch, "Linux", qt_dbus=fake_qt_dbus(bus))
    monkeypatch.delenv("FLASHGBX_DESKTOP_FILE", raising=False)
    FakeApplication.current = SimpleNamespace(applicationName=lambda: "flashgbx")
    progress = module._LinuxTaskbarProgress()

    progress.setRange(0, 100)
    progress.setVisible(True)
    progress.setValue(50)
    message_count = len(bus.messages)
    progress.setPaused(True)
    assert len(bus.messages) == message_count
    progress.setValue(150)
    progress.setRange(1, 1)

    payloads = [message.arguments[1] for message in bus.messages]
    assert payloads[0] == {"progress-visible": False, "progress": 0.0}
    assert {"progress-visible": True, "progress": 0.5} in payloads
    assert {"progress-visible": True, "progress": 1.0} in payloads
    assert payloads[-1] == {"progress-visible": False, "progress": 0.0}
    assert bus.messages[0].arguments[0] == "application://flashgbx.desktop"
    assert bus.messages[0].signal == (
        "/com/canonical/unity/launcherentry/1",
        "com.canonical.Unity.LauncherEntry",
        "Update",
    )

    button = module.QtWinExtras.QWinTaskbarButton()
    assert isinstance(button.progress(), module._LinuxTaskbarProgress)
    button.setWindow(object())
    module.QtWinExtras.QtWin.setCurrentProcessExplicitAppUserModelID("org.flashgbx")


@pytest.mark.parametrize(
    ("dbus_result", "expected_text"),
    [
        (None, "QtDBus is unavailable"),
        (RuntimeError("no session"), "no session"),
        (FakeBus(connected=False), "no DBus session bus"),
    ],
)
def test_linux_progress_handles_unavailable_dbus(
    monkeypatch: pytest.MonkeyPatch,
    dbus_result: object,
    expected_text: str,
) -> None:
    qt_dbus = _MISSING if dbus_result is None else fake_qt_dbus(dbus_result)
    module = load_pyside(monkeypatch, "Linux", qt_dbus=qt_dbus)
    debug = Mock()
    monkeypatch.setattr(module, "_debug_print", debug)

    progress = module._LinuxTaskbarProgress()
    progress.setVisible(True)

    assert progress._available is False
    assert expected_text in debug.call_args.args[0]


@pytest.mark.parametrize("send_result", [False, RuntimeError("send failed")])
def test_linux_progress_disables_itself_after_send_failure(
    monkeypatch: pytest.MonkeyPatch,
    send_result: object,
) -> None:
    bus = FakeBus(send_result=send_result)
    module = load_pyside(monkeypatch, "Linux", qt_dbus=fake_qt_dbus(bus))
    debug = Mock()
    monkeypatch.setattr(module, "_debug_print", debug)
    progress = module._LinuxTaskbarProgress()

    progress.setVisible(True)

    assert progress._available is False
    assert debug.called


class FakeOle32:
    def __init__(self, *, create_interface: bool = True) -> None:
        self.create_interface = create_interface
        self.calls: list[str] = []

    def CoInitialize(self, _reserved: object) -> int:
        self.calls.append("initialize")
        return 0

    def CLSIDFromString(self, _value: object, _output: object) -> int:
        self.calls.append("parse-guid")
        return 0

    def CoCreateInstance(
        self,
        _clsid: object,
        _outer: object,
        _context: int,
        _iid: object,
        output: object,
    ) -> int:
        self.calls.append("create")
        if self.create_interface:
            cast("Any", output)._obj.value = 1234
        return 0


def test_windows_progress_binding_and_state_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_pyside(monkeypatch, "Windows")
    ole32 = FakeOle32()
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(ole32=ole32), raising=False)
    module._WINFUNCTYPE = lambda *_args: object()
    progress = module._WindowsTaskbarProgress()
    progress._call = Mock(return_value=0)

    progress._bind(42)
    assert ole32.calls == ["initialize", "parse-guid", "parse-guid", "create"]
    assert progress._taskbar.value == 1234

    progress.setVisible(True)
    progress.setValue(150)
    progress.setPaused(True)
    progress.setRange(5, 5)
    progress.setVisible(False)
    assert progress._call.call_count >= 7

    progress._taskbar = None
    assert progress._call(3, object()) == 0
    module._WINFUNCTYPE = None
    progress._apply()


def test_windows_binding_rejects_missing_interface_or_prototypes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_pyside(monkeypatch, "Windows")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(ole32=FakeOle32(create_interface=False)),
        raising=False,
    )
    progress = module._WindowsTaskbarProgress()
    with pytest.raises(RuntimeError, match="empty taskbar interface"):
        progress._bind(1)

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(ole32=FakeOle32()), raising=False)
    module._WINFUNCTYPE = None
    with pytest.raises(RuntimeError, match="prototypes are unavailable"):
        module._WindowsTaskbarProgress()._bind(1)


def test_windows_vtable_call_dispatches_through_supplied_prototype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_pyside(monkeypatch, "Windows")
    progress = module._WindowsTaskbarProgress()
    prototype = Mock()
    function = Mock(return_value=17)
    prototype.return_value = function

    module._WINFUNCTYPE = object()
    assert progress._call(1, prototype) is None
    progress._taskbar = object()
    module._WINFUNCTYPE = None
    assert progress._call(1, prototype) is None
    progress._apply()

    module._WINFUNCTYPE = object()
    monkeypatch.setattr(module.ctypes, "cast", lambda *_args: [[100, 200, 300]])
    assert progress._call(1, prototype, "argument") == 17
    prototype.assert_called_once_with(200)
    function.assert_called_once_with(progress._taskbar, "argument")


def test_windows_button_and_application_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_pyside(monkeypatch, "Windows")
    button = module.QtWinExtras.QWinTaskbarButton()
    button._progress._bind = Mock()
    button.setWindow(None)
    button.setWindow(SimpleNamespace(winId=lambda: 77))
    button._progress._bind.assert_called_once_with(77)
    assert button.progress() is button._progress

    shell32 = SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=Mock(return_value=0))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=shell32), raising=False)
    module.QtWinExtras.QtWin.setCurrentProcessExplicitAppUserModelID("org.flashgbx")
    assert shell32.SetCurrentProcessExplicitAppUserModelID.called

    shell32.SetCurrentProcessExplicitAppUserModelID.return_value = -1
    with pytest.raises(OSError, match="SetCurrentProcessExplicitAppUserModelID"):
        module.QtWinExtras.QtWin.setCurrentProcessExplicitAppUserModelID("org.flashgbx")
