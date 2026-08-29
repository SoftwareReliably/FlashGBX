# FlashGBX
# Author: Lesserkuma (github.com/Lesserkuma)
#
# PySide6 helpers, partly contributed by J-Fox

from __future__ import annotations

import ctypes
import os
import platform
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, cast

from PySide6 import QtCore, QtGui, QtWidgets

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


_PLATFORM = platform.system()
_IS_WINDOWS = _PLATFORM == "Windows"
_IS_LINUX = _PLATFORM == "Linux"


class _TaskbarProgress(Protocol):
    """Small common interface used by the GUI's platform-specific progress bar."""

    def setRange(self, minimum: int, maximum: int) -> None:
        """Set the progress range."""
        ...

    def setValue(self, value: int) -> None:
        """Set the current progress value."""
        ...

    def setVisible(self, visible: bool) -> None:
        """Show or hide the progress indicator."""
        ...

    def setPaused(self, paused: bool) -> None:
        """Set the paused state when the backend supports it."""
        ...


class _TaskbarButton(Protocol):
    """Common interface exposed by the QtWinExtras compatibility object."""

    def progress(self) -> _TaskbarProgress:
        """Return the taskbar progress handle."""
        ...

    def setWindow(self, window: QtGui.QWindow | None) -> None:
        """Associate the taskbar button with a Qt window."""
        ...


class _QtWin(Protocol):
    @staticmethod
    def setCurrentProcessExplicitAppUserModelID(app_id: str) -> None:
        """Set the Windows application identity used by the taskbar."""
        ...


class _QtWinExtrasNamespace:
    """Typed replacement for the removed QtWinExtras compatibility module."""

    QWinTaskbarButton: type[_TaskbarButton]
    QtWin: type[_QtWin]

    def __init__(
        self,
        taskbar_button: type[_TaskbarButton],
        qt_win: type[_QtWin],
    ) -> None:
        self.QWinTaskbarButton = taskbar_button
        self.QtWin = qt_win


class _TaskbarProgressBase:
    """Store the Qt taskbar progress state shared by all backends."""

    def __init__(self) -> None:
        self._minimum = 0
        self._maximum = 100
        self._value = 0
        self._visible = False
        self._paused = False

    def _apply(self) -> None:
        """Apply the current state to the platform backend."""

    def setRange(self, minimum: int, maximum: int) -> None:
        self._minimum = int(minimum)
        self._maximum = int(maximum)
        self._apply()

    def setValue(self, value: int) -> None:
        self._value = int(value)
        self._apply()

    def setVisible(self, visible: bool) -> None:
        self._visible = bool(visible)
        self._apply()

    def setPaused(self, paused: bool) -> None:
        self._paused = bool(paused)
        self._apply()


class _NoopTaskbarProgress(_TaskbarProgressBase):
    """Fallback progress handle for platforms without taskbar integration."""


class _NoopTaskbarButton:
    def __init__(self) -> None:
        self._progress = _NoopTaskbarProgress()

    def progress(self) -> _NoopTaskbarProgress:
        return self._progress

    def setWindow(self, window: QtGui.QWindow | None) -> None:
        del window


class _NoopQtWin:
    @staticmethod
    def setCurrentProcessExplicitAppUserModelID(app_id: str) -> None:
        del app_id


def _check_hresult(result: int, operation: str) -> None:
    """Raise a useful error when a Windows COM call returns a failure HRESULT."""
    unsigned_result = result & 0xFFFFFFFF
    if result < 0 or unsigned_result >= 0x80000000:
        raise OSError(f"{operation} failed with HRESULT 0x{unsigned_result:08X}")


def _debug_print(*args: Any, **kwargs: Any) -> None:
    """Log when the package logger is already initialized."""
    logging_module = sys.modules.get(f"{__package__}.Logging")
    if logging_module is None:
        return
    debug_print = getattr(logging_module, "dprint", None)
    if callable(debug_print):
        debug_print(*args, **kwargs)


def _log_exception(message: str) -> None:
    """Log an exception when the package logger is already initialized."""
    logging_module = sys.modules.get(f"{__package__}.Logging")
    if logging_module is None:
        return
    log_exception = getattr(logging_module, "logger", None)
    exception = getattr(log_exception, "exception", None)
    if callable(exception):
        exception(message)


_WINFUNCTYPE: Callable[..., Any] | None = None
_taskbar_button_class: type[_TaskbarButton] = _NoopTaskbarButton
_qt_win_class: type[_QtWin] = _NoopQtWin

