"""Tests for flash-write input transformation."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from FlashGBX.Flashcart import Flashcart
from FlashGBX.hw_GBxCartRW import GbxDevice
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
