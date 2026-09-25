"""Hardware-free tests for the command-line frontend."""

from __future__ import annotations

import importlib
import sys
import zipfile
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from serial import SerialException

import FlashGBX.FlashGBX_CLI as cli_module
from FlashGBX.FlashGBX_CLI import CLIConfig, FlashGBX_CLI
from FlashGBX.PocketCamera import PocketCamera

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def make_args(**overrides: object) -> Namespace:
    """Return the complete argument surface consumed by CLI helpers."""
    values: dict[str, object] = {
        "action": "info",
        "device_port": None,
        "gbxcartrw_baudrate": None,
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


def configure_interactive_run(
    cli: FlashGBX_CLI,
    conn: FakeConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    original_stdout = sys.stdout
    monkeypatch.setattr(cli_module, "Logger", lambda: original_stdout)
    monkeypatch.setattr(cli_module, "HW_DEVICES", [])
    monkeypatch.setattr(cli, "_connect_for_action", lambda _args, _actions: True)
    cli.CONN = conn
    cli.DEVICE = ("Mock Reader", conn)
    console = SimpleNamespace(call_count=0)

    def run_console() -> None:
        console.call_count += 1

    monkeypatch.setattr(cli, "InteractiveConsole", run_console)
    return console


class FakeConnection:
    """Device protocol fake shared by CLI operation tests."""

    DEVICE_ID = "gbxcartrw"
    DEVICE_NAME = "GBxCart RW"

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


def configure_batteryless_profile(
    conn: FakeConnection,
    mode: str,
    **profile_values: object,
) -> str:
    profile_name = "Batteryless Profile"
    profile = {"type": mode, "names": [profile_name], **profile_values}
    conn.INFO[f"{mode.lower()}_carts"] = (["Generic", profile_name], [{}, profile])
    return profile_name


def expected_batteryless_write_args(
    *,
    path: str,
    mbc: int,
    offset: int,
    size: int,
    layout: int | None,
    verify_write: bool,
    compare_sectors: bool,
    erase: bool = False,
) -> dict[str, Any]:
    expected: dict[str, Any] = {
        "mode": 4,
        "path": "" if erase else path,
        "cart_type": 1,
        "override_voltage": False,
        "prefer_chip_erase": False,
        "fast_read_mode": True,
        "verify_write": verify_write,
        "fix_header": False,
        "fix_bootlogo": False,
        "mbc": mbc,
        "compare_sectors": compare_sectors,
        "bl_save": True,
        "flash_offset": offset,
        "flash_size": size,
        "bl_offset": offset,
        "bl_size": size,
    }
    if layout is not None:
        expected["bl_layout"] = layout
    if erase:
        expected["buffer"] = bytearray([0xFF] * size)
    return expected


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
    ("answer", "expected"),
    [
        ("", "info"),
        ("1", "info"),
        ("3", "interactive"),
        ("not-a-number", None),
        ("0", None),
        ("4", None),
    ],
)
def test_select_menu_action_maps_displayed_positions_and_rejects_invalid_answers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    expected: str | None,
) -> None:
    menu_items = [
        ("info", "Read Information"),
        ("backup", "Back Up"),
        ("interactive", "Interactive Console"),
    ]
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert FlashGBX_CLI._SelectMenuAction(menu_items) == expected

    output = capsys.readouterr().out
    assert "1) Read Information" in output
    assert "2) Back Up" in output
    assert "3) Interactive Console" in output


def test_print_config_messages_uses_status_colors_and_ignores_malformed_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    FlashGBX_CLI._PrintConfigMessages(
        [
            [],
            [0],
            ["bad", "wrong status"],
            [1, 42],
            [0, "plain"],
            [1, "warning"],
            [2, "error"],
            [3, "ignored status"],
        ],
    )

    output = capsys.readouterr().out
    assert "plain\n" in output
    assert f"{cli_module.ANSI.YELLOW}warning{cli_module.ANSI.RESET}\n" in output
    assert f"{cli_module.ANSI.RED}error{cli_module.ANSI.RESET}\n" in output
    assert "wrong status" not in output
    assert "ignored status" not in output


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
    capsys: pytest.CaptureFixture[str],
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
    output = capsys.readouterr().out
    if verified:
        assert "written and verified successfully" in output
    elif broken is not None:
        assert "verification of written data failed" in output
    else:
        assert "ROM writing complete" in output


@pytest.mark.parametrize(
    "case",
    [
        ("DMG", None, 1, 0x1234, 0x1234, False, None, "checksum was verified", False),
        ("DMG", None, 1, 0x1234, 0x9999, 0x8000, None, "checksum is not correct", True),
        ("DMG", None, 1, 0x1234, 0x9999, 0, None, "checksum is not correct", True),
        ("DMG", None, 1, 0x1234, 0x9999, False, None, "checksum is not correct", False),
        ("DMG", None, 1, 0x1234, 0x9999, False, 0x105, "ROM backup is complete!", False),
        ("AGB", {"rc": 1}, 1, 0, 0, False, None, "checksum was verified", False),
        ("AGB", {"rc": 2}, 1, 0, 0, 0x10000, None, "doesn't match the known database entry", True),
        ("AGB", {"rc": 2}, 1, 0, 0, False, None, "doesn't match the known database entry", False),
        ("AGB", None, 1, 0, 0, False, None, "verification was skipped", False),
    ],
)
def test_finish_operation_reports_rom_backup_results(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, dict[str, int] | None, int, int, int, int | bool, int | None, str, bool],
) -> None:
    mode, database, file_crc, rom_checksum, calculated, loop, mapper_raw, expected_message, expect_loop = case
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
    if mapper_raw is not None:
        conn.INFO["mapper_raw"] = mapper_raw
    cli.CONN = conn

    cli.FinishOperation()

    assert conn.INFO["last_action"] == 0
    output = capsys.readouterr().out
    assert "CRC32: 00000001" in output
    assert "SHA-1: sha1" in output
    assert expected_message in output
    assert ("A data loop was detected" in output) is expect_loop


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


def test_gbxcartrw_baudrate_only_changes_gbxcartrw_connections(tmp_path: Path) -> None:
    cli = make_cli(tmp_path, make_args(gbxcartrw_baudrate=1_700_000))
    gbxcartrw = FakeConnection()
    gbxcartrw.DEVICE_NAME = "Custom reader name"
    other_device = FakeConnection()
    other_device.DEVICE_ID = "other"
    other_device.DEVICE_NAME = "Other Reader"

    assert cli._GetDeviceMaxBaudRate(gbxcartrw) == 1_700_000
    assert cli._GetDeviceMaxBaudRate(other_device) == 2_000_000

    cli = make_cli(tmp_path)
    assert cli._GetDeviceMaxBaudRate(gbxcartrw) == 1_500_000


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
    assert "Revision:" in text
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
    header["db"] = {"gc": "DB-CODE", "rc": 0x123456, "rs": 0x4000000, "st": 2}
    header["3d_memory"] = True

    bad, text, parsed = cli.ReadCartridge(header)

    assert bad is False
    assert parsed["rom_size"] == 0x4000000
    assert "64 MiB" in text
    assert "Game Name:" in text
    assert "DB-CODE-1" in text
    assert "Game Code and Revision:  ABCD-1" not in text

    no_database_header = agb_header()
    no_database_header["game_code"] = ""
    _bad, text, _parsed = cli.ReadCartridge(no_database_header)
    assert "Game Name:" not in text
    assert "Game Code and Revision:" not in text
    assert "Revision:" not in text

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


