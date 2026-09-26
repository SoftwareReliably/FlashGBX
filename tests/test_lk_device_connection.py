"""Hardware-free regression tests for connection validation and cleanup."""

from __future__ import annotations

from unittest.mock import Mock, call

import pytest
from serial import SerialException

import FlashGBX.LK_Device as lk_device_module
from FlashGBX.hw_GBxCartRW import GbxDevice

from .fakes import MockSerial


@pytest.fixture
def device(monkeypatch: pytest.MonkeyPatch) -> GbxDevice:
    """Use a modern reader with deterministic time and firmware challenges."""
    reader = GbxDevice()
    reader.fw = {"cfw_id": "L", "fw_ver": 18, "pcb_ver": 6, "pcb_name": "Test reader"}
    monkeypatch.setattr(lk_device_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(lk_device_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(lk_device_module.os, "urandom", Mock(return_value=b"\xff"))
    return reader


def test_active_probe_accepts_zero_response_and_caches_success(device: GbxDevice) -> None:
    port = MockSerial(responses=[b"\x00", b"\x00"])
    device.device = port  # type: ignore[assignment]

    assert device.CheckActive() is True
    assert device.last_check_active == 100.0
    assert device.CheckActive() is True
    assert port.writes == [bytes([device.DEVICE_CMD["PING"], 0xFF])]

    # The cached result expires at exactly one second.
    device.last_check_active = 99.0
    assert device.CheckActive() is True
    assert len(port.writes) == 2
    assert port.is_open is True


@pytest.mark.parametrize("response", [b"", b"\xff", b"\x01"])
@pytest.mark.parametrize("reconnecting", [False, True])
def test_active_probe_disconnects_on_timeout_or_wrong_response(
    device: GbxDevice,
    capsys: pytest.CaptureFixture[str],
    response: bytes,
    reconnecting: bool,
) -> None:
    port = MockSerial(responses=[response])
    device.device = port  # type: ignore[assignment]
    device.user_answer = reconnecting

    assert device.CheckActive() is False
    assert device.device is None
    assert port.is_open is False
    assert device.last_check_active == 0.0
    assert port.writes == [bytes([device.DEVICE_CMD["PING"], 0xFF])]
    assert ("Invalid firmware response" in capsys.readouterr().out) is not reconnecting


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_older_custom_firmware_validates_supported_mode(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    assert device.fw is not None
    device.fw["fw_ver"] = 14
    port = MockSerial()
    device.device = port  # type: ignore[assignment]
    query = Mock(return_value=mode)
    monkeypatch.setattr(device, "_get_fw_variable", query)

    assert device.CheckActive() is (mode <= 2)
    query.assert_called_once_with("CART_MODE")
    assert port.is_open is (mode <= 2)
    expected_check_time = 100.0 if mode <= 2 else 0.0
    assert expected_check_time == device.last_check_active


@pytest.mark.parametrize("loaded", [False, True])
def test_active_probe_reloads_unknown_firmware(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    loaded: bool,
) -> None:
    device.fw = {"pcb_name": None}
    device.device = MockSerial()  # type: ignore[assignment]
    load = Mock(return_value=loaded)
    monkeypatch.setattr(device, "LoadFirmwareVersion", load)

    assert device.CheckActive() is loaded
    load.assert_called_once_with()
    expected_check_time = 100.0 if loaded else 0.0
    assert expected_check_time == device.last_check_active


@pytest.mark.parametrize("loaded", [False, True])
def test_try_connect_closes_temporary_port_after_firmware_probe(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    loaded: bool,
) -> None:
    port = MockSerial()
    open_port = Mock(return_value=port)
    monkeypatch.setattr(lk_device_module.serial, "Serial", open_port)

    def load_firmware() -> bool:
        assert device.device is port
        assert port.is_open is True
        return loaded

    monkeypatch.setattr(device, "LoadFirmwareVersion", load_firmware)

    assert device.TryConnect("test-port", 1_000_000) is loaded
    open_port.assert_called_once_with("test-port", 1_000_000, timeout=0.1, exclusive=True)
    assert device.device is None
    assert port.is_open is False


def test_try_connect_closes_temporary_port_when_probe_raises(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = MockSerial()
    monkeypatch.setattr(lk_device_module.serial, "Serial", Mock(return_value=port))
    monkeypatch.setattr(device, "LoadFirmwareVersion", Mock(side_effect=SerialException("probe failed")))

    with pytest.raises(SerialException, match="probe failed"):
        device.TryConnect("test-port", 1_000_000)

    assert device.device is None
    assert port.is_open is False


@pytest.mark.parametrize("error", [SerialException("busy"), OSError("unavailable")])
def test_try_connect_handles_port_open_failure(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr(lk_device_module.serial, "Serial", Mock(side_effect=error))
    load = Mock()
    monkeypatch.setattr(device, "LoadFirmwareVersion", load)

    assert device.TryConnect("test-port", 1_000_000) is False
    assert device.device is None
    load.assert_not_called()


@pytest.mark.parametrize("closed", [False, True])
def test_is_connected_skips_probe_without_open_port(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    closed: bool,
) -> None:
    if closed:
        port = MockSerial()
        port.close()
        device.device = port  # type: ignore[assignment]
    check = Mock()
    monkeypatch.setattr(device, "CheckActive", check)

    assert device.IsConnected() is False
    check.assert_not_called()


@pytest.mark.parametrize("active", [False, True])
def test_is_connected_drains_stale_input_before_probing(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    active: bool,
) -> None:
    port = MockSerial(responses=[b"stale reply"])
    port.write(b"previous command")
    device.device = port  # type: ignore[assignment]
    operations = Mock()
    monkeypatch.setattr(port, "reset_output_buffer", operations.reset_output_buffer)

    def check_active() -> bool:
        assert port.in_waiting == 0
        operations.reset_output_buffer.assert_called_once_with()
        return active

    monkeypatch.setattr(device, "CheckActive", check_active)

    assert device.IsConnected() is active
    assert port.in_waiting == 0


@pytest.mark.parametrize(
    ("message", "closed"),
    [("ClearCommError failed (disconnected)", True), ("read failed", False)],
)
def test_is_connected_handles_serial_errors(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
    closed: bool,
) -> None:
    port = MockSerial()
    device.device = port  # type: ignore[assignment]
    monkeypatch.setattr(port, "reset_output_buffer", Mock(side_effect=SerialException(message)))
    check = Mock()
    monkeypatch.setattr(device, "CheckActive", check)

    assert device.IsConnected() is False
    assert port.is_open is not closed
    assert "Connection lost!" in capsys.readouterr().out
    check.assert_not_called()


@pytest.mark.parametrize(
    ("firmware_version", "power_control", "power_off", "command", "wait"),
    [
        (18, True, True, "CART_PWR_OFF", True),
        (11, True, True, "OFW_CART_PWR_OFF", False),
        (18, False, True, "SET_VOLTAGE_3_3V", True),
        (18, True, False, "SET_VOLTAGE_3_3V", True),
        (11, True, False, "SET_VOLTAGE_3_3V", False),
    ],
)
def test_close_selects_shutdown_command_and_releases_port(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    firmware_version: int,
    power_control: bool,
    power_off: bool,
    command: str,
    wait: bool,
) -> None:
    assert device.fw is not None
    device.fw.update(fw_ver=firmware_version, cart_power_ctrl=power_control)
    device.mode = "DMG"
    port = MockSerial()
    device.device = port  # type: ignore[assignment]
    operations = Mock()
    monkeypatch.setattr(device, "ResetLEDs", operations.reset_leds)
    monkeypatch.setattr(device, "IsConnected", Mock(return_value=True))
    monkeypatch.setattr(device, "_set_fw_variable", operations.set_variable)
    monkeypatch.setattr(device, "_write", operations.write)

    device.Close(cartPowerOff=power_off)

    expected = [call.reset_leds()]
    if command != "SET_VOLTAGE_3_3V":
        expected.append(call.set_variable("AUTO_POWEROFF_TIME", 0))
    expected.append(call.write(device.DEVICE_CMD[command], wait=wait))
    assert operations.mock_calls == expected
    assert device.device is None
    assert device.mode is None
    assert port.is_open is False


@pytest.mark.parametrize("stage", ["ResetLEDs", "IsConnected", "_write"])
@pytest.mark.parametrize("error_type", [ConnectionError, OSError, SerialException])
def test_close_releases_port_even_when_shutdown_fails(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_type: type[Exception],
) -> None:
    port = MockSerial()
    device.device = port  # type: ignore[assignment]
    device.mode = "AGB"
    monkeypatch.setattr(device, "ResetLEDs", Mock())
    monkeypatch.setattr(device, "IsConnected", Mock(return_value=True))
    monkeypatch.setattr(device, "_write", Mock())
    monkeypatch.setattr(device, stage, Mock(side_effect=error_type("shutdown failed")))

    device.Close()

    assert device.device is None
    assert device.mode is None
    assert port.is_open is False


@pytest.mark.parametrize("error_type", [OSError, SerialException])
def test_close_clears_connection_state_even_when_serial_close_fails(
    device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    port = MockSerial()
    device.device = port  # type: ignore[assignment]
    device.mode = "DMG"
    close = Mock(side_effect=error_type("port vanished"))
    monkeypatch.setattr(port, "close", close)
    monkeypatch.setattr(device, "ResetLEDs", Mock())
    monkeypatch.setattr(device, "IsConnected", Mock(return_value=False))

    device.Close()

    close.assert_called_once_with()
    assert device.device is None
    assert device.mode is None


def test_close_is_safe_to_repeat_without_connected_hardware(device: GbxDevice) -> None:
    device.mode = "DMG"

    device.Close(cartPowerOff=True)
    device.Close()

    assert device.device is None
    assert device.mode is None
