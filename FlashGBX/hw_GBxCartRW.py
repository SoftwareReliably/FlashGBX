# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import datetime
import hashlib
import math
import os
import platform
import random
import re
import struct
import time
import zipfile
from collections.abc import Mapping
from typing import Any, ClassVar, Literal, Protocol, TypedDict

import serial
import serial.tools.list_ports
from serial import SerialException

from .app import AppInfo
from .i18n import __, c__, format_decimal
from .IniSettings import IniSettings
from .LK_Device import LK_Device
from .Logging import dprint, logger


class FirmwareInfo(TypedDict):
    """Firmware metadata reported by the GBxCart RW protocol."""

    cfw_id: str
    fw_ver: int
    pcb_ver: int
    fw_ts: int
    fw_dt: str
    ofw_ver: int
    pcb_name: str | None
    cart_power_ctrl: bool
    cart_presence_switch: bool
    cart_mode_switch: bool
    bootloader_reset: bool


class BootloaderInfo(TypedDict):
    magic: bytes
    tsb_version: int
    tsb_status: int
    signature: bytes
    page_size: int
    flash_size: int
    eeprom_size: int
    unknown: int
    avr_jmp_identifier: int
    jmp_mode: str
    device_type: str
    tsb_timeout: int


class StatusCallback(Protocol):
    def __call__(
        self,
        text: str,
        enableUI: bool = False,
        setProgress: float | None = None,
    ) -> Any: ...


FlashcartMap = Mapping[str, Mapping[str, Any]]
ConnectionMessage = list[int | str]
InitializeResult = list[ConnectionMessage] | Literal[False]
FirmwareUpdateResult = Literal[1, 2, 3]
MAX_V13_FIRMWARE_SIZE = 7_168


def _parse_intel_hex(contents: str) -> bytearray:
    """Parse and validate an Intel HEX image for the legacy AVR updater."""

    image = bytearray()
    address_base = 0
    found_data = False
    found_eof = False

    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"Intel HEX line {line_number} has no start marker")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise ValueError(f"Intel HEX line {line_number} contains invalid hexadecimal data") from exc
        if len(record) < 5 or len(record) != record[0] + 5:
            raise ValueError(f"Intel HEX line {line_number} has an invalid length")
        if sum(record) & 0xFF:
            raise ValueError(f"Intel HEX line {line_number} has an invalid checksum")

        byte_count = record[0]
        address = int.from_bytes(record[1:3], byteorder="big")
        record_type = record[3]
        data = record[4 : 4 + byte_count]

        if record_type == 0x00:
            absolute_address = address_base + address
            end_address = absolute_address + byte_count
            if end_address >= MAX_V13_FIRMWARE_SIZE:
                raise ValueError("The firmware image is too large")
            if len(image) < end_address:
                image.extend(b"\xff" * (end_address - len(image)))
            image[absolute_address:end_address] = data
            found_data = True
        elif record_type == 0x01:
            if byte_count != 0:
                raise ValueError(f"Intel HEX line {line_number} has an invalid EOF")
            found_eof = True
            break
        elif record_type == 0x02:
            if byte_count != 2:
                raise ValueError(f"Intel HEX line {line_number} has an invalid segment address")
            address_base = int.from_bytes(data, byteorder="big") << 4
        elif record_type == 0x04:
            if byte_count != 2:
                raise ValueError(f"Intel HEX line {line_number} has an invalid linear address")
            address_base = int.from_bytes(data, byteorder="big") << 16
        elif record_type not in (0x03, 0x05):
            raise ValueError(f"Intel HEX line {line_number} has unsupported record type 0x{record_type:02X}")

    if not found_data or not found_eof:
        raise ValueError("The Intel HEX firmware image is incomplete")
    return image


