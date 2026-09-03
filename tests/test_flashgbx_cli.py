"""Hardware-free tests for the command-line frontend."""

from __future__ import annotations

import sys
import zipfile
from argparse import Namespace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from serial import SerialException

import FlashGBX.FlashGBX_CLI as cli_module
from FlashGBX.FlashGBX_CLI import CLIConfig, FlashGBX_CLI
from FlashGBX.PocketCamera import PocketCamera

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def make_args(**overrides: object) -> Namespace:
    """Return the complete argument surface consumed by CLI helpers."""
    values: dict[str, object] = {
        "action": "info",
        "device_port": None,
        "device_limit_baudrate": False,
        "mode": "dmg",
        "ignore_bad_header": False,
        "path": "auto",
        "gbcamera_palette": PocketCamera.PALETTE_NAMES[0],
        "gbcamera_outfile_format": "png",
        "gbcamera_extract": False,
        "generate_dump_report": False,
        "flashcart_type": "autodetect",
        "dmg_mbc": "auto",
        "dmg_romsize": "auto",
        "agb_romsize": "auto",
        "dmg_savetype": "auto",
        "agb_savetype": "auto",
        "save_filename_add_datetime": False,
        "store_rtc": False,
        "overwrite": True,
        "keep_calibration": False,
        "force_5v": False,
        "prefer_chip_erase": False,
        "no_verify_write": False,
        "compare_sectors": False,
        "bl_offset": "auto",
        "bl_size": "auto",
        "bl_layout": "auto",
    }
    values.update(overrides)
    return Namespace(**values)


def make_cli(tmp_path: Path, args: Namespace | None = None) -> FlashGBX_CLI:
    config = cast(
        "CLIConfig",
        {
            "app_path": str(tmp_path),
            "config_path": str(tmp_path),
            "flashcarts": {"DMG": [{}], "AGB": [{}]},
            "config_ret": [],
            "argparsed": args or make_args(),
        },
    )
    return FlashGBX_CLI(config)


class FakeConnection:
    """Device protocol fake shared by CLI operation tests."""

    def __init__(self, mode: str = "DMG") -> None:
        self.mode = mode
        self.FW: dict[str, object] = {}
        self.FW_UPDATE_REQ = False
        self.USER_ANSWER: bool | None = None
        self.INFO: dict[str, Any] = {}
        self.calls: list[tuple[str, object]] = []
        self.transfer_calls: list[dict[str, Any]] = []
        self.header: dict[str, Any] = {}
        self.supported_modes: list[str] = [mode]
        self.connected = True
        self.initialize_results: list[object] = [True]

    def GetMode(self) -> str:
        return self.mode

    def SetMode(self, mode: str) -> None:
        self.mode = mode
        self.calls.append(("mode", mode))

    def GetSupprtedModes(self) -> list[str]:
        return self.supported_modes

    def GetCartModeSwitchState(self) -> int | bool:
        return cast("int | bool", self.INFO.get("switch_state", False))

    def SetAutoPowerOff(self, value: int) -> None:
        self.calls.append(("auto_power_off", value))

    def SetAGBReadMethod(self, method: int) -> None:
        self.calls.append(("agb_read_method", method))

    def CartPowerOn(self) -> None:
        self.calls.append(("power", "on"))

    def ReadHeader(self, checkRtc: bool = True) -> dict[str, Any]:
        self.calls.append(("read_header", checkRtc))
        return self.header

    def IsSupportedMbc(self, mapper: int) -> bool:
        self.calls.append(("supported_mbc", mapper))
        return bool(self.INFO.get("supported_mbc", True))

    def IsSupported3dMemory(self) -> bool:
        return bool(self.INFO.get("supported_3d", True))

    def GetFullName(self) -> str:
        return "Mock Reader"

    def GetFullNameExtended(self, more: bool = False) -> str:
        return "Mock Reader (details)" if more else "Mock Reader"

    def GetFWBuildDate(self) -> str:
        return cast("str", self.INFO.get("build_date", ""))

    def FirmwareUpdateAvailable(self) -> bool:
        return bool(self.INFO.get("firmware_update", False))

    def TransferData(self, *, args: dict[str, Any], signal: object) -> bool:
        del signal
        self.transfer_calls.append(args)
        return bool(self.INFO.get("transfer_result", True))

    def GetSupportedCartridgesDMG(self) -> tuple[list[str], list[dict[str, Any]]]:
        return cast(
            "tuple[list[str], list[dict[str, Any]]]",
            self.INFO.get("dmg_carts", (["Generic", "Mock Profile"], [{}, {}])),
        )

    def GetSupportedCartridgesAGB(self) -> tuple[list[str], list[dict[str, Any]]]:
        return cast(
            "tuple[list[str], list[dict[str, Any]]]",
            self.INFO.get("agb_carts", (["Generic", "Mock Profile"], [{}, {}])),
        )

    def CheckROMStable(self) -> bool:
        return bool(self.INFO.get("stable", True))

    def _DetectCartridge(self, *, args: dict[str, object]) -> None:
        self.calls.append(("detect", args))

    def CanSetVoltageByAutoswitch(self) -> bool:
        return bool(self.INFO.get("voltage_autoswitch", False))

    def CanSetVoltageByCode(self) -> bool:
        return bool(self.INFO.get("voltage_code", True))

    def CanPowerCycleCart(self) -> bool:
        return bool(self.INFO.get("power_cycle", False))

    def CartPowerCycle(self) -> None:
        self.calls.append(("power", "cycle"))

    def GetDumpReport(self) -> str | bool:
        return cast("str | bool", self.INFO.get("dump_report", False))

    def Initialize(
        self,
        flashcarts: object,
        *,
        port: str | None,
        max_baud: int,
    ) -> object:
        del flashcarts
        self.calls.append(("initialize", (port, max_baud)))
        return self.initialize_results.pop(0) if self.initialize_results else True

    def IsConnected(self) -> bool:
        return self.connected

    def GetPort(self) -> str:
        return "mock-port"

    def Close(self, cartPowerOff: bool = False) -> None:
        self.calls.append(("close", cartPowerOff))


