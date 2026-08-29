"""Tests for normalized transfer-progress event handling."""

from __future__ import annotations

from typing import TYPE_CHECKING

import FlashGBX.Progress as progress_module  # noqa: N813
from FlashGBX.Progress import Progress

if TYPE_CHECKING:
    import pytest


def test_progress_lifecycle_emits_initial_updates_and_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    waits: list[dict[str, object]] = []
    times = iter([10.0, 10.0, 12.0, 13.0, 14.0])
    monkeypatch.setattr(progress_module.time, "time", lambda: next(times))
    progress = Progress(lambda event: updates.append(dict(event)), waits.append)

    progress.SetProgress(
        {
            "action": "INITIALIZE",
            "method": "ROM_READ",
            "flash_offset": 100,
            "size": 1100,
            "pos": 90,
            "sector_count": 2,
            "time_start": 10.0,
            "abortable": False,
            "voltage": 5.0,
        },
    )
    progress.SetProgress({"action": "UPDATE_INFO", "text": "Reading"})
    progress.SetProgress(
        {
            "action": "UPDATE_POS",
            "pos": 300,
            "sector_pos": 1,
            "sector_erase_time": 1.5,
            "force_update": True,
        },
    )
    progress.SetProgress({"action": "READ", "bytes_added": 500, "force_update": True})
    progress.SetProgress({"action": "FINISHED", "verified": True})

    assert updates[0]["action"] == "INITIALIZE"
    assert updates[0]["size"] == 1000
    assert updates[0]["pos"] == 0
    assert updates[0]["voltage"] == 5.0
    assert updates[1]["action"] == "UPDATE_INFO"
    assert updates[1]["text"] == "Reading"
    assert updates[-2]["pos"] == 1000
    assert updates[-1]["action"] == "FINISHED"
    assert updates[-1]["verified"] is True
    assert progress.PROGRESS["action"] == "FINISHED"
    assert "method" not in progress.PROGRESS
    assert waits == []


def test_progress_handles_auxiliary_user_and_abort_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    waits: list[dict[str, object]] = []
    monkeypatch.setattr(progress_module.time, "time", lambda: 20.0)
    progress = Progress(updates.append, waits.append)

    progress.SetProgress({"action": "USER_ACTION", "user_action": "RETRY_5V"})
    progress.SetProgress({"action": "ERROR", "time_start": 19.0, "msg": "failed"})
    progress.SetProgress({"action": "ABORT", "from_user": True})
    progress.SetProgress({"action": "UNKNOWN"})

    assert waits == [{"action": "USER_ACTION", "user_action": "RETRY_5V"}]
    assert updates[0]["action"] == "ERROR"
    assert updates[0]["time_elapsed"] == 1.0
    assert updates[0]["pos"] == 1
    assert updates[1] == {"action": "ABORT", "from_user": True}
    assert progress.PROGRESS == {}


def test_progress_filters_directional_events_and_throttles_position_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    progress = Progress(updates.append, lambda _event: None)
    monkeypatch.setattr(progress_module.time, "time", lambda: 1.0)
    progress.SetProgress({"action": "INITIALIZE", "method": "SAVE_READ", "size": 100})
    update_count = len(updates)

    progress.SetProgress({"action": "WRITE", "bytes_added": 10})
    assert len(updates) == update_count
    progress.SetProgress({"action": "READ", "bytes_added": 10, "force_update": True})
    assert updates[-1]["pos"] == 10


def test_progress_speed_helpers_reject_bad_values_and_outliers() -> None:
    state = {"speeds": []}
    assert Progress._int_or_default(True, 4) == 4
    assert Progress._int_or_default(3, 4) == 3
    assert Progress._float_or_default(False, 4.0) == 4.0
    assert Progress._float_or_default(3, 4.0) == 3.0
    assert Progress._is_outlier([], 100.0, 25.0) is False
    Progress._record_speed(state, 10.0)  # type: ignore[arg-type]
    assert state["speeds"] == [10.0]
