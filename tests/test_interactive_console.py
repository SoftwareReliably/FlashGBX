"""Tests for interactive console command parsing with a fake device."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from FlashGBX.InteractiveConsole import InteractiveConsole

if TYPE_CHECKING:
    from pathlib import Path


class ConsoleDevice:
    DEVICE_CMD: ClassVar[dict[str, int]] = {"AGB_CART_READ_EEPROM": 0xC5, "AGB_CART_WRITE_EEPROM": 0xC6}

    def __init__(self, mode: str = "AGB") -> None:
        self.mode = mode
        self.writes: list[tuple[object, dict[str, object]]] = []
        self.reads: list[tuple[object, ...]] = []
        self.variables: list[tuple[str, int]] = []

    def GetMode(self) -> str:
        return self.mode

    def CanPowerCycleCart(self) -> bool:
        return True

    def _cart_read(self, *args: object, **_kwargs: object) -> bytearray:
        self.reads.append(args)
        return bytearray(range(int(args[1])))

    def _cart_write(self, *args: object, **kwargs: object) -> None:
        self.writes.append((args, kwargs))

    def _cart_write_flash(self, commands: object) -> None:
        self.writes.append((commands, {}))

    def _set_fw_variable(self, key: str, value: int) -> None:
        self.variables.append((key, value))

    def _write(self, data: object, wait: bool = False) -> int:
        self.writes.append((data, {"wait": wait}))
        return 1

    def _read(self, count: int) -> bytearray:
        return bytearray([0xAB] * count)

    def CartPowerOn(self) -> None:
        self.writes.append(("on", {}))

    def CartPowerOff(self) -> None:
        self.writes.append(("off", {}))


def test_console_help_hexdump_and_multi_command_execution() -> None:
    device = ConsoleDevice()
    output: list[str] = []
    errors: list[str] = []
    console = InteractiveConsole(device, output.append, errors.append)

    assert len(console.get_help_lines()) > 10
    console.hexdump(0x100, 0x41)
    assert output[-1].startswith("00000100:")
    assert console.execute_line("w 4000 10101010, r 100 2") is True
    assert device.writes[0][0] == (0x4000, 0xAA)
    assert device.writes[0][1]["sram"] is False
    assert console.last_read_data == bytearray([0, 1])
    assert errors == []


def test_console_save_and_eeprom_commands(tmp_path: Path) -> None:
    device = ConsoleDevice()
    output: list[str] = []
    console = InteractiveConsole(device, output.append, output.append)
    console.last_read_data = bytearray(b"saved")
    destination = tmp_path / "out.bin"

    assert console.execute_command(f"s {destination}") is True
    assert destination.read_bytes() == b"saved"
    assert console.execute_command("re 64 20 8") is True
    assert console.execute_command("we 4 20 0102030405060708") is True
    assert ("TRANSFER_SIZE", 8) in device.variables
    assert ("ADDRESS", 0x20) in device.variables
    assert "OK" in output


def test_console_reports_invalid_and_power_commands() -> None:
    device = ConsoleDevice()
    output: list[str] = []
    console = InteractiveConsole(device, output.append, output.append)

    assert console.execute_line("q, w 1 2") is False
    assert console.execute_command("re 4 20 3") is True
    assert console.execute_command("unknown") is True
    assert any("EEPROM read" in text for text in output)
    assert any("Unknown command" in text for text in output)
    assert console.execute_command("on") is True
    assert console.execute_command("off") is True
    assert ("on", {}) in device.writes
    assert ("off", {}) in device.writes


def test_console_handles_save_commands_and_agb_flash_paths(tmp_path: Path) -> None:
    device = ConsoleDevice()
    output: list[str] = []
    errors: list[str] = []
    console = InteractiveConsole(device, output.append, errors.append)

    assert console.execute_command("rs 20 2") is True
    assert device.reads[-1] == (0x20, 2)
    assert console.execute_command("ws 30 10101010") is True
    assert console.execute_command("wf 40 ff") is True
    assert ((0x30, 0xAA), {"sram": True}) in device.writes
    assert ([[0x40, 0xFF]], {}) in device.writes

    assert console.execute_command("rs 20 nope") is True
    assert console.execute_command("ws 30 nope") is True
    assert console.execute_command("wf 40 nope") is True
    assert console.execute_command("we 4 20 nope") is True
    assert any("Invalid" in text for text in output)

    console.last_read_data = None
    assert console.execute_command("s output.bin") is True
    console.last_read_data = bytearray(b"saved")
    assert console.execute_command(f"s {tmp_path}") is True
    assert console.execute_command(f"s {tmp_path / 'missing' / 'output.bin'}") is True
    assert any("No data available" in text for text in output)
    assert any("directory" in text.lower() for text in output)
    assert any("does not exist" in text for text in output)


def test_console_handles_read_errors_eeprom_errors_and_power_limits() -> None:
    device = ConsoleDevice()
    output: list[str] = []
    errors: list[str] = []
    console = InteractiveConsole(device, output.append, errors.append)

    device._cart_read = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    assert console.execute_command("r 10 2") is True
    assert console.execute_command("rs 10 2") is True
    device._read = lambda _count: False  # type: ignore[method-assign]
    assert console.execute_command("re 4 20 8") is True

    def reject_eeprom_write(_data: object, wait: bool = False) -> int | bool:
        return False if wait else 1

    device._write = reject_eeprom_write  # type: ignore[method-assign]
    assert console.execute_command("we 4 20 0102030405060708") is True
    assert errors.count("ERROR") == 4

    device.CanPowerCycleCart = lambda: False  # type: ignore[method-assign]
    help_lines = console.get_help_lines()
    assert not any(line.strip().startswith("on ") for line in help_lines)
    assert any(line.strip().startswith("h ") for line in help_lines)
    assert console.execute_command("on") is True
    assert console.execute_command("off") is True
    assert any("does not support" in text for text in output)


def test_console_dmg_sram_writes_and_syntax_failures() -> None:
    device = ConsoleDevice("DMG")
    output: list[str] = []
    console = InteractiveConsole(device, output.append)
    device.CanPowerCycleCart = lambda: False  # type: ignore[method-assign]
    help_lines = console.get_help_lines()
    assert not any(line.strip().startswith("rs ") for line in help_lines)
    assert not any(line.strip().startswith("on ") for line in help_lines)

    assert console.execute_command("w a000 ff") is True
    assert device.writes[-1][1]["sram"] is True
    assert console.execute_command("w a000 nope") is True
    assert console.execute_command("r nope 2") is True
    assert console.execute_command("w '") is True
    assert console.execute_command("   ") is True
    assert any("Invalid command syntax" in text for text in output)
