"""Tests for pathlib-backed INI settings storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from FlashGBX.IniSettings import IniSettings

if TYPE_CHECKING:
    from pathlib import Path


def test_ini_settings_accepts_path_and_creates_parent(tmp_path: Path) -> None:
    settings_path = tmp_path / "nested" / "settings.ini"

    settings = IniSettings(path=settings_path)
    settings.setValue("Theme", "dark")

    assert settings_path == settings.FILENAME
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
