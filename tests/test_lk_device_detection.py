"""Tests for cartridge detection using generated, hardware-free data."""

from __future__ import annotations

import pytest

from FlashGBX.CartridgeTypes import DmgSaveTypes
from FlashGBX.hw_GBxCartRW import GbxDevice


@pytest.mark.parametrize(
    ("save_size", "mbc", "expected"),
    [
        (0, None, (0, 0)),
        (0x20, None, (0x20, 0x103)),
        (0x21, None, (0x21, None)),
        (0x100, None, (0x100, 0x101)),
        (0x200, None, (0x200, 0x100)),
        (0x800, None, (0x800, 0x01)),
        (0x2000, None, (0x2000, 0x02)),
        (0x8000, None, (0x8000, 0x03)),
        (0x10000, None, (0x10000, 0x05)),
        (0x1234, None, (0x1234, None)),
    ],
)
def test_detect_dmg_save_type_small_and_supported_sizes(
    save_size: int,
    mbc: int | None,
    expected: tuple[int, int | None],
) -> None:
    device = GbxDevice()
    data = bytearray(0x20000)
    original = bytes(data)
    device.INFO["data"] = data

    result = device._DetectDmgSaveType(save_size, mbc)

    assert result == expected
    assert bytes(device.INFO["data"]) == original


@pytest.mark.parametrize(
    ("save_size", "expected_type"),
    [(256, 0x101), (512, 0x102), (0x2000, 0x02)],
)
def test_detect_dmg_mbc7_size_overrides_and_generic_mapping(
    save_size: int,
    expected_type: int,
) -> None:
    device = GbxDevice()
    data = bytearray(0x20000)
    original = bytes(data)
    device.INFO["data"] = data

    assert device._DetectDmgSaveType(save_size, 0x22) == (save_size, expected_type)
    assert bytes(device.INFO["data"]) == original


def make_dmg_128k_candidate(
    *,
    mirrored_halves: bool = False,
    mismatched_probe: int | None = None,
) -> bytearray:
    """Build bank patterns that lead the real classifier to each 128 KiB test stage."""
    data = bytearray([0x11] * 0x8000)
    second_bank = bytearray(data if mirrored_halves else [0x22] * 0x8000)
    data.extend(second_bank)
    if mismatched_probe is not None:
        data[mismatched_probe : mismatched_probe + 3] = b"\x31\x32\x33"
        if mirrored_halves:
            data[mismatched_probe - 0x8000 : mismatched_probe - 0x8000 + 3] = b"\x31\x32\x33"
    data.extend(bytearray([0x44] * 0x10000))
    return data


@pytest.mark.parametrize(
    ("case", "mirrored_halves", "first_bank_mismatch", "expected"),
    [
        ("distinct-homogeneous-halves", False, None, (0x8000, 0x03)),
        ("mirrored-halves-override-probe", True, 0x8000, (0x8000, 0x03)),
        ("early-second-bank-mismatch", False, 0x8000, (0x10000, 0x05)),
        ("strided-second-bank-mismatch", False, 0xA080, (0x10000, 0x05)),
        ("late-second-bank-mismatch", False, 0xFFC0, (0x10000, 0x05)),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_detect_dmg_large_save_classification_preserves_probe_data(
    case: str,
    mirrored_halves: bool,
    first_bank_mismatch: int | None,
    expected: tuple[int, int | None],
) -> None:
    del case
    data = make_dmg_128k_candidate(
        mirrored_halves=mirrored_halves,
        mismatched_probe=first_bank_mismatch,
    )
    original = bytes(data)
    device = GbxDevice()
    device.INFO["data"] = data

    result = device._DetectDmgSaveType(0x20000, 0x13)

    assert result == expected
    assert bytes(device.INFO["data"]) == original


@pytest.mark.parametrize("mismatch", [0x1A000, 0x1FFC0], ids=["early", "late"])
def test_detect_dmg_upper_region_mismatch_retains_128k(
    mismatch: int,
) -> None:
    data = make_dmg_128k_candidate(mismatched_probe=0x8000)
    data[mismatch : mismatch + 3] = b"\x71\x72\x73"
    original = bytes(data)
    device = GbxDevice()
    device.INFO["data"] = data

    result = device._DetectDmgSaveType(0x20000, 0x13)

    assert result == (0x20000, DmgSaveTypes(size=0x20000).GetMbc())
    assert bytes(device.INFO["data"]) == original