@pytest.mark.parametrize(
    ("database", "rom_size_calc", "rom_size", "checksum_text", "size_text", "expected_size", "bad_read"),
    [
        ({"rc": 0x123456, "rs": 0x200000}, 0x200000, 0, "0x123456", "2 MiB", 0x200000, False),
        ({"rc": 0x123456, "rs": 0x4000000}, 0x4000000, 0, None, "64 MiB", 0x4000000, False),
        (None, 0x200000, 0x200000, "No database entry", "2 MiB", 0x200000, False),
        (None, 0x200000, 0x300000, "No database entry", "32 MiB", 0x2000000, False),
        (None, 0x200000, 0, "No database entry", "Not detected", 0, True),
    ],
)
def test_agb_rom_details_formats_database_and_fallback_sizes(
    database: dict[str, int] | None,
    rom_size_calc: int,
    rom_size: int,
    checksum_text: str | None,
    size_text: str,
    expected_size: int,
    bad_read: bool,
) -> None:
    data: dict[str, Any] = {
        "db": database,
        "rom_size_calc": rom_size_calc,
        "rom_size": rom_size,
    }

    checksum, size, result_bad_read = FlashGBX_CLI._AgbROMDetails(data)

    assert size == size_text
    assert result_bad_read is bad_read
    assert data["rom_size"] == expected_size
    if checksum_text is None:
        assert checksum is None
    else:
        assert checksum_text in str(checksum)


@pytest.mark.parametrize(
    ("save_type", "save_chip", "sram_unstable", "mode", "expected_text"),
    [
        (None, None, False, "DMG", "None or unknown"),
        (0, "Unknown flash chip", None, "AGB", "Unknown flash chip"),
        (1, None, False, "AGB", "4K EEPROM"),
        (3, None, True, "AGB", "not stable or not battery-backed"),
        (4, None, False, "DMG", "256K SRAM"),
    ],
)
def test_detected_save_type_message(
    save_type: int | None,
    save_chip: str | None,
    sram_unstable: bool | None,
    mode: Literal["DMG", "AGB"],
    expected_text: str,
) -> None:
    message = FlashGBX_CLI._FormatDetectedSaveType(save_type, save_chip, sram_unstable, mode)

    assert "Save Type:" in message
    assert expected_text in message


@pytest.mark.parametrize(
    ("cfi_data", "expected_text"),
    [
        ("CFI details", "CFI details"),
        ("", "No data provided"),
    ],
)
def test_detected_cfi_message_preserves_data_and_empty_fallback(cfi_data: str, expected_text: str) -> None:
    message = FlashGBX_CLI._FormatDetectedCFI(cfi_data)

    assert "Common Flash Interface Data:" in message
    assert expected_text in message
    assert message.endswith("\n\n")


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


@pytest.mark.parametrize(
    ("mapper_profile", "expected_mapper"),
    [
        ({"mbc": "manual"}, "Manual selection"),
        ({"mbc": 0x13}, "MBC3"),
        ({"mbc": 0x7F}, None),
        ({}, "Default"),
    ],
)
def test_detect_cartridge_formats_dmg_mapper_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mapper_profile: dict[str, object],
    expected_mapper: str | None,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    conn.header = dmg_header()
    conn.INFO["dmg_carts"] = (["Generic", "Selected"], [{}, {**mapper_profile}])
    conn.INFO["detect_cart"] = (dmg_header(), None, 1, None, False, [1], 1, "", None, "id\n", 0)
    cli.CONN = conn
    monkeypatch.setattr(cli, "ReadCartridge", lambda _header: (False, "header", _header))

    assert cli.DetectCartridge() == 1
    output = capsys.readouterr().out
    if expected_mapper is None:
        assert "Mapper Type:" not in output
    else:
        assert expected_mapper in output


@pytest.mark.parametrize(
    ("flash_id", "expected_profile"),
    [
        ("prefix\nflash id\n", None),
        ("prefix [   AAA/AA]\nflash id\n", "Generic Flash Cartridge (AAA/AA)"),
        (
            "prefix [   AAA/AA] [     0/90]\nflash id\n",
            "Generic Flash Cartridge (0/90)",
        ),
    ],
)
def test_detect_cartridge_formats_unknown_flashcart_suggestions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flash_id: str,
    expected_profile: str | None,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    header = dmg_header()
    conn.INFO["dmg_carts"] = (["Generic"], [{}])
    conn.INFO["detect_cart"] = (header, None, 0, None, False, [], 0, "", None, flash_id, 0)
    cli.CONN = conn
    monkeypatch.setattr(cli, "ReadCartridge", lambda _header: (False, "header", _header))

    assert cli.DetectCartridge() is None

    output = capsys.readouterr().out
    assert "Unknown flash cartridge" in output
    if expected_profile is not None:
        assert expected_profile in output


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

    assert conn.transfer_calls == [
        {
            "mode": 1,
            "path": str(tmp_path / "backup.gb"),
            "mbc": 0x13,
            "rom_size": 0x200000,
            "agb_rom_size": 0x200000,
            "start_addr": 0,
            "fast_read_mode": True,
            "cart_type": 1,
        },
    ]


def test_backup_rom_falls_back_when_auto_size_lookup_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = make_args(action="backup-rom", path=str(tmp_path / "backup.gb"))
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")
    monkeypatch.setattr(cli_module.RomSizes, "GetSize", lambda _self, _index: None)

    cli.BackupROM(args, dmg_header())

    assert conn.transfer_calls[0]["rom_size"] == 8 * 1024 * 1024
    assert "Couldn't determine ROM size, will use 8 MiB" in capsys.readouterr().out


def test_backup_rom_uses_explicit_dmg_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="backup-rom", path=str(tmp_path / "backup.gb"), dmg_romsize="2mb")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")

    cli.BackupROM(args, dmg_header())

    assert conn.transfer_calls[0]["rom_size"] == 0x200000


def test_backup_mapper_message_handles_known_and_unknown_ids(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cli = make_cli(tmp_path)

    cli._PrintBackupMapper(0x13)
    known_mapper_message = capsys.readouterr().out
    assert "Mapper Type" in known_mapper_message
    assert "0x13" not in known_mapper_message

    cli._PrintBackupMapper(0x7F)
    assert "0x7F" in capsys.readouterr().out


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

    assert conn.transfer_calls == [
        {
            "mode": 4,
            "path": str(rom_path),
            "cart_type": 1,
            "override_voltage": False,
            "prefer_chip_erase": False,
            "fast_read_mode": True,
            "verify_write": True,
            "fix_header": False,
            "fix_bootlogo": False,
            "mbc": 0x19,
            "compare_sectors": False,
            "voltage_fallback": False,
        },
    ]


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


@pytest.mark.parametrize(
    ("mode", "logo_correct", "mbc"),
    [
        ("DMG", True, 0x19),
        ("AGB", True, 0),
        ("DMG", False, 0x203),
        ("DMG", False, 0x205),
    ],
)
def test_prompt_boot_logo_fix_bypasses_valid_and_exempt_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    logo_correct: bool,
    mbc: int,
) -> None:
    cli = make_cli(tmp_path)
    cli.CONN = FakeConnection(mode)

    def fail_if_prompted(_prompt: str) -> str:
        pytest.fail("valid and exempt headers must not prompt for a boot-logo fix")

    monkeypatch.setattr("builtins.input", fail_if_prompted)

    assert cli._PromptBootLogoFix({"logo_correct": logo_correct}, mbc) is False