def dmg_header(raw: bytearray | None = None) -> dict[str, Any]:
    return {
        "db": None,
        "game_title": "POKEMON RED",
        "game_code": "",
        "version": 0,
        "cgb": 0,
        "sgb": 3,
        "old_lic": 1,
        "rtc_string": "Present",
        "logo_correct": True,
        "header_checksum_correct": True,
        "raw": raw or bytearray(0x180),
        "rom_checksum": 0x1234,
        "rom_size_raw": 5,
        "rom_size": 0x100000,
        "mapper_raw": 0x13,
        "ram_size_raw": 3,
    }


def agb_header(raw: bytearray | None = None) -> dict[str, Any]:
    return {
        "db": None,
        "game_title": "TEST GAME",
        "game_code": "ABCD",
        "version": 1,
        "rtc_string": "Not detected",
        "logo_correct": True,
        "header_checksum_correct": True,
        "header_checksum": 0x42,
        "header_checksum_calc": 0x42,
        "raw": raw or bytearray(0x200),
        "rom_size_calc": 0x200000,
        "rom_size": 0x200000,
        "save_type": 1,
        "dacs_8m": False,
        "3d_memory": False,
        "vast_fame": False,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", 0x01), ("0x22", 0x22), ("7", 0x22), ("bad", 0x19), (None, 0x19)],
)
def test_cli_static_mapper_helpers(value: object, expected: int) -> None:
    assert FlashGBX_CLI._ParseDmgMbc(value) == expected
    assert FlashGBX_CLI._GetPlatformName("DMG") == "Game Boy or Game Boy Color"
    assert FlashGBX_CLI._GetPlatformName("custom") == "custom"


def test_cli_platform_autodetection_and_required_header_values() -> None:
    conn = FakeConnection()
    assert FlashGBX_CLI._GetAutoPlatformMode(conn, ["DMG"]) == "DMG"

    conn.supported_modes = ["DMG", "AGB"]
    conn.FW["cart_mode_switch"] = True
    conn.INFO["switch_state"] = 1
    assert FlashGBX_CLI._GetAutoPlatformMode(conn) == "AGB"
    conn.INFO["switch_state"] = False
    conn.mode = "DMG"
    assert FlashGBX_CLI._GetAutoPlatformMode(conn) == "DMG"
    conn.mode = "unsupported"
    assert FlashGBX_CLI._GetAutoPlatformMode(conn) is None

    assert FlashGBX_CLI._GetHeaderInt({"value": 3}, "value") == 3
    with pytest.raises(TypeError, match="value"):
        FlashGBX_CLI._GetHeaderInt({"value": True}, "value")


