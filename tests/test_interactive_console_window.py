"""Tests for the interactive console dialog using inert Qt widgets."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

interactive_console_window_module = importlib.import_module("FlashGBX.InteractiveConsoleWindow")


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)


class FakeFont:
    def __init__(self) -> None:
        self.style_hint: object | None = None

    def setStyleHint(self, style_hint: object) -> None:
        self.style_hint = style_hint


class FakeFontDatabase:
    SystemFont = SimpleNamespace(FixedFont=1)
    FixedFont = SystemFont.FixedFont

    @staticmethod
    def systemFont(_font_type: object) -> FakeFont:
        return FakeFont()


class FakeGeometry:
    def x(self) -> int:
        return 100

    def y(self) -> int:
        return 200

    def width(self) -> int:
        return 1000

    def height(self) -> int:
        return 800


class FakeScreen:
    def availableGeometry(self) -> FakeGeometry:
        return FakeGeometry()


class FakeLayout:
    def __init__(self) -> None:
        self.widgets: list[object] = []
        self.layouts: list[object] = []
        self.update_count = 0
        self.activate_count = 0

    def setContentsMargins(self, *_margins: int) -> None:
        pass

    def addWidget(self, widget: object) -> None:
        self.widgets.append(widget)

    def addLayout(self, layout: object) -> None:
        self.layouts.append(layout)

    def addStretch(self) -> None:
        pass

    def update(self) -> None:
        self.update_count += 1

    def activate(self) -> None:
        self.activate_count += 1


class FakeDialog:
    DialogCode = SimpleNamespace(Accepted=1, Rejected=0)

    def __init__(self, parent: object) -> None:
        self.parent = parent
        self._flags = 0
        self._width = 0
        self._height = 0
        self._result = self.DialogCode.Rejected
        self.visible = False
        self.position: tuple[int, int] | None = None
        self.hidden_event: object | None = None

    def setWindowIcon(self, _icon: object) -> None:
        pass

    def setWindowTitle(self, _title: str) -> None:
        pass

    def windowFlags(self) -> int:
        return self._flags

    def setWindowFlags(self, flags: int) -> None:
        self._flags = flags

    def resize(self, width: int, height: int) -> None:
        self._width = width
        self._height = height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height

    def setLayout(self, layout: object) -> None:
        self.assigned_layout = layout

    def screen(self) -> FakeScreen:
        return FakeScreen()

    def move(self, x: int, y: int) -> None:
        assert isinstance(x, int)
        assert isinstance(y, int)
        self.position = (x, y)

    def show(self) -> None:
        self.visible = True

    def reject(self) -> None:
        self._result = self.DialogCode.Rejected

    def setResult(self, result: int) -> None:
        self._result = result

    def result(self) -> int:
        return self._result

    def eventFilter(self, _obj: object, _event: object) -> bool:
        return False

    def hideEvent(self, event: object) -> None:
        self.hidden_event = event


class FakeScrollBar:
    def __init__(self) -> None:
        self.value = 0

    def maximum(self) -> int:
        return 100

    def setValue(self, value: int) -> None:
        self.value = value


class FakePlainTextEdit:
    LineWrapMode = SimpleNamespace(NoWrap=0)

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.scrollbar = FakeScrollBar()

    def setReadOnly(self, _read_only: bool) -> None:
        pass

    def setFont(self, _font: object) -> None:
        pass

    def setLineWrapMode(self, _mode: object) -> None:
        pass

    def appendPlainText(self, text: str) -> None:
        self.lines.append(text)

    def toPlainText(self) -> str:
        return "\n".join(self.lines)

    def verticalScrollBar(self) -> FakeScrollBar:
        return self.scrollbar


class FakeLineEdit:
    def __init__(self) -> None:
        self.returnPressed = FakeSignal()
        self._text = ""
        self._enabled = True
        self.focus_count = 0
        self.event_filter: object | None = None

    def setFont(self, _font: object) -> None:
        pass

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text

    def clear(self) -> None:
        self._text = ""

    def setEnabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def isEnabled(self) -> bool:
        return self._enabled

    def setFocus(self) -> None:
        self.focus_count += 1

    def installEventFilter(self, event_filter: object) -> None:
        self.event_filter = event_filter


class FakeButton:
    def __init__(self, _label: str) -> None:
        self.clicked = FakeSignal()
        self._enabled = True

    def setStyleSheet(self, _stylesheet: str) -> None:
        pass

    def setAutoDefault(self, _enabled: bool) -> None:
        pass

    def setDefault(self, _enabled: bool) -> None:
        pass

    def setEnabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def isEnabled(self) -> bool:
        return self._enabled


class FakeLabel:
    def __init__(self, _label: str) -> None:
        pass

    def setFont(self, _font: object) -> None:
        pass


class FakeKeyEvent:
    def __init__(self, key: int) -> None:
        self._key = key

    def key(self) -> int:
        return self._key


class ConsoleDevice:
    def __init__(self, mode: str | None = "AGB", power_error: Exception | None = None) -> None:
        self.mode = mode
        self.power_error = power_error
        self.auto_power_off_values: list[int] = []
        self.power_on_count = 0

    def GetMode(self) -> str | None:
        return self.mode

    def CanPowerCycleCart(self) -> bool:
        return True

    def SetAutoPowerOff(self, value: int) -> None:
        self.auto_power_off_values.append(value)

    def CartPowerOn(self) -> bool:
        self.power_on_count += 1
        if self.power_error is not None:
            raise self.power_error
        return True


class HostWindow:
    def __init__(self, connection: ConsoleDevice | None) -> None:
        self.CONN = connection
        self.restore_count = 0
        self.activate_count = 0
        self.restore_error: Exception | None = None
        self.activate_error: Exception | None = None

    def SetAutoPowerOff(self) -> None:
        self.restore_count += 1
        if self.restore_error is not None:
            raise self.restore_error

    def activateWindow(self) -> None:
        self.activate_count += 1
        if self.activate_error is not None:
            raise self.activate_error


class StubConsole:
    def __init__(self, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.lines: list[str] = []

    def execute_line(self, line: str) -> bool:
        self.lines.append(line)
        if self.error is not None:
            raise self.error
        return self.result


def load_window_with_fake_qt(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    qt_core = SimpleNamespace(
        Qt=SimpleNamespace(
            WindowType=SimpleNamespace(
                WindowContextHelpButtonHint=1,
                WindowCloseButtonHint=2,
                WindowMaximizeButtonHint=4,
            ),
            Key=SimpleNamespace(Key_Up=1, Key_Down=2),
        ),
        QObject=object,
        QEvent=object,
    )
    qt_gui = SimpleNamespace(
        QIcon=lambda icon: icon,
        QFontDatabase=FakeFontDatabase,
        QFont=SimpleNamespace(StyleHint=SimpleNamespace(TypeWriter=1)),
        QGuiApplication=SimpleNamespace(primaryScreen=FakeScreen),
        QKeyEvent=FakeKeyEvent,
        QHideEvent=object,
    )
    qt_widgets = SimpleNamespace(
        QDialog=FakeDialog,
        QVBoxLayout=FakeLayout,
        QHBoxLayout=FakeLayout,
        QPlainTextEdit=FakePlainTextEdit,
        QLabel=FakeLabel,
        QLineEdit=FakeLineEdit,
        QPushButton=FakeButton,
    )
    fake_pyside = ModuleType("PySide6")
    fake_pyside.QtCore = qt_core  # type: ignore[attr-defined]
    fake_pyside.QtGui = qt_gui  # type: ignore[attr-defined]
    fake_pyside.QtWidgets = qt_widgets  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PySide6", fake_pyside)

    module_name = "FlashGBX._InteractiveConsoleWindowFakeQt"
    module_path = Path(interactive_console_window_module.__file__)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_constructor_requires_an_active_connection_and_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_window_with_fake_qt(monkeypatch)
    with pytest.raises(RuntimeError, match="active device connection"):
        module.InteractiveConsoleWindow(HostWindow(None))

    with pytest.raises(RuntimeError, match="active cartridge mode"):
        module.InteractiveConsoleWindow(HostWindow(ConsoleDevice(mode=None)))


def test_run_initializes_hardware_help_and_window(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_window_with_fake_qt(monkeypatch)
    device = ConsoleDevice()
    app = HostWindow(device)
    window = module.InteractiveConsoleWindow(app, icon=object())

    window.run()

    assert device.auto_power_off_values == [0]
    assert device.power_on_count == 1
    assert "Interactive Console" in window.txtOutput.toPlainText()
    assert window.visible is True
    assert window.position == (250, 400)
    assert window.main_layout.update_count == 1
    assert window.main_layout.activate_count == 1

    event = object()
    window.hideEvent(event)
    assert app.restore_count == 1
    assert app.activate_count == 1
    assert window.hidden_event is event

    window.screen = lambda: None
    module.QtGui.QGuiApplication.primaryScreen = lambda: None
    window.position = None
    window.run()
    assert window.visible is True
    assert window.position is None


def test_run_restores_auto_power_off_after_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_window_with_fake_qt(monkeypatch)
    device = ConsoleDevice(power_error=RuntimeError("power failure"))
    app = HostWindow(device)
    window = module.InteractiveConsoleWindow(app)

    with pytest.raises(RuntimeError, match="power failure"):
        window.run()

    assert device.auto_power_off_values == [0]
    assert app.restore_count == 1
    assert window.visible is False


def test_submit_tracks_history_and_restores_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_window_with_fake_qt(monkeypatch)
    window = module.InteractiveConsoleWindow(HostWindow(ConsoleDevice()))
    console = StubConsole()
    window.IM = console

    window.txtInput.setText("  h  ")
    window.OnSubmit()
    assert console.lines == ["h"]
    assert window.History == ["h"]
    assert window.HistoryIndex == 1
    assert window.txtOutput.toPlainText().endswith("> h")
    assert window.txtOutput.scrollbar.value == 100
    assert window.txtInput.isEnabled() is True
    assert window.btnClose.isEnabled() is True

    window.txtInput.setText("h")
    window.OnSubmit()
    assert window.History == ["h"]

    window.txtInput.setText("   ")
    window.OnSubmit()
    assert console.lines == ["h", "h"]
    assert window.txtOutput.toPlainText().endswith("\n")

    failing_console = StubConsole(error=ValueError("bad command"))
    window.IM = failing_console
    window.txtInput.setText("broken")
    with pytest.raises(ValueError, match="bad command"):
        window.OnSubmit()
    assert window.txtInput.isEnabled() is True
    assert window.btnClose.isEnabled() is True


def test_submit_rejects_on_quit_and_history_keys_navigate(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_window_with_fake_qt(monkeypatch)
    window = module.InteractiveConsoleWindow(HostWindow(ConsoleDevice()))
    window.IM = StubConsole(result=False)
    window.setResult(window.DialogCode.Accepted)
    window.txtInput.setText("q")
    window.OnSubmit()
    assert window.result() == window.DialogCode.Rejected

    window.History = ["first", "second"]
    window.HistoryIndex = len(window.History)
    up = FakeKeyEvent(1)
    down = FakeKeyEvent(2)

    assert window.eventFilter(window.txtInput, up) is True
    assert window.txtInput.text() == "second"
    assert window.eventFilter(window.txtInput, up) is True
    assert window.txtInput.text() == "first"
    assert window.eventFilter(window.txtInput, down) is True
    assert window.txtInput.text() == "second"
    assert window.eventFilter(window.txtInput, down) is True
    assert window.txtInput.text() == ""
    window.History = []
    window.HistoryIndex = 0
    assert window.eventFilter(window.txtInput, up) is True
    assert window.eventFilter(window.txtInput, FakeKeyEvent(99)) is False
    assert window.eventFilter(object(), object()) is False


def test_hide_event_contains_restore_and_activation_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_window_with_fake_qt(monkeypatch)
    module.logger.exception = Mock()
    app = HostWindow(ConsoleDevice())
    app.restore_error = RuntimeError("restore failure")
    app.activate_error = RuntimeError("activation failure")
    window = module.InteractiveConsoleWindow(app)
    event = object()

    window.hideEvent(event)

    assert app.restore_count == 1
    assert app.activate_count == 1
    assert window.hidden_event is event
    assert module.logger.exception.call_count == 2
