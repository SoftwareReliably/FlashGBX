"""Tests for platform labels and deterministic output filename generation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import FlashGBX.app as app_module
from FlashGBX.app import AppInfo, generate_filename


def dmg_header(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "game_title": "POKEMON/RED",
        "game_code": "AXVE",
        "version": 2,
        "mapper_raw": 0x13,
        "cgb": 0x80,
        "old_lic": 0,
        "sgb": 0,
        "db": None,
    }
    value.update(overrides)
    return value


def agb_header(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {"game_title": "TEST/GAME", "game_code": "ABCD", "version": 3, "db": None}
    value.update(overrides)
    return value


def test_app_info_os_string_and_filename_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(app_module.platform, "release", lambda: "6.1")
    monkeypatch.setattr(app_module.platform, "platform", lambda: "Linux-6.1")
    assert AppInfo.os_string() == "Linux-6.1"
    assert generate_filename("DMG", dmg_header()) == "POKEMON_RED_AXVE-2.gbc"
    assert generate_filename("DMG", dmg_header(game_title="", game_code="")) == "ROM.gbc"
    assert generate_filename("AGB", agb_header()) == "TEST_GAME_ABCD-3.gba"
    assert generate_filename("AGB", agb_header(game_title="", game_code="")) == "ROM.gba"


def test_generate_filename_honors_settings_database_and_gbmemory_ids() -> None:
    class Settings:
        def __init__(self, values: dict[str, str]) -> None:
            self.values = values

        def value(self, key: str, default: str) -> str:
            return self.values.get(key, default)

    settings = Settings({"UseNoIntroFilenames": "disabled", "AutoFileExtensionSGB": "disabled"})
    assert (
        generate_filename(
            "DMG",
            dmg_header(cgb=0, old_lic=0x33, sgb=3, game_code=""),
            settings,
        )
        == "POKEMON_RED-2.gb"
    )

    db_header = dmg_header(db={"gn": "Pokemon Red", "ne": "USA"})
    assert generate_filename("DMG", db_header) == "Pokemon Red USA.gbc"

    gbmem_header = dmg_header(
        mapper_raw=0x105,
        game_title="MENU",
        db={"gn": "ignored", "ne": "ignored"},
        gbmem_parsed={"cart_id": "ABC123"},
    )
    assert generate_filename("DMG", gbmem_header) == "NP GB-Memory Cartridge (ABC123).gbc"


def test_generate_filename_accepts_list_gbmemory_data() -> None:
    header = dmg_header(
        mapper_raw=0x105,
        game_title="MENU",
        db={"gn": "ignored", "ne": "ignored"},
        gbmem_parsed=[{"cart_id": "LISTID"}],
    )

    assert generate_filename("DMG", header) == "NP GB-Memory Cartridge (LISTID).gbc"


def test_generate_filename_appends_list_gbmemory_id_without_database() -> None:
    header = dmg_header(
        mapper_raw=0x105,
        game_title="MENU",
        game_code="",
        gbmem_parsed=[{"cart_id": "LISTID"}],
    )

    assert generate_filename("DMG", header) == "MENU-2_LISTID.gbc"

    header["db"] = {"gn": "Database", "ne": "Entry"}
    header["gbmem_parsed"] = []
    assert generate_filename("DMG", header) == "Database Entry.gbc"


def test_generate_filename_validates_headers_and_ignores_non_string_settings() -> None:
    class Settings:
        def value(self, key: str, default: str) -> object:
            del key, default
            return object()

    assert generate_filename("AGB", agb_header(), Settings()) == "TEST_GAME_ABCD-3.gba"

    with pytest.raises(TypeError, match="game_title"):
        generate_filename("AGB", agb_header(game_title=123))
    with pytest.raises(TypeError, match="mapper_raw"):
        generate_filename("DMG", dmg_header(mapper_raw=True))
    with pytest.raises(TypeError, match="must be a mapping"):
        generate_filename("AGB", agb_header(db="invalid"))


def test_generate_filename_custom_mapper_pattern_and_unknown_mode() -> None:
    class Settings:
        def __init__(self) -> None:
            self.values = {
                "UseNoIntroFilenames": "disabled",
                "FileNameFormatDMG": "%TITLE%_%MAPPER%",
                "AutoFileExtensionSGB": "enabled",
            }

        def value(self, key: str, default: str) -> str:
            return self.values.get(key, default)

    header = dmg_header(cgb=0, old_lic=0x33, sgb=3, game_code="")
    assert generate_filename("DMG", header, Settings()) == "POKEMON_RED_MBC3.sgb"
    assert generate_filename(None, {"db": {"gn": "Unknown", "ne": "Mode"}}) == "Unknown Mode.bin"


def test_app_info_unknown_platform_falls_back_to_platform_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(app_module.platform, "platform", lambda: "Plan9 release")
    monkeypatch.setattr(app_module.platform, "machine", lambda: "generic")

    assert AppInfo.os_string() == "Plan9 release"


def test_app_info_formats_supported_windows_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    versions = [
        (10, 0, 22000, "Windows 11"),
        (10, 0, 19045, "Windows 10"),
        (6, 3, 9600, "Windows 8.1"),
        (6, 2, 9200, "Windows 8"),
        (6, 1, 7601, "Windows 7"),
        (6, 0, 6002, "Windows Vista"),
        (5, 1, 2600, "Windows XP"),
        (4, 0, 1381, "Windows 4.0"),
    ]

    for major, minor, build, expected in versions:
        monkeypatch.setattr(
            app_module.sys,
            "getwindowsversion",
            lambda major=major, minor=minor, build=build: SimpleNamespace(
                major=major,
                minor=minor,
                build=build,
            ),
            raising=False,
        )
        assert AppInfo.os_string() == f"{expected} (Build {build})"


def test_app_info_reads_windows_registry_version_details(monkeypatch: pytest.MonkeyPatch) -> None:
    class RegistryKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    values: dict[str, object] = {
        "ProductName": "Windows 10 Pro",
        "DisplayVersion": "24H2",
        "UBR": "1234",
    }

    def query_value(_key: object, name: str) -> tuple[object, int]:
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    fake_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=lambda _root, _path: RegistryKey(),
        QueryValueEx=query_value,
    )
    monkeypatch.setitem(app_module.sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        app_module.sys,
        "getwindowsversion",
        lambda: SimpleNamespace(major=10, minor=0, build=26100),
        raising=False,
    )

    assert AppInfo.os_string() == "Windows 11 (Version 24H2, Build 26100.1234)"

    del values["DisplayVersion"]
    values["ReleaseId"] = "2009"
    assert AppInfo.os_string() == "Windows 11 (Version 2009, Build 26100.1234)"


def test_app_info_windows_fallback_handles_version_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        app_module.sys,
        "getwindowsversion",
        lambda: (_ for _ in ()).throw(OSError("unsupported")),
        raising=False,
    )
    monkeypatch.setattr(app_module.platform, "release", lambda: "10")
    monkeypatch.setattr(app_module.platform, "version", lambda: "build")
    assert AppInfo.os_string() == "Windows 10 (build)"

    monkeypatch.setattr(app_module.platform, "release", lambda: "")
    monkeypatch.setattr(app_module.platform, "platform", lambda: "Windows fallback")
    assert AppInfo.os_string() == "Windows fallback"
