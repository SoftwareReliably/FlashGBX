"""Tests for text dump-report generation in both supported cartridge modes."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from FlashGBX.DumpReport import DumpReport


class ReportDevice:
    SUPPORTED_CARTS: ClassVar[dict[str, dict[str, dict[str, object]]]] = {
        "DMG": {"Profile": {}},
        "AGB": {"Vast Fame": {}},
    }

    def __init__(self, name: str = "GBxCart RW") -> None:
        self.name = name
        self.INFO = {"rom_checksum_calc": 0x1234}

    def GetFullName(self) -> str:
        return self.name

    def GetFirmwareVersion(self) -> str:
        return "L18"

    def GetName(self) -> str:
        return self.name

    def GetBaudRate(self) -> int:
        return 1_000_000

    def GetReadErrors(self) -> int:
        return 2


def dmg_header(**overrides: Any) -> dict[str, Any]:
    header = {
        "game_title": "TEST GAME",
        "game_code": "",
        "version": 1,
        "cgb": 0,
        "old_lic": 0,
        "sgb": 0,
        "logo_correct": True,
        "header_checksum": 0xAA,
        "header_checksum_calc": 0xAA,
        "header_checksum_correct": True,
        "rom_checksum": 0x1234,
        "rom_checksum_calc": 0x1234,
        "rom_size_raw": 0,
        "ram_size_raw": 0,
        "mapper_raw": 0x13,
        "db": None,
    }
    header.update(overrides)
    return header


def base_report(mode: str, header: dict[str, Any]) -> dict[str, Any]:
    return {
        "header": header,
        "system": mode,
        "rom_size": 32 * 1024,
        "cart_type": 0,
        "file_name": "test.gb",
        "file_size": 32 * 1024,
        "hash_crc32": 0xAABBCCDD,
        "hash_md5": "md5",
        "hash_sha1": "sha1",
        "hash_sha256": "sha256",
        "timestamp": "now",
        "transfer_size": 256,
        "mapper_type": 0x13,
        "dmg_read_method": "Fast",
        "agb_read_method": "Fast",
        "agb_savelib": "SRAM_V",
    }


def test_generate_dmg_report_includes_parsed_and_database_fields() -> None:
    header = dmg_header(
        cgb=0x80,
        game_code="ABCD",
        db={
            "gn": "Test",
            "ne": "USA",
            "rg": "US",
            "lg": "en",
            "rv": "1.0",
            "gc": "DMG-TEST",
            "rc": 0xAABBCCDD,
            "rs": 32 * 1024,
        },
    )
    report = DumpReport.generate(base_report("DMG", header), ReportDevice())

    assert "== File Information ==" in report
    assert "Hardware:" in report
    assert "Game Boy Color:" in report
    assert "Game Code:" in report
    assert "== Database Match ==" in report
    assert "Test USA" in report


def test_generate_agb_report_includes_flash_eeprom_and_vast_fame_fields() -> None:
    header = {
        "game_title_raw": "TEST GAME\x00",
        "game_code_raw": "ABCD",
        "version": 2,
        "logo_correct": False,
        "header_checksum": 0x01,
        "header_checksum_calc": 0x02,
        "header_checksum_correct": False,
        "db": {
            "gn": "Test",
            "ne": "USA",
            "rg": "US",
            "lg": "en",
            "rv": "1.0",
            "gc": "AGB-TEST",
            "rc": 0xAABBCCDD,
            "rs": 32 * 1024,
            "st": 1,
        },
    }
    report_data = base_report("AGB", header)
    report_data.update(
        {
            "file_name": "test.gba",
            "agb_save_flash_id": (0xBFD4, "SST 39VF512"),
            "eeprom_data": bytearray([1, 2, 3]),
            "vf_addr_reorder": "reverse",
            "vf_value_reorder": "swap",
        },
    )
    report = DumpReport.generate(report_data, ReportDevice("GBFlash"))

    assert "Game Boy Advance" in report
    assert "Save Flash Chip:" in report
    assert "EEPROM area:" in report
    assert "Vast Fame Protection Information" in report
    assert "Database Match" in report


def test_dump_report_rejects_unknown_system() -> None:
    with pytest.raises(NotImplementedError):
        DumpReport.generate(base_report("UNKNOWN", dmg_header()), ReportDevice())


def test_generate_dmg_report_includes_gbmemory_menu_entries() -> None:
    header = dmg_header(cgb=0x00, db=None)
    report_data = base_report("DMG", header)
    report_data["gbmem"] = bytearray(range(0x80))
    report_data["gbmem_parsed"] = [
        {
            "timestamp": "menu-time",
            "kiosk_id": "menu-kiosk",
            "num_games": 2,
            "write_count": 3,
            "cart_id": "CART1234",
        },
        {
            "menu_index": 0,
            "game_code": "GAME1",
            "title": "Menu Game",
            "timestamp": "game-time",
            "kiosk_id": "game-kiosk",
            "rom_offset": 0x20000,
            "rom_size": 0x4000,
            "crc32": 0x11223344,
            "md5": "md5-1",
            "sha1": "sha1-1",
            "sha256": "sha256-1",
            "header": {"logo_correct": True},
            "db_entry": {"rc": 0x11223344, "gn": "Nested", "ne": "JP"},
        },
        {
            "menu_index": 1,
            "game_code": "GAME2",
            "title": "Second Game",
            "timestamp": "game-time-2",
            "kiosk_id": "game-kiosk-2",
            "rom_offset": 0x24000,
            "rom_size": 0x4000,
            "header": {"logo_correct": True},
        },
        {"menu_index": 0xFF, "header": {"logo_correct": True}},
        {"menu_index": 2, "header": {"logo_correct": False}},
    ]

    report = DumpReport.generate(report_data, ReportDevice())

    assert "GB-Memory Data (Multi Menu)" in report
    assert "=== Menu ROM ===" in report
    assert "=== Game 1 ===" in report
    assert "Nested JP" in report


def test_generate_dmg_report_includes_single_gbmemory_entry() -> None:
    report_data = base_report("DMG", dmg_header(db=None))
    report_data["gbmem"] = bytearray(0x80)
    report_data["gbmem_parsed"] = {
        "game_code": "GAME",
        "title": "Single Game",
        "timestamp": "time",
        "kiosk_id": "kiosk",
        "write_count": 1,
        "cart_id": "CART",
    }

    report = DumpReport.generate(report_data, ReportDevice())

    assert "GB-Memory Data (Single Game)" in report
    assert "Single Game" in report
