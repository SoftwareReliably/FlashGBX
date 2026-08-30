"""Tests for GBA ROM header parsing without game payloads."""

from __future__ import annotations

import hashlib
import json
import zlib
from typing import TYPE_CHECKING

import pytest

import FlashGBX.RomFileAGB as agb_module  # noqa: N813
from FlashGBX.app import AppContext
from FlashGBX.RomFileAGB import RomFileAGB

if TYPE_CHECKING:
    from pathlib import Path


def make_agb_header(*, title: str = "TEST GAME", code: str = "ABCD", maker: str = "01") -> bytearray:
    header = bytearray(0x200)
    header[0xA0:0xAC] = title.encode("ascii")[:12].ljust(12, b"\x00")
    header[0xAC:0xB0] = code.encode("ascii")[:4].ljust(4, b"\x00")
    header[0xB0:0xB2] = maker.encode("ascii")[:2].ljust(2, b"\x00")
    header[0xB2] = 0x96
    header[0xBC] = 2
    header[0xBD] = RomFileAGB(header).CalcChecksumHeader()
    return header


def test_agb_file_loading_checksums_and_header_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "test.gba"
    path.write_bytes(make_agb_header() + b"payload")
    rom = RomFileAGB(path)
    monkeypatch.setattr(rom, "GetDatabaseEntry", lambda: None)

    assert len(rom.ROMFILE) == 0x207
    assert rom.CalcChecksumGlobal() == zlib.crc32(rom.ROMFILE) & 0xFFFFFFFF
    rom.LogoToImage = lambda _data, _valid=True: False  # type: ignore[method-assign]
    data = rom.GetHeader(unchanged=True)

    assert data["game_title"] == "TEST GAME"
    assert data["game_code"] == "ABCD"
    assert data["maker_code"] == "01"
    assert data["96h_correct"] is True
    assert data["header_checksum_correct"] is True
    assert data["unchanged"]["game_title"] == "TEST GAME"
    assert data["empty"] is True
    assert "logo" not in data


def test_agb_fix_header_and_logo_rejects_empty_data() -> None:
    header = make_agb_header()
    header[0xBD] = 0
    rom = RomFileAGB(header)

    fixed = rom.FixHeader()

    assert fixed == header[:0x200]
    assert header[0xBD] == rom.CalcChecksumHeader()
    assert rom.LogoToImage(bytearray(16)) is False
    assert rom.LogoToImage(bytearray([0xFF] * 16)) is False
    assert rom.LogoToImage(b"\x01") is False


def test_agb_instances_have_independent_buffers_and_accept_immutable_bytes() -> None:
    first = RomFileAGB()
    second = RomFileAGB()
    first.ROMFILE.extend(b"first")

    assert bytearray() == second.ROMFILE
    assert bytearray(b"immutable") == RomFileAGB(b"immutable").ROMFILE


def test_agb_header_text_fields_are_sanitized_consistently(monkeypatch: pytest.MonkeyPatch) -> None:
    header = make_agb_header()
    header[0xA0:0xAC] = b"BAD__  TITLE"
    header[0xAC:0xB0] = b"A__B"
    header[0xB0:0xB2] = b"__"
    header[0xBD] = RomFileAGB(header).CalcChecksumHeader()
    rom = RomFileAGB(header)
    monkeypatch.setattr(rom, "GetDatabaseEntry", lambda: None)

    data = rom.GetHeader()

    assert data["game_title"] == "BAD_ TITLE"
    assert data["game_code"] == "A_B"
    assert data["maker_code"] == "_"


@pytest.mark.parametrize(
    ("title", "code", "checksum", "expected"),
    [
        ("NGC-HIKARU3", "GHTJ", 0xB3, "dacs_8m"),
        ("CARDE READER", "PEAJ", 0x9E, "ereader"),
    ],
)
def test_agb_special_cartridge_flags(
    monkeypatch: pytest.MonkeyPatch,
    title: str,
    code: str,
    checksum: int,
    expected: str,
) -> None:
    rom = RomFileAGB(make_agb_header(title=title, code=code))
    rom.ROMFILE[0xBD] = checksum
    monkeypatch.setattr(rom, "GetDatabaseEntry", lambda: None)

    data = rom.GetHeader()

    assert data[expected] is True


def test_agb_detects_vast_fame_and_database_code_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = make_agb_header()
    header[0x15C:0x16C] = bytes.fromhex("B4009FE59910A0E30010C0E5AC009FE5")
    rom = RomFileAGB(header)
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    header_sha1 = hashlib.sha1(header[0:0x180]).hexdigest()
    (tmp_path / "db_AGB.json").write_text(
        json.dumps({header_sha1: {"gc": "ZMAJ", "3d": True}}),
        encoding="utf-8",
    )

    data = rom.GetHeader()

    assert data["vast_fame"] is True
    assert data["db"]["gc"] == "AGS-ZMAJ"
    assert data["3d_memory"] is True


@pytest.mark.parametrize(
    ("raw_code", "prefix"),
    [
        ("ZMAJ", "AGS-"),
        ("ZMBJ", "AGS-"),
        ("ZMDE", "AGS-"),
        ("ZBBJ", "NTR-"),
        ("PEAJ", "PEC-"),
        ("PSAJ", "PES-"),
        ("PSAE", "PES-"),
        ("ABCD", "AGB-"),
    ],
)
def test_agb_database_code_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_code: str,
    prefix: str,
) -> None:
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    (tmp_path / "db_AGB.json").write_text(
        json.dumps({"header": {"gc": raw_code}}),
        encoding="utf-8",
    )
    rom = RomFileAGB(bytearray(0x200))
    rom.DATA = {"header_sha1": "header"}

    entry = rom.GetDatabaseEntry()

    assert entry is not None
    assert entry["gc"] == prefix + raw_code


def test_agb_load_open_and_parser_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rom = RomFileAGB()
    assert rom.Load() is None

    path = tmp_path / "input.gba"
    path.write_bytes(b"payload")
    rom.Open(path)
    assert bytearray(b"payload") == rom.ROMFILE
    assert RomFileAGB(bytearray(0x100)).GetHeader() == {}

    monkeypatch.setattr(agb_module, "Image", None)
    full_rom = RomFileAGB(make_agb_header())
    monkeypatch.setattr(full_rom, "GetDatabaseEntry", lambda: None)
    data = full_rom.GetHeader()
    assert data["db"] is None
    assert full_rom.LogoToImage(bytearray(b"not-empty")) is False

    (tmp_path / "db_AGB.json").write_text("{invalid", encoding="utf-8")
    full_rom.DATA = {"header_sha1": "header"}
    assert full_rom.GetDatabaseEntry() is None


@pytest.mark.parametrize(
    "database",
    [
        [],
        {"header": []},
        {"header": {"gc": 123}},
    ],
)
def test_agb_database_rejects_invalid_structures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database: object,
) -> None:
    monkeypatch.setattr(AppContext, "CONFIG_PATH", str(tmp_path))
    (tmp_path / "db_AGB.json").write_text(json.dumps(database), encoding="utf-8")
    rom = RomFileAGB(bytearray(0x200))
    rom.DATA = {"header_sha1": "header"}

    assert rom.GetDatabaseEntry() is None
    assert RomFileAGB().GetDatabaseEntry() is None