@pytest.mark.parametrize("mode", ["DMG", "AGB"])
def test_prompt_boot_logo_fix_missing_file_returns_false_without_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    cli = make_cli(tmp_path)
    cli.CONN = FakeConnection(mode)
    monkeypatch.setattr(cli_module.AppContext, "CONFIG_PATH", str(tmp_path))

    def fail_if_prompted(_prompt: str) -> str:
        pytest.fail("a missing boot-logo file must not prompt")

    monkeypatch.setattr("builtins.input", fail_if_prompted)

    assert cli._PromptBootLogoFix({"logo_correct": False}, 0x19) is False


@pytest.mark.parametrize(
    ("mode", "file_name", "read_length", "answer", "accept"),
    [
        ("DMG", "bootlogo_dmg.bin", 0x30, "", True),
        ("DMG", "bootlogo_dmg.bin", 0x30, "n", False),
        ("AGB", "bootlogo_agb.bin", 0x9C, "y", True),
        ("AGB", "bootlogo_agb.bin", 0x9C, "n", False),
    ],
)
def test_prompt_boot_logo_fix_reads_exact_platform_length_and_honors_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    file_name: str,
    read_length: int,
    answer: str,
    accept: bool,
) -> None:
    source = bytes(range(256)) * 2
    (tmp_path / file_name).write_bytes(source)
    cli = make_cli(tmp_path)
    cli.CONN = FakeConnection(mode)
    monkeypatch.setattr(cli_module.AppContext, "CONFIG_PATH", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    result = cli._PromptBootLogoFix({"logo_correct": False}, 0x19)

    assert result == (bytearray(source[:read_length]) if accept else False)


@pytest.mark.parametrize(
    ("case", "mode", "arg_updates", "header_updates", "expected"),
    [
        ("zero-mapper", "DMG", {}, {"mapper_raw": 0}, (0x19, 3, 0)),
        ("unusable-mapper", "DMG", {}, {"mapper_raw": None}, (0x19, 3, 0)),
        ("explicit-mapper", "DMG", {"dmg_mbc": "3"}, {}, (0x13, 3, 0)),
        ("mbc2", "DMG", {}, {"mapper_raw": 0x06}, (0x06, 0x100, 0)),
        (
            "mbc7-kirby",
            "DMG",
            {},
            {"mapper_raw": 0x22, "game_title": "KORO2 KIRBYKKKJ"},
            (0x22, 0x101, 0),
        ),
        (
            "mbc7-command-master",
            "DMG",
            {},
            {"mapper_raw": 0x22, "game_title": "CMASTER_KCEJ"},
            (0x22, 0x102, 0),
        ),
        ("tama5", "DMG", {}, {"mapper_raw": 0xFD}, (0xFD, 0x103, 0)),
        ("mbc6", "DMG", {}, {"mapper_raw": 0x20}, (0x20, 0x104, 0)),
        ("dmg-batteryless", "DMG", {"dmg_savetype": "batteryless"}, {}, (0x13, 0x205, 0)),
        ("agb-auto", "AGB", {}, {"save_type": 4}, (0, 4, 0)),
        ("agb-explicit", "AGB", {"agb_savetype": "flash1m"}, {}, (0, 5, 0)),
        ("agb-batteryless", "AGB", {"agb_savetype": "batteryless"}, {}, (0, 9, 0)),
        ("agb-invalid", "AGB", {"agb_savetype": "unknown"}, {}, None),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_resolve_save_configuration_special_and_explicit_types(
    tmp_path: Path,
    case: str,
    mode: str,
    arg_updates: dict[str, object],
    header_updates: dict[str, object],
    expected: tuple[int, int, int | None] | None,
) -> None:
    del case
    cli = make_cli(tmp_path)
    cli.CONN = FakeConnection(mode)
    header = dmg_header() if mode == "DMG" else agb_header()
    header.update(header_updates)

    assert cli._ResolveSaveConfiguration(make_args(**arg_updates), header) == expected


def test_resolve_save_configuration_detects_photo_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path)
    cli.CONN = FakeConnection()
    detected: list[bool] = []

    def detect_cartridge() -> int:
        detected.append(True)
        return 7

    monkeypatch.setattr(cli, "DetectCartridge", detect_cartridge)

    assert cli._ResolveSaveConfiguration(make_args(dmg_savetype="photo"), dmg_header()) == (0x13, 0x204, 7)
    assert detected == [True]


@pytest.mark.parametrize("malformed_ram_size", ["missing", "wrong-type"])
def test_resolve_save_configuration_rejects_malformed_auto_save_metadata(
    tmp_path: Path,
    malformed_ram_size: str,
) -> None:
    cli = make_cli(tmp_path)
    cli.CONN = FakeConnection()
    header = dmg_header()
    if malformed_ram_size == "missing":
        del header["ram_size_raw"]
    else:
        header["ram_size_raw"] = "invalid"

    assert cli._ResolveSaveConfiguration(make_args(), header) is None


def test_backup_restore_ram_forwards_resolved_explicit_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "forwarded.sav"
    args = make_args(
        action="backup-save",
        path=str(path),
        dmg_mbc="3",
        dmg_savetype="64k",
    )
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")

    assert cli._ResolveSaveConfiguration(args, dmg_header()) == (0x13, 0x02, 0)
    cli.BackupRestoreRAM(args, dmg_header())

    assert conn.transfer_calls == [
        {
            "mode": 2,
            "path": str(path),
            "mbc": 0x13,
            "save_type": 0x02,
            "rtc": False,
        },
    ]


@pytest.mark.parametrize("mode", ["UNKNOWN", ""])
def test_unknown_save_mode_refuses_through_real_backup_restore_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    original = b"unmodified save"
    path = tmp_path / "refused.sav"
    path.write_bytes(original)
    args = make_args(action="restore-save", path=str(path))
    cli = make_cli(tmp_path, args)
    conn = FakeConnection(mode)
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")

    assert cli._ResolveSaveConfiguration(args, dmg_header()) is None
    cli.BackupRestoreRAM(args, dmg_header())

    assert conn.transfer_calls == []
    assert path.read_bytes() == original


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

    assert conn.transfer_calls == [
        {
            "mode": 2,
            "path": str(tmp_path / "backup.sav"),
            "mbc": 0x13,
            "save_type": 3,
            "rtc": False,
        },
        {
            "mode": 3,
            "path": str(restore_path),
            "mbc": 0x13,
            "save_type": 3,
            "erase": False,
            "rtc": False,
            "verify_write": True,
            "cart_type": 0,
        },
        {
            "mode": 3,
            "path": str(tmp_path / "unused.sav"),
            "mbc": 0x13,
            "save_type": 3,
            "erase": True,
            "rtc": False,
            "cart_type": 0,
        },
    ]


def test_prepare_ereader_calibration_rejects_legacy_firmware(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection("AGB")
    conn.INFO["build_date"] = ""
    cli.CONN = conn

    result = cli._PrepareEReaderCalibration(
        make_args(action="restore-save"),
        str(tmp_path / "unused.sav"),
    )

    assert result == (False, None)
    assert conn.calls == []
    assert "not supported in Legacy Mode" in capsys.readouterr().out


def test_prepare_ereader_calibration_allows_absent_device_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection("AGB")
    conn.INFO["build_date"] = "2026-01-01"
    cli.CONN = conn

    result = cli._PrepareEReaderCalibration(
        make_args(action="restore-save"),
        str(tmp_path / "unused.sav"),
    )

    assert result == (True, None)
    assert conn.calls == [("read_header", True)]
    assert "No existing e-Reader calibration data found" in capsys.readouterr().out


def test_prepare_ereader_calibration_preserves_identical_save(
    tmp_path: Path,
) -> None:
    calibration = bytes([0xA5] * 0x2000)
    original = bytearray((index * 37 + 11) % 256 for index in range(0x20000))
    original[0xD000:0xF000] = calibration
    path = tmp_path / "matching-ereader.sav"
    path.write_bytes(original)
    cli = make_cli(tmp_path)
    conn = FakeConnection("AGB")
    conn.INFO.update(build_date="2026-01-01", ereader_calibration=calibration)
    cli.CONN = conn

    continue_write, buffer = cli._PrepareEReaderCalibration(
        make_args(action="restore-save", keep_calibration=True),
        str(path),
    )

    assert continue_write is True
    assert buffer == original
    assert conn.calls == [("read_header", True)]


@pytest.mark.parametrize("keep_calibration", [False, True])
def test_prepare_ereader_calibration_overwrites_or_keeps_exact_range(
    tmp_path: Path,
    keep_calibration: bool,
) -> None:
    calibration = bytes([0xA5] * 0x2000)
    original = bytearray((index * 37 + 11) % 256 for index in range(0x20000))
    expected = original.copy()
    if keep_calibration:
        expected[0xD000:0xF000] = calibration
    path = tmp_path / f"different-{keep_calibration}.sav"
    path.write_bytes(original)
    cli = make_cli(tmp_path)
    conn = FakeConnection("AGB")
    conn.INFO.update(build_date="2026-01-01", ereader_calibration=calibration)
    cli.CONN = conn
    args = make_args(action="restore-save", keep_calibration=keep_calibration)

    continue_write, buffer = cli._PrepareEReaderCalibration(args, str(path))

    assert continue_write is True
    assert buffer == expected
    assert buffer is not None
    assert buffer[:0xD000] == original[:0xD000]
    assert buffer[0xD000:0xF000] == expected[0xD000:0xF000]
    assert buffer[0xF000:] == original[0xF000:]
    assert args.action == "restore-save"


def test_prepare_ereader_erase_converts_to_buffered_restore_when_preserving(
    tmp_path: Path,
) -> None:
    calibration = bytes([0xA5] * 0x2000)
    cli = make_cli(tmp_path)
    conn = FakeConnection("AGB")
    conn.INFO.update(build_date="2026-01-01", ereader_calibration=calibration)
    cli.CONN = conn
    args = make_args(action="erase-save", keep_calibration=True)

    continue_write, buffer = cli._PrepareEReaderCalibration(
        args,
        str(tmp_path / "unused-erase-path.sav"),
    )

    expected = bytearray([0xFF] * 0x20000)
    expected[0xD000:0xF000] = calibration
    assert continue_write is True
    assert buffer == expected
    assert args.action == "restore-save"


def test_backup_restore_ram_preserves_ereader_calibration_in_erase_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calibration = bytes([0xA5] * 0x2000)
    args = make_args(
        action="erase-save",
        path=str(tmp_path / "unused-erase-path.sav"),
        keep_calibration=True,
    )
    cli = make_cli(tmp_path, args)
    conn = FakeConnection("AGB")
    conn.INFO.update(
        build_date="2026-01-01",
        ereader=True,
        ereader_calibration=calibration,
    )
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.sav")

    cli.BackupRestoreRAM(args, agb_header())

    expected = bytearray([0xFF] * 0x20000)
    expected[0xD000:0xF000] = calibration
    assert args.action == "restore-save"
    assert conn.transfer_calls == [
        {
            "mode": 3,
            "path": None,
            "mbc": 0,
            "save_type": 1,
            "erase": False,
            "rtc": False,
            "verify_write": True,
            "cart_type": 0,
            "buffer": expected,
        },
    ]


def test_backup_restore_ram_legacy_ereader_stops_before_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = bytes([0x3C] * 0x20000)
    path = tmp_path / "legacy-ereader.sav"
    path.write_bytes(original)
    args = make_args(action="restore-save", path=str(path), keep_calibration=True)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection("AGB")
    conn.INFO.update(build_date="", ereader=True)
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.sav")

    cli.BackupRestoreRAM(args, agb_header())

    assert conn.calls == []
    assert conn.transfer_calls == []
    assert path.read_bytes() == original


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


@pytest.mark.parametrize(
    ("mode", "mbc", "layout", "no_verify_write"),
    [("DMG", 0x19, 2, False), ("AGB", 0, None, True)],
)
def test_batteryless_restore_uses_real_region_and_profile_resolution(
    tmp_path: Path,
    mode: str,
    mbc: int,
    layout: int | None,
    no_verify_write: bool,
) -> None:
    original = b"batteryless save"
    path = tmp_path / f"{mode.lower()}-restore.sav"
    path.write_bytes(original)
    cli = make_cli(tmp_path)
    conn = FakeConnection(mode)
    profile = configure_batteryless_profile(conn, mode)
    cli.CONN = conn
    args = make_args(
        action="restore-save",
        path=str(path),
        flashcart_type=profile,
        bl_offset="0x1200",
        bl_size="16",
        bl_layout="2",
        no_verify_write=no_verify_write,
        compare_sectors=True,
    )

    cli._BatterylessSRAM(args, {}, mbc, 0x205, args.path)

    assert conn.transfer_calls == [
        expected_batteryless_write_args(
            path=str(path),
            mbc=mbc,
            offset=0x1200,
            size=16,
            layout=layout,
            verify_write=not no_verify_write,
            compare_sectors=True,
        ),
    ]
    assert path.read_bytes() == original


@pytest.mark.parametrize(("answer", "transfers"), [("y", 1), ("n", 0)])
def test_batteryless_restore_overwrite_prompt_controls_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    transfers: int,
) -> None:
    original = b"prompted restore"
    path = tmp_path / f"prompt-{answer}.sav"
    path.write_bytes(original)
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    profile = configure_batteryless_profile(conn, "DMG")
    cli.CONN = conn
    args = make_args(
        action="restore-save",
        path=str(path),
        flashcart_type=profile,
        overwrite=False,
        bl_offset="0x2000",
        bl_size="32",
        bl_layout="1",
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    cli._BatterylessSRAM(args, {}, 0x19, 0x205, args.path)

    assert len(conn.transfer_calls) == transfers
    if transfers:
        assert conn.transfer_calls == [
            expected_batteryless_write_args(
                path=str(path),
                mbc=0x19,
                offset=0x2000,
                size=32,
                layout=1,
                verify_write=True,
                compare_sectors=False,
            ),
        ]
    assert path.read_bytes() == original


def test_batteryless_restore_missing_input_stops_before_transfer(tmp_path: Path) -> None:
    path = tmp_path / "missing.sav"
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    profile = configure_batteryless_profile(conn, "DMG")
    cli.CONN = conn
    args = make_args(
        action="restore-save",
        path=str(path),
        flashcart_type=profile,
        bl_offset="0x3000",
        bl_size="8",
        bl_layout="0",
    )

    cli._BatterylessSRAM(args, {}, 0x19, 0x205, args.path)

    assert conn.transfer_calls == []
    assert not path.exists()


def test_batteryless_restore_permission_failure_preserves_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = b"permission protected"
    path = tmp_path / "permission.sav"
    path.write_bytes(original)
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    profile = configure_batteryless_profile(conn, "DMG")
    cli.CONN = conn
    args = make_args(
        action="restore-save",
        path=str(path),
        flashcart_type=profile,
        bl_offset="0x3000",
        bl_size="8",
        bl_layout="0",
    )

    def deny_open(_path: object, *_args: object, **_kwargs: object) -> None:
        raise PermissionError

    original_open = type(path).open
    monkeypatch.setattr(type(path), "open", deny_open)
    cli._BatterylessSRAM(args, {}, 0x19, 0x205, args.path)

    assert conn.transfer_calls == []
    with original_open(path, "rb") as input_file:
        assert input_file.read() == original


@pytest.mark.parametrize("failure", ["region", "profile", "stress", "erase-refusal"])
def test_batteryless_rejections_stop_before_transfer_and_preserve_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original = b"preserve rejected input"
    path = tmp_path / f"{failure}.sav"
    path.write_bytes(original)
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    profile = configure_batteryless_profile(conn, "DMG")
    cli.CONN = conn
    args = make_args(
        action="erase-save" if failure == "erase-refusal" else "restore-save",
        path=str(path),
        flashcart_type="Missing Profile" if failure == "profile" else profile,
        overwrite=failure != "erase-refusal",
        bl_offset="auto" if failure == "region" else "0x4000",
        bl_size="auto" if failure == "region" else "8",
        bl_layout="0",
    )
    if failure == "stress":
        args.action = "debug-test-save"

        def fail_if_resolved(_args: Namespace, _header: object) -> None:
            pytest.fail("stress-test rejection must precede region resolution")

        monkeypatch.setattr(cli, "_ResolveBLArgs", fail_if_resolved)
    elif failure == "erase-refusal":
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    cli._BatterylessSRAM(args, {}, 0x19, 0x205, args.path)

    assert conn.transfer_calls == []
    assert path.read_bytes() == original


def test_batteryless_erase_uses_exact_region_and_erased_buffer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    profile = configure_batteryless_profile(conn, "DMG")
    cli.CONN = conn
    args = make_args(
        action="erase-save",
        path=str(tmp_path / "unused.sav"),
        flashcart_type=profile,
        bl_offset="0x5000",
        bl_size="4",
        bl_layout="2",
        no_verify_write=True,
        compare_sectors=True,
        overwrite=False,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    cli._BatterylessSRAM(args, {}, 0x19, 0x205, args.path)

    assert conn.transfer_calls == [
        expected_batteryless_write_args(
            path=args.path,
            mbc=0x19,
            offset=0x5000,
            size=4,
            layout=2,
            verify_write=False,
            compare_sectors=True,
            erase=True,
        ),
    ]


@pytest.mark.parametrize("profile_values", [{"voltage": 3.3}, {"voltage_variants": [3.3, 5]}])
@pytest.mark.parametrize(("answer", "transfers"), [("n", 0), ("y", 1)])
def test_batteryless_fixed_voltage_warning_controls_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_values: dict[str, object],
    answer: str,
    transfers: int,
) -> None:
    original = b"fixed voltage restore"
    path = tmp_path / f"fixed-{answer}-{len(profile_values)}.sav"
    path.write_bytes(original)
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    conn.INFO.update(voltage_autoswitch=True, voltage_code=False)
    profile = configure_batteryless_profile(conn, "DMG", **profile_values)
    cli.CONN = conn
    args = make_args(
        action="restore-save",
        path=str(path),
        flashcart_type=profile,
        bl_offset="0x6000",
        bl_size="8",
        bl_layout="1",
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    cli._BatterylessSRAM(args, {}, 0x19, 0x205, args.path)

    assert len(conn.transfer_calls) == transfers
    if transfers:
        assert conn.transfer_calls == [
            expected_batteryless_write_args(
                path=str(path),
                mbc=0x19,
                offset=0x6000,
                size=8,
                layout=1,
                verify_write=True,
                compare_sectors=False,
            ),
        ]
    assert path.read_bytes() == original


def test_batteryless_voltage_capable_device_skips_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = b"voltage capable restore"
    path = tmp_path / "voltage-capable.sav"
    path.write_bytes(original)
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    conn.INFO.update(voltage_autoswitch=True, voltage_code=True)
    profile = configure_batteryless_profile(conn, "DMG", voltage=3.3)
    cli.CONN = conn
    args = make_args(
        action="restore-save",
        path=str(path),
        flashcart_type=profile,
        bl_offset="0x7000",
        bl_size="8",
        bl_layout="0",
    )

    def fail_if_prompted(_prompt: str) -> str:
        pytest.fail("voltage-capable devices must not display a voltage confirmation")

    monkeypatch.setattr("builtins.input", fail_if_prompted)
    cli._BatterylessSRAM(args, {}, 0x19, 0x205, args.path)

    assert conn.transfer_calls == [
        expected_batteryless_write_args(
            path=str(path),
            mbc=0x19,
            offset=0x7000,
            size=8,
            layout=0,
            verify_write=True,
            compare_sectors=False,
        ),
    ]
    assert "Warning: A 3.3V flashcart profile" not in capsys.readouterr().out
    assert path.read_bytes() == original


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


@pytest.mark.parametrize(("result", "expected"), [(1, True), (3, False), (0, False)])
def test_gbflash_firmware_update_handles_status_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: int,
    expected: bool,
) -> None:
    cli = make_cli(tmp_path)
    monkeypatch.setattr(cli, "_LoadFirmwareInfo", lambda _path: ("1.0", 123))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    updated_ports: list[str] = []

    class FirmwareUpdater:
        def __init__(self, *, port: str) -> None:
            updated_ports.append(port)

        def WriteFirmware(self, file_name: Path, callback: object) -> int:
            del file_name, callback
            return result

    package = importlib.import_module("FlashGBX")
    monkeypatch.setattr(package, "hw_GBFlash", SimpleNamespace(FirmwareUpdater=FirmwareUpdater), raising=False)

    assert cli.UpdateFirmwareGBFlash(port="mock-port") is expected
    assert updated_ports == ["mock-port"]


@pytest.mark.parametrize(
    ("answer", "member", "result", "expected"),
    [
        ("1", "FIRMWARE_LK.JR", 1, True),
        ("2", "FIRMWARE_MSC.JR", 3, False),
        ("3", "FIRMWARE_JOEYGUI.JR", 0, False),
    ],
)
def test_joeyjr_firmware_update_reads_the_selected_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    member: str,
    result: int,
    expected: bool,
) -> None:
    resource_path = tmp_path / "res"
    resource_path.mkdir()
    archive_path = resource_path / "fw_JoeyJr.zip"
    payloads = {
        "FIRMWARE_LK.JR": b"lk firmware",
        "FIRMWARE_MSC.JR": b"msc firmware",
        "FIRMWARE_JOEYGUI.JR": b"joeygui firmware",
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("fw.ini", "[Firmware]\n")
        for name, payload in payloads.items():
            archive.writestr(name, payload)

    cli = make_cli(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)
    updates: list[tuple[str, bytes]] = []

    class FirmwareUpdater:
        def __init__(self, *, port: str) -> None:
            self.port = port

        def WriteFirmware(self, firmware: bytearray, _callback: object) -> int:
            updates.append((self.port, bytes(firmware)))
            return result

    package = importlib.import_module("FlashGBX")
    monkeypatch.setattr(package, "hw_JoeyJr", SimpleNamespace(FirmwareUpdater=FirmwareUpdater), raising=False)

    assert cli.UpdateFirmwareJoeyJr(port="mock-port") is expected
    assert updates == [("mock-port", payloads[member])]


def test_joeyjr_firmware_update_retries_serial_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_path = tmp_path / "res"
    resource_path.mkdir()
    archive_path = resource_path / "fw_JoeyJr.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("fw.ini", "[Firmware]\n")
        archive.writestr("FIRMWARE_LK.JR", b"selected firmware")

    cli = make_cli(tmp_path)
    answers: Iterator[str] = iter(["1", "second-port"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    attempts: list[str] = []

    class FirmwareUpdater:
        def __init__(self, *, port: str) -> None:
            self.port = port

        def WriteFirmware(self, firmware: bytearray, _callback: object) -> int:
            assert firmware == b"selected firmware"
            attempts.append(self.port)
            if len(attempts) == 1:
                raise SerialException
            return 1

    package = importlib.import_module("FlashGBX")
    monkeypatch.setattr(package, "hw_JoeyJr", SimpleNamespace(FirmwareUpdater=FirmwareUpdater), raising=False)

    assert cli.UpdateFirmwareJoeyJr(port="first-port") is True
    assert attempts == ["first-port", "second-port"]


@pytest.mark.parametrize("answer", ["0", "4", "cancel"])
def test_joeyjr_firmware_update_rejects_invalid_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    resource_path = tmp_path / "res"
    resource_path.mkdir()
    with zipfile.ZipFile(resource_path / "fw_JoeyJr.zip", "w") as archive:
        archive.writestr("fw.ini", "[Firmware]\n")
    cli = make_cli(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": answer)

    assert cli.UpdateFirmwareJoeyJr(port="mock-port") is False


def test_joeyjr_firmware_update_reports_no_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_path = tmp_path / "res"
    resource_path.mkdir()
    with zipfile.ZipFile(resource_path / "fw_JoeyJr.zip", "w") as archive:
        archive.writestr("fw.ini", "[Firmware]\n")
    cli = make_cli(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "1")

    assert cli.UpdateFirmwareJoeyJr() is False


def test_run_standalone_firmware_action_selects_matching_fake_updater(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="update-matching", device_port="requested-port")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    cli.CONN = conn
    updater_calls: list[dict[str, object]] = []

    class UnmatchedDevice:
        def SupportsFirmwareUpdates(self) -> bool:
            return True

        def FirmwareUpdateAction(self) -> str:
            return "update-other"

        def CLIUpdaterMethod(self) -> str:
            return "RunFakeUpdater"

    class MatchingDevice(UnmatchedDevice):
        def FirmwareUpdateAction(self) -> str:
            return "update-matching"

    monkeypatch.setattr(
        cli_module,
        "HW_DEVICES",
        [SimpleNamespace(GbxDevice=UnmatchedDevice), SimpleNamespace(GbxDevice=MatchingDevice)],
    )
    monkeypatch.setattr(
        cli,
        "RunFakeUpdater",
        lambda **kwargs: updater_calls.append(kwargs),
        raising=False,
    )

    assert cli._RunStandaloneAction(args, {"update-matching"}) == 0

    assert updater_calls == [{"port": "requested-port"}]
    assert not any(name == "read_header" for name, _value in conn.calls)


def test_run_standalone_unmatched_firmware_action_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="update-missing")
    cli = make_cli(tmp_path, args)
    updater_calls: list[dict[str, object]] = []

    class OtherDevice:
        def SupportsFirmwareUpdates(self) -> bool:
            return True

        def FirmwareUpdateAction(self) -> str:
            return "update-other"

        def CLIUpdaterMethod(self) -> str:
            return "RunFakeUpdater"

    monkeypatch.setattr(cli_module, "HW_DEVICES", [SimpleNamespace(GbxDevice=OtherDevice)])
    monkeypatch.setattr(
        cli,
        "RunFakeUpdater",
        lambda **kwargs: updater_calls.append(kwargs),
        raising=False,
    )

    assert cli._RunStandaloneAction(args, {"update-missing"}) is None
    assert updater_calls == []


def test_run_canceled_interactive_menu_stops_before_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action=None, mode=None)
    cli = make_cli(tmp_path, args)
    original_stdout = sys.stdout
    monkeypatch.setattr(cli_module, "Logger", lambda: original_stdout)
    monkeypatch.setattr(cli_module, "HW_DEVICES", [])
    monkeypatch.setattr(cli, "_SelectMenuAction", lambda _items: None)
    connection_attempts: list[bool] = []
    monkeypatch.setattr(
        cli,
        "_connect_for_action",
        lambda _args, _actions: connection_attempts.append(True),
    )

    assert cli.run() == 0

    assert cli.ARGS["called_with_args"] is False
    assert connection_attempts == []


def test_run_rejects_device_with_no_supported_platforms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="interactive", mode=None)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.supported_modes = []
    console = configure_interactive_run(cli, conn, monkeypatch)

    assert cli.run() == 1

    assert console.call_count == 0
    assert ("close", True) in conn.calls
    assert not any(name == "read_header" for name, _value in conn.calls)


def test_run_uses_only_supported_platform_without_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="interactive", mode=None)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection("DMG")
    conn.supported_modes = ["DMG"]
    console = configure_interactive_run(cli, conn, monkeypatch)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("Platform prompt opened")),
    )

    assert cli.run() == 0

    assert args.mode == "dmg"
    assert ("mode", "DMG") in conn.calls
    assert console.call_count == 1
    assert ("close", True) in conn.calls
    assert not any(name == "read_header" for name, _value in conn.calls)


