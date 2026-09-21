"""Tests for save-transfer input preparation and configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.LK_Device import _SaveTransferActionParameters

if TYPE_CHECKING:
    from pathlib import Path


class ActionMapper:
    """Minimal mapper exposing the name used by erase preparation."""

    def __init__(self, name: str = "MBC3") -> None:
        self.name = name

    def GetName(self) -> str:
        return self.name


def prepare_action(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    args: dict[str, Any],
    *,
    save_size: int = 4,
    empty_data_byte: int = 0,
    ram_banks: int = 2,
    extra_size: int = 0,
    mapper: ActionMapper | None = None,
) -> tuple[tuple[bytearray, int, int], list[dict[str, Any]]]:
    """Call the real preparation helper with a recording progress boundary."""
    progress: list[dict[str, Any]] = []
    monkeypatch.setattr(device, "SetProgress", lambda event: progress.append(dict(event)))
    parameters = _SaveTransferActionParameters(
        mbc=mapper,
        save_size=save_size,
        empty_data_byte=empty_data_byte,
        ram_banks=ram_banks,
        extra_size=extra_size,
    )
    return device._PrepareSaveTransferAction(args, parameters), progress


def test_backup_initializes_empty_buffer_and_total_size(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    args = {"mode": 2, "save_type": 1}

    result, progress = prepare_action(
        device,
        monkeypatch,
        args,
        save_size=8,
        ram_banks=3,
        extra_size=16,
    )

    assert result == (bytearray(), 3, 8)
    assert device.INFO["action"] == device.ACTIONS["SAVE_READ"]
    assert progress == [{"action": "INITIALIZE", "method": "SAVE_READ", "size": 24}]


def test_verification_only_backup_suppresses_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.INFO["action"] = "existing action"
    args = {"mode": 2, "save_type": 3, "verify_write": bytearray(b"expected")}

    result, progress = prepare_action(device, monkeypatch, args, save_size=8, ram_banks=1, extra_size=4)

    assert result == (bytearray(), 1, 8)
    assert device.INFO["action"] == "existing action"
    assert progress == []


@pytest.mark.parametrize(
    ("source", "same_object"),
    [
        (b"DATA", False),
        (bytearray(b"DATA"), True),
        (memoryview(b"DATA"), False),
    ],
    ids=["bytes", "bytearray", "memoryview"],
)
def test_restore_accepts_each_bytes_like_source(
    monkeypatch: pytest.MonkeyPatch,
    source: bytes | bytearray | memoryview,
    same_object: bool,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    args = {"mode": 3, "save_type": 1, "erase": False, "path": None, "buffer": source}

    (buffer, ram_banks, save_size), progress = prepare_action(device, monkeypatch, args)

    assert buffer == bytearray(b"DATA")
    assert (buffer is source) is same_object
    assert (ram_banks, save_size) == (2, 4)
    assert device.INFO["save_erase"] is False
    assert device.INFO["action"] == device.ACTIONS["SAVE_WRITE"]
    assert progress == [{"action": "INITIALIZE", "method": "SAVE_WRITE", "size": 4}]


def test_restore_uses_info_data_when_buffer_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.INFO["data"] = b"INFO"
    args = {"mode": 3, "save_type": 1, "erase": False, "path": None}

    (buffer, _, save_size), _ = prepare_action(device, monkeypatch, args)

    assert buffer == bytearray(b"INFO")
    assert save_size == 4


def test_restore_reads_the_selected_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "selected.sav"
    path.write_bytes(b"FILE")
    device = GbxDevice()
    device.MODE = "DMG"
    device.INFO["data"] = b"INFO"
    args = {
        "mode": 3,
        "save_type": 1,
        "erase": False,
        "path": str(path),
        "buffer": b"ARGS",
    }

    (buffer, _, save_size), _ = prepare_action(device, monkeypatch, args)

    assert buffer == bytearray(b"FILE")
    assert save_size == 4


@pytest.mark.parametrize(
    ("mode", "save_type", "source", "save_size", "expected"),
    [
        ("DMG", 1, b"abc", 5, b"abcabc"),
        ("DMG", 1, b"exact", 5, b"exact"),
        ("AGB", 6, b"ab", 5, b"ab"),
    ],
    ids=["repeat-past-nonmultiple-size", "exact-size", "dacs-does-not-repeat"],
)
def test_restore_size_handling(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    save_type: int,
    source: bytes,
    save_size: int,
    expected: bytes,
) -> None:
    device = GbxDevice()
    device.MODE = mode
    args = {"mode": 3, "save_type": save_type, "erase": False, "path": None, "buffer": source}

    (buffer, _, reported_size), progress = prepare_action(
        device,
        monkeypatch,
        args,
        save_size=save_size,
    )

    assert buffer == bytearray(expected)
    assert reported_size == save_size
    assert progress[-1]["size"] == save_size


@pytest.mark.parametrize(
    ("empty_data_byte", "mapper_name", "expected"),
    [
        (0x00, "MBC3", bytearray([0x00] * 4)),
        (0xFF, "MBC3", bytearray([0xFF] * 4)),
        (0xFF, "Xploder GB", bytearray([0x00, 0xFF, 0xFF, 0xFF])),
    ],
    ids=["zero-filled", "ff-filled", "xploder-first-byte"],
)
def test_erase_generates_the_mapper_specific_pattern(
    monkeypatch: pytest.MonkeyPatch,
    empty_data_byte: int,
    mapper_name: str,
    expected: bytearray,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    args = {"mode": 3, "save_type": 1, "erase": True, "path": None}

    (buffer, _, save_size), progress = prepare_action(
        device,
        monkeypatch,
        args,
        empty_data_byte=empty_data_byte,
        mapper=ActionMapper(mapper_name),
    )

    assert buffer == expected
    assert save_size == len(expected)
    assert device.INFO["save_erase"] is True
    assert progress == [{"action": "INITIALIZE", "method": "SAVE_WRITE", "size": 4}]


@pytest.mark.parametrize("source", [None, "text", 123, object()])
def test_restore_rejects_non_bytes_input(
    monkeypatch: pytest.MonkeyPatch,
    source: object,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    args = {"mode": 3, "save_type": 1, "erase": False, "path": None, "buffer": source}

    with pytest.raises(TypeError, match="bytes-like object"):
        prepare_action(device, monkeypatch, args)

    assert device.INFO["action"] is None


@pytest.mark.parametrize("source_location", ["buffer", "info", "file"])
def test_restore_rejects_empty_input_without_looping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_location: str,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    args: dict[str, Any] = {"mode": 3, "save_type": 1, "erase": False, "path": None}
    if source_location == "buffer":
        args["buffer"] = b""
    elif source_location == "info":
        device.INFO["data"] = bytearray()
    else:
        path = tmp_path / "empty.sav"
        path.write_bytes(b"")
        args["path"] = str(path)

    with pytest.raises(ValueError, match="Save data must not be empty"):
        prepare_action(device, monkeypatch, args)

    assert device.INFO["action"] is None


def test_unsupported_save_transfer_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "DMG"

    with pytest.raises(ValueError, match="Unsupported save transfer mode: 4"):
        prepare_action(device, monkeypatch, {"mode": 4})

    assert device.INFO["action"] is None
