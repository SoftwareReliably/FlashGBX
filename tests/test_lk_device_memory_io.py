"""Tests for ordinary cartridge RAM reads and writes."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import pytest

from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.LK_Device import _AGBSaveConfiguration

if TYPE_CHECKING:
    from collections.abc import Iterable


class MemoryIoRecords:
    """Finite firmware-variable, command, data, and progress boundaries."""

    def __init__(
        self,
        *,
        read_results: Iterable[int | bytearray | bool] = (),
        acknowledgements: Iterable[int | bool] = (),
    ) -> None:
        self.read_results = deque(read_results)
        self.acknowledgements = deque(acknowledgements)
        self.variables: list[tuple[str, int]] = []
        self.writes: list[tuple[int | bytes, bool]] = []
        self.read_counts: list[int] = []
        self.progress: list[dict[str, object]] = []


def install_memory_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    *,
    read_results: Iterable[int | bytearray | bool] = (),
    acknowledgements: Iterable[int | bool] = (),
) -> MemoryIoRecords:
    """Replace only the low-level boundaries used by ReadRAM and WriteRAM."""
    records = MemoryIoRecords(read_results=read_results, acknowledgements=acknowledgements)

    def set_variable(name: str, value: int) -> None:
        records.variables.append((name, value))

    def write(data: int | bytes | bytearray | memoryview, wait: bool = False) -> int | bool | None:
        payload: int | bytes = data if isinstance(data, int) else bytes(data)
        records.writes.append((payload, wait))
        if not wait:
            return None
        assert records.acknowledgements, "Unexpected acknowledged write after response script exhaustion"
        return records.acknowledgements.popleft()

    def read(count: int) -> int | bytearray | bool:
        records.read_counts.append(count)
        assert records.read_results, "Unexpected RAM read after response script exhaustion"
        result = records.read_results.popleft()
        return bytearray(result) if isinstance(result, bytearray) else result

    monkeypatch.setattr(device, "_set_fw_variable", set_variable)
    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "_read", read)
    monkeypatch.setattr(device, "SetProgress", lambda event: records.progress.append(dict(event)))
    return records


@pytest.mark.parametrize(
    ("mode", "expected_address", "expected_command", "mode_variables"),
    [
        (
            "DMG",
            0xA120,
            GbxDevice.DEVICE_CMD["DMG_CART_READ"],
            [("DMG_ACCESS_MODE", 3), ("DMG_READ_CS_PULSE", 1), ("DMG_READ_CS_PULSE", 0)],
        ),
        ("AGB", 0x120, GbxDevice.DEVICE_CMD["AGB_CART_READ_SRAM"], []),
    ],
)
def test_read_ram_uses_platform_address_and_default_command(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_address: int,
    expected_command: int,
    mode_variables: list[tuple[str, int]],
) -> None:
    device = GbxDevice()
    device.MODE = mode
    records = install_memory_boundaries(device, monkeypatch, read_results=[bytearray(b"DATA")])

    assert device.ReadRAM(address=0x120, length=4, max_length=8) == bytearray(b"DATA")
    expected_variables = [("TRANSFER_SIZE", 4), ("ADDRESS", expected_address), *mode_variables]
    assert records.variables == expected_variables
    assert records.writes == [(expected_command, False)]
    assert records.read_counts == [4]
    assert records.progress == [{"action": "READ", "bytes_added": 4}]
    assert not records.read_results


def test_read_ram_honors_device_cap_and_short_final_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.MAX_BUFFER_READ = 2
    records = install_memory_boundaries(
        device,
        monkeypatch,
        read_results=[bytearray(b"AB"), bytearray(b"CD"), bytearray(b"E")],
    )

    result = device.ReadRAM(address=0x40, length=5, command=0x91, max_length=4)

    assert result == bytearray(b"ABCDE")
    assert records.variables == [("TRANSFER_SIZE", 2), ("ADDRESS", 0x40), ("TRANSFER_SIZE", 1)]
    assert records.writes == [(0x91, False)] * 3
    assert records.read_counts == [2, 2, 1]
    assert records.progress == [
        {"action": "READ", "bytes_added": 2},
        {"action": "READ", "bytes_added": 2},
        {"action": "READ", "bytes_added": 1},
    ]
    assert not records.read_results


def test_read_ram_suppresses_progress_without_changing_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.NO_PROG_UPDATE = True
    records = install_memory_boundaries(device, monkeypatch, read_results=[bytearray(b"RAM")])

    assert device.ReadRAM(address=0, length=3) == bytearray(b"RAM")
    assert records.read_counts == [3]
    assert records.progress == []


def test_read_ram_distinguishes_one_byte_timeout_from_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    timed_out = GbxDevice()
    timed_out.MODE = "DMG"
    timeout_records = install_memory_boundaries(timed_out, monkeypatch, read_results=[False])

    assert timed_out.ReadRAM(address=0, length=1) == bytearray()
    assert timeout_records.progress == []
    assert timeout_records.variables[-1] == ("DMG_READ_CS_PULSE", 0)

    zero = GbxDevice()
    zero.MODE = "DMG"
    zero_records = install_memory_boundaries(zero, monkeypatch, read_results=[0])

    assert zero.ReadRAM(address=0, length=1) == bytearray(b"\x00")
    assert zero_records.progress == [{"action": "READ", "bytes_added": 1}]
    assert zero_records.variables[-1] == ("DMG_READ_CS_PULSE", 0)


def test_read_ram_rejects_short_reply_and_restores_dmg_pulse(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    records = install_memory_boundaries(device, monkeypatch, read_results=[bytearray(b"X")])

    assert device.ReadRAM(address=0x20, length=2) == bytearray()
    assert records.read_counts == [2]
    assert records.progress == []
    assert records.variables[-1] == ("DMG_READ_CS_PULSE", 0)


def test_dmg_ram_io_honors_explicit_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = GbxDevice()
    reader.MODE = "DMG"
    read_records = install_memory_boundaries(reader, monkeypatch, read_results=[bytearray(b"R")])

    assert reader.ReadRAM(address=0, length=1, command=0x91) == bytearray(b"R")
    assert read_records.writes == [(0x91, False)]

    writer = GbxDevice()
    writer.MODE = "DMG"
    write_records = install_memory_boundaries(writer, monkeypatch, acknowledgements=[1])

    assert writer.WriteRAM(address=0, buffer=b"W", command=0x92) is True
    assert write_records.writes == [(0x92, False), (b"W", True)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"BYTES", b"BYTES"),
        (bytearray(b"ARRAY"), b"ARRAY"),
        (memoryview(b"VIEW!"), b"VIEW!"),
    ],
)
def test_write_ram_accepts_bytes_like_inputs(
    monkeypatch: pytest.MonkeyPatch,
    source: bytes | bytearray | memoryview,
    expected: bytes,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.INFO["action"] = device.ACTIONS["SAVE_WRITE"]
    records = install_memory_boundaries(device, monkeypatch, acknowledgements=[1])

    assert device.WriteRAM(address=0x80, buffer=source) is True
    assert records.variables == [("TRANSFER_SIZE", 5), ("ADDRESS", 0x80)]
    assert records.writes == [
        (device.DEVICE_CMD["AGB_CART_WRITE_SRAM"], False),
        (expected, True),
    ]
    assert records.progress == [{"action": "WRITE", "bytes_added": 5}]
    assert not records.acknowledgements


def test_write_ram_uses_dmg_address_mode_and_restores_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.INFO["action"] = device.ACTIONS["SAVE_WRITE"]
    records = install_memory_boundaries(device, monkeypatch, acknowledgements=[3])

    assert device.WriteRAM(address=0x123, buffer=b"SAVE") is True
    assert records.variables == [
        ("TRANSFER_SIZE", 4),
        ("ADDRESS", 0xA123),
        ("DMG_ACCESS_MODE", 4),
        ("DMG_WRITE_CS_PULSE", 1),
        ("ADDRESS", 0),
        ("DMG_WRITE_CS_PULSE", 0),
    ]
    assert records.writes == [
        (device.DEVICE_CMD["DMG_CART_WRITE_SRAM"], False),
        (b"SAVE", True),
    ]
    assert records.progress == [{"action": "WRITE", "bytes_added": 4}]


def test_write_ram_honors_device_cap_and_short_final_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.MAX_BUFFER_WRITE = 2
    device.INFO["action"] = device.ACTIONS["SAVE_WRITE"]
    records = install_memory_boundaries(device, monkeypatch, acknowledgements=[1, 3, 1])

    assert device.WriteRAM(address=0x60, buffer=b"ABCDE", command=0x92, max_length=4) is True
    assert records.variables == [("TRANSFER_SIZE", 2), ("ADDRESS", 0x60), ("TRANSFER_SIZE", 1)]
    assert records.writes == [
        (0x92, False),
        (b"AB", True),
        (0x92, False),
        (b"CD", True),
        (0x92, False),
        (b"E", True),
    ]
    assert records.progress == [
        {"action": "WRITE", "bytes_added": 2},
        {"action": "WRITE", "bytes_added": 2},
        {"action": "WRITE", "bytes_added": 1},
    ]
    assert not records.acknowledgements


@pytest.mark.parametrize(
    ("action", "no_progress"),
    [(None, False), (GbxDevice.ACTIONS["SAVE_WRITE"], True)],
    ids=["different-action", "updates-disabled"],
)
def test_write_ram_suppresses_progress_without_changing_payload(
    monkeypatch: pytest.MonkeyPatch,
    action: int | None,
    no_progress: bool,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.INFO["action"] = action
    device.NO_PROG_UPDATE = no_progress
    records = install_memory_boundaries(device, monkeypatch, acknowledgements=[1])

    assert device.WriteRAM(address=0, buffer=b"OK") is True
    assert records.writes[-1] == (b"OK", True)
    assert records.progress == []


@pytest.mark.parametrize("acknowledgements", [[False], [1, False]], ids=["first-chunk", "later-chunk"])
def test_write_ram_stops_on_failed_payload_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    acknowledgements: list[int | bool],
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.MAX_BUFFER_WRITE = 2
    device.INFO["action"] = device.ACTIONS["SAVE_WRITE"]
    records = install_memory_boundaries(device, monkeypatch, acknowledgements=acknowledgements)

    assert device.WriteRAM(address=0x20, buffer=b"FAIL", max_length=4) is False
    successful_chunks = len(acknowledgements) - 1
    assert records.writes == [
        entry
        for index in range(len(acknowledgements))
        for entry in (
            (device.DEVICE_CMD["DMG_CART_WRITE_SRAM"], False),
            (b"FAIL"[index * 2 : index * 2 + 2], True),
        )
    ]
    assert records.progress == [{"action": "WRITE", "bytes_added": 2}] * successful_chunks
    assert records.variables[-2:] == [("ADDRESS", 0), ("DMG_WRITE_CS_PULSE", 0)]
    assert not records.acknowledgements


def test_ram_io_requires_selected_cartridge_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = GbxDevice()
    read_records = install_memory_boundaries(reader, monkeypatch)
    with pytest.raises(RuntimeError, match="selected before reading RAM"):
        reader.ReadRAM(address=0, length=1)
    assert read_records.writes == []

    writer = GbxDevice()
    write_records = install_memory_boundaries(writer, monkeypatch)
    with pytest.raises(RuntimeError, match="selected before writing RAM"):
        writer.WriteRAM(address=0, buffer=b"X")
    assert write_records.writes == []


def test_real_save_worker_stops_after_low_level_ram_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.FW = {"fw_ver": 12, "pcb_name": "Test device"}
    records = install_memory_boundaries(device, monkeypatch, acknowledgements=[False])

    def configure(
        _args: dict[str, object],
        _cart_type: dict[str, object] | None,
    ) -> _AGBSaveConfiguration:
        return _AGBSaveConfiguration(
            buffer_len=2,
            save_size=4,
            ram_banks=1,
            flash_chip=0,
            sram_5=0,
            command=device.DEVICE_CMD["AGB_CART_WRITE_SRAM"],
            empty_data_byte=0xFF,
            extra_size=0,
        )

    monkeypatch.setattr(device, "_configure_agb_save_transfer", configure)
    args = {
        "mode": 3,
        "save_type": 3,
        "path": None,
        "buffer": bytearray(b"A0A1"),
        "erase": False,
        "verify_write": False,
    }

    assert device._BackupRestoreRAM_Worker(args) is False
    payloads = [payload for payload, wait in records.writes if wait]
    assert payloads == [b"A0"]
    assert records.progress == [
        {"action": "INITIALIZE", "method": "SAVE_WRITE", "size": 4},
    ]
    assert device.INFO["last_action"] == device.ACTIONS["SAVE_WRITE"]
    assert device.INFO["action"] is None
    assert "last_path" not in device.INFO
