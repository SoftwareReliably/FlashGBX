"""Tests for the synchronous data-transfer worker lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from serial import SerialException

import FlashGBX.DataTransfer as data_transfer_module  # noqa: N813
from FlashGBX.DataTransfer import DataTransfer

if TYPE_CHECKING:
    from collections.abc import Callable


class RecordingPort:
    """Minimal transfer target that records worker calls and can fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[dict[str, Any], object]] = []

    def TransferData(self, config: dict[str, Any], signal: object) -> None:
        self.calls.append((config, signal))
        if self.error is not None:
            raise self.error
        emit = signal.emit  # type: ignore[attr-defined]
        emit({"action": "PORT_EVENT"})


def transfer_config(port: RecordingPort, name: str = "backup") -> dict[str, Any]:
    """Build the small configuration shape consumed by the worker."""
    return {"port": port, "name": name}


def test_worker_without_config_finishes_without_invoking_a_port() -> None:
    worker = DataTransfer()

    assert worker.isRunning() is True
    worker.run()

    assert worker.transfer_finished is True
    assert worker.isRunning() is False
    assert worker.config is None


def test_worker_forwards_config_and_progress_signal() -> None:
    port = RecordingPort()
    config = transfer_config(port)
    events: list[dict[str, object]] = []
    worker = DataTransfer(config)
    worker.updateProgress.connect(events.append)

    worker.run()

    assert len(port.calls) == 1
    called_config, called_signal = port.calls[0]
    assert called_config is config
    assert called_signal is worker.updateProgress
    assert events == [{"action": "PORT_EVENT"}]
    assert worker.transfer_finished is True
    assert worker.isRunning() is False


def test_set_config_reuses_worker_and_resets_completion() -> None:
    first_port = RecordingPort()
    second_port = RecordingPort()
    first_config = transfer_config(first_port, "first")
    second_config = transfer_config(second_port, "second")
    worker = DataTransfer(first_config)

    worker.run()
    assert worker.isRunning() is False

    worker.setConfig(second_config)
    assert worker.config is second_config
    assert worker.transfer_finished is False
    assert worker.isRunning() is True

    worker.run()

    assert [call[0] for call in first_port.calls] == [first_config]
    assert [call[0] for call in second_port.calls] == [second_config]
    assert worker.isRunning() is False


def test_windows_usb_disconnect_emits_specific_nonfatal_abort(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = SerialException("GetOverlappedResult failed: mocked disconnect")
    worker = DataTransfer(transfer_config(RecordingPort(error)))
    events: list[dict[str, object]] = []
    diagnostics: list[str] = []
    worker.updateProgress.connect(events.append)
    monkeypatch.setattr(data_transfer_module, "dprint", diagnostics.append)

    worker.run()

    assert len(events) == 1
    event = events[0]
    assert event["action"] == "ABORT"
    assert event["info_type"] == "msgbox_critical"
    assert event["abortable"] is False
    assert "USB connection was lost" in str(event["info_msg"])
    assert "fatal" not in event
    assert diagnostics == []
    assert capsys.readouterr().out == ""
    assert worker.isRunning() is False


@pytest.mark.parametrize(
    ("error_factory", "detail"),
    [
        (lambda: SerialException("serial failure"), "SerialException: serial failure"),
        (SerialException, "SerialException:"),
        (lambda: RuntimeError("bad transfer"), "RuntimeError: bad transfer"),
    ],
    ids=["serial", "serial-without-arguments", "unexpected"],
)
def test_other_exceptions_emit_fatal_abort_and_record_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_factory: Callable[[], Exception],
    detail: str,
) -> None:
    worker = DataTransfer(transfer_config(RecordingPort(error_factory())))
    events: list[dict[str, object]] = []
    diagnostics: list[str] = []
    worker.updateProgress.connect(events.append)
    monkeypatch.setattr(data_transfer_module, "dprint", diagnostics.append)

    worker.run()

    assert len(events) == 1
    event = events[0]
    assert event["action"] == "ABORT"
    assert event["info_type"] == "msgbox_critical"
    assert event["fatal"] is True
    assert event["abortable"] is False
    assert detail in str(event["info_msg"])
    assert len(diagnostics) == 1
    assert "Traceback (most recent call last)" in diagnostics[0]
    if str(error_factory()):
        assert str(error_factory()) in diagnostics[0]
    captured = capsys.readouterr().out
    assert captured == diagnostics[0] + "\n"
    assert worker.isRunning() is False


def test_workers_keep_configuration_and_completion_state_isolated() -> None:
    first_port = RecordingPort()
    second_port = RecordingPort()
    first_config = transfer_config(first_port, "first")
    replacement_config = transfer_config(first_port, "replacement")
    second_config = transfer_config(second_port, "second")
    first_worker = DataTransfer(first_config)
    second_worker = DataTransfer(second_config)

    first_worker.setConfig(replacement_config)
    first_worker.run()

    assert first_worker.config is replacement_config
    assert second_worker.config is second_config
    assert first_worker.isRunning() is False
    assert second_worker.isRunning() is True
    assert second_port.calls == []

    second_worker.run()

    assert [call[0] for call in first_port.calls] == [replacement_config]
    assert [call[0] for call in second_port.calls] == [second_config]
    assert second_worker.isRunning() is False