@pytest.mark.parametrize(
    ("payload", "answer", "expected"),
    [
        ({"user_action": "REINSERT_CART", "msg": "reinsert"}, "", True),
        ({"user_action": "REINSERT_CART", "msg": "reinsert"}, "cancel", False),
        ({"user_action": "RETRY_5V", "msg": "retry", "title": "Voltage"}, "yes", True),
        ({"user_action": "RETRY_5V", "msg": "retry", "title": "Voltage"}, "n", False),
    ],
)
def test_wait_progress_records_mock_user_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, str],
    answer: str,
    expected: bool,
) -> None:
    cli = make_cli(tmp_path)
    cli.CONN = FakeConnection()
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    cli.WaitProgress(payload)

    assert cli.CONN.USER_ANSWER is expected


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "INITIALIZE", "method": "ROM_WRITE_VERIFY"},
        {"action": "INITIALIZE", "method": "SAVE_WRITE_VERIFY"},
        {"action": "ERASE", "time_elapsed": 2},
        {"action": "UNLOCK", "time_elapsed": 3},
        {"action": "SECTOR_ERASE", "sector_pos": 0x4000},
        {"action": "UPDATE_RTC"},
        {"action": "CALC_CHECKSUMS"},
        {"action": "ERROR", "text": "failed"},
        {"action": "ABORTING"},
        {"action": "PROGRESS", "pos": 512, "size": 1024, "speed": 12.5, "time_elapsed": 1, "time_left": 1},
        {"action": "PROGRESS", "pos": 1, "size": 0},
    ],
)
def test_update_progress_handles_status_payloads(tmp_path: Path, payload: dict[str, object]) -> None:
    cli = make_cli(tmp_path)

    cli.UpdateProgress(payload)


