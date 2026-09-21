"""Tests for save-data read routing and bank selection."""

from __future__ import annotations

import hashlib
import zlib
from typing import TYPE_CHECKING

import pytest

import FlashGBX.LK_Device as lk_device_module
from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.LK_Device import _SaveBankContext, _SaveReadParameters, _SaveWriteParameters

if TYPE_CHECKING:
    from pathlib import Path


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


class SaveWriteRecords:
    """Recorded low-level operations for save writes."""

    def __init__(self, statuses: list[object] | None = None) -> None:
        self.events: list[str] = []
        self.ram_writes: list[dict[str, object]] = []
        self.rom_writes: list[dict[str, object]] = []
        self.mbc6_writes: list[dict[str, object]] = []
        self.mbc7_writes: list[dict[str, object]] = []
        self.tama5_writes: list[bytearray] = []
        self.xploder_writes: list[dict[str, object]] = []
        self.cart_writes: list[tuple[int, int]] = []
        self.flash_commands: list[tuple[list[list[int]], bool]] = []
        self.cart_reads: list[tuple[int, int, bool]] = []
        self.statuses = list(statuses or [])
        self.sleeps: list[float] = []
        self.progress: list[dict[str, object]] = []


def install_write_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    *,
    statuses: list[object] | None = None,
) -> SaveWriteRecords:
    """Replace physical writes and status reads with finite recorders."""
    records = SaveWriteRecords(statuses)

    def write_ram(
        *,
        address: int,
        buffer: bytes | bytearray | memoryview,
        command: object,
        max_length: int | None = None,
    ) -> None:
        records.events.append("WriteRAM")
        records.ram_writes.append(
            {
                "address": address,
                "buffer": bytearray(buffer),
                "command": command,
                "max_length": max_length,
            },
        )

    def write_rom(*, address: int, buffer: bytes | bytearray | memoryview) -> None:
        records.events.append("WriteROM")
        records.rom_writes.append({"address": address, "buffer": bytearray(buffer)})

    def write_mbc6(
        *,
        address: int,
        buffer: bytes | bytearray | memoryview,
        mapper: SaveIoMapper,
    ) -> None:
        records.events.append("WriteFlash_MBC6")
        records.mbc6_writes.append(
            {"address": address, "buffer": bytearray(buffer), "mapper": mapper.GetName()},
        )

    def write_mbc7(*, address: int, buffer: bytes | bytearray | memoryview) -> None:
        records.events.append("WriteEEPROM_MBC7")
        records.mbc7_writes.append({"address": address, "buffer": bytearray(buffer)})

    def write_tama5(*, buffer: bytes | bytearray | memoryview) -> None:
        records.events.append("WriteRAM_TAMA5")
        records.tama5_writes.append(bytearray(buffer))

    def write_xploder(
        *,
        address: int,
        buffer: bytes | bytearray | memoryview,
        bank: int,
    ) -> None:
        records.events.append("WriteROM_DMG_EEPROM")
        records.xploder_writes.append(
            {"address": address, "buffer": bytearray(buffer), "bank": bank},
        )

    def cart_write(address: int, value: int) -> None:
        records.events.append("cart_write")
        records.cart_writes.append((address, value))

    def cart_write_flash(commands: list[list[int]], flashcart: bool = False) -> None:
        records.events.append("cart_write_flash")
        records.flash_commands.append((commands, flashcart))

    def cart_read(address: int, length: int, agb_save_flash: bool = False) -> object:
        records.events.append("cart_read")
        records.cart_reads.append((address, length, agb_save_flash))
        assert records.statuses
        return records.statuses.pop(0)

    def record_sleep(seconds: float) -> None:
        records.events.append("sleep")
        records.sleeps.append(seconds)

    def record_progress(event: dict[str, object]) -> None:
        records.progress.append(dict(event))

    monkeypatch.setattr(device, "WriteRAM", write_ram)
    monkeypatch.setattr(device, "WriteROM", write_rom)
    monkeypatch.setattr(device, "WriteFlash_MBC6", write_mbc6)
    monkeypatch.setattr(device, "WriteEEPROM_MBC7", write_mbc7)
    monkeypatch.setattr(device, "WriteRAM_TAMA5", write_tama5)
    monkeypatch.setattr(device, "WriteROM_DMG_EEPROM", write_xploder)
    monkeypatch.setattr(device, "_cart_write", cart_write)
    monkeypatch.setattr(device, "_cart_write_flash", cart_write_flash)
    monkeypatch.setattr(device, "_cart_read", cart_read)
    monkeypatch.setattr(device, "SetProgress", record_progress)
    monkeypatch.setattr(lk_device_module.time, "sleep", record_sleep)
    return records


