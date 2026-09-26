"""Tests for save-write readback verification."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, call

import pytest

from FlashGBX.hw_GBxCartRW import GbxDevice


class SaveMapper:
    """Minimal save mapper used by the verification boundary."""

    def __init__(self, name: str = "MBC3") -> None:
        self.name = name
        self.selected_banks: list[int] = []

    def SelectBankRAM(self, bank: int) -> None:
        self.selected_banks.append(bank)

    def GetName(self) -> str:
        return self.name


def configure_readback(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    readback: object,
    *,
    backup_result: bool = True,
) -> tuple[Mock, Mock, list[dict[str, Any]]]:
    """Mock the transfer boundary while preserving the verification method."""
    progress = Mock()
    read_rom = Mock(return_value=bytearray(4))
    backup_calls: list[dict[str, Any]] = []

    def backup(args: dict[str, Any]) -> bool:
        backup_calls.append(args)
        device.info["data"] = readback
        return backup_result

    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "ReadROM", read_rom)
    monkeypatch.setattr(device, "_BackupRestoreRAM", backup)
    return progress, read_rom, backup_calls


@pytest.mark.parametrize("readback_type", [bytes, bytearray, memoryview])
def test_matching_save_readback_returns_true_and_preserves_request(
    monkeypatch: pytest.MonkeyPatch,
    readback_type: type,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveMapper()
    buffer = bytearray(b"SAVE DATA")
    readback = readback_type(buffer)
    progress, read_rom, backup_calls = configure_readback(device, monkeypatch, readback)
    args: dict[str, Any] = {"path": "original.sav", "save_type": 1}

    result = device._VerifySaveWrite(args, mapper, buffer, len(buffer))

    assert result is True
    assert mapper.selected_banks == [0]
    assert args["path"] == "original.sav"
    progress.assert_called_once_with(
        {"action": "INITIALIZE", "method": "SAVE_WRITE_VERIFY", "size": len(buffer)},
    )
    read_rom.assert_called_once_with(0, 4)
    assert len(backup_calls) == 1
    verify_args = backup_calls[0]
    assert verify_args is not args
    assert verify_args["mode"] == 2
    assert verify_args["verify_write"] is buffer
    assert verify_args["path"] is None


def test_failed_readback_transfer_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveMapper()
    buffer = bytearray(b"SAVE")
    progress, read_rom, backup_calls = configure_readback(
        device,
        monkeypatch,
        bytearray(),
        backup_result=False,
    )
    args: dict[str, Any] = {"path": "original.sav", "save_type": 1}

    assert device._VerifySaveWrite(args, mapper, buffer, len(buffer)) is None

    assert args["path"] == "original.sav"
    assert mapper.selected_banks == [0]
    progress.assert_called_once_with(
        {"action": "INITIALIZE", "method": "SAVE_WRITE_VERIFY", "size": len(buffer)},
    )
    read_rom.assert_called_once_with(0, 4)
    assert backup_calls[0]["path"] is None


@pytest.mark.parametrize("readback", [None, object(), "not bytes", 123])
def test_missing_or_invalid_readback_returns_false(
    monkeypatch: pytest.MonkeyPatch,
    readback: object,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveMapper()
    buffer = bytearray(b"SAVE")
    progress, _, _ = configure_readback(device, monkeypatch, readback)
    args: dict[str, Any] = {"path": "original.sav", "save_type": 1}

    assert device._VerifySaveWrite(args, mapper, buffer, len(buffer)) is False

    assert args["path"] == "original.sav"
    assert progress.call_args_list == [
        call({"action": "INITIALIZE", "method": "SAVE_WRITE_VERIFY", "size": len(buffer)}),
    ]


def test_mbc2_verification_compares_only_low_nibbles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveMapper("MBC2")
    buffer = bytearray([0xF1, 0xA2, 0x73, 0x44])
    readback = bytearray([0x01, 0x12, 0xE3, 0x94])
    progress, _, _ = configure_readback(device, monkeypatch, readback)
    args: dict[str, Any] = {"path": "mbc2.sav", "save_type": 1}

    assert device._VerifySaveWrite(args, mapper, buffer, len(buffer)) is True

    assert buffer == bytearray([1, 2, 3, 4])
    assert mapper.selected_banks == [0]
    assert progress.call_count == 1


def test_dacs_verification_ignores_read_only_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    mapper = SaveMapper()
    buffer = bytearray(0xFE100)
    readback = bytearray(buffer)
    readback[0xFE000] = 0xFF
    progress, _, _ = configure_readback(device, monkeypatch, readback)
    args: dict[str, Any] = {"path": "dacs.sav", "save_type": 6}

    assert device._VerifySaveWrite(args, mapper, buffer, len(buffer)) is True

    assert mapper.selected_banks == []
    progress.assert_called_once_with(
        {"action": "INITIALIZE", "method": "SAVE_WRITE_VERIFY", "size": len(buffer)},
    )


def test_ereader_calibration_blocks_are_preserved_from_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    device.info["ereader"] = True
    mapper = SaveMapper()
    buffer = bytearray(0x20000)
    readback = bytearray(buffer)
    calibration = bytes([0xA5] * 0x80)
    readback[0xFF80:0x10000] = calibration
    readback[0x1FF80:0x20000] = calibration
    configure_readback(device, monkeypatch, readback)
    args: dict[str, Any] = {"path": "ereader.sav", "save_type": 1}

    assert device._VerifySaveWrite(args, mapper, buffer, len(buffer)) is True

    assert buffer[0xFF80:0x10000] == calibration
    assert buffer[0x1FF80:0x20000] == calibration


def test_mismatch_report_caps_details_and_counts_all_differences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveMapper()
    buffer = bytearray(12)
    readback = bytearray(range(1, 13))
    progress, _, _ = configure_readback(device, monkeypatch, readback)
    args: dict[str, Any] = {"path": "mismatch.sav", "save_type": 1}

    assert device._VerifySaveWrite(args, mapper, buffer, len(buffer)) is False

    assert args["path"] == "mismatch.sav"
    assert progress.call_count == 2
    abort = progress.call_args_list[-1].args[0]
    assert abort["action"] == "ABORT"
    assert abort["info_type"] == "msgbox_critical"
    assert abort["abortable"] is False
    message = abort["info_msg"]
    assert "12 bytes (100.00%)" in message
    assert message.count("≠") == 10
    assert "more than 10 differences found" in message
    assert "0x000009" in message
    assert "0x00000A" not in message


@pytest.mark.parametrize(
    ("readback", "count", "percent", "first_missing"),
    [
        (bytearray(), 4, "100.00", 0),
        (bytearray(b"\x10\x20"), 2, "50.00", 2),
    ],
    ids=["empty", "truncated"],
)
def test_short_readback_counts_missing_bytes_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
    readback: bytearray,
    count: int,
    percent: str,
    first_missing: int,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveMapper()
    buffer = bytearray(b"\x10\x20\x30\x40")
    progress, _, _ = configure_readback(device, monkeypatch, readback)
    args: dict[str, Any] = {"path": "short.sav", "save_type": 1}

    assert device._VerifySaveWrite(args, mapper, buffer, len(buffer)) is False

    abort = progress.call_args_list[-1].args[0]
    message = abort["info_msg"]
    assert f"{count} bytes ({percent}%)" in message
    assert f"0x{first_missing:06X}: --≠" in message
    assert args["path"] == "short.sav"
