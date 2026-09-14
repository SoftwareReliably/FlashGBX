# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import datetime
import math
import os
import platform
import re
import shutil
import sys
import time
import traceback
import zipfile
from argparse import Namespace
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast

from loguru import logger  # pyright: ignore[reportMissingImports]
from serial import SerialException  # pyright: ignore[reportMissingModuleSource]
from serial.tools import list_ports

from .i18n import __, ___, c__, format_decimal

try:
    # pylint: disable=import-error
    import readline

    readline.set_completer_delims("\t\n=")
    readline.parse_and_bind("tab:complete")
except Exception:
    logger.exception("Readline tab completion is unavailable")

from .app import GBXCART_RW_BAUD_RATES, GBXCART_RW_DEFAULT_BAUD_RATE, HW_DEVICES, AppContext, generate_filename
from .CartridgeTypes import AgbSaveTypes, DmgSaveTypes, RomSizes
from .Flashcart import FlashcartMap, has_3v_compatible_profile
from .Formatter import Formatter
from .IniSettings import IniSettings
from .InteractiveConsole import InteractiveConsole
from .Logging import ANSI, Logger, dprint
from .Mapper import DMG_Mapper
from .PocketCamera import PocketCamera
from .Progress import Progress
from .RomFileAGB import RomFileAGB
from .RomFileDMG import RomFileDMG, from_isx

if TYPE_CHECKING:
    import argparse
    from argparse import Namespace

    from FlashGBX.hw_JoeyJr import FirmwareUpdater

type PlatformMode = Literal["DMG", "AGB"]
type HeaderData = dict[str, Any]
type ProgressPayload = Mapping[str, Any]
type BatterylessArgs = dict[str, int]
type Device = Any


class CLIConfig(TypedDict):
    """Startup data assembled by :mod:`FlashGBX.FlashGBX`."""

    app_path: str
    config_path: str
    flashcarts: FlashcartMap
    config_ret: list[list[Any]]
    argparsed: argparse.Namespace
    called_with_args: NotRequired[bool]
    debug: NotRequired[bool]


