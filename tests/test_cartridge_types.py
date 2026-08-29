"""Tests for cartridge ROM and save-memory type registries."""

from __future__ import annotations

import pytest

from FlashGBX.CartridgeTypes import AgbSaveTypes, DmgSaveTypes, RomSizes


def test_rom_sizes_support_display_lookup_and_cli_conversion() -> None:
    sizes = RomSizes(1024 * 1024)

    assert 32 * 1024 in sizes
    assert 12345 not in sizes
    assert sizes.GetString(localized=False) == "1 MiB"
    assert sizes.GetString(size=512, localized=False) == "512 Bytes"
    assert sizes.GetString(index=0, localized=False) == "32 KiB"
    assert sizes.GetString(index=len(RomSizes.ROM_SIZES), localized=False) == ""
    assert sizes.GetNextLarger(33 * 1024) == 64 * 1024
    assert sizes.GetNextLarger(512 * 1024 * 1024 + 1) is None
    assert sizes.GetSize(4) == 512 * 1024
    assert sizes.GetSize(len(RomSizes.ROM_SIZES)) is None
    assert sizes.GetIndex(64 * 1024) == 1
    assert sizes.GetIndex(12345) is None
    assert len(sizes.GetStringList("DMG")) == len(RomSizes.ROM_SIZES_DMG)
    assert sizes.GetStringList("unknown") == []
    assert sizes.GetNumberOfTypes("DMG") == len(RomSizes.ROM_SIZES_DMG)
    assert RomSizes.GetCLINames("DMG")[:2] == ["auto", "32kb"]
    assert RomSizes.GetCLINames("AGB", include_auto=False)[-1] == "512mb"
    assert RomSizes.GetSizeFromCLIName("64kb", "DMG") == 64 * 1024
    assert RomSizes.GetSizeFromCLIName("auto") is None
    assert RomSizes.GetSizeFromCLIName("not-a-size") is None


@pytest.mark.parametrize(
    ("index", "expected"),
    [(0, "None"), (1, "4K EEPROM (512 Bytes)"), (2, "64K EEPROM (8 KiB)"), (6, "8M DACS (1 MiB)")],
)
def test_agb_save_types_format_entries(index: int, expected: str) -> None:
    saves = AgbSaveTypes()

    assert saves.GetString(index=index, localized=False) == expected
    assert saves.GetName(index) == AgbSaveTypes.SAVE_TYPES[index][1]
    assert saves.GetSize(index) == AgbSaveTypes.SAVE_TYPES[index][0]


def test_agb_save_types_cover_lookups_library_names_and_flash_chips() -> None:
    saves = AgbSaveTypes(index=3)

    assert 32768 in saves
    assert saves.GetIndexFromSize(131072) == 5
    assert saves.GetString(index=-1) == "Unknown"
    assert saves.GetStringFromSaveLib("N/A", localized=False) == "None"
    assert saves.GetStringFromSaveLib("SRAM_F_V", localized=False) == "256K SRAM/FRAM (SRAM_F_V)"
    assert saves.GetStringFromSaveLib("SRAM_V", localized=False) == "256K SRAM (SRAM_V)"
    assert saves.GetStringFromSaveLib("EEPROM_V126", localized=False) == "4K or 64K EEPROM (EEPROM_V126)"
    assert saves.GetStringFromSaveLib("FLASH512_V", localized=False) == "512K FLASH (FLASH512_V)"
    assert saves.GetStringFromSaveLib("FLASH1M_V", localized=False) == "1M FLASH (FLASH1M_V)"
    assert saves.GetStringFromSaveLib("AGB_8MDACS_DL_V", localized=False) == "8M DACS (AGB_8MDACS_DL_V)"
    assert saves.GetStringFromSaveLib("mystery", localized=False) == "Unknown (mystery)"
    assert saves.GetFlashChipName(0xBFD4) == "SST 39VF512"
    assert saves.GetFlashChipSize(0xBFD4) == 0x10000
    assert saves.GetFlashChipName(0x1234) == "Unknown"
    assert saves.GetFlashChipSize(0x1234) == 0
    assert len(saves.GetStringList()) == saves.GetNumberOfTypes()
    assert AgbSaveTypes.GetCLINames()[:2] == ["auto", "eeprom4k"]
    assert AgbSaveTypes.GetCLINames(include_auto=False)[-1] == "batteryless"
    assert AgbSaveTypes.GetIndexFromCLIName("eeprom64k") == 2
    assert AgbSaveTypes.GetIndexFromCLIName("auto") is None
    assert AgbSaveTypes.GetIndexFromCLIName("missing") is None


def test_dmg_save_types_resolve_mbc_size_and_cli_names() -> None:
    saves = DmgSaveTypes(mbc=0x03)

    assert saves.GetName() == "256K SRAM"
    assert saves.GetSize() == 0x8000
    assert saves.GetMbc() == 0x03
    assert saves.GetIndex() == 4
    assert DmgSaveTypes(index=0).GetString(localized=False) == "None"
    assert DmgSaveTypes(index=3).GetString(localized=False) == "64K SRAM (8 KiB)"
    assert DmgSaveTypes(index=6).GetString(localized=False) == "1M SRAM (128 KiB)"
    assert DmgSaveTypes(index=7).GetString(localized=False) == "MBC6 SRAM+FLASH (1.03 MiB)"
    assert DmgSaveTypes(mbc=0xFFFF).GetName() == "Unknown Save Type"
    assert DmgSaveTypes(size=12345).GetSize() == 0
    assert DmgSaveTypes(index=999).GetIndex() is None
    assert 0x03 in DmgSaveTypes()
    assert DmgSaveTypes(mbc=0x03) in DmgSaveTypes()
    assert 0xFFFF not in DmgSaveTypes()
    assert len(DmgSaveTypes().GetStringList()) == DmgSaveTypes().GetNumberOfTypes()
    assert DmgSaveTypes.GetCLINames()[:2] == ["auto", "4k"]
    assert "batteryless" not in DmgSaveTypes.GetCLINames(include_batteryless=False)
    assert DmgSaveTypes.GetMbcFromCLIName("mbc6") == 0x104
    assert DmgSaveTypes.GetMbcFromCLIName("auto") is None
    assert DmgSaveTypes.GetMbcFromCLIName("missing") is None
