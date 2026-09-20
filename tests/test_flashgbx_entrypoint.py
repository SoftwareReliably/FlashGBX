"""Tests for FlashGBX startup configuration and platform helpers."""

from __future__ import annotations

import argparse
import builtins
import importlib
import runpy
import zipfile
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

import FlashGBX as package  # noqa: N813
import FlashGBX.FlashGBX as entrypoint  # noqa: N813
from FlashGBX.app import AppContext, AppInfo
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


@pytest.fixture
def startup_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Keep all main() startup paths and mutable process state inside this test."""
    subdir = tmp_path / "app" / "config"
    appdata = tmp_path / "appdata" / "FlashGBX"
    monkeypatch.setattr(
        entrypoint,
        "_startup_paths",
        lambda _portable: (
            str(tmp_path / "app"),
            {"subdir": str(subdir), "appdata": str(appdata)},
            str(subdir),
            None,
            "subdir",
        ),
    )
    monkeypatch.setattr(entrypoint, "_configure_platform_environment", lambda: None)
    monkeypatch.setattr(entrypoint, "init_language", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entrypoint, "_print_banner", lambda: None)
    monkeypatch.setattr(entrypoint.sys, "argv", ["FlashGBX"])
    monkeypatch.setattr(AppContext, "LAUNCH_TIMESTAMP", AppContext.LAUNCH_TIMESTAMP)
    monkeypatch.setattr(AppContext, "DEBUG", AppContext.DEBUG)
    monkeypatch.setattr(
        builtins,
        "input",
        Mock(side_effect=AssertionError("Unexpected startup prompt")),
    )
    monkeypatch.setattr(
        entrypoint,
        "LoadConfig",
        lambda _args: {"flashcarts": {"DMG": {}, "AGB": {}}, "config_ret": []},
    )
    return subdir, appdata


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
    monkeypatch.setattr(
        entrypoint.sys,
        "argv",
        ["FlashGBX", "--cli", "--gbxcartrw-baudrate", "1700000"],
    )

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.main(portableMode=True)

    assert exc_info.value.code == 7
    assert len(received) == 1
    assert received[0]["config_path"] == str(tmp_path / "config")
    assert received[0]["argparsed"].cli is True
    assert received[0]["argparsed"].gbxcartrw_baudrate == 1_700_000


def test_main_launches_gui_with_configured_startup_data(
    startup_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subdir, _appdata = startup_paths
    received: list[entrypoint.StartupArgs] = []
    runs: list[bool] = []

    class FakeGUI:
        def __init__(self, args: entrypoint.StartupArgs) -> None:
            received.append(args)

        def run(self) -> None:
            runs.append(True)

    monkeypatch.setattr(package, "FlashGBX_GUI", SimpleNamespace(FlashGBX_GUI=FakeGUI), raising=False)

    assert entrypoint.main(portableMode=True) is None

    assert len(received) == 1
    assert received[0]["config_path"] == str(subdir)
    assert received[0]["app_path"] == str(subdir.parent)
    assert received[0]["argparsed"].cli is False
    assert received[0]["flashcarts"] == {"DMG": {}, "AGB": {}}
    assert runs == [True]
    assert (subdir / "settings.ini").is_file()


def test_main_uses_selected_appdata_config_and_enables_debug(
    startup_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subdir, appdata = startup_paths
    received: list[entrypoint.StartupArgs] = []

    class FakeGUI:
        def __init__(self, args: entrypoint.StartupArgs) -> None:
            received.append(args)

        def run(self) -> None:
            pass

    monkeypatch.setattr(package, "FlashGBX_GUI", SimpleNamespace(FlashGBX_GUI=FakeGUI), raising=False)
    monkeypatch.setattr(entrypoint.sys, "argv", ["FlashGBX", "--cfgdir", "appdata", "--debug"])

    assert entrypoint.main(portableMode=True) is None

    assert len(received) == 1
    assert received[0]["config_path"] == str(appdata)
    assert received[0]["argparsed"].cfgdir == "appdata"
    assert received[0]["argparsed"].debug is True
    assert AppContext.DEBUG is True
    assert (appdata / "settings.ini").is_file()
    assert not subdir.exists()


@pytest.mark.parametrize("failure", ["import", "constructor"])
def test_main_falls_back_to_cli_after_gui_launch_failure(
    startup_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    subdir, _appdata = startup_paths
    received: list[entrypoint.StartupArgs] = []

    class FakeCLI:
        def __init__(self, args: entrypoint.StartupArgs) -> None:
            received.append(args)

        def run(self) -> int:
            return 23

    monkeypatch.setattr(package, "FlashGBX_CLI", SimpleNamespace(FlashGBX_CLI=FakeCLI), raising=False)
    if failure == "import":
        real_import = builtins.__import__

        def import_without_gui(
            name: str,
            globals: object = None,  # noqa: A002 - matches __import__ signature
            locals: object = None,  # noqa: A002 - matches __import__ signature
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> object:
            if name == "" and level == 1 and "FlashGBX_GUI" in fromlist:
                msg = "No module named 'FlashGBX.FlashGBX_GUI'"
                raise ModuleNotFoundError(msg)
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", import_without_gui)
    else:

        class FailingGUI:
            def __init__(self, _args: entrypoint.StartupArgs) -> None:
                msg = "GUI constructor failed"
                raise RuntimeError(msg)

        monkeypatch.setattr(package, "FlashGBX_GUI", SimpleNamespace(FlashGBX_GUI=FailingGUI), raising=False)

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.main(portableMode=True)

    assert exc_info.value.code == 23
    assert len(received) == 1
    assert received[0]["config_path"] == str(subdir)
    assert received[0]["argparsed"].cli is False
    output = capsys.readouterr().out
    assert "Falling back to CLI mode." in output
    assert "GUI mode couldn't be launched" in output
    assert "Optional command line switches" in output
    assert "Traceback" in output


@pytest.mark.parametrize("use_fallback", [False, True])
def test_main_reports_cli_keyboard_interrupt(
    startup_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    use_fallback: bool,
) -> None:
    _subdir, _appdata = startup_paths
    constructed: list[bool] = []

    class InterruptedCLI:
        def __init__(self, _args: entrypoint.StartupArgs) -> None:
            constructed.append(True)

        def run(self) -> int:
            raise KeyboardInterrupt

    monkeypatch.setattr(package, "FlashGBX_CLI", SimpleNamespace(FlashGBX_CLI=InterruptedCLI), raising=False)
    prompt = Mock(return_value="")
    monkeypatch.setattr(builtins, "input", prompt)
    if use_fallback:

        class FailingGUI:
            def __init__(self, _args: entrypoint.StartupArgs) -> None:
                msg = "GUI unavailable"
                raise RuntimeError(msg)

        monkeypatch.setattr(package, "FlashGBX_GUI", SimpleNamespace(FlashGBX_GUI=FailingGUI), raising=False)
        monkeypatch.setattr(entrypoint.sys, "argv", ["FlashGBX", "--wait"])
    else:
        monkeypatch.setattr(entrypoint.sys, "argv", ["FlashGBX", "--cli", "--wait"])

    with pytest.raises(SystemExit) as exc_info:
        entrypoint.main(portableMode=True)

    assert exc_info.value.code == -1
    assert constructed == [True]
    assert "Program stopped." in capsys.readouterr().out
    prompt.assert_called_once()
    assert "Press ENTER to exit." in prompt.call_args.args[0]


@pytest.mark.parametrize(
    ("argv", "expected_output"),
    [(["FlashGBX", "--help"], "usage:"), (["FlashGBX", "--mode", "invalid"], "invalid choice")],
)
def test_main_parser_exit_prompts_once_without_loading_config(
    startup_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected_output: str,
) -> None:
    subdir, _appdata = startup_paths
    prompt = Mock(return_value="")
    load_config = Mock(side_effect=AssertionError("Config loaded after parser exit"))
    monkeypatch.setattr(builtins, "input", prompt)
    monkeypatch.setattr(entrypoint, "LoadConfig", load_config)
    monkeypatch.setattr(entrypoint.sys, "argv", argv)

    result = entrypoint.main(portableMode=True)
    if "--help" in argv:
        assert result == 0
    # Invalid choices currently return 0 too; keep the exit-code fix separate from coverage work.

    prompt.assert_called_once()
    assert "Press ENTER to exit." in prompt.call_args.args[0]
    load_config.assert_not_called()
    assert not subdir.exists()
    output = capsys.readouterr()
    assert expected_output in output.out + output.err


@pytest.mark.parametrize("accept_fallback", [True, False])
def test_main_configuration_permission_fallback_requires_consent(
    startup_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    accept_fallback: bool,
) -> None:
    subdir, appdata = startup_paths
    original_mkdir = type(subdir).mkdir
    attempted: list[Path] = []

    def mkdir_with_denied_subdir(path: Path, *args: object, **kwargs: object) -> None:
        attempted.append(path)
        if path == subdir:
            msg = "portable config directory is read-only"
            raise PermissionError(msg)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(type(subdir), "mkdir", mkdir_with_denied_subdir)
    prompt = Mock(return_value="y" if accept_fallback else "n")
    monkeypatch.setattr(builtins, "input", prompt)
    gui_args: list[entrypoint.StartupArgs] = []

    class FakeGUI:
        def __init__(self, args: entrypoint.StartupArgs) -> None:
            gui_args.append(args)

        def run(self) -> None:
            pass

    monkeypatch.setattr(package, "FlashGBX_GUI", SimpleNamespace(FlashGBX_GUI=FakeGUI), raising=False)

    assert entrypoint.main(portableMode=True) is None

    prompt.assert_called_once()
    assert "Use directory" in prompt.call_args.args[0]
    assert str(appdata) in prompt.call_args.args[0]
    assert "no permission" in capsys.readouterr().out
    if accept_fallback:
        assert attempted[0] == subdir
        assert appdata in attempted
        assert len(gui_args) == 1
        assert gui_args[0]["config_path"] == str(appdata)
        assert (appdata / "settings.ini").is_file()
    else:
        assert attempted == [subdir]
        assert gui_args == []
        assert not appdata.exists()


def test_main_configuration_permission_without_fallback_stops(
    startup_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subdir, _appdata = startup_paths
    monkeypatch.setattr(
        entrypoint,
        "_startup_paths",
        lambda _portable: (str(subdir.parent), {"subdir": str(subdir)}, str(subdir), None, "subdir"),
    )
    original_mkdir = type(subdir).mkdir

    def deny_subdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == subdir:
            msg = "config directory is read-only"
            raise PermissionError(msg)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(type(subdir), "mkdir", deny_subdir)
    prompt = Mock(return_value="")
    monkeypatch.setattr(builtins, "input", prompt)
    monkeypatch.setattr(entrypoint.sys, "argv", ["FlashGBX", "--wait"])
    config_load = Mock(side_effect=AssertionError("Config loaded after permission failure"))
    monkeypatch.setattr(entrypoint, "LoadConfig", config_load)

    assert entrypoint.main(portableMode=True) is None

    assert prompt.call_count == 2
    assert prompt.call_args_list[0].args == ("",)
    assert "Press ENTER to exit." in prompt.call_args_list[1].args[0]
    config_load.assert_not_called()


def test_package_main_invokes_entrypoint_without_starting_app(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(entrypoint, "main", lambda: calls.append(True))

    runpy.run_module("FlashGBX.__main__", run_name="__main__")

    assert calls == [True]
