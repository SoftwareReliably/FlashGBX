"""Tests for cartridge detection using generated, hardware-free data."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

import pytest

from FlashGBX.CartridgeTypes import AgbSaveTypes, DmgSaveTypes
from FlashGBX.hw_GBxCartRW import GbxDevice

if TYPE_CHECKING:
    from collections.abc import Callable

from tests.fakes import flashcart_profile

STATE_OFFSETS = (0x400000 - 0x40000, 0x800000 - 0x40000, 0x1000000 - 0x40000, 0x2000000 - 0x40000)
STATE_IDS = (0x57A731D7, 0x57A731D8, 0x57A731D9)


@pytest.mark.parametrize("game_code", ["GMBC", "PNES"])
@pytest.mark.parametrize("state_id", STATE_IDS)
@pytest.mark.parametrize("match_index", range(len(STATE_OFFSETS)))
def test_batteryless_goomba_probe_stops_at_first_matching_state(
    monkeypatch: pytest.MonkeyPatch,
    game_code: str,
    state_id: int,
    match_index: int,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    header = bytearray(0x180)
    header[0xAC:0xB0] = game_code.encode("ascii")
    reads: list[tuple[int, int]] = []

    def read_rom(address: int, length: int, **_kwargs: object) -> bytearray:
        reads.append((address, length))
        if address == 0:
            return header
        value = state_id if address == STATE_OFFSETS[match_index] else 0
        return bytearray(struct.pack("<I", value))

    monkeypatch.setattr(device, "ReadROM", read_rom)

    result = device.CheckBatterylessSRAM()

    assert result == {"bl_offset": STATE_OFFSETS[match_index], "bl_size": 0x10000}
    assert reads == [(0, 0x180), *((offset, 4) for offset in STATE_OFFSETS[: match_index + 1])]


@pytest.mark.parametrize("game_code", ["GMBC", "PNES"])
def test_batteryless_goomba_probe_returns_false_after_all_misses(
    monkeypatch: pytest.MonkeyPatch,
    game_code: str,
) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    header = bytearray(0x180)
    header[0xAC:0xB0] = game_code.encode("ascii")
    reads: list[tuple[int, int]] = []

    def read_rom(address: int, length: int, **_kwargs: object) -> bytearray:
        reads.append((address, length))
        return header if address == 0 else bytearray(4)

    monkeypatch.setattr(device, "ReadROM", read_rom)

    assert device.CheckBatterylessSRAM() is False
    assert reads == [(0, 0x180), *((offset, 4) for offset in STATE_OFFSETS)]


def test_batteryless_probe_skips_rom_reads_outside_agb_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    monkeypatch.setattr(device, "ReadROM", lambda *_args, **_kwargs: pytest.fail("unexpected ROM read"))

    assert device.CheckBatterylessSRAM() is False


def test_batteryless_goomba_probe_propagates_short_state_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    device.MODE = "AGB"
    header = bytearray(0x180)
    header[0xAC:0xB0] = b"GMBC"

    def read_rom(address: int, _length: int, **_kwargs: object) -> bytearray:
        return header if address == 0 else bytearray([0])

    monkeypatch.setattr(device, "ReadROM", read_rom)

    with pytest.raises(struct.error):
        device.CheckBatterylessSRAM()


def make_identifier_profile(
    name: str,
    flash_ids: list[list[int]],
    read_identifier: list[list[int]],
    *,
    mode: str = "DMG",
    write_pin: str = "WR",
    reset: list[list[int]] | None = None,
) -> dict[str, object]:
    commands: dict[str, object] = {"read_identifier": read_identifier}
    if reset is not None:
        commands["reset"] = reset
    return flashcart_profile(
        type=mode,
        names=[name],
        flash_ids=flash_ids,
        write_pin=write_pin,
        commands=commands,
    )


def test_collect_flash_detection_commands_keeps_order_filters_and_identifier_deduplication() -> None:
    device = GbxDevice()
    device.MODE = "DMG"
    read_identifier = [[0x5555, 0xAA]]
    first_reset = [[0, 0xF0]]
    second_reset = [[0, 0xFF]]
    third_reset = [[0, 0xF1]]
    unlock = [[0x5555, 0xAA]]
    profiles = [
        {
            "command_set": "GENERIC",
            "commands": {"reset": first_reset, "read_identifier": read_identifier, "read_cfi": [[0, 0x98]]},
        },
        {
            "command_set": "GENERIC",
            "commands": {
                "reset": first_reset,
                "unlock": unlock,
                "read_identifier": read_identifier,
                "read_cfi": [[0, 0x90]],
            },
        },
        {
            "command_set": "GENERIC",
            "commands": {"reset": second_reset, "read_identifier": [[0x8000, 0x90]]},
        },
        {"command_set": "GENERIC", "commands": {"reset": third_reset, "read_cfi": [[0, 0x98]]}},
        {
            "command_set": "GENERIC",
            "manual_select": True,
            "commands": {"reset": [[[0, 0xAA]]], "read_identifier": [[0x5555, 0xAA]]},
        },
    ]
    flash_types: list[int] = []

    commands, reset_commands = device._CollectFlashDetectionCommands(profiles, flash_types)

    assert commands == [
        {"reset": first_reset, "read_identifier": read_identifier, "read_cfi": [[0, 0x98]]},
    ]
    assert reset_commands == [first_reset, unlock, second_reset, third_reset]
    assert flash_types == []


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


@pytest.mark.parametrize("flash_result", [False, (0, 0)], ids=["read-failure", "zero-id"])
def test_detect_agb_flash_no_id_preserves_candidate_size(
    monkeypatch: pytest.MonkeyPatch,
    flash_result: tuple[int, int] | bool,
) -> None:
    device = GbxDevice()
    monkeypatch.setattr(device, "ReadFlashSaveID", lambda: flash_result)

    assert device._DetectAgbFlashSaveType(0x2000) == (None, 0x2000, None)


def test_detect_agb_flash_unknown_id_reports_unknown_chip(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    monkeypatch.setattr(device, "ReadFlashSaveID", lambda: (0xBEEF, 0))

    save_type, save_size, save_chip = device._DetectAgbFlashSaveType(0x10000)

    assert (save_type, save_size) == (0, 0)
    assert save_chip is not None
    assert "Unknown FLASH save chip" in save_chip
    assert "0xBEEF" in save_chip


def test_detect_agb_flash_missing_probe_data_returns_unknown_chip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = GbxDevice()
    monkeypatch.setattr(device, "ReadFlashSaveID", lambda: (0xBF4B, 0))

    result = device._DetectAgbFlashSaveType(0)

    assert result == (0, 0, "Unknown FLASH save chip (0xBF4B)")
    assert "Error: Couldn't check save type with FLASH save ID 0xBF4B" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("flash_id", "expected_size", "expected_type"),
    [(0xBFD4, 0x10000, 4), (0xC209, 0x20000, 5)],
)
def test_detect_agb_flash_known_chip_uses_real_chip_table(
    monkeypatch: pytest.MonkeyPatch,
    flash_id: int,
    expected_size: int,
    expected_type: int,
) -> None:
    device = GbxDevice()
    data = bytearray([0x31] * 0x20000)
    original = bytes(data)
    device.INFO["data"] = data
    monkeypatch.setattr(device, "ReadFlashSaveID", lambda: (flash_id, 0))

    result = device._DetectAgbFlashSaveType(0)

    assert result == (expected_type, expected_size, AgbSaveTypes().GetFlashChipName(flash_id))
    assert bytes(device.INFO["data"]) == original


@pytest.mark.parametrize(
    ("data_factory", "expected_type"),
    [
        (lambda: bytearray([0xFF] * 0x20000), 5),
        (lambda: bytearray([0x22] * 0x20000), 4),
        (
            lambda: bytearray([0x22] * 0x10000) + bytearray([0x33] * 0x10000),
            5,
        ),
    ],
    ids=["blank", "mirrored-banks", "distinct-banks"],
)
def test_detect_agb_bootleg_flash_bank_patterns(
    monkeypatch: pytest.MonkeyPatch,
    data_factory: Callable[[], bytearray],
    expected_type: int,
) -> None:
    data = data_factory()
    original = bytes(data)
    device = GbxDevice()
    device.INFO["data"] = data
    flash_id = 0xBF4B
    monkeypatch.setattr(device, "ReadFlashSaveID", lambda: (flash_id, 0))

    result = device._DetectAgbFlashSaveType(0)

    assert result == (
        expected_type,
        AgbSaveTypes().GetFlashChipSize(flash_id),
        AgbSaveTypes().GetFlashChipName(flash_id),
    )
    assert bytes(device.INFO["data"]) == original


def test_detect_agb_resolved_flash_type_skips_non_flash_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = GbxDevice()

    def fail_if_probed() -> bool:
        pytest.fail("resolved FLASH types must bypass SRAM and EEPROM detection")

    monkeypatch.setattr(device, "CheckBatterylessSRAM", fail_if_probed)

    assert device._DetectAgbNonFlashSaveType(4, 0x10000, 0, {"dacs_8m": True}) == (4, 0x10000)


@pytest.mark.parametrize(
    ("save_size", "expected_type", "expected_size"),
    [
        (32768, 3, 32768),
        (65536, 7, 65536),
        (131072, 8, 131072),
        (8192, 2, 8192),
        (12345, None, 0),
    ],
)
def test_detect_agb_non_flash_sram_size_mapping(
    monkeypatch: pytest.MonkeyPatch,
    save_size: int,
    expected_type: int | None,
    expected_size: int,
) -> None:
    device = GbxDevice()
    batteryless_calls: list[bool] = []
    monkeypatch.setattr(device, "CheckBatterylessSRAM", lambda: batteryless_calls.append(True) or False)

    result = device._DetectAgbNonFlashSaveType(None, save_size, 0, {"dacs_8m": False})

    assert result == (expected_type, expected_size)
    assert batteryless_calls == [True]


def test_detect_agb_non_flash_dacs_size_and_type(monkeypatch: pytest.MonkeyPatch) -> None:
    device = GbxDevice()
    monkeypatch.setattr(device, "CheckBatterylessSRAM", lambda: False)

    assert device._DetectAgbNonFlashSaveType(None, 0, 0, {"dacs_8m": True}) == (6, 0x100000)


@pytest.mark.parametrize(
    ("is_dacs", "save_size", "expected_size"),
    [(False, 0x8000, 0x8000), (True, 0, 0x100000)],
    ids=["sram", "dacs"],
)
def test_detect_agb_batteryless_override_updates_both_metadata_locations(
    monkeypatch: pytest.MonkeyPatch,
    is_dacs: bool,
    save_size: int,
    expected_size: int,
) -> None:
    device = GbxDevice()
    info: dict[str, object] = {"dacs_8m": is_dacs}
    batteryless = {"bl_offset": 0x1234, "bl_size": 0x2000}
    monkeypatch.setattr(device, "CheckBatterylessSRAM", lambda: batteryless)

    result = device._DetectAgbNonFlashSaveType(None, save_size, 0, info)

    assert result == (9, expected_size)
    assert info["batteryless_sram"] == batteryless
    assert device.INFO["dump_info"]["batteryless_sram"] == batteryless


@pytest.mark.parametrize(
    ("eeprom_64k", "expected"),
    [
        (bytearray([0] * 0x2000), (0, 0)),
        (bytearray([0xFF] * 0x2000), (0, 0)),
        (bytearray((index % 251) + 1 for index in range(0x2000)), (2, 0x2000)),
        (bytearray([0x42] * 0x2000), (1, 512)),
    ],
    ids=["all-zero", "all-ff", "matching-prefix-64k", "different-prefix-4k"],
)
def test_detect_agb_eeprom_uses_two_exact_reads_and_skips_batteryless_probe(
    monkeypatch: pytest.MonkeyPatch,
    eeprom_64k: bytearray,
    expected: tuple[int | None, int],
) -> None:
    eeprom_4k = bytearray((index % 251) + 1 for index in range(512))
    if expected == (1, 512):
        eeprom_4k = bytearray([0x43] * 512)
    reads = iter([eeprom_4k, eeprom_64k])
    device = GbxDevice()
    backup_calls: list[dict[str, object]] = []

    def backup_restore(*, args: dict[str, object]) -> None:
        backup_calls.append(args.copy())
        device.INFO["data"] = bytearray(next(reads))

    def fail_if_batteryless_probe() -> bool:
        pytest.fail("EEPROM detection must not run the batteryless SRAM probe")

    monkeypatch.setattr(device, "_BackupRestoreRAM", backup_restore)
    monkeypatch.setattr(device, "CheckBatterylessSRAM", fail_if_batteryless_probe)

    result = device._DetectAgbNonFlashSaveType(None, 0, 7, {"dacs_8m": False})

    assert result == expected
    assert backup_calls == [
        {"mode": 2, "path": None, "mbc": 7, "save_type": 1, "rtc": False, "detect": True},
        {"mode": 2, "path": None, "mbc": 7, "save_type": 2, "rtc": False, "detect": True},
    ]


def test_match_detected_flash_types_skips_empty_and_non_dictionary_profiles() -> None:
    device = GbxDevice()
    device.MODE = "DMG"  # type: ignore[assignment]
    read_identifier = [[0x5555, 0x00F0]]
    matching = make_identifier_profile("Matched", [[0x12, 0x34]], read_identifier)
    missing_flash_ids = make_identifier_profile("No IDs", [[0x12, 0x34]], read_identifier)
    del missing_flash_ids["flash_ids"]
    missing_commands = make_identifier_profile("No commands", [[0x12, 0x34]], read_identifier)
    del missing_commands["commands"]
    empty_commands = make_identifier_profile("Empty commands", [[0x12, 0x34]], read_identifier)
    empty_commands["commands"] = {}
    supported_carts = [
        {},
        None,
        "not a profile",
        {},
        make_identifier_profile("Empty IDs", [], read_identifier),
        empty_commands,
        missing_flash_ids,
        missing_commands,
        matching,
    ]
    methods = [(0, 0, [0x12, 0x34, 0x56], None, read_identifier)]
    commands = [{"read_identifier": read_identifier}]
    resets: list[tuple[list[list[int]], bool]] = []
    device._cart_write_flash = lambda reset, flashcart=False: resets.append((reset, flashcart))  # type: ignore[method-assign]
    found: list[int] = []

    device._MatchDetectedFlashTypes(supported_carts, methods, commands, ["WR"], found)

    assert found == [8]
    assert resets == []


def test_match_detected_flash_types_without_probe_methods_does_not_match() -> None:
    device = GbxDevice()
    read_identifier = [[0x5555, 0x00F0]]
    profile = make_identifier_profile("Unprobed", [[0x12]], read_identifier)
    found: list[int] = []

    device._MatchDetectedFlashTypes([{}, profile], [], [{"read_identifier": read_identifier}], ["WR"], found)

    assert found == []


def test_match_detected_flash_types_requires_matching_identifier_command() -> None:
    device = GbxDevice()
    expected_command = [[0x5555, 0x00F0]]
    other_command = [[0x2AAA, 0x00F0]]
    profile = make_identifier_profile("Different command", [[0x12, 0x34]], expected_command)
    methods = [(0, 0, [0x12, 0x34], None, other_command)]
    found: list[int] = []

    device._MatchDetectedFlashTypes(
        [{}, profile],
        methods,
        [{"read_identifier": expected_command}],
        ["WR"],
        found,
    )

    assert found == []


def test_match_detected_flash_types_rejects_dmg_write_pin_mismatch() -> None:
    device = GbxDevice()
    device.MODE = "DMG"  # type: ignore[assignment]
    read_identifier = [[0x5555, 0x00F0]]
    profile = make_identifier_profile(
        "Audio pin only",
        [[0x12, 0x34]],
        read_identifier,
        write_pin="AUDIO",
    )
    methods = [(0, 0, [0x12, 0x34, 0x56], None, read_identifier)]
    found: list[int] = []

    device._MatchDetectedFlashTypes(
        [{}, profile],
        methods,
        [{"read_identifier": read_identifier}],
        ["WR", "AUDIO"],
        found,
    )

    assert found == []


def test_match_detected_flash_types_agb_ignores_dmg_write_pin() -> None:
    device = GbxDevice()
    device.MODE = "AGB"  # type: ignore[assignment]
    read_identifier = [[0x5555, 0x00F0]]
    profile = make_identifier_profile(
        "AGB ignores DMG pin",
        [[0x12, 0x34]],
        read_identifier,
        mode="AGB",
        write_pin="AUDIO",
    )
    methods = [(0, 0, [0x12, 0x34, 0x56], None, read_identifier)]
    found: list[int] = []

    device._MatchDetectedFlashTypes(
        [{}, profile],
        methods,
        [{"read_identifier": read_identifier}],
        ["WR"],
        found,
    )

    assert found == [1]


@pytest.mark.parametrize(
    ("observed_id", "expected"),
    [([0x12, 0x34, 0x56], [1]), ([0x12, 0x35, 0x56], [])],
    ids=["prefix-match", "prefix-mismatch"],
)
def test_match_detected_flash_types_matches_id_prefix(
    observed_id: list[int],
    expected: list[int],
) -> None:
    device = GbxDevice()
    device.MODE = "DMG"  # type: ignore[assignment]
    read_identifier = [[0x5555, 0x00F0]]
    profile = make_identifier_profile("Prefix profile", [[0x12, 0x34]], read_identifier)
    methods = [(0, 0, observed_id, None, read_identifier)]
    found: list[int] = []

    device._MatchDetectedFlashTypes(
        [{}, profile],
        methods,
        [{"read_identifier": read_identifier}],
        ["WR"],
        found,
    )

    assert found == expected


def test_match_detected_flash_types_keeps_unique_profile_order_and_resets_only_as_defined() -> None:
    device = GbxDevice()
    device.MODE = "DMG"  # type: ignore[assignment]
    read_identifier = [[0x5555, 0x00F0]]
    reset_first = [[0x5555, 0x00F0], [0x2AAA, 0x00F0]]
    reset_last = [[0x1234, 0x00F0]]
    supported_carts = [
        {},
        make_identifier_profile("First", [[0x12], [0x12], [0x12, 0x34]], read_identifier, reset=reset_first),
        make_identifier_profile("Second", [[0x12, 0x34]], read_identifier),
        make_identifier_profile("Third", [[0xAB]], read_identifier, reset=reset_last),
    ]
    methods = [
        (0, 0, [0x12, 0x34, 0x56], None, read_identifier),
        (0, 0, [0x12, 0x34, 0x56], None, read_identifier),
        (0, 0, [0xAB, 0xCD], None, read_identifier),
    ]
    resets: list[tuple[list[list[int]], bool]] = []
    device._cart_write_flash = lambda reset, flashcart=False: resets.append((reset, flashcart))  # type: ignore[method-assign]
    found: list[int] = []

    device._MatchDetectedFlashTypes(
        supported_carts,
        methods,
        [{"read_identifier": read_identifier}],
        ["WR"],
        found,
    )

    assert found == [1, 2, 3]
    assert resets == [(reset_first, True), (reset_last, True)]
