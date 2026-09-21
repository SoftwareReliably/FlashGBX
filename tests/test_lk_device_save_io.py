"""Tests for save-data read routing and bank selection."""

from __future__ import annotations

import pytest

import FlashGBX.LK_Device as lk_device_module
from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.LK_Device import _SaveBankContext, _SaveReadParameters


class SaveIoMapper:
    """Minimal mapper exposing the bank operations used by save I/O."""

    def __init__(
        self,
        name: str = "MBC3",
        *,
        ram_range: tuple[int, int] = (0, 0x2000),
        flash_range: tuple[int, int] = (0x4000, 0x4000),
        cs_pulse: bool = False,
    ) -> None:
        self.name = name
        self.ram_range = ram_range
        self.flash_range = flash_range
        self.cs_pulse = cs_pulse
        self.selected_ram: list[int] = []
        self.selected_flash: list[int] = []
        self.erase_count = 0

    def GetName(self) -> str:
        return self.name

    def WriteWithCSPulse(self) -> bool:
        return self.cs_pulse

    def SelectBankRAM(self, bank: int) -> tuple[int, int]:
        self.selected_ram.append(bank)
        return self.ram_range

    def SelectBankFlash(self, bank: int) -> tuple[int, int]:
        self.selected_flash.append(bank)
        return self.flash_range

    def EraseFlashSector(self) -> None:
        self.erase_count += 1


def install_read_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    source: bytearray,
) -> list[tuple[str, dict[str, object]]]:
    """Install finite recording read methods returning a fresh source copy."""
    calls: list[tuple[str, dict[str, object]]] = []

    def read_ram(*, address: int, length: int, command: object, max_length: int) -> bytearray:
        calls.append(
            (
                "ReadRAM",
                {"address": address, "length": length, "command": command, "max_length": max_length},
            ),
        )
        return bytearray(source)

    def read_rom(*, address: int, length: int, skip_init: bool, max_length: int) -> bytearray:
        calls.append(
            (
                "ReadROM",
                {"address": address, "length": length, "skip_init": skip_init, "max_length": max_length},
            ),
        )
        return bytearray(source)

    def read_mbc7(*, address: int, length: int) -> bytearray:
        calls.append(("ReadRAM_MBC7", {"address": address, "length": length}))
        return bytearray(source)

    def read_tama5() -> bytearray:
        calls.append(("ReadRAM_TAMA5", {}))
        return bytearray(source)

    monkeypatch.setattr(device, "ReadRAM", read_ram)
    monkeypatch.setattr(device, "ReadROM", read_rom)
    monkeypatch.setattr(device, "ReadRAM_MBC7", read_mbc7)
    monkeypatch.setattr(device, "ReadRAM_TAMA5", read_tama5)
    return calls


