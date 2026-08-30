# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)
#
# Terminal output, debug logging, and Python exception hook.

from __future__ import annotations

import datetime
import platform
import re
import sys
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, cast

from loguru import logger as _loguru_logger  # pyright: ignore[reportMissingImports]

from . import i18n
from .app import AppContext, AppInfo
from .i18n import __

if TYPE_CHECKING:
    from types import TracebackType

_PRINT_LOG_LIMIT = 16 * 1024
_DEBUG_LOG_LIMIT = 64 * 1024
_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


class _LoguruLogger(Protocol):
    def exception(self, message: str, *args: object, **kwargs: object) -> _LoguruLogger: ...

    def remove(self, handler_id: int | None = None) -> None: ...

    def add(self, sink: object, **kwargs: object) -> int: ...


logger = cast("_LoguruLogger", _loguru_logger)


def _format_message(args: tuple[object, ...], kwargs: dict[str, object]) -> str:
    separator = kwargs.get("sep", " ")
    if separator is None:
        separator = " "
    return str(separator).join(map(str, args))


def _append_capped(log: list[str], message: str, limit: int) -> None:
    log.append(message)
    if len(log) > limit:
        del log[:-limit]


class ANSI:
    BOLD: ClassVar[str] = "\033[1m"
    RED: ClassVar[str] = "\033[91m"
    GREEN: ClassVar[str] = "\033[92m"
    YELLOW: ClassVar[str] = "\033[33m"
    DARK_GRAY: ClassVar[str] = "\033[90m"
    RESET: ClassVar[str] = "\033[0m"
    CLEAR_LINE: ClassVar[str] = "\033[2K"


class Logger:
    def __init__(self) -> None:
        self.LOG_ERROR: bool = False
        _append_capped(
            AppContext.PRINT_LOG,
            "FlashGBX {version}\n© 2020–{year} Lesserkuma".format(
                version=AppInfo.VERSION,
                year=time.strftime("%Y"),
            ),
            _PRINT_LOG_LIMIT,
        )

    def write(self, *args: object, **kwargs: object) -> int:
        msg = _format_message(args, kwargs)
        if msg.strip():
            if ANSI.RED in msg:
                self.LOG_ERROR = True
            _append_capped(AppContext.PRINT_LOG, _ANSI_ESCAPE_RE.sub("", msg.strip()), _PRINT_LOG_LIMIT)
        output_stream = sys.__stdout__
        if output_stream is not None and output_stream is not self:
            output_stream.write(msg)
        return len(msg)

    def flush(self) -> None:
        output_stream = sys.__stdout__
        if output_stream is not None and output_stream is not self:
            output_stream.flush()

    @classmethod
    def _write_debug_message(cls, msg: str) -> None:
        _append_capped(AppContext.DEBUG_LOG, msg, _DEBUG_LOG_LIMIT)
        if AppContext.DEBUG:
            msg = ANSI.CLEAR_LINE + msg
            output_stream = sys.__stdout__
            if isinstance(sys.stdout, Logger) and output_stream is not None:
                output_stream.write(msg + "\n")

    @classmethod
    def dprint(cls, *args: object, **kwargs: object) -> None:
        stack = traceback.extract_stack(limit=2)[0]
        timestamp = datetime.datetime.now().astimezone()
        filename = Path(stack.filename).name
        message = _format_message(args, kwargs)
        msg = f"[{timestamp!s}] [{filename}:{stack.lineno}] {stack.name}(): {message}"
        cls._write_debug_message(msg)

    @classmethod
    def write_debug_log(cls, device: str | Literal[False] | None = False) -> bool:
        cls.dprint("Now writing debug log file")
        msg = "\n\n\n---- Debug Log ----\n"
        msg += f"{AppInfo.NAME:s} version: {AppInfo.VERSION_PEP440:s} ({AppInfo.VERSION_TIMESTAMP:d})\n"
        msg += f"Language: {i18n.CONFIGURED_LANGUAGE or 'unknown'}\n"
        msg += "Platform: {:s}\n".format(AppInfo.os_string() + ", " + platform.machine() + ", " + i18n.OS_LANGUAGE)
        if device is not False:
            if device is not None:
                msg += f"Connected device: {device!s}\n"
            else:
                msg += "No device connected\n"

        launch_time = datetime.datetime.fromtimestamp(AppContext.LAUNCH_TIMESTAMP).astimezone().replace(microsecond=0)
        now = datetime.datetime.now().astimezone().replace(microsecond=0)
        runtime = now - launch_time
        days, hours, minutes, seconds = (
            runtime.days,
            runtime.seconds // 3600,
            (runtime.seconds % 3600) // 60,
            runtime.seconds % 60,
        )
        msg += f"Launched: {launch_time.isoformat():s}\n"
        msg += f"Log generated: {now.isoformat():s}\n"
        msg += f"Runtime: {days}d {hours}h {minutes}m {seconds}s\n\n"

        log_path = Path(AppContext.CONFIG_PATH) / "debug.log"
        line_separator = "\r\n" if platform.system() == "Windows" else "\n"
        content = line_separator.join(AppContext.PRINT_LOG)
        content += msg.replace("\n", line_separator)
        content += line_separator.join(AppContext.DEBUG_LOG)
        try:
            log_path.write_bytes(content.encode("utf-8-sig"))
            print(__("The debug log was written to {logfile}", logfile=str(log_path)))
        except OSError:
            return False
        except ValueError:
            return False
        else:
            return True

    @classmethod
    def exception_hook(
        cls,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        exception_text = "EXCEPTION OCCURRED\n" + "".join(
            traceback.format_exception(exc_type, exc_value, exc_traceback),
        )
        print(exception_text)
        cls.write_debug_log()
        if isinstance(sys.stdout, Logger):
            sys.stdout.LOG_ERROR = True


# Top-level alias kept because dprint is the most-used helper in the codebase.
dprint = Logger.dprint


def _loguru_sink(message: object) -> None:
    Logger._write_debug_message(str(message).rstrip())


logger.remove()
logger.add(
    _loguru_sink,
    format="[{time:YYYY-MM-DDTHH:mm:ss.SSSZZ}] [{file.name}:{line}] {function}(): {level}: {message}",
    backtrace=True,
    diagnose=False,
)
