"""Focused, hardware-free mapper behavior tests."""

from __future__ import annotations

import struct
import time

import pytest

from FlashGBX.Mapper import (
    AGB_GPIO,
    DMG_MBC3,
    ConvertMapperToMapperType,
    ConvertMapperTypeToMapper,
    DMG_Mapper,
    compare_mbc,
    save_size_includes_rtc,
)


class RecordingCartridge:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int, bool]] = []

    def read(self, address: int, length: int = 0) -> int | bytearray:
        del address
        return 0 if length == 0 else bytearray(length)

    def write(self, address: int, value: int, *, sram: bool = False) -> None:
        self.writes.append((address, value, sram))


def test_pokemon_red_header_values_create_mbc3_without_rtc() -> None:
    cartridge = RecordingCartridge()
    mapper = DMG_Mapper().GetInstance(
        args={"mbc": 0x13, "rom_size": 1024 * 1024},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )

    assert isinstance(mapper, DMG_MBC3)
    assert mapper.GetName() == "MBC3"
    assert mapper.GetFullName() == "MBC3+SRAM+BATTERY"
    assert mapper.GetROMBanks(1024 * 1024) == 64
    assert mapper.GetROMSize() == 1024 * 1024
    assert mapper.HasRTC() is False


def test_pokemon_red_mbc3_bank_and_ram_commands_are_preserved() -> None:
    cartridge = RecordingCartridge()
    mapper = DMG_Mapper().GetInstance(
        args={"mbc": 0x13},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )

    assert mapper.SelectBankROM(1) == (0x4000, 0x4000)
    mapper.EnableRAM()
    mapper.EnableRAM(False)

    assert cartridge.writes == [
        (0x2100, 0x01, False),
        (0x0000, 0x0A, False),
        (0x0000, 0x00, False),
    ]


def test_mapper_runtime_state_is_isolated_between_instances() -> None:
    first = DMG_Mapper(args={"mbc": 0x19})
    second = DMG_Mapper(args={"mbc": 0x01})

    first.CURRENT_ROM_BANK = 7
    first.RTC_BUFFER = bytearray(b"rtc")

    assert second.GetROMBank() == 0
    assert second.RTC_BUFFER is None


def test_unconfigured_hardware_callback_has_actionable_error() -> None:
    mapper = DMG_Mapper(args={"mbc": 0x00})

    with pytest.raises(RuntimeError, match="cartridge read callback is not configured"):
        mapper.CartRead(0)


def test_mapper_conversion_helpers_keep_legacy_fallbacks() -> None:
    mapper_type, mapper_ids, mapper_index = ConvertMapperToMapperType(0x13)

    assert (mapper_type, mapper_ids, mapper_index) == (
        "MBC3",
        [0x0F, 0x10, 0x11, 0x12, 0x13],
        3,
    )
    assert ConvertMapperTypeToMapper(mapper_index) == 0x0F
    assert compare_mbc(0x0F, 0x13) is True
    assert compare_mbc(0x01, 0x13) is False
    assert ConvertMapperToMapperType(0xFFFF) == ("None", [0x00, 0x08, 0x09], 0)


def test_save_size_rtc_detection_rejects_invalid_sizes() -> None:
    assert save_size_includes_rtc("DMG", 0x13, 0, 0x03) is False
    assert save_size_includes_rtc("INVALID", 0x13, 0x8000, 0x03) is False


def test_mbc3_rtc_advance_uses_consistent_timezone_state() -> None:
    cartridge = RecordingCartridge()
    mapper = DMG_MBC3(
        args={"mbc": 0x10},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
        clk_toggle_fncptr=lambda _cycles: None,
    )
    rtc_buffer = bytearray(0x30)
    rtc_buffer[-8:] = struct.pack("<Q", int(time.time()) - 125)

    mapper.WriteRTC(rtc_buffer, advance=True)

    rtc_register_values = [value for address, value, is_sram in cartridge.writes if address == 0xA000 and is_sram]
    assert rtc_register_values[2] == 2  # Minutes advanced instead of silently staying at zero.


def test_agb_rtc_advance_defines_and_uses_local_timezone() -> None:
    cartridge = RecordingCartridge()
    gpio = AGB_GPIO(
        args={"rtc": True},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )
    rtc_buffer = bytearray([0x24, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00, 0x40])
    rtc_buffer.extend(struct.pack("<Q", int(time.time()) - 24 * 60 * 60))
    captured: dict[str, int] = {}
    gpio.WriteRTCDict = lambda values: captured.update(values) or True  # type: ignore[method-assign]
    gpio.RTCWriteStatus = lambda _value: None  # type: ignore[method-assign]

    gpio.WriteRTC(rtc_buffer, advance=True)

    assert captured["rtc_y"] == 24
    assert captured["rtc_m"] == 1
    assert captured["rtc_d"] == 2