class GbxDevice(LK_Device):
    DEVICE_NAME = "GBxCart RW"
    DEVICE_MIN_FW = 1
    DEVICE_MAX_FW = 1
    DEVICE_LATEST_FW_TS: ClassVar[dict[int, int]] = {
        4: 1709317610,
        5: 1780508702,
        6: 1780508702,
        2: 0,
        90: 0,
        100: 0,
    }
    PCB_VERSIONS: ClassVar[dict[int, str]] = {
        5: "v1.4",
        6: "v1.4a/b/c",
        2: "v1.1/v1.2",
        4: "v1.3",
        90: "XMAS v1.0",
        100: "Mini v1.0",
    }
    DEVICE_LABEL_LONG = "GBxCart RW v1.4 or v1.4a/b/c"
    DEVICE_LABEL_SHORT = "GBxCart RW v1.4"
    FWUPDATE_ACTION = "fwupdate-gbxcartrw"
    CLI_UPDATER_METHOD = "UpdateFirmwareGBxCartRW"
    DEVICE_SUPPORT_MESSAGE = (
        "For help with your GBxCart RW, please visit the insideGadgets Discord:\nhttps://gbxcart.com/discord"
    )

    BAUDRATE = 1000000
    MAX_BUFFER_READ = 0x1000
    MAX_BUFFER_WRITE = 0x400
    DEVICE_CMD: ClassVar[dict[str, int]] = LK_Device.DEVICE_CMD.copy()
    DEVICE_CMD.update(
        {
            "OFW_RESET_AVR": 0x2A,
            "OFW_CART_MODE": 0x43,
            "OFW_FW_VER": 0x56,
            "OFW_PCB_VER": 0x68,
            "OFW_USART_1_0M_SPEED": 0x3C,
            "OFW_USART_1_5M_SPEED": 0x3E,
            "OFW_CART_PWR_ON": 0x2F,
            "OFW_CART_PWR_OFF": 0x2E,
            "OFW_QUERY_CART_PWR": 0x5D,
            "OFW_DONE_LED_ON": 0x3D,
            "OFW_ERROR_LED_ON": 0x3F,
            "OFW_GB_CART_MODE": 0x47,
            "OFW_GB_FLASH_BANK_1_COMMAND_WRITES": 0x4E,
            "OFW_LNL_QUERY": 0x25,
        }
    )

    DEVICE: serial.Serial | None
    FW: FirmwareInfo | None

    def __init__(self) -> None:
        super().__init__()
        # LK_Device exposes connection state as class attributes. Make the
        # mutable state instance-local so independent probes cannot share it.
        self.DEVICE = None
        self.FW = None
        self.PORT = ""
        self.DEVICE_NAME = type(self).DEVICE_NAME
        self.BAUDRATE = type(self).BAUDRATE
        self.MAX_BUFFER_READ = type(self).MAX_BUFFER_READ
        self.MAX_BUFFER_WRITE = type(self).MAX_BUFFER_WRITE

    def _firmware(self) -> FirmwareInfo:
        if self.FW is None:
            raise RuntimeError("Firmware information is not available")
        return self.FW

    def _serial_device(self) -> serial.Serial:
        if self.DEVICE is None:
            raise ConnectionError("The GBxCart RW is not connected")
        return self.DEVICE

    def _read_byte(self) -> int:
        value = self._read(1)
        if value is False or not isinstance(value, int):
            raise ConnectionError("Expected one byte from the GBxCart RW")
        return value

    def _read_bytes(self, count: int) -> bytearray:
        value = self._read(count)
        if value is False:
            raise ConnectionError(f"Expected {count} bytes from the GBxCart RW firmware")
        if count == 1 and isinstance(value, int):
            return bytearray([value])
        if isinstance(value, bytearray) and len(value) == count:
            return value
        raise ConnectionError(f"Expected {count} bytes from the GBxCart RW firmware")

    def _close_serial_device(self) -> None:
        device = self.DEVICE
        self.DEVICE = None
        try:
            if device is not None and device.is_open:
                device.close()
        except OSError, SerialException:
            logger.exception("Failed to close the GBxCart RW serial connection")

    def Initialize(
        self,
        flashcarts: FlashcartMap | None = None,
        port: str | None = None,
        max_baud: int = 1_500_000,
    ) -> InitializeResult:
        if self.DEVICE is not None:
            self._close_serial_device()
        self.FW = None
        self.PORT = ""
        if platform.system() == "Darwin":
            max_baud = 1_000_000

        conn_msg: list[ConnectionMessage] = []
        if port is not None:
            ports = [port]
        else:
            ports = [
                candidate.device
                for candidate in serial.tools.list_ports.comports()
                if candidate.vid == 0x1A86 and candidate.pid == 0x7523
            ]
            if not ports:
                return False

        for current_port in ports:
            self.FW = None
            for baudrate in (1_000_000, 1_500_000):
                if max_baud < baudrate:
                    continue
                try:
                    if self.TryConnect(current_port, baudrate):
                        self.BAUDRATE = baudrate
                        self.DEVICE = serial.Serial(current_port, self.BAUDRATE, timeout=0.1)
                        break
                except (OSError, SerialException) as exc:
                    if isinstance(exc, PermissionError) or "Permission" in str(exc):
                        conn_msg.append(
                            [
                                3,
                                __(
                                    "The device on port {port} couldn’t be accessed. Make sure your user account has permission to use it and it’s not already in use by another application.",
                                    port=current_port,
                                ),
                            ]
                        )
                    elif isinstance(exc, FileNotFoundError) or "FileNotFoundError" in str(exc):
                        continue
                    else:
                        conn_msg.append(
                            [
                                3,
                                __(
                                    "A critical error occurred while trying to access the device on port {port}.",
                                    port=current_port,
                                )
                                + "\n\n"
                                + str(exc),
                            ]
                        )

            if not self.FW or self.DEVICE is None:
                self.FW = None
                continue
            if (
                max_baud >= 1_500_000
                and "pcb_ver" in self.FW
                and self.FW["pcb_ver"] in (5, 6, 101)
                and self.BAUDRATE < 1_500_000
            ):
                self.ChangeBaudRate(baudrate=1_500_000)
                self.DEVICE = serial.Serial(current_port, self.BAUDRATE, timeout=0.1)

            dprint(f"Found a {self.DEVICE_NAME}")
            dprint("Firmware information:", self.FW)
            dprint("Baud rate:", self.BAUDRATE)

            if (
                self.DEVICE is None
                or not self.IsConnected()
                or self.FW is None
                or self.FW["pcb_ver"] not in self.DEVICE_LATEST_FW_TS
            ):
                firmware = self.FW
                self._close_serial_device()
                if firmware is not None:
                    conn_msg.append(
                        [
                            0,
                            __(
                                "Couldn’t communicate with the {device_name} on port {port}. Please disconnect and reconnect the device, then try again.",
                                device_name=self.DEVICE_NAME,
                                port=current_port,
                            ),
                        ]
                    )
                self.FW = None
                continue
            elif self.FW["fw_ts"] > self.DEVICE_LATEST_FW_TS[self.FW["pcb_ver"]]:
                conn_msg.append(
                    [
                        1,
                        __(
                            "Note: The {device_name} on port {port} is running a firmware version that is newer than what this version of FlashGBX was developed to work with, so errors may occur.",
                            device_name=self.DEVICE_NAME,
                            port=current_port,
                        ),
                    ]
                )
            elif self.FW["pcb_ver"] in (5, 6, 101) and self.BAUDRATE > 1000000:
                self.MAX_BUFFER_READ = 0x1000
                self.MAX_BUFFER_WRITE = 0x400
            else:
                self.MAX_BUFFER_READ = 0x1000
                self.MAX_BUFFER_WRITE = 0x100

            self.PORT = current_port
            self._serial_device().timeout = self.DEVICE_TIMEOUT

            # Load Flash Cartridge Handlers
            if flashcarts is not None:
                self.UpdateFlashCarts(flashcarts)

            # Stop after first found device
            break

        return conn_msg

    def LoadFirmwareVersion(self) -> bool:
        dprint("Querying firmware version")
        device = self.DEVICE
        if device is None:
            return False

        old_timeout = device.timeout
        try:
            self.FW = None
            device.timeout = 0.075
            device.reset_input_buffer()
            device.reset_output_buffer()
            self._write(self.DEVICE_CMD["OFW_PCB_VER"])
            response = device.read(1)
            if not response:
                dprint("No response")
                return False

            pcb = response[0]
            self._write(self.DEVICE_CMD["OFW_FW_VER"])
            ofw = self._read_byte()
            if pcb == 2 and ofw == 2:
                dprint(f"Not a {self.DEVICE_NAME}")
                return False
            if pcb >= 5 and ofw == 0:
                dprint(f"Not a {self.DEVICE_NAME}")
                return False
            if pcb < 5 and ofw > 0:
                self.FW = {
                    "ofw_ver": ofw,
                    "pcb_ver": pcb,
                    "pcb_name": "GBxCart RW",
                    "cfw_id": "",
                    "fw_ver": 0,
                    "fw_ts": 0,
                    "fw_dt": "",
                    "cart_power_ctrl": pcb in (5, 6),
                    "cart_presence_switch": False,
                    "cart_mode_switch": False,
                    "bootloader_reset": False,
                }
                return True

            self._write(self.DEVICE_CMD["QUERY_FW_INFO"])
            size = self._read_byte()
            if size != 8:
                return False
            cfw_id, fw_ver, pcb_ver, fw_ts = struct.unpack(">cHBI", self._read_bytes(size))
            firmware: FirmwareInfo = {
                "cfw_id": cfw_id.decode("ascii"),
                "fw_ver": fw_ver,
                "pcb_ver": pcb_ver,
                "fw_ts": fw_ts,
                "fw_dt": (datetime.datetime.fromtimestamp(fw_ts).astimezone().replace(microsecond=0).isoformat()),
                "ofw_ver": ofw,
                "pcb_name": "",
                "cart_power_ctrl": False,
                "cart_presence_switch": False,
                "cart_mode_switch": False,
                "bootloader_reset": False,
            }
            self.FW = firmware
            if firmware["cfw_id"] == "L" and firmware["fw_ver"] >= 12:
                name_size = self._read_byte()
                if name_size > 0:
                    name = self._read_bytes(name_size)
                    try:
                        firmware["pcb_name"] = name.decode("UTF-8").replace("\x00", "").strip()
                    except UnicodeDecodeError:
                        firmware["pcb_name"] = "Unnamed Device"
                    if firmware["pcb_name"]:
                        self.DEVICE_NAME = firmware["pcb_name"]

                # Cartridge Power Control support
                capabilities = self._read_byte()
                firmware["cart_power_ctrl"] = capabilities & 1 == 1
                firmware["cart_presence_switch"] = (capabilities >> 1) & 1 == 1
                firmware["cart_mode_switch"] = (capabilities >> 2) & 1 == 1

                # Reset to bootloader support
                firmware["bootloader_reset"] = self._read_byte() == 1

            return True
        except (OSError, SerialException, ConnectionError, UnicodeDecodeError) as exc:
            dprint("Disconnecting due to an error", exc, sep="\n")
            try:
                if device.is_open:
                    device.reset_input_buffer()
                    device.reset_output_buffer()
                    device.close()
            except OSError, SerialException:
                logger.exception("Failed to close GBxCart RW after an initialization error")
            finally:
                self.DEVICE = None
            return False
        finally:
            if device.is_open:
                device.timeout = old_timeout

    def ChangeBaudRate(self, baudrate: int) -> None:
        if not self.IsConnected():
            return
        dprint("Changing baud rate to", baudrate)
        if baudrate == 1_500_000:
            self._write(self.DEVICE_CMD["OFW_USART_1_5M_SPEED"])
        elif baudrate == 1_000_000:
            self._write(self.DEVICE_CMD["OFW_USART_1_0M_SPEED"])
        else:
            raise ValueError(f"Unsupported GBxCart RW baud rate: {baudrate}")
        self.BAUDRATE = baudrate
        self._serial_device().close()

    def CheckActive(self) -> bool:
        if time.time() < self.LAST_CHECK_ACTIVE + 1:
            return True
        dprint("Checking if device is active (GBxCart RW specific)")
        if self.DEVICE is None or self.FW is None:
            return False
        firmware = self.FW
        if firmware.get("pcb_name") is None:
            if self.LoadFirmwareVersion():
                self.LAST_CHECK_ACTIVE = time.time()
                return True
            return False
        try:
            if firmware["fw_ver"] == 0:  # legacy GBxCart RW firmware
                self.LAST_CHECK_ACTIVE = time.time()
                return True
            if firmware["fw_ver"] < 12:
                self._write(bytearray([self.DEVICE_CMD["OFW_FW_VER"]]))
                self._read_byte()
                self.LAST_CHECK_ACTIVE = time.time()
                return True
            return super().CheckActive()
        except (OSError, SerialException, ConnectionError) as exc:
            dprint("Disconnecting...", exc)
            try:
                device = self.DEVICE
                if device is not None and device.is_open:
                    device.reset_input_buffer()
                    device.reset_output_buffer()
                    device.close()
            except OSError, SerialException:
                logger.exception("Failed to close GBxCart RW after a connection error")
            finally:
                self.DEVICE = None
            return False

    def GetFirmwareVersion(self, more: bool = False) -> str:
        firmware = self._firmware()
        if firmware["fw_ver"] == 0:  # old GBxCart RW
            return f"R{firmware['ofw_ver']:d}"

        if firmware["pcb_ver"] in (5, 6, 101):
            version = f"R{firmware['ofw_ver']:d}+{firmware['cfw_id']:s}{firmware['fw_ver']:d}"
        else:
            version = f"{firmware['cfw_id']:s}{firmware['fw_ver']:d}"
        if more:
            version += f" ({firmware['fw_dt']:s})"
        return version

    def GetFullNameExtended(self, more: bool = False) -> str:
        firmware = self._firmware()
        if firmware["fw_ver"] == 0:  # old GBxCart RW
            return __(
                "{device_name} – Firmware {fw_version} ({port})",
                device_name=self.GetFullName(),
                fw_version=self.GetFirmwareVersion(),
                port=self.GetPort(),
            )

        if more:
            return __(
                "{device_name} – Firmware {fw_version} ({timestamp}) on {port} at {baudrate}M baud",
                device_name=self.GetFullName(),
                fw_version=self.GetFirmwareVersion(),
                timestamp=firmware["fw_dt"],
                port=self.GetPort(),
                baudrate=format_decimal(self.BAUDRATE / 1_000_000, precision=1),
            )
        return __(
            "{device_name} – Firmware {fw_version} ({port})",
            device_name=self.GetFullName(),
            fw_version=self.GetFirmwareVersion(),
            port=self.GetPort(),
        )

    def CanSetVoltageBySwitch(self) -> bool:
        return False

    def CanSetVoltageByCode(self) -> bool:
        return True

    def CanSetVoltageByAutoswitch(self) -> bool:
        return False

    def CanPowerCycleCart(self) -> bool:
        if self.FW is None or self.DEVICE is None or not self.DEVICE.is_open:
            return False
        if self.FW["fw_ver"] >= 12:
            return self.FW.get("cart_power_ctrl", False)
        return self.FW["pcb_ver"] in (5, 6)

    def GetSupprtedModes(self) -> list[str]:
        if self._firmware()["pcb_ver"] == 101:
            return ["DMG"]
        return ["DMG", "AGB"]

    def IsSupported3dMemory(self) -> bool:
        return True

    def IsClkConnected(self) -> bool:
        return self._firmware()["pcb_ver"] in (5, 6, 101)

    def SupportsFirmwareUpdates(self) -> bool:
        firmware = self.FW
        if firmware is None:
            return True

        if firmware["ofw_ver"] == 30 and self.DEVICE is not None:
            self._write(self.DEVICE_CMD["OFW_LNL_QUERY"])
            old_timeout = self.DEVICE.timeout
            try:
                self.DEVICE.timeout = 0.15
                is_lnl = self._read(1) == 0x31
            finally:
                self.DEVICE.timeout = old_timeout
            dprint("LinkNLoad detected:", is_lnl)
            if is_lnl:
                return False
        return firmware["pcb_ver"] in (2, 4, 5, 6, 90, 100, 101)

    def FirmwareUpdateAvailable(self) -> bool:
        firmware = self._firmware()
        if firmware["fw_ver"] == 0 and firmware["pcb_ver"] in (
            2,
            4,
            90,
            100,
            101,
        ):
            self.FW_UPDATE_REQ = True if firmware["pcb_ver"] == 4 else 2
            return True
        if firmware["pcb_ver"] not in (4, 5, 6):
            return False
        if firmware["fw_ts"] != self.DEVICE_LATEST_FW_TS[firmware["pcb_ver"]]:
            if firmware["pcb_ver"] == 4:
                self.FW_UPDATE_REQ = True
            return True
        self.FW_UPDATE_REQ = False
        return False

    def GetFirmwareUpdaterClass(
        self: GbxDevice | None,
    ) -> tuple[type[Any] | None, type[Any]] | None:
        try:
            if self is None or self._firmware()["pcb_ver"] in (5, 6):
                return (FirmwareUpdater, FirmwareUpdaterWindow)
            if self._firmware()["pcb_ver"] in (2, 4, 90, 100, 101):
                return (None, FirmwareUpdaterWindowV13)
        except NameError:
            # Updater windows are optional when PySide6 is unavailable.
            return None
        return None

    def ResetLEDs(self) -> None:
        if self.DEVICE is None or not self.DEVICE.is_open:
            return
        self._write(self.DEVICE_CMD["OFW_CART_MODE"])  # Reset LEDs
        self._read_byte()

    def SupportsBootloaderReset(self) -> bool:
        firmware = self.FW
        return bool(firmware and firmware.get("fw_ver", 0) >= 12 and firmware.get("bootloader_reset", False))

    def BootloaderReset(self) -> bool:
        return False

    def SupportsAudioAsWe(self) -> bool:
        return True

    def Close(self, cartPowerOff: bool = False) -> Any:
        try:
            self.ResetLEDs()
        except OSError, SerialException, ConnectionError:
            pass
        return super().Close(cartPowerOff)

    def SetTimeout(self, seconds: float = 1) -> None:
        seconds = max(seconds, 1)
        self.DEVICE_TIMEOUT = seconds
        self._serial_device().timeout = self.DEVICE_TIMEOUT


