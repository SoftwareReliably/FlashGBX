"""Tests for FlashGBX startup configuration and platform helpers."""

from __future__ import annotations

import argparse
import importlib
import zipfile
from typing import TYPE_CHECKING

import pytest

import FlashGBX.FlashGBX as entrypoint  # noqa: N813
from FlashGBX.app import AppInfo
from FlashGBX.FlashGBX import BaseArgs, LoadConfig, ReadConfigFiles
from FlashGBX.IniSettings import IniSettings

if TYPE_CHECKING:
    from pathlib import Path


def make_base_args(app_path: Path, config_path: Path, *, reset: bool = False) -> BaseArgs:
    return {
        "app_path": str(app_path),
        "config_path": str(config_path),
        "argparsed": argparse.Namespace(reset=reset),
    }


def test_platform_environment_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert entrypoint._parse_macos_version("14.6.1") == (14, 6, 1)
    assert entrypoint._parse_macos_version("") == (0, 0)
    assert entrypoint._parse_macos_version("not-a-version") == (0, 0)

    windows_calls: list[bool] = []
    monkeypatch.setattr(entrypoint, "_enable_windows_ansi", lambda: windows_calls.append(True))
    entrypoint._configure_platform_environment("Windows")
    assert windows_calls == [True]

    monkeypatch.delenv("QT_MAC_WANTS_LAYER", raising=False)
    monkeypatch.setattr(entrypoint.platform, "mac_ver", lambda: ("11.7.10", ("", "", ""), ""))
    entrypoint._configure_platform_environment("Darwin")
    assert entrypoint.os.environ["QT_MAC_WANTS_LAYER"] == "1"

    monkeypatch.delenv("QT_MAC_WANTS_LAYER", raising=False)
    monkeypatch.setattr(entrypoint.platform, "mac_ver", lambda: ("14.6", ("", "", ""), ""))
    entrypoint._configure_platform_environment("Darwin")
    assert "QT_MAC_WANTS_LAYER" not in entrypoint.os.environ


def test_read_config_files_recovers_after_missing_profiles(
    tmp_path: Path,
) -> None:
    app_path = tmp_path / "app"
    config_path = tmp_path / "config"
    config_path.mkdir()
    settings_path = config_path / "settings.ini"
    settings_path.write_text("[General]\nConfigVersion = old-version\n", encoding="utf-8")

    config_version, profiles = ReadConfigFiles(make_base_args(app_path, config_path))

    assert config_version is False
    assert profiles == []
    assert len(list(config_path.glob("settings.ini_*.bak"))) == 1
    assert IniSettings(settings_path).value("ConfigVersion") == AppInfo.VERSION


def test_load_config_extracts_and_validates_profiles_and_archive_paths(
    tmp_path: Path,
) -> None:
    app_path = tmp_path / "app"
    config_path = tmp_path / "config"
    resources = app_path / "res"
    resources.mkdir(parents=True)
    profile = '{"type": "DMG", "names": ["Test Cart", "Alias"], "flash_ids": [[0xab, 0xCD]], "command": 0x10}'
    with zipfile.ZipFile(resources / "config.zip", "w") as archive:
        archive.writestr("fc_DMG_Test.txt", profile)
        archive.writestr("fc_DMG_Invalid.txt", '{"type": "DMG", "names": "not-a-list"}')
        archive.writestr("fc_DMG_Broken.txt", "{invalid")
        archive.writestr("../escape.txt", "unsafe")

    result = LoadConfig(make_base_args(app_path, config_path))

    test_profile = result["flashcarts"]["DMG"]["Test Cart"]
    alias_profile = result["flashcarts"]["DMG"]["Alias"]
    assert isinstance(test_profile, dict)
    assert isinstance(alias_profile, dict)
    assert test_profile["flash_ids"] == [[0xAB, 0xCD]]
    assert test_profile["command"] == 0x10
    assert test_profile["names"] == ["Test Cart"]
    assert alias_profile["names"] == ["Alias"]
    assert not (tmp_path / "escape.txt").exists()
    assert any("unsafe path" in str(message[1]) for message in result["config_ret"])
    assert any("could not be parsed" in str(message[1]) for message in result["config_ret"])


@pytest.mark.parametrize(
    "profile",
    [
        None,
        [],
        {"type": "DMG"},
        {"type": "UNKNOWN", "names": ["Cart"]},
        {"type": "AGB", "names": "Cart"},
        {"type": "AGB", "names": ["", 123]},
    ],
)
def test_flashcart_profile_rejects_invalid_structures(profile: object) -> None:
    assert entrypoint._flashcart_profile(profile) is None


def test_main_dispatches_cli_with_typed_startup_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_module = importlib.import_module("FlashGBX.FlashGBX_CLI")
    received: list[entrypoint.StartupArgs] = []

    class FakeCLI:
        def __init__(self, args: entrypoint.StartupArgs) -> None:
            received.append(args)

        def run(self) -> int:
            return 7

    monkeypatch.setattr(cli_module, "FlashGBX_CLI", FakeCLI)
    monkeypatch.setattr(entrypoint, "__file__", str(tmp_path / "FlashGBX.py"))
    monkeypatch.setattr(entrypoint, "_configure_platform_environment", lambda: None)
    monkeypatch.setattr(entrypoint, "init_language", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        entrypoint,
        "LoadConfig",
        lambda _args: {"flashcarts": {"DMG": {}, "AGB": {}}, "config_ret": []},
    )
    monkeypatch.setattr(entrypoint.sys, "argv", ["FlashGBX", "--cli"])

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.main(portableMode=True)

    assert exc_info.value.code == 7
    assert len(received) == 1
    assert received[0]["config_path"] == str(tmp_path / "config")
    assert received[0]["argparsed"].cli is True