def test_update_progress_handles_errors_abort_and_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = make_cli(tmp_path)
    finished: list[bool] = []
    monkeypatch.setattr(cli, "FinishOperation", lambda: finished.append(True))

    cli.UpdateProgress(None)
    cli.UpdateProgress({"error": "mock failure"})
    cli.UpdateProgress({"action": "FINISHED"})
    cli.UpdateProgress(
        {"action": "ABORT", "info_type": "msgbox_critical", "info_msg": "critical"},
    )
    assert cli.RETVAL == 1
    cli.UpdateProgress({"action": "ABORT", "info_type": "label", "info_msg": "stopped"})

    assert cli.RETVAL == 0
    assert finished == [True]
    assert "mock failure" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("verified", "broken", "retval"),
    [(True, None, 0), (False, [(0x1000, 0x100), (0x2000, 0x100)], 1), (False, None, 0)],
)
def test_finish_operation_reports_rom_write_results(
    tmp_path: Path,
    verified: bool,
    broken: list[tuple[int, int]] | None,
    retval: int,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    conn.INFO = {"last_action": 4, "transferred": 1024}
    if broken is not None:
        conn.INFO["broken_sectors"] = broken
    cli.CONN = conn
    cli.PROGRESS.PROGRESS["verified"] = verified

    cli.FinishOperation()

    assert conn.INFO["last_action"] == 0
    assert retval == cli.RETVAL


@pytest.mark.parametrize(
    "case",
    [
        ("DMG", None, 1, 0x1234, 0x1234, False),
        ("DMG", None, 1, 0x1234, 0x9999, 0x8000),
        ("AGB", {"rc": 1}, 1, 0, 0, False),
        ("AGB", {"rc": 2}, 1, 0, 0, 0x10000),
        ("AGB", None, 1, 0, 0, False),
    ],
)
def test_finish_operation_reports_rom_backup_results(
    tmp_path: Path,
    case: tuple[str, dict[str, int] | None, int, int, int, int | bool],
) -> None:
    mode, database, file_crc, rom_checksum, calculated, loop = case
    cli = make_cli(tmp_path)
    conn = FakeConnection(mode)
    conn.INFO = {
        "last_action": 1,
        "transferred": 1024,
        "last_path": str(tmp_path / "backup.gb"),
        "file_crc32": file_crc,
        "file_sha1": "sha1",
        "rom_checksum": rom_checksum,
        "rom_checksum_calc": calculated,
        "loop_detected": loop,
        "db": database,
    }
    cli.CONN = conn

    cli.FinishOperation()

    assert conn.INFO["last_action"] == 0


def test_finish_operation_writes_dump_report_and_save_results(tmp_path: Path) -> None:
    args = make_args(generate_dump_report=True)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    backup_path = tmp_path / "backup.gb"
    conn.INFO = {
        "last_action": 1,
        "transferred": 2048,
        "last_path": str(backup_path),
        "file_crc32": 1,
        "file_sha1": "sha1",
        "rom_checksum": 1,
        "rom_checksum_calc": 1,
        "loop_detected": False,
        "dump_report": "%TRANSFER_RATE% %TIME_ELAPSED%",
    }
    cli.CONN = conn
    cli.FinishOperation()
    assert (tmp_path / "backup.txt").read_bytes().startswith(bytes.fromhex("EFBBBF"))

    conn.INFO = {"last_action": 2, "transferred": 1, "mapper_raw": 0, "dump_info": {"header": {}}}
    cli.FinishOperation()
    conn.INFO = {"last_action": 3, "transferred": 1, "save_erase": True}
    cli.FinishOperation()
    assert "save_erase" not in conn.INFO
    conn.INFO = {"last_action": 3, "transferred": 1}
    cli.FinishOperation()
    conn.INFO = {"last_action": 99, "transferred": 1}
    cli.FinishOperation()
    assert conn.INFO["last_action"] == 0


def test_find_and_connect_device_use_mock_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(device_port="requested", device_limit_baudrate=True)
    cli = make_cli(tmp_path, args)
    disconnected = FakeConnection()
    disconnected.connected = False
    disconnected.initialize_results = [[(3, "<b>not connected</b>")]]
    connected = FakeConnection()
    connected.initialize_results = [[(0, "ready")], [(0, "connected"), (1, "info"), (2, "warning")]]
    modules = [
        SimpleNamespace(GbxDevice=lambda: disconnected),
        SimpleNamespace(GbxDevice=lambda: connected),
    ]
    monkeypatch.setattr(cli_module, "HW_DEVICES", modules)

    assert cli.FindDevices(port="requested") is True
    assert ("Mock Reader", connected) == cli.DEVICE
    assert ("initialize", ("requested", 1_000_000)) in connected.calls
    assert cli.ConnectDevice() is True
    assert cli.CONN is connected


@pytest.mark.parametrize("result", [False, [(3, "fatal")]])
def test_connect_device_rejects_backend_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: object,
) -> None:
    cli = make_cli(tmp_path)
    assert cli.ConnectDevice() is False
    device = FakeConnection()
    device.initialize_results = [result]
    cli.DEVICE = ("Mock", device)
    monkeypatch.setattr(cli_module.time, "sleep", lambda _seconds: None)

    assert cli.ConnectDevice() is False
    assert cli.CONN is None


def test_interactive_console_and_disconnect_are_fully_mocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    cli.CONN = conn
    executed: list[str] = []

    class FakeInteractiveConsole:
        def __init__(self, device: object, *, on_output: object, on_error: object) -> None:
            del device, on_output, on_error

        def print_help(self) -> None:
            executed.append("help")

        def execute_line(self, line: str) -> bool:
            executed.append(line)
            return line != "quit"

    answers: Iterator[str] = iter(["", "read 0 1", "quit"])
    monkeypatch.setattr(cli_module, "InteractiveConsole", FakeInteractiveConsole)
    monkeypatch.setattr("builtins.input", lambda: next(answers))

    cli.InteractiveConsole()
    cli.DisconnectDevice()

    assert executed == ["help", "read 0 1", "quit"]
    assert cli.CONN is None
    assert ("close", True) in conn.calls


def test_read_cartridge_formats_valid_and_invalid_dmg_headers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    conn.INFO = dmg_header()
    cli.CONN = conn

    bad, text, header = cli.ReadCartridge(dmg_header(bytearray(range(256)) + bytearray(128)))

    assert bad is False
    assert header["game_title"] == "POKEMON RED"
    assert "POKEMON RED" in text
    assert "MBC3" in text
    assert (tmp_path / "bootlogo_dmg.bin").read_bytes() == header["raw"][0x104:0x134]

    invalid = dmg_header()
    invalid.update(logo_correct=False, header_checksum_correct=False, rom_size_raw="bad", mapper_raw="bad")
    bad, text, _ = cli.ReadCartridge(invalid)
    assert bad is True
    assert "Not detected" in text

    conn.INFO["supported_mbc"] = False
    cli.ReadCartridge(dmg_header())
    assert "Warning" in capsys.readouterr().out


