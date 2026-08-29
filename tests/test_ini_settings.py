"""Tests for pathlib-backed INI settings storage."""

from pathlib import Path

from FlashGBX.IniSettings import IniSettings


def test_ini_settings_accepts_path_and_creates_parent(tmp_path: Path) -> None:
    settings_path = tmp_path / "nested" / "settings.ini"

    settings = IniSettings(path=settings_path)
    settings.setValue("Theme", "dark")

    assert settings_path == settings.FILENAME
    assert settings_path.is_file()
    assert IniSettings(path=settings_path).value("Theme") == "dark"
