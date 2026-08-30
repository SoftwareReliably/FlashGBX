"""Focused, hardware-free mapper behavior tests."""

from __future__ import annotations

import struct
import time

import pytest

import FlashGBX.Mapper as mapper_module  # noqa: N813
from FlashGBX.Mapper import (
    AGB_GPIO,
    BCD,
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


@pytest.mark.parametrize(
    ("mapper_id", "expected_name"),
    [
        (0x00, "None"),
        (0x01, "MBC1"),
        (0x05, "MBC2"),
        (0x0F, "MBC3"),
        (0x19, "MBC5"),
        (0x20, "MBC6"),
        (0x22, "MBC7"),
        (0x101, "MBC1M"),
        (0x0B, "MMM01"),
        (0xFC, "MAC-GBD"),
        (0x105, "G-MMC1"),
        (0x104, "M161"),
        (0xFF, "HuC-1"),
        (0xFE, "HuC-3"),
        (0xFD, "TAMA5"),
        (0x201, "256M Multi Cart"),
        (0x202, "Wisdom Tree"),
        (0x203, "Xploder GB"),
        (0x204, "Sachen"),
        (0x205, "Datel Orbit V2"),
        (0x206, "MBCX"),
        (0xFFFF, "Unknown MBC 65535"),
    ],
)
def test_mapper_factory_resolves_every_protocol_family(
    mapper_id: int,
    expected_name: str,
) -> None:
    mapper = DMG_Mapper().GetInstance(args={"mbc": mapper_id})

    assert mapper.GetName() == expected_name
    assert mapper.GetID() == mapper_id


def test_mapper_registry_and_bcd_helpers_cover_known_and_unknown_values() -> None:
    assert BCD.encode(42) == 0x42
    assert BCD.decode(0x59) == 59
    assert DMG_Mapper.GetMapperName(0x13) == "MBC3+SRAM+BATTERY"
    assert DMG_Mapper.GetMapperName(0xFFFF) == "Unknown"
    assert DMG_Mapper.GetMapperType(0x20) == "MBC6"
    assert DMG_Mapper.GetMapperIdsByType("MBC2") == [0x05, 0x06]
    assert DMG_Mapper.GetMapperIdsByType("missing") == []
    assert DMG_Mapper.IsValidMapperId(0xFD) is True
    assert DMG_Mapper.IsValidMapperId(-1) is False
    assert DMG_Mapper.HasFeature("battery", 0x13) is True
    assert "MBC5" in DMG_Mapper.GetAllMapperTypes()
    assert 0x206 in DMG_Mapper.GetAllMapperIds()


def test_base_mapper_callbacks_banking_and_default_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cartridge = RecordingCartridge()
    toggles: list[int] = []
    power_cycles: list[bool] = []
    sleeps: list[float] = []
    monkeypatch.setattr(mapper_module.time, "sleep", sleeps.append)
    mapper = DMG_Mapper(
        args={"mbc": 0, "rom_banks": 4},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
        cart_powercycle_fncptr=lambda: power_cycles.append(True),
        clk_toggle_fncptr=toggles.append,
    )

    assert mapper.CartRead(0x1234) == 0
    assert mapper.CartRead(0x1234, 3) == bytearray(3)
    mapper.CartWrite([[1, 2], [3, 4]], delay=0.01, sram=True)
    mapper._toggle_clock(8)
    mapper._power_cycle()
    assert mapper.SelectBankROM(0) == (0, 0x4000)
    assert mapper.SelectBankROM(2) == (0x4000, 0x4000)
    assert mapper.SelectBankRAM(3) == (0, 0x2000)
    mapper.SetStartBank(7)

    assert mapper.GetROMBankSize() == 0x4000
    assert mapper.GetRAMBankSize() == 0x2000
    assert mapper.GetROMBanks(0x9000) == 3
    assert mapper.GetRAMBanks(0x3000) == 2
    assert mapper.GetROMSize() == 0x10000
    assert mapper.GetMaxROMSize() == 0x8000
    assert mapper.GetFlashBank() == -1
    assert mapper.START_BANK == 7
    assert toggles == [8]
    assert power_cycles == [True]
    assert sleeps == [0.01, 0.01]
    assert cartridge.writes[:2] == [(1, 2, True), (3, 4, True)]
    assert mapper.HasFlashBanks() is False
    assert mapper.HasHiddenSector() is False
    assert mapper.ReadHiddenSector() is False
    assert mapper.GetRTCBufferSize() == 0
    assert mapper.LatchRTC() == 0
    assert mapper.ReadRTC() is False
    assert mapper.GetRTCDict() == {}
    assert mapper.ResetBeforeBankChange(0) is False
    assert mapper.ReadWithCSPulse() is False
    assert mapper.WriteWithCSPulse() is False
    assert mapper.SelectBankFlash(0) is None


def test_base_mapper_checksum_and_unsupported_flash_operations() -> None:
    data = bytearray(range(256)) * 2
    mapper = DMG_Mapper(args={"mbc": 0xFFFF})

    expected = (sum(data) - data[0x14E] - data[0x14F]) & 0xFFFF
    assert mapper.CalcChecksum(data) == expected
    assert mapper.GetFullName() == "Unknown MBC 65535"
    assert mapper.EnableMapper() is True
    assert mapper.GetRTCString()
    assert mapper.WriteRTC(bytearray()) is None
    assert mapper.WriteRTCDict({}) is None
    with pytest.raises(NotImplementedError, match="flash-memory access"):
        mapper.EnableFlash()
    with pytest.raises(NotImplementedError, match="flash-memory access"):
        mapper.EraseFlashSector()


@pytest.mark.parametrize("length", [0, 2])
def test_mapper_rejects_callback_results_of_the_wrong_shape(length: int) -> None:
    mapper = DMG_Mapper(
        args={"mbc": 0},
        cart_read_fncptr=lambda _address, _length=0: None,
    )

    with pytest.raises(RuntimeError, match="Cartridge read failed"):
        mapper.CartRead(0x4000, length)


@pytest.mark.parametrize(
    ("mapper_id", "bank", "expected_result", "last_write"),
    [
        (0x01, 33, (0x4000, 0x4000), (0x4000, 1, False)),
        (0x05, 2, (0x4000, 0x4000), (0x2100, 2, False)),
        (0x19, 0x101, (0x4000, 0x4000), (0x2100, 1, False)),
        (0x20, 2, (0x4000, 0x2000), (0x3000, 2, False)),
        (0x22, 2, (0x4000, 0x4000), (0x2100, 2, False)),
        (0x101, 16, (0, 0x4000), (0x4000, 1, False)),
        (0x0B, 1, (0x4000, 0x4000), (0x2100, 1, False)),
        (0xFC, 1, (0x4000, 0x4000), (0x2000, 1, False)),
        (0x104, 1, (0, 0x8000), (0x4000, 1, False)),
        (0xFF, 1, (0x4000, 0x4000), (0x2100, 1, False)),
        (0x202, 1, (0, 0x8000), (1, 0, False)),
        (0x204, 1, (0x4000, 0x4000), (0x2000, 1, False)),
        (0x205, 1, (0x4000, 0x2000), (0x7FE1, 1, False)),
        (0x206, 1, (0x4000, 0x4000), (0x2100, 1, False)),
    ],
)
def test_mapper_bank_switches_emit_expected_protocol_commands(
    monkeypatch: pytest.MonkeyPatch,
    mapper_id: int,
    bank: int,
    expected_result: tuple[int, int],
    last_write: tuple[int, int, bool],
) -> None:
    cartridge = RecordingCartridge()
    monkeypatch.setattr(mapper_module.time, "sleep", lambda _seconds: None)
    mapper = DMG_Mapper().GetInstance(
        args={"mbc": mapper_id},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )

    assert mapper.SelectBankROM(bank) == expected_result
    assert cartridge.writes[-1] == last_write


def test_mbc1_and_mbc7_ram_enable_protocols_cover_both_states() -> None:
    cartridge = RecordingCartridge()
    mbc1 = DMG_Mapper().GetInstance(args={"mbc": 0x01}, cart_write_fncptr=cartridge.write)
    mbc7 = DMG_Mapper().GetInstance(args={"mbc": 0x22}, cart_write_fncptr=cartridge.write)

    mbc1.EnableRAM(True)
    mbc1.EnableRAM(False)
    mbc7.EnableRAM(True)
    mbc7.EnableRAM(False)

    assert (0x6000, 1, False) in cartridge.writes
    assert (0x6000, 0, False) in cartridge.writes
    assert cartridge.writes[-2:] == [(0x0000, 0, False), (0x4000, 0x40, False)]
    assert mbc7.SelectBankRAM(7) == (0, 0x200)


def test_mbc6_flash_access_and_status_polling_are_fully_mocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cartridge = RecordingCartridge()
    cartridge.read = lambda _address, length=0: 0x80 if length == 0 else bytearray(range(length))  # type: ignore[method-assign]
    monkeypatch.setattr(mapper_module.time, "sleep", lambda _seconds: None)
    mapper = DMG_Mapper().GetInstance(
        args={"mbc": 0x20},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )

    assert mapper.HasFlashBanks() is True
    assert mapper.SelectBankFlash(3) == (0x4000, 0x2000)
    assert mapper.GetRAMBanks(0x108000) == 136
    assert mapper.SelectBankRAM(2) == (0, 0x1000)
    mapper.EnableFlash(enable=True, enable_write=True)
    mapper.EnableFlash(enable=False)
    assert mapper.GetFlashID() == bytearray(range(8))
    mapper.EraseFlashSector()
    assert mapper.GetMaxROMSize() == 1024 * 1024


def test_mbc3_rtc_detection_read_and_string_paths_use_mock_callbacks() -> None:
    cartridge = RecordingCartridge()

    def read_rtc(_address: int, length: int = 0) -> int | bytearray:
        return 1 if length == 0 else bytearray([1] * length)

    clock_toggles: list[int] = []
    mapper = DMG_MBC3(
        args={"mbc": 0x10},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=read_rtc,
        clk_toggle_fncptr=clock_toggles.append,
    )

    assert mapper.HasRTC() is True
    rtc_buffer = mapper.ReadRTC()
    assert isinstance(rtc_buffer, bytearray)
    assert len(rtc_buffer) == mapper.GetRTCBufferSize()
    rtc_buffer[0x00] = 3
    rtc_buffer[0x04] = 2
    rtc_buffer[0x08] = 1
    rtc_buffer[0x0C] = 4
    rtc_buffer[0x10] = 0
    mapper.RTC_BUFFER = rtc_buffer
    rtc = mapper.GetRTCDict()
    assert rtc.items() >= {"rtc_d": 4, "rtc_h": 1, "rtc_m": 2, "rtc_s": 3, "rtc_valid": True}.items()
    assert mapper.GetRTCString() == rtc["string"]
    assert clock_toggles


def test_unlicensed_mapper_power_cycle_and_unlock_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cartridge = RecordingCartridge()
    power_cycles: list[str] = []
    monkeypatch.setattr(mapper_module.time, "sleep", lambda _seconds: None)

    multicart = DMG_Mapper().GetInstance(
        args={"mbc": 0x201},
        cart_write_fncptr=cartridge.write,
        cart_powercycle_fncptr=lambda: power_cycles.append("multicart"),
    )
    assert multicart.SelectBankROM(512) == (0, 0x4000)
    assert multicart.GetFlashBank() == 1

    xploder = DMG_Mapper().GetInstance(
        args={"mbc": 0x203},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
        cart_powercycle_fncptr=lambda: power_cycles.append("xploder"),
    )
    assert xploder.SelectBankRAM(0) == (0x4000, 0x4000)

    datel = DMG_Mapper().GetInstance(
        args={"mbc": 0x205},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )
    assert datel.SelectBankROM(0) == (0x4000, 0x2000)
    assert power_cycles == ["multicart", "xploder"]
