"""In-memory test doubles for hardware protocols."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


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
