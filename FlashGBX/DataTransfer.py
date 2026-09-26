# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

import traceback
from typing import Any

from PySide6 import QtCore
from serial import SerialException

from .i18n import __
from .Logging import dprint


class DataTransfer(QtCore.QThread):
    updateProgress = QtCore.Signal(object)  # noqa: N815

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        QtCore.QThread.__init__(self)
        self.config = config
        self.transfer_finished = False

    def setConfig(self, config: dict[str, Any]) -> None:
        self.config = config
        self.transfer_finished = False

    def isRunning(self) -> bool:
        return not self.transfer_finished

    def run(self) -> None:
        tb = ""
        error = None
        try:
            if self.config is None:
                self.transfer_finished = True
                return
            self.transfer_finished = False
            self.config["port"].TransferData(self.config, self.updateProgress)
            self.transfer_finished = True

        except SerialException as e:
            if e.args and isinstance(e.args[0], str) and "GetOverlappedResult failed" in e.args[0]:
                self.updateProgress.emit(
                    {
                        "action": "ABORT",
                        "info_type": "msgbox_critical",
                        "info_msg": __(
                            "The USB connection was lost during a transfer. Try different USB cables, reconnect the device, restart the software and try again.",
                        ),
                        "abortable": False,
                    },
                )
                self.transfer_finished = True
                return
            tb: str = traceback.format_exc()
            error = e

        except Exception as e:
            tb = traceback.format_exc()
            error = e

        if error is not None:
            print(tb)
            dprint(tb)
            self.updateProgress.emit(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "fatal": True,
                    "info_msg": __(
                        "An unresolvable error has occured. See the debug log file for more information. Reconnect the device, restart the software and try again.",
                    )
                    + f"\n\n{type(error).__name__:s}: {error!s:s}",
                    "abortable": False,
                },
            )
            self.transfer_finished = True
