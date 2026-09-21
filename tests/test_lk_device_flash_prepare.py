"""Tests for flash-write input transformation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from FlashGBX.hw_GBxCartRW import GbxDevice

if TYPE_CHECKING:
    from pathlib import Path


FLASH_BLOCK_SIZE = 0x4000
BATTERYLESS_BLOCK_SIZE = 0x2000
AGB_32_MIB = 0x2000000
AGB_EEPROM_RESERVED_SIZE = 0x100


@pytest.mark.parametrize(
    "source",
    [b"ROM", bytearray(b"ROM"), memoryview(b"ROM")],
    ids=["bytes", "bytearray", "memoryview"],
)
def test_prepare_flash_data_accepts_bytes_like_buffers(
    source: bytes | bytearray | memoryview,
) -> None:
    device = GbxDevice()

    result, flash_offset = device._prepare_flash_data({"buffer": source}, "DMG")

    assert result == bytearray(b"ROM" + b"\xff" * (FLASH_BLOCK_SIZE - 3))
    assert flash_offset == 0


def test_prepare_flash_data_reads_a_file(tmp_path: Path) -> None:
    device = GbxDevice()
    source_path = tmp_path / "input.gbc"
    source_path.write_bytes(b"FILE")

    result, flash_offset = device._prepare_flash_data({"path": source_path}, "DMG")

    assert result == bytearray(b"FILE" + b"\xff" * (FLASH_BLOCK_SIZE - 4))
    assert flash_offset == 0


def test_prepare_flash_data_rejects_non_bytes_like_buffer() -> None:
    device = GbxDevice()

    with pytest.raises(TypeError, match="ROM data must be a bytes-like object"):
        device._prepare_flash_data({"buffer": "not ROM bytes"}, "DMG")


def test_prepare_flash_data_keeps_empty_input_empty() -> None:
    device = GbxDevice()

    result, flash_offset = device._prepare_flash_data({"buffer": bytearray()}, "DMG")

    assert result == bytearray()
    assert flash_offset == 0


def test_prepare_flash_data_preserves_flash_offset_and_prefixes_start_address() -> None:
    device = GbxDevice()
    args = {
        "buffer": b"ROM",
        "flash_offset": 0x2400,
        "start_addr": 4,
    }

    result, flash_offset = device._prepare_flash_data(args, "DMG")

    expected_prefix = b"\xff" * 4 + b"ROM"
    assert result == bytearray(expected_prefix + b"\xff" * (FLASH_BLOCK_SIZE - len(expected_prefix)))
    assert flash_offset == 0x2400


@pytest.mark.parametrize(
    ("source_length", "expected_length"),
    [
        (FLASH_BLOCK_SIZE - 1, FLASH_BLOCK_SIZE),
        (FLASH_BLOCK_SIZE, FLASH_BLOCK_SIZE),
        (FLASH_BLOCK_SIZE + 1, FLASH_BLOCK_SIZE * 2),
    ],
    ids=["below-block", "at-block", "above-block"],
)
def test_prepare_flash_data_pads_to_flash_block_boundary(
    source_length: int,
    expected_length: int,
) -> None:
    device = GbxDevice()
    source = bytes([0x5A]) * source_length

    result, _flash_offset = device._prepare_flash_data({"buffer": source}, "DMG")

    assert result[:source_length] == source
    assert result[source_length:] == bytearray([0xFF]) * (expected_length - source_length)
    assert len(result) == expected_length


@pytest.mark.parametrize("layout", [1, 2], ids=["first-half", "second-half"])
@pytest.mark.parametrize("source_blocks", [1, 2], ids=["one-block", "two-blocks"])
def test_prepare_flash_data_places_batteryless_blocks_in_selected_half_bank(
    layout: int,
    source_blocks: int,
) -> None:
    device = GbxDevice()
    source = bytearray()
    for block in range(source_blocks):
        source += bytearray([0x11 + block]) * BATTERYLESS_BLOCK_SIZE
    args = {
        "buffer": source,
        "bl_layout": layout,
        "bl_size": len(source),
        "flash_size": 0x12000,
    }

    result, flash_offset = device._prepare_flash_data(args, "AGB")

    expected = bytearray([0xFF]) * (len(source) * 2)
    half_bank_offset = 0 if layout == 1 else BATTERYLESS_BLOCK_SIZE
    for block in range(source_blocks):
        source_start = block * BATTERYLESS_BLOCK_SIZE
        destination_start = block * FLASH_BLOCK_SIZE + half_bank_offset
        expected[destination_start : destination_start + BATTERYLESS_BLOCK_SIZE] = source[
            source_start : source_start + BATTERYLESS_BLOCK_SIZE
        ]
    assert result == expected
    assert args["bl_size"] == len(source) * 2
    assert args["flash_size"] == 0x24000
    assert flash_offset == 0


def test_prepare_flash_data_repairs_dmg_logo_and_checksums_only() -> None:
    device = GbxDevice()
    source = bytearray((index * 17 + 3) & 0xFF for index in range(FLASH_BLOCK_SIZE))
    replacement_logo = bytearray((index * 5 + 1) & 0xFF for index in range(0x30))
    expected = source.copy()
    expected[0x104:0x134] = replacement_logo
    header_checksum = 0
    for value in expected[0x134:0x14D]:
        header_checksum = (header_checksum - value - 1) & 0xFF
    expected[0x14D] = header_checksum
    expected[0x14E:0x150] = b"\x00\x00"
    expected[0x14E:0x150] = (sum(expected[:0x200]) & 0xFFFF).to_bytes(2, "big")

    result, _flash_offset = device._prepare_flash_data(
        {"buffer": source, "fix_bootlogo": replacement_logo, "fix_header": True},
        "DMG",
    )

    assert result == expected
    assert result[0x104:0x134] == replacement_logo
    assert result[0x134:0x14D] == source[0x134:0x14D]
    assert result[0x150:] == source[0x150:]


def test_prepare_flash_data_repairs_agb_logo_and_checksum_only() -> None:
    device = GbxDevice()
    source = bytearray((index * 11 + 7) & 0xFF for index in range(FLASH_BLOCK_SIZE))
    replacement_logo = bytearray((index * 3 + 2) & 0xFF for index in range(0x9C))
    expected = source.copy()
    expected[0x04:0xA0] = replacement_logo
    expected[0xBD] = (-sum(expected[0xA0:0xBD]) - 0x19) & 0xFF

    result, _flash_offset = device._prepare_flash_data(
        {"buffer": source, "fix_bootlogo": replacement_logo, "fix_header": True},
        "AGB",
    )

    assert result == expected
    assert result[0x04:0xA0] == replacement_logo
    assert result[0xA0:0xBD] == source[0xA0:0xBD]
    assert result[0xBE:] == source[0xBE:]


def make_32_mib_agb_image(signature: bytes) -> bytearray:
    """Build a generated 32 MiB image with a save-library marker."""
    image = bytearray(AGB_32_MIB)
    signature_offset = 0x200
    image[signature_offset : signature_offset + len(signature)] = signature
    return image


def test_prepare_flash_data_trims_reserved_area_for_32_mib_eeprom_image() -> None:
    device = GbxDevice()
    source = make_32_mib_agb_image(b"EEPROM_V124\x00")
    source[-AGB_EEPROM_RESERVED_SIZE - 1] = 0x5A
    source[-AGB_EEPROM_RESERVED_SIZE:] = bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE

    result, _flash_offset = device._prepare_flash_data({"buffer": source}, "AGB")

    assert result == source[:-AGB_EEPROM_RESERVED_SIZE]
    assert len(result) == AGB_32_MIB - AGB_EEPROM_RESERVED_SIZE
    assert result[-1] == 0x5A


def test_prepare_flash_data_keeps_reserved_area_for_non_eeprom_32_mib_image() -> None:
    device = GbxDevice()
    source = make_32_mib_agb_image(b"SRAM_V123\x00")
    source[-AGB_EEPROM_RESERVED_SIZE:] = bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE

    result, _flash_offset = device._prepare_flash_data({"buffer": source}, "AGB")

    assert len(result) == AGB_32_MIB
    assert result[-AGB_EEPROM_RESERVED_SIZE:] == bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE


def test_prepare_flash_data_keeps_reserved_area_for_malformed_eeprom_signature() -> None:
    device = GbxDevice()
    malformed_signature = b"EEPROM_V" + b"X" * (0x20 - len(b"EEPROM_V"))
    source = make_32_mib_agb_image(malformed_signature)
    source[-AGB_EEPROM_RESERVED_SIZE:] = bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE

    result, _flash_offset = device._prepare_flash_data({"buffer": source}, "AGB")

    assert len(result) == AGB_32_MIB
    assert result[-AGB_EEPROM_RESERVED_SIZE:] == bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE
