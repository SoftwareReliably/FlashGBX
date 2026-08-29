"""Hardware-free tests for the GBxCart RW backend."""

from __future__ import annotations

import hashlib
import random
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import FlashGBX.hw_GBxCartRW as gbxcartrw
from FlashGBX.hw_GBxCartRW import (
    MAX_V13_FIRMWARE_SIZE,
    FirmwareInfo,
    FirmwareUpdater,
    GbxDevice,
    _parse_intel_hex,
)

from .fakes import EchoSerial, MockSerial


def modern_firmware(**overrides: object) -> FirmwareInfo:
    firmware: FirmwareInfo = {
        "cfw_id": "L",
        "fw_ver": 18,
        "pcb_ver": 6,
        "fw_ts": GbxDevice.DEVICE_LATEST_FW_TS[6],
        "fw_dt": "2026-06-03T12:25:02+00:00",
        "ofw_ver": 31,
        "pcb_name": "GBxCart RW",
        "cart_power_ctrl": False,
        "cart_presence_switch": False,
        "cart_mode_switch": False,
        "bootloader_reset": False,
    }
    firmware.update(overrides)  # type: ignore[typeddict-item]
    return firmware


def intel_hex_record(address: int, record_type: int, data: bytes = b"") -> str:
    """Build one checksummed Intel HEX record for parser tests."""
    record = bytearray([len(data)])
    record.extend(address.to_bytes(2, byteorder="big"))
    record.append(record_type)
    record.extend(data)
    record.append((-sum(record)) & 0xFF)
    return ":" + record.hex().upper()


def build_firmware_archive(path: Path, firmware: bytes) -> None:
    """Create the encrypted archive format consumed by ``FirmwareUpdater``."""
    key = bytearray(b"unit-test-key")
    seed = 0x12345678
    total_length = len(firmware) + 24
    while len(key) < total_length:
        key = key + key

    encrypted = bytearray(len(firmware))
    rng = random.Random(seed)
    for index, value in enumerate(firmware):
        random_byte = int(rng.random() * 256) % 256
        encrypted[len(firmware) - index - 1] = value ^ random_byte ^ key[len(key) - index - 1]
    payload = encrypted + struct.pack("<I", seed) + hashlib.sha1(firmware).digest()

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fw.ini", b"unit-test-key")
        archive.writestr("fw.bin", payload)


def test_parse_intel_hex_supports_offsets_and_fills_address_gaps() -> None:
    image = "\n".join(
        [
            intel_hex_record(0, 0x02, b"\x00\x01"),
            intel_hex_record(2, 0x00, b"RED"),
            intel_hex_record(0, 0x04, b"\x00\x00"),
            intel_hex_record(1, 0x00, b"!"),
            intel_hex_record(0, 0x05, b"\x00\x00\x00\x00"),
            intel_hex_record(0, 0x01),
        ],
    )

    assert _parse_intel_hex(image) == bytearray(b"\xff!" + b"\xff" * 16 + b"RED")


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("not-a-record", "no start marker"),
        (":xyz", "invalid hexadecimal"),
        (":02000000FF", "invalid length"),
        (":01000000FF01", "invalid checksum"),
        (intel_hex_record(0, 0x06), "unsupported record type"),
        (intel_hex_record(0, 0x00, b"data"), "incomplete"),
        (intel_hex_record(0, 0x01), "incomplete"),
    ],
)
def test_parse_intel_hex_rejects_malformed_images(
    contents: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _parse_intel_hex(contents)


def test_parse_intel_hex_rejects_oversized_images() -> None:
    image = "\n".join(
        [
            intel_hex_record(MAX_V13_FIRMWARE_SIZE - 1, 0x00, b"x"),
            intel_hex_record(0, 0x01),
        ],
    )

    with pytest.raises(ValueError, match="too large"):
        _parse_intel_hex(image)


def test_initialize_returns_false_without_discovered_hardware() -> None:
    """The default test environment must never fall through to real serial."""
    device = GbxDevice()

    assert device.Initialize() is False
    assert device.DEVICE is None
    assert device.PORT == ""


def test_runtime_state_is_isolated_between_device_instances() -> None:
    first = GbxDevice()
    second = GbxDevice()

    first.CANCEL_ARGS["from_user"] = True
    first.ERROR_ARGS["iteration"] = 2
    first.INFO["dump_info"]["game_title"] = "POKEMON RED"
    first.FW_VAR["ADDRESS"] = 0x4000

    assert second.CANCEL_ARGS == {}
    assert second.ERROR_ARGS == {}
    assert second.INFO["dump_info"] == {}
    assert second.FW_VAR == {}


def test_firmware_variable_metadata_uses_exact_typed_names() -> None:
    device = GbxDevice()

    assert device._resolve_fw_variable("TRANSFER_SIZE") == (2, 0)
    with pytest.raises(KeyError):
        device._resolve_fw_variable("SIZE")  # type: ignore[arg-type]


def test_write_accepts_byte_buffers_and_rejects_out_of_range_byte() -> None:
    serial_device = MockSerial()
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]

    device._write(b"\x12\x34")

    assert serial_device.writes == [b"\x12\x34"]
    with pytest.raises(ValueError, match="one byte"):
        device._write(0x100)


