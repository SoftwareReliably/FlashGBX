# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import ClassVar, Literal, TypeAlias, TypedDict, cast

ProgressAction: TypeAlias = Literal[  # noqa: UP040
    "USER_ACTION",
    "INITIALIZE",
    "ABORT",
    "ERASE",
    "SECTOR_ERASE",
    "UNLOCK",
    "UPDATE_RTC",
    "CALC_CHECKSUMS",
    "ERROR",
    "READ",
    "WRITE",
    "UPDATE_POS",
    "UPDATE_INFO",
    "FINISHED",
]
ProgressStateAction: TypeAlias = Literal[  # noqa: UP040
    "INITIALIZE", "PROGRESS", "UPDATE_INFO", "FINISHED"
]
UserAction: TypeAlias = Literal["REINSERT_CART", "RETRY_5V"]  # noqa: UP040
ProgressCallback: TypeAlias = Callable[[Mapping[str, object]], None]  # noqa: UP040
ProgressPayload: TypeAlias = dict[str, object]  # noqa: UP040


class _ProgressEventBase(TypedDict):
    action: ProgressAction


class ProgressEvent(_ProgressEventBase, total=False):
    """Payload sent to :class:`Progress` by transfer and flash-cart code."""

    method: str
    voltage: float
    flash_offset: int
    size: int
    pos: int
    sector_count: int
    time_start: float
    abortable: bool
    bytes_added: int
    skipping: bool
    force_update: bool
    sector_erase_time: float
    sector_pos: int
    text: str
    verified: bool
    user_action: UserAction
    msg: str
    title: str
    info_type: str
    info_msg: str
    fatal: bool
    from_user: bool
    error: str
    type: str
    time_estimated: float


class _OptionalProgressState(TypedDict, total=False):
    voltage: float
    sector_pos: int
    text: str
    skipping: bool
    time_elapsed: float
    bytes_last_emit: int
    verified: bool


class ProgressState(_OptionalProgressState):
    """Complete state maintained for an active transfer."""

    action: ProgressStateAction
    method: str
    flash_offset: int
    size: int
    pos: int
    sector_count: int
    time_start: float
    abortable: bool
    time_last_emit: float
    time_last_update_speed: float
    time_left: float
    speed: float
    speeds: list[float]
    bytes_last_update_speed: int
    sector_erase_time: float