if _IS_WINDOWS:
    from ctypes import wintypes

    _WINFUNCTYPE: Callable[..., Any] | None = cast(
        "Callable[..., Any]",
        getattr(ctypes, "WINFUNCTYPE"),  # noqa: B009
    )

    class _WindowsTaskbarProgress(_TaskbarProgressBase):
        """Windows taskbar progress implementation via ITaskbarList3."""

        _TBPF_NOPROGRESS: ClassVar[int] = 0
        _TBPF_INDETERMINATE: ClassVar[int] = 1
        _TBPF_NORMAL: ClassVar[int] = 2
        _TBPF_PAUSED: ClassVar[int] = 8

        def __init__(self) -> None:
            super().__init__()
            self._taskbar: ctypes.c_void_p | None = None
            self._window_handle: Any = None

        def _bind(self, window_handle: int) -> None:
            self._window_handle = wintypes.HWND(window_handle)
            taskbar = ctypes.c_void_p()
            ole32: Any = getattr(ctypes, "windll").ole32  # noqa: B009

            _check_hresult(int(ole32.CoInitialize(None)), "CoInitialize")
            clsid = (ctypes.c_ubyte * 16)()
            iid = (ctypes.c_ubyte * 16)()
            _check_hresult(
                int(
                    ole32.CLSIDFromString(
                        ctypes.c_wchar_p("{56FDF344-FD6D-11D0-958A-006097C9A090}"),
                        clsid,
                    ),
                ),
                "CLSIDFromString",
            )
            _check_hresult(
                int(
                    ole32.CLSIDFromString(
                        ctypes.c_wchar_p("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"),
                        iid,
                    ),
                ),
                "CLSIDFromString",
            )
            _check_hresult(
                int(ole32.CoCreateInstance(clsid, None, 1, iid, ctypes.byref(taskbar))),
                "CoCreateInstance",
            )
            if taskbar.value is None:
                raise RuntimeError("CoCreateInstance returned an empty taskbar interface")

            self._taskbar = taskbar
            winfunctype = _WINFUNCTYPE
            if winfunctype is None:
                raise RuntimeError("Windows function prototypes are unavailable")
            _check_hresult(
                int(self._call(3, winfunctype(ctypes.c_long, ctypes.c_void_p))),
                "ITaskbarList3::HrInit",
            )
            self._apply()

        def _call(
            self,
            index: int,
            prototype: Callable[..., Any],
            *args: Any,
        ) -> Any:
            taskbar = self._taskbar
            winfunctype = _WINFUNCTYPE
            if taskbar is None or winfunctype is None:
                return None
            vtable = cast(
                "Any",
                ctypes.cast(
                    taskbar,
                    ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
                ),
            )[0]
            function = prototype(vtable[index])
            return function(taskbar, *args)

        def _apply(self) -> None:
            if self._taskbar is None:
                return

            if not self._visible:
                state = self._TBPF_NOPROGRESS
            elif self._maximum - self._minimum <= 0:
                state = self._TBPF_INDETERMINATE
            else:
                state = self._TBPF_PAUSED if self._paused else self._TBPF_NORMAL

            winfunctype = _WINFUNCTYPE
            if winfunctype is None:
                return
            _check_hresult(
                int(
                    self._call(
                        10,
                        winfunctype(
                            ctypes.c_long,
                            ctypes.c_void_p,
                            wintypes.HWND,
                            ctypes.c_int,
                        ),
                        self._window_handle,
                        state,
                    ),
                ),
                "ITaskbarList3::SetProgressState",
            )
            if state in (self._TBPF_NORMAL, self._TBPF_PAUSED):
                span = self._maximum - self._minimum
                _check_hresult(
                    int(
                        self._call(
                            9,
                            winfunctype(
                                ctypes.c_long,
                                ctypes.c_void_p,
                                wintypes.HWND,
                                ctypes.c_ulonglong,
                                ctypes.c_ulonglong,
                            ),
                            self._window_handle,
                            ctypes.c_ulonglong(max(0, min(span, self._value - self._minimum))),
                            ctypes.c_ulonglong(span),
                        ),
                    ),
                    "ITaskbarList3::SetProgressValue",
                )

    class _WindowsTaskbarButton:
        def __init__(self) -> None:
            self._progress = _WindowsTaskbarProgress()

        def progress(self) -> _WindowsTaskbarProgress:
            return self._progress

        def setWindow(self, window: QtGui.QWindow | None) -> None:
            if window is not None:
                self._progress._bind(int(window.winId()))

    class _WindowsQtWin:
        @staticmethod
        def setCurrentProcessExplicitAppUserModelID(app_id: str) -> None:
            shell32: Any = getattr(ctypes, "windll").shell32  # noqa: B009
            _check_hresult(
                int(shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))),
                "SetCurrentProcessExplicitAppUserModelID",
            )

    _taskbar_button_class = _WindowsTaskbarButton
    _qt_win_class = _WindowsQtWin


_QT_DBUS: Any = None
if _IS_LINUX:
    try:
        from PySide6 import QtDBus as _QT_DBUS
    except ImportError:
        pass


