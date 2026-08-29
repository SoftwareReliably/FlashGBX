"""Tests for flash-cart profile accessors and CFI parsing helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import FlashGBX.Flashcart as flashcart_module  # noqa: N813
from FlashGBX.Flashcart import CFI, Flashcart, _profile_flash_ids, empty_flashcarts_map, has_3v_compatible_profile

if TYPE_CHECKING:
    import pytest


def callbacks() -> tuple[dict[str, list[Any]], dict[str, Any]]:
    calls: dict[str, list[Any]] = {"read": [], "write": [], "fast": [], "progress": []}

    def record_progress(event: Any) -> None:
        calls["progress"].append(event)

    functions: dict[str, Any] = {
        "cart_write_fncptr": lambda *args, **kwargs: calls["write"].append((args, kwargs)),
        "cart_write_fast_fncptr": lambda *args, **kwargs: calls["fast"].append((args, kwargs)),
        "cart_read_fncptr": lambda *args: (calls["read"].append(args) or bytearray(args[1])),
        "cart_powercycle_fncptr": lambda: calls["progress"].append("power"),
        "progress_fncptr": record_progress,
        "set_we_pin_wr": lambda: calls["progress"].append("wr"),
        "set_we_pin_audio": lambda: calls["progress"].append("audio"),
    }
    return calls, functions


def profile(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "DMG",
        "names": ["Test cart"],
        "flash_ids": [[0x12, 0x34]],
        "voltage": 5,
        "commands": {"single_write": [[0, 0]]},
        "command_set": "AMD",
        "write_pin": "WR",
        "flash_size": 0x10000,
        "buffer_size": 4,
    }
    value.update(overrides)
    return value


def test_flashcart_accessors_and_write_routing() -> None:
    calls, functions = callbacks()
    cart = Flashcart(profile(), functions)  # type: ignore[arg-type]

    assert cart.CONFIG["_command_set"] == "AMD"
    assert cart.GetCommandSetType() == "AMD"
    assert cart.GetName() == "Test cart"
    assert cart.GetFlashID() == [0x12, 0x34]
    assert cart.GetVoltage() == 5
    assert cart.GetMBC() is False
    assert cart.GetFlashSize() == 0x10000
    assert cart.GetBufferSize() == 4
    assert cart.GetCommands("missing") == []
    assert cart.SupportsSingleWrite() is True
    assert cart.SupportsBufferWrite() is False
    assert cart.WEisWR() is True
    assert cart.WEisAUDIO() is False
    assert cart.WEisWR_RESET() is False

    cart.CartRead(0x10)
    cart.CartWrite([[1, 2], [3, 4]])
    cart.CartWrite([[5, 6]], fast_write=False, sram=True)

    assert calls["read"] == [(0x10, 1)]
    assert calls["fast"] == [(([[1, 2], [3, 4]],), {"flashcart": True})]
    assert calls["write"] == [((5, 6), {"flashcart": False, "sram": True})]


def test_flashcart_sector_helpers_and_profile_matching() -> None:
    _calls, functions = callbacks()
    cart = Flashcart(
        profile(sector_size=[[0x1000, 2], [0x2000, 1]], flash_commands_on_bank_1=True, pulse_reset_after_write=True),
        functions,  # type: ignore[arg-type]
    )

    assert cart.FlashCommandsOnBank1() is True
    assert cart.PulseResetAfterWrite() is True
    assert cart.HasRTC() is False
    assert cart.HasDoubleDie() is False
    assert cart.GetSmallestSectorSize() == 0x1000
    assert cart.GetSectorOffsets() == [[0, 0x1000], [0x1000, 0x1000], [0x2000, 0x2000]]
    assert cart.GetSectorMap() == [[0x1000, 2], [0x2000, 1]]
    cart.SetFlashSize(0x20000)
    assert cart.GetFlashSize() == 0x20000
    assert _profile_flash_ids({"flash_ids": [[1, 2], [3, "bad"], "bad"]}) == {(1, 2)}
    assert _profile_flash_ids({}) == set()

    carts = [
        {"type": "A", "voltage": 5, "flash_ids": [[1, 2]]},
        {"type": "A", "voltage": 3.3, "flash_ids": [[1, 2]]},
    ]
    assert has_3v_compatible_profile(carts, 0) is True
    assert has_3v_compatible_profile(carts, 1) is False
    assert has_3v_compatible_profile(carts, None) is False
    assert empty_flashcarts_map() == {"DMG": {}, "AGB": {}}


def test_cfi_parser_accepts_valid_data_and_rejects_invalid_buffers() -> None:
    buffer = bytearray(0x400)
    buffer[0:8] = b"FLASHID!"
    buffer[0x20] = ord("Q")
    buffer[0x22] = ord("R")
    buffer[0x24] = ord("Y")
    buffer[0x36] = 0x33
    buffer[0x38] = 0x55
    buffer[0x3E] = 2
    buffer[0x40] = 3
    buffer[0x42] = 4
    buffer[0x44] = 5
    buffer[0x46] = 1
    buffer[0x48] = 1
    buffer[0x4A] = 1
    buffer[0x4C] = 1
    buffer[0x4E] = 20
    buffer[0x54] = 2
    buffer[0x58] = 1
    buffer[0x5E] = 4
    buffer[0] = ord("P")
    buffer[2] = ord("R")
    buffer[4] = ord("I")
    buffer[0x1E] = 2

    info = CFI().Parse(buffer)

    assert info is not False
    assert info["magic"] == "QRY"
    assert info["vdd_min"] == 3.3
    assert info["single_write"] is True
    assert info["buffer_size"] == 4
    assert info["device_size"] == 2**20
    assert info["tb_boot_sector"] == "As shown (0x02)"
    assert info["erase_sector_blocks"] == [[1024, 1, 1024]]
    assert "Device size" in info["info"]
    assert CFI().Parse(False) is False
    assert CFI().Parse(bytearray(0x3FF)) is False
    buffer[0x20] = ord("N")
    assert CFI().Parse(buffer) is False


def test_flashcart_reset_erase_and_bank_selection_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(flashcart_module.time, "sleep", lambda _seconds: None)
    calls, functions = callbacks()
    cart = Flashcart(
        profile(
            commands={
                "reset": [[0xAA, 0xF0]],
                "chip_erase": [[0, 0x10]],
                "chip_erase_wait_for": [[None, 0, 0]],
                "sector_erase": [["SA+2", 0x30]],
                "sector_erase_wait_for": [[None, 0, 0]],
            },
            sector_size=[[0x1000, 2]],
            flash_bank_select_type=1,
            chip_erase_timeout=1,
        ),
        functions,  # type: ignore[arg-type]
    )

    assert cart.Reset() is True
    assert cart.ChipErase() is True
    assert cart.SectorErase(pos=0x100, skip=True) == 0x1000
    assert cart.SectorErase(pos=0x100, skip=True) == 0x1000
    assert cart.SelectBankROM(0x12) is True
    assert cart.HasBanks() is True
    assert calls["write"]

    cart.CONFIG["flash_bank_select_type"] = 2
    calls["write"].clear()
    assert cart.SelectBankROM(5) is True
    assert len(calls["write"]) > 0
    cart.CONFIG.pop("flash_bank_select_type")
    assert cart.SelectBankROM(0) is False


def test_flashcart_cfi_and_flash_id_failure_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flashcart_module.time, "sleep", lambda _seconds: None)
    _calls, functions = callbacks()
    cart = Flashcart(
        profile(commands={"buffer_write": [], "single_write": [], "read_identifier": [[0, 0x90]]}),
        functions,  # type: ignore[arg-type]
    )
    cart.CONFIG.pop("buffer_size")
    cart.ReadCFI = lambda: False  # type: ignore[method-assign]
    assert cart.GetBufferSize() is False
    assert "buffer_write" not in cart.CONFIG["commands"]
    assert cart.VerifyFlashID() == (False, [0, 0])

    responses = iter([bytearray(b"AB"), bytearray(b"AB"), bytearray([0x12, 0x34])])
    cart = Flashcart(profile(commands={"read_identifier": [[0, 0x90]]}), functions)  # type: ignore[arg-type]
    cart._cart_read = lambda _address, _length: next(responses)
    assert cart.VerifyFlashID() == (True, [0x12, 0x34])
