"""ROM-read preparation and hardware-state restoration contracts."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

import FlashGBX.LK_Device as lk_device_module
from FlashGBX.hw_GBxCartRW import GbxDevice


class PreparationMapper:
    """Small DMG mapper with explicit initialization and bank records."""

    def __init__(
        self,
        name: str = "MBC3",
        *,
        rom_banks: int = 3,
        bank_size: int = 0x4000,
        rom_size: int = 0xC000,
        reset_banks: set[int] | None = None,
    ) -> None:
        self.name = name
        self.rom_banks = rom_banks
        self.bank_size = bank_size
        self.rom_size = rom_size
        self.reset_banks = set(reset_banks or set())
        self.enable_calls = 0
        self.requested_rom_sizes: list[int] = []
        self.start_banks: list[int] = []
        self.reset_requests: list[int] = []
        self.selected_banks: list[int] = []

    def GetName(self) -> str:
        return self.name

    def EnableMapper(self) -> None:
        self.enable_calls += 1

    def SetStartBank(self, bank: int) -> None:
        self.start_banks.append(bank)

    def GetROMBanks(self, rom_size: int) -> int:
        self.requested_rom_sizes.append(rom_size)
        return self.rom_banks

    def GetROMBankSize(self) -> int:
        return self.bank_size

    def GetROMSize(self) -> int:
        return self.rom_size

    def ResetBeforeBankChange(self, bank: int) -> bool:
        self.reset_requests.append(bank)
        return bank in self.reset_banks

    def SelectBankROM(self, bank: int) -> tuple[int, int]:
        self.selected_banks.append(bank)
        return bank * self.bank_size, self.bank_size


class MapperFactory:
    """Factory boundary matching the production DMG mapper lookup."""

    def __init__(self, mapper: PreparationMapper) -> None:
        self.mapper = mapper
        self.calls: list[dict[str, object]] = []

    def GetInstance(self, **kwargs: object) -> PreparationMapper:
        self.calls.append(dict(kwargs))
        return self.mapper


class PreparationFlashcart:
    """AGB flash-cart boundary for unlock and bank-select behavior."""

    def __init__(self) -> None:
        self.unlock_calls = 0
        self.selected_banks: list[int] = []

    def Unlock(self) -> bool:
        self.unlock_calls += 1
        return True

    def SelectBankROM(self, bank: int) -> None:
        self.selected_banks.append(bank)


class PreparationIO:
    """Device interactions emitted while preparing a ROM read."""

    def __init__(self) -> None:
        self.firmware_reads: list[str] = []
        self.firmware_writes: list[tuple[str, int]] = []
        self.device_writes: list[tuple[object, bool]] = []
        self.rom_reads: list[tuple[int, int]] = []
        self.progress: list[dict[str, object]] = []
        self.reconnects = 0


def install_preparation_io(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cart_mode: int,
) -> PreparationIO:
    """Install recording hardware boundaries for real ROM preparation."""
    records = PreparationIO()
    device.FW = {"fw_ver": 12, "pcb_name": "Test device"}
    device.INFO["dump_info"] = {}

    def get_firmware_variable(name: str) -> int:
        records.firmware_reads.append(name)
        if name != "CART_MODE":
            msg = f"Unexpected firmware variable read: {name}"
            raise AssertionError(msg)
        return cart_mode

    def set_firmware_variable(name: str, value: int) -> None:
        records.firmware_writes.append((name, value))

    def device_write(value: object, wait: bool = False) -> None:
        records.device_writes.append((value, wait))

    def read_rom(address: int, length: int) -> bytearray:
        records.rom_reads.append((address, length))
        return bytearray(length)

    def set_progress(event: dict[str, object]) -> None:
        records.progress.append(dict(event))

    def reconnect() -> None:
        records.reconnects += 1

    monkeypatch.setattr(device, "_get_fw_variable", get_firmware_variable)
    monkeypatch.setattr(device, "_set_fw_variable", set_firmware_variable)
    monkeypatch.setattr(device, "_write", device_write)
    monkeypatch.setattr(device, "ReadROM", read_rom)
    monkeypatch.setattr(device, "SetProgress", set_progress)
    monkeypatch.setattr(device, "CartPowerCycleOrAskReconnect", reconnect)
    return records


def test_prepare_rom_read_rejects_unsupported_dmg_mapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    records = install_preparation_io(device, monkeypatch, cart_mode=1)
    supported = Mock(return_value=False)
    full_name = Mock(return_value="Test Reader")
    mapper_constructor = Mock(side_effect=AssertionError("Unsupported mapper must stop before factory lookup"))
    monkeypatch.setattr(device, "IsSupportedMbc", supported)
    monkeypatch.setattr(device, "GetFullName", full_name)
    monkeypatch.setattr(lk_device_module, "DMG_Mapper", mapper_constructor)
    args = {"rom_size": 0x8000, "mbc": 0xFE}

    result = device._PrepareROMRead("DMG", args, {}, False)

    assert result is None
    supported.assert_called_once_with(0xFE)
    full_name.assert_called_once_with()
    mapper_constructor.assert_not_called()
    assert device.INFO["dump_info"] == {
        "rom_size": 0x8000,
        "mapper_type": 0xFE,
        "dmg_read_method": device.DMG_READ_METHODS[device.DMG_READ_METHOD],
    }
    assert device.INFO["mapper_raw"] == 0xFE
    assert records.progress[0]["action"] == "ABORT"
    assert records.progress[0]["info_type"] == "msgbox_critical"
    assert records.progress[0]["abortable"] is False
    assert "Test Reader" in str(records.progress[0]["info_msg"])
    assert records.firmware_reads == []
    assert records.firmware_writes == []
    assert records.device_writes == []


@pytest.mark.parametrize(
    ("mapper_name", "cart_mode", "use_verification_mapper"),
    [
        ("MBC3", 1, False),
        ("TAMA5", 2, True),
        ("Sachen", 1, False),
    ],
    ids=["ordinary-factory-current-mode", "tama5-verification-mode-change", "sachen-start-bank"],
)
def test_prepare_rom_read_configures_supported_dmg_mapper(
    monkeypatch: pytest.MonkeyPatch,
    mapper_name: str,
    cart_mode: int,
    use_verification_mapper: bool,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    records = install_preparation_io(device, monkeypatch, cart_mode=cart_mode)
    mapper = PreparationMapper(mapper_name)
    factory = MapperFactory(mapper)
    mapper_constructor = Mock(return_value=factory)
    supported = Mock(return_value=True)
    monkeypatch.setattr(device, "IsSupportedMbc", supported)
    monkeypatch.setattr(lk_device_module, "DMG_Mapper", mapper_constructor)
    args: dict[str, Any] = {"rom_size": 0x8000, "mbc": 0x19}
    if use_verification_mapper:
        args["verify_mbc"] = mapper

    result = device._PrepareROMRead("DMG", args, {}, False)

    assert result == (mapper, 0xC000, 3, 0x4000, 0x4000, False)
    supported.assert_called_once_with(0x19)
    assert device.INFO["dump_info"] == {
        "rom_size": 0x8000,
        "mapper_type": 0x19,
        "dmg_read_method": device.DMG_READ_METHODS[device.DMG_READ_METHOD],
    }
    assert device.INFO["mapper_raw"] == 0x19
    assert mapper.requested_rom_sizes == [0x8000]
    if use_verification_mapper:
        mapper_constructor.assert_not_called()
        assert factory.calls == []
    else:
        mapper_constructor.assert_called_once_with()
        assert len(factory.calls) == 1
        assert factory.calls[0]["args"] is args
        assert set(factory.calls[0]) == {
            "args",
            "cart_write_fncptr",
            "cart_read_fncptr",
            "cart_powercycle_fncptr",
            "clk_toggle_fncptr",
        }
    assert records.firmware_reads == ["CART_MODE"]
    expected_device_writes = []
    if cart_mode != 1:
        expected_device_writes.append((device.DEVICE_CMD["SET_MODE_DMG"], True))
    assert records.device_writes == expected_device_writes

    expected_firmware_writes = [("DMG_WRITE_CS_PULSE", 0), ("DMG_READ_CS_PULSE", 0)]
    if mapper_name == "TAMA5":
        expected_firmware_writes.extend(
            [("DMG_WRITE_CS_PULSE", 1), ("DMG_READ_CS_PULSE", 1), ("DMG_READ_CS_PULSE", 0)],
        )
        assert mapper.enable_calls == 1
        assert mapper.start_banks == []
    elif mapper_name == "Sachen":
        assert mapper.enable_calls == 0
        assert mapper.start_banks == [2]
    else:
        assert mapper.enable_calls == 1
        assert mapper.start_banks == []
    assert records.firmware_writes == expected_firmware_writes


@pytest.mark.parametrize(
    ("args", "cart_type", "cart_mode", "expected"),
    [
        (
            {},
            {},
            2,
            (32 * 1024 * 1024, 1, 32 * 1024 * 1024, 0x10000, False),
        ),
        (
            {"agb_rom_size": 0x250},
            {"command_set": "AMD", "flash_bank_size": 0x100},
            1,
            (0x250, 3, 0x100, 0x10000, False),
        ),
        (
            {"agb_rom_size": 0x500, "verify_write": bytearray(0x180)},
            {"command_set": "AMD", "flash_bank_size": 0x100},
            2,
            (0x500, 2, 0x100, 0x10000, False),
        ),
        (
            {"agb_rom_size": 0x1234},
            {"command_set": "3DMEMORY"},
            2,
            (0x1234, 1, 32 * 1024 * 1024, 0x10000, True),
        ),
    ],
    ids=["default-size", "partial-final-bank", "verification-buffer-banks", "3d-memory"],
)
def test_prepare_rom_read_configures_agb_size_banks_and_mode(
    monkeypatch: pytest.MonkeyPatch,
    args: dict[str, Any],
    cart_type: dict[str, Any],
    cart_mode: int,
    expected: tuple[int, int, int, int, bool],
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    records = install_preparation_io(device, monkeypatch, cart_mode=cart_mode)
    flashcart = PreparationFlashcart() if "flash_bank_size" in cart_type else False

    result = device._PrepareROMRead("AGB", args.copy(), cart_type, flashcart)  # type: ignore[arg-type]

    assert result is not None
    assert result.mbc is None
    assert tuple(result[1:]) == expected
    assert device.INFO["dump_info"]["mapper_type"] is None
    assert device.INFO["dump_info"]["rom_size"] == expected[0]
    expected_method = "3D Memory" if expected[-1] else device.AGB_READ_METHODS[device.AGB_READ_METHOD]
    assert device.INFO["dump_info"]["agb_read_method"] == expected_method
    assert records.firmware_reads == ["CART_MODE"]
    if cart_mode == 2:
        assert records.device_writes == []
        assert records.rom_reads == []
    else:
        assert records.device_writes == [(device.DEVICE_CMD["SET_MODE_AGB"], True)]
        assert records.rom_reads == [(0, 4)]


@pytest.mark.parametrize("verification", [False, True], ids=["ordinary", "verification"])
def test_prepare_rom_read_configures_gbamp_and_suppresses_verification_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    verification: bool,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    records = install_preparation_io(device, monkeypatch, cart_mode=2)
    flashcart = PreparationFlashcart()
    args: dict[str, Any] = {"agb_rom_size": 0x400000}
    if verification:
        args["verify_write"] = bytearray(0x28000)
    cart_type = {"command_set": "GBAMP", "flash_bank_size": 0x20000}

    result = device._PrepareROMRead("AGB", args, cart_type, flashcart)  # type: ignore[arg-type]

    assert result == (None, 0x400000, 2 if verification else 32, 0x20000, 0x4000, False)
    assert device.INFO["dump_info"]["agb_read_method"] == "GBA Movie Player"
    assert flashcart.unlock_calls == 1
    assert records.reconnects == int(not verification)
    assert records.device_writes == []
    assert records.rom_reads == []


@pytest.mark.parametrize("reset_before_bank_change", [False, True], ids=["select-only", "reset-first"])
def test_reset_rom_read_state_restores_dmg_bank_and_read_method(
    monkeypatch: pytest.MonkeyPatch,
    reset_before_bank_change: bool,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    mapper = PreparationMapper(reset_banks={0} if reset_before_bank_change else set())
    writes: list[tuple[object, bool]] = []
    set_read_method = Mock()

    def device_write(value: object, wait: bool = False) -> None:
        writes.append((value, wait))

    monkeypatch.setattr(device, "_write", device_write)
    monkeypatch.setattr(device, "SetDMGReadMethod", set_read_method)

    device._ResetROMReadState(mapper, {}, False, 3, 0)

    assert mapper.reset_requests == [0]
    assert mapper.selected_banks == [0]
    assert writes == ([(device.DEVICE_CMD["DMG_MBC_RESET"], True)] if reset_before_bank_change else [])
    set_read_method.assert_called_once_with(3)


@pytest.mark.parametrize(
    ("has_flashcart", "bank_select_type", "expected_banks"),
    [(False, 1, []), (True, 0, []), (True, 1, [0])],
    ids=["no-flashcart", "single-bank", "banked"],
)
def test_reset_rom_read_state_restores_agb_bank_only_for_banked_profile(
    monkeypatch: pytest.MonkeyPatch,
    has_flashcart: bool,
    bank_select_type: int,
    expected_banks: list[int],
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    mapper = PreparationMapper()
    flashcart = PreparationFlashcart()
    set_read_method = Mock()
    monkeypatch.setattr(device, "SetAGBReadMethod", set_read_method)

    device._ResetROMReadState(
        mapper,
        {"flash_bank_select_type": bank_select_type},
        flashcart if has_flashcart else False,  # type: ignore[arg-type]
        0,
        4,
    )

    assert mapper.reset_requests == []
    assert mapper.selected_banks == []
    assert flashcart.selected_banks == expected_banks
    set_read_method.assert_called_once_with(4)
