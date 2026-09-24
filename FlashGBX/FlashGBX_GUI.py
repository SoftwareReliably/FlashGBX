# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import calendar
import datetime
import html
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NamedTuple, TypedDict, cast

import requests
from packaging import version
from PySide6 import QtCore, QtGui, QtWidgets
from serial import SerialException

from . import i18n
from .app import (
    GBXCART_RW_BAUD_RATES,
    GBXCART_RW_DEFAULT_BAUD_RATE,
    HW_DEVICES,
    AppContext,
    AppInfo,
    generate_filename,
)
from .CartridgeTypes import AgbSaveTypes, DmgSaveTypes, RomSizes
from .Flashcart import (
    FlashcartMap,
    has_3v_compatible_profile,
)
from .Formatter import Formatter
from .i18n import (
    CONFIGURED_LANGUAGE,
    LANGUAGES,
    __,
    ___,
    c__,
    format_decimal,
    init_language,
    loadQtTranslation,
)
from .IniSettings import IniSettings
from .InteractiveConsoleWindow import InteractiveConsoleWindow
from .Logging import Logger, dprint, logger
from .Mapper import (
    ConvertMapperToMapperType,
    ConvertMapperTypeToMapper,
    DMG_Mapper,
    compare_mbc,
    get_mbc_name,
    save_size_includes_rtc,
)
from .PocketCameraWindow import PocketCameraWindow
from .Progress import Progress
from .pyside import (
    IsDarkMode,
    QtWinExtras,
    bitmap2pixmap,
)
from .RomFileAGB import RomFileAGB
from .RomFileDMG import RomFileDMG, from_isx
from .UserInputDialog import DialogArgs, UserInputDialog

if TYPE_CHECKING:
    import argparse
    from collections.abc import Mapping, Sequence

    from PIL.Image import Image as PILImage  # pyright: ignore[reportMissingImports]

    from .LK_Device import LK_Device

SAVE_EXTS = (".sav", ".srm", ".fla", ".eep")
ROM_EXTS_DMG = (".gb", ".sgb", ".gbc", ".bin", ".isx")
ROM_EXTS_AGB = (".gba", ".srl", ".bin")
ROM_EXTS_DMG_READ = (".gb", ".sgb", ".gbc")
DROP_ROM_EXTS_ALL = ROM_EXTS_DMG + ROM_EXTS_AGB
PlatformMode = Literal["DMG", "AGB"]
ConfigMessage = list[int | str]
GENERIC_FLASH_PROFILES = (
    ("[     0/90]", "Generic Flash Cartridge (0/90)"),
    ("[   AAA/AA]", "Generic Flash Cartridge (AAA/AA)"),
    ("[   AAA/A9]", "Generic Flash Cartridge (AAA/A9)"),
    ("[WR   / AAA/AA]", "Generic Flash Cartridge (WR/AAA/AA)"),
    ("[WR   / AAA/A9]", "Generic Flash Cartridge (WR/AAA/A9)"),
    ("[WR   / 555/AA]", "Generic Flash Cartridge (WR/555/AA)"),
    ("[WR   / 555/A9]", "Generic Flash Cartridge (WR/555/A9)"),
    ("[AUDIO/ AAA/AA]", "Generic Flash Cartridge (AUDIO/AAA/AA)"),
    ("[AUDIO/ 555/AA]", "Generic Flash Cartridge (AUDIO/555/AA)"),
)


class GuiArgs(TypedDict):
    app_path: str
    config_path: str
    flashcarts: FlashcartMap
    config_ret: list[ConfigMessage]
    argparsed: argparse.Namespace


class BatterylessSramInfo(TypedDict):
    bl_offset: int
    bl_size: int


class _BatterylessDialogSelection(NamedTuple):
    locations: list[int]
    lengths: list[int]
    intro: str
    location_index: int
    length_index: int
    layout_index: int


class _SaveWritePreparation(NamedTuple):
    mode: PlatformMode
    path: str
    mbc: int
    save_type: int
    cart_type: int
    file_size: int
    buffer: bytearray | None


class _SaveWritePathOptions(NamedTuple):
    mode: PlatformMode
    dpath: str
    erase: bool
    test: bool
    skip_warning: bool


class _DetectedCartProfileContext(NamedTuple):
    header: Mapping[str, Any]
    cart_type: int | None
    cart_types: Sequence[int]
    cart_type_id: int
    supported_cart_types: tuple[list[str], list[Any]]
    compatible_profiles: str
    selected_name: str
    detected_size: int
    flash_id: str
    cfi_data: str
    limit_voltage: bool


class _DetectedCartDetails(NamedTuple):
    cart_type_message: str
    cart_type_details: str
    flash_size_message: str
    flash_id_message: str
    cfi_message: str
    flash_mapper_message: str
    generic_profile: str | None
    found_supported: bool
    is_generic: bool


class _SaveStressTestResult(NamedTuple):
    tests_completed: int
    pattern_names: list[str]
    elapsed_message: str
    first_save: bytearray | None
    second_save: bytearray
    written_data: bytearray
    readback_data: bytearray


def _format_batteryless_sram_details(save_size: int, info: BatterylessSramInfo) -> str:
    """Build the save-size and ROM-location text shown after auto-detection."""
    save_size_text = __("unknown size") if save_size == 0 else Formatter.file_size(save_size, as_int=True)
    start = info["bl_offset"]
    size = info["bl_size"]
    end = start + size - 1
    location_label = __("{batteryless_sram} Location:", batteryless_sram="Batteryless SRAM")
    detected_size = Formatter.file_size(size, as_int=True)
    return f" ({save_size_text})<br><b>{location_label}</b> 0x{start:X}-0x{end:X} ({detected_size})"


def _parse_hex_address(text: str) -> int:
    """Parse a hexadecimal address with or without a ``0x`` prefix."""
    value = text.strip()
    return int(value, 0 if value.lower().startswith("0x") else 16)


def _is_supported_drop(extension: str, mode: PlatformMode | None) -> bool:
    """Return whether a dropped file can be handled in the current mode."""
    return (
        extension in SAVE_EXTS
        or (mode == "DMG" and extension in ROM_EXTS_DMG)
        or (mode == "AGB" and extension in ROM_EXTS_AGB)
    )


def _create_message_box(  # noqa: PLR0913 - mirrors the Qt message-box arguments
    *,
    parent: QtWidgets.QWidget | None = None,
    icon: QtWidgets.QMessageBox.Icon = QtWidgets.QMessageBox.Icon.NoIcon,
    windowTitle: str = "",
    text: str = "",
    standardButtons: QtWidgets.QMessageBox.StandardButton = (QtWidgets.QMessageBox.StandardButton.NoButton),
    defaultButton: QtWidgets.QMessageBox.StandardButton | None = None,
) -> QtWidgets.QMessageBox:
    """Construct a message box using the supported PySide6 overload."""
    msgbox = QtWidgets.QMessageBox(icon, windowTitle, text, standardButtons, parent)
    # PySide 6.11 may not apply the positional title on macOS.
    msgbox.setWindowTitle(windowTitle)
    if defaultButton is not None:
        msgbox.setDefaultButton(defaultButton)
    return msgbox


def _create_check_box(text: str, *, checked: bool = False) -> QtWidgets.QCheckBox:
    """Construct a check box and apply its initial checked state."""
    check_box = QtWidgets.QCheckBox(text)
    check_box.setChecked(checked)
    return check_box


def _set_bitmap(label: QtWidgets.QLabel, bitmap: PILImage) -> bool:
    """Convert and display a bitmap when conversion succeeds."""
    pixmap = bitmap2pixmap(bitmap)
    if pixmap is False:
        return False
    label.setPixmap(pixmap)
    return True


def _format_device_initialization_error(error: Exception) -> str:
    """Build a useful connection error without hiding the driver details."""
    message = __("The device could not be initialized. Reconnect it and try again.")
    details = str(error).strip()
    return f"{message}\n\n{details}" if details else message


def _ignore_progress(_event: object) -> None:
    """Discard progress events for the synchronous GUI stress test."""


def _system_executable(name: str) -> str:
    """Resolve a trusted system launcher to an absolute executable path."""
    executable = shutil.which(name)
    if executable is None:
        message = f"System executable not found: {name}"
        raise FileNotFoundError(message)
    return executable


def _generic_flash_profile(flash_id: str) -> str | None:
    """Return the first generic profile suggested by a flash-ID trace."""
    return next((profile for marker, profile in GENERIC_FLASH_PROFILES if marker in flash_id), None)