def test_read_cartridge_formats_agb_database_and_invalid_metadata(tmp_path: Path) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection("AGB")
    conn.INFO = agb_header()
    conn.INFO["supported_3d"] = False
    cli.CONN = conn
    header = agb_header(bytearray(range(256)) + bytearray(256))
    header["db"] = {"gc": "AGB-ABCD", "rc": 0x123456, "rs": 0x4000000, "st": 2}
    header["3d_memory"] = True

    bad, text, parsed = cli.ReadCartridge(header)

    assert bad is False
    assert parsed["rom_size"] == 0x4000000
    assert "64 MiB" in text

    invalid = agb_header()
    invalid.update(
        logo_correct=False,
        header_checksum_correct=False,
        rom_size=0,
        save_type=True,
    )
    bad, text, _ = cli.ReadCartridge(invalid)
    assert bad is True
    assert "Not detected" in text


@pytest.mark.parametrize(("stable", "profiles", "expected"), [(False, [{}], -1), (True, [], -2)])
def test_detect_cartridge_rejects_unstable_or_missing_profiles(
    tmp_path: Path,
    stable: bool,
    profiles: list[dict[str, object]],
    expected: int,
) -> None:
    cli = make_cli(tmp_path)
    cli.FLASHCARTS["DMG"] = cast("Any", profiles)
    conn = FakeConnection()
    conn.INFO["stable"] = stable
    cli.CONN = conn

    assert cli.DetectCartridge() == expected


def test_detect_cartridge_formats_successful_mock_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    conn.header = dmg_header()
    conn.INFO["dmg_carts"] = (
        ["Generic", "Selected", "Compatible"],
        [{}, {"mbc": "manual", "flash_size": 0x200000}, {}],
    )
    conn.INFO["detect_cart"] = (
        dmg_header(),
        None,
        1,
        None,
        False,
        [1, 2],
        1,
        "CFI details",
        None,
        "[   AAA/AA]\nflash id\n",
        0,
    )
    cli.CONN = conn
    monkeypatch.setattr(cli, "ReadCartridge", lambda _header: (False, "header", _header))

    assert cli.DetectCartridge(limitVoltage=True) == 1
    assert ("detect", {"limitVoltage": True, "checkSaveType": True}) in conn.calls


def test_detect_cartridge_handles_failed_and_unknown_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    conn.header = dmg_header()
    cli.CONN = conn
    monkeypatch.setattr(cli, "ReadCartridge", lambda _header: (False, "header", _header))
    assert cli.DetectCartridge() == -1

    conn.INFO["detect_cart"] = (dmg_header(), None, 0, None, False, [], 0, "", None, "id\n", 0)
    conn.mode = "other"
    with pytest.raises(NotImplementedError):
        cli.DetectCartridge()


def test_backup_rom_transfers_generated_dmg_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="backup-rom", path=str(tmp_path / "backup.gb"), flashcart_type="Mock Profile")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.INFO["dmg_carts"] = (
        ["Generic", "Mock Profile"],
        [{}, {"type": "DMG", "names": ["Mock Profile"], "flash_size": 0x200000}],
    )
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")

    cli.BackupROM(args, dmg_header())

    assert (
        conn.transfer_calls[0].items()
        >= {
            "mode": 1,
            "path": str(tmp_path / "backup.gb"),
            "mbc": 0x13,
            "rom_size": 0x200000,
            "cart_type": 1,
        }.items()
    )


def test_backup_rom_handles_invalid_header_and_overwrite_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "existing.gb"
    output.write_bytes(b"existing")
    args = make_args(action="backup-rom", path=str(output), overwrite=False)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    cli.BackupROM(args, {"mapper_raw": None, "rom_size_raw": None})

    assert conn.transfer_calls == []


def test_backup_rom_selects_agb_autodetected_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "backup.gba"
    args = make_args(action="backup-rom", mode="agb", path=str(path))
    cli = make_cli(tmp_path, args)
    conn = FakeConnection("AGB")
    conn.INFO["agb_carts"] = (["Generic", "3D"], [{}, {"3d_memory": True}])
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gba")
    header = agb_header()
    header["3d_memory"] = True

    cli.BackupROM(args, header)

    assert conn.transfer_calls[0]["cart_type"] == 1