@pytest.mark.parametrize(
    ("mode", "mapper_name", "save_type", "bank", "position", "firmware", "flash_chip", "expected"),
    [
        (
            "DMG",
            "MBC3",
            3,
            0,
            0x120,
            12,
            0,
            ("ram_writes", {"address": 0x120, "command": 0xC3, "max_length": None}),
        ),
        (
            "AGB",
            "MBC3",
            1,
            0,
            0x40,
            12,
            0,
            ("ram_writes", {"address": 8, "command": 0xC3, "max_length": None}),
        ),
        (
            "AGB",
            "MBC3",
            4,
            0,
            0x400,
            12,
            0x1F3D,
            ("ram_writes", {"address": 8, "command": 0xC3, "max_length": None}),
        ),
        (
            "DMG",
            "MBC7",
            1,
            0,
            0x120,
            12,
            0,
            ("mbc7_writes", {"address": 0x120}),
        ),
        (
            "DMG",
            "MBC6",
            0x104,
            8,
            0x4000,
            1,
            0,
            ("mbc6_writes", {"address": 0x4000, "mapper": "MBC6"}),
        ),
        (
            "DMG",
            "MBC6",
            0x104,
            8,
            0x4000,
            2,
            0,
            ("rom_writes", {"address": 0x4000}),
        ),
        ("DMG", "TAMA5", 0x103, 0, 0, 12, 0, ("tama5_writes", {})),
        (
            "DMG",
            "Xploder GB",
            0x203,
            2,
            0x300,
            12,
            0,
            ("xploder_writes", {"address": 0x300, "bank": 10}),
        ),
    ],
    ids=["sram", "eeprom", "atmel", "mbc7", "mbc6-old-firmware", "mbc6-new-firmware", "tama5", "xploder"],
)
def test_write_save_chunk_routes_nonzero_buffer_slice(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    mapper_name: str,
    save_type: int,
    bank: int,
    position: int,
    firmware: int,
    flash_chip: int,
    expected: tuple[str, dict[str, object]],
) -> None:
    device = GbxDevice()
    device.MODE = mode
    device.FW = {"fw_ver": firmware}
    mapper = SaveIoMapper(mapper_name)
    records = install_write_boundaries(device, monkeypatch)
    buffer = bytearray(b"__CHUNK__")
    parameters = _SaveWriteParameters(
        args={"save_type": save_type},
        mbc=mapper,
        bank=bank,
        pos=position,
        buffer=buffer,
        buffer_offset=2,
        buffer_len=5,
        command=0xC3,
        agb_flash_chip=flash_chip,
    )

    assert device._WriteSaveChunk(parameters) is True

    record_name, expected_fields = expected
    write_records = getattr(records, record_name)
    assert len(write_records) == 1
    if record_name == "tama5_writes":
        assert write_records == [bytearray(b"CHUNK")]
    else:
        assert write_records[0]["buffer"] == bytearray(b"CHUNK")
        assert {key: write_records[0][key] for key in expected_fields} == expected_fields
    if mapper_name == "MBC6" and firmware == 2:
        assert records.cart_writes == [(0x4004, 0xF0)]
        assert records.events == ["WriteROM", "cart_write"]
    else:
        assert records.cart_writes == []


FLASH_ERASE_COMMANDS = [
    [0x5555, 0xAA],
    [0x2AAA, 0x55],
    [0x5555, 0x80],
    [0x5555, 0xAA],
    [0x2AAA, 0x55],
    [0x240, 0x30],
]


