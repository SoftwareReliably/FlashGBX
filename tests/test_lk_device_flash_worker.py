"""Flash-write chunk routing and sector-range contracts."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.LK_Device import _FlashChunkParameters, _FlashSectorState

CHUNK_WRITERS = (
    "WriteROM",
    "WriteROM_GBMEMORY",
    "WriteROM_DMG_MBC5_32M_FLASH",
    "WriteROM_DMG_EEPROM",
    "WriteROM_DMG_DatelOrbitV2",
)


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
