"""Flash-write chunk routing and sector-range contracts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

import pytest

import FlashGBX.LK_Device as lk_device_module
from FlashGBX.Flashcart import Flashcart, Flashcart_DMG_MMSA
from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.LK_Device import (
    _FlashBankContext,
    _FlashBankState,
    _FlashChunkParameters,
    _FlashMatchContext,
    _FlashMatchResult,
    _FlashSectorEraseContext,
    _FlashSectorEraseResult,
    _FlashSectorState,
    _FlashWritePreparation,
)
from tests.fakes import flashcart_callbacks, flashcart_profile

if TYPE_CHECKING:
    from pathlib import Path

CHUNK_WRITERS = (
    "WriteROM",
    "WriteROM_GBMEMORY",
    "WriteROM_DMG_MBC5_32M_FLASH",
    "WriteROM_DMG_EEPROM",
    "WriteROM_DMG_DatelOrbitV2",
)


class SectorMapper:
    """Small mapper exposing bank selection for flash-sector decisions."""

    def __init__(
        self,
        *,
        bank_size: int = 4,
        reset_banks: set[int] | None = None,
    ) -> None:
        self.bank_size = bank_size
        self.reset_banks = set(reset_banks or set())
        self.reset_requests: list[int] = []
        self.selected_rom_banks: list[int] = []

    def ResetBeforeBankChange(self, bank: int) -> bool:
        self.reset_requests.append(bank)
        return bank in self.reset_banks

    def SelectBankROM(self, bank: int) -> tuple[int, int]:
        self.selected_rom_banks.append(bank)
        return bank * self.bank_size, self.bank_size


class SectorDecisionFlashcart:
    """Finite flash-cart boundary for matching and erase decisions."""

    def __init__(
        self,
        *,
        config: dict[str, object] | None = None,
        sector_results: list[int | bool] | None = None,
        unlock_results: list[bool] | None = None,
        pulse_reset: bool = False,
        commands_on_bank_one: bool = False,
    ) -> None:
        self.config = dict(config or {"command_set": "AMD"})
        self.sector_results = list(sector_results or [])
        self.unlock_results = list(unlock_results or [])
        self.pulse_reset = pulse_reset
        self.commands_on_bank_one = commands_on_bank_one
        self.LAST_SR = 0x80
        self.reset_calls: list[bool] = []
        self.selected_rom_banks: list[int] = []
        self.sector_erase_calls: list[dict[str, object]] = []
        self.physical_erase_calls: list[dict[str, object]] = []
        self.unlock_calls = 0

    def PulseResetAfterWrite(self) -> bool:
        return self.pulse_reset

    def Reset(self, full_reset: bool = False) -> bool:
        self.reset_calls.append(full_reset)
        return True

    def SelectBankROM(self, bank: int) -> None:
        self.selected_rom_banks.append(bank)

    def FlashCommandsOnBank1(self) -> bool:
        return self.commands_on_bank_one

    def SectorErase(self, pos: int, buffer_pos: int, skip: bool = False) -> int | bool:
        request = {"pos": pos, "buffer_pos": buffer_pos, "skip": skip}
        self.sector_erase_calls.append(request)
        if not skip:
            self.physical_erase_calls.append(request)
        if not self.sector_results:
            msg = "Unexpected SectorErase call without a finite response"
            raise AssertionError(msg)
        return self.sector_results.pop(0)

    def Unlock(self) -> bool:
        self.unlock_calls += 1
        if not self.unlock_results:
            msg = "Unexpected Unlock call without a finite response"
            raise AssertionError(msg)
        return self.unlock_results.pop(0)


class SuccessfulWorkerRecords:
    """Device I/O and completion calls from a successful flash loop."""

    def __init__(self, comparison_results: list[bool] | None = None) -> None:
        self.comparison_results = list(comparison_results or [])
        self.comparisons: list[dict[str, object]] = []
        self.rom_writes: list[dict[str, object]] = []
        self.firmware_variables: list[tuple[str, int]] = []
        self.device_writes: list[tuple[object, bool]] = []
        self.progress: list[dict[str, object]] = []
        self.finish = Mock(return_value=True)


class FlashSerialRecorder:
    """Open/closed serial state and reset counts used by retry recovery."""

    def __init__(self, *, is_open: bool = True) -> None:
        self.is_open = is_open
        self.input_resets = 0
        self.output_resets = 0

    def reset_input_buffer(self) -> None:
        self.input_resets += 1

    def reset_output_buffer(self) -> None:
        self.output_resets += 1


class FailureWorkerRecords:
    """Finite scripted device boundary for retry and abort scenarios."""

    def __init__(self, write_results: list[bool | None]) -> None:
        self.write_results = list(write_results)
        self.rom_writes: list[dict[str, object]] = []
        self.firmware_variables: list[tuple[str, int]] = []
        self.device_writes: list[tuple[object, bool]] = []
        self.cart_writes: list[tuple[int, int, dict[str, object]]] = []
        self.progress: list[dict[str, object]] = []
        self.status_reads: list[str] = []
        self.sleep_calls: list[float] = []
        self.serial = FlashSerialRecorder()
        self.finish = Mock(return_value=True)


class FinalizationRecords:
    """Observable boundaries surrounding the real flash finalizer."""

    def __init__(self) -> None:
        self.progress: list[dict[str, object]] = []
        self.modes: list[str] = []
        self.device_writes: list[tuple[object, bool]] = []
        self.firmware_variables: list[tuple[str, int]] = []
        self.auto_poweroff_finish = Mock()


def make_chunk_parameters(**overrides: object) -> _FlashChunkParameters:
    """Build a small write request with explicit, reusable defaults."""
    values: dict[str, object] = {
        "command_set_type": "AMD",
        "pos": 0x3456,
        "data_import": bytearray(b"0123456789"),
        "buffer_pos": 3,
        "buffer_len": 4,
        "bank": 2,
        "flash_buffer_size": 16,
        "skip_init": True,
        "rumble": False,
    }
    values.update(overrides)
    return _FlashChunkParameters(**values)


def make_preparation(**overrides: object) -> _FlashWritePreparation:
    """Build a tiny flash preparation with explicit worker defaults."""
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart()
    values: dict[str, object] = {
        "cart_name": "Test flash cart",
        "cart_type": {"command_set": "AMD"},
        "flashcart": flashcart,
        "data_import": bytearray(b"ABCDEFGH"),
        "data_map_import": bytearray(),
        "flash_offset": 0,
        "active_voltage": 5.0,
        "mbc": mapper,
        "end_bank": 2,
        "rom_bank_size": 4,
        "enable_pullup_wr": 0,
        "error_message": "",
        "flash_buffer_size": 4,
        "command_set_type": "AMD",
        "sector_offsets": [[0, 8]],
        "write_sectors": [[0, 8]],
        "delta_state": None,
        "state_path": "",
        "chip_erase": False,
        "buffer_len": 2,
        "verify_sectors": [],
    }
    values.update(overrides)
    return _FlashWritePreparation(**values)


def make_single_sector_preparation(
    flashcart: SectorDecisionFlashcart,
    mapper: SectorMapper,
) -> _FlashWritePreparation:
    """Build the smallest preparation that can exercise retry recovery."""
    return make_preparation(
        flashcart=flashcart,
        mbc=mapper,
        data_import=bytearray(b"ABCD"),
        end_bank=1,
        rom_bank_size=4,
        sector_offsets=[[0, 4]],
        write_sectors=[[0, 4]],
        buffer_len=2,
    )


def install_successful_worker_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    *,
    comparison_results: list[bool] | None = None,
) -> SuccessfulWorkerRecords:
    """Record physical I/O while retaining the real successful worker loop."""
    records = SuccessfulWorkerRecords(comparison_results)
    clock = [10.0]

    def write_rom(
        address: int,
        buffer: bytes | bytearray | memoryview,
        flash_buffer_size: int | bool = False,
        skip_init: bool = False,
        rumble_stop: bool = False,
        max_length: int = 0x400,
    ) -> None:
        records.rom_writes.append(
            {
                "address": address,
                "buffer": bytearray(buffer),
                "flash_buffer_size": flash_buffer_size,
                "skip_init": skip_init,
                "rumble_stop": rumble_stop,
                "max_length": max_length,
            },
        )

    def compare_crc32(
        *,
        buffer: bytes | bytearray | memoryview,
        offset: int,
        length: int,
        address: int,
        flashcart: object,
        reset: bool,
        mbc: object,
        bank: int,
    ) -> bool:
        records.comparisons.append(
            {
                "buffer": buffer,
                "offset": offset,
                "length": length,
                "address": address,
                "flashcart": flashcart,
                "reset": reset,
                "mbc": mbc,
                "bank": bank,
            },
        )
        if not records.comparison_results:
            msg = "Unexpected CompareCRC32 call without a finite response"
            raise AssertionError(msg)
        return records.comparison_results.pop(0)

    def set_firmware_variable(name: str, value: int) -> None:
        records.firmware_variables.append((name, value))

    def device_write(value: object, wait: bool = False) -> None:
        records.device_writes.append((value, wait))

    def fake_time() -> float:
        value = clock[0]
        clock[0] += 0.25
        return value

    monkeypatch.setattr(device, "WriteROM", write_rom)
    monkeypatch.setattr(device, "CompareCRC32", compare_crc32)
    monkeypatch.setattr(device, "_set_fw_variable", set_firmware_variable)
    monkeypatch.setattr(device, "_write", device_write)
    monkeypatch.setattr(device, "SetProgress", lambda event: records.progress.append(dict(event)))
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: False)
    monkeypatch.setattr(device, "_FinishFlashWrite", records.finish)
    monkeypatch.setattr(lk_device_module.time, "time", fake_time)
    return records


def install_failure_worker_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    *,
    write_results: list[bool | None],
) -> FailureWorkerRecords:
    """Install finite failure/recovery I/O while retaining the real loop."""
    records = FailureWorkerRecords(write_results)
    clock = [20.0]
    device.device = records.serial  # type: ignore[assignment]
    device.fw = {"fw_ver": 12, "pcb_name": "Test device"}

    def write_rom(
        address: int,
        buffer: bytes | bytearray | memoryview,
        flash_buffer_size: int | bool = False,
        skip_init: bool = False,
        rumble_stop: bool = False,
        max_length: int = 0x400,
    ) -> bool | None:
        records.rom_writes.append(
            {
                "address": address,
                "buffer": bytearray(buffer),
                "flash_buffer_size": flash_buffer_size,
                "skip_init": skip_init,
                "rumble_stop": rumble_stop,
                "max_length": max_length,
            },
        )
        if not records.write_results:
            msg = "Unexpected WriteROM call without a finite response"
            raise AssertionError(msg)
        return records.write_results.pop(0)

    def set_firmware_variable(name: str, value: int) -> None:
        records.firmware_variables.append((name, value))

    def device_write(value: object, wait: bool = False) -> None:
        records.device_writes.append((value, wait))

    def cart_write(address: int, value: int, *_args: object, **kwargs: object) -> None:
        records.cart_writes.append((address, value, dict(kwargs)))

    def get_firmware_variable(name: str) -> int:
        records.status_reads.append(name)
        return 0x80

    def fake_time() -> float:
        value = clock[0]
        clock[0] += 0.25
        return value

    def fake_sleep(duration: float) -> None:
        records.sleep_calls.append(duration)

    monkeypatch.setattr(device, "WriteROM", write_rom)
    monkeypatch.setattr(
        device,
        "CompareCRC32",
        Mock(side_effect=AssertionError("Failure scenarios disable sector comparison")),
    )
    monkeypatch.setattr(device, "_set_fw_variable", set_firmware_variable)
    monkeypatch.setattr(device, "_get_fw_variable", get_firmware_variable)
    monkeypatch.setattr(device, "_write", device_write)
    monkeypatch.setattr(device, "_cart_write", cart_write)
    monkeypatch.setattr(device, "SetProgress", lambda event: records.progress.append(dict(event)))
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: False)
    monkeypatch.setattr(device, "GetFullNameLabel", lambda: "Test device")
    monkeypatch.setattr(device, "_FinishFlashWrite", records.finish)
    monkeypatch.setattr(lk_device_module.time, "time", fake_time)
    monkeypatch.setattr(lk_device_module.time, "sleep", fake_sleep)
    return records


def install_finalization_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> FinalizationRecords:
    """Record mode, progress, and hardware calls while retaining finalization."""
    records = FinalizationRecords()
    device.fw = {"fw_ver": 12, "pcb_name": "Test device"}
    device.info["action"] = device.ACTIONS["ROM_WRITE"]

    def set_progress(event: dict[str, object]) -> None:
        records.progress.append(dict(event))

    def set_mode(mode: str) -> None:
        records.modes.append(mode)

    def device_write(value: object, wait: bool = False) -> None:
        records.device_writes.append((value, wait))

    def set_firmware_variable(name: str, value: int) -> None:
        records.firmware_variables.append((name, value))

    monkeypatch.setattr(device, "SetProgress", set_progress)
    monkeypatch.setattr(device, "SetMode", set_mode)
    monkeypatch.setattr(device, "_write", device_write)
    monkeypatch.setattr(device, "_set_fw_variable", set_firmware_variable)
    monkeypatch.setattr(device, "_thread_worker_auto_poweroff_finish", records.auto_poweroff_finish)
    return records


def install_chunk_writers(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Mock]:
    """Replace physical writers with strict recording boundaries."""
    writers: dict[str, Mock] = {}
    for name in CHUNK_WRITERS:
        writer = Mock(return_value=False)
        monkeypatch.setattr(device, name, writer)
        writers[name] = writer
    return writers


@pytest.mark.parametrize(
    ("skipping", "expected_skip_init"),
    [(False, True), (True, False)],
    ids=["reuse-initialization", "reinitialize-after-skipped-chunk"],
)
def test_write_flash_chunk_slices_normal_write_and_tracks_skip_state(
    monkeypatch: pytest.MonkeyPatch,
    skipping: bool,
    expected_skip_init: bool,
) -> None:
    device = GbxDevice()
    device.fw = {"fw_ver": 12}
    device.skipping = skipping
    writer = Mock(return_value=True)
    monkeypatch.setattr(device, "WriteROM", writer)
    parameters = make_chunk_parameters(rumble=True)

    status, bytes_advanced = device._WriteFlashChunk(parameters)

    assert status is True
    assert bytes_advanced == 4
    writer.assert_called_once_with(
        address=0x3456,
        buffer=bytearray(b"3456"),
        flash_buffer_size=16,
        skip_init=expected_skip_init,
        rumble_stop=True,
        max_length=device.max_buffer_write,
    )


@pytest.mark.parametrize(
    ("command_set", "firmware", "expected_writer", "expected_arguments"),
    [
        (
            "GBMEMORY",
            1,
            "WriteROM_GBMEMORY",
            {"address": 0x3456, "buffer": bytearray(b"3456"), "bank": 2},
        ),
        (
            "GBMEMORY",
            2,
            "WriteROM",
            {
                "address": 0x3456,
                "buffer": bytearray(b"3456"),
                "flash_buffer_size": 16,
                "skip_init": True,
            },
        ),
        (
            "DMG-MBC5-32M-FLASH",
            11,
            "WriteROM_DMG_MBC5_32M_FLASH",
            {"address": 0x3456, "buffer": bytearray(b"3456"), "bank": 2},
        ),
        (
            "EEPROM",
            12,
            "WriteROM_DMG_EEPROM",
            {
                "address": 0x3456,
                "buffer": bytearray(b"3456"),
                "bank": 2,
                "eeprom_buffer_size": 256,
            },
        ),
        (
            "BLAZE_XPLODER",
            12,
            "WriteROM_DMG_EEPROM",
            {"address": 0x3456, "buffer": bytearray(b"3456"), "bank": 2},
        ),
        (
            "DATEL_ORBITV2",
            12,
            "WriteROM",
            {
                "address": 0x02003456,
                "buffer": bytearray(b"3456"),
                "flash_buffer_size": 16,
                "skip_init": True,
            },
        ),
        (
            "DATEL_ORBITV2",
            11,
            "WriteROM_DMG_DatelOrbitV2",
            {"address": 0x3456, "buffer": bytearray(b"3456"), "bank": 2},
        ),
    ],
    ids=[
        "gbmemory-legacy",
        "gbmemory-modern",
        "mbc5-32m-legacy",
        "eeprom",
        "xploder",
        "datel-packed-bank",
        "datel-legacy",
    ],
)
def test_write_flash_chunk_routes_specialized_command_sets(
    monkeypatch: pytest.MonkeyPatch,
    command_set: str,
    firmware: int,
    expected_writer: str,
    expected_arguments: dict[str, object],
) -> None:
    device = GbxDevice()
    device.fw = {"fw_ver": firmware}
    writers = install_chunk_writers(device, monkeypatch)
    parameters = make_chunk_parameters(command_set_type=command_set)

    status, bytes_advanced = device._WriteFlashChunk(parameters)

    assert status is False
    assert bytes_advanced == 4
    writers[expected_writer].assert_called_once_with(**expected_arguments)
    for name, writer in writers.items():
        if name != expected_writer:
            writer.assert_not_called()


def test_write_flash_chunk_stops_at_agb_eeprom_reserved_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.fw = {"fw_ver": 12}
    writer = Mock(return_value=True)
    monkeypatch.setattr(device, "WriteROM", writer)
    reserved_start = 0x1FFFF00
    bytes_remaining = 100
    source = bytearray(reserved_start)
    expected_tail = bytearray((index * 7) & 0xFF for index in range(bytes_remaining))
    source[-bytes_remaining:] = expected_tail
    parameters = make_chunk_parameters(
        pos=0x7F00,
        data_import=source,
        buffer_pos=reserved_start - bytes_remaining,
        buffer_len=0x100,
        skip_init=False,
    )

    status, bytes_advanced = device._WriteFlashChunk(parameters)

    assert status is True
    assert bytes_advanced == bytes_remaining
    writer.assert_called_once_with(
        address=0x7F00,
        buffer=expected_tail,
        flash_buffer_size=16,
        skip_init=False,
        rumble_stop=False,
        max_length=256,
    )


@pytest.mark.parametrize(
    ("sector", "sector_offsets", "first_sector_written", "expected"),
    [
        (
            [0x4000, 0x4000, 0x11111111],
            [[0, 0x4000], [0x4000, 0x4000]],
            False,
            _FlashSectorState(
                retry_hp=15,
                buffer_pos=0x4000,
                start_address=0x4000,
                end_address=0x8000,
                sector_pos=1,
                start_bank=1,
                end_bank=2,
            ),
        ),
        (
            [0x3000, 0x3000, 0x22222222],
            [[0, 0x3000], [0x3000, 0x3000]],
            True,
            _FlashSectorState(
                retry_hp=100,
                buffer_pos=0x3000,
                start_address=0x3000,
                end_address=0x6000,
                sector_pos=1,
                start_bank=0,
                end_bank=2,
            ),
        ),
    ],
    ids=["aligned-first-sector", "crossing-subsequent-sector"],
)
def test_prepare_flash_sector_calculates_bank_range_and_retry_budget(
    sector: list[int],
    sector_offsets: list[list[int]],
    first_sector_written: bool,
    expected: _FlashSectorState,
) -> None:
    device = GbxDevice()

    result = device._PrepareFlashSector(
        sector=sector,
        first_sector_written=first_sector_written,
        sector_offsets=sector_offsets,
        rom_bank_size=0x4000,
    )

    assert result == expected


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_prepare_flash_sector_rejects_missing_delta_sector(
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    device = GbxDevice()
    device.mode = mode  # type: ignore[assignment]

    result = device._PrepareFlashSector(
        sector=[0x1000, 0x1000, 0x12345678],
        first_sector_written=False,
        sector_offsets=[[0, 0x1000], [0x2000, 0x1000]],
        rom_bank_size=0x4000,
    )

    assert result is None
    output = capsys.readouterr().out
    assert ("Sector not found for delta writing" in output) is (mode == "DMG")


@pytest.mark.parametrize(
    ("current_bank", "reset_banks", "expected_reset_write"),
    [(0, {1}, True), (1, set(), False)],
    ids=["bank-change", "bank-unchanged"],
)
def test_select_flash_write_bank_maps_dmg_window(
    monkeypatch: pytest.MonkeyPatch,
    current_bank: int,
    reset_banks: set[int],
    expected_reset_write: bool,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.fw = {"fw_ver": 12}
    mapper = SectorMapper(reset_banks=reset_banks)
    flashcart = SectorDecisionFlashcart()
    write = Mock()
    set_variable = Mock()
    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "_set_fw_variable", set_variable)
    context = _FlashBankContext(
        mbc=mapper,
        flashcart=flashcart,  # type: ignore[arg-type]
        cart_type={"command_set": "AMD"},
        bank=1,
        current_bank=current_bank,
        start_address=0,
        end_address=12,
        buffer_position=6,
        rom_bank_size=4,
        sector_size=4,
        buffer_length=10,
    )

    result = device._SelectFlashWriteBank(context)

    assert result == _FlashBankState(
        start_address=6,
        end_address=8,
        current_bank=current_bank,
        buffer_length=4,
    )
    assert mapper.reset_requests == [1]
    assert mapper.selected_rom_banks == [1]
    set_variable.assert_called_once_with("DMG_ROM_BANK", 1)
    if expected_reset_write:
        write.assert_called_once_with(device.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
    else:
        write.assert_not_called()
    assert flashcart.reset_calls == []
    assert flashcart.selected_rom_banks == []


@pytest.mark.parametrize(
    ("current_bank", "expected", "expected_selected"),
    [
        (1, _FlashBankState(2, 8, 2, 2), [2]),
        (2, _FlashBankState(10, 16, 2, 2), []),
    ],
    ids=["select-new-bank", "keep-current-bank"],
)
def test_select_flash_write_bank_uses_agb_profile_bank_selection(
    current_bank: int,
    expected: _FlashBankState,
    expected_selected: list[int],
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    mapper = SectorMapper(bank_size=8)
    flashcart = SectorDecisionFlashcart()
    context = _FlashBankContext(
        mbc=mapper,
        flashcart=flashcart,  # type: ignore[arg-type]
        cart_type={"command_set": "AMD", "flash_bank_select_type": 1, "flash_bank_size": 8},
        bank=2,
        current_bank=current_bank,
        start_address=10,
        end_address=16,
        buffer_position=10,
        rom_bank_size=8,
        sector_size=6,
        buffer_length=2,
    )

    result = device._SelectFlashWriteBank(context)

    assert result == expected
    assert flashcart.selected_rom_banks == expected_selected
    assert flashcart.reset_calls == ([True] if expected_selected else [])
    assert mapper.reset_requests == []
    assert mapper.selected_rom_banks == []


@pytest.mark.parametrize(
    ("firmware", "compare_sectors", "chip_erase", "command_set"),
    [
        (12, False, False, "AMD"),
        (11, True, False, "AMD"),
        (12, True, True, "AMD"),
        (12, True, False, "GBAMP"),
    ],
    ids=["comparison-disabled", "unsupported-firmware", "chip-erased", "gbamp-excluded"],
)
def test_try_skip_matching_flash_sector_bypasses_ineligible_comparisons(
    monkeypatch: pytest.MonkeyPatch,
    firmware: int,
    compare_sectors: bool,
    chip_erase: bool,
    command_set: str,
) -> None:
    device = GbxDevice()
    device.fw = {"fw_ver": firmware}
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart()
    preparation = make_preparation(
        mbc=mapper,
        flashcart=flashcart,
        cart_type={"command_set": command_set},
        chip_erase=chip_erase,
    )
    compare = Mock(side_effect=AssertionError("Ineligible sector must not be compared"))
    monkeypatch.setattr(device, "CompareCRC32", compare)
    context = _FlashMatchContext(
        args={"compare_sectors": compare_sectors},
        preparation=preparation,
        sector=[0, 8],
        bank=0,
        end_bank=2,
        current_bank=None,
        start_address=0,
        end_address=8,
        buffer_position=0,
        sector_position=0,
        buffer_length=2,
    )

    result = device._TrySkipMatchingFlashSector(context)

    assert result == _FlashMatchResult(
        skipped=False,
        canceled=False,
        bank=0,
        current_bank=None,
        start_address=0,
        end_address=8,
        buffer_position=0,
        sector_size=8,
        buffer_length=2,
    )
    compare.assert_not_called()
    assert flashcart.sector_erase_calls == []
    assert mapper.selected_rom_banks == []


def test_try_skip_matching_flash_sector_checks_every_bank_before_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.fw = {"fw_ver": 12}
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[8])
    preparation = make_preparation(mbc=mapper, flashcart=flashcart)
    compare = Mock(side_effect=[True, True])
    progress = Mock()
    set_variable = Mock()
    device_write = Mock()
    monkeypatch.setattr(device, "CompareCRC32", compare)
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "_set_fw_variable", set_variable)
    monkeypatch.setattr(device, "_write", device_write)
    context = _FlashMatchContext(
        args={"compare_sectors": True},
        preparation=preparation,
        sector=[0, 8],
        bank=0,
        end_bank=2,
        current_bank=None,
        start_address=0,
        end_address=8,
        buffer_position=0,
        sector_position=0,
        buffer_length=4,
    )

    result = device._TrySkipMatchingFlashSector(context)

    assert result == _FlashMatchResult(
        skipped=True,
        canceled=False,
        bank=2,
        current_bank=None,
        start_address=4,
        end_address=8,
        buffer_position=8,
        sector_size=8,
        buffer_length=4,
    )
    assert compare.call_args_list == [
        call(
            buffer=preparation.data_import,
            offset=0,
            length=4,
            address=0,
            flashcart=flashcart,
            reset=True,
            mbc=mapper,
            bank=0,
        ),
        call(
            buffer=preparation.data_import,
            offset=4,
            length=4,
            address=4,
            flashcart=flashcart,
            reset=True,
            mbc=mapper,
            bank=1,
        ),
    ]
    assert mapper.selected_rom_banks == [0, 1]
    assert set_variable.call_args_list == [call("DMG_ROM_BANK", 0), call("DMG_ROM_BANK", 1)]
    assert progress.call_args_list == [
        call({"action": "UPDATE_POS", "pos": 0}),
        call({"action": "UPDATE_POS", "pos": 4}),
    ]
    assert flashcart.sector_erase_calls == [{"pos": 4, "buffer_pos": 0, "skip": True}]
    assert flashcart.physical_erase_calls == []
    device_write.assert_not_called()
    assert device.no_prog_update is False


def test_try_skip_matching_flash_sector_keeps_original_position_on_late_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.fw = {"fw_ver": 12}
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart()
    data = bytearray(b"ABCDEFGHIJKL")
    preparation = make_preparation(
        mbc=mapper,
        flashcart=flashcart,
        data_import=data,
        sector_offsets=[[0, 12]],
        write_sectors=[[0, 12]],
    )
    compare = Mock(side_effect=[True, False])
    progress = Mock()
    monkeypatch.setattr(device, "CompareCRC32", compare)
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "_set_fw_variable", Mock())
    context = _FlashMatchContext(
        args={"compare_sectors": True},
        preparation=preparation,
        sector=[0, 12],
        bank=0,
        end_bank=3,
        current_bank=None,
        start_address=0,
        end_address=12,
        buffer_position=0,
        sector_position=0,
        buffer_length=4,
    )

    result = device._TrySkipMatchingFlashSector(context)

    assert result == _FlashMatchResult(
        skipped=False,
        canceled=False,
        bank=0,
        current_bank=None,
        start_address=4,
        end_address=8,
        buffer_position=0,
        sector_size=12,
        buffer_length=4,
    )
    assert compare.call_count == 2
    assert mapper.selected_rom_banks == [0, 1]
    assert progress.call_args_list == [
        call({"action": "UPDATE_POS", "pos": 0}),
        call({"action": "UPDATE_POS", "pos": 4}),
    ]
    assert flashcart.sector_erase_calls == []


def test_matching_sector_advances_real_flashcart_state_without_physical_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    device.fw = {"fw_ver": 12}
    callback_calls, callbacks = flashcart_callbacks()
    profile = flashcart_profile(
        type="AGB",
        sector_size=[[4, 1], [8, 1]],
        commands={
            "single_write": [[0, 0]],
            "sector_erase": [["SA", 0x30]],
        },
    )
    flashcart = Flashcart(profile, callbacks)
    mapper = SectorMapper()
    preparation = make_preparation(
        flashcart=flashcart,
        mbc=mapper,
        cart_type=profile,
        data_import=bytearray(b"DATA"),
        end_bank=1,
        sector_offsets=[[0, 4], [4, 8]],
        write_sectors=[[0, 4]],
    )
    compare = Mock(return_value=True)
    progress = Mock()
    device_write = Mock()
    monkeypatch.setattr(device, "CompareCRC32", compare)
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "_write", device_write)
    context = _FlashMatchContext(
        args={"compare_sectors": True},
        preparation=preparation,
        sector=[0, 4],
        bank=0,
        end_bank=1,
        current_bank=0,
        start_address=0,
        end_address=4,
        buffer_position=0,
        sector_position=0,
        buffer_length=2,
    )

    result = device._TrySkipMatchingFlashSector(context)

    assert result.skipped is True
    assert result.canceled is False
    assert result.sector_size == 8
    assert result.buffer_position == 8
    assert callback_calls == {"read": [], "write": [], "fast": [], "progress": []}
    device_write.assert_not_called()
    compare.assert_called_once()
    progress.assert_called_once_with({"action": "UPDATE_POS", "pos": 0})


def test_try_skip_matching_flash_sector_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.fw = {"fw_ver": 12}
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[8])
    preparation = make_preparation(mbc=mapper, flashcart=flashcart)
    compare_results = iter([True, True])

    def compare(**_kwargs: object) -> bool:
        result = next(compare_results)
        if mapper.selected_rom_banks == [0, 1]:
            device.cancel = True
            device.cancel_args = {"from_user": True}
        return result

    progress = Mock()
    set_variable = Mock()
    monkeypatch.setattr(device, "CompareCRC32", compare)
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "_set_fw_variable", set_variable)
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: False)
    context = _FlashMatchContext(
        args={"compare_sectors": True},
        preparation=preparation,
        sector=[0, 8],
        bank=0,
        end_bank=2,
        current_bank=None,
        start_address=0,
        end_address=8,
        buffer_position=0,
        sector_position=0,
        buffer_length=4,
    )

    result = device._TrySkipMatchingFlashSector(context)

    assert result == _FlashMatchResult(
        skipped=False,
        canceled=True,
        bank=2,
        current_bank=None,
        start_address=4,
        end_address=8,
        buffer_position=0,
        sector_size=8,
        buffer_length=4,
    )
    assert flashcart.sector_erase_calls == [{"pos": 4, "buffer_pos": 0, "skip": True}]
    assert progress.call_args_list[-1] == call({"action": "ABORT", "abortable": False, "from_user": True})
    assert set_variable.call_args_list[-1] == call("AGB_IRQ_ENABLED", 0)
    assert device.cancel_args == {}
    assert device.no_prog_update is False


@pytest.mark.parametrize("already_listed", [False, True], ids=["add-verification", "avoid-duplicate"])
def test_erase_flash_sector_runs_once_at_boundary_and_updates_next_size(
    monkeypatch: pytest.MonkeyPatch,
    already_listed: bool,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    flashcart = SectorDecisionFlashcart(sector_results=[8])
    mapper = SectorMapper()
    sector = [0, 4]
    verify_sectors = [sector.copy()] if already_listed else []
    preparation = make_preparation(
        flashcart=flashcart,
        mbc=mapper,
        sector_offsets=[[0, 4], [4, 8]],
        write_sectors=[sector],
        verify_sectors=verify_sectors,
    )
    progress = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: False)
    monkeypatch.setattr(lk_device_module.time, "time", Mock(side_effect=[10.0, 10.25]))
    context = _FlashSectorEraseContext(
        preparation=preparation,
        sector=sector,
        bank=0,
        position=0,
        buffer_position=0,
        sector_position=0,
        sector_size=4,
    )

    result = device._EraseFlashSector(context)

    assert result == _FlashSectorEraseResult(
        status=8,
        sector_position=1,
        sector_size=8,
        erased=True,
        canceled=False,
    )
    assert flashcart.sector_erase_calls == [{"pos": 0, "buffer_pos": 0, "skip": False}]
    assert flashcart.physical_erase_calls == flashcart.sector_erase_calls
    assert preparation.verify_sectors == [[0, 4]]
    assert progress.call_args_list == [
        call({"action": "UPDATE_POS", "pos": 0, "force_update": True}),
        call(
            {
                "action": "UPDATE_POS",
                "pos": 0,
                "sector_pos": 1,
                "sector_erase_time": 0.25,
                "force_update": True,
            },
        ),
    ]
    assert device.no_prog_update is False


@pytest.mark.parametrize(
    ("chip_erase", "buffer_position", "sector_position"),
    [(True, 0, 0), (False, 2, 0), (False, 8, 2)],
    ids=["chip-erased", "inside-sector", "past-sector-map"],
)
def test_erase_flash_sector_avoids_non_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    chip_erase: bool,
    buffer_position: int,
    sector_position: int,
) -> None:
    device = GbxDevice()
    flashcart = SectorDecisionFlashcart()
    preparation = make_preparation(
        flashcart=flashcart,
        chip_erase=chip_erase,
        sector_offsets=[[0, 4], [4, 4]],
    )
    progress = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    context = _FlashSectorEraseContext(
        preparation=preparation,
        sector=[0, 4],
        bank=0,
        position=buffer_position,
        buffer_position=buffer_position,
        sector_position=sector_position,
        sector_size=4,
    )

    result = device._EraseFlashSector(context)

    assert result == _FlashSectorEraseResult(
        status=None,
        sector_position=sector_position,
        sector_size=4,
        erased=False,
        canceled=False,
    )
    assert flashcart.sector_erase_calls == []
    assert preparation.verify_sectors == []
    progress.assert_not_called()


def test_erase_flash_sector_propagates_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    flashcart = SectorDecisionFlashcart(sector_results=[False])
    sector = [0, 4]
    preparation = make_preparation(
        flashcart=flashcart,
        sector_offsets=[sector],
        write_sectors=[sector],
    )
    progress = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: False)
    monkeypatch.setattr(lk_device_module.time, "time", Mock(return_value=10.0))
    context = _FlashSectorEraseContext(preparation, sector, 0, 0, 0, 0, 4)

    result = device._EraseFlashSector(context)

    assert result == _FlashSectorEraseResult(False, 1, 4, True, False)
    assert flashcart.physical_erase_calls == [{"pos": 0, "buffer_pos": 0, "skip": False}]
    assert preparation.verify_sectors == [sector]
    progress.assert_called_once_with({"action": "UPDATE_POS", "pos": 0, "force_update": True})
    assert device.no_prog_update is False


def test_erase_flash_sector_propagates_user_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.cancel_args = {"from_user": True}
    flashcart = SectorDecisionFlashcart(sector_results=[8])
    sector = [0, 4]
    preparation = make_preparation(
        flashcart=flashcart,
        sector_offsets=[sector],
        write_sectors=[sector],
    )
    progress = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: False)
    monkeypatch.setattr(lk_device_module.time, "time", Mock(return_value=10.0))
    context = _FlashSectorEraseContext(preparation, sector, 0, 0, 0, 0, 4)

    result = device._EraseFlashSector(context)

    assert result == _FlashSectorEraseResult(8, 1, 4, True, True)
    assert flashcart.physical_erase_calls == [{"pos": 0, "buffer_pos": 0, "skip": False}]
    assert preparation.verify_sectors == []
    progress.assert_called_once_with({"action": "UPDATE_POS", "pos": 0, "force_update": True})
    assert device.no_prog_update is False


def test_write_prepared_flash_rom_writes_two_sectors_across_bank_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.fw = {"fw_ver": 12}
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[4, 4])
    sectors = [[0, 4], [4, 4]]
    preparation = make_preparation(
        flashcart=flashcart,
        mbc=mapper,
        data_import=bytearray(b"ABCDEFGH"),
        end_bank=2,
        rom_bank_size=4,
        flash_buffer_size=4,
        sector_offsets=sectors,
        write_sectors=sectors,
        buffer_len=2,
    )
    records = install_successful_worker_boundaries(device, monkeypatch)
    args = {"compare_sectors": False}

    result = device._WritePreparedFlashROM(args, "DMG", preparation)

    assert result is True
    assert records.rom_writes == [
        {
            "address": 0,
            "buffer": bytearray(b"AB"),
            "flash_buffer_size": 4,
            "skip_init": False,
            "rumble_stop": False,
            "max_length": device.max_buffer_write,
        },
        {
            "address": 2,
            "buffer": bytearray(b"CD"),
            "flash_buffer_size": 4,
            "skip_init": True,
            "rumble_stop": False,
            "max_length": device.max_buffer_write,
        },
        {
            "address": 4,
            "buffer": bytearray(b"EF"),
            "flash_buffer_size": 4,
            "skip_init": False,
            "rumble_stop": False,
            "max_length": device.max_buffer_write,
        },
        {
            "address": 6,
            "buffer": bytearray(b"GH"),
            "flash_buffer_size": 4,
            "skip_init": True,
            "rumble_stop": False,
            "max_length": device.max_buffer_write,
        },
    ]
    assert flashcart.sector_erase_calls == [
        {"pos": 0, "buffer_pos": 0, "skip": False},
        {"pos": 4, "buffer_pos": 4, "skip": False},
    ]
    assert flashcart.physical_erase_calls == flashcart.sector_erase_calls
    assert flashcart.sector_results == []
    assert preparation.verify_sectors == sectors
    assert mapper.selected_rom_banks == [0, 1]
    assert records.firmware_variables == [("DMG_ROM_BANK", 0), ("DMG_ROM_BANK", 1)]
    assert records.device_writes == []
    assert records.comparisons == []
    assert records.progress == [
        {"action": "UPDATE_POS", "pos": 0, "force_update": True},
        {
            "action": "UPDATE_POS",
            "pos": 0,
            "sector_pos": 1,
            "sector_erase_time": 0.25,
            "force_update": True,
        },
        {"action": "UPDATE_POS", "pos": 2},
        {"action": "UPDATE_POS", "pos": 4},
        {"action": "UPDATE_POS", "pos": 4, "force_update": True},
        {
            "action": "UPDATE_POS",
            "pos": 4,
            "sector_pos": 2,
            "sector_erase_time": 0.25,
            "force_update": True,
        },
        {"action": "UPDATE_POS", "pos": 6},
        {"action": "UPDATE_POS", "pos": 8},
    ]
    records.finish.assert_called_once_with(args, "DMG", preparation, 2)
    assert device.cancel is False
    assert device.error is False


@pytest.mark.parametrize("second_sector_matches", [False, True], ids=["second-changed", "both-match"])
def test_write_prepared_flash_rom_programs_only_changed_sectors(
    monkeypatch: pytest.MonkeyPatch,
    second_sector_matches: bool,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.fw = {"fw_ver": 12}
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[4, 4])
    sectors = [[0, 4], [4, 4]]
    preparation = make_preparation(
        flashcart=flashcart,
        mbc=mapper,
        data_import=bytearray(b"ABCDEFGH"),
        end_bank=2,
        rom_bank_size=4,
        sector_offsets=sectors,
        write_sectors=sectors,
        buffer_len=2,
    )
    records = install_successful_worker_boundaries(
        device,
        monkeypatch,
        comparison_results=[True, second_sector_matches],
    )
    args = {"compare_sectors": True}

    result = device._WritePreparedFlashROM(args, "DMG", preparation)

    assert result is True
    assert [(item["offset"], item["length"], item["address"], item["bank"]) for item in records.comparisons] == [
        (0, 4, 0, 0),
        (4, 4, 4, 1),
    ]
    assert records.comparison_results == []
    if second_sector_matches:
        assert records.rom_writes == []
        assert flashcart.sector_erase_calls == [
            {"pos": 0, "buffer_pos": 0, "skip": True},
            {"pos": 4, "buffer_pos": 4, "skip": True},
        ]
        assert flashcart.physical_erase_calls == []
        assert mapper.selected_rom_banks == [0, 1]
        assert preparation.verify_sectors == []
    else:
        assert [(item["address"], item["buffer"]) for item in records.rom_writes] == [
            (4, bytearray(b"EF")),
            (6, bytearray(b"GH")),
        ]
        assert flashcart.sector_erase_calls == [
            {"pos": 0, "buffer_pos": 0, "skip": True},
            {"pos": 4, "buffer_pos": 4, "skip": False},
        ]
        assert flashcart.physical_erase_calls == [{"pos": 4, "buffer_pos": 4, "skip": False}]
        assert mapper.selected_rom_banks == [0, 1, 1]
        assert preparation.verify_sectors == [[4, 4]]
    records.finish.assert_called_once_with(args, "DMG", preparation, 2)


def test_write_prepared_flash_rom_chip_erased_path_skips_sector_erase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.fw = {"fw_ver": 12}
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart()
    preparation = make_preparation(
        flashcart=flashcart,
        mbc=mapper,
        data_import=bytearray(b"ABCDEFGH"),
        end_bank=2,
        rom_bank_size=4,
        sector_offsets=[[0, 4], [4, 4]],
        write_sectors=[[0, 8]],
        chip_erase=True,
        buffer_len=2,
    )
    records = install_successful_worker_boundaries(device, monkeypatch)
    args = {"compare_sectors": True}

    result = device._WritePreparedFlashROM(args, "DMG", preparation)

    assert result is True
    assert [(item["address"], item["buffer"]) for item in records.rom_writes] == [
        (0, bytearray(b"AB")),
        (2, bytearray(b"CD")),
        (4, bytearray(b"EF")),
        (6, bytearray(b"GH")),
    ]
    assert flashcart.sector_erase_calls == []
    assert flashcart.physical_erase_calls == []
    assert records.comparisons == []
    assert mapper.selected_rom_banks == [0, 1]
    records.finish.assert_called_once_with(args, "DMG", preparation, 2)


def test_write_prepared_flash_rom_nonzero_sector_preserves_earlier_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.fw = {"fw_ver": 12}
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[4])
    sector_offsets = [[0, 4], [4, 4]]
    preparation = make_preparation(
        flashcart=flashcart,
        mbc=mapper,
        data_import=bytearray(b"ABCDEFGH"),
        end_bank=2,
        rom_bank_size=4,
        sector_offsets=sector_offsets,
        write_sectors=[[4, 4]],
        buffer_len=2,
    )
    records = install_successful_worker_boundaries(device, monkeypatch)
    args = {"compare_sectors": False}

    result = device._WritePreparedFlashROM(args, "DMG", preparation)

    assert result is True
    assert records.rom_writes == [
        {
            "address": 4,
            "buffer": bytearray(b"EF"),
            "flash_buffer_size": 4,
            "skip_init": False,
            "rumble_stop": False,
            "max_length": device.max_buffer_write,
        },
        {
            "address": 6,
            "buffer": bytearray(b"GH"),
            "flash_buffer_size": 4,
            "skip_init": True,
            "rumble_stop": False,
            "max_length": device.max_buffer_write,
        },
    ]
    assert flashcart.sector_erase_calls == [{"pos": 4, "buffer_pos": 4, "skip": False}]
    assert preparation.verify_sectors == [[4, 4]]
    assert all(item["address"] >= 4 for item in records.rom_writes)
    assert [event for event in records.progress if event.get("pos") in (2, 3)] == []
    records.finish.assert_called_once_with(args, "DMG", preparation, 2)


def test_write_prepared_flash_rom_retries_failed_chunk_from_sector_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[4, 4], unlock_results=[True])
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_failure_worker_boundaries(
        device,
        monkeypatch,
        write_results=[False, None, None],
    )
    args = {"compare_sectors": False}

    result = device._WritePreparedFlashROM(args, "DMG", preparation)

    assert result is True
    assert [(item["address"], item["buffer"], item["skip_init"]) for item in records.rom_writes] == [
        (0, bytearray(b"AB"), False),
        (0, bytearray(b"AB"), False),
        (2, bytearray(b"CD"), True),
    ]
    assert flashcart.sector_erase_calls == [
        {"pos": 0, "buffer_pos": 0, "skip": False},
        {"pos": 0, "buffer_pos": 0, "skip": False},
    ]
    assert records.status_reads == ["STATUS_REGISTER"]
    assert records.serial.input_resets == 2
    assert records.serial.output_resets == 2
    assert records.cart_writes == [(4, 0xF0, {}), (4, 0xFF, {})]
    assert flashcart.unlock_calls == 1
    assert records.sleep_calls == [0.5]
    assert [event["pos"] for event in records.progress if event["action"] == "ERROR"] == [0]
    records.finish.assert_called_once_with(args, "DMG", preparation, 2)
    assert preparation.verify_sectors == [[0, 4]]
    assert device.cancel is False
    assert device.error is False


def test_write_prepared_flash_rom_stops_after_first_sector_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[4, 4], unlock_results=[True])
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_failure_worker_boundaries(
        device,
        monkeypatch,
        write_results=[False, False],
    )

    result = device._WritePreparedFlashROM({"compare_sectors": False}, "DMG", preparation)

    assert result is None
    assert [(item["address"], item["buffer"]) for item in records.rom_writes] == [
        (0, bytearray(b"AB")),
        (0, bytearray(b"AB")),
    ]
    assert len(flashcart.sector_erase_calls) == 2
    assert flashcart.unlock_calls == 1
    assert records.serial.input_resets == 3
    assert records.serial.output_resets == 3
    assert records.sleep_calls == [0.5]
    assert len([event for event in records.progress if event["action"] == "ERROR"]) == 1
    abort = records.progress[-1]
    assert abort["action"] == "ABORT"
    assert abort["abortable"] is False
    assert abort["info_type"] == "msgbox_critical"
    assert "Status Register:" in str(abort["info_msg"])
    records.finish.assert_not_called()
    assert device.cancel is True
    assert device.error is True
    assert device.cancel_args == {}
    assert device.error_args == {}


def test_write_prepared_flash_rom_bounds_later_sector_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(
        sector_results=[4] * 11,
        unlock_results=[True] * 9,
    )
    sectors = [[0, 4], [4, 4]]
    preparation = make_preparation(
        flashcart=flashcart,
        mbc=mapper,
        data_import=bytearray(b"ABCDEFGH"),
        end_bank=2,
        rom_bank_size=4,
        sector_offsets=sectors,
        write_sectors=sectors,
        buffer_len=2,
    )
    records = install_failure_worker_boundaries(
        device,
        monkeypatch,
        write_results=[None, None, *([False] * 10)],
    )

    result = device._WritePreparedFlashROM({"compare_sectors": False}, "DMG", preparation)

    assert result is None
    assert [(item["address"], item["buffer"]) for item in records.rom_writes[:2]] == [
        (0, bytearray(b"AB")),
        (2, bytearray(b"CD")),
    ]
    assert [(item["address"], item["buffer"]) for item in records.rom_writes[2:]] == [
        (4, bytearray(b"EF")),
    ] * 10
    assert len(flashcart.sector_erase_calls) == 11
    assert flashcart.unlock_calls == 9
    assert records.serial.input_resets == 19
    assert records.serial.output_resets == 19
    assert records.sleep_calls == [0.5] * 9
    assert len([event for event in records.progress if event["action"] == "ERROR"]) == 9
    assert records.progress[-1]["action"] == "ABORT"
    assert records.progress[-1]["info_type"] == "msgbox_critical"
    records.finish.assert_not_called()
    assert device.cancel is True
    assert device.error is True


def test_write_prepared_flash_rom_never_programs_after_sector_erase_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[False, False], unlock_results=[True])
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_failure_worker_boundaries(device, monkeypatch, write_results=[])

    result = device._WritePreparedFlashROM({"compare_sectors": False}, "DMG", preparation)

    assert result is None
    assert records.rom_writes == []
    assert len(flashcart.sector_erase_calls) == 2
    assert flashcart.unlock_calls == 1
    assert records.status_reads == []
    assert records.serial.input_resets == 1
    assert records.serial.output_resets == 1
    assert records.progress[-1]["action"] == "ABORT"
    assert records.progress[-1]["info_type"] == "msgbox_critical"
    records.finish.assert_not_called()


def test_write_prepared_flash_rom_returns_false_when_retry_unlock_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[4], unlock_results=[False])
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_failure_worker_boundaries(device, monkeypatch, write_results=[False])

    result = device._WritePreparedFlashROM({"compare_sectors": False}, "DMG", preparation)

    assert result is False
    assert [(item["address"], item["buffer"]) for item in records.rom_writes] == [
        (0, bytearray(b"AB")),
    ]
    assert len(flashcart.sector_erase_calls) == 1
    assert flashcart.unlock_calls == 1
    assert records.serial.input_resets == 2
    assert records.serial.output_resets == 2
    assert records.sleep_calls == [0.5]
    assert records.progress[-1]["action"] == "ERROR"
    records.finish.assert_not_called()
    assert device.cancel is False
    assert device.error is False


@pytest.mark.parametrize("connection", ["disconnected", "closed"])
def test_write_prepared_flash_rom_aborts_without_retry_when_port_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    connection: str,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[4])
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_failure_worker_boundaries(device, monkeypatch, write_results=[False])
    device.fw = {"fw_ver": 11, "pcb_name": "Test device"}
    if connection == "disconnected":
        device.device = None
    else:
        records.serial.is_open = False

    result = device._WritePreparedFlashROM({"compare_sectors": False}, "DMG", preparation)

    assert result is None
    assert len(records.rom_writes) == 1
    assert len(flashcart.sector_erase_calls) == 1
    assert flashcart.unlock_calls == 0
    assert records.serial.input_resets == 0
    assert records.serial.output_resets == 0
    assert records.sleep_calls == []
    abort = records.progress[-1]
    assert abort["action"] == "ABORT"
    assert abort["abortable"] is False
    assert abort["info_type"] == "msgbox_critical"
    assert "re-connect the device" in str(abort["info_msg"])
    records.finish.assert_not_called()
    assert device.cancel is True
    assert device.error is True


def test_write_prepared_flash_rom_honors_user_cancellation_during_erase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[4])
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_failure_worker_boundaries(device, monkeypatch, write_results=[])
    sector_erase = flashcart.SectorErase

    def canceling_sector_erase(pos: int, buffer_pos: int, skip: bool = False) -> int | bool:
        result = sector_erase(pos, buffer_pos, skip)
        device.cancel = True
        device.cancel_args = {"from_user": True}
        return result

    monkeypatch.setattr(flashcart, "SectorErase", canceling_sector_erase)

    result = device._WritePreparedFlashROM({"compare_sectors": False}, "DMG", preparation)

    assert result is None
    assert len(flashcart.sector_erase_calls) == 1
    assert records.rom_writes == []
    assert flashcart.unlock_calls == 0
    assert records.status_reads == []
    assert records.progress[-1] == {"action": "ABORT", "abortable": False, "from_user": True}
    records.finish.assert_not_called()
    assert preparation.verify_sectors == []


def test_write_prepared_flash_rom_honors_user_cancellation_during_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[4])
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_failure_worker_boundaries(device, monkeypatch, write_results=[None])
    write_rom = device.WriteROM

    def canceling_write(**kwargs: object) -> bool | None:
        result = write_rom(**kwargs)
        device.cancel = True
        device.cancel_args = {"from_user": True}
        return result

    monkeypatch.setattr(device, "WriteROM", canceling_write)

    result = device._WritePreparedFlashROM({"compare_sectors": False}, "DMG", preparation)

    assert result is None
    assert [(item["address"], item["buffer"]) for item in records.rom_writes] == [
        (0, bytearray(b"AB")),
    ]
    assert len(flashcart.sector_erase_calls) == 1
    assert flashcart.unlock_calls == 0
    assert records.status_reads == []
    assert records.progress[-1] == {"action": "ABORT", "abortable": False, "from_user": True}
    records.finish.assert_not_called()
    assert device.cancel is True
    assert device.error is False


def test_write_prepared_flash_rom_runs_real_verified_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.fw = {"fw_ver": 12, "pcb_name": "Test device"}
    device.info["action"] = device.ACTIONS["ROM_WRITE"]
    mapper = SectorMapper(reset_banks={0})
    flashcart = SectorDecisionFlashcart(sector_results=[4])
    state_path = tmp_path / "flash-state.json"
    preparation = make_single_sector_preparation(flashcart, mapper)._replace(
        delta_state=[[0, 4]],
        state_path=state_path,
    )
    real_finish = device._FinishFlashWrite
    records = install_successful_worker_boundaries(device, monkeypatch)
    verify = Mock(return_value=True)
    set_mode = Mock()
    auto_poweroff_finish = Mock()
    monkeypatch.setattr(device, "_FinishFlashWrite", real_finish)
    monkeypatch.setattr(device, "_verify_flash_write", verify)
    monkeypatch.setattr(device, "SetMode", set_mode)
    monkeypatch.setattr(device, "_thread_worker_auto_poweroff_finish", auto_poweroff_finish)
    args = {"compare_sectors": False, "verify_write": True}

    result = device._WritePreparedFlashROM(args, "DMG", preparation)

    assert result is True
    assert [(item["address"], item["buffer"]) for item in records.rom_writes] == [
        (0, bytearray(b"AB")),
        (2, bytearray(b"CD")),
    ]
    verification_context = verify.call_args.args[0]
    assert verification_context.args is args
    assert verification_context.data_import == bytearray(b"ABCD")
    assert verification_context.verify_sectors == [[0, 4]]
    assert verification_context.buffer_len == 2
    assert json.loads(state_path.read_text(encoding="utf-8-sig")) == [[0, 4]]
    assert mapper.reset_requests == [0, 0]
    assert mapper.selected_rom_banks == [0, 0]
    assert records.device_writes == [
        (device.DEVICE_CMD["DMG_MBC_RESET"], True),
        (device.DEVICE_CMD["DMG_MBC_RESET"], True),
    ]
    assert records.firmware_variables == [("DMG_ROM_BANK", 0), ("DMG_ROM_BANK", 0)]
    assert flashcart.reset_calls == [True]
    set_mode.assert_called_once_with("DMG")
    auto_poweroff_finish.assert_called_once_with()
    assert records.progress[-1] == {"action": "FINISHED", "verified": True}
    assert device.info["last_action"] == device.ACTIONS["ROM_WRITE"]
    assert device.info["action"] is None


def test_finish_flash_write_without_verification_reports_unverified_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart()
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_finalization_boundaries(device, monkeypatch)

    result = device._FinishFlashWrite({}, "DMG", preparation, 2)

    assert result is True
    assert records.progress == [
        {"action": "UPDATE_POS", "pos": 4, "force_update": True},
        {"action": "FINISHED", "verified": False},
    ]
    assert flashcart.reset_calls == [True]
    assert mapper.selected_rom_banks == [0]
    assert records.firmware_variables == [("DMG_ROM_BANK", 0)]
    assert records.modes == ["DMG"]
    records.auto_poweroff_finish.assert_called_once_with()


def test_finish_flash_write_mismatch_still_reports_completed_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart()
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_finalization_boundaries(device, monkeypatch)
    verify = Mock(return_value=False)
    monkeypatch.setattr(device, "_verify_flash_write", verify)

    result = device._FinishFlashWrite({"verify_write": True}, "DMG", preparation, 2)

    assert result is True
    verify.assert_called_once()
    assert records.progress[-1] == {"action": "FINISHED", "verified": False}
    assert mapper.selected_rom_banks == [0]
    assert records.modes == ["DMG"]
    records.auto_poweroff_finish.assert_called_once_with()


def test_finish_flash_write_verification_cancellation_stops_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart()
    state_path = tmp_path / "canceled-state.json"
    preparation = make_single_sector_preparation(flashcart, mapper)._replace(
        delta_state=[[0, 4]],
        state_path=state_path,
    )
    records = install_finalization_boundaries(device, monkeypatch)
    monkeypatch.setattr(device, "_verify_flash_write", Mock(return_value=None))

    result = device._FinishFlashWrite({"verify_write": True}, "DMG", preparation, 2)

    assert result is None
    assert records.progress == [{"action": "UPDATE_POS", "pos": 4, "force_update": True}]
    assert not state_path.exists()
    assert flashcart.reset_calls == [True]
    assert mapper.selected_rom_banks == []
    assert records.firmware_variables == []
    assert records.modes == []
    records.auto_poweroff_finish.assert_not_called()
    assert device.info["action"] == device.ACTIONS["ROM_WRITE"]


def test_finish_flash_write_photo_mode_suppresses_completion_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart()
    preparation = make_single_sector_preparation(flashcart, mapper)
    records = install_finalization_boundaries(device, monkeypatch)
    monkeypatch.setattr(device, "_verify_flash_write", Mock(return_value=True))

    result = device._FinishFlashWrite({"photo_mode": True}, "DMG", preparation, 2)

    assert result is True
    assert records.progress == [{"action": "UPDATE_POS", "pos": 4, "force_update": True}]
    assert mapper.selected_rom_banks == [0]
    assert records.modes == ["DMG"]
    records.auto_poweroff_finish.assert_not_called()
    assert device.info["action"] == device.ACTIONS["ROM_WRITE"]
    assert device.info["last_action"] is None


def test_finish_flash_write_hidden_map_failure_stops_before_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    _, callbacks = flashcart_callbacks()
    flashcart = Flashcart_DMG_MMSA(
        flashcart_profile(command_set="GBMEMORY"),
        callbacks,
    )
    erase_hidden_sector = Mock(return_value=True)
    monkeypatch.setattr(flashcart, "EraseHiddenSector", erase_hidden_sector)
    preparation = make_preparation(
        flashcart=flashcart,
        command_set_type="GBMEMORY",
        data_map_import=bytearray(b"hidden map"),
    )
    progress: list[dict[str, object]] = []
    verify = Mock(side_effect=AssertionError("Map failure must stop before verification"))
    write_map = Mock(return_value=False)
    monkeypatch.setattr(device, "SetProgress", lambda event: progress.append(dict(event)))
    monkeypatch.setattr(device, "_verify_flash_write", verify)
    monkeypatch.setattr(device, "WriteROM_GBMEMORY", write_map)

    result = device._FinishFlashWrite({}, "DMG", preparation, 2)

    assert result is False
    erase_hidden_sector.assert_called_once_with(buffer=bytearray(b"hidden map"))
    write_map.assert_called_once_with(
        address=0,
        buffer=bytearray(b"hidden map"),
        bank=1,
    )
    verify.assert_not_called()
    assert progress[0] == {"action": "UPDATE_POS", "pos": 8, "force_update": True}
    assert progress[1]["action"] == "ABORT"
    assert progress[1]["info_type"] == "msgbox_critical"
    assert progress[1]["abortable"] is False


@pytest.mark.parametrize(
    ("delta_state", "chip_erase", "broken_sectors", "expected_write"),
    [
        ([[0, 4], [4, 4]], False, False, True),
        (None, False, False, False),
        ([[0, 4]], True, False, False),
        ([[0, 4]], False, True, False),
    ],
    ids=["applicable", "no-delta", "chip-erased", "broken-sector"],
)
def test_save_flash_delta_state_writes_only_reusable_sector_state(
    tmp_path: Path,
    delta_state: list[list[int]] | None,
    chip_erase: bool,
    broken_sectors: bool,
    expected_write: bool,
) -> None:
    device = GbxDevice()
    state_path = tmp_path / "flash-state.json"
    if broken_sectors:
        device.info["broken_sectors"] = [[0, 4]]

    device._SaveFlashDeltaState(delta_state, chip_erase, state_path)

    assert state_path.exists() is expected_write
    if expected_write:
        assert state_path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert json.loads(state_path.read_text(encoding="utf-8-sig")) == delta_state


@pytest.mark.parametrize(
    ("bank_select_type", "expected_banks"),
    [(0, []), (1, [0])],
    ids=["single-bank", "banked"],
)
def test_restore_first_rom_bank_selects_agb_bank_zero_when_required(
    bank_select_type: int,
    expected_banks: list[int],
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart()

    device._RestoreFirstROMBank(
        mapper,
        {"flash_bank_select_type": bank_select_type},
        flashcart,
    )

    assert mapper.selected_rom_banks == []
    assert flashcart.selected_rom_banks == expected_banks


@pytest.mark.parametrize(
    ("identifier", "expected_match"),
    [
        (bytearray([0x12, 0x34, 0x56, 0x78]), True),
        (bytearray([0x56, 0x78, 0x9A, 0xBC]), False),
    ],
)
def test_probe_bung_16m_flash_cart_restores_write_pin(
    monkeypatch: pytest.MonkeyPatch,
    identifier: bytearray,
    expected_match: bool,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    monkeypatch.setattr(device, "SupportsAudioAsWe", lambda: True)
    reads = Mock(side_effect=[bytearray(b"\x00\x00\x00\x00"), identifier])
    writes = Mock()
    flash_writes = Mock()
    set_audio_pin = Mock()
    set_write_pin = Mock()
    monkeypatch.setattr(device, "_cart_read", reads)
    monkeypatch.setattr(device, "_cart_write", writes)
    monkeypatch.setattr(device, "_cart_write_flash", flash_writes)
    monkeypatch.setattr(device, "_set_we_pin_audio", set_audio_pin)
    monkeypatch.setattr(device, "_set_we_pin_wr", set_write_pin)
    cart_type = {"command_set": "BUNG_16M", "flash_ids": [[0x12, 0x34]]}

    assert device._ProbeSpecialFlashCart(cart_type) is expected_match

    set_audio_pin.assert_called_once_with()
    reads.assert_has_calls([call(0, 4), call(0, 4)])
    expected_writes = [
        call(0x2000, 0x02, flashcart=False),
        call(0x6AAA, 0xAA, flashcart=True),
        call(0x2000, 0x01, flashcart=False),
        call(0x5554, 0x55, flashcart=True),
        call(0x2000, 0x02, flashcart=False),
        call(0x6AAA, 0x90, flashcart=True),
    ]
    if expected_match:
        expected_writes += [
            call(0x2000, 0x02, flashcart=False),
            call(0x6AAA, 0xAA, flashcart=True),
            call(0x2000, 0x01, flashcart=False),
            call(0x5554, 0x55, flashcart=True),
            call(0x2000, 0x02, flashcart=False),
            call(0x6AAA, 0xF0, flashcart=True),
            call(0x2000, 0x00, flashcart=False),
        ]
        set_write_pin.assert_not_called()
        flash_writes.assert_not_called()
    else:
        flash_writes.assert_has_calls(
            [call([[0, 0xFF]], flashcart=True), call([[0, 0xF0]], flashcart=True)],
        )
        set_write_pin.assert_called_once_with()
    assert writes.call_args_list == expected_writes


@pytest.mark.parametrize(
    ("identifier", "expected_match"),
    [
        (bytearray([0x12, 0x34, 0x56, 0x78]), True),
        (bytearray([0x56, 0x78, 0x9A, 0xBC]), False),
    ],
)
def test_probe_datel_orbit_v2_preserves_command_order(
    monkeypatch: pytest.MonkeyPatch,
    identifier: bytearray,
    expected_match: bool,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    address = 0x1234
    unlock_reads = [[0x111, 0x00], [0x222, 0x00]]
    commands = {
        "unlock_read": unlock_reads,
        "unlock": [[0x5555, 0xAA]],
        "read_identifier": [[0x5555, 0x90]],
        "reset": [[0, 0xF0]],
    }
    cart_type = {
        "command_set": "DATEL_ORBITV2",
        "read_identifier_at": address,
        "flash_ids": [[0x12, 0x34]],
        "commands": commands,
    }
    rom1 = bytearray(b"\x00" * 10)
    reads = Mock(side_effect=[rom1, bytearray(1), bytearray(1), identifier])
    writes = Mock()
    monkeypatch.setattr(device, "_cart_read", reads)
    monkeypatch.setattr(device, "_cart_write_flash", writes)

    assert device._ProbeSpecialFlashCart(cart_type) is expected_match

    reads.assert_has_calls([call(address, 10), call(0x111, 1), call(0x222, 1), call(address, 10)])
    expected_writes = [call(commands["unlock"]), call(commands["read_identifier"])]
    if expected_match:
        expected_writes.append(call(commands["reset"]))
    assert writes.call_args_list == expected_writes


@pytest.mark.parametrize(
    ("supports_audio", "identifier", "expected_match"),
    [
        (True, bytearray([0x12, 0x34, 0x56, 0x78]), True),
        (True, bytearray([0x56, 0x78, 0x9A, 0xBC]), False),
        (False, bytearray([0x12, 0x34, 0x56, 0x78]), None),
    ],
)
def test_probe_dmg_mbc5_flash_cart_preserves_probe_and_recovery_order(
    monkeypatch: pytest.MonkeyPatch,
    supports_audio: bool,
    identifier: bytearray,
    expected_match: bool | None,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    monkeypatch.setattr(device, "SupportsAudioAsWe", lambda: supports_audio)
    reads = Mock(side_effect=[bytearray(b"\x00" * 8), identifier])
    flash_writes = Mock()
    set_audio_pin = Mock()
    set_write_pin = Mock()
    monkeypatch.setattr(device, "_cart_read", reads)
    monkeypatch.setattr(device, "_cart_write_flash", flash_writes)
    monkeypatch.setattr(device, "_set_we_pin_audio", set_audio_pin)
    monkeypatch.setattr(device, "_set_we_pin_wr", set_write_pin)
    cart_type = {
        "command_set": "GENERIC",
        "dmg-mbc5-32m-flash": True,
        "flash_ids": [[0x12, 0x34]],
        "commands": {
            "unlock": [[0x5555, 0xAA]],
            "reset": [[0, 0xF0]],
            "read_identifier": [[0x5555, 0x90]],
        },
    }

    assert device._ProbeSpecialFlashCart(cart_type) is expected_match

    if expected_match is None:
        reads.assert_not_called()
        flash_writes.assert_not_called()
        set_audio_pin.assert_not_called()
        set_write_pin.assert_not_called()
    else:
        set_audio_pin.assert_called_once_with()
        reads.assert_has_calls([call(0, 8), call(0, 8)])
        expected_calls = [
            call(cart_type["commands"]["unlock"], flashcart=False),
            call(cart_type["commands"]["reset"]),
            call(cart_type["commands"]["read_identifier"]),
        ]
        if expected_match:
            expected_calls.append(call(cart_type["commands"]["reset"]))
            set_write_pin.assert_not_called()
        else:
            expected_calls.extend([call([[0, 0xFF]], flashcart=True), call([[0, 0xF0]], flashcart=True)])
            set_write_pin.assert_called_once_with()
        assert flash_writes.call_args_list == expected_calls