class FirmwareUpdater:
    PORT: str | None = None

    def __init__(self, app_path: str | os.PathLike[str] = ".", port: str | None = None) -> None:
        self.APP_PATH = os.fspath(app_path)
        self.PORT = port

    def WriteFirmware(
        self,
        zipfn: str | os.PathLike[str],
        fncSetStatus: StatusCallback,
    ) -> FirmwareUpdateResult:
        try:
            with zipfile.ZipFile(zipfn) as archive:
                with archive.open("fw.ini") as f:
                    buffer1 = bytearray(f.read())
                with archive.open("fw.bin") as f:
                    buffer2 = bytearray(f.read())
        except OSError, zipfile.BadZipFile, KeyError:
            fncSetStatus(__("The firmware update file is corrupted."))
            return 3
        if not buffer1 or len(buffer2) < 0x20:
            fncSetStatus(__("The firmware update file is corrupted."))
            return 3
        while len(buffer1) < len(buffer2):
            buffer1 = buffer1 + buffer1
        rng = random.Random(struct.unpack("<I", buffer2[-0x18:-0x14])[0])
        chk = buffer2[-0x14:]
        encrypted = buffer2[:-0x18]
        buffer = bytearray()
        for i in range(len(encrypted)):
            random_byte = int(rng.random() * 256) % 256
            buffer.append(encrypted[len(encrypted) - i - 1] ^ random_byte ^ buffer1[len(buffer1) - i - 1])
        if chk != hashlib.sha1(buffer).digest():
            fncSetStatus(__("The firmware update file is corrupted."))
            return 3

        if self.PORT is None:
            ports = [
                candidate.device
                for candidate in serial.tools.list_ports.comports()
                if candidate.vid == 0x1A86 and candidate.pid == 0x7523
            ]
            if not ports:
                fncSetStatus(__("No device found."))
                return 2
            port = ports[0]
        else:
            port = self.PORT
        data = buffer
        buffer = bytearray()

        fncSetStatus(text=__("Connecting..."))
        try:
            dev = serial.Serial(port=port, baudrate=57600, timeout=1)
        except OSError, SerialException:
            fncSetStatus(text=__("Device not accessible."), enableUI=True)
            return 2
        try:
            dev.reset_input_buffer()

            # Write firmware
            fncSetStatus(__("Updating firmware..."), setProgress=0)

            size = len(data)
            last_progress_step = -1
            for counter, byte in enumerate(data, start=1):
                expected = bytes([byte])
                dev.write(expected)
                if dev.read(1) != expected:
                    if counter == 1:
                        text = __("Update failed!")
                    else:
                        text = __(
                            "Update failed at offset {offset}!",
                            offset=f"0x{counter - 1:04X}",
                        )
                    fncSetStatus(text=text, enableUI=True)
                    return 2

                progress_step = counter * 1_000 // size
                if progress_step != last_progress_step:
                    fncSetStatus(
                        text=__("Updating firmware... Do not unplug the device!"),
                        setProgress=progress_step / 10,
                    )
                    last_progress_step = progress_step
        except (OSError, SerialException) as exc:
            dprint("GBxCart RW firmware update failed:", exc)
            fncSetStatus(text=__("Update failed!"), enableUI=True)
            return 2
        finally:
            dev.close()

        time.sleep(0.8)
        fncSetStatus(__("Done!"))
        time.sleep(0.2)
        return 1


