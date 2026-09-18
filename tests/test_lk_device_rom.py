"""Tests for ROM-read boundaries, recovery, verification, and checksums."""

from __future__ import annotations

import hashlib
import io
import zlib
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, call

import pytest

import FlashGBX.LK_Device as lk_device_module
from FlashGBX.app import AppContext
from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.RomFileAGB import RomFileAGB
from FlashGBX.RomFileDMG import RomFileDMG

if TYPE_CHECKING:
    from pathlib import Path


class ChecksumMapper:
    """Small mapper double for header and checksum processing."""

    def __init__(self, name: str = "MBC3", checksum: int = 0x1234) -> None:
        self.name = name
        self.checksum = checksum
        self.checksum_inputs: list[bytes] = []

    def GetName(self) -> str:
        return self.name

    def CalcChecksum(self, buffer: bytearray) -> int:
        self.checksum_inputs.append(bytes(buffer))
        return self.checksum


class ReadLoopMapper:
    """Mapper boundary used while retaining the real backup read loop."""

    def __init__(self, bank_size: int) -> None:
        self.bank_size = bank_size
        self.selected_banks: list[int] = []

    def ResetBeforeBankChange(self, _bank: int) -> bool:
        return False

    def SelectBankROM(self, bank: int) -> tuple[int, int]:
        self.selected_banks.append(bank)
        return bank * self.bank_size, self.bank_size

    def GetROMBankSize(self) -> int:
        return self.bank_size


class FailingClose:
    """File-like boundary that reports a close failure."""

    def close(self) -> None:
        msg = "close failed"
        raise OSError(msg)


@pytest.mark.parametrize(
    ("args", "expected", "expected_bl_size"),
    [
        ({}, (0, 0, 0x8000, 0, 2), None),
        (
            {"verify_write": bytearray(0x800), "verify_from": 0x1000, "verify_len": 0x800},
            (0x1000, 0x1000, 0x1800, 0, 1),
            None,
        ),
        (
            {"verify_write": bytearray(0x200), "verify_from": 0x3F00, "verify_len": 0x200},
            (0x3F00, 0x3F00, 0x4100, 0, 2),
            None,
        ),
        ({"bl_offset": 0x2000, "bl_size": 0x1000, "bl_layout": 0}, (0x2000, 0x2000, 0x3000, 0, 1), 0x1000),
        ({"bl_offset": 0x2000, "bl_size": 0x1000, "bl_layout": 1}, (0x2000, 0x2000, 0x4000, 0, 1), 0x2000),
        ({"bl_offset": 0x5000, "bl_size": 0x1000, "bl_layout": 2}, (0x5000, 0x5000, 0x7000, 1, 2), 0x2000),
    ],
)
def test_rom_read_ranges_cover_normal_verification_and_batteryless_layouts(
    args: dict[str, Any],
    expected: tuple[int, int, int, int, int],
    expected_bl_size: int | None,
) -> None:
    result = GbxDevice._GetROMReadRange(args, size=0x8000, rom_bank_size=0x4000, rom_banks=2)

    assert tuple(result) == expected
    if expected_bl_size is not None:
        assert args["bl_size"] == expected_bl_size


def test_write_rom_backup_chunk_selects_requested_batteryless_half() -> None:
    chunk = bytearray([0x11] * 0x2000 + [0x22] * 0x2000)

    ordinary = io.BytesIO()
    GbxDevice._WriteROMBackupChunk(ordinary, {}, chunk)
    assert ordinary.getvalue() == chunk

    first_half = io.BytesIO()
    GbxDevice._WriteROMBackupChunk(first_half, {"bl_layout": 1}, chunk)
    assert first_half.getvalue() == bytes([0x11] * 0x2000)

    second_half = io.BytesIO()
    GbxDevice._WriteROMBackupChunk(second_half, {"bl_layout": 2}, chunk)
    assert second_half.getvalue() == bytes([0x22] * 0x2000)

    GbxDevice._WriteROMBackupChunk(None, {"bl_layout": 1}, chunk)


def test_open_rom_backup_file_supports_memory_only_and_disk_outputs(tmp_path: Path) -> None:
    assert GbxDevice._OpenROMBackupFile("") is None
    path = tmp_path / "dump.gb"
    output = GbxDevice._OpenROMBackupFile(str(path))
    assert output is not None
    output.write(b"ROM")
    output.close()
    assert path.read_bytes() == b"ROM"