@pytest.mark.parametrize(
    ("use_switch", "detected_mode", "expected_arg", "expected_device_mode"),
    [
        (True, "unsupported", "agb", "AGB"),
        (False, "DMG", "dmg", "DMG"),
    ],
)
def test_run_uses_switch_or_device_platform_autodetection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_switch: bool,
    detected_mode: str,
    expected_arg: str,
    expected_device_mode: str,
) -> None:
    args = make_args(action="interactive", mode=None)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.supported_modes = ["DMG", "AGB"]
    conn.mode = detected_mode
    if use_switch:
        conn.FW["cart_mode_switch"] = True
        conn.INFO["switch_state"] = 1
    console = configure_interactive_run(cli, conn, monkeypatch)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("Platform prompt opened")),
    )

    assert cli.run() == 0

    assert args.mode == expected_arg
    assert ("mode", expected_device_mode) in conn.calls
    assert console.call_count == 1
    assert ("close", True) in conn.calls
    assert not any(name == "read_header" for name, _value in conn.calls)


@pytest.mark.parametrize(
    ("answer", "expected_arg", "expected_device_mode"),
    [
        ("1", "dmg", "DMG"),
        ("2", "agb", "AGB"),
        ("", "agb", "AGB"),
        ("invalid", None, None),
    ],
)
def test_run_platform_prompt_handles_dmg_agb_default_and_invalid_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    expected_arg: str | None,
    expected_device_mode: str | None,
) -> None:
    args = make_args(action="interactive", mode=None)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.supported_modes = ["DMG", "AGB"]
    conn.mode = "unsupported"
    console = configure_interactive_run(cli, conn, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert cli.run() == 0

    assert args.mode == expected_arg
    assert console.call_count == (0 if expected_arg is None else 1)
    if expected_device_mode is None:
        assert not any(name == "mode" for name, _value in conn.calls)
    else:
        assert ("mode", expected_device_mode) in conn.calls
    assert ("close", True) in conn.calls
    assert not any(name == "read_header" for name, _value in conn.calls)


def test_run_interactive_console_keyboard_interrupt_disconnects_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="interactive", mode="dmg")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    configure_interactive_run(cli, conn, monkeypatch)

    def interrupt_console() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "InteractiveConsole", interrupt_console)

    assert cli.run() == 0

    assert ("mode", "DMG") in conn.calls
    assert ("close", True) in conn.calls
    assert not any(name == "read_header" for name, _value in conn.calls)


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


