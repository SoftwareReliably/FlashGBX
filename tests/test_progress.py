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
    assert progress.progress["action"] == "FINISHED"
    assert "method" not in progress.progress
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
    assert progress.progress == {}


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


def test_progress_validates_and_clamps_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    monkeypatch.setattr(progress_module.time, "time", lambda: 5.0)
    progress = Progress(lambda event: updates.append(dict(event)), lambda _event: None)

    progress.SetProgress({"action": 1})
    progress.SetProgress({"action": "INITIALIZE", "method": None})
    assert updates == []

    progress.SetProgress(
        {
            "action": "INITIALIZE",
            "method": "ROM_READ",
            "flash_offset": -10,
            "size": True,
            "pos": -20,
            "sector_count": 0,
            "time_start": "invalid",
            "abortable": "invalid",
            "voltage": True,
        },
    )

    assert (
        updates[-1].items()
        >= {
            "flash_offset": 0,
            "size": 0,
            "pos": 0,
            "sector_count": 1,
            "time_start": 5.0,
            "abortable": True,
            "voltage": 0.0,
        }.items()
    )

    update_count = len(updates)
    progress.SetProgress({"action": "UPDATE_INFO", "text": 123})
    progress.SetProgress({"action": "UPDATE_POS", "pos": True, "force_update": True})
    assert len(updates) == update_count


def test_progress_calculates_speed_skip_and_sector_time_estimates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    times = iter([0.0, 3.0, 4.0, 5.0])
    monkeypatch.setattr(progress_module.time, "time", lambda: next(times))
    progress = Progress(lambda event: updates.append(dict(event)), lambda _event: None)

    progress.SetProgress(
        {
            "action": "INITIALIZE",
            "method": "ROM_WRITE",
            "size": 4096,
            "sector_count": 4,
            "time_start": 0.0,
        },
    )
    progress.SetProgress(
        {
            "action": "UPDATE_POS",
            "pos": 1024,
            "sector_pos": 1,
            "sector_erase_time": 2.0,
            "abortable": False,
            "force_update": True,
        },
    )
    progress.SetProgress({"action": "UPDATE_POS", "pos": 2048, "force_update": True})

    assert updates[-1]["speed"] == 1.0
    assert updates[-1]["time_left"] == 8.0
    assert updates[-1]["sector_erase_time"] == 2.0
    assert updates[-1]["abortable"] is False

    progress.SetProgress(
        {
            "action": "UPDATE_POS",
            "pos": 3072,
            "sector_pos": 2,
            "skipping": True,
            "force_update": True,
        },
    )
    assert updates[-1]["speed"] == 0.0
    assert updates[-1]["skipping"] is True
    assert updates[-1]["sector_erase_time"] == 0.0


def test_progress_averages_erase_time_and_ignores_mismatched_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    times = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(progress_module.time, "time", lambda: next(times))
    progress = Progress(lambda event: updates.append(dict(event)), lambda _event: None)
    progress.SetProgress({"action": "INITIALIZE", "method": "ROM_WRITE", "size": 100})

    progress.SetProgress(
        {"action": "UPDATE_POS", "pos": 25, "sector_erase_time": 2.0, "force_update": True},
    )
    progress.SetProgress(
        {"action": "UPDATE_POS", "pos": 50, "sector_erase_time": 4.0, "force_update": True},
    )
    assert updates[-1]["sector_erase_time"] == 3.0

    update_count = len(updates)
    progress.SetProgress({"action": "READ", "bytes_added": 25, "force_update": True})
    assert len(updates) == update_count


def test_progress_uses_active_state_for_auxiliary_elapsed_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    times = iter([10.0, 12.5])
    monkeypatch.setattr(progress_module.time, "time", lambda: next(times))
    progress = Progress(lambda event: updates.append(dict(event)), lambda _event: None)
    progress.SetProgress({"action": "INITIALIZE", "method": "SAVE_READ", "size": 10, "time_start": 10.0})

    progress.SetProgress({"action": "ERASE"})

    assert updates[-1]["time_elapsed"] == 2.5
    assert updates[-1]["pos"] == 1
    assert updates[-1]["size"] == 0


def test_progress_speed_history_discards_outliers_and_stays_bounded() -> None:
    steady_state = {"speeds": [10.0] * Progress.SPEED_OUTLIER_START}
    Progress._record_speed(steady_state, 100.0)  # type: ignore[arg-type]
    assert steady_state["speeds"] == [10.0] * Progress.SPEED_OUTLIER_START

    full_state = {"speeds": [float(value) for value in range(Progress.SPEED_SAMPLE_LIMIT)]}
    Progress._record_speed(full_state, 25.0)  # type: ignore[arg-type]
    assert len(full_state["speeds"]) == Progress.SPEED_SAMPLE_LIMIT
    assert full_state["speeds"][0] == 1.0


def test_progress_covers_throttled_repeated_and_optional_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    times = iter([0.0, 0.01, 3.0, 4.0])
    monkeypatch.setattr(progress_module.time, "time", lambda: next(times))
    progress = Progress(lambda event: updates.append(dict(event)), lambda _event: None)
    progress.SetProgress({"action": "INITIALIZE", "method": "ROM_READ", "size": 2048, "time_start": 0.0})

    initial_count = len(updates)
    progress.SetProgress({"action": "READ", "bytes_added": 256})
    assert len(updates) == initial_count

    progress.SetProgress(
        {"action": "UPDATE_POS", "pos": 1024, "sector_pos": True, "force_update": True},
    )
    assert "sector_pos" not in updates[-1]
    assert updates[-1]["speed"] > 0
    assert updates[-1]["time_left"] > 0

    progress.SetProgress({"action": "UPDATE_POS", "pos": 1024, "force_update": True})
    assert updates[-1]["pos"] == 1024


def test_progress_handles_auxiliary_and_finish_events_without_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    times = iter([1.0, 2.0, 3.0, 4.0])
    monkeypatch.setattr(progress_module.time, "time", lambda: next(times))
    progress = Progress(lambda event: updates.append(dict(event)), lambda _event: None)

    progress.SetProgress({"action": "ERASE"})
    assert "time_elapsed" not in updates[-1]

    progress._initialize({"action": "INITIALIZE"}, 2.0)  # type: ignore[typeddict-item]
    progress.SetProgress({"action": "INITIALIZE", "method": "SAVE_READ", "size": 16})
    progress.SetProgress({"action": "FINISHED"})
    assert "verified" not in updates[-1]

    progress.SetProgress({"action": "UNKNOWN"})
