"""Headless tests for the Game Boy Camera album window."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call

import pytest
from PIL import Image
from PySide6 import QtCore, QtWidgets

from FlashGBX.PocketCamera import PocketCamera
from FlashGBX.PocketCameraWindow import PocketCameraWindow, _parse_palette_setting

from .test_pocket_camera import camera_save

camera_window_module = importlib.import_module("FlashGBX.PocketCameraWindow")


class FakeSettings:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = {} if values is None else dict(values)
        self.writes: list[tuple[str, object]] = []

    def value(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def setValue(self, key: str, value: object) -> None:
        self.values[key] = value
        self.writes.append((key, value))


class FakeCombo:
    def __init__(self, index: int) -> None:
        self.index = index

    def currentIndex(self) -> int:
        return self.index

    def setCurrentIndex(self, index: int) -> None:
        self.index = index


class FakeLabel:
    def __init__(self) -> None:
        self.tooltip = ""
        self.pixmap: object | None = None
        self.styles: list[str] = []

    def setToolTip(self, value: str) -> None:
        self.tooltip = value

    def setPixmap(self, value: object) -> None:
        self.pixmap = value

    def setStyleSheet(self, value: str) -> None:
        self.styles.append(value)


class FakeMouseEvent:
    def __init__(self, button: QtCore.Qt.MouseButton) -> None:
        self._button = button

    def button(self) -> QtCore.Qt.MouseButton:
        return self._button


class FakeDropEvent:
    def __init__(self, urls: list[QtCore.QUrl] | None = None) -> None:
        self._urls = urls
        self.accepted = False
        self.ignored = False
        self.drop_action: object | None = None

    def mimeData(self) -> SimpleNamespace:
        urls = [] if self._urls is None else self._urls
        return SimpleNamespace(hasUrls=lambda: self._urls is not None, urls=lambda: urls)

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True

    def setDropAction(self, action: object) -> None:
        self.drop_action = action


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)


class FakeQtObject:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.currentIndexChanged = FakeSignal()
        self.clicked = FakeSignal()
        self.items: list[str] = []
        self.index = -1
        self.number = 0
        self.checked = False
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, name: str):
        def method(*args: object, **_kwargs: object) -> FakeQtObject:
            self.calls.append((name, args))
            return self

        return method

    def windowFlags(self) -> int:
        return 0

    def view(self) -> FakeQtObject:
        return self

    def addItems(self, items: list[str]) -> None:
        self.items.extend(items)

    def addItem(self, item: str) -> None:
        self.items.append(item)

    def setCurrentIndex(self, index: int) -> None:
        self.index = index

    def currentIndex(self) -> int:
        return self.index

    def setValue(self, value: int) -> None:
        self.number = value

    def value(self) -> int:
        return self.number

    def setChecked(self, checked: bool) -> None:
        self.checked = checked

    def isChecked(self) -> bool:
        return self.checked


class FakeDialog(FakeQtObject):
    DialogCode = SimpleNamespace(Accepted=1, Rejected=0)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        super().__init__()
        self.hidden_event: object | None = None

    def hideEvent(self, event: object) -> None:
        self.hidden_event = event


class FakeComboWidget(FakeQtObject):
    SizeAdjustPolicy = SimpleNamespace(AdjustToContents=1)


def load_window_with_fake_qt(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Reload the window module against inert widgets for constructor coverage."""
    qt_core = SimpleNamespace(
        Qt=SimpleNamespace(
            WindowType=SimpleNamespace(MSWindowsFixedSizeDialogHint=1, WindowContextHelpButtonHint=2),
            ScrollBarPolicy=SimpleNamespace(ScrollBarAsNeeded=1),
            CursorShape=SimpleNamespace(PointingHandCursor=1),
            AlignmentFlag=SimpleNamespace(AlignCenter=1, AlignTop=2),
            MouseButton=QtCore.Qt.MouseButton,
            DropAction=QtCore.Qt.DropAction,
        ),
        QStandardPaths=SimpleNamespace(
            StandardLocation=SimpleNamespace(DocumentsLocation=1),
            writableLocation=lambda _location: "/mock/documents",
        ),
        QUrl=QtCore.QUrl,
    )
    standard_buttons = SimpleNamespace(Ok=1, Cancel=2)
    qt_widgets = SimpleNamespace(
        QDialog=FakeDialog,
        QGridLayout=FakeQtObject,
        QVBoxLayout=FakeQtObject,
        QHBoxLayout=FakeQtObject,
        QGroupBox=FakeQtObject,
        QLabel=FakeQtObject,
        QComboBox=FakeComboWidget,
        QSpinBox=FakeQtObject,
        QCheckBox=FakeQtObject,
        QPushButton=FakeQtObject,
        QLayout=SimpleNamespace(SizeConstraint=SimpleNamespace(SetFixedSize=1)),
        QMessageBox=SimpleNamespace(StandardButton=standard_buttons),
    )
    qt_gui = SimpleNamespace(QIcon=FakeQtObject, QCursor=FakeQtObject)
    fake_pyside = ModuleType("PySide6")
    fake_pyside.QtCore = qt_core  # type: ignore[attr-defined]
    fake_pyside.QtGui = qt_gui  # type: ignore[attr-defined]
    fake_pyside.QtWidgets = qt_widgets  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PySide6", fake_pyside)

    module_name = "FlashGBX._PocketCameraWindowFakeQt"
    module_path = Path(camera_window_module.__file__)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def bare_window(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "APP": SimpleNamespace(SETTINGS=FakeSettings()),
        "CUR_PC": None,
        "CUR_FILE": None,
        "CUR_EXPORT_PATH": "",
        "CUR_INDEX": 0,
        "CUR_BICUBIC": False,
        "CUR_PALETTE": 3,
        "PHOTO_SAVE_SIZE": PocketCameraWindow.PHOTO_SAVE_SIZE,
        "_palettes": list(PocketCamera.PALETTES),
        "SetColors": Mock(),
        "BuildPhotoList": Mock(),
        "UpdateViewer": Mock(),
        "SavePicture": Mock(),
        "OpenFile": Mock(),
        "_batch_export_path": PocketCameraWindow._batch_export_path,
        "_url_to_path": PocketCameraWindow._url_to_path,
    }
    values.update(overrides)
    window = SimpleNamespace(**values)
    window._dragEventHover = lambda event: PocketCameraWindow._dragEventHover(window, event)
    return window


