"""Protocol-level tests for base flash-cart erase operations."""

from __future__ import annotations

from typing import Any

import pytest

import FlashGBX.Flashcart as flashcart_module  # noqa: N813
from FlashGBX.Flashcart import Flashcart, FlashcartCallbacks, FlashCommands, ProgressInfo
from tests.fakes import flashcart_profile


class AdvancingClock:
    """Deterministic clock whose sleeps advance instead of blocking."""

    def __init__(self) -> None:
        self.current = 100.0

    def time(self) -> float:
        result = self.current
        self.current += 0.01
        return result

    def sleep(self, seconds: float) -> None:
        self.current += seconds


class EraseHarness:
    """Strict cartridge callback harness with ordered protocol events."""

    def __init__(self, responses: list[tuple[int, int, bytes]] | None = None) -> None:
        self.responses = list(responses or [])
        self.events: list[tuple[Any, ...]] = []
        self.progress: list[ProgressInfo] = []

    def read(self, address: int, length: int) -> bytearray:
        self.events.append(("read", address, length))
        if not self.responses:
            msg = f"Unexpected cartridge read at {address:#x} for {length} bytes"
            raise AssertionError(msg)
        expected_address, expected_length, response = self.responses.pop(0)
        assert (address, length) == (expected_address, expected_length)
        return bytearray(response)

    def write(self, address: int, value: int, *, flashcart: bool = False, sram: bool = False) -> None:
        self.events.append(("write_slow", address, value, flashcart, sram))

    def write_fast(self, commands: FlashCommands, *, flashcart: bool = False) -> None:
        self.events.append(("write", [list(command) for command in commands], flashcart))

    def record_progress(self, event: ProgressInfo) -> None:
        copied = dict(event)
        self.progress.append(copied)
        self.events.append(("progress", copied))

    def functions(self) -> FlashcartCallbacks:
        return {
            "cart_write_fncptr": self.write,
            "cart_write_fast_fncptr": self.write_fast,
            "cart_read_fncptr": self.read,
            "cart_powercycle_fncptr": lambda: self.events.append(("power",)),
            "progress_fncptr": self.record_progress,
            "set_we_pin_wr": lambda: self.events.append(("pin", "WR")),
            "set_we_pin_audio": lambda: self.events.append(("pin", "AUDIO")),
        }

    def assert_responses_consumed(self) -> None:
        assert self.responses == []

    def writes(self) -> list[tuple[Any, ...]]:
        return [event for event in self.events if event[0].startswith("write")]

    def pins(self) -> list[tuple[Any, ...]]:
        return [event for event in self.events if event[0] == "pin"]


def ready(value: int = 0x80) -> bytes:
    return value.to_bytes(2, byteorder="little")


def patch_clock(monkeypatch: pytest.MonkeyPatch) -> AdvancingClock:
    clock = AdvancingClock()
    monkeypatch.setattr(flashcart_module.time, "time", clock.time)
    monkeypatch.setattr(flashcart_module.time, "sleep", clock.sleep)
    return clock


def chip_cart(harness: EraseHarness, *, status_polling: bool = False, timeout: float = 1) -> Flashcart:
    commands: dict[str, object] = {
        "reset": [[0xAAA, 0xF0]],
        "chip_erase": [[0x555, 0x10, "AUDIO"]],
        "chip_erase_wait_for": [[0x100, 0x80, 0x80]],
        "read_status_register": [[0x777, 0x70]],
    }
    return Flashcart(
        flashcart_profile(
            commands=commands,
            chip_erase_timeout=timeout,
            wait_read_status_register=status_polling,
        ),
        harness.functions(),
    )