try:
    from PySide6 import QtCore, QtGui, QtWidgets

    def _message_box(
        *,
        parent: QtWidgets.QWidget,
        icon: QtWidgets.QMessageBox.Icon,
        windowTitle: str,
        text: str,
        standardButtons: QtWidgets.QMessageBox.StandardButton,
        defaultButton: QtWidgets.QMessageBox.StandardButton | None = None,
    ) -> QtWidgets.QMessageBox:
        """Build a QMessageBox using the typed positional constructor."""

        message_box_type = QtWidgets.QMessageBox
        message_box = message_box_type(icon, windowTitle, text, standardButtons, parent)
        if defaultButton is not None:
            message_box.setDefaultButton(defaultButton)
        return message_box

    class FirmwareUpdaterWindow(QtWidgets.QDialog):
        APP: Any
        DEVICE: GbxDevice | None
        FWUPD: FirmwareUpdater
        DEV_NAME: str
        FW_VER: str
        PCB_VER: str

        def __init__(
            self,
            app: Any,
            app_path: str | os.PathLike[str],
            file: str | None = None,
            icon: str | os.PathLike[str] | QtGui.QIcon | None = None,
            device: GbxDevice | None = None,
        ) -> None:
            QtWidgets.QDialog.__init__(self, app)
            if icon is not None:
                self.setWindowIcon(icon if isinstance(icon, QtGui.QIcon) else QtGui.QIcon(os.fspath(icon)))
            self.setStyleSheet("QMessageBox { messagebox-text-interaction-flags: 5; }")
            self.setWindowTitle(
                AppInfo.NAME + " – " + __("Firmware Updater for {device_name}", device_name="GBxCart RW")
            )
            self.setWindowFlags(
                (self.windowFlags() | QtCore.Qt.WindowType.MSWindowsFixedSizeDialogHint)
                & ~QtCore.Qt.WindowType.WindowContextHelpButtonHint
            )

            self.APP = app
            self.DEVICE = device
            self.DEV_NAME = "GBxCart RW"
            self.FW_VER = ""
            self.PCB_VER = ""
            if device is not None:
                self.FWUPD = FirmwareUpdater(app_path, device.GetPort())
                self.DEV_NAME = device.GetName()
                self.FW_VER = device.GetFirmwareVersion(more=True)
                self.PCB_VER = device.GetPCBVersion()
            else:
                self.APP.QT_APP.processEvents()
                self.FWUPD = FirmwareUpdater(app_path, None)

            self.main_layout = QtWidgets.QGridLayout()
            self.main_layout.setContentsMargins(-1, 8, -1, 8)
            self.main_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetFixedSize)
            self.layout_device = QtWidgets.QVBoxLayout()

            # ↓↓↓ Current Device Information
            self.grpDeviceInfo = QtWidgets.QGroupBox(__("Current Firmware"))
            self.grpDeviceInfo.setMinimumWidth(420)
            self.grpDeviceInfoLayout = QtWidgets.QVBoxLayout()
            self.grpDeviceInfoLayout.setContentsMargins(-1, 3, -1, -1)
            rowDeviceInfo1 = QtWidgets.QHBoxLayout()
            self.lblDeviceName = QtWidgets.QLabel(__("Device:"))
            self.lblDeviceName.setMinimumWidth(120)
            self.lblDeviceNameResult = QtWidgets.QLabel("GBxCart RW")
            rowDeviceInfo1.addWidget(self.lblDeviceName)
            rowDeviceInfo1.addWidget(self.lblDeviceNameResult)
            rowDeviceInfo1.addStretch(1)
            self.grpDeviceInfoLayout.addLayout(rowDeviceInfo1)
            rowDeviceInfo3 = QtWidgets.QHBoxLayout()
            self.lblDeviceFWVer = QtWidgets.QLabel(__("Firmware version:"))
            self.lblDeviceFWVer.setMinimumWidth(120)
            self.lblDeviceFWVerResult = QtWidgets.QLabel("")
            rowDeviceInfo3.addWidget(self.lblDeviceFWVer)
            rowDeviceInfo3.addWidget(self.lblDeviceFWVerResult)
            rowDeviceInfo3.addStretch(1)
            self.grpDeviceInfoLayout.addLayout(rowDeviceInfo3)
            rowDeviceInfo2 = QtWidgets.QHBoxLayout()
            self.lblDevicePCBVer = QtWidgets.QLabel(__("PCB version:"))
            self.lblDevicePCBVer.setMinimumWidth(120)
            self.optDevicePCBVer14 = QtWidgets.QRadioButton("v1.4")
            self.optDevicePCBVer14.clicked.connect(self.SetPCBVersion)
            self.optDevicePCBVer14a = QtWidgets.QRadioButton("v1.4a/b/c")
            self.optDevicePCBVer14a.clicked.connect(self.SetPCBVersion)
            rowDeviceInfo2.addWidget(self.lblDevicePCBVer)
            rowDeviceInfo2.addWidget(self.optDevicePCBVer14)
            rowDeviceInfo2.addWidget(self.optDevicePCBVer14a)
            rowDeviceInfo2.addStretch(1)
            self.grpDeviceInfoLayout.addLayout(rowDeviceInfo2)
            self.grpDeviceInfo.setLayout(self.grpDeviceInfoLayout)
            self.layout_device.addWidget(self.grpDeviceInfo)
            # ↑↑↑ Current Device Information

            # ↓↓↓ Available Firmware Updates
            self.grpAvailableFwUpdates = QtWidgets.QGroupBox(__("Available Firmware"))
            self.grpAvailableFwUpdates.setMinimumWidth(400)
            self.grpAvailableFwUpdatesLayout = QtWidgets.QVBoxLayout()
            self.grpAvailableFwUpdatesLayout.setContentsMargins(-1, 3, -1, -1)

            rowDeviceInfo4 = QtWidgets.QHBoxLayout()
            self.lblDeviceFWVer2 = QtWidgets.QLabel(__("Firmware version:"))
            self.lblDeviceFWVer2.setMinimumWidth(120)
            self.lblDeviceFWVer2Result = QtWidgets.QLabel("(" + __("Please choose the PCB version") + ")")
            rowDeviceInfo4.addWidget(self.lblDeviceFWVer2)
            rowDeviceInfo4.addWidget(self.lblDeviceFWVer2Result)
            rowDeviceInfo4.addStretch(1)
            self.grpAvailableFwUpdatesLayout.addLayout(rowDeviceInfo4)

            self.rowUpdate = QtWidgets.QHBoxLayout()
            self.btnUpdate = QtWidgets.QPushButton(__("Install Firmware Update"))
            self.btnUpdate.setMinimumWidth(200)
            self.btnUpdate.setContentsMargins(20, 20, 20, 20)
            self.btnUpdate.clicked.connect(self.UpdateFirmware)
            self.rowUpdate.addStretch()
            self.rowUpdate.addWidget(self.btnUpdate)
            self.rowUpdate.addStretch()

            self.grpAvailableFwUpdatesLayout.addSpacing(3)
            self.grpAvailableFwUpdatesLayout.addItem(self.rowUpdate)
            self.grpAvailableFwUpdates.setLayout(self.grpAvailableFwUpdatesLayout)
            self.layout_device.addWidget(self.grpAvailableFwUpdates)
            # ↑↑↑ Available Firmware Updates

            self.grpStatus = QtWidgets.QGroupBox("")
            self.grpStatusLayout = QtWidgets.QGridLayout()
            self.prgStatus = QtWidgets.QProgressBar()
            self.prgStatus.setMinimum(0)
            self.prgStatus.setMaximum(1000)
            self.prgStatus.setValue(0)
            self.lblStatus = QtWidgets.QLabel(__("Status: Ready."))

            self.grpStatusLayout.addWidget(self.prgStatus, 1, 0)
            self.grpStatusLayout.addWidget(self.lblStatus, 2, 0)

            self.grpStatus.setLayout(self.grpStatusLayout)
            self.layout_device.addWidget(self.grpStatus)

            self.grpFooterLayout = QtWidgets.QHBoxLayout()
            self.btnClose = QtWidgets.QPushButton(c__("Button (& = Keyboard Shortcut)", "&Close"))
            self.btnClose.clicked.connect(self.reject)
            self.grpFooterLayout.addStretch()
            self.grpFooterLayout.addWidget(self.btnClose)
            self.layout_device.addItem(self.grpFooterLayout)

            self.main_layout.addLayout(self.layout_device, 0, 0)
            self.setLayout(self.main_layout)

            self.lblDeviceNameResult.setText(self.DEV_NAME)
            self.lblDeviceFWVerResult.setText(self.FW_VER)
            if self.PCB_VER == "v1.4":
                self.optDevicePCBVer14.setChecked(True)
                self.optDevicePCBVer14a.setEnabled(False)
            elif self.PCB_VER == "v1.4a/b/c":
                self.optDevicePCBVer14a.setChecked(True)
                self.optDevicePCBVer14.setEnabled(False)
            self.SetPCBVersion()

        def SetPCBVersion(self) -> None:
            if self.optDevicePCBVer14.isChecked():
                file_name = self.FWUPD.APP_PATH + os.sep + os.path.join("res", "fw_GBxCart_RW_v1_4.zip")
            elif self.optDevicePCBVer14a.isChecked():
                file_name = self.FWUPD.APP_PATH + os.sep + os.path.join("res", "fw_GBxCart_RW_v1_4a.zip")
            else:
                return

            with zipfile.ZipFile(file_name) as archive:
                with archive.open("fw.ini") as f:
                    ini_file = f.read()
                ini_file = ini_file.decode(encoding="utf-8")
                self.INI = IniSettings(ini=ini_file, main_section="Firmware")
                self.OFW_VER = str(self.INI.GetValue("fw_ver") or "")
                self.OFW_BUILDTS = int(self.INI.GetValue("fw_buildts") or 0)
                self.OFW_TEXT = str(self.INI.GetValue("fw_text") or "")

            self.lblDeviceFWVer2Result.setText(
                f"{self.OFW_VER:s} ({datetime.datetime.fromtimestamp(self.OFW_BUILDTS).astimezone().replace(microsecond=0).isoformat():s})"
            )

        def run(self) -> None:
            try:
                self.main_layout.update()
                self.main_layout.activate()
                screenGeometry = (self.screen() or QtGui.QGuiApplication.primaryScreen()).geometry()
                x = (screenGeometry.width() - self.width()) // 2
                y = (screenGeometry.height() - self.height()) // 2
                self.move(x, y)
                self.show()
            except Exception:
                return

        def hideEvent(self, event: QtGui.QHideEvent) -> None:
            if self.DEVICE is None:
                self.APP.ConnectDevice()
            self.APP.activateWindow()

        def reject(self) -> None:
            if self.CloseDialog():
                super().reject()

        def CloseDialog(self) -> bool:
            if self.btnClose.isEnabled() is False:
                text = (
                    __(
                        "<b>Warning:</b> If you close this window while a firmware update is still running, it might leave the device in an unbootable state."
                    )
                    + " "
                    + __("You can still recover it by running the Firmware Updater again later.")
                    + "<br><br>"
                    + __("Are you sure you want to close this window?")
                )
                msgbox = _message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Warning,
                    windowTitle=AppInfo.NAME,
                    text=text,
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                )
                msgbox.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
                answer = msgbox.exec()
                if answer == QtWidgets.QMessageBox.StandardButton.No:
                    return False
            return True

        def UpdateFirmware(self) -> bool | None:
            if self.optDevicePCBVer14.isChecked():
                device_version = "v1.4"
                file_name = self.FWUPD.APP_PATH + os.sep + os.path.join("res", "fw_GBxCart_RW_v1_4.zip")
                led = "Done"
            elif self.optDevicePCBVer14a.isChecked():
                device_version = "v1.4a/b/c"
                file_name = self.FWUPD.APP_PATH + os.sep + os.path.join("res", "fw_GBxCart_RW_v1_4a.zip")
                led = "Status"
            else:
                msgbox = _message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Critical,
                    windowTitle=AppInfo.NAME,
                    text=__("Please select the PCB version of your GBxCart RW device."),
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                )
                answer = msgbox.exec()
                return False

            self.APP.DisconnectDevice()

            text = __("Please follow these steps to proceed with the firmware update:")
            text += "\n\n" + __(
                "- Disconnect the USB cable of your GBxCart RW {device_version}.\n"
                "- On the circuit board of your GBxCart RW {device_version}, press and hold down the small button while connecting the USB cable again.\n"
                "- Keep the small button held for at least 2 seconds, then let go of it.\n"
                "- If done right, the green LED labeled “{led}” should remain lit.",
                device_version=device_version,
                led=led,
            )
            text += "\n" + __("- Click OK to continue.")
            msgbox = _message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Information,
                windowTitle=AppInfo.NAME,
                text=text,
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            )
            msgbox.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Ok)
            answer = msgbox.exec()
            if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
                return
            self.btnUpdate.setEnabled(False)
            self.btnClose.setEnabled(False)
            self.optDevicePCBVer14.setEnabled(False)
            self.optDevicePCBVer14a.setEnabled(False)

            while True:
                ret = self.FWUPD.WriteFirmware(file_name, self.SetStatus)
                if ret == 1:
                    text = __("The firmware update is complete!")
                    self.btnUpdate.setEnabled(True)
                    self.btnClose.setEnabled(True)
                    self.optDevicePCBVer14.setEnabled(True)
                    self.optDevicePCBVer14a.setEnabled(True)
                    msgbox = _message_box(
                        parent=self,
                        icon=QtWidgets.QMessageBox.Icon.Information,
                        windowTitle=AppInfo.NAME,
                        text=text,
                        standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                    )
                    answer = msgbox.exec()
                    self.DEVICE = None
                    self.reject()
                    return True
                elif ret == 2:
                    text = __("The firmware update has failed. Please try again.")
                    self.btnUpdate.setEnabled(True)
                    self.btnClose.setEnabled(True)
                    self.optDevicePCBVer14.setEnabled(True)
                    self.optDevicePCBVer14a.setEnabled(True)
                    msgbox = _message_box(
                        parent=self,
                        icon=QtWidgets.QMessageBox.Icon.Critical,
                        windowTitle=AppInfo.NAME,
                        text=text,
                        standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                    )
                    answer = msgbox.exec()
                    return False
                elif ret == 3:
                    text = __("The firmware update file is corrupted. Please re-install the application.")
                    self.btnUpdate.setEnabled(True)
                    self.btnClose.setEnabled(True)
                    self.optDevicePCBVer14.setEnabled(True)
                    self.optDevicePCBVer14a.setEnabled(True)
                    msgbox = _message_box(
                        parent=self,
                        icon=QtWidgets.QMessageBox.Icon.Critical,
                        windowTitle=AppInfo.NAME,
                        text=text,
                        standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                    )
                    answer = msgbox.exec()
                    return False

        def SetStatus(
            self,
            text: str,
            enableUI: bool = False,
            setProgress: float | None = None,
        ) -> None:
            self.lblStatus.setText(__("Status: {text}", text=text))
            if setProgress is not None:
                self.prgStatus.setValue(round(setProgress * 10))
            if enableUI:
                self.btnUpdate.setEnabled(True)
                self.btnClose.setEnabled(True)
                self.optDevicePCBVer14.setEnabled(True)
                self.optDevicePCBVer14a.setEnabled(True)
            self.APP.QT_APP.processEvents()