class FlashGBX_GUI(QtWidgets.QMainWindow):
    CONN: LK_Device | None
    SETTINGS: IniSettings
    DEVICES: dict[str, LK_Device]
    FLASHCARTS: FlashcartMap
    APP_PATH: ClassVar[str] = ""
    CONFIG_PATH: ClassVar[str] = ""
    TBPROG: Any  # Taskbar progress handle (platform-dependent)
    PROGRESS: Progress
    CAMWIN: PocketCameraWindow | None
    FWUPWIN: Any
    INTWIN: InteractiveConsoleWindow | None
    STATUS: dict[str, Any]
    TEXT_COLOR: tuple[int, int, int, int]
    MSGBOX_QUEUE: queue.Queue[QtWidgets.QMessageBox]
    MSGBOX_DISPLAYING: bool
    DEFAULT_STYLESHEET: str

    def _InitializeState(self, args: GuiArgs) -> None:
        self.CONN = None
        self.DEVICES = {}
        self.TBPROG = None
        self.CAMWIN = None
        self.FWUPWIN = None
        self.INTWIN = None
        self.STATUS = {}
        self.MSGBOX_QUEUE = queue.Queue()
        self.MSGBOX_DISPLAYING = False
        self.DEFAULT_STYLESHEET = ""
        self.SETTINGS = IniSettings(path=Path(args["config_path"]) / "settings.ini")
        self.FLASHCARTS = args["flashcarts"]
        self.PROGRESS = Progress(self.UpdateProgress, self.WaitProgress)

    def _ApplyConfigMenuSettings(self) -> None:
        for action_index in (0, 1, 2, 3, 4, 6, 7, 8, 9, 10):
            self.mnuConfig.actions()[action_index].setCheckable(True)
        self.mnuConfig.actions()[0].setChecked(self.SETTINGS.value("UpdateCheck") == "enabled")
        self.mnuConfig.actions()[1].setChecked(
            self.SETTINGS.value("SaveFileNameAddDateTime", default="disabled") == "enabled",
        )
        self.mnuConfig.actions()[2].setChecked(self.SETTINGS.value("PreferChipErase", default="disabled") == "enabled")
        self.mnuConfig.actions()[3].setChecked(self.SETTINGS.value("VerifyData", default="enabled") == "enabled")
        self.mnuConfig.actions()[4].setChecked(
            self.SETTINGS.value("AutoDetectLimitVoltage", default="disabled") == "enabled",
        )
        self._UpdateGBxCartRWBaudRateActions(self._GetGBxCartRWBaudRate())
        self.mnuConfig.actions()[6].setChecked(
            self.SETTINGS.value("GenerateDumpReports", default="disabled") == "enabled",
        )
        self.mnuConfig.actions()[7].setChecked(
            self.SETTINGS.value("UseNoIntroFilenames", default="enabled") == "enabled",
        )
        self.mnuConfig.actions()[8].setChecked(self.SETTINGS.value("AutoPowerOff", default="350") != "0")
        self.mnuConfig.actions()[9].setChecked(self.SETTINGS.value("CompareSectors", default="enabled") == "enabled")
        self.mnuConfig.actions()[10].setChecked(self.SETTINGS.value("ForceWrPullup", default="disabled") == "enabled")

    def _CreateActionsGroup(self) -> None:
        self.grpActions = QtWidgets.QGroupBox()
        self.grpActionsLayout = QtWidgets.QVBoxLayout()
        self.grpActionsLayout.setContentsMargins(-1, 3, -1, -1)

        rowActionsMode = QtWidgets.QHBoxLayout()
        self.lblMode = QtWidgets.QLabel()
        rowActionsMode.addWidget(self.lblMode)
        self.optDMG = QtWidgets.QRadioButton()
        self.optDMG.clicked.connect(self.SetMode)
        self.optAGB = QtWidgets.QRadioButton()
        self.optAGB.clicked.connect(self.SetMode)
        rowActionsMode.addWidget(self.optDMG)
        rowActionsMode.addWidget(self.optAGB)

        rowActionsGeneral1 = QtWidgets.QHBoxLayout()
        self.btnHeaderRefresh = QtWidgets.QPushButton()
        self.btnHeaderRefresh.setMinimumHeight(25)
        self.btnHeaderRefresh.setMinimumWidth(140)
        self.btnHeaderRefresh.clicked.connect(lambda _checked=False: self.ReadCartridge())
        rowActionsGeneral1.addWidget(self.btnHeaderRefresh)

        self.btnDetectCartridge = QtWidgets.QPushButton()
        self.btnDetectCartridge.setMinimumHeight(25)
        self.btnDetectCartridge.setMinimumWidth(140)
        self.btnDetectCartridge.clicked.connect(lambda _checked=False: self.DetectCartridge())
        rowActionsGeneral1.addWidget(self.btnDetectCartridge)

        rowActionsGeneral2 = QtWidgets.QHBoxLayout()
        self.btnBackupROM = QtWidgets.QPushButton()
        self.btnBackupROM.setMinimumHeight(25)
        self.btnBackupROM.setMinimumWidth(140)
        self.btnBackupROM.clicked.connect(self.BackupROM)
        rowActionsGeneral2.addWidget(self.btnBackupROM)
        self.btnBackupRAM = QtWidgets.QPushButton()
        self.btnBackupRAM.setMinimumHeight(25)
        self.btnBackupRAM.setMinimumWidth(140)
        self.btnBackupRAM.clicked.connect(lambda _checked=False: self.BackupRAM())
        rowActionsGeneral2.addWidget(self.btnBackupRAM)

        self.cmbDMGCartridgeTypeResult.currentIndexChanged.connect(self.CartridgeTypeChanged)
        self.cmbDMGHeaderMapperResult.currentIndexChanged.connect(self.DMGMapperTypeChanged)

        rowActionsGeneral3 = QtWidgets.QHBoxLayout()
        self.btnFlashROM = QtWidgets.QPushButton()
        self.btnFlashROM.setMinimumHeight(25)
        self.btnFlashROM.setMinimumWidth(140)
        self.btnFlashROM.clicked.connect(lambda _checked=False: self.FlashROM())
        rowActionsGeneral3.addWidget(self.btnFlashROM)
        self.btnRestoreRAM = QtWidgets.QPushButton()
        self.mnuRestoreRAM = QtWidgets.QMenu()
        self.mnuRestoreRAM.addAction("", lambda _checked=False: self.WriteRAM())
        self.mnuRestoreRAM.addAction("", lambda: self.WriteRAM(erase=True))
        self.mnuRestoreRAM.addSeparator()
        self.mnuRestoreRAM.addAction("", lambda: self.WriteRAM(test=True))
        self.btnRestoreRAM.setMenu(self.mnuRestoreRAM)
        self.btnRestoreRAM.setMinimumHeight(25)
        self.btnRestoreRAM.setMinimumWidth(140)
        rowActionsGeneral3.addWidget(self.btnRestoreRAM)

        self.grpActionsLayout.setSpacing(4)
        self.grpActionsLayout.addLayout(rowActionsMode)
        self.grpActionsLayout.addLayout(rowActionsGeneral1)
        self.grpActionsLayout.addLayout(rowActionsGeneral2)
        self.grpActionsLayout.addLayout(rowActionsGeneral3)
        self.grpActions.setLayout(self.grpActionsLayout)
        self.layout_right.addWidget(self.grpActions)

    def _CreateTransferStatusGroup(self) -> None:
        self.grpStatus = QtWidgets.QGroupBox()
        grpStatusLayout = QtWidgets.QVBoxLayout()
        grpStatusLayout.setContentsMargins(-1, 3, -1, -1)
        if platform.system() == "Linux":
            grpStatusLayout.setSpacing(4)

        rowStatus1a = QtWidgets.QHBoxLayout()
        self.lblStatus1a = QtWidgets.QLabel()
        rowStatus1a.addWidget(self.lblStatus1a)
        self.lblStatus1aResult = QtWidgets.QLabel("-")
        rowStatus1a.addWidget(self.lblStatus1aResult)
        grpStatusLayout.addLayout(rowStatus1a)
        rowStatus2a = QtWidgets.QHBoxLayout()
        self.lblStatus2a = QtWidgets.QLabel()
        rowStatus2a.addWidget(self.lblStatus2a)
        self.lblStatus2aResult = QtWidgets.QLabel("-")
        rowStatus2a.addWidget(self.lblStatus2aResult)
        grpStatusLayout.addLayout(rowStatus2a)
        rowStatus3a = QtWidgets.QHBoxLayout()
        self.lblStatus3a = QtWidgets.QLabel()
        rowStatus3a.addWidget(self.lblStatus3a)
        self.lblStatus3aResult = QtWidgets.QLabel("-")
        rowStatus3a.addWidget(self.lblStatus3aResult)
        grpStatusLayout.addLayout(rowStatus3a)
        rowStatus4a = QtWidgets.QHBoxLayout()
        self.lblStatus4a = QtWidgets.QLabel()
        self.lblStatus4a.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        rowStatus4a.addWidget(self.lblStatus4a)
        self.lblStatus4aResult = QtWidgets.QLabel("")
        self.lblStatus4aResult.setVisible(False)
        rowStatus4a.addWidget(self.lblStatus4aResult)
        grpStatusLayout.addLayout(rowStatus4a)

        rowStatus2 = QtWidgets.QHBoxLayout()
        self.prgStatus = QtWidgets.QProgressBar()
        self.SetProgressBars(min=0, max=1, value=0)
        rowStatus2.addWidget(self.prgStatus)
        self.btnCancel = QtWidgets.QPushButton()
        self.btnCancel.setEnabled(False)
        self.btnCancel.clicked.connect(self.AbortOperation)
        rowStatus2.addWidget(self.btnCancel)

        grpStatusLayout.addLayout(rowStatus2)
        self.grpStatus.setLayout(grpStatusLayout)
        self.layout_right.addWidget(self.grpStatus)

    def _ShowStartupMessages(self, config_ret: list[ConfigMessage]) -> None:
        for config_message in config_ret:
            status = config_message[0]
            message = str(config_message[1])
            if status == 0:
                print(message)
            elif status == 1:
                QtWidgets.QMessageBox.information(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    message,
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
            elif status == 2:
                QtWidgets.QMessageBox.warning(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    message,
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
            elif status == 3:
                QtWidgets.QMessageBox.critical(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    message,
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )

        if platform.system() == "Windows":
            # Warm up fallback font rendering to prevent lag on first use later
            text_layout = QtGui.QTextLayout("⚠️")
            text_layout.beginLayout()
            text_layout.createLine()
            text_layout.endLayout()

    def _DisableDisconnectedControls(self) -> None:
        self.optAGB.setEnabled(False)
        self.optDMG.setEnabled(False)
        self.btnHeaderRefresh.setEnabled(False)
        self.btnDetectCartridge.setEnabled(False)
        self.btnBackupROM.setEnabled(False)
        self.btnFlashROM.setEnabled(False)
        self.btnBackupRAM.setEnabled(False)
        self.btnRestoreRAM.setEnabled(False)
        self.btnConnect.setEnabled(False)
        self.grpDMGCartridgeInfo.setEnabled(False)
        self.grpAGBCartridgeInfo.setEnabled(False)

    def _StartStatusTimers(self) -> None:
        self.MSGBOX_TIMER = QtCore.QTimer()
        self.MSGBOX_TIMER.timeout.connect(self.MsgBoxCheck)
        self.MSGBOX_TIMER.start(200)
        self.LOG_ERROR_TIMER = QtCore.QTimer()
        self.LOG_ERROR_TIMER.timeout.connect(self.LogErrorCheck)
        self.LOG_ERROR_TIMER.start(200)

    def _CreateDeviceStatusLayout(self) -> None:
        self.layout_devices = QtWidgets.QHBoxLayout()
        self.lblDevice = QtWidgets.QLabel()
        # PySide supports assigning an event callback on the instance.
        self.lblDevice.mousePressEvent = lambda event: self.WriteDebugLog(event, open_log=True)  # ty: ignore[invalid-assignment]
        self.lblDevice.setToolTip("")
        self.lblDevice.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.cmbDevice = QtWidgets.QComboBox()
        self.cmbDevice.setStyleSheet("QComboBox { border: 0; margin: 0; padding: 0; max-width: 0px; }")
        self.lblWarning = QtWidgets.QLabel("⚠️")
        # PySide supports assigning an event callback on the instance.
        self.lblWarning.mousePressEvent = lambda event: self.WriteDebugLog(event, open_log=True)  # ty: ignore[invalid-assignment]
        self.lblWarning.setToolTip("")
        self.lblWarning.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.lblWarning.setVisible(False)

        self.layout_devices.addWidget(self.lblDevice)
        self.layout_devices.addWidget(self.cmbDevice)
        self.layout_devices.addWidget(self.lblWarning)
        self.layout_devices.addStretch()

    def _ConfigureColorScheme(self) -> None:
        try:
            if self.SETTINGS.value("AllowDarkMode", default="enabled") == "disabled":
                QtGui.QGuiApplication.styleHints().setColorScheme(QtCore.Qt.ColorScheme.Light)
            if platform.system() == "Windows":
                qt_app.setStyle("fusion" if IsDarkMode() else "windowsvista")
        except Exception:
            logger.exception("Failed to configure the Qt color scheme")

    def _CreateLanguageMenu(self) -> None:
        self.mnuLanguage = QtWidgets.QMenu()
        self.languageActionGroup = QtGui.QActionGroup(self.mnuLanguage)
        self.languageActionGroup.setExclusive(True)
        for code, names in sorted(LANGUAGES.items()):
            native_name = names[1] if isinstance(names, tuple) else names
            action = self.mnuLanguage.addAction(native_name + (f" ({code})"))
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, lang=code: self.ChangeLanguage(lang))
            self.languageActionGroup.addAction(action)
            if code == CONFIGURED_LANGUAGE:
                action.setChecked(True)

    def _ConfigurePlatformLayoutSpacing(self) -> None:
        if platform.system() != "Linux":
            return
        self.main_layout.setHorizontalSpacing(8)
        self.main_layout.setVerticalSpacing(5)
        self.layout_left.setSpacing(5)
        self.layout_right.setSpacing(5)
        self.layout_devices.setSpacing(6)

    def _CreateDeviceAndToolsMenus(self) -> None:
        self._CreateDeviceStatusLayout()
        self.mnuTools = QtWidgets.QMenu()
        self.mnuTools.addAction("", self.ShowPocketCameraWindow)
        self.mnuTools.addAction("", self.ShowInteractiveConsoleWindow)
        self.mnuTools.addSeparator()
        self.mnuTools.addAction("", self.ShowFirmwareUpdateWindow)
        self.mnuTools.actions()[1].setEnabled(False)

    def _CreateConfigMenu(self) -> None:
        self.mnuConfig = QtWidgets.QMenu()
        self.mnuConfig.addAction("", lambda: [self.EnableUpdateCheck()])
        self.mnuConfig.addAction(
            "",
            lambda: self.SETTINGS.setValue(
                "SaveFileNameAddDateTime",
                str(self.mnuConfig.actions()[1].isChecked())
                .lower()
                .replace("true", "enabled")
                .replace("false", "disabled"),
            ),
        )
        self.mnuConfig.addAction(
            "",
            lambda: self.SETTINGS.setValue(
                "PreferChipErase",
                str(self.mnuConfig.actions()[2].isChecked())
                .lower()
                .replace("true", "enabled")
                .replace("false", "disabled"),
            ),
        )
        self.mnuConfig.addAction(
            "",
            lambda: self.SETTINGS.setValue(
                "VerifyData",
                str(self.mnuConfig.actions()[3].isChecked())
                .lower()
                .replace("true", "enabled")
                .replace("false", "disabled"),
            ),
        )
        self.mnuConfig.addAction(
            "",
            lambda: self.SETTINGS.setValue(
                "AutoDetectLimitVoltage",
                str(self.mnuConfig.actions()[4].isChecked())
                .lower()
                .replace("true", "enabled")
                .replace("false", "disabled"),
            ),
        )
        self.mnuConfigBaudRate = QtWidgets.QMenu()
        self.mnuConfigBaudRateActionGroup = QtGui.QActionGroup(self.mnuConfigBaudRate)
        self.mnuConfigBaudRateActionGroup.setExclusive(True)
        self.mnuConfigBaudRateActions: dict[int, QtGui.QAction] = {}
        for baudrate in GBXCART_RW_BAUD_RATES:
            action = self.mnuConfigBaudRate.addAction("")
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, selected_baudrate=baudrate: self.SetGBxCartRWBaudRate(selected_baudrate)
            )
            self.mnuConfigBaudRateActionGroup.addAction(action)
            self.mnuConfigBaudRateActions[baudrate] = action
        self.mnuConfig.addMenu(self.mnuConfigBaudRate)
        self.mnuConfig.addAction(
            "",
            lambda: self.SETTINGS.setValue(
                "GenerateDumpReports",
                str(self.mnuConfig.actions()[6].isChecked())
                .lower()
                .replace("true", "enabled")
                .replace("false", "disabled"),
            ),
        )
        self.mnuConfig.addAction(
            "",
            lambda: self.SETTINGS.setValue(
                "UseNoIntroFilenames",
                str(self.mnuConfig.actions()[7].isChecked())
                .lower()
                .replace("true", "enabled")
                .replace("false", "disabled"),
            ),
        )
        self.mnuConfig.addAction(
            "",
            lambda: [
                self.SETTINGS.setValue(
                    "AutoPowerOff",
                    str(self.mnuConfig.actions()[8].isChecked()).lower().replace("true", "350").replace("false", "0"),
                ),
                self.SetAutoPowerOff(),
            ],
        )
        self.mnuConfig.addAction(
            "",
            lambda: self.SETTINGS.setValue(
                "CompareSectors",
                str(self.mnuConfig.actions()[9].isChecked())
                .lower()
                .replace("true", "enabled")
                .replace("false", "disabled"),
            ),
        )
        self.mnuConfig.addAction(
            "",
            lambda: self.SETTINGS.setValue(
                "ForceWrPullup",
                str(self.mnuConfig.actions()[10].isChecked())
                .lower()
                .replace("true", "enabled")
                .replace("false", "disabled"),
            ),
        )
        self.mnuConfig.addSeparator()
        self.mnuConfigReadModeAGB = QtWidgets.QMenu()
        self.mnuConfigReadModeAGB.addAction(
            "",
            lambda: [
                self.SETTINGS.setValue(
                    "AGBReadMethod",
                    str(self.mnuConfigReadModeAGB.actions()[1].isChecked()).lower().replace("true", "2"),
                ),
                self.SetAGBReadMethod(),
            ],
        )
        self.mnuConfigReadModeAGB.addAction(
            "",
            lambda: [
                self.SETTINGS.setValue(
                    "AGBReadMethod",
                    str(self.mnuConfigReadModeAGB.actions()[0].isChecked()).lower().replace("true", "0"),
                ),
                self.SetAGBReadMethod(),
            ],
        )
        self.mnuConfigReadModeAGB.actions()[0].setCheckable(True)
        self.mnuConfigReadModeAGB.actions()[1].setCheckable(True)
        self.mnuConfigReadModeAGB.actions()[0].setChecked(self.SETTINGS.value("AGBReadMethod", default="2") == "2")
        self.mnuConfigReadModeAGB.actions()[1].setChecked(self.SETTINGS.value("AGBReadMethod", default="2") == "0")
        self.mnuConfigReadModeDMG = QtWidgets.QMenu()
        self.mnuConfigReadModeDMG.addAction(
            "",
            lambda: [
                self.SETTINGS.setValue(
                    "DMGReadMethod",
                    str(self.mnuConfigReadModeDMG.actions()[0].isChecked()).lower().replace("true", "1"),
                ),
                self.SetDMGReadMethod(),
            ],
        )
        self.mnuConfigReadModeDMG.addAction(
            "",
            lambda: [
                self.SETTINGS.setValue(
                    "DMGReadMethod",
                    str(self.mnuConfigReadModeDMG.actions()[1].isChecked()).lower().replace("true", "2"),
                ),
                self.SetDMGReadMethod(),
            ],
        )
        self.mnuConfigReadModeDMG.actions()[0].setCheckable(True)
        self.mnuConfigReadModeDMG.actions()[1].setCheckable(True)
        self.mnuConfigReadModeDMG.actions()[0].setChecked(self.SETTINGS.value("DMGReadMethod", default="1") == "1")
        self.mnuConfigReadModeDMG.actions()[1].setChecked(self.SETTINGS.value("DMGReadMethod", default="1") == "2")
        self.mnuConfig.addMenu(self.mnuConfigReadModeDMG)
        self.mnuConfig.addMenu(self.mnuConfigReadModeAGB)
        self.mnuConfig.addSeparator()
        self.mnuConfig.addAction("", self.ReEnableMessages)
        self._ApplyConfigMenuSettings()

    def __init__(self, args: GuiArgs) -> None:
        sys.excepthook = Logger.exception_hook
        self._InitializeState(args)
        self._ConfigureColorScheme()

        QtWidgets.QMainWindow.__init__(self)
        AppContext.CONFIG_PATH = args["config_path"]
        AppContext.APP_PATH = args["app_path"]

        self.setStyleSheet("QMessageBox { messagebox-text-interaction-flags: 5; }")
        self.setWindowTitle(f"{AppInfo.NAME:s} {AppInfo.VERSION:s}")
        # self.setContentsMargins(0, 0, 0, 0)
        self.TEXT_COLOR = cast(
            "tuple[int, int, int, int]",
            QtGui.QPalette().color(QtGui.QPalette.ColorRole.Text).toTuple(),
        )

        # Create the QtWidgets.QVBoxLayout that lays out the whole form
        self.main_layout = QtWidgets.QGridLayout()
        self.layout_left = QtWidgets.QVBoxLayout()
        self.layout_right = QtWidgets.QVBoxLayout()

        # Cartridge Information GroupBox
        self.grpDMGCartridgeInfo = self.GuiCreateGroupBoxDMGCartInfo()
        self.grpAGBCartridgeInfo = self.GuiCreateGroupBoxAGBCartInfo()
        self.grpAGBCartridgeInfo.setVisible(False)
        self.layout_left.addWidget(self.grpDMGCartridgeInfo)
        self.layout_left.addWidget(self.grpAGBCartridgeInfo)

        # Actions
        self._CreateActionsGroup()

        # Transfer Status
        self._CreateTransferStatusGroup()

        self.main_layout.addLayout(self.layout_left, 0, 0)
        self.main_layout.addLayout(self.layout_right, 0, 1)

        self._CreateDeviceAndToolsMenus()

        self._CreateConfigMenu()

        self._CreateLanguageMenu()

        self.mnuThirdParty = QtWidgets.QMenu()
        self.mnuDeviceSupport = self.mnuThirdParty.addAction("", self.AboutConnectedDevice)
        self.mnuDeviceSupport.setVisible(False)
        self.mnuThirdParty.addAction("", lambda: [QtWidgets.QMessageBox.aboutQt(None)])
        self.mnuThirdParty.addAction("", self.AboutGameDB)
        self.mnuThirdParty.addAction(
            "",
            lambda: [self.OpenPath(str(Path(AppContext.APP_PATH) / "res" / "Third Party Notices.md"))],
        )

        self.btnMainMenu = QtWidgets.QPushButton()
        self.mnuMainMenu = QtWidgets.QMenu()
        self.mnuMainMenu.addMenu(self.mnuConfig)
        self.mnuMainMenu.addMenu(self.mnuTools)
        self.mnuMainMenu.addMenu(self.mnuLanguage)
        self.mnuMainMenu.addSeparator()
        self.mnuMainMenu.addSeparator()
        self.mnuMainMenu.addAction("", lambda _checked=False: self.OpenPath())
        self.mnuMainMenu.addSeparator()
        self.mnuMainMenu.addMenu(self.mnuThirdParty)
        self.mnuMainMenu.addAction("", self.AboutFlashGBX)
        self.btnMainMenu.setMenu(self.mnuMainMenu)

        self.btnConnect = QtWidgets.QPushButton()
        self.btnConnect.clicked.connect(self.ConnectDevice)
        self.layout_devices.addWidget(self.btnMainMenu)
        self.layout_devices.addWidget(self.btnConnect)

        self._ConfigurePlatformLayoutSpacing()

        self.InitWidgetTexts()

        self.main_layout.addLayout(self.layout_devices, 1, 0, 1, 0)

        # Disable widgets
        self._DisableDisconnectedControls()

        # Set the main layout on a central widget for QMainWindow
        self.central_widget = QtWidgets.QWidget()
        self.central_widget.setContentsMargins(0, 0, 0, 0)
        self.central_widget.setLayout(self.main_layout)
        self.setCentralWidget(self.central_widget)

        # Show app window first, then do update check
        self.QT_APP = qt_app
        qt_app.processEvents()

        self._ShowStartupMessages(args["config_ret"])

        self.DEFAULT_STYLESHEET = self.lblDevice.styleSheet()

        QtCore.QTimer.singleShot(
            1,
            lambda: [
                self.UpdateCheck(),
                self.FindDevices(port=args["argparsed"].device_port, firstRun=True),
            ],
        )
        self._StartStatusTimers()

    @property
    def _device(self) -> LK_Device:
        """Return the active device or fail with a clear internal-state error."""
        if self.CONN is None:
            msg = "No cartridge reader is connected"
            raise ConnectionError(msg)
        return self.CONN

    def _GetAutoPlatformMode(
        self,
        conn: LK_Device | None = None,
        supported_modes: Sequence[PlatformMode] | None = None,
    ) -> PlatformMode | None:
        if conn is None:
            conn = self.CONN
        if conn is None:
            return None
        if supported_modes is None:
            supported_modes = tuple(mode for mode in conn.GetSupprtedModes() if mode in ("DMG", "AGB"))
        if len(supported_modes) == 1:
            return supported_modes[0]
        if conn.FW.get("cart_mode_switch"):
            switch_mode = conn.GetCartModeSwitchState()
            if switch_mode is not False:
                mode = "AGB" if switch_mode == 1 else "DMG"
                if mode in supported_modes:
                    return mode
        mode = conn.GetMode()
        if mode in ("DMG", "AGB") and mode in supported_modes:
            return mode
        return None

    def _UpdatePlatformModeFromFirmware(self) -> None:
        if self.CONN is None:
            return
        if not (self._device.CanSetVoltageByAutoswitch() and not self._device.CanSetVoltageByCode()):
            return

        auto_mode = self._GetAutoPlatformMode(self.CONN)
        if auto_mode not in ("DMG", "AGB"):
            return
        if auto_mode == "DMG":
            self.optDMG.setChecked(True)
        else:
            self.optAGB.setChecked(True)
        self.SetMode()

    def MsgBoxCheck(self) -> None:
        if not self.MSGBOX_DISPLAYING and not self.MSGBOX_QUEUE.empty():
            self.MSGBOX_DISPLAYING = True
            msgbox = self.MSGBOX_QUEUE.get()
            dprint(f"Processing Message Box: {msgbox}")
            msgbox.exec()
            self.MSGBOX_DISPLAYING = False

    def LogErrorCheck(self) -> None:
        if isinstance(sys.stdout, Logger) and sys.stdout.LOG_ERROR is True:
            self.lblWarning.setVisible(True)

    def SetDMGPlatformBadge(self, data: Mapping[str, Any] | None = None) -> None:
        base = "QLabel { border-radius: 4px; padding: 0px 6px; font-weight: bold; font-size: 10px; }"
        if data is None:
            self.lblDMGPlatformBadge.setText("")
            self.lblDMGPlatformBadge.setStyleSheet("")
            self.lblDMGPlatformBadge.setToolTip("")
            self.lblDMGPlatformBadge.setVisible(False)
            return
        cgb = data.get("cgb", 0)
        sgb = data.get("sgb", 0)
        old_lic = data.get("old_lic", 0)
        if cgb == 0xC0:
            text = "CGB"
            tooltip = __("Game Boy Color exclusive")
            style = (
                "QLabel {"
                " background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
                "  stop:0 rgba(255,60,60,115), stop:0.25 rgba(255,180,30,115),"
                "  stop:0.5 rgba(40,200,80,115), stop:0.75 rgba(50,140,255,115),"
                "  stop:1 rgba(160,70,255,115));"
                " color: #ffffff;"
                " border: 1px solid rgba(255,255,255,115);"
                " border-radius: 4px; padding: 0px 6px;"
                " font-weight: bold; font-size: 10px;"
                "}"
            )
        elif cgb == 0x80:
            text = "CGB"
            tooltip = __("Game Boy Color")
            style = base + (
                "QLabel {"
                " background-color: rgba(124, 92, 252, 51);"
                " color: #a78bfa;"
                " border: 1px solid rgba(124, 92, 252, 89);"
                "}"
            )
        elif old_lic == 0x33 and sgb == 0x03:
            text = "SGB"
            tooltip = __("Super Game Boy")
            style = base + (
                "QLabel {"
                " background-color: rgba(63, 220, 142, 38);"
                " color: #3fdc8e;"
                " border: 1px solid rgba(63, 220, 142, 76);"
                "}"
            )
        else:
            text = "DMG"
            tooltip = __("Original Game Boy")
            style = base + (
                "QLabel {"
                " background-color: rgba(148, 163, 184, 31);"
                " color: #94a3b8;"
                " border: 1px solid rgba(148, 163, 184, 64);"
                "}"
            )
        self.lblDMGPlatformBadge.setText(text)
        self.lblDMGPlatformBadge.setToolTip(tooltip)
        self.lblDMGPlatformBadge.setStyleSheet(style)
        self.lblDMGPlatformBadge.setVisible(True)
        self._UpdateDMGGameNameLayout()

    def SetDMGGameNameText(self, text: str | None) -> None:
        self._dmgGameNameFullText = text or ""
        self.lblDMGGameNameResult.setText(self._dmgGameNameFullText)
        self._UpdateDMGGameNameLayout()
        if self.lblDMGGameNameResult.text() != self._dmgGameNameFullText:
            self.lblDMGGameNameResult.setToolTip(self._dmgGameNameFullText)
        else:
            self.lblDMGGameNameResult.setToolTip("")

    def _UpdateDMGGameNameLayout(self) -> None:
        if not hasattr(self, "_rowDMGGameName"):
            return
        default_col_w = self._dmgGameNameDefaultColWidth
        if default_col_w <= 0:
            return
        row_w = self._rowDMGGameName.geometry().width()
        if row_w <= 0:
            return

        label = self.lblDMGGameName
        result = self.lblDMGGameNameResult
        badge = self.lblDMGPlatformBadge
        full_text = self._dmgGameNameFullText

        fm = result.fontMetrics()
        text_w = fm.horizontalAdvance(full_text)

        outer_spacing = self._rowDMGGameName.spacing()
        if outer_spacing < 0:
            outer_spacing = self.style().pixelMetric(QtWidgets.QStyle.PixelMetric.PM_LayoutHorizontalSpacing)
            if outer_spacing < 0:
                outer_spacing = 6
        inner_spacing = max(self._resultDMGGameName.spacing(), 0)

        badge_w = badge.sizeHint().width() + inner_spacing if badge.isVisible() and badge.text() else 0

        # 2 px safety buffer for pixel rounding in text rendering
        reserved = outer_spacing + badge_w + 2

        table_avail = row_w - default_col_w - reserved
        if text_w <= table_avail:
            new_col_w = default_col_w
            elided = full_text
        else:
            label_fm = label.fontMetrics()
            margins = label.contentsMargins()
            natural_label_w = label_fm.horizontalAdvance(label.text()) + margins.left() + margins.right()
            natural_label_w = max(natural_label_w, 0)
            new_col_w = max(natural_label_w, row_w - reserved - text_w)
            new_col_w = min(new_col_w, default_col_w)
            avail = max(row_w - new_col_w - reserved, 0)
            elided = (
                fm.elidedText(full_text, QtCore.Qt.TextElideMode.ElideRight, avail) if text_w > avail else full_text
            )

        if label.minimumWidth() != new_col_w or label.maximumWidth() != new_col_w:
            label.setMinimumWidth(new_col_w)
            label.setMaximumWidth(new_col_w)
        if result.text() != elided:
            result.setText(elided)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._UpdateDMGGameNameLayout()

    def _ResetWidgetTexts(self) -> None:
        default_stylesheet = self.DEFAULT_STYLESHEET if self.DEFAULT_STYLESHEET is not None else ""
        for label in (
            self.lblDMGGameNameResult,
            self.lblDMGRomTitleResult,
            self.lblDMGGameCodeRevisionResult,
            self.lblDMGHeaderRtcResult,
            self.lblDMGHeaderBootlogoResult,
            self.lblDMGHeaderROMChecksumResult,
            self.lblAGBGameNameResult,
            self.lblAGBRomTitleResult,
            self.lblAGBHeaderGameCodeRevisionResult,
            self.lblAGBGpioRtcResult,
            self.lblAGBHeaderBootlogoResult,
            self.lblAGBHeaderChecksumResult,
            self.lblAGBHeaderROMChecksumResult,
        ):
            label.clear()
            label.setStyleSheet(default_stylesheet)
            label.setToolTip("")
        self._dmgGameNameFullText = ""
        self._UpdateDMGGameNameLayout()
        self.lblDMGHeaderRtcResult.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.lblAGBGpioRtcResult.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.cmbDMGHeaderROMSizeResult.clear()
        self.cmbDMGHeaderSaveTypeResult.clear()
        self.cmbDMGHeaderMapperResult.clear()
        self.cmbDMGCartridgeTypeResult.clear()
        self.cmbAGBHeaderROMSizeResult.clear()
        self.cmbAGBSaveTypeResult.clear()
        self.cmbAGBCartridgeTypeResult.clear()
        self.lblStatus1aResult.setText("-")
        self.lblStatus2aResult.setText("-")
        self.lblStatus3aResult.setText("-")
        self.SetStatus4aResult("")
        self.SetDMGPlatformBadge(None)

    def _InitCartridgeInfoWidgetTexts(self) -> None:
        # DMG Cartridge Info
        self.grpDMGCartridgeInfo.setTitle(__("Game Boy Cartridge Information"))
        self.lblDMGGameName.setText(__("Game Name:"))
        self.lblDMGRomTitle.setText(__("ROM Title:"))
        self.lblDMGGameCodeRevision.setText(__("Game Code and Revision:"))
        self.lblDMGHeaderRtc.setText(__("Real Time Clock:"))
        self.lblDMGHeaderBootlogo.setText(__("Boot Logo:"))
        self.lblDMGHeaderROMChecksum.setText(__("ROM Checksum:"))
        self.lblDMGHeaderROMSize.setText(__("ROM Size:"))
        self.lblDMGHeaderSaveType.setText(__("Save Type:"))
        self.lblDMGHeaderMapper.setText(__("Mapper Type:"))
        self.lblDMGCartridgeType.setText(__("Profile:"))

        # AGB Cartridge Info
        self.grpAGBCartridgeInfo.setTitle(__("Game Boy Advance Cartridge Information"))
        self.lblAGBGameName.setText(__("Game Name:"))
        self.lblAGBRomTitle.setText(__("ROM Title:"))
        self.lblAGBHeaderGameCodeRevision.setText(__("Game Code and Revision:"))
        self.lblAGBGpioRtc.setText(__("Real Time Clock:"))
        self.lblAGBHeaderBootlogo.setText(__("Boot Logo:"))
        self.lblAGBHeaderChecksum.setText(__("Header Checksum:"))
        self.lblAGBHeaderROMChecksum.setText(__("ROM Checksum:"))
        self.lblAGBHeaderROMSize.setText(__("ROM Size:"))
        self.lblAGBHeaderSaveType.setText(__("Save Type:"))
        self.lblAGBCartridgeType.setText(__("Profile:"))

    def InitWidgetTexts(self) -> None:
        self._ResetWidgetTexts()

        self._InitCartridgeInfoWidgetTexts()

        self._InitActionWidgetTexts()

        # Transfer Status
        self.grpStatus.setTitle(__("Transfer Status"))
        self.lblStatus1a.setText(__("Data transferred:"))
        self.lblStatus2a.setText(__("Transfer rate:"))
        self.lblStatus3a.setText(__("Time elapsed:"))
        self.lblStatus4a.setText(__("Ready."))
        btnText = __("Stop")
        self.btnCancel.setText(btnText)
        btnWidth = self.btnCancel.fontMetrics().boundingRect(btnText).width() + 15
        if platform.system() == "Darwin":
            btnWidth += 12
        self.btnCancel.setMaximumWidth(btnWidth)

        # Device area
        self.lblDevice.setToolTip(__("Click here to generate a log file for debugging"))
        self.lblWarning.setToolTip(__("Click here to generate a log file for debugging"))

        # Tools menu
        self.mnuTools.setTitle(c__("Menu Item (& = Keyboard Shortcut)", "&Tools"))
        self.mnuTools.actions()[0].setText(c__("Menu Item (& = Keyboard Shortcut)", "Game Boy &Camera Album Viewer"))
        self.mnuTools.actions()[1].setText(c__("Menu Item (& = Keyboard Shortcut)", "&Interactive Console"))
        self.mnuTools.actions()[3].setText(c__("Menu Item (& = Keyboard Shortcut)", "Firmware &Updater"))

        # Settings menu
        self.mnuConfig.setTitle(c__("Menu Item (& = Keyboard Shortcut)", "&Settings"))
        self.mnuConfig.actions()[0].setText(
            c__(
                "Menu Item (& = Keyboard Shortcut)",
                "Check for &updates on application startup",
            ),
        )
        self.mnuConfig.actions()[1].setText(
            c__(
                "Menu Item (& = Keyboard Shortcut)",
                "&Append date && time to filename of save data backups",
            ),
        )
        self.mnuConfig.actions()[2].setText(c__("Menu Item (& = Keyboard Shortcut)", "Prefer full &chip erase"))
        self.mnuConfig.actions()[3].setText(c__("Menu Item (& = Keyboard Shortcut)", "&Verify transferred data"))
        self.mnuConfig.actions()[4].setText(
            c__(
                "Menu Item (& = Keyboard Shortcut)",
                "&Limit voltage when analyzing Game Boy carts",
            ),
        )
        self.mnuConfigBaudRate.setTitle(c__("Menu Item (& = Keyboard Shortcut)", "GBxCart RW &baud rate"))
        self.mnuConfigBaudRateActions[1_000_000].setText("1.0 Mbps")
        self.mnuConfigBaudRateActions[1_500_000].setText("1.5 Mbps")
        self.mnuConfigBaudRateActions[1_700_000].setText("1.7 Mbps")
        self.mnuConfig.actions()[6].setText(
            c__("Menu Item (& = Keyboard Shortcut)", "Always &generate ROM dump reports"),
        )
        self.mnuConfig.actions()[7].setText(c__("Menu Item (& = Keyboard Shortcut)", "Use &No-Intro file names"))
        self.mnuConfig.actions()[8].setText(c__("Menu Item (& = Keyboard Shortcut)", "Automatic cartridge &power off"))
        self.mnuConfig.actions()[9].setText(
            c__("Menu Item (& = Keyboard Shortcut)", "Skip writing matching ROM chunk&s"),
        )
        self.mnuConfig.actions()[10].setText(
            c__(
                "Menu Item (& = Keyboard Shortcut)",
                "Alternative address set mode (can fix or cause write &errors)",
            ),
        )
        self.mnuConfig.actions()[15].setText(c__("Menu Item (& = Keyboard Shortcut)", "Re-&enable suppressed messages"))

        # Read method sub-menus
        self.mnuConfigReadModeAGB.setTitle(c__("Menu Item (& = Keyboard Shortcut)", "&Read Method (Game Boy Advance)"))
        self.mnuConfigReadModeAGB.actions()[0].setText(c__("Menu Item (& = Keyboard Shortcut)", "S&tream"))
        self.mnuConfigReadModeAGB.actions()[1].setText(c__("Menu Item (& = Keyboard Shortcut)", "&Single"))
        self.mnuConfigReadModeDMG.setTitle(c__("Menu Item (& = Keyboard Shortcut)", "&Read Method (Game Boy)"))
        self.mnuConfigReadModeDMG.actions()[0].setText(c__("Menu Item (& = Keyboard Shortcut)", "&Normal"))
        self.mnuConfigReadModeDMG.actions()[1].setText(c__("Menu Item (& = Keyboard Shortcut)", "&Delayed"))

        # Language menu
        label_language = c__("Menu Item (& = Keyboard Shortcut)", "&Language")
        if label_language != "&Language":
            label_language = label_language.replace("&", "")
            label_language += " (&Language)"
        self.mnuLanguage.setTitle(label_language)

        # Third Party menu
        self.mnuThirdParty.setTitle(c__("Menu Item (& = Keyboard Shortcut)", "Third Party &Notices"))
        self.mnuThirdParty.actions()[1].setText(c__("Menu Item (& = Keyboard Shortcut)", "About &Qt"))
        self.mnuThirdParty.actions()[2].setText(c__("Menu Item (& = Keyboard Shortcut)", "About Game &Database"))
        self.mnuThirdParty.actions()[3].setText(c__("Menu Item (& = Keyboard Shortcut)", "Licenses"))
        self.UpdateThirdPartySupportAction()

        # Main menu actions
        self.mnuMainMenu.actions()[5].setText(c__("Menu Item (& = Keyboard Shortcut)", "Open &config folder"))
        self.mnuMainMenu.actions()[8].setText(c__("Menu Item (& = Keyboard Shortcut)", "About &FlashGBX"))

        # Options and Connect buttons
        btnText = c__("Button (& = Keyboard Shortcut)", "&Options")
        self.btnMainMenu.setText(btnText)
        btnWidth = self.btnMainMenu.fontMetrics().boundingRect(btnText).width() + 24
        if platform.system() == "Darwin":
            btnWidth += 12
        self.btnMainMenu.setMaximumWidth(btnWidth)
        self.btnConnect.setText(c__("Button (& = Keyboard Shortcut)", "&Connect"))
        self.ApplyInfoColumnWidths()

    def _InitActionWidgetTexts(self) -> None:
        self.grpActions.setTitle(__("Functions"))
        self.lblMode.setText(__("Plattform:") + " ")
        self.optDMG.setText(c__("Radio Button (& = Keyboard Shortcut)", "&Game Boy"))
        self.optAGB.setText(c__("Radio Button (& = Keyboard Shortcut)", "Game Boy &Advance"))
        self.btnHeaderRefresh.setText(c__("Button (& = Keyboard Shortcut)", "&Refresh"))
        self.btnDetectCartridge.setText(c__("Button (& = Keyboard Shortcut)", "Analyze &Flash Cart"))
        self.btnBackupROM.setText(c__("Button (& = Keyboard Shortcut)", "&Backup ROM"))
        self.btnBackupRAM.setText(c__("Button (& = Keyboard Shortcut)", "Backup &Save Data"))
        self.btnFlashROM.setText(c__("Button (& = Keyboard Shortcut)", "&Write ROM"))
        self.btnRestoreRAM.setText(c__("Button (& = Keyboard Shortcut)", "Writ&e Save Data"))
        self.mnuRestoreRAM.actions()[0].setText(
            c__("Menu Item (& = Keyboard Shortcut)", "&Restore from save data file")
        )
        self.mnuRestoreRAM.actions()[1].setText(c__("Menu Item (& = Keyboard Shortcut)", "&Erase cartridge save data"))
        self.mnuRestoreRAM.actions()[3].setText(c__("Menu Item (& = Keyboard Shortcut)", "Run stress &test"))

    def ApplyInfoColumnWidths(self) -> None:
        labels = (
            self.lblDMGGameName,
            self.lblDMGRomTitle,
            self.lblDMGGameCodeRevision,
            self.lblDMGHeaderRtc,
            self.lblDMGHeaderBootlogo,
            self.lblDMGHeaderROMChecksum,
            self.lblDMGHeaderROMSize,
            self.lblDMGHeaderSaveType,
            self.lblDMGHeaderMapper,
            # self.lblDMGCartridgeType,
            self.lblAGBGameName,
            self.lblAGBRomTitle,
            self.lblAGBHeaderGameCodeRevision,
            self.lblAGBGpioRtc,
            self.lblAGBHeaderBootlogo,
            self.lblAGBHeaderChecksum,
            self.lblAGBHeaderROMChecksum,
            self.lblAGBHeaderROMSize,
            self.lblAGBHeaderSaveType,
            # self.lblAGBCartridgeType,
        )
        max_width = max(label.sizeHint().width() for label in labels)
        for label in labels:
            label.setMinimumWidth(max_width)
            label.setMaximumWidth(max_width)
        self._dmgGameNameDefaultColWidth = max_width
        self._UpdateDMGGameNameLayout()

    def _CreateDMGGameNameRow(self, group_layout: QtWidgets.QVBoxLayout) -> None:
        rowDMGGameName = QtWidgets.QHBoxLayout()
        self.lblDMGGameName = QtWidgets.QLabel()
        self.lblDMGGameName.setContentsMargins(0, 1, 3, 1)
        rowDMGGameName.addWidget(self.lblDMGGameName)
        resultDMGGameName = QtWidgets.QHBoxLayout()
        resultDMGGameName.setContentsMargins(0, 0, 0, 0)
        resultDMGGameName.setSpacing(4)
        self.lblDMGGameNameResult = QtWidgets.QLabel("")
        self.lblDMGGameNameResult.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        resultDMGGameName.addWidget(self.lblDMGGameNameResult, 1)
        self.lblDMGPlatformBadge = QtWidgets.QLabel("")
        self.lblDMGPlatformBadge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lblDMGPlatformBadge.setVisible(False)
        self.lblDMGPlatformBadge.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.lblDMGPlatformBadge.setMaximumHeight(self.lblDMGGameNameResult.fontMetrics().height())
        resultDMGGameName.addWidget(
            self.lblDMGPlatformBadge,
            0,
            QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignRight,
        )
        rowDMGGameName.addLayout(resultDMGGameName)
        rowDMGGameName.setStretch(0, 9)
        rowDMGGameName.setStretch(1, 15)
        group_layout.addLayout(rowDMGGameName)
        self._rowDMGGameName = rowDMGGameName
        self._resultDMGGameName = resultDMGGameName
        self._dmgGameNameFullText = ""
        self._dmgGameNameDefaultColWidth = 0

    def _CreateDMGRomTitleRow(self, group_layout: QtWidgets.QVBoxLayout) -> None:
        rowDMGRomTitle = QtWidgets.QHBoxLayout()
        self.lblDMGRomTitle = QtWidgets.QLabel()
        self.lblDMGRomTitle.setContentsMargins(0, 1, 3, 1)
        rowDMGRomTitle.addWidget(self.lblDMGRomTitle)
        resultDMGRomTitle = QtWidgets.QHBoxLayout()
        resultDMGRomTitle.setContentsMargins(0, 0, 0, 0)
        resultDMGRomTitle.setSpacing(0)
        self.lblDMGRomTitleResult = QtWidgets.QLabel("")
        resultDMGRomTitle.addWidget(self.lblDMGRomTitleResult)
        rowDMGRomTitle.addLayout(resultDMGRomTitle)
        rowDMGRomTitle.setStretch(0, 9)
        rowDMGRomTitle.setStretch(1, 15)
        group_layout.addLayout(rowDMGRomTitle)

    def _CreateDMGGameCodeRevisionRow(self, group_layout: QtWidgets.QVBoxLayout) -> None:
        rowDMGGameCodeRevision = QtWidgets.QHBoxLayout()
        self.lblDMGGameCodeRevision = QtWidgets.QLabel()
        self.lblDMGGameCodeRevision.setContentsMargins(0, 1, 3, 1)
        rowDMGGameCodeRevision.addWidget(self.lblDMGGameCodeRevision)
        self.lblDMGGameCodeRevisionResult = QtWidgets.QLabel("")
        rowDMGGameCodeRevision.addWidget(self.lblDMGGameCodeRevisionResult)
        rowDMGGameCodeRevision.setStretch(0, 9)
        rowDMGGameCodeRevision.setStretch(1, 15)
        group_layout.addLayout(rowDMGGameCodeRevision)

    def _CreateDMGHeaderBootlogoRow(self, group_layout: QtWidgets.QVBoxLayout) -> None:
        row = QtWidgets.QHBoxLayout()
        self.lblDMGHeaderBootlogo = QtWidgets.QLabel()
        self.lblDMGHeaderBootlogo.setContentsMargins(0, 1, 3, 1)
        row.addWidget(self.lblDMGHeaderBootlogo)
        self.lblDMGHeaderBootlogoResult = QtWidgets.QLabel("")
        row.addWidget(self.lblDMGHeaderBootlogoResult)
        row.setStretch(0, 9)
        row.setStretch(1, 15)
        group_layout.addLayout(row)

    def GuiCreateGroupBoxDMGCartInfo(self) -> QtWidgets.QGroupBox:
        self.grpDMGCartridgeInfo = QtWidgets.QGroupBox()
        self.grpDMGCartridgeInfo.setMinimumWidth(450 if platform.system() == "Linux" else 400)
        group_layout = QtWidgets.QVBoxLayout()
        group_layout.setContentsMargins(-1, 5, -1, -1)
        if platform.system() == "Linux":
            group_layout.setSpacing(4)

        self._CreateDMGGameNameRow(group_layout)

        self._CreateDMGRomTitleRow(group_layout)

        self._CreateDMGGameCodeRevisionRow(group_layout)

        rowDMGHeaderRtc = QtWidgets.QHBoxLayout()
        self.lblDMGHeaderRtc = QtWidgets.QLabel()
        self.lblDMGHeaderRtc.setContentsMargins(0, 1, 3, 1)
        rowDMGHeaderRtc.addWidget(self.lblDMGHeaderRtc)
        self.lblDMGHeaderRtcResult = QtWidgets.QLabel("")
        # PySide supports assigning an event callback on the instance.
        self.lblDMGHeaderRtcResult.mousePressEvent = self._EditRTCFromMouseEvent  # ty: ignore[invalid-assignment]
        rowDMGHeaderRtc.addWidget(self.lblDMGHeaderRtcResult)
        rowDMGHeaderRtc.setStretch(0, 9)
        rowDMGHeaderRtc.setStretch(1, 15)
        group_layout.addLayout(rowDMGHeaderRtc)

        self._CreateDMGHeaderBootlogoRow(group_layout)

        rowDMGHeaderROMChecksum = QtWidgets.QHBoxLayout()
        self.lblDMGHeaderROMChecksum = QtWidgets.QLabel()
        self.lblDMGHeaderROMChecksum.setContentsMargins(0, 1, 3, 1)
        rowDMGHeaderROMChecksum.addWidget(self.lblDMGHeaderROMChecksum)
        self.lblDMGHeaderROMChecksumResult = QtWidgets.QLabel("")
        rowDMGHeaderROMChecksum.addWidget(self.lblDMGHeaderROMChecksumResult)
        rowDMGHeaderROMChecksum.setStretch(0, 9)
        rowDMGHeaderROMChecksum.setStretch(1, 15)
        group_layout.addLayout(rowDMGHeaderROMChecksum)

        rowDMGHeaderROMSize = QtWidgets.QHBoxLayout()
        self.lblDMGHeaderROMSize = QtWidgets.QLabel()
        rowDMGHeaderROMSize.addWidget(self.lblDMGHeaderROMSize)
        self.cmbDMGHeaderROMSizeResult = QtWidgets.QComboBox()
        self.cmbDMGHeaderROMSizeResult.setStyleSheet("combobox-popup: 0;")
        self.cmbDMGHeaderROMSizeResult.view().setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rowDMGHeaderROMSize.addWidget(self.cmbDMGHeaderROMSizeResult)
        rowDMGHeaderROMSize.setStretch(0, 9)
        rowDMGHeaderROMSize.setStretch(1, 15)
        group_layout.addLayout(rowDMGHeaderROMSize)

        rowDMGHeaderSaveType = QtWidgets.QHBoxLayout()
        self.lblDMGHeaderSaveType = QtWidgets.QLabel()
        rowDMGHeaderSaveType.addWidget(self.lblDMGHeaderSaveType)
        self.cmbDMGHeaderSaveTypeResult = QtWidgets.QComboBox()
        self.cmbDMGHeaderSaveTypeResult.setStyleSheet("combobox-popup: 0;")
        self.cmbDMGHeaderSaveTypeResult.view().setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rowDMGHeaderSaveType.addWidget(self.cmbDMGHeaderSaveTypeResult)
        rowDMGHeaderSaveType.setStretch(0, 9)
        rowDMGHeaderSaveType.setStretch(1, 15)
        group_layout.addLayout(rowDMGHeaderSaveType)

        rowDMGHeaderMapper = QtWidgets.QHBoxLayout()
        self.lblDMGHeaderMapper = QtWidgets.QLabel()
        rowDMGHeaderMapper.addWidget(self.lblDMGHeaderMapper)
        self.cmbDMGHeaderMapperResult = QtWidgets.QComboBox()
        self.cmbDMGHeaderMapperResult.setStyleSheet("combobox-popup: 0;")
        self.cmbDMGHeaderMapperResult.view().setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rowDMGHeaderMapper.addWidget(self.cmbDMGHeaderMapperResult)
        rowDMGHeaderMapper.setStretch(0, 9)
        rowDMGHeaderMapper.setStretch(1, 15)
        group_layout.addLayout(rowDMGHeaderMapper)

        rowDMGCartridgeType = QtWidgets.QHBoxLayout()
        self.lblDMGCartridgeType = QtWidgets.QLabel()
        rowDMGCartridgeType.addWidget(self.lblDMGCartridgeType)
        self.cmbDMGCartridgeTypeResult = QtWidgets.QComboBox()
        self.cmbDMGCartridgeTypeResult.setStyleSheet("max-width: 260px;")
        self.cmbDMGCartridgeTypeResult.setStyleSheet("combobox-popup: 0;")
        self.cmbDMGCartridgeTypeResult.view().setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rowDMGCartridgeType.addWidget(self.cmbDMGCartridgeTypeResult)
        group_layout.addLayout(rowDMGCartridgeType)

        self.grpDMGCartridgeInfo.setLayout(group_layout)

        return self.grpDMGCartridgeInfo

    def _CreateAGBGameNameRow(self, group_layout: QtWidgets.QVBoxLayout) -> None:
        rowAGBGameName = QtWidgets.QHBoxLayout()
        self.lblAGBGameName = QtWidgets.QLabel()
        self.lblAGBGameName.setContentsMargins(0, 1, 3, 1)
        rowAGBGameName.addWidget(self.lblAGBGameName)
        self.lblAGBGameNameResult = QtWidgets.QLabel("")
        rowAGBGameName.addWidget(self.lblAGBGameNameResult)
        rowAGBGameName.setStretch(0, 9)
        rowAGBGameName.setStretch(1, 15)
        group_layout.addLayout(rowAGBGameName)

    def _CreateAGBRomTitleAndGameCodeRows(self, group_layout: QtWidgets.QVBoxLayout) -> None:
        rowAGBRomTitle = QtWidgets.QHBoxLayout()
        self.lblAGBRomTitle = QtWidgets.QLabel()
        self.lblAGBRomTitle.setContentsMargins(0, 1, 3, 1)
        rowAGBRomTitle.addWidget(self.lblAGBRomTitle)
        self.lblAGBRomTitleResult = QtWidgets.QLabel("")
        rowAGBRomTitle.addWidget(self.lblAGBRomTitleResult)
        rowAGBRomTitle.setStretch(0, 9)
        rowAGBRomTitle.setStretch(1, 15)
        group_layout.addLayout(rowAGBRomTitle)

        rowAGBHeaderGameCodeRevision = QtWidgets.QHBoxLayout()
        self.lblAGBHeaderGameCodeRevision = QtWidgets.QLabel()
        self.lblAGBHeaderGameCodeRevision.setContentsMargins(0, 1, 3, 1)
        rowAGBHeaderGameCodeRevision.addWidget(self.lblAGBHeaderGameCodeRevision)
        self.lblAGBHeaderGameCodeRevisionResult = QtWidgets.QLabel("")
        rowAGBHeaderGameCodeRevision.addWidget(self.lblAGBHeaderGameCodeRevisionResult)
        rowAGBHeaderGameCodeRevision.setStretch(0, 9)
        rowAGBHeaderGameCodeRevision.setStretch(1, 15)
        group_layout.addLayout(rowAGBHeaderGameCodeRevision)

    def _CreateAGBHeaderBootlogoRow(self, group_layout: QtWidgets.QVBoxLayout) -> None:
        row = QtWidgets.QHBoxLayout()
        self.lblAGBHeaderBootlogo = QtWidgets.QLabel()
        self.lblAGBHeaderBootlogo.setContentsMargins(0, 1, 3, 1)
        row.addWidget(self.lblAGBHeaderBootlogo)
        self.lblAGBHeaderBootlogoResult = QtWidgets.QLabel("")
        row.addWidget(self.lblAGBHeaderBootlogoResult)
        row.setStretch(0, 9)
        row.setStretch(1, 15)
        group_layout.addLayout(row)

    def GuiCreateGroupBoxAGBCartInfo(self) -> QtWidgets.QGroupBox:
        self.grpAGBCartridgeInfo = QtWidgets.QGroupBox()
        self.grpAGBCartridgeInfo.setMinimumWidth(432 if platform.system() == "Linux" else 400)
        group_layout = QtWidgets.QVBoxLayout()
        group_layout.setContentsMargins(-1, 5, -1, -1)
        if platform.system() == "Linux":
            group_layout.setSpacing(4)

        self._CreateAGBGameNameRow(group_layout)

        self._CreateAGBRomTitleAndGameCodeRows(group_layout)

        rowAGBGpioRtc = QtWidgets.QHBoxLayout()
        self.lblAGBGpioRtc = QtWidgets.QLabel()
        self.lblAGBGpioRtc.setContentsMargins(0, 1, 3, 1)
        rowAGBGpioRtc.addWidget(self.lblAGBGpioRtc)
        self.lblAGBGpioRtcResult = QtWidgets.QLabel("")
        # PySide supports assigning an event callback on the instance.
        self.lblAGBGpioRtcResult.mousePressEvent = self._EditRTCFromMouseEvent  # ty: ignore[invalid-assignment]
        rowAGBGpioRtc.addWidget(self.lblAGBGpioRtcResult)
        rowAGBGpioRtc.setStretch(0, 9)
        rowAGBGpioRtc.setStretch(1, 15)
        group_layout.addLayout(rowAGBGpioRtc)

        self._CreateAGBHeaderBootlogoRow(group_layout)

        rowAGBHeaderChecksum = QtWidgets.QHBoxLayout()
        self.lblAGBHeaderChecksum = QtWidgets.QLabel()
        self.lblAGBHeaderChecksum.setContentsMargins(0, 1, 3, 1)
        rowAGBHeaderChecksum.addWidget(self.lblAGBHeaderChecksum)
        self.lblAGBHeaderChecksumResult = QtWidgets.QLabel("")
        rowAGBHeaderChecksum.addWidget(self.lblAGBHeaderChecksumResult)
        rowAGBHeaderChecksum.setStretch(0, 9)
        rowAGBHeaderChecksum.setStretch(1, 15)
        group_layout.addLayout(rowAGBHeaderChecksum)

        rowAGBHeaderROMChecksum = QtWidgets.QHBoxLayout()
        self.lblAGBHeaderROMChecksum = QtWidgets.QLabel()
        self.lblAGBHeaderROMChecksum.setContentsMargins(0, 1, 3, 1)
        rowAGBHeaderROMChecksum.addWidget(self.lblAGBHeaderROMChecksum)
        self.lblAGBHeaderROMChecksumResult = QtWidgets.QLabel("")
        rowAGBHeaderROMChecksum.addWidget(self.lblAGBHeaderROMChecksumResult)
        rowAGBHeaderROMChecksum.setStretch(0, 9)
        rowAGBHeaderROMChecksum.setStretch(1, 15)
        group_layout.addLayout(rowAGBHeaderROMChecksum)

        rowAGBHeaderROMSize = QtWidgets.QHBoxLayout()
        self.lblAGBHeaderROMSize = QtWidgets.QLabel()
        rowAGBHeaderROMSize.addWidget(self.lblAGBHeaderROMSize)
        self.cmbAGBHeaderROMSizeResult = QtWidgets.QComboBox()
        self.cmbAGBHeaderROMSizeResult.setStyleSheet("combobox-popup: 0;")
        self.cmbAGBHeaderROMSizeResult.view().setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rowAGBHeaderROMSize.addWidget(self.cmbAGBHeaderROMSizeResult)
        rowAGBHeaderROMSize.setStretch(0, 9)
        rowAGBHeaderROMSize.setStretch(1, 15)
        group_layout.addLayout(rowAGBHeaderROMSize)

        rowAGBHeaderSaveType = QtWidgets.QHBoxLayout()
        self.lblAGBHeaderSaveType = QtWidgets.QLabel()
        rowAGBHeaderSaveType.addWidget(self.lblAGBHeaderSaveType)
        self.cmbAGBSaveTypeResult = QtWidgets.QComboBox()
        self.cmbAGBSaveTypeResult.setStyleSheet("combobox-popup: 0;")
        self.cmbAGBSaveTypeResult.view().setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        rowAGBHeaderSaveType.addWidget(self.cmbAGBSaveTypeResult)
        rowAGBHeaderSaveType.setStretch(0, 9)
        rowAGBHeaderSaveType.setStretch(1, 15)
        group_layout.addLayout(rowAGBHeaderSaveType)

        rowAGBCartridgeType = QtWidgets.QHBoxLayout()
        self.lblAGBCartridgeType = QtWidgets.QLabel()
        rowAGBCartridgeType.addWidget(self.lblAGBCartridgeType)
        self.cmbAGBCartridgeTypeResult = QtWidgets.QComboBox()
        self.cmbAGBCartridgeTypeResult.setStyleSheet("max-width: 260px;")
        self.cmbAGBCartridgeTypeResult.setStyleSheet("combobox-popup: 0;")
        self.cmbAGBCartridgeTypeResult.view().setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.cmbAGBCartridgeTypeResult.currentIndexChanged.connect(self.CartridgeTypeChanged)
        rowAGBCartridgeType.addWidget(self.cmbAGBCartridgeTypeResult)
        group_layout.addLayout(rowAGBCartridgeType)

        self.grpAGBCartridgeInfo.setLayout(group_layout)
        return self.grpAGBCartridgeInfo

    def SetAutoPowerOff(self) -> None:
        if not self.CheckDeviceAlive():
            return
        try:
            value = int(str(self.SETTINGS.value("AutoPowerOff", default="0")))
        except ValueError:
            value = 0
        self._device.SetAutoPowerOff(value=value)

    def SetDMGReadMethod(self) -> None:
        if not self.CheckDeviceAlive():
            return
        try:
            method = int(str(self.SETTINGS.value("DMGReadMethod", "1")))
        except ValueError:
            method = 1
        self._device.SetDMGReadMethod(method)
        self.mnuConfigReadModeDMG.actions()[0].setChecked(False)
        self.mnuConfigReadModeDMG.actions()[1].setChecked(False)
        if method == 1:
            self.mnuConfigReadModeDMG.actions()[0].setChecked(True)
        elif method == 2:
            self.mnuConfigReadModeDMG.actions()[1].setChecked(True)

    def SetAGBReadMethod(self) -> None:
        if not self.CheckDeviceAlive():
            return
        try:
            method = int(str(self.SETTINGS.value("AGBReadMethod", "2")))
        except ValueError:
            method = 2
        self._device.SetAGBReadMethod(method)
        self.mnuConfigReadModeAGB.actions()[0].setChecked(False)
        self.mnuConfigReadModeAGB.actions()[1].setChecked(False)
        if method == 2:
            self.mnuConfigReadModeAGB.actions()[0].setChecked(True)
        elif method == 0:
            self.mnuConfigReadModeAGB.actions()[1].setChecked(True)

    def _GetGBxCartRWBaudRate(self) -> int:
        configured = self.SETTINGS.value("GBxCartRWBaudRate")
        legacy_limit = self.SETTINGS.value("LimitBaudRate")
        if configured is None:
            configured = (
                min(GBXCART_RW_BAUD_RATES) if str(legacy_limit).lower() == "enabled" else GBXCART_RW_DEFAULT_BAUD_RATE
            )
        try:
            baudrate = int(str(configured))
        except ValueError:
            baudrate = GBXCART_RW_DEFAULT_BAUD_RATE
        if baudrate not in GBXCART_RW_BAUD_RATES:
            baudrate = GBXCART_RW_DEFAULT_BAUD_RATE
        if str(configured) != str(baudrate) or self.SETTINGS.value("GBxCartRWBaudRate") is None:
            self.SETTINGS.setValue("GBxCartRWBaudRate", str(baudrate))
        if legacy_limit is not None:
            self.SETTINGS.setValue("LimitBaudRate", None)
        return baudrate

    def _UpdateGBxCartRWBaudRateActions(self, baudrate: int) -> None:
        for action_baudrate, action in self.mnuConfigBaudRateActions.items():
            action.setChecked(action_baudrate == baudrate)

    @staticmethod
    def _IsGBxCartRWDevice(device: object) -> bool:
        return getattr(device, "DEVICE_ID", "") == "gbxcartrw" or getattr(device, "DEVICE_NAME", "") == "GBxCart RW"

    def _GetDeviceMaxBaudRate(self, device: object) -> int:
        if self._IsGBxCartRWDevice(device):
            return self._GetGBxCartRWBaudRate()
        return 2_000_000

    def SetGBxCartRWBaudRate(self, baudrate: int) -> None:
        if baudrate not in GBXCART_RW_BAUD_RATES:
            msg = f"Unsupported GBxCart RW baud rate: {baudrate}"
            raise ValueError(msg)
        self.SETTINGS.setValue("GBxCartRWBaudRate", str(baudrate))
        self._UpdateGBxCartRWBaudRateActions(baudrate)
        if not self.CheckDeviceAlive() or not self._IsGBxCartRWDevice(self._device):
            return
        mode = self._device.GetMode()
        self._device.ChangeBaudRate(baudrate=baudrate)
        self.DisconnectDevice()
        self.FindDevices(connectToFirst=True, mode=mode)

    def SetLimitBaudRate(self) -> None:
        """Apply the legacy boolean baud-rate setting."""
        baudrate = (
            min(GBXCART_RW_BAUD_RATES)
            if str(self.SETTINGS.value("LimitBaudRate")).lower() == "enabled"
            else GBXCART_RW_DEFAULT_BAUD_RATE
        )
        self.SetGBxCartRWBaudRate(baudrate)

    def EnableUpdateCheck(self) -> None:
        update_check = self.SETTINGS.value("UpdateCheck")
        if update_check is None:
            self.UpdateCheck()
            return
        new_value = (
            str(self.mnuConfig.actions()[0].isChecked()).lower().replace("true", "enabled").replace("false", "disabled")
        )
        if new_value == "enabled":
            answer = QtWidgets.QMessageBox.question(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __(
                    "Would you like to automatically check for new versions at application startup? This will make use of the GitHub API ({url}).",
                    url='<a href="https://docs.github.com/en/site-policy/privacy-policies/github-privacy-statement">'
                    + c__("GitHub API Link", "privacy policy")
                    + "</a>",
                ),
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                self.SETTINGS.setValue("UpdateCheck", "enabled")
                self.mnuConfig.actions()[0].setChecked(True)
                update_check = "enabled"
                self.UpdateCheck()
            else:
                self.mnuConfig.actions()[0].setChecked(False)
                self.SETTINGS.setValue("UpdateCheck", "disabled")
        else:
            self.SETTINGS.setValue("UpdateCheck", "disabled")

    def ChangeLanguage(self, language_code: str) -> None:
        self.SETTINGS.setValue("Language", language_code)
        init_language(AppContext.CONFIG_PATH, override=language_code)
        loadQtTranslation(self.QT_APP, language=language_code)
        self.InitWidgetTexts()
        self.DisconnectDevice()

    def _PromptFirstRunUpdateCheck(self) -> str | None:
        answer = QtWidgets.QMessageBox.question(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            __(
                "Welcome to {app} by {author}!",
                app=AppInfo.NAME + " " + AppInfo.VERSION,
                author="Lesserkuma",
            )
            + "<br><br>"
            + __(
                "Would you like to automatically check for new versions at application startup? This will make use of the GitHub API ({url}).",
                url='<a href="https://docs.github.com/en/site-policy/privacy-policies/github-privacy-statement">'
                + c__("GitHub API Link", "privacy policy")
                + "</a>",
            ),
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.Yes,
        )
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            self.SETTINGS.setValue("UpdateCheck", "enabled")
            self.mnuConfig.actions()[0].setChecked(True)
            update_check = "enabled"
        else:
            self.SETTINGS.setValue("UpdateCheck", "disabled")
            update_check = None
        QtWidgets.QMessageBox.warning(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            __(
                "General precautions:\n- Due to voltage differences, do not insert a Game Boy Advance cartridge while the platform mode is set to “Game Boy”.\n- Always keep the cartridge contacts as clean as possible to ensure a stable connection.",
            ).replace("\n", "<br>"),
            QtWidgets.QMessageBox.StandardButton.Ok,
        )
        return update_check

    def _ReportUpdateHTTPError(self, response: requests.Response) -> None:
        """Report an unsuccessful HTTP response from the update endpoint."""
        if (
            response.status_code == 403
            and "X-RateLimit-Remaining" in response.headers
            and response.headers["X-RateLimit-Remaining"] == "0"
        ):
            print(__("Error: Failed to check for updates (too many API requests). Try again later."))
        else:
            print(
                __(
                    "Error: Failed to check for updates (HTTP status {status_code}).",
                    status_code=response.status_code,
                ),
            )

    def UpdateCheck(self) -> None:
        update_check: str | None = self.SETTINGS.value("UpdateCheck")
        if update_check is None:
            update_check = self._PromptFirstRunUpdateCheck()

        if update_check and update_check.lower() == "enabled":
            print()
            url = "https://api.github.com/repos/Lesserkuma/FlashGBX/releases/latest"
            site = "https://github.com/Lesserkuma/FlashGBX/releases/latest"
            try:
                ret = requests.get(url, allow_redirects=True, timeout=1.5)
            except requests.exceptions.ConnectTimeout as e:
                print(
                    __(
                        "Error: Update check failed due to a connection timeout. Please check your internet connection."
                    ),
                    e,
                    sep="\n",
                )
                ret = False
            except requests.exceptions.ConnectionError as e:
                print(
                    __("Error: Update check failed due to a connection error. Please check your network connection."),
                    e,
                    sep="\n",
                )
                ret = False
            except Exception as e:
                print(
                    __("Error: An unexpected error occured while querying the latest version information from GitHub."),
                    e,
                    sep="\n",
                )
                ret = False
            if ret is not False and ret.status_code == 200:
                ret = ret.content
                try:
                    ret = json.loads(ret)
                    if "tag_name" in ret:
                        latest_version = str(ret["tag_name"])
                        if version.parse(latest_version) == version.parse(AppInfo.VERSION_PEP440):
                            print(__("You are using the latest version of FlashGBX."))
                        elif version.parse(latest_version) > version.parse(AppInfo.VERSION_PEP440):
                            msg_text = f"A new version of FlashGBX has been released!\nVersion {latest_version:s} is now available."
                            print(
                                __(
                                    "A new version of FlashGBX has been released!\nVersion {new_version} is now available.",
                                    new_version=latest_version,
                                ),
                            )
                            msgbox = _create_message_box(
                                parent=self,
                                icon=QtWidgets.QMessageBox.Icon.Question,
                                windowTitle=AppInfo.NAME + " " + __("Update Check"),
                                text=msg_text,
                            )
                            button_open = msgbox.addButton(
                                c__(
                                    "Button (& = Keyboard Shortcut)",
                                    "&Open release notes",
                                ),
                                QtWidgets.QMessageBox.ButtonRole.ActionRole,
                            )
                            button_cancel = msgbox.addButton(
                                c__("Button (& = Keyboard Shortcut)", "&Close"),
                                QtWidgets.QMessageBox.ButtonRole.RejectRole,
                            )
                            msgbox.setDefaultButton(button_open)
                            msgbox.setEscapeButton(button_cancel)
                            msgbox.exec()
                            if msgbox.clickedButton() == button_open:
                                self.OpenWebURL(site)
                        else:
                            print(
                                __(
                                    "This version of FlashGBX ({appver}) seems to be newer than the latest public release ({public_version}).",
                                    appver=AppInfo.VERSION_PEP440,
                                    public_version=latest_version,
                                ),
                            )
                    else:
                        print(
                            __(
                                "Error: Update check failed due to missing version information in JSON data from GitHub.",
                            ),
                        )
                except json.decoder.JSONDecodeError:
                    print(__("Error: Update check failed due to malformed JSON data from GitHub."))
                except Exception as e:
                    print(
                        __(
                            "Error: An unexpected error occured while querying the latest version information from GitHub.",
                        ),
                        e,
                        sep="\n",
                    )
            elif ret is not False:
                self._ReportUpdateHTTPError(ret)

    def GetHostLauncherEnv(self) -> dict[str, str]:
        env = os.environ.copy()
        if platform.system() != "Linux":
            return env

        # Avoid leaking bundled AppImage libs into system launchers like xdg-open.
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
        else:
            env.pop("LD_LIBRARY_PATH", None)

        for var in ("APPDIR", "APPIMAGE", "ARGV0"):
            env.pop(var, None)
        return env

    def OpenWebURL(self, url: str) -> None:
        try:
            if platform.system() == "Linux":
                subprocess.Popen(  # noqa: S603 - resolved system launcher; no shell
                    [_system_executable("xdg-open"), url],
                    env=self.GetHostLauncherEnv(),
                )
            else:
                webbrowser.open(url)
        except Exception:
            logger.exception("Failed to open URL: {}", url)

    def DisconnectDevice(self) -> None:
        try:
            devname = self._device.GetFullNameExtended()
            self._device.Close(cartPowerOff=True)
            print(__("Disconnected from {device_name}", device_name=devname))
        except Exception:
            logger.exception("Failed to disconnect the GUI device")

        self.DEVICES = {}
        self.cmbDevice.clear()
        self.CONN = None

        self.optAGB.setEnabled(False)
        self.optDMG.setEnabled(False)
        self.grpDMGCartridgeInfo.setEnabled(False)
        self.grpAGBCartridgeInfo.setEnabled(False)
        self.btnCancel.setEnabled(False)
        self.btnHeaderRefresh.setEnabled(False)
        self.btnDetectCartridge.setEnabled(False)
        self.btnBackupROM.setEnabled(False)
        self.btnFlashROM.setEnabled(False)
        self.btnBackupRAM.setEnabled(False)
        self.btnRestoreRAM.setEnabled(False)
        self.btnConnect.setText(c__("Button (& = Keyboard Shortcut)", "&Connect"))
        self.lblDevice.setText(__("Disconnected."))
        self.SetProgressBars(min=0, max=1, value=0)
        self.lblStatus4a.setText(__("Disconnected."))
        self.lblStatus1aResult.setText("-")
        self.lblStatus2aResult.setText("-")
        self.lblStatus3aResult.setText("-")
        self.SetStatus4aResult("")
        self.lblStatus4a.setText(__("Disconnected."))
        self.grpStatus.setTitle(__("Transfer Status"))
        self.mnuConfig.actions()[5].setVisible(True)
        self.mnuConfig.actions()[8].setVisible(True)
        self.mnuConfig.actions()[9].setVisible(True)
        self.mnuConfig.actions()[10].setVisible(False)
        self.mnuTools.actions()[3].setEnabled(True)
        self.mnuTools.actions()[1].setEnabled(False)
        self.mnuConfigReadModeAGB.setEnabled(True)
        self.mnuLanguage.setEnabled(True)
        self.UpdateThirdPartySupportAction()

    def ReEnableMessages(self) -> None:
        self.SETTINGS.setValue("AutoReconnect", "disabled")
        self.SETTINGS.setValue("SkipModeChangeWarning", "disabled")
        self.SETTINGS.setValue("SkipAutodetectMessage", "disabled")
        self.SETTINGS.setValue("SkipFinishMessage", "disabled")
        self.SETTINGS.setValue("SkipCameraSavePopup", "disabled")

    def AboutFlashGBX(self) -> None:
        msg = "This software is being developed by Lesserkuma as a hobby project. There is no affiliation with Nintendo or any other company. This software is provided as-is and the developer is not responsible for any damage that is caused by the use of it. Use at your own risk!<br><br>"
        msg += f"© 2020-{datetime.datetime.now(tz=datetime.UTC).year} Lesserkuma<br>"
        msg += '<a href="https://github.com/Lesserkuma/FlashGBX">https://github.com/Lesserkuma/FlashGBX</a><br>'
        msg += "<br>"
        if i18n.CONFIGURED_LANGUAGE and i18n.CONFIGURED_LANGUAGE != "en" and i18n.TRANSLATION_AUTHOR:
            lang_name = LANGUAGES.get(i18n.CONFIGURED_LANGUAGE, i18n.CONFIGURED_LANGUAGE)
            if isinstance(lang_name, tuple):
                lang_name = lang_name[0]
            msg += f"Translated to {lang_name} by {i18n.TRANSLATION_AUTHOR}<br><br>"
        msg += "Acknowledgments and Contributors:<br><small>2358, 90sFlav, AcoVanConis, AdmirtheSableye, AlexiG, ALXCO-Hardware, AndehX, antPL, aronson, Ausar, bbsan, BennVenn, Boeuffy, CaptainBean, ccs21, chobby, ClassicOldSong, Cliffback, CodyWick13, Corborg, Cristóbal, crizzlycruz, Crystal, Därk, Davidish, delibird_deals, DevDavisNunez, Diddy_Kong, djedditt, Dr-InSide, Duckman, dyf2007, easthighNerd, EchelonPrime, edo999, Eldram, Eli, Ell, EmperorOfTigers, endrift, Erba Verde, ethanstrax, eveningmoose, Falknör, FerrantePescara, frarees, fredemmott, Frost Clock, Gahr, gandalf1980, gboh, gekkio, Godan, Goon, Grender, HDR, Herax, Hiccup, hiks, howie0210, iamevn, Icesythe7, ide, infinest, inYourBackline, iyatemu, Jayro, Jenetrix, JFox, joyrider3774, jrharbort, JS7457, julgr, Kaede, kane159, KOOORAY, kscheel, kyokohunter, Leitplanke, litlemoran, LovelyA72, Lu, Luca DS, LucentW, luxkiller65, manuelcm1, marv17, Merkin, metroid-maniac, Mr_V, Mufsta, numma_cway, olDirdey, orangeglo, paarongiroux, Paradoxical, Pese, Rairch, Raphaël BOICHOT, redalchemy, RetroGorek, RevZ, RibShark, s1cp, Satumox, Sgt.DoudouMiel, SH, Shinichi999, Sillyhatday, simonK, Sithdown, skite2001, Smelly-Ghost, Sonikks, Squiddy, Stitch, Super Maker, t5b6_de, Tauwasser, TheNFCookie, Timville, twitnic, velipso, Veund, voltagex, Voultar, Warez Waldo, wickawack, Winter1760, Wkr, x7l7j8cc, xactoes, xukkorz, yosoo, Zeii, Zelante, zipplet, Zoo, zvxr</small>"
        QtWidgets.QMessageBox.information(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            msg,
            QtWidgets.QMessageBox.StandardButton.Ok,
        )

    def AboutGameDB(self) -> None:
        msg = (
            __(
                "FlashGBX uses a game database that is based on the ongoing efforts of the No-Intro project. Visit {url} for more information.",
                url='<a href="https://no-intro.org/">https://no-intro.org/</a>',
            )
            + "<br><br>"
        )
        msg += __("No-Intro databases referenced for this version of FlashGBX:") + "<br>"
        msg += "• Nintendo - Game Boy (20260605-002844)<br>• Nintendo - Game Boy Advance (20260602-094414)<br>• Nintendo - Game Boy Advance (Video) (20260522-144016)<br>• Nintendo - Game Boy Color (20260605-010629)"  # No-Intro DBs
        QtWidgets.QMessageBox.information(
            self,
            f"FlashGBX {AppInfo.VERSION:s}",
            msg,
            QtWidgets.QMessageBox.StandardButton.Ok,
        )

    def _GetDeviceSupportData(self) -> tuple[str | None, str | None]:
        if self.CONN is None:
            return (None, None)
        message = self._device.GetSupportMessage()
        if message is None:
            return (None, None)
        device_name = self._device.GetName()
        return (str(device_name), str(message))

    def UpdateThirdPartySupportAction(self) -> None:
        device_name, support_message = self._GetDeviceSupportData()
        if device_name is None or support_message is None:
            self.mnuDeviceSupport.setVisible(False)
            return
        self.mnuDeviceSupport.setText(c__("Menu Item", "About {device_name}", device_name=device_name))
        self.mnuDeviceSupport.setVisible(True)

    def _ConvertUrlsToAnchors(self, text: str) -> str:
        escaped = html.escape(text)

        def repl(match: re.Match[str]) -> str:
            url = match.group(1)
            # url = "\u2060".join(url)
            return f'<a href="{url}">{url}</a>'

        return re.sub(r"(https?://[^\s<]+)", repl, escaped)

    def AboutConnectedDevice(self) -> None:
        device_name, support_message = self._GetDeviceSupportData()
        if device_name is None or support_message is None:
            return

        fw_version_text = (
            __("Connected to {device_name}", device_name=self._device.GetFullName())
            + "\n"
            + __(
                "Firmware version: {fw_version}",
                fw_version=self._device.GetFirmwareVersion(more=True),
            )
        )
        msg = self._ConvertUrlsToAnchors(fw_version_text).replace("\n", "<br>") + "<br><br>"
        msg += self._ConvertUrlsToAnchors(support_message).replace("\n", "<br>")
        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Information,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=msg,
            standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
        )
        msgbox.setTextFormat(QtCore.Qt.TextFormat.RichText)
        label = msgbox.findChild(QtWidgets.QLabel, "qt_msgbox_label")
        if label is not None:
            label.setOpenExternalLinks(True)
            label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextBrowserInteraction)
        msgbox.exec()

    def OpenPath(self, path: str | None = None, select_file: bool = False) -> None:
        if path is None:
            path = AppContext.CONFIG_PATH
            kbmod = QtWidgets.QApplication.keyboardModifiers()
            if kbmod != QtCore.Qt.KeyboardModifier.ShiftModifier:
                self.WriteDebugLog()

        system: str = platform.system()
        env: dict[str, str] = self.GetHostLauncherEnv()
        try:
            target_path = Path(path).resolve()
            if select_file and target_path.is_file():
                abs_path = str(target_path)
                if system == "Windows":
                    subprocess.Popen(  # noqa: S603 - resolved system launcher; no shell
                        [_system_executable("explorer"), "/select,", abs_path],
                    )
                    return
                if system == "Darwin":
                    subprocess.Popen(  # noqa: S603 - resolved system launcher; no shell
                        [_system_executable("open"), "-R", abs_path],
                        env=env,
                    )
                    return
                try:
                    file_uri: str = "file://" + urllib.parse.quote(abs_path)
                    subprocess.check_call(  # noqa: S603 - fixed system utility; no shell
                        [
                            _system_executable("dbus-send"),
                            "--session",
                            "--print-reply",
                            "--dest=org.freedesktop.FileManager1",
                            "--type=method_call",
                            "/org/freedesktop/FileManager1",
                            "org.freedesktop.FileManager1.ShowItems",
                            "array:string:" + file_uri,
                            "string:",
                        ],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    target_path = target_path.parent
                else:
                    return

            path_uri: str = target_path.as_uri()
            if system == "Windows":
                cast("Any", os).startfile(path_uri)
            elif system == "Darwin":
                subprocess.Popen(  # noqa: S603 - resolved system launcher; no shell
                    [_system_executable("open"), path_uri],
                    env=env,
                )
            else:
                subprocess.Popen(  # noqa: S603 - resolved system launcher; no shell
                    [_system_executable("xdg-open"), path_uri],
                    env=env,
                )
        except Exception:
            logger.exception("Failed to open path: {}", path)
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("The file was not found.") + "\n\n" + str(path),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )

    def WriteDebugLog(self, event: QtGui.QMouseEvent | None = None, open_log: bool = False) -> None:
        if isinstance(event, QtGui.QMouseEvent) and event.button() in (
            QtCore.Qt.MouseButton.MiddleButton,
            QtCore.Qt.MouseButton.RightButton,
        ):
            return

        device: Any = False
        try:
            device = self._device.GetFullNameExtended(more=True)
        except Exception:
            logger.exception("Failed to read the connected device name for the debug log")

        Logger.write_debug_log(device)
        try:
            if open_log is True:
                self.OpenPath(str(Path(AppContext.CONFIG_PATH) / "debug.log"))
                self.lblWarning.setVisible(False)
                if isinstance(sys.stdout, Logger) and sys.stdout.LOG_ERROR is True:
                    sys.stdout.LOG_ERROR = False
        except Exception:
            logger.exception("Failed to open the debug log")

    def _DisplayConnectionMessages(self, messages: list[Any]) -> tuple[bool, str]:
        displayed_text = ""
        icons = {
            1: QtWidgets.QMessageBox.Icon.Information,
            2: QtWidgets.QMessageBox.Icon.Warning,
            3: QtWidgets.QMessageBox.Icon.Critical,
        }
        for status, raw_text in messages:
            message = str(raw_text)
            if message in displayed_text:
                continue
            if status == 0:
                displayed_text += message + "\n"
                continue
            icon = icons.get(status)
            if icon is None:
                continue
            msgbox = _create_message_box(
                parent=self,
                icon=icon,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=message,
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            if "\n" not in message:
                msgbox.setTextFormat(QtCore.Qt.TextFormat.RichText)
            msgbox.exec()
            if status == 3:
                self.CONN = None
                return False, displayed_text
        return True, displayed_text

    def _HandleFirmwareUpdate(self, dev: LK_Device, *, supported: bool) -> None:
        if supported:
            if not dev.FirmwareUpdateAvailable():
                return
            dontShowAgain = str(self.SETTINGS.value("SkipFirmwareUpdate", default="disabled")).lower() == "enabled"
            if dontShowAgain and not dev.FW_UPDATE_REQ:
                return

            cb = None
            if dev.FW_UPDATE_REQ is True:
                text = __(
                    "A firmware update for your {device_name} is required to use this software. Do you want to update now?",
                    device_name=dev.GetFullName(),
                )
                msgbox = _create_message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Warning,
                    windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    text=text,
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    defaultButton=QtWidgets.QMessageBox.StandardButton.Yes,
                )
            elif dev.FW_UPDATE_REQ == 2:
                text = (
                    __(
                        "Your {device_name} is no longer supported in this version of FlashGBX due to technical limitations. The last supported version is {url}.",
                        device_name=dev.GetFullName(),
                        url='<a href="https://github.com/Lesserkuma/FlashGBX/releases/tag/3.37">FlashGBX v3.37</a>',
                    )
                    + "<br><br>"
                    + __(
                        "The Firmware Updater can still be used, however any other functions are no longer available.",
                    )
                    + "<br><br>"
                    + __("Do you want to run the Firmware Updater now?")
                )
                msgbox = _create_message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Warning,
                    windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    text=text,
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    defaultButton=QtWidgets.QMessageBox.StandardButton.Yes,
                )
            else:
                text = __(
                    "A firmware update for your {device_name} is available. Do you want to update now?",
                    device_name=dev.GetFullName(),
                )
                msgbox = _create_message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Information,
                    windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    text=text,
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    defaultButton=QtWidgets.QMessageBox.StandardButton.Yes,
                )
                cb = _create_check_box(
                    c__(
                        "Check Box (& = Keyboard Shortcut)",
                        "&Ignore firmware updates",
                    ),
                    checked=dontShowAgain,
                )
            answer = msgbox.exec()
            if dev.FW_UPDATE_REQ:
                if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                    self.ShowFirmwareUpdateWindow()
                if not AppContext.DEBUG:
                    self.DisconnectDevice()
                return
            if cb is not None:
                dontShowAgain = cb.isChecked()
                if dontShowAgain:
                    self.SETTINGS.setValue("SkipFirmwareUpdate", "enabled")
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                self.ShowFirmwareUpdateWindow()
            return

        if dev.FW_UPDATE_REQ:
            text = (
                __(
                    "A firmware update for your {device_name} is required to use this software.",
                    device_name=dev.GetFullName(),
                )
                + "<br><br>"
                + __(
                    "Current firmware version: {fw_version}",
                    fw_version=dev.GetFirmwareVersion(),
                )
            )
            if not AppContext.DEBUG:
                self.DisconnectDevice()
            QtWidgets.QMessageBox.warning(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text,
                QtWidgets.QMessageBox.StandardButton.Ok,
            )

    def _ConfigureConnectedPlatformButtons(self) -> None:
        self.optDMG.setAutoExclusive(False)
        self.optAGB.setAutoExclusive(False)
        device_auto_switch_only = self._device.CanSetVoltageByAutoswitch() and not self._device.CanSetVoltageByCode()
        if "DMG" in self._device.GetSupprtedModes():
            self.optDMG.setEnabled(not device_auto_switch_only)
            self.optDMG.setChecked(False)
        if "AGB" in self._device.GetSupprtedModes():
            self.optAGB.setEnabled(not device_auto_switch_only)
            self.optAGB.setChecked(False)
        self.optAGB.setAutoExclusive(True)
        self.optDMG.setAutoExclusive(True)
        if len(self._device.GetSupprtedModes()) == 2:
            self.lblStatus4a.setText(__("Ready. Please select Platform Mode."))
        else:
            self.lblStatus4a.setText(__("Ready."))

    def _complete_device_connection(self, dev: LK_Device, msg: str) -> bool:
        self.CONN = dev
        dev.SetWriteDelay(enable=str(self.SETTINGS.value("WriteDelay", default="disabled")).lower() == "enabled")
        self.SetAutoPowerOff()
        self.SetDMGReadMethod()
        self.SetAGBReadMethod()
        self.mnuConfig.actions()[5].setVisible(self._IsGBxCartRWDevice(self._device))  # GBxCart RW baud rate
        self.mnuConfig.actions()[8].setVisible(
            self._device.CanPowerCycleCart() and self._device.CanPowerCycleCart() and self._device.FW["fw_ver"] >= 12,
        )  # Auto Power Off
        self.mnuConfig.actions()[9].setVisible(
            self._device.FW["fw_ver"] >= 12,
        )  # Skip writing matching ROM chunks
        self.mnuConfig.actions()[10].setVisible(self._device.DEVICE_NAME == "Joey Jr")  # Force WR Pullup
        self.mnuConfigReadModeAGB.setEnabled(self._device.FW["fw_ver"] >= 12)
        self.mnuConfigReadModeDMG.setEnabled(self._device.FW["fw_ver"] >= 12)
        self.UpdateThirdPartySupportAction()

        cast("Any", self._device).SetTimeout(float(str(self.SETTINGS.value("SerialTimeout", default="1"))))
        self._ConfigureConnectedPlatformButtons()
        self.btnConnect.setText(c__("Button (& = Keyboard Shortcut)", "&Disconnect"))
        self.cmbDevice.setStyleSheet("QComboBox { border: 0; margin: 0; padding: 0; max-width: 0px; }")
        if dev.GetFWBuildDate() == "":
            self.lblDevice.setText(dev.GetFullNameLabel() + " [" + __("Legacy Mode") + "]")
        else:
            self.lblDevice.setText(dev.GetFullNameLabel())
        print(
            "\n"
            + __(
                "Connected to {device_name}",
                device_name=dev.GetFullNameExtended(more=True),
            ),
        )
        self.grpActions.setEnabled(True)
        self.mnuTools.setEnabled(True)
        self.mnuConfig.setEnabled(True)
        self.mnuLanguage.setEnabled(True)
        self.btnCancel.setEnabled(False)

        # Firmware Update Menu
        self.mnuTools.actions()[3].setEnabled(True)
        supports_firmware_updates = self._device.SupportsFirmwareUpdates()
        if supports_firmware_updates is False:
            self.mnuTools.actions()[3].setEnabled(False)

        # Interactive Console Menu
        self.mnuTools.actions()[1].setEnabled(self._device.GetMode() is not None)

        self.SetProgressBars(min=0, max=1, value=0)

        if self._device.GetMode() == "DMG":
            self.cmbDMGCartridgeTypeResult.clear()
            self.cmbDMGCartridgeTypeResult.addItems(self._device.GetSupportedCartridgesDMG()[0])
            self.grpAGBCartridgeInfo.setVisible(False)
            self.grpDMGCartridgeInfo.setVisible(True)
        elif self._device.GetMode() == "AGB":
            self.cmbAGBCartridgeTypeResult.clear()
            self.cmbAGBCartridgeTypeResult.addItems(self._device.GetSupportedCartridgesAGB()[0])
            self.grpDMGCartridgeInfo.setVisible(False)
            self.grpAGBCartridgeInfo.setVisible(True)

        print(msg, end="")

        self._HandleFirmwareUpdate(dev, supported=supports_firmware_updates)

        if dev.IsUnregistered():
            try:
                text = cast("Any", dev).GetRegisterInformation()
                QtWidgets.QMessageBox.critical(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    text,
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
            except Exception:
                logger.exception("Failed to display device registration information")

        if self.CONN is None:
            return False

        modes = cast("Sequence[PlatformMode]", self._device.GetSupprtedModes())
        auto_mode = self._GetAutoPlatformMode(self.CONN, modes)
        if auto_mode == "DMG":
            self.optDMG.setChecked(True)
            self.SetMode()
        elif auto_mode == "AGB":
            self.optAGB.setChecked(True)
            self.SetMode()
        return True

    def ConnectDevice(self) -> bool | None:
        if self.CONN is not None:
            self.DisconnectDevice()
            return True
        self.CONN = None
        index = self.cmbDevice.currentText() if self.cmbDevice.count() > 0 else self.lblDevice.text()

        if index not in self.DEVICES:
            self.FindDevices()
            return None

        dev = self.DEVICES[index]
        port = dev.GetPort()
        max_baud = self._GetDeviceMaxBaudRate(dev)
        try:
            flashcarts = cast("Mapping[str, Mapping[str, Any]]", self.FLASHCARTS)
            ret = dev.Initialize(flashcarts, port=port, max_baud=max_baud)
        except Exception as exc:
            logger.exception("Failed to initialize the device on port {}", port)
            self.CONN = None
            self.lblDevice.setText(__("No connection."))
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                _format_device_initialization_error(exc),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return False
        if ret is False:
            self.CONN = None
            if self.cmbDevice.count() == 0:
                self.lblDevice.setText(__("No connection."))
            return False
        msg = ""
        if isinstance(ret, list):
            connected, msg = self._DisplayConnectionMessages(ret)
            if not connected:
                return False

        if dev.IsConnected():
            return self._complete_device_connection(dev, msg)

        return False

    def _SetRequestedMode(self, mode: PlatformMode | None) -> None:
        if mode == "DMG":
            self.optDMG.setChecked(True)
            self.SetMode()
        elif mode == "AGB":
            self.optAGB.setChecked(True)
            self.SetMode()

    def _ShowNoDevicesFoundMessage(self, messages: Sequence[str], first_run: bool) -> None:
        if messages:
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                "\n\n".join(messages),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return
        if first_run:
            return

        compatible_devices = []
        for hw_device in HW_DEVICES:
            device_name = getattr(hw_device.GbxDevice, "DEVICE_NAME", None)
            if not device_name or device_name in compatible_devices:
                continue
            if device_name == "Joey Jr":
                device_name += (
                    " ("
                    + c__(
                        "Joey Jr is compatible, but requires a firmware update",
                        "firmware update required",
                    )
                    + ")"
                )
            compatible_devices.append(device_name)

        compatible_devices_text = "".join(f"- {device_name}\n" for device_name in compatible_devices)
        msg = (
            __("No compatible devices found. Please ensure the device is connected properly.")
            + "\n\n"
            + __("Compatible devices:")
            + "\n"
            + compatible_devices_text
            + "\n"
            + __(
                "Troubleshooting advice:\n"
                "- Re-connect the device with different USB cables and ports\n"
                "- Avoid battery charging cables and passive USB hubs\n"
                "- Perform a Firmware Update",
            )
        )
        if platform.system() == "Darwin":
            msg += "\n\n" + __(
                "<b>For Joey Jr on macOS:</b>\nAn extra step is necessary to update the firmware: {url}",
                url='<a href="https://github.com/Lesserkuma/JoeyJr_FWUpdater">https://github.com/Lesserkuma/JoeyJr_FWUpdater</a>',
            )
        elif platform.system() == "Linux":
            msg += "\n\n" + __(
                "<b>For Linux users:</b>\nEnsure your user account has permissions to use the device. See the {readme} file for more information.",
                readme="<b>README.md</b>",
            )

        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Question,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=msg.replace("\n", "<br>"),
        )
        button_ok = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&OK"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        button_fwupdate = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&Firmware-Updater"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        msgbox.setDefaultButton(button_ok)
        msgbox.setEscapeButton(button_ok)
        msgbox.exec()
        if msgbox.clickedButton() == button_fwupdate:
            self.ShowFirmwareUpdateWindow()

    def FindDevices(
        self,
        connectToFirst: bool = False,
        port: str | None = None,
        mode: PlatformMode | None = None,
        firstRun: bool = False,
    ) -> bool:
        if self.CONN is not None:
            self.DisconnectDevice()
        self.DEVICES = {}
        self.lblDevice.setText(__("Searching..."))
        self.btnConnect.setEnabled(False)
        qt_app.processEvents()

        messages = []

        for hw_device in HW_DEVICES:
            ports = []
            while True:  # for finding other devices of the same type
                dev = hw_device.GbxDevice()
                max_baud = self._GetDeviceMaxBaudRate(dev)
                try:
                    ret = dev.Initialize(self.FLASHCARTS, port=port, max_baud=max_baud)
                    is_active = dev.CheckActive()
                except Exception as exc:
                    logger.exception("Failed to initialize a device while scanning port {}", port)
                    message = _format_device_initialization_error(exc)
                    if message not in messages:
                        messages.append(message)
                    self.CONN = None
                    break
                if ret is False or is_active is False:
                    self.CONN = None
                    break
                if isinstance(ret, list):
                    self._RecordDeviceInitializationMessages(ret, messages)

                if dev.GetPort() in ports:
                    break
                ports.append(dev.GetPort())

                if dev.IsConnected():
                    self.DEVICES[dev.GetFullNameExtended()] = dev
                    if dev.GetPort() in ports:
                        break

        for dev in self.DEVICES.values():
            dev.Close()

        self.cmbDevice.setStyleSheet("QComboBox { border: 0; margin: 0; padding: 0; max-width: 0px; }")

        if len(self.DEVICES) == 0:
            self._ShowNoDevicesFoundMessage(messages, firstRun)

            self.lblDevice.setText(__("No devices found."))
            self.lblDevice.setStyleSheet("")
            self.cmbDevice.clear()

            self.btnConnect.setEnabled(False)
        elif len(self.DEVICES) == 1 or (connectToFirst and len(self.DEVICES) > 1):
            self.lblDevice.setText(next(iter(self.DEVICES.keys())))
            self.lblDevice.setStyleSheet("")
            self.ConnectDevice()
            self.cmbDevice.clear()
            self.btnConnect.setEnabled(True)
        else:
            self.lblDevice.setText(__("Connect to:"))
            self.cmbDevice.clear()
            self.cmbDevice.addItems(list(self.DEVICES))
            self.cmbDevice.setCurrentIndex(0)
            self.cmbDevice.setStyleSheet("")
            self.btnConnect.setEnabled(True)

        self.btnConnect.setEnabled(True)

        if len(self.DEVICES) == 0:
            return False

        self._SetRequestedMode(mode)

        return True

    def _RecordDeviceInitializationMessages(
        self,
        connection_messages: list[ConfigMessage],
        messages: list[str],
    ) -> None:
        """Keep unique critical connection errors and mark the connection unavailable."""
        for connection_message in connection_messages:
            status, message = connection_message
            if status == 3 and isinstance(message, str) and message not in messages:
                messages.append(message)
                self.CONN = None

    def _GetLastDirectory(self, setting_name: str) -> str:
        """Return a saved directory, falling back to the user's Documents folder."""
        last_dir: str | None = self.SETTINGS.value(setting_name)
        if last_dir is None:
            last_dir = QtCore.QStandardPaths.writableLocation(
                QtCore.QStandardPaths.StandardLocation.DocumentsLocation,
            )
        return last_dir

    def AbortOperation(self) -> None:
        if "stresstest_running" in self.STATUS:
            del self.STATUS["stresstest_running"]
        self._device.AbortOperation()
        self.lblStatus4a.setText(__("Stopping... Please wait."))
        self.SetStatus4aResult("")

    def _OpenCameraBackup(self, *, check_box_default: bool) -> bool:
        skip_popup = str(self.SETTINGS.value("SkipCameraSavePopup", default="disabled")).lower() == "enabled"
        if skip_popup:
            return False
        is_camera_save = (
            self._device.GetMode() == "DMG"
            and self._device.INFO["mapper_raw"] == 252
            and (
                self._device.INFO["transferred"] == 0x20000
                or (
                    self._device.INFO["transferred"] == 0x100000
                    and "Unlicensed Photo!"
                    in DmgSaveTypes(index=self.cmbDMGHeaderSaveTypeResult.currentIndex()).GetString()
                )
            )
        )
        if not is_camera_save:
            return False

        check_box = _create_check_box(
            c__("Check Box (& = Keyboard Shortcut)", "&Don't show this message again"),
            checked=check_box_default,
        )
        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Question,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=__("Would you like to load your save data with the GB Camera Viewer now?"),
        )
        msgbox.setStandardButtons(
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        msgbox.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
        msgbox.setCheckBox(check_box)
        answer = msgbox.exec()
        if check_box.isChecked():
            self.SETTINGS.setValue("SkipCameraSavePopup", "enabled")
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return False

        camera_window = PocketCameraWindow(
            self,
            icon=self.windowIcon(),
            file=self._device.INFO["last_path"],
            config_path=AppContext.CONFIG_PATH,
            app_path=AppContext.APP_PATH,
        )
        self.CAMWIN = camera_window
        camera_window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, on=True)
        camera_window.setModal(True)
        camera_window.run()
        return True

    def _FinishTiming(self) -> tuple[float | None, str, str | None]:
        time_elapsed = None
        message = ""
        speed = None
        if "time_start" in self.STATUS and self.STATUS["time_start"] > 0:
            time_elapsed = time.time() - self.STATUS["time_start"]
            message = "\n\n" + __(
                "Total time elapsed: {elapsed}",
                elapsed=Formatter.progress_time(time_elapsed, as_float=True),
            )
            if "transferred" in self._device.INFO and time_elapsed > 0:
                speed = format_decimal(
                    (self._device.INFO["transferred"] / 1024.0) / time_elapsed,
                    precision=2,
                ) + __(" KiB/s")
            self.STATUS["time_start"] = 0
        return time_elapsed, message, speed

    @staticmethod
    def _FormatBrokenSectors(broken_sectors: list[list[int]]) -> tuple[str, int]:
        sectors = ""
        sector_count = 0
        for sector in broken_sectors:
            sector_count += 1
            if sector_count > 10:
                sectors += (
                    c__(
                        "Shortened list of Broken Sectors (e.g. 0x0000~0x07FF and others)",
                        "and others",
                    )
                    + "  "
                )
                break
            sectors += f"0x{sector[0]:X}~0x{sector[0] + sector[1] - 1:X}, "
        return sectors[:-2], sector_count

    def _FinishFlashROM(self, msgbox: QtWidgets.QMessageBox, elapsed_message: str) -> bool:
        if "broken_sectors" in self._device.INFO:
            broken_sectors: list[list[int]] = self._device.INFO["broken_sectors"]
            sectors, sector_count = self._FormatBrokenSectors(broken_sectors)
            message = ___(
                "The ROM was written completely, but verification of written data failed in the following sector: {sectors}.",
                "The ROM was written completely, but verification of written data failed in the following sectors: {sectors}.",
                n=sector_count,
                sectors=sectors,
            )
            if "verify_error_params" in self._device.INFO:
                cart_types = []
                if self._device.GetMode() == "DMG":
                    cart_types = self._device.GetSupportedCartridgesDMG()[0]
                elif self._device.GetMode() == "AGB":
                    cart_types = self._device.GetSupportedCartridgesAGB()[0]
                cart_type_index = self._device.INFO["dump_info"]["cart_type"]
                cart_type_text = (
                    f" ({cart_types[cart_type_index]:s})"
                    if isinstance(cart_type_index, int) and 0 <= cart_type_index < len(cart_types)
                    else ""
                )
                message += (
                    "\n\n"
                    + __(
                        "Troubleshooting advice:\n"
                        "- Clean cartridge contacts\n"
                        "- Check soldering if it's a DIY cartridge\n"
                        "- Avoid passive USB hubs and try different USB ports/cables\n"
                        "- Check flashcart profile selection",
                    )
                    + cart_type_text
                    + "\n"
                    + __(
                        "- Check cartridge ROM storage size (at least {rom_size} is required)",
                        rom_size=Formatter.file_size(self._device.INFO["verify_error_params"]["rom_size"]),
                    )
                )
                if "mapper_selection_type" in self._device.INFO["verify_error_params"]:
                    selection_type = self._device.INFO["verify_error_params"]["mapper_selection_type"]
                    if selection_type == 1:  # manual
                        mapper_source = c__("Mapper Type", "manual selection")
                    elif selection_type == 2:  # forced by cart type
                        mapper_source = c__("Mapper Type", "forced by selected flashcart profile")
                    else:
                        mapper_source = ""
                    if mapper_source:
                        message += (
                            "\n"
                            + __("- Check mapper type used:")
                            + " "
                            + self._device.INFO["verify_error_params"]["mapper_name"]
                            + f" ({mapper_source})"
                        )
                    if (
                        self._device.INFO["verify_error_params"]["rom_size"]
                        > self._device.INFO["verify_error_params"]["mapper_max_size"]
                    ):
                        message += "\n" + __(
                            "- Check mapper type ROM size limit: likely up to {max_size}",
                            max_size=Formatter.file_size(
                                self._device.INFO["verify_error_params"]["mapper_max_size"],
                            ),
                        )
            message += "\n\n" + __("Do you want to write the sectors again that failed verification?")
            answer = QtWidgets.QMessageBox.warning(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                message,
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.Yes,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                args = self.STATUS["args"]
                args.update({"flash_sectors": self._device.INFO["broken_sectors"]})
                self._device.FlashROM(fncSetProgress=self.PROGRESS.SetProgress, args=args)
                return True

        self._device.INFO["last_action"] = 0
        self.lblStatus4a.setText(__("Done!"))
        message = (
            __("The ROM was written and verified successfully!")
            if self.PROGRESS.PROGRESS.get("verified")
            else __("ROM writing complete!")
        )
        msgbox.setText(message + elapsed_message)
        msgbox.exec()

        if (
            self._device.GetMode() == "AGB"
            and "Batteryless SRAM" in AgbSaveTypes().GetStringList()[self.cmbAGBSaveTypeResult.currentIndex()]
        ):
            cart_type_index = self.cmbAGBCartridgeTypeResult.currentIndex()
            save_type_index = self.cmbAGBSaveTypeResult.currentIndex()
            batteryless_sram = self._device.INFO["dump_info"].get("batteryless_sram")
            self.ReadCartridge(resetStatus=False)
            self.cmbAGBCartridgeTypeResult.setCurrentIndex(cart_type_index)
            self.cmbAGBSaveTypeResult.setCurrentIndex(save_type_index)
            if batteryless_sram is not None and "batteryless_sram" in self._device.INFO["dump_info"]:
                self._device.INFO["dump_info"]["batteryless_sram"] = batteryless_sram
        else:
            self.ReadCartridge(resetStatus=False)
        return False

    def _FinishRAMBackup(
        self,
        msgbox: QtWidgets.QMessageBox,
        elapsed_message: str,
        *,
        check_box_default: bool,
    ) -> bool:
        self.lblStatus4a.setText(__("Done!"))
        self._device.INFO["last_action"] = 0
        if self._OpenCameraBackup(check_box_default=check_box_default):
            return True

        button_open_dir = None
        if "last_path" in self._device.INFO:
            button_open_dir = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "Open Fol&der"),
                QtWidgets.QMessageBox.ButtonRole.ActionRole,
            )
        msgbox.setText(__("The save data backup is complete!") + elapsed_message)
        msgbox.exec()
        if button_open_dir is not None and msgbox.clickedButton() == button_open_dir:
            self.OpenPath(self._device.INFO["last_path"], select_file=True)
        return False

    def _FinishRAMRestore(self, msgbox: QtWidgets.QMessageBox, elapsed_message: str) -> None:
        self.lblStatus4a.setText(__("Done!"))
        self._device.INFO["last_action"] = 0
        if self._device.INFO.get("save_erase"):
            msg_text = __("The save data was erased.")
            del self._device.INFO["save_erase"]
        elif self.PROGRESS.PROGRESS.get("verified"):
            msg_text = __("The save data was written and verified successfully!")
        else:
            msg_text = __("Save data writing complete!")
        msgbox.setText(msg_text + elapsed_message)
        msgbox.exec()

    def _PrepareROMBackupReport(
        self,
        msgbox: QtWidgets.QMessageBox,
        time_elapsed: float | None,
        speed: str | None,
    ) -> tuple[str | Literal[False], str, bool, QtWidgets.QPushButton | None, QtWidgets.QPushButton]:
        dumpinfo_file = ""
        generate_report = str(self.SETTINGS.value("GenerateDumpReports", default="disabled")).lower() == "enabled"
        dump_report = self._device.GetDumpReport()
        if dump_report is not False:
            if time_elapsed is not None and speed is not None:
                self.lblStatus2aResult.setText(speed)
                dump_report = dump_report.replace(
                    "%TRANSFER_RATE%",
                    "{:.2f}".format((self._device.INFO["transferred"] / 1024.0) / time_elapsed) + " KiB/s",
                )
                dump_report = dump_report.replace(
                    "%TIME_ELAPSED%",
                    Formatter.progress_time(time_elapsed, localized=False),
                )
            else:
                dump_report = dump_report.replace("%TRANSFER_RATE%", "N/A")
                dump_report = dump_report.replace("%TIME_ELAPSED%", "N/A")
            dumpinfo_file = str(Path(self.STATUS["last_path"]).with_suffix(".txt"))

        button_dump_report = None
        if dump_report is not False and dumpinfo_file != "" and generate_report:
            try:
                with Path(dumpinfo_file).open("wb") as f:
                    f.write(bytearray([0xEF, 0xBB, 0xBF]))  # UTF-8 BOM
                    f.write(dump_report.encode("UTF-8"))
                button_dump_report = msgbox.addButton(
                    c__("Button (& = Keyboard Shortcut)", "Open Dump &Report"),
                    QtWidgets.QMessageBox.ButtonRole.ActionRole,
                )
            except Exception as e:
                print(__("Error:") + f" {e!s:s}")
        else:
            button_dump_report = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "Generate Dump &Report"),
                QtWidgets.QMessageBox.ButtonRole.ActionRole,
            )

        button_open_dir = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "Open Fol&der"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        return dump_report, dumpinfo_file, generate_report, button_dump_report, button_open_dir

    def _HandleROMBackupReportAction(
        self,
        msgbox: QtWidgets.QMessageBox,
        report: tuple[str | Literal[False], str, bool, QtWidgets.QPushButton | None, QtWidgets.QPushButton],
    ) -> None:
        dump_report, dumpinfo_file, report_generated, button_dump_report, button_open_dir = report
        if msgbox.clickedButton() == button_dump_report:
            if dump_report is not False and dumpinfo_file:
                try:
                    if not report_generated:
                        with Path(dumpinfo_file).open("wb") as f:
                            f.write(bytearray([0xEF, 0xBB, 0xBF]))  # UTF-8 BOM
                            f.write(dump_report.encode("UTF-8"))
                    self.OpenPath(dumpinfo_file)
                except Exception as e:
                    print(f"Error: {e!s:s}")
        elif msgbox.clickedButton() == button_open_dir:
            self.OpenPath(self.STATUS["last_path"], select_file=True)

    def _PrepareOperationFinish(self) -> None:
        if self.lblStatus2aResult.text() == __("Pending..."):
            self.lblStatus2aResult.setText("-")
        self.SetStatus4aResult("")
        self.grpDMGCartridgeInfo.setEnabled(True)
        self.grpAGBCartridgeInfo.setEnabled(True)
        self.grpActions.setEnabled(True)
        self.mnuTools.setEnabled(True)
        self.mnuConfig.setEnabled(True)
        self.mnuLanguage.setEnabled(True)
        self.btnCancel.setEnabled(False)

    def _FinishAGBROMBackup(self, msgbox: QtWidgets.QMessageBox, elapsed_message: str) -> None:
        if "db" in self._device.INFO and self._device.INFO["db"] is not None:
            if self._device.INFO["db"]["rc"] == self._device.INFO.get("file_crc32"):
                self.lblAGBHeaderROMChecksumResult.setText(
                    c__("Game Data", "Valid") + " (0x{:06X})".format(self._device.INFO["db"]["rc"]),
                )
                self.lblAGBHeaderROMChecksumResult.setStyleSheet("QLabel { color: green; }")
                self.lblStatus4a.setText(__("Done!"))
                msg = __("The ROM backup is complete and the checksum was verified successfully!")
                msgbox.setText(msg + elapsed_message)
                msgbox.exec()
            else:
                self.lblAGBHeaderROMChecksumResult.setText(
                    c__("Game Data", "Invalid")
                    + " (0x{:06X}≠0x{:06X})".format(
                        self._device.INFO.get("file_crc32", 0),
                        self._device.INFO["db"]["rc"],
                    ),
                )
                self.lblAGBHeaderROMChecksumResult.setStyleSheet("QLabel { color: red; }")
                self.lblStatus4a.setText(__("Done!"))
                msg = __("The ROM backup is complete, but the checksum doesn't match the known database entry.")
                if self._device.INFO["loop_detected"] is not False:
                    msg += "\n\n" + __(
                        "A data loop was detected in the ROM backup at position {pos} ({size}). This may indicate a bad dump or overdump.",
                        pos="0x{:X}".format(self._device.INFO["loop_detected"]),
                        size=Formatter.file_size(self._device.INFO["loop_detected"], as_int=True),
                    )
                else:
                    msg += " " + __(
                        "This may indicate a bad dump, however this can be normal for some reproduction cartridges, unlicensed games, prototypes, patched games and intentional overdumps.",
                    )
                msgbox.setText(msg + elapsed_message)
                msgbox.setIcon(QtWidgets.QMessageBox.Icon.Warning)
                msgbox.exec()
        else:
            self.lblAGBHeaderROMChecksumResult.setText("0x{:06X}".format(self._device.INFO.get("file_crc32", 0)))
            self.lblAGBHeaderROMChecksumResult.setStyleSheet(self.DEFAULT_STYLESHEET)
            self.lblStatus4a.setText(__("Done!"))
            msg = __(
                "The ROM backup is complete! As there is no known checksum for this ROM in the database, verification was skipped.",
            )
            if self._device.INFO["loop_detected"] is not False:
                msg += "\n\n" + __(
                    "A data loop was detected in the ROM backup at position {pos} ({size}). This may indicate a bad dump or overdump.",
                    pos="0x{:X}".format(self._device.INFO["loop_detected"]),
                    size=Formatter.file_size(self._device.INFO["loop_detected"], as_int=True),
                )
                msgbox.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            msgbox.setText(msg + elapsed_message)
            msgbox.exec()

    def _CompleteOperationFinish(self, *, skip_finish_message: bool) -> None:
        if skip_finish_message:
            self.SETTINGS.setValue("SkipFinishMessage", "enabled")
        self.SetProgressBars(min=0, max=1, value=1)

    def _RetryBackupWithGmmc1(self) -> None:
        self.cmbDMGHeaderMapperResult.setCurrentIndex(ConvertMapperToMapperType(0x105)[2])
        self.cmbDMGHeaderROMSizeResult.setCurrentIndex(5)
        cart_type = 0
        cart_types = self._device.GetSupportedCartridgesDMG()
        for i in range(len(cart_types[0])):
            if "dmg-mmsa-jpn" in cart_types[1][i]:
                self.cmbDMGCartridgeTypeResult.setCurrentIndex(i)
                cart_type = i
        self.STATUS["args"]["mbc"] = 0x105
        self.STATUS["args"]["rom_size"] = 1048576
        self.STATUS["args"]["cart_type"] = cart_type
        self.STATUS["time_start"] = time.time()
        QtCore.QTimer.singleShot(
            1,
            lambda: [
                self._device.BackupROM(
                    fncSetProgress=self.PROGRESS.SetProgress,
                    args=self.STATUS["args"],
                ),
            ],
        )

    def FinishOperation(self) -> None:
        self._PrepareOperationFinish()

        dontShowAgain = str(self.SETTINGS.value("SkipFinishMessage", default="disabled")).lower() == "enabled"
        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Information,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=__("Operation complete!"),
            standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
        )

        time_elapsed, msg_te, speed = self._FinishTiming()

        if self._device.INFO["last_action"] == 1:  # Backup ROM
            self._device.INFO["last_action"] = 0
            report = self._PrepareROMBackupReport(
                msgbox,
                time_elapsed,
                speed,
            )

            if self._device.GetMode() == "DMG":
                if self._FinishDMGROMBackup(msgbox, msg_te):
                    return
            elif self._device.GetMode() == "AGB":
                self._FinishAGBROMBackup(msgbox, msg_te)

            self._HandleROMBackupReportAction(msgbox, report)

        elif self._device.INFO["last_action"] == 2:  # Backup RAM
            if self._FinishRAMBackup(msgbox, msg_te, check_box_default=dontShowAgain):
                return

        elif self._device.INFO["last_action"] == 3:  # Restore RAM
            self._FinishRAMRestore(msgbox, msg_te)

        elif self._device.INFO["last_action"] == 4:  # Flash ROM
            if self._FinishFlashROM(msgbox, msg_te):
                return

        elif self._device.INFO["last_action"] == 6:  # Detect Cartridge
            self.lblStatus4a.setText(__("Ready."))
            self._device.INFO["last_action"] = 0
            self.FinishDetectCartridge(self._device.INFO.get("detect_cart", False))

        else:
            self.lblStatus4a.setText(__("Ready."))
            self._device.INFO["last_action"] = 0

        # if self.CONN is not None and self._device.CanPowerCycleCart(): self._device.CartPowerOff()
        self._CompleteOperationFinish(skip_finish_message=dontShowAgain)

    def _FinishDMGROMBackup(self, msgbox: QtWidgets.QMessageBox, msg_te: str) -> bool:
        """Show the DMG checksum result and report whether a retry started."""
        if self._device.INFO.get("rom_checksum", -1) == self._device.INFO.get("rom_checksum_calc", -2):
            self.lblDMGHeaderROMChecksumResult.setText(
                c__("Game Data", "Valid") + " (0x{:04X})".format(self._device.INFO.get("rom_checksum", 0)),
            )
            self.lblDMGHeaderROMChecksumResult.setStyleSheet("QLabel { color: green; }")
            self.lblStatus4a.setText(__("Done!"))
            msgbox.setText(__("The ROM backup is complete and the checksum was verified successfully!") + msg_te)
            msgbox.exec()
            return False

        self.lblStatus4a.setText(__("Done!"))
        if "mapper_raw" in self._device.INFO and self._device.INFO["mapper_raw"] in (0x202, 0x203, 0x205):
            msgbox.setText(__("The ROM backup is complete.") + msg_te)
            msgbox.exec()
            return False

        self.lblDMGHeaderROMChecksumResult.setText(
            c__("Game Data", "Invalid")
            + " (0x{:04X}≠0x{:04X})".format(
                self._device.INFO.get("rom_checksum_calc", 0),
                self._device.INFO.get("rom_checksum", 0),
            ),
        )
        self.lblDMGHeaderROMChecksumResult.setStyleSheet("QLabel { color: red; }")
        message, retry_button = self._DMGROMBackupFailureMessage(msgbox)
        msgbox.setText(message + msg_te)
        msgbox.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        msgbox.exec()
        if msgbox.clickedButton() == retry_button and self.CheckDeviceAlive():
            self._RetryBackupWithGmmc1()
            return True
        return False

    def _DMGROMBackupFailureMessage(self, msgbox: QtWidgets.QMessageBox) -> tuple[str, object | None]:
        """Build the warning shown when a DMG backup checksum is invalid."""
        message = __("The ROM was dumped, but the checksum is not correct.")
        retry_button = None
        loop_detected = self._device.INFO["loop_detected"]
        if loop_detected is not False:
            message += "\n\n" + __(
                "A data loop was detected in the ROM backup at position {pos} ({size}). This may indicate a bad dump or overdump.",
                pos=f"0x{loop_detected:X}",
                size=Formatter.file_size(loop_detected, as_int=True),
            )
        else:
            message += (
                " "
                + __(
                    "This may indicate a bad dump, however this can be normal for some reproduction cartridges, unlicensed games, prototypes, patched games and intentional overdumps.",
                )
                + " "
                + c__(
                    "Advice when ROM backup was bad",
                    "You can also try to change the read mode in the options.",
                )
            )
            if self._device.GetMode() == "DMG" and self.cmbDMGHeaderMapperResult.currentText() == "MBC1":
                message += "\n\n" + __(
                    "If this is a “{gb_memory_cartridge}”, try the “{label}” option.",
                    gb_memory_cartridge="GB-Memory Cartridge",
                    label=__("Retry with {mapper}", mapper="G-MMC1"),
                )
                retry_button = msgbox.addButton(
                    __("Retry with {mapper}", mapper="G-MMC1"),
                    QtWidgets.QMessageBox.ButtonRole.ActionRole,
                )
        return message, retry_button

    def DMGMapperTypeChanged(self, index: int) -> None:
        if index in (-1, 0):
            return

    def SetDMGMapperResult(self, cart_type: Mapping[str, Any]) -> None:
        mbc = 0
        if "mbc" in cart_type:
            if isinstance(cart_type["mbc"], int):
                mbc = cart_type["mbc"]
            elif self.cmbDMGHeaderMapperResult.currentIndex() > 0:
                mbc = ConvertMapperTypeToMapper(self.cmbDMGHeaderMapperResult.currentIndex())
            self.cmbDMGHeaderMapperResult.setCurrentIndex(ConvertMapperToMapperType(mbc)[2])

    def CartridgeTypeChanged(self, index: int) -> None:
        self.STATUS["cart_type"] = {}
        if index in (-1, 0):
            return
        if "detect_cartridge_args" in self.STATUS:
            return
        if self._device.GetMode() == "DMG":
            cart_types = self._device.GetSupportedCartridgesDMG()
            profile = cart_types[1][index]
            if isinstance(profile, dict):
                flash_size = profile.get("flash_size")
                if "dmg-mmsa-jpn" not in profile and isinstance(flash_size, int) and flash_size in RomSizes():
                    size_index = RomSizes().GetIndex(flash_size)
                    if size_index is not None:
                        self.cmbDMGHeaderROMSizeResult.setCurrentIndex(size_index)
                self.STATUS["cart_type"] = profile
                self.SetDMGMapperResult(profile)

        elif self._device.GetMode() == "AGB":
            cart_types = self._device.GetSupportedCartridgesAGB()
            profile = cart_types[1][index]
            if isinstance(profile, dict):
                flash_size = profile.get("flash_size")
                if isinstance(flash_size, int) and flash_size in RomSizes():
                    size_index = RomSizes().GetIndex(flash_size)
                    if size_index is not None:
                        self.cmbAGBHeaderROMSizeResult.setCurrentIndex(size_index)
                self.STATUS["cart_type"] = profile

    def CheckHeader(self) -> bool:
        if "dump_info" not in self._device.INFO or "header" not in self._device.INFO["dump_info"]:
            return True
        data = self._device.INFO["dump_info"]["header"]
        if (
            not (self._device.GetMode() == "DMG" and data["mapper_raw"] in (0x203, 0x204, 0x205))
            and not data["logo_correct"]
            and not data["header_checksum_correct"]
            and not data["empty"]
        ):
            msg = (
                __(
                    "ROM header checksum and boot logo checks failed. Please ensure that the cartridge contacts are clean.",
                )
                + "\n\n"
                + __("Do you still want to continue?")
            )
            answer = QtWidgets.QMessageBox.question(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                msg,
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.No:
                return False
        return True

    def BackupROM(self) -> None:
        if not self.CheckDeviceAlive():
            return
        if not self.CheckHeader():
            return

        mbc = ConvertMapperTypeToMapper(self.cmbDMGHeaderMapperResult.currentIndex())

        rom_size = 0
        cart_type = 0
        path = generate_filename(
            mode=self._device.GetMode(),
            header=self._device.INFO,
            settings=self.SETTINGS,
        )
        if self._device.GetMode() == "DMG":
            setting_name = "LastDirRomDMG"
            last_dir = self.SETTINGS.value(setting_name)
            if last_dir is None:
                last_dir = QtCore.QStandardPaths.writableLocation(
                    QtCore.QStandardPaths.StandardLocation.DocumentsLocation,
                )

            path = QtWidgets.QFileDialog.getSaveFileName(
                self,
                __("Backup ROM"),
                str(Path(last_dir) / path),
                __("Game Boy ROM File")
                + " ("
                + " ".join("*" + e for e in ROM_EXTS_DMG_READ)
                + ");;"
                + __("All Files")
                + " (*.*)",
            )[0]
            cart_type = self.cmbDMGCartridgeTypeResult.currentIndex()
            rom_size = RomSizes().GetSize(self.cmbDMGHeaderROMSizeResult.currentIndex())

        elif self._device.GetMode() == "AGB":
            setting_name = "LastDirRomAGB"
            last_dir = self.SETTINGS.value(setting_name)
            if last_dir is None:
                last_dir = QtCore.QStandardPaths.writableLocation(
                    QtCore.QStandardPaths.StandardLocation.DocumentsLocation,
                )

            rom_size = RomSizes().GetSize(self.cmbAGBHeaderROMSizeResult.currentIndex())
            path = QtWidgets.QFileDialog.getSaveFileName(
                self,
                __("Backup ROM"),
                str(Path(last_dir) / path),
                __("Game Boy Advance ROM File")
                + " ("
                + " ".join("*" + e for e in ROM_EXTS_AGB)
                + ");;"
                + __("All Files")
                + " (*.*)",
            )[0]
            cart_type = self.cmbAGBCartridgeTypeResult.currentIndex()
        else:
            return

        if path == "":
            return

        self.SETTINGS.setValue(setting_name, str(Path(path).parent))
        self.lblDMGHeaderROMChecksumResult.setStyleSheet(self.DEFAULT_STYLESHEET)
        self.lblAGBHeaderROMChecksumResult.setStyleSheet(self.DEFAULT_STYLESHEET)

        self.grpDMGCartridgeInfo.setEnabled(False)
        self.grpAGBCartridgeInfo.setEnabled(False)
        self.grpActions.setEnabled(False)
        self.mnuTools.setEnabled(False)
        self.mnuConfig.setEnabled(False)
        self.mnuLanguage.setEnabled(False)
        self.lblStatus4a.setText(__("Preparing..."))
        qt_app.processEvents()
        args = {
            "path": path,
            "mbc": mbc,
            "rom_size": rom_size,
            "agb_rom_size": rom_size,
            "fast_read_mode": True,
            "cart_type": cart_type,
            "settings": self.SETTINGS,
        }
        self._device.BackupROM(fncSetProgress=self.PROGRESS.SetProgress, args=args)
        self.grpStatus.setTitle(__("Transfer Status"))
        self.STATUS["time_start"] = time.time()
        self.STATUS["last_path"] = path
        self.STATUS["args"] = args

    def _ConfirmFlashROMPath(self, path: str) -> bool:
        extension = Path(path).suffix
        if extension.lower() == ".isx":
            text = (
                __(
                    "The following ISX file will now be converted to a regular ROM file and then written to the flash cartridge:",
                )
                + "\n"
                + path
            )
        else:
            text = __("The following ROM file will now be written to the flash cartridge:") + "\n" + path
        answer = QtWidgets.QMessageBox.question(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text,
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Ok,
        )
        return answer != QtWidgets.QMessageBox.StandardButton.Cancel

    def _PrepareFlashCartSelection(
        self,
        dpath: str,
    ) -> tuple[PlatformMode, str, str, str, list[Any], int, dict[str, Any]] | None:
        """Resolve the device mode and selected flash-cart profile."""
        if not self.CheckDeviceAlive():
            return None

        mode = self._device.GetMode()
        if mode not in ("DMG", "AGB"):
            return None
        path = ""
        if dpath != "":
            if not self._ConfirmFlashROMPath(dpath):
                if "detected_cart_type" in self.STATUS:
                    del self.STATUS["detected_cart_type"]
                return None
            path = dpath

        if mode == "DMG":
            setting_name = "LastDirRomDMG"
            carts = self._device.GetSupportedCartridgesDMG()[1]
            cart_type = self.cmbDMGCartridgeTypeResult.currentIndex()
        else:
            setting_name = "LastDirRomAGB"
            carts = self._device.GetSupportedCartridgesAGB()[1]
            cart_type = self.cmbAGBCartridgeTypeResult.currentIndex()
        last_dir = self._GetLastDirectory(setting_name)
        if cart_type == 0:
            if "detected_cart_type" not in self.STATUS:
                self.STATUS["detected_cart_type"] = ""
            if self.STATUS["detected_cart_type"] == "":
                self.STATUS["detected_cart_type"] = "WAITING_FLASH"
                self.STATUS["detect_cartridge_args"] = {"dpath": path}
                self.STATUS["can_skip_message"] = True
                self.DetectCartridge(checkSaveType=False)
                return None
            cart_type = self.STATUS["detected_cart_type"]
            self.STATUS.pop("detected_cart_type", None)

            if cart_type is False:  # clicked Cancel button
                return None
            if cart_type is None or cart_type == 0 or not isinstance(cart_type, int):
                QtWidgets.QMessageBox.critical(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    __("A compatible flashcart profile could not be auto-detected."),
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
                return None

            if mode == "DMG":
                self.cmbDMGCartridgeTypeResult.setCurrentIndex(cart_type)
            else:
                self.cmbAGBCartridgeTypeResult.setCurrentIndex(cart_type)

        self.STATUS.pop("detected_cart_type", None)

        cart_profile = carts[cart_type]
        if not isinstance(cart_profile, dict):
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("The selected flashcart profile is invalid."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return None
        cart_profile = cast("dict[str, Any]", cart_profile)

        return mode, path, setting_name, last_dir, carts, cart_type, cart_profile

    def _LoadFlashROMFile(
        self,
        path: str,
        setting_name: str,
        cart_profile: dict[str, Any],
    ) -> bytearray | None:
        rom_path = Path(path)
        self.SETTINGS.setValue(setting_name, str(rom_path.parent))
        rom_size = rom_path.stat().st_size
        if rom_size == 0:
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("The selected ROM file is empty."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return None
        if rom_size > 0x20000000:  # reject too large files to avoid exploding RAM
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __(
                    "ROM files bigger than 512{mib} are not supported.",
                    mib=__(" MiB"),
                ),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return None

        with rom_path.open("rb") as file:
            if rom_path.suffix.lower() == ".isx":
                buffer = from_isx(bytearray(file.read()))
            else:
                buffer = bytearray(file.read(0x1000))
        flash_size = cart_profile.get("flash_size")
        if isinstance(flash_size, int) and rom_size > flash_size:
            msg = __(
                "The selected flashcart profile seems to support ROMs that are up to {max_size} in size, but the file you selected is {file_size}.",
                max_size=Formatter.file_size(flash_size),
                file_size=Formatter.file_size(rom_size),
            )
            msg += " " + __(
                "You can still give it a try, but it's possible that it's too large which may cause the ROM writing to fail.",
            )
            answer = QtWidgets.QMessageBox.warning(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                msg,
                QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
                return None
        return buffer

    def _ConfirmFlashMapper(
        self,
        mode: PlatformMode,
        buffer: bytearray,
        mbc: int,
        cart_profile: Mapping[str, Any],
    ) -> tuple[bool, int, Mapping[str, Any]]:
        if mode == "AGB":
            return True, mbc, RomFileAGB(buffer).GetHeader()

        header = RomFileDMG(buffer).GetHeader()
        if compare_mbc(header["mapper_raw"], mbc):
            return True, mbc, header
        selected_mbc = get_mbc_name(mbc)
        rom_mbc = get_mbc_name(header["mapper_raw"])
        compatible_mbc = {
            "None",
            "MBC2",
            "MBC3",
            "MBC30",
            "MBC5",
            "MBC7",
            "MAC-GBD",
            "G-MMC1",
            "HuC-1",
            "HuC-3",
            "Unlicensed MBCX Mapper",
            "Unlicensed 256M Multi Cart Mapper",
        }
        compatible_pair = rom_mbc == "None" or {selected_mbc, rom_mbc} == {"MBC1", "G-MMC1"}
        if compatible_pair or (selected_mbc in compatible_mbc and rom_mbc in compatible_mbc):
            return True, mbc, header

        if cart_profile.get("mbc") == "manual":
            msg_text = (
                __(
                    "The ROM file you selected uses a different mapper type than your current selection. What mapper should be used when writing the ROM?",
                )
                + "\n\n"
                + __("Selected mapper type:")
                + " "
                + selected_mbc
                + "\n"
                + __("ROM mapper type:")
                + " "
                + rom_mbc
            )
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Warning,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=msg_text,
            )
            if mbc == 0:
                selected_mbc = "MBC5"
            button_selected = msgbox.addButton(selected_mbc, QtWidgets.QMessageBox.ButtonRole.ActionRole)
            button_rom = msgbox.addButton(rom_mbc, QtWidgets.QMessageBox.ButtonRole.ActionRole)
            button_cancel = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "&Cancel"),
                QtWidgets.QMessageBox.ButtonRole.RejectRole,
            )
            msgbox.setDefaultButton(button_selected)
            msgbox.setEscapeButton(button_cancel)
            msgbox.exec()
            if msgbox.clickedButton() == button_cancel:
                return False, mbc, header
            if msgbox.clickedButton() == button_rom:
                mbc = header["mapper_raw"]
            return True, mbc, header

        if selected_mbc == "None":
            selected_mbc = c__("Mapper Type", c__("Mapper Type", "None/Unknown"))
        msg_text = (
            __(
                "Warning: The ROM file you selected uses a different mapper type than your flashcart profile. The ROM file may be incompatible with your cartridge.",
            )
            + "\n\n"
            + __("Selected mapper type:")
            + " "
            + selected_mbc
            + "\n"
            + __("ROM mapper type:")
            + " "
            + rom_mbc
        )
        answer = QtWidgets.QMessageBox.warning(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            msg_text,
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        return answer != QtWidgets.QMessageBox.StandardButton.Cancel, mbc, header

    def _ConfirmFlashBootLogo(
        self,
        mode: PlatformMode,
        header: Mapping[str, Any],
        mbc: int,
    ) -> tuple[bool, bool | bytearray]:
        if header["logo_correct"] or (mode == "DMG" and mbc in (0x203, 0x205)):
            return True, False

        msg_text = __(
            "Warning: The ROM file you selected will not boot on actual hardware due to invalid boot logo data.",
        )
        bootlogo = None
        if mode == "DMG":
            bootlogo_path = Path(AppContext.CONFIG_PATH) / "bootlogo_dmg.bin"
            if bootlogo_path.exists():
                with bootlogo_path.open("rb") as f:
                    bootlogo = bytearray(f.read(0x30))
        elif mode == "AGB":
            bootlogo_path = Path(AppContext.CONFIG_PATH) / "bootlogo_agb.bin"
            if bootlogo_path.exists():
                with bootlogo_path.open("rb") as f:
                    bootlogo = bytearray(f.read(0x9C))

        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Warning,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=msg_text,
        )
        if bootlogo is not None:
            button_fix = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "&Fix and Continue"),
                QtWidgets.QMessageBox.ButtonRole.ActionRole,
            )
            msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "Continue &without fixing"),
                QtWidgets.QMessageBox.ButtonRole.ActionRole,
            )
            button_cancel = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "&Cancel"),
                QtWidgets.QMessageBox.ButtonRole.RejectRole,
            )
            msgbox.setDefaultButton(button_fix)
            msgbox.setEscapeButton(button_cancel)
            msgbox.exec()
            if msgbox.clickedButton() == button_fix:
                return True, bootlogo
            return msgbox.clickedButton() != button_cancel, False

        dprint("Couldn't find boot logo file in configuration folder")
        msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&OK"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        button_cancel = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&Cancel"),
            QtWidgets.QMessageBox.ButtonRole.RejectRole,
        )
        msgbox.setDefaultButton(button_cancel)
        msgbox.setEscapeButton(button_cancel)
        return msgbox.exec() != QtWidgets.QMessageBox.StandardButton.Cancel, False

    def _ConfirmFlashHeaderChecksum(
        self,
        mode: PlatformMode,
        header: Mapping[str, Any],
        mbc: int,
    ) -> bool | None:
        if header["header_checksum_correct"] or not (mode == "AGB" or (mode == "DMG" and mbc not in (0x203, 0x205))):
            return False

        msg_text = __(
            "Warning: The ROM file you selected will not boot on actual hardware due to an invalid header checksum (expected {calc} instead of {actual}).",
            calc=f"0x{header['header_checksum_calc']:02X}",
            actual=f"0x{header['header_checksum']:02X}",
        )
        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Warning,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=msg_text,
        )
        button_fix = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&Fix and Continue"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        button_continue = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "Continue &without fixing"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        button_cancel = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&Cancel"),
            QtWidgets.QMessageBox.ButtonRole.RejectRole,
        )
        msgbox.setDefaultButton(button_fix)
        msgbox.setEscapeButton(button_cancel)
        msgbox.exec()
        if msgbox.clickedButton() == button_fix:
            return True
        if msgbox.clickedButton() == button_cancel:
            return None
        if msgbox.clickedButton() == button_continue:
            return False
        return False

    def _ConfirmFlashHeaderRepairs(
        self,
        mode: PlatformMode,
        buffer: bytearray,
        mbc: int,
        cart_profile: dict[str, Any],
    ) -> tuple[int, bool | bytearray, bool] | None:
        continue_write, mbc, header = self._ConfirmFlashMapper(mode, buffer, mbc, cart_profile)
        if not continue_write:
            return None

        continue_write, fix_bootlogo = self._ConfirmFlashBootLogo(mode, header, mbc)
        if not continue_write:
            return None

        fix_header = self._ConfirmFlashHeaderChecksum(mode, header, mbc)
        if fix_header is None:
            return None
        return mbc, fix_bootlogo, fix_header

    def FlashROM(self, dpath: str = "") -> None:
        selection = self._PrepareFlashCartSelection(dpath)
        if selection is None:
            return
        mode, path, setting_name, last_dir, carts, cart_type, cart_profile = selection
        just_erase = False
        buffer = bytearray()

        if mode == "DMG":
            self.SetDMGMapperResult(cart_profile)
            mbc = ConvertMapperTypeToMapper(self.cmbDMGHeaderMapperResult.currentIndex())
        else:
            mbc = 0

        if path == "":
            file_type_name, rom_extensions = {
                "DMG": ("Game Boy ROM File", ROM_EXTS_DMG),
                "AGB": ("Game Boy Advance ROM File", ROM_EXTS_AGB),
            }[mode]
            path = QtWidgets.QFileDialog.getOpenFileName(
                self,
                __("Write ROM"),
                last_dir,
                __(file_type_name)
                + " ("
                + " ".join("*" + extension for extension in rom_extensions)
                + ");;"
                + __("All Files")
                + " (*.*)",
            )[0]

        if path == "":
            msg = __("No ROM file was selected. Do you want to wipe the ROM contents of the cartridge instead?")
            answer = QtWidgets.QMessageBox.question(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                msg,
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.No:
                return
            just_erase = True
            path = False

        if not just_erase:
            if not isinstance(path, str):
                msg_0 = "ROM path must be a string when not erasing the cartridge."
                raise TypeError(msg_0)
            loaded_buffer = self._LoadFlashROMFile(path, setting_name, cart_profile)
            if loaded_buffer is None:
                return
            buffer = loaded_buffer

        override_voltage, voltage_fallback, ask_voltage_fallback, device_voltage_locked = self._ResolveFlashVoltage(
            carts, cart_type, cart_profile
        )

        prefer_chip_erase = self.SETTINGS.value("PreferChipErase", default="disabled")
        prefer_chip_erase = bool(prefer_chip_erase and prefer_chip_erase.lower() == "enabled")

        verify_write = self.SETTINGS.value("VerifyData", default="enabled")
        verify_write = bool(verify_write and verify_write.lower() == "enabled")

        fix_bootlogo: bool | bytearray = False
        fix_header = False
        if not just_erase and len(buffer) >= 0x1000:
            header_repairs = self._ConfirmFlashHeaderRepairs(mode, buffer, mbc, cart_profile)
            if header_repairs is None:
                return
            mbc, fix_bootlogo, fix_header = header_repairs

        flash_offset = 0
        force_wr_pullup = str(self.SETTINGS.value("ForceWrPullup", default="disabled")).lower() == "enabled"

        if self._CancelFlashForLockedVoltage(mode, cart_profile, override_voltage, device_voltage_locked):
            return

        self.grpDMGCartridgeInfo.setEnabled(False)
        self.grpAGBCartridgeInfo.setEnabled(False)
        self.grpActions.setEnabled(False)
        self.mnuTools.setEnabled(False)
        self.mnuConfig.setEnabled(False)
        self.mnuLanguage.setEnabled(False)
        self.lblStatus4a.setText(__("Preparing..."))
        qt_app.processEvents()
        if len(buffer) > 0x1000 or just_erase:
            if just_erase:
                prefer_chip_erase = True
                verify_write = False
            args = {
                "path": "",
                "buffer": buffer,
                "cart_type": cart_type,
                "override_voltage": override_voltage,
                "prefer_chip_erase": prefer_chip_erase,
                "fast_read_mode": True,
                "verify_write": verify_write,
                "fix_header": fix_header,
                "fix_bootlogo": fix_bootlogo,
                "mbc": mbc,
                "voltage_fallback": voltage_fallback,
                "ask_voltage_fallback": ask_voltage_fallback,
            }
        else:
            args = {
                "path": path,
                "cart_type": cart_type,
                "override_voltage": override_voltage,
                "prefer_chip_erase": prefer_chip_erase,
                "fast_read_mode": True,
                "verify_write": verify_write,
                "fix_header": fix_header,
                "fix_bootlogo": fix_bootlogo,
                "mbc": mbc,
                "flash_offset": flash_offset,
                "force_wr_pullup": force_wr_pullup,
                "voltage_fallback": voltage_fallback,
                "ask_voltage_fallback": ask_voltage_fallback,
            }
        args["compare_sectors"] = str(self.SETTINGS.value("CompareSectors", default="disabled")).lower() == "enabled"
        self._device.FlashROM(fncSetProgress=self.PROGRESS.SetProgress, args=args)
        # self._device._FlashROM(args=args)
        self.grpStatus.setTitle(__("Transfer Status"))
        buffer = None
        self.STATUS["time_start"] = time.time()
        self.STATUS["last_path"] = path
        self.STATUS["args"] = args

    def _ResolveFlashVoltage(
        self,
        carts: list[Any],
        cart_type: int,
        cart_profile: dict[str, Any],
    ) -> tuple[float | Literal[False], int | Literal[False], bool, bool]:
        device_voltage_locked = self._device.CanSetVoltageByAutoswitch() and not self._device.CanSetVoltageByCode()
        override_voltage: float | Literal[False] = False
        voltage_fallback: int | Literal[False] = False
        ask_voltage_fallback = False
        if not device_voltage_locked and (
            ("voltage_variants" in cart_profile and cart_profile.get("voltage") == 3.3)
            or (cart_profile.get("voltage") == 5 and has_3v_compatible_profile(carts, cart_type))
        ):
            # Some PCBs share the same flash chip but need 3.3V; ask before falling back to 5V.
            override_voltage = 3.3
            voltage_fallback = 5
            ask_voltage_fallback = True
        return override_voltage, voltage_fallback, ask_voltage_fallback, device_voltage_locked

    def _CancelFlashForLockedVoltage(
        self,
        mode: PlatformMode,
        cart_profile: dict[str, Any],
        override_voltage: float | Literal[False],
        device_voltage_locked: bool,
    ) -> bool:
        effective_voltage = override_voltage if override_voltage is not False else cart_profile.get("voltage")
        if not (
            (effective_voltage == 3.3 or "voltage_variants" in cart_profile) and device_voltage_locked and mode == "DMG"
        ):
            return False
        msg_text = (
            __(
                "Warning: A 3.3V flashcart profile is selected, but your device is fixed to a 5V supply in Game Boy mode. Writing to a 3.3V flash chip at 5V may cause overvoltage issues.",
            )
            + "\n"
            + __("Do you want to continue?")
        )
        answer = QtWidgets.QMessageBox.warning(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            msg_text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        return answer == QtWidgets.QMessageBox.StandardButton.Cancel

    def _prepare_save_backup_cartridge(self, mode: PlatformMode, path: str) -> bool:
        needs_detection = (
            (
                mode == "AGB"
                and self.cmbAGBSaveTypeResult.currentIndex() < AgbSaveTypes().GetNumberOfTypes()
                and "Batteryless SRAM" in AgbSaveTypes().GetStringList()[self.cmbAGBSaveTypeResult.currentIndex()]
            )
            or (
                mode == "DMG"
                and self.cmbDMGHeaderSaveTypeResult.currentIndex() < DmgSaveTypes().GetNumberOfTypes()
                and "Batteryless SRAM" in DmgSaveTypes(index=self.cmbDMGHeaderSaveTypeResult.currentIndex()).GetString()
            )
            or (
                mode == "DMG"
                and "Unlicensed Photo!"
                in DmgSaveTypes(index=self.cmbDMGHeaderSaveTypeResult.currentIndex()).GetString()
            )
        )
        if not needs_detection:
            return True

        if self._device.GetFWBuildDate() == "":  # Legacy Mode
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Critical,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=__("This feature is not supported in Legacy Mode."),
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()
            return False

        cart_type = (
            self.cmbAGBCartridgeTypeResult.currentIndex()
            if mode == "AGB"
            else self.cmbDMGCartridgeTypeResult.currentIndex()
        )
        if cart_type != 0 and (
            "dump_info" in self._device.INFO and "batteryless_sram" in self._device.INFO["dump_info"]
        ):
            return True

        if "detected_cart_type" not in self.STATUS:
            self.STATUS["detected_cart_type"] = ""
        if self.STATUS["detected_cart_type"] == "":
            self.STATUS["detected_cart_type"] = "WAITING_SAVE_READ"
            self.STATUS["detect_cartridge_args"] = {"dpath": path}
            self.STATUS["can_skip_message"] = True
            self.DetectCartridge(checkSaveType=True)
            return False

        cart_type = self.STATUS.pop("detected_cart_type")
        if cart_type is False:  # clicked Cancel button
            return False
        if cart_type is None or cart_type == 0 or not isinstance(cart_type, int):
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("A compatible flashcart profile could not be auto-detected."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return False
        if mode == "AGB":
            self.cmbAGBCartridgeTypeResult.setCurrentIndex(cart_type)
        else:
            self.cmbDMGCartridgeTypeResult.setCurrentIndex(cart_type)
        return True

    def _PromptBackupRtc(self, mbc: int) -> bool | None:
        if self._device.INFO["has_rtc"] is not True:
            return False
        if self._device.GetMode() == "DMG" and mbc in (0x10, 0x110) and not self._device.IsClkConnected():
            return False

        msg = __(
            "A Real Time Clock cartridge was detected. Do you want the cartridge's Real Time Clock register values also to be saved?",
        )
        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Question,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=msg,
            standardButtons=QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No
            | QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        msgbox.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
        answer = msgbox.exec()
        if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
            return None
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    def BackupRAM(self, dpath: str = "") -> None:
        mode: Literal["DMG", "AGB"] | None = self._device.GetMode() if self.CheckDeviceAlive() else None
        if mode not in ("DMG", "AGB"):
            return
        rtc = False
        path = ""

        if not self._prepare_save_backup_cartridge(mode, dpath):
            return

        cart_type = 0
        if mode == "DMG":
            setting_name = "LastDirSaveDataDMG"
            mbc = ConvertMapperTypeToMapper(self.cmbDMGHeaderMapperResult.currentIndex())
            save_type = DmgSaveTypes(index=self.cmbDMGHeaderSaveTypeResult.currentIndex()).GetMbc()
            if save_type == 0:
                QtWidgets.QMessageBox.critical(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    __("No save type was selected."),
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
                return
            cart_type = self.cmbDMGCartridgeTypeResult.currentIndex()

        else:
            setting_name = "LastDirSaveDataAGB"
            mbc = 0
            save_type = self.cmbAGBSaveTypeResult.currentIndex()
            if save_type == 0:
                QtWidgets.QMessageBox.warning(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    __("No save type was selected."),
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
                return
            cart_type = self.cmbAGBCartridgeTypeResult.currentIndex()
        last_dir = self._GetLastDirectory(setting_name)
        if not self.CheckHeader():
            return
        selected_path = self._SelectSaveBackupPath(dpath, last_dir)
        if selected_path is None:
            return
        path = selected_path

        verify_read = self.SETTINGS.value("VerifyData", default="enabled")
        verify_read = bool(verify_read and verify_read.lower() == "enabled")

        rtc = self._PromptBackupRtc(mbc)
        if rtc is None:
            return

        bl_args = self._GetBatterylessBackupArgs(mode)
        if bl_args is None:
            return

        self.SETTINGS.setValue(setting_name, str(Path(path).parent))

        self.grpDMGCartridgeInfo.setEnabled(False)
        self.grpAGBCartridgeInfo.setEnabled(False)
        self.grpActions.setEnabled(False)
        self.mnuTools.setEnabled(False)
        self.mnuConfig.setEnabled(False)
        self.mnuLanguage.setEnabled(False)
        self.lblStatus4a.setText(__("Preparing..."))
        qt_app.processEvents()

        if len(bl_args) > 0:
            args = {
                "path": path,
                "mbc": mbc,
                "rom_size": bl_args["bl_size"],
                "agb_rom_size": bl_args["bl_size"],
                "fast_read_mode": True,
                "cart_type": cart_type,
            }
            args.update(bl_args)
            self._device.BackupROM(fncSetProgress=self.PROGRESS.SetProgress, args=args)
        else:
            args = {
                "path": path,
                "mbc": mbc,
                "save_type": save_type,
                "rtc": rtc,
                "verify_read": verify_read,
                "cart_type": cart_type,
            }
            self._device.BackupRAM(fncSetProgress=self.PROGRESS.SetProgress, args=args)

        self.grpStatus.setTitle(__("Transfer Status"))
        self.STATUS["time_start"] = time.time()
        self.STATUS["last_path"] = path
        self.STATUS["args"] = args

    def _SelectSaveBackupPath(self, dpath: str, last_dir: str) -> str | None:
        if dpath:
            return dpath
        path = generate_filename(
            mode=self._device.GetMode(),
            header=self._device.INFO,
            settings=self.SETTINGS,
        )
        path = str(Path(path).with_suffix(""))
        add_date_time: str | None = self.SETTINGS.value("SaveFileNameAddDateTime", default="disabled")
        if path and add_date_time and add_date_time.lower() == "enabled":
            path += "_{:s}".format(datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%d_%H-%M-%S"))
        path += ".sav"
        selected_path = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Backup Save Data",
            str(Path(last_dir) / path),
            "Save Data File (" + " ".join("*" + extension for extension in SAVE_EXTS) + ");;All Files (*.*)",
        )[0]
        return selected_path or None

    def _GetBatterylessBackupArgs(self, mode: PlatformMode) -> dict[str, int] | None:
        needs_batteryless_args = (
            self._device.GetMode() == "AGB"
            and self.cmbAGBSaveTypeResult.currentIndex() < AgbSaveTypes().GetNumberOfTypes()
            and "Batteryless SRAM" in AgbSaveTypes().GetStringList()[self.cmbAGBSaveTypeResult.currentIndex()]
        ) or (
            self._device.GetMode() == "DMG"
            and self.cmbDMGHeaderSaveTypeResult.currentIndex() < DmgSaveTypes().GetNumberOfTypes()
            and "Batteryless SRAM" in DmgSaveTypes(index=self.cmbDMGHeaderSaveTypeResult.currentIndex()).GetString()
        )
        if not needs_batteryless_args:
            return {}

        self.STATUS.pop("detected_cart_type", None)
        if "dump_info" in self._device.INFO and "batteryless_sram" in self._device.INFO["dump_info"]:
            detected = self._device.INFO["dump_info"]["batteryless_sram"]
        else:
            detected = False

        if mode == "AGB":
            rom_size = RomSizes().GetSize(self.cmbAGBHeaderROMSizeResult.currentIndex())
        else:
            rom_size = RomSizes().GetSize(self.cmbDMGHeaderROMSizeResult.currentIndex())
        if rom_size is None:
            return None
        bl_args = self.GetBLArgs(rom_size=rom_size, detected=detected)
        return None if bl_args is False else bl_args

    def _prepare_save_write_cartridge(
        self,
        mode: PlatformMode,
        dpath: str,
        erase: bool,
        test: bool,
    ) -> bool:
        needs_detection: bool = not test and (
            (
                mode == "AGB"
                and self.cmbAGBSaveTypeResult.currentIndex() < AgbSaveTypes().GetNumberOfTypes()
                and "Batteryless SRAM" in AgbSaveTypes().GetStringList()[self.cmbAGBSaveTypeResult.currentIndex()]
            )
            or (
                mode == "DMG"
                and self.cmbDMGHeaderSaveTypeResult.currentIndex() < DmgSaveTypes().GetNumberOfTypes()
                and "Batteryless SRAM" in DmgSaveTypes(index=self.cmbDMGHeaderSaveTypeResult.currentIndex()).GetString()
            )
            or (
                mode == "DMG"
                and "Unlicensed Photo!"
                in DmgSaveTypes(index=self.cmbDMGHeaderSaveTypeResult.currentIndex()).GetString()
            )
        )
        if not needs_detection:
            return True

        if self._device.GetFWBuildDate() == "":  # Legacy Mode
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Critical,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=__("This feature is not supported in Legacy Mode."),
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()
            return False

        cart_type = (
            self.cmbAGBCartridgeTypeResult.currentIndex()
            if mode == "AGB"
            else self.cmbDMGCartridgeTypeResult.currentIndex()
        )
        if cart_type != 0 and (
            "dump_info" in self._device.INFO and "batteryless_sram" in self._device.INFO["dump_info"]
        ):
            return True

        if "detected_cart_type" not in self.STATUS:
            self.STATUS["detected_cart_type"] = ""
        if self.STATUS["detected_cart_type"] == "":
            self.STATUS["detected_cart_type"] = "WAITING_SAVE_WRITE"
            self.STATUS["detect_cartridge_args"] = {
                "dpath": dpath,
                "erase": erase,
            }
            self.STATUS["can_skip_message"] = True
            self.DetectCartridge(checkSaveType=True)
            return False

        cart_type = self.STATUS.pop("detected_cart_type")
        if cart_type is False:  # clicked Cancel button
            return False
        if cart_type is None or cart_type == 0 or not isinstance(cart_type, int):
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("A compatible flashcart profile could not be auto-detected."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return False
        if mode == "AGB":
            self.cmbAGBCartridgeTypeResult.setCurrentIndex(cart_type)
        else:
            self.cmbDMGCartridgeTypeResult.setCurrentIndex(cart_type)
        return True

    def _prepare_save_calibration(
        self,
        mode: PlatformMode,
        path: str,
        erase: bool,
        test: bool,
    ) -> tuple[bool, bytearray | None]:
        if mode == "AGB" and self._device.INFO.get("ereader") is True:
            return self._prepare_ereader_save(path=path, erase=erase)
        if mode == "DMG" and self._device.INFO.get("dump_info", {}).get("header", {}).get("mapper_raw") == 0xFC:
            return self._prepare_camera_save(path=path, erase=erase, test=test)
        return True, None

    def _prepare_ereader_save(self, path: str, erase: bool) -> tuple[bool, bytearray | None]:
        if self._device.GetFWBuildDate() == "":  # Legacy Mode
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Critical,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=__("This cartridge is not supported in Legacy Mode."),
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()
            return False, None

        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Question,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text="",
        )
        button_keep = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&Keep existing calibration data"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        self._device.ReadHeader()
        cart_name = "e-Reader"
        if self._device.INFO["db"] is not None:
            cart_name = self._device.INFO["db"]["gn"]
        if "ereader_calibration" in self._device.INFO:
            if erase:
                buffer = bytearray([0xFF] * 0x20000)
                msg_text = (
                    __(
                        "This {cart_name} cartridge currently has calibration data in place. It is strongly recommended to keep the existing calibration data.",
                        cart_name=cart_name,
                    )
                    + "\n\n"
                    + __("How do you want to proceed?")
                )
                button_overwrite = msgbox.addButton(
                    c__("Button (& = Keyboard Shortcut)", "&Erase everything"),
                    QtWidgets.QMessageBox.ButtonRole.ActionRole,
                )
            else:
                with Path(path).open("rb") as file:
                    buffer = bytearray(file.read())
                msg_text = (
                    __(
                        "This {cart_name} cartridge currently has calibration data in place that is different from this save file's data. It is strongly recommended to keep the existing calibration data unless you actually need to restore it from a previous backup.",
                        cart_name=cart_name,
                    )
                    + "\n\n"
                    + __(
                        "Would you like to keep the existing calibration data, or overwrite it with data from the file you selected?",
                    )
                )
                button_overwrite = msgbox.addButton(
                    c__("Button (& = Keyboard Shortcut)", "&Restore from save data"),
                    QtWidgets.QMessageBox.ButtonRole.ActionRole,
                )
            button_cancel = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "&Cancel"),
                QtWidgets.QMessageBox.ButtonRole.RejectRole,
            )
            msgbox.setText(msg_text)
            msgbox.setDefaultButton(button_keep)
            msgbox.setEscapeButton(button_cancel)

            if buffer[0xD000:0xF000] != self._device.INFO["ereader_calibration"]:
                msgbox.exec()
                if msgbox.clickedButton() == button_cancel:
                    return False, None
                if msgbox.clickedButton() == button_keep:
                    buffer[0xD000:0xF000] = self._device.INFO["ereader_calibration"]
                elif msgbox.clickedButton() == button_overwrite:
                    pass
            return True, buffer

        msg_text = (
            __(
                "Warning: This {cart_name} cartridge may currently have calibration data in place. Erasing or overwriting this data may render the “{feature_name}” feature unusable. It is strongly recommended to create a backup of the original save data first and store it in a safe place. That way the calibration data can be restored later.",
                cart_name=cart_name,
                feature_name="Scan Card",
            )
            + "\n\n"
            + __("Do you still want to continue?")
        )
        answer = QtWidgets.QMessageBox.warning(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            msg_text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer != QtWidgets.QMessageBox.StandardButton.No, None

    def _prepare_camera_save(self, path: str, erase: bool, test: bool) -> tuple[bool, bytearray | None]:
        if self._device.GetFWBuildDate() == "":  # Legacy Mode
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Critical,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=__("This cartridge is not supported in Legacy Mode."),
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()
            return False, None

        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Question,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text="",
        )
        button_keep = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&Keep existing calibration data"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        if (
            "Unlicensed Photo!"
            not in DmgSaveTypes(
                index=self.cmbDMGHeaderSaveTypeResult.currentIndex(),
            ).GetString()
        ):
            button_reset = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "&Force recalibration"),
                QtWidgets.QMessageBox.ButtonRole.ActionRole,
            )
        else:
            button_reset = None
        self._device.ReadHeader()
        cart_name = "Game Boy Camera"
        if self._device.INFO["db"] is not None:
            cart_name = self._device.INFO["db"]["gn"]
        if test:
            return True, None

        if "gbcamera_calibration1" in self._device.INFO:
            if erase:
                buffer = bytearray([0x00] * 0x20000)
                if (
                    "Unlicensed Photo!"
                    in DmgSaveTypes(
                        index=self.cmbDMGHeaderSaveTypeResult.currentIndex(),
                    ).GetString()
                ):
                    buffer += bytearray([0xFF] * 0xE0000)
                msg_text = (
                    __(
                        "This {cart_name} cartridge currently has calibration data in place.\n\nHow do you want to proceed?",
                        cart_name=cart_name,
                    )
                    + "\n\n"
                    + __(
                        "It is recommended to keep the existing calibration data, but you can also choose to erase it or overwrite it with data from the file you selected.",
                    )
                )
                button_overwrite = msgbox.addButton(
                    c__("Button (& = Keyboard Shortcut)", "&Erase everything"),
                    QtWidgets.QMessageBox.ButtonRole.ActionRole,
                )
            else:
                with Path(path).open("rb") as file:
                    buffer = bytearray(file.read())
                msg_text = __(
                    "This {cart_name} cartridge currently has calibration data in place that is different from this save file's data.\n\nHow do you want to proceed?",
                    cart_name=cart_name,
                )
                button_overwrite = msgbox.addButton(
                    c__("Button (& = Keyboard Shortcut)", "&Restore from save data"),
                    QtWidgets.QMessageBox.ButtonRole.ActionRole,
                )
            button_cancel = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "&Cancel"),
                QtWidgets.QMessageBox.ButtonRole.RejectRole,
            )
            msgbox.setText(msg_text)
            msgbox.setDefaultButton(button_keep)
            msgbox.setEscapeButton(button_cancel)

            if (
                buffer[0x4FF2:0x5000] != self._device.INFO["gbcamera_calibration1"]
                or buffer[0x11FF2:0x12000] != self._device.INFO["gbcamera_calibration2"]
            ):
                msgbox.exec()
                if msgbox.clickedButton() == button_cancel:
                    return False, None
                if msgbox.clickedButton() == button_keep:
                    buffer[0x4FF2:0x5000] = self._device.INFO["gbcamera_calibration1"]
                    buffer[0x11FF2:0x12000] = self._device.INFO["gbcamera_calibration2"]
                elif msgbox.clickedButton() == button_reset:
                    buffer[0x4FF2:0x5000] = bytearray([0xAA] * 0xE)
                    buffer[0x11FF2:0x12000] = bytearray([0xAA] * 0xE)
                elif msgbox.clickedButton() == button_overwrite:
                    pass
            return True, buffer

        msg_text = (
            __(
                "Warning: This {cart_name} cartridge may currently have calibration data in place. It is recommended to create a backup of the original save data first and store it in a safe place. That way the calibration data can be restored later.",
                cart_name=cart_name,
            )
            + "\n\n"
            + __("Do you still want to continue?")
        )
        answer = QtWidgets.QMessageBox.warning(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            msg_text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer != QtWidgets.QMessageBox.StandardButton.No, None

    def _prepare_save_write_path(
        self,
        options: _SaveWritePathOptions,
        setting_name: str,
        last_dir: str,
    ) -> tuple[str, int] | None:
        mode, dpath, erase, test, skip_warning = options
        path: str | None = ""
        if dpath != "":
            if not skip_warning:
                text = __("The following save data file will now be written to the cartridge:") + "\n" + dpath
                answer = QtWidgets.QMessageBox.question(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    text,
                    QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
                if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
                    return None
            path = dpath
            self.SETTINGS.setValue(setting_name, str(Path(path).parent))
        elif erase:
            if not skip_warning:
                answer = QtWidgets.QMessageBox.warning(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    __("The save data on your cartridge will now be erased."),
                    QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
                    QtWidgets.QMessageBox.StandardButton.Cancel,
                )
                if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
                    return None
        elif test:
            path = None
            if not self._confirm_save_write_test(mode):
                return None
        else:
            generated_path = generate_filename(mode=mode, header=self._device.INFO, settings=self.SETTINGS)
            path = generated_path if isinstance(generated_path, str) else "save.sav"
            path = str(Path(path).with_suffix("")) + ".sav"
            path = QtWidgets.QFileDialog.getOpenFileName(
                self,
                __("Restore Save Data"),
                str(Path(last_dir) / path),
                __("Save Data File") + " (" + " ".join("*" + e for e in SAVE_EXTS) + ");;" + __("All Files") + " (*.*)",
            )[0]
            if path != "":
                self.SETTINGS.setValue(setting_name, str(Path(path).parent))
            if path == "":
                return None

        if not isinstance(path, str):
            return None
        filesize = 0
        if not erase and not test and len(path) > 0:
            filesize = Path(path).stat().st_size
            if filesize == 0 or filesize > 0x200000:  # reject too large files to avoid exploding RAM
                QtWidgets.QMessageBox.critical(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    __("The size of this file is not supported."),
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
                return None
        return path, filesize

    def _confirm_save_write_test(self, mode: PlatformMode) -> bool:
        if self._device.GetFWBuildDate() == "":  # Legacy Mode
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Critical,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=__("This feature is not supported in Legacy Mode."),
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()
            return False

        dmg_save_type_index = self.cmbDMGHeaderSaveTypeResult.currentIndex()
        agb_save_type_index = self.cmbAGBSaveTypeResult.currentIndex()
        unsupported = (
            (
                mode == "AGB"
                and agb_save_type_index < AgbSaveTypes().GetNumberOfTypes()
                and "Batteryless SRAM" in AgbSaveTypes().GetStringList()[agb_save_type_index]
            )
            or (
                mode == "DMG"
                and dmg_save_type_index < DmgSaveTypes().GetNumberOfTypes()
                and "Batteryless SRAM" in DmgSaveTypes(index=dmg_save_type_index).GetString()
            )
            or (
                mode == "DMG"
                and dmg_save_type_index < DmgSaveTypes().GetNumberOfTypes()
                and "Unlicensed Photo!" in DmgSaveTypes(index=dmg_save_type_index).GetString()
            )
            or ("8M DACS" in AgbSaveTypes().GetStringList()[agb_save_type_index])
            or (mode == "AGB" and "ereader" in self._device.INFO and self._device.INFO["ereader"] is True)
            or (
                mode == "DMG"
                and "256M Multi Cart" in self.cmbDMGHeaderMapperResult.currentText()
                and not self._device.CanPowerCycleCart()
            )
        )
        if unsupported:
            QtWidgets.QMessageBox.information(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("Stress test is not supported for this save type."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return False

        msg = __(
            "The cartridge's save chip will be tested for potential problems as follows:\n- Read the same data multiple times\n- Writing and reading different test patterns\n\nPlease ensure the cartridge pins are freshly cleaned and the save data is backed up before proceeding.",
        )
        if not self._device.CanPowerCycleCart() and (
            (mode == "AGB" and "SRAM" in self.cmbAGBSaveTypeResult.currentText())
            or (mode == "DMG" and "SRAM" in self.cmbDMGHeaderSaveTypeResult.currentText())
        ):
            msg += "\n\n" + __(
                "Note: Your {device_name} does not support automatic power cycling, so some tests may be skipped.",
                device_name=self._device.GetName(),
            )
        answer = QtWidgets.QMessageBox.question(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            msg,
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Ok,
        )
        return answer != QtWidgets.QMessageBox.StandardButton.Cancel

    def _PrepareSaveWrite(
        self,
        dpath: str = "",
        erase: bool = False,
        test: bool = False,
        skip_warning: bool = False,
    ) -> _SaveWritePreparation | None:
        mode = self._device.GetMode() if self.CheckDeviceAlive() else None
        path = ""
        if erase is True:
            dpath = ""

        if mode not in ("DMG", "AGB") or not self._prepare_save_write_cartridge(
            mode=mode,
            dpath=dpath,
            erase=erase,
            test=test,
        ):
            return None

        if mode == "DMG":
            setting_name = "LastDirSaveDataDMG"
            last_dir = self.SETTINGS.value(setting_name)
            if last_dir is None:
                last_dir = QtCore.QStandardPaths.writableLocation(
                    QtCore.QStandardPaths.StandardLocation.DocumentsLocation,
                )
            mbc = ConvertMapperTypeToMapper(self.cmbDMGHeaderMapperResult.currentIndex())
            save_type = DmgSaveTypes(index=self.cmbDMGHeaderSaveTypeResult.currentIndex()).GetMbc()
            if save_type in (None, 0):
                QtWidgets.QMessageBox.critical(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    __("No save type was selected."),
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
                return None
            cart_type = self.cmbDMGCartridgeTypeResult.currentIndex()

        elif mode == "AGB":
            setting_name = "LastDirSaveDataAGB"
            last_dir = self.SETTINGS.value(setting_name)
            if last_dir is None:
                last_dir = QtCore.QStandardPaths.writableLocation(
                    QtCore.QStandardPaths.StandardLocation.DocumentsLocation,
                )
            mbc = 0
            save_type = self.cmbAGBSaveTypeResult.currentIndex()
            if save_type == 0:
                QtWidgets.QMessageBox.warning(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    __("No save type was selected."),
                    QtWidgets.QMessageBox.StandardButton.Ok,
                )
                return None
            cart_type = self.cmbAGBCartridgeTypeResult.currentIndex()
        else:
            return None
        if not self.CheckHeader():
            return None

        save_location = self._prepare_save_write_path(
            _SaveWritePathOptions(mode, dpath, erase, test, skip_warning),
            setting_name=setting_name,
            last_dir=last_dir,
        )
        if save_location is None:
            return None
        path, filesize = save_location

        continue_write, buffer = self._prepare_save_calibration(mode=mode, path=path, erase=erase, test=test)
        if not continue_write:
            return None

        return _SaveWritePreparation(mode, path, mbc, save_type, cart_type, filesize, buffer)

    def _PrepareSaveWriteRtc(
        self,
        *,
        mode: PlatformMode,
        mbc: int,
        filesize: int,
        save_type: int,
        erase: bool,
    ) -> tuple[bool, bool] | None:
        rtc = False
        rtc_advance = False
        if self._device.INFO["has_rtc"] is not True:
            return rtc, rtc_advance
        if mode == "DMG" and mbc in (0x10, 0x110) and not self._device.IsClkConnected():
            return rtc, rtc_advance
        if not (erase or save_size_includes_rtc(mode=mode, mbc=mbc, save_size=filesize, save_type=save_type)):
            return rtc, rtc_advance

        msg = __(
            "A Real Time Clock cartridge was detected. Do you want the Real Time Clock register values to be written as well?",
        )
        cb = _create_check_box(
            c__("Check Box (& = Keyboard Shortcut)", "&Adjust RTC"),
            checked=True,
        )
        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Question,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=msg,
            standardButtons=QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No
            | QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        msgbox.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
        if erase:
            cb.setChecked(True)
        else:
            msgbox.setCheckBox(cb)
        answer = msgbox.exec()
        if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
            return None
        rtc_advance = cb.isChecked()
        rtc = answer == QtWidgets.QMessageBox.StandardButton.Yes
        return rtc, rtc_advance

    def _PrepareSaveStressTest(self, mbc: int) -> tuple[list[bytearray], list[str]]:
        self.grpDMGCartridgeInfo.setEnabled(False)
        self.grpAGBCartridgeInfo.setEnabled(False)
        self.grpActions.setEnabled(False)
        self.mnuTools.setEnabled(False)
        self.mnuConfig.setEnabled(False)
        self.mnuLanguage.setEnabled(False)
        self.lblStatus4a.setText(__("Preparing..."))
        self.grpStatus.setTitle(__("Transfer Status"))
        self.lblStatus1aResult.setText("-")
        self.lblStatus2aResult.setText("-")
        self.lblStatus3aResult.setText("-")
        self.SetStatus4aResult("")
        self.btnCancel.setEnabled(True)
        self.STATUS["stresstest_running"] = True
        qt_app.processEvents()

        test_patterns = [
            bytearray(os.urandom(128 * 1024)),
            bytearray([0x00, 0x00, 0x00, 0x00] * 32768),
            bytearray([0x55, 0xAA, 0xAA, 0x55] * 32768),
            bytearray([0x00, 0xFF, 0xFF, 0x00] * 32768),
            bytearray([0xFF, 0xFF, 0xFF, 0xFF] * 32768),
            bytearray(range(256)),
            bytearray(reversed(range(256))),
        ]
        if get_mbc_name(mbc) == "MBC2":
            for pattern in test_patterns:
                for index, value in enumerate(pattern):
                    pattern[index] = value & 0x0F

        test_pattern_names = [
            c__("Stress Test Pattern", "reading twice"),
            c__("Stress Test Pattern", "writing random values"),
            c__("Stress Test Pattern", "writing {pattern}", pattern="00, 00, 00, 00"),
            c__("Stress Test Pattern", "writing {pattern}", pattern="55, AA, AA, 55"),
            c__("Stress Test Pattern", "writing {pattern}", pattern="00, FF, FF, 00"),
            c__("Stress Test Pattern", "writing {pattern}", pattern="FF, FF, FF, FF"),
            c__("Stress Test Pattern", "writing incrementing values"),
            c__("Stress Test Pattern", "writing decrementing values"),
        ]
        return test_patterns, test_pattern_names

    def _FinishSaveStressTest(self) -> None:
        self.grpDMGCartridgeInfo.setEnabled(True)
        self.grpAGBCartridgeInfo.setEnabled(True)
        self.grpActions.setEnabled(True)
        self.mnuTools.setEnabled(True)
        self.mnuConfig.setEnabled(True)
        self.mnuLanguage.setEnabled(True)
        self.btnCancel.setEnabled(False)
        if not self._device.IsConnected():
            self.DisconnectDevice()

    def _RunSaveStressTestTransfer(self, args: dict[str, Any]) -> None:
        transfer = threading.Thread(
            target=self._device.TransferData,
            kwargs={"args": args, "signal": _ignore_progress},
        )
        transfer.start()
        while transfer.is_alive():
            qt_app.processEvents()
            time.sleep(0.02)
        transfer.join()

    def _RestoreSaveStressTest(
        self,
        preparation: _SaveWritePreparation,
        save_data: bytearray,
        rtc_advance: bool,
        *,
        erase: bool,
        progress_max: int,
    ) -> None:
        self.btnCancel.setEnabled(False)
        self.lblStatus4a.setText(__("Restoring original save data..."))
        self.SetProgressBars(min=0, max=progress_max, value=progress_max - 1)
        qt_app.processEvents()
        self._RunSaveStressTestTransfer(
            {
                "mode": 3,
                "path": preparation.path,
                "mbc": preparation.mbc,
                "save_type": preparation.save_type,
                "rtc": False,
                "rtc_advance": rtc_advance,
                "erase": erase,
                "verify_write": False,
                "buffer": save_data,
                "cart_type": preparation.cart_type,
            },
        )
        self._RunSaveStressTestTransfer(
            {
                "mode": 2,
                "path": preparation.path,
                "mbc": preparation.mbc,
                "save_type": preparation.save_type,
                "rtc": False,
                "cart_type": preparation.cart_type,
            },
        )

    def _ShowSaveStressTestResult(self, result: _SaveStressTestResult) -> None:
        if result.tests_completed == len(result.pattern_names) + 1:
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Information,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=__("All tests completed successfully!") + result.elapsed_message,
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()
            return

        try:
            written_data = result.written_data
            readback_data = result.readback_data
            if result.tests_completed == 0:
                written_data = result.first_save or bytearray()
                readback_data = result.second_save
            with (Path(AppContext.CONFIG_PATH) / "debug_stress_test_1.bin").open("wb") as file:
                file.write(written_data[: len(readback_data)])
            with (Path(AppContext.CONFIG_PATH) / "debug_stress_test_2.bin").open("wb") as file:
                file.write(readback_data)
        except Exception:
            logger.exception("Failed to write cartridge stress-test diagnostics")
        if result.tests_completed > 0:
            msg = __(
                "Test {num} ({pattern}) failed!",
                num=result.tests_completed + 1,
                pattern=result.pattern_names[result.tests_completed],
            )
            msg += result.elapsed_message
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Warning,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=msg,
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()

    def _WaitForSaveStressTestPowerCycle(self, progress_max: int) -> None:
        if self._device.CanPowerCycleCart():
            self._device.CartPowerOff()
            self.SetProgressBars(min=0, max=progress_max, value=1)
            for i in range(5, 0, -1):
                self.lblStatus4a.setText(__("Waiting for power cycle ({countdown})...", countdown=i))
                qt_app.processEvents()
                time.sleep(1)
                if "stresstest_running" not in self.STATUS:
                    break
            self._device.CartPowerOn()
        else:
            time.sleep(1)

    def _ConfirmSaveStressTestMismatch(self, test_number: int, pattern_name: str) -> bool:
        msg = (
            __(
                "Test {num} ({pattern}) failed!",
                num=test_number,
                pattern=pattern_name,
            )
            + "\n"
            + __("Note: SRAM requires a working battery to retain save data.")
            + "\n\n"
            + __("Continue anyway?")
        )
        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Warning,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=msg,
            standardButtons=QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        msgbox.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
        return msgbox.exec() != QtWidgets.QMessageBox.StandardButton.No

    def _RunSaveStressTest(
        self,
        preparation: _SaveWritePreparation,
        rtc_advance: bool,
        *,
        erase: bool,
    ) -> None:
        path = preparation.path
        mbc = preparation.mbc
        save_type = preparation.save_type
        cart_type = preparation.cart_type
        test_patterns, test_patterns_names = self._PrepareSaveStressTest(mbc)

        time_start = time.time()
        test_ok = 0
        save1 = bytearray([0])
        save2 = bytearray([1])
        towrite = save1
        readback = save2
        backup_fn = Path(AppContext.CONFIG_PATH) / "backup_stress_test.bin"

        try:
            self.lblStatus4a.setText(__("Testing ({pattern} 1/2)...", pattern=test_patterns_names[0]))
            self.SetProgressBars(min=0, max=len(test_patterns) + 3, value=0)
            qt_app.processEvents()
            args = {
                "mode": 2,
                "path": path,
                "mbc": mbc,
                "save_type": save_type,
                "rtc": False,
                "cart_type": cart_type,
            }
            self._RunSaveStressTestTransfer(args)
            save1 = self._device.INFO["data"]
            self._WaitForSaveStressTestPowerCycle(len(test_patterns) + 3)
            self.lblStatus4a.setText(__("Testing ({pattern} 2/2)...", pattern=test_patterns_names[0]))
            qt_app.processEvents()
            self._RunSaveStressTestTransfer(args)
            save2 = self._device.INFO["data"]
        except KeyError:
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Critical,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=__("An error occured. Please ensure you selected the correct save type."),
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()
            save1 = None

        stop = False
        if (save1 is not None and save1 != save2) and "stresstest_running" in self.STATUS:
            with (Path(AppContext.CONFIG_PATH) / "debug_stress_test_1.bin").open("wb") as f:
                f.write(save1)
            with (Path(AppContext.CONFIG_PATH) / "debug_stress_test_2.bin").open("wb") as f:
                f.write(save2)
            if not self._ConfirmSaveStressTestMismatch(test_ok + 1, test_patterns_names[test_ok]):
                stop = True

        if not stop and save1 is not None:
            with backup_fn.open("wb") as f:
                f.write(save1)
            test_ok += 1
            for i in range(len(test_patterns)):
                if "stresstest_running" not in self.STATUS:
                    break
                self.lblStatus4a.setText(__("Testing ({pattern})...", pattern=test_patterns_names[i + 1]))
                self.SetProgressBars(min=0, max=len(test_patterns) + 3, value=i + 2)
                qt_app.processEvents()
                towrite = test_patterns[i]
                args = {
                    "mode": 3,
                    "path": path,
                    "mbc": mbc,
                    "save_type": save_type,
                    "rtc": False,
                    "rtc_advance": rtc_advance,
                    "erase": erase,
                    "verify_write": False,
                    "buffer": towrite,
                    "cart_type": cart_type,
                }
                self._RunSaveStressTestTransfer(args)
                if i == 0 and save1 == save2:  # user "continued anyway"
                    self._device.CartPowerOff()
                    time.sleep(0.5)
                    self._device.CartPowerOn()
                args = {
                    "mode": 2,
                    "path": path,
                    "mbc": mbc,
                    "save_type": save_type,
                    "rtc": False,
                    "cart_type": cart_type,
                }
                self._RunSaveStressTestTransfer(args)
                readback = self._device.INFO["data"]
                if towrite[: len(readback)] != readback:
                    break
                test_ok += 1

            self._RestoreSaveStressTest(
                preparation,
                save1,
                rtc_advance,
                erase=erase,
                progress_max=len(test_patterns) + 3,
            )

        time_elapsed = time.time() - time_start
        msg_te = "\n\n" + __(
            "Total time elapsed: {elapsed}",
            elapsed=Formatter.progress_time(time_elapsed, as_float=True),
        )

        self.SetProgressBars(min=0, max=100, value=100)
        self.lblStatus4a.setText(__("Done!"))
        qt_app.processEvents()

        if "stresstest_running" in self.STATUS:
            self._ShowSaveStressTestResult(
                _SaveStressTestResult(test_ok, test_patterns_names, msg_te, save1, save2, towrite, readback),
            )
        else:
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Information,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=__("The stress test process was cancelled."),
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()

        self._FinishSaveStressTest()

    def WriteRAM(
        self,
        dpath: str = "",
        erase: bool = False,
        test: bool = False,
        skip_warning: bool = False,
    ) -> None:
        preparation = self._PrepareSaveWrite(
            dpath=dpath,
            erase=erase,
            test=test,
            skip_warning=skip_warning,
        )
        if preparation is None:
            return
        mode, path, mbc, save_type, cart_type, filesize, buffer = preparation

        verify_write = self.SETTINGS.value("VerifyData", default="enabled")
        verify_write = bool(verify_write and verify_write.lower() == "enabled")

        rtc_settings = (
            self._PrepareSaveWriteRtc(
                mode=mode,
                mbc=mbc,
                filesize=filesize,
                save_type=save_type,
                erase=erase,
            )
            if not test
            else (False, False)
        )
        if rtc_settings is None:
            return
        rtc, rtc_advance = rtc_settings

        if test:
            self._RunSaveStressTest(preparation, rtc_advance, erase=erase)
            return

        args: dict[str, Any]
        bl_args = {}
        if (
            mode == "AGB"
            and self.cmbAGBSaveTypeResult.currentIndex() < AgbSaveTypes().GetNumberOfTypes()
            and "Batteryless SRAM" in AgbSaveTypes().GetStringList()[self.cmbAGBSaveTypeResult.currentIndex()]
        ) or (
            mode == "DMG"
            and self.cmbDMGHeaderSaveTypeResult.currentIndex() < DmgSaveTypes().GetNumberOfTypes()
            and "Batteryless SRAM" in DmgSaveTypes(index=self.cmbDMGHeaderSaveTypeResult.currentIndex()).GetString()
        ):
            if "detected_cart_type" in self.STATUS:
                del self.STATUS["detected_cart_type"]

            if "dump_info" in self._device.INFO and "batteryless_sram" in self._device.INFO["dump_info"]:
                detected = self._device.INFO["dump_info"]["batteryless_sram"]
            else:
                detected = False
            rom_size_index = (
                self.cmbAGBHeaderROMSizeResult.currentIndex()
                if mode == "AGB"
                else self.cmbDMGHeaderROMSizeResult.currentIndex()
            )
            rom_size = RomSizes().GetSize(rom_size_index)
            if rom_size is None:
                return
            bl_args = self.GetBLArgs(
                rom_size=rom_size,
                detected=detected,
            )
            if bl_args is False:
                return

            if mode == "DMG" and self._device.CanSetVoltageByAutoswitch() and not self._device.CanSetVoltageByCode():
                bl_carts = self._device.GetSupportedCartridgesDMG()[1]
                bl_profile = bl_carts[cart_type]
                if isinstance(bl_profile, dict) and (
                    bl_profile.get("voltage") == 3.3 or "voltage_variants" in bl_profile
                ):
                    msg_text = (
                        __(
                            "Warning: A 3.3V flashcart profile is selected, but your device is fixed to a 5V supply in Game Boy mode. Writing to a 3.3V flash chip at 5V may cause overvoltage issues.",
                        )
                        + "\n"
                        + __("Do you want to continue?")
                    )
                    answer = QtWidgets.QMessageBox.warning(
                        self,
                        f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                        msg_text,
                        QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
                        QtWidgets.QMessageBox.StandardButton.Cancel,
                    )
                    if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
                        return

            args = {
                "path": path,
                "cart_type": cart_type,
                "override_voltage": False,
                "prefer_chip_erase": False,
                "fast_read_mode": True,
                "verify_write": verify_write,
                "fix_header": False,
                "fix_bootlogo": False,
                "mbc": mbc,
            }
            args.update(bl_args)
            args.update(
                {
                    "bl_save": True,
                    "flash_offset": bl_args["bl_offset"],
                    "flash_size": bl_args["bl_size"],
                },
            )
            if erase:
                args["path"] = ""
                args["buffer"] = bytearray([0xFF] * bl_args["bl_size"])
            self.STATUS["args"] = args
            self._device.FlashROM(fncSetProgress=self.PROGRESS.SetProgress, args=args)
            # self._device._FlashROM(args=args)

        else:
            args = {
                "path": path,
                "mbc": mbc,
                "save_type": save_type,
                "rtc": rtc,
                "rtc_advance": rtc_advance,
                "erase": erase,
                "verify_write": verify_write,
                "cart_type": cart_type,
            }
            if buffer is not None:
                args["buffer"] = buffer
                args["path"] = None
                args["erase"] = False
            self.STATUS["args"] = args
            self._device.RestoreRAM(fncSetProgress=self.PROGRESS.SetProgress, args=args)
            # args = { "mode":3, "path":path, "mbc":mbc, "save_type":save_type, "rtc":rtc, "rtc_advance":rtc_advance, "erase":erase, "verify_write":verify_write }
            # self._device._BackupRestoreRAM(args=args)

        self.STATUS["time_start"] = time.time()
        self.STATUS["last_path"] = path
        self.STATUS["args"] = args
        self.grpDMGCartridgeInfo.setEnabled(False)
        self.grpAGBCartridgeInfo.setEnabled(False)
        self.grpActions.setEnabled(False)
        self.mnuTools.setEnabled(False)
        self.mnuConfig.setEnabled(False)
        self.mnuLanguage.setEnabled(False)
        self.lblStatus4a.setText(__("Preparing..."))
        self.grpStatus.setTitle(__("Transfer Status"))
        self.lblStatus1aResult.setText("-")
        self.lblStatus2aResult.setText("-")
        self.lblStatus3aResult.setText("-")
        self.SetStatus4aResult("")
        qt_app.processEvents()

    @staticmethod
    def _get_default_bl_location_index(rom_size: int, locations: list[int]) -> int:
        location_index = 0
        for location in locations:
            if location + 0x40000 >= rom_size:
                break
            location_index += 1
        if location_index >= len(locations):
            return len(locations) - 1
        return location_index

    def _PreselectBatterylessSramParameters(
        self,
        locs: list[int],
        lens: list[int],
    ) -> tuple[int | None, int | None, int | None, str]:
        loc_index = None
        len_index = None
        lay_index = None
        message = "⚠️ The required parameters could not be auto-detected. Please enter the ROM location and size manually below. Note that wrong values can corrupt your game upon writing, so having a full ROM backup is recommended."
        try:
            header = self._device.INFO["dump_info"]["header"]
            preselect = header.get("batteryless_sram") or RomFileDMG.GetBatterylessSramConfig(header)
            if preselect is not None:
                game_title_raw = header.get("game_title_raw", header.get("game_title", "")).replace("\x00", "").rstrip()
                loc_index = locs.index(preselect["bl_offset"])
                len_index = lens.index(preselect["bl_size"])
                lay_index = preselect.get("bl_layout")
                message = (
                    "The required parameters were pre-selected based on the ROM title “"
                    + game_title_raw
                    + "”. These may still be inaccurate, so you can adjust them below if necessary. Note that wrong values can corrupt your game when writing, so having a full ROM backup is recommended."
                )
        except Exception:
            logger.exception("Failed to preselect batteryless SRAM parameters")
        return loc_index, len_index, lay_index, message

    def GetBLArgs(
        self,
        rom_size: int,
        detected: BatterylessSramInfo | Literal[False] = False,
    ) -> dict[str, int] | Literal[False]:
        mode = self._device.GetMode()
        if mode not in ("DMG", "AGB"):
            msg = "Batteryless SRAM parameters require a platform mode"
            raise RuntimeError(msg)
        selection = self._PrepareBatterylessDialogSelection(mode, rom_size, detected)
        locs = selection.locations
        lens = selection.lengths
        intro_msg = selection.intro
        loc_index = selection.location_index
        len_index = selection.length_index
        lay_index = selection.layout_index

        dlg_args = {
            "title": __("{batteryless_sram} Parameters", batteryless_sram="Batteryless SRAM"),
            "intro": intro_msg.replace("\n", "<br>"),
            "params": [
                ["loc", "cmb_e", __("Location:"), [f"0x{location:X}" for location in locs], loc_index],
                ["len", "cmb", __("Size:"), [Formatter.file_size(size, as_int=True) for size in lens], len_index],
            ],
        }
        if mode == "DMG":
            dlg_args["params"].append(
                [
                    "layout",
                    "cmb",
                    __("Layout:"),
                    [__("Continuous"), __("First half of ROM bank"), __("Second half of ROM bank")],
                    lay_index,
                ],
            )

        dlg = UserInputDialog(self, icon=self.windowIcon(), args=cast("DialogArgs", dlg_args))
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            result = dlg.GetResult()
            if result["loc"].currentText() not in [f"0x{location:X}" for location in locs]:
                try:
                    bl_args = {"bl_offset": _parse_hex_address(result["loc"].currentText())}
                except ValueError:
                    return False
            else:
                bl_args = {"bl_offset": locs[result["loc"].currentIndex()]}
            bl_args["bl_size"] = lens[result["len"].currentIndex()]
            if mode == "DMG":
                bl_args["bl_layout"] = result["layout"].currentIndex()

            locs.append(bl_args["bl_offset"])
            self.SETTINGS.setValue(f"BatterylessSramLocations{mode:s}", json.dumps(locs))
            self.SETTINGS.setValue(f"BatterylessSramLastLocation{mode:s}", json.dumps(bl_args["bl_offset"]))
            ret = bl_args
        else:
            ret = False
        del dlg
        return ret

    def _PrepareBatterylessDialogSelection(
        self,
        mode: PlatformMode,
        rom_size: int,
        detected: BatterylessSramInfo | Literal[False],
    ) -> _BatterylessDialogSelection:
        if mode == "AGB":
            locs = [0x3C0000, 0x7C0000, 0xFC0000, 0x1FC0000]
            lens = [0x2000, 0x8000, 0x10000, 0x20000]
        else:
            locs = [0xD0000, 0x100000, 0x110000, 0x1D0000, 0x1E0000, 0x210000, 0x3D0000]
            lens = [0x2000, 0x8000, 0x10000, 0x20000]

        saved_locations = self.SETTINGS.value(f"BatterylessSramLocations{mode:s}", "[]")
        loc_index = None
        len_index = None
        lay_index = None

        try:
            parsed_locations = json.loads(str(saved_locations))
            if isinstance(parsed_locations, list):
                locs.extend(location for location in parsed_locations if isinstance(location, int))
            if detected is not False:
                locs.append(detected["bl_offset"])
            locs = list(set(locs))
            locs.sort()
        except Exception:
            logger.exception("Failed to load saved batteryless SRAM locations")

        intro_msg = ""
        if detected is not False:
            try:
                loc_index = locs.index(detected["bl_offset"])
                len_index = lens.index(detected["bl_size"])
                intro_msg = "In order to access Batteryless SRAM save data, its ROM location and size must be specified.\n\nThe previously detected parameters have been pre-selected. Please adjust if necessary, then click “OK” to continue."
            except KeyError, TypeError, ValueError:
                loc_index = None
                len_index = None
                detected = False
        if detected is False:
            intro_msg = (
                "In order to access Batteryless SRAM save data, its ROM location and size must be specified.\n\n"
            )
            if mode == "DMG":
                loc_index, len_index, lay_index, intro_msg2 = self._PreselectBatterylessSramParameters(locs, lens)
            else:
                intro_msg2 = "⚠️ The required parameters could not be auto-detected. Please enter the ROM location and size manually below. Note that wrong values can corrupt your game upon writing, so having a full ROM backup is recommended."

            intro_msg += intro_msg2

        try:
            if loc_index is None:
                loc_index = locs.index(int(str(self.SETTINGS.value(f"BatterylessSramLastLocation{mode:s}"))))
        except Exception:
            logger.exception("Failed to restore the last batteryless SRAM location")

        if loc_index is None:
            loc_index = self._get_default_bl_location_index(rom_size, locs)
        if len_index is None:
            len_index = {"AGB": 2, "DMG": 1}[mode]
        if lay_index is None:
            lay_index = 2
        return _BatterylessDialogSelection(locs, lens, intro_msg, loc_index, len_index, lay_index)

    def _EditRTCFromMouseEvent(self, event: QtGui.QMouseEvent) -> None:
        self.EditRTC(event)

    @staticmethod
    def _DmgRtcDialogValues(result: Mapping[str, Any]) -> dict[str, int | bool]:
        rtc_dict: dict[str, int | bool] = {}
        for key, value in result.items():
            if isinstance(value, QtWidgets.QSpinBox):
                rtc_dict[key] = value.value()
            elif isinstance(value, QtWidgets.QCheckBox):
                rtc_dict[key] = value.isChecked()
        return rtc_dict

    def _EditAgbRTC(self, rtc_data: Mapping[str, Any]) -> dict[str, Any] | None:
        dlg_args = {
            "title": __("GBA Real Time Clock Editor"),
            "intro": __("Enter the date and time for the Real Time Clock.")
            + "\n\n"
            + __(
                "Please note that all values are internal values. The game may use these only as a relative reference.",
            ),
            "params": [
                ["rtc_y", "spb", c__("Real Time Clock Setting", "Year:"), (2000, 2099), rtc_data["rtc_y"] + 2000],
                ["rtc_m", "spb", c__("Real Time Clock Setting", "Month:"), (1, 12), rtc_data["rtc_m"]],
                ["rtc_d", "spb", c__("Real Time Clock Setting", "Day:"), (1, 31), rtc_data["rtc_d"]],
                ["rtc_h", "spb", c__("Real Time Clock Setting", "Hours:"), (0, 23), rtc_data["rtc_h"]],
                ["rtc_i", "spb", c__("Real Time Clock Setting", "Minutes:"), (0, 59), rtc_data["rtc_i"]],
                ["rtc_s", "spb", c__("Real Time Clock Setting", "Seconds:"), (0, 59), rtc_data["rtc_s"]],
                [
                    "rtc_w",
                    "cmb",
                    c__("Real Time Clock Setting", "Weekday:"),
                    [__(day) for day in list(calendar.day_name)],
                    rtc_data["rtc_w"],
                ],
                [
                    "current",
                    "chk",
                    c__("Real Time Clock Setting", "Ignore above values and use the system time instead"),
                    None,
                    False,
                ],
            ],
        }
        dlg = UserInputDialog(self, icon=self.windowIcon(), args=cast("DialogArgs", dlg_args))
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None

        result = dlg.GetResult()
        rtc_dict = {}
        for key, value in result.items():
            if isinstance(value, QtWidgets.QSpinBox):
                rtc_dict[key] = value.value()
            elif isinstance(value, QtWidgets.QComboBox):
                rtc_dict[key] = value.currentIndex()
        if result["current"].isChecked():
            dt = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=1)
            rtc_dict.update(
                {
                    "rtc_y": dt.year,
                    "rtc_m": dt.month,
                    "rtc_d": dt.day,
                    "rtc_w": dt.weekday(),
                    "rtc_h": dt.hour,
                    "rtc_i": dt.minute,
                    "rtc_s": dt.second,
                },
            )
        rtc_dict["rtc_y"] -= 2000
        return {"rtc_dict": rtc_dict}

    @staticmethod
    def _Tama5SystemTimeValues(rtc_dict: dict[str, Any], rtc_data: dict[str, Any]) -> dict[str, Any]:
        dt = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(seconds=2)
        rtc_dict.update(
            {
                "rtc_m": dt.month,
                "rtc_d": dt.day,
                "rtc_h": dt.hour,
                "rtc_i": dt.minute,
                "rtc_s": dt.second,
            },
        )
        for year in range(dt.year, 0, -1):
            if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0):
                rtc_dict["rtc_leap_year_state"] = dt.year - year
                break
        rtc_dict["rtc_y"] += 19
        rtc_dict["rtc_buffer"] = rtc_data["rtc_buffer"]
        return rtc_dict

    def EditRTC(self, _: QtGui.QMouseEvent) -> bool | None:
        if not self.CheckDeviceAlive() or not self.CheckHeader():
            return None

        data = self._device.INFO
        if (
            "dump_info" not in data
            or "has_rtc" not in data
            or data["has_rtc"] is not True
            or "rtc_dict" not in data
            or len(data["rtc_dict"]) == 0
        ):
            return None
        rtc_data = data["rtc_dict"]
        args: dict[str, Any] | Literal[False] | None = None

        if self._device.GetMode() == "DMG":
            args = self._EditDmgRTC(rtc_data)
        elif self._device.GetMode() == "AGB":
            args = self._EditAgbRTC(rtc_data)
            if args is None:
                return False

        if args is None or args is False:
            return False
        self.STATUS["args"] = args
        ret = self._device.WriteRTC(args=args)
        self.ReadCartridge(resetStatus=False)
        if ret:
            QtWidgets.QMessageBox.information(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("The Real Time Clock register values have been updated."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return True
        QtWidgets.QMessageBox.critical(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            __("An error occured while updating the Real Time Clock register values."),
            QtWidgets.QMessageBox.StandardButton.Ok,
        )
        return False

    def _EditDmgRTC(self, rtc_data: dict[str, Any]) -> dict[str, Any] | Literal[False] | None:
        """Build a DMG RTC write request from the mapper-specific dialog."""
        args: dict[str, Any] | Literal[False] | None = None
        if self._device.GetMode() == "DMG":
            mbc = get_mbc_name(ConvertMapperTypeToMapper(self.cmbDMGHeaderMapperResult.currentIndex()))
            if mbc in ("MBC3", "MBC30", "Unlicensed MBCX Mapper"):
                dlg_args = {
                    "title": __("{mapper} Real Time Clock Editor", mapper="MBC3/MBC30"),
                    "intro": __(
                        "Enter the number of days, hours, minutes and seconds that passed since the RTC initially started.",
                    )
                    + "\n\n"
                    + __(
                        "Please note that all values are internal values. The game may use these only as a relative reference.",
                    ),
                    "params": [
                        # ID, Type, Value(s), Default Index
                        [
                            "rtc_d",
                            "spb",
                            c__("Real Time Clock Setting", "Days:"),
                            (0, 511),
                            rtc_data["rtc_d"],
                        ],
                        [
                            "rtc_h",
                            "spb",
                            c__("Real Time Clock Setting", "Hours:"),
                            (0, 23),
                            rtc_data["rtc_h"],
                        ],
                        [
                            "rtc_m",
                            "spb",
                            c__("Real Time Clock Setting", "Minutes:"),
                            (0, 59),
                            rtc_data["rtc_m"],
                        ],
                        [
                            "rtc_s",
                            "spb",
                            c__("Real Time Clock Setting", "Seconds:"),
                            (0, 59),
                            rtc_data["rtc_s"],
                        ],
                        [
                            "current",
                            "chk",
                            c__(
                                "Real Time Clock Setting",
                                "Ignore above time values and use the system time instead",
                            ),
                            None,
                            False,
                        ],
                    ],
                }
                dlg = UserInputDialog(self, icon=self.windowIcon(), args=cast("DialogArgs", dlg_args))
                if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                    result = dlg.GetResult()
                    rtc_dict = self._DmgRtcDialogValues(result)
                    if result["current"].isChecked():
                        dt = datetime.datetime.now(tz=datetime.UTC).astimezone() + datetime.timedelta(seconds=1)
                        rtc_dict.update(
                            {
                                "rtc_h": dt.hour,
                                "rtc_m": dt.minute,
                                "rtc_s": dt.second,
                            },
                        )
                    mbc = ConvertMapperTypeToMapper(self.cmbDMGHeaderMapperResult.currentIndex())
                    args = {"mbc": mbc, "rtc_dict": rtc_dict}
                else:
                    return False

            elif mbc in ("HuC-3"):
                dlg_args = {
                    "title": __("{mapper} Real Time Clock Editor", mapper="HuC-3"),
                    "intro": __("Enter the number of days since your last play, and the current time.")
                    + "\n\n"
                    + __(
                        "Please note that the day value is an internal value. The game may use it only as a relative reference.",
                    ),
                    "params": [
                        # ID, Type, Value(s), Default Index
                        [
                            "rtc_d",
                            "spb",
                            c__("Real Time Clock Setting", "Days:"),
                            (0, 4095),
                            rtc_data["rtc_d"],
                        ],
                        [
                            "rtc_h",
                            "spb",
                            c__("Real Time Clock Setting", "Hours:"),
                            (0, 23),
                            rtc_data["rtc_h"],
                        ],
                        [
                            "rtc_m",
                            "spb",
                            c__("Real Time Clock Setting", "Minutes:"),
                            (0, 59),
                            rtc_data["rtc_m"],
                        ],
                        [
                            "current",
                            "chk",
                            c__(
                                "Real Time Clock Setting",
                                "Ignore above time values and use the system time instead",
                            ),
                            None,
                            False,
                        ],
                    ],
                }
                dlg = UserInputDialog(self, icon=self.windowIcon(), args=cast("DialogArgs", dlg_args))
                if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                    result = dlg.GetResult()
                    rtc_dict = self._DmgRtcDialogValues(result)
                    if result["current"].isChecked():
                        dt = datetime.datetime.now(tz=datetime.UTC).astimezone()
                        rtc_dict.update({"rtc_h": dt.hour, "rtc_m": dt.minute})
                    mbc = ConvertMapperTypeToMapper(self.cmbDMGHeaderMapperResult.currentIndex())
                    args = {"mbc": mbc, "rtc_dict": rtc_dict}
                else:
                    return False

            elif mbc in ("TAMA5"):
                dlg_args = {
                    "title": __("{mapper} Real Time Clock Editor", mapper="TAMA5"),
                    "intro": __("Enter the date and time used in the game.")
                    + "\n\n"
                    + __(
                        "Please note that the day value is an internal value. The game may use it only as a relative reference.",
                    ),
                    "params": [
                        # ID, Type, Value(s), Default Index
                        [
                            "rtc_y",
                            "spb",
                            c__("Real Time Clock Setting", "Years passed:"),
                            (0, 80),
                            rtc_data["rtc_y"] - 19,
                        ],  # 19-99
                        [
                            "rtc_leap_year_state",
                            "spb",
                            c__("Real Time Clock Setting", "Years since last leap year:"),
                            (0, 4),
                            rtc_data["rtc_leap_year_state"],
                        ],
                        [
                            "rtc_m",
                            "spb",
                            c__("Real Time Clock Setting", "Month:"),
                            (1, 12),
                            rtc_data["rtc_m"],
                        ],
                        [
                            "rtc_d",
                            "spb",
                            c__("Real Time Clock Setting", "Day:"),
                            (1, 31),
                            rtc_data["rtc_d"],
                        ],
                        [
                            "rtc_h",
                            "spb",
                            c__("Real Time Clock Setting", "Hours:"),
                            (0, 23),
                            rtc_data["rtc_h"],
                        ],
                        [
                            "rtc_i",
                            "spb",
                            c__("Real Time Clock Setting", "Minutes:"),
                            (0, 59),
                            rtc_data["rtc_i"],
                        ],
                        [
                            "rtc_s",
                            "spb",
                            c__("Real Time Clock Setting", "Seconds:"),
                            (0, 59),
                            rtc_data["rtc_s"],
                        ],
                        [
                            "current",
                            "chk",
                            c__(
                                "Real Time Clock Setting",
                                "Ignore above values and use the system time instead",
                            ),
                            None,
                            False,
                        ],
                    ],
                }
                dlg = UserInputDialog(self, icon=self.windowIcon(), args=cast("DialogArgs", dlg_args))
                if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                    result = dlg.GetResult()
                    rtc_dict = self._DmgRtcDialogValues(result)
                    if result["current"].isChecked():
                        rtc_dict = self._Tama5SystemTimeValues(rtc_dict, rtc_data)
                    mbc = ConvertMapperTypeToMapper(self.cmbDMGHeaderMapperResult.currentIndex())
                    if not result["current"].isChecked():
                        rtc_dict["rtc_y"] += 19
                        rtc_dict["rtc_buffer"] = rtc_data["rtc_buffer"]
                    args = {"mbc": mbc, "rtc_dict": rtc_dict}
                else:
                    return False

        return args

    def CheckDeviceAlive(self, setMode: PlatformMode | Literal[False] = False) -> bool:
        _ = setMode
        if self.CONN is not None:
            mode = self._device.GetMode()
            if self._device.DEVICE is None:
                self.DisconnectDevice()
            elif not self._device.IsConnected():
                self.DisconnectDevice()
                self.CONN = None
                self.DEVICES = {}
                msgbox = _create_message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Warning,
                    windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    text=__(
                        "The connection to the device was lost!\n\nThis can be happen in one of the following cases:\n- The USB cable was unplugged or is faulty\n- The inserted cartridge may draw too much peak power (try re-connecting a few times or try hotswapping the cartridge after connecting)\n- The inserted cartrdige may induce a short circuit (check for bad soldering)",
                    )
                    + "\n\n"
                    + __("Do you want to try and reconnect to the device?"),
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                )
                msgbox.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
                answer = msgbox.exec()
                if answer == QtWidgets.QMessageBox.StandardButton.No:
                    self.DisconnectDevice()
                    return False

                QtCore.QTimer.singleShot(500, lambda: [self.FindDevices(connectToFirst=True, mode=mode)])
                return False
            else:
                return True
        return False

    def _GetSelectedMode(self, mode: PlatformMode | None) -> PlatformMode | None:
        if mode == "DMG" and not self.optDMG.isChecked():
            return "AGB"
        if mode == "AGB" and not self.optAGB.isChecked():
            return "DMG"
        if self.optDMG.isChecked():
            return "DMG"
        if self.optAGB.isChecked():
            return "AGB"
        return None

    def _ConfirmModeChange(self, mode: PlatformMode | None, set_to: PlatformMode) -> bool:
        voltage_warning = ""
        device_auto_switch_only = self._device.CanSetVoltageByAutoswitch() and not self._device.CanSetVoltageByCode()
        if device_auto_switch_only:
            dont_show_again = True
        elif self._device.CanSetVoltageByCode() or self._device.CanSetVoltageByAutoswitch():
            dont_show_again = str(self.SETTINGS.value("SkipModeChangeWarning", default="disabled")).lower() == "enabled"
        elif self._device.CanSetVoltageBySwitch():
            voltage_warning = "\n\n" + __("Important: Also make sure your device is set to the correct voltage!")
            dont_show_again = False
        else:
            dont_show_again = False

        if dont_show_again or mode is None:
            return True

        check_box = _create_check_box(
            c__(
                "Check Box (& = Keyboard Shortcut)",
                "&Don't show this message again",
            ),
            checked=False,
        )
        mode_warning = (
            "\n\n"
            + __(
                "Caution: Game Boy Advance cartridges must not be inserted in Game Boy mode. Doing so can break the cartridge, so please be careful.",
            )
            if set_to == "DMG"
            else ""
        )
        message = (
            __(
                "The platform mode will now be changed to {mode} mode.",
                mode={"DMG": __("Game Boy"), "AGB": __("Game Boy Advance")}[set_to],
            )
            + mode_warning
            + voltage_warning
        )
        message_box = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Warning,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=message,
            standardButtons=QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        message_box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Ok)
        if self._device.CanSetVoltageByCode() or self._device.CanSetVoltageByAutoswitch():
            message_box.setCheckBox(check_box)
        answer = message_box.exec()
        dont_show_again = check_box.isChecked()
        if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
            if mode == "DMG":
                self.optDMG.setChecked(True)
            if mode == "AGB":
                self.optAGB.setChecked(True)
            return False
        if not device_auto_switch_only and dont_show_again:
            self.SETTINGS.setValue("SkipModeChangeWarning", "enabled")
        return True

    def SetMode(self) -> bool | None:
        mode = self._device.GetMode()
        setTo = self._GetSelectedMode(mode)
        if setTo == mode:
            return None

        if setTo is None:
            return False

        if not self._ConfirmModeChange(mode, setTo):
            return False

        if not self.CheckDeviceAlive(setMode=setTo):
            return None

        try:
            if self.optDMG.isChecked() and (mode == "AGB" or mode is None):
                self._device.SetMode("DMG")
            elif self.optAGB.isChecked() and (mode == "DMG" or mode is None):
                self._device.SetMode("AGB")
            if self._device.GetMode() is not None:
                self.mnuTools.actions()[1].setEnabled(True)
        except BrokenPipeError, SerialException:
            msg = (
                __("Failed to turn on the cartridge power.")
                + "\n"
                + __(
                    "The “{setting}” setting has therefore been disabled.",
                    setting=__("Automatic cartridge &power off").replace("&", ""),
                )
                + "\n\n"
                + __(
                    "Workaround advice:\n1. Eject the cartridge.\n2. Re-connect the USB cable.\n3. Click “{button_connect}” and select Platform mode.\n4. Insert the cartridge and click “{button_refresh}”.",
                    button_connect=__("&Connect").replace("&", ""),
                    button_refresh=__("&Refresh").replace("&", ""),
                )
            )
            self.mnuConfig.actions()[8].setChecked(False)
            self.SETTINGS.setValue("AutoPowerOff", "0")
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                msg,
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            self.DisconnectDevice()
            return False

        ok = self.ReadCartridge()
        qt_app.processEvents()
        if ok not in (False, None):
            self.btnHeaderRefresh.setEnabled(True)
            self.btnDetectCartridge.setEnabled(True)
            self.btnBackupROM.setEnabled(True)
            self.btnFlashROM.setEnabled(True)
            self.btnBackupRAM.setEnabled(True)
            self.btnRestoreRAM.setEnabled(True)
            self.grpDMGCartridgeInfo.setEnabled(True)
            self.grpAGBCartridgeInfo.setEnabled(True)
        return None

    def _DisplayDmgMapperDetails(self, data: dict[str, Any]) -> None:
        if data["mapper_raw"] == 0x203:  # Xploder GB
            self.lblDMGHeaderRtcResult.setText("")
            self.lblDMGHeaderBootlogoResult.setText("")
            self.lblDMGHeaderBootlogoResult.setStyleSheet(self.DEFAULT_STYLESHEET)
            self.lblDMGHeaderROMChecksumResult.setText("")
            self.lblDMGHeaderROMChecksumResult.setStyleSheet(self.DEFAULT_STYLESHEET)
        elif data["mapper_raw"] == 0x205:  # Datel
            self.lblDMGHeaderRtcResult.setText("")
            self.lblDMGHeaderBootlogoResult.setText("")
            self.lblDMGHeaderBootlogoResult.setStyleSheet(self.DEFAULT_STYLESHEET)
            self.lblDMGGameCodeRevisionResult.setText("")
            self.lblDMGGameCodeRevisionResult.setStyleSheet(self.DEFAULT_STYLESHEET)
            self.lblDMGHeaderROMChecksumResult.setText("")
            self.lblDMGHeaderROMChecksumResult.setStyleSheet(self.DEFAULT_STYLESHEET)
        elif data["mapper_raw"] == 0x204:  # Sachen
            self.SetDMGGameNameText(Formatter.title(data["game_title"]))
            self.lblDMGHeaderRtcResult.setText("")
            self.lblDMGRomTitleResult.setText("")
            self.lblDMGGameCodeRevisionResult.setText("")
            self.lblDMGHeaderBootlogoResult.setText("")
            self.lblDMGHeaderBootlogoResult.setStyleSheet(self.DEFAULT_STYLESHEET)
            if "logo_sachen" in data:
                data["logo_sachen"].putpalette([255, 255, 255, 128, 128, 128])
                try:
                    _set_bitmap(self.lblDMGHeaderBootlogoResult, data["logo_sachen"])
                except Exception:
                    logger.exception("Failed to display the Sachen boot logo")
        elif "logo" in data:
            if data["logo_correct"]:
                rgb = (
                    self.TEXT_COLOR[0],
                    self.TEXT_COLOR[1],
                    self.TEXT_COLOR[2],
                )  # GUI font color
                rgb = tuple(
                    min(255, int(c + (127.5 - c) * 0.25)) if c < 127.5 else max(0, int(c - (c - 127.5) * 0.25))
                    for c in rgb
                )
                data["logo"].putpalette([255, 255, 255, rgb[0], rgb[1], rgb[2]])
            else:
                data["logo"].putpalette([255, 255, 255, 251, 0, 24])
            try:
                _set_bitmap(self.lblDMGHeaderBootlogoResult, data["logo"])
            except Exception:
                logger.exception("Failed to display the Game Boy boot logo")

    def _PrepareDmgHeaderControls(self, data: Mapping[str, Any]) -> None:
        if self.cmbDMGHeaderMapperResult.count() == 0:
            self.cmbDMGHeaderMapperResult.addItems(DMG_Mapper().GetAllMapperTypes())
            self.cmbDMGHeaderMapperResult.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        if self.cmbDMGCartridgeTypeResult.count() == 0:
            self.cmbDMGCartridgeTypeResult.addItems(self._device.GetSupportedCartridgesDMG()[0])
            self.cmbDMGCartridgeTypeResult.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        if "flash_type" in data:
            self.cmbDMGCartridgeTypeResult.setCurrentIndex(data["flash_type"])
        else:
            self.cmbDMGCartridgeTypeResult.setCurrentIndex(0)
        if self.cmbDMGHeaderROMSizeResult.count() == 0:
            self.cmbDMGHeaderROMSizeResult.addItems(RomSizes().GetStringList(mode="DMG"))
            self.cmbDMGHeaderROMSizeResult.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        if self.cmbDMGHeaderSaveTypeResult.count() == 0:
            self.cmbDMGHeaderSaveTypeResult.addItems(DmgSaveTypes().GetStringList())
            self.cmbDMGHeaderSaveTypeResult.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents,
            )

    def _DisplayDmgHeaderIdentity(self, data: Mapping[str, Any]) -> None:
        self.lblDMGRomTitleResult.setText(Formatter.title(data["game_title"]))
        self.lblDMGGameCodeRevision.setText(__("Game Code and Revision:"))
        self.lblDMGGameNameResult.setToolTip("")
        if data["logo_correct"] is False:
            self.SetDMGPlatformBadge(None)
        else:
            self.SetDMGPlatformBadge(data)
        if data["db"] is not None:
            self.lblDMGGameCodeRevisionResult.setText("{:s}-{:s}".format(data["db"]["gc"], str(data["version"])))
            self.SetDMGGameNameText(data["db"]["gn"])
        else:
            self.SetDMGGameNameText(c__("Game Data", "(No database entry)"))
            if len(data["game_code"]) > 0:
                self.lblDMGGameCodeRevisionResult.setText(
                    "{:s}-{:s}".format(Formatter.title(data["game_code"]), str(data["version"])),
                )
            else:
                self.lblDMGGameCodeRevision.setText("Revision:")
                self.lblDMGGameCodeRevisionResult.setText(str(data["version"]))

        if (
            data["has_rtc"] is True
            and len(data["rtc_dict"]) > 0
            and "rtc_valid" in data["rtc_dict"]
            and data["rtc_dict"]["rtc_valid"] is True
        ):
            self.lblDMGHeaderRtcResult.setText(data["rtc_string"] + " ⚙️")
            self.lblDMGHeaderRtcResult.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.lblDMGHeaderRtcResult.setToolTip(__("Click here to edit the Real Time Clock register values"))
        else:
            self.lblDMGHeaderRtcResult.setText(data["rtc_string"])
            self.lblDMGHeaderRtcResult.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            self.lblDMGHeaderRtcResult.setToolTip("")

    def _ReadCartridgeData(self, reset_status: bool) -> dict[str, Any] | None:
        try:
            data = self._device.ReadHeader()
        except BrokenPipeError, SerialException:
            self.LimitBaudRateGBxCartRW()
            self.DisconnectDevice()
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __(
                    "The connection to the device was lost while trying to read the ROM header. This may happen if the inserted cartridge issues a short circuit or its peak power draw is too high.\n\nAs a potential workaround for the latter, you can try hotswapping the cartridge:\n1. Remove the cartridge from the device.\n2. Reconnect the device and select platform mode.\n3. Then insert the cartridge and click “{button}”.",
                    button=self.btnHeaderRefresh.text().replace("&", ""),
                ),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return None

        if not data:
            self.LimitBaudRateGBxCartRW()
            self.DisconnectDevice()
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("Invalid response from the device. Please re-connect the USB cable."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return None
        if self._device.CheckROMStable() is False and reset_status:
            QtWidgets.QMessageBox.critical(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("The cartridge connection is unstable!")
                + "\n"
                + __("Please clean the cartridge pins, carefully realign the cartridge and then try again."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
        return cast("dict[str, Any]", data)

    def _PrepareAgbHeaderControls(self, data: Mapping[str, Any], *, reset_status: bool) -> None:
        if reset_status:
            self.cmbAGBCartridgeTypeResult.clear()
            self.cmbAGBCartridgeTypeResult.addItems(self._device.GetSupportedCartridgesAGB()[0])
            self.cmbAGBCartridgeTypeResult.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
            if "flash_type" in data:
                self.cmbAGBCartridgeTypeResult.setCurrentIndex(data["flash_type"])
            else:
                self.cmbAGBCartridgeTypeResult.setCurrentIndex(0)
        if self.cmbAGBHeaderROMSizeResult.count() == 0:
            self.cmbAGBHeaderROMSizeResult.addItems(RomSizes().GetStringList())
            self.cmbAGBHeaderROMSizeResult.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
            self.cmbAGBHeaderROMSizeResult.setCurrentIndex(self.cmbAGBHeaderROMSizeResult.count() - 1)
        if self.cmbAGBSaveTypeResult.count() == 0:
            self.cmbAGBSaveTypeResult.addItems(AgbSaveTypes().GetStringList())
            self.cmbAGBSaveTypeResult.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
            self.cmbAGBSaveTypeResult.setCurrentIndex(self.cmbAGBSaveTypeResult.count() - 1)

    def _DisplayAgbRtc(self, data: Mapping[str, Any]) -> None:
        rtc_is_valid = data["has_rtc"] is True and bool(data["rtc_dict"]) and data["rtc_dict"].get("rtc_valid") is True
        if rtc_is_valid:
            self.lblAGBGpioRtcResult.setText(data["rtc_string"] + " ⚙️")
            self.lblAGBGpioRtcResult.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.lblAGBGpioRtcResult.setToolTip(__("Click here to edit the Real Time Clock register values"))
        else:
            self.lblAGBGpioRtcResult.setText(data["rtc_string"])
            self.lblAGBGpioRtcResult.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            self.lblAGBGpioRtcResult.setToolTip("")

    def _DisplayAgbLogo(self, data: dict[str, Any]) -> None:
        if "logo" not in data:
            return
        if data["logo_correct"]:
            rgb = self.TEXT_COLOR[:3]
            rgb = tuple(
                min(255, int(color + (127.5 - color) * 0.25))
                if color < 127.5
                else max(0, int(color - (color - 127.5) * 0.25))
                for color in rgb
            )
            data["logo"].putpalette([255, 255, 255, rgb[0], rgb[1], rgb[2]])
        else:
            data["logo"].putpalette([255, 255, 255, 251, 0, 24])
        try:
            _set_bitmap(self.lblAGBHeaderBootlogoResult, data["logo"])
        except Exception:
            logger.exception("Failed to display the Game Boy Advance boot logo")

    def _DisplayDmgCartridge(self, data: dict[str, Any]) -> None:
        self._PrepareDmgHeaderControls(data)
        self._DisplayDmgHeaderIdentity(data)

        if data["logo_correct"] and data["header_checksum_correct"]:
            self.lblDMGHeaderBootlogoResult.setText(c__("Game Data", "OK"))
            self.lblDMGHeaderBootlogoResult.setStyleSheet(self.DEFAULT_STYLESHEET)
            bootlogo_path = Path(AppContext.CONFIG_PATH) / "bootlogo_dmg.bin"
            if not bootlogo_path.exists():
                with bootlogo_path.open("wb") as file:
                    file.write(data["raw"][0x104:0x134])
        else:
            self.lblDMGHeaderBootlogoResult.setText(c__("Game Data", "Invalid"))
            self.lblDMGHeaderBootlogoResult.setStyleSheet("QLabel { color: red; }")

        self.lblDMGHeaderROMChecksumResult.setText("0x{:04X}".format(data["rom_checksum"]))
        self.lblDMGHeaderROMChecksumResult.setStyleSheet(self.DEFAULT_STYLESHEET)
        self.cmbDMGHeaderROMSizeResult.setCurrentIndex(data["rom_size_raw"])
        for index in range(DmgSaveTypes().GetNumberOfTypes()):
            if data["ram_size_raw"] == DmgSaveTypes(index=index).GetMbc():
                self.cmbDMGHeaderSaveTypeResult.setCurrentIndex(index)
        mapper_type = ConvertMapperToMapperType(data["mapper_raw"])[2]
        self.cmbDMGHeaderMapperResult.setCurrentIndex(mapper_type)

        if data["empty"]:
            empty_label = __("No cartridge connected") if data["empty_nocart"] else __("No ROM data detected")
            self.SetDMGGameNameText(f"({empty_label})")
            self.lblDMGGameNameResult.setStyleSheet("QLabel { color: red; }")
            self.cmbDMGHeaderROMSizeResult.setCurrentIndex(0)
            self.cmbDMGHeaderSaveTypeResult.setCurrentIndex(0)
            self.cmbDMGHeaderMapperResult.setCurrentIndex(0)
        else:
            self.lblDMGGameNameResult.setStyleSheet(self.DEFAULT_STYLESHEET)
            if data["logo_correct"] and data["game_title"] in (
                "NP M-MENU MENU",
                "DMG MULTI MENU ",
                "GBMEM-MENU MMSA",
            ):
                cart_types = self._device.GetSupportedCartridgesDMG()
                for index, profile in enumerate(cart_types[1]):
                    if "dmg-mmsa-jpn" in profile:
                        self.cmbDMGCartridgeTypeResult.setCurrentIndex(index)

        self._DisplayDmgMapperDetails(data)
        self.grpAGBCartridgeInfo.setVisible(False)
        self.grpDMGCartridgeInfo.setVisible(True)

    def _SetAgbRomSize(self, data: dict[str, Any]) -> None:
        if data["db"] is not None:
            size_index = RomSizes().GetIndex(data["db"]["rs"])
            if size_index is not None:
                self.cmbAGBHeaderROMSizeResult.setCurrentIndex(size_index)
            if data["rom_size_calc"] < 0x400000:
                self.lblAGBHeaderROMChecksumResult.setText(
                    c__("Game Data", "In database") + " (0x{:06X})".format(data["db"]["rc"]),
                )
        elif data["rom_size"] != 0:
            if data["rom_size"] not in RomSizes().GetStringList():
                data["rom_size"] = 0x2000000
            size_index = RomSizes().GetIndex(data["rom_size"])
            if size_index is not None:
                self.cmbAGBHeaderROMSizeResult.setCurrentIndex(size_index)
        else:
            self.cmbAGBHeaderROMSizeResult.setCurrentIndex(0)

    def _DisplayAgbGameName(self, data: dict[str, Any]) -> None:
        """Show the database name or fallback code for an AGB cartridge."""
        if data["db"] is not None:
            self.lblAGBHeaderGameCodeRevisionResult.setText(
                "{:s}-{:s}".format(data["db"]["gc"], str(data["version"])),
            )
            temp = data["db"]["gn"]
            self.lblAGBGameNameResult.setText(temp)
            while self.lblAGBGameNameResult.fontMetrics().boundingRect(self.lblAGBGameNameResult.text()).width() > 240:
                temp = temp[:-1]
                self.lblAGBGameNameResult.setText(temp + "…")
            if temp != data["db"]["gn"]:
                self.lblAGBGameNameResult.setToolTip(data["db"]["gn"])
        else:
            if len(data["game_code"]) > 0:
                self.lblAGBHeaderGameCodeRevisionResult.setText(
                    "{:s}-{:s}".format(Formatter.title(data["game_code"]), str(data["version"])),
                )
            else:
                self.lblAGBHeaderGameCodeRevisionResult.setText("")
            self.lblAGBGameNameResult.setText(c__("Game Data", "(No database entry)"))

    def _DisplayAgbBootLogo(self, logo_correct: bool, raw: bytes | bytearray) -> None:
        """Display AGB logo validity and cache the logo bytes on first read."""
        if logo_correct:
            self.lblAGBHeaderBootlogoResult.setText("OK")
            self.lblAGBHeaderBootlogoResult.setStyleSheet(self.lblAGBRomTitleResult.styleSheet())
            bootlogo_path = Path(AppContext.CONFIG_PATH) / "bootlogo_agb.bin"
            if not bootlogo_path.exists():
                with bootlogo_path.open("wb") as file:
                    file.write(raw[0x04:0xA0])
        else:
            self.lblAGBHeaderBootlogoResult.setText(c__("Game Data", "Invalid"))
            self.lblAGBHeaderBootlogoResult.setStyleSheet("QLabel { color: red; }")

    def _DisplayAgbCartridge(self, data: dict[str, Any], *, reset_status: bool) -> None:
        self._PrepareAgbHeaderControls(data, reset_status=reset_status)

        self.lblAGBRomTitleResult.setText(Formatter.title(data["game_title"]))
        self.lblAGBGameNameResult.setToolTip("")
        self._DisplayAgbGameName(data)

        self._DisplayAgbBootLogo(data["logo_correct"], data["raw"])

        self._DisplayAgbRtc(data)

        if data["header_checksum_correct"]:
            self.lblAGBHeaderChecksumResult.setText(
                c__("Game Data", "Valid") + " (0x{:02X})".format(data["header_checksum"]),
            )
            self.lblAGBHeaderChecksumResult.setStyleSheet(self.lblAGBRomTitleResult.styleSheet())
        else:
            self.lblAGBHeaderChecksumResult.setText(
                c__("Game Data", "Invalid") + " (0x{:02X})".format(data["header_checksum"]),
            )
            self.lblAGBHeaderChecksumResult.setStyleSheet("QLabel { color: red; }")

        self.lblAGBHeaderROMChecksumResult.setStyleSheet(self.DEFAULT_STYLESHEET)
        self.lblAGBHeaderROMChecksumResult.setText("Not available")

        if data["db"] is None:
            self.lblAGBHeaderROMChecksumResult.setText(c__("Game Data", "(No database entry)"))
        self._SetAgbRomSize(data)

        if data["save_type"] is None:
            self.cmbAGBSaveTypeResult.setCurrentIndex(0)
            if data["db"] is not None and data["db"]["st"] < AgbSaveTypes().GetNumberOfTypes():
                self.cmbAGBSaveTypeResult.setCurrentIndex(data["db"]["st"])

        if data["empty"]:  # defaults
            if data["empty_nocart"]:
                self.lblAGBGameNameResult.setText("(" + __("No cartridge connected") + ")")
            else:
                self.lblAGBGameNameResult.setText("(" + __("No ROM data detected") + ")")
            self.lblAGBGameNameResult.setStyleSheet("QLabel { color: red; }")
            self.cmbAGBSaveTypeResult.setCurrentIndex(0)
        else:
            self.lblAGBGameNameResult.setStyleSheet(self.DEFAULT_STYLESHEET)
            if data["logo_correct"]:
                cart_types = self._device.GetSupportedCartridgesAGB()
                for i in range(len(cart_types[0])):
                    if (data["3d_memory"] is True and "3d_memory" in cart_types[1][i]) or (
                        data["vast_fame"] is True and "vast_fame" in cart_types[1][i]
                    ):
                        self.cmbAGBCartridgeTypeResult.setCurrentIndex(i)

        if data["dacs_8m"] is True:
            self.cmbAGBSaveTypeResult.setCurrentIndex(6)

        self.grpDMGCartridgeInfo.setVisible(False)
        self.grpAGBCartridgeInfo.setVisible(True)

        if (
            data["logo_correct"]
            and isinstance(data["db"], dict)
            and "rs" in data["db"]
            and data["db"]["rs"] == 0x4000000
            and not self._device.IsSupported3dMemory()
            and reset_status
        ):
            QtWidgets.QMessageBox.warning(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __(
                    "This cartridge uses a mapper that may not be completely supported by the firmware of the {device_name}. Check for firmware updates.",
                    device_name=self._device.GetFullName(),
                ),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )

        self._DisplayAgbLogo(data)

    def ReadCartridge(self, resetStatus: bool = True) -> bool | None:
        if self.CheckDeviceAlive() is not True:
            return None
        self._UpdatePlatformModeFromFirmware()
        if resetStatus:
            self.btnHeaderRefresh.setEnabled(False)
            self.btnDetectCartridge.setEnabled(False)
            self.btnBackupROM.setEnabled(False)
            self.btnFlashROM.setEnabled(False)
            self.btnBackupRAM.setEnabled(False)
            self.btnRestoreRAM.setEnabled(False)
            self.lblStatus4a.setText(__("Reading cartridge data..."))
            self.SetProgressBars(min=0, max=0, value=1)
            qt_app.processEvents()

        data = self._ReadCartridgeData(resetStatus)
        if data is None:
            return False

        if self._device.GetMode() == "DMG":
            self._DisplayDmgCartridge(data)

        elif self._device.GetMode() == "AGB":
            self._DisplayAgbCartridge(data, reset_status=resetStatus)

        if resetStatus:
            self.lblStatus1aResult.setText("-")
            self.lblStatus2aResult.setText("-")
            self.lblStatus3aResult.setText("-")
            self.lblStatus4a.setText(__("Ready."))
            self.grpStatus.setTitle(__("Transfer Status"))
            self.FinishOperation()
            self.btnHeaderRefresh.setEnabled(True)
            self.btnDetectCartridge.setEnabled(True)
            self.btnBackupROM.setEnabled(True)
            self.btnFlashROM.setEnabled(True)
            self.btnBackupRAM.setEnabled(True)
            self.btnRestoreRAM.setEnabled(True)
            self.btnHeaderRefresh.setFocus()
            self.SetProgressBars(min=0, max=100, value=0)
            qt_app.processEvents()

        if data["game_title"][:11] == "YJencrypted" and resetStatus:
            QtWidgets.QMessageBox.warning(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __(
                    "This cartridge may be protected against reading or writing a ROM. If you don't want to risk this cartridge to render itself unusable, please do not try to write a new ROM to it.",
                ),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
        return None

    def LimitBaudRateGBxCartRW(self) -> None:
        if (
            self._IsGBxCartRWDevice(self._device)
            and str(self.SETTINGS.value("AutoLimitBaudRate", default="enabled")).lower() == "enabled"
            and self._GetGBxCartRWBaudRate() != min(GBXCART_RW_BAUD_RATES)
        ):
            baudrate = min(GBXCART_RW_BAUD_RATES)
            dprint(f"Setting “GBxCart RW baud rate” to “{baudrate:d}”")
            self._UpdateGBxCartRWBaudRateActions(baudrate)
            self.SETTINGS.setValue("GBxCartRWBaudRate", str(baudrate))
            dprint("Setting “" + self.mnuConfig.actions()[8].text().replace("&", "") + "” to “0”")
            self.mnuConfig.actions()[8].setChecked(False)
            self.SETTINGS.setValue("AutoPowerOff", "0")
            try:
                self._device.ChangeBaudRate(baudrate=baudrate)
            except Exception:
                logger.exception("Failed to change the device baud rate")
                try:
                    self.DisconnectDevice()
                except Exception:
                    logger.exception("Failed to disconnect after changing the baud rate")

    def DetectCartridge(self, checkSaveType: bool = True) -> None:
        if not self.CheckDeviceAlive():
            return
        if not self._device.CheckROMStable():
            answer = QtWidgets.QMessageBox.warning(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("The cartridge connection is unstable!")
                + "\n"
                + __("Please clean the cartridge pins, carefully realign the cartridge for best results.")
                + "\n\n"
                + __("Continue anyway?"),
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.No:
                return
        self.btnHeaderRefresh.setEnabled(False)
        self.btnDetectCartridge.setEnabled(False)
        self.btnBackupROM.setEnabled(False)
        self.btnFlashROM.setEnabled(False)
        self.btnBackupRAM.setEnabled(False)
        self.btnRestoreRAM.setEnabled(False)
        self.grpStatus.setTitle(__("Transfer Status"))
        self.lblStatus1aResult.setText("-")
        self.lblStatus2aResult.setText("-")
        self.lblStatus3aResult.setText("-")
        self.SetStatus4aResult("")
        # self.lblStatus4a.setText("Analyzing Cartridge...")
        self.SetProgressBars(min=0, max=0, value=1)
        qt_app.processEvents()

        if "can_skip_message" not in self.STATUS:
            self.STATUS["can_skip_message"] = False
        limitVoltage = str(self.SETTINGS.value("AutoDetectLimitVoltage", default="disabled")).lower() == "enabled"
        self._device.DetectCartridge(
            fncSetProgress=self.PROGRESS.SetProgress,
            args={"limitVoltage": limitVoltage, "checkSaveType": checkSaveType},
        )

    @staticmethod
    def _format_gb_memory_detection_message(header: Mapping[str, Any]) -> str:
        parsed = header.get("gbmem_parsed")
        if parsed is None:
            return ""

        message = "<br><b>" + __("{gb_memory_cartridge} Data:", gb_memory_cartridge="GB-Memory Cartridge") + "</b><br>"
        if isinstance(parsed, list):
            first_entry = parsed[0]
            message += (
                "- "
                + __("Write Timestamp:")
                + " {timestamp:s}<br>".format(timestamp=first_entry["timestamp"].replace("\0", ""))
                + "- "
                + __("Write Kiosk ID:")
                + " {kiosk_id:s}<br>".format(kiosk_id=first_entry["kiosk_id"].replace("\0", ""))
                + "- "
                + __("Number of Games:")
                + " {num_games:d}<br>".format(num_games=first_entry["num_games"])
                + "- "
                + __("Write Counter:")
                + " {write_count:d}<br>".format(write_count=first_entry["write_count"])
                + "- "
                + __("Cartridge ID:")
                + " {cart_id:s}<br>".format(cart_id=first_entry["cart_id"].replace("\0", ""))
            )
            for index, game in enumerate(parsed[1:], start=1):
                if game["menu_index"] == 0xFF:
                    continue
                label = __("Menu ROM:") if index == 1 else __("Game {number}:", number=index - 1)
                message += "- " + label + " {:s}<br>".format(game["title"].replace("\0", ""))
            return message

        return message + (
            "- "
            + __("Write Timestamp:")
            + " {timestamp:s}<br>".format(timestamp=parsed["timestamp"].replace("\0", ""))
            + "- "
            + __("Write Kiosk ID:")
            + " {kiosk_id:s}<br>".format(kiosk_id=parsed["kiosk_id"].replace("\0", ""))
            + "- "
            + __("Write Counter:")
            + " {write_count:d}<br>".format(write_count=parsed["write_count"])
            + "- "
            + __("Cartridge ID:")
            + " {cart_id:s}<br>".format(cart_id=parsed["cart_id"].replace("\0", ""))
            + "- "
            + __("Game Title:")
            + " {game_title:s}<br>".format(game_title=parsed["title"].replace("\0", ""))
        )

    def _ResumeDetectedCartridgeAction(self, cart_type: int | None) -> None:
        waiting = None
        if "detected_cart_type" in self.STATUS and self.STATUS["detected_cart_type"] in (
            "WAITING_FLASH",
            "WAITING_SAVE_READ",
            "WAITING_SAVE_WRITE",
        ):
            waiting = self.STATUS["detected_cart_type"]
            self.STATUS["detected_cart_type"] = cart_type
        self.STATUS["can_skip_message"] = False

        if waiting == "WAITING_FLASH":
            if "detect_cartridge_args" in self.STATUS:
                self.FlashROM(dpath=self.STATUS["detect_cartridge_args"]["dpath"])
                del self.STATUS["detect_cartridge_args"]
            else:
                self.FlashROM()
        elif waiting == "WAITING_SAVE_READ":
            if "detect_cartridge_args" in self.STATUS:
                self.BackupRAM(dpath=self.STATUS["detect_cartridge_args"]["dpath"])
                del self.STATUS["detect_cartridge_args"]
            else:
                self.BackupRAM()
        elif waiting == "WAITING_SAVE_WRITE":
            if "detect_cartridge_args" in self.STATUS:
                self.WriteRAM(
                    dpath=self.STATUS["detect_cartridge_args"]["dpath"],
                    erase=self.STATUS["detect_cartridge_args"]["erase"],
                    skip_warning=True,
                )
                del self.STATUS["detect_cartridge_args"]
            else:
                self.WriteRAM()

    def _FormatDetectedSaveType(
        self,
        save_size: int,
        save_type: int | None,
        save_chip: str | None,
        sram_unstable: bool,
        header: dict[str, Any],
    ) -> str:
        if self.STATUS["can_skip_message"] or save_type is False or save_type is None:
            return ""

        if save_chip is not None:
            if (
                save_type == 5
                and "Unlicensed" in save_chip
                and "data" in self._device.INFO
                and self._device.INFO["data"] == bytearray([0xFF] * len(self._device.INFO["data"]))
            ):
                description = (
                    f"{AgbSaveTypes().GetStringList()[4]:s} or {AgbSaveTypes().GetStringList()[5]:s} ({save_chip:s})"
                )
            else:
                description = f"{AgbSaveTypes().GetStringList()[save_type]:s} ({save_chip:s})"
        elif self._device.GetMode() == "DMG":
            try:
                description = f"{DmgSaveTypes(mbc=save_type).GetString():s}"
            except IndexError, KeyError, TypeError, ValueError:
                description = "Unknown"
        elif self._device.GetMode() == "AGB":
            description = f"{AgbSaveTypes().GetStringList()[save_type]:s}"
            try:
                if "Batteryless SRAM" in AgbSaveTypes().GetStringList()[save_type]:
                    description += _format_batteryless_sram_details(save_size, header["batteryless_sram"])
            except Exception:
                logger.exception("Failed to format batteryless SRAM details")
        else:
            description = ""

        if save_type == 0:
            if save_chip and "Unknown" not in save_chip:
                return "<b>" + __("Save Type:") + f"</b> {save_chip:s}<br>"
            return (
                "<b>"
                + __("Save Type:")
                + "</b> "
                + c__("Save Type", "None or unknown (no save data detected)")
                + "<br>"
            )
        if sram_unstable and "SRAM" in description:
            return (
                "<b>"
                + __("Save Type:")
                + '</b> {:s} <span style="color: red;">('
                + c__("Save Data Access", "not stable or not battery-backed")
                + ")</span><br>"
            )
        return "<b>" + __("Save Type:") + f"</b> {description:s}<br>"

    def _SelectDetectedCartType(
        self,
        mode: PlatformMode,
        cart_types: Sequence[int],
        cart_type_id: int,
    ) -> tuple[int | None, str, str, tuple[list[str], list[Any]]] | None:
        try:
            supported_cart_types = (
                self._device.GetSupportedCartridgesDMG() if mode == "DMG" else self._device.GetSupportedCartridgesAGB()
            )
        except Exception as error:
            msgbox = _create_message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Critical,
                windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                text=__("An unknown error occured. Please try again.") + "\n\n" + str(error),
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            msgbox.exec()
            self.LimitBaudRateGBxCartRW()
            return None

        cart_type = None
        cart_type_message = ""
        selected_name = ""
        try:
            if cart_types:
                cart_type = cart_type_id
                combo = self.cmbDMGCartridgeTypeResult if mode == "DMG" else self.cmbAGBCartridgeTypeResult
                combo.setCurrentIndex(0)
                combo.setCurrentIndex(cart_type)
                self.STATUS["cart_type"] = supported_cart_types[1][cart_type]
                for compatible_type in cart_types:
                    name = supported_cart_types[0][compatible_type]
                    if compatible_type == cart_type_id:
                        cart_type_message += "- {:s} ← {:s}<br>".format(
                            name,
                            c__("Flashcart Profile List “- PROFILE NAME ← selected”", "selected"),
                        )
                        selected_name = name
                    else:
                        cart_type_message += f"- {name:s}<br>"
                cart_type_message = cart_type_message[:-4]
        except Exception:
            logger.exception("Failed to select the detected flash-cart type")
        return cart_type, cart_type_message, selected_name, supported_cart_types

    @staticmethod
    def _FormatFlashDetectionDetails(
        flash_id: str,
        cfi_data: str,
        *,
        is_generic: bool,
        limit_voltage: bool,
    ) -> tuple[str, str]:
        flash_id_message = ""
        if len(flash_id.split("\n")) > 2:
            title = __("Flash ID Check (limited voltage):") if limit_voltage else __("Flash ID Check:")
            flash_id_message = (
                "<br><b>" + title + f'</b><pre style="font-size: 8pt; margin: 0;">{flash_id[:-1]:s}</pre>'
            )
        if is_generic:
            return flash_id_message, ""
        label = __("{common_flash_interface} Data:", common_flash_interface="Common Flash Interface")
        if cfi_data:
            cfi_message = "<br><b>" + label + "</b><br>{:s}<br><br>".format(cfi_data.replace("\n", "<br>"))
        else:
            cfi_message = "<br><b>" + label + "</b> " + c__("Common Flash Interface Data", "Not available") + "<br><br>"
        return flash_id_message, cfi_message

    def _FormatDetectedFlashMapper(self, cart_config: Mapping[str, Any]) -> str:
        if self._device.GetMode() != "DMG":
            return ""
        if "mbc" not in cart_config:
            return "<b>" + __("Mapper Type:") + "</b> " + c__("Mapper Type", "Default") + " (MBC5)<br>"

        mapper = cart_config["mbc"]
        if mapper == "manual":
            return "<b>" + __("Mapper Type:") + "</b> <i>" + __("Manual selection") + "</i><br>"
        if mapper in DMG_Mapper().GetAllMapperIds():
            return "<b>" + __("Mapper Type:") + f"</b> {DMG_Mapper().GetMapperType(mapper):s}<br>"
        return ""

    @staticmethod
    def _FormatDetectedCartProfile(cart_types: Sequence[int], selected_name: str) -> str:
        if len(cart_types) > 1:
            return (
                "<b>"
                + __("Flashcart Profile:")
                + f"</b> {selected_name:s} ("
                + c__(
                    "Flashcart Profile: PROFILE NAME (or compatble)",
                    "or compatible",
                )
                + ")<br>"
            )
        return "<b>" + __("Flashcart Profile:") + f"</b> {selected_name:s}<br>"

    def _FormatDetectedCartDetails(self, context: _DetectedCartProfileContext) -> _DetectedCartDetails:
        cart_type_message = ""
        cart_type_details = ""
        flash_size_message = ""
        flash_mapper_message = ""
        generic_profile = None
        found_supported = False
        is_generic = False

        if context.cart_type is not None:
            cart_type_message = self._FormatDetectedCartProfile(context.cart_types, context.selected_name)
            cart_type_details = (
                "<b>" + __("Compatible Flashcart Profiles:") + f"</b><br>{context.compatible_profiles:s}<br>"
            )
            found_supported = True
            size = (
                context.detected_size
                if context.detected_size > 0
                else context.supported_cart_types[1][context.cart_type_id].get("flash_size", 0)
            )
            if size > 0:
                flash_size_message = "<b>" + __("ROM Size:") + f"</b> {Formatter.file_size(size, as_int=True):s}<br>"
            flash_mapper_message = self._FormatDetectedFlashMapper(
                context.supported_cart_types[1][context.cart_type_id],
            )
        elif (len(context.flash_id.split("\n")) > 2) and (
            self._device.GetMode() == "DMG" or ("dacs_8m" in context.header and context.header["dacs_8m"] is not True)
        ):
            cart_type_message = "<b>" + __("Flashcart Profile:") + "</b> " + __("Unknown flash cartridge")
            generic_profile = _generic_flash_profile(context.flash_id)
            if generic_profile is not None:
                cart_type_message += " " + __(
                    "For ROM writing, you can give the option called “{option}” a try at your own risk.",
                    option=generic_profile,
                )
            cart_type_message += "<br>"
        else:
            cart_type_message = (
                "<b>"
                + __("Flashcart Profile:")
                + "</b> "
                + "Generic ROM Cartridge"
                + " ("
                + __("not rewritable or not auto-detectable")
                + ")"
                + "<br>"
            )
            is_generic = True

        flash_id_message, cfi_message = self._FormatFlashDetectionDetails(
            context.flash_id,
            context.cfi_data,
            is_generic=is_generic,
            limit_voltage=context.limit_voltage,
        )
        return _DetectedCartDetails(
            cart_type_message,
            cart_type_details or cart_type_message,
            flash_size_message,
            flash_id_message,
            cfi_message,
            flash_mapper_message,
            generic_profile,
            found_supported,
            is_generic,
        )

    def _ApplyDetectedSaveType(self, save_type: int | Literal[False] | None) -> None:
        if self.STATUS["can_skip_message"] or save_type is None or save_type is False:
            return
        try:
            if self._device.GetMode() == "DMG":
                save_index = DmgSaveTypes(mbc=save_type).GetIndex()
                if save_index is not None:
                    self.cmbDMGHeaderSaveTypeResult.setCurrentIndex(save_index)
            elif self._device.GetMode() == "AGB":
                self.cmbAGBSaveTypeResult.setCurrentIndex(save_type)
        except Exception:
            logger.exception("Failed to select the detected save type")

    def _ResetDetectionControls(self) -> None:
        self.btnHeaderRefresh.setEnabled(True)
        self.btnDetectCartridge.setEnabled(True)
        self.btnBackupROM.setEnabled(True)
        self.btnFlashROM.setEnabled(True)
        self.btnBackupRAM.setEnabled(True)
        self.btnRestoreRAM.setEnabled(True)
        self.btnHeaderRefresh.setFocus()
        self.SetProgressBars(min=0, max=100, value=0)
        self.lblStatus4a.setText(__("Ready."))

    def _ResetDetectionLabels(self) -> None:
        self.lblStatus1aResult.setText("-")
        self.lblStatus2aResult.setText("-")
        self.lblStatus3aResult.setText("-")

    def _HandleFailedCartridgeDetection(self) -> None:
        QtWidgets.QMessageBox.critical(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            __(
                "An error occured while trying to analyze the cartridge and you may need to physically reconnect the device.",
            )
            + "\n\n"
            + __("This cartridge may not be auto-detectable, please select the flashcart profile manually."),
            QtWidgets.QMessageBox.StandardButton.Ok,
        )
        self.LimitBaudRateGBxCartRW()
        self.DisconnectDevice()

    def _RetryDetectionWithoutVoltageLimit(
        self,
        *,
        limit_voltage: bool,
        is_generic: bool,
        found_supported: bool,
    ) -> bool:
        if self._device.GetMode() != "DMG" or not limit_voltage or (not is_generic and found_supported):
            return False
        text = __(
            "No known flashcart profile could be detected. The option “{limit_voltage}” has been enabled which can cause auto-detection to fail. As it is usually not recommended to enable this option, do you now want to disable it and try again?",
            limit_voltage=__("&Limit voltage when analyzing Game Boy carts").replace("&", ""),
        )
        answer = QtWidgets.QMessageBox.warning(
            self,
            f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.Yes,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return False
        self.SETTINGS.setValue("AutoDetectLimitVoltage", "disabled")
        self.mnuConfig.actions()[4].setChecked(False)
        self.STATUS["can_skip_message"] = False
        self.DetectCartridge()
        return True

    @staticmethod
    def _AddGenericProfileButton(
        msgbox: QtWidgets.QMessageBox,
        profile_name: str | None,
    ) -> QtWidgets.QPushButton | None:
        if profile_name is None:
            return None
        button = msgbox.addButton(
            c__(
                "Button (& = Keyboard Shortcut)",
                "&Try “{generic_type}”",
                generic_type="Generic Type",
            ),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        button.setToolTip(profile_name)
        return button

    @staticmethod
    def _CopyHtmlToClipboard(html: str) -> None:
        clipboard = QtWidgets.QApplication.clipboard()
        doc = QtGui.QTextDocument()
        doc.setHtml(html)
        clipboard.setText(doc.toPlainText())

    def _ShowSupportedDetectionSummary(self, summary: str) -> tuple[bool, bool]:
        dont_show_again = str(self.SETTINGS.value("SkipAutodetectMessage", default="disabled")).lower() == "enabled"
        if dont_show_again and self.STATUS["can_skip_message"]:
            return False, False

        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Information,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s} | {self._device.GetFullNameLabel():s}",
            text=summary[:-4],
        )
        msgbox.setTextFormat(QtCore.Qt.TextFormat.RichText)
        button_ok = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&OK"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        button_details = msgbox.addButton(
            c__("Button (& = Keyboard Shortcut)", "&Details"),
            QtWidgets.QMessageBox.ButtonRole.ActionRole,
        )
        button_cancel = None
        msgbox.setDefaultButton(button_ok)
        checkbox = _create_check_box(
            c__("Check Box (& = Keyboard Shortcut)", "&Always skip this message"),
            checked=False,
        )
        if self.STATUS["can_skip_message"]:
            button_cancel = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "&Cancel"),
                QtWidgets.QMessageBox.ButtonRole.RejectRole,
            )
            msgbox.setEscapeButton(button_cancel)
            msgbox.setCheckBox(checkbox)
        else:
            msgbox.setEscapeButton(button_ok)

        msgbox.exec()
        if checkbox.isChecked() and self.STATUS["can_skip_message"]:
            self.SETTINGS.setValue("SkipAutodetectMessage", "enabled")
        if msgbox.clickedButton() == button_cancel:
            self._ResetDetectionControls()
            self.STATUS["can_skip_message"] = False
            self.STATUS.pop("detected_cart_type", None)
            return False, True
        return msgbox.clickedButton() == button_details, False

    def _DetectionFirmwareFooter(
        self,
        msgbox: QtWidgets.QMessageBox,
        is_generic: bool,
    ) -> tuple[str, QtWidgets.QAbstractButton | None]:
        if not is_generic:
            footer = f'<br><span style="font-size: 8pt;"><i>{AppInfo.NAME:s} {AppInfo.VERSION:s} | {self._device.GetFullNameExtended():s}</i></span><br>'
            button = msgbox.addButton(
                c__("Button (& = Keyboard Shortcut)", "&Copy to Clipboard"),
                QtWidgets.QMessageBox.ButtonRole.ActionRole,
            )
            return footer, button
        return "", None

    def FinishDetectCartridge(self, ret: object) -> None:
        self._ResetDetectionLabels()

        limitVoltage = str(self.SETTINGS.value("AutoDetectLimitVoltage", default="disabled")).lower() == "enabled"
        if ret is False or not isinstance(ret, (list, tuple)) or len(ret) < 11:
            cart_type = self._HandleFailedCartridgeDetection()
        else:
            (
                header,
                save_size,
                save_type,
                save_chip,
                sram_unstable,
                cart_types,
                cart_type_id,
                cfi_s,
                _,
                flash_id,
                detected_size,
            ) = ret

            self._ApplyDetectedSaveType(save_type)

            # Cart Type
            mode = self._device.GetMode()
            if mode not in ("DMG", "AGB"):
                msgbox = _create_message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Critical,
                    windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    text=__("An unknown error occured. Please try again."),
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                )
                msgbox.exec()
                self.LimitBaudRateGBxCartRW()
                return

            cart_selection = self._SelectDetectedCartType(mode, cart_types, cart_type_id)
            if cart_selection is None:
                return
            cart_type, msg_cart_type, msg_cart_type_used, supp_cart_types = cart_selection

            # Messages
            # Header
            msg_header_s = "<b>" + __("ROM Title:") + "</b> {:s}<br>".format(Formatter.title(header["game_title"]))

            # Save Type
            msg_save_type_s = self._FormatDetectedSaveType(
                save_size,
                save_type,
                save_chip,
                sram_unstable,
                header,
            )

            details = self._FormatDetectedCartDetails(
                _DetectedCartProfileContext(
                    header,
                    cart_type,
                    cart_types,
                    cart_type_id,
                    supp_cart_types,
                    msg_cart_type,
                    msg_cart_type_used,
                    detected_size,
                    flash_id,
                    cfi_s,
                    limitVoltage,
                ),
            )
            (
                msg_cart_type_s,
                msg_cart_type_s_detail,
                msg_flash_size_s,
                msg_flash_id_s,
                msg_cfi_s,
                msg_flash_mapper_s,
                try_this,
                found_supported,
                is_generic,
            ) = details
            self.SetProgressBars(min=0, max=100, value=100)
            show_details = False

            msg_gbmem = self._format_gb_memory_detection_message(header)

            msg = __("The following cartridge configuration was detected:") + "<br><br>"
            if found_supported:
                summary = f"{msg:s}{msg_flash_size_s:s}{msg_save_type_s:s}{msg_flash_mapper_s:s}{msg_cart_type_s:s}{msg_gbmem:s}"
                show_details, cancelled = self._ShowSupportedDetectionSummary(summary)
                if cancelled:
                    return
                if show_details:
                    msg = ""

            if not found_supported or show_details is True:
                msgbox = _create_message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Information,
                    windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s} | {self._device.GetFullNameLabel():s}",
                )
                button_ok = msgbox.addButton(
                    c__("Button (& = Keyboard Shortcut)", "&OK"),
                    QtWidgets.QMessageBox.ButtonRole.ActionRole,
                )
                msgbox.setDefaultButton(button_ok)
                msgbox.setEscapeButton(button_ok)
                button_try = self._AddGenericProfileButton(msgbox, try_this)

                msg_fw, button_clipboard = self._DetectionFirmwareFooter(msgbox, is_generic)

                if self._RetryDetectionWithoutVoltageLimit(
                    limit_voltage=limitVoltage,
                    is_generic=is_generic,
                    found_supported=found_supported,
                ):
                    return

                temp = f"{msg:s}{msg_header_s:s}{msg_flash_size_s:s}{msg_save_type_s:s}{msg_flash_mapper_s:s}{msg_flash_id_s:s}{msg_cfi_s:s}{msg_cart_type_s_detail:s}{msg_gbmem:s}{msg_fw:s}"
                temp = temp[:-4]
                msgbox.setText(temp)
                msgbox.setTextFormat(QtCore.Qt.TextFormat.RichText)
                msgbox.exec()
                if msgbox.clickedButton() == button_clipboard:
                    self._CopyHtmlToClipboard(temp)
                elif msgbox.clickedButton() == button_try:
                    if try_this in supp_cart_types[0]:
                        cart_type = supp_cart_types[0].index(try_this)
                    if not isinstance(cart_type, int):
                        return
                    if self._device.GetMode() == "DMG":
                        self.cmbDMGCartridgeTypeResult.setCurrentIndex(cart_type)
                    elif self._device.GetMode() == "AGB":
                        self.cmbAGBCartridgeTypeResult.setCurrentIndex(cart_type)

        self._ResetDetectionControls()

        self._ResumeDetectedCartridgeAction(cart_type)

    def WaitProgress(self, args: Mapping[str, Any]) -> None:
        if args["user_action"] == "REINSERT_CART":
            title = f"{AppInfo.NAME:s} {AppInfo.VERSION:s}"
            if "title" in args:
                title += " - " + args["title"]
            msg = args["msg"]
            answer = QtWidgets.QMessageBox.warning(
                self,
                title,
                msg,
                QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Ok:
                cast("Any", self._device).USER_ANSWER = True
            else:
                cast("Any", self._device).USER_ANSWER = False
        elif args["user_action"] == "RETRY_5V":
            title = f"{AppInfo.NAME:s} {AppInfo.VERSION:s}"
            if "title" in args:
                title += " - " + args["title"]
            msg = args["msg"]
            answer = QtWidgets.QMessageBox.question(
                self,
                title,
                msg,
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                cast("Any", self._device).USER_ANSWER = True
            else:
                cast("Any", self._device).USER_ANSWER = False

    def _UpdateProgressMethodTitle(self, args: Mapping[str, Any]) -> None:
        voltage_suffix = ""
        if "voltage" in args and args["voltage"] in (3.3, 5):
            voltage_suffix = " " + __(
                "at {voltage}V",
                voltage=format_decimal(args["voltage"], precision=1),
            )
        if args["method"] == "ROM_READ":
            self.grpStatus.setTitle(__("Transfer Status") + " (" + __("Backup ROM") + ")")
        elif args["method"] == "ROM_WRITE":
            self.grpStatus.setTitle(__("Transfer Status") + " (" + __("Write ROM") + voltage_suffix + ")")
        elif args["method"] == "ROM_WRITE_VERIFY":
            self.grpStatus.setTitle(__("Transfer Status") + " (" + __("Verify Flash") + ")")
        elif args["method"] == "SAVE_READ":
            self.grpStatus.setTitle(__("Transfer Status") + " (" + __("Backup Save Data") + ")")
        elif args["method"] == "SAVE_WRITE":
            self.grpStatus.setTitle(__("Transfer Status") + " (" + __("Write Save Data") + ")")
        elif args["method"] == "SAVE_WRITE_VERIFY":
            self.grpStatus.setTitle(__("Transfer Status") + " (" + __("Verify Save Data") + ")")
        elif args["method"] == "DETECT_CART":
            self.grpStatus.setTitle(__("Transfer Status") + " (" + __("Analyze Cartridge") + ")")

    def _HandleProgressAbort(self, args: Mapping[str, Any]) -> None:
        watchdog = 10
        try:
            while cast("Any", self._device.WORKER).isRunning():
                time.sleep(0.1)
                watchdog -= 1
                if watchdog == 0:
                    break
        except AttributeError:
            return
        self._device.CANCEL = False
        self._device.ERROR = False
        self.grpDMGCartridgeInfo.setEnabled(True)
        self.grpAGBCartridgeInfo.setEnabled(True)
        self.grpActions.setEnabled(True)
        self.mnuTools.setEnabled(True)
        self.mnuConfig.setEnabled(True)
        self.mnuLanguage.setEnabled(True)
        self.grpStatus.setTitle(__("Transfer Status"))
        self.lblStatus1aResult.setText("-")
        self.lblStatus2aResult.setText("-")
        self.lblStatus3aResult.setText("-")
        self.lblStatus4a.setText(__("Stopped."))
        self.SetStatus4aResult("")
        self.btnCancel.setEnabled(False)
        self.SetProgressBars(min=0, max=1, value=0)

        if "info_type" in args and "info_msg" in args:
            info_type = args["info_type"]
            if info_type in ("msgbox_critical", "msgbox_information"):
                icon = (
                    QtWidgets.QMessageBox.Icon.Critical
                    if info_type == "msgbox_critical"
                    else QtWidgets.QMessageBox.Icon.Information
                )
                msgbox = _create_message_box(
                    parent=self,
                    icon=icon,
                    windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    text=args["info_msg"],
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                )
                dprint(
                    "Queueing Message Box {:s}:\n----\n{:s} {:s}\n----\n{:s}\n----".format(
                        str(msgbox),
                        AppInfo.NAME,
                        AppInfo.VERSION,
                        args["info_msg"],
                    ),
                )
                if "\n" not in args["info_msg"]:
                    msgbox.setTextFormat(QtCore.Qt.TextFormat.RichText)
                self.MSGBOX_QUEUE.put(msgbox)
                if info_type == "msgbox_critical":
                    self.WriteDebugLog()
                    if "fatal" in args:
                        self.LimitBaudRateGBxCartRW()
                        self.DisconnectDevice()
            elif info_type == "label":
                self.lblStatus4a.setText(args["info_msg"])

        QtCore.QTimer.singleShot(1, lambda: [self.ReadCartridge(resetStatus=False)])

    def _SetProgressControlsEnabled(self, enabled: bool) -> None:
        self.grpDMGCartridgeInfo.setEnabled(enabled)
        self.grpAGBCartridgeInfo.setEnabled(enabled)
        self.grpActions.setEnabled(enabled)
        self.mnuTools.setEnabled(enabled)
        self.mnuConfig.setEnabled(enabled)
        self.mnuLanguage.setEnabled(enabled)

    def _ShowProgressError(self, error: object) -> None:
        self.lblStatus4a.setText(__("Failed!"))
        self._SetProgressControlsEnabled(enabled=True)
        self.btnCancel.setEnabled(False)
        error_text = str(error)
        msgbox = _create_message_box(
            parent=self,
            icon=QtWidgets.QMessageBox.Icon.Critical,
            windowTitle=f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
            text=error_text,
            standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
        )
        if "\n" not in error_text:
            msgbox.setTextFormat(QtCore.Qt.TextFormat.RichText)
        msgbox.exec()
        self.LimitBaudRateGBxCartRW()

    def _MaybeUpdateProgressMethodTitle(self, args: Mapping[str, Any]) -> None:
        if "method" in args:
            self._UpdateProgressMethodTitle(args)

    def _UpdateTransferProgress(
        self,
        args: Mapping[str, Any],
    ) -> None:
        pos = args.get("pos", 0)
        size = args.get("size", 0)
        speed = args.get("speed", 0)
        left = args.get("time_left", 0)
        elapsed = args.get("time_elapsed", 0)
        self.SetProgressBars(min=0, max=size, value=pos)
        self.btnCancel.setEnabled(args.get("abortable", True))
        self.lblStatus1aResult.setText(f"{Formatter.file_size(pos):s}")
        if speed > 0:
            self.lblStatus2aResult.setText(format_decimal(speed, precision=2) + __(" KiB/s"))
        else:
            self.lblStatus2aResult.setText(__("Pending..."))
        if left > 0:
            self.SetStatus4aResult(Formatter.progress_time(left))
        else:
            self.SetStatus4aResult(__("Pending..."))
        if elapsed > 0:
            self.lblStatus3aResult.setText(Formatter.progress_time(elapsed))

        if speed == 0 and "skipping" in args and args["skipping"] is True:
            self.SetStatus4aResult(__("Pending..."))
        self.lblStatus4a.setText(__("Time left:"))

    def UpdateProgress(self, args: Mapping[str, Any] | None) -> None:
        if args is None or self.CONN is None:
            return

        self._MaybeUpdateProgressMethodTitle(args)

        if "error" in args:
            self._ShowProgressError(args["error"])
            return

        self._SetProgressControlsEnabled(enabled=False)

        pos = args.get("pos", 0)
        size = args.get("size", 0)
        elapsed = args.get("time_elapsed", 0)
        estimated = args.get("time_estimated", 0)

        if args.get("action") == "PROGRESS":
            self._UpdateTransferProgress(args)
            return

        self._UpdateProgressAction(args, pos, size, elapsed, estimated)

    def _UpdateProgressAction(
        self,
        args: Mapping[str, Any],
        pos: int,
        size: int,
        elapsed: float,
        estimated: float,
    ) -> None:
        if "action" in args:
            if args["action"] == "ERASE":
                self.lblStatus1aResult.setText(__("Pending..."))
                self.lblStatus2aResult.setText(__("Pending..."))
                self.lblStatus3aResult.setText(Formatter.progress_time(elapsed))
                if estimated != 0:
                    self.lblStatus4a.setText(
                        __(
                            "Erasing... This may take up to {seconds} seconds.",
                            seconds=estimated,
                        ),
                    )
                else:
                    self.lblStatus4a.setText(__("Erasing... This may take some time."))
                self._SetProgressActionControls(abortable=bool(args["abortable"]), size=size, pos=pos)
            elif args["action"] == "UNLOCK":
                self.lblStatus1aResult.setText(__("Pending..."))
                self.lblStatus2aResult.setText(__("Pending..."))
                self.lblStatus3aResult.setText(__("Pending..."))
                self.lblStatus4a.setText(__("Unlocking flash..."))
                self._SetProgressActionControls(abortable=bool(args["abortable"]), size=size, pos=pos)
            elif args["action"] == "UPDATE_RTC":
                self.lblStatus1aResult.setText(__("Pending..."))
                self.lblStatus2aResult.setText(__("Pending..."))
                self.lblStatus3aResult.setText(__("Pending..."))
                self.lblStatus4a.setText(__("Updating Real Time Clock..."))
                self._SetProgressActionControls(abortable=False, size=size, pos=pos)
            elif args["action"] == "CALC_CHECKSUMS":
                self._ShowChecksumProgress(args, pos, size)
            elif args["action"] == "SECTOR_ERASE":
                if elapsed >= 1:
                    self.lblStatus3aResult.setText(Formatter.progress_time(elapsed))
                self.lblStatus4a.setText(
                    __(
                        "Erasing sector at address {address}...",
                        address="0x{:X}".format(args["sector_pos"]),
                    ),
                )
                self._SetProgressActionControls(abortable=bool(args["abortable"]), size=size, pos=pos)
            elif args["action"] == "ABORTING":
                self.lblStatus1aResult.setText("-")
                self.lblStatus2aResult.setText("-")
                self.lblStatus3aResult.setText("-")
                self.lblStatus4a.setText(__("Stopping... Please wait."))
                self._SetProgressActionControls(abortable=bool(args["abortable"]), size=size, pos=pos)
            elif args["action"] == "ERROR":
                self.lblStatus2aResult.setText(__("Pending..."))
                self.lblStatus3aResult.setText(__("Pending..."))
                self.lblStatus4a.setText('<span style="color: red;">{:s}</span>'.format(args["text"]))
                self._SetProgressActionControls(abortable=bool(args["abortable"]), size=size, pos=pos)
            elif args["action"] == "UPDATE_INFO":
                self.lblStatus4a.setText(args["text"])
                self._SetProgressActionControls(abortable=bool(args["abortable"]), size=size, pos=pos)
            elif args["action"] == "FINISHED":
                if pos > 0:
                    self.lblStatus1aResult.setText(Formatter.file_size(pos))
                self.FinishOperation()
            elif args["action"] == "ABORT":
                self._HandleProgressAbort(args)
                return

    def _ShowChecksumProgress(self, args: Mapping[str, Any], pos: int, size: int) -> None:
        self.lblStatus1aResult.setText(__("Pending..."))
        self.lblStatus2aResult.setText(__("Pending..."))
        self.lblStatus3aResult.setText(__("Pending..."))
        if "type" in args and len(str(args["type"])) > 0:
            self.lblStatus4a.setText(__("Calculating {checksum_type}...", checksum_type=args["type"]))
        else:
            self.lblStatus4a.setText(__("Calculating checksums..."))
        self._SetProgressActionControls(abortable=False, size=size, pos=pos)

    def _SetProgressActionControls(self, *, abortable: bool, size: int, pos: int) -> None:
        self.SetStatus4aResult("")
        self.btnCancel.setEnabled(abortable)
        self.SetProgressBars(min=0, max=size, value=pos)

    def SetStatus4aResult(self, text: str) -> None:
        if text:
            self.lblStatus4aResult.setText(text)
            self.lblStatus4aResult.setVisible(True)
            self.lblStatus4a.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Preferred,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )
        else:
            self.lblStatus4aResult.setVisible(False)
            self.lblStatus4a.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Preferred,
            )

    def SetProgressBars(
        self,
        min: int = 0,  # noqa: A002 - retained as a stable keyword argument
        max: int = 100,  # noqa: A002 - retained as a stable keyword argument
        value: int = 0,
        setPause: bool | None = None,
    ) -> None:
        self.prgStatus.setMinimum(min)
        self.prgStatus.setMaximum(max)
        self.prgStatus.setValue(value)
        if self.TBPROG is not None:
            if not value > max:
                self.TBPROG.setRange(min, max)
                self.TBPROG.setValue(value)
                if value not in (min, max):
                    self.TBPROG.setVisible(True)
                else:
                    self.TBPROG.setVisible(False)
            if setPause is not None:
                self.TBPROG.setPaused(setPause)
            else:
                self.TBPROG.setPaused(False)

    def ShowFirmwareUpdateWindow(self) -> Literal[False] | None:
        if self.CONN is None:
            try:
                dev_types = {
                    hw_mod.GbxDevice.DEVICE_LABEL_LONG: hw_mod.GbxDevice.GetFirmwareUpdaterClass(None)
                    for hw_mod in HW_DEVICES
                    if hw_mod.GbxDevice().SupportsFirmwareUpdates()
                }
                dlg_args = {
                    "title": __("Firmware Updater"),
                    "intro": __("Please select your device."),
                    "params": [
                        # ID, Type, Value(s), Default Index
                        ["dev_type", "cmb", __("Device Type:"), dev_types.keys(), 0],
                    ],
                }
                dlg = UserInputDialog(self, icon=self.windowIcon(), args=cast("DialogArgs", dlg_args))
                if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                    result = dlg.GetResult()
                    FirmwareUpdater = list(dev_types.values())[result["dev_type"].currentIndex()][1]
                else:
                    return False
            except KeyError:
                return False
        elif not self._device.SupportsFirmwareUpdates():
            QtWidgets.QMessageBox.information(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("FlashGBX currently does not support updating the firmware of your device."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return False
        else:
            updater_classes = self._device.GetFirmwareUpdaterClass()
            if updater_classes is None:
                return False
            FirmwareUpdater = updater_classes[1]

        firmware_window = FirmwareUpdater(self, app_path=AppContext.APP_PATH, icon=self.windowIcon(), device=self.CONN)
        self.FWUPWIN = firmware_window
        firmware_window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, on=True)
        firmware_window.setModal(True)
        firmware_window.run()
        return None

    def ShowPocketCameraWindow(self) -> None:
        data = None
        if self.CONN is not None:
            if self._device.GetMode() is None and "DMG" in self._device.GetSupprtedModes():
                answer = QtWidgets.QMessageBox.question(
                    self,
                    f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                    __("Is a Game Boy Camera cartridge currently inserted?"),
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No,
                )
                if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                    self.optDMG.setChecked(True)
                    self.SetMode()
            if self._device.GetMode() == "DMG":
                header = self._device.ReadHeader()
                if header is not False and header["mapper_raw"] == 252:  # GBD
                    args = {
                        "path": None,
                        "mbc": 252,
                        "save_type": header["ram_size_raw"],
                        "rtc": False,
                    }
                    self.lblStatus4a.setText(__("Loading data, please wait..."))
                    qt_app.processEvents()
                    self._device.BackupRAM(fncSetProgress=False, args=args)
                    data = self._device.INFO["data"]
                    self.lblStatus4a.setText(__("Ready."))

        camera_window = PocketCameraWindow(
            self,
            icon=self.windowIcon(),
            file=data,
            config_path=AppContext.CONFIG_PATH,
            app_path=AppContext.APP_PATH,
        )
        self.CAMWIN = camera_window
        camera_window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, on=True)
        camera_window.setModal(True)
        camera_window.run()

    def ShowInteractiveConsoleWindow(self) -> Literal[False] | None:
        if self.CONN is None:
            QtWidgets.QMessageBox.information(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("Please connect to a device first."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return False
        if self._device.GetMode() not in ("DMG", "AGB"):
            QtWidgets.QMessageBox.information(
                self,
                f"{AppInfo.NAME:s} {AppInfo.VERSION:s}",
                __("Please select a platform mode (Game Boy or Game Boy Advance) first."),
                QtWidgets.QMessageBox.StandardButton.Ok,
            )
            return False

        console_window = InteractiveConsoleWindow(self, icon=self.windowIcon())
        self.INTWIN = console_window
        console_window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, on=True)
        console_window.setModal(True)
        console_window.run()
        return None

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
        if (
            self.btnHeaderRefresh.isEnabled()
            and self.grpActions.isEnabled()
            and self.CONN is not None
            and e.mimeData().hasUrls()
        ):
            mode = self._device.GetMode()
            for url in e.mimeData().urls():
                fn = str(url.toLocalFile())
                if fn == "":
                    fn = urllib.parse.unquote(str(QtCore.QUrl(str(url.toString())).toLocalFile() or url.path()))

                ext = Path(fn).resolve().suffix.lower()
                return _is_supported_drop(ext, mode)
        return False

    def dropEvent(self, e: QtGui.QDropEvent) -> None:
        if self.btnHeaderRefresh.isEnabled() and self.grpActions.isEnabled() and e.mimeData().hasUrls():
            e.setDropAction(QtCore.Qt.DropAction.CopyAction)
            e.accept()
            for url in e.mimeData().urls():
                fn = str(url.toLocalFile())
                if fn == "":
                    fn = urllib.parse.unquote(str(QtCore.QUrl(str(url.toString())).toLocalFile() or url.path()))

                ext = Path(fn).resolve().suffix.lower()
                if ext in DROP_ROM_EXTS_ALL:
                    self.FlashROM(fn)
                elif ext in SAVE_EXTS:
                    self.WriteRAM(fn)
        else:
            e.ignore()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.DisconnectDevice()

        self.MSGBOX_TIMER.stop()
        self.MSGBOX_DISPLAYING = True
        with self.MSGBOX_QUEUE.mutex:
            self.MSGBOX_QUEUE.queue.clear()
        event.accept()

    def run(self) -> None:
        self.main_layout.update()
        self.main_layout.activate()
        self.adjustSize()
        fixed_size = self.size()
        screen = self.screen() or QtGui.QGuiApplication.primaryScreen()
        screen_geometry = screen.availableGeometry()
        frame_geometry = self.frameGeometry()
        frame_geometry.moveCenter(screen_geometry.center())
        self.move(frame_geometry.topLeft())
        self.setAcceptDrops(True)
        icon_filename = "icon.png" if platform.system() == "Linux" else "icon.ico"
        app_icon = QtGui.QIcon(str(Path(AppContext.APP_PATH) / "res" / icon_filename))
        qt_app.setWindowIcon(app_icon)
        self.setWindowIcon(app_icon)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowMaximizeButtonHint, on=False)
        if platform.system() == "Windows":
            self.setWindowFlag(QtCore.Qt.WindowType.MSWindowsFixedSizeDialogHint, on=True)
        self.show()
        self.setFixedSize(fixed_size)
        sys.stdout = Logger()

        # Taskbar Progress on Windows and Linux (Unity Launcher API)
        if platform.system() in ("Windows", "Linux"):
            try:
                if platform.system() == "Windows":
                    myappid = "Lesserkuma.FlashGBX"
                    QtWinExtras.QtWin.setCurrentProcessExplicitAppUserModelID(myappid)
                taskbar_button = QtWinExtras.QWinTaskbarButton()
                self.TBPROG = taskbar_button.progress()
                self.TBPROG.setRange(0, 100)
                taskbar_button.setWindow(self.windowHandle())
                self.TBPROG.setVisible(False)
            except ImportError, AttributeError, RuntimeError:
                pass

        qt_app.exec()
        sys.stdout = sys.__stdout__


qt_app = QtWidgets.QApplication(sys.argv)
if platform.system() == "Linux":
    try:
        desktop_id = AppInfo.NAME.lower()
        os.environ["FLASHGBX_DESKTOP_FILE"] = desktop_id + ".desktop"
        QtGui.QGuiApplication.setDesktopFileName(desktop_id)
        qt_app.setApplicationName(desktop_id)
        qt_app.setApplicationDisplayName(AppInfo.NAME)
    except AttributeError, TypeError:
        qt_app.setApplicationName(AppInfo.NAME)
else:
    qt_app.setApplicationName(AppInfo.NAME)
loadQtTranslation(qt_app)
