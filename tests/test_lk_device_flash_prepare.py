"""Tests for flash-write input transformation."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

import FlashGBX.LK_Device as lk_device_module
from FlashGBX.Flashcart import Flashcart
from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.LK_Device import _FlashConfiguration, _FlashSectorPlan
from tests.fakes import flashcart_callbacks, flashcart_profile

if TYPE_CHECKING:
    from pathlib import Path


FLASH_BLOCK_SIZE = 0x4000
BATTERYLESS_BLOCK_SIZE = 0x2000
AGB_32_MIB = 0x2000000
AGB_EEPROM_RESERVED_SIZE = 0x100


class WritePinFlashcart:
    """Expose the write-enable pin predicates used by command setup."""

    def __init__(self, pin: str | None) -> None:
        self.pin = pin

    def WEisWR(self) -> bool:
        return self.pin == "WR"

    def WEisAUDIO(self) -> bool:
        return self.pin == "AUDIO"

    def WEisWR_RESET(self) -> bool:
        return self.pin == "WR+RESET"


class FlashCommandRecords:
    """Device boundary calls emitted while loading flash commands."""

    def __init__(self) -> None:
        self.writes: list[tuple[int | bytearray, bool]] = []
        self.reads: list[int] = []
        self.firmware_variables: list[tuple[str, int]] = []
        self.progress: list[dict[str, object]] = []
        self.ack_count = 0


class EraseChoiceFlashcart:
    """Minimal flash-cart surface used to verify erase selection."""

    def __init__(
        self,
        *,
        supports_chip: bool,
        supports_sector: bool,
        chip_result: bool = True,
    ) -> None:
        self.supports_chip = supports_chip
        self.supports_sector = supports_sector
        self.chip_result = chip_result
        self.chip_erase_calls = 0

    def SupportsChipErase(self) -> bool:
        return self.supports_chip

    def SupportsSectorErase(self) -> bool:
        return self.supports_sector

    def ChipErase(self) -> bool:
        self.chip_erase_calls += 1
        return self.chip_result


class RecordingFlashMapper:
    """Small mapper surface used by flashcart write configuration tests."""

    def __init__(
        self,
        events: list[tuple[object, ...]],
        *,
        bank_size: int = 0x4000,
        max_size: int = 0x8000,
        name: str = "Test MBC",
    ) -> None:
        self.events = events
        self.bank_size = bank_size
        self.max_size = max_size
        self.name = name

    def GetROMBankSize(self) -> int:
        return self.bank_size

    def GetMaxROMSize(self) -> int:
        return self.max_size

    def GetName(self) -> str:
        return self.name

    def EnableMapper(self) -> bool:
        self.events.append(("mapper-enable",))
        return True


class RecordingMapperFactory:
    """Record mapper construction and return one deterministic mapper."""

    def __init__(self, mapper: RecordingFlashMapper, events: list[tuple[object, ...]]) -> None:
        self.mapper = mapper
        self.events = events
        self.arguments: list[dict[str, object]] = []

    def GetInstance(self, **kwargs: object) -> RecordingFlashMapper:
        self.arguments.append(kwargs)
        args = kwargs["args"]
        assert isinstance(args, dict)
        self.events.append(("mapper-create", args["mbc"]))
        return self.mapper


class FlashConfigurationRecords:
    """Calls made while configuring a flashcart for writing."""

    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self.writes: list[tuple[int, bool]] = []
        self.variables: list[tuple[str, int]] = []
        self.progress: list[dict[str, object]] = []


def install_flash_configuration_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    *,
    firmware: int,
    mapper_bank_size: int = 0x4000,
    mapper_max_size: int = 0x8000,
) -> tuple[FlashConfigurationRecords, RecordingMapperFactory]:
    """Record setup boundaries while retaining real profile accessors."""
    records = FlashConfigurationRecords()
    device.FW = {"fw_ver": firmware}
    mapper = RecordingFlashMapper(
        records.events,
        bank_size=mapper_bank_size,
        max_size=mapper_max_size,
    )
    factory = RecordingMapperFactory(mapper, records.events)

    def write(value: int, wait: bool = False) -> None:
        records.writes.append((value, wait))
        records.events.append(("write", value, wait))

    def set_firmware_variable(name: str, value: int) -> None:
        records.variables.append((name, value))
        records.events.append(("variable", name, value))

    def set_progress(update: dict[str, object]) -> None:
        records.progress.append(dict(update))
        records.events.append(("progress", update["action"]))

    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "_set_fw_variable", set_firmware_variable)
    monkeypatch.setattr(device, "SetProgress", set_progress)
    monkeypatch.setattr(lk_device_module, "DMG_Mapper", lambda: factory)
    return records, factory


def make_command_flashcart(**overrides: object) -> tuple[dict[str, Any], Flashcart]:
    """Build a real Flashcart and return its matching profile."""
    profile = flashcart_profile(**overrides)
    _calls, callbacks = flashcart_callbacks()
    return profile, Flashcart(profile, callbacks)


def install_flash_command_boundaries(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    *,
    firmware: int,
    mode: str = "DMG",
    bank_response: int = 1,
) -> FlashCommandRecords:
    """Record the device protocol around real flash-command loading."""
    records = FlashCommandRecords()
    device.MODE = mode  # type: ignore[assignment]
    device.FW = {"fw_ver": firmware}

    def write(value: int | bytes | bytearray, wait: bool = False) -> None:
        recorded = bytearray(value) if isinstance(value, (bytes, bytearray)) else value
        records.writes.append((recorded, wait))

    def read(count: int) -> int:
        records.reads.append(count)
        return bank_response

    def set_firmware_variable(name: str, value: int) -> None:
        records.firmware_variables.append((name, value))

    def set_progress(update: dict[str, object]) -> None:
        records.progress.append(dict(update))

    def wait_for_ack() -> int:
        records.ack_count += 1
        return 1

    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "_read", read)
    monkeypatch.setattr(device, "_set_fw_variable", set_firmware_variable)
    monkeypatch.setattr(device, "SetProgress", set_progress)
    monkeypatch.setattr(device, "wait_for_ack", wait_for_ack)
    return records


def encode_command_records(
    commands: list[list[int | str | None]],
    *,
    agb: bool = False,
) -> list[bytearray]:
    """Encode the six firmware records expected from a profile command list."""
    records: list[bytearray] = []
    for index in range(6):
        address, value = commands[index] if index < len(commands) else (0, 0)
        numeric_address = address if isinstance(address, int) else 0
        numeric_value = value if isinstance(value, int) else 0
        if agb:
            numeric_address >>= 1
        records.append(bytearray(struct.pack(">IH", numeric_address, numeric_value)))
    return records


@pytest.mark.parametrize(
    "source",
    [b"ROM", bytearray(b"ROM"), memoryview(b"ROM")],
    ids=["bytes", "bytearray", "memoryview"],
)
def test_prepare_flash_data_accepts_bytes_like_buffers(
    source: bytes | bytearray | memoryview,
) -> None:
    device = GbxDevice()

    result, flash_offset = device._prepare_flash_data({"buffer": source}, "DMG")

    assert result == bytearray(b"ROM" + b"\xff" * (FLASH_BLOCK_SIZE - 3))
    assert flash_offset == 0


def test_prepare_flash_data_reads_a_file(tmp_path: Path) -> None:
    device = GbxDevice()
    source_path = tmp_path / "input.gbc"
    source_path.write_bytes(b"FILE")

    result, flash_offset = device._prepare_flash_data({"path": source_path}, "DMG")

    assert result == bytearray(b"FILE" + b"\xff" * (FLASH_BLOCK_SIZE - 4))
    assert flash_offset == 0


def test_prepare_flash_data_rejects_non_bytes_like_buffer() -> None:
    device = GbxDevice()

    with pytest.raises(TypeError, match="ROM data must be a bytes-like object"):
        device._prepare_flash_data({"buffer": "not ROM bytes"}, "DMG")


def test_prepare_flash_data_keeps_empty_input_empty() -> None:
    device = GbxDevice()

    result, flash_offset = device._prepare_flash_data({"buffer": bytearray()}, "DMG")

    assert result == bytearray()
    assert flash_offset == 0


def test_prepare_flash_data_preserves_flash_offset_and_prefixes_start_address() -> None:
    device = GbxDevice()
    args = {
        "buffer": b"ROM",
        "flash_offset": 0x2400,
        "start_addr": 4,
    }

    result, flash_offset = device._prepare_flash_data(args, "DMG")

    expected_prefix = b"\xff" * 4 + b"ROM"
    assert result == bytearray(expected_prefix + b"\xff" * (FLASH_BLOCK_SIZE - len(expected_prefix)))
    assert flash_offset == 0x2400


@pytest.mark.parametrize(
    ("source_length", "expected_length"),
    [
        (FLASH_BLOCK_SIZE - 1, FLASH_BLOCK_SIZE),
        (FLASH_BLOCK_SIZE, FLASH_BLOCK_SIZE),
        (FLASH_BLOCK_SIZE + 1, FLASH_BLOCK_SIZE * 2),
    ],
    ids=["below-block", "at-block", "above-block"],
)
def test_prepare_flash_data_pads_to_flash_block_boundary(
    source_length: int,
    expected_length: int,
) -> None:
    device = GbxDevice()
    source = bytes([0x5A]) * source_length

    result, _flash_offset = device._prepare_flash_data({"buffer": source}, "DMG")

    assert result[:source_length] == source
    assert result[source_length:] == bytearray([0xFF]) * (expected_length - source_length)
    assert len(result) == expected_length


@pytest.mark.parametrize("layout", [1, 2], ids=["first-half", "second-half"])
@pytest.mark.parametrize("source_blocks", [1, 2], ids=["one-block", "two-blocks"])
def test_prepare_flash_data_places_batteryless_blocks_in_selected_half_bank(
    layout: int,
    source_blocks: int,
) -> None:
    device = GbxDevice()
    source = bytearray()
    for block in range(source_blocks):
        source += bytearray([0x11 + block]) * BATTERYLESS_BLOCK_SIZE
    args = {
        "buffer": source,
        "bl_layout": layout,
        "bl_size": len(source),
        "flash_size": 0x12000,
    }

    result, flash_offset = device._prepare_flash_data(args, "AGB")

    expected = bytearray([0xFF]) * (len(source) * 2)
    half_bank_offset = 0 if layout == 1 else BATTERYLESS_BLOCK_SIZE
    for block in range(source_blocks):
        source_start = block * BATTERYLESS_BLOCK_SIZE
        destination_start = block * FLASH_BLOCK_SIZE + half_bank_offset
        expected[destination_start : destination_start + BATTERYLESS_BLOCK_SIZE] = source[
            source_start : source_start + BATTERYLESS_BLOCK_SIZE
        ]
    assert result == expected
    assert args["bl_size"] == len(source) * 2
    assert args["flash_size"] == 0x24000
    assert flash_offset == 0


def test_prepare_flash_data_repairs_dmg_logo_and_checksums_only() -> None:
    device = GbxDevice()
    source = bytearray((index * 17 + 3) & 0xFF for index in range(FLASH_BLOCK_SIZE))
    replacement_logo = bytearray((index * 5 + 1) & 0xFF for index in range(0x30))
    expected = source.copy()
    expected[0x104:0x134] = replacement_logo
    header_checksum = 0
    for value in expected[0x134:0x14D]:
        header_checksum = (header_checksum - value - 1) & 0xFF
    expected[0x14D] = header_checksum
    expected[0x14E:0x150] = b"\x00\x00"
    expected[0x14E:0x150] = (sum(expected[:0x200]) & 0xFFFF).to_bytes(2, "big")

    result, _flash_offset = device._prepare_flash_data(
        {"buffer": source, "fix_bootlogo": replacement_logo, "fix_header": True},
        "DMG",
    )

    assert result == expected
    assert result[0x104:0x134] == replacement_logo
    assert result[0x134:0x14D] == source[0x134:0x14D]
    assert result[0x150:] == source[0x150:]


def test_prepare_flash_data_repairs_agb_logo_and_checksum_only() -> None:
    device = GbxDevice()
    source = bytearray((index * 11 + 7) & 0xFF for index in range(FLASH_BLOCK_SIZE))
    replacement_logo = bytearray((index * 3 + 2) & 0xFF for index in range(0x9C))
    expected = source.copy()
    expected[0x04:0xA0] = replacement_logo
    expected[0xBD] = (-sum(expected[0xA0:0xBD]) - 0x19) & 0xFF

    result, _flash_offset = device._prepare_flash_data(
        {"buffer": source, "fix_bootlogo": replacement_logo, "fix_header": True},
        "AGB",
    )

    assert result == expected
    assert result[0x04:0xA0] == replacement_logo
    assert result[0xA0:0xBD] == source[0xA0:0xBD]
    assert result[0xBE:] == source[0xBE:]


def make_32_mib_agb_image(signature: bytes) -> bytearray:
    """Build a generated 32 MiB image with a save-library marker."""
    image = bytearray(AGB_32_MIB)
    signature_offset = 0x200
    image[signature_offset : signature_offset + len(signature)] = signature
    return image


def test_prepare_flash_data_trims_reserved_area_for_32_mib_eeprom_image() -> None:
    device = GbxDevice()
    source = make_32_mib_agb_image(b"EEPROM_V124\x00")
    source[-AGB_EEPROM_RESERVED_SIZE - 1] = 0x5A
    source[-AGB_EEPROM_RESERVED_SIZE:] = bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE

    result, _flash_offset = device._prepare_flash_data({"buffer": source}, "AGB")

    assert result == source[:-AGB_EEPROM_RESERVED_SIZE]
    assert len(result) == AGB_32_MIB - AGB_EEPROM_RESERVED_SIZE
    assert result[-1] == 0x5A


def test_prepare_flash_data_keeps_reserved_area_for_non_eeprom_32_mib_image() -> None:
    device = GbxDevice()
    source = make_32_mib_agb_image(b"SRAM_V123\x00")
    source[-AGB_EEPROM_RESERVED_SIZE:] = bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE

    result, _flash_offset = device._prepare_flash_data({"buffer": source}, "AGB")

    assert len(result) == AGB_32_MIB
    assert result[-AGB_EEPROM_RESERVED_SIZE:] == bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE


def test_prepare_flash_data_keeps_reserved_area_for_malformed_eeprom_signature() -> None:
    device = GbxDevice()
    malformed_signature = b"EEPROM_V" + b"X" * (0x20 - len(b"EEPROM_V"))
    source = make_32_mib_agb_image(malformed_signature)
    source[-AGB_EEPROM_RESERVED_SIZE:] = bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE

    result, _flash_offset = device._prepare_flash_data({"buffer": source}, "AGB")

    assert len(result) == AGB_32_MIB
    assert result[-AGB_EEPROM_RESERVED_SIZE:] == bytearray([0xC3]) * AGB_EEPROM_RESERVED_SIZE


@pytest.mark.parametrize(
    ("command_set", "expected_id", "expected_status_flag"),
    [
        ("AMD", 0x01, None),
        ("INTEL", 0x02, 0),
        ("SHARP", 0x02, 1),
        ("GBMEMORY", 0x00, None),
        ("DMG-MBC5-32M-FLASH", 0x00, None),
        ("BLAZE_XPLODER", 0x00, None),
        ("DATEL_ORBITV2", 0x00, None),
        ("EEPROM", 0x00, None),
        ("GBAMP", 0x00, None),
        ("BUNG_16M", 0x00, None),
    ],
)
def test_configure_flash_command_set_selects_firmware_id_and_status_flag(
    monkeypatch: pytest.MonkeyPatch,
    command_set: str,
    expected_id: int,
    expected_status_flag: int | None,
) -> None:
    device = GbxDevice()
    firmware_variables: list[tuple[str, int]] = []
    progress: list[dict[str, object]] = []
    monkeypatch.setattr(
        device,
        "_set_fw_variable",
        lambda name, value: firmware_variables.append((name, value)),
    )
    monkeypatch.setattr(device, "SetProgress", lambda update: progress.append(dict(update)))

    assert device._configure_flash_command_set(command_set) == expected_id

    expected_variables = [] if expected_status_flag is None else [("FLASH_SHARP_VERIFY_SR", expected_status_flag)]
    assert firmware_variables == expected_variables
    assert progress == []


def test_configure_flash_command_set_aborts_unsupported_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    progress: list[dict[str, object]] = []
    monkeypatch.setattr(device, "SetProgress", lambda update: progress.append(dict(update)))

    assert device._configure_flash_command_set("UNKNOWN") is None

    assert len(progress) == 1
    assert progress[0]["action"] == "ABORT"
    assert progress[0]["info_type"] == "msgbox_critical"
    assert progress[0]["abortable"] is False
    assert "not supported" in str(progress[0]["info_msg"])


@pytest.mark.parametrize(
    ("pin", "expected_value"),
    [("WR", 0x01), ("AUDIO", 0x02), ("WR+RESET", 0x03), (None, 0x00)],
    ids=["wr", "audio", "wr-reset", "unset"],
)
def test_configure_flash_write_pin_sends_selected_value(
    monkeypatch: pytest.MonkeyPatch,
    pin: str | None,
    expected_value: int,
) -> None:
    device = GbxDevice()
    write = Mock()
    monkeypatch.setattr(device, "_write", write)

    result = device._configure_flash_write_pin(WritePinFlashcart(pin))  # type: ignore[arg-type]

    assert result == expected_value
    write.assert_called_once_with(expected_value)


@pytest.mark.parametrize(
    ("mode", "firmware", "address_divisor"),
    [("DMG", 11, 1), ("AGB", 12, 2)],
    ids=["dmg-firmware-11", "agb-firmware-12"],
)
def test_send_flash_commands_encodes_six_big_endian_records_and_modern_ack(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    firmware: int,
    address_divisor: int,
) -> None:
    device = GbxDevice()
    device.MODE = mode  # type: ignore[assignment]
    device.FW = {"fw_ver": firmware}
    writes: list[bytearray] = []
    wait_for_ack = Mock()

    def record_write(record: bytearray) -> None:
        writes.append(bytearray(record))

    monkeypatch.setattr(device, "_write", record_write)
    monkeypatch.setattr(device, "wait_for_ack", wait_for_ack)
    commands: list[list[int | str | None]] = [
        [0x12345678, 0x9ABC],
        ["SA", 0x1234],
        [0x11223344, "PD"],
        [None, None],
    ]
    expected_values = [
        (0x12345678 // address_divisor, 0x9ABC),
        (0, 0x1234),
        (0x11223344 // address_divisor, 0),
        (0, 0),
        (0, 0),
        (0, 0),
    ]

    device._send_flash_commands(commands)

    assert writes == [bytearray(struct.pack(">IH", address, value)) for address, value in expected_values]
    assert len(writes) == 6
    assert all(len(record) == 6 for record in writes)
    assert wait_for_ack.call_count == (1 if firmware >= 12 else 0)


@pytest.mark.parametrize(
    ("commands", "expected_method", "expected_commands"),
    [
        (
            {"buffer_write": [[0x1111, 0x22]], "single_write": [[0x3333, 0x44]]},
            0x02,
            [[0x1111, 0x22]],
        ),
        (
            {"page_write": [[0x2222, 0x33]], "single_write": [[0x3333, 0x44]]},
            0x08,
            [[0x2222, 0x33]],
        ),
        ({"single_write": [[0x3333, 0x44]]}, 0x01, [[0x3333, 0x44]]),
    ],
    ids=["buffered", "page", "single"],
)
def test_load_flash_commands_selects_supported_write_method(
    monkeypatch: pytest.MonkeyPatch,
    commands: dict[str, list[list[int]]],
    expected_method: int,
    expected_commands: list[list[int | str | None]],
) -> None:
    device = GbxDevice()
    records = install_flash_command_boundaries(device, monkeypatch, firmware=12)
    cart_type, flashcart = make_command_flashcart(commands=commands)

    result = device._load_flash_commands(cart_type, flashcart, flash_buffer_size=4)

    expected_writes: list[tuple[int | bytearray, bool]] = [
        (device.DEVICE_CMD["SET_FLASH_CMD"], False),
        (0x01, False),
        (expected_method, False),
        (0x01, False),
        *((record, False) for record in encode_command_records(expected_commands)),
        (device.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"], False),
        (0x00, True),
    ]
    assert result == ("AMD", 0x01)
    assert records.writes == expected_writes
    assert records.firmware_variables == [
        ("FLASH_DOUBLE_DIE", 0),
        ("FLASH_COMMANDS_BANK_1", 0),
        ("STATUS_REGISTER_MASK", 0x80),
        ("STATUS_REGISTER_VALUE", 0x80),
        ("AGB_IRQ_ENABLED", 0),
    ]
    assert records.ack_count == 1
    assert records.reads == []
    assert records.progress == []


@pytest.mark.parametrize(
    ("firmware", "commands"),
    [(11, {"page_write": [[0x2222, 0x33]]}), (12, {})],
    ids=["page-before-firmware-12", "no-write-capability"],
)
def test_load_flash_commands_rejects_unsupported_write_capability_before_payload(
    monkeypatch: pytest.MonkeyPatch,
    firmware: int,
    commands: dict[str, list[list[int]]],
) -> None:
    device = GbxDevice()
    records = install_flash_command_boundaries(device, monkeypatch, firmware=firmware)
    cart_type, flashcart = make_command_flashcart(commands=commands)

    assert device._load_flash_commands(cart_type, flashcart, flash_buffer_size=4) is None

    assert records.writes == [
        (device.DEVICE_CMD["SET_FLASH_CMD"], False),
        (0x01, False),
    ]
    assert records.firmware_variables == [("FLASH_DOUBLE_DIE", 0)]
    assert records.ack_count == 0
    assert records.reads == []
    assert len(records.progress) == 1
    assert records.progress[0]["action"] == "ABORT"
    assert records.progress[0]["abortable"] is False


@pytest.mark.parametrize(
    ("command_set", "firmware", "expected_method", "legacy_pin"),
    [
        ("GBMEMORY", 1, None, 0x01),
        ("GBMEMORY", 2, 0x03, None),
        ("DMG-MBC5-32M-FLASH", 11, None, 0x02),
        ("DMG-MBC5-32M-FLASH", 12, 0x0A, None),
    ],
    ids=["gbmemory-legacy", "gbmemory-modern", "e201264-legacy", "e201264-modern"],
)
def test_load_flash_commands_selects_legacy_and_modern_firmware_paths(
    monkeypatch: pytest.MonkeyPatch,
    command_set: str,
    firmware: int,
    expected_method: int | None,
    legacy_pin: int | None,
) -> None:
    device = GbxDevice()
    records = install_flash_command_boundaries(device, monkeypatch, firmware=firmware)
    cart_type, flashcart = make_command_flashcart(command_set=command_set, commands={})

    result = device._load_flash_commands(cart_type, flashcart, flash_buffer_size=4)

    if expected_method is None:
        expected_writes: list[tuple[int | bytearray, bool]] = []
        expected_variables = [("FLASH_DOUBLE_DIE", 0), ("FLASH_WE_PIN", legacy_pin)]
        expected_we = 0
    else:
        expected_writes = [
            (device.DEVICE_CMD["SET_FLASH_CMD"], False),
            (0x00, False),
            (expected_method, False),
            (0x01, False),
            *((record, False) for record in encode_command_records([])),
        ]
        expected_variables = [("FLASH_DOUBLE_DIE", 0), ("FLASH_COMMANDS_BANK_1", 0)]
        expected_we = 1
        if firmware >= 6:
            expected_writes.extend(
                [
                    (device.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"], False),
                    (0x00, True),
                ],
            )
        if firmware >= 12:
            expected_variables.extend(
                [
                    ("STATUS_REGISTER_MASK", 0x80),
                    ("STATUS_REGISTER_VALUE", 0x80),
                    ("AGB_IRQ_ENABLED", 0),
                ],
            )
    assert result == (command_set, expected_we)
    assert records.writes == expected_writes
    assert records.firmware_variables == expected_variables
    assert records.ack_count == (1 if expected_method is not None and firmware >= 12 else 0)
    assert records.progress == []


@pytest.mark.parametrize(
    ("command_set", "cart_mode", "commands", "command_set_id", "method_id", "payload_commands"),
    [
        (
            "AMD",
            "AGB",
            {"buffer_write": [["SA+2", 0]]},
            0x01,
            0x05,
            [["SA", 0xE8], ["SA", "BS"], ["PA", "PD"], ["SA", 0xD0], ["SA", 0xFF]],
        ),
        ("DATEL_ORBITV2", "DMG", {}, 0x00, 0x09, []),
        ("GBAMP", "AGB", {}, 0x00, 0x0B, []),
        ("BUNG_16M", "DMG", {}, 0x00, 0x0C, []),
    ],
    ids=["flash2advance", "datel-orbit-v2", "gbamp", "bung-16m"],
)
def test_load_flash_commands_selects_specialized_method_ids(
    monkeypatch: pytest.MonkeyPatch,
    command_set: str,
    cart_mode: str,
    commands: dict[str, list[list[int | str]]],
    command_set_id: int,
    method_id: int,
    payload_commands: list[list[int | str | None]],
) -> None:
    device = GbxDevice()
    records = install_flash_command_boundaries(device, monkeypatch, firmware=12, mode=cart_mode)
    cart_type, flashcart = make_command_flashcart(
        type=cart_mode,
        command_set=command_set,
        commands=commands,
    )

    result = device._load_flash_commands(cart_type, flashcart, flash_buffer_size=4)

    expected_writes: list[tuple[int | bytearray, bool]] = [
        (device.DEVICE_CMD["SET_FLASH_CMD"], False),
        (command_set_id, False),
        (method_id, False),
        (0x01, False),
        *((record, False) for record in encode_command_records(payload_commands, agb=cart_mode == "AGB")),
        (device.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"], False),
        (0x00, True),
    ]
    assert result == (command_set, 0x01)
    assert records.writes == expected_writes
    assert records.firmware_variables == [
        ("FLASH_DOUBLE_DIE", 0),
        ("FLASH_COMMANDS_BANK_1", 0),
        ("STATUS_REGISTER_MASK", 0x80),
        ("STATUS_REGISTER_VALUE", 0x80),
        ("AGB_IRQ_ENABLED", 0),
    ]
    assert records.ack_count == 1
    assert records.progress == []


def test_load_flash_commands_configures_double_die_bank_switch_status_and_irq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    records = install_flash_command_boundaries(device, monkeypatch, firmware=12)
    single_write = [[0x5555, 0xAA]]
    cart_type, flashcart = make_command_flashcart(
        commands={
            "single_write": single_write,
            "bank_switch": [[0x2100, "ID"], [0x3000, 7]],
        },
        write_pin="AUDIO",
        double_die=True,
        flash_commands_on_bank_1=True,
        status_register_mask=0x12,
        status_register_value=0x34,
        set_irq_high=True,
    )

    result = device._load_flash_commands(cart_type, flashcart, flash_buffer_size=4)

    expected_writes: list[tuple[int | bytearray, bool]] = [
        (device.DEVICE_CMD["SET_FLASH_CMD"], False),
        (0x01, False),
        (0x01, False),
        (0x02, False),
        *((record, False) for record in encode_command_records(single_write)),
        (device.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"], False),
        (0x02, False),
        (bytearray(struct.pack(">I", 0x2100)), False),
        (0x00, False),
        (bytearray(struct.pack(">I", 7)), False),
        (0x01, False),
    ]
    assert result == ("AMD", 0x02)
    assert records.writes == expected_writes
    assert records.reads == [1]
    assert records.firmware_variables == [
        ("FLASH_DOUBLE_DIE", 1),
        ("FLASH_COMMANDS_BANK_1", 1),
        ("STATUS_REGISTER_MASK", 0x12),
        ("STATUS_REGISTER_VALUE", 0x34),
        ("AGB_IRQ_ENABLED", 1),
    ]
    assert records.ack_count == 1
    assert records.progress == []


@pytest.mark.parametrize(
    ("firmware", "bank_one", "bank_switch", "bank_response", "expected_variables", "expected_writes", "expected_reads"),
    [
        (5, True, None, 1, [("FLASH_COMMANDS_BANK_1", 1)], [], []),
        (5, False, None, 1, [("FLASH_COMMANDS_BANK_1", 0)], [], []),
        (
            6,
            False,
            None,
            1,
            [("FLASH_COMMANDS_BANK_1", 0)],
            [(GbxDevice.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"], False), (0, True)],
            [],
        ),
        (
            6,
            True,
            None,
            1,
            [("FLASH_COMMANDS_BANK_1", 1)],
            [(GbxDevice.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"], False), (0, True)],
            [],
        ),
        (
            6,
            True,
            [[0x2100, "ID"], [0x3000, 7]],
            0,
            [("FLASH_COMMANDS_BANK_1", 1)],
            [
                (GbxDevice.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"], False),
                (2, False),
                (bytearray(struct.pack(">I", 0x2100)), False),
                (0, False),
                (bytearray(struct.pack(">I", 7)), False),
                (1, False),
            ],
            [1],
        ),
    ],
    ids=["legacy-bank-one", "legacy-bank-zero", "modern-bank-zero", "modern-empty-switch", "modern-switch-error"],
)
def test_configure_flash_command_bank_preserves_firmware_protocol(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    firmware: int,
    bank_one: bool,
    bank_switch: list[list[int | str]] | None,
    bank_response: int,
    expected_variables: list[tuple[str, int]],
    expected_writes: list[tuple[int | bytearray, bool]],
    expected_reads: list[int],
) -> None:
    device = GbxDevice()
    records = install_flash_command_boundaries(device, monkeypatch, firmware=firmware, bank_response=bank_response)
    cart_type: dict[str, Any] = {"flash_commands_on_bank_1": bank_one, "commands": {}}
    if bank_switch is not None:
        cart_type["commands"]["bank_switch"] = bank_switch

    device._configure_flash_command_bank(cart_type)

    assert records.firmware_variables == expected_variables
    assert records.writes == expected_writes
    assert records.reads == expected_reads
    assert ("Error in DMG_SET_BANK_CHANGE_CMD" in capsys.readouterr().out) is (
        bank_response != 1 and bool(expected_reads)
    )


@pytest.mark.parametrize(
    ("firmware", "pullups", "irq_setting", "expected_prefix", "expected_irq"),
    [
        (7, True, None, [], None),
        (8, None, None, [], None),
        (8, True, None, [("ENABLE_PULLUPS", True)], None),
        (8, False, None, [("DISABLE_PULLUPS", True)], None),
        (11, None, None, [], None),
        (12, None, None, [], 0),
        (12, None, True, [], 1),
        (12, None, False, [], 1),
    ],
    ids=[
        "firmware-7-ignores-pullups",
        "firmware-8-absent-pullups",
        "firmware-8-enables-pullups",
        "firmware-8-disables-pullups",
        "firmware-11-no-ack",
        "firmware-12-ack-no-irq-key",
        "firmware-12-irq-key-enabled",
        "firmware-12-irq-key-disabled-value",
    ],
)
def test_configure_flashcart_for_write_honors_agb_firmware_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    firmware: int,
    pullups: bool | None,
    irq_setting: bool | None,
    expected_prefix: list[tuple[str, bool]],
    expected_irq: int | None,
) -> None:
    device = GbxDevice()
    records, factory = install_flash_configuration_boundaries(device, monkeypatch, firmware=firmware)
    overrides: dict[str, object] = {"type": "AGB"}
    if pullups is not None:
        overrides["enable_pullups"] = pullups
    if irq_setting is not None:
        overrides["set_irq_high"] = irq_setting
    _profile, flashcart = make_command_flashcart(**overrides)

    result = device._configure_flashcart_for_write(
        {},
        flashcart.CONFIG,
        flashcart,
        bytearray(0x4000),
        "AGB",
    )

    pullup_commands = [(device.DEVICE_CMD[name], wait) for name, wait in expected_prefix]
    assert records.writes == [
        *pullup_commands,
        (device.DEVICE_CMD["SET_MODE_AGB"], firmware >= 12),
    ]
    assert records.variables == ([] if expected_irq is None else [("AGB_IRQ_ENABLED", expected_irq)])
    assert result == _FlashConfiguration(
        mbc=None,
        end_bank=1,
        rom_bank_size=0x2000000,
        enable_pullup_wr=0,
        error_message="",
        buffer_size=4,
    )
    assert factory.arguments == []
    assert records.progress == []


@pytest.mark.parametrize(
    ("firmware", "profile_pullup", "forced_pullup", "expected_pullup"),
    [
        (11, True, None, None),
        (12, None, None, 0),
        (12, False, False, 0),
        (12, True, None, 2),
        (12, None, True, 2),
    ],
    ids=[
        "firmware-11-no-wr-setting",
        "firmware-12-settings-absent",
        "firmware-12-settings-disabled",
        "firmware-12-profile-enabled",
        "firmware-12-manually-forced",
    ],
)
def test_configure_flashcart_for_write_honors_dmg_wr_pullup_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    firmware: int,
    profile_pullup: bool | None,
    forced_pullup: bool | None,
    expected_pullup: int | None,
) -> None:
    device = GbxDevice()
    records, factory = install_flash_configuration_boundaries(device, monkeypatch, firmware=firmware)
    overrides: dict[str, object] = {"mbc": 0x19}
    if profile_pullup is not None:
        overrides["enable_pullup_wr"] = profile_pullup
    _profile, flashcart = make_command_flashcart(**overrides)
    args: dict[str, Any] = {"mbc": 0}
    if forced_pullup is not None:
        args["force_wr_pullup"] = forced_pullup

    result = device._configure_flashcart_for_write(
        args,
        flashcart.CONFIG,
        flashcart,
        bytearray(0x4000),
        "DMG",
    )

    expected_variables = [("FLASH_PULSE_RESET", 0)]
    if expected_pullup is not None:
        expected_variables.insert(0, ("PULLUPS_ENABLED", expected_pullup))
    assert records.writes == [(device.DEVICE_CMD["SET_MODE_DMG"], firmware >= 12)]
    assert records.variables == expected_variables
    assert result is not None
    assert result.enable_pullup_wr == (expected_pullup or 0)
    assert args["mbc"] == 0x19
    assert len(factory.arguments) == 1
    assert records.events[-1] == ("mapper-enable",)


@pytest.mark.parametrize(
    (
        "profile_mbc",
        "selected_mbc",
        "expected_mbc",
        "data_length",
        "pulse_reset",
        "expected_banks",
        "selection_text",
        "size_advisory",
    ),
    [
        (0x01, 0x03, 0x01, 0x8000, False, 2, "forced by selected flashcart profile", False),
        ("manual", 0x03, 0x03, 0x8001, True, 3, "manual selection", True),
        (None, 0x00, 0x19, 0x4000, False, 1, "forced by selected flashcart profile", False),
    ],
    ids=["forced-exact-banks", "manual-partial-bank", "default-mbc5"],
)
def test_configure_flashcart_for_write_selects_dmg_mapper_and_geometry(
    monkeypatch: pytest.MonkeyPatch,
    profile_mbc: int | str | None,
    selected_mbc: int,
    expected_mbc: int,
    data_length: int,
    pulse_reset: bool,
    expected_banks: int,
    selection_text: str,
    size_advisory: bool,
) -> None:
    device = GbxDevice()
    records, factory = install_flash_configuration_boundaries(device, monkeypatch, firmware=11)
    overrides: dict[str, object] = {"pulse_reset_after_write": pulse_reset}
    if profile_mbc is not None:
        overrides["mbc"] = profile_mbc
    _profile, flashcart = make_command_flashcart(**overrides)
    args: dict[str, Any] = {"mbc": selected_mbc}

    result = device._configure_flashcart_for_write(
        args,
        flashcart.CONFIG,
        flashcart,
        bytearray(data_length),
        "DMG",
    )

    assert result is not None
    assert result.mbc is factory.mapper
    assert result.end_bank == expected_banks
    assert result.rom_bank_size == 0x4000
    assert result.enable_pullup_wr == 0
    assert "Test MBC" in result.error_message
    assert selection_text in result.error_message
    assert ("ROM size limit" in result.error_message) is size_advisory
    assert result.buffer_size == 4
    assert args["mbc"] == expected_mbc
    assert len(factory.arguments) == 1
    assert factory.arguments[0]["args"] is args
    assert records.events == [
        ("write", device.DEVICE_CMD["SET_MODE_DMG"], False),
        ("mapper-create", expected_mbc),
        ("variable", "FLASH_PULSE_RESET", int(pulse_reset)),
        ("mapper-enable",),
    ]
    assert records.progress == []


def test_configure_flashcart_for_write_rejects_unsupported_mapper_before_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    records, factory = install_flash_configuration_boundaries(device, monkeypatch, firmware=11)
    _profile, flashcart = make_command_flashcart(mbc=0x7F)
    args: dict[str, Any] = {"mbc": 0x01}

    result = device._configure_flashcart_for_write(
        args,
        flashcart.CONFIG,
        flashcart,
        bytearray(0x4000),
        "DMG",
    )

    assert result is None
    assert args["mbc"] == 0x7F
    assert records.writes == [(device.DEVICE_CMD["SET_MODE_DMG"], False)]
    assert records.variables == []
    assert factory.arguments == []
    assert len(records.progress) == 1
    assert records.progress[0]["action"] == "ABORT"
    assert records.progress[0]["info_type"] == "msgbox_critical"
    assert records.progress[0]["abortable"] is False


@pytest.mark.parametrize(
    ("bank_size", "data_length", "expected_banks", "expected_bank_size"),
    [
        (None, 0x4000, 1, 0x2000000),
        (0x100000, 0x200000, 2, 0x100000),
        (0x100000, 0x200001, 3, 0x100000),
    ],
    ids=["default-geometry", "exact-banked-geometry", "partial-banked-geometry"],
)
def test_configure_flashcart_for_write_calculates_agb_bank_geometry(
    monkeypatch: pytest.MonkeyPatch,
    bank_size: int | None,
    data_length: int,
    expected_banks: int,
    expected_bank_size: int,
) -> None:
    device = GbxDevice()
    records, factory = install_flash_configuration_boundaries(device, monkeypatch, firmware=12)
    overrides: dict[str, object] = {"type": "AGB", "buffer_size": 32}
    if bank_size is not None:
        overrides["flash_bank_size"] = bank_size
    _profile, flashcart = make_command_flashcart(**overrides)

    result = device._configure_flashcart_for_write(
        {},
        flashcart.CONFIG,
        flashcart,
        bytearray(data_length),
        "AGB",
    )

    assert result == _FlashConfiguration(
        mbc=None,
        end_bank=expected_banks,
        rom_bank_size=expected_bank_size,
        enable_pullup_wr=0,
        error_message="",
        buffer_size=32,
    )
    assert records.writes == [(device.DEVICE_CMD["SET_MODE_AGB"], True)]
    assert records.variables == [("AGB_IRQ_ENABLED", 0)]
    assert factory.arguments == []
    assert records.progress == []


@pytest.mark.parametrize(
    (
        "supports_chip",
        "supports_sector",
        "prefer_chip_erase",
        "flash_offset",
        "has_sector_map",
        "chip_result",
        "expected",
        "expected_chip_calls",
        "expected_abort",
    ),
    [
        (True, True, True, 0, True, True, True, 1, False),
        (True, True, False, 0, True, True, False, 0, False),
        (True, True, False, 0, False, True, True, 1, False),
        (True, True, True, 0x2000, True, True, False, 0, False),
        (False, True, True, 0, True, True, False, 0, False),
        (False, False, True, 0, False, True, None, 0, True),
        (True, False, True, 0, False, False, None, 1, False),
    ],
    ids=[
        "chip-erase",
        "sector-preferred",
        "preference-needs-sector-map",
        "nonzero-offset",
        "sector-only",
        "unavailable",
        "chip-erase-failed",
    ],
)
def test_erase_flash_for_write_selects_available_strategy(
    supports_chip: bool,
    supports_sector: bool,
    prefer_chip_erase: bool,
    flash_offset: int,
    has_sector_map: bool,
    chip_result: bool,
    expected: bool | None,
    expected_chip_calls: int,
    expected_abort: bool,
) -> None:
    device = GbxDevice()
    progress: list[dict[str, object]] = []
    device.SetProgress = lambda update: progress.append(dict(update))  # type: ignore[method-assign]
    flashcart = EraseChoiceFlashcart(
        supports_chip=supports_chip,
        supports_sector=supports_sector,
        chip_result=chip_result,
    )

    result = device._EraseFlashForWrite(
        {"prefer_chip_erase": prefer_chip_erase},
        flashcart,  # type: ignore[arg-type]
        flash_offset,
        has_sector_map,
    )

    assert result is expected
    assert flashcart.chip_erase_calls == expected_chip_calls
    assert [event["action"] for event in progress] == (["ABORT"] if expected_abort else [])


@pytest.mark.parametrize(
    ("mode", "chip_erase", "active_voltage", "expected_write_sectors", "expected_verify_sectors"),
    [
        ("DMG", False, 5.0, [[0, 4], [4, 4]], []),
        ("AGB", True, 3.3, [[0, 8]], [[0, 0x20000]]),
    ],
    ids=["dmg-sector-erase", "agb-chip-erase"],
)
def test_prepare_flash_write_builds_complete_copied_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    chip_erase: bool,
    active_voltage: float,
    expected_write_sectors: list[list[int]],
    expected_verify_sectors: list[list[int]],
) -> None:
    device = GbxDevice()
    device.MODE = mode  # type: ignore[assignment]
    device.FW = {"fw_ver": 12, "pcb_name": "GBxCart RW"}
    profile_name = f"Prepared {mode}"
    profile = flashcart_profile(type=mode, names=[profile_name])
    device.SUPPORTED_CARTS = {"DMG": {}, "AGB": {}, mode: {profile_name: profile}}
    _cart_profile, flashcart = make_command_flashcart(type=mode, names=[profile_name])
    mapper = object()
    state_path = tmp_path / "flash-state.json"
    sector_plan = _FlashSectorPlan(
        data_import=bytearray(b"PREPARED"),
        smallest_sector_size=4,
        sector_offsets=[[0, 4], [4, 4]],
        write_sectors=[[0, 4], [4, 4]],
        delta_state=[[0, 4, 123]],
        state_path=state_path,
        has_sector_map=True,
    )
    progress: list[dict[str, object]] = []
    firmware_variables: list[tuple[str, int]] = []

    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: False)
    monkeypatch.setattr(device, "_prepare_flash_data", lambda _args, _mode: (bytearray(b"PREPARED"), 0))
    monkeypatch.setattr(device, "_create_flashcart", lambda _profile, _callbacks: flashcart)
    monkeypatch.setattr(device, "_set_flash_voltage", lambda _args, _cart: active_voltage)
    monkeypatch.setattr(
        device,
        "_configure_flashcart_for_write",
        lambda _args, _profile, _cart, _data, _mode: _FlashConfiguration(
            mbc=mapper,
            end_bank=2,
            rom_bank_size=4,
            enable_pullup_wr=2,
            error_message="mapper details",
            buffer_size=16,
        ),
    )
    monkeypatch.setattr(device, "_PrepareGBMemoryMap", lambda _args, _mapper, _data: bytearray(b"MAP"))
    monkeypatch.setattr(device, "_load_flash_commands", lambda _profile, _cart, _size: ("AMD", 1))
    monkeypatch.setattr(device, "_CheckFlashID", lambda _profile, _cart, _command_set: True)
    monkeypatch.setattr(device, "_plan_flash_sectors", lambda *_args: sector_plan)
    monkeypatch.setattr(device, "_EraseFlashForWrite", lambda *_args: chip_erase)
    monkeypatch.setattr(device, "SetProgress", lambda update: progress.append(dict(update)))
    monkeypatch.setattr(
        device,
        "_set_fw_variable",
        lambda name, value: firmware_variables.append((name, value)),
    )
    args = {
        "buffer": b"unused by the preparation boundary",
        "cart_type": 0,
        "override_voltage": False,
        "path": tmp_path / f"prepared.{mode.lower()}",
        "prefer_chip_erase": False,
    }

    preparation = device._prepare_flash_write(args, mode)  # type: ignore[arg-type]

    assert preparation is not None
    assert preparation.cart_name == profile_name
    assert preparation.cart_type is not profile
    assert preparation.cart_type["commands"] is not profile["commands"]
    assert preparation.cart_type["_index"] == 0
    assert "_index" not in profile
    assert preparation.flashcart is flashcart
    assert preparation.data_import == bytearray(b"PREPARED")
    assert preparation.data_map_import == bytearray(b"MAP")
    assert preparation.flash_offset == 0
    assert preparation.active_voltage == active_voltage
    assert preparation.mbc is mapper
    assert preparation.end_bank == 2
    assert preparation.rom_bank_size == 4
    assert preparation.enable_pullup_wr == 2
    assert preparation.error_message == "mapper details"
    assert preparation.flash_buffer_size == 16
    assert preparation.command_set_type == "AMD"
    assert preparation.sector_offsets == [[0, 4], [4, 4]]
    assert preparation.write_sectors == expected_write_sectors
    assert preparation.delta_state == [[0, 4, 123]]
    assert preparation.state_path == state_path
    assert preparation.chip_erase is chip_erase
    assert preparation.buffer_len == 4
    assert preparation.verify_sectors == expected_verify_sectors
    assert progress == [
        {
            "action": "INITIALIZE",
            "method": "ROM_WRITE",
            "size": 8,
            "flash_offset": 0,
            "sector_count": 2,
            "voltage": active_voltage,
        },
        {
            "action": "INITIALIZE",
            "method": "ROM_WRITE",
            "size": 8,
            "flash_offset": 0,
            "sector_count": len(expected_write_sectors),
            "voltage": active_voltage,
        },
        {"action": "UPDATE_POS", "pos": 0},
    ]
    assert firmware_variables == [("FLASH_WE_PIN", 1)]
    assert device.INFO["action"] == device.ACTIONS["ROM_WRITE"]


@pytest.mark.parametrize(
    "rejected_stage",
    ["firmware", "configuration", "map", "commands", "flash-id", "sector-plan", "erase"],
)
def test_flash_rom_worker_stops_at_each_preparation_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rejected_stage: str,
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.FW = {"fw_ver": 12, "pcb_name": "GBxCart RW"}
    profile_name = "Rejected preparation"
    profile = flashcart_profile(type="DMG", names=[profile_name])
    if rejected_stage == "firmware":
        profile["set_audio_high"] = True
    device.SUPPORTED_CARTS = {"DMG": {profile_name: profile}, "AGB": {}}
    _cart_profile, flashcart = make_command_flashcart(type="DMG", names=[profile_name])
    calls: list[str] = []
    expected_order = ["firmware", "configuration", "map", "commands", "flash-id", "sector-plan", "erase"]
    real_firmware_check = device._check_flashcart_firmware

    def check_firmware(cart_type: dict[str, Any]) -> bool:
        calls.append("firmware")
        return real_firmware_check(cart_type)

    def configure(*_args: object) -> _FlashConfiguration | None:
        calls.append("configuration")
        if rejected_stage == "configuration":
            return None
        return _FlashConfiguration(
            mbc=object(),
            end_bank=1,
            rom_bank_size=0x4000,
            enable_pullup_wr=0,
            error_message="",
            buffer_size=4,
        )

    def prepare_map(*_args: object) -> bytearray | None:
        calls.append("map")
        return None if rejected_stage == "map" else bytearray()

    def load_commands(*_args: object) -> tuple[str, int] | None:
        calls.append("commands")
        return None if rejected_stage == "commands" else ("AMD", 0)

    def check_flash_id(*_args: object) -> bool:
        calls.append("flash-id")
        return rejected_stage != "flash-id"

    def plan_sectors(*_args: object) -> _FlashSectorPlan | None:
        calls.append("sector-plan")
        if rejected_stage == "sector-plan":
            return None
        return _FlashSectorPlan(
            data_import=bytearray(b"DATA"),
            smallest_sector_size=4,
            sector_offsets=[[0, 4]],
            write_sectors=[[0, 4]],
            delta_state=None,
            state_path="",
            has_sector_map=True,
        )

    def erase(*_args: object) -> bool | None:
        calls.append("erase")
        return None if rejected_stage == "erase" else False

    writer = Mock(return_value=True)
    monkeypatch.setattr(device, "CanPowerCycleCart", lambda: False)
    monkeypatch.setattr(device, "_check_flashcart_firmware", check_firmware)
    monkeypatch.setattr(device, "_create_flashcart", lambda _profile, _callbacks: flashcart)
    monkeypatch.setattr(device, "_set_flash_voltage", lambda _args, _cart: 5.0)
    monkeypatch.setattr(device, "_configure_flashcart_for_write", configure)
    monkeypatch.setattr(device, "_PrepareGBMemoryMap", prepare_map)
    monkeypatch.setattr(device, "_load_flash_commands", load_commands)
    monkeypatch.setattr(device, "_CheckFlashID", check_flash_id)
    monkeypatch.setattr(device, "_plan_flash_sectors", plan_sectors)
    monkeypatch.setattr(device, "_EraseFlashForWrite", erase)
    monkeypatch.setattr(device, "_set_fw_variable", Mock())
    monkeypatch.setattr(device, "SetProgress", Mock())
    monkeypatch.setattr(device, "_WritePreparedFlashROM", writer)
    args = {
        "buffer": b"DATA",
        "cart_type": 0,
        "override_voltage": False,
        "path": tmp_path / "rejected.gb",
        "prefer_chip_erase": False,
    }

    result = device._FlashROM_Worker(args)

    rejected_index = expected_order.index(rejected_stage)
    assert result is False
    assert calls == expected_order[: rejected_index + 1]
    writer.assert_not_called()
    assert device.FAST_READ is True


@pytest.mark.parametrize("writer_result", [True, False, None])
def test_flash_rom_worker_returns_prepared_writer_result(
    monkeypatch: pytest.MonkeyPatch,
    writer_result: bool | None,
) -> None:
    device = GbxDevice()
    args = {"request": "flash"}
    preparation = object()
    start = Mock(return_value=("AGB", preparation))
    writer = Mock(return_value=writer_result)
    monkeypatch.setattr(device, "_StartFlashROMWrite", start)
    monkeypatch.setattr(device, "_WritePreparedFlashROM", writer)

    result = device._FlashROM_Worker(args)

    assert result is writer_result
    start.assert_called_once_with(args)
    writer.assert_called_once_with(args, "AGB", preparation)


def test_prepare_flash_write_integrates_real_input_commands_and_sector_planner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    device = GbxDevice()
    records = install_flash_command_boundaries(device, monkeypatch, firmware=12, mode="AGB")
    profile_name = "Integrated AGB"
    profile = flashcart_profile(
        type="AGB",
        names=[profile_name],
        voltage=3.3,
        flash_size=0x4000,
        sector_size=0x1000,
        buffer_size=4,
        commands={
            "single_write": [[0xAAAA, 0xAA]],
            "sector_erase": [[0xAAAA, 0x80]],
        },
    )
    del profile["flash_ids"]
    device.SUPPORTED_CARTS = {"DMG": {}, "AGB": {profile_name: profile}}
    rom_reads: list[tuple[int, int]] = []

    def read_rom(address: int, length: int, *_args: object, **_kwargs: object) -> bytearray:
        rom_reads.append((address, length))
        return bytearray(length)

    monkeypatch.setattr(device, "ReadROM", read_rom)
    source_path = tmp_path / "integrated.gba"
    args = {
        "buffer": b"ROM",
        "cart_type": 0,
        "override_voltage": False,
        "path": source_path,
        "prefer_chip_erase": False,
    }

    preparation = device._prepare_flash_write(args, "AGB")

    assert preparation is not None
    assert preparation.cart_name == profile_name
    assert preparation.cart_type is not profile
    assert preparation.cart_type["_index"] == 0
    assert preparation.cart_type["_command_set"] == "AMD"
    assert "_index" not in profile
    assert "_command_set" not in profile
    assert preparation.flashcart.CONFIG is preparation.cart_type
    assert preparation.data_import[:3] == b"ROM"
    assert preparation.data_import[3:] == bytearray([0xFF]) * (0x4000 - 3)
    assert preparation.data_map_import == bytearray()
    assert preparation.flash_offset == 0
    assert preparation.active_voltage == 3.3
    assert preparation.mbc is None
    assert preparation.end_bank == 1
    assert preparation.rom_bank_size == 0x2000000
    assert preparation.enable_pullup_wr == 0
    assert preparation.error_message == ""
    assert preparation.flash_buffer_size == 4
    assert preparation.command_set_type == "AMD"
    assert preparation.sector_offsets == [
        [0, 0x1000],
        [0x1000, 0x1000],
        [0x2000, 0x1000],
        [0x3000, 0x1000],
    ]
    assert preparation.write_sectors == preparation.sector_offsets
    assert preparation.chip_erase is False
    assert preparation.buffer_len == 0x1000
    assert preparation.verify_sectors == []
    assert rom_reads == [(0, 2)]
    assert records.writes[:6] == [
        (device.DEVICE_CMD["SET_VOLTAGE_3_3V"], True),
        (device.DEVICE_CMD["SET_MODE_AGB"], True),
        (device.DEVICE_CMD["SET_FLASH_CMD"], False),
        (0x01, False),
        (0x01, False),
        (0x01, False),
    ]
    assert records.writes[6] == (bytearray(struct.pack(">IH", 0x5555, 0xAA)), False)
    assert records.reads == []
    assert records.ack_count == 1
    assert records.firmware_variables == [
        ("AGB_IRQ_ENABLED", 0),
        ("FLASH_DOUBLE_DIE", 0),
        ("FLASH_COMMANDS_BANK_1", 0),
        ("STATUS_REGISTER_MASK", 0x80),
        ("STATUS_REGISTER_VALUE", 0x80),
        ("AGB_IRQ_ENABLED", 0),
        ("FLASH_WE_PIN", 1),
    ]
    assert records.progress == [
        {
            "action": "INITIALIZE",
            "method": "ROM_WRITE",
            "size": 0x4000,
            "flash_offset": 0,
            "sector_count": 4,
            "voltage": 3.3,
        },
        {
            "action": "INITIALIZE",
            "method": "ROM_WRITE",
            "size": 0x4000,
            "flash_offset": 0,
            "sector_count": 4,
            "voltage": 3.3,
        },
        {"action": "UPDATE_POS", "pos": 0},
    ]
