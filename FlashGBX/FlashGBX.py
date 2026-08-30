# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import platform
import re
import sys
import time
import traceback
import zipfile
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from .app import HW_DEVICES, AppContext, AppInfo
from .CartridgeTypes import AgbSaveTypes, DmgSaveTypes, RomSizes
from .Flashcart import empty_flashcarts_map
from .i18n import __, c__, init_language
from .IniSettings import IniSettings
from .Logging import ANSI, logger
from .PocketCamera import PocketCamera

if TYPE_CHECKING:
    from .Flashcart import FlashcartMap

ConfigVersion = str | Literal[False] | None
ConfigMessage = list[int | str]
FlashcartProfile = dict[str, Any]
PlatformMode = Literal["DMG", "AGB"]


class BaseArgs(TypedDict):
    app_path: str
    config_path: str
    argparsed: argparse.Namespace


class ConfigLoadResult(TypedDict):
    flashcarts: FlashcartMap
    config_ret: list[ConfigMessage]


class StartupArgs(BaseArgs, ConfigLoadResult):
    pass


class ConfigPaths(TypedDict):
    subdir: str
    appdata: str


STATIC_ACTIONS: list[str] = [
    "info",
    "backup-rom",
    "flash-rom",
    "backup-save",
    "restore-save",
    "erase-save",
    "gbcamera-extract",
    "interactive",
    "debug-test-save",
]


def _get_firmware_update_actions() -> list[str]:
    actions: list[str] = []
    for hardware_module in HW_DEVICES:
        try:
            device = hardware_module.GbxDevice()
            if device.SupportsFirmwareUpdates():
                action = device.FirmwareUpdateAction()
                if isinstance(action, str):
                    actions.append(action)
        except Exception as exc:
            logger.exception("Failed to inspect a hardware backend for firmware-update support: {}", exc)
    return actions


def _parse_macos_version(version: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0, 0)
    return parsed or (0, 0)


def _enable_windows_ansi() -> None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # pyright: ignore[reportAttributeAccessIssue]
        output_handle = kernel32.GetStdHandle(-11)
        console_mode = ctypes.c_uint()
        if output_handle not in (0, -1) and kernel32.GetConsoleMode(output_handle, ctypes.byref(console_mode)):
            kernel32.SetConsoleMode(output_handle, console_mode.value | 0x0004)
    except (AttributeError, OSError):
        logger.exception("Failed to enable Windows virtual-terminal output")


def _configure_platform_environment(system: str | None = None) -> None:
    current_system = platform.system() if system is None else system
    if current_system == "Windows":
        _enable_windows_ansi()
    elif current_system == "Darwin" and _parse_macos_version(platform.mac_ver()[0]) < (12, 0):
        os.environ["QT_MAC_WANTS_LAYER"] = "1"


def _backup_path(path: Path) -> Path:
    timestamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d%H%M%S")
    return path.with_name(f"{path.name}_{timestamp}.bak")


def _archive_destination(config_path: Path, member_name: str) -> Path | None:
    destination = (config_path / member_name).resolve()
    try:
        destination.relative_to(config_path.resolve())
    except ValueError:
        return None
    return destination


def _flashcart_profile(raw_profile: object) -> tuple[PlatformMode, list[str], FlashcartProfile] | None:
    if not isinstance(raw_profile, Mapping):
        return None
    cart_type = raw_profile.get("type")
    raw_names = raw_profile.get("names")
    if cart_type not in ("DMG", "AGB") or not isinstance(raw_names, list):
        return None
    names = [name for name in raw_names if isinstance(name, str) and name]
    if not names:
        return None
    return cast("PlatformMode", cart_type), names, copy.deepcopy(dict(raw_profile))


FWUPDATE_ACTIONS = _get_firmware_update_actions()
ALL_ACTIONS = STATIC_ACTIONS + FWUPDATE_ACTIONS