def test_set_pin_serializes_each_selected_pin() -> None:
    device = GbxDevice()
    device.FW = modern_firmware()
    device._write = Mock(return_value=1)  # type: ignore[method-assign]

    assert device.SetPin(["PIN_WR", "PIN_A0"], True) == 1

    expected_mask = (1 << 2) | (1 << 5)
    expected = bytearray([device.DEVICE_CMD["SET_PIN"]])
    expected.extend(struct.pack(">I", expected_mask))
    expected.append(1)
    device._write.assert_called_once_with(expected, wait=True)


def test_initialize_filters_ports_and_uses_injected_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ports = [
        SimpleNamespace(device="/dev/tty.ignore", vid=0x1234, pid=0x5678),
        SimpleNamespace(device="/dev/tty.mock-gbx", vid=0x1A86, pid=0x7523),
    ]
    serial_device = MockSerial()
    attempts: list[tuple[str, int]] = []
    serial_opens: list[tuple[tuple[object, ...], dict[str, object]]] = []
    device = GbxDevice()

    def try_connect(port: str, baudrate: int) -> bool:
        attempts.append((port, baudrate))
        device.FW = modern_firmware()
        return True

    def open_mock_serial(*args: object, **kwargs: object) -> MockSerial:
        serial_opens.append((args, kwargs))
        return serial_device

    monkeypatch.setattr(gbxcartrw.serial.tools.list_ports, "comports", lambda: ports)
    monkeypatch.setattr(gbxcartrw.serial, "Serial", open_mock_serial)
    monkeypatch.setattr(gbxcartrw.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(device, "TryConnect", try_connect)
    monkeypatch.setattr(device, "IsConnected", lambda: True)

    assert device.Initialize(max_baud=1_000_000) == []
    assert attempts == [("/dev/tty.mock-gbx", 1_000_000)]
    assert serial_opens == [(("/dev/tty.mock-gbx", 1_000_000), {"timeout": 0.1})]
    assert device.DEVICE is serial_device
    assert device.PORT == "/dev/tty.mock-gbx"
    assert serial_device.timeout == device.DEVICE_TIMEOUT


def test_initialize_falls_back_to_high_speed_and_loads_flashcart_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial_device = MockSerial()
    attempts: list[tuple[str, int]] = []
    flashcarts = {
        "DMG": {"Test Cart": {"names": ["Test Cart"]}},
        "AGB": {},
    }
    device = GbxDevice()

    def try_connect(port: str, baudrate: int) -> bool:
        attempts.append((port, baudrate))
        if baudrate == 1_500_000:
            device.FW = modern_firmware(pcb_ver=5)
            return True
        return False

    monkeypatch.setattr(gbxcartrw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gbxcartrw.serial, "Serial", lambda *args, **kwargs: serial_device)
    monkeypatch.setattr(device, "TryConnect", try_connect)
    monkeypatch.setattr(device, "IsConnected", lambda: True)

    assert device.Initialize(flashcarts=flashcarts, port="mock-port") == []
    assert attempts == [("mock-port", 1_000_000), ("mock-port", 1_500_000)]
    assert device.BAUDRATE == 1_500_000
    assert device.MAX_BUFFER_WRITE == 0x400
    assert device.SUPPORTED_CARTS["DMG"]["Test Cart"] == {"names": ["Test Cart"]}


def test_initialize_closes_device_when_connection_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serial_device = MockSerial()
    device = GbxDevice()

    def try_connect(_port: str, _baudrate: int) -> bool:
        device.FW = modern_firmware(pcb_ver=255)
        return True

    monkeypatch.setattr(gbxcartrw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gbxcartrw.serial, "Serial", lambda *args, **kwargs: serial_device)
    monkeypatch.setattr(device, "TryConnect", try_connect)
    monkeypatch.setattr(device, "IsConnected", lambda: True)

    messages = device.Initialize(port="mock-port", max_baud=1_000_000)

    assert messages is not False
    assert messages[0][0] == 0
    assert "mock-port" in str(messages[0][1])
    assert device.DEVICE is None
    assert device.FW is None
    assert serial_device.is_open is False


def test_load_firmware_version_parses_mocked_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = 1_700_000_000
    device_name = b"GBxCart RW v1.4c"
    firmware_payload = (
        bytes([8])
        + struct.pack(">cHBI", b"L", 18, 6, timestamp)
        + bytes([len(device_name)])
        + device_name
        + bytes([0b111, 1])
    )
    serial_device = MockSerial(
        timeout=0.25,
        responses=[bytes([6]), bytes([31]), firmware_payload],
    )
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]
    monkeypatch.setattr(gbxcartrw.platform, "system", lambda: "Linux")

    assert device.LoadFirmwareVersion() is True
    assert device.FW is not None
    assert device.FW["cfw_id"] == "L"
    assert device.FW["fw_ver"] == 18
    assert device.FW["pcb_ver"] == 6
    assert device.FW["fw_ts"] == timestamp
    assert device.FW["pcb_name"] == "GBxCart RW v1.4c"
    assert device.FW["cart_power_ctrl"] is True
    assert device.FW["cart_presence_switch"] is True
    assert device.FW["cart_mode_switch"] is True
    assert device.FW["bootloader_reset"] is True
    assert device.DEVICE_NAME == "GBxCart RW v1.4c"
    assert serial_device.timeout == 0.25
    assert serial_device.writes == [
        bytes([device.DEVICE_CMD["OFW_PCB_VER"]]),
        bytes([device.DEVICE_CMD["OFW_FW_VER"]]),
        bytes([device.DEVICE_CMD["QUERY_FW_INFO"]]),
    ]