@pytest.mark.parametrize(
    ("max_length", "position", "lives", "expected_length", "expected_lives"),
    [
        (256, 0, 10, 128, 9),
        (64, 0, 10, 64, 9),
        (256, 0x40, 20, 256, 19),
    ],
)
def test_incomplete_read_adjusts_buffer_and_preserves_retry_floor(
    monkeypatch: pytest.MonkeyPatch,
    max_length: int,
    position: int,
    lives: int,
    expected_length: int,
    expected_lives: int,
) -> None:
    device = GbxDevice()
    serial_device = Mock()
    device.DEVICE = serial_device
    device.MAX_BUFFER_READ = 512
    progress = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    context = lk_device_module._IncompleteROMReadContext({}, 0x100, position, max_length, lives)

    result = device._HandleIncompleteROMRead(context)

    assert result == (expected_length, expected_lives, False)
    assert device.INFO["dump_info"]["transfer_size"] == expected_length
    assert device.CANCEL is False
    assert device.ERROR is False
    progress.assert_called_once_with({"action": "UPDATE_POS", "pos": position})
    serial_device.reset_input_buffer.assert_called_once_with()
    serial_device.reset_output_buffer.assert_called_once_with()
    if max_length == 256 and position == 0:
        assert device.MAX_BUFFER_READ == 128
    else:
        assert device.MAX_BUFFER_READ == 512


@pytest.mark.parametrize("verification", [False, True])
def test_incomplete_read_exhaustion_sets_cancel_and_verification_result(
    monkeypatch: pytest.MonkeyPatch,
    verification: bool,
) -> None:
    device = GbxDevice()
    serial_device = Mock()
    device.DEVICE = serial_device
    progress = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    args: dict[str, Any] = {}
    expected_position = 0x20
    if verification:
        args.update({"verify_write": bytearray(4), "verify_base_pos": 0x100, "verify_from": 0x200})
        expected_position = 0x220
    context = lk_device_module._IncompleteROMReadContext(args, 4, 0x20, 128, 1)

    result = device._HandleIncompleteROMRead(context)

    assert result == (64, 0, verification)
    assert device.CANCEL is True
    assert device.ERROR is True
    assert device.CANCEL_ARGS["info_type"] == "msgbox_critical"
    assert "error occured while reading" in device.CANCEL_ARGS["info_msg"]
    progress.assert_called_once_with({"action": "UPDATE_POS", "pos": expected_position})
    serial_device.reset_input_buffer.assert_called_once_with()
    serial_device.reset_output_buffer.assert_called_once_with()


def test_abort_rom_read_noops_until_canceled(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    output = io.BytesIO()
    progress = Mock()
    power_cycle = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "CanPowerCycleCart", Mock(return_value=True))
    monkeypatch.setattr(device, "CartPowerCycle", power_cycle)

    assert device._AbortROMReadIfCanceled(output) is False

    assert output.closed is False
    progress.assert_not_called()
    power_cycle.assert_not_called()


@pytest.mark.parametrize("can_power_cycle", [False, True])
def test_abort_rom_read_closes_output_clears_metadata_and_optionally_cycles_power(
    monkeypatch: pytest.MonkeyPatch,
    can_power_cycle: bool,
) -> None:
    device = GbxDevice()
    output = io.BytesIO()
    progress = Mock()
    power_cycle = Mock()
    device.CANCEL = True
    device.CANCEL_ARGS = {"from_user": True, "reason": "stop"}
    device.ERROR_ARGS = {"iteration": 2}
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "CanPowerCycleCart", Mock(return_value=can_power_cycle))
    monkeypatch.setattr(device, "CartPowerCycle", power_cycle)

    assert device._AbortROMReadIfCanceled(output) is True

    assert output.closed is True
    assert device.CANCEL_ARGS == {}
    assert device.ERROR_ARGS == {}
    progress.assert_called_once_with(
        {"action": "ABORT", "abortable": False, "from_user": True, "reason": "stop"},
    )
    assert power_cycle.call_count == int(can_power_cycle)


def test_abort_rom_read_logs_close_failure_and_still_cycles_power(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    log_exception = Mock()
    power_cycle = Mock()
    device.CANCEL = True
    monkeypatch.setattr(device, "SetProgress", Mock())
    monkeypatch.setattr(device, "CanPowerCycleCart", Mock(return_value=True))
    monkeypatch.setattr(device, "CartPowerCycle", power_cycle)
    monkeypatch.setattr(lk_device_module.logger, "exception", log_exception)

    assert device._AbortROMReadIfCanceled(FailingClose()) is True

    log_exception.assert_called_once_with("Failed to close the canceled ROM-read output file")
    power_cycle.assert_called_once_with()


@pytest.mark.parametrize("mismatch", [0, 2, 3], ids=["first", "interior", "final"])
def test_verify_rom_chunk_reports_exact_mismatch_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: int,
) -> None:
    device = GbxDevice()
    expected = bytearray(b"ABCD")
    actual = bytearray(expected)
    actual[mismatch] ^= 0xFF
    progress = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(AppContext, "DEBUG", False)
    args = {"verify_write": expected, "verify_from": 0x100, "rtc_area": False}

    assert device._VerifyROMReadChunk(args, actual, actual, len(actual)) == mismatch

    progress.assert_not_called()