def ReadConfigFiles(args: BaseArgs) -> tuple[ConfigVersion, list[Path]]:
    reset = bool(args["argparsed"].reset)
    config_path = Path(args["config_path"])
    settings_path = config_path / "settings.ini"
    settings = IniSettings(path=settings_path)
    raw_config_version = settings.value("ConfigVersion")
    config_version: ConfigVersion = raw_config_version if isinstance(raw_config_version, str) else None
    config_path.mkdir(parents=True, exist_ok=True)
    fc_files = list(config_path.glob("fc_*.txt"))
    if config_version is not None and len(fc_files) == 0:
        print(
            __(
                "No flashcart profile files found in {config_path}. Resetting configuration...",
                config_path=args["config_path"],
            ),
        )
        settings.clear()
        settings_path.rename(_backup_path(settings_path))
        settings = IniSettings(path=settings_path)
        config_version = False  # extracts the config.zip again
    elif reset:
        settings.clear()
        print(__("All configuration has been reset."))

    if config_version != AppInfo.VERSION:
        settings.setValue("UpdateCheck", None, quiet=True)
    settings.setValue("ConfigVersion", AppInfo.VERSION, quiet=True)
    return config_version, fc_files


def LoadConfig(args: BaseArgs) -> ConfigLoadResult:
    app_path = Path(args["app_path"])
    config_path = Path(args["config_path"])
    ret: list[ConfigMessage] = []
    flashcarts = empty_flashcarts_map()

    # Settings and Config
    config_version, fc_files = ReadConfigFiles(args=args)
    if config_version != AppInfo.VERSION:
        # Rename old files that have since been replaced/renamed/merged
        deprecated_files = [
            "fc_AGB_TEST.txt",
            "fc_DMG_TEST.txt",
            "fc_AGB_Nintendo_E201850.txt",
            "fc_AGB_Nintendo_E201868.txt",
            "config.ini",
            "fc_DMG_MX29LV320ABTC.txt",
            "fc_DMG_iG_4MB_MBC3_RTC.txt",
            "fc_AGB_Flash2Advance.txt",
            "fc_AGB_MX29LV640_AUDIO.txt",
            "fc_AGB_M36L0R7050T.txt",
            "fc_AGB_M36L0R8060B.txt",
            "fc_AGB_M36L0R8060T.txt",
            "fc_AGB_iG_32MB_S29GL512N.txt",
            "fc_DMG_SST39SF010_MBC1_AUDIO.txt",
            "fc_DMG_SST39SF040_MBC5_AUDIO.txt",
            "fc_DMG_AM29F010_MBC1_AUDIO.txt",
            "fc_DMG_AM29F040_MBC1_AUDIO.txt",
            "fc_DMG_AM29F040_MBC1_WR.txt",
            "fc_DMG_AM29F080_MBC1_AUDIO.txt",
            "fc_DMG_AM29F080_MBC1_WR.txt",
            "fc_DMG_SST39SF040_MBC1_AUDIO.txt",
            "fc_DMG_SST39SF020_MBC1_AUDIO.txt",
            "fc_DMG_29LV016T.txt",
            "fc_DMG_Retrostage.txt",
        ]
        for file in deprecated_files:
            deprecated_path = config_path / file
            if deprecated_path.exists():
                deprecated_path.rename(_backup_path(deprecated_path))

        replaced_files: list[str] = []
        config_zip_path = app_path / "res" / "config.zip"
        if config_zip_path.exists():
            try:
                with zipfile.ZipFile(config_zip_path) as zips:
                    for zfile in zips.namelist():
                        extracted_path = _archive_destination(config_path, zfile)
                        if extracted_path is None:
                            ret.append([2, f"The configuration archive contains an unsafe path: {zfile}"])
                            continue
                        if extracted_path.exists():
                            zfile_crc = zips.getinfo(zfile).CRC
                            buffer = extracted_path.read_bytes()
                            ofile_crc = zlib.crc32(buffer) & 0xFFFFFFFF
                            if zfile_crc == ofile_crc:
                                continue
                            extracted_path.rename(_backup_path(extracted_path))
                            replaced_files.append(zfile)
                        zips.extract(zfile, config_path)
            except zipfile.BadZipFile:
                print(__("Warning: config.zip is corrupted and could not be read."))

            if replaced_files:
                ret.append(
                    [
                        1,
                        __(
                            "The application was recently updated and some flashcart profile files have been updated as well. You will find backup copies of them in your configuration directory.",
                        )
                        + "\n\n"
                        + __("Updated files:")
                        + "\n"
                        + "\n".join(replaced_files),
                    ],
                )
            fc_files = list(config_path.glob("fc_*.txt"))
        else:
            print(
                __(
                    "Warning: {config_zip_file} not found. This is required to load new flashcart profile configurations after updating.",
                    config_zip_file=str(config_zip_path),
                ),
            )

    # Read flash cart types
    for file in fc_files:
        file_path = Path(file)
        if file_path.exists():
            try:
                data = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                ret.append([2, f"The flashchip type file “{file_path.name}” could not be read.\n\nError: {exc}"])
                continue
            else:
                specs_int = re.sub(
                    r"(0x[0-9A-Fa-f]+)",
                    lambda m: str(int(m.group(1), 16)),
                    data,
                )  # hex numbers to int numbers, otherwise not valid json
                try:
                    raw_specs: object = json.loads(specs_int)
                except (json.JSONDecodeError, ValueError) as exc:
                    ret.append(
                        [
                            2,
                            f"The flashchip type file “{file_path.name:s}” could not be parsed and needs to be fixed before it can be used.\n\nError: {exc}",
                        ],
                    )
                    continue
                profile_data = _flashcart_profile(raw_specs)
                if profile_data is None:
                    continue
                cart_type, names, specs = profile_data
                for name in names:
                    temp = copy.deepcopy(specs)
                    temp["names"] = [name]
                    flashcarts[cart_type][name] = temp

    return {"flashcarts": flashcarts, "config_ret": ret}


class ArgParseCustomFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def main(portableMode: bool = False) -> int | None:
    _configure_platform_environment()
    AppContext.LAUNCH_TIMESTAMP = time.time()

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        app_path = str(Path(sys.executable).parent)
    else:
        app_path = str(Path(__file__).resolve().parent)

    try:
        from PySide6 import QtCore  # pyright: ignore[reportMissingImports]

        cp: ConfigPaths = {
            "subdir": str(Path(app_path) / "config"),
            "appdata": str(
                Path(
                    QtCore.QDir.toNativeSeparators(
                        QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.AppConfigLocation),
                    ),
                )
                / "FlashGBX",
            ),
        }
    except Exception as e:
        logger.exception(f"Failed to import PySide: {e}")
        cp = {
            "subdir": str(Path(app_path) / "config"),
            "appdata": str(Path.home() / "FlashGBX"),
        }

    cfgdir_default = "subdir" if portableMode else "appdata"

    config_path: str | None = None
    language_choice: str | None = None
    for i, arg in enumerate(sys.argv):
        if arg == "--cfgdir" and i + 1 < len(sys.argv):
            cfgdir_choice = sys.argv[i + 1].lower()
            if cfgdir_choice in cp:
                config_path = cp[cfgdir_choice]
        elif arg == "--language" and i + 1 < len(sys.argv):
            language_choice = sys.argv[i + 1].lower()

    if config_path is None:
        config_path = cp[cfgdir_default] if cfgdir_default in cp else cp["subdir"]

    init_language(config_path, override=language_choice)

    print(f"FlashGBX {AppInfo.VERSION}\n© 2020–{time.strftime('%Y')} Lesserkuma")
    print("https://github.com/Lesserkuma/FlashGBX")

    examples = (
        "\n"
        + __("Examples")
        + ":\n"
        + "  "
        + __("Backup the ROM of a Game Boy Advance cartridge")
        + ":\n\tFlashGBX --mode agb --action backup-rom\n\n"
        + "  "
        + __("Backup Save Data from a Game Boy cartridge")
        + ":\n\tFlashGBX --mode dmg --action backup-save\n\n"
        + "  "
        + __("Write a Game Boy Advance ROM relying on auto-detecting the flash cartridge")
        + ":\n\tFlashGBX --mode agb --action flash-rom ROM.gba\n\n"
        + "  "
        + __("Extract Game Boy Camera pictures as .png files from a save data file")
        + ":\n\tFlashGBX --mode dmg --action gbcamera-extract --gbcamera-outfile-format png GAMEBOYCAMERA.sav\n\n"
        + "  "
        + __(
            "Backup a {gb_memory_cartridge} ROM including its hidden sector .map file",
            gb_memory_cartridge="NP GB-Memory Cartridge",
        )
        + ":\n\tFlashGBX --mode dmg --action backup-rom --dmg-mbc 0x105\n\n"
    )

    parser = argparse.ArgumentParser(formatter_class=ArgParseCustomFormatter, epilog=examples)
    try:
        # pylint: disable=protected-access
        parser._action_groups[1].title = c__("Command Line Arguments Category", "General arguments")
    except Exception as e:
        logger.exception(f"Failed to customize the argparse action-group title: {e}")
    parser.add_argument(
        "--cli",
        help=c__("Command Line Help", "force command line interface mode"),
        action="store_true",
    )
    parser.add_argument(
        "--reset",
        help=c__(
            "Command Line Help",
            "clears all settings such as last used directory information",
        ),
        action="store_true",
    )
    parser.add_argument(
        "--debug",
        help=c__("Command Line Help", "enable debug messages used for development"),
        action="store_true",
    )
    parser.add_argument(
        "--language",
        action="store",
        help=c__(
            "Command Line Help",
            "sets the language of the program (e.g. “auto”, “en”, “de”, ...)",
        ),
    )

    parser.add_argument_group("")
    ap_config = parser.add_argument_group(c__("Command Line Arguments Category", "Configuration arguments"))
    if "appdata" in cp:
        ap_config.add_argument(
            "--cfgdir",
            choices=["appdata", "subdir"],
            type=str.lower,
            default=cfgdir_default,
            help=c__(
                "Configuration Help",
                "sets the config directory to either the OS-provided local app config directory ({appdata}), or a subdirectory of this application ({subdir})",
                appdata=cp["appdata"],
                subdir=cp["subdir"],
            ),
        )

    ap_cli1 = parser.add_argument_group(c__("Command Line Arguments Category", "Main command line interface arguments"))
    ap_cli1.add_argument(
        "--mode",
        choices=["dmg", "agb"],
        type=str.lower,
        default=None,
        help=c__(
            "Command Line Help",
            "set platform to “dmg” (Game Boy) or “agb” (Game Boy Advance)",
        ),
    )
    ap_cli1.add_argument(
        "--action",
        choices=ALL_ACTIONS,
        type=str.lower,
        default=None,
        help=c__("Command Line Help", "select program action"),
    )
    ap_cli1.add_argument(
        "--overwrite",
        action="store_true",
        help=c__(
            "Command Line Help",
            "overwrite without asking if target file already exists",
        ),
    )
    ap_cli1.add_argument(
        "path",
        nargs="?",
        default="auto",
        help=c__(
            "Command Line Help",
            "target or source file path (optional when reading, required when writing)",
        ),
    )

    ap_cli2 = parser.add_argument_group(
        c__(
            "Command Line Arguments Category",
            "Optional command line interface arguments",
        ),
    )
    ap_cli2.add_argument(
        "--dmg-romsize",
        choices=RomSizes.GetCLINames(mode="DMG"),
        type=str.lower,
        default="auto",
        help=c__("Command Line Help", "set size of Game Boy cartridge ROM data"),
    )
    ap_cli2.add_argument(
        "--dmg-mbc",
        type=str.lower,
        default="auto",
        help=c__("Command Line Help", "set mapper type of Game Boy cartridge"),
    )
    ap_cli2.add_argument(
        "--dmg-savetype",
        choices=DmgSaveTypes.GetCLINames(),
        type=str.lower,
        default="auto",
        help=c__("Command Line Help", "set type of Game Boy cartridge save data"),
    )
    ap_cli2.add_argument(
        "--agb-romsize",
        choices=RomSizes.GetCLINames(mode="AGB"),
        type=str.lower,
        default="auto",
        help=c__("Command Line Help", "set size of Game Boy Advance cartridge ROM data"),
    )
    ap_cli2.add_argument(
        "--agb-savetype",
        choices=AgbSaveTypes.GetCLINames(),
        type=str.lower,
        default="auto",
        help=c__("Command Line Help", "set type of Game Boy Advance cartridge save data"),
    )
    ap_cli2.add_argument(
        "--bl-offset",
        type=str,
        default="auto",
        help=c__(
            "Command Line Help",
            "Location of Batteryless SRAM data in ROM (e.g. 0xFC0000); only with “{dmg_savetype_batteryless}” or “{agb_savetype_batteryless}”)",
            dmg_savetype_batteryless="--dmg-savetype batteryless",
            agb_savetype_batteryless="--agb-savetype batteryless",
        ),
    )
    ap_cli2.add_argument(
        "--bl-size",
        type=str,
        default="auto",
        help=c__("Command Line Help", "Size of Batteryless SRAM data in ROM (e.g. 0x10000)"),
    )
    ap_cli2.add_argument(
        "--bl-layout",
        choices=["auto", "0", "1", "2"],
        type=str.lower,
        default="auto",
        help=c__(
            "Command Line Help",
            "Bank layout of Batteryless SRAM data for DMG mode: 0=continuous, 1=first half of ROM bank, 2=second half",
        ),
    )
    ap_cli2.add_argument(
        "--store-rtc",
        action="store_true",
        default=False,
        help=c__("Command Line Help", "store RTC register values if supported"),
    )
    ap_cli2.add_argument(
        "--keep-calibration",
        action="store_true",
        default=True,
        help=c__(
            "Command Line Help",
            "keep existing calibration data of the e-Reader when writing save data",
        ),
    )
    ap_cli2.add_argument(
        "--ignore-bad-header",
        action="store_true",
        help=c__(
            "Command Line Help",
            "don’t stop if invalid data found in cartridge header data",
        ),
    )
    ap_cli2.add_argument(
        "--flashcart-type",
        type=str,
        default="autodetect",
        help=c__(
            "Command Line Help",
            "name of flash cart profile; see .txt files in config directory",
        ),
    )
    ap_cli2.add_argument(
        "--prefer-chip-erase",
        action="store_true",
        help=c__(
            "Command Line Help",
            "prefer full chip erase over sector erase when both available",
        ),
    )
    ap_cli2.add_argument(
        "--force-5v",
        action="store_true",
        help=c__("Command Line Help", "force 5V when writing Game Boy flash cartridges"),
    )
    ap_cli2.add_argument(
        "--no-verify-write",
        action="store_true",
        help=c__("Command Line Help", "do not verify written data"),
    )
    ap_cli2.add_argument(
        "--generate-dump-report",
        action="store_true",
        help=c__("Command Line Help", "generate a dump report for a ROM backup"),
    )
    ap_cli2.add_argument(
        "--save-filename-add-datetime",
        action="store_true",
        help=c__(
            "Command Line Help",
            "adds a timestamp to the file name of save data backups",
        ),
    )
    ap_cli2.add_argument(
        "--gbcamera-palette",
        choices=PocketCamera.PALETTE_NAMES,
        type=str.lower,
        default="grayscale",
        help=c__(
            "Command Line Help",
            "sets the color palette of pictures extracted from Game Boy Camera saves",
        ),
    )
    ap_cli2.add_argument(
        "--gbcamera-outfile-format",
        choices=PocketCamera.OUTPUT_FORMATS,
        type=str.lower,
        default="png",
        help=c__(
            "Command Line Help",
            "sets the file format of saved pictures extracted from Game Boy Camera saves",
        ),
    )
    ap_cli2.add_argument(
        "--gbcamera-extract",
        action="store_true",
        default=False,
        help=c__(
            "Command Line Help",
            "automatically extract Game Boy Camera pictures after backing up save data",
        ),
    )
    ap_cli2.add_argument(
        "--device-port",
        help=c__("Command Line Help", "override device port"),
        default=None,
    )
    ap_cli2.add_argument(
        "--device-limit-baudrate",
        action="store_true",
        help=c__("Command Line Help", "limit connection to a slower baud rate"),
    )
    ap_cli2.add_argument(
        "--compare-sectors",
        action="store_true",
        help=c__(
            "Command Line Help",
            "compare sectors and only write those that differ when writing a ROM (only for flash carts that support this feature)",
        ),
        default=True,
    )
    ap_cli2.add_argument(
        "--wait",
        action="store_true",
        help=c__("Command Line Help", "wait for key press after the program has ended"),
    )
    try:
        parsed_args, _ = parser.parse_known_args()
    except SystemExit:
        input("\n\n" + __("Press ENTER to exit.") + "\n")
        return 0

    parsed_cfgdir = getattr(parsed_args, "cfgdir", None)
    if parsed_cfgdir == "appdata":
        parsed_config_path = cp["appdata"]
    elif parsed_cfgdir == "subdir":
        parsed_config_path = cp["subdir"]
    else:
        parsed_config_path = config_path
    if parsed_config_path is not None and parsed_config_path != config_path:
        config_path = parsed_config_path

    if parsed_args.mode is not None or parsed_args.action is not None:
        parsed_args.cli = True

    if parsed_args.debug:
        AppContext.DEBUG = True

    base_args: BaseArgs = {"app_path": app_path, "config_path": config_path, "argparsed": parsed_args}
    while True:
        try:
            config_dir = Path(config_path)
            config_dir.mkdir(parents=True, exist_ok=True)
            tf = config_dir / "settings.ini"
            tf.touch(exist_ok=True)
            break
        except PermissionError:
            print(
                "\n"
                + ANSI.RED
                + __(
                    "Error: This program has no permission to use the configuration directory “{config_path}”!",
                    config_path=config_path,
                )
                + ANSI.RESET,
            )
            if "appdata" in cp and parsed_args.cfgdir == "subdir":
                answer = (
                    input(
                        __(
                            "Use directory “{appdata_folder}” instead?",
                            appdata_folder=cp["appdata"],
                        )
                        + " [y/N] ",
                    )
                    .strip()
                    .lower()
                )
                if answer != "y":
                    return None
                config_path = cp["appdata"]
                base_args["config_path"] = config_path
                continue
            input("")
            if parsed_args.wait:
                input("\n\n" + __("Press ENTER to exit.") + "\n")
            return None

    loaded_config = LoadConfig(base_args)
    startup_args: StartupArgs = {**base_args, **loaded_config}

    app: Any = None
    exc: str | None = None
    retval = -1
    if not parsed_args.cli:
        try:
            from . import FlashGBX_GUI

            app = FlashGBX_GUI.FlashGBX_GUI(startup_args)
        except ModuleNotFoundError:
            exc = traceback.format_exc()
            app = None
        except Exception as e:
            logger.exception(f"Failed to launch GUI: {e}")
            exc = traceback.format_exc()
            app = None

        if app is None:
            from . import FlashGBX_CLI

            if parsed_args.action is None:
                parser.print_help()
                print(
                    f"\n\n{ANSI.RED}"
                    + __("Note: GUI mode couldn’t be launched, but the application can be run in CLI mode.")
                    + "\n      "
                    + __("Optional command line switches are explained above.")
                    + f"{ANSI.RESET}\n",
                )
                if exc is not None:
                    print(ANSI.YELLOW + str(exc) + ANSI.RESET)

            print(__("Falling back to CLI mode.") + "\n")
            cli_args = cast("FlashGBX_CLI.CLIConfig", startup_args)
            app = FlashGBX_CLI.FlashGBX_CLI(cli_args)
            try:
                retval = app.run()
            except KeyboardInterrupt:
                print("\n\n" + __("Program stopped."))
            if parsed_args.wait:
                input("\n" + __("Press ENTER to exit.") + "\n")
            sys.exit(retval)

        app.run()

    else:
        from . import FlashGBX_CLI

        print("\n" + __("Now running in CLI mode."))
        cli_args = cast("FlashGBX_CLI.CLIConfig", startup_args)
        app = FlashGBX_CLI.FlashGBX_CLI(cli_args)
        try:
            retval = app.run()
        except KeyboardInterrupt:
            print("\n\n" + __("Program stopped."))
        if parsed_args.wait:
            input("\n" + __("Press ENTER to exit.") + "\n")
        sys.exit(retval)
    return None