def sector_cart(
    harness: EraseHarness,
    *,
    sector_size: object = 0x1000,
    include_command: bool = True,
    status_polling: bool = False,
) -> Flashcart:
    commands: dict[str, object] = {"reset": [[0xAAA, 0xF0]]}
    if include_command:
        commands.update(
            {
                "sector_erase": [["SA+1", 0x30, "AUDIO"]],
                "sector_erase_wait_for": [["SA+1", 0x80, 0x80]],
                "read_status_register": [[0x777, 0x70]],
            },
        )
    overrides: dict[str, object] = {
        "commands": commands,
        "wait_read_status_register": status_polling,
    }
    if sector_size is not None:
        overrides["sector_size"] = sector_size
    return Flashcart(flashcart_profile(**overrides), harness.functions())


def test_chip_erase_immediate_ready_polls_status_and_restores_write_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_clock(monkeypatch)
    harness = EraseHarness([(0x100, 2, b"\x00\x00"), (0x100, 2, ready())])
    cart = chip_cart(harness, status_polling=True)

    assert cart.ChipErase() is True

    harness.assert_responses_consumed()
    assert cart.LAST_SR == 0x80
    assert harness.writes() == [
        ("write", [[0xAAA, 0xF0]], True),
        ("write", [[0x555, 0x10]], True),
        ("write", [[0x777, 0x70]], True),
        ("write", [[0xAAA, 0xF0]], True),
    ]
    assert harness.pins() == [("pin", "AUDIO"), ("pin", "WR"), ("pin", "AUDIO"), ("pin", "WR")]
    assert [event["action"] for event in harness.progress] == ["ERASE", "ERASE"]
    assert all(event["abortable"] is False for event in harness.progress)


def test_chip_erase_waits_while_busy_then_resets_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_clock(monkeypatch)
    harness = EraseHarness(
        [
            (0x100, 2, b"\x00\x00"),
            (0x100, 2, ready(0)),
            (0x100, 2, b"\x00\x00"),
            (0x100, 2, ready()),
        ],
    )
    cart = chip_cart(harness)

    assert cart.ChipErase() is True

    harness.assert_responses_consumed()
    assert cart.LAST_SR == 0x80
    assert harness.writes().count(("write", [[0xAAA, 0xF0]], True)) == 2
    assert [event["action"] for event in harness.progress] == ["ERASE", "ERASE", "ERASE"]


def test_chip_erase_short_read_fails_without_final_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_clock(monkeypatch)
    harness = EraseHarness([(0x100, 2, b"\x00\x00"), (0x100, 2, b"\x80")])
    cart = chip_cart(harness)

    assert cart.ChipErase() is False

    harness.assert_responses_consumed()
    assert cart.LAST_SR == 0
    assert harness.writes().count(("write", [[0xAAA, 0xF0]], True)) == 1
    assert [event["action"] for event in harness.progress] == ["ERASE", "ERASE"]


def test_chip_erase_timeout_reports_last_status_without_final_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_clock(monkeypatch)
    responses = [(0x100, 2, ready(0)), (0x100, 2, ready(0))] * 2
    harness = EraseHarness(responses)
    cart = chip_cart(harness, timeout=1)

    assert cart.ChipErase() is False

    harness.assert_responses_consumed()
    assert cart.LAST_SR == 0
    assert harness.writes().count(("write", [[0xAAA, 0xF0]], True)) == 1
    assert [event["action"] for event in harness.progress] == ["ERASE", "ERASE", "ERASE", "ABORT"]
    assert "0x0" in str(harness.progress[-1]["info_msg"])


def test_sector_erase_missing_command_returns_false_after_initial_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_clock(monkeypatch)
    harness = EraseHarness()
    cart = sector_cart(harness, include_command=False)

    assert cart.SectorErase(pos=0x100) is False

    assert harness.writes() == [("write", [[0xAAA, 0xF0]], True)]
    harness.assert_responses_consumed()