@pytest.mark.parametrize(
    (
        "mode",
        "mapper_name",
        "save_type",
        "bank",
        "position",
        "command",
        "source",
        "expected",
        "expected_method",
        "expected_arguments",
    ),
    [
        (
            "DMG",
            "MBC3",
            3,
            0,
            0x120,
            0xC3,
            bytearray(b"RAM!"),
            bytearray(b"RAM!"),
            "ReadRAM",
            {"address": 0x120, "length": 4, "command": 0xC3, "max_length": 128},
        ),
        (
            "DMG",
            "MBC2",
            1,
            0,
            0x40,
            0xC3,
            bytearray([0xF1, 0xA2, 0x73, 0x44]),
            bytearray([0x01, 0x02, 0x03, 0x04]),
            "ReadRAM",
            {"address": 0x40, "length": 4, "command": 0xC3, "max_length": 128},
        ),
        (
            "DMG",
            "MBC7",
            1,
            0,
            0x20,
            None,
            bytearray(b"MBC7"),
            bytearray(b"MBC7"),
            "ReadRAM_MBC7",
            {"address": 0x20, "length": 4},
        ),
        (
            "DMG",
            "MBC6",
            0x104,
            7,
            0x80,
            0xC3,
            bytearray(b"SRAM"),
            bytearray(b"SRAM"),
            "ReadRAM",
            {"address": 0x80, "length": 4, "command": 0xC3, "max_length": 128},
        ),
        (
            "DMG",
            "MBC6",
            0x104,
            8,
            0x4000,
            None,
            bytearray(b"FLSH"),
            bytearray(b"FLSH"),
            "ReadROM",
            {"address": 0x4000, "length": 4, "skip_init": False, "max_length": 128},
        ),
        (
            "DMG",
            "TAMA5",
            0x103,
            0,
            0,
            None,
            bytearray(b"TAMA"),
            bytearray(b"TAMA"),
            "ReadRAM_TAMA5",
            {},
        ),
        (
            "DMG",
            "Xploder GB",
            0x203,
            0,
            0x300,
            None,
            bytearray(b"XPLO"),
            bytearray(b"XPLO"),
            "ReadROM",
            {"address": 0x20300, "length": 4, "skip_init": False, "max_length": 128},
        ),
        (
            "AGB",
            "MBC3",
            1,
            0,
            0x40,
            bytearray([0xC5, 1]),
            bytearray(b"EEPR"),
            bytearray(b"EEPR"),
            "ReadRAM",
            {"address": 8, "length": 4, "command": bytearray([0xC5, 1]), "max_length": 128},
        ),
        (
            "AGB",
            "MBC3",
            6,
            0,
            0x2000,
            False,
            bytearray(b"DACS"),
            bytearray(b"DACS"),
            "ReadROM",
            {"address": 0x1F02000, "length": 4, "skip_init": False, "max_length": 128},
        ),
        (
            "AGB",
            "MBC3",
            3,
            0,
            0x100,
            0xC3,
            bytearray(b"SRAM"),
            bytearray(b"SRAM"),
            "ReadRAM",
            {"address": 0x100, "length": 4, "command": 0xC3, "max_length": 128},
        ),
    ],
    ids=[
        "dmg-sram",
        "mbc2-mask",
        "mbc7",
        "mbc6-sram",
        "mbc6-flash",
        "tama5",
        "xploder",
        "agb-eeprom",
        "dacs",
        "agb-sram",
    ],
)
def test_read_save_chunk_routes_to_the_expected_physical_read(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    mapper_name: str,
    save_type: int,
    bank: int,
    position: int,
    command: object,
    source: bytearray,
    expected: bytearray,
    expected_method: str,
    expected_arguments: dict[str, object],
) -> None:
    device = GbxDevice()
    device.MODE = mode
    mapper = SaveIoMapper(mapper_name)
    calls = install_read_boundaries(device, monkeypatch, source)
    parameters = _SaveReadParameters(
        args={"save_type": save_type},
        mbc=mapper,
        bank=bank,
        pos=position,
        buffer_len=4,
        command=command,
        max_length=128,
    )

    result = device._ReadSaveChunk(parameters)

    assert result == expected
    assert calls == [(expected_method, expected_arguments)]


class SaveBankRecords:
    """Recorded device-side operations during save bank selection."""

    def __init__(self) -> None:
        self.firmware_variables: list[tuple[str, int]] = []
        self.cart_writes: list[tuple[int, int]] = []
        self.flash_writes: list[list[list[int]]] = []
        self.sleeps: list[float] = []


def install_bank_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> SaveBankRecords:
    """Record bank-selection side effects and suppress real delays."""
    records = SaveBankRecords()

    def record_flash_write(commands: list[list[int]]) -> None:
        records.flash_writes.append(commands)

    def record_sleep(seconds: float) -> None:
        records.sleeps.append(seconds)

    monkeypatch.setattr(
        device,
        "_set_fw_variable",
        lambda name, value: records.firmware_variables.append((name, value)),
    )
    monkeypatch.setattr(device, "_cart_write", lambda address, value: records.cart_writes.append((address, value)))
    monkeypatch.setattr(device, "_cart_write_flash", record_flash_write)
    monkeypatch.setattr(lk_device_module.time, "sleep", record_sleep)
    return records