def test_flash_rom_writes_through_selected_mock_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rom_path = tmp_path / "generated.gb"
    rom_path.write_bytes(bytes(0x1200))
    args = make_args(action="flash-rom", path=str(rom_path), flashcart_type="Mock Profile", dmg_mbc="5")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.INFO["dmg_carts"] = (
        ["Generic", "Mock Profile"],
        [
            {},
            {
                "type": "DMG",
                "names": ["Mock Profile"],
                "flash_size": 0x200000,
                "voltage": 5,
                "commands": ["chip_erase", "sector_erase"],
                "mbc": "manual",
            },
        ],
    )
    cli.CONN = conn
    parsed_header = {"logo_correct": True, "header_checksum_correct": True}
    monkeypatch.setattr(cli_module, "RomFileDMG", lambda _buffer: SimpleNamespace(GetHeader=lambda: parsed_header))

    cli.FlashROM(args, dmg_header())

    transfer = conn.transfer_calls[0]
    assert transfer["mode"] == 4
    assert transfer["path"] == str(rom_path)
    assert transfer["cart_type"] == 1
    assert transfer["mbc"] == 0x19
    assert transfer["verify_write"] is True


@pytest.mark.parametrize(
    ("path_kind", "flashcart_type"),
    [("auto", "Mock Profile"), ("missing", "Mock Profile"), ("small", "Mock Profile"), ("valid", "unknown")],
)
def test_flash_rom_rejects_invalid_inputs(
    tmp_path: Path,
    path_kind: str,
    flashcart_type: str,
) -> None:
    path = tmp_path / "input.gb"
    if path_kind == "small":
        path.write_bytes(b"small")
    elif path_kind == "valid":
        path.write_bytes(bytes(0x1000))
    args = make_args(path="auto" if path_kind == "auto" else str(path), flashcart_type=flashcart_type)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.INFO["dmg_carts"] = (
        ["Generic", "Mock Profile"],
        [
            {},
            {
                "type": "DMG",
                "names": ["Mock Profile"],
                "voltage": 5,
                "commands": [],
            },
        ],
    )
    cli.CONN = conn

    cli.FlashROM(args, dmg_header())

    assert conn.transfer_calls == []


def test_flash_rom_autodetection_failure_does_not_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(path=str(tmp_path / "input.gb"))
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.INFO["dmg_carts"] = (["Generic"], [{}])
    cli.CONN = conn
    monkeypatch.setattr(cli, "DetectCartridge", lambda: 0)

    cli.FlashROM(args, dmg_header())

    assert conn.transfer_calls == []


def test_backup_restore_ram_covers_backup_restore_and_erase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = FakeConnection()
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")

    backup_args = make_args(action="backup-save", path=str(tmp_path / "backup.sav"))
    cli = make_cli(tmp_path, backup_args)
    cli.CONN = conn
    cli.BackupRestoreRAM(backup_args, dmg_header())

    restore_path = tmp_path / "restore.sav"
    restore_path.write_bytes(bytes(0x2000))
    restore_args = make_args(action="restore-save", path=str(restore_path))
    cli.BackupRestoreRAM(restore_args, dmg_header())

    erase_args = make_args(action="erase-save", path=str(tmp_path / "unused.sav"))
    cli.BackupRestoreRAM(erase_args, dmg_header())

    assert [call["mode"] for call in conn.transfer_calls] == [2, 3, 3]
    assert conn.transfer_calls[1]["erase"] is False
    assert conn.transfer_calls[2]["erase"] is True


@pytest.mark.parametrize(("mode", "save_field"), [("DMG", {"ram_size_raw": 0}), ("AGB", {"save_type": None})])
def test_backup_restore_ram_requires_detectable_save_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    save_field: dict[str, object],
) -> None:
    args = make_args(action="backup-save")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection(mode)
    cli.CONN = conn
    header = dmg_header() if mode == "DMG" else agb_header()
    header.update(save_field)
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.sav")

    cli.BackupRestoreRAM(args, header)

    assert conn.transfer_calls == []


def test_backup_restore_routes_batteryless_sram_to_special_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="backup-save", dmg_savetype="batteryless")
    cli = make_cli(tmp_path, args)
    cli.CONN = FakeConnection()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")
    monkeypatch.setattr(cli, "_BatterylessSRAM", lambda **kwargs: calls.append(kwargs))

    cli.BackupRestoreRAM(args, dmg_header())

    assert calls[0]["save_type"] == 0x205


