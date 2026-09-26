# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .i18n import __

if TYPE_CHECKING:
    from collections.abc import Callable

    from .LK_Device import DeviceReadResult, LK_Device


class InteractiveConsole:
    def __init__(
        self,
        conn: LK_Device,
        on_output: Callable[[str], object],
        on_error: Callable[[str], object] | None = None,
    ) -> None:
        self.conn: LK_Device = conn
        self.mode: Literal["DMG", "AGB"] | None = conn.GetMode()
        self.on_output: Callable[[str], object] = on_output
        self.on_error: Callable[[str], object] = on_error if on_error is not None else on_output
        self.last_read_data = None

    def get_help_lines(self) -> list[Any]:
        lines = []
        lines.append(__("Interactive Console") + " - " + __("Commands:"))
        lines.append("  r <addr> <size>               " + __("Read from ROM region"))
        lines.append("  s <filepath>                  " + __("Save last read data to file"))
        lines.append("  w <addr> <value>              " + __("Write to ROM region (e.g. mapper registers)"))
        if self.mode == "AGB":
            lines.append("  rs <addr> <size>              " + __("Read from SRAM or FLASH save region"))
            lines.append("  ws <addr> <value>             " + __("Write to SRAM or FLASH save region"))
            lines.append("  wf <addr> <value>             " + __("Send commands to FLASH save chip"))
            lines.append("  re <4|64> <addr> <size>       " + __("Read from EEPROM save region"))
            lines.append("  we <4|64> <addr> <data>       " + __("Write to EEPROM save region"))
        if self.conn.CanPowerCycleCart():
            lines.append("  on                            " + __("Cartridge Power On"))
            lines.append("  off                           " + __("Cartridge Power Off"))
        lines.append("  h                             " + __("Show this help"))
        lines.append("  q                             " + __("Quit interactive console"))
        lines.append("")
        lines.append("  " + __("Multiple commands can be entered on one line, separated by commas."))
        lines.append("  " + __("All addresses, sizes and values are hexadecimal."))
        lines.append("")
        return lines

    def print_help(self) -> None:
        for line in self.get_help_lines():
            self.on_output(line)

    def hexdump(self, base_addr: int, data: int | bytearray) -> None:
        if isinstance(data, int):
            data = bytearray([data])
        for offset in range(0, len(data), 16):
            chunk: bytearray = data[offset : offset + 16]
            hex_part: str = " ".join(f"{b:02x}" for b in chunk)
            ascii_part: str = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
            self.on_output(f"{base_addr + offset:08x}: {hex_part:<47}  {ascii_part:s}")

    def execute_line(self, line: str) -> bool:
        cmds: list[str] = [c.strip() for c in line.split(",") if c.strip()]
        return all(self.execute_command(cmdline) for cmdline in cmds)

    def execute_command(self, cmdline: str) -> bool:
        try:
            return self._execute_command_inner(cmdline)
        except Exception:
            return False

    def _execute_command_inner(self, cmdline: str) -> bool:
        try:
            parts: list[str] = shlex.split(cmdline)
        except ValueError:
            self.on_output(__("Invalid command syntax."))
            return True
        if not parts:
            return True
        command: str = parts[0].lower()

        if command == "q":
            return False

        if command == "h":
            self.print_help()
            return True

        handlers: dict[str, Callable[[list[str]], bool]] = {
            "w": self._execute_rom_write,
            "r": self._execute_rom_read,
            "s": self._execute_save,
            "on": self._execute_power_on,
            "off": self._execute_power_off,
        }
        if self.mode == "AGB":
            handlers.update(
                {
                    "rs": self._execute_save_read,
                    "ws": self._execute_save_write,
                    "wf": self._execute_flash_write,
                    "re": self._execute_eeprom_read,
                    "we": self._execute_eeprom_write,
                },
            )

        handler = handlers.get(command)
        if handler is not None and handler(parts):
            return True

        self.on_output(__("Unknown command. Type “h” for help."))
        return True

    def _execute_rom_write(self, parts: list[str]) -> bool:
        if len(parts) != 3:
            return False
        try:
            address = int(parts[1], 16)
            value: int = int(parts[2], 2) if re.fullmatch(r"[01]{8}|[01]{16}", parts[2]) else int(parts[2], 16)
        except ValueError:
            self.on_output(__("Invalid input. Use hexadecimal or 8/16-bit binary for the value."))
            return True
        self.conn._cart_write(
            address,
            value,
            sram=bool(self.mode == "DMG" and 40960 <= address < 49152),
        )
        self.on_output(__("OK"))
        return True

    def _execute_rom_read(self, parts: list[str]) -> bool:
        if len(parts) != 3:
            return False
        try:
            address = int(parts[1], 16)
            size = int(parts[2], 16)
        except ValueError:
            self.on_output(__("Invalid hexadecimal input."))
            return True
        if size == 0:
            return True
        raw: int | bytearray | Literal[False] = self.conn._cart_read(address, size)
        self._show_read_result(address, size, raw)
        return True

    def _execute_save(self, parts: list[str]) -> bool:
        if len(parts) != 2:
            return False
        if self.last_read_data is None:
            self.on_output(__("No data available. Read data first with “r”, “rs” or “re”."))
            return True
        filepath: Path = Path(parts[1]).resolve()
        if filepath.is_dir():
            self.on_output(__("Invalid file path. Path is a directory."))
            return True
        if not filepath.parent.exists():
            self.on_output(__("Invalid file path. Directory does not exist."))
            return True
        backup_path: Path = filepath.with_name(filepath.name + ".bak")
        try:
            if filepath.exists():
                backup_path.unlink(missing_ok=True)
                filepath.replace(backup_path)
            with filepath.open("wb") as fh:
                fh.write(self.last_read_data)
        except OSError as e:
            self.on_error(__("Failed to save file: {error}", error=str(e)))
            return True
        self.on_output(__("Saved to {filepath}", filepath=str(filepath)))
        return True

    def _execute_save_read(self, parts: list[str]) -> bool:
        if len(parts) != 3:
            return False
        try:
            address = int(parts[1], 16)
            size = int(parts[2], 16)
        except ValueError:
            self.on_output(__("Invalid hexadecimal input."))
            return True
        if size == 0:
            return True
        raw: int | bytearray | Literal[False] = self.conn._cart_read(address, size, agb_save_flash=True)
        self._show_read_result(address, size, raw)
        return True

    def _show_read_result(self, address: int, size: int, raw: DeviceReadResult) -> None:
        if raw is False or (isinstance(raw, bytearray) and len(raw) == 0):
            self.on_error(__("ERROR"))
            return
        data = bytearray(raw)[:size]
        self.last_read_data = data
        self.hexdump(address, data)

    def _execute_save_write(self, parts: list[str]) -> bool:
        if len(parts) != 3:
            return False
        try:
            address = int(parts[1], 16)
            value = int(parts[2], 2) if re.fullmatch(r"[01]{8}", parts[2]) else int(parts[2], 16)
        except ValueError:
            self.on_output(__("Invalid input. Use hexadecimal or 8-bit binary for the value."))
            return True
        self.conn._cart_write(address, value, sram=True)
        self.on_output(__("OK"))
        return True

    def _execute_flash_write(self, parts: list[str]) -> bool:
        if len(parts) != 3:
            return False
        try:
            address = int(parts[1], 16)
            value = int(parts[2], 16)
        except ValueError:
            self.on_output(__("Invalid hexadecimal input."))
            return True
        self.conn._cart_write_flash([[address, value]])
        self.on_output(__("OK"))
        return True

    def _execute_eeprom_read(self, parts: list[str]) -> bool:
        if len(parts) != 4:
            return False
        if parts[1] not in ("4", "64"):
            self.on_output(__("EEPROM type must be 4 or 64."))
            return True
        eeprom_type: Literal[2, 1] = 2 if parts[1] == "64" else 1
        try:
            address = int(parts[2], 16)
            size = int(parts[3], 16)
        except ValueError:
            self.on_output(__("Invalid hexadecimal input."))
            return True
        if size % 8 != 0 or size == 0:
            self.on_output(__("EEPROM read requires size to be a multiple of 8 bytes."))
            return True
        self.conn._set_fw_variable("TRANSFER_SIZE", size)
        self.conn._set_fw_variable("ADDRESS", address)
        cmd = bytearray([self.conn.DEVICE_CMD["AGB_CART_READ_EEPROM"], eeprom_type])
        self.conn._write(cmd)
        eeprom_data: int | bytearray | Literal[False] = self.conn._read(size)
        if not isinstance(eeprom_data, bytearray) or len(eeprom_data) == 0:
            self.on_error(__("ERROR"))
        else:
            self.last_read_data = bytearray(eeprom_data)
            self.hexdump(address, eeprom_data)
        return True

    def _execute_eeprom_write(self, parts: list[str]) -> bool:
        if len(parts) != 4:
            return False
        if parts[1] not in ("4", "64"):
            self.on_output(__("EEPROM type must be 4 or 64."))
            return True
        eeprom_type: Literal[2, 1] = 2 if parts[1] == "64" else 1
        try:
            address = int(parts[2], 16)
            data: bytearray = bytearray.fromhex(parts[3])
        except ValueError:
            self.on_output(__("Invalid input."))
            return True
        if len(data) == 0 or len(data) % 8 != 0:
            self.on_output(__("EEPROM write requires data length to be a multiple of 8 bytes."))
            return True
        self.conn._set_fw_variable("TRANSFER_SIZE", len(data))
        self.conn._set_fw_variable("ADDRESS", address)
        cmd = bytearray([self.conn.DEVICE_CMD["AGB_CART_WRITE_EEPROM"], eeprom_type])
        self.conn._write(cmd)
        ack: int | Literal[False] | None = self.conn._write(data, wait=True)
        if ack is False:
            self.on_error(__("ERROR"))
        else:
            self.on_output(__("OK"))
        return True

    def _execute_power_on(self, _parts: list[str]) -> bool:
        return self._execute_power_command(self.conn.CartPowerOn)

    def _execute_power_off(self, _parts: list[str]) -> bool:
        return self._execute_power_command(self.conn.CartPowerOff)

    def _execute_power_command(self, command: Callable[[], object]) -> bool:
        if not self.conn.CanPowerCycleCart():
            self.on_output(__("This device does not support cartridge power control."))
            return True
        try:
            command()
            self.on_output(__("OK"))
        except Exception as e:
            self.on_error(__("ERROR") + ": " + str(e))
        return True
