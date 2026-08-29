"""Hardware-free tests for the GBxCart RW backend."""

from __future__ import annotations

import struct
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import FlashGBX.hw_GBxCartRW as gbxcartrw
from FlashGBX.hw_GBxCartRW import FirmwareInfo, GbxDevice

from .fakes import MockSerial


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


def test_initialize_returns_false_without_discovered_hardware() -> None:
    """The default test environment must never fall through to real serial."""

    device = GbxDevice()

    assert device.Initialize() is False
    assert device.DEVICE is None
    assert device.PORT == ""


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


def test_read_rom_chunks_requests_without_serial_hardware() -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    device.INFO = {"action": None, "last_action": None, "dump_info": {}}
    device._set_fw_variable = Mock()  # type: ignore[method-assign]
    device._write = Mock()  # type: ignore[method-assign]
    device._read = Mock(  # type: ignore[method-assign]
        side_effect=[bytearray(b"ABCD"), bytearray(b"EFGH")]
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