def test_resolve_batteryless_arguments_from_flags_detection_and_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    cli.CONN = conn

    explicit = make_args(bl_offset="0x100", bl_size="512", bl_layout="2")
    assert cli._ResolveBLArgs(explicit, {}) == {"bl_offset": 0x100, "bl_size": 512, "bl_layout": 2}

    detected = make_args()
    conn.INFO["dump_info"] = {"batteryless_sram": {"bl_offset": 3, "bl_size": 4, "bl_layout": 1}}
    assert cli._ResolveBLArgs(detected, {}) == {"bl_offset": 3, "bl_size": 4, "bl_layout": 1}

    conn.INFO = {}
    monkeypatch.setattr(
        cli_module.RomFileDMG,
        "GetBatterylessSramConfig",
        lambda _header: {"bl_offset": 5, "bl_size": 6},
    )
    assert cli._ResolveBLArgs(detected, {}) == {"bl_offset": 5, "bl_size": 6, "bl_layout": 0}

    assert cli._ResolveBLArgs(make_args(bl_offset="invalid"), {}) is None
    assert cli._ResolveBLArgs(make_args(bl_size="invalid"), {}) is None


def test_batteryless_backup_and_erase_use_mock_transfers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    cli.CONN = conn
    monkeypatch.setattr(
        cli, "_ResolveBLArgs", lambda _args, _header: {"bl_offset": 0x1000, "bl_size": 16, "bl_layout": 0}
    )

    backup = make_args(action="backup-save", path=str(tmp_path / "backup.sav"))
    cli._BatterylessSRAM(backup, dmg_header(), 0x19, 0x205, backup.path)

    erase = make_args(action="erase-save", path=str(tmp_path / "unused.sav"), flashcart_type="Mock Profile")
    monkeypatch.setattr(cli, "_ResolveFlashcartType", lambda _args: 1)
    cli._BatterylessSRAM(erase, dmg_header(), 0x19, 0x205, erase.path)

    assert conn.transfer_calls[0]["mode"] == 1
    assert conn.transfer_calls[0]["bl_offset"] == 0x1000
    assert conn.transfer_calls[1]["mode"] == 4
    assert conn.transfer_calls[1]["buffer"] == bytearray([0xFF] * 16)


def test_resolve_flashcart_type_manual_and_autodetect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    conn.INFO["dmg_carts"] = (
        ["Generic", "Mock"],
        [{}, {"type": "DMG", "names": ["Mock"]}],
    )
    cli.CONN = conn

    assert cli._ResolveFlashcartType(make_args(flashcart_type="Mock")) == 1
    assert cli._ResolveFlashcartType(make_args(flashcart_type="Missing")) is None
    monkeypatch.setattr(cli, "DetectCartridge", lambda: 2)
    assert cli._ResolveFlashcartType(make_args()) == 2
    monkeypatch.setattr(cli, "DetectCartridge", lambda: -1)
    assert cli._ResolveFlashcartType(make_args()) is None


def test_firmware_metadata_and_progress_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive_path = tmp_path / "firmware.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("fw.ini", "[Firmware]\nfw_ver=1.2.3\nfw_buildts=123\n")
    cli = make_cli(tmp_path)

    assert cli._LoadFirmwareInfo(archive_path) == ("1.2.3", 123)
    cli.UpdateFirmware_PrintText("writing", setProgress=42.9)
    cli.UpdateFirmware_PrintText("done")

    assert "writing (42%)" in capsys.readouterr().out


def test_firmware_metadata_rejects_missing_values(tmp_path: Path) -> None:
    archive_path = tmp_path / "firmware.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("fw.ini", "[Firmware]\nfw_ver=1.2.3\n")
    cli = make_cli(tmp_path)

    with pytest.raises(TypeError, match="Invalid firmware metadata"):
        cli._LoadFirmwareInfo(archive_path)