def _application_desktop_file() -> str:
    """Return the desktop-file basename used by the Unity launcher API."""
    app_name = os.environ.get("FLASHGBX_DESKTOP_FILE", "").strip()
    app = QtWidgets.QApplication.instance()
    if not app_name and app is not None:
        app_name = app.applicationName().strip()
    if not app_name:
        desktop_file_name = getattr(QtGui.QGuiApplication, "desktopFileName", None)
        if callable(desktop_file_name):
            try:
                app_name = str(desktop_file_name()).strip()
            except Exception:
                app_name = ""
    if not app_name:
        app_name = "flashgbx"

    desktop_file = os.path.basename(app_name) or "flashgbx"
    if not desktop_file.lower().endswith(".desktop"):
        desktop_file += ".desktop"
    return desktop_file


class _LinuxTaskbarProgress(_TaskbarProgressBase):
    """Unity Launcher progress implementation over the optional session bus."""

    _SIGNAL_PATH: ClassVar[str] = "/com/canonical/unity/launcherentry/1"
    _SIGNAL_INTERFACE: ClassVar[str] = "com.canonical.Unity.LauncherEntry"
    _SIGNAL_NAME: ClassVar[str] = "Update"

    def __init__(self) -> None:
        super().__init__()
        self._app_uri = f"application://{_application_desktop_file()}"
        self._bus: Any = None
        self._available = False
        self._last_payload: tuple[bool, float] | None = None

        if _QT_DBUS is None:
            _debug_print("Unity Launcher progress disabled: QtDBus is unavailable.")
            return
        try:
            self._bus = _QT_DBUS.QDBusConnection.sessionBus()
            self._available = bool(self._bus.isConnected())
        except Exception as err:
            _debug_print(f"Unity Launcher progress disabled: {err!s:s}")
            return
        if not self._available:
            _debug_print("Unity Launcher progress disabled: no DBus session bus.")

    def _apply(self) -> None:
        if not self._available or self._bus is None:
            return

        span = self._maximum - self._minimum
        progress_visible = bool(self._visible and span > 0)
        progress = max(0.0, min(1.0, float(self._value - self._minimum) / float(span))) if progress_visible else 0.0
        state = (progress_visible, progress)
        if state == self._last_payload:
            return

        payload: dict[str, bool | float] = {
            "progress-visible": state[0],
            "progress": state[1],
        }
        try:
            message = _QT_DBUS.QDBusMessage.createSignal(
                self._SIGNAL_PATH,
                self._SIGNAL_INTERFACE,
                self._SIGNAL_NAME,
            )
            message.setArguments([self._app_uri, payload])
            if not self._bus.send(message):
                self._available = False
                _debug_print(
                    "Unity Launcher progress disabled: launcher API is not available in this desktop environment.",
                )
                return
        except Exception as err:
            self._available = False
            _debug_print(f"Unity Launcher progress update failed: {err!s:s}")
            return

        self._last_payload = state


class _LinuxTaskbarButton:
    def __init__(self) -> None:
        self._progress = _LinuxTaskbarProgress()

    def progress(self) -> _LinuxTaskbarProgress:
        return self._progress

    def setWindow(self, window: QtGui.QWindow | None) -> None:
        del window


class _LinuxQtWin:
    @staticmethod
    def setCurrentProcessExplicitAppUserModelID(app_id: str) -> None:
        del app_id


if _IS_LINUX:
    _taskbar_button_class = _LinuxTaskbarButton
    _qt_win_class = _LinuxQtWin

QtWinExtras: _QtWinExtrasNamespace = _QtWinExtrasNamespace(
    _taskbar_button_class,
    _qt_win_class,
)


__all__ = [
    "IsDarkMode",
    "QtWinExtras",
    "bitmap2pixmap",
]


def IsDarkMode() -> bool:
    """Return whether Qt reports that the current color scheme is dark."""
    try:
        scheme = QtGui.QGuiApplication.styleHints().colorScheme()
        return scheme == QtCore.Qt.ColorScheme.Dark
    except Exception:
        _log_exception("Failed to determine the Qt color scheme")
        return False


def bitmap2pixmap(
    data: PILImage,
    scale_factor: int = 4,
) -> QtGui.QPixmap | Literal[False]:
    """Convert a Pillow image to a nearest-neighbor scaled Qt pixmap.

    The returned pixmap is marked with the same scale factor so pixel-art images
    retain their intended logical size on high-DPI displays. ``False`` is
    returned when Pillow or Qt cannot perform the conversion.
    """
    try:
        if scale_factor <= 0:
            raise ValueError("scale_factor must be greater than zero")

        from PIL import Image
        from PIL.ImageQt import ImageQt

        data_converted = data.convert("RGBA")
        scaled_image = data_converted.resize(
            (
                data_converted.width * scale_factor,
                data_converted.height * scale_factor,
            ),
            Image.Resampling.NEAREST,
        )
        pixmap = QtGui.QPixmap.fromImage(ImageQt(scaled_image))
        pixmap.setDevicePixelRatio(scale_factor)
        return pixmap
    except Exception as err:
        _debug_print("Couldn’t convert bitmap to pixmap. Error: {error}", error=str(err))
        return False