class Progress:
    """Aggregate transfer events and publish throttled progress updates.

    Transfer workers send small event dictionaries while the GUI or CLI
    consumes the normalized state through ``updater``.  The public uppercase
    attributes are retained for compatibility with existing callers.
    """

    MUTEX: ClassVar[threading.Lock] = threading.Lock()

    # These are deliberately conservative: progress updates are UI-facing and
    # should not compete with the cartridge I/O loop for too much time.
    EMIT_INTERVAL: ClassVar[float] = 0.06
    SPEED_WARMUP: ClassVar[float] = 2.0
    SPEED_OUTLIER_START: ClassVar[int] = 40
    SPEED_SAMPLE_LIMIT: ClassVar[int] = 50
    SPEED_OUTLIER_THRESHOLD: ClassVar[float] = 25.0

    def __init__(
        self,
        updater: ProgressCallback,
        waiter: ProgressCallback,
    ) -> None:
        self.PROGRESS: ProgressState = cast(ProgressState, {})
        self.UPDATER: ProgressCallback = updater
        self.WAITER: ProgressCallback = waiter

    def _active_state(self) -> ProgressState | None:
        """Return the active state, if an operation has been initialized."""

        if "method" not in self.PROGRESS:
            return None
        return self.PROGRESS

    def _emit(self, payload: Mapping[str, object]) -> None:
        """Publish a payload through the configured update callback."""

        self.UPDATER(payload)

    @staticmethod
    def _int_or_default(value: object, default: int) -> int:
        """Return an integer payload value, falling back for bad input."""

        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return default

    @staticmethod
    def _float_or_default(value: object, default: float) -> float:
        """Return a numeric payload value as a float, falling back if needed."""

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return default

    @staticmethod
    def _is_outlier(
        speeds: Sequence[float],
        new_number: float,
        threshold: float,
    ) -> bool:
        """Return whether ``new_number`` is far outside the speed history."""

        if not speeds:
            return False
        mean = statistics.fmean(speeds)
        standard_deviation = statistics.pstdev(speeds)
        return bool(abs(new_number - mean) > threshold * standard_deviation)

    @classmethod
    def _record_speed(cls, state: ProgressState, speed: float) -> None:
        """Add a valid speed sample while keeping a bounded history."""

        speeds = state["speeds"]
        if len(speeds) < cls.SPEED_OUTLIER_START or not cls._is_outlier(
            speeds=speeds,
            new_number=speed,
            threshold=cls.SPEED_OUTLIER_THRESHOLD,
        ):
            speeds.append(speed)
        if len(speeds) > cls.SPEED_SAMPLE_LIMIT:
            del speeds[: len(speeds) - cls.SPEED_SAMPLE_LIMIT]

    def _initialize(self, event: ProgressEvent, now: float) -> None:
        """Start a fresh operation and publish its initial state."""

        method = event.get("method")
        if not isinstance(method, str):
            return
        flash_offset = max(self._int_or_default(event.get("flash_offset"), 0), 0)
        size = max(self._int_or_default(event.get("size"), 0) - flash_offset, 0)
        position = max(
            self._int_or_default(event.get("pos"), flash_offset) - flash_offset,
            0,
        )
        position = min(position, size)
        abortable = event.get("abortable")
        if not isinstance(abortable, bool):
            abortable = True

        state: ProgressState = {
            "action": "INITIALIZE",
            "method": method,
            "flash_offset": flash_offset,
            "size": size,
            "pos": position,
            "sector_count": max(self._int_or_default(event.get("sector_count"), 1), 1),
            "time_start": self._float_or_default(event.get("time_start"), now),
            "abortable": abortable,
            "time_last_emit": now,
            "time_last_update_speed": now,
            "time_left": 0.0,
            "speed": 0.0,
            "speeds": [],
            "bytes_last_update_speed": position,
            "sector_erase_time": 0.0,
        }
        if "voltage" in event:
            state["voltage"] = self._float_or_default(event.get("voltage"), 0.0)

        self.PROGRESS = state
        self._emit(state)

    def _handle_auxiliary_event(
        self,
        event: ProgressEvent,
        state: ProgressState | None,
        now: float,
    ) -> None:
        """Publish non-transfer status events such as erase or error updates."""

        payload: ProgressPayload = dict(event)
        if state is not None:
            payload["time_elapsed"] = max(now - state["time_start"], 0.0)
        elif "time_start" in event:
            payload["time_elapsed"] = max(
                now - self._float_or_default(event.get("time_start"), now),
                0.0,
            )
        payload["pos"] = 1
        payload["size"] = 0
        self._emit(payload)

    def _handle_position_event(
        self,
        event: ProgressEvent,
        state: ProgressState,
        now: float,
    ) -> None:
        """Apply a read/write position event and emit it when due."""

        action = event["action"]
        method = state["method"]
        if (
            action == "READ"
            and method in ("SAVE_WRITE", "ROM_WRITE")
            or action == "WRITE"
            and method in ("SAVE_READ", "ROM_READ", "ROM_WRITE_VERIFY")
        ):
            return

        skip_speed = False
        state["action"] = "PROGRESS"
        if action in ("READ", "WRITE"):
            state["pos"] = min(
                state["size"],
                state["pos"] + max(self._int_or_default(event.get("bytes_added"), 0), 0),
            )
        else:
            position = event.get("pos")
            if not isinstance(position, int) or isinstance(position, bool):
                return
            relative_position = position - state["flash_offset"]
            if state["pos"] == relative_position:
                skip_speed = True
            if event.get("skipping") is True:
                skip_speed = True
            state["pos"] = max(0, min(relative_position, state["size"]))

            if "sector_erase_time" in event:
                sector_erase_time = max(
                    self._float_or_default(event.get("sector_erase_time"), 0.0),
                    0.0,
                )
                previous_erase_time = state["sector_erase_time"]
                if previous_erase_time > 0:
                    sector_erase_time = (previous_erase_time + sector_erase_time) / 2
                state["sector_erase_time"] = sector_erase_time
            elif "sector_pos" in event:
                state["sector_erase_time"] = 0.0

            if "sector_pos" in event:
                sector_position = event.get("sector_pos")
                if isinstance(sector_position, int) and not isinstance(sector_position, bool):
                    state["sector_pos"] = max(sector_position, 0)
            abortable = event.get("abortable")
            if isinstance(abortable, bool):
                state["abortable"] = abortable

        force_update = event.get("force_update") is True
        if now - state["time_last_emit"] <= self.EMIT_INTERVAL and not force_update:
            return

        state["time_elapsed"] = max(now - state["time_start"], 0.0)
        time_delta = now - state["time_last_update_speed"]
        position_delta = state["pos"] - state["bytes_last_update_speed"]
        if time_delta > 0 and now - state["time_start"] > self.SPEED_WARMUP and "sector_erase_time" not in event:
            speed = (position_delta / time_delta) / 1024
            if speed > 0 and not skip_speed:
                self._record_speed(state, speed)
            state["speed"] = statistics.fmean(state["speeds"]) if state["speeds"] else 0.0

        state["time_last_update_speed"] = now
        state["bytes_last_update_speed"] = state["pos"]

        if event.get("skipping") is True:
            state["speed"] = 0.0
            state["skipping"] = True
        else:
            state["skipping"] = False

        if state["speed"] > 0 and state["speeds"]:
            state["time_left"] = max(state["size"] - state["pos"], 0) / 1024 / state["speed"]
            if state["sector_erase_time"] > 0:
                sector_position = state.get("sector_pos", 0)
                state["time_left"] += state["sector_erase_time"] * max(
                    state["sector_count"] - sector_position,
                    0,
                )

        self._emit(state)
        state["time_last_emit"] = now

    def _finish(self, event: ProgressEvent, state: ProgressState, now: float) -> None:
        """Publish the final position and completed operation state."""

        # Keep the first emission for compatibility: consumers see the bar
        # reach its endpoint before receiving the FINISHED action.
        state["pos"] = state["size"]
        self._emit(state)

        state["action"] = "FINISHED"
        state["bytes_last_update_speed"] = state["size"]
        elapsed = max(now - state["time_start"], 0.001)
        state["time_elapsed"] = elapsed
        state["time_last_emit"] = now
        state["time_last_update_speed"] = now
        state["time_left"] = 0.0
        state["speed"] = min(
            (state["size"] / elapsed) / 1024,
            state["size"] / 1024,
        )
        state["bytes_last_emit"] = state["size"]
        if "verified" in event:
            state["verified"] = event["verified"] is True

        self._emit(state)
        self.PROGRESS.pop("method", None)

    def SetProgress(self, args: Mapping[str, object]) -> None:
        """Consume one progress event from a transfer worker.

        Events without a recognized action or required fields are ignored. This
        keeps optional progress reporting from interrupting cartridge operations
        if a future worker adds an event that this version does not understand.
        """

        action = args.get("action")
        if not isinstance(action, str):
            return

        event = cast(ProgressEvent, args)
        with self.MUTEX:
            now = time.time()
            state = self._active_state()
            if state is None:
                # ``FINISHED`` removes ``method`` but leaves the last state
                # available for callers that inspect it after completion.
                # Start subsequent event handling from a clean state.
                self.PROGRESS = cast(ProgressState, {})

            if action == "USER_ACTION":
                self.WAITER(args)
                return

            if action == "INITIALIZE":
                if not isinstance(event.get("method"), str):
                    return
                self._initialize(event, now)
                return

            if action == "ABORT":
                self._emit(args)
                self.PROGRESS = cast(ProgressState, {})
                return

            if action in (
                "ERASE",
                "SECTOR_ERASE",
                "UNLOCK",
                "UPDATE_RTC",
                "CALC_CHECKSUMS",
                "ERROR",
            ):
                self._handle_auxiliary_event(event, state, now)
                return

            if state is None:
                return

            if action in ("READ", "WRITE", "UPDATE_POS"):
                self._handle_position_event(event, state, now)
            elif action == "UPDATE_INFO":
                text = event.get("text")
                if not isinstance(text, str):
                    return
                state["text"] = text
                state["action"] = "UPDATE_INFO"
                self._emit(state)
            elif action == "FINISHED":
                self._finish(event, state, now)