@pytest.mark.parametrize(("result", "expected"), [(1, True), (3, False), (0, False)])
def test_gbxcartrw_firmware_update_is_fully_mocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: int,
    expected: bool,
) -> None:
    cli = make_cli(tmp_path)
    monkeypatch.setattr(cli, "_LoadFirmwareInfo", lambda _path: ("1.0", 123))
    answers: Iterator[str] = iter(["1", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    class FirmwareUpdater:
        def __init__(self, *, port: str) -> None:
            assert port == "mock-port"

        def WriteFirmware(self, file_name: Path, callback: object) -> int:
            del file_name, callback
            return result

    monkeypatch.setattr(cli_module.sys.modules["FlashGBX.hw_GBxCartRW"], "FirmwareUpdater", FirmwareUpdater)

    assert cli.UpdateFirmwareGBxCartRW(port="mock-port") is expected
    assert cli.UpdateFirmwareGBxCartRW(pcb=4) is False


def test_gbxcartrw_firmware_update_retries_serial_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path)
    monkeypatch.setattr(cli, "_LoadFirmwareInfo", lambda _path: ("1.0", 123))
    answers: Iterator[str] = iter(["2", "", "second-port"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    class FirmwareUpdater:
        attempts = 0

        def __init__(self, *, port: str) -> None:
            del port

        def WriteFirmware(self, file_name: Path, callback: object) -> int:
            del file_name, callback
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise SerialException
            return 1

    monkeypatch.setattr(cli_module.sys.modules["FlashGBX.hw_GBxCartRW"], "FirmwareUpdater", FirmwareUpdater)

    assert cli.UpdateFirmwareGBxCartRW(port="first-port") is True
    assert FirmwareUpdater.attempts == 2


def test_run_info_flow_uses_only_mock_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="info", mode="dmg")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.header = dmg_header()
    cli.DEVICE = ("Mock Reader", conn)
    cli.CONN = conn
    output = sys.stdout
    monkeypatch.setattr(cli_module, "Logger", lambda: output)
    monkeypatch.setattr(cli_module, "HW_DEVICES", [])
    monkeypatch.setattr(cli, "FindDevices", lambda *, port: port is None)
    monkeypatch.setattr(cli, "ConnectDevice", lambda: True)

    assert cli.run() == 0
    assert ("mode", "DMG") in conn.calls
    assert ("close", True) in conn.calls


def test_run_reports_missing_device_without_hardware_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path, make_args(action="info"))
    output = sys.stdout
    monkeypatch.setattr(cli_module, "Logger", lambda: output)
    monkeypatch.setattr(cli_module, "HW_DEVICES", [])
    monkeypatch.setattr(cli, "FindDevices", lambda *, port: port is not None)

    assert cli.run() == 1


def test_run_dispatches_mocked_backup_and_disconnects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="backup-rom", mode="agb")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection("AGB")
    conn.header = agb_header()
    cli.DEVICE = ("Mock Reader", conn)
    cli.CONN = conn
    calls: list[tuple[Namespace, dict[str, Any]]] = []
    output = sys.stdout
    monkeypatch.setattr(cli_module, "Logger", lambda: output)
    monkeypatch.setattr(cli_module, "HW_DEVICES", [])
    monkeypatch.setattr(cli, "FindDevices", lambda *, port: port is None)
    monkeypatch.setattr(cli, "ConnectDevice", lambda: True)
    monkeypatch.setattr(cli, "ReadCartridge", lambda header: (False, "header", header))
    monkeypatch.setattr(cli, "BackupROM", lambda action_args, header: calls.append((action_args, header)))

    assert cli.run() == 0
    assert calls == [(args, conn.header)]


def test_run_camera_extract_uses_mock_camera(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_path = tmp_path / "camera.sav"
    save_path.write_bytes(b"generated")
    args = make_args(action="gbcamera-extract", path=str(save_path))
    cli = make_cli(tmp_path, args)
    exports: list[tuple[int, Path]] = []
    output = sys.stdout
    monkeypatch.setattr(cli_module, "Logger", lambda: output)
    monkeypatch.setattr(cli_module, "HW_DEVICES", [])

    class FakeCamera:
        PALETTE_NAMES = PocketCamera.PALETTE_NAMES

        def LoadFile(self, path: str) -> bool:
            return path == str(save_path)

        def SetPalette(self, palette: int) -> None:
            assert palette == 0

        def ExportPicture(self, index: int, path: Path, *, scale: int) -> None:
            assert scale == 1
            exports.append((index, path))

    monkeypatch.setattr(cli_module, "PocketCamera", FakeCamera)

    assert cli.run() == 0
    assert len(exports) == 32
    assert exports[0][1].name == "IMG_PC01.png"