class FlashGBX_CLI:
    """Command-line frontend for cartridge and firmware operations."""

    def __init__(self, args: CLIConfig) -> None:
        self.ARGS: CLIConfig = args
        AppContext.APP_PATH = args["app_path"]
        AppContext.CONFIG_PATH = args["config_path"]
        self.FLASHCARTS: FlashcartMap = args["flashcarts"]
        # Hardware backends are loaded dynamically and expose a shared runtime
        # interface without inheriting from one concrete device class.
        self.CONN: Any = None
        self.DEVICE: tuple[str, Device] | None = None
        self.PROGRESS = Progress(self.UpdateProgress, self.WaitProgress)
        self.FWUPD_R = False
        self.INI: IniSettings | None = None
        self.RETVAL = 0

        if platform.system() == "Windows":
            self.prog_bar_part_chars = (" ", " ", " ", " ", "▌", "▌", "▌", "▌")
        else:
            self.prog_bar_part_chars = (" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉")

    def _GetDeviceMaxBaudRate(self, device: Device) -> int:
        """Return the configured connection speed for a hardware backend."""
        args: Namespace = self.ARGS["argparsed"]
        configured = getattr(args, "gbxcartrw_baudrate", None)
        if getattr(args, "device_limit_baudrate", False):
            return min(GBXCART_RW_BAUD_RATES)
        if self._IsGBxCartRWDevice(device):
            if configured in GBXCART_RW_BAUD_RATES:
                return cast("int", configured)
            return GBXCART_RW_DEFAULT_BAUD_RATE
        return 2_000_000

    @staticmethod
    def _IsGBxCartRWDevice(device: Device) -> bool:
        return getattr(device, "DEVICE_ID", "") == "gbxcartrw" or getattr(device, "DEVICE_NAME", "") == "GBxCart RW"

    @staticmethod
    def _GetPlatformName(mode: str) -> str:
        return {
            "DMG": __("Game Boy or Game Boy Color"),
            "AGB": __("Game Boy Advance"),
        }.get(mode, mode)

    @staticmethod
    def _GetAutoPlatformMode(
        conn: Device,
        supported_modes: Sequence[PlatformMode] | None = None,
    ) -> PlatformMode | None:
        if supported_modes is None:
            supported_modes = cast("Sequence[PlatformMode]", conn.GetSupprtedModes())
        if len(supported_modes) == 1:
            return supported_modes[0]
        if conn.FW.get("cart_mode_switch"):
            switch_mode = conn.GetCartModeSwitchState()
            if switch_mode is not False:
                mode: Literal["AGB", "DMG"] = "AGB" if switch_mode == 1 else "DMG"
                if mode in supported_modes:
                    return mode
        mode = cast("PlatformMode", conn.GetMode())
        if mode in supported_modes:
            return mode
        return None

    @staticmethod
    def _ParseDmgMbc(value: object) -> int:
        """Parse a CLI mapper value while retaining the legacy shortcuts."""
        if not isinstance(value, str):
            return 0x19
        if value.lower().startswith("0x"):
            try:
                return int(value, 0)
            except ValueError:
                return 0x19
        if not value.isdecimal():
            return 0x19
        return {
            1: 0x01,
            2: 0x06,
            3: 0x13,
            5: 0x19,
            6: 0x20,
            7: 0x22,
        }.get(int(value), 0x19)

    @staticmethod
    def _GetHeaderInt(header: HeaderData, key: str) -> int:
        """Return a required integer header field or reject malformed data."""
        value = header.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            msg: str = f"Invalid cartridge header field: {key}"
            raise TypeError(msg)
        return value

    @staticmethod
    def _SelectMenuAction(menu_items: Sequence[tuple[str, str]]) -> str | None:
        """Prompt for a menu item and return its action name."""
        print(__("Select Operation:"))
        for index, (_, label) in enumerate(menu_items, start=1):
            print(f"{index:>3d}) {label}")
        print()
        item_count: int = len(menu_items)
        answer: str = (
            input(
                __(
                    "Enter number ({range}) [{default}]:",
                    range=f"1-{item_count}",
                    default="1",
                )
                + " ",
            )
            .lower()
            .strip()
        )
        if answer == "":
            return "info"
        try:
            selection = int(answer)
        except TypeError, ValueError:
            return None
        if not 1 <= selection <= item_count:
            return None
        return menu_items[selection - 1][0]

    @staticmethod
    def _ExtractCameraPictures(args: argparse.Namespace) -> int:
        """Extract all pictures from a Game Boy Camera save file."""
        if args.path == "auto":
            args.path = input(__("Enter file path of Game Boy Camera save data file:") + " ").strip().replace('"', "")
            print()
            if args.path == "":
                print(__("Canceled."))
                return 0

        camera = PocketCamera()
        if not camera.LoadFile(args.path):
            print("\n" + ANSI.RED + __("Couldn't parse the save data file.") + ANSI.RESET)
            return 0

        camera.SetPalette(PocketCamera.PALETTE_NAMES.index(args.gbcamera_palette))
        destination: Path = Path(args.path).with_suffix("")
        if destination.is_file():
            print(
                "\n"
                + ANSI.RED
                + __("Can't save pictures at location “{path}”.", path=str(destination.resolve()))
                + ANSI.RESET,
            )
            return 1
        destination.mkdir(parents=True, exist_ok=True)
        for index in range(32):
            file: Path = destination / f"IMG_PC{index + 1:02d}.{args.gbcamera_outfile_format}"
            camera.ExportPicture(index, file, scale=1)
        print(
            __(
                "The pictures from “{save_file}” were extracted to “{destination}”.",
                save_file=str(Path(args.path).resolve()),
                destination=str(destination.resolve() / f"IMG_PC**.{args.gbcamera_outfile_format:s}"),
            ),
        )
        return 0

    def _connect_for_action(self, args: argparse.Namespace, fwupdate_actions: set[str]) -> bool:
        if args.action is not None and args.action in ({"gbcamera-extract"} | fwupdate_actions):
            return True
        if not self.FindDevices(port=args.device_port):
            print(__("No devices found."))
            return False
        if not self.ConnectDevice() or self.DEVICE is None:
            print(__("Couldn't connect to the device."))
            return False

        dev = self.DEVICE[1]
        if dev.FirmwareUpdateAvailable() and dev.FW_UPDATE_REQ is True:
            print(
                __(
                    "The current firmware version of your device is not supported.\nPlease update to a supported firmware version first.",
                ),
            )
            return False

        builddate = dev.GetFWBuildDate()
        print(
            "\n"
            + __(
                "Connected to {device_name}",
                device_name=dev.GetFullNameExtended(more=builddate != ""),
            ),
        )
        self.CONN.SetAutoPowerOff(value=1500)
        self.CONN.SetAGBReadMethod(method=2)
        return True

    def _RunCartridgeAction(self, args: argparse.Namespace, header: HeaderData) -> int | None:
        try:
            if args.action == "backup-rom":
                self.BackupROM(args, header)

            elif args.action == "backup-save":
                self.BackupRestoreRAM(args, header)

            elif args.action == "restore-save":
                if args.path == "auto":
                    args.path = input(__("Enter file path of save data file:") + " ").strip().replace('"', "")
                    print()
                    if args.path == "":
                        print(__("Canceled."))
                        return 0
                self.BackupRestoreRAM(args, header)

            elif args.action in {"erase-save", "debug-test-save"}:
                self.BackupRestoreRAM(args, header)

            elif args.action == "flash-rom":
                if args.path == "auto":
                    args.path = input(__("Enter file path of ROM file:") + " ").strip().replace('"', "")
                    print()
                    if args.path == "":
                        print(__("Canceled."))
                        return 0
                self.FlashROM(args, header)

            if args.action != "info":
                print()

        except KeyboardInterrupt:
            print("\n\n" + __("Operation stopped."))
        return None

    @staticmethod
    def _PrintConfigMessages(config_messages: Sequence[Sequence[object]]) -> None:
        for config_message in config_messages:
            if len(config_message) < 2:
                continue
            status, message = config_message[:2]
            if not isinstance(status, int) or not isinstance(message, str):
                continue
            if status < 1:
                print(message)
            elif status == 1:
                print(f"{ANSI.YELLOW:s}{message:s}{ANSI.RESET:s}")
            elif status == 2:
                print(f"{ANSI.RED:s}{message:s}{ANSI.RESET:s}")

    def run(self) -> int:
        sys.stdout = Logger()
        self._PrintConfigMessages(self.ARGS["config_ret"])

        args: Namespace = self.ARGS["argparsed"]
        config_path: str = AppContext.CONFIG_PATH
        print(__("Configuration folder:") + " " + config_path + "\n")

        menu_items = [
            ("info", __("Read Cartridge Information")),
            ("backup-rom", __("Backup ROM")),
            ("flash-rom", __("Write ROM")),
            ("backup-save", __("Backup Save Data")),
            ("restore-save", __("Restore Save Data")),
            ("erase-save", __("Erase Save Data")),
            (
                "gbcamera-extract",
                __("Extract Game Boy Camera Pictures From Existing Save Data Backup"),
            ),
            ("interactive", __("Interactive Console")),
        ]
        for hw_mod in HW_DEVICES:
            try:
                cls = hw_mod.GbxDevice
                dev = cls()
                action = dev.FirmwareUpdateAction()
                if dev.SupportsFirmwareUpdates() and action is not None:
                    menu_items.append(
                        (
                            action,
                            __(
                                "Firmware Update for {device_name}",
                                device_name=cls.DEVICE_LABEL_SHORT,
                            ),
                        ),
                    )
            except Exception:
                logger.exception("Failed to add a firmware-update action to the CLI menu")

        fwupdate_actions: set[str] = set()
        for hw_mod in HW_DEVICES:
            try:
                dev = hw_mod.GbxDevice()
                if dev.SupportsFirmwareUpdates():
                    action = dev.FirmwareUpdateAction()
                    if action is not None:
                        fwupdate_actions.add(action)
            except Exception:
                logger.exception("Failed to inspect a firmware-update action")

        # Ask interactively if no args set
        if args.action is None:
            self.ARGS["called_with_args"] = False
            args.action = self._SelectMenuAction(menu_items)
            if args.action is None:
                print(__("Canceled."))
                return 0
        else:
            self.ARGS["called_with_args"] = True

        if not self._connect_for_action(args, fwupdate_actions):
            return 1

        action_result = self._RunStandaloneAction(args, fwupdate_actions)
        if action_result is not None:
            return action_result

        if args.mode is None:
            supported_modes = self.CONN.GetSupprtedModes()
            auto_mode = self._GetAutoPlatformMode(self.CONN, supported_modes)
            match len(supported_modes):
                case 0:
                    print(__("The connected device does not support any platform modes.") + "\n")
                    self.DisconnectDevice()
                    return 1
                case 1:
                    mode = auto_mode or supported_modes[0]
                    print(__("Using only supported platform: {platform}", platform=mode) + "\n")
                    args.mode = mode.lower()
                case _:
                    if auto_mode is not None:
                        if self.CONN.FW.get("cart_mode_switch"):
                            print(
                                __(
                                    "Using platform mode set by cartridge mode switch: {platform}",
                                    platform=self._GetPlatformName(auto_mode),
                                )
                                + "\n",
                            )
                        else:
                            print(
                                __(
                                    "Using platform mode: {platform}",
                                    platform=self._GetPlatformName(auto_mode),
                                )
                                + "\n",
                            )
                        args.mode = auto_mode.lower()
                    else:
                        print(
                            __("Select Platform:") + "\n"
                            "  1) " + __("Game Boy or Game Boy Color") + "\n"
                            "  2) " + __("Game Boy Advance") + "\n",
                        )
                        answer = (
                            input(
                                __(
                                    "Enter number ({range}) [{default}]:",
                                    range="1-2",
                                    default="2",
                                )
                                + " ",
                            )
                            .lower()
                            .strip()
                        )
                        print()
                        if answer == "1":
                            args.mode = "dmg"
                        elif answer in {"2", ""}:
                            args.mode = "agb"
                        else:
                            print(__("Canceled."))
                            self.DisconnectDevice()
                            return 0
                        print()

        if args.mode == "dmg":
            print(__("Platform: {platform}", platform=__("Game Boy or Game Boy Color")))
            self.CONN.SetMode("DMG")
        else:
            print(__("Platform: {platform}", platform=__("Game Boy Advance")))
            self.CONN.SetMode("AGB")
        # time.sleep(0.2)

        if args.action == "interactive":
            try:
                self.InteractiveConsole()
            except KeyboardInterrupt:
                print("\n\n" + __("Operation stopped."))
            self.DisconnectDevice()
            return 0

        header = self.CONN.ReadHeader()
        (bad_read, s_header, header) = self.ReadCartridge(header)
        if s_header == "":
            print("\n" + ANSI.RED + __("Couldn't read cartridge header. Please try again.") + ANSI.RESET + "\n")
            self.DisconnectDevice()
            return 1
        if (
            bad_read
            and not args.ignore_bad_header
            and (
                self.CONN.GetMode() == "AGB"
                or (self.CONN.GetMode() == "DMG" and "mapper_raw" in header and header["mapper_raw"] != 0x203)
            )
        ):
            print(
                "\n"
                + ANSI.RED
                + __(
                    "Invalid data was detected which usually means that the cartridge couldn't be read correctly. Please make sure you selected the correct platform and that the cartridge contacts are clean. This check can be disabled with the command line switch “{switch}”.",
                    switch="--ignore-bad-header",
                )
                + ANSI.RESET
                + "\n",
            )
            print(__("Cartridge Information:"))
            print(s_header)
            self.DisconnectDevice()
            return 1

        print("\n" + __("Cartridge Information:"))
        print(s_header)

        action_result = self._RunCartridgeAction(args, header)
        self.DisconnectDevice()
        return self.RETVAL if action_result is None else action_result

    def _RunStandaloneAction(self, args: argparse.Namespace, fwupdate_actions: set[str]) -> int | None:
        if args.action == "gbcamera-extract":
            return self._ExtractCameraPictures(args)

        if args.action not in fwupdate_actions:
            return None

        for hw_mod in HW_DEVICES:
            cls = hw_mod.GbxDevice
            dev = cls()
            action = dev.FirmwareUpdateAction()
            if dev.SupportsFirmwareUpdates() and action == args.action:
                method = getattr(self, dev.CLIUpdaterMethod())
                kwargs = {"port": args.device_port}
                method(**kwargs)
                return 0
        return None

    def WaitProgress(self, args: ProgressPayload) -> None:
        if args["user_action"] == "REINSERT_CART":
            msg = "\n\n"
            msg += args["msg"]
            msg += "\n\n" + __("Press ENTER to continue.") + "\n"
            answer = input(msg).strip().lower()
            if len(answer.strip()) != 0:
                self.CONN.USER_ANSWER = False
            else:
                self.CONN.USER_ANSWER = True
        elif args["user_action"] == "RETRY_5V":
            msg = "\n\n"
            msg += args["msg"]
            msg += "\n\n" + args["title"] + " [y/N] "
            answer = input(msg).strip().lower()
            self.CONN.USER_ANSWER = answer in ("y", "yes")

    def UpdateProgress(self, args: ProgressPayload | None) -> None:
        if args is None:
            return

        if "error" in args:
            print("{:s}{:s}{:s}".format(ANSI.RED, args["error"], ANSI.RESET))
            return

        pos = args.get("pos", 0)
        size = args.get("size", 0)
        speed = args.get("speed", 0)
        elapsed = args.get("time_elapsed", 0)
        left = args.get("time_left", 0)

        if "action" in args:
            if args["action"] == "INITIALIZE":
                if args["method"] == "ROM_WRITE_VERIFY":
                    print("\n\n" + __("The newly written ROM data will now be checked for errors.") + "\n")
                elif args["method"] == "SAVE_WRITE_VERIFY":
                    print("\n\n" + __("The newly written save data will now be checked for errors.") + "\n")
            elif args["action"] == "ERASE":
                print(
                    ANSI.CLEAR_LINE
                    + __(
                        "Please wait while the flash chip is being erased... (Elapsed time: {elapsed_time})",
                        elapsed_time=Formatter.progress_time(elapsed),
                    ),
                    end="\r",
                )
            elif args["action"] == "UNLOCK":
                print(
                    ANSI.CLEAR_LINE
                    + __(
                        "Please wait while the flash chip is being unlocked... (Elapsed time: {elapsed_time})",
                        elapsed_time=Formatter.progress_time(elapsed),
                    ),
                    end="\r",
                )
            elif args["action"] == "SECTOR_ERASE":
                print(
                    ANSI.CLEAR_LINE
                    + __(
                        "Erasing flash sector at address {address}...",
                        address="0x{:X}".format(args["sector_pos"]),
                    ),
                    end="\r",
                )
            elif args["action"] == "UPDATE_RTC":
                print("\n" + __("Updating Real Time Clock..."))
            elif args["action"] == "CALC_CHECKSUMS":
                pass
            elif args["action"] == "ERROR":
                print(ANSI.CLEAR_LINE + ANSI.RED + args["text"] + ANSI.RESET)
            elif args["action"] == "ABORTING":
                print("\n" + __("Stopping..."))
            elif args["action"] == "FINISHED":
                print("\n")
                self.FinishOperation()
            elif args["action"] == "ABORT":
                print("\n" + __("Operation stopped.") + "\n")
                if "info_type" in args and "info_msg" in args:
                    if args["info_type"] == "msgbox_critical":
                        self.RETVAL = 1
                        print(ANSI.RED + args["info_msg"] + ANSI.RESET)
                    elif args["info_type"] == "msgbox_information" or args["info_type"] == "label":
                        self.RETVAL = 0
                        print(args["info_msg"])
                return
            elif args["action"] == "PROGRESS":
                if size <= 0:
                    return
                # pv style progress status
                prog_str = "{:s}/{:s} {:s} [{:s}{:s}] [{:s}] {:s}% {:s} {:s} ".format(
                    Formatter.file_size(pos, space="", short=True).replace(" ", "").rjust(8),
                    Formatter.file_size(size, space="", short=True).replace(" ", ""),
                    Formatter.progress_time_short(elapsed),
                    format_decimal(speed, precision=2).rjust(6),
                    __(" KiB/s").replace(" ", ""),
                    "%PROG_BAR%",
                    f"{int(pos / size * 100):d}".rjust(3),
                    c__("Estimated Time abbreviation (3 characters)", "ETA"),
                    Formatter.progress_time_short(left),
                )
                prog_width = max(
                    1,
                    shutil.get_terminal_size((80, 20))[0] - (len(prog_str) - 10),
                )
                progress = min(1, max(0, pos / size))
                whole_width = math.floor(progress * prog_width)
                remainder_width = (progress * prog_width) % 1
                part_width = math.floor(remainder_width * 8)
                try:
                    part_char = self.prog_bar_part_chars[part_width]
                    if (prog_width - whole_width - 1) < 0:
                        part_char = ""
                    prog_bar = "█" * whole_width + part_char + " " * (prog_width - whole_width - 1)
                    print(prog_str.replace("%PROG_BAR%", prog_bar), end="\r")
                except UnicodeEncodeError:
                    prog_bar = "#" * whole_width + " " * (prog_width - whole_width)
                    print(prog_str.replace("%PROG_BAR%", prog_bar), end="\r", flush=True)
                except Exception:
                    logger.exception("Failed to render the CLI progress bar")

    def _FinishBackupRAM(self) -> None:
        self.CONN.INFO["last_action"] = 0
        is_camera_save = (
            "debug" not in self.ARGS
            and self.CONN.GetMode() == "DMG"
            and self.CONN.INFO["mapper_raw"] == 252
            and self.CONN.INFO["transferred"] == 0x20000
        ) or (
            self.CONN.INFO["transferred"] == 0x100000
            and "ram_size_raw" in self.CONN.INFO["dump_info"]["header"]
            and self.CONN.INFO["dump_info"]["header"]["ram_size_raw"] == 0x204
        )
        if is_camera_save:
            if getattr(self.ARGS["argparsed"], "gbcamera_extract", False):
                if self.CONN.INFO["transferred"] == 0x100000:
                    base = Path(self.CONN.INFO["last_path"]).with_suffix("")
                    if base.is_file():
                        print(
                            __(
                                "Can't save pictures at location “{path}”.",
                                path=str(base.resolve()),
                            ),
                        )
                        self.RETVAL = 1
                        return
                    base.mkdir(parents=True, exist_ok=True)
                    pc = PocketCamera()
                    pc.SetPalette(PocketCamera.PALETTE_NAMES.index(self.ARGS["argparsed"].gbcamera_palette))
                    for roll in range(1, 9):
                        with Path(self.CONN.INFO["last_path"]).open("rb") as f:
                            f.seek(0x20000 * (roll - 1))
                            roll_data = bytearray(f.read(0x20000))
                        if pc.LoadFile(roll_data):
                            for i in range(32):
                                file = base / "IMG_P{:1d}{:02d}.{}".format(
                                    roll,
                                    i,
                                    self.ARGS["argparsed"].gbcamera_outfile_format,
                                )
                                pc.ExportPicture(i, file, scale=1)
                else:
                    file = self.CONN.INFO["last_path"]
                    pc = PocketCamera()
                    if pc.LoadFile(file):
                        pc.SetPalette(PocketCamera.PALETTE_NAMES.index(self.ARGS["argparsed"].gbcamera_palette))
                        destination = Path(self.CONN.INFO["last_path"]).with_suffix("")
                        file = destination / "IMG_PC00.png"
                        if destination.is_file():
                            print(
                                __(
                                    "Can't save pictures at location “{path}”.",
                                    path=str(destination.resolve()),
                                ),
                            )
                            self.RETVAL = 1
                            return
                        destination.mkdir(parents=True, exist_ok=True)
                        for i in range(32):
                            file = destination / f"IMG_PC{i:02d}.{self.ARGS['argparsed'].gbcamera_outfile_format}"
                            pc.ExportPicture(i, file, scale=1)
                print(__("The pictures were extracted."))
            print()

        print(__("The save data backup is complete!"))

    def _FormatBrokenSectors(self) -> tuple[str, int]:
        sectors = ""
        sector_count = 0
        for sector in self.CONN.INFO["broken_sectors"]:
            sector_count += 1
            if sector_count > 10:
                sectors += (
                    c__(
                        "Shortened list of Broken Sectors (e.g. 0x0000~0x07FF and others)",
                        "and others",
                    )
                    + "  "
                )
                break
            sectors += f"0x{sector[0]:X}~0x{sector[0] + sector[1] - 1:X}, "
        return sectors, sector_count

    def FinishOperation(self) -> None:
        time_elapsed = None
        speed = None
        if "time_start" in self.PROGRESS.PROGRESS and self.PROGRESS.PROGRESS["time_start"] > 0:
            time_elapsed = time.time() - self.PROGRESS.PROGRESS["time_start"]
            speed = format_decimal((self.CONN.INFO["transferred"] / 1024.0) / time_elapsed, precision=2) + __(" KiB/s")
            self.PROGRESS.PROGRESS["time_start"] = 0

        if self.CONN.INFO["last_action"] == 4:  # Flash ROM
            self.CONN.INFO["last_action"] = 0
            if self.PROGRESS.PROGRESS.get("verified"):
                print(ANSI.GREEN + __("The ROM was written and verified successfully!") + ANSI.RESET)
            elif "broken_sectors" in self.CONN.INFO:
                s, sc = self._FormatBrokenSectors()
                print(
                    ANSI.RED
                    + ___(
                        "The ROM was written completely, but verification of written data failed in the following sector: {sectors}.",
                        "The ROM was written completely, but verification of written data failed in the following sectors: {sectors}.",
                        n=sc,
                        sectors=s[:-2],
                    )
                    + ANSI.RESET,
                )
                self.RETVAL = 1
            else:
                print(__("ROM writing complete!"))

        elif self.CONN.INFO["last_action"] == 1:  # Backup ROM
            self.CONN.INFO["last_action"] = 0
            dump_report = False
            dumpinfo_file = ""
            if self.ARGS["argparsed"].generate_dump_report is True:
                try:
                    dump_report = self.CONN.GetDumpReport()
                    if dump_report is not False:
                        if time_elapsed is not None and speed is not None:
                            dump_report = dump_report.replace(
                                "%TRANSFER_RATE%",
                                "{:.2f}".format((self.CONN.INFO["transferred"] / 1024.0) / time_elapsed) + " KiB/s",
                            )
                            dump_report = dump_report.replace(
                                "%TIME_ELAPSED%",
                                Formatter.progress_time(time_elapsed, localized=False),
                            )
                        else:
                            dump_report = dump_report.replace("%TRANSFER_RATE%", "N/A")
                            dump_report = dump_report.replace("%TIME_ELAPSED%", "N/A")
                        dumpinfo_file = Path(self.CONN.INFO["last_path"]).with_suffix(".txt")
                        with dumpinfo_file.open("wb") as f:
                            f.write(bytearray([0xEF, 0xBB, 0xBF]))  # UTF-8 BOM
                            f.write(dump_report.encode("UTF-8"))
                except Exception as e:
                    print(__("Error:") + " " + str(e))

            if self.CONN.GetMode() == "DMG":
                print("CRC32: {:08x}".format(self.CONN.INFO["file_crc32"]))
                print("SHA-1: {:s}\n".format(self.CONN.INFO["file_sha1"]))
                if self.CONN.INFO["rom_checksum"] == self.CONN.INFO["rom_checksum_calc"]:
                    print(
                        ANSI.GREEN
                        + __("The ROM backup is complete and the checksum was verified successfully!")
                        + ANSI.RESET,
                    )
                elif ("DMG-MMSA-JPN" in self.ARGS["argparsed"].flashcart_type) or (
                    "mapper_raw" in self.CONN.INFO and self.CONN.INFO["mapper_raw"] in (0x105, 0x202)
                ):
                    print(__("The ROM backup is complete!"))
                else:
                    msg = __("The ROM was dumped, but the checksum is not correct.")
                    if self.CONN.INFO["loop_detected"] is not False:
                        msg += "\n" + __(
                            "A data loop was detected in the ROM backup at position {pos} ({size}). This may indicate a bad dump or overdump.",
                            pos="0x{:X}".format(self.CONN.INFO["loop_detected"]),
                            size=Formatter.file_size(self.CONN.INFO["loop_detected"], as_int=True),
                        )
                    else:
                        msg += "\n" + __(
                            "This may indicate a bad dump, however this can be normal for some reproduction cartridges, unlicensed games, prototypes, patched games and intentional overdumps.",
                        )
                    print(f"{ANSI.YELLOW:s}{msg:s}{ANSI.RESET:s}")
            elif self.CONN.GetMode() == "AGB":
                print("CRC32: {:08x}".format(self.CONN.INFO["file_crc32"]))
                print("SHA-1: {:s}\n".format(self.CONN.INFO["file_sha1"]))
                if "db" in self.CONN.INFO and self.CONN.INFO["db"] is not None:
                    if self.CONN.INFO["db"]["rc"] == self.CONN.INFO["file_crc32"]:
                        print(
                            ANSI.GREEN
                            + __("The ROM backup is complete and the checksum was verified successfully!")
                            + ANSI.RESET,
                        )
                    else:
                        msg = __("The ROM backup is complete, but the checksum doesn't match the known database entry.")
                        if self.CONN.INFO["loop_detected"] is not False:
                            msg += "\n" + __(
                                "A data loop was detected in the ROM backup at position {pos} ({size}). This may indicate a bad dump or overdump.",
                                pos="0x{:X}".format(self.CONN.INFO["loop_detected"]),
                                size=Formatter.file_size(self.CONN.INFO["loop_detected"], as_int=True),
                            )
                        else:
                            msg += "\n" + __(
                                "This may indicate a bad dump, however this can be normal for some reproduction cartridges, unlicensed games, prototypes, patched games and intentional overdumps.",
                            )
                        print(ANSI.YELLOW + msg + ANSI.RESET)
                else:
                    msg = __(
                        "The ROM backup is complete! As there is no known checksum for this ROM in the database, verification was skipped.",
                    )
                    if self.CONN.INFO["loop_detected"] is not False:
                        msg += "\n" + __(
                            "A data loop was detected in the ROM backup at position {pos} ({size}). This may indicate a bad dump or overdump.",
                            pos="0x{:X}".format(self.CONN.INFO["loop_detected"]),
                            size=Formatter.file_size(self.CONN.INFO["loop_detected"], as_int=True),
                        )
                    print(ANSI.YELLOW + msg + ANSI.RESET)

        elif self.CONN.INFO["last_action"] == 2:  # Backup RAM
            self._FinishBackupRAM()

        elif self.CONN.INFO["last_action"] == 3:  # Restore RAM
            self.CONN.INFO["last_action"] = 0
            if self.CONN.INFO.get("save_erase"):
                print(__("The save data was erased."))
                del self.CONN.INFO["save_erase"]
            else:
                print(__("The save data was restored!"))

        else:
            self.CONN.INFO["last_action"] = 0

    def FindDevices(self, port: str | None = None) -> bool:
        self.DEVICE = None
        for hw_device in HW_DEVICES:
            dev = hw_device.GbxDevice()
            ret = dev.Initialize(
                self.FLASHCARTS,
                port=port,
                max_baud=self._GetDeviceMaxBaudRate(dev),
            )
            if ret is False:
                self.CONN = None
            elif isinstance(ret, list):
                if len(ret) > 0:
                    print()
                for i in range(len(ret)):
                    status = ret[i][0]
                    msg = re.sub("<[^<]+?>", "", ret[i][1])
                    if status == 3:
                        print(ANSI.RED + msg.replace("\n\n", "\n") + ANSI.RESET)
                        self.CONN = None

            if dev.IsConnected():
                self.DEVICE = (dev.GetFullNameExtended(), dev)
                dev.Close()
                break

        return self.DEVICE is not None

    def ConnectDevice(self) -> bool:
        if self.DEVICE is None:
            self.CONN = None
            return False
        dev = self.DEVICE[1]
        port = dev.GetPort()
        ret = dev.Initialize(
            self.FLASHCARTS,
            port=port,
            max_baud=self._GetDeviceMaxBaudRate(dev),
        )

        if ret is False:
            print("\n" + ANSI.RED + __("An error occured while trying to connect to the device.") + ANSI.RESET)
            traceback.print_stack()
            self.CONN = None
            return False

        if isinstance(ret, list):
            for i in range(len(ret)):
                status = ret[i][0]
                msg = re.sub("<[^<]+?>", "", ret[i][1])
                if status == 0:
                    print("\n" + msg)
                elif status == 1:
                    print(f"{msg:s}")
                elif status == 2:
                    print(ANSI.YELLOW + msg + ANSI.RESET)
                elif status == 3:
                    print(ANSI.RED + msg + ANSI.RESET)
                    self.CONN = None
                    return False

        if dev.FW_UPDATE_REQ:
            print(
                ANSI.RED
                + __(
                    "A firmware update for your {device_name} is required to fully use this software.",
                    device_name=dev.GetFullName(),
                )
                + "\n"
                + ANSI.YELLOW
                + __(
                    "Current firmware version: {fw_version}",
                    fw_version=dev.GetFirmwareVersion(),
                )
                + ANSI.RESET,
            )
            time.sleep(5)

        self.CONN = dev
        return True

    def InteractiveConsole(self) -> None:
        self.CONN.SetAutoPowerOff(value=0)
        self.CONN.CartPowerOn()

        im = InteractiveConsole(
            self.CONN,
            on_output=print,
            on_error=lambda text: print(ANSI.RED + text + ANSI.RESET),
        )

        print()
        im.print_help()
        print()

        while True:
            print("> ", end="", flush=True)
            try:
                line = input().strip()
            except EOFError:
                break
            if not line:
                continue
            if not im.execute_line(line):
                break

    def DisconnectDevice(self) -> None:
        try:
            devname = self.CONN.GetFullNameExtended()
            self.CONN.SetAutoPowerOff(value=0)
            self.CONN.Close(cartPowerOff=True)
            print(__("Disconnected from {device_name}", device_name=devname))
        except Exception:
            logger.exception("Failed to disconnect the CLI device")
        self.CONN = None

    @staticmethod
    def _DmgPlatformString(data: HeaderData) -> str:
        cgb = data.get("cgb", 0)
        if cgb == 0xC0:
            return __("Game Boy Color exclusive")
        if cgb == 0x80:
            return __("Game Boy Color")
        if data.get("old_lic", 0) == 0x33 and data.get("sgb", 0) == 0x03:
            return __("Super Game Boy")
        return __("Original Game Boy")

    @staticmethod
    def _DmgSaveTypeString(data: HeaderData) -> str:
        try:
            if data["mapper_raw"] == 0x06:  # MBC2
                return DmgSaveTypes(index=1).GetString()
            if data["mapper_raw"] == 0x22 and data["game_title"] in (
                "KORO2 KIRBY",
                "KIRBY TNT",
            ):  # MBC7 Kirby
                return DmgSaveTypes(mbc=0x101).GetString()
            if data["mapper_raw"] == 0x22 and data["game_title"] in ("CMASTER"):  # MBC7 Command Master
                return DmgSaveTypes(mbc=0x102).GetString()
            if data["mapper_raw"] == 0xFD:  # TAMA5
                return DmgSaveTypes(mbc=0x103).GetString()
            if data["mapper_raw"] == 0x20:  # MBC6
                return DmgSaveTypes(mbc=0x104).GetString()
            return DmgSaveTypes(mbc=data["ram_size_raw"]).GetString()
        except KeyError, TypeError, ValueError, IndexError:
            return c__("Game Data", "Not detected")

    @staticmethod
    def _AgbSaveTypeString(data: HeaderData, database_entry: object) -> str:
        save_type = data.get("save_type")
        save_type_count = AgbSaveTypes().GetNumberOfTypes()
        if isinstance(save_type, int) and not isinstance(save_type, bool) and 0 <= save_type < save_type_count:
            return AgbSaveTypes(save_type).GetString()
        if data.get("dacs_8m") is True:
            data["save_type"] = 6
            return AgbSaveTypes(6).GetString()

        database_save_type = database_entry.get("st") if isinstance(database_entry, dict) else None
        if (
            isinstance(database_save_type, int)
            and not isinstance(database_save_type, bool)
            and 0 <= database_save_type < save_type_count
        ):
            data["save_type"] = database_save_type
            return AgbSaveTypes(database_save_type).GetString()
        return c__("Game Data", "No database entry")

    def ReadCartridge(
        self,
        data: HeaderData,
    ) -> tuple[bool, str, HeaderData]:
        bad_read = False
        s = ""
        rows: list[tuple[str, str | None]] = []
        if self.CONN.GetMode() == "DMG":
            # Use (label_with_colon, value) pairs to match existing GUI translation keys
            game_name = None
            if data["db"]:
                game_name: str | None = Path(
                    generate_filename(mode=self.CONN.GetMode(), header=self.CONN.INFO, settings=None),
                ).stem
            if game_name is not None:
                rows.append((__("Game Name:"), game_name))

            rows.append((__("ROM Title:"), Formatter.title(data["game_title"])))

            if data["db"] is not None:
                rows.append(
                    (
                        __("Game Code and Revision:"),
                        "{:s}-{:s}".format(data["db"]["gc"], str(data["version"])),
                    ),
                )
            elif len(data["game_code"]) > 0:
                rows.append(
                    (
                        __("Game Code and Revision:"),
                        "{:s}-{:s}".format(data["game_code"], str(data["version"])),
                    ),
                )
            else:
                rows.append((__("Revision:"), str(data["version"])))

            rows.append((__("Platform:"), self._DmgPlatformString(data)))

            rows.append((__("Real Time Clock:"), data["rtc_string"]))

            if data["logo_correct"] and data["header_checksum_correct"]:
                rows.append((__("Boot Logo:"), c__("Game Data", "OK")))
                bootlogo_path: Path = Path(AppContext.CONFIG_PATH) / "bootlogo_dmg.bin"
                if not bootlogo_path.exists():
                    with bootlogo_path.open("wb") as f:
                        f.write(data["raw"][0x104:0x134])
            else:
                rows.append(
                    (
                        __("Boot Logo:"),
                        ANSI.RED + c__("Game Data", "Invalid") + ANSI.RESET,
                    ),
                )
                bad_read = True

            rows.append((__("ROM Checksum:"), "0x{:04X}".format(data["rom_checksum"])))

            try:
                rows.append((__("ROM Size:"), RomSizes().GetString(index=data["rom_size_raw"])))
            except KeyError, TypeError, ValueError, IndexError:
                rows.append(
                    (
                        __("ROM Size:"),
                        ANSI.RED + c__("Game Data", "Not detected") + ANSI.RESET,
                    ),
                )
                bad_read = True

            rows.append((__("Save Type:"), self._DmgSaveTypeString(data)))

            try:
                rows.append((__("Mapper Type:"), DMG_Mapper().GetMapperName(data["mapper_raw"])))
            except KeyError, TypeError, ValueError, IndexError:
                rows.append(
                    (
                        __("Mapper Type:"),
                        ANSI.RED + c__("Game Data", "Not detected") + ANSI.RESET,
                    ),
                )
                bad_read = True

            if data["logo_correct"] and not self.CONN.IsSupportedMbc(data["mapper_raw"]):
                print(
                    ANSI.YELLOW
                    + "\n"
                    + __(
                        "Warning: This cartridge uses a mapper that may not be completely supported by FlashGBX using the current firmware version of the {device_name}. Please check for firmware updates.",
                        device_name=self.CONN.GetFullName(),
                    )
                    + ANSI.RESET,
                )

        elif self.CONN.GetMode() == "AGB":
            game_name = None
            if data["db"]:
                game_name = Path(
                    generate_filename(mode=self.CONN.GetMode(), header=self.CONN.INFO, settings=None),
                ).stem
            if game_name is not None:
                rows.append((__("Game Name:"), game_name))

            rows.append((__("ROM Title:"), Formatter.title(data["game_title"])))

            if data["db"] is not None:
                rows.append(
                    (
                        __("Game Code and Revision:"),
                        "{:s}-{:s}".format(data["db"]["gc"], str(data["version"])),
                    ),
                )
            elif len(data["game_code"]) > 0:
                rows.append(
                    (
                        __("Game Code and Revision:"),
                        "{:s}-{:s}".format(data["game_code"], str(data["version"])),
                    ),
                )

            rows.append((__("Real Time Clock:"), data["rtc_string"]))

            if data["logo_correct"]:
                rows.append((__("Boot Logo:"), c__("Game Data", "OK")))
                bootlogo_path = Path(AppContext.CONFIG_PATH) / "bootlogo_agb.bin"
                if not bootlogo_path.exists():
                    with bootlogo_path.open("wb") as f:
                        f.write(data["raw"][0x04:0xA0])
            else:
                rows.append(
                    (
                        __("Boot Logo:"),
                        ANSI.RED + c__("Game Data", "Invalid") + ANSI.RESET,
                    ),
                )
                bad_read = True

            if data["header_checksum_correct"]:
                rows.append(
                    (
                        __("Header Checksum:"),
                        c__("Game Data", "Valid") + " (0x{:02X})".format(data["header_checksum"]),
                    ),
                )
            else:
                rows.append(
                    (
                        __("Header Checksum:"),
                        ANSI.RED
                        + c__("Game Data", "Invalid")
                        + " (0x{:02X})".format(data["header_checksum"])
                        + ANSI.RESET,
                    ),
                )
                bad_read = True

            db_agb_entry = data["db"]
            rom_checksum_str = None
            rom_size_str = None
            if db_agb_entry is not None:
                if data["rom_size_calc"] < 0x400000:
                    rom_checksum_str = c__("Game Data", "In database") + " (0x{:06X})".format(db_agb_entry["rc"])
                rom_size_str = "{:d} MiB".format(int(db_agb_entry["rs"] / 1024 / 1024))
                data["rom_size"] = db_agb_entry["rs"]
            elif data["rom_size"] != 0:
                rom_checksum_str = c__("Game Data", "No database entry")
                if data["rom_size"] not in RomSizes():
                    data["rom_size"] = 0x2000000
                rom_size_str = "{:d} MiB".format(int(data["rom_size"] / 1024 / 1024))
            else:
                rom_checksum_str = c__("Game Data", "No database entry")
                rom_size_str = c__("Game Data", "Not detected")
                bad_read = True
            if rom_checksum_str is not None:
                rows.append((__("ROM Checksum:"), rom_checksum_str))
            rows.append((__("ROM Size:"), rom_size_str))

            rows.append((__("Save Type:"), self._AgbSaveTypeString(data, db_agb_entry)))

            if (
                data["logo_correct"]
                and isinstance(db_agb_entry, dict)
                and "rs" in db_agb_entry
                and db_agb_entry["rs"] == 0x4000000
                and not self.CONN.IsSupported3dMemory()
            ):
                print(
                    ANSI.YELLOW
                    + "\n"
                    + __(
                        "Warning: This cartridge uses a mapper that may not be completely supported yet. A future version of the {device_name} firmware may add support for it.",
                        device_name=self.CONN.GetFullName(),
                    )
                    + ANSI.RESET,
                )

        max_len: int = max((len(label) for label, _ in rows), default=0)
        for label, value in rows:
            if value is not None:
                s += f"{label.ljust(max_len + 1):s} {value:s}\n"

        return (bad_read, s, data)

    def DetectCartridge(self, limitVoltage: bool = False) -> int | None:
        print(__("Now attempting to auto-detect the flashcart profile..."))
        if self.CONN.CheckROMStable() is False:
            print(
                ANSI.RED
                + __(
                    "The cartridge connection is unstable!\nPlease clean the cartridge pins, carefully re-align the cartridge and then try again.",
                )
                + ANSI.RESET,
            )
            return -1
        if self.CONN.GetMode() in self.FLASHCARTS and len(self.FLASHCARTS[self.CONN.GetMode()]) == 0:
            print(
                ANSI.RED
                + __(
                    "No flashcart profile configuration files found. Try to restart the application with the “{switch}” command line switch to reset the configuration.",
                    switch="--reset",
                )
                + ANSI.RESET,
            )
            return -2

        header = self.CONN.ReadHeader()
        self.ReadCartridge(header)
        self.CONN._DetectCartridge(args={"limitVoltage": limitVoltage, "checkSaveType": True})  # noqa: SLF001
        ret = self.CONN.INFO.get("detect_cart")
        if not ret or len(ret) < 11:
            print(ANSI.RED + __("Cartridge detection failed.") + ANSI.RESET)
            return -1
        (
            header,
            _,
            save_type,
            save_chip,
            sram_unstable,
            cart_types,
            cart_type_id,
            cfi_s,
            _,
            flash_id,
            detected_size,
        ) = ret

        # Save Type
        save_type = 0 if save_type is None else save_type

        # Cart Type
        cart_type = None
        msg_cart_type = ""
        if self.CONN.GetMode() == "DMG":
            supp_cart_types = self.CONN.GetSupportedCartridgesDMG()
        elif self.CONN.GetMode() == "AGB":
            supp_cart_types = self.CONN.GetSupportedCartridgesAGB()
        else:
            raise NotImplementedError

        if len(cart_types) > 0:
            cart_type = cart_type_id
            for i in range(len(cart_types)):
                if cart_types[i] == cart_type_id:
                    msg_cart_type += "- {:s} ← {:s}\n".format(
                        supp_cart_types[0][cart_types[i]],
                        c__(
                            "Flashcart Profile List “- PROFILE NAME ← selected”",
                            "selected",
                        ),
                    )
                else:
                    msg_cart_type += f"- {supp_cart_types[0][cart_types[i]]:s}\n"
            msg_cart_type: str = msg_cart_type[:-1]

        # Messages
        # Header
        msg_header_s: str = __("Game Title:") + " " + Formatter.title(header["game_title"]) + "\n"

        # Save Type
        msg_save_type_s = ""
        temp = ""
        if save_chip is not None:
            temp: str = f"{AgbSaveTypes(save_type).GetString():s} ({save_chip:s})"
        elif self.CONN.GetMode() == "DMG":
            temp = f"{DmgSaveTypes(index=save_type).GetString():s}"
        elif self.CONN.GetMode() == "AGB":
            temp = f"{AgbSaveTypes(save_type).GetString():s}"
        if save_type == 0:
            if save_chip and "Unknown" in save_chip:
                msg_save_type_s = __("Save Type:") + " " + save_chip + "\n"
            else:
                msg_save_type_s: str = (
                    __("Save Type:") + " " + c__("Save Type", "None or unknown (no save data detected)") + "\n"
                )
        elif sram_unstable and "SRAM" in temp:
            msg_save_type_s = (
                __("Save Type:")
                + " "
                + temp
                + " "
                + ANSI.RED
                + c__("Save Data Access", "not stable or not battery-backed")
                + ANSI.RESET
                + "\n"
            )
        else:
            msg_save_type_s = __("Save Type:") + " " + temp + "\n"

        # Cart Type
        msg_cart_type_s = ""
        msg_flash_size_s = ""
        msg_flash_mapper_s = ""

        if cart_type is not None:
            msg_cart_type_s: str = (
                __("Flashcart Profile:")
                + " "
                + __("Supported flash cartridge - compatible with:")
                + "\n"
                + msg_cart_type
                + "\n\n"
            )

            if detected_size > 0:
                size: int = detected_size
                msg_flash_size_s: str = __("ROM Size:") + " " + Formatter.file_size(size, as_int=True) + "\n"
            elif "flash_size" in supp_cart_types[1][cart_type_id]:
                size = supp_cart_types[1][cart_type_id]["flash_size"]
                msg_flash_size_s = __("ROM Size:") + " " + Formatter.file_size(size, as_int=True) + "\n"

            if self.CONN.GetMode() == "DMG":
                if "mbc" in supp_cart_types[1][cart_type_id]:
                    if supp_cart_types[1][cart_type_id]["mbc"] == "manual":
                        msg_flash_mapper_s: str = __("Mapper Type:") + " " + __("Manual selection") + "\n"
                    elif supp_cart_types[1][cart_type_id]["mbc"] in DMG_Mapper().GetAllMapperIds():
                        msg_flash_mapper_s = (
                            __("Mapper Type:")
                            + " "
                            + DMG_Mapper().GetMapperType(supp_cart_types[1][cart_type_id]["mbc"])
                            + "\n"
                        )
                else:
                    msg_flash_mapper_s = __("Mapper Type:") + " " + c__("Mapper Type", "Default") + " (MBC5)\n"

        elif (len(flash_id.split("\n")) > 2) and (
            (self.CONN.GetMode() == "DMG") or ("dacs_8m" in header and header["dacs_8m"] is not True)
        ):
            msg_cart_type_s = __("Flashcart Profile:") + " " + __("Unknown flash cartridge")
            generic_profiles = (
                ("[     0/90]", "Generic Flash Cartridge (0/90)"),
                ("[   AAA/AA]", "Generic Flash Cartridge (AAA/AA)"),
                ("[   AAA/A9]", "Generic Flash Cartridge (AAA/A9)"),
                ("[WR   / AAA/AA]", "Generic Flash Cartridge (WR/AAA/AA)"),
                ("[WR   / AAA/A9]", "Generic Flash Cartridge (WR/AAA/A9)"),
                ("[WR   / 555/AA]", "Generic Flash Cartridge (WR/555/AA)"),
                ("[WR   / 555/A9]", "Generic Flash Cartridge (WR/555/A9)"),
                ("[AUDIO/ AAA/AA]", "Generic Flash Cartridge (AUDIO/AAA/AA)"),
                ("[AUDIO/ 555/AA]", "Generic Flash Cartridge (AUDIO/555/AA)"),
            )
            try_this = next((profile for marker, profile in generic_profiles if marker in flash_id), "")
            if try_this != "":
                msg_cart_type_s += " " + __(
                    "For ROM writing, you can give the option called “{try_this}” a try at your own risk.",
                    try_this=try_this,
                )
            msg_cart_type_s += "\n"
        else:
            msg_cart_type_s = (
                __("Flashcart Profile:")
                + " "
                + "Generic ROM Cartridge"
                + " ("
                + c__("Flashcart Profile", "not rewritable or not auto-detectable")
                + ")\n"
            )

        msg_flash_id_s = __("Flash ID Check:") + "\n" + flash_id[:-1] + "\n\n"

        if cfi_s != "":
            msg_cfi_s = (
                __(
                    "{common_flash_interface} Data:",
                    common_flash_interface="Common Flash Interface",
                )
                + "\n"
                + cfi_s
                + "\n\n"
            )
        else:
            msg_cfi_s: str = (
                __(
                    "{common_flash_interface} Data:",
                    common_flash_interface="Common Flash Interface",
                )
                + " "
                + c__("Common Flash Interface Data", "No data provided")
                + "\n\n"
            )

        msg: str = "\n\n" + __("The following cartridge configuration was detected:") + "\n\n"
        temp = (
            msg
            + f"{msg_header_s}{msg_flash_size_s}{msg_flash_mapper_s}{msg_save_type_s}\n{msg_flash_id_s}{msg_cfi_s}{msg_cart_type_s}"
        )
        print(temp[:-1])

        return cart_type

    def _ResolveBackupCartType(
        self,
        args: argparse.Namespace,
        header: HeaderData,
        rom_size: int | None,
    ) -> tuple[int, int | None]:
        cart_type = 0
        if args.flashcart_type != "autodetect":
            if self.CONN.GetMode() == "DMG":
                carts = self.CONN.GetSupportedCartridgesDMG()[1]
            elif self.CONN.GetMode() == "AGB":
                carts = self.CONN.GetSupportedCartridgesAGB()[1]
            else:
                raise NotImplementedError

            for i, cart in enumerate(carts):
                if "names" not in cart or cart["type"] != self.CONN.GetMode():
                    continue
                if args.flashcart_type in cart["names"] and "flash_size" in cart:
                    print(
                        __(
                            "Selected flashcart profile: {profile}",
                            profile=args.flashcart_type,
                        )
                        + "\n",
                    )
                    cart_type = i
                    rom_size = cart["flash_size"]
                    break
            if cart_type == 0:
                print(__("Error: Couldn't select the flashcart profile.") + "\n")
        elif self.CONN.GetMode() == "AGB":
            cart_types = self.CONN.GetSupportedCartridgesAGB()
            if "flash_type" in header:
                print(
                    __(
                        "Selected flashcart profile: {profile}",
                        profile=cart_types[0][header["flash_type"]],
                    )
                    + "\n",
                )
                cart_type = header["flash_type"]
            elif header["logo_correct"]:
                for i in range(len(cart_types[0])):
                    if (header["3d_memory"] is True and "3d_memory" in cart_types[1][i]) or (
                        header["vast_fame"] is True and "vast_fame" in cart_types[1][i]
                    ):
                        print(
                            __(
                                "Selected flashcart profile: {profile}",
                                profile=cart_types[0][i],
                            )
                            + "\n",
                        )
                        cart_type = i
                        break
        return cart_type, rom_size

    def BackupROM(self, args: argparse.Namespace, header: HeaderData) -> None:
        mbc = 1
        rom_size = 0

        path: str = generate_filename(mode=self.CONN.GetMode(), header=self.CONN.INFO, settings=None)
        if self.CONN.GetMode() == "DMG":
            if args.dmg_mbc == "auto":
                try:
                    mbc: int = self._GetHeaderInt(header, "mapper_raw")
                    if mbc == 0:
                        mbc = 0x19  # MBC5 default
                except TypeError:
                    print(
                        ANSI.YELLOW
                        + __(
                            "Couldn't determine mapper type, will try to use MBC5. It can also be manually set with the “{switch}” command line switch.",
                            switch="--dmg-mbc",
                        )
                        + ANSI.RESET,
                    )
                    mbc = 0x19
            else:
                mbc = self._ParseDmgMbc(args.dmg_mbc)

            if args.dmg_romsize == "auto":
                try:
                    rom_size: int | None = RomSizes().GetSize(self._GetHeaderInt(header, "rom_size_raw"))
                    if not isinstance(rom_size, int):
                        msg = "Invalid ROM size"
                        raise TypeError(msg)  # noqa: TRY301
                except TypeError:
                    print(
                        ANSI.YELLOW
                        + __(
                            "Couldn't determine ROM size, will use 8{mib}. It can also be manually set with the “{switch}” command line switch.",
                            mib=__(" MiB"),
                            switch="--dmg-romsize",
                        )
                        + ANSI.RESET,
                    )
                    rom_size = 8 * 1024 * 1024
            else:
                rom_size = RomSizes.GetSizeFromCLIName(args.dmg_romsize, mode="DMG")

        elif self.CONN.GetMode() == "AGB":
            if args.agb_romsize == "auto":
                rom_size = header["rom_size"]
            else:
                rom_size = RomSizes.GetSizeFromCLIName(args.agb_romsize, mode="AGB")

        if args.path != "auto":
            path = str(Path(args.path) / path) if Path(args.path).is_dir() else args.path

        if path == "":
            return
        output_path: Path = Path(path).resolve()
        if not args.overwrite and output_path.exists():
            answer: str = (
                input(
                    __(
                        "The target file “{file_path}” already exists.\nDo you want to overwrite it?",
                        file_path=str(output_path),
                    )
                    + " [y/N]: ",
                )
                .strip()
                .lower()
            )
            print()
            if answer != "y":
                print(__("Canceled."))
                return

        try:
            with Path(path).open("ab+"):
                pass
        except PermissionError:
            print(ANSI.RED + __("Couldn't access file “{path}”.", path=path) + ANSI.RESET)
            return
        except FileNotFoundError:
            print(ANSI.RED + __("Couldn't find file “{path}”.", path=path) + ANSI.RESET)
            return

        print(
            __(
                "The ROM will now be read and saved to “{path}”.",
                path=str(output_path),
            ),
        )
        if self.CONN.GetMode() == "DMG":
            if mbc in DMG_Mapper().GetAllMapperIds():
                print(
                    __(
                        "Mapper Type “{mapper_type}” is used.",
                        mapper_type=DMG_Mapper().GetMapperType(mbc),
                    ),
                )
            else:
                print(
                    __(
                        "Mapper Type {mapper_type_value} is used.",
                        mapper_type_value=f"0x{mbc:02X}",
                    ),
                )

        print()

        cart_type, rom_size = self._ResolveBackupCartType(args, header, rom_size)
        self.CONN.TransferData(
            args={
                "mode": 1,
                "path": path,
                "mbc": mbc,
                "rom_size": rom_size,
                "agb_rom_size": rom_size,
                "start_addr": 0,
                "fast_read_mode": True,
                "cart_type": cart_type,
            },
            signal=self.PROGRESS.SetProgress,
        )

    def _LoadFlashROMFile(
        self,
        args: argparse.Namespace,
        carts: Sequence[Mapping[str, Any]],
        cart_type: int,
    ) -> tuple[Path, bytearray] | None:
        rom_path = Path(args.path)
        try:
            rom_size: int = rom_path.stat().st_size
            if rom_size > 0x20000000:  # reject too large files to avoid exploding RAM
                print(
                    ANSI.RED
                    + __(
                        "ROM files bigger than 512{mib} are not supported.",
                        mib=__(" MiB"),
                    )
                    + ANSI.RESET,
                )
                return None
            if rom_size < 0x400:
                print(
                    ANSI.RED
                    + __(
                        "ROM files smaller than 1{kib} are not supported.",
                        kib=__(" KiB"),
                    )
                    + ANSI.RESET,
                )
                return None

            with rom_path.open("rb") as file:
                if rom_path.suffix.lower() == ".isx":
                    buffer: bytearray = from_isx(bytearray(file.read()))
                else:
                    buffer = bytearray(file.read(0x1000))
            if "flash_size" in carts[cart_type] and rom_size > carts[cart_type]["flash_size"]:
                print(
                    ANSI.YELLOW
                    + __(
                        "The selected flashcart profile seems to support ROMs that are up to {max_size} in size, but the file you selected is {file_size}. You can still give it a try, but it's possible that it's too large which may cause the ROM writing to fail.",
                        max_size=Formatter.file_size(carts[cart_type]["flash_size"]),
                        file_size=Formatter.file_size(rom_size),
                    )
                    + ANSI.RESET,
                )
                answer: str = input(__("Do you want to continue?") + " [y/N]: ").strip().lower()
                print()
                if answer != "y":
                    print(__("Canceled."))
                    return None

        except PermissionError:
            print(ANSI.RED + __("Couldn't access file “{path}”.", path=args.path) + ANSI.RESET)
            return None
        except FileNotFoundError:
            print(ANSI.RED + __("Couldn't find file “{path}”.", path=args.path) + ANSI.RESET)
            return None
        return rom_path, buffer

    def _GetFlashVoltageOverrides(
        self,
        args: argparse.Namespace,
        carts: Sequence[Mapping[str, Any]],
        cart_type: int,
    ) -> tuple[float | Literal[False], float | Literal[False], bool]:
        override_voltage: float | Literal[False] = False
        voltage_fallback: float | Literal[False] = False
        device_voltage_locked: bool = self.CONN.CanSetVoltageByAutoswitch() and not self.CONN.CanSetVoltageByCode()
        if device_voltage_locked:
            return override_voltage, voltage_fallback, device_voltage_locked
        if args.force_5v is True:
            override_voltage = 5
        elif "voltage_variants" in carts[cart_type] and carts[cart_type]["voltage"] == 3.3:
            print(
                __(
                    "The selected flashcart profile usually flashes fine with 3.3V, however sometimes it may require 5V. You can use the “{switch}” command line switch if necessary. Please note that 5V can be unsafe for some flash chips.",
                    switch="--force-5v",
                ),
            )
        elif carts[cart_type].get("voltage") == 5 and has_3v_compatible_profile(carts, cart_type):
            # Some PCBs share the same flash chip but need 3.3V; try 3.3V silently first,
            # fall back to 5V if writing fails.
            override_voltage = 3.3
            voltage_fallback = 5
        return override_voltage, voltage_fallback, device_voltage_locked

    def _PromptBootLogoFix(self, header: Mapping[str, Any], mbc: int) -> bool | bytearray:
        mode = self.CONN.GetMode()
        if header["logo_correct"] or (mode == "DMG" and mbc in (0x203, 0x205)):
            return False

        print(
            ANSI.YELLOW
            + __(
                "Warning: The ROM file you selected will not boot on actual hardware due to invalid boot logo data.",
            )
            + ANSI.RESET,
        )
        bootlogo = None
        if mode == "DMG":
            bootlogo_path = Path(AppContext.CONFIG_PATH) / "bootlogo_dmg.bin"
            if bootlogo_path.exists():
                with bootlogo_path.open("rb") as f:
                    bootlogo = bytearray(f.read(0x30))
        elif mode == "AGB":
            bootlogo_path = Path(AppContext.CONFIG_PATH) / "bootlogo_agb.bin"
            if bootlogo_path.exists():
                with bootlogo_path.open("rb") as f:
                    bootlogo = bytearray(f.read(0x9C))
        if bootlogo is None:
            dprint(__("Couldn't find boot logo file in configuration folder."))
            return False

        answer = input(__("Fix the boot logo before continuing?") + " [Y/n]: ").strip().lower()
        print()
        return bootlogo if answer != "n" else False

    def _PromptHeaderChecksumFix(self, header: Mapping[str, Any], mbc: int) -> bool:
        mode = self.CONN.GetMode()
        should_prompt = not header["header_checksum_correct"] and (
            mode == "AGB" or (mode == "DMG" and mbc not in (0x203, 0x205))
        )
        if not should_prompt:
            return False

        print(
            ANSI.YELLOW
            + __(
                "Warning: The ROM file you selected will not boot on actual hardware due to an invalid header checksum (expected {expected} instead of {actual}).",
                expected="0x{:02X}".format(header["header_checksum_calc"]),
                actual="0x{:02X}".format(header["header_checksum"]),
            )
            + ANSI.RESET,
        )
        answer = input(__("Fix the header checksum before continuing?") + " [Y/n]: ").strip().lower()
        print()
        return answer != "n"

    def FlashROM(self, args: argparse.Namespace, header: HeaderData) -> None:
        del header
        mbc = 0

        mode = self.CONN.GetMode()
        if mode == "DMG":
            carts = self.CONN.GetSupportedCartridgesDMG()[1]
        elif mode == "AGB":
            carts = self.CONN.GetSupportedCartridgesAGB()[1]
        else:
            return

        cart_type = 0

        for i in range(len(carts)):
            if "names" not in carts[i]:
                continue
            if carts[i]["type"] != mode:
                continue
            if args.flashcart_type in carts[i]["names"]:
                print(
                    __(
                        "Selected flashcart profile: {profile}",
                        profile=args.flashcart_type,
                    ),
                )
                cart_type = i
                break

        if cart_type <= 0 and args.flashcart_type == "autodetect":
            cart_type = self.DetectCartridge()
            if cart_type is None:
                cart_type = 0
            if cart_type == 0:
                msg_5v = ""
                if mode == "DMG":
                    msg_5v = __(
                        "If your flash cartridge requires 5V to work, you can use the “{switch}” command line switch, however please note that 5V can be unsafe for some flash chips.",
                        switch="--force-5v",
                    )
                print(
                    "\n"
                    + ANSI.RED
                    + __(
                        "Auto-detection failed. Please use the “{switch}” command line switch to select the flashcart profile manually.",
                        switch="--flashcart-type",
                    )
                    + "\n"
                    + ANSI.RESET
                    + msg_5v
                    + ANSI.RESET,
                )
                return
            if cart_type < 0:
                return
        elif cart_type == 0 and args.flashcart_type != "autodetect":
            print(
                ANSI.RED
                + __(
                    "Couldn't find the selected flashcart profile “{profile}”. Please make sure the correct platform is selected and copy the exact name from the configuration files located in {config_path}.",
                    profile=args.flashcart_type,
                    config_path=AppContext.CONFIG_PATH,
                )
                + ANSI.RESET,
            )
            return

        if args.path == "auto":
            print(ANSI.RED + __("No ROM file for writing was selected.") + ANSI.RESET)
            return
        path = args.path
        loaded_rom = self._LoadFlashROMFile(args, carts, cart_type)
        if loaded_rom is None:
            return
        rom_path, buffer = loaded_rom

        override_voltage, voltage_fallback, device_voltage_locked = self._GetFlashVoltageOverrides(
            args,
            carts,
            cart_type,
        )

        prefer_chip_erase = args.prefer_chip_erase is True
        if (
            not prefer_chip_erase
            and "chip_erase" in carts[cart_type]["commands"]
            and "sector_erase" in carts[cart_type]["commands"]
        ):
            print(
                __(
                    "This flash cartridge supports both Sector Erase and Full Chip Erase methods. You can use the “{switch}” command line switch if necessary.",
                    switch="--prefer-chip-erase",
                ),
            )

        verify_write = args.no_verify_write is False
        compare_sectors = args.compare_sectors is True

        fix_bootlogo: bool | bytearray = False
        if self.CONN.GetMode() == "DMG":
            hdr = RomFileDMG(buffer).GetHeader()

            mbc = 0x19  # MBC5 default
            if "mbc" in carts[cart_type]:
                if carts[cart_type]["mbc"] == "manual":
                    if args.dmg_mbc != "auto":
                        mbc = self._ParseDmgMbc(args.dmg_mbc)
                elif isinstance(carts[cart_type]["mbc"], int):
                    mbc = carts[cart_type]["mbc"]
                else:
                    mbc = self._ParseDmgMbc(args.dmg_mbc)

        elif self.CONN.GetMode() == "AGB":
            hdr = RomFileAGB(buffer).GetHeader()
        else:
            raise NotImplementedError

        fix_bootlogo = self._PromptBootLogoFix(hdr, mbc)
        fix_header = self._PromptHeaderChecksumFix(hdr, mbc)

        print()
        v: float = override_voltage or carts[cart_type]["voltage"]
        print(
            __(
                "The following ROM file will now be written to the flash cartridge at {voltage}V:",
                voltage=str(v),
            )
            + "\n"
            + str(rom_path.resolve()),
        )
        if self.CONN.GetMode() == "DMG":
            if mbc in DMG_Mapper().GetAllMapperIds():
                print(
                    __(
                        "Mapper Type “{mapper_type}” is used.",
                        mapper_type=DMG_Mapper().GetMapperType(mbc),
                    ),
                )
            else:
                print(
                    __(
                        "Mapper Type {mapper_type_value} is used.",
                        mapper_type_value=f"0x{mbc:02X}",
                    ),
                )

        if (
            (v == 3.3 or "voltage_variants" in carts[cart_type])
            and device_voltage_locked
            and self.CONN.GetMode() == "DMG"
        ):
            print()
            print(
                ANSI.YELLOW
                + __(
                    "Warning: A 3.3V flashcart profile is selected, but your device is fixed to a 5V supply in Game Boy mode. Writing to a 3.3V flash chip at 5V may cause overvoltage issues.",
                )
                + ANSI.RESET,
            )
            answer = input(__("Do you want to continue?") + " [y/N]: ").strip().lower()
            if answer != "y":
                print(__("Canceled."))
                return

        print()
        if len(buffer) > 0x1000:
            transfer_args = {
                "mode": 4,
                "path": "",
                "buffer": buffer,
                "cart_type": cart_type,
                "override_voltage": override_voltage,
                "prefer_chip_erase": prefer_chip_erase,
                "fast_read_mode": True,
                "verify_write": verify_write,
                "fix_header": fix_header,
                "fix_bootlogo": fix_bootlogo,
                "mbc": mbc,
                "compare_sectors": compare_sectors,
                "voltage_fallback": voltage_fallback,
            }
        else:
            transfer_args = {
                "mode": 4,
                "path": path,
                "cart_type": cart_type,
                "override_voltage": override_voltage,
                "prefer_chip_erase": prefer_chip_erase,
                "fast_read_mode": True,
                "verify_write": verify_write,
                "fix_header": fix_header,
                "fix_bootlogo": fix_bootlogo,
                "mbc": mbc,
                "compare_sectors": compare_sectors,
                "voltage_fallback": voltage_fallback,
            }
        self.CONN.TransferData(signal=self.PROGRESS.SetProgress, args=transfer_args)

        buffer = None

    def _ConfirmSaveAction(self, args: argparse.Namespace, target_path: Path) -> bool:
        if args.action == "backup-save":
            if not args.overwrite and target_path.exists():
                answer: str = (
                    input(
                        __(
                            "The target file “{file_path}” already exists.\nDo you want to overwrite it?",
                            file_path=str(target_path),
                        )
                        + " [y/N]: ",
                    )
                    .strip()
                    .lower()
                )
                print()
                if answer != "y":
                    print(__("Canceled."))
                    return False
            print(
                __("The cartridge save data will now be read and saved to the following file:")
                + "\n"
                + str(target_path),
            )
        elif args.action == "restore-save":
            if not args.overwrite:
                answer = (
                    input(
                        __("Do you want to overwrite the existing save data that's currently on the cartridge?")
                        + " [y/N]: ",
                    )
                    .strip()
                    .lower()
                )
                if answer != "y":
                    print(__("Canceled."))
                    return False
            print(
                __("The following save data file will now be written to the cartridge:") + "\n" + str(target_path),
            )
        elif args.action == "erase-save":
            if not args.overwrite:
                answer = (
                    input(__("Do you really want to erase the save data from the cartridge?") + " [y/N]: ")
                    .strip()
                    .lower()
                )
                if answer != "y":
                    print(__("Canceled."))
                    return False
            print(__("The cartridge save data will now be erased from the cartridge."))
        elif args.action == "debug-test-save":
            print(
                __("The cartridge save data size will now be examined.")
                + "\n"
                + __("Note: This is for debug use only.")
                + "\n",
            )
        return True

    def _ResolveSaveConfiguration(
        self,
        args: argparse.Namespace,
        header: HeaderData,
    ) -> tuple[int, int, int | None] | None:
        cart_type = 0
        if self.CONN.GetMode() == "DMG":
            if args.dmg_mbc == "auto":
                try:
                    mbc: int = self._GetHeaderInt(header, "mapper_raw")
                    if mbc == 0:
                        mbc = 0x19  # MBC5 default
                except TypeError:
                    print(
                        ANSI.YELLOW
                        + __(
                            "Couldn't determine mapper type, will try to use MBC5. It can also be manually set with the “{switch}” command line switch.",
                            switch="--dmg-mbc",
                        )
                        + ANSI.RESET,
                    )
                    mbc = 0x19
            else:
                mbc = self._ParseDmgMbc(args.dmg_mbc)

            if args.dmg_savetype == "auto":
                try:
                    if header["mapper_raw"] == 0x06:  # MBC2
                        save_type = 0x100
                    elif header["mapper_raw"] == 0x22 and header["game_title"] in (
                        "KORO2 KIRBYKKKJ",
                        "KIRBY TNT_KTNE",
                    ):  # MBC7 Kirby
                        save_type = 0x101
                    elif header["mapper_raw"] == 0x22 and header["game_title"] in (
                        "CMASTER_KCEJ"
                    ):  # MBC7 Command Master
                        save_type = 0x102
                    elif header["mapper_raw"] == 0xFD:  # TAMA5
                        save_type = 0x103
                    elif header["mapper_raw"] == 0x20:  # MBC6
                        save_type = 0x104
                    else:
                        save_type = header["ram_size_raw"]
                except KeyError, TypeError, ValueError, IndexError:
                    save_type = 0
            elif args.dmg_savetype == "batteryless":
                save_type = 0x205
            else:
                save_type: int | None = DmgSaveTypes.GetMbcFromCLIName(args.dmg_savetype) or 0

            if save_type == 0:
                print(
                    ANSI.RED
                    + __(
                        "Unable to auto-detect the save size. Please use the “{switch}” command line switch to manually select it.",
                        switch="--dmg-savetype",
                    )
                    + ANSI.RESET,
                )
                return None

            if save_type == 0x204:
                cart_type: int | None = self.DetectCartridge()

        elif self.CONN.GetMode() == "AGB":
            if args.agb_savetype == "auto":
                save_type = header["save_type"]
            elif args.agb_savetype == "batteryless":
                save_type = 9
            else:
                save_type = AgbSaveTypes.GetIndexFromCLIName(args.agb_savetype)

            mbc = 0
            if save_type in {0, None}:
                print(
                    ANSI.RED
                    + __(
                        "Unable to auto-detect the save type. Please use the “{switch}” command line switch to manually select it.",
                        switch="--agb-savetype",
                    )
                    + ANSI.RESET,
                )
                return None
        else:
            return None

        if not isinstance(save_type, int):
            return None
        return mbc, save_type, cart_type

    def _PrepareEReaderCalibration(
        self,
        args: argparse.Namespace,
        path: str,
    ) -> tuple[bool, bytearray | None]:
        if self.CONN.GetFWBuildDate() == "":  # Legacy Mode
            print(__("This cartridge is not supported in Legacy Mode."))
            return False, None
        self.CONN.ReadHeader()
        if "ereader_calibration" not in self.CONN.INFO:
            print(__("Note: No existing e-Reader calibration data found."))
            return True, None

        with Path(path).open("rb") as file:
            buffer = bytearray(file.read())
        if buffer[0xD000:0xF000] == self.CONN.INFO["ereader_calibration"]:
            return True, buffer
        if args.keep_calibration:
            if args.action == "erase-save":
                args.action = "restore-save"
            print(__("Note: Keeping existing e-Reader calibration data."))
            buffer[0xD000:0xF000] = self.CONN.INFO["ereader_calibration"]
        else:
            print(__("Note: Overwriting existing e-Reader calibration data."))
        return True, buffer

    def _DebugTestSave(self, mbc: int, save_type: int) -> None:
        self.ARGS["debug"] = True
        config_path = Path(AppContext.CONFIG_PATH)
        test1_path: Path = config_path / "test1.bin"
        test2_path: Path = config_path / "test2.bin"
        test3_path: Path = config_path / "test3.bin"
        test4_path: Path = config_path / "test4.bin"

        print(__("Making a backup of the original save data."))
        ret = self.CONN.TransferData(
            args={
                "mode": 2,
                "path": str(test1_path),
                "mbc": mbc,
                "save_type": save_type,
            },
            signal=self.PROGRESS.SetProgress,
        )
        if ret is False:
            return
        time.sleep(0.1)
        print(__("Writing random data."))
        test2 = bytearray(os.urandom(test1_path.stat().st_size))
        with test2_path.open("wb") as f:
            f.write(test2)
        self.CONN.TransferData(
            args={
                "mode": 3,
                "path": str(test2_path),
                "mbc": mbc,
                "save_type": save_type,
                "erase": False,
            },
            signal=self.PROGRESS.SetProgress,
        )
        time.sleep(0.1)
        print(__("Reading back and comparing data."))
        self.CONN.TransferData(
            args={
                "mode": 2,
                "path": str(test3_path),
                "mbc": mbc,
                "save_type": save_type,
            },
            signal=self.PROGRESS.SetProgress,
        )
        time.sleep(0.1)
        with test3_path.open("rb") as f:
            test3 = bytearray(f.read())
        if self.CONN.CanPowerCycleCart():
            print("\n" + __("Power cycling."))
            for _ in range(5):
                self.CONN.CartPowerCycle()
                time.sleep(0.1)
            self.CONN.ReadHeader(checkRtc=False)
        time.sleep(0.2)
        print("\n" + __("Reading back and comparing data again."))
        self.CONN.TransferData(
            args={
                "mode": 2,
                "path": str(test4_path),
                "mbc": mbc,
                "save_type": save_type,
            },
            signal=self.PROGRESS.SetProgress,
        )
        time.sleep(0.1)
        with test4_path.open("rb") as f:
            test4 = bytearray(f.read())
        print(__("Restoring original save data."))
        self.CONN.TransferData(
            args={
                "mode": 3,
                "path": str(test1_path),
                "mbc": mbc,
                "save_type": save_type,
                "erase": False,
            },
            signal=self.PROGRESS.SetProgress,
        )
        time.sleep(0.1)

        if mbc == 6:
            for i in range(len(test2)):
                test2[i] &= 0x0F
                test3[i] &= 0x0F
                test4[i] &= 0x0F

        if test2 != test4:
            diffcount = 0
            for i in range(len(test2)):
                if test2[i] != test4[i]:
                    diffcount += 1
            print("\n" + ANSI.RED + __("Differences found:") + str(diffcount) + ANSI.RESET)
        if test3 != test4:
            diffcount = 0
            for i in range(len(test3)):
                if test3[i] != test4[i]:
                    diffcount += 1
            print(
                "\n"
                + ANSI.RED
                + __("Differences found between two consecutive readbacks:")
                + str(diffcount)
                + ANSI.RESET,
            )
            input("")

        found_offset: int = test2.find(test3[0:512])
        if found_offset < 0:
            if self.CONN.GetMode() == "AGB":
                print(
                    "\n"
                    + ANSI.RED
                    + __(
                        "It was not possible to save any data to the cartridge using save type “{save_type}”.",
                        save_type=AgbSaveTypes(save_type).GetString(),
                    )
                    + ANSI.RESET,
                )
            else:
                print("\n" + ANSI.RED + __("It was not possible to save any data to the cartridge.") + ANSI.RESET)
            return

        if found_offset == 0 and test2 != test3:  # Pokémon Crystal JPN
            found_length: int = 0
            for _, (expected, actual) in enumerate(zip(test2, test3, strict=False)):
                if expected != actual:
                    break
        else:
            found_length = len(test2) - found_offset

        if self.CONN.GetMode() == "DMG":
            print(
                "\n"
                + ANSI.GREEN
                + __(
                    "Done! The writable save data size is {data_writable} out of {data_checked} checked.",
                    data_writable=Formatter.file_size(found_length),
                    data_checked=Formatter.file_size(DmgSaveTypes(mbc=save_type).GetSize()),
                )
                + ANSI.RESET,
            )
        elif self.CONN.GetMode() == "AGB":
            print(
                "\n"
                + ANSI.GREEN
                + __(
                    "Done! The writable save data size using save type “{save_type}” is {data_writable}.",
                    save_type=AgbSaveTypes(save_type).GetString(),
                    data_writable=Formatter.file_size(found_length),
                )
                + ANSI.RESET,
            )

    def BackupRestoreRAM(
        self,
        args: argparse.Namespace,
        header: HeaderData,
    ) -> None:
        add_date_time: bool = args.save_filename_add_datetime is True
        rtc: bool = args.store_rtc is True

        path_datetime = ""
        if add_date_time:
            path_datetime: str = "_{:s}".format(datetime.datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S"))

        path = generate_filename(mode=self.CONN.GetMode(), header=self.CONN.INFO, settings=None)
        path = str(Path(path).with_suffix(""))
        path += f"{path_datetime:s}.sav"

        configuration: tuple[int, int, int | None] | None = self._ResolveSaveConfiguration(args, header)
        if configuration is None:
            return
        mbc, save_type, cart_type = configuration

        if args.path != "auto":
            path = str(Path(args.path) / path) if Path(args.path).is_dir() else args.path

        if path == "":
            return

        # Batteryless SRAM saves are stored inside the ROM flash, so they take a
        # separate code path (BackupROM/FlashROM with bl_offset) instead of the
        # normal SRAM/EEPROM save transfer.
        if (self.CONN.GetMode() == "DMG" and save_type == 0x205) or (self.CONN.GetMode() == "AGB" and save_type == 9):
            self._BatterylessSRAM(args=args, header=header, mbc=mbc, save_type=save_type, path=path)
            return

        buffer = None
        target_path: Path = Path(path).resolve()
        if not self._ConfirmSaveAction(args, target_path):
            return

        if self.CONN.GetMode() == "DMG":
            if mbc in DMG_Mapper().GetAllMapperIds():
                print(
                    __(
                        "Mapper Type “{mapper_type}” is used.",
                        mapper_type=DMG_Mapper().GetMapperType(mbc),
                    ),
                )
            else:
                print(
                    __(
                        "Mapper Type {mapper_type_value} is used.",
                        mapper_type_value=f"0x{mbc:02X}",
                    ),
                )

        mode = self.CONN.GetMode()
        if mode == "AGB" and args.action in ("restore-save", "erase-save") and self.CONN.INFO.get("ereader") is True:
            continue_write, buffer = self._PrepareEReaderCalibration(args, path)
            if not continue_write:
                return
        if mode == "AGB":
            print(
                __(
                    "Using Save Type “{save_type}”.",
                    save_type=AgbSaveTypes(save_type).GetString(),
                ),
            )
        elif (
            mode == "DMG"
            and rtc
            and header["mapper_raw"]
            in (
                0x10,
                0x110,
                0xFE,
            )
        ):  # RTC of MBC3, MBC30, HuC-3
            print(__("Real Time Clock register values will also be written if applicable/possible."))

        try:
            if args.action == "backup-save":
                with Path(path).open("ab+"):
                    pass
            elif args.action == "restore-save":
                with Path(path).open("rb+"):
                    pass
        except PermissionError:
            print(ANSI.RED + __("Couldn't access file “{path}”.", path=path) + ANSI.RESET)
            return
        except FileNotFoundError:
            print(ANSI.RED + __("Couldn't find file “{path}”.", path=path) + ANSI.RESET)
            return

        print()
        if args.action == "backup-save":
            self.CONN.TransferData(
                args={
                    "mode": 2,
                    "path": path,
                    "mbc": mbc,
                    "save_type": save_type,
                    "rtc": rtc,
                },
                signal=self.PROGRESS.SetProgress,
            )
        elif args.action == "restore-save":
            verify_write: bool = args.no_verify_write is False
            targs = {
                "mode": 3,
                "path": path,
                "mbc": mbc,
                "save_type": save_type,
                "erase": False,
                "rtc": rtc,
                "verify_write": verify_write,
                "cart_type": cart_type,
            }
            if buffer is not None:
                targs["buffer"] = buffer
                targs["path"] = None
            self.CONN.TransferData(args=targs, signal=self.PROGRESS.SetProgress)
        elif args.action == "erase-save":
            self.CONN.TransferData(
                args={
                    "mode": 3,
                    "path": path,
                    "mbc": mbc,
                    "save_type": save_type,
                    "erase": True,
                    "rtc": rtc,
                    "cart_type": cart_type,
                },
                signal=self.PROGRESS.SetProgress,
            )
        elif args.action == "debug-test-save":  # debug
            self._DebugTestSave(mbc, save_type)

    def _ResolveBLArgs(
        self,
        args: argparse.Namespace,
        header: HeaderData,
    ) -> BatterylessArgs | None:
        mode = self.CONN.GetMode()
        bl_offset = None
        bl_size = None
        bl_layout = None

        # 1) CLI flags take precedence
        if args.bl_offset != "auto":
            try:
                txt = args.bl_offset.strip()
                bl_offset: int | None = int(txt, 16) if txt.lower().startswith("0x") else int(txt, 0)
            except ValueError:
                print(
                    ANSI.RED
                    + __(
                        "Invalid value for {switch}: {value}",
                        switch="--bl-offset",
                        value=args.bl_offset,
                    )
                    + ANSI.RESET,
                )
                return None
        if args.bl_size != "auto":
            try:
                txt = args.bl_size.strip()
                bl_size = int(txt, 16) if txt.lower().startswith("0x") else int(txt, 0)
            except ValueError:
                print(
                    ANSI.RED
                    + __(
                        "Invalid value for {switch}: {value}",
                        switch="--bl-size",
                        value=args.bl_size,
                    )
                    + ANSI.RESET,
                )
                return None
        if mode == "DMG" and args.bl_layout != "auto":
            bl_layout = int(args.bl_layout)

        # 2) Previously auto-detected on this connection
        if (
            (bl_offset is None or bl_size is None)
            and "dump_info" in self.CONN.INFO
            and "batteryless_sram" in self.CONN.INFO["dump_info"]
        ):
            detected = self.CONN.INFO["dump_info"]["batteryless_sram"]
            if bl_offset is None and "bl_offset" in detected:
                bl_offset = detected["bl_offset"]
            if bl_size is None and "bl_size" in detected:
                bl_size = detected["bl_size"]
            if mode == "DMG" and bl_layout is None and "bl_layout" in detected:
                bl_layout = detected["bl_layout"]

        # 3) DMG title-based fallback database
        if mode == "DMG" and (bl_offset is None or bl_size is None):
            preselect = header.get("batteryless_sram") or RomFileDMG.GetBatterylessSramConfig(header)
            if preselect is not None:
                if bl_offset is None:
                    bl_offset = preselect["bl_offset"]
                if bl_size is None:
                    bl_size = preselect["bl_size"]
                if bl_layout is None and "bl_layout" in preselect:
                    bl_layout = preselect["bl_layout"]

        if bl_offset is None or bl_size is None:
            print(
                ANSI.RED
                + __(
                    "Batteryless SRAM offset and size could not be auto-detected. Use the “{switch_offset}” and “{switch_size}” command line switches to specify them manually.",
                    switch_offset="--bl-offset",
                    switch_size="--bl-size",
                )
                + ANSI.RESET,
            )
            return None
        if mode == "DMG" and bl_layout is None:
            bl_layout = 0  # continuous

        bl_args = {"bl_offset": bl_offset, "bl_size": bl_size}
        if mode == "DMG":
            bl_args["bl_layout"] = bl_layout
        return bl_args

    def _BatterylessSRAM(
        self,
        args: argparse.Namespace,
        header: HeaderData,
        mbc: int,
        save_type: int,
        path: str,
    ) -> None:
        del save_type
        mode = self.CONN.GetMode()

        if args.action == "debug-test-save":
            print(ANSI.RED + __("Stress test is not supported for this save type.") + ANSI.RESET)
            return

        # Resolve Batteryless SRAM region (offset, size, layout for DMG)
        bl_args: BatterylessArgs | None = self._ResolveBLArgs(args, header)
        if bl_args is None:
            return
        bl_offset: int = bl_args["bl_offset"]
        bl_size: int = bl_args["bl_size"]

        print(__("Batteryless SRAM Mode"))
        print(
            "- "
            + __("Location:")
            + f" 0x{bl_offset:X}-0x{bl_offset + bl_size - 1:X} ({Formatter.file_size(bl_size, as_int=True):s})",
        )
        if mode == "DMG":
            layout_names: list[str] = [
                __("Continuous"),
                __("First half of ROM bank"),
                __("Second half of ROM bank"),
            ]
            print("- " + __("Layout:") + " " + layout_names[bl_args["bl_layout"]])
        print()

        if args.action == "backup-save":
            self._BackupBatterylessSRAM(args, path, mbc, bl_size, bl_args)
            return

        # restore-save / erase-save: write into ROM flash, so a flash cart profile is required.
        erase = args.action == "erase-save"
        cart_type: int | None = self._ResolveFlashcartType(args)
        if cart_type is None:
            return

        if args.action == "restore-save":
            if not args.overwrite:
                answer: str = (
                    input(
                        __("Do you want to overwrite the existing Batteryless SRAM save data on the cartridge?")
                        + " [y/N]: ",
                    )
                    .strip()
                    .lower()
                )
                print()
                if answer != "y":
                    print(__("Canceled."))
                    return
            print(
                __("The following save data file will now be written to the cartridge's Batteryless SRAM region:")
                + "\n"
                + str(Path(path).resolve()),
            )
            try:
                with Path(path).open("rb+"):
                    pass
            except PermissionError, FileNotFoundError:
                print(ANSI.RED + __("Couldn't access file “{path}”.", path=path) + ANSI.RESET)
                return
        elif erase:
            if not args.overwrite:
                answer = (
                    input(
                        __("Do you really want to erase the Batteryless SRAM save data from the cartridge?")
                        + " [y/N]: ",
                    )
                    .strip()
                    .lower()
                )
                print()
                if answer != "y":
                    print(__("Canceled."))
                    return
            print(__("The Batteryless SRAM save data will now be erased from the cartridge."))

        if mode == "DMG" and self.CONN.CanSetVoltageByAutoswitch() and not self.CONN.CanSetVoltageByCode():
            bl_carts = self.CONN.GetSupportedCartridgesDMG()[1]
            if isinstance(bl_carts[cart_type], dict) and (
                bl_carts[cart_type].get("voltage") == 3.3 or "voltage_variants" in bl_carts[cart_type]
            ):
                print()
                print(
                    ANSI.YELLOW
                    + __(
                        "Warning: A 3.3V flashcart profile is selected, but your device is fixed to a 5V supply in Game Boy mode. Writing to a 3.3V flash chip at 5V may cause overvoltage issues.",
                    )
                    + ANSI.RESET,
                )
                answer = input(__("Do you want to continue?") + " [y/N]: ").strip().lower()
                if answer != "y":
                    print(__("Canceled."))
                    return

        print()
        verify_write: bool = args.no_verify_write is False
        targs = {
            "mode": 4,
            "path": path,
            "cart_type": cart_type,
            "override_voltage": False,
            "prefer_chip_erase": False,
            "fast_read_mode": True,
            "verify_write": verify_write,
            "fix_header": False,
            "fix_bootlogo": False,
            "mbc": mbc,
            "compare_sectors": args.compare_sectors is True,
            "bl_save": True,
            "flash_offset": bl_offset,
            "flash_size": bl_size,
        }
        targs.update(bl_args)
        if erase:
            targs["path"] = ""
            targs["buffer"] = bytearray([0xFF] * bl_size)
        self.CONN.TransferData(args=targs, signal=self.PROGRESS.SetProgress)

    def _BackupBatterylessSRAM(
        self,
        args: argparse.Namespace,
        path: str,
        mbc: int,
        bl_size: int,
        bl_args: BatterylessArgs,
    ) -> None:
        target_path: Path = Path(path).resolve()
        if not args.overwrite and target_path.exists():
            answer: str = (
                input(
                    __(
                        "The target file “{file_path}” already exists.\nDo you want to overwrite it?",
                        file_path=str(target_path),
                    )
                    + " [y/N]: ",
                )
                .strip()
                .lower()
            )
            print()
            if answer != "y":
                print(__("Canceled."))
                return
        print(
            __("The Batteryless SRAM save data will now be read and saved to the following file:")
            + "\n"
            + str(target_path),
        )
        try:
            with Path(path).open("ab+"):
                pass
        except PermissionError, FileNotFoundError:
            print(ANSI.RED + __("Couldn't access file “{path}”.", path=path) + ANSI.RESET)
            return
        print()
        targs = {
            "mode": 1,
            "path": path,
            "mbc": mbc,
            "rom_size": bl_size,
            "agb_rom_size": bl_size,
            "fast_read_mode": True,
            "cart_type": 0,
        }
        targs.update(bl_args)
        self.CONN.TransferData(args=targs, signal=self.PROGRESS.SetProgress)

    def _ResolveFlashcartType(self, args: argparse.Namespace) -> int | None:
        mode = self.CONN.GetMode()
        if mode == "DMG":
            carts = self.CONN.GetSupportedCartridgesDMG()[1]
        elif mode == "AGB":
            carts = self.CONN.GetSupportedCartridgesAGB()[1]
        else:
            return None

        if args.flashcart_type != "autodetect":
            for i in range(len(carts)):
                if not isinstance(carts[i], dict):
                    continue
                if "names" not in carts[i]:
                    continue
                if carts[i].get("type") != mode:
                    continue
                if args.flashcart_type in carts[i]["names"]:
                    print(
                        __(
                            "Selected flashcart profile: {profile}",
                            profile=args.flashcart_type,
                        ),
                    )
                    return i
            print(
                ANSI.RED
                + __(
                    "Couldn't find the selected flashcart profile “{profile}”. Please make sure the correct platform is selected and copy the exact name from the configuration files located in {config_path}.",
                    profile=args.flashcart_type,
                    config_path=AppContext.CONFIG_PATH,
                )
                + ANSI.RESET,
            )
            return None

        cart_type = self.DetectCartridge()
        if cart_type is None or cart_type == 0 or not isinstance(cart_type, int) or cart_type < 0:
            print(
                "\n"
                + ANSI.RED
                + __(
                    "Auto-detection failed. Please use the “{switch}” command line switch to select the flashcart profile manually.",
                    switch="--flashcart-type",
                )
                + ANSI.RESET,
            )
            return None
        return cart_type

    def _LoadFirmwareInfo(self, file_name: str | Path) -> tuple[str, int]:
        """Load and validate display metadata from a firmware archive."""
        with zipfile.ZipFile(file_name) as archive, archive.open("fw.ini") as firmware_file:
            ini_data = firmware_file.read().decode(encoding="utf-8")

        settings = IniSettings(ini=ini_data, main_section="Firmware")
        self.INI = settings
        version = settings.GetValue("fw_ver")
        build_timestamp = settings.GetValue("fw_buildts")
        if not isinstance(version, str) or not isinstance(build_timestamp, str):
            msg = f"Invalid firmware metadata in {file_name}"
            raise TypeError(msg)
        return version, int(build_timestamp)

    def UpdateFirmware_PrintText(
        self,
        text: str,
        enableUI: bool = False,
        setProgress: float | None = None,
    ) -> None:
        del enableUI
        if setProgress is not None:
            self.FWUPD_R = True
            print(f"\33[2K\r{text:s} ({int(setProgress):d}%)", flush=True, end="")
        else:
            if self.FWUPD_R is True:
                print()
            print(text, flush=True)

    def UpdateFirmwareGBxCartRW(
        self,
        pcb: int = 5,
        port: str | Literal[False] | None = False,
    ) -> bool:
        if pcb != 5:
            return False
        title: str = __("Firmware Updater for {device_name}", device_name="GBxCart RW v1.4")
        print("\n" + title)
        print("=" * len(title) + "\n")
        print(__("Select your PCB version:") + "\n1) GBxCart RW v1.4\n2) GBxCart RW v1.4a/b/c\n")
        answer: str = input(__("Enter number ({range}):", range="1-2") + " ").lower().strip()
        print()
        if answer == "1":
            led = "Done"
            file_name: Path = Path(AppContext.APP_PATH) / "res" / "fw_GBxCart_RW_v1_4.zip"
        elif answer == "2":
            led = "Status"
            file_name = Path(AppContext.APP_PATH) / "res" / "fw_GBxCart_RW_v1_4a.zip"
        else:
            print(__("Canceled."))
            return False

        fw_ver, fw_buildts = self._LoadFirmwareInfo(file_name)

        print(
            __("Available firmware version:")
            + "\n{:s}\n".format(
                f"{fw_ver:s} ({datetime.datetime.fromtimestamp(int(fw_buildts)).astimezone().replace(microsecond=0).isoformat():s})",
            ),
        )
        text: str = __("Please follow these steps to proceed with the firmware update:")
        text += "\n\n" + __(
            "- Disconnect the USB cable of your GBxCart RW.\n"
            "- On the circuit board of your GBxCart RW, press and hold down the small button while connecting the USB cable again.\n"
            "- Keep the small button held for at least 2 seconds, then let go of it.\n"
            "- If done right, the green LED labeled “{led}” should remain lit.",
            led=led,
        )
        text += "\n" + __("- Press ENTER to continue.")
        print(text)
        if len(input("").strip()) != 0:
            print(__("Canceled."))
            return False

        try:
            ports = []
            if port is None or port is False:
                comports = list_ports.comports()
                ports.extend(
                    [comport.device for comport in comports if comport.vid == 0x1A86 and comport.pid == 0x7523]
                )
                if len(ports) == 0:
                    print(__("No devices found."))
                    return False
                port = ports[0]
            if not isinstance(port, str):
                print(__("No devices found."))
                return False

            from . import hw_GBxCartRW  # noqa: PLC0415 - load only the selected firmware backend

            while True:
                try:
                    print(__("Using port {port}", port=port) + "\n")
                    FirmwareUpdater = hw_GBxCartRW.FirmwareUpdater
                    FWUPD = FirmwareUpdater(port=port)
                    ret: Literal[1, 2, 3] = FWUPD.WriteFirmware(file_name, self.UpdateFirmware_PrintText)
                    break
                except SerialException:
                    port = input(__("Couldn't access port {port}.\nEnter new port:", port=port) + " ").strip()
                    if len(port) == 0:
                        print(__("Canceled."))
                        return False
                    continue
                except Exception as err:
                    traceback.print_exception(type(err), err, err.__traceback__)
                    print(err)
                    return False

            update_succeeded: bool = ret == 1
            if update_succeeded:
                print(__("The firmware update is complete!"))
            elif ret == 3:
                print(__("Please re-install the application."))
            return update_succeeded  # noqa: TRY300

        except Exception as err:
            traceback.print_exception(type(err), err, err.__traceback__)
            print(str(err))
            return False

    def UpdateFirmwareGBFlash(
        self,
        port: str | Literal[False] | None = False,
    ) -> bool:
        title: str = __("Firmware Updater for {device_name}", device_name="GBFlash")
        print("\n" + title)
        print("=" * len(title))
        print(__("Supported revisions:") + " v1.0, v1.1, v1.2, v1.3\n")
        file_name: Path = Path(AppContext.APP_PATH) / "res" / "fw_GBFlash.zip"

        fw_ver, fw_buildts = self._LoadFirmwareInfo(file_name)

        print(
            __("Available firmware version:")
            + "\n{:s}\n".format(
                f"{fw_ver:s} ({datetime.datetime.fromtimestamp(int(fw_buildts)).astimezone().replace(microsecond=0).isoformat():s})",
            ),
        )
        text: str = __("Note: Cloned GBFlash hardware often don't come with a firmware update feature.") + "\n\n"
        text += (
            __("Please follow these steps to proceed with the firmware update:")
            + "\n\n"
            + __(
                "- Unplug your GBFlash device.\n"
                "- On your GBFlash circuit board, push and hold the small button (U22) while plugging the USB cable back in.\n"
                "- If done right, the blue LED labeled “ACT” should now keep blinking twice.",
            )
        )
        text += "\n" + __("- Press ENTER to continue.")
        print(text)

        if len(input("").strip()) != 0:
            print(__("Canceled."))
            return False

        try:
            ports = []
            if port is None or port is False:
                comports = list_ports.comports()
                ports.extend([c.device for c in comports if c.vid == 0x1A86 and c.pid == 0x7523])
                if len(ports) == 0:
                    print(__("No device found."))
                    return False
                port = ports[0]
            if not isinstance(port, str):
                print(__("No device found."))
                return False

            from . import hw_GBFlash  # noqa: PLC0415 - load only the selected firmware backend

            while True:
                try:
                    print(__("Using port {port}", port=port) + "\n")
                    FirmwareUpdater = hw_GBFlash.FirmwareUpdater
                    FWUPD = FirmwareUpdater(port=port)
                    ret = FWUPD.WriteFirmware(file_name, self.UpdateFirmware_PrintText)
                    break
                except SerialException:
                    port = input(__("Couldn't access port {port}.\nEnter new port:", port=port) + " ").strip()
                    if len(port) == 0:
                        print(__("Canceled."))
                        return False
                    continue
                except Exception as err:
                    traceback.print_exception(type(err), err, err.__traceback__)
                    print(err)
                    return False

            if ret == 1:
                print(__("The firmware update is complete!"))
                return True
            if ret == 3:
                print(__("Please re-install the application."))
                return False
            return False  # noqa: TRY300

        except Exception as err:
            traceback.print_exception(type(err), err, err.__traceback__)
            print(str(err))
            return False

    def UpdateFirmwareJoeyJr(
        self,
        port: str | Literal[False] | None = False,
    ) -> bool:
        title: str = __("Firmware Updater for {device_name}", device_name="Joey Jr")
        print("\n" + title)
        print("=" * len(title))
        file_name = Path(AppContext.APP_PATH) / "res" / "fw_JoeyJr.zip"

        with zipfile.ZipFile(file_name) as zf:
            with zf.open("fw.ini") as f:
                ini_file = f.read()
            ini_file = ini_file.decode(encoding="utf-8")
            self.INI = IniSettings(ini=ini_file, main_section="Firmware")

        print()
        print(
            __("Select the firmware to install:") + "\n"
            "  1) " + __("Lesserkuma's FlashGBX firmware") + "\n"
            "  2) " + __("BennVenn's Drag'n'Drop firmware") + "\n"
            "  3) " + __("BennVenn's JoeyGUI firmware") + "\n",
        )
        answer = input(__("Enter number ({range}):", range="1-3") + " ").lower().strip()
        print()
        if answer == "1":
            fw_choice = 1
        elif answer == "2":
            fw_choice = 2
        elif answer == "3":
            fw_choice = 3
        else:
            fw_choice = 0

        if fw_choice == 0:
            print(__("Canceled."))
            return False

        try:
            ports = []
            if port is None or port is False:
                comports = list_ports.comports()
                ports: list[str] = [
                    comports[i].device
                    for i in range(len(comports))
                    if comports[i].vid == 0x483 and comports[i].pid == 0x5740
                ]
                if len(ports) == 0:
                    print(
                        __(
                            "No devices found. If your Joey Jr is running the Drag'n'Drop firmware, you will have to use the JoeyGUI software to update the firmware.",
                        ),
                    )
                    return False
                port = ports[0]
            if not isinstance(port, str):
                print(__("No devices found."))
                return False

            from . import hw_JoeyJr  # noqa: PLC0415 - load only the selected firmware backend

            while True:
                try:
                    print(__("Using port {port}", port=port) + "\n")
                    FirmwareUpdater = hw_JoeyJr.FirmwareUpdater
                    FWUPD: FirmwareUpdater = FirmwareUpdater(port=port)
                    file_name: Path = Path(AppContext.APP_PATH) / "res" / "fw_JoeyJr.zip"
                    with zipfile.ZipFile(file_name) as archive:
                        fw_data = None
                        if fw_choice == 1:
                            with archive.open("FIRMWARE_LK.JR") as f:
                                fw_data = bytearray(f.read())
                        elif fw_choice == 2:
                            with archive.open("FIRMWARE_MSC.JR") as f:
                                fw_data = bytearray(f.read())
                        elif fw_choice == 3:
                            with archive.open("FIRMWARE_JOEYGUI.JR") as f:
                                fw_data = bytearray(f.read())

                    ret = FWUPD.WriteFirmware(fw_data, self.UpdateFirmware_PrintText)
                    break
                except SerialException:
                    port = input(__("Couldn't access port {port}.\nEnter new port:", port=port) + " ").strip()
                    if len(port) == 0:
                        print(__("Canceled."))
                        return False
                    continue
                except Exception as err:
                    traceback.print_exception(type(err), err, err.__traceback__)
                    print(err)
                    return False

            print()
            if ret == 1:
                print(__("The firmware update is complete!"))
                return True
            if ret == 3:
                print(__("Please re-install the application."))
                return False
            return False  # noqa: TRY300

        except Exception as err:
            traceback.print_exception(type(err), err, err.__traceback__)
            print(str(err))
            return False
