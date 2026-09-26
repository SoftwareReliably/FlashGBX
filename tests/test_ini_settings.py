"""Tests for pathlib-backed INI settings storage."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from FlashGBX.IniSettings import IniSettings

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def test_ini_settings_accepts_path_and_creates_parent(tmp_path: Path) -> None:
    settings_path = tmp_path / "nested" / "settings.ini"

    settings = IniSettings(path=settings_path)
    settings.setValue("Theme", "dark")

    assert settings_path == settings.filename
    assert settings_path.is_file()
    assert IniSettings(path=settings_path).value("Theme") == "dark"


def test_ini_settings_supports_defaults_aliases_deletion_and_clear() -> None:
    settings = IniSettings(ini="[General]\nExisting=yes\n")

    assert settings.value("existing") == "yes"
    assert settings.value("Missing", default="fallback") == "fallback"
    settings.SetValue("Alias", "value", quiet=True)
    assert settings.GetValue("Alias") == "value"
    settings.setValue("Alias", None, quiet=True)
    assert settings.value("Alias") is None
    assert "existing" in settings.GetString()
    settings.Clear()
    assert settings.GetString() == ""


def test_ini_settings_resets_a_malformed_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings_path = tmp_path / "settings.ini"
    settings_path.write_text("missing-section=true\n", encoding="utf-8")

    settings = IniSettings(path=settings_path)

    assert settings.filename is None
    assert settings.GetString() == ""
    assert settings_path.read_text(encoding="utf-8") == ""
    assert "Resetting invalid settings file" in capsys.readouterr().out


def test_ini_settings_reports_an_inaccessible_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def deny_access(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(Path, "mkdir", deny_access)

    settings = IniSettings(path=tmp_path / "blocked" / "settings.ini")

    assert settings.filename is None
    assert settings.settings is None
    assert "Can't access the configuration directory" in capsys.readouterr().out


def test_ini_settings_persists_clear_and_handles_disabled_storage(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.ini"
    settings = IniSettings(path=settings_path)
    settings.SetValue("Temporary", "value", quiet=True)

    settings.Clear()

    assert settings_path.read_text(encoding="utf-8") == ""

    settings.settings = None
    no_result_callbacks: tuple[Callable[[], object], ...] = (
        settings.Reload,
        lambda: settings.GetValue("Missing"),
        lambda: settings.SetValue("Ignored", "value"),
        settings.Clear,
    )
    assert [callback() for callback in no_result_callbacks] == [None, None, None, None]
    assert settings.GetString() == ""