@pytest.mark.parametrize("cs_pulse", [False, True])
def test_prepare_dmg_ram_bank_limits_range_and_configures_cs_pulse(
    monkeypatch: pytest.MonkeyPatch,
    cs_pulse: bool,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = SaveIoMapper(ram_range=(0x200, 0x1000), cs_pulse=cs_pulse)
    records = install_bank_boundaries(device, monkeypatch)
    context = _SaveBankContext(
        args={"mode": 2, "save_type": 3},
        mbc=mapper,
        bank=2,
        save_size=0x900,
        buffer_length=0x100,
        buffer_offset=0,
        agb_flash_chip=0,
    )

    result = device._PrepareSaveBank(context)

    assert result == (0x200, 0x900, 0x100)
    assert mapper.selected_ram == [2]
    assert mapper.selected_flash == []
    assert records.firmware_variables == [("DMG_WRITE_CS_PULSE", int(cs_pulse))]
    assert records.cart_writes == []
    assert records.flash_writes == []
    assert records.sleeps == []


def test_prepare_xploder_bank_selects_rom_window(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = SaveIoMapper("Xploder GB", ram_range=(0, 0x2000))
    records = install_bank_boundaries(device, monkeypatch)
    context = _SaveBankContext(
        args={"mode": 2, "save_type": 0x203},
        mbc=mapper,
        bank=2,
        save_size=0x1800,
        buffer_length=0x100,
        buffer_offset=0,
        agb_flash_chip=0,
    )

    result = device._PrepareSaveBank(context)

    assert result == (0, 0x1800, 0x100)
    assert mapper.selected_ram == [2]
    assert records.firmware_variables == [("DMG_ROM_BANK", 10)]


@pytest.mark.parametrize(
    ("transfer_mode", "buffer_offset", "expected_erases"),
    [
        (2, 0x8000, 0),
        (3, 0x8000, 1),
        (3, 0x9000, 0),
        (3, 0x28000, 1),
    ],
    ids=["backup", "first-boundary", "interior", "next-boundary"],
)
def test_prepare_mbc6_flash_bank_erases_only_restore_sector_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    transfer_mode: int,
    buffer_offset: int,
    expected_erases: int,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = SaveIoMapper("MBC6", flash_range=(0x4000, 0x4000))
    records = install_bank_boundaries(device, monkeypatch)
    context = _SaveBankContext(
        args={"mode": transfer_mode, "save_type": 0x104},
        mbc=mapper,
        bank=8,
        save_size=0x108000,
        buffer_length=0x100,
        buffer_offset=buffer_offset,
        agb_flash_chip=0,
    )

    result = device._PrepareSaveBank(context)

    assert result == (0x4000, 0x8000, 0x2000)
    assert mapper.selected_flash == [0]
    assert mapper.selected_ram == []
    assert mapper.erase_count == expected_erases
    assert records.firmware_variables == [("DMG_ROM_BANK", 0)]
    assert records.sleeps == []


@pytest.mark.parametrize(
    ("save_type", "save_size", "buffer_length", "expected_buffer_length"),
    [
        (3, 0x8000, 0x400, 0x400),
        (6, 0x100000, 0x400, 0x2000),
    ],
    ids=["sram", "dacs"],
)
def test_prepare_single_agb_bank_does_not_issue_bank_switch(
    monkeypatch: pytest.MonkeyPatch,
    save_type: int,
    save_size: int,
    buffer_length: int,
    expected_buffer_length: int,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    mapper = SaveIoMapper()
    records = install_bank_boundaries(device, monkeypatch)
    context = _SaveBankContext(
        args={"mode": 2, "save_type": save_type},
        mbc=mapper,
        bank=0,
        save_size=save_size,
        buffer_length=buffer_length,
        buffer_offset=0,
        agb_flash_chip=0,
    )

    result = device._PrepareSaveBank(context)

    assert result == (0, save_size, expected_buffer_length)
    assert records.cart_writes == []
    assert records.flash_writes == []
    assert records.sleeps == []


def test_prepare_multibank_agb_flash_uses_standard_bank_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    records = install_bank_boundaries(device, monkeypatch)
    context = _SaveBankContext(
        args={"mode": 2, "save_type": 5},
        mbc=SaveIoMapper(),
        bank=1,
        save_size=0x20000,
        buffer_length=0x1000,
        buffer_offset=0x10000,
        agb_flash_chip=0xC209,
    )

    result = device._PrepareSaveBank(context)

    assert result == (0, 0x10000, 0x1000)
    assert records.flash_writes == [
        [[0x5555, 0xAA], [0x2AAA, 0x55], [0x5555, 0xB0], [0, 1]],
    ]
    assert records.cart_writes == []
    assert records.sleeps == [0.05]


@pytest.mark.parametrize(
    ("save_type", "flash_chip"),
    [(8, 0), (5, 0xBF4B)],
    ids=["unlicensed-sram", "bootleg-flash"],
)
def test_prepare_multibank_agb_bootleg_uses_sram_bank_select(
    monkeypatch: pytest.MonkeyPatch,
    save_type: int,
    flash_chip: int,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    records = install_bank_boundaries(device, monkeypatch)
    context = _SaveBankContext(
        args={"mode": 2, "save_type": save_type},
        mbc=SaveIoMapper(),
        bank=1,
        save_size=0x20000,
        buffer_length=0x1000,
        buffer_offset=0x10000,
        agb_flash_chip=flash_chip,
    )

    result = device._PrepareSaveBank(context)

    assert result == (0, 0x10000, 0x1000)
    assert records.cart_writes == [(0x1000000, 1)]
    assert records.flash_writes == []
    assert records.sleeps == [0.05]
