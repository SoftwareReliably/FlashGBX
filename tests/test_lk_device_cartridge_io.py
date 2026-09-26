"""Hardware-free checks of cartridge addressing and firmware wire formats."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock, call

import pytest

import FlashGBX.LK_Device as device_module
from FlashGBX.hw_GBxCartRW import GbxDevice
from tests.test_lk_device_protocol import RecoverySerial

if TYPE_CHECKING:
    from FlashGBX.LK_Device import DeviceMode, FirmwareVariable


@pytest.fixture
def device() -> GbxDevice:
    result = GbxDevice()
    result.fw = {"fw_ver": 12, "pcb_name": "Test device"}
    result.max_buffer_read = 32
    return result


@pytest.mark.parametrize(
    ("mode", "address", "length", "save_flash", "reader", "translated", "read_length", "reply", "expected"),
    [
        ("DMG", 0x9FFF, 0, False, "ReadROM", 0x9FFF, 1, b"\x00", 0),
        ("DMG", 0xA000, 0, False, "ReadRAM", 0, 1, b"\x81", 0x81),
        ("DMG", 0xA123, 0, False, "ReadRAM", 0x123, 1, b"\xff", 0xFF),
        ("DMG", 0x4000, 3, False, "ReadROM", 0x4000, 3, b"ROM", bytearray(b"ROM")),
        ("DMG", 0xA123, 3, False, "ReadRAM", 0x123, 3, b"RAM", bytearray(b"RAM")),
        ("AGB", 0x2468, 0, False, "ReadROM", 0x1234, 2, b"\x12\x34", 0x1234),
        ("AGB", 0x2468, 3, False, "ReadROM", 0x2468, 3, b"ROM", bytearray(b"ROM")),
        ("AGB", 0x123, 0, True, "ReadRAM", 0x123, 1, b"\x81", 0x81),
        ("AGB", 0x123, 3, True, "ReadRAM", 0x123, 3, b"RAM", bytearray(b"RAM")),
    ],
)
def test_cart_read_routes_addresses_and_decodes_scalar_values(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    mode: DeviceMode,
    address: int,
    length: int,
    save_flash: bool,
    reader: str,
    translated: int,
    read_length: int,
    reply: bytes,
    expected: int | bytearray,
) -> None:
    device.mode = mode
    rom = Mock(return_value=bytearray(reply))
    ram = Mock(return_value=bytearray(reply))
    monkeypatch.setattr(device, "ReadROM", rom)
    monkeypatch.setattr(device, "ReadRAM", ram)

    result = device._cart_read(address, length, agb_save_flash=save_flash)

    assert result == expected
    assert type(result) is type(expected)
    options = {"max_length": 32}
    if mode == "AGB" and save_flash:
        options["command"] = 0xC3
    selected, unused = (rom, ram) if reader == "ReadROM" else (ram, rom)
    selected.assert_called_once_with(translated, read_length, **options)
    unused.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "address", "save_flash", "reply"),
    [
        ("DMG", 0x4000, False, False),
        ("DMG", 0xA000, False, bytearray()),
        ("AGB", 0x2000, False, False),
        ("AGB", 0x2000, False, bytearray(b"\x12")),
        ("AGB", 0, True, bytearray()),
    ],
)
def test_scalar_cart_read_rejects_missing_or_short_data(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    mode: DeviceMode,
    address: int,
    save_flash: bool,
    reply: bool | bytearray,
) -> None:
    device.mode = mode
    monkeypatch.setattr(device, "ReadROM", Mock(return_value=reply))
    monkeypatch.setattr(device, "ReadRAM", Mock(return_value=reply))

    assert device._cart_read(address, agb_save_flash=save_flash) is False


def test_cart_io_requires_a_mode_before_accessing_hardware(device: GbxDevice) -> None:
    with pytest.raises(RuntimeError, match="mode must be DMG or AGB before reading"):
        device._cart_read(0)
    with pytest.raises(RuntimeError, match="mode must be DMG or AGB before writing"):
        device._cart_write(0, 0)


@pytest.mark.parametrize("firmware", [11, 12])
@pytest.mark.parametrize(
    ("mode", "flashcart", "expected"),
    [
        ("DMG", False, bytes.fromhex("B2 00 00 24 68 78")),
        ("DMG", True, bytes.fromhex("D1 00 00 24 68 78")),
        ("AGB", False, bytes.fromhex("C2 00 00 12 34 56 78")),
        ("AGB", True, bytes.fromhex("D2 00 00 12 34 56 78")),
    ],
)
def test_cart_write_encodes_address_value_width_and_firmware_transport(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    firmware: int,
    mode: DeviceMode,
    flashcart: bool,
    expected: bytes,
) -> None:
    device.mode = mode
    device.fw["fw_ver"] = firmware
    write, try_write = Mock(), Mock()
    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "_try_write", try_write)

    device._cart_write(0x2468, 0x12345678, flashcart=flashcart)

    selected, unused = (try_write, write) if firmware >= 12 else (write, try_write)
    selected.assert_called_once_with(expected)
    unused.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (
            "DMG",
            [
                call.variable("DMG_WRITE_CS_PULSE", 1),
                call.variable("ADDRESS", 0xA123),
                call.variable("TRANSFER_SIZE", 1),
                call.write(0xB3),
                call.write(0x81, wait=True),
            ],
        ),
        (
            "AGB",
            [
                call.variable("TRANSFER_SIZE", 1),
                call.variable("ADDRESS", 0xA123),
                call.write(0xC4),
                call.write(0x81, wait=True),
            ],
        ),
    ],
)
def test_cart_sram_write_configures_transfer_before_sending_payload(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    mode: DeviceMode,
    expected: list[object],
) -> None:
    device.mode = mode
    transport = Mock()
    monkeypatch.setattr(device, "_set_fw_variable", transport.variable)
    monkeypatch.setattr(device, "_write", transport.write)
    monkeypatch.setattr(device, "_try_write", transport.retry)

    device._cart_write(0xA123, 0x81, sram=True)

    assert transport.mock_calls == expected


@pytest.mark.parametrize(
    ("firmware", "mode", "flashcart", "expected"),
    [
        (5, "AGB", False, "D4 02 00 00 24 68 AA 00 00 13 56 55"),
        (6, "DMG", False, "D4 00 02 00 00 24 68 00 AA 00 00 13 56 00 55"),
        (12, "DMG", True, "D4 01 02 00 00 24 68 00 AA 00 00 13 56 00 55"),
        (12, "AGB", False, "D4 00 02 00 00 24 68 00 AA 00 00 13 56 00 55"),
        (12, "AGB", True, "D4 01 02 00 00 12 34 00 AA 00 00 09 AB 00 55"),
    ],
)
def test_flash_command_batch_preserves_order_and_firmware_wire_format(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    firmware: int,
    mode: DeviceMode,
    flashcart: bool,
    expected: str,
) -> None:
    device.mode = mode
    device.fw["fw_ver"] = firmware
    write = Mock()
    read = Mock(side_effect=[1])
    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "_read", read)

    assert device._cart_write_flash([[0x2468, 0xAA], [0x1356, 0x55]], flashcart=flashcart) is True

    write.assert_called_once_with(bytes.fromhex(expected))
    read.assert_called_once_with(1)


@pytest.mark.parametrize(("mode", "flashcart"), [("DMG", False), ("DMG", True), ("AGB", True)])
def test_old_firmware_falls_back_to_individual_flash_commands(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    mode: DeviceMode,
    flashcart: bool,
) -> None:
    device.mode = mode
    device.fw["fw_ver"] = 5
    write = Mock()
    monkeypatch.setattr(device, "_cart_write", write)

    assert device._cart_write_flash([[0x555, 0xAA], [0x2AA, 0x55]], flashcart=flashcart) is None

    assert write.call_args_list == [
        call(0x555, 0xAA, flashcart=flashcart),
        call(0x2AA, 0x55, flashcart=flashcart),
    ]


@pytest.mark.parametrize(
    ("response", "message", "resets"),
    [(False, "No response", 1), (0, "Bad response", 0), (2, "Bad response", 0)],
)
def test_flash_command_batch_rejects_bad_acknowledgements_and_clears_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    device: GbxDevice,
    response: int | bool,
    message: str,
    resets: int,
) -> None:
    device.mode = "DMG"
    serial_device = RecoverySerial()
    device.device = serial_device  # type: ignore[assignment]
    write = Mock()
    read = Mock(side_effect=[response])
    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "_read", read)
    monkeypatch.setattr(device_module.time, "sleep", Mock())

    assert device._cart_write_flash([[0x555, 0xAA]], flashcart=True) is False

    write.assert_called_once_with(bytes.fromhex("D4 01 01 00 00 05 55 00 AA"))
    read.assert_called_once_with(1)
    assert message in capsys.readouterr().out
    assert serial_device.reset_input_calls == resets


@pytest.mark.parametrize(
    ("key", "expected_packet"),
    [
        ("ADDRESS", "AD 04 00 00 00 00"),
        ("BUFFER_SIZE", "AD 02 00 00 00 01"),
        ("FLASH_WE_PIN", "AD 01 00 00 00 04"),
    ],
)
def test_get_firmware_variable_encodes_width_and_decodes_big_endian_reply(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    key: FirmwareVariable,
    expected_packet: str,
) -> None:
    write = Mock()
    read = Mock(side_effect=[bytearray.fromhex("12 34 56 78")])
    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "_read", read)

    assert device._get_fw_variable(key) == 0x12345678
    write.assert_called_once_with(bytes.fromhex(expected_packet))
    read.assert_called_once_with(4)


def test_old_firmware_variable_read_returns_zero_without_io(device: GbxDevice) -> None:
    device.fw["fw_ver"] = 9
    assert device._get_fw_variable("ADDRESS") == 0


@pytest.mark.parametrize("response", [False, 0, 1])
def test_get_firmware_variable_rejects_non_buffer_responses(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    response: int | bool,
) -> None:
    monkeypatch.setattr(device, "_write", Mock())
    monkeypatch.setattr(device, "_read", Mock(side_effect=[response]))
    assert device._get_fw_variable("ADDRESS") is False


@pytest.mark.parametrize("firmware", [11, 12])
@pytest.mark.parametrize(
    ("key", "value", "expected_packet"),
    [
        ("ADDRESS", 0xFFFFFFFF, "A6 04 00 00 00 00 FF FF FF FF"),
        ("BUFFER_SIZE", 0x1234, "A6 02 00 00 00 01 00 00 12 34"),
        ("FLASH_WE_PIN", 0, "A6 01 00 00 00 04 00 00 00 00"),
    ],
)
def test_set_firmware_variable_encodes_value_and_selects_transport(
    monkeypatch: pytest.MonkeyPatch,
    device: GbxDevice,
    firmware: int,
    key: FirmwareVariable,
    value: int,
    expected_packet: str,
) -> None:
    device.fw["fw_ver"] = firmware
    write, try_write = Mock(return_value=None), Mock(return_value=3)
    monkeypatch.setattr(device, "_write", write)
    monkeypatch.setattr(device, "_try_write", try_write)

    result = device._set_fw_variable(key, value)

    assert result == (3 if firmware >= 12 else None)
    selected, unused = (try_write, write) if firmware >= 12 else (write, try_write)
    selected.assert_called_once_with(bytes.fromhex(expected_packet))
    unused.assert_not_called()
    assert {key: value} == device.fw_var


@pytest.mark.parametrize("value", [-1, 0x100000000])
def test_set_firmware_variable_rejects_overflow_without_changing_cache(
    device: GbxDevice,
    value: int,
) -> None:
    device.fw_var["ADDRESS"] = 0x1234

    with pytest.raises(ValueError, match="must fit in 32 bits"):
        device._set_fw_variable("ADDRESS", value)

    assert device.fw_var == {"ADDRESS": 0x1234}
