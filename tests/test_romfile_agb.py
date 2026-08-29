"""Tests for GBA ROM header parsing without game payloads."""

from __future__ import annotations

import hashlib
import json
import zlib
from typing import TYPE_CHECKING

import pytest

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


def test_agb_fix_header_and_logo_rejects_empty_data(
    pokemon_red_header: bytearray,
) -> None:
    del pokemon_red_header
    header = make_agb_header()
    header[0xBD] = 0
    rom = RomFileAGB(header)

    fixed = rom.FixHeader()

    assert fixed == header[:0x200]
    assert header[0xBD] == rom.CalcChecksumHeader()
    assert rom.LogoToImage(bytearray(16)) is False
    assert rom.LogoToImage(bytearray([0xFF] * 16)) is False


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
