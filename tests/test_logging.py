"""Tests for terminal capture, debug-log generation, and exception handling."""

from __future__ import annotations

import io
import time
from typing import TYPE_CHECKING

import pytest

import FlashGBX.Logging as logging_module  # noqa: N813
from FlashGBX.app import AppContext, AppInfo
from FlashGBX.Logging import ANSI, Logger

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolate_logging_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AppContext, "PRINT_LOG", [])
    monkeypatch.setattr(AppContext, "DEBUG_LOG", [])
    monkeypatch.setattr(AppContext, "DEBUG", False)
    monkeypatch.setattr(AppContext, "LAUNCH_TIMESTAMP", time.time() - 60)
    monkeypatch.setattr(logging_module.sys, "__stdout__", io.StringIO())


def test_logger_captures_clean_text_and_handles_missing_original_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()
    monkeypatch.setattr(logging_module.sys, "__stdout__", output)
    logger = Logger()
    AppContext.PRINT_LOG.clear()
    message = f"{ANSI.RED}failure{ANSI.RESET}"

    assert logger.write(message) == len(message)
    assert AppContext.PRINT_LOG == ["failure"]
    assert logger.LOG_ERROR is True
    assert output.getvalue() == message

    monkeypatch.setattr(logging_module.sys, "__stdout__", None)
    assert logger.write("captured only") == len("captured only")
    logger.flush()


def test_debug_messages_honor_separator_and_echo_without_swapping_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()
    logger = Logger()
    AppContext.PRINT_LOG.clear()
    monkeypatch.setattr(AppContext, "DEBUG", True)
    monkeypatch.setattr(logging_module.sys, "__stdout__", output)
    monkeypatch.setattr(logging_module.sys, "stdout", logger)

    Logger.dprint("first", "second", sep="\n")

    message = AppContext.DEBUG_LOG[-1]
    assert message.endswith("first\nsecond")
    assert output.getvalue() == ANSI.CLEAR_LINE + message + "\n"
    assert logging_module.sys.stdout is logger


@pytest.mark.parametrize(
    ("system", "separator"),
    [("Linux", "\n"), ("Windows", "\r\n")],
)
def test_write_debug_log_uses_platform_line_endings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    separator: str,
) -> None:
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    monkeypatch.setattr(AppInfo, "os_string", classmethod(lambda _cls: "Test OS"))
    monkeypatch.setattr(logging_module.platform, "machine", lambda: "test-machine")
    monkeypatch.setattr(logging_module.platform, "system", lambda: system)
    monkeypatch.setattr(logging_module.i18n, "CONFIGURED_LANGUAGE", "en")
    monkeypatch.setattr(logging_module.i18n, "OS_LANGUAGE", "en")
    AppContext.PRINT_LOG.extend(["print one", "print two"])
    AppContext.DEBUG_LOG.extend(["debug one", "debug two"])

    assert Logger.write_debug_log("GBxCart RW") is True

    payload = (tmp_path / "debug.log").read_bytes()
    assert payload.startswith(b"\xef\xbb\xbf")
    text = payload.decode("utf-8-sig")
    assert f"print one{separator}print two" in text
    assert f"debug one{separator}debug two" in text
    assert "Connected device: GBxCart RW" in text
    if system == "Windows":
        assert "\n" not in text.replace("\r\n", "")
    else:
        assert "\r\n" not in text


def test_write_debug_log_returns_false_when_destination_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path / "missing"))

    assert Logger.write_debug_log() is False


def test_exception_hook_delegates_interrupts_and_records_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegated: list[tuple[type[BaseException], BaseException, object]] = []
    monkeypatch.setattr(logging_module.sys, "__excepthook__", lambda *args: delegated.append(args))
    interrupt = KeyboardInterrupt()

    Logger.exception_hook(KeyboardInterrupt, interrupt, None)

    assert delegated == [(KeyboardInterrupt, interrupt, None)]

    logger = Logger()
    AppContext.PRINT_LOG.clear()
    monkeypatch.setattr(logging_module.sys, "stdout", logger)
    monkeypatch.setattr(logging_module.sys, "__stdout__", io.StringIO())
    monkeypatch.setattr(Logger, "write_debug_log", classmethod(lambda _cls, _device=False: True))
    error = ValueError("bad value")

    Logger.exception_hook(ValueError, error, error.__traceback__)

    assert logger.LOG_ERROR is True
    assert any("EXCEPTION OCCURRED" in message and "bad value" in message for message in AppContext.PRINT_LOG)


def test_log_buffers_discard_all_entries_beyond_their_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_module, "_PRINT_LOG_LIMIT", 2)
    logger = Logger()
    logger.write("one")
    logger.write("two")
    logger.write("three")

    assert AppContext.PRINT_LOG == ["two", "three"]
