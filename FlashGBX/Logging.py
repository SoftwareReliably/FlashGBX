# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)
#
# Terminal output, debug logging, and Python exception hook.

import datetime
import os
import platform
import re
import sys
import time
import traceback

from loguru import logger

from . import i18n
from .app import AppContext, AppInfo
from .i18n import __


class ANSI:
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[33m"
    DARK_GRAY = "\033[90m"
    RESET = "\033[0m"
    CLEAR_LINE = "\033[2K"


class Logger:
    LOG_ERROR = False

    def __init__(self):
        AppContext.PRINT_LOG.append(
            "FlashGBX {version}\n© 2020–{year} Lesserkuma".format(
                version=AppInfo.VERSION,
                year=time.strftime("%Y"),
            ),
        )

    def write(self, *args, **kwargs):
        msg = "{:s}".format(" ".join(map(str, args)), **kwargs)
        if len(msg.strip()) > 0:
            if ANSI.RED in msg:
                self.LOG_ERROR = True
            AppContext.PRINT_LOG.append(re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", msg.strip()))
            if len(AppContext.PRINT_LOG) > 16 * 1024:
                AppContext.PRINT_LOG.pop(0)
        sys.__stdout__.write(msg)

    def flush(self):
        sys.__stdout__.flush()

    @classmethod
    def _write_debug_message(cls, msg):
        AppContext.DEBUG_LOG.append(msg)
        if len(AppContext.DEBUG_LOG) > 64 * 1024:
            AppContext.DEBUG_LOG.pop(0)
        if AppContext.DEBUG:
            msg = ANSI.CLEAR_LINE + msg
            if isinstance(sys.stdout, Logger):
                temp = sys.stdout
                sys.stdout = sys.__stdout__
                print(msg)
                sys.stdout = temp

    @classmethod
    def dprint(cls, *args, **kwargs):
        stack = traceback.extract_stack()
        stack = stack[len(stack) - 2]
        msg = "[{:s}] [{:s}:{:d}] {:s}(): {:s}".format(
            str(datetime.datetime.now().astimezone()),
            os.path.split(stack.filename)[1],
            stack.lineno,
            stack.name,
            " ".join(map(str, args)),
            **kwargs,
        )
        cls._write_debug_message(msg)

    @classmethod
    def write_debug_log(cls, device=False):
        cls.dprint("Now writing debug log file")
        msg = "\n\n\n---- Debug Log ----\n"
        msg += f"{AppInfo.NAME:s} version: {AppInfo.VERSION_PEP440:s} ({AppInfo.VERSION_TIMESTAMP:d})\n"
        msg += f"Language: {i18n.CONFIGURED_LANGUAGE:s}\n"
        msg += "Platform: {:s}\n".format(AppInfo.os_string() + ", " + platform.machine() + ", " + i18n.OS_LANGUAGE)
        if device is not False:
            if device is not None:
                msg += f"Connected device: {device:s}\n"
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

        try:
            fn = AppContext.CONFIG_PATH + os.sep + "debug.log"
            with open(fn, "wb") as f:
                f.write("".encode("UTF-8-SIG"))
                if platform.system() == "Windows":
                    f.write("\r\n".join(AppContext.PRINT_LOG).encode("UTF-8"))
                    f.write(msg.replace("\n", "\r\n").encode("UTF-8"))
                    f.write("\r\n".join(AppContext.DEBUG_LOG).encode("UTF-8"))
                else:
                    f.write("\n".join(AppContext.PRINT_LOG).encode("UTF-8"))
                    f.write(msg.encode("UTF-8"))
                    f.write("\n".join(AppContext.DEBUG_LOG).encode("UTF-8"))
                print(__("The debug log was written to {logfile}", logfile=fn))
            return True
        except:
            return False

    @classmethod
    def exception_hook(cls, exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        s = "EXCEPTION OCCURED\n"
        lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
        for line in lines:
            s += f"{line:s}"
        print(s)
        cls.write_debug_log()
        if isinstance(sys.stdout, Logger):
            sys.stdout.LOG_ERROR = True


# Top-level alias kept because dprint is the most-used helper in the codebase.
dprint = Logger.dprint


def _loguru_sink(message):
    Logger._write_debug_message(str(message).rstrip())


logger.remove()
logger.add(
    _loguru_sink,
    format="[{time:YYYY-MM-DDTHH:mm:ss.SSSZZ}] [{file.name}:{line}] {function}(): {level}: {message}",
    backtrace=True,
    diagnose=False,
)