@pytest.mark.parametrize("action", ["restore-save", "flash-rom"])
def test_run_canceled_file_prompt_disconnects_without_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    args = make_args(action=action, path="auto")
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
    monkeypatch.setattr(cli, "ReadCartridge", lambda header: (False, "header", header))
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    assert cli.run() == 0

    assert conn.transfer_calls == []
    assert ("close", True) in conn.calls
    assert args.path == ""


def test_run_connection_failure_does_not_read_or_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path, make_args(action="flash-rom"))
    conn = FakeConnection()
    cli.DEVICE = ("Mock Reader", conn)
    cli.CONN = conn
    output = sys.stdout
    monkeypatch.setattr(cli_module, "Logger", lambda: output)
    monkeypatch.setattr(cli_module, "HW_DEVICES", [])
    monkeypatch.setattr(cli, "FindDevices", lambda *, port: port is None)
    monkeypatch.setattr(cli, "ConnectDevice", lambda: False)

    assert cli.run() == 1

    assert conn.transfer_calls == []
    assert not any(name == "read_header" for name, _value in conn.calls)


def test_run_invalid_cartridge_header_stops_before_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path, make_args(action="backup-rom"))
    conn = FakeConnection()
    conn.header = dmg_header()
    cli.DEVICE = ("Mock Reader", conn)
    cli.CONN = conn
    output = sys.stdout
    monkeypatch.setattr(cli_module, "Logger", lambda: output)
    monkeypatch.setattr(cli_module, "HW_DEVICES", [])
    monkeypatch.setattr(cli, "FindDevices", lambda *, port: port is None)
    monkeypatch.setattr(cli, "ConnectDevice", lambda: True)
    monkeypatch.setattr(cli, "ReadCartridge", lambda header: (True, "invalid header", header))

    assert cli.run() == 1

    assert conn.transfer_calls == []
    assert ("close", True) in conn.calls


