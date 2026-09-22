"""Flash-write chunk routing and sector-range contracts."""

from __future__ import annotations

from unittest.mock import Mock, call

import pytest

import FlashGBX.LK_Device as lk_device_module
from FlashGBX.Flashcart import Flashcart
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
        pulse_reset: bool = False,
        commands_on_bank_one: bool = False,
    ) -> None:
        self.CONFIG = dict(config or {"command_set": "AMD"})
        self.sector_results = list(sector_results or [])
        self.pulse_reset = pulse_reset
        self.commands_on_bank_one = commands_on_bank_one
        self.reset_calls: list[bool] = []
        self.selected_rom_banks: list[int] = []
        self.sector_erase_calls: list[dict[str, object]] = []
        self.physical_erase_calls: list[dict[str, object]] = []

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
    device.FW = {"fw_ver": 12}
    device.SKIPPING = skipping
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
        max_length=device.MAX_BUFFER_WRITE,
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
    device.FW = {"fw_ver": firmware}
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
    device.FW = {"fw_ver": 12}
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
    device.MODE = mode  # type: ignore[assignment]

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
    device.MODE = "DMG"
    device.FW = {"fw_ver": 12}
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
    device.MODE = "AGB"
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
    device.FW = {"fw_ver": firmware}
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
    device.MODE = "DMG"
    device.FW = {"fw_ver": 12}
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
    assert device.NO_PROG_UPDATE is False


def test_try_skip_matching_flash_sector_keeps_original_position_on_late_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.FW = {"fw_ver": 12}
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
    device.MODE = "AGB"
    device.FW = {"fw_ver": 12}
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
    device.MODE = "DMG"
    device.FW = {"fw_ver": 12}
    mapper = SectorMapper()
    flashcart = SectorDecisionFlashcart(sector_results=[8])
    preparation = make_preparation(mbc=mapper, flashcart=flashcart)
    compare_results = iter([True, True])

    def compare(**_kwargs: object) -> bool:
        result = next(compare_results)
        if mapper.selected_rom_banks == [0, 1]:
            device.CANCEL = True
            device.CANCEL_ARGS = {"from_user": True}
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
    assert device.CANCEL_ARGS == {}
    assert device.NO_PROG_UPDATE is False


@pytest.mark.parametrize("already_listed", [False, True], ids=["add-verification", "avoid-duplicate"])
def test_erase_flash_sector_runs_once_at_boundary_and_updates_next_size(
    monkeypatch: pytest.MonkeyPatch,
    already_listed: bool,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
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
    assert device.NO_PROG_UPDATE is False


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
    assert device.NO_PROG_UPDATE is False


def test_erase_flash_sector_propagates_user_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.CANCEL_ARGS = {"from_user": True}
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
    assert device.NO_PROG_UPDATE is False
