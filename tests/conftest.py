"""Shared fixtures for hardware-free FlashGBX tests."""

from __future__ import annotations

from typing import Any

import pytest
import serial
import serial.tools.list_ports


@pytest.fixture(autouse=True)
def prevent_real_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if a test tries to open serial hardware accidentally."""

    def unexpected_serial_access(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        msg = "A test tried to open real serial hardware; inject MockSerial instead"
        raise AssertionError(msg)

    monkeypatch.setattr(serial, "Serial", unexpected_serial_access)
    monkeypatch.setattr(serial.tools.list_ports, "comports", list)


@pytest.fixture
def pokemon_red_header() -> bytearray:
    """Return a synthetic, valid DMG header shaped like Pokemon Red.

    This is generated metadata only; it contains no game code or ROM assets.
    """
    nintendo_logo = bytes.fromhex(
        "CE ED 66 66 CC 0D 00 0B 03 73 00 83 00 0C 00 0D "
        "00 08 11 1F 88 89 00 0E DC CC 6E E6 DD DD D9 99 "
        "BB BB 67 63 6E 0E EC CC DD DC 99 9F BB B9 33 3E",
    )
    header = bytearray(0x180)
    header[0x104:0x134] = nintendo_logo
    header[0x134:0x144] = b"POKEMON RED".ljust(16, b"\x00")
    header[0x143] = 0x00  # Original Game Boy title
    header[0x146] = 0x03  # Super Game Boy enhanced
    header[0x147] = 0x13  # MBC3 + RAM + battery
    header[0x148] = 0x05  # 1 MiB ROM
    header[0x149] = 0x03  # 32 KiB RAM
    header[0x14A] = 0x01  # Non-Japanese destination
    header[0x14B] = 0x01  # Nintendo licensee code
    header[0x14C] = 0x00

    checksum = 0
    for value in header[0x134:0x14D]:
        checksum = (checksum - value - 1) & 0xFF
    header[0x14D] = checksum

    global_checksum = sum(header) & 0xFFFF
    header[0x14E:0x150] = global_checksum.to_bytes(2, byteorder="big")
    return header


@pytest.fixture
def game_boy_camera_header(pokemon_red_header: bytearray) -> bytearray:
    """Return generated metadata matching the attached USA/EUR Pocket Camera.

    Only public cartridge-header fields are retained from the hardware probe;
    no ROM payload or camera save/photo data is part of this fixture.
    """
    header = bytearray(pokemon_red_header)
    header[0x134:0x144] = b"GAMEBOYCAMERA".ljust(16, b"\x00")
    header[0x144:0x146] = b"01"  # Nintendo new licensee code
    header[0x146] = 0x03  # Super Game Boy enhanced
    header[0x147] = 0xFC  # MAC-GBD + SRAM + battery
    header[0x148] = 0x05  # 1 MiB ROM
    header[0x149] = 0x04  # Camera's banked SRAM declaration
    header[0x14A] = 0x01  # Non-Japanese destination
    header[0x14B] = 0x33  # Use the new licensee code
    header[0x14C] = 0x00

    checksum = 0
    for value in header[0x134:0x14D]:
        checksum = (checksum - value - 1) & 0xFF
    header[0x14D] = checksum
    # Preserve the checksum declared by the observed cartridge. It describes
    # the full ROM, so it intentionally cannot validate against this header-only fixture.
    header[0x14E:0x150] = bytes.fromhex("BAF9")
    return header