def write_agb_flash_chunk(
    device: GbxDevice,
    mapper: SaveIoMapper,
    *,
    buffer: bytearray,
    buffer_offset: int,
    buffer_len: int,
    position: int = 0x10240,
) -> bool:
    """Call the real AGB FLASH write path with explicit chunk bounds."""
    return device._WriteSaveChunk(
        _SaveWriteParameters(
            args={"save_type": 5},
            mbc=mapper,
            bank=1,
            pos=position,
            buffer=buffer,
            buffer_offset=buffer_offset,
            buffer_len=buffer_len,
            command=0xC4,
            agb_flash_chip=0xC209,
        ),
    )


@pytest.mark.parametrize(
    ("statuses", "expected_reads"),
    [
        ([0xFFFF], 1),
        ([0, bytearray(b"\xff\xff")], 2),
        ([object(), 0xFFFF], 2),
    ],
    ids=["immediately-ready", "busy-then-ready", "malformed-then-ready"],
)
def test_agb_flash_waits_for_erase_before_programming(
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[object],
    expected_reads: int,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    records = install_write_boundaries(device, monkeypatch, statuses=statuses)
    buffer = bytearray(b"__FLASH__")

    assert (
        write_agb_flash_chunk(
            device,
            SaveIoMapper(),
            buffer=buffer,
            buffer_offset=2,
            buffer_len=5,
        )
        is True
    )

    assert records.flash_commands == [(FLASH_ERASE_COMMANDS, False)]
    assert records.ram_writes == [
        {"address": 0x10240, "buffer": bytearray(b"FLASH"), "command": 0xC4, "max_length": None},
    ]
    assert len(records.cart_reads) == expected_reads
    assert records.cart_reads == [(0x240, 2, True)] * expected_reads
    assert records.sleeps == [0.01] * expected_reads
    assert records.events.index("cart_write_flash") < records.events.index("WriteRAM")


def test_agb_flash_all_ff_chunk_erases_without_programming(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    records = install_write_boundaries(device, monkeypatch, statuses=[0xFFFF])

    assert (
        write_agb_flash_chunk(
            device,
            SaveIoMapper(),
            buffer=bytearray(b"__\xff\xff\xff\xff__"),
            buffer_offset=2,
            buffer_len=4,
        )
        is True
    )

    assert records.flash_commands == [(FLASH_ERASE_COMMANDS, False)]
    assert records.ram_writes == []
    assert records.events == ["cart_write_flash", "sleep", "cart_read"]


def test_agb_flash_erase_exhaustion_stops_before_programming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    records = install_write_boundaries(device, monkeypatch, statuses=[0] * 50)

    assert (
        write_agb_flash_chunk(
            device,
            SaveIoMapper(),
            buffer=bytearray(b"__FLASH__"),
            buffer_offset=2,
            buffer_len=5,
        )
        is False
    )

    assert records.flash_commands == [(FLASH_ERASE_COMMANDS, False)]
    assert len(records.cart_reads) == 50
    assert records.sleeps == [0.01] * 50
    assert records.ram_writes == []


def test_ereader_final_flash_sector_preserves_protected_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.INFO["ereader"] = True
    records = install_write_boundaries(device, monkeypatch, statuses=[0xFFFF])
    payload = bytearray(index % 251 for index in range(0x1000))
    buffer = bytearray(b"HEAD") + payload

    assert (
        write_agb_flash_chunk(
            device,
            SaveIoMapper(),
            buffer=buffer,
            buffer_offset=4,
            buffer_len=0x1000,
            position=0x1F000,
        )
        is True
    )

    assert records.flash_commands[0][0][-1] == [0xF000, 0x30]
    assert records.ram_writes == [
        {
            "address": 0x1F000,
            "buffer": payload[:0xF80],
            "command": 0xC4,
            "max_length": 0x80,
        },
    ]
    assert payload[0xF80:] not in [write["buffer"] for write in records.ram_writes]


@pytest.mark.parametrize("write_result", [False, True], ids=["failure", "success"])
def test_dacs_write_result_propagates_from_write_save_chunk(
    monkeypatch: pytest.MonkeyPatch,
    write_result: bool,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    calls: list[tuple[int, int, bytearray]] = []

    def record_dacs_write(sector_address: int, position: int, data: bytearray) -> bool:
        calls.append((sector_address, position, bytearray(data)))
        return write_result

    monkeypatch.setattr(device, "_WriteDACSSaveChunk", record_dacs_write)
    parameters = _SaveWriteParameters(
        args={"save_type": 6},
        mbc=SaveIoMapper(),
        bank=0,
        pos=0x2000,
        buffer=bytearray(b"__DACS__"),
        buffer_offset=2,
        buffer_len=4,
        command=False,
        agb_flash_chip=0,
    )

    assert device._WriteSaveChunk(parameters) is write_result
    assert calls == [(0x1F02000, 0x2000, bytearray(b"DACS"))]


def test_dacs_protocol_waits_for_each_command_then_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    records = install_write_boundaries(
        device,
        monkeypatch,
        statuses=[bytearray(b"\x80\x00"), bytearray(b"\x80\x00")],
    )

    assert device._WriteDACSSaveChunk(0x1F00000, 0, bytearray(b"DACS")) is True

    assert records.flash_commands == [
        ([[0, 0x50], [0, 0x60], [0, 0xD0]], True),
        ([[0, 0x50], [0x1F00000, 0x20], [0x1F00000, 0xD0]], True),
    ]
    assert records.cart_reads == [(0x1F00000, 2, False)] * 2
    assert records.sleeps == [0.1, 0.1]
    assert records.rom_writes == [{"address": 0x1F00000, "buffer": bytearray(b"DACS")}]
    assert records.progress == []


def test_dacs_protocol_exhaustion_aborts_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    records = install_write_boundaries(
        device,
        monkeypatch,
        statuses=[bytearray(b"\x00\x00")] * 20,
    )

    assert device._WriteDACSSaveChunk(0x1F00000, 0, bytearray(b"DACS")) is False

    assert records.flash_commands == [
        ([[0, 0x50], [0, 0x60], [0, 0xD0]], True),
    ]
    assert len(records.cart_reads) == 20
    assert records.sleeps == [0.1] * 20
    assert records.rom_writes == []
    assert len(records.progress) == 1
    assert records.progress[0]["action"] == "ABORT"
    assert records.progress[0]["info_type"] == "msgbox_critical"
    assert records.progress[0]["abortable"] is False


def test_save_worker_dacs_write_failure_prevents_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.FW = {"pcb_name": "Test", "fw_ver": 12}
    device.INFO["action"] = "RESTORE_RAM"
    progress: list[dict[str, object]] = []
    dacs_calls: list[tuple[int, int, bytearray]] = []

    monkeypatch.setattr(device, "_require_cartridge_mode", lambda _operation: "AGB")
    monkeypatch.setattr(device, "_prepare_save_cart_type", lambda _args, _mode: {})
    monkeypatch.setattr(device, "_set_fw_variable", lambda _name, _value: None)
    monkeypatch.setattr(
        device,
        "_configure_agb_save_transfer",
        lambda _args, _cart_type: (4, 4, 1, 0, 0, False, 0xFF, 0),
    )
    monkeypatch.setattr(
        device,
        "_PrepareSaveTransferAction",
        lambda _args, _parameters: (bytearray(b"FAIL"), 1, 4),
    )
    monkeypatch.setattr(device, "_PrepareSaveBank", lambda _context: (0, 4, 4))
    monkeypatch.setattr(device, "_AbortSaveTransferIfCanceled", lambda: False)
    monkeypatch.setattr(device, "SetProgress", lambda event: progress.append(dict(event)))

    def fail_dacs_write(sector_address: int, position: int, data: bytearray) -> bool:
        dacs_calls.append((sector_address, position, bytearray(data)))
        return False

    monkeypatch.setattr(device, "_WriteDACSSaveChunk", fail_dacs_write)
    args = {"mode": 3, "save_type": 6, "path": "failed.sav"}

    assert device._BackupRestoreRAM_Worker(args) is False

    assert dacs_calls == [(0x1F00000, 0, bytearray(b"FAIL"))]
    assert all(event.get("action") != "FINISHED" for event in progress)
    assert device.INFO["last_action"] == "RESTORE_RAM"
    assert device.INFO["action"] is None


class CompletionMapper:
    """Minimal mapper for save completion and hardware-reset behavior."""

    def __init__(
        self,
        name: str = "MBC3",
        *,
        has_rtc: bool = False,
        rtc_read: bytearray | bool = False,
        rtc_size: int = 4,
        events: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.name = name
        self.has_rtc = has_rtc
        self.rtc_read = rtc_read
        self.rtc_size = rtc_size
        self.events = events if events is not None else []

    def GetName(self) -> str:
        return self.name

    def HasRTC(self) -> bool:
        self.events.append(("has_rtc",))
        return self.has_rtc

    def LatchRTC(self) -> None:
        self.events.append(("latch_rtc",))

    def ReadRTC(self) -> bytearray | bool:
        self.events.append(("read_rtc",))
        return self.rtc_read

    def GetRTCBufferSize(self) -> int:
        return self.rtc_size

    def WriteRTC(self, buffer: bytearray, advance: bool = False) -> None:
        self.events.append(("write_rtc", bytearray(buffer), advance))

    def SelectBankRAM(self, bank: int) -> None:
        self.events.append(("select_ram", bank))

    def EnableRAM(self, enable: bool = True) -> None:
        self.events.append(("enable_ram", enable))

    def SelectBankROM(self, bank: int) -> None:
        self.events.append(("select_rom", bank))


@pytest.mark.parametrize(
    ("mapper_name", "source", "expected"),
    [
        ("MBC3", bytearray(b"SAVE"), bytearray(b"SAVE")),
        ("MBC2", bytearray([0xF1, 0xA2, 0x73, 0x44]), bytearray([1, 2, 3, 4])),
    ],
    ids=["ordinary", "mbc2-nibbles"],
)
def test_finish_save_backup_writes_final_disk_bytes_and_hashes(
    tmp_path: Path,
    mapper_name: str,
    source: bytearray,
    expected: bytearray,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = CompletionMapper(mapper_name)
    destination = tmp_path / f"{mapper_name}.sav"
    args = {"rtc": False, "save_type": 1, "path": destination}

    result = device._finish_save_backup(args, mapper, None, source, 0)

    assert result == (False, True)
    assert destination.read_bytes() == expected
    assert source == expected
    assert device.INFO["transferred"] == len(expected)
    assert device.INFO["file_crc32"] == zlib.crc32(expected) & 0xFFFFFFFF
    assert device.INFO["file_sha1"] == hashlib.sha1(expected).hexdigest()
    assert "data" not in device.INFO


def test_finish_save_backup_memory_restores_agb_bank_byte_and_hashes() -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    buffer = bytearray(b"BANK-X-DATA")
    expected = bytearray(buffer)
    expected[5] = 0xA5
    args = {"rtc": False, "save_type": 3, "path": None}

    result = device._finish_save_backup(
        args,
        CompletionMapper(),
        {"flash_bank_select_type": 1},
        buffer,
        0xA5,
    )

    assert result == (False, True)
    assert buffer == expected
    assert device.INFO["data"] is buffer
    assert device.INFO["file_crc32"] == zlib.crc32(expected) & 0xFFFFFFFF
    assert device.INFO["file_sha1"] == hashlib.sha1(expected).hexdigest()


@pytest.mark.parametrize(
    ("has_rtc", "rtc_read", "expected_rtc", "expected_events"),
    [
        (False, False, bytearray(), [("has_rtc",)]),
        (
            True,
            bytearray(b"RTC-DATA"),
            bytearray(b"RTC-DATA"),
            [("has_rtc",), ("latch_rtc",), ("read_rtc",)],
        ),
        (True, False, bytearray(), [("has_rtc",), ("latch_rtc",), ("read_rtc",)]),
    ],
    ids=["absent", "valid", "failed"],
)
def test_finish_dmg_backup_appends_only_valid_rtc_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    has_rtc: bool,
    rtc_read: bytearray | bool,
    expected_rtc: bytearray,
    expected_events: list[tuple[object, ...]],
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = CompletionMapper(has_rtc=has_rtc, rtc_read=rtc_read)
    progress: list[dict[str, object]] = []
    monkeypatch.setattr(device, "SetProgress", lambda event: progress.append(dict(event)))
    destination = tmp_path / "rtc.sav"
    buffer = bytearray(b"SAVE")
    args = {"rtc": True, "save_type": 3, "path": destination}

    assert device._finish_save_backup(args, mapper, None, buffer, 0) == (False, True)

    assert destination.read_bytes() == buffer + expected_rtc
    assert mapper.events == expected_events
    assert progress == [{"action": "UPDATE_POS", "pos": len(buffer) + len(expected_rtc)}]
    assert device.NO_PROG_UPDATE is False
    assert device.INFO["file_crc32"] == zlib.crc32(buffer) & 0xFFFFFFFF
    assert device.INFO["file_sha1"] == hashlib.sha1(buffer).hexdigest()


def test_finish_agb_backup_uses_firmware_rtc_bytes_and_frozen_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.FW = {"fw_ver": 12}
    raw_rtc = bytearray([0xAA, 1, 2, 3, 4, 5, 6, 7])
    frozen_time = 1_700_000_123
    gpio_calls: list[tuple[str, object]] = []
    writes: list[int] = []
    progress: list[dict[str, object]] = []

    class FakeAgbGpio:
        def __init__(self, **_kwargs: object) -> None:
            gpio_calls.append(("init", None))

        def HasRTC(self, buffer: bytearray | None = None) -> bool:
            gpio_calls.append(("has_rtc", bytearray(buffer) if buffer is not None else None))
            return True

        def RTCReadStatus(self) -> int:
            gpio_calls.append(("status", None))
            return 0x40

    def record_write(command: int) -> None:
        writes.append(command)

    monkeypatch.setattr(lk_device_module, "AGB_GPIO", FakeAgbGpio)
    monkeypatch.setattr(lk_device_module.time, "time", lambda: frozen_time)
    monkeypatch.setattr(device, "_write", record_write)
    monkeypatch.setattr(device, "_read", lambda _length: bytearray(raw_rtc))
    monkeypatch.setattr(device, "SetProgress", lambda event: progress.append(dict(event)))
    destination = tmp_path / "agb-rtc.sav"
    buffer = bytearray(b"SAVE")
    args = {"rtc": True, "save_type": 3, "path": destination}
    expected_rtc = raw_rtc[1:] + bytearray([0x40]) + bytearray(frozen_time.to_bytes(8, "little"))

    assert device._finish_save_backup(args, CompletionMapper(), None, buffer, 0) == (False, True)

    assert destination.read_bytes() == buffer + expected_rtc
    assert writes == [device.DEVICE_CMD["AGB_READ_GPIO_RTC"]]
    assert gpio_calls == [("init", None), ("has_rtc", raw_rtc), ("status", None)]
    assert progress == [{"action": "UPDATE_POS", "pos": len(buffer) + 16}]
    assert device.NO_PROG_UPDATE is False


def test_finish_dmg_restore_forwards_rtc_tail_and_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = CompletionMapper(rtc_size=4)
    progress: list[dict[str, object]] = []
    monkeypatch.setattr(device, "SetProgress", lambda event: progress.append(dict(event)))
    buffer = bytearray(b"SAVE") + bytearray(b"RTCD")
    args = {
        "rtc": True,
        "rtc_advance": True,
        "save_type": 3,
        "erase": False,
        "verify_write": False,
    }

    assert device._FinishSaveRestore(args, mapper, buffer, 4) is True

    assert mapper.events == [("write_rtc", bytearray(b"RTCD"), True)]
    assert progress == [
        {"action": "UPDATE_RTC", "method": "write"},
        {"action": "UPDATE_POS", "pos": 8, "force_update": True},
    ]
    assert device.INFO["transferred"] == 8


def test_finish_agb_restore_forwards_rtc_tail_to_explicit_gpio_fake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    gpio_calls: list[tuple[bytearray, bool]] = []
    progress: list[dict[str, object]] = []

    class FakeAgbGpio:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def WriteRTC(self, buffer: bytearray, advance: bool = False) -> None:
            gpio_calls.append((bytearray(buffer), advance))

    monkeypatch.setattr(lk_device_module, "AGB_GPIO", FakeAgbGpio)
    monkeypatch.setattr(device, "SetProgress", lambda event: progress.append(dict(event)))
    rtc = bytearray(range(16))
    buffer = bytearray(b"SAVE") + rtc
    args = {
        "rtc": True,
        "rtc_advance": True,
        "save_type": 3,
        "erase": False,
        "verify_write": False,
    }

    assert device._FinishSaveRestore(args, CompletionMapper(), buffer, 4) is True

    assert gpio_calls == [(rtc, True)]
    assert progress == [
        {"action": "UPDATE_RTC", "method": "write"},
        {"action": "UPDATE_POS", "pos": 20, "force_update": True},
    ]


@pytest.mark.parametrize(
    ("verify_write", "erase", "verification_result", "expected_result", "expected_calls"),
    [
        (False, False, False, True, 0),
        (True, False, True, True, 1),
        (True, False, False, False, 1),
        (True, False, None, None, 1),
        (True, True, False, True, 0),
    ],
    ids=["disabled", "success", "failure", "canceled", "erase-skips-readback"],
)
def test_finish_restore_preserves_verification_result(
    monkeypatch: pytest.MonkeyPatch,
    verify_write: bool,
    erase: bool,
    verification_result: bool | None,
    expected_result: bool | None,
    expected_calls: int,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = CompletionMapper()
    verification_calls: list[tuple[dict[str, object], CompletionMapper, bytearray, int]] = []
    progress: list[dict[str, object]] = []

    def verify(
        args: dict[str, object],
        verify_mapper: CompletionMapper,
        buffer: bytearray,
        buffer_offset: int,
    ) -> bool | None:
        verification_calls.append((args, verify_mapper, buffer, buffer_offset))
        return verification_result

    monkeypatch.setattr(device, "_VerifySaveWrite", verify)
    monkeypatch.setattr(device, "SetProgress", lambda event: progress.append(dict(event)))
    buffer = bytearray(b"SAVE")
    args = {
        "rtc": False,
        "save_type": 3,
        "erase": erase,
        "verify_write": verify_write,
    }

    assert device._FinishSaveRestore(args, mapper, buffer, len(buffer)) is expected_result

    assert len(verification_calls) == expected_calls
    if verification_calls:
        assert verification_calls == [(args, mapper, buffer, len(buffer))]
    assert progress == [{"action": "UPDATE_POS", "pos": 4, "force_update": True}]


@pytest.mark.parametrize(
    ("firmware", "audio_low"),
    [(11, False), (12, True)],
    ids=["legacy-no-audio", "acknowledged-audio-restore"],
)
def test_reset_dmg_save_hardware_orders_mapper_pin_and_firmware_operations(
    monkeypatch: pytest.MonkeyPatch,
    firmware: int,
    audio_low: bool,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.FW = {"fw_ver": firmware}
    events: list[tuple[object, ...]] = []
    mapper = CompletionMapper(events=events)

    def set_fw_variable(name: str, value: int) -> None:
        events.append(("fw_variable", name, value))

    def set_pin(pins: list[str], *, set_high: bool) -> None:
        events.append(("set_pin", pins, set_high))

    def write(command: int, *, wait: bool) -> None:
        events.append(("write", command, wait))

    monkeypatch.setattr(device, "_set_fw_variable", set_fw_variable)
    monkeypatch.setattr(device, "SetPin", set_pin)
    monkeypatch.setattr(device, "_write", write)

    device._ResetSaveTransferHardware(mapper, None, bytearray(b"SAVE"), audio_low)

    expected = [
        ("select_ram", 0),
        ("enable_ram", False),
        ("fw_variable", "DMG_READ_CS_PULSE", 0),
    ]
    if audio_low:
        expected.extend(
            [
                ("fw_variable", "FLASH_WE_PIN", 0x02),
                ("set_pin", ["PIN_AUDIO"], True),
            ],
        )
    expected.append(("write", device.DEVICE_CMD["SET_ADDR_AS_INPUTS"], firmware >= 12))
    assert events == expected


def test_reset_agb_bank_select_restores_captured_byte_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    writes: list[tuple[int, int, bool]] = []

    def cart_write(*, address: int, value: int, sram: bool) -> None:
        writes.append((address, value, sram))

    monkeypatch.setattr(device, "_cart_write", cart_write)
    buffer = bytearray(b"BANK-\xa5DATA")

    device._ResetSaveTransferHardware(
        CompletionMapper(),
        {"flash_bank_select_type": 1},
        buffer,
        False,
    )

    assert buffer[5] == 0xA5
    assert writes == [(5, 0, True), (5, 0xA5, True)]
