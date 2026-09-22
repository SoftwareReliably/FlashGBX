"""Headless tests for the Qt graphical frontend."""

from __future__ import annotations

import importlib
import importlib.util
import json
import queue
import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[Callable[..., object]] = []

    def connect(self, callback: Callable[..., object]) -> None:
        self.callbacks.append(callback)

    def emit(self, *args: object) -> None:
        for callback in self.callbacks:
            callback(*args)


class FakeMargins:
    def left(self) -> int:
        return 0

    def right(self) -> int:
        return 0


class FakeRect:
    def __init__(self, width: int = 640, height: int = 480) -> None:
        self._width = width
        self._height = height

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height

    def center(self) -> tuple[int, int]:
        return (self._width // 2, self._height // 2)

    def moveCenter(self, _center: object) -> None:
        pass

    def topLeft(self) -> tuple[int, int]:
        return (0, 0)


class FakeMetrics:
    def horizontalAdvance(self, text: str) -> int:
        return len(text) * 7

    def boundingRect(self, text: str) -> FakeRect:
        return FakeRect(len(text) * 7, 12)

    def height(self) -> int:
        return 12

    def elidedText(self, text: str, _mode: object, width: int) -> str:
        characters = max(width // 7, 0)
        return text if len(text) <= characters else text[: max(characters - 1, 0)] + "…"


class FakeQtObject:
    """Stateful, permissive stand-in for Qt widgets and layouts."""

    SizeAdjustPolicy = SimpleNamespace(AdjustToContents=0)
    DialogCode = SimpleNamespace(Accepted=1, Rejected=0)

    def __init__(self, *args: object, **_kwargs: object) -> None:
        self.clicked = FakeSignal()
        self.triggered = FakeSignal()
        self.currentIndexChanged = FakeSignal()
        self.stateChanged = FakeSignal()
        self.valueChanged = FakeSignal()
        self.timeout = FakeSignal()
        self._actions: list[FakeQtObject] = []
        self._items: list[tuple[str, object]] = []
        self._text = args[0] if args and isinstance(args[0], str) else ""
        self._checked = False
        self._enabled = True
        self._visible = True
        self._index = -1
        self._value = 0
        self._minimum_width = 0
        self._maximum_width = 16_777_215
        self._spacing = 0
        self._stylesheet = ""
        self._result = 1
        self.menu: FakeQtObject | None = None
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, name: str) -> Callable[..., FakeQtObject]:
        """Return an inert callable for unsupported Qt methods."""

        def method(*args: object, **_kwargs: object) -> FakeQtObject:
            self.calls.append((name, args))
            return self

        return method

    def addAction(self, *args: object) -> FakeQtObject:
        action = FakeQtObject(args[0] if args and isinstance(args[0], str) else "")
        self._actions.append(action)
        return action

    def addSeparator(self) -> FakeQtObject:
        action = FakeQtObject()
        self._actions.append(action)
        return action

    def addMenu(self, menu: FakeQtObject) -> FakeQtObject:
        action = FakeQtObject()
        action.menu = menu
        self._actions.append(action)
        return action

    def actions(self) -> list[FakeQtObject]:
        return self._actions

    def addItem(self, text: str, data: object = None) -> None:
        self._items.append((text, data))
        self._index = max(self._index, 0)

    def addItems(self, texts: list[str]) -> None:
        for text in texts:
            self.addItem(text)

    def insertItem(self, index: int, text: str, data: object = None) -> None:
        self._items.insert(index, (text, data))

    def clear(self) -> None:
        self._items.clear()
        self._text = ""
        self._index = -1

    def count(self) -> int:
        return len(self._items)

    def itemData(self, index: int) -> object:
        return self._items[index][1]

    def itemText(self, index: int) -> str:
        return self._items[index][0]

    def currentData(self) -> object:
        return self.itemData(self._index) if 0 <= self._index < len(self._items) else None

    def currentText(self) -> str:
        return self.itemText(self._index) if 0 <= self._index < len(self._items) else self._text

    def findData(self, data: object) -> int:
        return next((index for index, item in enumerate(self._items) if item[1] == data), -1)

    def setCurrentIndex(self, index: int) -> None:
        self._index = index

    def currentIndex(self) -> int:
        return self._index

    def setText(self, text: object) -> None:
        self._text = str(text)

    def text(self) -> str:
        return str(self._text)

    def setChecked(self, checked: bool) -> None:
        self._checked = checked

    def isChecked(self) -> bool:
        return self._checked

    def setEnabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def isEnabled(self) -> bool:
        return self._enabled

    def setVisible(self, visible: bool) -> None:
        self._visible = visible

    def isVisible(self) -> bool:
        return self._visible

    def setValue(self, value: float) -> None:
        self._value = value

    def value(self) -> int | float:
        return self._value

    def setMinimumWidth(self, width: int) -> None:
        self._minimum_width = width

    def setMaximumWidth(self, width: int) -> None:
        self._maximum_width = width

    def minimumWidth(self) -> int:
        return self._minimum_width

    def maximumWidth(self) -> int:
        return self._maximum_width

    def setSpacing(self, spacing: int) -> None:
        self._spacing = spacing

    def spacing(self) -> int:
        return self._spacing

    def setStyleSheet(self, stylesheet: str) -> None:
        self._stylesheet = stylesheet

    def styleSheet(self) -> str:
        return self._stylesheet

    def geometry(self) -> FakeRect:
        return FakeRect()

    def frameGeometry(self) -> FakeRect:
        return FakeRect()

    def sizeHint(self) -> FakeRect:
        return FakeRect(max(len(self.text()) * 7, 1), 12)

    def fontMetrics(self) -> FakeMetrics:
        return FakeMetrics()

    def contentsMargins(self) -> FakeMargins:
        return FakeMargins()

    def view(self) -> FakeQtObject:
        return self

    def style(self) -> FakeQtObject:
        return self

    def pixelMetric(self, _metric: object) -> int:
        return 6

    def screen(self) -> FakeQtObject:
        return self

    def availableGeometry(self) -> FakeRect:
        return FakeRect()

    def size(self) -> tuple[int, int]:
        return (640, 480)

    def exec(self) -> int:
        return self._result

    def result(self) -> int:
        return self._result

    def setResult(self, result: int) -> None:
        self._result = result

    def windowFlags(self) -> int:
        return 0


class FakeColor:
    def toTuple(self) -> tuple[int, int, int, int]:
        return (0, 0, 0, 255)


class FakePalette(FakeQtObject):
    ColorRole = SimpleNamespace(Text=0)

    def color(self, _role: object) -> FakeColor:
        return FakeColor()


class FakeMessageBox(FakeQtObject):
    Icon = SimpleNamespace(NoIcon=0, Information=1, Warning=2, Critical=3, Question=4)
    StandardButton = SimpleNamespace(NoButton=0, Ok=1, Yes=2, No=4, Cancel=8, Close=16)
    ButtonRole = SimpleNamespace(AcceptRole=0, RejectRole=1, ActionRole=2)
    next_answer = StandardButton.Yes

    @classmethod
    def information(cls, *_args: object) -> int:
        return cls.next_answer

    @classmethod
    def warning(cls, *_args: object) -> int:
        return cls.next_answer

    @classmethod
    def critical(cls, *_args: object) -> int:
        return cls.next_answer

    @classmethod
    def question(cls, *_args: object) -> int:
        return cls.next_answer

    @classmethod
    def about(cls, *_args: object) -> int:
        return cls.next_answer

    @classmethod
    def aboutQt(cls, *_args: object) -> int:
        return cls.next_answer


class FakeDecisionMessageBox(FakeMessageBox):
    """Record custom buttons and return one named user decision."""

    def __init__(self, decision: str | None = None) -> None:
        super().__init__()
        self.decision = decision
        self.buttons: dict[str, FakeQtObject] = {}
        self.exec_count = 0
        self._clicked_button: FakeQtObject | None = None

    def addButton(self, text: str, _role: object) -> FakeQtObject:
        button = FakeQtObject(text)
        if "Keep existing" in text:
            name = "keep"
        elif "Force recalibration" in text:
            name = "reset"
        elif "Cancel" in text:
            name = "cancel"
        else:
            name = "overwrite"
        self.buttons[name] = button
        return button

    def exec(self) -> int:
        self.exec_count += 1
        if self.decision is not None:
            self._clicked_button = self.buttons[self.decision]
        return super().exec()

    def clickedButton(self) -> FakeQtObject | None:
        return self._clicked_button


class FakeApplication(FakeQtObject):
    _clipboard = FakeQtObject()

    @classmethod
    def clipboard(cls) -> FakeQtObject:
        return cls._clipboard

    @classmethod
    def keyboardModifiers(cls) -> int:
        return 0


class FakeGuiApplication(FakeApplication):
    @classmethod
    def styleHints(cls) -> FakeQtObject:
        return FakeQtObject()

    @classmethod
    def primaryScreen(cls) -> FakeQtObject:
        return FakeQtObject()

    @classmethod
    def setDesktopFileName(cls, _name: str) -> None:
        pass


class FakeTimer(FakeQtObject):
    @staticmethod
    def singleShot(_milliseconds: int, _callback: Callable[[], object]) -> None:
        pass


class FakeUrl:
    def __init__(self, value: str) -> None:
        self.value = value

    def toLocalFile(self) -> str:
        return self.value

    def toString(self) -> str:
        return self.value

    def path(self) -> str:
        return self.value


class FakeMimeData:
    def __init__(self, paths: list[str]) -> None:
        self._urls = [FakeUrl(path) for path in paths]

    def hasUrls(self) -> bool:
        return bool(self._urls)

    def urls(self) -> list[FakeUrl]:
        return self._urls


class FakeDropEvent:
    def __init__(self, paths: list[str]) -> None:
        self._mime_data = FakeMimeData(paths)
        self.accepted = False
        self.ignored = False
        self.drop_action: object = None

    def mimeData(self) -> FakeMimeData:
        return self._mime_data

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True

    def setDropAction(self, action: object) -> None:
        self.drop_action = action


class FakeSettings:
    def __init__(self, values: dict[str, object] | None = None, **_kwargs: object) -> None:
        self.values = {} if values is None else dict(values)
        self.writes: list[tuple[str, object]] = []

    def value(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def setValue(self, key: str, value: object) -> None:
        if value is None:
            self.values.pop(key, None)
        else:
            self.values[key] = value
        self.writes.append((key, value))

    def clear(self) -> None:
        self.values.clear()


class FakeDevice:
    """Mock the shared cartridge-reader protocol used by the GUI."""

    DEVICE_ID = "gbxcartrw"
    DEVICE_NAME: str = "GBxCart RW"

    def __init__(self, mode: str = "DMG") -> None:
        self.mode = mode
        self.INFO: dict[str, Any] = {}
        self.FW: dict[str, Any] = {"fw_ver": 12}
        self.FW_UPDATE_REQ: bool | int = False
        self.DEVICE: object | None = object()
        self.CANCEL = False
        self.ERROR = False
        self.USER_ANSWER: bool | None = None
        self.WORKER = SimpleNamespace(isRunning=lambda: False)
        self.calls: list[tuple[str, object]] = []
        self.header: dict[str, Any] = {}
        self.initialize_result: object = True
        self.connected = True
        self.active = True
        self.supported_modes = ["DMG", "AGB"]

    def GetMode(self) -> str | None:
        return self.mode

    def SetMode(self, mode: str) -> None:
        self.mode = mode
        self.calls.append(("mode", mode))

    def GetSupprtedModes(self) -> list[str]:
        return self.supported_modes

    def GetCartModeSwitchState(self) -> int | bool:
        return cast("int | bool", self.INFO.get("switch_state", False))

    def CanSetVoltageByAutoswitch(self) -> bool:
        return bool(self.INFO.get("voltage_autoswitch", False))

    def CanSetVoltageByCode(self) -> bool:
        return bool(self.INFO.get("voltage_code", True))

    def CanSetVoltageBySwitch(self) -> bool:
        return bool(self.INFO.get("voltage_switch", False))

    def CanPowerCycleCart(self) -> bool:
        return True

    def SetAutoPowerOff(self, *, value: int) -> None:
        self.calls.append(("auto_power_off", value))

    def SetDMGReadMethod(self, method: int) -> None:
        self.calls.append(("dmg_read", method))

    def SetAGBReadMethod(self, method: int) -> None:
        self.calls.append(("agb_read", method))

    def SetWriteDelay(self, *, enable: bool) -> None:
        self.calls.append(("write_delay", enable))

    def SetTimeout(self, timeout: float) -> None:
        self.calls.append(("timeout", timeout))

    def ChangeBaudRate(self, *, baudrate: int) -> None:
        self.calls.append(("baud", baudrate))

    def Initialize(self, _flashcarts: object, *, port: str, max_baud: int) -> object:
        self.calls.append(("initialize", (port, max_baud)))
        if isinstance(self.initialize_result, Exception):
            raise self.initialize_result
        return self.initialize_result

    def CheckActive(self) -> bool:
        return self.active

    def IsConnected(self) -> bool:
        return self.connected

    def GetPort(self) -> str:
        return "mock-port"

    def Close(self, cartPowerOff: bool = False) -> None:
        self.calls.append(("close", cartPowerOff))

    def GetName(self) -> str:
        return self.DEVICE_NAME

    def GetFullName(self) -> str:
        return "Mock Reader"

    def GetFullNameLabel(self) -> str:
        return "Mock Reader"

    def GetFullNameExtended(self, more: bool = False) -> str:
        return "Mock Reader (details)" if more else "Mock Reader"

    def GetFirmwareVersion(self, more: bool = False) -> str:
        return "1.0 details" if more else "1.0"

    def GetFWBuildDate(self) -> str:
        return "2026-01-01"

    def SupportsFirmwareUpdates(self) -> bool:
        return bool(self.INFO.get("supports_updates", False))

    def FirmwareUpdateAvailable(self) -> bool:
        return bool(self.INFO.get("update_available", False))

    def IsUnregistered(self) -> bool:
        return False

    def GetSupportMessage(self) -> str | None:
        return cast("str | None", self.INFO.get("support_message"))

    def ReadHeader(self) -> dict[str, Any]:
        if isinstance(self.header, Exception):
            raise self.header
        return self.header

    def CheckROMStable(self) -> bool:
        return bool(self.INFO.get("stable", True))

    def IsSupported3dMemory(self) -> bool:
        return bool(self.INFO.get("supported_3d", True))

    def GetSupportedCartridgesDMG(self) -> tuple[list[str], list[dict[str, Any]]]:
        return cast(
            "tuple[list[str], list[dict[str, Any]]]",
            self.INFO.get(
                "dmg_carts",
                (["Generic", "Mock Profile"], [{}, {"type": "DMG", "names": ["Mock Profile"]}]),
            ),
        )

    def GetSupportedCartridgesAGB(self) -> tuple[list[str], list[dict[str, Any]]]:
        return cast(
            "tuple[list[str], list[dict[str, Any]]]",
            self.INFO.get(
                "agb_carts",
                (["Generic", "Mock Profile"], [{}, {"type": "AGB", "names": ["Mock Profile"]}]),
            ),
        )

    def GetDumpReport(self) -> str | bool:
        return cast("str | bool", self.INFO.get("dump_report", False))

    def AbortOperation(self) -> None:
        self.calls.append(("abort", True))

    def DetectCartridge(self, *, fncSetProgress: object, args: dict[str, object]) -> None:
        del fncSetProgress
        self.calls.append(("detect", args))

    def FlashROM(self, *, fncSetProgress: object, args: dict[str, object]) -> None:
        del fncSetProgress
        self.calls.append(("flash", args))

    def BackupROM(self, *, fncSetProgress: object, args: dict[str, object]) -> None:
        del fncSetProgress
        self.calls.append(("backup_rom", args))

    def BackupRAM(self, *, fncSetProgress: object, args: dict[str, object]) -> None:
        del fncSetProgress
        self.calls.append(("backup_ram", args))

    def RestoreRAM(self, *, fncSetProgress: object, args: dict[str, object]) -> None:
        del fncSetProgress
        self.calls.append(("restore_ram", args))


def fake_qt_modules() -> tuple[ModuleType, object, object, object]:
    enum = SimpleNamespace(
        ColorScheme=SimpleNamespace(Light=0),
        CursorShape=SimpleNamespace(PointingHandCursor=0, ArrowCursor=1),
        AlignmentFlag=SimpleNamespace(AlignCenter=1, AlignVCenter=2, AlignRight=4),
        ScrollBarPolicy=SimpleNamespace(ScrollBarAsNeeded=0),
        TextElideMode=SimpleNamespace(ElideRight=0),
        TextFormat=SimpleNamespace(RichText=0),
        WindowType=SimpleNamespace(WindowMaximizeButtonHint=1, MSWindowsFixedSizeDialogHint=2),
        WidgetAttribute=SimpleNamespace(WA_DeleteOnClose=0),
        DropAction=SimpleNamespace(CopyAction=0),
        KeyboardModifier=SimpleNamespace(ControlModifier=1, ShiftModifier=2),
        MouseButton=SimpleNamespace(LeftButton=1, MiddleButton=2, RightButton=4),
        ItemDataRole=SimpleNamespace(UserRole=0),
        CheckState=SimpleNamespace(Checked=2),
        TextInteractionFlag=SimpleNamespace(TextBrowserInteraction=1),
    )
    qt_core = SimpleNamespace(Qt=enum, QTimer=FakeTimer, QUrl=FakeUrl)
    qt_gui = SimpleNamespace(
        QPalette=FakePalette,
        QGuiApplication=FakeGuiApplication,
        QActionGroup=FakeQtObject,
        QIcon=FakeQtObject,
        QTextLayout=FakeQtObject,
        QFont=FakeQtObject,
        QDesktopServices=SimpleNamespace(openUrl=lambda _url: True),
        QCursor=FakeQtObject,
        QMouseEvent=FakeQtObject,
        QTextDocument=FakeQtObject,
    )
    qt_widgets = SimpleNamespace(
        QApplication=FakeApplication,
        QMainWindow=FakeQtObject,
        QWidget=FakeQtObject,
        QGridLayout=FakeQtObject,
        QVBoxLayout=FakeQtObject,
        QHBoxLayout=FakeQtObject,
        QGroupBox=FakeQtObject,
        QLabel=FakeQtObject,
        QRadioButton=FakeQtObject,
        QPushButton=FakeQtObject,
        QProgressBar=FakeQtObject,
        QComboBox=FakeQtObject,
        QMenu=FakeQtObject,
        QCheckBox=FakeQtObject,
        QMessageBox=FakeMessageBox,
        QSizePolicy=SimpleNamespace(Policy=SimpleNamespace(Expanding=0, Preferred=1, Fixed=2)),
        QStyle=SimpleNamespace(PixelMetric=SimpleNamespace(PM_LayoutHorizontalSpacing=0)),
        QFileDialog=FakeQtObject,
        QInputDialog=FakeQtObject,
        QDialog=FakeQtObject,
    )
    fake_pyside = ModuleType("PySide6")
    fake_pyside.QtCore = qt_core  # type: ignore[attr-defined]
    fake_pyside.QtGui = qt_gui  # type: ignore[attr-defined]
    fake_pyside.QtWidgets = qt_widgets  # type: ignore[attr-defined]
    return fake_pyside, qt_core, qt_gui, qt_widgets


@pytest.fixture(scope="module")
def gui_module() -> Generator[ModuleType]:
    """Load the GUI module once against inert Qt classes."""
    importlib.import_module("FlashGBX.InteractiveConsoleWindow")
    importlib.import_module("FlashGBX.PocketCameraWindow")
    importlib.import_module("FlashGBX.UserInputDialog")
    i18n_module = importlib.import_module("FlashGBX.i18n")
    original_translation_loader = i18n_module.loadQtTranslation
    cast("Any", i18n_module).loadQtTranslation = lambda *_args, **_kwargs: True
    original_pyside = sys.modules["PySide6"]
    original_gui = sys.modules.get("FlashGBX.FlashGBX_GUI")
    fake_pyside, _, _, _ = fake_qt_modules()
    sys.modules["PySide6"] = fake_pyside
    try:
        source = importlib.util.find_spec("FlashGBX.FlashGBX_GUI")
        assert source is not None
        assert source.origin is not None
        module_name = "FlashGBX.FlashGBX_GUI"
        spec = importlib.util.spec_from_file_location(module_name, source.origin)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        cast("Any", module).IniSettings = FakeSettings
        yield module
    finally:
        cast("Any", i18n_module).loadQtTranslation = original_translation_loader
        sys.modules["PySide6"] = original_pyside
        if original_gui is None:
            sys.modules.pop("FlashGBX.FlashGBX_GUI", None)
        else:
            sys.modules["FlashGBX.FlashGBX_GUI"] = original_gui


def make_gui(gui_module: ModuleType, **attributes: object):
    gui = object.__new__(gui_module.FlashGBX_GUI)
    FakeQtObject.__init__(gui)
    for name, value in attributes.items():
        setattr(gui, name, value)
    return gui


def install_batteryless_dialog(
    monkeypatch: pytest.MonkeyPatch,
    gui_module: ModuleType,
    *,
    accepted: bool = True,
    selections: dict[str, int | str] | None = None,
) -> list[dict[str, object]]:
    """Install a recording dialog that returns the existing fake controls."""
    recorded: list[dict[str, object]] = []
    chosen = {} if selections is None else selections

    class RecordingUserInputDialog:
        def __init__(self, _parent: object, *, icon: object, args: dict[str, object]) -> None:
            del icon
            controls: dict[str, FakeQtObject] = {}
            for param in cast("list[list[object]]", args["params"]):
                control = FakeQtObject()
                control.addItems(cast("list[str]", param[3]))
                control.setCurrentIndex(cast("int", param[4]))
                selection = chosen.get(cast("str", param[0]))
                if isinstance(selection, str):
                    control.setCurrentIndex(-1)
                    control.setText(selection)
                elif isinstance(selection, int):
                    control.setCurrentIndex(selection)
                controls[cast("str", param[0])] = control
            recorded.append({"args": args, "controls": controls})
            self.controls = controls

        def exec(self) -> int:
            return FakeQtObject.DialogCode.Accepted if accepted else FakeQtObject.DialogCode.Rejected

        def GetResult(self) -> dict[str, FakeQtObject]:
            return self.controls

    monkeypatch.setattr(gui_module, "UserInputDialog", RecordingUserInputDialog)
    return recorded


def build_gui(gui_module: ModuleType, tmp_path: Path):
    args = {
        "app_path": str(tmp_path),
        "config_path": str(tmp_path),
        "flashcarts": {"DMG": [{}, {}], "AGB": [{}, {}]},
        "config_ret": [],
        "argparsed": SimpleNamespace(device_port=None),
    }
    return gui_module.FlashGBX_GUI(cast("Any", args))


def build_rom_gui(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> tuple[Any, FakeDevice]:
    """Prepare the existing GUI and device fakes for ROM operation tests."""
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice(mode)
    gui.CONN = device
    gui.CheckDeviceAlive = always_device_alive
    gui.SETTINGS.values.update(LastDirRomDMG=str(tmp_path), LastDirRomAGB=str(tmp_path))
    profile = {"type": mode, "names": ["Mock Profile"], "voltage": 5, "mbc": 0x13}
    device.INFO[f"{mode.lower()}_carts"] = (["Generic", "Mock Profile"], [{}, profile])
    gui.cmbDMGCartridgeTypeResult.setCurrentIndex(1)
    gui.cmbAGBCartridgeTypeResult.setCurrentIndex(1)
    gui.cmbDMGHeaderMapperResult.setCurrentIndex(gui_module.ConvertMapperToMapperType(0x13)[2])
    gui.cmbDMGHeaderROMSizeResult.setCurrentIndex(5)
    gui.cmbAGBHeaderROMSizeResult.setCurrentIndex(6)
    monkeypatch.setattr(gui_module, "generate_filename", lambda **_kwargs: "generated.gb")
    return gui, device


def build_save_gui(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> tuple[Any, FakeDevice]:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, mode)
    device.INFO["has_rtc"] = False
    gui.SETTINGS.values.update(LastDirSaveDataDMG=str(tmp_path), LastDirSaveDataAGB=str(tmp_path))
    gui.cmbDMGHeaderSaveTypeResult.setCurrentIndex(4)  # 32 KiB SRAM
    gui.cmbAGBSaveTypeResult.setCurrentIndex(3)  # 32 KiB SRAM/FRAM
    return gui, device


def select_detection_save(gui: object, mode: str, save_kind: str) -> None:
    gui = cast("Any", gui)
    if mode == "AGB":
        assert save_kind == "batteryless"
        gui.cmbAGBSaveTypeResult.setCurrentIndex(9)
    else:
        gui.cmbDMGHeaderSaveTypeResult.setCurrentIndex(14 if save_kind == "batteryless" else 13)


def prepare_save_cartridge(
    gui: object,
    operation: str,
    mode: str,
    path: str,
    *,
    erase: bool = False,
    test: bool = False,
) -> bool:
    gui = cast("Any", gui)
    if operation == "backup":
        return cast("bool", gui._prepare_save_backup_cartridge(mode, path))
    return cast(
        "bool",
        gui._prepare_save_write_cartridge(mode, path, erase=erase, test=test),
    )


def configure_camera_calibration(device: FakeDevice) -> tuple[bytes, bytes]:
    calibration1 = bytes(range(0x10, 0x1E))
    calibration2 = bytes(range(0x80, 0x8E))
    device.INFO.update(
        db=None,
        dump_info={
            "header": {
                "mapper_raw": 0xFC,
                "logo_correct": True,
                "header_checksum_correct": True,
                "empty": False,
            },
        },
        gbcamera_calibration1=calibration1,
        gbcamera_calibration2=calibration2,
    )
    return calibration1, calibration2


def configure_ereader_calibration(device: FakeDevice) -> bytes:
    calibration = bytes([0xA5] * 0x2000)
    device.INFO.update(db=None, ereader=True, ereader_calibration=calibration)
    return calibration


def patterned_save(size: int = 0x20000) -> bytearray:
    return bytearray((index * 37 + 11) % 256 for index in range(size))


def always_device_alive(setMode: object = False) -> bool:
    """Accept the production keyword while keeping mocked checks deterministic."""
    return setMode in (False, "DMG", "AGB")


def ignore_cartridge_refresh(resetStatus: bool = False) -> None:
    """Stand in for refreshes triggered as an operation finishes."""
    assert isinstance(resetStatus, bool)


def dmg_header() -> dict[str, Any]:
    return {
        "game_title": "POKEMON RED",
        "game_code": "",
        "version": 0,
        "cgb": 0,
        "sgb": 3,
        "old_lic": 1,
        "db": None,
        "has_rtc": True,
        "rtc_dict": {"rtc_valid": True},
        "rtc_string": "Present",
        "logo_correct": True,
        "header_checksum_correct": True,
        "raw": bytearray(0x180),
        "rom_checksum": 0x1234,
        "rom_size_raw": 5,
        "rom_size": 0x100000,
        "ram_size_raw": 3,
        "mapper_raw": 0x13,
        "empty": False,
        "empty_nocart": False,
    }


def agb_header() -> dict[str, Any]:
    return {
        "game_title": "TEST GAME",
        "game_code": "ABCD",
        "version": 1,
        "db": None,
        "has_rtc": False,
        "rtc_dict": {},
        "rtc_string": "Not detected",
        "logo_correct": True,
        "header_checksum_correct": True,
        "header_checksum": 0x42,
        "raw": bytearray(0x200),
        "rom_size_calc": 0x200000,
        "rom_size": 0x200000,
        "save_type": 1,
        "empty": False,
        "empty_nocart": False,
        "3d_memory": False,
        "vast_fame": False,
        "dacs_8m": False,
    }


def test_gui_module_helpers(gui_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    details = gui_module._format_batteryless_sram_details(0x2000, {"bl_offset": 0x100, "bl_size": 0x80})
    assert "0x100-0x17F" in details
    assert gui_module._parse_hex_address("0x20") == 0x20
    assert gui_module._parse_hex_address("20") == 0x20
    assert gui_module._is_supported_drop(".sav", None) is True
    assert gui_module._is_supported_drop(".gb", "DMG") is True
    assert gui_module._is_supported_drop(".gba", "AGB") is True
    assert gui_module._is_supported_drop(".txt", "AGB") is False

    box = gui_module._create_message_box(windowTitle="Title", text="Body", defaultButton=1)
    assert isinstance(box, FakeMessageBox)
    checkbox = gui_module._create_check_box("Choice", checked=True)
    assert checkbox.isChecked() is True

    label = FakeQtObject()
    monkeypatch.setattr(gui_module, "bitmap2pixmap", lambda bitmap: f"pixmap:{bitmap}")
    assert gui_module._set_bitmap(label, "image") is True
    monkeypatch.setattr(gui_module, "bitmap2pixmap", lambda _bitmap: False)
    assert gui_module._set_bitmap(label, "image") is False

    assert "driver error" in gui_module._format_device_initialization_error(RuntimeError("driver error"))
    assert "\n\n" not in gui_module._format_device_initialization_error(RuntimeError())
    gui_module._ignore_progress(object())
    monkeypatch.setattr(gui_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert gui_module._system_executable("open") == "/usr/bin/open"
    monkeypatch.setattr(gui_module.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="missing"):
        gui_module._system_executable("missing")


@pytest.mark.parametrize(
    ("rom_size", "expected"),
    [
        (0x13FFFF, 0),
        (0x140000, 0),
        (0x140001, 1),
        (0x300000, 1),
    ],
)
def test_batteryless_default_location_obeys_rom_boundary(
    gui_module: ModuleType,
    rom_size: int,
    expected: int,
) -> None:
    assert (
        gui_module.FlashGBX_GUI._get_default_bl_location_index(
            rom_size,
            [0x100000, 0x200000],
        )
        == expected
    )


@pytest.mark.parametrize(
    ("mode", "rom_size", "expected"),
    [
        ("DMG", 0x110000, {"bl_offset": 0xD0000, "bl_size": 0x8000, "bl_layout": 2}),
        ("AGB", 0x400000, {"bl_offset": 0x3C0000, "bl_size": 0x10000}),
    ],
)
def test_batteryless_defaults_follow_platform(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    rom_size: int,
    expected: dict[str, int],
) -> None:
    dialogs = install_batteryless_dialog(monkeypatch, gui_module)
    settings = FakeSettings()
    gui = make_gui(gui_module, CONN=FakeDevice(mode), SETTINGS=settings)

    assert gui.GetBLArgs(rom_size) == expected

    params = cast("dict[str, object]", dialogs[0]["args"])["params"]
    defaults = [param[4] for param in cast("list[list[object]]", params)]
    assert defaults == ([0, 1, 2] if mode == "DMG" else [0, 2])


def test_batteryless_detected_values_override_remembered_location(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs = install_batteryless_dialog(monkeypatch, gui_module)
    settings = FakeSettings({"BatterylessSramLastLocationAGB": str(0x3C0000)})
    gui = make_gui(gui_module, CONN=FakeDevice("AGB"), SETTINGS=settings)

    result = gui.GetBLArgs(0x400000, detected={"bl_offset": 0x500000, "bl_size": 0x20000})

    assert result == {"bl_offset": 0x500000, "bl_size": 0x20000}
    params = cast("list[list[object]]", cast("dict[str, object]", dialogs[0]["args"])["params"])
    assert [param[4] for param in params] == [1, 3]


def test_batteryless_invalid_detected_size_uses_platform_fallback(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs = install_batteryless_dialog(monkeypatch, gui_module, accepted=False)
    settings = FakeSettings({"BatterylessSramLastLocationAGB": "unavailable"})
    gui = make_gui(gui_module, CONN=FakeDevice("AGB"), SETTINGS=settings)

    assert (
        gui.GetBLArgs(
            0x400000,
            detected={"bl_offset": 0x500000, "bl_size": 0x1234},
        )
        is False
    )

    params = cast("list[list[object]]", cast("dict[str, object]", dialogs[0]["args"])["params"])
    assert [param[4] for param in params] == [0, 2]
    assert settings.writes == []


def test_batteryless_dmg_header_preselects_all_parameters(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs = install_batteryless_dialog(monkeypatch, gui_module)
    settings = FakeSettings({"BatterylessSramLastLocationDMG": str(0xD0000)})
    device = FakeDevice("DMG")
    device.INFO["dump_info"] = {
        "header": {
            "game_title_raw": "PATCHED GAME\x00",
            "batteryless_sram": {"bl_offset": 0x110000, "bl_size": 0x20000, "bl_layout": 1},
        },
    }
    gui = make_gui(gui_module, CONN=device, SETTINGS=settings)

    assert gui.GetBLArgs(0x200000) == {
        "bl_offset": 0x110000,
        "bl_size": 0x20000,
        "bl_layout": 1,
    }
    params = cast("list[list[object]]", cast("dict[str, object]", dialogs[0]["args"])["params"])
    assert [param[4] for param in params] == [2, 3, 1]


def test_batteryless_saved_locations_keep_sorted_unique_integers(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs = install_batteryless_dialog(monkeypatch, gui_module, accepted=False)
    settings = FakeSettings(
        {
            "BatterylessSramLocationsAGB": json.dumps(
                [0x500000, "0x600000", 0x3C0000, 0x500000, None, 1.5],
            ),
            "BatterylessSramLastLocationAGB": str(0x500000),
        },
    )
    gui = make_gui(gui_module, CONN=FakeDevice("AGB"), SETTINGS=settings)

    assert gui.GetBLArgs(0x400000) is False

    params = cast("list[list[object]]", cast("dict[str, object]", dialogs[0]["args"])["params"])
    assert params[0][3] == ["0x3C0000", "0x500000", "0x7C0000", "0xFC0000", "0x1FC0000"]
    assert params[0][4] == 1
    assert settings.writes == []


def test_batteryless_malformed_locations_and_unavailable_last_use_default(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs = install_batteryless_dialog(monkeypatch, gui_module, accepted=False)
    settings = FakeSettings(
        {
            "BatterylessSramLocationsAGB": "{malformed",
            "BatterylessSramLastLocationAGB": "1234",
        },
    )
    gui = make_gui(gui_module, CONN=FakeDevice("AGB"), SETTINGS=settings)

    assert gui.GetBLArgs(0x800001) is False

    params = cast("list[list[object]]", cast("dict[str, object]", dialogs[0]["args"])["params"])
    assert params[0][3] == ["0x3C0000", "0x7C0000", "0xFC0000", "0x1FC0000"]
    assert params[0][4] == 2
    assert settings.writes == []


@pytest.mark.parametrize(
    ("mode", "selections", "expected", "expected_locations"),
    [
        (
            "DMG",
            {"loc": 3, "len": 2, "layout": 1},
            {"bl_offset": 0x1D0000, "bl_size": 0x10000, "bl_layout": 1},
            [0xD0000, 0x100000, 0x110000, 0x1D0000, 0x1E0000, 0x210000, 0x3D0000, 0x1D0000],
        ),
        (
            "AGB",
            {"loc": "0x500123", "len": 3},
            {"bl_offset": 0x500123, "bl_size": 0x20000},
            [0x3C0000, 0x7C0000, 0xFC0000, 0x1FC0000, 0x500123],
        ),
    ],
)
def test_batteryless_accepts_preset_and_custom_hex_and_persists_exactly(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    selections: dict[str, int | str],
    expected: dict[str, int],
    expected_locations: list[int],
) -> None:
    install_batteryless_dialog(monkeypatch, gui_module, selections=selections)
    settings = FakeSettings()
    gui = make_gui(gui_module, CONN=FakeDevice(mode), SETTINGS=settings)

    assert gui.GetBLArgs(0x400000) == expected
    assert settings.writes == [
        (f"BatterylessSramLocations{mode}", json.dumps(expected_locations)),
        (f"BatterylessSramLastLocation{mode}", json.dumps(expected["bl_offset"])),
    ]


def test_batteryless_cancel_and_invalid_custom_text_do_not_write_settings(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = FakeSettings()
    gui = make_gui(gui_module, CONN=FakeDevice("AGB"), SETTINGS=settings)
    install_batteryless_dialog(monkeypatch, gui_module, accepted=False)
    assert gui.GetBLArgs(0x400000) is False
    assert settings.writes == []

    install_batteryless_dialog(
        monkeypatch,
        gui_module,
        selections={"loc": "not a hexadecimal address"},
    )
    assert gui.GetBLArgs(0x400000) is False
    assert settings.writes == []


def test_batteryless_parameters_require_a_platform_mode(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs = install_batteryless_dialog(monkeypatch, gui_module)
    settings = FakeSettings()
    device = FakeDevice()
    device.mode = None  # type: ignore[assignment]
    gui = make_gui(gui_module, CONN=device, SETTINGS=settings)

    with pytest.raises(RuntimeError, match="require a platform mode"):
        gui.GetBLArgs(0x400000)
    assert dialogs == []
    assert settings.writes == []


def test_gui_constructor_builds_complete_inert_widget_tree(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gui_module, "IniSettings", FakeSettings)
    monkeypatch.setattr(gui_module.platform, "system", lambda: "Linux")
    args = {
        "app_path": str(tmp_path),
        "config_path": str(tmp_path),
        "flashcarts": {"DMG": [], "AGB": []},
        "config_ret": [[0, "loaded"], [1, "notice"], [2, "warning"], [3, "error"]],
        "argparsed": SimpleNamespace(device_port=None),
    }

    gui = gui_module.FlashGBX_GUI(cast("Any", args))

    assert gui.CONN is None
    assert len(gui.mnuConfig.actions()) == 16
    assert len(gui.mnuRestoreRAM.actions()) == 4
    assert gui.btnConnect.isEnabled() is False
    assert gui.MSGBOX_TIMER.timeout.callbacks == [gui.MsgBoxCheck]
    assert gui.LOG_ERROR_TIMER.timeout.callbacks == [gui.LogErrorCheck]


def test_device_property_and_platform_autodetection(gui_module: ModuleType) -> None:
    gui = make_gui(gui_module, CONN=None)
    with pytest.raises(ConnectionError):
        _ = gui._device

    conn = SimpleNamespace(
        FW={"cart_mode_switch": True},
        GetSupprtedModes=lambda: ["DMG", "AGB"],
        GetCartModeSwitchState=lambda: 1,
        GetMode=lambda: "DMG",
    )
    gui.CONN = conn
    assert gui._device is conn
    assert gui._GetAutoPlatformMode() == "AGB"
    conn.FW = {}
    assert gui._GetAutoPlatformMode() == "DMG"
    assert gui._GetAutoPlatformMode(supported_modes=["AGB"]) == "AGB"
    gui.CONN = None
    assert gui._GetAutoPlatformMode() is None


@pytest.mark.parametrize(
    ("data", "badge"),
    [(None, ""), ({"cgb": 0xC0}, "CGB"), ({"cgb": 0x80}, "CGB"), ({"old_lic": 0x33, "sgb": 3}, "SGB"), ({}, "DMG")],
)
def test_platform_badges_and_game_name_layout(
    gui_module: ModuleType,
    data: dict[str, int] | None,
    badge: str,
) -> None:
    gui = make_gui(
        gui_module,
        lblDMGPlatformBadge=FakeQtObject(),
        lblDMGGameName=FakeQtObject("Game Name"),
        lblDMGGameNameResult=FakeQtObject(),
        _rowDMGGameName=FakeQtObject(),
        _resultDMGGameName=FakeQtObject(),
        _dmgGameNameFullText="A very long generated game name",
        _dmgGameNameDefaultColWidth=100,
    )

    gui.SetDMGPlatformBadge(data)
    gui.SetDMGGameNameText("A very long generated game name")

    assert gui.lblDMGPlatformBadge.text() == badge
    assert gui.lblDMGGameNameResult.text()


def test_message_queue_and_progress_bar_helpers(gui_module: ModuleType) -> None:
    box = FakeMessageBox()
    message_queue: queue.Queue[FakeMessageBox] = queue.Queue()
    message_queue.put(box)
    gui = make_gui(
        gui_module,
        MSGBOX_DISPLAYING=False,
        MSGBOX_QUEUE=message_queue,
        prgStatus=FakeQtObject(),
        TBPROG=FakeQtObject(),
        lblStatus4a=FakeQtObject(),
        lblStatus4aResult=FakeQtObject(),
    )

    gui.MsgBoxCheck()
    gui.SetProgressBars(min=1, max=10, value=4)
    gui.SetStatus4aResult("working")

    assert message_queue.empty()
    assert gui.MSGBOX_DISPLAYING is False
    assert gui.prgStatus.value() == 4
    assert gui.lblStatus4aResult.text() == "working"


def test_device_settings_and_platform_firmware_switch(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    gui.CONN = device
    gui.CheckDeviceAlive = always_device_alive

    gui.SETTINGS.values.update(AutoPowerOff="invalid", DMGReadMethod="invalid", AGBReadMethod="invalid")
    gui.SetAutoPowerOff()
    gui.SetDMGReadMethod()
    gui.SetAGBReadMethod()
    gui.SETTINGS.values.update(AutoPowerOff="350", DMGReadMethod="2", AGBReadMethod="0")
    gui.SetAutoPowerOff()
    gui.SetDMGReadMethod()
    gui.SetAGBReadMethod()

    assert ("auto_power_off", 0) in device.calls
    assert ("auto_power_off", 350) in device.calls
    assert ("dmg_read", 2) in device.calls
    assert ("agb_read", 0) in device.calls

    mode_calls: list[str] = []
    gui.SetMode = lambda: mode_calls.append("set")
    device.INFO.update(voltage_autoswitch=True, voltage_code=False, switch_state=1)
    device.FW["cart_mode_switch"] = True
    gui._UpdatePlatformModeFromFirmware()
    assert gui.optAGB.isChecked() is True
    assert mode_calls == ["set"]


def test_gbxcartrw_baudrate_update_preferences_and_language(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    device.DEVICE_NAME = "Custom reader name"
    gui.CONN = device
    gui.CheckDeviceAlive = always_device_alive
    disconnects: list[bool] = []
    scans: list[dict[str, object]] = []
    gui.DisconnectDevice = lambda: disconnects.append(True)
    gui.FindDevices = lambda **kwargs: scans.append(kwargs)

    gui.SetGBxCartRWBaudRate(1_000_000)
    gui.SetGBxCartRWBaudRate(1_500_000)
    gui.SetGBxCartRWBaudRate(1_700_000)
    assert ("baud", 1_000_000) in device.calls
    assert ("baud", 1_500_000) in device.calls
    assert ("baud", 1_700_000) in device.calls
    assert gui.SETTINGS.values["GBxCartRWBaudRate"] == "1700000"
    assert gui.mnuConfigBaudRateActions[1_700_000].isChecked() is True
    assert gui.mnuConfigBaudRateActions[1_000_000].isChecked() is False
    assert scans[-1] == {"connectToFirst": True, "mode": "DMG"}

    updates: list[bool] = []
    gui.UpdateCheck = lambda: updates.append(True)
    gui.SETTINGS.values["UpdateCheck"] = None
    gui.EnableUpdateCheck()
    gui.SETTINGS.values["UpdateCheck"] = "disabled"
    gui.mnuConfig.actions()[0].setChecked(True)
    FakeMessageBox.next_answer = FakeMessageBox.StandardButton.Yes
    gui.EnableUpdateCheck()
    assert gui.SETTINGS.values["UpdateCheck"] == "enabled"
    assert len(updates) == 2

    languages: list[str] = []
    monkeypatch.setattr(gui_module, "init_language", lambda _path, *, override: languages.append(override))
    monkeypatch.setattr(gui_module, "loadQtTranslation", lambda _app, *, language: languages.append(language))
    gui.InitWidgetTexts = lambda: None
    gui.ChangeLanguage("de")
    assert languages == ["de", "de"]
    assert disconnects


def test_gbxcartrw_baudrate_setting_migrates_and_validates(gui_module: ModuleType) -> None:
    settings = FakeSettings({"LimitBaudRate": "enabled"})
    gui = make_gui(gui_module, SETTINGS=settings)

    assert gui._GetGBxCartRWBaudRate() == 1_000_000
    assert settings.values["GBxCartRWBaudRate"] == "1000000"
    assert "LimitBaudRate" not in settings.values

    settings.values["GBxCartRWBaudRate"] = "2000000"
    assert gui._GetGBxCartRWBaudRate() == 1_500_000
    assert settings.values["GBxCartRWBaudRate"] == "1500000"

    settings.values["GBxCartRWBaudRate"] = "invalid"
    assert gui._GetGBxCartRWBaudRate() == 1_500_000
    assert settings.values["GBxCartRWBaudRate"] == "1500000"
    with pytest.raises(ValueError, match="Unsupported"):
        gui.SetGBxCartRWBaudRate(2_000_000)
    assert gui._GetDeviceMaxBaudRate(SimpleNamespace(DEVICE_NAME="Other Reader")) == 2_000_000


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(status_code=200, content=b'{"tag_name":"5.0.1"}', headers={}),
        SimpleNamespace(status_code=200, content=b'{"tag_name":"99.0"}', headers={}),
        SimpleNamespace(status_code=200, content=b"not-json", headers={}),
        SimpleNamespace(status_code=403, content=b"", headers={"X-RateLimit-Remaining": "0"}),
        SimpleNamespace(status_code=500, content=b"", headers={}),
    ],
)
def test_update_check_handles_mock_http_responses(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: object,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    gui.SETTINGS.values["UpdateCheck"] = "enabled"
    opened: list[str] = []
    gui.OpenWebURL = opened.append
    monkeypatch.setattr(gui_module.requests, "get", lambda *_args, **_kwargs: response)

    gui.UpdateCheck()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(Exception("unexpected"), id="unexpected"),
        pytest.param(None, id="disabled"),
    ],
)
def test_update_check_handles_errors_and_disabled_setting(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception | None,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    if error is None:
        gui.SETTINGS.values["UpdateCheck"] = "disabled"
        monkeypatch.setattr(gui_module.requests, "get", Mock(side_effect=AssertionError("network used")))
    else:
        gui.SETTINGS.values["UpdateCheck"] = "enabled"
        monkeypatch.setattr(gui_module.requests, "get", Mock(side_effect=error))
    gui.UpdateCheck()


def test_launcher_environment_and_open_url_are_platform_aware(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    monkeypatch.setattr(
        gui_module.os,
        "environ",
        {
            "APPDIR": "app",
            "APPIMAGE": "image",
            "ARGV0": "arg",
            "LD_LIBRARY_PATH": "bundled",
            "LD_LIBRARY_PATH_ORIG": "system",
        },
    )
    monkeypatch.setattr(gui_module.platform, "system", lambda: "Linux")
    environment = gui.GetHostLauncherEnv()
    assert environment["LD_LIBRARY_PATH"] == "system"
    assert "APPDIR" not in environment

    launches: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(gui_module, "_system_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(gui_module.subprocess, "Popen", lambda argv, *, env: launches.append((argv, env)))
    gui.OpenWebURL("https://example.invalid")
    assert launches[0][0] == ["/usr/bin/xdg-open", "https://example.invalid"]

    monkeypatch.setattr(gui_module.platform, "system", lambda: "Darwin")
    browser_urls: list[str] = []
    monkeypatch.setattr(gui_module.webbrowser, "open", browser_urls.append)
    gui.OpenWebURL("https://example.invalid/mac")
    assert browser_urls == ["https://example.invalid/mac"]


def test_disconnect_and_about_helpers_reset_gui_state(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    device.INFO["support_message"] = "Support at https://example.invalid/help"
    gui.CONN = device
    gui.DEVICES = {"Mock": device}

    assert gui._GetDeviceSupportData() == ("GBxCart RW", "Support at https://example.invalid/help")
    gui.UpdateThirdPartySupportAction()
    assert gui.mnuDeviceSupport.isVisible() is True
    assert "<a href=" in gui._ConvertUrlsToAnchors("See https://example.invalid/?a=1&b=2")
    gui.AboutConnectedDevice()
    gui.AboutFlashGBX()
    gui.AboutGameDB()
    gui.ReEnableMessages()
    assert gui.SETTINGS.values["SkipFinishMessage"] == "disabled"

    gui.DisconnectDevice()
    assert gui.CONN is None
    assert gui.DEVICES == {}
    assert ("close", True) in device.calls


def test_connect_device_success_and_backend_failures(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    gui.DEVICES = {"Mock Reader": device}
    gui.lblDevice.setText("Mock Reader")

    assert gui.ConnectDevice() is True
    assert gui.CONN is device
    assert ("initialize", ("mock-port", 1_500_000)) in device.calls
    assert gui.btnConnect.text().replace("&", "") == "Disconnect"

    gui.DisconnectDevice()
    device.initialize_result = False
    gui.DEVICES = {"Mock Reader": device}
    gui.lblDevice.setText("Mock Reader")
    assert gui.ConnectDevice() is False

    device.initialize_result = RuntimeError("driver unavailable")
    gui.DEVICES = {"Mock Reader": device}
    gui.lblDevice.setText("Mock Reader")
    assert gui.ConnectDevice() is False


def test_find_devices_discovers_mock_and_handles_scan_exception(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    monkeypatch.setattr(gui_module, "HW_DEVICES", [SimpleNamespace(GbxDevice=lambda: device)])
    connections: list[bool] = []
    gui.ConnectDevice = lambda: connections.append(True)

    assert gui.FindDevices() is True
    assert "Mock Reader" in gui.DEVICES
    assert connections == [True]

    broken = FakeDevice()
    broken.initialize_result = RuntimeError("scan failure")
    monkeypatch.setattr(gui_module, "HW_DEVICES", [SimpleNamespace(GbxDevice=lambda: broken)])
    assert gui.FindDevices(firstRun=True) is False


def test_abort_and_header_validation_use_mock_device(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    gui.CONN = device
    gui.STATUS["stresstest_running"] = True
    gui.AbortOperation()
    assert "stresstest_running" not in gui.STATUS
    assert ("abort", True) in device.calls

    device.INFO = {
        "dump_info": {
            "header": {"mapper_raw": 1, "logo_correct": False, "header_checksum_correct": False, "empty": False}
        }
    }
    FakeMessageBox.next_answer = FakeMessageBox.StandardButton.No
    assert gui.CheckHeader() is False
    FakeMessageBox.next_answer = FakeMessageBox.StandardButton.Yes
    assert gui.CheckHeader() is True
    device.INFO = {}
    assert gui.CheckHeader() is True


def test_read_cartridge_populates_dmg_widgets_without_hardware(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice("DMG")
    device.header = dmg_header()
    gui.CONN = device
    gui.CheckDeviceAlive = always_device_alive
    gui._UpdatePlatformModeFromFirmware = lambda: None
    gui.FinishOperation = lambda: None

    assert gui.ReadCartridge() is None
    assert gui.lblDMGRomTitleResult.text() == "POKEMON RED"
    assert gui.lblDMGHeaderBootlogoResult.text() == "OK"
    assert gui.cmbDMGHeaderROMSizeResult.currentIndex() == 5
    assert (tmp_path / "bootlogo_dmg.bin").exists()

    device.header = {**dmg_header(), "empty": True, "empty_nocart": True, "logo_correct": False}
    gui.ReadCartridge(resetStatus=False)
    assert "No cartridge" in gui.lblDMGGameNameResult.text()


def test_read_cartridge_populates_agb_widgets_without_hardware(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice("AGB")
    device.header = agb_header()
    gui.CONN = device
    gui.CheckDeviceAlive = always_device_alive
    gui._UpdatePlatformModeFromFirmware = lambda: None
    gui.FinishOperation = lambda: None

    gui.ReadCartridge()

    assert gui.lblAGBRomTitleResult.text() == "TEST GAME"
    assert "Valid" in gui.lblAGBHeaderChecksumResult.text()
    assert gui.grpAGBCartridgeInfo.isVisible() is True
    assert (tmp_path / "bootlogo_agb.bin").exists()

    database_header = agb_header()
    database_header["db"] = {"gc": "AGB-ABCD", "gn": "Database Game", "rs": 0x4000000, "rc": 0x123456, "st": 2}
    database_header["rom_size_calc"] = 0x200000
    database_header["3d_memory"] = True
    device.header = database_header
    device.INFO["supported_3d"] = False
    gui.ReadCartridge(resetStatus=False)
    assert gui.lblAGBGameNameResult.text() == "Database Game"


def test_read_cartridge_handles_empty_and_serial_failures(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    gui.CONN = device
    gui.CheckDeviceAlive = always_device_alive
    gui._UpdatePlatformModeFromFirmware = lambda: None
    limited: list[bool] = []
    disconnected: list[bool] = []
    gui.LimitBaudRateGBxCartRW = lambda: limited.append(True)
    gui.DisconnectDevice = lambda: disconnected.append(True)

    device.header = {}
    assert gui.ReadCartridge() is False
    device.header = cast("Any", BrokenPipeError())
    assert gui.ReadCartridge() is False
    assert len(limited) == 2
    assert len(disconnected) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "ERASE", "pos": 1, "size": 10, "time_elapsed": 2, "time_estimated": 5, "abortable": True},
        {"action": "UNLOCK", "pos": 1, "size": 10, "abortable": True},
        {"action": "UPDATE_RTC", "pos": 1, "size": 10},
        {"action": "CALC_CHECKSUMS", "pos": 1, "size": 10, "type": "SHA-1"},
        {"action": "SECTOR_ERASE", "pos": 1, "size": 10, "time_elapsed": 2, "sector_pos": 0x1000, "abortable": True},
        {"action": "ABORTING", "pos": 1, "size": 10, "abortable": False},
        {"action": "ERROR", "pos": 1, "size": 10, "text": "failed", "abortable": False},
        {"action": "UPDATE_INFO", "pos": 1, "size": 10, "text": "working", "abortable": False},
        {"action": "PROGRESS", "pos": 5, "size": 10, "speed": 2.5, "time_elapsed": 3, "time_left": 4},
    ],
)
def test_update_progress_handles_transfer_actions(
    gui_module: ModuleType,
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    gui = build_gui(gui_module, tmp_path)
    gui.CONN = FakeDevice()
    gui.UpdateProgress(payload)


@pytest.mark.parametrize(
    "method",
    ["ROM_READ", "ROM_WRITE", "ROM_WRITE_VERIFY", "SAVE_READ", "SAVE_WRITE", "SAVE_WRITE_VERIFY", "DETECT_CART"],
)
def test_update_progress_sets_transfer_method_titles(
    gui_module: ModuleType,
    tmp_path: Path,
    method: str,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    gui.CONN = FakeDevice()
    gui.UpdateProgress({"method": method, "voltage": 3.3})


def test_update_progress_handles_error_finish_and_abort(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    gui.CONN = device
    limited: list[bool] = []
    finishes: list[bool] = []
    gui.LimitBaudRateGBxCartRW = lambda: limited.append(True)
    gui.FinishOperation = lambda: finishes.append(True)
    gui.WriteDebugLog = lambda: None
    gui.DisconnectDevice = lambda: None

    gui.UpdateProgress({"error": "failure"})
    gui.UpdateProgress({"action": "FINISHED", "pos": 10})
    gui.UpdateProgress({"action": "ABORT", "info_type": "msgbox_critical", "info_msg": "fatal", "fatal": True})
    gui.UpdateProgress({"action": "ABORT", "info_type": "msgbox_information", "info_msg": "notice"})
    gui.UpdateProgress({"action": "ABORT", "info_type": "label", "info_msg": "stopped"})

    assert limited == [True, True]
    assert finishes == [True]
    assert gui.MSGBOX_QUEUE.qsize() == 2


@pytest.mark.parametrize(
    ("user_action", "answer", "expected"),
    [
        ("REINSERT_CART", FakeMessageBox.StandardButton.Ok, True),
        ("REINSERT_CART", FakeMessageBox.StandardButton.Cancel, False),
        ("RETRY_5V", FakeMessageBox.StandardButton.Yes, True),
        ("RETRY_5V", FakeMessageBox.StandardButton.No, False),
    ],
)
def test_wait_progress_records_dialog_answers(
    gui_module: ModuleType,
    tmp_path: Path,
    user_action: str,
    answer: int,
    expected: bool,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    gui.CONN = device
    FakeMessageBox.next_answer = answer
    gui.WaitProgress({"user_action": user_action, "title": "Mock", "msg": "Continue?"})
    assert expected is device.USER_ANSWER


@pytest.mark.parametrize(
    ("mode", "info", "verified"),
    [
        ("DMG", {"last_action": 1, "rom_checksum": 1, "rom_checksum_calc": 1, "loop_detected": False}, False),
        ("AGB", {"last_action": 1, "db": {"rc": 1}, "file_crc32": 1, "loop_detected": False}, False),
        ("AGB", {"last_action": 1, "db": None, "file_crc32": 1, "loop_detected": False}, False),
        ("DMG", {"last_action": 2, "mapper_raw": 1, "transferred": 1, "last_path": "save.sav"}, False),
        ("DMG", {"last_action": 3}, True),
        ("DMG", {"last_action": 4, "dump_info": {}}, True),
        ("DMG", {"last_action": 99}, False),
    ],
)
def test_finish_operation_handles_primary_results(
    gui_module: ModuleType,
    tmp_path: Path,
    mode: str,
    info: dict[str, object],
    verified: bool,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice(mode)
    device.INFO = {"transferred": 1, **info}
    gui.CONN = device
    gui.STATUS["last_path"] = str(tmp_path / "backup.gb")
    gui.PROGRESS.PROGRESS["verified"] = verified
    gui.ReadCartridge = ignore_cartridge_refresh

    gui.FinishOperation()

    assert device.INFO["last_action"] == 0


def test_finish_detect_cartridge_handles_success_and_failure(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice("DMG")
    device.INFO["dmg_carts"] = (
        ["Generic", "Mock Profile", "Compatible"],
        [{}, {"mbc": "manual", "flash_size": 0x200000}, {}],
    )
    gui.CONN = device
    gui.STATUS["can_skip_message"] = False
    result = (
        dmg_header(),
        0x2000,
        3,
        None,
        False,
        [1, 2],
        1,
        "CFI data",
        None,
        "[   AAA/AA]\nflash id\n",
        0,
    )

    gui.FinishDetectCartridge(result)
    assert gui.STATUS["cart_type"] == {"mbc": "manual", "flash_size": 0x200000}
    assert gui.cmbDMGCartridgeTypeResult.currentIndex() == 1

    disconnected: list[bool] = []
    gui.DisconnectDevice = lambda: disconnected.append(True)
    gui.LimitBaudRateGBxCartRW = lambda: None
    gui.FinishDetectCartridge(False)
    assert disconnected == [True]


def test_detect_cartridge_and_baud_fallback_are_mocked(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice()
    gui.CONN = device
    gui.CheckDeviceAlive = always_device_alive
    gui.SETTINGS.values.update(
        AutoDetectLimitVoltage="enabled",
        AutoLimitBaudRate="enabled",
        GBxCartRWBaudRate="1500000",
    )

    gui.DetectCartridge(checkSaveType=False)
    gui.LimitBaudRateGBxCartRW()

    assert ("detect", {"limitVoltage": True, "checkSaveType": False}) in device.calls
    assert ("baud", 1_000_000) in device.calls
    assert gui.SETTINGS.values["GBxCartRWBaudRate"] == "1000000"


def test_check_device_alive_covers_connection_states(
    gui_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui = make_gui(gui_module, CONN=None)
    assert gui.CheckDeviceAlive() is False

    device = FakeDevice()
    disconnects: list[bool] = []
    gui.CONN = device
    gui.DisconnectDevice = lambda: disconnects.append(True)
    device.DEVICE = None
    assert gui.CheckDeviceAlive() is False
    assert disconnects == [True]

    device.DEVICE = object()
    device.connected = True
    assert gui.CheckDeviceAlive() is True

    device.connected = False
    no_box = FakeMessageBox()
    no_box.setResult(FakeMessageBox.StandardButton.No)
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: no_box)
    gui.CONN = device
    assert gui.CheckDeviceAlive() is False
    assert len(disconnects) == 3

    scheduled: list[tuple[int, object]] = []
    yes_box = FakeMessageBox()
    yes_box.setResult(FakeMessageBox.StandardButton.Yes)
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: yes_box)
    monkeypatch.setattr(
        gui_module.QtCore.QTimer,
        "singleShot",
        lambda milliseconds, callback: scheduled.append((milliseconds, callback)),
    )
    gui.CONN = device
    gui.DEVICES = {"mock": device}
    assert gui.CheckDeviceAlive() is False
    assert scheduled[0][0] == 500


def test_set_mode_switches_or_restores_platform_choice(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice("DMG")
    gui.CONN = device
    gui.optDMG.setChecked(False)
    gui.optAGB.setChecked(True)
    gui.SETTINGS.values["SkipModeChangeWarning"] = "enabled"
    gui.CheckDeviceAlive = always_device_alive
    gui.ReadCartridge = lambda: True

    assert gui.SetMode() is None
    assert device.mode == "AGB"
    assert gui.btnBackupROM.isEnabled() is True
    assert gui.grpAGBCartridgeInfo.isEnabled() is True

    device.mode = "DMG"
    gui.optDMG.setChecked(False)
    gui.optAGB.setChecked(True)
    gui.SETTINGS.values["SkipModeChangeWarning"] = "disabled"
    cancel_box = FakeMessageBox()
    cancel_box.setResult(FakeMessageBox.StandardButton.Cancel)
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: cancel_box)

    assert gui.SetMode() is False
    assert gui.optDMG.isChecked() is True
    assert device.mode == "DMG"


def test_auxiliary_windows_run_with_mocked_dependencies(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window_runs: list[tuple[str, object]] = []

    class StubWindow(FakeQtObject):
        def __init__(self, *_args: object, **kwargs: object) -> None:
            super().__init__()
            self.kwargs = kwargs

        def run(self) -> None:
            window_runs.append((type(self).__name__, self.kwargs.get("file")))

    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice("DMG")
    gui.CONN = device

    assert gui.ShowFirmwareUpdateWindow() is False
    device.INFO["supports_updates"] = True
    monkeypatch.setattr(device, "GetFirmwareUpdaterClass", lambda: (None, StubWindow), raising=False)
    assert gui.ShowFirmwareUpdateWindow() is None
    assert gui.FWUPWIN.kwargs["device"] is device

    monkeypatch.setattr(gui_module, "PocketCameraWindow", StubWindow)
    device.header = {"mapper_raw": 252, "ram_size_raw": 3}
    device.INFO["data"] = b"camera-save"
    gui.ShowPocketCameraWindow()
    assert gui.CAMWIN.kwargs["file"] == b"camera-save"
    assert any(call[0] == "backup_ram" for call in device.calls)

    monkeypatch.setattr(gui_module, "InteractiveConsoleWindow", StubWindow)
    gui.CONN = None
    assert gui.ShowInteractiveConsoleWindow() is False
    gui.CONN = device
    device.mode = cast("Any", None)
    assert gui.ShowInteractiveConsoleWindow() is False
    device.mode = "AGB"
    assert gui.ShowInteractiveConsoleWindow() is None
    assert gui.INTWIN is not None


def test_drag_drop_routes_roms_and_saves_without_file_io(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    gui.CONN = FakeDevice("DMG")
    gui.btnHeaderRefresh.setEnabled(True)
    gui.grpActions.setEnabled(True)
    flashed: list[str] = []
    restored: list[str] = []
    gui.FlashROM = flashed.append
    gui.WriteRAM = restored.append

    supported = FakeDropEvent([str(tmp_path / "pokemon-red.gb")])
    gui.dragEnterEvent(cast("Any", supported))
    assert supported.accepted is True

    unsupported = FakeDropEvent([str(tmp_path / "notes.txt")])
    gui.dragMoveEvent(cast("Any", unsupported))
    assert unsupported.ignored is True

    dropped = FakeDropEvent([str(tmp_path / "pokemon-red.gb"), str(tmp_path / "pokemon-red.sav")])
    gui.dropEvent(cast("Any", dropped))
    assert dropped.accepted is True
    assert flashed == [str(tmp_path / "pokemon-red.gb")]
    assert restored == [str(tmp_path / "pokemon-red.sav")]


def test_close_event_clears_pending_messages(gui_module: ModuleType) -> None:
    pending: queue.Queue[object] = queue.Queue()
    pending.put(object())
    disconnects: list[bool] = []
    event = FakeDropEvent([])
    gui = make_gui(
        gui_module,
        CONN=None,
        DisconnectDevice=lambda: disconnects.append(True),
        MSGBOX_TIMER=FakeQtObject(),
        MSGBOX_DISPLAYING=False,
        MSGBOX_QUEUE=pending,
    )

    gui.closeEvent(cast("Any", event))

    assert disconnects == [True]
    assert pending.empty()
    assert gui.MSGBOX_DISPLAYING is True
    assert event.accepted is True


def test_open_path_uses_mocked_platform_launchers(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[tuple[list[str], dict[str, object]]] = []
    gui = make_gui(gui_module)
    gui.GetHostLauncherEnv = lambda: {"PATH": "/mock/bin"}
    monkeypatch.setattr(gui_module, "_system_executable", lambda name: f"/mock/{name}")
    monkeypatch.setattr(
        gui_module.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append((command, kwargs)),
    )

    cartridge = tmp_path / "pokemon-red.gb"
    cartridge.touch()
    monkeypatch.setattr(gui_module.platform, "system", lambda: "Darwin")
    gui.OpenPath(str(cartridge), select_file=True)
    assert launched[-1][0] == ["/mock/open", "-R", str(cartridge)]

    monkeypatch.setattr(gui_module.platform, "system", lambda: "Linux")
    gui.OpenPath(str(tmp_path))
    assert launched[-1][0] == ["/mock/xdg-open", tmp_path.as_uri()]


@pytest.mark.parametrize(("mode", "extension", "rom_size"), [("DMG", ".gb", 0x100000), ("AGB", ".gba", 0x200000)])
def test_backup_rom_uses_selected_platform_and_exact_transfer_request(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    extension: str,
    rom_size: int,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, mode)
    path = tmp_path / f"backup{extension}"
    monkeypatch.setattr(
        gui_module.QtWidgets,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *_args: (str(path), "")),
    )

    gui.BackupROM()

    transfers = [args for name, args in device.calls if name == "backup_rom"]
    assert transfers == [
        {
            "path": str(path),
            "mbc": 0x0F,
            "rom_size": rom_size,
            "agb_rom_size": rom_size,
            "fast_read_mode": True,
            "cart_type": 1,
            "settings": gui.SETTINGS,
        },
    ]
    assert gui.STATUS["last_path"] == str(path)
    assert gui.STATUS["args"] is transfers[0]
    assert gui.SETTINGS.values[f"LastDirRom{mode}"] == str(tmp_path)
    assert gui.grpActions.isEnabled() is False


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_backup_rom_cancelled_file_dialog_never_starts_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, mode)
    monkeypatch.setattr(gui_module.QtWidgets, "QFileDialog", SimpleNamespace(getSaveFileName=lambda *_args: ("", "")))

    gui.BackupROM()

    assert not any(name == "backup_rom" for name, _args in device.calls)
    assert "last_path" not in gui.STATUS
    assert gui.grpActions.isEnabled() is True


def test_backup_rom_rejected_header_check_never_opens_file_dialog(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, "DMG")
    device.INFO["dump_info"] = {
        "header": {"mapper_raw": 0x13, "logo_correct": False, "header_checksum_correct": False, "empty": False},
    }
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.No)
    monkeypatch.setattr(
        gui_module.QtWidgets,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=Mock(side_effect=AssertionError("File dialog opened"))),
    )

    gui.BackupROM()

    assert not any(name == "backup_rom" for name, _args in device.calls)
    assert gui.grpActions.isEnabled() is True


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_prepare_flash_cart_selection_uses_selected_profile(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, mode)

    selection = gui._PrepareFlashCartSelection("")

    assert selection is not None
    selected_mode, path, setting_name, last_dir, carts, index, profile = selection
    assert selected_mode == mode
    assert path == ""
    assert setting_name == f"LastDirRom{mode}"
    assert last_dir == str(tmp_path)
    assert carts is device.INFO[f"{mode.lower()}_carts"][1]
    assert index == 1
    assert profile is carts[1]
    assert not any(name == "flash" for name, _args in device.calls)


def test_prepare_flash_cart_selection_canceled_drop_clears_detected_profile(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, "DMG")
    gui.STATUS["detected_cart_type"] = 1
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Cancel)

    assert gui._PrepareFlashCartSelection(str(tmp_path / "drop.gb")) is None

    assert "detected_cart_type" not in gui.STATUS
    assert not any(name == "flash" for name, _args in device.calls)


@pytest.mark.parametrize("detected", [False, None, 0])
def test_prepare_flash_cart_selection_rejects_canceled_or_missing_detection(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detected: object,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, "DMG")
    gui.cmbDMGCartridgeTypeResult.setCurrentIndex(0)
    gui.STATUS["detected_cart_type"] = detected

    assert gui._PrepareFlashCartSelection("") is None

    assert "detected_cart_type" not in gui.STATUS
    assert not any(name == "flash" for name, _args in device.calls)


def test_prepare_flash_cart_selection_starts_profile_detection(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, "DMG")
    gui.cmbDMGCartridgeTypeResult.setCurrentIndex(0)
    detection_calls: list[bool] = []
    gui.DetectCartridge = lambda *, checkSaveType: detection_calls.append(checkSaveType)

    assert gui._PrepareFlashCartSelection("") is None

    assert detection_calls == [False]
    assert gui.STATUS["detected_cart_type"] == "WAITING_FLASH"
    assert gui.STATUS["detect_cartridge_args"] == {"dpath": ""}
    assert gui.STATUS["can_skip_message"] is True
    assert not any(name == "flash" for name, _args in device.calls)


def test_prepare_flash_cart_selection_rejects_invalid_profile(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, "DMG")
    device.INFO["dmg_carts"] = (["Generic", "Broken"], [{}, "not a profile"])
    critical = Mock(return_value=FakeMessageBox.StandardButton.Ok)
    monkeypatch.setattr(FakeMessageBox, "critical", critical)

    assert gui._PrepareFlashCartSelection("") is None

    critical.assert_called_once()
    assert not any(name == "flash" for name, _args in device.calls)


@pytest.mark.parametrize(("mode", "extension"), [("DMG", ".gb"), ("AGB", ".gba")])
def test_flash_rom_dispatches_exact_platform_request(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    extension: str,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, mode)
    path = tmp_path / f"flash{extension}"
    path.write_bytes(bytes(0x1000))
    monkeypatch.setattr(
        gui_module.QtWidgets, "QFileDialog", SimpleNamespace(getOpenFileName=lambda *_args: (str(path), ""))
    )
    rom_header = {"mapper_raw": 0x0F, "logo_correct": True, "header_checksum_correct": True}
    rom_parser = "RomFileDMG" if mode == "DMG" else "RomFileAGB"
    monkeypatch.setattr(gui_module, rom_parser, lambda _buffer: SimpleNamespace(GetHeader=lambda: rom_header))
    gui.SETTINGS.values.update(PreferChipErase="enabled", VerifyData="enabled", CompareSectors="enabled")

    gui.FlashROM()

    transfers = [args for name, args in device.calls if name == "flash"]
    assert transfers == [
        {
            "path": str(path),
            "cart_type": 1,
            "override_voltage": False,
            "prefer_chip_erase": True,
            "fast_read_mode": True,
            "verify_write": True,
            "fix_header": False,
            "fix_bootlogo": False,
            "mbc": 0x0F if mode == "DMG" else 0,
            "flash_offset": 0,
            "force_wr_pullup": False,
            "voltage_fallback": False,
            "ask_voltage_fallback": False,
            "compare_sectors": True,
        },
    ]
    assert gui.STATUS["last_path"] == str(path)
    assert gui.STATUS["args"] is transfers[0]
    assert gui.grpActions.isEnabled() is False


def test_flash_rom_converted_isx_uses_in_memory_buffer_request(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, "AGB")
    path = tmp_path / "converted.isx"
    path.write_bytes(b"synthetic conversion input")
    converted = bytearray(b"R" * 0x1200)
    conversion_inputs: list[bytearray] = []

    def convert(source: bytearray) -> bytearray:
        conversion_inputs.append(source)
        return converted

    monkeypatch.setattr(gui_module, "from_isx", convert)
    monkeypatch.setattr(
        gui_module,
        "RomFileAGB",
        lambda _buffer: SimpleNamespace(GetHeader=lambda: {"logo_correct": True, "header_checksum_correct": True}),
    )
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Ok)
    monkeypatch.setattr(
        gui_module.QtWidgets,
        "QFileDialog",
        SimpleNamespace(getOpenFileName=Mock(side_effect=AssertionError("File dialog opened"))),
    )

    gui.FlashROM(str(path))

    assert conversion_inputs == [bytearray(b"synthetic conversion input")]
    transfers = [args for name, args in device.calls if name == "flash"]
    assert transfers == [
        {
            "path": "",
            "buffer": converted,
            "cart_type": 1,
            "override_voltage": False,
            "prefer_chip_erase": False,
            "fast_read_mode": True,
            "verify_write": True,
            "fix_header": False,
            "fix_bootlogo": False,
            "mbc": 0,
            "voltage_fallback": False,
            "ask_voltage_fallback": False,
            "compare_sectors": False,
        },
    ]
    assert gui.STATUS["last_path"] == str(path)


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_flash_rom_cancelled_file_dialog_and_wipe_refusal_do_not_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, mode)
    monkeypatch.setattr(gui_module.QtWidgets, "QFileDialog", SimpleNamespace(getOpenFileName=lambda *_args: ("", "")))
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.No)

    gui.FlashROM()

    assert not any(name == "flash" for name, _args in device.calls)
    assert "last_path" not in gui.STATUS
    assert gui.grpActions.isEnabled() is True


def test_flash_rom_rejected_drop_confirmation_does_not_read_or_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, "DMG")
    path = tmp_path / "dropped.gb"
    path.write_bytes(bytes(0x1000))
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Cancel)
    monkeypatch.setattr(
        gui_module.QtWidgets,
        "QFileDialog",
        SimpleNamespace(getOpenFileName=Mock(side_effect=AssertionError("File dialog opened"))),
    )

    gui.FlashROM(str(path))

    assert path.read_bytes() == bytes(0x1000)
    assert not any(name == "flash" for name, _args in device.calls)
    assert gui.grpActions.isEnabled() is True


@pytest.mark.parametrize("reason", ["empty", "larger_than_profile"])
def test_flash_rom_rejects_unsuitable_input_before_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, "DMG")
    path = tmp_path / "unsuitable.gb"
    path.write_bytes(b"" if reason == "empty" else bytes(0x1000))
    if reason == "larger_than_profile":
        device.INFO["dmg_carts"][1][1]["flash_size"] = 0x800
        monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Cancel)
    monkeypatch.setattr(
        gui_module.QtWidgets, "QFileDialog", SimpleNamespace(getOpenFileName=lambda *_args: (str(path), ""))
    )

    gui.FlashROM()

    assert not any(name == "flash" for name, _args in device.calls)
    assert gui.grpActions.isEnabled() is True


def test_flash_rom_rejected_voltage_warning_does_not_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_rom_gui(gui_module, tmp_path, monkeypatch, "DMG")
    path = tmp_path / "flash.gb"
    path.write_bytes(bytes(0x1000))
    device.INFO["dmg_carts"][1][1]["voltage"] = 3.3
    device.INFO.update(voltage_autoswitch=True, voltage_code=False)
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Cancel)
    monkeypatch.setattr(
        gui_module.QtWidgets, "QFileDialog", SimpleNamespace(getOpenFileName=lambda *_args: (str(path), ""))
    )
    monkeypatch.setattr(
        gui_module,
        "RomFileDMG",
        lambda _buffer: SimpleNamespace(
            GetHeader=lambda: {"mapper_raw": 0x0F, "logo_correct": True, "header_checksum_correct": True}
        ),
    )

    gui.FlashROM()

    assert not any(name == "flash" for name, _args in device.calls)
    assert gui.grpActions.isEnabled() is True


@pytest.mark.parametrize("operation", ["backup", "write"])
@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_ordinary_save_preparation_bypasses_profile_detection(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mode: str,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    detection = Mock()
    gui.DetectCartridge = detection

    assert prepare_save_cartridge(gui, operation, mode, str(tmp_path / "ordinary.sav")) is True

    detection.assert_not_called()
    assert "detected_cart_type" not in gui.STATUS
    assert device.calls == []


@pytest.mark.parametrize(
    ("operation", "mode", "save_kind", "erase"),
    [
        ("backup", "DMG", "batteryless", False),
        ("backup", "AGB", "batteryless", False),
        ("backup", "DMG", "photo", False),
        ("write", "DMG", "batteryless", False),
        ("write", "AGB", "batteryless", True),
        ("write", "DMG", "photo", True),
    ],
)
def test_special_save_preparation_starts_profile_detection_with_resume_arguments(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mode: str,
    save_kind: str,
    erase: bool,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    select_detection_save(gui, mode, save_kind)
    profile_combo = gui.cmbAGBCartridgeTypeResult if mode == "AGB" else gui.cmbDMGCartridgeTypeResult
    profile_combo.setCurrentIndex(0)
    detection = Mock()
    gui.DetectCartridge = detection
    path = str(tmp_path / f"{operation}-{mode.lower()}.sav")

    assert prepare_save_cartridge(gui, operation, mode, path, erase=erase) is False

    assert gui.STATUS["detected_cart_type"] == ("WAITING_SAVE_READ" if operation == "backup" else "WAITING_SAVE_WRITE")
    expected_args: dict[str, object] = {"dpath": path}
    if operation == "write":
        expected_args["erase"] = erase
    assert gui.STATUS["detect_cartridge_args"] == expected_args
    assert gui.STATUS["can_skip_message"] is True
    detection.assert_called_once_with(checkSaveType=True)
    assert device.calls == []


def test_save_write_test_mode_bypasses_special_profile_detection(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "AGB")
    select_detection_save(gui, "AGB", "batteryless")
    gui.cmbAGBCartridgeTypeResult.setCurrentIndex(0)
    detection = Mock()
    gui.DetectCartridge = detection

    assert gui._prepare_save_write_cartridge("AGB", "", erase=False, test=True) is True

    detection.assert_not_called()
    assert "detected_cart_type" not in gui.STATUS
    assert device.calls == []


@pytest.mark.parametrize("operation", ["backup", "write"])
def test_special_save_preparation_rejects_legacy_firmware_before_detection(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    select_detection_save(gui, "DMG", "batteryless")
    device.GetFWBuildDate = lambda: ""  # type: ignore[method-assign]
    detection = Mock()
    gui.DetectCartridge = detection
    dialog = SimpleNamespace(exec=Mock(return_value=FakeMessageBox.StandardButton.Ok))
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    assert prepare_save_cartridge(gui, operation, "DMG", "legacy.sav") is False

    dialog.exec.assert_called_once()
    detection.assert_not_called()
    assert "detected_cart_type" not in gui.STATUS
    assert device.calls == []


@pytest.mark.parametrize("operation", ["backup", "write"])
@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_selected_profile_and_batteryless_metadata_skip_detection(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mode: str,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    select_detection_save(gui, mode, "batteryless")
    device.INFO["dump_info"] = {
        "batteryless_sram": {"bl_offset": 0x100000, "bl_size": 0x8000},
    }
    detection = Mock()
    gui.DetectCartridge = detection

    assert prepare_save_cartridge(gui, operation, mode, "selected.sav") is True

    detection.assert_not_called()
    assert "detected_cart_type" not in gui.STATUS
    assert device.calls == []


@pytest.mark.parametrize("operation", ["backup", "write"])
@pytest.mark.parametrize("detected", [False, None, 0, "wrong type"])
def test_save_profile_resumption_rejects_invalid_detection_without_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    detected: object,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    select_detection_save(gui, "DMG", "photo")
    gui.cmbDMGCartridgeTypeResult.setCurrentIndex(0)
    detection = Mock()
    gui.DetectCartridge = detection
    critical = Mock(return_value=FakeMessageBox.StandardButton.Ok)
    monkeypatch.setattr(FakeMessageBox, "critical", critical)
    path = str(tmp_path / "resume.sav")

    if operation == "backup":
        gui.BackupRAM(dpath=path)
        expected_args = {"dpath": path}
    else:
        gui.WriteRAM(erase=True)
        expected_args = {"dpath": "", "erase": True}
    assert gui.STATUS["detect_cartridge_args"] == expected_args

    gui._ResumeDetectedCartridgeAction(cast("Any", detected))

    detection.assert_called_once_with(checkSaveType=True)
    assert critical.call_count == (0 if detected is False else 1)
    assert "detected_cart_type" not in gui.STATUS
    assert "detect_cartridge_args" not in gui.STATUS
    assert gui.STATUS["can_skip_message"] is False
    assert gui.cmbDMGCartridgeTypeResult.currentIndex() == 0
    assert device.calls == []


@pytest.mark.parametrize("operation", ["backup", "write"])
@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_save_profile_preparation_accepts_valid_detection_for_each_platform(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mode: str,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    select_detection_save(gui, mode, "batteryless")
    profile_combo = gui.cmbAGBCartridgeTypeResult if mode == "AGB" else gui.cmbDMGCartridgeTypeResult
    profile_combo.setCurrentIndex(0)
    gui.STATUS["detected_cart_type"] = 1

    assert prepare_save_cartridge(gui, operation, mode, "detected.sav") is True

    assert "detected_cart_type" not in gui.STATUS
    assert profile_combo.currentIndex() == 1
    assert device.calls == []


def test_backup_save_resumes_once_with_original_destination(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    select_detection_save(gui, "DMG", "photo")
    gui.cmbDMGCartridgeTypeResult.setCurrentIndex(0)
    detection = Mock()
    gui.DetectCartridge = detection
    path = str(tmp_path / "detected-backup.sav")

    gui.BackupRAM(dpath=path)
    assert gui.STATUS["detect_cartridge_args"] == {"dpath": path}

    gui._ResumeDetectedCartridgeAction(1)

    detection.assert_called_once_with(checkSaveType=True)
    assert device.calls == [
        (
            "backup_ram",
            {
                "path": path,
                "mbc": 0x0F,
                "save_type": 0x204,
                "rtc": False,
                "verify_read": True,
                "cart_type": 1,
            },
        ),
    ]
    assert gui.STATUS["args"] is device.calls[0][1]
    assert gui.STATUS["last_path"] == path
    assert "detected_cart_type" not in gui.STATUS
    assert "detect_cartridge_args" not in gui.STATUS
    assert gui.cmbDMGCartridgeTypeResult.currentIndex() == 1


def test_erase_save_resumes_once_with_original_operation(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    select_detection_save(gui, "DMG", "photo")
    gui.cmbDMGCartridgeTypeResult.setCurrentIndex(0)
    detection = Mock()
    gui.DetectCartridge = detection

    gui.WriteRAM(erase=True)
    assert gui.STATUS["detect_cartridge_args"] == {"dpath": "", "erase": True}

    gui._ResumeDetectedCartridgeAction(1)

    detection.assert_called_once_with(checkSaveType=True)
    assert device.calls == [
        (
            "restore_ram",
            {
                "path": "",
                "mbc": 0x0F,
                "save_type": 0x204,
                "rtc": False,
                "rtc_advance": False,
                "erase": True,
                "verify_write": True,
                "cart_type": 1,
            },
        ),
    ]
    assert gui.STATUS["args"] is device.calls[0][1]
    assert gui.STATUS["last_path"] == ""
    assert "detected_cart_type" not in gui.STATUS
    assert "detect_cartridge_args" not in gui.STATUS
    assert gui.cmbDMGCartridgeTypeResult.currentIndex() == 1


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_backup_ram_dispatches_selected_save_request(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    path = tmp_path / f"{mode.lower()}.sav"
    gui.SETTINGS.values["VerifyData"] = "enabled"
    monkeypatch.setattr(
        gui_module.QtWidgets, "QFileDialog", SimpleNamespace(getSaveFileName=lambda *_args: (str(path), ""))
    )

    gui.BackupRAM()

    request = {
        "path": str(path),
        "mbc": 0x0F if mode == "DMG" else 0,
        "save_type": 3,
        "rtc": False,
        "verify_read": True,
        "cart_type": 1,
    }
    assert device.calls == [("backup_ram", request)]
    assert gui.STATUS["args"] is device.calls[0][1]
    assert gui.STATUS["last_path"] == str(path)
    assert gui.grpActions.isEnabled() is False


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_backup_ram_unknown_size_and_cancelled_selection_do_not_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    save_combo = gui.cmbDMGHeaderSaveTypeResult if mode == "DMG" else gui.cmbAGBSaveTypeResult
    save_combo.setCurrentIndex(0)
    dialog = Mock(return_value=("", ""))
    monkeypatch.setattr(gui_module.QtWidgets, "QFileDialog", SimpleNamespace(getSaveFileName=dialog))

    gui.BackupRAM()

    assert device.calls == []
    dialog.assert_not_called()
    assert gui.grpActions.isEnabled() is True

    save_combo.setCurrentIndex(4 if mode == "DMG" else 3)
    gui.BackupRAM()

    dialog.assert_called_once()
    assert device.calls == []
    assert "args" not in gui.STATUS
    assert gui.grpActions.isEnabled() is True


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_prepare_save_write_accepts_valid_file_and_write_ram_dispatches_exact_request(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    path = tmp_path / f"restore-{mode.lower()}.sav"
    path.write_bytes(bytes(0x8000))
    gui.SETTINGS.values["VerifyData"] = "disabled"
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Ok)

    preparation = gui._PrepareSaveWrite(dpath=str(path))
    assert preparation is not None
    assert tuple(preparation) == (mode, str(path), 0x0F if mode == "DMG" else 0, 3, 1, 0x8000, None)
    assert device.calls == []

    gui.WriteRAM(dpath=str(path))

    request = {
        "path": str(path),
        "mbc": 0x0F if mode == "DMG" else 0,
        "save_type": 3,
        "rtc": False,
        "rtc_advance": False,
        "erase": False,
        "verify_write": False,
        "cart_type": 1,
    }
    assert device.calls == [("restore_ram", request)]
    assert gui.STATUS["args"] is device.calls[0][1]
    assert gui.STATUS["last_path"] == str(path)
    assert gui.grpActions.isEnabled() is False


@pytest.mark.parametrize("size", [0, 0x200001])
def test_write_ram_rejects_unsupported_file_size_before_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "AGB")
    path = tmp_path / "wrong-size.sav"
    with path.open("wb") as save_file:
        save_file.truncate(size)
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Ok)
    critical = Mock(return_value=FakeMessageBox.StandardButton.Ok)
    monkeypatch.setattr(FakeMessageBox, "critical", critical)

    assert gui._PrepareSaveWrite(dpath=str(path)) is None
    gui.WriteRAM(dpath=str(path))

    assert critical.call_count == 2
    assert "size of this file is not supported" in critical.call_args.args[2]
    assert device.calls == []
    assert gui.grpActions.isEnabled() is True


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_write_ram_cancelled_selection_and_destructive_refusal_do_not_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    dialog = Mock(return_value=("", ""))
    monkeypatch.setattr(gui_module.QtWidgets, "QFileDialog", SimpleNamespace(getOpenFileName=dialog))

    gui.WriteRAM()

    dialog.assert_called_once()
    assert device.calls == []
    assert gui.grpActions.isEnabled() is True

    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Cancel)
    gui.WriteRAM(erase=True)

    assert device.calls == []
    assert "args" not in gui.STATUS
    assert gui.grpActions.isEnabled() is True


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_prepare_save_write_rejects_unknown_save_size(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    save_combo = gui.cmbDMGHeaderSaveTypeResult if mode == "DMG" else gui.cmbAGBSaveTypeResult
    save_combo.setCurrentIndex(0)
    dialog = Mock(side_effect=AssertionError("File dialog opened"))
    monkeypatch.setattr(gui_module.QtWidgets, "QFileDialog", SimpleNamespace(getOpenFileName=dialog))

    assert gui._PrepareSaveWrite() is None
    gui.WriteRAM()

    dialog.assert_not_called()
    assert device.calls == []
    assert gui.grpActions.isEnabled() is True


def test_write_ram_confirmed_erase_dispatches_without_a_file(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    monkeypatch.setattr(FakeMessageBox, "next_answer", FakeMessageBox.StandardButton.Ok)

    gui.WriteRAM(erase=True)

    assert device.calls == [
        (
            "restore_ram",
            {
                "path": "",
                "mbc": 0x0F,
                "save_type": 3,
                "rtc": False,
                "rtc_advance": False,
                "erase": True,
                "verify_write": True,
                "cart_type": 1,
            },
        )
    ]


@pytest.mark.parametrize("save_kind", ["camera", "ereader"])
def test_calibration_save_rejects_legacy_firmware(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save_kind: str,
) -> None:
    mode = "DMG" if save_kind == "camera" else "AGB"
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    if save_kind == "camera":
        configure_camera_calibration(device)
    else:
        configure_ereader_calibration(device)
    monkeypatch.setattr(device, "GetFWBuildDate", lambda: "")
    dialog = FakeDecisionMessageBox()
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    if save_kind == "camera":
        result = gui._prepare_camera_save(path="unused.sav", erase=False, test=False)
    else:
        result = gui._prepare_ereader_save(path="unused.sav", erase=False)

    assert result == (False, None)
    assert dialog.exec_count == 1
    assert dialog.buttons == {}


@pytest.mark.parametrize("save_kind", ["camera", "ereader"])
def test_matching_calibration_skips_decision_and_preserves_entire_save(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save_kind: str,
) -> None:
    mode = "DMG" if save_kind == "camera" else "AGB"
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    original = patterned_save()
    if save_kind == "camera":
        calibration1, calibration2 = configure_camera_calibration(device)
        original[0x4FF2:0x5000] = calibration1
        original[0x11FF2:0x12000] = calibration2
    else:
        calibration = configure_ereader_calibration(device)
        original[0xD000:0xF000] = calibration
    path = tmp_path / f"matching-{save_kind}.sav"
    path.write_bytes(original)
    dialog = FakeDecisionMessageBox()
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    if save_kind == "camera":
        result = gui._prepare_camera_save(path=str(path), erase=False, test=False)
        assert set(dialog.buttons) == {"keep", "reset", "overwrite", "cancel"}
    else:
        result = gui._prepare_ereader_save(path=str(path), erase=False)
        assert set(dialog.buttons) == {"keep", "overwrite", "cancel"}

    assert result == (True, original)
    assert dialog.exec_count == 0


@pytest.mark.parametrize("save_kind", ["camera", "ereader"])
@pytest.mark.parametrize("decision", ["keep", "overwrite", "cancel"])
def test_calibration_save_honors_keep_overwrite_and_cancel(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save_kind: str,
    decision: str,
) -> None:
    mode = "DMG" if save_kind == "camera" else "AGB"
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    original = patterned_save()
    expected = original.copy()
    if save_kind == "camera":
        calibration1, calibration2 = configure_camera_calibration(device)
        if decision == "keep":
            expected[0x4FF2:0x5000] = calibration1
            expected[0x11FF2:0x12000] = calibration2
    else:
        calibration = configure_ereader_calibration(device)
        if decision == "keep":
            expected[0xD000:0xF000] = calibration
    path = tmp_path / f"{save_kind}-{decision}.sav"
    path.write_bytes(original)
    dialog = FakeDecisionMessageBox(decision)
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    if save_kind == "camera":
        continue_write, buffer = gui._prepare_camera_save(path=str(path), erase=False, test=False)
    else:
        continue_write, buffer = gui._prepare_ereader_save(path=str(path), erase=False)

    assert dialog.exec_count == 1
    if decision == "cancel":
        assert (continue_write, buffer) == (False, None)
    else:
        assert continue_write is True
        assert buffer == expected


def test_camera_calibration_can_be_forced_without_changing_other_bytes(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    configure_camera_calibration(device)
    original = patterned_save()
    expected = original.copy()
    expected[0x4FF2:0x5000] = bytes([0xAA] * 0xE)
    expected[0x11FF2:0x12000] = bytes([0xAA] * 0xE)
    path = tmp_path / "camera-reset.sav"
    path.write_bytes(original)
    dialog = FakeDecisionMessageBox("reset")
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    continue_write, buffer = gui._prepare_camera_save(path=str(path), erase=False, test=False)

    assert continue_write is True
    assert buffer == expected
    assert dialog.exec_count == 1


@pytest.mark.parametrize("photo", [False, True])
def test_camera_erase_uses_photo_layout_and_available_choices(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    photo: bool,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    configure_camera_calibration(device)
    gui.cmbDMGHeaderSaveTypeResult.setCurrentIndex(13 if photo else 4)
    dialog = FakeDecisionMessageBox("overwrite")
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    continue_write, buffer = gui._prepare_camera_save(path="", erase=True, test=False)

    expected = bytearray(0x20000)
    if photo:
        expected.extend([0xFF] * 0xE0000)
    assert continue_write is True
    assert buffer == expected
    assert ("reset" in dialog.buttons) is not photo
    assert set(dialog.buttons) == (
        {"keep", "overwrite", "cancel"} if photo else {"keep", "reset", "overwrite", "cancel"}
    )


def test_ereader_erase_can_keep_only_the_protected_calibration_range(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "AGB")
    calibration = configure_ereader_calibration(device)
    dialog = FakeDecisionMessageBox("keep")
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    continue_write, buffer = gui._prepare_ereader_save(path="", erase=True)

    expected = bytearray([0xFF] * 0x20000)
    expected[0xD000:0xF000] = calibration
    assert continue_write is True
    assert buffer == expected
    assert dialog.exec_count == 1


@pytest.mark.parametrize("save_kind", ["camera", "ereader"])
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (FakeMessageBox.StandardButton.Yes, True),
        (FakeMessageBox.StandardButton.No, False),
    ],
)
def test_absent_calibration_warning_can_be_accepted_or_declined(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save_kind: str,
    answer: int,
    expected: bool,
) -> None:
    mode = "DMG" if save_kind == "camera" else "AGB"
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    device.INFO["db"] = None
    warning = Mock(return_value=answer)
    monkeypatch.setattr(FakeMessageBox, "warning", warning)
    dialog = FakeDecisionMessageBox()
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    if save_kind == "camera":
        result = gui._prepare_camera_save(path="unused.sav", erase=False, test=False)
    else:
        result = gui._prepare_ereader_save(path="unused.sav", erase=False)

    assert result == (expected, None)
    warning.assert_called_once()
    assert dialog.exec_count == 0


def test_camera_calibration_test_mode_bypasses_data_and_decision(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    device.INFO["db"] = None
    read_header = Mock(return_value={})
    monkeypatch.setattr(device, "ReadHeader", read_header)
    dialog = FakeDecisionMessageBox()
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    result = gui._prepare_camera_save(path="missing.sav", erase=False, test=True)

    assert result == (True, None)
    assert set(dialog.buttons) == {"keep", "reset"}
    assert dialog.exec_count == 0
    read_header.assert_called_once_with()


@pytest.mark.parametrize("save_kind", ["camera", "ereader"])
def test_write_ram_calibration_refusal_stops_before_transfer(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save_kind: str,
) -> None:
    mode = "DMG" if save_kind == "camera" else "AGB"
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, mode)
    if save_kind == "camera":
        configure_camera_calibration(device)
    else:
        configure_ereader_calibration(device)
    path = tmp_path / f"refused-{save_kind}.sav"
    path.write_bytes(patterned_save())
    dialog = FakeDecisionMessageBox("cancel")
    monkeypatch.setattr(gui_module, "_create_message_box", lambda **_kwargs: dialog)

    gui.WriteRAM(dpath=str(path), skip_warning=True)

    assert dialog.exec_count == 1
    assert device.calls == []
    assert "args" not in gui.STATUS
    assert gui.grpActions.isEnabled() is True


@pytest.mark.parametrize("verified", [True, False])
def test_finish_flash_rom_shows_result_and_restores_controls(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    device.INFO["last_action"] = 4
    gui.PROGRESS.PROGRESS["verified"] = verified
    gui.grpActions.setEnabled(False)
    gui.btnCancel.setEnabled(True)
    refresh = Mock()
    gui.ReadCartridge = refresh
    boxes: list[FakeMessageBox] = []

    def make_box(**_kwargs: object) -> FakeMessageBox:
        box = FakeMessageBox()
        boxes.append(box)
        return box

    monkeypatch.setattr(gui_module, "_create_message_box", make_box)

    gui.FinishOperation()

    assert len(boxes) == 1
    expected = "written and verified successfully" if verified else "ROM writing complete"
    assert expected in boxes[0].text()
    assert gui.lblStatus4a.text() == "Done!"
    assert device.INFO["last_action"] == 0
    refresh.assert_called_once_with(resetStatus=False)
    assert gui.grpActions.isEnabled() is True
    assert gui.btnCancel.isEnabled() is False


@pytest.mark.parametrize("answer", [FakeMessageBox.StandardButton.Yes, FakeMessageBox.StandardButton.No])
def test_finish_flash_rom_verification_failure_retry_or_decline(
    gui_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: int,
) -> None:
    gui, device = build_save_gui(gui_module, tmp_path, monkeypatch, "DMG")
    sectors = [[0x2000, 0x1000]]
    device.INFO.update(last_action=4, broken_sectors=sectors)
    gui.STATUS["args"] = {"path": "rom.gb", "verify_write": True}
    gui.grpActions.setEnabled(False)
    gui.btnCancel.setEnabled(True)
    gui.ReadCartridge = Mock()
    warnings: list[str] = []

    def warn(_parent: object, _title: str, message: str, *_args: object) -> int:
        warnings.append(message)
        return answer

    monkeypatch.setattr(FakeMessageBox, "warning", warn)
    boxes: list[FakeMessageBox] = []

    def make_box(**_kwargs: object) -> FakeMessageBox:
        box = FakeMessageBox()
        boxes.append(box)
        return box

    monkeypatch.setattr(gui_module, "_create_message_box", make_box)

    gui.FinishOperation()

    assert len(warnings) == 1
    assert "0x2000~0x2FFF" in warnings[0]
    assert "failed" in warnings[0]
    if answer == FakeMessageBox.StandardButton.Yes:
        assert device.calls == [("flash", {"path": "rom.gb", "verify_write": True, "flash_sectors": sectors})]
        assert device.INFO["last_action"] == 4
        assert gui.grpActions.isEnabled() is True
        gui.ReadCartridge.assert_not_called()
    else:
        assert device.calls == []
        assert device.INFO["last_action"] == 0
        assert "ROM writing complete" in boxes[0].text()
        assert gui.lblStatus4a.text() == "Done!"
        assert gui.grpActions.isEnabled() is True
        assert gui.btnCancel.isEnabled() is False
        gui.ReadCartridge.assert_called_once_with(resetStatus=False)


def test_cancelled_flash_rom_shows_abort_result_and_restores_controls(
    gui_module: ModuleType,
    tmp_path: Path,
) -> None:
    gui = build_gui(gui_module, tmp_path)
    device = FakeDevice("DMG")
    device.INFO["last_action"] = 4
    device.CANCEL = True
    gui.CONN = device
    gui.grpActions.setEnabled(False)
    gui.btnCancel.setEnabled(True)
    gui.ReadCartridge = Mock()

    gui.UpdateProgress({"action": "ABORT", "info_type": "label", "info_msg": "ROM write cancelled"})

    assert gui.lblStatus4a.text() == "ROM write cancelled"
    assert gui.grpActions.isEnabled() is True
    assert gui.btnCancel.isEnabled() is False
    assert device.CANCEL is False
    assert not any(name == "flash" for name, _args in device.calls)
    gui.ReadCartridge.assert_not_called()
