# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import functools
import json
import shutil
import urllib.parse
from os import PathLike  # noqa: TC003
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

from PIL import Image, ImageDraw
from PIL.ImageQt import ImageQt
from PySide6 import QtCore, QtGui, QtWidgets

from .app import AppInfo
from .i18n import __, c__
from .Logging import logger
from .PocketCamera import CameraSource, FrameData, Palette, PocketCamera
from .UserInputDialog import DialogArgs, UserInputDialog

if TYPE_CHECKING:
    from .FlashGBX_GUI import FlashGBX_GUI


def _parse_palette_setting(value: object) -> Palette | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if (
        not isinstance(parsed, list)
        or len(parsed) != 12
        or any(
            isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 255 for channel in parsed
        )
    ):
        return None
    return tuple(parsed)


class PocketCameraWindow(QtWidgets.QDialog):
    PHOTO_SAVE_SIZE: ClassVar[int] = 0x100000
    PALETTES: ClassVar[tuple[Palette, ...]] = PocketCamera.PALETTES

    def __init__(
        self,
        app: FlashGBX_GUI,
        file: CameraSource | None = None,
        icon: QtGui.QIcon | None = None,
        config_path: str | PathLike[str] = ".",
        app_path: str | PathLike[str] = ".",
    ) -> None:
        super().__init__(app)
        self.CUR_PIC: ImageQt | None = None
        self.CUR_THUMBS: list[ImageQt] = []
        self.CUR_INDEX = 0
        self.CUR_BICUBIC = False
        self.CUR_FILE: CameraSource | None = file
        self.CUR_EXPORT_PATH = ""
        self.CUR_PC: PocketCamera | None = None
        self.CUR_PALETTE = 3
        self.APP_PATH = Path(app_path)
        self.CONFIG_PATH = Path(config_path)
        self.APP = app
        self.FORCE_EXIT = False
        self._palettes = list(self.PALETTES)

        self.setAcceptDrops(True)
        if icon is not None:
            self.setWindowIcon(QtGui.QIcon(icon))

        self.setWindowTitle(AppInfo.NAME + " – " + __("GB Camera Album Viewer"))
        self.setWindowFlags(
            (self.windowFlags() | QtCore.Qt.WindowType.MSWindowsFixedSizeDialogHint)
            & ~QtCore.Qt.WindowType.WindowContextHelpButtonHint,
        )

        self.main_layout = QtWidgets.QGridLayout()
        self.main_layout.setContentsMargins(-1, 8, -1, 8)
        self.main_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetFixedSize)
        self.layout_options1 = QtWidgets.QVBoxLayout()
        self.layout_options2 = QtWidgets.QVBoxLayout()
        self.layout_options3 = QtWidgets.QVBoxLayout()
        self.layout_photos = QtWidgets.QHBoxLayout()

        # Options
        self.grpOptions = QtWidgets.QGroupBox(__("Options"))
        grpOptionsLayout = QtWidgets.QVBoxLayout()
        grpOptionsLayout.setContentsMargins(-1, 3, -1, -1)
        self.rowOptions1 = QtWidgets.QHBoxLayout()
        self.lblColor = QtWidgets.QLabel(__("Color Palette:"))
        self.cmbColor = QtWidgets.QComboBox()
        self.cmbColor.setStyleSheet("combobox-popup: 0;")
        self.cmbColor.view().setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.cmbColor.addItems(
            [
                __("Grayscale"),
                __("Original Game Boy"),
                __("Super Game Boy"),
                __("Game Boy Color (Pocket Camera)"),
                __("Game Boy Color (Game Boy Camera Gold)"),
                __("Game Boy Color (Game Boy Camera)"),
            ],
        )
        self.cmbColor.currentIndexChanged.connect(self.SetColors)
        self.cmbColor.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.cmbColor.setCurrentIndex(-1)
        self.rowOptions1.addWidget(self.lblColor)
        self.rowOptions1.addWidget(self.cmbColor)
        self.rowOptions1.addStretch(1)

        self.lblZoom = QtWidgets.QLabel(__("Saved Picture Zoom:"))
        self.spnZoom = QtWidgets.QSpinBox()
        self.spnZoom.setRange(1, 10)
        self.spnZoom.setSuffix("×")
        self.rowOptions1.addWidget(self.lblZoom)
        self.rowOptions1.addWidget(self.spnZoom)
        self.rowOptions1.addStretch(1)

        self.chkFrame = QtWidgets.QCheckBox(c__("Check Box (& = Keyboard Shortcut)", "Save With &Frame"))
        self.rowOptions1.addWidget(self.chkFrame)

        grpOptionsLayout.addLayout(self.rowOptions1)
        self.grpOptions.setLayout(grpOptionsLayout)

        self.layout_options1.addWidget(self.grpOptions)

        rowActionsGeneral1 = QtWidgets.QHBoxLayout()
        self.btnOpenSRAM = QtWidgets.QPushButton(c__("Button (& = Keyboard Shortcut)", "&Open Save Data File"))
        self.btnOpenSRAM.setStyleSheet("padding: 5px 10px;")
        self.btnOpenSRAM.clicked.connect(self.btnOpenSRAM_Clicked)
        self.btnClose = QtWidgets.QPushButton(c__("Button (& = Keyboard Shortcut)", "&Close"))
        self.btnClose.setStyleSheet("padding: 5px 15px;")
        self.btnClose.clicked.connect(self.btnClose_Clicked)
        rowActionsGeneral1.addWidget(self.btnOpenSRAM)
        rowActionsGeneral1.addStretch()
        rowActionsGeneral1.addWidget(self.btnClose)
        self.layout_options3.addLayout(rowActionsGeneral1)

        # Photo Viewer
        self.grpPhotoView = QtWidgets.QGroupBox(__("Preview"))
        self.grpPhotoViewLayout = QtWidgets.QVBoxLayout()
        self.grpPhotoViewLayout.setContentsMargins(-1, 3, -1, -1)
        self.lblPhotoViewer = QtWidgets.QLabel(self)
        self.lblPhotoViewer.setMinimumSize(256, 223)
        self.lblPhotoViewer.setMaximumSize(256, 223)
        self.lblPhotoViewer.setStyleSheet(
            "border-top: 1px solid #adadad; border-left: 1px solid #adadad; border-bottom: 1px solid #ffffff; border-right: 1px solid #ffffff;",
        )
        self.lblPhotoViewer.mousePressEvent = self.lblPhotoViewer_Clicked
        self.grpPhotoViewLayout.addWidget(self.lblPhotoViewer)

        # Actions below Viewer
        rowActionsGeneral2 = QtWidgets.QHBoxLayout()
        self.btnSavePhoto = QtWidgets.QPushButton(c__("Button (& = Keyboard Shortcut)", "&Save This Picture"))
        self.btnSavePhoto.setStyleSheet("padding: 5px 10px;")
        self.btnSavePhoto.clicked.connect(self.btnSavePhoto_Clicked)
        rowActionsGeneral2.addWidget(self.btnSavePhoto)
        self.btnSaveAll = QtWidgets.QPushButton(c__("Button (& = Keyboard Shortcut)", "Save &All Pictures"))
        self.btnSaveAll.setStyleSheet("padding: 5px 10px;")
        self.btnSaveAll.clicked.connect(self.btnSaveAll_Clicked)
        rowActionsGeneral2.addWidget(self.btnSaveAll)
        self.grpPhotoViewLayout.addLayout(rowActionsGeneral2)

        self.grpPhotoView.setLayout(self.grpPhotoViewLayout)

        # Photo List
        self.grpPhotoThumbs = QtWidgets.QGroupBox(__("Photo Album"))
        self.grpPhotoThumbsLayout = QtWidgets.QVBoxLayout()
        self.grpPhotoThumbsLayout.setSpacing(2)
        self.grpPhotoThumbsLayout.setContentsMargins(-1, 3, -1, -1)
        self.lblPhoto = []
        rowsPhotos = []
        for row in range(5):
            rowsPhotos.append(QtWidgets.QHBoxLayout())
            rowsPhotos[row].setSpacing(2)
            for _ in range(6):
                self.lblPhoto.append(QtWidgets.QLabel(self))
                self.lblPhoto[len(self.lblPhoto) - 1].setMinimumSize(49, 43)
                self.lblPhoto[len(self.lblPhoto) - 1].setMaximumSize(49, 43)
                self.lblPhoto[len(self.lblPhoto) - 1].mousePressEvent = functools.partial(
                    self.lblPhoto_Clicked,
                    index=len(self.lblPhoto) - 1,
                )
                self.lblPhoto[len(self.lblPhoto) - 1].setCursor(
                    QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor),
                )
                self.lblPhoto[len(self.lblPhoto) - 1].setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.lblPhoto[len(self.lblPhoto) - 1].setStyleSheet(
                    "border-top: 1px solid #adadad; border-left: 1px solid #adadad; border-bottom: 1px solid #fefefe; border-right: 1px solid #fefefe;",
                )
                rowsPhotos[row].addWidget(self.lblPhoto[len(self.lblPhoto) - 1])
            self.grpPhotoThumbsLayout.addLayout(rowsPhotos[row])

        rowActionsGeneral3 = QtWidgets.QHBoxLayout()
        self.btnShowGameFace = QtWidgets.QPushButton(c__("Button (& = Keyboard Shortcut)", "&Game Face"))
        self.btnShowGameFace.setStyleSheet("padding: 5px 10px;")
        self.btnShowGameFace.clicked.connect(self.btnShowGameFace_Clicked)
        rowActionsGeneral3.addWidget(self.btnShowGameFace)
        self.btnShowLastSeen = QtWidgets.QPushButton(c__("Button (& = Keyboard Shortcut)", "&Last Seen Image"))
        self.btnShowLastSeen.setStyleSheet("padding: 5px 10px;")
        self.btnShowLastSeen.clicked.connect(self.btnShowLastSeen_Clicked)
        rowActionsGeneral3.addWidget(self.btnShowLastSeen)
        self.grpPhotoThumbsLayout.addStretch()
        self.grpPhotoThumbsLayout.addLayout(rowActionsGeneral3)

        self.grpPhotoThumbsLayout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.grpPhotoThumbs.setLayout(self.grpPhotoThumbsLayout)

        self.layout_photos.addWidget(self.grpPhotoThumbs)
        self.layout_photos.addWidget(self.grpPhotoView)

        self.main_layout.addLayout(self.layout_options1, 0, 0)
        self.main_layout.addLayout(self.layout_options2, 1, 0)
        self.main_layout.addLayout(self.layout_photos, 2, 0)
        self.main_layout.addLayout(self.layout_options3, 3, 0)
        self.setLayout(self.main_layout)

        zoom_setting = self.APP.SETTINGS.value("PocketCameraZoom", default="2")
        try:
            self.spnZoom.setValue(int(zoom_setting) if isinstance(zoom_setting, (int, str)) else 2)
        except (TypeError, ValueError):
            self.spnZoom.setValue(2)
        self.chkFrame.setChecked(self.APP.SETTINGS.value("PocketCameraFrame", default="disabled") == "enabled")

        palette = _parse_palette_setting(self.APP.SETTINGS.value("PocketCameraPalette"))
        if palette is None:
            palette = PocketCamera.DEFAULT_PALETTE
        if palette not in self._palettes:
            self._palettes.append(palette)
            self.cmbColor.addItem(__("Custom"))
        self.CUR_PALETTE = self._palettes.index(palette)
        self.cmbColor.setCurrentIndex(self.CUR_PALETTE)

        last_export_path = self.APP.SETTINGS.value("LastDirPocketCamera")
        if isinstance(last_export_path, str):
            self.CUR_EXPORT_PATH = last_export_path

        if self.CUR_FILE is not None and self.OpenFile(self.CUR_FILE) is False:
            self.FORCE_EXIT = True
            return

        if self.CUR_EXPORT_PATH == "":
            self.CUR_EXPORT_PATH = QtCore.QStandardPaths.writableLocation(
                QtCore.QStandardPaths.StandardLocation.DocumentsLocation,
            )

        self.btnSaveAll.setDefault(True)
        self.btnSaveAll.setAutoDefault(True)
        self.btnSaveAll.setFocus()

    def run(self) -> None:
        if self.FORCE_EXIT:
            self.reject()
            return
        self.main_layout.update()
        self.main_layout.activate()
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            frame_geometry = self.frameGeometry()
            frame_geometry.moveCenter(screen.availableGeometry().center())
            self.move(frame_geometry.topLeft())
        self.show()

    def SetColors(self, _: int | None = None) -> None:
        if self.CUR_PC is None:
            return
        palette_index = self.cmbColor.currentIndex()
        if not 0 <= palette_index < len(self._palettes):
            palette_index = 3
            self.cmbColor.setCurrentIndex(palette_index)
        self.CUR_PALETTE = palette_index
        self.CUR_PC.SetPalette(self._palettes[palette_index])
        self.BuildPhotoList()
        self.UpdateViewer(self.CUR_INDEX)

    def OpenFile(self, file: CameraSource) -> bool:
        try:
            source: CameraSource = file
            source_path = None if isinstance(source, (bytes, bytearray, memoryview)) else Path(source)
            source_size = (
                len(source) if isinstance(source, (bytes, bytearray, memoryview)) else Path(source).stat().st_size
            )
            if source_size == self.PHOTO_SAVE_SIZE:
                roll = self._select_photo_roll(source)
                if roll is None:
                    self.CUR_PC = None
                    return False
                source = roll

            camera = PocketCamera()
            if not camera.LoadFile(source):
                self.CUR_PC = None
                QtWidgets.QMessageBox.critical(
                    self,
                    AppInfo.NAME,
                    __("The save data file couldn’t be loaded."),
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
                return False
            self.CUR_PC = camera
            self.CUR_FILE = source
            if self.CUR_EXPORT_PATH == "" and source_path is not None:
                self.CUR_EXPORT_PATH = str(source_path.parent)
            self.CUR_INDEX = 0
            self.SetColors()
        except Exception:
            logger.exception("Failed to load Game Boy Camera save data")
            self.CUR_PC = None
            QtWidgets.QMessageBox.critical(
                self,
                AppInfo.NAME,
                __("An error occured while trying to load the save data file."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return False
        return True

    def _select_photo_roll(self, source: CameraSource) -> bytes | None:
        dialog_args: DialogArgs = {
            "title": "Photo!",
            "intro": __(
                "A “Photo!” save file was detected. Please select the roll of pictures that you would like to load.",
            ),
            "params": [
                [
                    "index",
                    "cmb",
                    __("Film roll:"),
                    [__("Current Save Data")]
                    + [
                        __(
                            "“{flash_directory}” Slot {number}",
                            flash_directory="Flash Directory",
                            number=f"{slot:d}",
                        )
                        for slot in range(1, 8)
                    ],
                    0,
                ],
            ],
        }
        dialog = UserInputDialog(self, icon=self.windowIcon(), args=dialog_args)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None

        combo_box = dialog.GetResult()["index"]
        index = cast("QtWidgets.QComboBox", combo_box).currentIndex()
        data = bytes(source) if isinstance(source, (bytes, bytearray, memoryview)) else Path(source).read_bytes()
        offset = PocketCamera.SAVE_SIZE * index
        return data[offset : offset + PocketCamera.SAVE_SIZE]

    def lblPhoto_Clicked(self, event: QtGui.QMouseEvent, index: int) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.CUR_INDEX = index
            self.UpdateViewer(self.CUR_INDEX)

    def lblPhotoViewer_Clicked(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.CUR_BICUBIC = not self.CUR_BICUBIC
            self.UpdateViewer(self.CUR_INDEX)

    def btnOpenSRAM_Clicked(self, _: bool = False) -> None:
        last_dir = self.APP.SETTINGS.value("LastDirSaveDataDMG")
        if not isinstance(last_dir, str):
            last_dir = QtCore.QStandardPaths.writableLocation(
                QtCore.QStandardPaths.StandardLocation.DocumentsLocation,
            )
        path = QtWidgets.QFileDialog.getOpenFileName(
            self,
            __("Open GB Camera Save Data File"),
            last_dir,
            __("Save Data File") + " (*.sav);;" + __("All Files") + " (*.*)",
        )[0]
        if path == "":
            return
        if self.OpenFile(path) is True:
            self.APP.SETTINGS.setValue("LastDirSaveDataDMG", str(Path(path).parent))

    def btnShowGameFace_Clicked(self, _: bool = False) -> None:
        self.CUR_INDEX = PocketCamera.GAME_FACE_INDEX
        self.UpdateViewer(self.CUR_INDEX)

    def btnShowLastSeen_Clicked(self, _: bool = False) -> None:
        self.CUR_INDEX = PocketCamera.LAST_SEEN_INDEX
        self.UpdateViewer(self.CUR_INDEX)

    def btnSaveAll_Clicked(self, _: bool = False) -> None:
        if self.CUR_PC is None:
            return
        path = str(Path(self.CUR_EXPORT_PATH) / "IMG_PC.png")
        path = QtWidgets.QFileDialog.getSaveFileName(
            self,
            __("Export all pictures"),
            path,
            __("PNG files")
            + " (*.png);;"
            + __("BMP files")
            + " (*.bmp);;"
            + __("GIF files")
            + " (*.gif);;"
            + __("JPEG files")
            + " (*.jpg);;"
            + __("All files")
            + " (*.*)",
        )[0]
        if path == "":
            return
        self.CUR_EXPORT_PATH = str(Path(path).parent)
        output_path = Path(path)

        output_files = [self._batch_export_path(output_path, index) for index in range(PocketCamera.IMAGE_COUNT)]
        if any(file.exists() for file in output_files):
            answer = QtWidgets.QMessageBox.warning(
                self,
                AppInfo.NAME,
                __(
                    "There are already pictures that use the same file names. If you continue, these files will be overwritten.",
                ),
                QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Ok:
                return

        for index, file in enumerate(output_files):
            self.SavePicture(index, path=file)

    @staticmethod
    def _batch_export_path(output_path: Path, index: int) -> Path:
        return output_path.with_name(f"{output_path.stem}{index + 1:02d}{output_path.suffix}")

    def btnSavePhoto_Clicked(self, _: bool = False) -> None:
        if self.CUR_PC is None:
            return
        self.SavePicture(self.CUR_INDEX)

    def btnClose_Clicked(self, _: bool = False) -> None:
        self.FORCE_EXIT = True
        self.reject()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        palette_index = self.cmbColor.currentIndex()
        if not 0 <= palette_index < len(self._palettes):
            palette_index = 3
        self.APP.SETTINGS.setValue(
            "PocketCameraPalette",
            json.dumps(self._palettes[palette_index]),
        )
        self.APP.SETTINGS.setValue("PocketCameraZoom", str(self.spnZoom.value()))
        self.APP.SETTINGS.setValue(
            "PocketCameraFrame",
            str(self.chkFrame.isChecked()).lower().replace("true", "enabled").replace("false", "disabled"),
        )
        self.APP.SETTINGS.setValue("LastDirPocketCamera", self.CUR_EXPORT_PATH)
        self.APP.activateWindow()
        super().hideEvent(event)

    def BuildPhotoList(self) -> None:
        cam = self.CUR_PC
        if cam is None:
            return
        self.CUR_THUMBS = []
        for index in range(PocketCamera.PHOTO_COUNT):
            picture = cam.GetPicture(index).convert("RGBA")
            self.lblPhoto[index].setToolTip("")
            if not cam.IsEmpty(index) and cam.IsDeleted(index):
                draw_bg = Image.new("RGBA", picture.size)
                draw = ImageDraw.Draw(draw_bg)
                draw.line([0, 0, picture.width - 1, picture.height - 1], fill=(255, 0, 0, 192), width=8)
                draw.line([0, picture.height - 1, picture.width - 1, 0], fill=(255, 0, 0, 192), width=8)
                picture.paste(draw_bg, mask=draw_bg)
                self.lblPhoto[index].setToolTip(
                    __("This picture was marked as “deleted” and may be overwritten when you take new pictures."),
                )
            thumbnail = ImageQt(picture.resize((47, 41), Image.Resampling.HAMMING))
            self.CUR_THUMBS.append(thumbnail)
            self.lblPhoto[index].setPixmap(QtGui.QPixmap.fromImage(thumbnail))

    def UpdateViewer(self, index: int, scale_factor: float = 4) -> None:
        resampler = Image.Resampling.NEAREST
        if self.CUR_BICUBIC or index == PocketCamera.LAST_SEEN_INDEX:
            resampler = Image.Resampling.BICUBIC
        if resampler == Image.Resampling.BICUBIC:
            scale_factor = 0.5
        cam = self.CUR_PC
        if cam is None:
            return

        for label in self.lblPhoto:
            label.setStyleSheet(
                "border-top: 1px solid #adadad; border-left: 1px solid #adadad; border-bottom: 1px solid #ffffff; border-right: 1px solid #ffffff;",
            )

        self.CUR_PIC = ImageQt(
            cam.GetPicture(index).resize((int(256 * scale_factor), int(224 * scale_factor)), resampler),
        )
        if index < PocketCamera.PHOTO_COUNT:
            self.lblPhoto[index].setStyleSheet("border: 3px solid green; padding: 1px;")

        qpixmap = QtGui.QPixmap.fromImage(self.CUR_PIC)
        qpixmap.setDevicePixelRatio(scale_factor)
        self.lblPhotoViewer.setPixmap(qpixmap)

    def SavePicture(self, index: int, path: str | PathLike[str] = "") -> None:
        if self.CUR_PC is None:
            return
        output_path = str(path)
        if output_path == "":
            path = str(Path(self.CUR_EXPORT_PATH) / f"IMG_PC{index + 1:02d}.png")
            output_path = QtWidgets.QFileDialog.getSaveFileName(
                self,
                __("Save Photo"),
                path,
                __("PNG files")
                + " (*.png);;"
                + __("BMP files")
                + " (*.bmp);;"
                + __("GIF files")
                + " (*.gif);;"
                + __("JPEG files")
                + " (*.jpg);;"
                + __("All files")
                + " (*.*)",
            )[0]
            if output_path != "":
                self.CUR_EXPORT_PATH = str(Path(output_path).parent)
        if output_path == "":
            return

        cam = self.CUR_PC

        frame: FrameData = False
        if self.chkFrame.isChecked():
            own_frame = Path(self.CONFIG_PATH) / "pc_frame.png"
            if not own_frame.exists():
                shutil.copy(
                    Path(self.APP_PATH) / "res" / "pc_frame.png",
                    own_frame,
                )
            frame = own_frame.read_bytes()

        if index == PocketCamera.LAST_SEEN_INDEX:
            frame = False  # last seen image
        cam.ExportPicture(index=index, path=output_path, scale=self.spnZoom.value(), frame=frame)

    def dragEnterEvent(self, e: QtGui.QDragEnterEvent) -> None:
        if self._dragEventHover(e):
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e: QtGui.QDragMoveEvent) -> None:
        if self._dragEventHover(e):
            e.accept()
        else:
            e.ignore()

    def _dragEventHover(self, e: QtGui.QDragEnterEvent | QtGui.QDragMoveEvent) -> bool:
        if e.mimeData().hasUrls():
            for url in e.mimeData().urls():
                if self._url_to_path(url).suffix.lower() == ".sav":
                    return True
        return False

    def dropEvent(self, e: QtGui.QDropEvent) -> None:
        if e.mimeData().hasUrls():
            e.setDropAction(QtCore.Qt.DropAction.CopyAction)
            e.accept()
            for url in e.mimeData().urls():
                path = self._url_to_path(url)
                if path.suffix.lower() == ".sav":
                    self.OpenFile(path)
        else:
            e.ignore()

    @staticmethod
    def _url_to_path(url: QtCore.QUrl) -> Path:
        filename = url.toLocalFile()
        if filename == "":
            filename = urllib.parse.unquote(str(QtCore.QUrl(url.toString()).toLocalFile() or url.path()))
        return Path(filename)
