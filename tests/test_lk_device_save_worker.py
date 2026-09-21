"""Successful save-transfer worker scenarios with finite hardware boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.LK_Device import _AGBSaveConfiguration, _DMGSaveConfiguration

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class WorkerMapper:
    """Small two-bank DMG mapper used by real worker helpers."""

    def __init__(self, bank_size: int = 4) -> None:
        self.bank_size = bank_size
        self.current_bank = 0
        self.selected_banks: list[int] = []
        self.ram_enabled: list[bool] = []

    def GetName(self) -> str:
        return "MBC3"

    def WriteWithCSPulse(self) -> bool:
        return False

    def SelectBankRAM(self, bank: int) -> tuple[int, int]:
        self.current_bank = bank
        self.selected_banks.append(bank)
        return 0, self.bank_size

    def EnableRAM(self, enable: bool = True) -> None:
        self.ram_enabled.append(enable)


class WorkerRecords:
    """Physical I/O and progress emitted by a save worker scenario."""

    def __init__(self, read_responses: list[bytearray] | None = None) -> None:
        self.read_responses = list(read_responses or [])
        self.ram_reads: list[dict[str, object]] = []
        self.ram_writes: list[dict[str, object]] = []
        self.firmware_variables: list[tuple[str, int]] = []
        self.device_writes: list[tuple[object, bool]] = []
        self.progress: list[dict[str, object]] = []


def install_worker_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mapper: WorkerMapper | None = None,
    read_responses: list[bytearray] | None = None,
) -> WorkerRecords:
    """Install recording hardware boundaries while retaining worker helpers."""
    records = WorkerRecords(read_responses)
    device.FW = {"fw_ver": 12, "pcb_name": "Test device"}

    def read_ram(*, address: int, length: int, command: object, max_length: int) -> bytearray:
        records.ram_reads.append(
            {
                "bank": None if mapper is None else mapper.current_bank,
                "address": address,
                "length": length,
                "command": command,
                "max_length": max_length,
            },
        )
        assert records.read_responses
        return bytearray(records.read_responses.pop(0))

    def write_ram(
        *,
        address: int,
        buffer: bytes | bytearray | memoryview,
        command: object,
        max_length: int | None = None,
    ) -> None:
        records.ram_writes.append(
            {
                "bank": None if mapper is None else mapper.current_bank,
                "address": address,
                "buffer": bytearray(buffer),
                "command": command,
                "max_length": max_length,
            },
        )

    def set_fw_variable(name: str, value: int) -> None:
        records.firmware_variables.append((name, value))

    def device_write(value: object, wait: bool = False) -> None:
        records.device_writes.append((value, wait))

    monkeypatch.setattr(device, "ReadRAM", read_ram)
    monkeypatch.setattr(device, "WriteRAM", write_ram)
    monkeypatch.setattr(device, "_set_fw_variable", set_fw_variable)
    monkeypatch.setattr(device, "_write", device_write)
    monkeypatch.setattr(device, "SetProgress", lambda event: records.progress.append(dict(event)))
    return records


def install_dmg_configuration(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    mapper: WorkerMapper,
) -> None:
    """Choose a two-bank, two-chunk DMG transfer without replacing worker helpers."""

    def configure(_args: dict[str, object]) -> _DMGSaveConfiguration:
        return _DMGSaveConfiguration(
            mbc=mapper,
            buffer_len=2,
            save_size=8,
            ram_banks=2,
            empty_data_byte=0,
            extra_size=0,
            audio_low=False,
        )

    monkeypatch.setattr(device, "_configure_dmg_save_transfer", configure)


def install_agb_configuration(device: GbxDevice, monkeypatch: pytest.MonkeyPatch) -> None:
    """Choose a one-bank, two-chunk AGB SRAM transfer."""

    def configure(
        args: dict[str, object],
        _cart_type: dict[str, object] | None,
    ) -> _AGBSaveConfiguration:
        command_name = "AGB_CART_READ_SRAM" if args["mode"] == 2 else "AGB_CART_WRITE_SRAM"
        return _AGBSaveConfiguration(
            buffer_len=2,
            save_size=4,
            ram_banks=1,
            flash_chip=0,
            sram_5=0,
            command=GbxDevice.DEVICE_CMD[command_name],
            empty_data_byte=0,
            extra_size=0,
        )

    monkeypatch.setattr(device, "_configure_agb_save_transfer", configure)


def finished_events(records: WorkerRecords) -> list[dict[str, object]]:
    return [event for event in records.progress if event.get("action") == "FINISHED"]


def test_dmg_worker_backs_up_two_banks_and_resets_mapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = WorkerMapper()
    responses = [bytearray(b"A0"), bytearray(b"A1"), bytearray(b"B0"), bytearray(b"B1")]
    records = install_worker_boundaries(device, monkeypatch, mapper=mapper, read_responses=responses)
    install_dmg_configuration(device, monkeypatch, mapper)
    args = {"mode": 2, "save_type": 3, "path": None, "verify_read": False}

    assert device._BackupRestoreRAM_Worker(args) is True

    assert device.INFO["data"] == bytearray(b"A0A1B0B1")
    assert device.INFO["transferred"] == 8
    assert device.INFO["last_action"] == device.ACTIONS["SAVE_READ"]
    assert device.INFO["action"] is None
    assert device.INFO["last_path"] is None
    assert records.ram_reads == [
        {"bank": 0, "address": 0, "length": 2, "command": None, "max_length": 0x1000},
        {"bank": 0, "address": 2, "length": 2, "command": None, "max_length": 0x1000},
        {"bank": 1, "address": 0, "length": 2, "command": None, "max_length": 0x1000},
        {"bank": 1, "address": 2, "length": 2, "command": None, "max_length": 0x1000},
    ]
    assert records.read_responses == []
    assert mapper.selected_banks == [0, 1, 0]
    assert mapper.ram_enabled == [False]
    assert records.firmware_variables == [
        ("STATUS_REGISTER_MASK", 0x80),
        ("STATUS_REGISTER_VALUE", 0x80),
        ("DMG_WRITE_CS_PULSE", 0),
        ("DMG_WRITE_CS_PULSE", 0),
        ("DMG_READ_CS_PULSE", 0),
    ]
    assert records.device_writes == [(device.DEVICE_CMD["SET_ADDR_AS_INPUTS"], True)]
    assert records.progress == [
        {"action": "INITIALIZE", "method": "SAVE_READ", "size": 8},
        {"action": "UPDATE_POS", "pos": 2},
        {"action": "UPDATE_POS", "pos": 4},
        {"action": "UPDATE_POS", "pos": 6},
        {"action": "UPDATE_POS", "pos": 8},
        {"action": "FINISHED", "verified": True},
    ]
    assert finished_events(records) == [{"action": "FINISHED", "verified": True}]


def test_dmg_worker_restores_exact_slices_across_two_banks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = WorkerMapper()
    records = install_worker_boundaries(device, monkeypatch, mapper=mapper)
    install_dmg_configuration(device, monkeypatch, mapper)
    source = bytearray(b"A0A1B0B1")
    args = {
        "mode": 3,
        "save_type": 3,
        "path": None,
        "buffer": source,
        "erase": False,
        "verify_write": False,
    }

    assert device._BackupRestoreRAM_Worker(args) is True

    assert records.ram_writes == [
        {"bank": 0, "address": 0, "buffer": bytearray(b"A0"), "command": None, "max_length": None},
        {"bank": 0, "address": 2, "buffer": bytearray(b"A1"), "command": None, "max_length": None},
        {"bank": 1, "address": 0, "buffer": bytearray(b"B0"), "command": None, "max_length": None},
        {"bank": 1, "address": 2, "buffer": bytearray(b"B1"), "command": None, "max_length": None},
    ]
    assert mapper.selected_banks == [0, 1, 0]
    assert mapper.ram_enabled == [False]
    assert device.INFO["transferred"] == len(source)
    assert device.INFO["last_action"] == device.ACTIONS["SAVE_WRITE"]
    assert device.INFO["action"] is None
    assert device.INFO["last_path"] is None
    assert records.progress == [
        {"action": "INITIALIZE", "method": "SAVE_WRITE", "size": 8},
        {"action": "UPDATE_POS", "pos": 2},
        {"action": "UPDATE_POS", "pos": 4},
        {"action": "UPDATE_POS", "pos": 6},
        {"action": "UPDATE_POS", "pos": 8},
        {"action": "UPDATE_POS", "pos": 8, "force_update": True},
        {"action": "FINISHED", "verified": True},
    ]
    assert finished_events(records) == [{"action": "FINISHED", "verified": True}]


def test_agb_worker_backs_up_paired_reads_to_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    responses = [bytearray(b"A0"), bytearray(b"A0"), bytearray(b"A1"), bytearray(b"A1")]
    records = install_worker_boundaries(device, monkeypatch, read_responses=responses)
    install_agb_configuration(device, monkeypatch)
    destination = tmp_path / "paired.sav"
    args = {"mode": 2, "save_type": 3, "path": destination, "verify_read": True}

    assert device._BackupRestoreRAM_Worker(args) is True

    assert destination.read_bytes() == b"A0A1"
    assert [read["address"] for read in records.ram_reads] == [0, 0, 2, 2]
    assert all(read["command"] == device.DEVICE_CMD["AGB_CART_READ_SRAM"] for read in records.ram_reads)
    assert records.read_responses == []
    assert device.INFO["transferred"] == 4
    assert device.INFO["last_action"] == device.ACTIONS["SAVE_READ"]
    assert device.INFO["action"] is None
    assert device.INFO["last_path"] == destination
    assert records.progress == [
        {"action": "INITIALIZE", "method": "SAVE_READ", "size": 4},
        {"action": "UPDATE_POS", "pos": 2},
        {"action": "UPDATE_POS", "pos": 4},
        {"action": "FINISHED", "verified": True},
    ]
    assert finished_events(records) == [{"action": "FINISHED", "verified": True}]


def test_agb_worker_restores_memory_and_uses_controlled_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    records = install_worker_boundaries(device, monkeypatch)
    install_agb_configuration(device, monkeypatch)
    source = bytearray(b"A0A1")
    controlled_readback = bytearray(source)
    verification_calls: list[tuple[bytearray, int]] = []

    def verify(
        _args: dict[str, object],
        _mapper: object,
        buffer: bytearray,
        buffer_offset: int,
    ) -> bool:
        verification_calls.append((bytearray(buffer), buffer_offset))
        return controlled_readback == buffer[:buffer_offset]

    monkeypatch.setattr(device, "_VerifySaveWrite", verify)
    args = {
        "mode": 3,
        "save_type": 3,
        "path": None,
        "buffer": source,
        "erase": False,
        "verify_write": True,
    }

    assert device._BackupRestoreRAM_Worker(args) is True

    assert records.ram_writes == [
        {
            "bank": None,
            "address": 0,
            "buffer": bytearray(b"A0"),
            "command": device.DEVICE_CMD["AGB_CART_WRITE_SRAM"],
            "max_length": None,
        },
        {
            "bank": None,
            "address": 2,
            "buffer": bytearray(b"A1"),
            "command": device.DEVICE_CMD["AGB_CART_WRITE_SRAM"],
            "max_length": None,
        },
    ]
    assert verification_calls == [(source, len(source))]
    assert device.INFO["transferred"] == 4
    assert device.INFO["last_action"] == device.ACTIONS["SAVE_WRITE"]
    assert device.INFO["action"] is None
    assert device.INFO["last_path"] is None
    assert records.progress == [
        {"action": "INITIALIZE", "method": "SAVE_WRITE", "size": 4},
        {"action": "UPDATE_POS", "pos": 2},
        {"action": "UPDATE_POS", "pos": 4},
        {"action": "UPDATE_POS", "pos": 4, "force_update": True},
        {"action": "FINISHED", "verified": True},
    ]
    assert finished_events(records) == [{"action": "FINISHED", "verified": True}]


def test_agb_worker_keeps_real_normal_sram_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    payload = bytearray(index % 251 for index in range(0x2000))
    records = install_worker_boundaries(device, monkeypatch, read_responses=[payload])
    args = {
        "mode": 2,
        "save_type": 3,
        "save_size": len(payload),
        "path": None,
        "verify_read": False,
    }

    assert device._BackupRestoreRAM_Worker(args) is True

    assert device.INFO["data"] == payload
    assert device.INFO["transferred"] == len(payload)
    assert records.ram_reads == [
        {
            "bank": None,
            "address": 0,
            "length": len(payload),
            "command": device.DEVICE_CMD["AGB_CART_READ_SRAM"],
            "max_length": 0x1000,
        },
    ]
    assert records.device_writes == [
        (device.DEVICE_CMD["SET_MODE_AGB"], True),
        (device.DEVICE_CMD["SET_VOLTAGE_3_3V"], True),
    ]
    assert records.firmware_variables == [
        ("STATUS_REGISTER_MASK", 0x80),
        ("STATUS_REGISTER_VALUE", 0x80),
    ]
    assert records.progress == [
        {"action": "INITIALIZE", "method": "SAVE_READ", "size": len(payload)},
        {"action": "UPDATE_POS", "pos": len(payload)},
        {"action": "FINISHED", "verified": True},
    ]
    assert device.INFO["last_path"] is None
    assert device.INFO["action"] is None
    assert finished_events(records) == [{"action": "FINISHED", "verified": True}]
