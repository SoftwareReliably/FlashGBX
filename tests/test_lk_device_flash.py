"""Tests for flash-write decisions and verification bookkeeping."""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.LK_Device import _FlashVerificationContext

if TYPE_CHECKING:
    from pathlib import Path


class DecisionFlashcart:
    """Small flash-cart double exposing only the decision helpers need."""

    def __init__(
        self,
        *,
        voltage: float = 5,
        chip_erase: bool = False,
        sector_erase: bool = True,
        flash_size: int | bool = 16,
        sector_size: int = 4,
        sector_map: object = True,
        mbc: object = False,
    ) -> None:
        self.voltage = voltage
        self.chip_erase = chip_erase
        self.sector_erase = sector_erase
        self.flash_size = flash_size
        self.sector_size = sector_size
        self.sector_map = sector_map
        self.mbc = mbc
        self.offset_requests: list[tuple[int, int]] = []

    def GetVoltage(self) -> float:
        return self.voltage

    def SupportsChipErase(self) -> bool:
        return self.chip_erase

    def SupportsSectorErase(self) -> bool:
        return self.sector_erase

    def GetFlashSize(self, default: int | bool = False) -> int | bool:
        return self.flash_size if self.flash_size is not False else default

    def GetSectorMap(self) -> object:
        return self.sector_map

    def GetSmallestSectorSize(self) -> int:
        return self.sector_size

    def GetSectorOffsets(self, rom_size: int, rom_bank_size: int) -> list[list[int]]:
        self.offset_requests.append((rom_size, rom_bank_size))
        return [[offset, self.sector_size] for offset in range(0, rom_size, self.sector_size)]

    def GetMBC(self) -> object:
        return self.mbc


class VerificationMapper:
    def __init__(self, name: str = "MBC5", maximum_size: int = 0x200000) -> None:
        self.name = name
        self.maximum_size = maximum_size

    def GetName(self) -> str:
        return self.name

    def GetMaxROMSize(self) -> int:
        return self.maximum_size


@pytest.mark.parametrize(
    ("firmware", "override", "profile_voltage", "command_name", "wait", "expected"),
    [
        (11, False, 3.3, "SET_VOLTAGE_3_3V", False, 3.3),
        (12, 5, 3.3, "SET_VOLTAGE_5V", True, 5),
        (12, 3.3, 5, "SET_VOLTAGE_3_3V", True, 3.3),
    ],
)
def test_set_flash_voltage_selects_profile_or_override_and_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    firmware: int,
    override: object,
    profile_voltage: float,
    command_name: str,
    wait: bool,
    expected: float,
) -> None:
    device = GbxDevice()
    device.FW = {"fw_ver": firmware}
    write = Mock()
    monkeypatch.setattr(device, "_write", write)
    cart = DecisionFlashcart(voltage=profile_voltage)

    result = device._set_flash_voltage({"override_voltage": override}, cart)  # type: ignore[arg-type]

    assert result == expected
    write.assert_called_once_with(device.DEVICE_CMD[command_name], wait=wait)


@pytest.mark.parametrize(("mode", "expected"), [("DMG", 5), ("AGB", 3.3)])
def test_set_flash_voltage_reports_slot_voltage_for_voltage_locked_devices(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: float,
) -> None:
    device = GbxDevice()
    device.MODE = mode  # type: ignore[assignment]
    device.FW = {"fw_ver": 12}
    write = Mock()
    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "CanSetVoltageByAutoswitch", lambda: True)
    monkeypatch.setattr(device, "CanSetVoltageByCode", lambda: False)

    result = device._set_flash_voltage(
        {"override_voltage": 3.3 if mode == "DMG" else 5},
        DecisionFlashcart(),
    )

    assert result == expected
    expected_command = "SET_VOLTAGE_3_3V" if mode == "DMG" else "SET_VOLTAGE_5V"
    write.assert_called_once_with(device.DEVICE_CMD[expected_command], wait=True)


@pytest.mark.parametrize(
    ("cart", "prefer_chip_erase", "data"),
    [
        (DecisionFlashcart(chip_erase=True), True, bytearray(b"\xff" * 4)),
        (DecisionFlashcart(sector_erase=False), True, bytearray(b"\xff" * 4)),
        (DecisionFlashcart(), False, bytearray(b"\xff" * 4)),
        (DecisionFlashcart(), True, bytearray(b"\xff\x00\xff\xff")),
        (DecisionFlashcart(flash_size=4), True, bytearray(b"\xff" * 4)),
        (DecisionFlashcart(flash_size=3), True, bytearray(b"\xff" * 4)),
        (DecisionFlashcart(flash_size=False), True, bytearray(b"\xff" * 4)),
    ],
)
def test_sector_erase_padding_leaves_ineligible_data_unchanged(
    cart: DecisionFlashcart,
    prefer_chip_erase: bool,
    data: bytearray,
) -> None:
    original = data.copy()

    GbxDevice._pad_flash_data_for_sector_erase(
        {"prefer_chip_erase": prefer_chip_erase},
        cart,  # type: ignore[arg-type]
        data,
    )

    assert data == original