def test_constructor_and_hide_event_with_inert_qt_widgets(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_window_with_fake_qt(monkeypatch)
    custom_palette = tuple(range(12))
    settings = FakeSettings(
        {
            "PocketCameraZoom": "invalid",
            "PocketCameraFrame": "enabled",
            "PocketCameraPalette": json.dumps(custom_palette),
            "LastDirPocketCamera": "/exports",
        },
    )
    app = SimpleNamespace(SETTINGS=settings, activateWindow=Mock())

    window = module.PocketCameraWindow(
        app,
        icon=object(),
        config_path="/config",
        app_path="/app",
    )

    assert window.spnZoom.value() == 2
    assert window.chkFrame.isChecked() is True
    assert window.CUR_EXPORT_PATH == "/exports"
    assert len(PocketCamera.PALETTES) == window.CUR_PALETTE
    assert window.cmbColor.items[-1] == "Custom"
    assert len(window.lblPhoto) == PocketCamera.PHOTO_COUNT

    window.cmbColor.setCurrentIndex(-1)
    event = object()
    window.hideEvent(event)
    assert settings.writes[-4:] == [
        ("PocketCameraPalette", json.dumps(PocketCamera.PALETTES[3])),
        ("PocketCameraZoom", "2"),
        ("PocketCameraFrame", "enabled"),
        ("LastDirPocketCamera", "/exports"),
    ]
    app.activateWindow.assert_called_once_with()
    assert window.hidden_event is event

    window.cmbColor.setCurrentIndex(window.CUR_PALETTE)
    window.hideEvent(event)
    assert settings.writes[-4] == ("PocketCameraPalette", json.dumps(custom_palette))


def test_constructor_uses_defaults_and_forces_exit_after_failed_initial_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_window_with_fake_qt(monkeypatch)
    settings = FakeSettings({"PocketCameraZoom": object()})
    app = SimpleNamespace(SETTINGS=settings, activateWindow=Mock())
    open_file = Mock(return_value=False)
    monkeypatch.setattr(module.PocketCameraWindow, "OpenFile", open_file)

    window = module.PocketCameraWindow(app, file=b"invalid")

    assert window.FORCE_EXIT is True
    assert window.spnZoom.value() == 2
    open_file.assert_called_once_with(b"invalid")

    default_window = module.PocketCameraWindow(app)
    assert default_window.CUR_EXPORT_PATH == "/mock/documents"


def test_palette_parser_rejects_non_strings_and_malformed_json() -> None:
    assert _parse_palette_setting(json.dumps(list(range(12)))) == tuple(range(12))
    assert _parse_palette_setting(None) is None
    assert _parse_palette_setting(123) is None
    assert _parse_palette_setting("not-json") is None
    assert _parse_palette_setting(json.dumps([True] + [0] * 11)) is None
    assert _parse_palette_setting(json.dumps([0.5] + [0] * 11)) is None


def test_open_file_loads_bytes_and_path_without_dialogs(tmp_path: Path) -> None:
    window = bare_window(CUR_EXPORT_PATH="")

    assert PocketCameraWindow.OpenFile(window, camera_save()) is True  # type: ignore[arg-type]
    assert isinstance(window.CUR_PC, PocketCamera)
    assert window.CUR_INDEX == 0
    window.SetColors.assert_called_once_with()

    save_path = tmp_path / "camera.sav"
    save_path.write_bytes(camera_save())
    window.SetColors.reset_mock()
    assert PocketCameraWindow.OpenFile(window, save_path) is True  # type: ignore[arg-type]
    assert str(tmp_path) == window.CUR_EXPORT_PATH
    window.SetColors.assert_called_once_with()


def test_open_file_handles_photo_roll_cancellation_and_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    critical = Mock()
    monkeypatch.setattr(camera_window_module.QtWidgets.QMessageBox, "critical", critical)
    photo_save = bytes(PocketCameraWindow.PHOTO_SAVE_SIZE)
    cancelled = bare_window(_select_photo_roll=Mock(return_value=None))

    assert PocketCameraWindow.OpenFile(cancelled, photo_save) is False  # type: ignore[arg-type]
    assert cancelled.CUR_PC is None
    critical.assert_not_called()

    invalid = bare_window()
    assert PocketCameraWindow.OpenFile(invalid, b"short") is False  # type: ignore[arg-type]
    assert invalid.CUR_PC is None
    critical.assert_called_once()


def test_open_file_loads_selected_photo_roll() -> None:
    selected = camera_save()
    window = bare_window(_select_photo_roll=Mock(return_value=selected))

    assert PocketCameraWindow.OpenFile(window, bytes(PocketCameraWindow.PHOTO_SAVE_SIZE)) is True  # type: ignore[arg-type]
    assert selected == window.CUR_FILE
    window.SetColors.assert_called_once_with()


def test_open_file_reports_filesystem_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    critical = Mock()
    monkeypatch.setattr(camera_window_module.QtWidgets.QMessageBox, "critical", critical)
    window = bare_window()

    assert PocketCameraWindow.OpenFile(window, tmp_path / "missing.sav") is False  # type: ignore[arg-type]
    assert window.CUR_PC is None
    critical.assert_called_once()


@pytest.mark.parametrize("as_path", [False, True])
def test_select_photo_roll_returns_selected_bank(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    as_path: bool,
) -> None:
    rolls = b"".join(bytes([index]) * PocketCamera.SAVE_SIZE for index in range(8))
    source: bytes | Path = rolls
    if as_path:
        source = tmp_path / "photo.sav"
        source.write_bytes(rolls)

    combo = SimpleNamespace(currentIndex=lambda: 3)
    dialog = SimpleNamespace(
        exec=lambda: QtWidgets.QDialog.DialogCode.Accepted,
        GetResult=lambda: {"index": combo},
    )
    dialog_factory = Mock(return_value=dialog)
    monkeypatch.setattr(camera_window_module, "UserInputDialog", dialog_factory)
    window = bare_window(windowIcon=Mock(return_value=None))

    selected = PocketCameraWindow._select_photo_roll(window, source)  # type: ignore[arg-type]

    assert selected == bytes([3]) * PocketCamera.SAVE_SIZE
    assert len(dialog_factory.call_args.kwargs["args"]["params"][0][3]) == 8


def test_select_photo_roll_returns_none_when_dialog_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    dialog = SimpleNamespace(exec=lambda: QtWidgets.QDialog.DialogCode.Rejected)
    monkeypatch.setattr(camera_window_module, "UserInputDialog", Mock(return_value=dialog))
    window = bare_window(windowIcon=Mock(return_value=None))

    assert PocketCameraWindow._select_photo_roll(window, bytes(PocketCameraWindow.PHOTO_SAVE_SIZE)) is None  # type: ignore[arg-type]


def test_palette_and_mouse_actions_update_the_viewer() -> None:
    no_camera = bare_window()
    PocketCameraWindow.SetColors(no_camera)  # type: ignore[arg-type]
    no_camera.BuildPhotoList.assert_not_called()

    camera = Mock()
    combo = FakeCombo(-1)
    window = bare_window(CUR_PC=camera, cmbColor=combo)

    PocketCameraWindow.SetColors(window)  # type: ignore[arg-type]

    assert window.CUR_PALETTE == 3
    camera.SetPalette.assert_called_once_with(PocketCamera.PALETTES[3])
    window.BuildPhotoList.assert_called_once_with()
    window.UpdateViewer.assert_called_once_with(0)

    window.UpdateViewer.reset_mock()
    PocketCameraWindow.lblPhoto_Clicked(  # type: ignore[arg-type]
        window,
        FakeMouseEvent(QtCore.Qt.MouseButton.RightButton),
        5,
    )
    assert window.CUR_INDEX == 0
    PocketCameraWindow.lblPhoto_Clicked(  # type: ignore[arg-type]
        window,
        FakeMouseEvent(QtCore.Qt.MouseButton.LeftButton),
        5,
    )
    assert window.CUR_INDEX == 5
    window.UpdateViewer.assert_called_once_with(5)

    PocketCameraWindow.lblPhotoViewer_Clicked(  # type: ignore[arg-type]
        window,
        FakeMouseEvent(QtCore.Qt.MouseButton.LeftButton),
    )
    assert window.CUR_BICUBIC is True
    window.UpdateViewer.assert_called_with(5)

    window.UpdateViewer.reset_mock()
    PocketCameraWindow.lblPhotoViewer_Clicked(  # type: ignore[arg-type]
        window,
        FakeMouseEvent(QtCore.Qt.MouseButton.RightButton),
    )
    window.UpdateViewer.assert_not_called()

    combo.index = 1
    camera.SetPalette.reset_mock()
    PocketCameraWindow.SetColors(window)  # type: ignore[arg-type]
    camera.SetPalette.assert_called_once_with(PocketCamera.PALETTES[1])


def test_open_button_and_special_picture_buttons(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    save_path = tmp_path / "camera.sav"
    settings = FakeSettings({"LastDirSaveDataDMG": 42})
    window = bare_window(APP=SimpleNamespace(SETTINGS=settings), OpenFile=Mock(return_value=True))
    monkeypatch.setattr(camera_window_module.QtCore.QStandardPaths, "writableLocation", Mock(return_value="docs"))
    monkeypatch.setattr(
        camera_window_module.QtWidgets.QFileDialog,
        "getOpenFileName",
        Mock(return_value=(str(save_path), "")),
    )

    PocketCameraWindow.btnOpenSRAM_Clicked(window)  # type: ignore[arg-type]

    window.OpenFile.assert_called_once_with(str(save_path))
    assert settings.writes == [("LastDirSaveDataDMG", str(tmp_path))]

    PocketCameraWindow.btnShowGameFace_Clicked(window)  # type: ignore[arg-type]
    assert window.CUR_INDEX == PocketCamera.GAME_FACE_INDEX
    PocketCameraWindow.btnShowLastSeen_Clicked(window)  # type: ignore[arg-type]
    assert window.CUR_INDEX == PocketCamera.LAST_SEEN_INDEX
    assert window.UpdateViewer.call_args_list[-2:] == [
        call(PocketCamera.GAME_FACE_INDEX),
        call(PocketCamera.LAST_SEEN_INDEX),
    ]


def test_open_button_returns_when_picker_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    window = bare_window()
    monkeypatch.setattr(camera_window_module.QtWidgets.QFileDialog, "getOpenFileName", Mock(return_value=("", "")))

    PocketCameraWindow.btnOpenSRAM_Clicked(window)  # type: ignore[arg-type]

    window.OpenFile.assert_not_called()


def test_open_button_only_remembers_successful_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    save_path = tmp_path / "invalid.sav"
    settings = FakeSettings({"LastDirSaveDataDMG": str(tmp_path)})
    window = bare_window(APP=SimpleNamespace(SETTINGS=settings), OpenFile=Mock(return_value=False))
    monkeypatch.setattr(
        camera_window_module.QtWidgets.QFileDialog,
        "getOpenFileName",
        Mock(return_value=(str(save_path), "")),
    )

    PocketCameraWindow.btnOpenSRAM_Clicked(window)  # type: ignore[arg-type]

    assert settings.writes == []


def test_batch_save_exports_all_images(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selected = tmp_path / "album.png"
    window = bare_window(CUR_PC=Mock(), CUR_EXPORT_PATH=str(tmp_path))
    monkeypatch.setattr(
        camera_window_module.QtWidgets.QFileDialog,
        "getSaveFileName",
        Mock(return_value=(str(selected), "")),
    )

    PocketCameraWindow.btnSaveAll_Clicked(window)  # type: ignore[arg-type]

    assert str(tmp_path) == window.CUR_EXPORT_PATH
    assert window.SavePicture.call_count == PocketCamera.IMAGE_COUNT
    assert window.SavePicture.call_args_list[0] == call(0, path=tmp_path / "album01.png")
    assert window.SavePicture.call_args_list[-1] == call(31, path=tmp_path / "album32.png")


def test_batch_save_honors_overwrite_cancellation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selected = tmp_path / "album.png"
    (tmp_path / "album01.png").write_bytes(b"existing")
    window = bare_window(CUR_PC=Mock(), CUR_EXPORT_PATH=str(tmp_path))
    monkeypatch.setattr(
        camera_window_module.QtWidgets.QFileDialog,
        "getSaveFileName",
        Mock(return_value=(str(selected), "")),
    )
    warning = Mock(return_value=QtWidgets.QMessageBox.StandardButton.Cancel)
    monkeypatch.setattr(camera_window_module.QtWidgets.QMessageBox, "warning", warning)

    PocketCameraWindow.btnSaveAll_Clicked(window)  # type: ignore[arg-type]

    warning.assert_called_once()
    window.SavePicture.assert_not_called()


def test_batch_save_can_confirm_overwrite_and_cancel_picker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selected = tmp_path / "album.png"
    (tmp_path / "album01.png").write_bytes(b"existing")
    window = bare_window(CUR_PC=Mock(), CUR_EXPORT_PATH=str(tmp_path))
    picker = Mock(return_value=(str(selected), ""))
    monkeypatch.setattr(camera_window_module.QtWidgets.QFileDialog, "getSaveFileName", picker)
    monkeypatch.setattr(
        camera_window_module.QtWidgets.QMessageBox,
        "warning",
        Mock(return_value=QtWidgets.QMessageBox.StandardButton.Ok),
    )

    PocketCameraWindow.btnSaveAll_Clicked(window)  # type: ignore[arg-type]
    assert window.SavePicture.call_count == PocketCamera.IMAGE_COUNT

    window.SavePicture.reset_mock()
    picker.return_value = ("", "")
    PocketCameraWindow.btnSaveAll_Clicked(window)  # type: ignore[arg-type]
    window.SavePicture.assert_not_called()


def test_save_buttons_and_close_are_guarded_without_a_camera() -> None:
    window = bare_window(reject=Mock(), FORCE_EXIT=False)

    PocketCameraWindow.btnSaveAll_Clicked(window)  # type: ignore[arg-type]
    PocketCameraWindow.btnSavePhoto_Clicked(window)  # type: ignore[arg-type]
    window.SavePicture.assert_not_called()

    window.CUR_PC = Mock()
    window.CUR_INDEX = 7
    PocketCameraWindow.btnSavePhoto_Clicked(window)  # type: ignore[arg-type]
    window.SavePicture.assert_called_once_with(7)

    PocketCameraWindow.btnClose_Clicked(window)  # type: ignore[arg-type]
    assert window.FORCE_EXIT is True
    window.reject.assert_called_once_with()


def test_build_photo_list_marks_deleted_images(monkeypatch: pytest.MonkeyPatch) -> None:
    no_camera = bare_window(CUR_THUMBS=["unchanged"])
    PocketCameraWindow.BuildPhotoList(no_camera)  # type: ignore[arg-type]
    assert no_camera.CUR_THUMBS == ["unchanged"]

    camera = Mock()
    camera.GetPicture.side_effect = lambda _index: Image.new("P", (128, 112))
    camera.IsEmpty.side_effect = lambda index: index != 2
    camera.IsDeleted.side_effect = lambda index: index == 2
    labels = [FakeLabel() for _ in range(PocketCamera.PHOTO_COUNT)]
    window = bare_window(CUR_PC=camera, CUR_THUMBS=[], lblPhoto=labels)
    monkeypatch.setattr(camera_window_module, "ImageQt", lambda image: image)
    monkeypatch.setattr(camera_window_module.QtGui.QPixmap, "fromImage", Mock(side_effect=lambda image: image))

    PocketCameraWindow.BuildPhotoList(window)  # type: ignore[arg-type]

    assert len(window.CUR_THUMBS) == PocketCamera.PHOTO_COUNT
    assert "deleted" in labels[2].tooltip
    assert labels[0].tooltip == ""
    assert all(label.pixmap is not None for label in labels)


@pytest.mark.parametrize(
    ("index", "bicubic", "expected_ratio", "highlighted"),
    [
        (4, False, 4, True),
        (PocketCamera.LAST_SEEN_INDEX, False, 0.5, False),
        (4, True, 0.5, True),
    ],
)
def test_update_viewer_selects_resampler_and_highlight(
    monkeypatch: pytest.MonkeyPatch,
    index: int,
    bicubic: bool,
    expected_ratio: float,
    highlighted: bool,
) -> None:
    camera = Mock()
    camera.GetPicture.return_value = Image.new("P", (128, 123 if index == PocketCamera.LAST_SEEN_INDEX else 112))
    labels = [FakeLabel() for _ in range(PocketCamera.PHOTO_COUNT)]
    viewer = FakeLabel()
    pixmap = SimpleNamespace(setDevicePixelRatio=Mock())
    window = bare_window(CUR_PC=camera, CUR_BICUBIC=bicubic, lblPhoto=labels, lblPhotoViewer=viewer, CUR_PIC=None)
    monkeypatch.setattr(camera_window_module, "ImageQt", lambda image: image)
    monkeypatch.setattr(camera_window_module.QtGui.QPixmap, "fromImage", Mock(return_value=pixmap))

    PocketCameraWindow.UpdateViewer(window, index)  # type: ignore[arg-type]

    pixmap.setDevicePixelRatio.assert_called_once_with(expected_ratio)
    assert viewer.pixmap is pixmap
    assert any("green" in style for style in labels[4].styles) is highlighted


def test_update_viewer_returns_without_camera() -> None:
    window = bare_window(lblPhoto=[FakeLabel()], lblPhotoViewer=FakeLabel())

    PocketCameraWindow.UpdateViewer(window, 0)  # type: ignore[arg-type]

    assert window.lblPhotoViewer.pixmap is None


def test_save_picture_uses_custom_frame_and_disables_it_for_last_seen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app_path = tmp_path / "app"
    config_path = tmp_path / "config"
    (app_path / "res").mkdir(parents=True)
    config_path.mkdir()
    Image.new("RGB", (160, 144), "white").save(app_path / "res" / "pc_frame.png")
    camera = Mock()
    window = bare_window(
        CUR_PC=camera,
        CUR_EXPORT_PATH=str(tmp_path),
        CONFIG_PATH=config_path,
        APP_PATH=app_path,
        chkFrame=SimpleNamespace(isChecked=lambda: True),
        spnZoom=SimpleNamespace(value=lambda: 3),
    )
    chosen = tmp_path / "chosen.png"
    monkeypatch.setattr(
        camera_window_module.QtWidgets.QFileDialog,
        "getSaveFileName",
        Mock(return_value=(str(chosen), "")),
    )

    PocketCameraWindow.SavePicture(window, 2)  # type: ignore[arg-type]

    own_frame = config_path / "pc_frame.png"
    assert own_frame.exists()
    first_frame = camera.ExportPicture.call_args.kwargs["frame"]
    assert first_frame == own_frame.read_bytes()
    assert camera.ExportPicture.call_args.kwargs["scale"] == 3

    PocketCameraWindow.SavePicture(window, PocketCamera.LAST_SEEN_INDEX, path=chosen)  # type: ignore[arg-type]
    assert camera.ExportPicture.call_args.kwargs["frame"] is False


def test_save_picture_returns_after_cancelled_picker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    camera = Mock()
    window = bare_window(
        CUR_PC=camera,
        CUR_EXPORT_PATH=str(tmp_path),
        chkFrame=SimpleNamespace(isChecked=lambda: False),
        spnZoom=SimpleNamespace(value=lambda: 2),
    )
    monkeypatch.setattr(camera_window_module.QtWidgets.QFileDialog, "getSaveFileName", Mock(return_value=("", "")))

    PocketCameraWindow.SavePicture(window, 0)  # type: ignore[arg-type]

    camera.ExportPicture.assert_not_called()


def test_save_picture_guards_missing_camera_and_exports_without_frame(tmp_path: Path) -> None:
    missing = bare_window()
    PocketCameraWindow.SavePicture(missing, 0, path=tmp_path / "ignored.png")  # type: ignore[arg-type]

    camera = Mock()
    output = tmp_path / "photo.png"
    window = bare_window(
        CUR_PC=camera,
        chkFrame=SimpleNamespace(isChecked=lambda: False),
        spnZoom=SimpleNamespace(value=lambda: 2),
    )
    PocketCameraWindow.SavePicture(window, 0, path=output)  # type: ignore[arg-type]

    camera.ExportPicture.assert_called_once_with(index=0, path=str(output), scale=2, frame=False)


def test_run_handles_force_exit_and_normal_display() -> None:
    forced = bare_window(FORCE_EXIT=True, reject=Mock())
    PocketCameraWindow.run(forced)  # type: ignore[arg-type]
    forced.reject.assert_called_once_with()

    geometry = SimpleNamespace(moveCenter=Mock(), topLeft=Mock(return_value="top-left"))
    screen = SimpleNamespace(availableGeometry=lambda: SimpleNamespace(center=lambda: "center"))
    normal = bare_window(
        FORCE_EXIT=False,
        main_layout=SimpleNamespace(update=Mock(), activate=Mock()),
        screen=Mock(return_value=screen),
        frameGeometry=Mock(return_value=geometry),
        move=Mock(),
        show=Mock(),
    )
    PocketCameraWindow.run(normal)  # type: ignore[arg-type]
    geometry.moveCenter.assert_called_once_with("center")
    normal.move.assert_called_once_with("top-left")
    normal.show.assert_called_once_with()

    no_screen = bare_window(
        FORCE_EXIT=False,
        main_layout=SimpleNamespace(update=Mock(), activate=Mock()),
        screen=Mock(return_value=None),
        frameGeometry=Mock(),
        move=Mock(),
        show=Mock(),
    )
    PocketCameraWindow.run(no_screen)  # type: ignore[arg-type]
    no_screen.move.assert_not_called()
    no_screen.show.assert_called_once_with()


def test_drag_hover_and_drop_only_accept_save_files(tmp_path: Path) -> None:
    save_path = tmp_path / "camera.sav"
    text_path = tmp_path / "readme.txt"
    save_url = QtCore.QUrl.fromLocalFile(str(save_path))
    text_url = QtCore.QUrl.fromLocalFile(str(text_path))
    window = bare_window()

    assert PocketCameraWindow._dragEventHover(window, FakeDropEvent([text_url, save_url])) is True  # type: ignore[arg-type]
    assert PocketCameraWindow._dragEventHover(window, FakeDropEvent(None)) is False  # type: ignore[arg-type]

    accepted = FakeDropEvent([text_url, save_url])
    PocketCameraWindow.dragEnterEvent(window, accepted)  # type: ignore[arg-type]
    assert accepted.accepted is True
    PocketCameraWindow.dropEvent(window, accepted)  # type: ignore[arg-type]
    assert accepted.drop_action == QtCore.Qt.DropAction.CopyAction
    window.OpenFile.assert_called_once_with(save_path)

    ignored = FakeDropEvent(None)
    PocketCameraWindow.dragMoveEvent(window, ignored)  # type: ignore[arg-type]
    PocketCameraWindow.dropEvent(window, ignored)  # type: ignore[arg-type]
    assert ignored.ignored is True

    no_match = FakeDropEvent([text_url])
    PocketCameraWindow.dragEnterEvent(window, no_match)  # type: ignore[arg-type]
    assert no_match.ignored is True

    moved = FakeDropEvent([save_url])
    PocketCameraWindow.dragMoveEvent(window, moved)  # type: ignore[arg-type]
    assert moved.accepted is True


def test_url_to_path_supports_local_and_percent_encoded_urls(tmp_path: Path) -> None:
    camera_path = tmp_path / "camera.sav"
    assert PocketCameraWindow._url_to_path(QtCore.QUrl.fromLocalFile(str(camera_path))) == camera_path
    spaced_path = tmp_path / "My Camera.sav"
    encoded_url = QtCore.QUrl.fromLocalFile(str(spaced_path)).toString()
    encoded = SimpleNamespace(
        toLocalFile=lambda: "",
        toString=lambda: encoded_url,
        path=lambda: encoded_url.removeprefix("file://"),
    )

    assert PocketCameraWindow._url_to_path(encoded) == spaced_path  # type: ignore[arg-type]
