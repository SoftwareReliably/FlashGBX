"""Tests for shared device transfer dispatch and worker cleanup."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, call

import pytest
from serial import PortNotOpenError, SerialTimeoutException

import FlashGBX.DataTransfer as data_transfer_module  # noqa: N813
import FlashGBX.LK_Device as lk_device_module
from FlashGBX.hw_GBxCartRW import GbxDevice


@pytest.fixture
def connected_device(monkeypatch: pytest.MonkeyPatch) -> GbxDevice:
    """Return a device whose connection boundary is fully mocked."""
    device = GbxDevice()
    device.FW = {"fw_ver": 1, "pcb_name": "Test device"}  # type: ignore[typeddict-item]
    monkeypatch.setattr(device, "IsConnected", Mock(return_value=True))
    monkeypatch.setattr(device, "CanPowerCycleCart", Mock(return_value=False))
    return device


@pytest.mark.parametrize(
    ("mode", "selected_handler"),
    [
        (1, "_BackupROM"),
        (2, "_BackupRestoreRAM"),
        (3, "_BackupRestoreRAM"),
        (4, "_FlashROM"),
        (5, "_DetectCartridge"),
        (0xFF, "Debug"),
    ],
)
def test_transfer_data_dispatches_each_mode_and_resets_state(
    connected_device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
    selected_handler: str,
) -> None:
    handlers = {
        name: Mock(return_value=True)
        for name in ("_BackupROM", "_BackupRestoreRAM", "_FlashROM", "_DetectCartridge", "Debug")
    }
    for name, handler in handlers.items():
        monkeypatch.setattr(connected_device, name, handler)

    connected_device.ERROR = True
    connected_device.CANCEL = True
    connected_device.CANCEL_ARGS = {"reason": "old cancellation"}
    connected_device.READ_ERRORS = 3
    connected_device.WRITE_ERRORS = 4
    connected_device.NO_PROG_UPDATE = True

    def signal(_event: dict[str, object]) -> None:
        pass

    args: dict[str, Any] = {"mode": mode, "buffer": bytearray(b"payload")}

    result = connected_device.TransferData(args, signal)

    assert result is True
    assert connected_device.ERROR is False
    assert connected_device.CANCEL is False
    assert connected_device.CANCEL_ARGS == {}
    assert connected_device.READ_ERRORS == 0
    assert connected_device.WRITE_ERRORS == 0
    assert connected_device.NO_PROG_UPDATE is False
    assert connected_device.SIGNAL is signal
    for name, handler in handlers.items():
        if name != selected_handler:
            handler.assert_not_called()
        elif name == "Debug":
            handler.assert_called_once_with()
        else:
            handler.assert_called_once_with(args)


def test_transfer_data_powers_cartridge_before_dispatch(
    connected_device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    power_on = Mock()
    backup = Mock(return_value=True)
    monkeypatch.setattr(connected_device, "CanPowerCycleCart", Mock(return_value=True))
    monkeypatch.setattr(connected_device, "CartPowerOn", power_on)
    monkeypatch.setattr(connected_device, "_BackupROM", backup)

    assert connected_device.TransferData({"mode": 1}, lambda _event: None) is True

    power_on.assert_called_once_with()
    backup.assert_called_once_with({"mode": 1})


@pytest.mark.parametrize(
    ("result", "error", "commands", "expected_command"),
    [
        (True, False, {"OFW_DONE_LED_ON": 10, "OFW_ERROR_LED_ON": 11}, 10),
        (False, True, {"OFW_DONE_LED_ON": 10, "OFW_ERROR_LED_ON": 11}, 11),
        (True, True, {"OFW_DONE_LED_ON": 10, "OFW_ERROR_LED_ON": 11}, 10),
        (False, True, {"OFW_DONE_LED_ON": 10}, None),
    ],
)
def test_transfer_data_updates_supported_completion_led(
    connected_device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    result: bool,
    error: bool,
    commands: dict[str, int],
    expected_command: int | None,
) -> None:
    connected_device.FW["fw_ver"] = 2
    connected_device.FW["pcb_name"] = "GBxCart RW"
    connected_device.DEVICE_CMD = commands
    write = Mock()
    monkeypatch.setattr(connected_device, "_write", write)

    def backup_rom(_args: dict[str, Any]) -> bool:
        connected_device.ERROR = error
        return result

    monkeypatch.setattr(connected_device, "_BackupROM", backup_rom)

    assert connected_device.TransferData({"mode": 1}, lambda _event: None) is True

    if expected_command is None:
        write.assert_not_called()
    else:
        write.assert_called_once_with(expected_command)


def test_cart_power_on_gbxcartrw_retries_unexpected_ack(
    connected_device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected_device.FW["fw_ver"] = 12
    connected_device.FW["pcb_name"] = "GBxCart RW"
    connected_device.MODE = "DMG"
    connected_device.DEVICE_CMD = {
        "QUERY_CART_PWR": "query",
        "SET_MODE_DMG": "dmg_mode",
        "CART_PWR_ON": "power_on",
        "DMG_MBC_RESET": "reset_mbc",
    }
    serial_device = Mock(in_waiting=1)
    serial_device.read.side_effect = [b"\x00", b"\x01"]
    monkeypatch.setattr(connected_device, "CanPowerCycleCart", Mock(return_value=True))
    write = Mock()
    cart_write = Mock()
    monkeypatch.setattr(connected_device, "_write", write)
    monkeypatch.setattr(connected_device, "_read", Mock(side_effect=[0, 1]))
    monkeypatch.setattr(connected_device, "_serial_device", Mock(return_value=serial_device))
    monkeypatch.setattr(connected_device, "_cart_write", cart_write)
    monkeypatch.setattr(lk_device_module.time, "sleep", Mock())

    assert connected_device.CartPowerOn() is True

    assert write.call_args_list == [
        call("query"),
        call("dmg_mode", wait=True),
        call("power_on"),
        call("query"),
        call("query"),
        call("reset_mbc", wait=True),
    ]
    cart_write.assert_called_once_with(0, 0xFF)
    assert serial_device.timeout == connected_device.DEVICE_TIMEOUT


def test_cart_power_on_gbxcartrw_closes_device_after_ack_timeouts(
    connected_device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected_device.FW["fw_ver"] = 12
    connected_device.FW["pcb_name"] = "GBxCart RW"
    connected_device.MODE = "DMG"
    connected_device.DEVICE_CMD = {
        "QUERY_CART_PWR": "query",
        "SET_MODE_DMG": "dmg_mode",
        "CART_PWR_ON": "power_on",
        "DMG_MBC_RESET": "reset_mbc",
    }
    serial_device = Mock(in_waiting=1)
    serial_device.read.return_value = b"\x00"
    monkeypatch.setattr(connected_device, "CanPowerCycleCart", Mock(return_value=True))
    write = Mock()
    monkeypatch.setattr(connected_device, "_write", write)
    monkeypatch.setattr(connected_device, "_read", Mock(return_value=0))
    monkeypatch.setattr(connected_device, "_serial_device", Mock(return_value=serial_device))
    monkeypatch.setattr(lk_device_module.time, "sleep", Mock())

    with pytest.raises(BrokenPipeError, match="Couldn't power on the cartridge"):
        connected_device.CartPowerOn()

    serial_device.close.assert_called_once_with()
    assert connected_device.DEVICE is None
    assert connected_device.ERROR is True
    assert write.call_args_list.count(call("query")) == 11


def test_disconnected_transfer_resets_counters_without_dispatching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    handlers = [Mock() for _ in range(5)]
    for name, handler in zip(
        ("_BackupROM", "_BackupRestoreRAM", "_FlashROM", "_DetectCartridge", "Debug"),
        handlers,
        strict=True,
    ):
        monkeypatch.setattr(device, name, handler)
    monkeypatch.setattr(device, "IsConnected", Mock(return_value=False))
    device.ERROR = True
    device.CANCEL = True
    device.CANCEL_ARGS = {"stale": True}
    device.READ_ERRORS = 8
    device.WRITE_ERRORS = 9

    assert device.TransferData({"mode": 1}, lambda _event: None) is None

    assert device.ERROR is False
    assert device.CANCEL is False
    assert device.CANCEL_ARGS == {}
    assert device.READ_ERRORS == 0
    assert device.WRITE_ERRORS == 0
    assert all(handler.call_count == 0 for handler in handlers)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (SerialTimeoutException("timeout"), "Connection timed out"),
        (PortNotOpenError(), "Connection closed"),
    ],
)
def test_serial_failures_clear_voltage_fallback_state(
    connected_device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    message: str,
) -> None:
    monkeypatch.setattr(connected_device, "_BackupROM", Mock(side_effect=error))
    connected_device.VOLTAGE_FALLBACK_PENDING = True

    result = connected_device.TransferData({"mode": 1}, lambda _event: None)

    assert result is False
    assert connected_device.VOLTAGE_FALLBACK_PENDING is False
    assert message in capsys.readouterr().out


def test_flash_without_fallback_does_not_retry_after_abort(
    connected_device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = []

    def flash(_args: dict[str, Any]) -> bool:
        connected_device.SetProgress({"action": "ABORT", "abortable": False})
        return False

    flash_handler = Mock(side_effect=flash)
    monkeypatch.setattr(connected_device, "_FlashROM", flash_handler)
    args = {"mode": 4}

    assert connected_device.TransferData(args, events.append) is True

    flash_handler.assert_called_once_with(args)
    assert events == [{"action": "ABORT", "abortable": False}]
    assert connected_device.VOLTAGE_FALLBACK_PENDING is False
    assert connected_device.VOLTAGE_FALLBACK_TRIGGERED is False
    assert "override_voltage" not in args


def test_flash_automatically_retries_with_fallback_voltage(
    connected_device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = []
    calls: list[dict[str, Any]] = []

    def flash(args: dict[str, Any]) -> bool:
        calls.append(dict(args))
        if len(calls) == 1:
            connected_device.ERROR = True
            connected_device.CANCEL = True
            connected_device.CANCEL_ARGS = {"old": True}
            connected_device.ERROR_ARGS = {"old": True}
            connected_device.SetProgress({"action": "ABORT", "abortable": False})
            return False
        return True

    monkeypatch.setattr(connected_device, "_FlashROM", flash)
    args: dict[str, Any] = {"mode": 4, "voltage_fallback": 5, "ask_voltage_fallback": False}

    assert connected_device.TransferData(args, events.append) is True

    assert len(calls) == 2
    assert calls[0]["voltage_fallback"] == 5
    assert "override_voltage" not in calls[0]
    assert calls[1]["voltage_fallback"] is False
    assert calls[1]["override_voltage"] == 5
    assert [event["action"] for event in events] == ["UPDATE_INFO"]
    assert connected_device.ERROR is False
    assert connected_device.CANCEL is False
    assert connected_device.CANCEL_ARGS == {}
    assert connected_device.ERROR_ARGS == {}
    assert connected_device.VOLTAGE_FALLBACK_PENDING is False
    assert connected_device.VOLTAGE_FALLBACK_TRIGGERED is False


@pytest.mark.parametrize(("answer", "expected_calls"), [(True, 2), (False, 1)])
def test_flash_prompt_accepts_or_declines_fallback_voltage(
    connected_device: GbxDevice,
    monkeypatch: pytest.MonkeyPatch,
    answer: bool,
    expected_calls: int,
) -> None:
    events: list[dict[str, object]] = []
    calls: list[dict[str, Any]] = []

    def flash(args: dict[str, Any]) -> bool:
        calls.append(dict(args))
        if len(calls) == 1:
            connected_device.SetProgress({"action": "ABORT", "abortable": False})
            return False
        return True

    def answer_prompt(event: dict[str, object]) -> None:
        events.append(dict(event))
        if event.get("user_action") == "RETRY_5V":
            connected_device.USER_ANSWER = answer

    monkeypatch.setattr(connected_device, "_FlashROM", flash)
    args: dict[str, Any] = {"mode": 4, "voltage_fallback": 5, "ask_voltage_fallback": True}

    assert connected_device.TransferData(args, answer_prompt) is True

    assert len(calls) == expected_calls
    assert events[0]["action"] == "USER_ACTION"
    assert events[0]["user_action"] == "RETRY_5V"
    if answer:
        assert [event["action"] for event in events] == ["USER_ACTION", "UPDATE_INFO"]
        assert calls[1]["override_voltage"] == 5
        assert calls[1]["voltage_fallback"] is False
    else:
        assert events[-1] == {"action": "ABORT", "abortable": False}
        assert "override_voltage" not in args
        assert args["voltage_fallback"] == 5
    assert connected_device.USER_ANSWER is None
    assert connected_device.VOLTAGE_FALLBACK_PENDING is False
    assert connected_device.VOLTAGE_FALLBACK_TRIGGERED is False


def test_transfer_wrappers_create_connect_and_reuse_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workers: list[RecordingWorker] = []

    class RecordingSignal:
        def __init__(self) -> None:
            self.callbacks: list[object] = []

        def connect(self, callback: object) -> None:
            self.callbacks.append(callback)

    class RecordingWorker:
        def __init__(self, config: dict[str, Any]) -> None:
            self.configs = [dict(config)]
            self.updateProgress = RecordingSignal()
            self.starts = 0
            workers.append(self)

        def setConfig(self, config: dict[str, Any]) -> None:
            self.configs.append(dict(config))

        def start(self) -> None:
            self.starts += 1

    monkeypatch.setattr(data_transfer_module, "DataTransfer", RecordingWorker)
    device = GbxDevice()
    progress = Mock()
    operations = [
        (device.BackupROM, 1),
        (device.BackupRAM, 2),
        (device.RestoreRAM, 3),
        (device.FlashROM, 4),
        (device.DetectCartridge, 5),
    ]

    for operation, mode in operations:
        operation(fncSetProgress=progress, args={"requested_mode": mode})

    assert len(workers) == 1
    worker = workers[0]
    assert [config["mode"] for config in worker.configs] == [1, 2, 3, 4, 5]
    assert all(config["port"] is device for config in worker.configs)
    assert [config["requested_mode"] for config in worker.configs] == [1, 2, 3, 4, 5]
    assert worker.updateProgress.callbacks == [progress]
    assert worker.starts == 5
    assert device.WORKER is worker


def test_backup_ram_false_progress_runs_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    existing_worker = object()
    device.WORKER = existing_worker
    backup_restore = Mock(return_value=True)
    monkeypatch.setattr(device, "_BackupRestoreRAM", backup_restore)
    args: dict[str, Any] = {"path": "save.sav"}

    device.BackupRAM(fncSetProgress=False, args=args)

    backup_restore.assert_called_once_with(args=args)
    assert args == {"path": "save.sav", "mode": 2, "port": device}
    assert device.WORKER is existing_worker


@pytest.mark.parametrize(
    ("wrapper_name", "worker_name"),
    [
        ("_BackupROM", "_BackupROM_Worker"),
        ("_BackupRestoreRAM", "_BackupRestoreRAM_Worker"),
        ("_FlashROM", "_FlashROM_Worker"),
    ],
)
@pytest.mark.parametrize("raises", [False, True], ids=["success", "exception"])
def test_transfer_wrappers_restore_auto_poweroff_on_every_exit(
    monkeypatch: pytest.MonkeyPatch,
    wrapper_name: str,
    worker_name: str,
    raises: bool,
) -> None:
    device = GbxDevice()
    device.FW = {"fw_ver": 18}  # type: ignore[typeddict-item]
    monkeypatch.setattr(device, "CanPowerCycleCart", Mock(return_value=True))

    def get_variable(key: str) -> int:
        return 1 if key == "AUTO_POWEROFF_ENABLED" else 30_000

    get_fw_variable = Mock(side_effect=get_variable)
    set_fw_variable = Mock()
    monkeypatch.setattr(device, "_get_fw_variable", get_fw_variable)
    monkeypatch.setattr(device, "_set_fw_variable", set_fw_variable)
    worker = Mock(side_effect=RuntimeError("transfer failed")) if raises else Mock(return_value=True)
    monkeypatch.setattr(device, worker_name, worker)
    wrapper = getattr(device, wrapper_name)
    args = {"mode": 1}

    if raises:
        with pytest.raises(RuntimeError, match="transfer failed"):
            wrapper(args)
    else:
        assert wrapper(args) is True

    worker.assert_called_once_with(args)
    assert get_fw_variable.call_args_list == [
        call("AUTO_POWEROFF_ENABLED"),
        call("AUTO_POWEROFF_TIME"),
    ]
    assert set_fw_variable.call_args_list == [
        call("AUTO_POWEROFF_TIME", 5000),
        call("AUTO_POWEROFF_TIME", 30_000),
    ]
    assert device.THREAD_AUTO_POWEROFF_TIME is None


@pytest.mark.parametrize("firmware", [None, {"fw_ver": 11}], ids=["missing", "old"])
def test_auto_poweroff_is_unchanged_for_unsupported_firmware(
    monkeypatch: pytest.MonkeyPatch,
    firmware: dict[str, int] | None,
) -> None:
    device = GbxDevice()
    device.FW = firmware  # type: ignore[assignment]
    can_power_cycle = Mock(return_value=True)
    get_fw_variable = Mock()
    set_fw_variable = Mock()
    monkeypatch.setattr(device, "CanPowerCycleCart", can_power_cycle)
    monkeypatch.setattr(device, "_get_fw_variable", get_fw_variable)
    monkeypatch.setattr(device, "_set_fw_variable", set_fw_variable)
    monkeypatch.setattr(device, "_BackupROM_Worker", Mock(return_value=True))

    assert device._BackupROM({}) is True

    can_power_cycle.assert_not_called()
    get_fw_variable.assert_not_called()
    set_fw_variable.assert_not_called()
    assert device.THREAD_AUTO_POWEROFF_TIME is None


def test_auto_poweroff_is_unchanged_when_firmware_setting_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.FW = {"fw_ver": 18}  # type: ignore[typeddict-item]
    monkeypatch.setattr(device, "CanPowerCycleCart", Mock(return_value=True))
    get_fw_variable = Mock(return_value=0)
    set_fw_variable = Mock()
    monkeypatch.setattr(device, "_get_fw_variable", get_fw_variable)
    monkeypatch.setattr(device, "_set_fw_variable", set_fw_variable)
    monkeypatch.setattr(device, "_BackupROM_Worker", Mock(return_value=True))

    assert device._BackupROM({}) is True

    get_fw_variable.assert_called_once_with("AUTO_POWEROFF_ENABLED")
    set_fw_variable.assert_not_called()
    assert device.THREAD_AUTO_POWEROFF_TIME is None


def test_auto_poweroff_restore_failure_is_logged_without_masking_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()
    device.FW = {"fw_ver": 18}  # type: ignore[typeddict-item]
    monkeypatch.setattr(device, "CanPowerCycleCart", Mock(return_value=True))
    monkeypatch.setattr(
        device,
        "_get_fw_variable",
        Mock(side_effect=lambda key: 1 if key == "AUTO_POWEROFF_ENABLED" else 30_000),
    )
    writes: list[tuple[str, int]] = []

    def set_variable(key: str, value: int) -> None:
        writes.append((key, value))
        if value == 30_000:
            msg = "restore failed"
            raise RuntimeError(msg)

    diagnostics: list[tuple[object, ...]] = []
    monkeypatch.setattr(device, "_set_fw_variable", set_variable)
    monkeypatch.setattr(device, "_BackupROM_Worker", Mock(return_value=True))
    monkeypatch.setattr(lk_device_module, "dprint", lambda *args: diagnostics.append(args))

    assert device._BackupROM({}) is True

    assert writes == [("AUTO_POWEROFF_TIME", 5000), ("AUTO_POWEROFF_TIME", 30_000)]
    assert diagnostics == [("AUTO_POWEROFF thread leave failed:", "restore failed")]
    assert device.THREAD_AUTO_POWEROFF_TIME is None