def test_verify_rom_chunk_accepts_match_and_updates_absolute_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    data = bytearray(b"ABCDEFGH")
    progress = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    args = {"verify_write": data, "verify_from": 0x200, "rtc_area": False}

    result = device._VerifyROMReadChunk(args, data[4:8], data, 8)

    assert result is None
    progress.assert_called_once_with({"action": "UPDATE_POS", "pos": 0x208})


def test_verify_rom_chunk_ignores_only_rtc_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    expected = bytearray(0xCA)
    actual = bytearray(expected)
    actual[0xC4:0xCA] = b"\x01\x02\x03\x04\x05\x06"
    progress = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    args = {"verify_write": expected, "verify_from": 0, "rtc_area": True}

    assert device._VerifyROMReadChunk(args, actual, actual, len(actual)) is None
    progress.assert_called_once_with({"action": "UPDATE_POS", "pos": len(actual)})

    actual[0xC3] = 1
    progress.reset_mock()
    assert device._VerifyROMReadChunk(args, actual, actual, len(actual)) == 0xC3
    progress.assert_not_called()


def test_dmg_checksums_use_generated_header_and_independent_hashes(
    pokemon_red_header: bytearray,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.INFO["dump_info"] = {}
    progress = Mock()
    mapper = ChecksumMapper(checksum=0xBEEF)
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(RomFileDMG, "GetDatabaseEntry", lambda _self: None)
    buffer = bytearray(pokemon_red_header)

    device._CalculateROMChecksums(buffer, None, mapper)

    assert mapper.checksum_inputs == [bytes(buffer)]
    assert device.INFO["rom_checksum_calc"] == 0xBEEF
    assert device.INFO["dump_info"]["header"]["game_title"] == "POKEMON RED"
    assert_hash_metadata(device, buffer)
    assert [item.args[0]["type"] for item in progress.call_args_list[1:]] == [
        "ROM checksum",
        "MD5 hash",
        "SHA-1 hash",
        "SHA-256 hash",
        "CRC32 checksum",
    ]


@pytest.mark.parametrize(
    ("save_library", "expected_library", "expected_flash_id"),
    [
        (None, "N/A", None),
        (b"FLASH1M_V123\x00", "FLASH1M_V123", (0x1234, "Mock flash")),
        (b"malformed", "N/A", None),
    ],
)
def test_agb_checksum_save_library_detection(
    monkeypatch: pytest.MonkeyPatch,
    save_library: bytes | None,
    expected_library: str,
    expected_flash_id: tuple[int, str] | None,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.INFO["dump_info"] = {}
    mapper = ChecksumMapper()
    read_flash_id = Mock(return_value=(0x1234, "Mock flash"))
    monkeypatch.setattr(device, "SetProgress", Mock())
    monkeypatch.setattr(device, "ReadFlashSaveID", read_flash_id)
    monkeypatch.setattr(RomFileAGB, "GetDatabaseEntry", lambda _self: None)
    buffer = make_agb_buffer()
    if save_library == b"malformed":
        buffer[0x180:0x1A0] = b"A" * 0x20
        buffer[0x180:0x187] = b"FLASH_V"
    elif save_library is not None:
        buffer[0x180 : 0x180 + len(save_library)] = save_library

    device._CalculateROMChecksums(buffer, None, mapper)

    assert device.INFO["dump_info"]["agb_savelib"] == expected_library
    assert device.INFO["dump_info"]["agb_save_flash_id"] == expected_flash_id
    assert read_flash_id.call_count == int(expected_flash_id is not None)
    assert_hash_metadata(device, buffer)


def make_agb_buffer() -> bytearray:
    """Create a minimal generated AGB header and room for save signatures."""
    buffer = bytearray(0x240)
    buffer[0xA0:0xAC] = b"TEST GAME".ljust(12, b"\x00")
    buffer[0xAC:0xB0] = b"ABCD"
    buffer[0xB0:0xB2] = b"01"
    buffer[0xB2] = 0x96
    buffer[0xBD] = RomFileAGB(buffer).CalcChecksumHeader()
    return buffer


def assert_hash_metadata(device: GbxDevice, buffer: bytearray) -> None:
    """Assert independently calculated file and report hashes."""
    expected = {
        "file_md5": hashlib.md5(buffer).hexdigest(),
        "file_sha1": hashlib.sha1(buffer).hexdigest(),
        "file_sha256": hashlib.sha256(buffer).hexdigest(),
        "file_crc32": zlib.crc32(buffer) & 0xFFFFFFFF,
    }
    assert {key: device.INFO[key] for key in expected} == expected
    assert device.INFO["dump_info"]["hash_md5"] == expected["file_md5"]
    assert device.INFO["dump_info"]["hash_sha1"] == expected["file_sha1"]
    assert device.INFO["dump_info"]["hash_sha256"] == expected["file_sha256"]
    assert device.INFO["dump_info"]["hash_crc32"] == expected["file_crc32"]


def configure_small_backup(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    mapper: ReadLoopMapper,
    size: int,
    buffer_len: int,
) -> tuple[list[dict[str, Any]], Mock, Mock]:
    """Configure only the hardware boundaries around the real backup loop."""
    progress: list[dict[str, Any]] = []
    process_result = Mock(return_value=True)
    reset_state = Mock()
    device.MODE = "DMG"
    device.FW = {"fw_ver": 9, "pcb_name": "Test", "pcb_ver": 1}  # type: ignore[typeddict-item]
    device.INFO["dump_info"] = {}
    device.MAX_BUFFER_READ = 128
    monkeypatch.setattr(device, "_PrepareBackupFlashcart", Mock(return_value=({}, False)))
    monkeypatch.setattr(device, "_apply_legacy_flashcart_compatibility", Mock())
    monkeypatch.setattr(
        device,
        "_PrepareROMRead",
        Mock(
            return_value=lk_device_module._ROMReadConfiguration(
                mapper,
                size,
                1,
                size,
                buffer_len,
                False,
            ),
        ),
    )
    monkeypatch.setattr(device, "_configure_rom_read_pullups", Mock())
    monkeypatch.setattr(device, "_process_rom_backup_result", process_result)
    monkeypatch.setattr(device, "_ResetROMReadState", reset_state)
    monkeypatch.setattr(device, "_thread_worker_auto_poweroff_finish", Mock())
    monkeypatch.setattr(device, "SetProgress", lambda event: progress.append(dict(event)))
    return progress, process_result, reset_state


def test_backup_rom_worker_reads_bounded_chunks_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    mapper = ReadLoopMapper(bank_size=8)
    progress, process_result, reset_state = configure_small_backup(device, monkeypatch, mapper, 8, 4)
    read_rom = Mock(side_effect=[bytearray(b"ABCD"), bytearray(b"EFGH")])
    monkeypatch.setattr(device, "ReadROM", read_rom)
    path = tmp_path / "tiny.gb"
    args = {"path": str(path), "rom_size": 8, "cart_type": 0, "mbc": 0}

    assert device._BackupROM_Worker(args) is True

    assert path.read_bytes() == b"ABCDEFGH"
    assert read_rom.call_args_list == [
        call(address=0, length=4, skip_init=False, max_length=128),
        call(address=4, length=4, skip_init=True, max_length=128),
    ]
    assert mapper.selected_banks == [0]
    assert [event.get("pos") for event in progress if event["action"] == "UPDATE_POS"] == [4, 8]
    assert progress[0] == {"action": "INITIALIZE", "method": "ROM_READ", "size": 8}
    assert progress[-1] == {"action": "FINISHED"}
    process_result.assert_called_once()
    assert process_result.call_args.args[1] == bytearray(b"ABCDEFGH")
    reset_state.assert_called_once_with(mapper, {}, False, 1, 0)
    assert device.INFO["last_action"] == device.ACTIONS["ROM_READ"]
    assert device.INFO["action"] is None
    assert device.INFO["last_path"] == str(path)


def test_backup_rom_worker_short_read_then_cancel_closes_empty_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    mapper = ReadLoopMapper(bank_size=8)
    progress, process_result, reset_state = configure_small_backup(device, monkeypatch, mapper, 8, 4)
    serial_device = Mock()
    device.DEVICE = serial_device
    monkeypatch.setattr(device, "CanPowerCycleCart", Mock(return_value=False))

    def short_read(**_kwargs: object) -> bytearray:
        device.CANCEL = True
        device.CANCEL_ARGS = {"from_user": True}
        return bytearray(b"AB")

    read_rom = Mock(side_effect=short_read)
    monkeypatch.setattr(device, "ReadROM", read_rom)
    path = tmp_path / "canceled.gb"
    args = {"path": str(path), "rom_size": 8, "cart_type": 0, "mbc": 0}

    assert device._BackupROM_Worker(args) is None

    assert path.read_bytes() == b""
    read_rom.assert_called_once_with(address=0, length=4, skip_init=False, max_length=128)
    assert [event["action"] for event in progress] == ["INITIALIZE", "UPDATE_POS", "ABORT"]
    assert progress[1]["pos"] == 0
    assert progress[2] == {"action": "ABORT", "abortable": False, "from_user": True}
    serial_device.reset_input_buffer.assert_called_once_with()
    serial_device.reset_output_buffer.assert_called_once_with()
    process_result.assert_not_called()
    reset_state.assert_not_called()