def test_run_transfer_abort_returns_failure_and_disconnects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = make_args(action="backup-save", path=str(tmp_path / "backup.sav"))
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.header = dmg_header()
    conn.INFO = dmg_header()
    cli.DEVICE = ("Mock Reader", conn)
    cli.CONN = conn
    output = sys.stdout
    monkeypatch.setattr(cli_module, "Logger", lambda: output)
    monkeypatch.setattr(cli_module, "HW_DEVICES", [])
    monkeypatch.setattr(cli, "FindDevices", lambda *, port: port is None)
    monkeypatch.setattr(cli, "ConnectDevice", lambda: True)
    monkeypatch.setattr(cli, "ReadCartridge", lambda header: (False, "header", header))

    def aborting_transfer(*, args: dict[str, Any], signal: Callable[[dict[str, object]], None]) -> bool:
        conn.transfer_calls.append(args)
        signal({"action": "ABORT", "info_type": "msgbox_critical", "info_msg": "Transfer failed"})
        return False

    monkeypatch.setattr(conn, "TransferData", aborting_transfer)

    assert cli.run() == 1

    assert len(conn.transfer_calls) == 1
    assert conn.transfer_calls[0]["mode"] == 2
    assert ("close", True) in conn.calls


def test_backup_rom_overwrite_refusal_preserves_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "backup.gb"
    output_path.write_bytes(b"original backup")
    args = make_args(action="backup-rom", path=str(output_path), overwrite=False)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    cli.BackupROM(args, dmg_header())

    assert output_path.read_bytes() == b"original backup"
    assert conn.transfer_calls == []


