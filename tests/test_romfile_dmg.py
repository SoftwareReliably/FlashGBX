"""Generated-ROM tests for Game Boy header parsing and conversion helpers."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

import FlashGBX.RomFileDMG as dmg_module  # noqa: N813
from FlashGBX.app import AppContext
from FlashGBX.RomFileDMG import RomFileDMG, from_isx


def make_header(
    base: bytearray,
    *,
    title: str,
    mapper: int = 0,
    rom_size: int = 0,
    ram_size: int = 0,
    checksum: int | None = None,
    cgb: int = 0,
) -> bytearray:
    """Return generated header metadata with no copyrighted ROM payload."""
    header = bytearray(base)
    header[0x134:0x144] = b"\x00" * 16
    title_size = 15 if cgb in (0x80, 0xC0) else 16
    header[0x134 : 0x134 + title_size] = title.encode("ascii")[:title_size].ljust(title_size, b"\x00")
    if cgb in (0x80, 0xC0):
        header[0x143] = cgb
    header[0x147] = mapper
    header[0x148] = rom_size
    header[0x149] = ram_size
    header[0x14D] = RomFileDMG(header).CalcChecksumHeader() if checksum is None else checksum
    header[0x14E:0x150] = b"\x00\x00"
    header[0x14E:0x150] = (sum(header) & 0xFFFF).to_bytes(2, byteorder="big")
    return header


def parse_without_databases(
    monkeypatch: pytest.MonkeyPatch,
    header: bytearray,
    *,
    unchanged: bool = False,
) -> dict:
    rom = RomFileDMG(header)
    monkeypatch.setattr(rom, "GetDatabaseEntry", lambda: None)
    monkeypatch.setattr(rom, "GetBatterylessSramConfig", lambda _header: None)
    return rom.GetHeader(unchanged=unchanged)


def test_file_loading_and_checksum_repair(
    tmp_path: Path,
    pokemon_red_header: bytearray,
) -> None:
    source = make_header(pokemon_red_header, title="POKEMON RED", mapper=0x13, rom_size=5, ram_size=3)
    source[0x14D:0x150] = b"\x00\x00\x00"
    rom_path = tmp_path / "generated.gb"
    rom_path.write_bytes(source + bytes(0x2000))

    rom = RomFileDMG(rom_path)
    fixed = rom.FixHeader()

    assert len(rom.ROMFILE) == 0x1000
    assert len(fixed) == 0x200
    assert rom.ROMFILE[0x14D] == rom.CalcChecksumHeader()
    assert int.from_bytes(rom.ROMFILE[0x14E:0x150], "big") == rom.CalcChecksumGlobal()


def test_short_buffer_has_no_header() -> None:
    assert RomFileDMG(bytearray(0x17F)).GetHeader() == {}


def test_dmg_logo_rendering_and_unknown_header_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rom = RomFileDMG()
    assert rom.Load() is None

    image = rom.LogoToImage(bytearray(48), valid=False)
    assert image is not False
    assert image.getpalette()[:6] == [255, 255, 255, 255, 0, 0]

    monkeypatch.setattr(dmg_module, "Image", None)
    assert rom.LogoToImage(bytearray(b"logo")) is False

    header = make_header(bytearray(0x200), title="UNKNOWN", mapper=0, rom_size=0xFF, ram_size=0xFF)
    monkeypatch.setattr(rom, "GetDatabaseEntry", lambda: None)
    data = RomFileDMG(header).GetHeader()
    assert data["rom_size"] == "?"
    assert data["ram_size"] == "?"


def test_cgb_title_extracts_game_and_maker_codes(
    monkeypatch: pytest.MonkeyPatch,
    pokemon_red_header: bytearray,
) -> None:
    header = make_header(pokemon_red_header, title="POKEMON REDAXVE", mapper=0x13, cgb=0x80)
    header[0x14B] = 0x33
    header[0x144:0x146] = b"01"

    data = parse_without_databases(monkeypatch, header)

    assert data["game_title"] == "POKEMON RED"
    assert data["game_code"] == "AXVE"
    assert data["maker_code_new"] == "01"
    assert data["cgb"] == 0x80


def test_unchanged_header_snapshot_precedes_mapper_normalization(
    monkeypatch: pytest.MonkeyPatch,
    pokemon_red_header: bytearray,
) -> None:
    header = make_header(pokemon_red_header, title="MBC2 TEST", mapper=0x06)

    data = parse_without_databases(monkeypatch, header, unchanged=True)

    assert data["mapper_raw"] == 0x06
    assert data["ram_size_raw"] == 0
    assert data["unchanged"]["ram_size_raw"] == 0


@pytest.mark.parametrize(
    ("title", "mapper", "ram_size", "checksum", "expected"),
    [
        ("MBC2 TEST", 0x06, 0, None, {"mapper_raw": 0x06, "ram_size_raw": 0x100}),
        ("MBC30 TEST", 0x10, 5, None, {"mapper_raw": 0x110}),
        ("MBC6 TEST", 0x20, 0, None, {"ram_size_raw": 0x104}),
        ("KIRBY TNT", 0x22, 0, None, {"ram_size_raw": 0x101}),
        ("CMASTER", 0x22, 0, None, {"ram_size_raw": 0x102}),
        ("TAMA5 TEST", 0xFD, 0, None, {"ram_size_raw": 0x103}),
        ("BOMCOL", 0x01, 0, 0x86, {"mapper_raw": 0x101}),
        (
            "NP M-MENU MENU",
            0x19,
            0,
            0xD3,
            {"mapper_raw": 0x105, "rom_size_raw": 5, "ram_size_raw": 4},
        ),
        ("TETRIS SET", 0x10, 0, 0x3F, {"mapper_raw": 0x104}),
        ("BUBBLEBOBBLE SET", 0x11, 0, 0xC6, {"mapper_raw": 0x0B}),
        (
            "GB HICOL",
            0x19,
            0,
            0x4A,
            {"mapper_raw": 0x201, "rom_size_raw": 0x0A, "ram_size_raw": 0x201},
        ),
        (
            "MBCX_MENU",
            0,
            0,
            None,
            {"mapper_raw": 0x206, "rom_size_raw": 0x0A, "ram_size_raw": 3},
        ),
        ("PHOTO", 0, 0, None, {"ram_size_raw": 0x204}),
    ],
)
def test_special_mapper_metadata_is_normalized(
    monkeypatch: pytest.MonkeyPatch,
    pokemon_red_header: bytearray,
    title: str,
    mapper: int,
    ram_size: int,
    checksum: int | None,
    expected: dict[str, int],
) -> None:
    header = make_header(
        pokemon_red_header,
        title=title,
        mapper=mapper,
        ram_size=ram_size,
        checksum=checksum,
    )

    data = parse_without_databases(monkeypatch, header)

    assert data.items() >= expected.items()


def test_database_lookup_handles_match_corruption_and_missing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    rom = RomFileDMG()
    rom.DATA = {"header_sha1": "header-id"}
    database_path = tmp_path / "db_DMG.json"
    database_path.write_text(json.dumps({"header-id": {"gc": "DMG-APAE"}}), encoding="utf-8")

    assert rom.GetDatabaseEntry() == {"gc": "DMG-APAE"}

    database_path.write_text("{broken", encoding="utf-8")
    assert rom.GetDatabaseEntry() is None
    assert "corrupted" in capsys.readouterr().out

    database_path.unlink()
    assert rom.GetDatabaseEntry() is None
    assert "not found" in capsys.readouterr().out


def test_batteryless_sram_database_matches_raw_and_clean_titles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "db_DMG_bl.json"
    database_path.write_text(json.dumps({"POKEMON RED": [0x100, 0x200, 1]}), encoding="utf-8")
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    monkeypatch.setattr(RomFileDMG, "BATTERYLESS_SRAM_DB", None)

    assert RomFileDMG.GetBatterylessSramConfig({"game_title_raw": "POKEMON RED\x00 "}) == {
        "bl_offset": 0x100,
        "bl_size": 0x200,
        "bl_layout": 1,
    }
    assert RomFileDMG.GetBatterylessSramConfig({"game_title_raw": "UNKNOWN"}) is None
    assert RomFileDMG.GetBatterylessSramConfig(None) is None


def test_batteryless_sram_database_failure_is_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "db_DMG_bl.json"
    database_path.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    monkeypatch.setattr(RomFileDMG, "BATTERYLESS_SRAM_DB", None)
    monkeypatch.setattr(Path, "exists", lambda path: path == database_path)

    assert RomFileDMG.GetBatterylessSramConfig({"game_title_raw": "POKEMON RED"}) is None
    assert RomFileDMG.BATTERYLESS_SRAM_DB is False
    assert "Could not load" in capsys.readouterr().out


def test_isx_conversion_places_banked_data_and_rounds_rom_size() -> None:
    record = b"\x01\x02" + struct.pack("<HH", 0x0123, 3) + b"RED" + b"\x04"

    converted = from_isx(record)

    offset = 2 * 0x4000 + 0x0123
    assert converted[offset : offset + 3] == b"RED"
    assert len(converted) == 0x10000


def test_isx_conversion_reports_unknown_and_truncated_records(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert from_isx(b"\x02\x04") == bytearray(0x8000)
    assert "Unhandled ISX record type" in capsys.readouterr().out

    assert from_isx(b"\x01") == bytearray(0x8000)
    assert "Couldn’t convert ISX" in capsys.readouterr().out