except ImportError:
    pass


try:
    from PySide6 import QtCore, QtGui, QtWidgets

    class FirmwareUpdaterWindowV13(QtWidgets.QDialog):
        APP: Any
        DEVICE: GbxDevice | None
        PORT: str
        FW_FILES: ClassVar[dict[str, str]] = {
            "v1.1/v1.2": "fw_GBxCart_RW_v1_1_v1_2.zip",
            "v1.3": "fw_GBxCart_RW_v1_3.zip",
            "XMAS v1.0": "fw_GBxCart_RW_XMAS_v1_0.zip",
            "Mini v1.0": "fw_GBxCart_RW_Mini_v1_0.zip",
        }

        def __init__(
            self,
            app: Any,
            app_path: str | os.PathLike[str],
            file: str | None = None,
            icon: str | os.PathLike[str] | QtGui.QIcon | None = None,
            device: GbxDevice | None = None,
        ) -> None:
            QtWidgets.QDialog.__init__(self, app)
            if icon is not None:
                self.setWindowIcon(icon if isinstance(icon, QtGui.QIcon) else QtGui.QIcon(os.fspath(icon)))
            self.setStyleSheet("QMessageBox { messagebox-text-interaction-flags: 5; }")
            if device is None:
                raise ValueError("A connected GBxCart RW is required for this updater")
            self.APP = app
            self.APP_PATH = os.fspath(app_path)
            self.DEVICE = device
            self.PCB_VER = device.GetPCBVersion()
            self.FW_VER = device.GetFirmwareVersion()
            self.PORT = device.GetPort()

            self.setWindowTitle(
                AppInfo.NAME + " – " + __("Firmware Updater for {device_name}", device_name="GBxCart RW")
            )
            self.setWindowFlags(
                (self.windowFlags() | QtCore.Qt.WindowType.MSWindowsFixedSizeDialogHint)
                & ~QtCore.Qt.WindowType.WindowContextHelpButtonHint
            )

            with zipfile.ZipFile(
                self.APP_PATH + os.sep + os.path.join("res", f"{self.FW_FILES[self.PCB_VER]:s}")
            ) as archive:
                with archive.open("fw.ini") as f:
                    ini_file = f.read()
                ini_file = ini_file.decode(encoding="utf-8")
                self.INI = IniSettings(ini=ini_file, main_section="Firmware")
                self.CFW_VER = str(self.INI.GetValue("cfw_ver") or "")
                self.CFW_TEXT = str(self.INI.GetValue("cfw_text") or "")
                self.OFW_VER = str(self.INI.GetValue("ofw_ver") or "")
                self.OFW_TEXT = str(self.INI.GetValue("ofw_text") or "")

            self.main_layout = QtWidgets.QGridLayout()
            self.main_layout.setContentsMargins(-1, 8, -1, 8)
            self.main_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetFixedSize)
            self.layout_device = QtWidgets.QVBoxLayout()

            # ↓↓↓ Current Device Information
            self.grpDeviceInfo = QtWidgets.QGroupBox(__("Current Device Information"))
            self.grpDeviceInfo.setMinimumWidth(420)
            self.grpDeviceInfoLayout = QtWidgets.QVBoxLayout()
            self.grpDeviceInfoLayout.setContentsMargins(-1, 3, -1, -1)
            rowDeviceInfo1 = QtWidgets.QHBoxLayout()
            self.lblDeviceName = QtWidgets.QLabel(__("Device Name:"))
            self.lblDeviceName.setMinimumWidth(120)
            self.lblDeviceNameResult = QtWidgets.QLabel("GBxCart RW")
            rowDeviceInfo1.addWidget(self.lblDeviceName)
            rowDeviceInfo1.addWidget(self.lblDeviceNameResult)
            rowDeviceInfo1.addStretch(1)
            self.grpDeviceInfoLayout.addLayout(rowDeviceInfo1)
            rowDeviceInfo2 = QtWidgets.QHBoxLayout()
            self.lblDevicePCBVer = QtWidgets.QLabel(__("PCB version:"))
            self.lblDevicePCBVer.setMinimumWidth(120)
            self.lblDevicePCBVerResult = QtWidgets.QLabel("1.3")
            rowDeviceInfo2.addWidget(self.lblDevicePCBVer)
            rowDeviceInfo2.addWidget(self.lblDevicePCBVerResult)
            rowDeviceInfo2.addStretch(1)
            self.grpDeviceInfoLayout.addLayout(rowDeviceInfo2)
            rowDeviceInfo3 = QtWidgets.QHBoxLayout()
            self.lblDeviceFWVer = QtWidgets.QLabel(__("Firmware version:"))
            self.lblDeviceFWVer.setMinimumWidth(120)
            self.lblDeviceFWVerResult = QtWidgets.QLabel("R26")
            rowDeviceInfo3.addWidget(self.lblDeviceFWVer)
            rowDeviceInfo3.addWidget(self.lblDeviceFWVerResult)
            rowDeviceInfo3.addStretch(1)
            self.grpDeviceInfoLayout.addLayout(rowDeviceInfo3)
            self.grpDeviceInfo.setLayout(self.grpDeviceInfoLayout)
            self.layout_device.addWidget(self.grpDeviceInfo)
            # ↑↑↑ Current Device Information

            # ↓↓↓ Available Firmware Updates
            self.grpAvailableFwUpdates = QtWidgets.QGroupBox(__("Firmware Update Options"))
            self.grpAvailableFwUpdates.setMinimumWidth(400)
            self.grpAvailableFwUpdatesLayout = QtWidgets.QVBoxLayout()
            self.grpAvailableFwUpdatesLayout.setContentsMargins(-1, 3, -1, -1)

            self.optCFW = QtWidgets.QRadioButton(f"{self.CFW_VER:s}")
            self.lblCFW_Info = QtWidgets.QLabel(f"{self.CFW_TEXT:s}")
            self.lblCFW_Info.setWordWrap(True)
            self.lblCFW_Info.mousePressEvent = self._select_cfw
            self.optOFW = QtWidgets.QRadioButton(f"{self.OFW_VER:s}")
            self.lblOFW_Info = QtWidgets.QLabel(f"{self.OFW_TEXT:s}")
            self.lblOFW_Info.setWordWrap(True)
            self.lblOFW_Info.mousePressEvent = self._select_ofw
            self.optExternal = QtWidgets.QRadioButton(__("External firmware file"))

            self.rowUpdate = QtWidgets.QHBoxLayout()
            self.btnUpdate = QtWidgets.QPushButton(__("Install Firmware Update"))
            self.btnUpdate.setMinimumWidth(200)
            self.btnUpdate.setContentsMargins(20, 20, 20, 20)
            self.btnUpdate.clicked.connect(self.UpdateFirmware)
            self.rowUpdate.addStretch()
            self.rowUpdate.addWidget(self.btnUpdate)
            self.rowUpdate.addStretch()

            if self.PCB_VER == "v1.3":
                self.grpAvailableFwUpdatesLayout.addWidget(self.optCFW)
                self.grpAvailableFwUpdatesLayout.addWidget(self.lblCFW_Info)
                self.optCFW.setChecked(True)
            else:
                self.optOFW.setChecked(True)
            self.grpAvailableFwUpdatesLayout.addWidget(self.optOFW)
            self.grpAvailableFwUpdatesLayout.addWidget(self.lblOFW_Info)
            self.grpAvailableFwUpdatesLayout.addWidget(self.optExternal)
            self.grpAvailableFwUpdatesLayout.addSpacing(3)
            self.grpAvailableFwUpdatesLayout.addItem(self.rowUpdate)
            self.grpAvailableFwUpdates.setLayout(self.grpAvailableFwUpdatesLayout)
            self.layout_device.addWidget(self.grpAvailableFwUpdates)
            # ↑↑↑ Available Firmware Updates

            self.grpStatus = QtWidgets.QGroupBox("")
            self.grpStatusLayout = QtWidgets.QGridLayout()
            self.prgStatus = QtWidgets.QProgressBar()
            self.prgStatus.setMinimum(0)
            self.prgStatus.setMaximum(100)
            self.prgStatus.setValue(0)
            self.lblStatus = QtWidgets.QLabel(__("Ready."))

            self.grpStatusLayout.addWidget(self.prgStatus, 1, 0)
            self.grpStatusLayout.addWidget(self.lblStatus, 2, 0)

            self.grpStatus.setLayout(self.grpStatusLayout)
            self.layout_device.addWidget(self.grpStatus)

            self.grpFooterLayout = QtWidgets.QHBoxLayout()
            self.btnClose = QtWidgets.QPushButton(c__("Button (& = Keyboard Shortcut)", "&Close"))
            self.btnClose.clicked.connect(self.reject)
            self.grpFooterLayout.addStretch()
            self.grpFooterLayout.addWidget(self.btnClose)
            self.layout_device.addItem(self.grpFooterLayout)

            self.main_layout.addLayout(self.layout_device, 0, 0)
            self.setLayout(self.main_layout)

            self.ReadDeviceInfo()

        def _select_cfw(self, event: QtGui.QMouseEvent) -> None:
            self.optCFW.setChecked(True)

        def _select_ofw(self, event: QtGui.QMouseEvent) -> None:
            self.optOFW.setChecked(True)

        def run(self) -> None:
            self.main_layout.update()
            self.main_layout.activate()
            screenGeometry = (self.screen() or QtGui.QGuiApplication.primaryScreen()).geometry()
            x = (screenGeometry.width() - self.width()) // 2
            y = (screenGeometry.height() - self.height()) // 2
            self.move(x, y)
            self.show()

        def hideEvent(self, event: QtGui.QHideEvent) -> None:
            if self.DEVICE is None:
                self.APP.ConnectDevice()
            self.APP.activateWindow()

        def reject(self) -> None:
            if self.CloseDialog():
                super().reject()

        def CloseDialog(self) -> bool:
            if self.btnClose.isEnabled() is False:
                text = (
                    __(
                        "<b>Warning:</b> If you close this window while a firmware update is still running, it might leave the device in an unbootable state."
                    )
                    + "<br><br>"
                    + __("Are you sure you want to close this window?")
                )
                msgbox = _message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Warning,
                    windowTitle=AppInfo.NAME,
                    text=text,
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                )
                msgbox.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
                answer = msgbox.exec()
                if answer == QtWidgets.QMessageBox.StandardButton.No:
                    return False
            return True

        def ReadDeviceInfo(self) -> None:
            device = self.DEVICE
            if device is None:
                raise RuntimeError("The GBxCart RW connection was closed")
            self.lblDeviceNameResult.setText(device.GetName())
            self.lblDeviceFWVerResult.setText(device.GetFirmwareVersion(more=True))
            self.lblDevicePCBVerResult.setText(device.GetPCBVersion())

        def ResetAVR(self, delay: float = 0.1) -> bool:
            try:
                with serial.Serial(self.PORT, 1_000_000, timeout=1) as dev:
                    dev.write(b"0")
                    dev.flush()
                    time.sleep(0.00125)
                    dev.write(struct.pack(">BIBB", 0x2A, 0x37653565, 0x31, 0))
                    dev.flush()
                    time.sleep(0.00125)
                    self.APP.QT_APP.processEvents()
                    time.sleep(0.3 + delay)
                    dev.reset_input_buffer()
                    dev.reset_output_buffer()
            except OSError, SerialException:
                return False
            return True

        def UpdateFirmware(self) -> bool | None:
            fw = ""
            path = ""
            archive_member: str | None
            if self.optCFW.isChecked():
                fw = self.CFW_VER
                archive_member = "cfw.hex"
            elif self.optOFW.isChecked():
                fw = self.OFW_VER
                archive_member = "ofw.hex"
            else:
                last_directory = self.APP.SETTINGS.value("LastDirFirmwareUpdate") or ""
                path = QtWidgets.QFileDialog.getOpenFileName(
                    self,
                    __("Choose GBxCart RW Firmware File"),
                    last_directory,
                    __("Firmware Update") + " (*.hex);;" + __("All Files") + " (*.*)",
                )[0]
                if path == "":
                    return
                temp = re.search(r"^(gbx(?:cart|mas)_rw_.+_pcb_r.+\.hex)$", os.path.basename(path))
                if temp is None:
                    msg = __(
                        "The expected filename for a valid firmware file is <b>{filename_pattern}</b>. Please visit {url} for the latest official firmware updates.",
                        filename_pattern="gbx*_rw_*_pcb_r*.hex",
                        url='<a href="https://www.gbxcart.com/">https://www.gbxcart.com</a>',
                    )
                    msgbox = _message_box(
                        parent=self,
                        icon=QtWidgets.QMessageBox.Icon.Critical,
                        windowTitle=AppInfo.NAME,
                        text=msg,
                        standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                    )
                    answer = msgbox.exec()
                    return
                self.APP.SETTINGS.setValue("LastDirFirmwareUpdate", os.path.dirname(path))
                fw = f"{path:s}\n\n" + __(
                    "Please double check that this is a valid firmware file for your GBxCart RW. If it is invalid or an update for a different device, it may render your device unusable."
                )
                archive_member = None

            text = __("The following firmware will now be written to your GBxCart RW device:") + f"\n- {fw}"
            text += "\n\n" + __("Do you want to continue?")
            msgbox = _message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Question,
                windowTitle=AppInfo.NAME,
                text=text,
                standardButtons=QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            )
            msgbox.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
            answer = msgbox.exec()
            if answer == QtWidgets.QMessageBox.StandardButton.No:
                return
            self.btnUpdate.setEnabled(False)
            self.btnClose.setEnabled(False)
            self.grpAvailableFwUpdates.setEnabled(False)

            try:
                if path == "":
                    if archive_member is None:
                        raise ValueError("No bundled firmware file was selected")
                    with (
                        zipfile.ZipFile(os.path.join(self.APP_PATH, "res", self.FW_FILES[self.PCB_VER])) as archive,
                        archive.open(archive_member) as firmware_file,
                    ):
                        ihex = firmware_file.read().decode("ascii")
                else:
                    with open(path, "rb") as firmware_file:
                        ihex = firmware_file.read().decode("ascii")
                buffer = _parse_intel_hex(ihex)
            except (
                OSError,
                UnicodeDecodeError,
                ValueError,
                zipfile.BadZipFile,
                KeyError,
            ) as exc:
                dprint("Invalid GBxCart RW firmware image:", exc)
                if "too large" in str(exc):
                    self.SetStatus(__("Firmware file is too large."))
                else:
                    self.SetStatus(__("Firmware checksum error."))
                self.prgStatus.setValue(0)
                self.btnUpdate.setEnabled(True)
                self.btnClose.setEnabled(True)
                self.grpAvailableFwUpdates.setEnabled(True)
                return False

            self.APP.DisconnectDevice()

            while True:
                ret = self.WriteFirmware(buffer, self.SetStatus)
                if ret == 1:
                    return True
                elif ret == 2:
                    return False
                elif ret == 3:
                    continue

        def SetStatus(
            self,
            text: str,
            enableUI: bool = False,
            setProgress: float | None = None,
        ) -> None:
            self.lblStatus.setText(__("Status: {text}", text=text))
            if setProgress is not None:
                self.prgStatus.setValue(round(setProgress))
            if enableUI:
                self.btnUpdate.setEnabled(True)
                self.btnClose.setEnabled(True)
                self.grpAvailableFwUpdates.setEnabled(True)

        def WriteFirmware(self, data: bytearray, fncSetStatus: StatusCallback) -> FirmwareUpdateResult:
            fw_buffer = data
            port = self.PORT

            delay = 0
            lives = 10
            buffer = bytearray()

            msgWarnBadResponse = __(
                "Failed to update your GBxCart RW {pcb_version} ({fw_version})!\n\n"
                "The firmware update failed as the device is not responding correctly. Please ensure you use a genuine GBxCart RW, re-connect using a different USB cable and try again.\n\n"
                "⚠️ Please note that FlashGBX does not work with the “{flashboy}” series devices.",
                pcb_version=self.PCB_VER,
                fw_version=self.FW_VER,
                flashboy="FLASH BOY",
            )

            fncSetStatus(text=__("Waiting for bootloader..."), setProgress=0)
            if self.ResetAVR(delay) is False:
                fncSetStatus(text=__("Bootloader error."), enableUI=True)
                self.prgStatus.setValue(0)
                msgbox = _message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Critical,
                    windowTitle=AppInfo.NAME
                    + " – "
                    + __(
                        "Firmware Updater for {device_name}",
                        device_name="GBxCart RW " + self.PCB_VER,
                    ),
                    text=msgWarnBadResponse,
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                )
                answer = msgbox.exec()
                return 2

            while True:
                try:
                    dev = serial.Serial(port=port, baudrate=9600 * 4, timeout=1)
                except OSError, SerialException:
                    fncSetStatus(text=__("Device access error."), enableUI=True)
                    return 2
                dev.reset_input_buffer()
                dev.reset_output_buffer()
                dev.write(b"@@@")
                dev.flush()
                time.sleep(0.00125)
                buffer = dev.read(0x11)
                if (len(buffer) < 0x11) or (buffer[0:3] != b"TSB"):
                    dev.write(b"?")
                    dev.flush()
                    time.sleep(0.00125)
                    dev.close()
                    self.APP.QT_APP.processEvents()
                    time.sleep(1)
                    if len(buffer) != 0x11:
                        delay += 0.05
                    fncSetStatus(
                        __(
                            "Waiting for bootloader... (+{milliseconds}ms)",
                            milliseconds=math.ceil(delay * 1000),
                        )
                    )
                    if self.ResetAVR(delay) is False:
                        fncSetStatus(text=__("Bootloader error."), enableUI=True)
                        msgbox = _message_box(
                            parent=self,
                            icon=QtWidgets.QMessageBox.Icon.Critical,
                            windowTitle=AppInfo.NAME
                            + " – "
                            + __(
                                "Firmware Updater for {device_name}",
                                device_name="GBxCart RW " + self.PCB_VER,
                            ),
                            text=msgWarnBadResponse,
                            standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                        )
                        answer = msgbox.exec()
                        return 2
                    lives -= 1
                    if lives < 0:
                        fncSetStatus(text=__("Bootloader timeout."), enableUI=True)
                        msgbox = _message_box(
                            parent=self,
                            icon=QtWidgets.QMessageBox.Icon.Critical,
                            windowTitle=AppInfo.NAME
                            + " – "
                            + __(
                                "Firmware Updater for {device_name}",
                                device_name="GBxCart RW " + self.PCB_VER,
                            ),
                            text=msgWarnBadResponse,
                            standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
                        )
                        answer = msgbox.exec()
                        return 2
                    continue
                break

            fncSetStatus(__("Reading bootloader information..."))
            (
                magic,
                tsb_version,
                tsb_status,
                signature,
                page_size,
                flash_size,
                eeprom_size,
                unknown,
                avr_jmp_identifier,
            ) = struct.unpack("<3sHB3sBHHBB", buffer[:-1])
            jmp_mode = "unknown"
            device_type = "unknown"
            if avr_jmp_identifier == 0x00:
                jmp_mode = "relative"
                device_type = "attiny"
            elif avr_jmp_identifier == 0x0C:
                jmp_mode = "absolute"
                device_type = "attiny"
            elif avr_jmp_identifier == 0xAA:
                jmp_mode = "relative"
                device_type = "atmega"
            info: BootloaderInfo = {
                "magic": magic,
                "tsb_version": tsb_version,
                "tsb_status": tsb_status,
                "signature": signature,
                "page_size": page_size * 2,
                "flash_size": flash_size * 2,
                "eeprom_size": eeprom_size + 1,
                "unknown": unknown,
                "avr_jmp_identifier": avr_jmp_identifier,
                "jmp_mode": jmp_mode,
                "device_type": device_type,
                "tsb_timeout": 0,
            }

            if (
                info["page_size"] != 64
                or info["flash_size"] != 7616
                or info["eeprom_size"] != 512
                or info["jmp_mode"] != "relative"
                or info["device_type"] != "atmega"
                or info["signature"] != b"\x1e\x93\x06"
            ):
                fncSetStatus(text="Wrong device detected.", enableUI=True)
                dev.close()
                return 2

            if info["tsb_version"] < 32768:
                info["tsb_version"] = int(
                    (info["tsb_version"] & 31)
                    + ((info["tsb_version"] & 480) / 32) * 100
                    + ((info["tsb_version"] & 65024) / 512) * 10000
                    + 20000000
                )
            else:
                fncSetStatus(text="Wrong device detected.", enableUI=True)
                dev.close()
                return 2

            #################

            # Read user data
            fncSetStatus(__("Reading user data..."))
            dev.write(b"c")
            user_data = bytearray(dev.read(0x41))
            if len(user_data) != 0x41:
                dev.close()
                fncSetStatus(text=__("Bootloader error."), enableUI=True)
                return 2
            info["tsb_timeout"] = user_data[2]

            # Change timeout to 6s
            fncSetStatus(__("Writing user data..."))
            user_data[2] = 254
            dev.write(b"C")
            dev.read(1)
            dev.write(b"!")
            dev.write(user_data)
            dev.flush()
            time.sleep(0.00125)
            dev.read(0x41)

            # Write firmware
            fncSetStatus(__("Updating firmware... Do not unplug the device!"))
            iterations = math.ceil(len(fw_buffer) / 0x40)
            if len(fw_buffer) < iterations * 0x40:
                fw_buffer = fw_buffer + bytearray([0xFF] * ((iterations * 0x40) - len(fw_buffer)))

            lives = 10
            dev.write(b"F")
            dev.flush()
            time.sleep(0.00125)
            ret = dev.read(1)
            while ret != b"?":
                dev.write(b"F")
                dev.flush()
                time.sleep(0.00125)
                ret = dev.read(1)
                lives -= 1
                if lives == 0:
                    dev.write(b"?")
                    dev.flush()
                    time.sleep(0.00125)
                    dev.close()
                    fncSetStatus(text=__("Protocol Error. Please try again."), enableUI=True)
                    msgbox = _message_box(
                        parent=self,
                        icon=QtWidgets.QMessageBox.Icon.Critical,
                        windowTitle=AppInfo.NAME,
                        text="The firmware update was not successful (Protocol Error). Do you want to try again?\n\nIf it doesn’t work even after multiple retries, please use the insideGadgets standalone firmware updater instead.",
                        standardButtons=QtWidgets.QMessageBox.StandardButton.Yes
                        | QtWidgets.QMessageBox.StandardButton.No,
                        defaultButton=QtWidgets.QMessageBox.StandardButton.Yes,
                    )
                    answer = msgbox.exec()
                    if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                        time.sleep(1)
                        return 3
                    return 2

            for i in range(iterations):
                self.APP.QT_APP.processEvents()
                dev.write(b"!")
                dev.write(fw_buffer[i * 0x40 : i * 0x40 + 0x40])
                fncSetStatus(
                    text=__("Updating firmware... Do not unplug the device!"),
                    setProgress=(i * 0x40 + 0x40) / len(fw_buffer) * 100,
                )
                ret = dev.read(1)
                if ret != b"?":
                    dev.write(b"?")
                    dev.flush()
                    time.sleep(0.00125)
                    dev.close()
                    fncSetStatus(
                        text=__(
                            "Write Error ({error_value}). Please try again.",
                            error_value=str(ret),
                        ),
                        enableUI=True,
                    )
                    msgbox = _message_box(
                        parent=self,
                        icon=QtWidgets.QMessageBox.Icon.Critical,
                        windowTitle=AppInfo.NAME,
                        text=__(
                            "The firmware update was not successful (Write Error, {error_value}). Do you want to try again?\n\nIf it doesn’t work even after multiple retries, please use the insideGadgets standalone firmware updater instead.",
                            error_value=str(ret),
                        ),
                        standardButtons=QtWidgets.QMessageBox.StandardButton.Yes
                        | QtWidgets.QMessageBox.StandardButton.No,
                        defaultButton=QtWidgets.QMessageBox.StandardButton.Yes,
                    )
                    answer = msgbox.exec()
                    if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                        time.sleep(1)
                        return 3
                    return 2
            dev.write(b"?")
            dev.flush()
            time.sleep(0.00125)
            dev.read(1)

            # verify flash
            fncSetStatus(__("Verifying update..."))
            buffer2 = bytearray()
            dev.write(b"f")
            dev.flush()
            time.sleep(0.00125)
            for i in range(0, 0x1DC0, 0x40):
                self.APP.QT_APP.processEvents()
                dev.write(b"!")
                dev.flush()
                time.sleep(0.00125)
                read_deadline = time.monotonic() + 1
                while dev.in_waiting == 0 and time.monotonic() < read_deadline:
                    time.sleep(0.01)
                if dev.in_waiting == 0:
                    dev.close()
                    fncSetStatus(text=__("Verification Error."), enableUI=True)
                    return 2
                ret = bytearray(dev.read(0x40))
                buffer2 += ret
                self.prgStatus.setValue(round(len(buffer2) / 0x1DC0 * 100))
            dev.read(1)

            buffer2 = buffer2[: len(fw_buffer)]

            if fw_buffer == buffer2:
                fncSetStatus(__("Verification OK."))
                self.APP.QT_APP.processEvents()
                time.sleep(0.2)
            else:
                fncSetStatus(text=__("Verification Error."), enableUI=True)
                dev.write(b"?")
                dev.flush()
                time.sleep(0.00125)
                dev.close()
                msgbox = _message_box(
                    parent=self,
                    icon=QtWidgets.QMessageBox.Icon.Critical,
                    windowTitle=AppInfo.NAME,
                    text=__(
                        "The firmware update was not successful (Verification Error). Do you want to try again?\n\nIf it doesn’t work even after multiple retries, please use the insideGadgets standalone firmware updater instead."
                    ),
                    standardButtons=QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    defaultButton=QtWidgets.QMessageBox.StandardButton.Yes,
                )
                answer = msgbox.exec()
                if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                    time.sleep(1)
                    return 3
                return 2

            # Change timeout to 1s
            fncSetStatus(__("Writing user data..."))
            user_data[2] = 42
            dev.write(b"C")
            dev.flush()
            time.sleep(0.00125)
            ret = dev.read(1)
            while ret != b"?":
                dev.write(b"C")
                dev.flush()
                time.sleep(0.00125)
                ret = dev.read(1)
                lives -= 1
                if lives == 0:
                    dev.write(b"?")
                    dev.flush()
                    time.sleep(0.00125)
                    dev.close()
                    fncSetStatus(
                        text=__("User data update error. Please try again."),
                        enableUI=True,
                    )
                    return 2
            dev.write(b"!")
            dev.write(user_data)
            dev.flush()
            time.sleep(0.00125)
            dev.read(0x41)

            # Restart
            self.APP.QT_APP.processEvents()
            time.sleep(0.1)
            fncSetStatus(__("Restarting the device..."))
            dev.write(b"?")
            dev.flush()
            time.sleep(0.00125)
            dev.close()
            self.APP.QT_APP.processEvents()
            time.sleep(0.8)
            fncSetStatus(__("Done!"))
            self.APP.QT_APP.processEvents()
            time.sleep(0.2)
            self.DEVICE = None
            self.btnUpdate.setEnabled(True)
            self.btnClose.setEnabled(True)
            self.grpAvailableFwUpdates.setEnabled(True)
            text = __("The firmware update is complete!")
            msgbox = _message_box(
                parent=self,
                icon=QtWidgets.QMessageBox.Icon.Information,
                windowTitle=AppInfo.NAME,
                text=text,
                standardButtons=QtWidgets.QMessageBox.StandardButton.Ok,
            )
            answer = msgbox.exec()
            self.reject()
            return 1
except ImportError:
    pass