@pytest.mark.parametrize(
    "responses",
    [
        [(0x101, 2, ready()), (0x101, 2, b"\x80")],
        [(0x101, 2, ready()), (0x101, 2, ready()), (0x101, 2, b"\x80")],
    ],
    ids=["comparison-read", "status-read"],
)
def test_sector_erase_short_read_stages_fail_without_final_reset(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[int, int, bytes]],
) -> None:
    patch_clock(monkeypatch)
    harness = EraseHarness(responses)
    cart = sector_cart(harness)

    assert cart.SectorErase(pos=0x100) is False

    harness.assert_responses_consumed()
    assert harness.writes().count(("write", [[0xAAA, 0xF0]], True)) == 1


def test_sector_erase_resolves_symbolic_address_and_returns_fixed_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_clock(monkeypatch)
    harness = EraseHarness(
        [(0x101, 2, ready()), (0x101, 2, ready()), (0x101, 2, ready())],
    )
    cart = sector_cart(harness)

    result = cart.SectorErase(pos=0x100, buffer_pos=0x40)

    assert type(result) is int
    assert result == 0x1000
    assert cart.LAST_SR == 0x80
    assert harness.writes() == [
        ("write", [[0xAAA, 0xF0]], True),
        ("write", [[0x101, 0x30]], True),
        ("write", [[0xAAA, 0xF0]], True),
    ]
    assert harness.pins() == [("pin", "AUDIO"), ("pin", "WR")]
    assert harness.progress == []
    harness.assert_responses_consumed()


def test_sector_erase_reports_busy_progress_then_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_clock(monkeypatch)
    harness = EraseHarness(
        [
            (0x101, 2, ready()),
            (0x101, 2, ready()),
            (0x101, 2, ready(0)),
            (0x101, 2, ready()),
            (0x101, 2, ready()),
            (0x101, 2, ready()),
        ],
    )
    cart = sector_cart(harness)

    assert cart.SectorErase(pos=0x100, buffer_pos=0x40) == 0x1000

    assert cart.LAST_SR == 0x80
    assert harness.progress == [
        {
            "action": "SECTOR_ERASE",
            "sector_pos": 0x40,
            "time_start": harness.progress[0]["time_start"],
            "abortable": True,
        },
    ]
    assert harness.writes().count(("write", [[0xAAA, 0xF0]], True)) == 2
    harness.assert_responses_consumed()


def test_sector_erase_timeout_fails_without_final_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_clock(monkeypatch)
    one_poll = [(0x101, 2, ready()), (0x101, 2, ready()), (0x101, 2, ready(0))]
    harness = EraseHarness(one_poll * 100)
    cart = sector_cart(harness)

    assert cart.SectorErase(pos=0x100) is False

    assert cart.LAST_SR == 0
    assert len(harness.progress) == 99
    assert harness.writes().count(("write", [[0xAAA, 0xF0]], True)) == 1
    harness.assert_responses_consumed()


@pytest.mark.parametrize(
    ("sector_size", "expected"),
    [(None, False), (0x2000, 0x2000)],
    ids=["absent", "fixed"],
)
def test_skipped_sector_erase_uses_absent_or_fixed_sector_map(
    sector_size: object,
    expected: int | bool,
) -> None:
    harness = EraseHarness()
    cart = sector_cart(harness, sector_size=sector_size)

    result = cart.SectorErase(pos=0x400, skip=True)

    if expected is False:
        assert result is False
    else:
        assert type(result) is int
        assert result == expected
    assert harness.events == []


def test_skipped_sector_erase_advances_mixed_map_at_region_boundary() -> None:
    harness = EraseHarness()
    cart = sector_cart(harness, sector_size=[[0x1000, 2], [0x2000, 1]])

    assert cart.SectorErase(skip=True) == 0x1000
    assert cart._sector_pos == 0
    assert cart.SectorErase(skip=True) == 0x2000
    assert cart._sector_pos == 1
    assert cart.SectorErase(skip=True) == 0x2000
    assert cart._sector_pos == 1
    assert cart.CONFIG["sector_size"] == [[0x1000, 0], [0x2000, 0]]
    assert harness.events == []
