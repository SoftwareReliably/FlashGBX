"""Headless tests for the reusable user-input dialog."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], object]] = []

    def connect(self, callback: Callable[[], object]) -> None:
        self.callbacks.append(callback)

    def emit(self) -> None:
        for callback in self.callbacks:
            callback()


class FakeWidget:
    def __init__(self, text: object = "", *_args: object, **_kwargs: object) -> None:
        self.text = str(text)
        self.enabled = True
        self.checked = False
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def setMaximumWidth(self, width: int) -> None:
        self.calls.append(("setMaximumWidth", (width,)))

    def setWordWrap(self, enabled: bool) -> None:
        self.calls.append(("setWordWrap", (enabled,)))

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def setChecked(self, checked: bool) -> None:
        self.checked = checked

    def setText(self, text: str) -> None:
        self.text = text


class FakeButton(FakeWidget):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.clicked = FakeSignal()


class FakeComboBox(FakeWidget):
    SizeAdjustPolicy = SimpleNamespace(AdjustToContents=1)

    def __init__(self) -> None:
        super().__init__()
        self.items: list[str] = []
        self.index = -1
        self.editable = False
        self.adjust_policy: object = None

    def clear(self) -> None:
        self.items.clear()

    def addItems(self, items: list[str]) -> None:
        self.items.extend(items)

    def setCurrentIndex(self, index: int) -> None:
        self.index = index

    def currentIndex(self) -> int:
        return self.index

    def setEditable(self, editable: bool) -> None:
        self.editable = editable

    def setSizeAdjustPolicy(self, policy: object) -> None:
        self.adjust_policy = policy


class FakeSpinBox(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.minimum = 0
        self.maximum = 0
        self.number = 0

    def setRange(self, minimum: int, maximum: int) -> None:
        self.minimum = minimum
        self.maximum = maximum

    def setValue(self, value: int) -> None:
        self.number = value

    def value(self) -> int:
        return self.number


class FakeLayout:
    def __init__(self) -> None:
        self.widgets: list[tuple[object, tuple[object, ...]]] = []
        self.layouts: list[tuple[object, tuple[object, ...]]] = []
        self.stretches: list[int] = []
        self.column_stretches: list[tuple[int, int]] = []

    def addWidget(self, widget: object, *position: object) -> None:
        self.widgets.append((widget, position))

    def addLayout(self, layout: object, *position: object) -> None:
        self.layouts.append((layout, position))

    def addStretch(self, stretch: int) -> None:
        self.stretches.append(stretch)

    def setColumnStretch(self, column: int, stretch: int) -> None:
        self.column_stretches.append((column, stretch))


class FakeDialog:
    DialogCode = SimpleNamespace(Accepted=1, Rejected=0)

    def __init__(self, parent: object) -> None:
        self.parent = parent
        self._flags = 0
        self.result = self.DialogCode.Rejected
        self.window_icon: object = None
        self.stylesheet = ""
        self.title = ""
        self.layout: object = None

    def setWindowIcon(self, icon: object) -> None:
        self.window_icon = icon

    def setStyleSheet(self, stylesheet: str) -> None:
        self.stylesheet = stylesheet

    def setWindowTitle(self, title: str) -> None:
        self.title = title

    def windowFlags(self) -> int:
        return self._flags

    def setWindowFlags(self, flags: int) -> None:
        self._flags = flags

    def setLayout(self, layout: object) -> None:
        self.layout = layout

    def accept(self) -> None:
        self.result = self.DialogCode.Accepted

    def reject(self) -> None:
        self.result = self.DialogCode.Rejected


class FakeIcon:
    def __init__(self, source: object = None) -> None:
        self.source = source


def fake_pyside_module() -> ModuleType:
    qt_core = SimpleNamespace(
        Qt=SimpleNamespace(
            WindowType=SimpleNamespace(MSWindowsFixedSizeDialogHint=1, WindowContextHelpButtonHint=2),
            AlignmentFlag=SimpleNamespace(AlignRight=4),
        ),
    )
    qt_gui = SimpleNamespace(QIcon=FakeIcon)
    qt_widgets = SimpleNamespace(
        QWidget=FakeWidget,
        QDialog=FakeDialog,
        QLabel=FakeWidget,
        QPushButton=FakeButton,
        QComboBox=FakeComboBox,
        QSpinBox=FakeSpinBox,
        QCheckBox=FakeWidget,
        QGridLayout=FakeLayout,
        QHBoxLayout=FakeLayout,
    )
    fake_pyside = ModuleType("PySide6")
    fake_pyside.QtCore = qt_core  # type: ignore[attr-defined]
    fake_pyside.QtGui = qt_gui  # type: ignore[attr-defined]
    fake_pyside.QtWidgets = qt_widgets  # type: ignore[attr-defined]
    return fake_pyside


@pytest.fixture(scope="module")
def dialog_module() -> ModuleType:
    original_module = importlib.import_module("FlashGBX.UserInputDialog")
    original_pyside = sys.modules["PySide6"]
    module_path = Path(original_module.__file__)
    sys.modules["PySide6"] = fake_pyside_module()
    try:
        spec = importlib.util.spec_from_file_location("FlashGBX.UserInputDialog", module_path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["FlashGBX.UserInputDialog"] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules["PySide6"] = original_pyside
        sys.modules["FlashGBX.UserInputDialog"] = original_module


def test_default_dialog_connects_accept_and_reject_actions(dialog_module: ModuleType) -> None:
    dialog = dialog_module.UserInputDialog(None)

    assert dialog.APP is None
    assert dialog.title.endswith(" – ")
    assert dialog.paramWidgets == {}
    assert dialog.lblIntro.text == ""
    assert dialog.window_icon is None

    dialog.btnOK.clicked.emit()
    assert dialog.result == FakeDialog.DialogCode.Accepted
    dialog.btnCancel.clicked.emit()
    assert dialog.result == FakeDialog.DialogCode.Rejected


def test_dialog_builds_all_supported_parameter_widgets(dialog_module: ModuleType) -> None:
    app = SimpleNamespace(activateWindow=lambda: None)
    icon_source = object()
    args: dict[str, Any] = {
        "title": "Backup options",
        "intro": "Choose how the cartridge should be read.",
        "params": [
            ["reader", "cmb", "Reader:", ["Automatic", "Stable"], 1],
            ["single", "cmb", "Only choice:", ["Fixed"], 0],
            ["filename", "cmb_e", "Filename:", ["pokemon-red.gb"], 0],
            ["retries", "spb", "Retries:", [1, 10], 4],
            ["verify", "chk", "Verify after reading", None, True],
            ["ignored", "unsupported", "Ignored", [], 0],
        ],
    }

    dialog = dialog_module.UserInputDialog(app, icon=icon_source, args=args)
    result = dialog.GetResult()

    assert dialog.APP is app
    assert dialog.title.endswith(" – Backup options")
    assert isinstance(dialog.window_icon, FakeIcon)
    assert dialog.window_icon.source is icon_source
    assert set(result) == {"reader", "single", "filename", "retries", "verify"}

    reader = result["reader"]
    assert reader.items == ["Automatic", "Stable"]
    assert reader.currentIndex() == 1
    assert reader.enabled is True
    assert reader.adjust_policy == FakeComboBox.SizeAdjustPolicy.AdjustToContents

    assert result["single"].enabled is False
    assert result["filename"].editable is True
    assert result["retries"].value() == 4
    assert (result["retries"].minimum, result["retries"].maximum) == (1, 10)
    assert result["verify"].checked is True
    assert result["verify"].text == "Verify after reading"


def test_hide_event_reactivates_parent_when_present(dialog_module: ModuleType) -> None:
    activations: list[bool] = []
    app = SimpleNamespace(activateWindow=lambda: activations.append(True))
    dialog = dialog_module.UserInputDialog(app)

    dialog.hideEvent(object())
    assert activations == [True]

    dialog.APP = None
    dialog.hideEvent(object())
    assert activations == [True]