def test_load_firmware_version_accepts_legacy_official_firmware() -> None:
    serial_device = MockSerial(timeout=0.4, responses=[b"\x04", b"\x1f"])
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]

    assert device.LoadFirmwareVersion() is True
    assert device.FW == {
        "ofw_ver": 31,
        "pcb_ver": 4,
        "pcb_name": "GBxCart RW",
        "cfw_id": "",
        "fw_ver": 0,
        "fw_ts": 0,
        "fw_dt": "",
        "cart_power_ctrl": False,
        "cart_presence_switch": False,
        "cart_mode_switch": False,
        "bootloader_reset": False,
    }
    assert serial_device.timeout == 0.4


@pytest.mark.parametrize(
    "responses",
    [
        [b"\x02", b"\x02"],
        [b"\x06", b"\x00"],
        [b"\x06", b"\x1f", b"\x07"],
    ],
)
def test_load_firmware_version_rejects_unexpected_device_signatures(
    responses: list[bytes],
) -> None:
    serial_device = MockSerial(timeout=0.3, responses=responses)
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]

    assert device.LoadFirmwareVersion() is False
    assert device.FW is None
    assert serial_device.is_open is True
    assert serial_device.timeout == 0.3


def test_load_firmware_version_disconnects_after_truncated_protocol() -> None:
    serial_device = MockSerial(timeout=0.3, responses=[b"\x06"])
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]
    device._read = Mock(side_effect=[31, 8, False])  # type: ignore[method-assign]

    assert device.LoadFirmwareVersion() is False
    assert device.DEVICE is None
    assert serial_device.is_open is False


def test_load_firmware_version_handles_non_utf8_device_name() -> None:
    timestamp = 1_700_000_000
    payload = bytes([8]) + struct.pack(">cHBI", b"L", 12, 6, timestamp)
    serial_device = MockSerial(responses=[b"\x06", b"\x1f", payload + b"\x02\xff\xfe\x00\x00"])
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]

    assert device.LoadFirmwareVersion() is True
    assert device.FW is not None
    assert device.FW["pcb_name"] == "Unnamed Device"
    assert device.DEVICE_NAME == "Unnamed Device"


