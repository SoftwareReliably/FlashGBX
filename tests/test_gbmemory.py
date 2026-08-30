"""Tests for GB-Memory hidden-sector map generation and parsing."""

from __future__ import annotations

import struct

import pytest

from FlashGBX.GBMemory import GBMemoryMap
from FlashGBX.RomFileDMG import RomFileDMG


def test_gbmemory_static_helpers_cover_mapper_sizes_and_encoding() -> None:
    assert GBMemoryMap._title_encoding("GBMEM-MENU MMSA") == "UTF-8"
    assert GBMemoryMap._title_encoding("POKEMON RED") == "SHIFT-JIS"
    assert GBMemoryMap._decode(b"title\xff") == "title"
    assert GBMemoryMap._fixed_ascii("abc", 5) == b"abc\xff\xff"
    assert GBMemoryMap._fixed_ascii("abcdef", 3) == b"abc"
    assert len(GBMemoryMap._timestamp()) == 18
    assert [GBMemoryMap._rom_size_type(size) for size in (0x20000, 0x20001, 0x40000, 0x80000, 0x80001)] == [
        2,
        3,
        3,
        4,
        5,
    ]
    assert GBMemoryMap._sram_type(2, 0) == 2
    assert GBMemoryMap._sram_type(0, 0x03) == 3
    assert GBMemoryMap._sram_type(0, 0xFFFF) == 0
    packed = GBMemoryMap._pack_map(3, 5, 2, 7, 9)
    assert packed == (3 << 29) | (5 << 26) | (2 << 23) | (7 << 16) | (9 << 8)


def test_import_and_parse_single_game_map(
    monkeypatch: pytest.MonkeyPatch,
    pokemon_red_header: bytearray,
) -> None:
    monkeypatch.setattr(RomFileDMG, "GetDatabaseEntry", lambda _self: None)
    monkeypatch.setattr(GBMemoryMap, "_timestamp", staticmethod(lambda: b"01/01/202600:00:00"))
    memory_map = GBMemoryMap()

    assert memory_map.ImportROM(pokemon_red_header) is True
    assert memory_map.IsMenu() is False
    assert memory_map.GetMapData()[0:3] != b"\xff\xff\xff"

    raw = memory_map.ParseMapData(memory_map.GetMapData())
    assert raw["f_size"] == 1
    assert raw["b_size"] == 256
    assert raw["game_code"] == b"DMG -    -  "
    parsed = memory_map.ParseMapData(memory_map.GetMapData(), pokemon_red_header)
    assert parsed["title"].startswith("POKEMON RED")
    assert parsed["game_code"] == "DMG -    -  "


def test_map_constructor_preserves_cart_id_and_saturates_write_count(
    monkeypatch: pytest.MonkeyPatch,
    pokemon_red_header: bytearray,
) -> None:
    monkeypatch.setattr(RomFileDMG, "GetDatabaseEntry", lambda _self: None)
    old_map = bytearray(0x80)
    old_map[0x6E:0x70] = struct.pack("=H", 0xFFFF)
    old_map[0x70:0x78] = b"CARTID!!"

    memory_map = GBMemoryMap(pokemon_red_header, old_map)

    assert memory_map.GetMapData()[0x6E:0x70] == b"\xff\xff"
    assert memory_map.GetMapData()[0x70:0x78] == b"CARTID!!"
    assert GBMemoryMap(pokemon_red_header, bytearray(2)).GetMapData()[0x6E:0x70] == b"\x00\x00"


def test_menu_map_without_entries_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    pokemon_red_header: bytearray,
) -> None:
    monkeypatch.setattr(RomFileDMG, "GetDatabaseEntry", lambda _self: None)
    menu_header = bytearray(pokemon_red_header)
    menu_header[0x134:0x144] = b"NP M-MENU MENU".ljust(16, b"\x00")
    menu_rom = menu_header + bytearray([0xFF] * (0x100000 - len(menu_header)))
    memory_map = GBMemoryMap()

    assert memory_map.ImportROM(menu_rom) is True
    assert memory_map.IsMenu() is True
    parsed = memory_map.ParseMapData(memory_map.GetMapData(), menu_rom)
    assert isinstance(parsed, list)
    assert parsed[0]["num_games"] == 0


def test_populated_menu_map_import_and_parse(
    monkeypatch: pytest.MonkeyPatch,
    pokemon_red_header: bytearray,
) -> None:
    monkeypatch.setattr(RomFileDMG, "GetDatabaseEntry", lambda _self: None)
    menu_header = bytearray(pokemon_red_header)
    menu_header[0x134:0x144] = b"NP M-MENU MENU".ljust(16, b"\x00")
    menu_rom = bytearray([0xFF] * 0x100000)
    menu_rom[: len(menu_header)] = menu_header

    item = struct.pack(
        "=BBBHH12s44s384s18s8s23s16s",
        0,
        2,
        0,
        1,
        1,
        b"GAME -  -  ",
        b"Nested game".ljust(44, b"\x00"),
        b"\x00" * 384,
        b"01/01/202600:00:00",
        b"TESTAPP".ljust(8, b"\x00"),
        b"\x00" * 23,
        b"comment".ljust(16, b"\x00"),
    )
    menu_rom[0x1C000 : 0x1C000 + len(item)] = item
    menu_rom[0x40000 : 0x40000 + len(pokemon_red_header)] = pokemon_red_header

    memory_map = GBMemoryMap()

    assert memory_map.ImportROM(menu_rom) is True
    parsed = memory_map.ParseMapData(memory_map.GetMapData(), menu_rom)

    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[1]["menu_index"] == 0
    assert parsed[1]["rom_offset"] == 0x40000
    assert parsed[1]["header"]["game_title"] == "POKEMON RED"
    assert "crc32" in parsed[1]


def test_gbmemory_rejects_unknown_menu_layout_and_maps_all_mbc_types() -> None:
    with pytest.raises(ValueError, match="Unsupported GB-Memory menu"):
        GBMemoryMap._unpack_menu_item(bytearray(0x100), "unknown", 0)

    memory_map = GBMemoryMap()
    assert [memory_map.MapperToMBCType(value) for value in (0x00, 0x01, 0x06, 0x10, 0x19, 0xFF)] == [0, 1, 2, 3, 5, 5]
    assert [memory_map.GetBlockSizeBackup(value) for value in (None, 0, 1, 64, 256, 1024, 2)] == [4, 0, 1, 1, 4, 16, 4]


def test_map_rejects_short_and_invalid_rom_data() -> None:
    assert GBMemoryMap().ImportROM(bytearray(0x17F)) is False
    assert GBMemoryMap().ParseMapData(False) is False
    assert GBMemoryMap().ParseMapData(bytearray(2)) is False