def test_sector_erase_padding_extends_an_erased_image_to_capacity() -> None:
    data = bytearray(b"\xff" * 4)

    GbxDevice._pad_flash_data_for_sector_erase(
        {"prefer_chip_erase": True},
        DecisionFlashcart(flash_size=10),  # type: ignore[arg-type]
        data,
    )

    assert data == bytearray(b"\xff" * 10)


def test_plan_flash_sectors_selects_regular_ranges() -> None:
    device = GbxDevice()
    cart = DecisionFlashcart(flash_size=16)
    data = bytearray(b"ABCDEF")

    plan = device._plan_flash_sectors(
        {"path": "game.gbc", "prefer_chip_erase": False},
        cart,  # type: ignore[arg-type]
        data,
        rom_bank_size=8,
        flash_offset=0,
    )

    assert plan is not None
    assert plan.data_import is data
    assert plan.smallest_sector_size == 4
    assert plan.sector_offsets == [[0, 4], [4, 4], [8, 4], [12, 4]]
    assert plan.write_sectors == [[0, 4], [4, 4]]
    assert plan.delta_state is None
    assert plan.state_path == ""
    assert plan.has_sector_map is True
    assert cart.offset_requests == [(16, 8)]


def test_plan_flash_sectors_expands_ranges_beyond_nominal_capacity() -> None:
    device = GbxDevice()
    cart = DecisionFlashcart(flash_size=8)
    data = bytearray(b"ABCDEFGHIJKL")

    plan = device._plan_flash_sectors(
        {"path": "large.gbc", "prefer_chip_erase": False},
        cart,  # type: ignore[arg-type]
        data,
        rom_bank_size=8,
        flash_offset=0,
    )

    assert plan is not None
    assert plan.sector_offsets == [[0, 4], [4, 4], [8, 4]]
    assert plan.write_sectors == plan.sector_offsets
    assert cart.offset_requests == [(8, 8), (12, 8)]


def test_plan_flash_sectors_uses_half_open_range_for_nonzero_offset() -> None:
    device = GbxDevice()
    cart = DecisionFlashcart(flash_size=16)

    plan = device._plan_flash_sectors(
        {
            "path": "batteryless.gbc",
            "prefer_chip_erase": False,
            "flash_size": 4,
            "bl_save": True,
        },
        cart,  # type: ignore[arg-type]
        bytearray(b"SAVEtrailing"),
        rom_bank_size=8,
        flash_offset=4,
    )

    assert plan is not None
    assert plan.write_sectors == [[4, 4]]
    assert plan.data_import == bytearray(b"\x00\x00\x00\x00SAVE")


def delta_state_path(delta_path: Path, sectors: list[list[int]]) -> Path:
    digest = base64.urlsafe_b64encode(hashlib.sha1(str(sectors).encode()).digest()).decode()[:4]
    return delta_path.with_name(f"{delta_path.stem}_{digest}.json")


def test_plan_flash_sectors_delta_records_only_changed_checksums_and_ignores_bad_state(
    tmp_path: Path,
) -> None:
    device = GbxDevice()
    cart = DecisionFlashcart(flash_size=12)
    source_path = tmp_path / "game.gbc"
    delta_path = tmp_path / "game.delta.gbc"
    source_path.write_bytes(b"AAAAXXXXCCCC")
    data = bytearray(b"AAAABBBBCCCC")
    sectors = [[0, 4], [4, 4], [8, 4]]
    state_path = delta_state_path(delta_path, sectors)
    state_path.write_bytes(b"not valid json \xff")

    plan = device._plan_flash_sectors(
        {"path": delta_path, "prefer_chip_erase": False},
        cart,  # type: ignore[arg-type]
        data,
        rom_bank_size=8,
        flash_offset=0,
    )

    changed = [[4, 4, zlib.crc32(b"BBBB") & 0xFFFFFFFF]]
    assert plan is not None
    assert plan.delta_state == changed
    assert plan.write_sectors == changed
    assert plan.state_path == state_path


