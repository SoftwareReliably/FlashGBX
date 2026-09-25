"""Generated-ROM tests for Game Boy header parsing and conversion helpers."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import Mock

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
    assert image.getpalette()[:6] == [255, 255, 255, 255, 0, 0]  # pyright: ignore[reportOptionalSubscript]

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


@pytest.mark.parametrize(
    ("signature_size", "digest", "title_range", "title", "expected_mapper"),
    [
        (
            0x4C,
            "06ACDCB6D19BD9E395A238B800970D783FC6B7BD",
            (0, 0x10),
            b"XPLODER TEST",
            0x203,
        ),
        (
            0x33,
            "FA685A3785EF65232D6F23AC020515208BDEC523",
            (0x134, 0x150),
            b"ORBIT V2 TEST",
            0x205,
        ),
        (
            0x3F,
            "C1F4154AEFCC5BE7EC83A8BB7BC0958335EC9AF2",
            (0x134, 0x140),
            b"DATEL LEGACY",
            0x205,
        ),
    ],
)
def test_special_mapper_titles_use_each_original_header_slice(
    monkeypatch: pytest.MonkeyPatch,
    signature_size: int,
    digest: str,
    title_range: tuple[int, int],
    title: bytes,
    expected_mapper: int,
) -> None:
    matching_digest = bytearray.fromhex(digest)

    class FakeHash:
        def __init__(self, value: bytearray) -> None:
            self.value = value

        def digest(self) -> bytearray:
            return self.value

    def signature_sha1(payload: bytes | bytearray) -> FakeHash:
        result = matching_digest if len(payload) == signature_size else bytearray(20)
        return FakeHash(result)

    monkeypatch.setattr(dmg_module.hashlib, "sha1", signature_sha1)
    buffer = bytearray(0x280)
    start, end = title_range
    buffer[start:end] = title.ljust(end - start, b"\x00")
    data: dict[str, object] = {
        "mapper_raw": 0,
        "game_title": "REGULAR TEST",
        "header_checksum": 0,
        "version": 0,
    }

    RomFileDMG(buffer)._ApplySpecialMapperOverrides(data, buffer)

    assert data["mapper_raw"] == expected_mapper
    assert data["game_title"] == title.decode("ascii")


def test_unlicensed_mapper_title_decode_failure_keeps_original_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dmg_module.re, "sub", Mock(side_effect=ValueError("malformed title")))
    data: dict[str, object] = {"mapper_raw": 0x203}

    dmg_module._update_unlicensed_mapper_title(data, bytearray(b"XPL"), "title parse failed")

    assert data == {"mapper_raw": 0x203}


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


def test_database_lookup_preserves_string_and_integer_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    rom = RomFileDMG()
    rom.DATA = {"header_sha1": "header-id"}
    entry = {"gn": "Test Game", "gc": "DMG-TEST", "rc": 0x12345678, "extra": 42}
    (tmp_path / "db_DMG.json").write_text(json.dumps({"header-id": entry}), encoding="utf-8")

    assert rom.GetDatabaseEntry() == entry


def test_database_lookup_returns_none_for_missing_key_and_null_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    rom = RomFileDMG()
    rom.DATA = {"header_sha1": "header-id"}
    database_path = tmp_path / "db_DMG.json"

    database_path.write_text(json.dumps({"other-id": {"gc": "DMG-OTHER"}}), encoding="utf-8")
    assert rom.GetDatabaseEntry() is None

    database_path.write_text(json.dumps({"header-id": None}), encoding="utf-8")
    assert rom.GetDatabaseEntry() is None


@pytest.mark.parametrize(
    "database",
    [
        [],
        {"header-id": []},
        {"header-id": {"bad": []}},
        {"header-id": {"bad": {"nested": "object"}}},
        {"header-id": {"bad": 1.5}},
        {"header-id": {"bad": True}},
        {"header-id": {"bad": None}},
    ],
    ids=[
        "non-object-root",
        "non-object-entry",
        "list-value",
        "object-value",
        "float-value",
        "bool-value",
        "null-value",
    ],
)
def test_database_lookup_rejects_invalid_json_shapes_and_values(
    database: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    rom = RomFileDMG()
    rom.DATA = {"header_sha1": "header-id"}
    (tmp_path / "db_DMG.json").write_text(json.dumps(database), encoding="utf-8")

    assert rom.GetDatabaseEntry() is None
    assert "corrupted" in capsys.readouterr().out


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
    assert "Couldn't convert ISX" in capsys.readouterr().out