def test_flash_rom_unsafe_voltage_refusal_does_not_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rom_path = tmp_path / "input.gb"
    rom_path.write_bytes(bytes(0x1000))
    args = make_args(action="flash-rom", path=str(rom_path), flashcart_type="3V Profile")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.INFO["voltage_autoswitch"] = True
    conn.INFO["voltage_code"] = False
    conn.INFO["dmg_carts"] = (
        ["Generic", "3V Profile"],
        [{}, {"type": "DMG", "names": ["3V Profile"], "voltage": 3.3, "commands": {}}],
    )
    cli.CONN = conn
    monkeypatch.setattr(
        cli_module,
        "RomFileDMG",
        lambda _buffer: SimpleNamespace(GetHeader=lambda: {"logo_correct": True, "header_checksum_correct": True}),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    cli.FlashROM(args, dmg_header())

    assert rom_path.read_bytes() == bytes(0x1000)
    assert conn.transfer_calls == []


@pytest.mark.parametrize("action", ["backup-save", "restore-save", "erase-save"])
def test_save_overwrite_refusal_does_not_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    save_path = tmp_path / "existing.sav"
    save_path.write_bytes(b"existing save")
    args = make_args(action=action, path=str(save_path), overwrite=False)
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    cli.BackupRestoreRAM(args, dmg_header())

    assert save_path.read_bytes() == b"existing save"
    assert conn.transfer_calls == []


def test_restore_save_invalid_type_stops_before_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_path = tmp_path / "restore.sav"
    save_path.write_bytes(b"save")
    args = make_args(action="restore-save", path=str(save_path), dmg_savetype="unknown")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    cli.CONN = conn
    monkeypatch.setattr(cli_module, "generate_filename", lambda **_kwargs: "generated.gb")

    cli.BackupRestoreRAM(args, dmg_header())

    assert conn.transfer_calls == []
    assert save_path.read_bytes() == b"save"


def test_flash_verification_failure_reports_broken_sectors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection()
    conn.INFO = {"last_action": 4, "transferred": 0x2000, "broken_sectors": [[0x1000, 0x1000]]}
    cli.CONN = conn
    cli.PROGRESS.PROGRESS["verified"] = False

    cli.FinishOperation()

    assert cli.RETVAL == 1
    assert conn.INFO["last_action"] == 0
    output = capsys.readouterr().out
    assert "verification" in output.lower()
    assert "0x1000" in output.lower()
    assert "verified successfully" not in output.lower()


@pytest.mark.parametrize(("rolls", "expected_count"), [(1, 32), (8, 256)])
def test_finish_backup_ram_exports_camera_rolls_to_expected_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rolls: int,
    expected_count: int,
) -> None:
    save_path = tmp_path / "camera.sav"
    save_path.write_bytes(b"".join(bytes([roll]) * 0x20000 for roll in range(rolls)))
    args = make_args(action="backup-save", gbcamera_extract=True, gbcamera_outfile_format="bmp")
    cli = make_cli(tmp_path, args)
    conn = FakeConnection()
    conn.INFO = {
        "last_action": 2,
        "mapper_raw": 252,
        "transferred": 0x20000 * rolls,
        "last_path": str(save_path),
        "dump_info": {"header": {"ram_size_raw": 0x204 if rolls == 8 else 3}},
    }
    cli.CONN = conn
    loaded: list[object] = []
    exported: list[tuple[int, Path, int]] = []
    palettes: list[int] = []

    class FakeCamera:
        PALETTE_NAMES = PocketCamera.PALETTE_NAMES

        def LoadFile(self, source: object) -> bool:
            loaded.append(source)
            return True

        def SetPalette(self, palette: int) -> None:
            palettes.append(palette)

        def ExportPicture(self, index: int, path: Path, *, scale: int) -> None:
            exported.append((index, path, scale))

    monkeypatch.setattr(cli_module, "PocketCamera", FakeCamera)

    cli._FinishBackupRAM()

    assert cli.RETVAL == 0
    assert conn.INFO["last_action"] == 0
    assert len(loaded) == rolls
    if rolls == 1:
        assert loaded == [str(save_path)]
    else:
        assert loaded == [bytearray(bytes([roll]) * 0x20000) for roll in range(rolls)]
    assert len(exported) == expected_count
    assert {scale for _index, _path, scale in exported} == {1}
    assert palettes == [0]
    destination = save_path.with_suffix("")
    assert destination.is_dir()
    assert all(path.parent == destination for _index, path, _scale in exported)
    assert [index for index, _path, _scale in exported] == list(range(32)) * rolls
    assert exported[0][1].name == ("IMG_PC00.bmp" if rolls == 1 else "IMG_P100.bmp")
    assert exported[-1][1].name == ("IMG_PC31.bmp" if rolls == 1 else "IMG_P831.bmp")