def test_read_rom_chunks_requests_without_serial_hardware() -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.INFO = {"action": None, "last_action": None, "dump_info": {}}
    device._set_fw_variable = Mock()  # type: ignore[method-assign]
    device._write = Mock()  # type: ignore[method-assign]
    device._read = Mock(  # type: ignore[method-assign]
        side_effect=[bytearray(b"ABCD"), bytearray(b"EFGH")],
    )

    result = device.ReadROM(address=0x4000, length=8, max_length=4)

    assert result == bytearray(b"ABCDEFGH")
    assert device._set_fw_variable.call_args_list == [
        call("TRANSFER_SIZE", 4),
        call("ADDRESS", 0x4000),
        call("DMG_ACCESS_MODE", 1),
    ]
    assert device._write.call_args_list == [
        call(device.DEVICE_CMD["DMG_CART_READ"]),
        call(device.DEVICE_CMD["DMG_CART_READ"]),
    ]
    assert device._read.call_args_list == [call(4), call(4)]


def test_read_rom_returns_empty_buffer_after_mocked_timeout() -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.INFO = {"action": None, "last_action": None, "dump_info": {}}
    device._set_fw_variable = Mock()  # type: ignore[method-assign]
    device._write = Mock()  # type: ignore[method-assign]
    device._read = Mock(return_value=False)  # type: ignore[method-assign]

    assert device.ReadROM(address=0, length=4, max_length=4) == bytearray()


def test_read_rom_configures_word_addressing_for_agb() -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    device.INFO = {"action": None, "last_action": None, "dump_info": {}}
    device._set_fw_variable = Mock()  # type: ignore[method-assign]
    device._write = Mock()  # type: ignore[method-assign]
    device._read = Mock(return_value=bytearray(b"GBA!"))  # type: ignore[method-assign]

    assert device.ReadROM(address=0x100, length=4, max_length=4) == b"GBA!"
    assert device._set_fw_variable.call_args_list == [
        call("TRANSFER_SIZE", 4),
        call("ADDRESS", 0x80),
    ]
    device._write.assert_called_once_with(device.DEVICE_CMD["AGB_CART_READ"])


@pytest.mark.parametrize(
    ("mode", "expected_voltage", "expected_cart_mode"),
    [
        ("DMG", "SET_VOLTAGE_5V", 1),
        ("AGB", "SET_VOLTAGE_3_3V", 2),
    ],
)
def test_set_mode_configures_protocol_without_power_cycle(
    mode: str,
    expected_voltage: str,
    expected_cart_mode: int,
) -> None:
    device = GbxDevice()
    device.FW = modern_firmware(cart_power_ctrl=False)
    device.DEVICE = MockSerial()  # type: ignore[assignment]
    device._write = Mock()  # type: ignore[method-assign]
    device._set_fw_variable = Mock()  # type: ignore[method-assign]
    device.SetPin = Mock()  # type: ignore[method-assign]

    device.SetMode(mode)

    assert mode == device.MODE
    device._write.assert_any_call(
        device.DEVICE_CMD[f"SET_MODE_{mode}"],
        wait=True,
    )
    device._write.assert_any_call(device.DEVICE_CMD[expected_voltage], wait=True)
    device._set_fw_variable.assert_any_call("CART_MODE", expected_cart_mode)
    device._set_fw_variable.assert_any_call(key="ADDRESS", value=0)
    device.SetPin.assert_called_once_with(["PIN_AUDIO"], mode == "DMG")


def test_change_baud_rate_sends_protocol_command_and_closes_port() -> None:
    serial_device = MockSerial()
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]
    device.IsConnected = Mock(return_value=True)  # type: ignore[method-assign]
    device._write = Mock()  # type: ignore[method-assign]

    device.ChangeBaudRate(1_500_000)

    device._write.assert_called_once_with(device.DEVICE_CMD["OFW_USART_1_5M_SPEED"])
    assert device.BAUDRATE == 1_500_000
    assert serial_device.is_open is False


def test_change_baud_rate_rejects_unsupported_speed() -> None:
    device = GbxDevice()
    device.DEVICE = MockSerial()  # type: ignore[assignment]
    device.IsConnected = Mock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="Unsupported"):
        device.ChangeBaudRate(115_200)


