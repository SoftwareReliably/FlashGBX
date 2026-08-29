"""Tests for user-facing size and time formatting helpers."""

from __future__ import annotations

from FlashGBX.Formatter import Formatter


def test_file_size_formats_bytes_kib_and_mib() -> None:
    assert Formatter.round2(1.239, 2) == 1.23
    assert Formatter.file_size(1, localized=False) == " Byte"
    assert Formatter.file_size(1, short=True, localized=False) == "B"
    assert Formatter.file_size(12, short=True, localized=False) == "12B"
    assert Formatter.file_size(1024, localized=False) == "1.0 KiB"
    assert Formatter.file_size(1024, as_int=True, space="", localized=False) == "1KiB"
    assert Formatter.file_size(2 * 1024 * 1024, localized=False) == "2.00 MiB"


def test_progress_time_formats_duration_variants() -> None:
    assert Formatter.progress_time_short(25 * 3600 + 61) == "01:01:01"
    assert Formatter.progress_time(0, localized=False) == "0 seconds"
    assert Formatter.progress_time(1, localized=False) == "1 second"
    assert Formatter.progress_time(3661, localized=False) == "1 hour, 1 minute, 1 second"
    assert Formatter.progress_time(86400 + 2 * 3600, localized=False) == "1 day, 2 hours"
    assert Formatter.progress_time(0.25, as_float=True, localized=False) == "0.25 seconds"
    assert Formatter.progress_time(-1, localized=False) == "0 seconds"


def test_datetime_and_title_helpers_validate_exact_values() -> None:
    assert Formatter.validate_datetime("2026-08-29", "%Y-%m-%d") is True
    assert Formatter.validate_datetime("2026-8-29", "%Y-%m-%d") is False
    assert Formatter.validate_datetime("not-a-date", "%Y-%m-%d") is False
    assert Formatter.title(None) == ""
    assert Formatter.title("one\ntwo\r\nthree") == "one␤two␤three"
