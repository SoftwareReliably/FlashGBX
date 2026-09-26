# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from PySide6 import QtCore, QtGui, QtWidgets

from .app import AppInfo
from .i18n import __, c__
from .InteractiveConsole import InteractiveConsole
from .Logging import logger

if TYPE_CHECKING:
    from .FlashGBX_GUI import FlashGBX_GUI
    from .LK_Device import DeviceMode, LK_Device


class InteractiveConsoleWindow(QtWidgets.QDialog):
    def _CreateCloseButtonRow(self) -> None:
        row_buttons = QtWidgets.QHBoxLayout()
        row_buttons.addStretch()
        self.btnClose = QtWidgets.QPushButton(c__("Button (& = Keyboard Shortcut)", "&Close"))
        self.btnClose.setStyleSheet("padding: 5px 15px;")
        self.btnClose.setAutoDefault(False)
        self.btnClose.setDefault(False)
        self.btnClose.clicked.connect(self.reject)
        row_buttons.addWidget(self.btnClose)
        self.main_layout.addLayout(row_buttons)

    def __init__(self, app: FlashGBX_GUI, icon: QtGui.QIcon | None = None) -> None:
        super().__init__(app)
        if icon is not None:
            self.setWindowIcon(QtGui.QIcon(icon))
        self.setWindowTitle(AppInfo.NAME + " - " + __("Interactive Console"))
        flags = self.windowFlags()
        flags = (
            (flags & ~QtCore.Qt.WindowType.WindowContextHelpButtonHint)
            | QtCore.Qt.WindowType.WindowCloseButtonHint
            | QtCore.Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setWindowFlags(flags)
        self.resize(700, 400)

        self.app: FlashGBX_GUI = app
        connection: LK_Device | None = app.conn
        if connection is None:
            msg = "The interactive console requires an active device connection"
            raise RuntimeError(msg)
        self.conn: LK_Device = connection
        mode: Literal["DMG", "AGB"] | None = self.conn.GetMode()
        if mode is None:
            msg = "The interactive console requires an active cartridge mode"
            raise RuntimeError(msg)
        self.mode: DeviceMode = mode
        self.IM = InteractiveConsole(self.conn, on_output=self.AppendOutput, on_error=self.AppendOutput)

        self.main_layout = QtWidgets.QVBoxLayout()
        self.main_layout.setContentsMargins(8, 8, 8, 8)

        mono_font: QtGui.QFont = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        mono_font.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)

        self.txtOutput = QtWidgets.QPlainTextEdit()
        self.txtOutput.setReadOnly(True)
        self.txtOutput.setFont(mono_font)
        self.txtOutput.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.main_layout.addWidget(self.txtOutput)

        row_input = QtWidgets.QHBoxLayout()
        self.lblPrompt = QtWidgets.QLabel(">")
        self.lblPrompt.setFont(mono_font)
        self.txtInput = QtWidgets.QLineEdit()
        self.txtInput.setFont(mono_font)
        self.txtInput.returnPressed.connect(self.OnSubmit)
        row_input.addWidget(self.lblPrompt)
        row_input.addWidget(self.txtInput)
        self.main_layout.addLayout(row_input)

        self._CreateCloseButtonRow()

        self.setLayout(self.main_layout)

        self.History: list[str] = []
        self.HistoryIndex = 0
        self.txtInput.installEventFilter(self)

    def run(self) -> None:
        self.conn.SetAutoPowerOff(value=0)
        try:
            self.conn.CartPowerOn()
            self.IM.print_help()
            self.txtInput.setFocus()
            self.main_layout.update()
            self.main_layout.activate()
            screen: QtGui.QScreen = self.screen() or QtGui.QGuiApplication.primaryScreen()
            if screen is not None:
                geometry: QtCore.QRect = screen.availableGeometry()
                x: int = geometry.x() + (geometry.width() - self.width()) // 2
                y: int = geometry.y() + (geometry.height() - self.height()) // 2
                self.move(x, y)
            self.show()
        except Exception:
            self._restore_auto_power_off()
            raise

    def _restore_auto_power_off(self) -> None:
        try:
            self.app.SetAutoPowerOff()
        except Exception:
            logger.exception("Failed to restore automatic power-off settings")

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self._restore_auto_power_off()
        try:
            self.app.activateWindow()
        except Exception:
            logger.exception("Failed to reactivate the main application window")
        super().hideEvent(event)

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if obj is self.txtInput and isinstance(event, QtGui.QKeyEvent):
            if event.key() == QtCore.Qt.Key.Key_Up:
                if self.History and self.HistoryIndex > 0:
                    self.HistoryIndex -= 1
                    self.txtInput.setText(self.History[self.HistoryIndex])
                return True
            if event.key() == QtCore.Qt.Key.Key_Down:
                if self.History and self.HistoryIndex < len(self.History) - 1:
                    self.HistoryIndex += 1
                    self.txtInput.setText(self.History[self.HistoryIndex])
                else:
                    self.HistoryIndex = len(self.History)
                    self.txtInput.clear()
                return True
        return super().eventFilter(obj, event)

    def AppendOutput(self, text: str) -> None:
        self.txtOutput.appendPlainText(text)
        scrollbar: QtWidgets.QScrollBar = self.txtOutput.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def OnSubmit(self) -> None:
        line: str = self.txtInput.text().strip()
        self.txtInput.clear()
        if not line:
            self.AppendOutput("")
            return
        self.AppendOutput("> " + line)
        if not self.History or self.History[-1] != line:
            self.History.append(line)
        self.HistoryIndex: int = len(self.History)

        self.txtInput.setEnabled(False)
        self.btnClose.setEnabled(False)
        try:
            if not self.IM.execute_line(line):
                self.reject()
                return
        finally:
            self.txtInput.setEnabled(True)
            self.btnClose.setEnabled(True)
            self.txtInput.setFocus()
