# ruff: noqa: N813
"""Extended hardware-free tests for mapper and RTC protocols."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

import pytest

import FlashGBX.Mapper as mapper_module
from FlashGBX.Mapper import (
    AGB_GPIO,
    DMG_GMMC1,
    DMG_TAMA5,
    DMG_HuC3,
    DMG_Unlicensed_256M,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class DMGCartridge:
    """Record DMG bus traffic and return deterministic mock data."""

    def __init__(self, reads: Iterator[int | bytearray] | None = None) -> None:
        """Create a recorder with optional sequential read responses."""
        self.writes: list[tuple[int, int, bool]] = []
        self.reads = reads

    def read(self, _address: int, length: int = 0) -> int | bytearray:
        if self.reads is not None:
            return next(self.reads)
        return 0 if length == 0 else bytearray(length)

    def write(self, address: int, value: int, *, sram: bool = False) -> None:
        self.writes.append((address, value, sram))


class AGBCartridge:
    """Record AGB GPIO traffic with separate scalar and block responses."""

    def __init__(
        self,
        *,
        scalar: int = 0,
        blocks: Iterator[bytearray] | None = None,
    ) -> None:
        """Create a recorder with configurable scalar and block responses."""
        self.scalar = scalar
        self.blocks = blocks
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int]] = []

    def read(self, address: int, length: int = 0) -> int | bytearray:
        self.reads.append((address, length))
        if length:
            if self.blocks is not None:
                return next(self.blocks)
            return bytearray(length)
        return self.scalar

    def write(self, address: int, value: int) -> None:
        self.writes.append((address, value))


def test_gmmc1_command_builders_enable_mapper_and_switch_bank() -> None:
    cartridge = DMGCartridge()
    mapper = DMG_GMMC1(
        args={"mbc": 0x105},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )

    assert mapper.lk_dmg_mmsa_flash_command(0x1234, 0x56) == [
        [0x120, 0x0F],
        [0x125, 0x12],
        [0x126, 0x34],
        [0x127, 0x56],
        [0x13F, 0xA5],
    ]
    assert mapper.lk_dmg_mmsa_access_mapper()[0] == [0x120, 0x09]
    assert mapper.lk_dmg_mmsa_access_rom() == [[0x120, 0x08], [0x13F, 0xA5]]
    assert mapper.lk_dmg_mmsa_access_mbc(enable=True)[0] == [0x120, 0x11]
    assert mapper.lk_dmg_mmsa_access_mbc(enable=False)[0] == [0x120, 0x10]
    assert mapper.lk_dmg_mmsa_disable_flash_write_protect()[0] == [0x120, 0x0A]
    assert mapper.lk_dmg_mmsa_map_full()[0] == [0x120, 0x04]
    assert mapper.lk_dmg_mmsa_map_menu()[0] == [0x120, 0x05]

    assert mapper.EnableMapper() is True
    assert mapper.SelectBankROM(0) == (0, 0x4000)
    assert mapper.SelectBankROM(3) == (0x4000, 0x4000)
    assert mapper.HasHiddenSector() is True
    assert mapper.GetMaxROMSize() == 1024 * 1024
    assert (0x2000, 3, False) in cartridge.writes


def test_gmmc1_hidden_sector_success_and_retry_failure(capsys: pytest.CaptureFixture[str]) -> None:
    visible = bytearray([0x11] * 128)
    hidden = bytearray([0x22] * 128)
    success_cartridge = DMGCartridge(iter([visible, hidden]))
    success = DMG_GMMC1(
        args={"mbc": 0x105},
        cart_write_fncptr=success_cartridge.write,
        cart_read_fncptr=success_cartridge.read,
    )

    assert success.ReadHiddenSector() == hidden

    failure_cartridge = DMGCartridge(iter([visible, visible] * 5))
    failure = DMG_GMMC1(
        args={"mbc": 0x105},
        cart_write_fncptr=failure_cartridge.write,
        cart_read_fncptr=failure_cartridge.read,
    )
    assert failure.ReadHiddenSector() is False
    assert "Failed to read the hidden sector" in capsys.readouterr().out


def test_gmmc1_special_menu_checksum_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    title = {"value": "NP M-MENU MENU"}
    digest = {"value": "wrong"}

    class FakeRomFile:
        def __init__(self, _buffer: bytearray) -> None:
            pass

        def GetHeader(self) -> dict[str, str]:
            return {"game_title": title["value"]}

    class FakeHash:
        def hexdigest(self) -> str:
            return digest["value"]

    monkeypatch.setattr(mapper_module, "RomFileDMG", FakeRomFile)
    monkeypatch.setattr(mapper_module.hashlib, "sha1", lambda _buffer: FakeHash())
    mapper = DMG_GMMC1(args={"mbc": 0x105})
    buffer = bytearray(0x20200)

    assert mapper.CalcChecksum(buffer) == 0
    digest["value"] = "15f5d445c0b2fdf4221cf2a986a4a5cb8dfda131"
    assert mapper.CalcChecksum(buffer) == 1
    buffer[0x20000] = 1
    assert mapper.CalcChecksum(buffer) == 0x19E8

    title["value"] = "DMG MULTI MENU "
    digest["value"] = "b8949fb9c4343b2c04ad59064e9d1dd78a131366"
    assert mapper.CalcChecksum(buffer) == 0xC297

    title["value"] = "OTHER"
    small_buffer = bytearray(range(256)) * 2
    expected = (sum(small_buffer) - small_buffer[0x14E] - small_buffer[0x14F]) & 0xFFFF
    assert mapper.CalcChecksum(small_buffer) == expected


def test_huc3_read_rtc_serializes_registers_and_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    cartridge = DMGCartridge()
    cartridge.read = lambda _address, length=0: 0x0A if length == 0 else bytearray(length)  # type: ignore[method-assign]
    monkeypatch.setattr(mapper_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(mapper_module.time, "time", lambda: 123456)
    mapper = DMG_HuC3(
        args={"mbc": 0xFE},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )

    buffer = mapper.ReadRTC()

    assert buffer[:4] == struct.pack("<I", 0xAAAAAA)
    assert buffer[4:] == struct.pack("<Q", 123456)
    assert buffer is mapper.RTC_BUFFER
    assert mapper.HasRTC() is True
    assert mapper.GetRTCBufferSize() == 0x0C
    assert mapper.GetMaxROMSize() == 2 * 1024 * 1024


def test_huc3_write_and_decode_rtc_data(monkeypatch: pytest.MonkeyPatch) -> None:
    cartridge = DMGCartridge()
    monkeypatch.setattr(mapper_module.time, "sleep", lambda _seconds: None)
    mapper = DMG_HuC3(
        args={"mbc": 0xFE},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )

    assert mapper.WriteRTCDict({"rtc_h": 7, "rtc_m": 45, "rtc_d": 321}) is True
    assert cartridge.writes[0] == (0x0000, 0x0A, False)
    assert (0xA000, 0x31, False) in cartridge.writes
    assert (0xA000, 0x61, False) in cartridge.writes

    packed = (2 * 60 + 34) | (5 << 12)
    mapper.RTC_BUFFER = bytearray(struct.pack("<I", packed) + bytes(8))
    rtc = mapper.GetRTCDict()
    assert rtc.items() >= {"rtc_h": 2, "rtc_m": 34, "rtc_d": 5, "rtc_valid": True}.items()
    assert mapper.GetRTCString() == rtc["string"]


@pytest.mark.parametrize(
    ("buffer", "expected"),
    [
        (bytearray(12), {"rtc_h": 0, "rtc_m": 0, "rtc_d": 0}),
        (
            bytearray(struct.pack("<I", (3 * 60 + 15) | (9 << 12)) + struct.pack("<Q", 1)),
            {},
        ),
    ],
)
def test_huc3_advance_paths_delegate_decoded_values(
    monkeypatch: pytest.MonkeyPatch,
    buffer: bytearray,
    expected: dict[str, int],
) -> None:
    mapper = DMG_HuC3(args={"mbc": 0xFE})
    captured: dict[str, int] = {}
    monkeypatch.setattr(mapper, "WriteRTCDict", lambda values: captured.update(values) or True)
    monkeypatch.setattr(mapper_module.time, "time", lambda: 200000)

    mapper.WriteRTC(buffer, advance=True)

    assert captured.items() >= expected.items()
    assert captured.keys() == {"rtc_h", "rtc_m", "rtc_d"}


def test_tama5_enable_mapper_success_and_failure(capsys: pytest.CaptureFixture[str]) -> None:
    success_cartridge = DMGCartridge(iter([0, 1]))
    success = DMG_TAMA5(
        args={"mbc": 0xFD},
        cart_write_fncptr=success_cartridge.write,
        cart_read_fncptr=success_cartridge.read,
    )
    assert success.EnableMapper() is True
    assert success_cartridge.writes == [(0xA001, 0x0A, True)]

    failure_cartridge = DMGCartridge(iter([0] * 22))
    failure = DMG_TAMA5(
        args={"mbc": 0xFD},
        cart_write_fncptr=failure_cartridge.write,
        cart_read_fncptr=failure_cartridge.read,
    )
    assert failure.EnableMapper() is False
    assert "Couldn’t enable" in capsys.readouterr().out


def test_tama5_read_rtc_and_mapper_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    cartridge = DMGCartridge()
    cartridge.read = lambda _address, _length=0: 0x0A  # type: ignore[method-assign]
    monkeypatch.setattr(mapper_module.time, "time", lambda: 987654)
    mapper = DMG_TAMA5(
        args={"mbc": 0xFD},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )

    buffer = mapper.ReadRTC()

    assert buffer[:32] == bytearray([0xAA] * 32)
    assert buffer[32:] == struct.pack("<Q", 987654)
    assert mapper.SelectBankROM(0x21) == (0x4000, 0x4000)
    assert mapper.HasRTC() is True
    assert mapper.GetRTCBufferSize() == 0x28
    assert mapper.ReadWithCSPulse() is False
    assert mapper.WriteWithCSPulse() is True
    assert mapper.GetMaxROMSize() == 512 * 1024


def test_tama5_write_dict_and_raw_register_paths() -> None:
    cartridge = DMGCartridge()
    cartridge.read = lambda _address, _length=0: 1  # type: ignore[method-assign]
    mapper = DMG_TAMA5(
        args={"mbc": 0xFD},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )
    buffer = bytearray(40)
    values = {
        "rtc_y": 24,
        "rtc_m": 8,
        "rtc_d": 29,
        "rtc_h": 13,
        "rtc_i": 45,
        "rtc_s": 56,
        "rtc_leap_year_state": 2,
        "rtc_buffer": buffer,
    }

    assert mapper.WriteRTCDict(values) is True
    assert buffer[:7] == bytearray([0x56, 0x45, 0x13, 0x96, 0x82, 0x40, 0x02])
    assert buffer[0x0D] == 0x21

    writes_before = len(cartridge.writes)
    mapper.WriteRTC(buffer, advance=False)
    assert len(cartridge.writes) > writes_before
    assert all(is_sram for _address, _value, is_sram in cartridge.writes)


def test_tama5_advance_and_decode_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    mapper = DMG_TAMA5(args={"mbc": 0xFD})
    captured: dict[str, object] = {}
    monkeypatch.setattr(mapper, "WriteRTCDict", lambda values: captured.update(values) or True)

    reset = bytearray(40)
    mapper.WriteRTC(reset, advance=True)
    assert captured.items() >= {"rtc_y": 0, "rtc_m": 1, "rtc_d": 1, "rtc_valid": True}.items()

    rtc_buffer = bytearray(40)
    rtc_buffer[:7] = bytearray([0x56, 0x45, 0x13, 0x96, 0x82, 0x40, 0x02])
    rtc_buffer[0x0D] = 0x01
    mapper.RTC_BUFFER = rtc_buffer
    rtc = mapper.GetRTCDict()
    assert (
        rtc.items()
        >= {
            "rtc_y": 24,
            "rtc_m": 8,
            "rtc_d": 29,
            "rtc_h": 13,
            "rtc_i": 45,
            "rtc_s": 56,
            "rtc_leap_year_state": 0,
            "rtc_valid": True,
        }.items()
    )
    assert "ᴸ" in rtc["string"]
    assert mapper.GetRTCString() == rtc["string"]

    rtc_buffer[0x0D] = 0x11
    assert "ᴸ" not in mapper.GetRTCDict()["string"]


def test_unlicensed_256m_ram_bank_boundaries_are_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    cartridge = DMGCartridge()
    monkeypatch.setattr(mapper_module.time, "sleep", lambda _seconds: None)
    mapper = DMG_Unlicensed_256M(
        args={"mbc": 0x201},
        cart_write_fncptr=cartridge.write,
    )

    assert mapper.SelectBankRAM(16) == (0, 0x2000)
    assert mapper.GetFlashBank() == 1
    assert cartridge.writes[:5] == [
        (0x0000, 0x00, False),
        (0x7000, 0x00, False),
        (0x7001, 0xC0, False),
        (0x7002, 0x01, False),
        (0x0000, 0x0A, False),
    ]
    writes_before = len(cartridge.writes)
    assert mapper.SelectBankRAM(17) == (0, 0x2000)
    assert cartridge.writes[writes_before:] == [(0x4000, 1, False)]
    assert mapper.GetMaxROMSize() == 32 * 1024 * 1024


def test_agb_gpio_bus_conversions_and_bit_protocols(monkeypatch: pytest.MonkeyPatch) -> None:
    cartridge = AGBCartridge(scalar=0x3412)
    sleeps: list[float] = []
    monkeypatch.setattr(mapper_module.time, "sleep", sleeps.append)
    gpio = AGB_GPIO(
        args={"rtc": True},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )

    assert gpio.CartRead(0x20) == 0x1234
    assert cartridge.reads[-1] == (0x40, 0)
    gpio.CartWrite([[1, 2], [3, 4]], delay=0.5)
    assert sleeps == [0.5, 0.5]

    gpio.RTCCommand(0x81)
    gpio.RTCWriteData(0x81)
    assert len(cartridge.writes) == 2 + 32 + 32
    assert (gpio.GPIO_REG_DAT, 7) in cartridge.writes

    cartridge.scalar = 0x0200
    assert gpio.RTCReadData() == 0xFF


@pytest.mark.parametrize("length", [0, 2])
def test_agb_gpio_rejects_missing_or_malformed_read_callbacks(length: int) -> None:
    unconfigured = AGB_GPIO()
    with pytest.raises(RuntimeError, match="cartridge read callback is not configured"):
        unconfigured.CartRead(0, length)

    malformed = AGB_GPIO(cart_read_fncptr=lambda _address, _length=0: None)
    with pytest.raises(RuntimeError, match="Cartridge read failed"):
        malformed.CartRead(0, length)


def test_agb_gpio_status_register_read_and_write_sequences(monkeypatch: pytest.MonkeyPatch) -> None:
    cartridge = AGBCartridge()
    gpio = AGB_GPIO(
        args={"rtc": True},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )
    commands: list[int] = []
    written_data: list[int] = []
    monkeypatch.setattr(gpio, "RTCCommand", commands.append)
    monkeypatch.setattr(gpio, "RTCReadData", lambda: 0x40)
    monkeypatch.setattr(gpio, "RTCWriteData", written_data.append)

    assert gpio.RTCReadStatus() == 0x40
    gpio.RTCWriteStatus(0xC0)

    assert commands == [gpio.RTC_READ_STATUS, gpio.RTC_WRITE_STATUS]
    assert written_data == [0xC0]
    assert cartridge.writes[-1] == (gpio.GPIO_REG_RE, 0)


def test_agb_gpio_has_rtc_offline_and_live_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = AGB_GPIO(args={"rtc": False})
    assert disabled.HasRTC() is False

    same_rom = bytearray(range(6))
    same_cartridge = AGBCartridge(blocks=iter([same_rom]))
    gpio = AGB_GPIO(
        args={"rtc": True},
        cart_write_fncptr=same_cartridge.write,
        cart_read_fncptr=same_cartridge.read,
    )
    assert gpio.HasRTC(bytearray([0x80]) + same_rom) == 1
    assert gpio.HasRTC(bytearray([0x40]) + same_rom) == 3
    assert same_rom == gpio.RTC_BUFFER

    different_cartridge = AGBCartridge(blocks=iter([bytearray(6), bytearray([1] * 6)]))
    live = AGB_GPIO(
        args={"rtc": True},
        cart_write_fncptr=different_cartridge.write,
        cart_read_fncptr=different_cartridge.read,
    )
    monkeypatch.setattr(live, "RTCReadStatus", lambda: 0)
    assert live.HasRTC() is True
    assert different_cartridge.writes == [(live.GPIO_REG_RE, 1), (live.GPIO_REG_RE, 0)]


def test_agb_gpio_read_rtc_from_buffer_and_live_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = AGB_GPIO(args={"rtc": False})
    assert disabled.ReadRTC() is False

    gpio = AGB_GPIO(args={"rtc": True})
    monkeypatch.setattr(gpio, "RTCReadStatus", lambda: 0x40)
    monkeypatch.setattr(mapper_module.time, "time", lambda: 1234)
    offline = gpio.ReadRTC(bytearray([0x24, 0x08, 0x29, 0x06, 0x92, 0x34, 0x56]))
    assert offline == bytearray([0x24, 0x08, 0x29, 0x06, 0x92, 0x34, 0x56, 0x40]) + struct.pack("<Q", 1234)

    cartridge = AGBCartridge()
    live = AGB_GPIO(
        args={"rtc": True},
        cart_write_fncptr=cartridge.write,
        cart_read_fncptr=cartridge.read,
    )
    values = iter([0x24, 0x08, 0x29, 0x06, 0x92, 0x34, 0x56])
    commands: list[int] = []
    monkeypatch.setattr(live, "RTCReadData", lambda: next(values))
    monkeypatch.setattr(live, "RTCReadStatus", lambda: 0x40)
    monkeypatch.setattr(live, "RTCCommand", commands.append)
    result = live.ReadRTC()
    assert isinstance(result, bytearray)
    assert result[:8] == offline[:8]
    assert commands == [live.RTC_READ_DATE]


def test_agb_gpio_write_dict_encodes_bcd_and_pm_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    cartridge = AGBCartridge()
    gpio = AGB_GPIO(args={"rtc": True}, cart_write_fncptr=cartridge.write)
    commands: list[int] = []
    data: list[int] = []
    monkeypatch.setattr(gpio, "RTCCommand", commands.append)
    monkeypatch.setattr(gpio, "RTCWriteData", data.append)

    assert (
        gpio.WriteRTCDict(
            {"rtc_y": 24, "rtc_m": 8, "rtc_d": 29, "rtc_w": 4, "rtc_h": 13, "rtc_i": 45, "rtc_s": 56},
        )
        is True
    )
    assert commands == [gpio.RTC_WRITE_DATE]
    assert data == [0x24, 0x08, 0x29, 0x04, 0x93, 0x45, 0x56]


def test_agb_gpio_write_rtc_reset_and_legacy_status(monkeypatch: pytest.MonkeyPatch) -> None:
    gpio = AGB_GPIO(args={"rtc": True})
    values: list[dict[str, int]] = []
    statuses: list[int] = []
    monkeypatch.setattr(gpio, "WriteRTCDict", lambda rtc: values.append(dict(rtc)) or True)
    monkeypatch.setattr(gpio, "RTCWriteStatus", statuses.append)

    gpio.WriteRTC(bytearray([0xFF] * 16))
    assert values[-1] == {"rtc_y": 0, "rtc_m": 1, "rtc_d": 1, "rtc_w": 0, "rtc_h": 0, "rtc_i": 0, "rtc_s": 0}
    assert statuses[-1] == 0xC0

    legacy = bytearray([0x24, 0x08, 0x29, 0x04, 0x93, 0x45, 0x56, 0x01])
    gpio.WriteRTC(legacy)
    assert values[-1]["rtc_h"] == 13
    assert statuses[-1] == 0x40


def test_agb_gpio_rtc_dict_availability_validity_and_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    gpio = AGB_GPIO(args={"rtc": True})
    assert gpio.GetRTCDict(has_rtc=False)["string"] == "Not available"
    assert gpio.GetRTCDict(has_rtc=2)["string"] == "Not available"
    assert gpio.GetRTCDict(has_rtc=3)["string"] == "Not available"
    assert "Battery dry" in gpio.GetRTCDict(has_rtc=1)["string"]

    gpio.RTC_BUFFER = bytearray(16)
    invalid = gpio.GetRTCDict(has_rtc=True)
    assert invalid["rtc_valid"] is False

    gpio.RTC_BUFFER = bytearray([0x24, 0x08, 0x29, 0x04, 0x93, 0x45, 0x56, 0x40])
    valid = gpio.GetRTCDict(has_rtc=True)
    assert valid.items() >= {"rtc_y": 24, "rtc_m": 8, "rtc_d": 29, "rtc_h": 13, "rtc_valid": True}.items()
    assert gpio.GetRTCString(has_rtc=True) == valid["string"]

    gpio.RTC_BUFFER = None
    monkeypatch.setattr(gpio, "ReadRTC", lambda: False)
    with pytest.raises(RuntimeError, match="Could not read AGB RTC data"):
        gpio.GetRTCDict(has_rtc=True)