def test_finish_backup_ram_does_not_extract_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = make_cli(tmp_path, make_args(gbcamera_extract=False))
    conn = FakeConnection()
    conn.INFO = {
        "last_action": 2,
        "mapper_raw": 252,
        "transferred": 0x20000,
        "last_path": str(tmp_path / "camera.sav"),
        "dump_info": {"header": {}},
    }
    cli.CONN = conn

    def unexpected_camera() -> None:
        msg = "Camera extraction was disabled"
        raise AssertionError(msg)

    monkeypatch.setattr(cli_module, "PocketCamera", unexpected_camera)

    cli._FinishBackupRAM()

    assert cli.RETVAL == 0
    assert conn.INFO["last_action"] == 0
    assert not (tmp_path / "camera").exists()


@pytest.mark.parametrize("rolls", [1, 8])
def test_finish_backup_ram_destination_collision_stops_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rolls: int,
) -> None:
    save_path = tmp_path / "camera.sav"
    save_path.write_bytes(bytes(0x20000 * rolls))
    destination = save_path.with_suffix("")
    destination.write_bytes(b"existing destination")
    cli = make_cli(tmp_path, make_args(gbcamera_extract=True))
    conn = FakeConnection()
    conn.INFO = {
        "last_action": 2,
        "mapper_raw": 252,
        "transferred": 0x20000 * rolls,
        "last_path": str(save_path),
        "dump_info": {"header": {"ram_size_raw": 0x204 if rolls == 8 else 3}},
    }
    cli.CONN = conn
    exports: list[Path] = []

    class FakeCamera:
        PALETTE_NAMES = PocketCamera.PALETTE_NAMES

        def LoadFile(self, _source: object) -> bool:
            return True

        def SetPalette(self, _palette: int) -> None:
            pass

        def ExportPicture(self, _index: int, path: Path, *, scale: int) -> None:
            del scale
            exports.append(path)

    monkeypatch.setattr(cli_module, "PocketCamera", FakeCamera)

    cli._FinishBackupRAM()

    assert cli.RETVAL == 1
    assert conn.INFO["last_action"] == 0
    assert destination.read_bytes() == b"existing destination"
    assert exports == []


@pytest.mark.parametrize(
    ("power_cycle", "fail_backup", "mode"),
    [(True, False, "DMG"), (False, False, "DMG"), (True, True, "DMG"), (False, False, "AGB"), (True, True, "AGB")],
)
def test_debug_save_test_restores_data_and_orders_optional_power_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    power_cycle: bool,
    fail_backup: bool,
    mode: str,
) -> None:
    cli = make_cli(tmp_path)
    conn = FakeConnection(mode)
    cli.CONN = conn
    monkeypatch.setattr(cli_module.AppContext, "CONFIG_PATH", str(tmp_path))
    monkeypatch.setattr(cli_module.os, "urandom", lambda size: b"\xa5" * size)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    initial_data = bytes(index % 256 for index in range(512))
    cartridge_data = bytearray(initial_data)
    events: list[tuple[str, object]] = []
    transfer_calls: list[dict[str, Any]] = []

    def transfer(*, args: dict[str, Any], signal: object) -> bool:
        del signal
        transfer_calls.append(args)
        mode = args["mode"]
        path = Path(str(args["path"]))
        events.append(("transfer", (mode, path.name)))
        if len(transfer_calls) == 1 and fail_backup:
            return False
        if mode == 2:
            path.write_bytes(cartridge_data)
        else:
            cartridge_data[:] = path.read_bytes()
        return True

    monkeypatch.setattr(conn, "TransferData", transfer)
    monkeypatch.setattr(conn, "CanPowerCycleCart", lambda: power_cycle)
    monkeypatch.setattr(conn, "CartPowerCycle", lambda: events.append(("power", "cycle")))
    monkeypatch.setattr(
        conn,
        "ReadHeader",
        lambda *, checkRtc=True: events.append(("header", checkRtc)) or {},
    )
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: events.append(("sleep", seconds)))

    cli._DebugTestSave(mbc=0x02, save_type=1 if mode == "AGB" else 0x02)

    expected_names = ["test1.bin"] if fail_backup else ["test1.bin", "test2.bin", "test3.bin", "test4.bin", "test1.bin"]
    assert [Path(str(args["path"])).name for args in transfer_calls] == expected_names
    assert [args["mode"] for args in transfer_calls] == ([2] if fail_backup else [2, 3, 2, 2, 3])
    expected_save_type = 1 if mode == "AGB" else 0x02
    assert all(args["mbc"] == 0x02 and args["save_type"] == expected_save_type for args in transfer_calls)

    if fail_backup:
        assert "Done! The writable save data size" not in capsys.readouterr().out
        assert cartridge_data == initial_data
        assert not (tmp_path / "test1.bin").exists()
        assert events == [("transfer", (2, "test1.bin"))]
        return

    output = capsys.readouterr().out
    if mode == "AGB":
        assert "Done! The writable save data size using save type" in output
    else:
        assert "Done! The writable save data size is" in output

    assert (tmp_path / "test1.bin").read_bytes() == initial_data
    assert cartridge_data == initial_data
    power_events = [event for event in events if event[0] == "power"]
    header_events = [event for event in events if event[0] == "header"]
    assert len(power_events) == (5 if power_cycle else 0)
    assert header_events == ([("header", False)] if power_cycle else [])
    transfer_positions = [index for index, event in enumerate(events) if event[0] == "transfer"]
    if power_cycle:
        power_positions = [index for index, event in enumerate(events) if event[0] == "power"]
        header_position = next(index for index, event in enumerate(events) if event[0] == "header")
        delay_position = events.index(("sleep", 0.2))
        assert transfer_positions[2] < power_positions[0] < power_positions[-1] < header_position
        assert header_position < delay_position < transfer_positions[3]
    else:
        assert not any(event[0] == "header" for event in events)