def test_check_active_uses_legacy_firmware_query() -> None:
    device = GbxDevice()
    device.DEVICE = MockSerial()  # type: ignore[assignment]
    device.FW = modern_firmware(fw_ver=11)
    device.LAST_CHECK_ACTIVE = 0
    device._write = Mock()  # type: ignore[method-assign]
    device._read = Mock(return_value=31)  # type: ignore[method-assign]

    assert device.CheckActive() is True
    device._write.assert_called_once_with(bytearray([device.DEVICE_CMD["OFW_FW_VER"]]))


def test_check_active_disconnects_when_legacy_query_fails() -> None:
    serial_device = MockSerial()
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]
    device.FW = modern_firmware(fw_ver=11)
    device.LAST_CHECK_ACTIVE = 0
    device._write = Mock()  # type: ignore[method-assign]
    device._read = Mock(return_value=False)  # type: ignore[method-assign]

    assert device.CheckActive() is False
    assert device.DEVICE is None
    assert serial_device.is_open is False


def test_device_capabilities_and_version_labels_are_derived_from_firmware() -> None:
    device = GbxDevice()
    device.DEVICE = MockSerial()  # type: ignore[assignment]
    device.FW = modern_firmware(cart_power_ctrl=True, bootloader_reset=True)
    device.PORT = "mock-port"

    assert device.GetFirmwareVersion() == "R31+L18"
    assert device.GetFirmwareVersion(more=True).endswith(" (2026-06-03T12:25:02+00:00)")
    assert device.GetFullName() == "GBxCart RW v1.4a/b/c"
    assert device.GetFullNameExtended() == ("GBxCart RW v1.4a/b/c – Firmware R31+L18 (mock-port)")
    assert device.CanPowerCycleCart() is True
    assert device.GetSupprtedModes() == ["DMG", "AGB"]
    assert device.IsClkConnected() is True
    assert device.SupportsBootloaderReset() is True


def test_linknload_probe_disables_updates_and_restores_timeout() -> None:
    serial_device = MockSerial(timeout=0.7)
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]
    device.FW = modern_firmware(ofw_ver=30)
    device._write = Mock()  # type: ignore[method-assign]
    device._read = Mock(return_value=0x31)  # type: ignore[method-assign]

    assert device.SupportsFirmwareUpdates() is False
    device._write.assert_called_once_with(device.DEVICE_CMD["OFW_LNL_QUERY"])
    assert serial_device.timeout == 0.7


@pytest.mark.parametrize(
    ("firmware", "expected", "expected_request"),
    [
        (modern_firmware(fw_ver=0, pcb_ver=4), True, True),
        (modern_firmware(fw_ver=0, pcb_ver=2), True, 2),
        (modern_firmware(pcb_ver=100), False, False),
        (modern_firmware(pcb_ver=6, fw_ts=0), True, False),
        (modern_firmware(), False, False),
    ],
)
def test_firmware_update_availability(
    firmware: FirmwareInfo,
    expected: bool,
    expected_request: bool | int,
) -> None:
    device = GbxDevice()
    device.FW = firmware
    device.FW_UPDATE_REQ = False

    assert device.FirmwareUpdateAvailable() is expected
    assert expected_request == device.FW_UPDATE_REQ


def test_set_timeout_clamps_to_backend_minimum() -> None:
    serial_device = MockSerial(timeout=0.1)
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]

    device.SetTimeout(0.05)

    assert device.DEVICE_TIMEOUT == 1
    assert serial_device.timeout == 1


def test_firmware_updater_rejects_corrupt_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad-firmware.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("fw.ini", b"key")
    status = Mock()

    assert FirmwareUpdater(port="mock-port").WriteFirmware(archive_path, status) == 3
    assert "corrupted" in status.call_args.args[0]