def test_plan_flash_sectors_skips_already_completed_delta_sectors(tmp_path: Path) -> None:
    device = GbxDevice()
    cart = DecisionFlashcart(flash_size=12)
    source_path = tmp_path / "game.gbc"
    delta_path = tmp_path / "game.delta.gbc"
    source_path.write_bytes(b"xxxxxxxxxxxx")
    data = bytearray(b"AAAABBBBCCCC")
    sectors = [[0, 4], [4, 4], [8, 4]]
    changed = [[offset, size, zlib.crc32(data[offset : offset + size]) & 0xFFFFFFFF] for offset, size in sectors]
    state_path = delta_state_path(delta_path, sectors)
    state_path.write_text(json.dumps([changed[1]]), encoding="utf-8")

    plan = device._plan_flash_sectors(
        {"path": delta_path, "prefer_chip_erase": False},
        cart,  # type: ignore[arg-type]
        data,
        rom_bank_size=8,
        flash_offset=0,
    )

    assert plan is not None
    assert plan.delta_state == changed
    assert plan.write_sectors == [changed[0], changed[2]]
    assert plan.state_path == state_path


def test_abort_flash_verification_clears_metadata_power_cycles_and_resets_irq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.FW = {"fw_ver": 12}
    device.CANCEL_ARGS = {"info_type": "msgbox_warning", "info_msg": "Canceled"}
    device.ERROR_ARGS = {"stale": True}
    progress = Mock()
    power_cycle = Mock()
    set_variable = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: True)
    monkeypatch.setattr(device, "CartPowerCycle", power_cycle)
    monkeypatch.setattr(device, "_set_fw_variable", set_variable)

    device._AbortFlashVerification()

    progress.assert_called_once_with(
        {
            "action": "ABORT",
            "abortable": False,
            "info_type": "msgbox_warning",
            "info_msg": "Canceled",
        },
    )
    assert device.CANCEL_ARGS == {}
    assert device.ERROR_ARGS == {}
    power_cycle.assert_called_once_with()
    set_variable.assert_called_once_with("AGB_IRQ_ENABLED", 0)


def test_abort_flash_verification_avoids_unsupported_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.FW = {"fw_ver": 11}
    progress = Mock()
    power_cycle = Mock()
    set_variable = Mock()
    monkeypatch.setattr(device, "SetProgress", progress)
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: False)
    monkeypatch.setattr(device, "CartPowerCycle", power_cycle)
    monkeypatch.setattr(device, "_set_fw_variable", set_variable)

    device._AbortFlashVerification()

    progress.assert_called_once_with({"action": "ABORT", "abortable": False})
    power_cycle.assert_not_called()
    set_variable.assert_not_called()


def verification_context(
    cart: DecisionFlashcart,
    mapper: VerificationMapper,
    data: bytearray,
) -> _FlashVerificationContext:
    return _FlashVerificationContext(
        args={},
        cart_type={},
        flashcart=cart,  # type: ignore[arg-type]
        data_import=data,
        flash_offset=0,
        active_voltage=5,
        verify_sectors=[],
        rom_bank_size=0x4000,
        mbc=mapper,
        buffer_len=64,
    )


def test_store_flash_verification_errors_records_dmg_mapper_metadata() -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    cart = DecisionFlashcart(mbc="manual")
    mapper = VerificationMapper(name="MBC3", maximum_size=0x100000)
    broken = [[0x4000, 0x2000], [0x8000, 0x2000]]

    result = device._StoreFlashVerificationErrors(
        verification_context(cart, mapper, bytearray(0x9000)),
        broken,
    )

    assert result is True
    assert device.INFO["broken_sectors"] is broken
    assert device.INFO["verify_error_params"] == {
        "rom_size": 0x9000,
        "mapper_name": "MBC3",
        "mapper_selection_type": 1,
        "mapper_max_size": 0x100000,
    }


def test_store_flash_verification_errors_records_agb_metadata_without_mapper_fields() -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    cart = DecisionFlashcart(mbc=False)
    mapper = VerificationMapper()
    broken = [[0, 4]]

    assert (
        device._StoreFlashVerificationErrors(
            verification_context(cart, mapper, bytearray(b"123456")),
            broken,
        )
        is True
    )
    assert device.INFO["broken_sectors"] == broken
    assert device.INFO["verify_error_params"] == {"rom_size": 6}

    existing_info = device.INFO.copy()
    assert (
        device._StoreFlashVerificationErrors(
            verification_context(cart, mapper, bytearray()),
            [],
        )
        is False
    )
    assert existing_info == device.INFO
