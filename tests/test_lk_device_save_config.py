"""Tests for save-transfer input preparation and configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import FlashGBX.LK_Device as lk_device_module
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


class SaveConfigMapper:
    """Small DMG mapper double for save-transfer configuration."""

    def __init__(
        self,
        name: str = "MBC3",
        *,
        ram_bank_size: int = 0x100,
        ram_banks: int = 3,
        rtc_size: int = 8,
    ) -> None:
        self.name = name
        self.ram_bank_size = ram_bank_size
        self.ram_banks = ram_banks
        self.rtc_size = rtc_size
        self.ram_size_requests: list[int] = []
        self.actions: list[tuple[object, ...]] = []

    def GetName(self) -> str:
        return self.name

    def GetRAMBanks(self, save_size: int) -> int:
        self.ram_size_requests.append(save_size)
        return self.ram_banks

    def GetRAMBankSize(self) -> int:
        return self.ram_bank_size

    def GetRTCBufferSize(self) -> int:
        return self.rtc_size

    def EnableMapper(self) -> None:
        self.actions.append(("enable_mapper",))

    def EnableFlash(self, *, enable: bool, enable_write: bool) -> None:
        self.actions.append(("enable_flash", enable, enable_write))

    def EnableRAM(self, *, enable: bool) -> None:
        self.actions.append(("enable_ram", enable))


class DmgConfigRecords:
    """Recorded calls at the device boundaries used by DMG configuration."""

    def __init__(self) -> None:
        self.writes: list[tuple[object, bool]] = []
        self.firmware_variables: list[tuple[str, int]] = []
        self.cart_writes: list[tuple[int, int]] = []
        self.cart_reads: list[tuple[int, int]] = []
        self.pin_changes: list[tuple[list[str], bool]] = []
        self.progress: list[dict[str, Any]] = []
        self.factory_kwargs: list[dict[str, object]] = []


def configure_dmg_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    mapper: SaveConfigMapper,
    *,
    firmware: int = 12,
    flash_id: bytearray | None = None,
) -> DmgConfigRecords:
    """Inject a mapper and recording device boundaries around real configuration."""
    records = DmgConfigRecords()
    device.fw = {"fw_ver": firmware, "pcb_name": "Test device"}

    def get_instance(_factory: object, **kwargs: object) -> SaveConfigMapper:
        records.factory_kwargs.append(kwargs)
        return mapper

    def cart_read(address: int, length: int, *args: object, **kwargs: object) -> bytearray:
        del args, kwargs
        records.cart_reads.append((address, length))
        return bytearray([0x00, 0x00]) if flash_id is None else bytearray(flash_id)

    def cart_write(address: int, value: int, *args: object, **kwargs: object) -> None:
        del args, kwargs
        records.cart_writes.append((address, value))

    monkeypatch.setattr(lk_device_module.DMG_Mapper, "GetInstance", get_instance)
    monkeypatch.setattr(
        device,
        "_write",
        lambda value, wait=False: records.writes.append((value, wait)),
    )
    monkeypatch.setattr(
        device,
        "_set_fw_variable",
        lambda name, value: records.firmware_variables.append((name, value)),
    )
    monkeypatch.setattr(device, "_cart_write", cart_write)
    monkeypatch.setattr(device, "_cart_read", cart_read)
    monkeypatch.setattr(
        device,
        "SetPin",
        lambda pins, *, set_high: records.pin_changes.append((list(pins), set_high)),
    )
    monkeypatch.setattr(device, "SetProgress", lambda event: records.progress.append(dict(event)))
    monkeypatch.setattr(device, "GetFullName", lambda: "Test device")
    return records


class AgbConfigRecords:
    """Recorded calls at the device boundaries used by AGB configuration."""

    def __init__(self) -> None:
        self.writes: list[tuple[object, bool]] = []
        self.firmware_variables: list[tuple[str, int]] = []
        self.cart_writes: list[tuple[int, int, dict[str, object]]] = []
        self.cart_reads: list[tuple[int, int, dict[str, object]]] = []
        self.progress: list[dict[str, Any]] = []
        self.flash_id_calls = 0
        self.ack_count = 0


def configure_agb_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    *,
    firmware: int = 12,
    flash_id_result: tuple[int, str] | bool | None = None,
    read_responses: list[bytearray | bool] | None = None,
) -> AgbConfigRecords:
    """Record AGB configuration commands with finite fake responses."""
    records = AgbConfigRecords()
    responses = [] if read_responses is None else list(read_responses)
    device.fw = {"fw_ver": firmware, "pcb_name": "Test device"}

    def cart_write(address: int, value: int, **kwargs: object) -> None:
        records.cart_writes.append((address, value, kwargs))

    def cart_read(address: int, length: int = 0, **kwargs: object) -> bytearray | bool:
        records.cart_reads.append((address, length, kwargs))
        if not responses:
            msg = f"Unexpected AGB save read at 0x{address:X}"
            raise AssertionError(msg)
        return responses.pop(0)

    def read_flash_save_id() -> tuple[int, str] | bool:
        records.flash_id_calls += 1
        if flash_id_result is None:
            msg = "Unexpected AGB flash-save ID request"
            raise AssertionError(msg)
        return flash_id_result

    def acknowledge() -> None:
        records.ack_count += 1

    monkeypatch.setattr(
        device,
        "_write",
        lambda value, wait=False: records.writes.append((value, wait)),
    )
    monkeypatch.setattr(
        device,
        "_set_fw_variable",
        lambda name, value: records.firmware_variables.append((name, value)),
    )
    monkeypatch.setattr(device, "_cart_write", cart_write)
    monkeypatch.setattr(device, "_cart_read", cart_read)
    monkeypatch.setattr(device, "ReadFlashSaveID", read_flash_save_id)
    monkeypatch.setattr(device, "wait_for_ack", acknowledge)
    monkeypatch.setattr(device, "SetProgress", lambda event: records.progress.append(dict(event)))
    return records


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
    device.mode = "DMG"
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
    assert device.info["action"] == device.ACTIONS["SAVE_READ"]
    assert progress == [{"action": "INITIALIZE", "method": "SAVE_READ", "size": 24}]


def test_verification_only_backup_suppresses_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    device.info["action"] = "existing action"
    args = {"mode": 2, "save_type": 3, "verify_write": bytearray(b"expected")}

    result, progress = prepare_action(device, monkeypatch, args, save_size=8, ram_banks=1, extra_size=4)

    assert result == (bytearray(), 1, 8)
    assert device.info["action"] == "existing action"
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
    device.mode = "DMG"
    args = {"mode": 3, "save_type": 1, "erase": False, "path": None, "buffer": source}

    (buffer, ram_banks, save_size), progress = prepare_action(device, monkeypatch, args)

    assert buffer == bytearray(b"DATA")
    assert (buffer is source) is same_object
    assert (ram_banks, save_size) == (2, 4)
    assert device.info["save_erase"] is False
    assert device.info["action"] == device.ACTIONS["SAVE_WRITE"]
    assert progress == [{"action": "INITIALIZE", "method": "SAVE_WRITE", "size": 4}]


def test_restore_uses_info_data_when_buffer_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    device.info["data"] = b"INFO"
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
    device.mode = "DMG"
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
    device.mode = mode
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
    device.mode = "DMG"
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
    assert device.info["save_erase"] is True
    assert progress == [{"action": "INITIALIZE", "method": "SAVE_WRITE", "size": 4}]


@pytest.mark.parametrize("source", [None, "text", 123, object()])
def test_restore_rejects_non_bytes_input(
    monkeypatch: pytest.MonkeyPatch,
    source: object,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    args = {"mode": 3, "save_type": 1, "erase": False, "path": None, "buffer": source}

    with pytest.raises(TypeError, match="bytes-like object"):
        prepare_action(device, monkeypatch, args)

    assert device.info["action"] is None


@pytest.mark.parametrize("source_location", ["buffer", "info", "file"])
def test_restore_rejects_empty_input_without_looping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_location: str,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    args: dict[str, Any] = {"mode": 3, "save_type": 1, "erase": False, "path": None}
    if source_location == "buffer":
        args["buffer"] = b""
    elif source_location == "info":
        device.info["data"] = bytearray()
    else:
        path = tmp_path / "empty.sav"
        path.write_bytes(b"")
        args["path"] = str(path)

    with pytest.raises(ValueError, match="Save data must not be empty"):
        prepare_action(device, monkeypatch, args)

    assert device.info["action"] is None


def test_unsupported_save_transfer_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.mode = "DMG"

    with pytest.raises(ValueError, match="Unsupported save transfer mode: 4"):
        prepare_action(device, monkeypatch, {"mode": 4})

    assert device.info["action"] is None


@pytest.mark.parametrize(
    "args",
    [{}, {"cart_type": 0}, {"cart_type": -1}],
    ids=["absent", "zero", "negative"],
)
def test_save_cart_type_returns_none_without_a_selected_profile(args: dict[str, int]) -> None:
    device = GbxDevice()
    device.fw = {"fw_ver": 12}
    device.supported_carts = {"DMG": {}, "AGB": {}}

    assert device._prepare_save_cart_type(args, "DMG") is None


def test_save_cart_type_returns_a_deep_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.fw = {"fw_ver": 11}
    original = {"names": ["Selected"], "commands": {"write": [[1, 2]]}}
    device.supported_carts = {
        "DMG": {"Placeholder": {"names": ["None"]}, "Selected": original},
        "AGB": {},
    }
    firmware_variables: list[tuple[str, int]] = []
    monkeypatch.setattr(
        device,
        "_set_fw_variable",
        lambda name, value: firmware_variables.append((name, value)),
    )

    selected = device._prepare_save_cart_type({"cart_type": 1}, "DMG")

    assert selected == original
    assert selected is not original
    assert selected is not None
    selected["names"].append("Changed")
    selected["commands"]["write"][0][0] = 99
    assert original == {"names": ["Selected"], "commands": {"write": [[1, 2]]}}
    assert firmware_variables == []


@pytest.mark.parametrize(
    ("firmware", "mode", "profile_pullup", "force_pullup", "expected"),
    [
        (11, "DMG", True, False, []),
        (12, "DMG", True, False, [("PULLUPS_ENABLED", 2)]),
        (12, "DMG", False, True, [("PULLUPS_ENABLED", 2)]),
        (12, "DMG", False, False, [("PULLUPS_ENABLED", 0)]),
        (12, "AGB", True, True, []),
    ],
    ids=["legacy", "profile", "override", "disabled", "agb"],
)
def test_save_cart_type_configures_wr_pullup_only_for_modern_dmg_firmware(
    monkeypatch: pytest.MonkeyPatch,
    firmware: int,
    mode: str,
    profile_pullup: bool,
    force_pullup: bool,
    expected: list[tuple[str, int]],
) -> None:
    device = GbxDevice()
    device.fw = {"fw_ver": firmware}
    profile = {"names": ["Selected"], "enable_pullup_wr": profile_pullup}
    device.supported_carts = {
        "DMG": {"Placeholder": {"names": ["None"]}, "Selected": profile},
        "AGB": {"Placeholder": {"names": ["None"]}, "Selected": profile},
    }
    firmware_variables: list[tuple[str, int]] = []
    monkeypatch.setattr(
        device,
        "_set_fw_variable",
        lambda name, value: firmware_variables.append((name, value)),
    )

    selected = device._prepare_save_cart_type(
        {"cart_type": 1, "force_wr_pullup": force_pullup},
        mode,
    )

    assert selected == profile
    assert firmware_variables == expected


@pytest.mark.parametrize(
    (
        "mapper_name",
        "transfer_mode",
        "rtc",
        "expected_buffer_len",
        "expected_empty_byte",
        "expected_extra_size",
        "expected_audio_low",
        "expected_firmware_variables",
        "expected_mapper_actions",
    ),
    [
        (
            "MBC3",
            2,
            False,
            0x100,
            0x00,
            0,
            False,
            [("DMG_WRITE_CS_PULSE", 0), ("DMG_READ_CS_PULSE", 0)],
            [("enable_mapper",), ("enable_ram", True)],
        ),
        (
            "TAMA5",
            2,
            True,
            0x20,
            0x00,
            8,
            False,
            [
                ("DMG_WRITE_CS_PULSE", 0),
                ("DMG_READ_CS_PULSE", 0),
                ("DMG_WRITE_CS_PULSE", 1),
                ("DMG_READ_CS_PULSE", 1),
                ("DMG_READ_CS_PULSE", 0),
            ],
            [("enable_mapper",), ("enable_ram", True)],
        ),
        (
            "MBC7",
            2,
            True,
            0x180,
            0x00,
            8,
            False,
            [("DMG_WRITE_CS_PULSE", 0), ("DMG_READ_CS_PULSE", 0)],
            [("enable_ram", True)],
        ),
        (
            "MBC6",
            2,
            False,
            0x100,
            0xFF,
            0,
            True,
            [
                ("DMG_WRITE_CS_PULSE", 0),
                ("DMG_READ_CS_PULSE", 0),
                ("FLASH_METHOD", 0x04),
                ("FLASH_WE_PIN", 0x01),
                ("FLASH_WE_PIN", 0x01),
            ],
            [("enable_flash", True, False), ("enable_ram", True)],
        ),
        (
            "MBC6",
            3,
            True,
            0x100,
            0xFF,
            8,
            True,
            [
                ("DMG_WRITE_CS_PULSE", 0),
                ("DMG_READ_CS_PULSE", 0),
                ("FLASH_METHOD", 0x04),
                ("FLASH_WE_PIN", 0x01),
                ("FLASH_WE_PIN", 0x01),
            ],
            [("enable_flash", True, True), ("enable_ram", True)],
        ),
    ],
    ids=["ordinary", "tama5", "mbc7", "mbc6-backup", "mbc6-restore"],
)
def test_dmg_save_configuration_for_mapper_variants(
    monkeypatch: pytest.MonkeyPatch,
    mapper_name: str,
    transfer_mode: int,
    rtc: bool,
    expected_buffer_len: int,
    expected_empty_byte: int,
    expected_extra_size: int,
    expected_audio_low: bool,
    expected_firmware_variables: list[tuple[str, int]],
    expected_mapper_actions: list[tuple[object, ...]],
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveConfigMapper(mapper_name)
    records = configure_dmg_boundaries(device, monkeypatch, mapper)
    args = {
        "mode": transfer_mode,
        "mbc": 0x20 if mapper_name == "MBC6" else 0x13,
        "save_type": 0x02,
        "save_size": 0x180,
        "rtc": rtc,
    }

    configuration = device._configure_dmg_save_transfer(args)

    assert configuration is not None
    assert configuration.mbc is mapper
    assert configuration.buffer_len == expected_buffer_len
    assert configuration.save_size == 0x180
    assert configuration.ram_banks == 3
    assert configuration.empty_data_byte == expected_empty_byte
    assert configuration.extra_size == expected_extra_size
    assert configuration.audio_low is expected_audio_low
    assert mapper.ram_size_requests == [0x180]
    assert mapper.actions == expected_mapper_actions
    assert records.writes == [
        (device.DEVICE_CMD["DMG_MBC_RESET"], True),
        (device.DEVICE_CMD["SET_MODE_DMG"], True),
    ]
    assert records.firmware_variables == expected_firmware_variables
    assert records.cart_writes == [
        (0x2000, 0x00),
        (0x4000, 0x90),
        (0x4000, 0xF0),
        (0x4000, 0xFF),
        (0x2000, 0x01),
        (0x4000, 0x00),
    ]
    assert records.cart_reads == [(0x4000, 2)]
    assert records.pin_changes == ([(["PIN_AUDIO"], False)] if expected_audio_low else [])
    assert len(records.factory_kwargs) == 1
    assert records.factory_kwargs[0]["args"] is args


def test_dmg_save_configuration_derives_size_from_save_type(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveConfigMapper(ram_bank_size=0x2000, ram_banks=1)
    configure_dmg_boundaries(device, monkeypatch, mapper, firmware=11)
    args = {"mode": 2, "mbc": 0x13, "save_type": 0x02, "rtc": False}

    configuration = device._configure_dmg_save_transfer(args)

    assert configuration is not None
    assert configuration.save_size == 0x2000
    assert configuration.buffer_len == 0x200
    assert configuration.ram_banks == 1
    assert mapper.ram_size_requests == [0x2000]


def test_dmg_save_configuration_rejects_unresolved_size(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveConfigMapper()
    records = configure_dmg_boundaries(device, monkeypatch, mapper)
    args = {"mode": 2, "mbc": 0x13, "save_type": 0x02, "save_size": None, "rtc": False}

    assert device._configure_dmg_save_transfer(args) is None

    assert mapper.ram_size_requests == []
    assert mapper.actions == []
    assert records.writes == [(device.DEVICE_CMD["DMG_MBC_RESET"], True)]
    assert records.firmware_variables == []
    assert records.cart_writes == []


def test_dmg_save_configuration_rejects_unsupported_mapper_before_enabling_ram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveConfigMapper()
    records = configure_dmg_boundaries(device, monkeypatch, mapper)
    args = {"mode": 2, "mbc": 0x999, "save_type": 0x02, "rtc": False}

    assert device._configure_dmg_save_transfer(args) is None

    assert mapper.ram_size_requests == []
    assert mapper.actions == []
    assert records.writes == [(device.DEVICE_CMD["DMG_MBC_RESET"], True)]
    assert records.firmware_variables == []
    assert records.cart_writes == []
    assert len(records.progress) == 1
    assert records.progress[0]["action"] == "ABORT"
    assert records.progress[0]["info_type"] == "msgbox_critical"
    assert records.progress[0]["abortable"] is False
    assert "Test device" in records.progress[0]["info_msg"]


@pytest.mark.parametrize(
    ("flash_id", "expected_audio_low"),
    [
        (bytearray([0x00, 0x00]), False),
        (bytearray([0xB0, 0x88]), True),
    ],
    ids=["ordinary-cart", "development-cart"],
)
def test_development_flash_id_controls_audio_pin(
    monkeypatch: pytest.MonkeyPatch,
    flash_id: bytearray,
    expected_audio_low: bool,
) -> None:
    device = GbxDevice()
    device.mode = "DMG"
    mapper = SaveConfigMapper()
    records = configure_dmg_boundaries(device, monkeypatch, mapper, flash_id=flash_id)
    args = {"mode": 2, "mbc": 0x13, "save_type": 0x02, "save_size": 0x100, "rtc": False}

    configuration = device._configure_dmg_save_transfer(args)

    assert configuration is not None
    assert configuration.audio_low is expected_audio_low
    assert records.pin_changes == ([(["PIN_AUDIO"], False)] if expected_audio_low else [])
    expected_variables = [("DMG_WRITE_CS_PULSE", 0), ("DMG_READ_CS_PULSE", 0)]
    if expected_audio_low:
        expected_variables.append(("FLASH_WE_PIN", 0x01))
    assert records.firmware_variables == expected_variables


@pytest.mark.parametrize(
    (
        "save_type",
        "transfer_mode",
        "rtc",
        "expected_size",
        "expected_banks",
        "expected_buffer_len",
        "expected_empty_byte",
        "command_name",
        "command_argument",
        "flash_chip",
    ),
    [
        (1, 2, False, 512, 1, 256, 0x00, "AGB_CART_READ_EEPROM", 1, 0),
        (1, 3, False, 512, 1, 64, 0x00, "AGB_CART_WRITE_EEPROM", 1, 0),
        (2, 2, False, 8192, 1, 256, 0x00, "AGB_CART_READ_EEPROM", 2, 0),
        (2, 3, False, 8192, 1, 64, 0x00, "AGB_CART_WRITE_EEPROM", 2, 0),
        (3, 2, True, 32768, 1, 0x2000, 0x00, "AGB_CART_READ_SRAM", None, 0),
        (3, 3, False, 32768, 1, 0x2000, 0x00, "AGB_CART_WRITE_SRAM", None, 0),
        (4, 2, False, 65536, 1, 0x1000, 0xFF, "AGB_CART_READ_SRAM", None, 0xBFD4),
        (4, 3, False, 65536, 1, 0x1000, 0xFF, "AGB_CART_WRITE_FLASH_DATA", 1, 0xBFD4),
        (5, 3, False, 131072, 2, 0x1000, 0xFF, "AGB_CART_WRITE_FLASH_DATA", 1, 0xC209),
    ],
    ids=[
        "eeprom-4k-read",
        "eeprom-4k-write",
        "eeprom-64k-read",
        "eeprom-64k-write",
        "sram-read-with-rtc",
        "sram-write",
        "flash-read",
        "flash-512k-write",
        "flash-1m-write",
    ],
)
def test_agb_save_types_select_exact_transfer_configuration(
    monkeypatch: pytest.MonkeyPatch,
    save_type: int,
    transfer_mode: int,
    rtc: bool,
    expected_size: int,
    expected_banks: int,
    expected_buffer_len: int,
    expected_empty_byte: int,
    command_name: str,
    command_argument: int | None,
    flash_chip: int,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    flash_id_result = (flash_chip, "Test flash") if flash_chip else None
    records = configure_agb_boundaries(device, monkeypatch, flash_id_result=flash_id_result)
    args = {"mode": transfer_mode, "save_type": save_type, "rtc": rtc}

    configuration = device._configure_agb_save_transfer(args, cart_type=None)

    assert configuration is not None
    assert configuration.buffer_len == expected_buffer_len
    assert configuration.save_size == expected_size
    assert configuration.ram_banks == expected_banks
    assert configuration.flash_chip == flash_chip
    assert configuration.sram_5 == 0
    expected_command: int | bytearray = device.DEVICE_CMD[command_name]
    if command_argument is not None:
        expected_command = bytearray([expected_command, command_argument])
    assert configuration.command == expected_command
    assert configuration.empty_data_byte == expected_empty_byte
    assert configuration.extra_size == (0x10 if rtc else 0)
    assert records.writes == [
        (device.DEVICE_CMD["SET_MODE_AGB"], True),
        (device.DEVICE_CMD["SET_VOLTAGE_3_3V"], True),
    ]
    assert records.flash_id_calls == (1 if flash_chip else 0)
    assert records.firmware_variables == []
    assert records.cart_reads == []
    assert records.cart_writes == []
    assert records.progress == []


@pytest.mark.parametrize(
    ("flash_chip", "expected_buffer_len", "command_argument"),
    [
        (0xBFD4, 0x1000, 1),
        (0x1F3D, 0x1000, 2),
        (0xBF4B, 0x800, 1),
        (0xBF6D, 0x8000, 1),
    ],
    ids=["normal", "atmel", "bootleg-small-buffer", "bootleg-large-buffer"],
)
def test_agb_flash_chip_controls_buffer_and_write_command(
    monkeypatch: pytest.MonkeyPatch,
    flash_chip: int,
    expected_buffer_len: int,
    command_argument: int,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    records = configure_agb_boundaries(
        device,
        monkeypatch,
        flash_id_result=(flash_chip, "Test flash"),
    )
    args = {"mode": 3, "save_type": 4, "rtc": False}

    configuration = device._configure_agb_save_transfer(args, cart_type=None)

    assert configuration is not None
    assert configuration.buffer_len == expected_buffer_len
    assert configuration.flash_chip == flash_chip
    assert configuration.command == bytearray(
        [device.DEVICE_CMD["AGB_CART_WRITE_FLASH_DATA"], command_argument],
    )
    assert configuration.empty_data_byte == 0xFF
    assert records.flash_id_calls == 1


@pytest.mark.parametrize("detect", [False, True], ids=["interactive", "detection"])
def test_agb_flash_id_failure_stops_before_command_selection(
    monkeypatch: pytest.MonkeyPatch,
    detect: bool,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    records = configure_agb_boundaries(device, monkeypatch, flash_id_result=False)
    args = {"mode": 3, "save_type": 4, "rtc": False, "detect": detect}

    assert device._configure_agb_save_transfer(args, cart_type=None) is None

    assert records.writes == [
        (device.DEVICE_CMD["SET_MODE_AGB"], True),
        (device.DEVICE_CMD["SET_VOLTAGE_3_3V"], True),
    ]
    assert records.flash_id_calls == 1
    assert records.firmware_variables == []
    assert records.cart_reads == []
    assert records.cart_writes == []
    assert len(records.progress) == (0 if detect else 1)
    if records.progress:
        assert records.progress[0]["action"] == "ABORT"
        assert records.progress[0]["info_type"] == "msgbox_critical"
        assert records.progress[0]["abortable"] is False


@pytest.mark.parametrize("firmware", [11, 12])
def test_dacs_configuration_encodes_commands_and_acknowledges_modern_firmware(
    monkeypatch: pytest.MonkeyPatch,
    firmware: int,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    records = configure_agb_boundaries(
        device,
        monkeypatch,
        firmware=firmware,
        read_responses=[bytearray([0xB0, 0x00, 0x9F, 0x00])],
    )
    args = {"mode": 3, "save_type": 6, "rtc": True}

    configuration = device._configure_agb_save_transfer(args, cart_type=None)

    assert configuration is not None
    assert configuration.buffer_len == 0x2000
    assert configuration.save_size == 0x100000
    assert configuration.ram_banks == 1
    assert configuration.flash_chip == 0
    assert configuration.sram_5 == 0
    assert configuration.command is False
    assert configuration.empty_data_byte == 0xFF
    assert configuration.extra_size == 0x10
    wait = firmware >= 12
    command_records = [
        bytearray([0x00, 0x00, 0x00, 0x00, 0x00, value]) for value in (0x70, 0x10, 0x00, 0x00, 0x00, 0x00)
    ]
    assert records.writes == [
        (device.DEVICE_CMD["SET_MODE_AGB"], wait),
        (device.DEVICE_CMD["SET_VOLTAGE_3_3V"], wait),
        (device.DEVICE_CMD["SET_FLASH_CMD"], False),
        (0x02, False),
        (0x01, False),
        (0x00, False),
        *((record, False) for record in command_records),
    ]
    assert records.firmware_variables == [("FLASH_SHARP_VERIFY_SR", 1)]
    assert records.cart_reads == [(0, 4, {})]
    assert records.cart_writes == [(0, 0x90, {}), (0, 0x50, {}), (0, 0xFF, {})]
    assert records.ack_count == (1 if wait else 0)
    assert records.progress == []


@pytest.mark.parametrize("detect", [False, True], ids=["interactive", "detection"])
def test_dacs_unknown_id_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    detect: bool,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    records = configure_agb_boundaries(
        device,
        monkeypatch,
        read_responses=[bytearray([0x00, 0x01, 0x02, 0x03])],
    )
    args = {"mode": 2, "save_type": 6, "rtc": False, "detect": detect}

    assert device._configure_agb_save_transfer(args, cart_type=None) is None

    assert records.writes == [
        (device.DEVICE_CMD["SET_MODE_AGB"], True),
        (device.DEVICE_CMD["SET_VOLTAGE_3_3V"], True),
    ]
    assert records.firmware_variables == []
    assert records.cart_reads == [(0, 4, {})]
    assert records.cart_writes == [(0, 0x90, {}), (0, 0x50, {}), (0, 0xFF, {})]
    assert records.ack_count == 0
    assert len(records.progress) == (0 if detect else 1)
    if records.progress:
        assert "00 01 02 03" in records.progress[0]["info_msg"]


def test_agb_bank_select_profile_captures_sram_byte_before_switching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    records = configure_agb_boundaries(
        device,
        monkeypatch,
        read_responses=[bytearray([0xA5])],
    )
    args = {"mode": 2, "save_type": 3, "rtc": False}

    configuration = device._configure_agb_save_transfer(args, cart_type={"flash_bank_select_type": 1})

    assert configuration is not None
    assert configuration.sram_5 == 0xA5
    assert configuration.command == device.DEVICE_CMD["AGB_CART_READ_SRAM"]
    assert records.cart_reads == [(5, 1, {"agb_save_flash": True})]
    assert records.cart_writes == [(5, 1, {"sram": True})]


def test_agb_bank_select_profile_stops_when_sram_byte_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    records = configure_agb_boundaries(device, monkeypatch, read_responses=[False])
    args = {"mode": 3, "save_type": 3, "rtc": False}

    assert device._configure_agb_save_transfer(args, cart_type={"flash_bank_select_type": 1}) is None

    assert records.cart_reads == [(5, 1, {"agb_save_flash": True})]
    assert records.cart_writes == []
    assert records.progress == []


def test_agb_save_configuration_rejects_unknown_save_size(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.mode = "AGB"
    records = configure_agb_boundaries(device, monkeypatch)
    args = {"mode": 2, "save_type": 99, "rtc": False}

    assert device._configure_agb_save_transfer(args, cart_type=None) is None

    assert records.writes == [
        (device.DEVICE_CMD["SET_MODE_AGB"], True),
        (device.DEVICE_CMD["SET_VOLTAGE_3_3V"], True),
    ]
    assert records.flash_id_calls == 0
    assert records.cart_reads == []
    assert records.cart_writes == []