def test_firmware_updater_reports_when_no_device_is_discovered(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "firmware.zip"
    build_firmware_archive(archive_path, b"pokemon-red")
    status = Mock()

    assert FirmwareUpdater().WriteFirmware(archive_path, status) == 2
    assert status.call_args.args[0] == "No device found."


def test_firmware_updater_writes_decrypted_payload_over_mock_serial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firmware = b"pokemon-red"
    archive_path = tmp_path / "firmware.zip"
    build_firmware_archive(archive_path, firmware)
    serial_device = EchoSerial()
    status = Mock()
    monkeypatch.setattr(gbxcartrw.serial, "Serial", lambda *args, **kwargs: serial_device)
    monkeypatch.setattr(gbxcartrw.time, "sleep", lambda _seconds: None)

    result = FirmwareUpdater(port="mock-port").WriteFirmware(archive_path, status)

    assert result == 1
    assert serial_device.writes == [bytes([value]) for value in firmware]
    assert serial_device.is_open is False
    assert status.call_args.args[0] == "Done!"


def test_read_header_identifies_synthetic_pokemon_red_without_hardware(
    pokemon_red_header: bytearray,
) -> None:
    device = GbxDevice()
    device.DEVICE = MockSerial()  # type: ignore[assignment]
    device.FW = modern_firmware()
    device.MODE = "DMG"
    device.INFO = {"action": None, "last_action": None, "dump_info": {}}
    device.IsConnected = Mock(return_value=True)  # type: ignore[method-assign]
    device._write = Mock(return_value=1)  # type: ignore[method-assign]
    device._set_fw_variable = Mock()  # type: ignore[method-assign]
    device.ReadROM = Mock(return_value=pokemon_red_header)  # type: ignore[method-assign]

    header = device.ReadHeader(checkRtc=False)

    assert header["game_title"] == "POKEMON RED"
    assert header["logo_correct"] is True
    assert header["header_checksum_correct"] is True
    assert header["rom_checksum_correct"] is True
    assert header["mapper_raw"] == 0x13
    assert header["rom_size_raw"] == 0x05
    assert header["ram_size_raw"] == 0x03
    assert header["has_rtc"] is False
    assert header["raw"] == pokemon_red_header
    device.ReadROM.assert_called_once_with(0, 0x180)
    device._write.assert_any_call(device.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
    device._write.assert_any_call(device.DEVICE_CMD["SET_ADDR_AS_INPUTS"], wait=True)


def test_connection_guards_and_typed_read_helpers_reject_missing_data() -> None:
    device = GbxDevice()

    with pytest.raises(RuntimeError, match="Firmware information"):
        device._firmware()
    with pytest.raises(ConnectionError, match="not connected"):
        device._serial_device()

    device._read = Mock(side_effect=[7, bytearray(b"AB"), 9, False, bytearray(b"X")])  # type: ignore[method-assign]
    assert device._read_byte() == 7
    assert device._read_bytes(2) == b"AB"
    assert device._read_bytes(1) == b"\x09"
    with pytest.raises(ConnectionError, match="Expected 2 bytes"):
        device._read_bytes(2)
    with pytest.raises(ConnectionError, match="Expected 2 bytes"):
        device._read_bytes(2)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (PermissionError("Permission denied"), "permission"),
        (OSError("serial controller failure"), "critical error"),
    ],
)
def test_initialize_reports_serial_open_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    message: str,
) -> None:
    device = GbxDevice()
    monkeypatch.setattr(gbxcartrw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(device, "TryConnect", Mock(side_effect=error))

    result = device.Initialize(port="mock-port", max_baud=1_000_000)

    assert result is not False
    assert result[0][0] == 3
    assert message in str(result[0][1]).lower()
    assert device.DEVICE is None


def test_initialize_ignores_disappearing_serial_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    monkeypatch.setattr(gbxcartrw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(device, "TryConnect", Mock(side_effect=FileNotFoundError("gone")))

    assert device.Initialize(port="mock-port", max_baud=1_000_000) == []
    assert device.FW is None


def test_initialize_reopens_supported_device_at_high_speed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_serial = MockSerial()
    second_serial = MockSerial()
    serial_devices = iter([first_serial, second_serial])
    opens: list[tuple[object, ...]] = []
    device = GbxDevice()

    def connect(_port: str, _baudrate: int) -> bool:
        device.FW = modern_firmware(pcb_ver=6)
        return True

    def open_serial(*args: object, **_kwargs: object) -> MockSerial:
        opens.append(args)
        return next(serial_devices)

    def change_baud(baudrate: int) -> None:
        device.BAUDRATE = baudrate
        first_serial.close()

    monkeypatch.setattr(gbxcartrw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gbxcartrw.serial, "Serial", open_serial)
    monkeypatch.setattr(device, "TryConnect", connect)
    monkeypatch.setattr(device, "ChangeBaudRate", change_baud)
    monkeypatch.setattr(device, "IsConnected", lambda: True)

    assert device.Initialize(port="mock-port", max_baud=1_500_000) == []
    assert opens == [("mock-port", 1_000_000), ("mock-port", 1_500_000)]
    assert first_serial.is_open is False
    assert device.DEVICE is second_serial
    assert device.BAUDRATE == 1_500_000


@pytest.mark.parametrize(
    ("firmware", "expected_status", "expected_write_buffer"),
    [
        (modern_firmware(fw_ts=GbxDevice.DEVICE_LATEST_FW_TS[6] + 1), 1, 0x400),
        (modern_firmware(fw_ver=0, pcb_ver=4, fw_ts=0), None, 0x100),
    ],
)
def test_initialize_warns_for_new_firmware_and_sizes_legacy_buffers(
    monkeypatch: pytest.MonkeyPatch,
    firmware: FirmwareInfo,
    expected_status: int | None,
    expected_write_buffer: int,
) -> None:
    serial_device = MockSerial()
    device = GbxDevice()

    def connect(_port: str, _baudrate: int) -> bool:
        device.FW = firmware
        return True

    monkeypatch.setattr(gbxcartrw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gbxcartrw.serial, "Serial", lambda *args, **kwargs: serial_device)
    monkeypatch.setattr(device, "TryConnect", connect)
    monkeypatch.setattr(device, "IsConnected", lambda: True)

    messages = device.Initialize(port="mock-port", max_baud=1_000_000)

    assert messages is not False
    assert ([message[0] for message in messages] or [None]) == [expected_status]
    assert expected_write_buffer == device.MAX_BUFFER_WRITE


def test_load_firmware_version_handles_absent_device_and_empty_response() -> None:
    device = GbxDevice()
    assert device.LoadFirmwareVersion() is False

    serial_device = MockSerial(timeout=0.6, responses=[b""])
    device.DEVICE = serial_device  # type: ignore[assignment]
    assert device.LoadFirmwareVersion() is False
    assert serial_device.is_open is True
    assert serial_device.timeout == 0.6


@pytest.mark.parametrize(("cfw_id", "version"), [(b"A", 18), (b"L", 11)])
def test_load_firmware_version_skips_modern_capabilities_for_old_protocols(
    cfw_id: bytes,
    version: int,
) -> None:
    payload = bytes([8]) + struct.pack(">cHBI", cfw_id, version, 6, 1_700_000_000)
    serial_device = MockSerial(responses=[b"\x06", b"\x1f", payload])
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]

    assert device.LoadFirmwareVersion() is True
    assert device.FW is not None
    assert device.FW["cfw_id"] == cfw_id.decode()
    assert device.FW["pcb_name"] == ""
    assert device.FW["cart_power_ctrl"] is False


def test_load_firmware_version_accepts_empty_modern_device_name() -> None:
    payload = bytes([8]) + struct.pack(">cHBI", b"L", 12, 6, 1_700_000_000)
    serial_device = MockSerial(responses=[b"\x06", b"\x1f", payload + b"\x00\x05\x00"])
    device = GbxDevice()
    device.DEVICE = serial_device  # type: ignore[assignment]

    assert device.LoadFirmwareVersion() is True
    assert device.FW is not None
    assert device.FW["pcb_name"] == ""
    assert device.FW["cart_power_ctrl"] is True
    assert device.FW["cart_mode_switch"] is True


def test_change_baud_rate_noops_when_disconnected_and_supports_one_megabaud() -> None:
    device = GbxDevice()
    device.IsConnected = Mock(return_value=False)  # type: ignore[method-assign]
    device.ChangeBaudRate(1_000_000)
    assert device.DEVICE is None

    serial_device = MockSerial()
    device.DEVICE = serial_device  # type: ignore[assignment]
    device.IsConnected = Mock(return_value=True)  # type: ignore[method-assign]
    device._write = Mock()  # type: ignore[method-assign]
    device.ChangeBaudRate(1_000_000)
    device._write.assert_called_once_with(device.DEVICE_CMD["OFW_USART_1_0M_SPEED"])
    assert serial_device.is_open is False


def test_check_active_fast_paths_and_modern_parent_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = GbxDevice()
    cached.LAST_CHECK_ACTIVE = gbxcartrw.time.time()
    assert cached.CheckActive() is True

    disconnected = GbxDevice()
    disconnected.LAST_CHECK_ACTIVE = 0
    assert disconnected.CheckActive() is False

    legacy = GbxDevice()
    legacy.DEVICE = MockSerial()  # type: ignore[assignment]
    legacy.FW = modern_firmware(fw_ver=0)
    legacy.LAST_CHECK_ACTIVE = 0
    assert legacy.CheckActive() is True

    reloadable = GbxDevice()
    reloadable.DEVICE = MockSerial()  # type: ignore[assignment]
    reloadable.FW = modern_firmware(pcb_name=None)
    reloadable.LAST_CHECK_ACTIVE = 0
    reloadable.LoadFirmwareVersion = Mock(return_value=True)  # type: ignore[method-assign]
    assert reloadable.CheckActive() is True

    modern = GbxDevice()
    modern.DEVICE = MockSerial()  # type: ignore[assignment]
    modern.FW = modern_firmware()
    modern.LAST_CHECK_ACTIVE = 0
    monkeypatch.setattr(gbxcartrw.LK_Device, "CheckActive", lambda _self: True)
    assert modern.CheckActive() is True


def test_legacy_labels_and_capability_fallbacks() -> None:
    legacy = GbxDevice()
    legacy.DEVICE = MockSerial()  # type: ignore[assignment]
    legacy.FW = modern_firmware(fw_ver=0, pcb_ver=5)
    legacy.PORT = "legacy-port"
    assert legacy.GetFirmwareVersion() == "R31"
    assert "legacy-port" in legacy.GetFullNameExtended(more=True)
    assert legacy.CanPowerCycleCart() is True

    custom = GbxDevice()
    custom.FW = modern_firmware(pcb_ver=4)
    custom.PORT = "custom-port"
    assert custom.GetFirmwareVersion() == "L18"
    assert custom.GetFullNameExtended(more=True).endswith("custom-port at 1.0M baud")
    assert custom.CanSetVoltageBySwitch() is False
    assert custom.CanSetVoltageByCode() is True
    assert custom.CanSetVoltageByAutoswitch() is False
    assert custom.IsSupported3dMemory() is True
    assert custom.IsClkConnected() is False

    dmg_only = GbxDevice()
    dmg_only.FW = modern_firmware(pcb_ver=101)
    assert dmg_only.GetSupprtedModes() == ["DMG"]


def test_update_support_reset_leds_and_static_capabilities() -> None:
    device = GbxDevice()
    assert device.SupportsFirmwareUpdates() is True
    assert device.CanPowerCycleCart() is False
    assert device.SupportsBootloaderReset() is False
    assert device.BootloaderReset() is False
    assert device.SupportsAudioAsWe() is True

    serial_device = MockSerial()
    device.DEVICE = serial_device  # type: ignore[assignment]
    device.FW = modern_firmware(pcb_ver=255, bootloader_reset=True)
    device._write = Mock()  # type: ignore[method-assign]
    device._read = Mock(return_value=0)  # type: ignore[method-assign]
    device.ResetLEDs()
    device._write.assert_called_once_with(device.DEVICE_CMD["OFW_CART_MODE"])
    assert device.SupportsFirmwareUpdates() is False
    assert device.SupportsBootloaderReset() is True


def test_firmware_updater_reports_open_and_echo_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "firmware.zip"
    build_firmware_archive(archive_path, b"pokemon-red")
    status = Mock()
    monkeypatch.setattr(
        gbxcartrw.serial,
        "Serial",
        Mock(side_effect=gbxcartrw.SerialException("busy")),
    )

    assert FirmwareUpdater(port="mock-port").WriteFirmware(archive_path, status) == 2
    assert status.call_args.kwargs == {"text": "Device not accessible.", "enableUI": True}

    serial_device = MockSerial()
    monkeypatch.setattr(gbxcartrw.serial, "Serial", lambda *args, **kwargs: serial_device)
    status.reset_mock()
    assert FirmwareUpdater(port="mock-port").WriteFirmware(archive_path, status) == 2
    assert status.call_args.kwargs == {"text": "Update failed!", "enableUI": True}
    assert serial_device.is_open is False
