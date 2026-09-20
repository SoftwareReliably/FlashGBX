"""In-memory test doubles for hardware protocols."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from FlashGBX.Flashcart import CartReadCallback, FlashcartCallbacks, FlashcartProfile, FlashCommands, ProgressInfo


def flashcart_callbacks() -> tuple[dict[str, list[object]], FlashcartCallbacks]:
    """Return recording callbacks suitable for a base flash-cart instance."""
    calls: dict[str, list[object]] = {"read": [], "write": [], "fast": [], "progress": []}

    def cart_write(address: int, value: int, *, flashcart: bool = False, sram: bool = False) -> None:
        calls["write"].append(((address, value), {"flashcart": flashcart, "sram": sram}))

    def cart_write_fast(commands: FlashCommands, *, flashcart: bool = False) -> None:
        calls["fast"].append(((commands,), {"flashcart": flashcart}))

    def cart_read(address: int, length: int) -> bytearray:
        calls["read"].append((address, length))
        return bytearray(length)

    def record_progress(event: ProgressInfo) -> None:
        calls["progress"].append(event)

    functions: FlashcartCallbacks = {
        "cart_write_fncptr": cart_write,
        "cart_write_fast_fncptr": cart_write_fast,
        "cart_read_fncptr": cart_read,
        "cart_powercycle_fncptr": lambda: calls["progress"].append("power"),
        "progress_fncptr": record_progress,
        "set_we_pin_wr": lambda: calls["progress"].append("wr"),
        "set_we_pin_audio": lambda: calls["progress"].append("audio"),
    }
    return calls, functions


def flashcart_profile(**overrides: object) -> FlashcartProfile:
    """Build the minimal valid flash-cart profile used by unit tests."""
    value: FlashcartProfile = {
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


def response_reader(responses: Iterator[bytearray]) -> CartReadCallback:
    """Return a cartridge reader that consumes a response iterator."""

    def read(address: int, length: int) -> bytearray:
        del address, length
        return next(responses)

    return read


def filled_reader(value: int = 0) -> CartReadCallback:
    """Return a cartridge reader filled with one byte value."""

    def read(address: int, length: int) -> bytearray:
        del address
        return bytearray([value] * length)

    return read


class MockCameraCartridge:
    """Read-only Pocket Camera model used behind mocked device methods."""

    def __init__(self, header: bytes | bytearray, calibration: bytes) -> None:
        self.header = bytearray(header)
        self.calibration = bytearray(calibration)
        self.rom_reads: list[tuple[int, int]] = []
        self.ram_reads: list[tuple[int, int]] = []
        self.mapper_writes: list[tuple[int, int]] = []

    def read_rom(self, address: int, length: int) -> bytearray:
        self.rom_reads.append((address, length))
        if (address, length) != (0, 0x180):
            msg = f"Unexpected mock camera ROM read: {address:#x}, {length:#x}"
            raise AssertionError(msg)
        return self.header[:]

    def read_ram(self, address: int, length: int) -> bytearray:
        self.ram_reads.append((address, length))
        if address not in (0xFF2, 0x1FF2) or length != len(self.calibration):
            msg = f"Unexpected mock camera RAM read: {address:#x}, {length:#x}"
            raise AssertionError(msg)
        return self.calibration[:]

    def write_mapper(self, address: int, value: int, *_args: object, **_kwargs: object) -> int:
        self.mapper_writes.append((address, value))
        return 1


class MockSerial:
    """Small pyserial stand-in with responses queued after each write.

    Responses are released by ``write`` rather than preloaded so calls to
    ``reset_input_buffer`` behave like real hardware initialization.
    """

    def __init__(
        self,
        port: str | None = None,
        baudrate: int | None = None,
        timeout: float | None = None,
        exclusive: bool | None = None,
        *,
        responses: Iterable[bytes] = (),
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.exclusive = exclusive
        self.is_open = True
        self.writes: list[bytes] = []
        self._responses = list(responses)
        self._read_buffer = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self._read_buffer)

    # Keep pyserial's camelCase compatibility method.
    def isOpen(self) -> bool:
        return self.is_open

    def close(self) -> None:
        self.is_open = False

    def flush(self) -> None:
        pass

    def read(self, count: int = 1) -> bytes:
        result = bytes(self._read_buffer[:count])
        del self._read_buffer[:count]
        return result

    def write(self, data: bytes | bytearray) -> int:
        payload = bytes(data)
        self.writes.append(payload)
        if self._responses:
            self._read_buffer.extend(self._responses.pop(0))
        return len(payload)

    def reset_input_buffer(self) -> None:
        self._read_buffer.clear()

    def reset_output_buffer(self) -> None:
        pass


class EchoSerial(MockSerial):
    """Serial double that echoes each write, as the firmware bootloader does."""

    def write(self, data: bytes | bytearray) -> int:
        written = super().write(data)
        self._read_buffer.extend(data)
        return written
