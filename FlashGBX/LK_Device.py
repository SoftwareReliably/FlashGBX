# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import base64
import copy
import datetime
import hashlib
import json
import math
import os
import platform
import struct
import threading
import time
import traceback
import zlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, ClassVar, Literal, NamedTuple, Protocol, overload

import serial  # pyright: ignore[reportMissingModuleSource]
from serial import (  # pyright: ignore[reportMissingModuleSource]
    PortNotOpenError,
    SerialException,
    SerialTimeoutException,
)

from FlashGBX.RomFileAGB import AGBHeader

from .app import AppContext, AppInfo, generate_filename
from .CartridgeTypes import AgbSaveTypes, DmgSaveTypes
from .Flashcart import (
    CFI,
    Flashcart,
    Flashcart_AGB_GBAMP,
    Flashcart_DMG_BUNG_16M,
    Flashcart_DMG_MMSA,
    FlashcartCallbacks,
)
from .Formatter import Formatter
from .GBMemory import GBMemoryMap
from .i18n import __, ___, c__
from .Logging import ANSI, dprint, logger  # pyright: ignore[reportAttributeAccessIssue]
from .Mapper import AGB_GPIO, DMG_Mapper
from .RomFileAGB import RomFileAGB
from .RomFileDMG import RomFileDMG

if TYPE_CHECKING:
    from FlashGBX.RomFileAGB import AGBHeader

DeviceMode = Literal["DMG", "AGB"]
DeviceReadResult = int | bytearray | Literal[False]
DeviceWriteResult = int | Literal[False] | None
ProgressUpdate = dict[str, Any]
FlashcartRegistry = Mapping[str, Mapping[str, Any]]
FirmwareInfo = dict[str, Any]
FirmwareUpdaterClasses = tuple[type[Any] | None, type[Any]] | None
CartridgeDetectionResult = tuple[Any, ...] | Literal[False] | None
ROMBackupResult = bool | int | None
FirmwareVariable = Literal[
    "ADDRESS",
    "AUTO_POWEROFF_TIME",
    "TRANSFER_SIZE",
    "BUFFER_SIZE",
    "DMG_ROM_BANK",
    "STATUS_REGISTER",
    "LAST_BANK_ACCESSED",
    "STATUS_REGISTER_MASK",
    "STATUS_REGISTER_VALUE",
    "CART_MODE",
    "DMG_ACCESS_MODE",
    "FLASH_COMMAND_SET",
    "FLASH_METHOD",
    "FLASH_WE_PIN",
    "FLASH_PULSE_RESET",
    "FLASH_COMMANDS_BANK_1",
    "FLASH_SHARP_VERIFY_SR",
    "DMG_READ_CS_PULSE",
    "DMG_WRITE_CS_PULSE",
    "FLASH_DOUBLE_DIE",
    "DMG_READ_METHOD",
    "AGB_READ_METHOD",
    "CART_POWERED",
    "PULLUPS_ENABLED",
    "AUTO_POWEROFF_ENABLED",
    "AGB_IRQ_ENABLED",
    "DMG_AUDIO_ENABLED",
]


class ProgressSignal(Protocol):
    """Structural cart_type for Qt-style progress signals."""

    def emit(self, update: ProgressUpdate) -> object: ...


class MBC6FlashMapper(Protocol):
    """Mapper operations required by the MBC6 flash writer."""

    def GetROMBank(self) -> int: ...

    def SelectBankFlash(self, index: int) -> object: ...


class _ROMBackupMapper(Protocol):
    def GetName(self) -> str: ...

    def CalcChecksum(self, buffer: bytearray) -> int: ...

    def HasHiddenSector(self) -> bool: ...

    def ReadHiddenSector(self) -> bytearray | Literal[False]: ...


class _SaveMapper(Protocol):
    def GetName(self) -> str: ...

    def SelectBankRAM(self, bank: int) -> object: ...

    def EnableRAM(self, enable: bool = True) -> object: ...

    def HasRTC(self) -> bool: ...

    def LatchRTC(self) -> object: ...

    def ReadRTC(self) -> bytearray | Literal[False]: ...

    def GetRTCBufferSize(self) -> int: ...

    def WriteRTC(self, buffer: bytearray, advance: bool = False) -> None: ...

    def SelectBankROM(self, bank: int) -> object: ...


class _DmgRtcMapper(Protocol):
    def HasRTC(self) -> bool: ...

    def GetName(self) -> str: ...

    def EnableMapper(self) -> object: ...

    def LatchRTC(self) -> object: ...

    def ReadRTC(self) -> bytearray | bool: ...

    def GetRTCDict(self) -> dict[str, Any]: ...

    def GetRTCString(self) -> str: ...

    def SelectBankROM(self, index: int) -> object: ...

    def ReadHiddenSector(self) -> object: ...

    def EnableRAM(self, enable: bool = True) -> object: ...

    def SelectBankRAM(self, index: int) -> object: ...


class _SaveActionMapper(Protocol):
    def GetName(self) -> str: ...


class _HiddenSectorMapper(Protocol):
    def GetName(self) -> str: ...

    def ReadHiddenSector(self) -> object: ...


class _FlashResetMapper(Protocol):
    def ResetBeforeBankChange(self, bank: int) -> bool: ...

    def SelectBankROM(self, bank: int) -> object: ...


ProgressCallback = Callable[[ProgressUpdate], object]


class _FlashVerificationContext(NamedTuple):
    args: dict[str, Any]
    cart_type: dict[str, Any]
    flashcart: Flashcart
    data_import: bytearray
    flash_offset: int
    active_voltage: Any
    verify_sectors: list[list[int]]
    rom_bank_size: int
    mbc: Any
    buffer_len: int


class _FlashIdSearchContext(NamedTuple):
    mode: DeviceMode
    rom: bytearray
    flash_id_commands: list[Any]
    cfi_buffer: bytearray | None
    read_cfi_commands: list[Any]
    flash_id_methods: list[Any]
    flash_id_text: str
    write_enable_pins: list[str]


class _FlashConfiguration(NamedTuple):
    mbc: Any
    end_bank: int
    rom_bank_size: int
    enable_pullup_wr: int
    error_message: str
    buffer_size: int | Literal[False]


class _AGBSaveConfiguration(NamedTuple):
    buffer_len: int
    save_size: int
    ram_banks: int
    flash_chip: int
    sram_5: int
    command: Any
    empty_data_byte: int
    extra_size: int


class _DMGSaveConfiguration(NamedTuple):
    mbc: Any
    buffer_len: int
    save_size: int
    ram_banks: int
    empty_data_byte: int
    extra_size: int
    audio_low: bool


class _SaveTransferActionParameters(NamedTuple):
    mbc: _SaveActionMapper | None
    save_size: int
    empty_data_byte: int
    ram_banks: int
    extra_size: int


class _SaveBankContext(NamedTuple):
    args: dict[str, Any]
    mbc: Any
    bank: int
    save_size: int
    buffer_length: int
    buffer_offset: int
    agb_flash_chip: int


class _SaveBankRange(NamedTuple):
    start_address: int
    end_address: int
    buffer_length: int


class _FlashSectorPlan(NamedTuple):
    data_import: bytearray
    smallest_sector_size: int | Literal[False]
    sector_offsets: list[list[int]]
    write_sectors: list[list[int]]
    delta_state: list[list[int]] | None
    state_path: str | Path
    has_sector_map: bool


class _FlashWritePreparation(NamedTuple):
    cart_name: str
    cart_type: dict[str, Any]
    flashcart: Flashcart
    data_import: bytearray
    data_map_import: bytearray
    flash_offset: int
    active_voltage: float
    mbc: Any
    end_bank: int
    rom_bank_size: int
    enable_pullup_wr: int
    error_message: str
    flash_buffer_size: int | Literal[False]
    command_set_type: str
    sector_offsets: list[list[int]]
    write_sectors: list[list[int]]
    delta_state: list[list[int]] | None
    state_path: str | Path
    chip_erase: bool
    buffer_len: int
    verify_sectors: list[list[int]]


class _FlashSectorState(NamedTuple):
    retry_hp: int
    buffer_pos: int
    start_address: int
    end_address: int
    sector_pos: int
    start_bank: int
    end_bank: int


class _ROMReadConfiguration(NamedTuple):
    mbc: Any
    size: int
    rom_banks: int
    rom_bank_size: int
    buffer_len: int
    is_3d_memory: bool


class _ROMReadRange(NamedTuple):
    buffer_position: int
    start_address: int
    end_address: int
    start_bank: int
    end_bank: int


class _IncompleteROMReadContext(NamedTuple):
    args: Mapping[str, Any]
    buffer_length: int
    total_position: int
    max_length: int
    lives: int


class _IncompleteROMReadResult(NamedTuple):
    max_length: int
    lives: int
    abort_verification: bool


class _SaveReadParameters(NamedTuple):
    args: Mapping[str, Any]
    mbc: Any
    bank: int
    pos: int
    buffer_len: int
    command: Any
    max_length: int


class _SaveWriteParameters(NamedTuple):
    args: Mapping[str, Any]
    mbc: Any
    bank: int
    pos: int
    buffer: bytearray
    buffer_offset: int
    buffer_len: int
    command: Any
    agb_flash_chip: int


class _FlashChunkParameters(NamedTuple):
    command_set_type: str
    pos: int
    data_import: bytearray
    buffer_pos: int
    buffer_len: int
    bank: int
    flash_buffer_size: int | Literal[False]
    skip_init: bool
    rumble: bool


class _FlashBankContext(NamedTuple):
    mbc: Any
    flashcart: Flashcart
    cart_type: dict[str, Any]
    bank: int
    current_bank: int | None
    start_address: int
    end_address: int
    buffer_position: int
    rom_bank_size: int
    sector_size: int
    buffer_length: int


class _FlashBankState(NamedTuple):
    start_address: int
    end_address: int
    current_bank: int | None
    buffer_length: int


class _FlashMatchContext(NamedTuple):
    args: Mapping[str, Any]
    preparation: _FlashWritePreparation
    sector: list[int]
    bank: int
    end_bank: int
    current_bank: int | None
    start_address: int
    end_address: int
    buffer_position: int
    sector_position: int
    buffer_length: int


class _FlashMatchResult(NamedTuple):
    skipped: bool
    canceled: bool
    bank: int
    current_bank: int | None
    start_address: int
    end_address: int
    buffer_position: int
    sector_size: int
    buffer_length: int


class LK_Device(ABC):
    DEVICE_NAME: str = ""
    DEVICE_MIN_FW: ClassVar[int] = 0
    DEVICE_MAX_FW: ClassVar[int] = 0
    DEVICE_LATEST_FW_TS: ClassVar[Any] = {}
    PCB_VERSIONS: ClassVar[dict[int, str]] = {}
    BAUDRATE: int = 1_000_000
    MAX_BUFFER_READ: int = 0x1000
    MAX_BUFFER_WRITE: int = 0x400

    DEVICE_CMD: ClassVar[dict[str, int]] = {
        "NULL": 0x30,
        "DEBUG": 0xA0,
        "QUERY_FW_INFO": 0xA1,
        "SET_MODE_AGB": 0xA2,
        "SET_MODE_DMG": 0xA3,
        "SET_VOLTAGE_3_3V": 0xA4,
        "SET_VOLTAGE_5V": 0xA5,
        "SET_VARIABLE": 0xA6,
        "SET_FLASH_CMD": 0xA7,
        "SET_ADDR_AS_INPUTS": 0xA8,
        "CLK_TOGGLE": 0xA9,
        "ENABLE_PULLUPS": 0xAB,
        "DISABLE_PULLUPS": 0xAC,
        "GET_VARIABLE": 0xAD,
        "GET_VAR_STATE": 0xAE,
        "SET_VAR_STATE": 0xAF,
        "DMG_CART_READ": 0xB1,
        "DMG_CART_WRITE": 0xB2,
        "DMG_CART_WRITE_SRAM": 0xB3,
        "DMG_MBC_RESET": 0xB4,
        "DMG_MBC7_READ_EEPROM": 0xB5,
        "DMG_MBC7_WRITE_EEPROM": 0xB6,
        "DMG_MBC6_MMSA_WRITE_FLASH": 0xB7,
        "DMG_SET_BANK_CHANGE_CMD": 0xB8,
        "DMG_EEPROM_WRITE": 0xB9,
        "DMG_CART_READ_MEASURE": 0xBA,
        "AGB_CART_READ": 0xC1,
        "AGB_CART_WRITE": 0xC2,
        "AGB_CART_READ_SRAM": 0xC3,
        "AGB_CART_WRITE_SRAM": 0xC4,
        "AGB_CART_READ_EEPROM": 0xC5,
        "AGB_CART_WRITE_EEPROM": 0xC6,
        "AGB_CART_WRITE_FLASH_DATA": 0xC7,
        "AGB_CART_READ_3D_MEMORY": 0xC8,
        "AGB_BOOTUP_SEQUENCE": 0xC9,
        "AGB_READ_GPIO_RTC": 0xCA,
        "DMG_FLASH_WRITE_BYTE": 0xD1,
        "AGB_FLASH_WRITE_SHORT": 0xD2,
        "FLASH_PROGRAM": 0xD3,
        "CART_WRITE_FLASH_CMD": 0xD4,
        "CALC_CRC32": 0xD5,
        "BOOTLOADER_RESET": 0xF1,
        "CART_PWR_ON": 0xF2,
        "CART_PWR_OFF": 0xF3,
        "QUERY_CART_PWR": 0xF4,
        "SET_PIN": 0xF5,
        "GET_SWITCH_STATE": 0xF6,
        "PING": 0xFE,
    }
    # \#define VAR(\d+)_([^\t]+)\t+(.+)
    DEVICE_VAR: ClassVar[dict[FirmwareVariable, tuple[int, int]]] = {
        "ADDRESS": (32, 0x00),
        "AUTO_POWEROFF_TIME": (32, 0x01),
        "TRANSFER_SIZE": (16, 0x00),
        "BUFFER_SIZE": (16, 0x01),
        "DMG_ROM_BANK": (16, 0x02),
        "STATUS_REGISTER": (16, 0x03),
        "LAST_BANK_ACCESSED": (16, 0x04),
        "STATUS_REGISTER_MASK": (16, 0x05),
        "STATUS_REGISTER_VALUE": (16, 0x06),
        "CART_MODE": (8, 0x00),
        "DMG_ACCESS_MODE": (8, 0x01),
        "FLASH_COMMAND_SET": (8, 0x02),
        "FLASH_METHOD": (8, 0x03),
        "FLASH_WE_PIN": (8, 0x04),
        "FLASH_PULSE_RESET": (8, 0x05),
        "FLASH_COMMANDS_BANK_1": (8, 0x06),
        "FLASH_SHARP_VERIFY_SR": (8, 0x07),
        "DMG_READ_CS_PULSE": (8, 0x08),
        "DMG_WRITE_CS_PULSE": (8, 0x09),
        "FLASH_DOUBLE_DIE": (8, 0x0A),
        "DMG_READ_METHOD": (8, 0x0B),
        "AGB_READ_METHOD": (8, 0x0C),
        "CART_POWERED": (8, 0x0D),
        "PULLUPS_ENABLED": (8, 0x0E),
        "AUTO_POWEROFF_ENABLED": (8, 0x0F),
        "AGB_IRQ_ENABLED": (8, 0x10),
        "DMG_AUDIO_ENABLED": (8, 0x11),
    }

    ACTIONS: ClassVar[dict[str, int]] = {
        "ROM_READ": 1,
        "SAVE_READ": 2,
        "SAVE_WRITE": 3,
        "ROM_WRITE": 4,
        "ROM_WRITE_VERIFY": 4,
        "SAVE_WRITE_VERIFY": 3,
        "RTC_WRITE": 5,
        "DETECT_CART": 6,
    }
    SUPPORTED_CARTS: dict[str, dict[str, Any]] = {}  # noqa: RUF012 - copied into each initialized instance

    FW: Any = {}  # noqa: RUF012 - legacy fallback for backends that do not call super().__init__
    FW_UPDATE_REQ = False
    FW_VAR = {}  # noqa: RUF012 - legacy fallback for backends that do not call super().__init__
    MODE = None
    PORT = ""
    DEVICE = None
    WORKER = None
    INFO = {  # noqa: RUF012 - legacy fallback for backends that do not call super().__init__
        "action": None,
        "last_action": None,
        "dump_info": {},
    }
    ERROR = False
    ERROR_ARGS = {}  # noqa: RUF012 - legacy fallback for backends that do not call super().__init__
    CANCEL = False
    CANCEL_ARGS = {}  # noqa: RUF012 - legacy fallback for backends that do not call super().__init__
    SIGNAL = None
    POS = 0
    NO_PROG_UPDATE = False
    FAST_READ = False
    SKIPPING = False
    DEVICE_TIMEOUT = 1
    WRITE_DELAY = False
    READ_ERRORS = 0
    WRITE_ERRORS = 0
    DMG_READ_METHOD = 1
    DMG_READ_METHODS: ClassVar[tuple[str, ...]] = ("RD", "A15", "SlowA15")
    AGB_READ_METHOD = 0
    AGB_READ_METHODS: ClassVar[tuple[str, ...]] = ("Single", "MemCpy", "Stream")
    LAST_CHECK_ACTIVE = 0
    USER_ANSWER = None
    SKIP_POWERCYCLE = False
    VOLTAGE_FALLBACK_PENDING = False
    VOLTAGE_FALLBACK_TRIGGERED = False
    THREAD_AUTO_POWEROFF_TIME = None

    def __init__(self) -> None:
        """Initialize transfer state that must never be shared by instances."""
        # Backends expose slightly different firmware dictionaries. Keep the
        # storage flexible and provide a checked, typed accessor below.
        self.FW: Any = None
        self.FW_UPDATE_REQ = False
        self.FW_VAR: dict[str, int] = {}
        self.MODE: DeviceMode | None = None
        self.PORT = ""
        self.DEVICE: serial.Serial | None = None
        self.WORKER: Any | None = None
        self.INFO: dict[str, Any] = {"action": None, "last_action": None, "dump_info": {}}
        self.ERROR = False
        self.ERROR_ARGS: dict[str, Any] = {}
        self.CANCEL = False
        self.CANCEL_ARGS: dict[str, Any] = {}
        self.SIGNAL: ProgressSignal | ProgressCallback | None = None
        self.POS = 0
        self.NO_PROG_UPDATE = False
        self.FAST_READ = False
        self.SKIPPING = False
        self.DEVICE_TIMEOUT = 1.0
        self.WRITE_DELAY = False
        self.READ_ERRORS = 0
        self.WRITE_ERRORS = 0
        self.DMG_READ_METHOD = 1
        self.AGB_READ_METHOD = 0
        self.LAST_CHECK_ACTIVE = 0.0
        self.USER_ANSWER: bool | None = None
        self.SKIP_POWERCYCLE = False
        self.VOLTAGE_FALLBACK_PENDING = False
        self.VOLTAGE_FALLBACK_TRIGGERED = False
        self.THREAD_AUTO_POWEROFF_TIME: int | Literal[False] | None = None
        self.SUPPORTED_CARTS = copy.deepcopy(type(self).SUPPORTED_CARTS)

    @abstractmethod
    def Initialize(
        self,
        flashcarts: FlashcartRegistry | None = None,
        port: str | None = None,
        max_baud: int = 2_000_000,
    ) -> list[list[int | str]] | Literal[False]:
        raise NotImplementedError

    @abstractmethod
    def LoadFirmwareVersion(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def GetFirmwareVersion(self, more: bool = False) -> str:
        raise NotImplementedError

    @abstractmethod
    def ChangeBaudRate(self, baudrate: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def CanSetVoltageBySwitch(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def CanSetVoltageByCode(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def CanSetVoltageByAutoswitch(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def CanPowerCycleCart(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def GetSupprtedModes(self) -> Sequence[str]:
        raise NotImplementedError

    @abstractmethod
    def IsSupported3dMemory(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def IsClkConnected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def GetFullNameExtended(self, more: bool = False) -> str:
        raise NotImplementedError

    @abstractmethod
    def SupportsFirmwareUpdates(self) -> bool:
        raise NotImplementedError

    def FirmwareUpdateAction(self) -> str | None:
        return getattr(type(self), "FWUPDATE_ACTION", None)

    def CLIUpdaterMethod(self) -> str | None:
        return getattr(type(self), "CLI_UPDATER_METHOD", None)

    def GetSupportMessage(self) -> str | None:
        message = getattr(type(self), "DEVICE_SUPPORT_MESSAGE", None)
        if callable(message):
            message = message()
        return message if isinstance(message, str) else None

    @abstractmethod
    def FirmwareUpdateAvailable(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def GetFirmwareUpdaterClass(self) -> FirmwareUpdaterClasses:
        raise NotImplementedError

    @abstractmethod
    def ResetLEDs(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def SupportsBootloaderReset(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def BootloaderReset(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def SupportsAudioAsWe(self) -> bool:
        raise NotImplementedError

    #################################################################

    def _firmware_info(self) -> FirmwareInfo:
        if not isinstance(self.FW, dict):
            msg = "Firmware information is not available"
            raise ConnectionError(msg)
        return self.FW

    def _serial_device(self) -> serial.Serial:
        if self.DEVICE is None:
            msg = "The cartridge reader is not connected"
            raise ConnectionError(msg)
        return self.DEVICE

    def CheckActive(self) -> bool:
        if time.time() < self.LAST_CHECK_ACTIVE + 1:
            return True
        dprint("Checking if device is active")
        if self.DEVICE is None:
            return False
        firmware = self.FW
        if firmware is None or firmware.get("pcb_name") is None:
            if self.LoadFirmwareVersion():
                self.LAST_CHECK_ACTIVE = time.time()
                return True
            return False
        try:
            if firmware.get("cfw_id") == "L" and firmware.get("fw_ver", 0) >= 15:
                challenge = os.urandom(1)[0]
                self._write(bytearray([self.DEVICE_CMD["PING"], challenge]))
                response = self._read(1)
                if response is False or response != ((~challenge) & 0xFF):
                    msg = (
                        f"Invalid firmware response (ping response was {response!s} instead of {(~challenge) & 0xFF!s})"
                    )
                    raise ConnectionError(msg)  # noqa: TRY301
            else:
                modes: Sequence[str] = self.GetSupprtedModes()
                mode: int | Literal[False] = self._get_fw_variable("CART_MODE")
                if mode > len(modes):
                    msg_0: str = f"Invalid firmware response (mode={mode - 1!s})"
                    raise ConnectionError(msg_0)  # noqa: TRY301
        except Exception as e:
            if self.USER_ANSWER is not True:  # Called from CartPowerCycleOrAskReconnect()
                print(
                    ANSI.RED
                    + __(
                        "Disconnecting from the device, because a critical error occurred!\n{error}",
                        error=str(e),
                    )
                    + ANSI.RESET,
                )
                dprint(
                    __(
                        "Disconnecting from the device, because a critical error occurred!\n{error}",
                        error=str(e),
                    ),
                )
            try:
                device = self._serial_device()
                if device.is_open:
                    device.reset_input_buffer()
                    device.reset_output_buffer()
                    device.close()
                self.DEVICE = None
            except Exception:
                logger.exception("Failed to close the device after a connection error")
            return False
        else:
            self.LAST_CHECK_ACTIVE = time.time()
            return True

    def IsSupportedMbc(self, mbc: int) -> bool:
        return mbc in (
            0x00,
            0x01,
            0x02,
            0x03,
            0x05,
            0x06,
            0x08,
            0x09,
            0x0B,
            0x0D,
            0x0F,
            0x10,
            0x11,
            0x12,
            0x13,
            0x19,
            0x1A,
            0x1B,
            0x1C,
            0x1D,
            0x1E,
            0x20,
            0x22,
            0xFC,
            0xFD,
            0xFE,
            0xFF,
            0x101,
            0x103,
            0x104,
            0x105,
            0x110,
            0x201,
            0x202,
            0x203,
            0x204,
            0x205,
            0x206,
        )

    def IsUnregistered(self) -> bool:
        return bool(self.FW and self.FW.get("unregistered", False))

    def TryConnect(self, port: str, baudrate: int) -> bool:
        dprint(f"Trying to connect to {port:s} at baud rate {baudrate:d} ({type(self).__module__:s})")
        dev: serial.Serial | None = None
        try:
            dev = serial.Serial(port, baudrate, timeout=0.1, exclusive=True)
        except (SerialException, OSError) as e:
            dprint(f"Couldn't connect to port {port:s} at baudrate {baudrate:d}:", e)
            return False
        try:
            self.DEVICE = dev
            return bool(self.LoadFirmwareVersion())
        finally:
            self.DEVICE = None
            dev.close()

    def GetBaudRate(self) -> int:
        return self.BAUDRATE

    def SetDMGReadMethod(self, method: int) -> None:
        if self._firmware_info().get("fw_ver", 0) < 12:
            return
        if method < 0 or method >= len(self.DMG_READ_METHODS):
            method = 0
        self.DMG_READ_METHOD: int = method
        self._set_fw_variable("DMG_READ_METHOD", self.DMG_READ_METHOD)

    def SetAGBReadMethod(self, method: int) -> None:
        if self._firmware_info().get("fw_ver", 0) < 12:
            return
        if method < 0 or method >= len(self.AGB_READ_METHODS):
            method = 0
        self.AGB_READ_METHOD: int = method
        self._set_fw_variable("AGB_READ_METHOD", self.AGB_READ_METHOD)

    def SetPin(self, pins: Sequence[int | str], set_high: bool) -> DeviceWriteResult:
        if self._firmware_info().get("fw_ver", 0) < 12:
            return None
        pin_names: list[str] = ["CART_POWER", "PIN_CLK", "PIN_WR", "PIN_RD", "PIN_CS"]
        pin_names.extend([f"PIN_A{i}" for i in range(24)])
        pin_names += ["PIN_CS2", "PIN_AUDIO"]
        value = 0
        selected_pins: list[int] = []
        for pin in pins:
            if isinstance(pin, int):
                pin_index: int = pin
                if pin_index < 0 or pin_index >= len(pin_names):
                    print(__("Invalid pin index specified:"), pin)
                    continue
            else:
                if pin not in pin_names:
                    print(__("Invalid pin index specified:"), pin)
                    continue
                pin_index = pin_names.index(pin)
            selected_pins.append(pin_index)
            value |= 1 << pin_index

        for pin_index in selected_pins:
            dprint(f"Setting pin {pin_names[pin_index]}: {set_high}")

        # dprint(f"Value: {value:031b}")
        buffer = bytearray([self.DEVICE_CMD["SET_PIN"]])
        buffer.extend(struct.pack(">I", value))
        buffer.extend(struct.pack("B", 1 if set_high else 0))
        return self._write(buffer, wait=True)

    def _GetSwitchState(self) -> int | Literal[False]:
        if self._firmware_info().get("fw_ver", 0) < 15:
            return False
        self._write(self.DEVICE_CMD["GET_SWITCH_STATE"])
        state: int | Literal[False] = self._read(1)
        return state if isinstance(state, int) else False

    def GetCartModeSwitchState(self) -> int | Literal[False]:
        firmware: FirmwareInfo = self._firmware_info()
        if firmware.get("fw_ver", 0) < 15:
            return False
        if not firmware.get("cart_mode_switch", False):
            return False
        state: int | Literal[False] = self._GetSwitchState()
        if state is False:
            return False
        state_mode: int = (state >> 1) & 1
        if state_mode == 1:
            dprint("Cartridge Mode Switch: DMG")
            return 0
        dprint("Cartridge Mode Switch: AGB")
        return 1

    def GetCartPresenceSwitchState(self) -> int | Literal[False]:
        firmware = self._firmware_info()
        if firmware.get("fw_ver", 0) < 15:
            return False
        if not firmware.get("cart_presence_switch", False):
            return False
        state: int | Literal[False] = self._GetSwitchState()
        if state is False:
            return False
        state_presence: int = state & 1
        if state_presence == 1:
            dprint("Cartridge Presence Switch: ON")
        else:
            dprint("Cartridge Presence Switch: OFF")
        return state_presence

    def UpdateFlashCarts(self, flashcarts: FlashcartRegistry) -> None:
        self.SUPPORTED_CARTS = {
            "DMG": {"Generic ROM Cartridge": "RETAIL"},
            "AGB": {"Generic ROM Cartridge": "RETAIL"},
        }
        for mode in flashcarts:
            for key in sorted(flashcarts[mode].keys(), key=str.casefold):
                self.SUPPORTED_CARTS[mode][key] = flashcarts[mode][key]

    def IsConnected(self) -> bool:
        device = self.DEVICE
        if device is None:
            return False
        if not device.is_open:
            return False
        try:
            while device.in_waiting > 0:
                dprint(
                    f"Clearing input buffer... ({device.in_waiting:d})",
                    device.read(device.in_waiting),
                )
                device.reset_input_buffer()
                time.sleep(0.05)
            device.reset_output_buffer()
            return self.CheckActive()
        except SerialException as e:
            print(__("Connection lost!"))
            try:
                if e.args and isinstance(e.args[0], str) and e.args[0].startswith("ClearCommError failed"):
                    device.close()
                    return False
            except Exception:
                logger.exception("Failed to inspect or close the device after a serial error")
            print(str(e))
            return False

    def Close(self, cartPowerOff: bool = False) -> None:
        device = self.DEVICE
        try:
            if device is not None and self.IsConnected():
                dprint("Disconnecting from the device")
                firmware = self._firmware_info()
                if cartPowerOff and self.CanPowerCycleCart():
                    self._set_fw_variable("AUTO_POWEROFF_TIME", 0)
                    if firmware.get("fw_ver", 0) >= 12 or "OFW_CART_PWR_OFF" not in self.DEVICE_CMD:
                        self._write(
                            self.DEVICE_CMD["CART_PWR_OFF"],
                            wait=firmware.get("fw_ver", 0) >= 12,
                        )
                    else:
                        self._write(self.DEVICE_CMD["OFW_CART_PWR_OFF"], wait=False)
                else:
                    self._write(
                        self.DEVICE_CMD["SET_VOLTAGE_3_3V"],
                        wait=firmware.get("fw_ver", 0) >= 12,
                    )
        except ConnectionError, OSError, SerialException:
            logger.exception("Failed to shut down the cartridge reader cleanly")
        finally:
            self.DEVICE = None
            self.MODE = None
            try:
                if device is not None and device.is_open:
                    device.close()
            except OSError, SerialException:
                logger.exception("Failed to close the cartridge reader serial port")

    def GetName(self) -> str:
        return self.DEVICE_NAME

    def GetFullNameLabel(self) -> str:
        return f"{self.GetFullName():s} - Firmware {self.GetFirmwareVersion():s}"

    def GetPCBVersion(self) -> str:
        pcb_version = self._firmware_info().get("pcb_ver")
        if pcb_version in self.PCB_VERSIONS:
            return self.PCB_VERSIONS[pcb_version]
        return "(" + c__("Device Type", "unknown revision") + ")"

    def GetFullName(self) -> str:
        if len(self.GetPCBVersion()) > 0:
            return f"{self.GetName():s} {self.GetPCBVersion():s}"
        return self.GetName()

    def GetPort(self) -> str:
        return self.PORT

    def GetFWBuildDate(self) -> str:
        return self._firmware_info().get("fw_dt", "")

    def SetWriteDelay(self, enable: bool = True) -> None:
        if enable != self.WRITE_DELAY:
            dprint("Setting Write Delay to", enable)
            self.WRITE_DELAY = enable

    def SetTimeout(self, seconds: float = 1) -> None:
        seconds = max(seconds, 0.1)
        self.DEVICE_TIMEOUT = seconds
        self._serial_device().timeout = self.DEVICE_TIMEOUT

    def AbortOperation(self, from_user: bool = True) -> None:
        self.CANCEL_ARGS["from_user"] = from_user
        self.CANCEL = True
        self.ERROR = False

    def wait_for_ack(self, values: Sequence[int] | None = None) -> int | Literal[False]:
        if values is None:
            values = (0x01, 0x03)
        buffer = self._read(1)
        if buffer not in values:
            tb_stack = traceback.extract_stack()
            stack = tb_stack[len(tb_stack) - 2]  # caller only
            if stack.name == "_write":
                stack = tb_stack[len(tb_stack) - 3]
            dprint("CANCEL_ARGS:", self.CANCEL_ARGS)
            if self.CANCEL_ARGS.get("from_user"):
                return False
            if buffer is False:
                timeout = self._serial_device().timeout
                dprint(f"Timeout error ({stack.name:s}(), line {stack.lineno:d}): {timeout!s}")
                dprint("Traceback:\n", "".join(traceback.format_stack()[:-1]))
                self.CANCEL_ARGS.update(
                    {
                        "info_type": "msgbox_critical",
                        "info_msg": __(
                            "A timeout error has occured at {function_name}() in line {line_number}. Please make sure that the cartridge contacts are clean, re-connect the device and try again from the beginning.",
                            function_name=stack.name,
                            line_number=str(stack.lineno),
                        ),
                    },
                )
            elif buffer == 2:
                dprint(f"Error reported ({stack.name:s}(), line {stack.lineno:d}): {buffer!s:s}")
                dprint("Traceback:\n", "".join(traceback.format_stack()[:-1]))
                self.CANCEL_ARGS.update(
                    {
                        "info_type": "msgbox_critical",
                        "info_msg": __(
                            "The device reported an error while running {function_name}() in line {line_number}. Please make sure that the cartridge contacts are clean, re-connect the device and try again from the beginning.",
                            function_name=stack.name,
                            line_number=str(stack.lineno),
                        ),
                    },
                )
            else:
                dprint(f"Communication error ({stack.name:s}(), line {stack.lineno:d}): {buffer!s:s}")
                dprint("Traceback:\n", "".join(traceback.format_stack()[:-1]))
                self.CANCEL_ARGS.update(
                    {
                        "info_type": "msgbox_critical",
                        "info_msg": __(
                            "A communication error has occured at {function_name}() in line {line_number}. Please make sure that the cartridge contacts are clean, re-connect the device and try again from the beginning.",
                            function_name=stack.name,
                            line_number=str(stack.lineno),
                        ),
                    },
                )
            self.ERROR = True
            self.CANCEL = True
            self.WRITE_ERRORS += 1
            if self.WRITE_ERRORS > 3:
                self.SetWriteDelay(enable=True)
            return False
        return buffer

    def _try_write(
        self,
        data: int | bytes | bytearray | memoryview,
        retries: int = 5,
    ) -> int | Literal[False]:
        if retries < 1:
            msg = "retries must be at least 1"
            raise ValueError(msg)
        while retries > 0:
            ack = self._write(data, wait=True)
            if self.CANCEL_ARGS.get("from_user"):
                return False
            if isinstance(ack, int) and ack is not False:
                self.ERROR = False
                self.CANCEL = False
                self.CANCEL_ARGS = {}
                return ack
            retries -= 1
            dprint("Retries left:", retries)

            hp = 20
            temp = 0
            device = self._serial_device()
            while temp not in (1, 2) and hp > 0:
                device.reset_output_buffer()
                device.reset_input_buffer()
                device.write(b"\x00")
                device.flush()
                temp = self._read(1)
                hp -= 1
                dprint("Current response:", temp, ", HP:", hp)
        return False

    def _write(
        self,
        data: int | bytes | bytearray | memoryview,
        wait: bool = False,
    ) -> DeviceWriteResult:
        if isinstance(data, int):
            if not 0 <= data <= 0xFF:
                msg = "integer writes must fit in one byte"
                raise ValueError(msg)
            payload = bytearray([data])
        else:
            payload = bytearray(data)

        if AppContext.DEBUG:
            dprint("_write() thread_id:", threading.get_ident())
            dstr: str = " ".join(format(x, "02X") for x in payload)
            cmd = ""
            if len(payload) < 32:
                try:
                    cmd: str = f"[{list(self.DEVICE_CMD.keys())[list(self.DEVICE_CMD.values()).index(payload[0])]:s}] "
                except Exception:
                    logger.exception("Failed to identify the outgoing device command")
            dprint(f"[{int(len(dstr) / 3) + 1:02X}] {cmd:s}{dstr[:96]:s}")

        device = self._serial_device()
        device.write(payload)
        device.flush()

        # On MacOS it's possible not all bytes are transmitted successfully,
        # even though we're using flush() which is the tcdrain function.
        # Still looking for a better solution than delaying here.
        if self.WRITE_DELAY is True or (
            platform.system() == "Darwin" and (self.FW is None or self.FW.get("pcb_name") == "GBxCart RW")
        ):
            time.sleep(0.0014)

        if wait:
            return self.wait_for_ack()
        return None

    @overload
    def _read(self, count: Literal[1]) -> int | Literal[False]: ...

    @overload
    def _read(self, count: Literal[2, 3, 4, 5, 6, 7, 8]) -> bytearray | Literal[False]: ...

    @overload
    def _read(self, count: int) -> DeviceReadResult: ...

    def _read(self, count: int) -> DeviceReadResult:
        if count < 1:
            msg = "read count must be at least 1"
            raise ValueError(msg)
        if AppContext.DEBUG:
            dprint("_read() thread_id:", threading.get_ident())

        device = self._serial_device()
        if device.in_waiting > 1000:
            dprint(f"Warning: in_waiting={device.in_waiting:d} bytes")
        buffer: bytes = device.read(count)

        if len(buffer) != count:
            hp = 50
            while device.in_waiting != count - len(buffer) and hp > 0:
                time.sleep(0.01)
                hp -= 1
            if hp > 0:
                buffer += device.read(count - len(buffer))

        if len(buffer) != count:
            tb_stack: traceback.StackSummary = traceback.extract_stack()
            stack: traceback.FrameSummary = tb_stack[len(tb_stack) - 2]  # caller only
            if stack.name == "_read":
                stack = tb_stack[len(tb_stack) - 3]
            dprint(
                f"Error: Received only {len(buffer):d} of {count:d} byte(s) ({stack.name:s}(), line {stack.lineno:d})",
            )
            dprint("Timeout value:", device.timeout)
            dprint("Traceback:\n", "".join(traceback.format_stack()[:-1]))
            self.READ_ERRORS += 1
            while device.in_waiting > 0:
                device.reset_input_buffer()
                time.sleep(0.5)
            device.reset_output_buffer()
            return False

        if count == 1:
            return buffer[0]
        return bytearray(buffer)

    def _resolve_fw_variable(self, key: FirmwareVariable) -> tuple[int, int]:
        bit_width, variable_id = self.DEVICE_VAR[key]
        return bit_width // 8, variable_id

    def _get_fw_variable(self, key: FirmwareVariable) -> int | Literal[False]:
        if self._firmware_info().get("fw_ver", 0) < 10:
            return 0
        dprint(f"Getting firmware variable {key:s}")

        size, variable_id = self._resolve_fw_variable(key)

        buffer = bytearray([self.DEVICE_CMD["GET_VARIABLE"], size])
        buffer.extend(struct.pack(">I", variable_id))
        self._write(buffer)
        temp = self._read(4)
        if temp is False or isinstance(temp, int):
            dprint("Communication error:", temp)
            return False
        return struct.unpack(">I", temp)[0]

    def _set_fw_variable(self, key: FirmwareVariable, value: int) -> DeviceWriteResult:
        if not 0 <= value <= 0xFFFFFFFF:
            msg = "firmware variable values must fit in 32 bits"
            raise ValueError(msg)
        dprint(f"Setting firmware variable {key:s} to 0x{value:X}")
        self.FW_VAR[key] = value

        size, variable_id = self._resolve_fw_variable(key)

        buffer = bytearray([self.DEVICE_CMD["SET_VARIABLE"], size])
        buffer.extend(struct.pack(">I", variable_id))
        buffer.extend(struct.pack(">I", value))

        if self._firmware_info().get("fw_ver", 0) >= 12:
            return self._try_write(buffer)
        return self._write(buffer)

    @staticmethod
    def _unpack_cart_read(
        raw: bytearray | Literal[False], minimum_length: int, data_format: str
    ) -> int | Literal[False]:
        if raw is False or len(raw) < minimum_length:
            return False
        return struct.unpack(data_format, raw)[0]

    @overload
    def _cart_read(
        self,
        address: int,
        length: Literal[0] = 0,
        agb_save_flash: bool = False,
    ) -> int | Literal[False]: ...

    @overload
    def _cart_read(
        self,
        address: int,
        length: Literal[1, 2, 4, 8, 10, 16, 1024],
        agb_save_flash: bool = False,
    ) -> bytearray: ...

    @overload
    def _cart_read(
        self,
        address: int,
        length: int,
        agb_save_flash: bool = False,
    ) -> DeviceReadResult: ...

    def _cart_read(
        self,
        address: int,
        length: int = 0,
        agb_save_flash: bool = False,
    ) -> DeviceReadResult:
        if self.MODE == "DMG":
            if length == 0:
                length = 1
                if address < 0xA000:
                    raw: bytearray = self.ReadROM(address, 1, max_length=self.MAX_BUFFER_READ)
                else:
                    raw = self.ReadRAM(address - 0xA000, 1, max_length=self.MAX_BUFFER_READ)
                return self._unpack_cart_read(raw, 1, "B")
            if address < 0xA000:
                return self.ReadROM(address, length, max_length=self.MAX_BUFFER_READ)
            return self.ReadRAM(address - 0xA000, length, max_length=self.MAX_BUFFER_READ)
        if self.MODE == "AGB":
            if length == 0:
                if agb_save_flash:
                    length = 1
                    raw = self.ReadRAM(
                        address,
                        length,
                        command=self.DEVICE_CMD["AGB_CART_READ_SRAM"],
                        max_length=self.MAX_BUFFER_READ,
                    )
                    return self._unpack_cart_read(raw, 1, "B")
                length = 2
                raw = self.ReadROM(address >> 1, length, max_length=self.MAX_BUFFER_READ)
                return self._unpack_cart_read(raw, 2, ">H")
            if agb_save_flash:
                return self.ReadRAM(
                    address,
                    length,
                    command=self.DEVICE_CMD["AGB_CART_READ_SRAM"],
                    max_length=self.MAX_BUFFER_READ,
                )
            return self.ReadROM(address, length, max_length=self.MAX_BUFFER_READ)
        msg = "Cartridge mode must be DMG or AGB before reading"
        raise RuntimeError(msg)

    def _mapper_cart_read(self, address: int, length: int = 0) -> DeviceReadResult:
        """Expose the mapper callback's non-overloaded legacy signature."""
        return self._cart_read(address, length)

    def _cart_write(
        self,
        address: int,
        value: int,
        flashcart: bool = False,
        sram: bool = False,
    ) -> None:
        dprint(f"Writing to cartridge: 0x{address:X} = 0x{value & 0xFF:X} (args: {flashcart!s:s}, {sram!s:s})")
        buffer: bytearray
        if self.MODE == "DMG":
            if flashcart:
                buffer = bytearray([self.DEVICE_CMD["DMG_FLASH_WRITE_BYTE"]])
                buffer.extend(struct.pack(">I", address))
                buffer.extend(struct.pack("B", value & 0xFF))
            elif sram:
                self._set_fw_variable("DMG_WRITE_CS_PULSE", 1)
                self._set_fw_variable("ADDRESS", address)
                self._set_fw_variable("TRANSFER_SIZE", 1)
                self._write(self.DEVICE_CMD["DMG_CART_WRITE_SRAM"])
                self._write(value, wait=True)
                return
            else:
                buffer = bytearray([self.DEVICE_CMD["DMG_CART_WRITE"]])
                buffer.extend(struct.pack(">I", address))
                buffer.extend(struct.pack("B", value & 0xFF))
        elif self.MODE == "AGB":
            if sram:
                self._set_fw_variable("TRANSFER_SIZE", 1)
                self._set_fw_variable("ADDRESS", address)
                self._write(self.DEVICE_CMD["AGB_CART_WRITE_SRAM"])
                self._write(value, wait=True)
                return
            if flashcart:
                buffer = bytearray([self.DEVICE_CMD["AGB_FLASH_WRITE_SHORT"]])
            else:
                buffer = bytearray([self.DEVICE_CMD["AGB_CART_WRITE"]])

            buffer.extend(struct.pack(">I", address >> 1))
            buffer.extend(struct.pack(">H", value & 0xFFFF))
        else:
            msg = "Cartridge mode must be DMG or AGB before writing"
            raise RuntimeError(msg)

        if self._firmware_info().get("fw_ver", 0) >= 12:
            self._try_write(buffer)
        else:
            self._write(buffer)

        if self.MODE == "DMG" and sram:
            self._set_fw_variable("DMG_WRITE_CS_PULSE", 0)

    def _cart_write_flash(
        self,
        commands: Sequence[Sequence[int]],
        flashcart: bool = False,
    ) -> bool | None:
        firmware_version = self._firmware_info().get("fw_ver", 0)
        if firmware_version < 6 and not (self.MODE == "AGB" and not flashcart):
            for command in commands:
                if self._cart_write(command[0], command[1], flashcart=flashcart) is False:
                    print(__("An error has occured while writing to the cartridge."))
            return None

        num = len(commands)
        buffer = bytearray([self.DEVICE_CMD["CART_WRITE_FLASH_CMD"]])
        if firmware_version >= 6:
            buffer.extend(struct.pack("B", 1 if flashcart else 0))
        buffer.extend(struct.pack("B", num))
        for i in range(num):
            dprint(f"Writing to cartridge: 0x{commands[i][0]:X} = 0x{commands[i][1]:X} ({i + 1:d} of {num:d})")
            if self.MODE == "AGB" and flashcart:
                buffer.extend(struct.pack(">I", commands[i][0] >> 1))
            else:
                buffer.extend(struct.pack(">I", commands[i][0]))

            if firmware_version < 6:
                buffer.extend(struct.pack("B", commands[i][1]))
            else:
                buffer.extend(struct.pack(">H", commands[i][1]))

        self._write(buffer)
        ret = self._read(1)
        if ret != 0x01:
            if ret is False:
                msg = __("Error: No response while trying to communicate with the device.")
                time.sleep(0.5)
                self._serial_device().reset_input_buffer()
            else:
                msg = __(
                    "Error: Bad response “{response}” while trying to communicate with the device.",
                    response=str(ret),
                )
            print(ANSI.RED + msg + ANSI.RESET)
            return False
        return True

    def _clk_toggle(self, num: int = 1) -> DeviceWriteResult | bool:
        if num < 0:
            msg = "toggle count cannot be negative"
            raise ValueError(msg)
        if self._firmware_info().get("fw_ver", 0) >= 12:
            buffer = bytearray()
            buffer.extend(struct.pack("B", self.DEVICE_CMD["CLK_TOGGLE"]))
            buffer.extend(struct.pack(">I", num))
            return self._write(buffer, wait=True)
        if not self.CanPowerCycleCart():
            return False
        for _ in range(num):
            self._write(0xA9)  # CLK_HIGH
            self._write(0xAA)  # CLK_LOW
        return True

    def _set_we_pin_wr(self) -> None:
        if self.MODE == "DMG":
            self._set_fw_variable("FLASH_WE_PIN", 0x01)  # FLASH_WE_PIN_WR

    def _set_we_pin_audio(self) -> None:
        if self.MODE == "DMG":
            self._set_fw_variable("FLASH_WE_PIN", 0x02)  # FLASH_WE_PIN_AUDIO

    def CartPowerCycleOrAskReconnect(self) -> bool | None:
        if self.CanPowerCycleCart():
            self.CartPowerCycle()
            return None

        if self.FW["fw_ver"] < 12:
            self.CANCEL_ARGS.update(
                {
                    "info_type": "msgbox_critical",
                    "info_msg": __("This flashcart profile requires at least firmware version L12."),
                },
            )
            self.CANCEL = True
            self.USER_ANSWER = None
            return False

        mode = self.GetMode()
        if mode is None:
            self.CANCEL_ARGS.update(
                {
                    "info_type": "msgbox_critical",
                    "info_msg": __("A cartridge mode must be selected before reconnecting the device."),
                },
            )
            self.CANCEL = True
            return False
        var_state: bytearray = self.GetVarState()

        title: str = __("Power cycle required")
        msg: str = __(
            "To continue, please re-connect the USB cable of your {device_name} on port {device_port} now.",
            device_name=self.GetName(),
            device_port=self.PORT,
        )
        while True:
            self.USER_ANSWER = None
            self.SetProgress(
                {
                    "action": "USER_ACTION",
                    "user_action": "REINSERT_CART",
                    "msg": msg,
                    "title": title,
                },
            )
            while self.USER_ANSWER is None:
                dprint("Waiting for the user to confirm the re-connecting of the device.")
                time.sleep(1)

            if self.USER_ANSWER is False:
                self.CANCEL_ARGS.update({"from_user": True})
                self.CANCEL = True
                self.USER_ANSWER = None
                return False

            if self.CheckActive():
                continue
            self.USER_ANSWER = None

            dev = None
            try:
                dev = serial.Serial(self.PORT, self.BAUDRATE, timeout=0.1)
                self.DEVICE = dev
                self.LoadFirmwareVersion()
                self.SetMode(mode)
                self.SetVarState(var_state)
                time.sleep(0.5)
                break

            except SerialException as e:
                del dev
                dprint("An error occured while re-connecting to the device:\n", e)
                continue

    def CartPowerCycle(self) -> None:
        if self.CanPowerCycleCart():
            dprint("Power cycling cartridge")
            self.CartPowerOff()
            self.CartPowerOn()

    def CartPowerOff(self) -> None:
        dprint("Turning off the cartridge power")
        if self.CanPowerCycleCart():
            if self.FW["fw_ver"] >= 12 or "OFW_CART_PWR_OFF" not in self.DEVICE_CMD:
                self._write(self.DEVICE_CMD["CART_PWR_OFF"], wait=self.FW["fw_ver"] >= 12)
            else:
                self._write(self.DEVICE_CMD["OFW_CART_PWR_OFF"])
        else:
            self._write(self.DEVICE_CMD["SET_ADDR_AS_INPUTS"], wait=self.FW["fw_ver"] >= 12)

    def CartPowerOn(self) -> bool:
        if self.CanPowerCycleCart():
            if self.FW["fw_ver"] >= 12:
                self._write(self.DEVICE_CMD["QUERY_CART_PWR"])
                if self._read(1) == 0:
                    dprint("Turning on the cartridge power")
                    if self.MODE == "DMG":
                        self._write(
                            self.DEVICE_CMD["SET_MODE_DMG"],
                            wait=self.FW["fw_ver"] >= 12,
                        )
                    elif self.MODE == "AGB":
                        self._write(
                            self.DEVICE_CMD["SET_MODE_AGB"],
                            wait=self.FW["fw_ver"] >= 12,
                        )

                    if (
                        self.FW["pcb_name"] == "GBxCart RW"
                    ):  # Workaround for GBxCart RW, it sometimes glitches after cart power on?
                        self._write(self.DEVICE_CMD["CART_PWR_ON"])
                        time.sleep(0.2)
                        device = self._serial_device()
                        hp = 10
                        while hp > 0:
                            if device.in_waiting == 0:
                                dprint("Waiting for ACK...")
                                hp -= 1
                                time.sleep(0.1)
                                continue
                            temp = device.read(device.in_waiting)
                            if len(temp) >= 1:
                                temp = temp[len(temp) - 1]
                            if temp == 1:
                                break
                            device.timeout = 0.1
                            dprint("Unexpected ACK value:", temp)
                            self._write(self.DEVICE_CMD["QUERY_CART_PWR"])
                            time.sleep(0.05)
                            hp -= 1

                        if hp == 0:
                            device.close()
                            self.DEVICE = None
                            self.ERROR = True
                            msg = "Couldn't power on the cartridge."
                            raise BrokenPipeError(msg)
                    else:
                        self._write(self.DEVICE_CMD["CART_PWR_ON"], wait=True)

                    self._serial_device().timeout = self.DEVICE_TIMEOUT

                    self._write(self.DEVICE_CMD["QUERY_CART_PWR"])
                    if self._read(1) != 1:
                        dprint("Warning: No response from firmware on QUERY_CART_PWR.")

                    if self.MODE == "DMG":
                        dprint("Resetting Memory Bank Controller")
                        self._write(
                            self.DEVICE_CMD["DMG_MBC_RESET"],
                            wait=True,
                        )  # Sachen (and Xploder GB?) may need this
                    elif self.MODE == "AGB":
                        dprint("Executing AGB Bootup Sequence")
                        self._write(
                            self.DEVICE_CMD["AGB_BOOTUP_SEQUENCE"],
                            wait=self.FW["fw_ver"] >= 12,
                        )

                    if self.FW["pcb_name"] == "GBxCart RW":
                        self._cart_write(0, 0xFF)  # workaround for strange bootlegs

            elif "OFW_QUERY_CART_PWR" in self.DEVICE_CMD and "OFW_CART_PWR_ON" in self.DEVICE_CMD:
                self._write(self.DEVICE_CMD["OFW_QUERY_CART_PWR"])
                if self._read(1) == 0:
                    dprint("Turning on the cartridge power.")
                    self._write(self.DEVICE_CMD["OFW_CART_PWR_ON"])
                    time.sleep(1)
                    self._serial_device().reset_input_buffer()  # bug workaround
            else:
                self._write(self.DEVICE_CMD["QUERY_CART_PWR"])
                if self._read(1) == 0:
                    dprint("Turning on the cartridge power.")
                    self._write(self.DEVICE_CMD["CART_PWR_ON"], wait=False)

        elif self.MODE == "AGB":
            dprint("Executing AGB Bootup Sequence")
            self._write(self.DEVICE_CMD["AGB_BOOTUP_SEQUENCE"], wait=self.FW["fw_ver"] >= 12)

        return True

    def GetVarState(self) -> bytearray:
        self._write(self.DEVICE_CMD["GET_VAR_STATE"])
        time.sleep(0.2)
        device: serial.Serial = self._serial_device()
        var_state = bytearray(device.read(device.in_waiting))
        dprint(f"Got the state of variables ({len(var_state):d} bytes)")
        return var_state

    def SetVarState(self, var_state: bytes | bytearray | memoryview) -> DeviceWriteResult:
        dprint(f"Sending the state of variables ({len(var_state):d} bytes)")
        self._write(self.DEVICE_CMD["SET_VAR_STATE"])
        time.sleep(0.2)
        device: serial.Serial = self._serial_device()
        device.write(var_state)
        device.flush()
        if self.FW["fw_ver"] >= 15:
            return self.wait_for_ack()
        return None

    def GetMode(self) -> DeviceMode | None:
        if time.time() < self.LAST_CHECK_ACTIVE + 1:
            return self.MODE
        if self.CheckActive() is False:
            return None
        if self.MODE is None:
            return None
        if self.FW["fw_ver"] < 12:
            return self.MODE
        mode: int | Literal[False] = self._get_fw_variable("CART_MODE")
        if mode is False:
            print(
                ANSI.RED
                + __("Error: The firmware no longer responds correctly. Please re-connect the device.")
                + ANSI.RESET,
            )
            self._serial_device().close()
            self.DEVICE = None
            self.ERROR = True
            self.CANCEL = True
            return self.MODE
        if mode == 0:
            return None
        modes: Sequence[str] = self.GetSupprtedModes()
        if mode > len(modes):
            print(ANSI.RED + __("Error: Invalid mode {mode}", mode=str(mode - 1)) + ANSI.RESET)
            return self.MODE
        selected_mode: str = modes[mode - 1]
        if selected_mode not in ("DMG", "AGB"):
            msg: str = f"Invalid cartridge mode reported by firmware: {selected_mode!r}"
            raise ConnectionError(msg)
        self.MODE = selected_mode
        return self.MODE

    def _require_cartridge_mode(self, operation: str) -> DeviceMode:
        mode = self.MODE
        if mode is None:
            msg = f"Cartridge mode must be selected before {operation}"
            raise RuntimeError(msg)
        return mode

    def SetMode(self, mode: DeviceMode, delay: float = 0.1) -> None:
        del delay  # Retained for API compatibility with older callers.
        if mode == "DMG":
            self._write(self.DEVICE_CMD["SET_MODE_DMG"], wait=self.FW["fw_ver"] >= 12)
            self._write(self.DEVICE_CMD["SET_VOLTAGE_5V"], wait=self.FW["fw_ver"] >= 12)
            self._set_fw_variable("DMG_READ_METHOD", self.DMG_READ_METHOD)
            self._set_fw_variable("CART_MODE", 1)
            # if self.FW["fw_ver"] >= 14: self._set_fw_variable("DMG_AUDIO_ENABLED", 0)
            self.MODE = "DMG"
        elif mode == "AGB":
            self._write(self.DEVICE_CMD["SET_MODE_AGB"], wait=self.FW["fw_ver"] >= 12)
            self._write(self.DEVICE_CMD["SET_VOLTAGE_3_3V"], wait=self.FW["fw_ver"] >= 12)
            self._set_fw_variable("AGB_READ_METHOD", self.AGB_READ_METHOD)
            self._set_fw_variable("CART_MODE", 2)
            if self.FW["fw_ver"] >= 12:
                self._set_fw_variable("AGB_IRQ_ENABLED", 0)
            self.MODE = "AGB"
        self._set_fw_variable(key="ADDRESS", value=0)

        if self.CanPowerCycleCart():
            self.CartPowerOn()
        elif mode == "DMG":
            self.SetPin(["PIN_AUDIO"], set_high=True)
        elif mode == "AGB":
            self.SetPin(["PIN_AUDIO"], set_high=False)

    def SetAutoPowerOff(self, value: int) -> None:
        if not self.CanPowerCycleCart():
            return
        value &= 0xFFFFFFFF
        # dprint(f"Setting automatic power off time value to {value}")
        self._set_fw_variable("AUTO_POWEROFF_TIME", value)
        self._set_fw_variable("AUTO_POWEROFF_ENABLED", 1 if value != 0 else 0)
        self.SKIP_POWERCYCLE = value == 0

    def _thread_worker_auto_poweroff_enter(self) -> int | Literal[False] | None:
        if self.FW is None or self.FW["fw_ver"] < 12:
            return None
        if not self.CanPowerCycleCart():
            return None
        try:
            if self._get_fw_variable("AUTO_POWEROFF_ENABLED") != 1:
                return None
            auto_poweroff_time = self._get_fw_variable("AUTO_POWEROFF_TIME")
            self._set_fw_variable("AUTO_POWEROFF_TIME", 5000)
        except Exception as e:
            dprint("AUTO_POWEROFF thread enter failed:", str(e))
            return None
        else:
            return auto_poweroff_time

    def _thread_worker_auto_poweroff_leave(self, auto_poweroff_time: int | Literal[False] | None) -> None:
        if auto_poweroff_time is None:
            return
        if self.FW is None or self.FW["fw_ver"] < 12:
            return
        if not self.CanPowerCycleCart():
            return
        try:
            self._set_fw_variable("AUTO_POWEROFF_TIME", auto_poweroff_time)
        except Exception as e:
            dprint("AUTO_POWEROFF thread leave failed:", str(e))

    def _thread_worker_auto_poweroff_start(self) -> None:
        self.THREAD_AUTO_POWEROFF_TIME = self._thread_worker_auto_poweroff_enter()

    def _thread_worker_auto_poweroff_finish(self) -> None:
        auto_poweroff_time = self.THREAD_AUTO_POWEROFF_TIME
        self.THREAD_AUTO_POWEROFF_TIME = None
        self._thread_worker_auto_poweroff_leave(auto_poweroff_time)

    def GetSupportedCartridgesDMG(self) -> tuple[list[str], list[Any]]:
        return (
            list(self.SUPPORTED_CARTS["DMG"].keys()),
            list(self.SUPPORTED_CARTS["DMG"].values()),
        )

    def GetSupportedCartridgesAGB(self) -> tuple[list[str], list[Any]]:
        return (
            list(self.SUPPORTED_CARTS["AGB"].keys()),
            list(self.SUPPORTED_CARTS["AGB"].values()),
        )

    def SetProgress(
        self,
        args: ProgressUpdate,
        signal: ProgressSignal | ProgressCallback | None = None,
    ) -> None:
        if self.CANCEL and args["action"] not in ("ABORT", "FINISHED", "ERROR"):
            return
        # Swallow hardware-triggered ABORT popups while a voltage fallback retry is queued,
        # so the silent retry isn't preceded by an error dialog. User cancels carry from_user=True
        # and must always pass through.
        if (
            self.VOLTAGE_FALLBACK_PENDING is True
            and args.get("action") == "ABORT"
            and args.get("from_user", False) is not True
        ):
            self.VOLTAGE_FALLBACK_TRIGGERED = True
            return
        if "pos" in args:
            self.POS = args["pos"]
        if args["action"] == "UPDATE_POS":
            self.INFO["transferred"] = args["pos"]
        if signal is None:
            signal = self.SIGNAL

        emit = getattr(signal, "emit", None)
        if callable(emit):
            emit(args)
        elif callable(signal):
            signal(args)

        if args["action"] == "INITIALIZE":
            if self.CanPowerCycleCart():
                self.CartPowerOn()  # Ensure cart is powered
            self.POS = 0
        elif args["action"] == "FINISHED":
            self.POS = 0
            signal = None
            self.SIGNAL = None

    def Debug(self) -> bool:
        if not self.IsConnected():
            return False
        self._write(self.DEVICE_CMD["DEBUG"], wait=True)  # For delay measurement on CLK line
        return True

    def _StoreHeaderData(self, data: dict[str, Any], header: bytearray) -> None:
        dprint("Header data:", data)
        data["raw"] = header
        self.INFO = {**self.INFO, **data}
        if "batteryless_sram" in self.INFO["dump_info"]:
            del self.INFO["dump_info"]["batteryless_sram"]
        self.INFO["dump_info"]["header"] = data
        self.INFO["flash_type"] = 0
        self.INFO["last_action"] = 0

        if self.MODE == "DMG":
            self._write(self.DEVICE_CMD["SET_ADDR_AS_INPUTS"], wait=self.FW["fw_ver"] >= 12)

    def _ReadDmgRtc(self, data: dict[str, Any], mbc: _DmgRtcMapper, check_rtc: bool) -> None:
        if not check_rtc:
            return
        data["has_rtc"] = mbc.HasRTC() is True
        if data["has_rtc"] is not True:
            return
        if mbc.GetName() == "TAMA5":
            mbc.EnableMapper()
        mbc.LatchRTC()
        data["rtc_buffer"] = mbc.ReadRTC()
        if mbc.GetName() == "TAMA5":
            self._set_fw_variable("DMG_READ_CS_PULSE", 0)
        try:
            data["rtc_dict"] = mbc.GetRTCDict()
            data["rtc_string"] = mbc.GetRTCString()
        except Exception:
            data["rtc_string"] = c__("Game Data", "Invalid RTC data")

    def _ReadAgbRtc(self, data: AGBHeader, header: bytearray, check_rtc: bool) -> None:
        data["rtc_dict"] = {}
        data["rtc_string"] = c__("Game Data", "Not available")
        rtc_header_is_valid = header[0xC5] == 0 and header[0xC7] == 0 and header[0xC9] == 0
        if not check_rtc or data["logo_correct"] is not True or not rtc_header_is_valid:
            data["has_rtc"] = False
            data["no_rtc_reason"] = None
            return

        agb_gpio = AGB_GPIO(
            args={"rtc": True},
            cart_write_fncptr=self._cart_write,
            cart_read_fncptr=self._mapper_cart_read,
            cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
            clk_toggle_fncptr=self._clk_toggle,
        )
        if self.FW["fw_ver"] >= 12:
            self._write(self.DEVICE_CMD["AGB_READ_GPIO_RTC"])
            rtc_data: bytearray | Literal[False] = self._read(8)
            data["has_rtc"] = rtc_data is not False and agb_gpio.HasRTC(rtc_data) is True
            if data["has_rtc"] is True and rtc_data is not False:
                data["rtc_buffer"] = rtc_data[1:8]
        else:
            data["has_rtc"] = agb_gpio.HasRTC() is True
            if data["has_rtc"] is True:
                data["rtc_buffer"] = agb_gpio.ReadRTC()
        try:
            data["rtc_dict"] = agb_gpio.GetRTCDict(has_rtc=data["has_rtc"])
            data["rtc_string"] = agb_gpio.GetRTCString(has_rtc=data["has_rtc"])
        except Exception as error:
            dprint("RTC exception:", str(error.args[0]))
            data["has_rtc"] = False

    def _ReadDmgSpecialData(
        self,
        data: dict[str, Any],
        header: bytearray,
        mbc: _DmgRtcMapper,
    ) -> None:
        if mbc.GetName() == "G-MMC1":
            try:
                temp = bytearray([0] * 0x100000)
                temp[0:0x180] = header
                mbc.SelectBankROM(7)
                if data["game_title"] in ("NP M-MENU MENU", "GBMEM-MENU MMSA"):
                    gbmem_menudata = self.ReadROM(0x4000, 0x1000)
                    temp[0x1C000 : 0x1C000 + 0x1000] = gbmem_menudata
                elif data["game_title"] == "DMG MULTI MENU ":
                    gbmem_menudata = self.ReadROM(0x4000, 0x4000)
                    temp[0x1C000 : 0x1C000 + 0x4000] = gbmem_menudata
                elif data["game_title"] == "GBMEM-MENU 256M":
                    gbmem_menudata = self.ReadROM(0x4000, 0x1000)
                    temp[0x10010 : 0x10010 + 0x1000] = gbmem_menudata
                mbc.SelectBankROM(0)
                hidden_sector = mbc.ReadHiddenSector()
                if isinstance(hidden_sector, (bytes, bytearray, memoryview)):
                    gbmem_parsed = (GBMemoryMap()).ParseMapData(
                        buffer_map=hidden_sector,
                        buffer_rom=temp,
                    )
                    if gbmem_parsed:
                        data["gbmem_parsed"] = gbmem_parsed
            except Exception:
                print(traceback.format_exc())
                print(
                    ANSI.RED
                    + __(
                        "An error occured while trying to read the hidden sector data of the {gb_memory_cartridge}.",
                        gb_memory_cartridge="NP GB-Memory Cartridge",
                    )
                    + ANSI.RESET,
                )

        elif mbc.GetName() == "MAC-GBD":
            dprint("Reading Game Boy Camera calibration data...")
            mbc.EnableRAM(enable=True)
            mbc.SelectBankRAM(2)
            temp = self.ReadRAM(address=0xFF2, length=0xE)
            if temp and temp != bytearray([temp[0]] * len(temp)):
                data["gbcamera_calibration1"] = temp
                dprint(
                    "Game Boy Camera calibration data 1:",
                    "".join(format(x, "02X") for x in temp),
                )
            mbc.SelectBankRAM(8)
            temp = self.ReadRAM(address=0x1FF2, length=0xE)
            if temp and temp != bytearray([temp[0]] * len(temp)):
                data["gbcamera_calibration2"] = temp
                dprint(
                    "Game Boy Camera calibration data 2:",
                    "".join(format(x, "02X") for x in temp),
                )
            mbc.EnableRAM(enable=False)

    def ReadHeader(self, checkRtc: bool = True) -> dict[str, Any] | Literal[False]:
        if not self.IsConnected():
            msg = "Couldn't access the the device."
            raise ConnectionError(msg)
        data = {}
        self.SIGNAL = None

        if self.CanPowerCycleCart():
            self.ResetLEDs()
            self.CartPowerOn()

        if self.FW["fw_ver"] >= 8:
            self._write(self.DEVICE_CMD["DISABLE_PULLUPS"], wait=True)
        if self.MODE == "DMG":
            self._write(self.DEVICE_CMD["SET_VOLTAGE_5V"], wait=self.FW["fw_ver"] >= 12)
            self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
            self._set_fw_variable("DMG_READ_CS_PULSE", 0)
            self._set_fw_variable("DMG_WRITE_CS_PULSE", 0)
        elif self.MODE == "AGB":
            self._write(self.DEVICE_CMD["SET_VOLTAGE_3_3V"], wait=self.FW["fw_ver"] >= 12)
            if not self.CanPowerCycleCart():
                dprint("Executing AGB Bootup Sequence")
                self._write(self.DEVICE_CMD["AGB_BOOTUP_SEQUENCE"], wait=self.FW["fw_ver"] >= 12)
        else:
            print(ANSI.RED + __("Error: No mode was set.") + ANSI.RESET)
            return False

        header = self.ReadROM(0, 0x180)

        if ".dev" in AppInfo.VERSION_PEP440 or AppContext.DEBUG:
            with (Path(AppContext.CONFIG_PATH) / "debug_header.bin").open("wb") as f:
                f.write(header)

        # Parse ROM header
        if self.MODE == "DMG":
            data = RomFileDMG(header).GetHeader()
            if (
                "game_title" in data
                and data["game_title"] == "TETRIS"
                and hashlib.sha1(header).digest()
                != bytearray(
                    [
                        0x1D,
                        0x69,
                        0x2A,
                        0x4B,
                        0x31,
                        0x7A,
                        0xA5,
                        0xE9,
                        0x67,
                        0xEE,
                        0xC2,
                        0x2F,
                        0xCC,
                        0x32,
                        0x43,
                        0x8C,
                        0xCB,
                        0xC5,
                        0x78,
                        0x0B,
                    ],
                )
            ):  # Sachen
                header = self.ReadROM(0, 0x280)
                data = RomFileDMG(header).GetHeader()
            if "logo_correct" in data and data["logo_correct"] is False and b"Future Console Design" not in header:
                if self.FW["pcb_name"] == "GBxCart RW":
                    self._cart_write(0, 0xFF)  # workaround for strange bootlegs
                    time.sleep(0.1)
                header = self.ReadROM(0, 0x280)
                data = RomFileDMG(header).GetHeader()
            if (
                "mapper_raw" in data and data["mapper_raw"] == 0x203
            ) or b"Future Console Design" in header:  # Xploder GB version number
                self._cart_write(0x0006, 0)
                header[0:0x10] = self.ReadROM(0x4000, 0x10)
                header[0xD0:0xE0] = self.ReadROM(0x40D0, 0x10)
                data = RomFileDMG(header).GetHeader()
            if data == {}:
                return False

            data["has_rtc"] = False
            data["rtc_dict"] = {}
            data["rtc_string"] = c__("Game Data", "Not available")
            if data["logo_correct"] is True:
                _mbc = DMG_Mapper().GetInstance(
                    args={"mbc": data["mapper_raw"]},
                    cart_write_fncptr=self._cart_write,
                    cart_read_fncptr=self._mapper_cart_read,
                    cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
                    clk_toggle_fncptr=self._clk_toggle,
                )
                self._ReadDmgRtc(data, _mbc, checkRtc)

                self._ReadDmgSpecialData(data, header, _mbc)

        elif self.MODE == "AGB":
            # Unlock DACS carts on older firmware
            if (not self.CanPowerCycleCart() or self.FW["fw_ver"] == 1) and header[0x04 : 0x04 + 0x9C] in (
                bytearray([0x00] * 0x9C),
                bytearray([0xFF] * 0x9C),
            ):
                self.ReadROM(0x1FFFFE0, 20)
                header: bytearray = self.ReadROM(0, 0x180)

            data: AGBHeader = RomFileAGB(header).GetHeader()
            if data["logo_correct"] is False:  # workaround for strange bootlegs
                self._cart_write(0, 0xFF)
                header = self.ReadROM(0, 0x180)
                data = RomFileAGB(header).GetHeader()

            if data["vast_fame"]:  # Unlock full address space for Vast Fame protected carts
                self._cart_write(0xFFF8, 0x99, sram=True)
                self._cart_write(0xFFF9, 0x02, sram=True)
                self._cart_write(0xFFFA, 0x05, sram=True)
                self._cart_write(0xFFFB, 0x02, sram=True)
                self._cart_write(0xFFFC, 0x03, sram=True)

                self._cart_write(0xFFFD, 0x00, sram=True)

                self._cart_write(0xFFF8, 0x99, sram=True)
                self._cart_write(0xFFF9, 0x03, sram=True)
                self._cart_write(0xFFFA, 0x62, sram=True)
                self._cart_write(0xFFFB, 0x02, sram=True)
                self._cart_write(0xFFFC, 0x56, sram=True)

            if data["empty"] or data["empty_nocart"]:
                data["rom_size"] = 0x2000000
            else:
                # Check where the ROM data repeats (for unlicensed carts)
                size_check: bytearray = header[0xA0 : 0xA0 + 16]
                currAddr = 0x10000
                while currAddr < 0x2000000:
                    buffer: bytearray = self.ReadROM(currAddr + 0xA0, 64)[:16]
                    if buffer == size_check:
                        break
                    currAddr *= 2

                if data["vast_fame"]:
                    if currAddr < 0x2000000:
                        currAddr >>= 1  # Vast Fame carts are blank for the 1st mirror, so divide by 2
                    else:  # Some Vast Fame carts have no mirror, check using VF pattern behaviour instead
                        currAddr = 0x200000
                        while currAddr < 0x2000000:
                            sentinel: bytearray = self.ReadROM(currAddr + 0x2AAAA, 2)
                            if int.from_bytes(sentinel, byteorder="big") == 0xAAAA:
                                break
                            currAddr *= 2

                data["rom_size"] = currAddr

            if self.ReadROM(0x1FFE000, 0x0C) == b"AGBFLASHDACS":
                data["dacs_8m"] = True
                if self.FW["pcb_name"] == "GBFlash" and self.FW["pcb_ver"] < 13:
                    print(
                        ANSI.YELLOW
                        + __(
                            "Note: This cartridge may not be fully compatible with your GBFlash hardware revision. Upgrade to v1.3 or newer for better compatibility.",
                        )
                        + ANSI.RESET,
                    )

            self._ReadAgbRtc(data, header, checkRtc)

            if data["ereader"] is True:
                bank = 0
                dprint(f"Switching to FLASH bank {bank:d}")
                cmds = [[0x5555, 0xAA], [0x2AAA, 0x55], [0x5555, 0xB0], [0, bank]]
                self._cart_write_flash(cmds)
                temp = self.ReadRAM(
                    address=0xD000,
                    length=0x2000,
                    command=self.DEVICE_CMD["AGB_CART_READ_SRAM"],
                )
                if temp[0:0x14] == b"Card-E Reader 2001\0\0":
                    data["ereader_calibration"] = temp
                else:
                    data["ereader_calibration"] = None
                    del data["ereader_calibration"]

        self._StoreHeaderData(data, header)
        return data

    def _DetectCartridge(self, args: dict[str, Any]) -> bool:  # Wrapper for thread call
        self.SetProgress({"action": "INITIALIZE", "abortable": False, "method": "DETECT_CART"})
        signal: ProgressSignal | ProgressCallback | None = self.SIGNAL
        self.SIGNAL = None

        auto_poweroff_enabled: int | Literal[False] = self._get_fw_variable("AUTO_POWEROFF_ENABLED")
        if auto_poweroff_enabled:
            self._set_fw_variable("AUTO_POWEROFF_ENABLED", 0)

        auto_poweroff_time: int | Literal[False] | None = self._thread_worker_auto_poweroff_enter()
        try:
            ret = self._DetectCartridge_Worker(
                mbc=None,
                limitVoltage=args["limitVoltage"],
                checkSaveType=args["checkSaveType"],
                signal=signal,
            )
            self.INFO["detect_cart"] = ret
        finally:
            self._thread_worker_auto_poweroff_leave(auto_poweroff_time)
            if auto_poweroff_enabled:
                self._set_fw_variable("AUTO_POWEROFF_ENABLED", auto_poweroff_enabled)

        self.INFO["detect_cart"] = ret
        self.INFO["last_action"] = self.ACTIONS["DETECT_CART"]
        self.INFO["action"] = None
        self.SIGNAL = signal
        self.SetProgress({"action": "FINISHED"})
        return True

    def _PrepareCartridgeSaveDetection(
        self,
        cart_type: dict[str, Any],
        check_save_type: bool,
        info: dict[str, Any],
        signal: ProgressSignal | ProgressCallback | None,
    ) -> tuple[bool, int | None, int | None]:
        save_size = None
        save_type = None
        if ("command_set" in cart_type and cart_type["command_set"] in ("DMG-MBC5-32M-FLASH", "GBAMP")) or (
            "dmg-mbc5-32m-flash" in cart_type
        ):
            check_save_type = False
        elif self.MODE == "AGB" and "flash_bank_select_type" in cart_type and cart_type["flash_bank_select_type"] == 1:
            save_size = 65536
            save_type = 7
            check_save_type = False
        elif self.MODE == "AGB" and "flash_bank_select_type" in cart_type and cart_type["flash_bank_select_type"] > 1:
            check_save_type = False
        elif self.MODE == "DMG" and "mbc" in cart_type and cart_type["mbc"] == 0x105:  # G-MMC1
            if signal is not None:
                self.SetProgress(
                    {
                        "action": "UPDATE_INFO",
                        "text": __("Detecting {gb_memory}...", gb_memory="GB-Memory"),
                    },
                    signal=signal,
                )
            header = self.ReadROM(0, 0x180)
            data = RomFileDMG(header).GetHeader()
            mbc = DMG_Mapper().GetInstance(
                args={"mbc": cart_type["mbc"]},
                cart_write_fncptr=self._cart_write,
                cart_read_fncptr=self._mapper_cart_read,
                cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
                clk_toggle_fncptr=self._clk_toggle,
            )
            temp = bytearray([0] * 0x100000)
            temp[0:0x180] = header
            if data["game_title"] in ("NP M-MENU MENU", "GBMEM-MENU MMSA"):
                mbc.SelectBankROM(7)
                gbmem_menudata: bytearray = self.ReadROM(0x4000, 0x1000)
                temp[0x1C000 : 0x1C000 + 0x1000] = gbmem_menudata
            elif data["game_title"] == "DMG MULTI MENU ":
                mbc.SelectBankROM(7)
                gbmem_menudata = self.ReadROM(0x4000, 0x4000)
                temp[0x1C000 : 0x1C000 + 0x4000] = gbmem_menudata
            elif data["game_title"] == "GBMEM-MENU 256M":
                mbc.SelectBankROM(4)
                gbmem_menudata = self.ReadROM(0x4010, 0x1000)
                temp[0x10010 : 0x10010 + 0x1000] = gbmem_menudata
            mbc.SelectBankROM(0)
            hidden_sector: bytearray | bool = mbc.ReadHiddenSector()
            if isinstance(hidden_sector, (bytes, bytearray, memoryview)):
                info["gbmem"] = hidden_sector
                gbmem_parsed = (GBMemoryMap()).ParseMapData(buffer_map=hidden_sector, buffer_rom=temp)
                if gbmem_parsed:
                    info["gbmem_parsed"] = gbmem_parsed
        return check_save_type, save_size, save_type

    def _DetectDmgSaveType(self, save_size: int, mbc: int | None) -> tuple[int, int | None]:
        try:
            save_type: int | None = DmgSaveTypes(size=save_size).GetMbc()
        except KeyError, TypeError, ValueError:
            save_size = 0
            save_type = 0

        if save_size <= 0x20:
            return save_size, save_type
        if mbc == 0x22:  # MBC7
            if save_size == 256:
                save_type = 0x101
            elif save_size == 512:
                save_type = 0x102
            return save_size, save_type
        if save_size <= 0x10000:
            return save_size, save_type

        check = True
        for i in range(0x8000, 0x10000, 0x40):
            if self.INFO["data"][i : i + 3] != bytearray([self.INFO["data"][i]] * 3):
                check = False
                break

        if self.INFO["data"][0:0x8000] == self.INFO["data"][0x8000:0x10000]:  # MBCX
            check = True

        if check:
            return 32768, 0x03

        check = True
        for i in range(0x1A000, 0x20000, 0x40):
            if self.INFO["data"][i : i + 3] != bytearray([self.INFO["data"][i]] * 3):
                check = False
                break
        if check:
            return 65536, 0x05
        return save_size, save_type

    def _DetectAgbFlashSaveType(self, save_size: int) -> tuple[int | None, int, str | None]:
        save_type = None
        save_chip = None
        flash_id_result = self.ReadFlashSaveID()
        if flash_id_result is False:
            return save_type, save_size, save_chip
        flash_save_id, _ = flash_id_result
        try:
            if flash_save_id == 0:
                return save_type, save_size, save_chip
            if not AgbSaveTypes().IsValidFlashChipIndex(flash_save_id):
                return 0, 0, __("Unknown FLASH save chip") + f" (0x{flash_save_id:04X})"
            save_size = AgbSaveTypes().GetFlashChipSize(flash_save_id)
            save_chip = AgbSaveTypes().GetFlashChipName(flash_save_id)
            if flash_save_id in (0xBF4B, 0xBF5B, 0xFFFF, 0xBF6D):  # Bootlegs
                if self.INFO["data"][0:0x20000] == bytearray([0xFF] * 0x20000):
                    save_type = 5
                elif self.INFO["data"][0:0x10000] == self.INFO["data"][0x10000:0x20000]:
                    save_type = 4
                else:
                    save_type = 5
            elif save_size == 131072:
                save_type = 5
            elif save_size == 65536:
                save_type = 4
        except Exception as error:
            print(
                ANSI.RED
                + __(
                    "Error: Couldn't check save type with FLASH save ID {flash_save_id}",
                    flash_save_id=f"0x{flash_save_id:04X}",
                )
                + ANSI.RESET
                + "\n"
                + str(error),
            )
            return 0, 0, __("Unknown FLASH save chip") + f" (0x{flash_save_id:04X})"
        return save_type, save_size, save_chip

    def _DetectAgbNonFlashSaveType(
        self,
        save_type: int | None,
        save_size: int,
        mbc: int | None,
        info: dict[str, Any],
    ) -> tuple[int | None, int]:
        if save_type is not None:
            return save_type, save_size

        check_batteryless_sram = True
        if info["dacs_8m"] is True:
            save_size = 1048576
            save_type = 6
        elif save_size > 256:  # SRAM
            if save_size == 131072:
                save_type = 8
            elif save_size == 65536:
                save_type = 7
            elif save_size == 32768:
                save_type = 3
            elif save_size in AgbSaveTypes():
                save_type = AgbSaveTypes().GetIndexFromSize(save_size)
            else:
                save_type = None
                save_size = 0
        else:
            dprint("Testing EEPROM")
            self._BackupRestoreRAM(
                args={
                    "mode": 2,
                    "path": None,
                    "mbc": mbc,
                    "save_type": 1,
                    "rtc": False,
                    "detect": True,
                },
            )
            eeprom_4k = self.INFO["data"]
            self._BackupRestoreRAM(
                args={
                    "mode": 2,
                    "path": None,
                    "mbc": mbc,
                    "save_type": 2,
                    "rtc": False,
                    "detect": True,
                },
            )
            save_size = self._find_repeating_size(self.INFO["data"], len(self.INFO["data"]))
            eeprom_64k = self.INFO["data"]
            if eeprom_64k in (
                bytearray([0xFF] * len(eeprom_64k)),
                bytearray([0] * len(eeprom_64k)),
            ):
                save_size = 0
                save_type = 0
            elif eeprom_4k == eeprom_64k[: len(eeprom_4k)]:
                save_type = 2
                save_size = 8192
            else:
                save_type = 1
                save_size = 512
            check_batteryless_sram = False

        if check_batteryless_sram:
            batteryless: dict[str, int] | Literal[False] = self.CheckBatterylessSRAM()
            if batteryless is not False:
                save_type = 9
                info["batteryless_sram"] = batteryless
                self.INFO["dump_info"]["batteryless_sram"] = batteryless
        return save_type, save_size

    def _DetectCartridge_Worker(
        self,
        mbc: int | None = None,
        limitVoltage: bool = False,
        checkSaveType: bool = True,
        signal: ProgressSignal | ProgressCallback | None = None,
    ) -> CartridgeDetectionResult:
        self.SIGNAL = None
        self.CANCEL = False
        self.ERROR = False
        cart_type_id = 0
        save_type = None
        save_chip: str | None = None
        sram_unstable = None
        save_size: int | None = None
        _apot = 0

        # Header
        if signal is not None:
            self.SetProgress({"action": "UPDATE_INFO", "text": __("Detecting ROM...")}, signal=signal)
        info = self.ReadHeader(checkRtc=True)
        if info is False:
            return False
        if self.MODE == "DMG" and mbc is None:
            mbc = int(info["mapper_raw"])
            if mbc > 0x200:
                checkSaveType = False

        # Disable Auto Power Off
        _apoe = False
        if self.FW["fw_ver"] >= 12 and self.SKIP_POWERCYCLE is False and self.CanPowerCycleCart():
            self.CartPowerCycle()
            _apoe = self._get_fw_variable("AUTO_POWEROFF_ENABLED") == 1
            if _apoe is True:
                _apot = self._get_fw_variable("AUTO_POWEROFF_TIME")
                self._set_fw_variable("AUTO_POWEROFF_TIME", 5000)

        # Detect Flash Cart
        if signal is not None:
            self.SetProgress(
                {"action": "UPDATE_INFO", "text": __("Detecting Flash...")},
                signal=signal,
            )
        ret = self.DetectFlash(limitVoltage=limitVoltage)
        if ret is False:
            return False
        (cart_types, cart_type_id, flash_id, cfi_s, cfi, detected_size) = ret
        mode: Literal["DMG", "AGB"] | None = self.MODE
        if mode is None:
            return False
        supported_carts = list(self.SUPPORTED_CARTS[mode].values())
        cart_type = supported_carts[cart_type_id]

        # Skip DMG save type detection
        if self.MODE == "DMG" and cart_type_id == 0:
            checkSaveType = False

        # Preparations
        checkSaveType, save_size, save_type = self._PrepareCartridgeSaveDetection(
            cart_type,
            checkSaveType,
            info,
            signal,
        )

        # Save Type and Size
        if checkSaveType:
            if signal is not None:
                self.SetProgress(
                    {"action": "UPDATE_INFO", "text": __("Detecting save type...")},
                    signal=signal,
                )
            if self.MODE == "DMG":
                save_size = 131072
                save_type = 0x04
                if mbc == 0x20:  # MBC6
                    save_size = 1081344
                    save_type = 0x104
                    return (
                        info,
                        save_size,
                        save_type,
                        save_chip,
                        sram_unstable,
                        cart_types,
                        cart_type_id,
                        cfi_s,
                        cfi,
                        flash_id,
                        detected_size,
                    )
                if mbc == 0x22:  # MBC7
                    save_type = 0x102
                    save_size = 512
                elif mbc == 0xFD:  # TAMA5
                    save_size = 32
                    save_type = 0x103
                    return (
                        info,
                        save_size,
                        save_type,
                        save_chip,
                        sram_unstable,
                        cart_types,
                        cart_type_id,
                        cfi_s,
                        cfi,
                        flash_id,
                        detected_size,
                    )
                elif mbc == 0x105:  # G-MMC1
                    save_size = 0x20000
                    save_type = 0x04
                    return (
                        info,
                        save_size,
                        save_type,
                        save_chip,
                        sram_unstable,
                        cart_types,
                        cart_type_id,
                        cfi_s,
                        cfi,
                        flash_id,
                        detected_size,
                    )
                args = {
                    "mode": 2,
                    "path": None,
                    "mbc": mbc,
                    "save_type": save_type,
                    "rtc": False,
                    "detect": True,
                }
            elif self.MODE == "AGB":
                args = {
                    "mode": 2,
                    "path": None,
                    "mbc": mbc,
                    "save_type": 8,
                    "rtc": False,
                    "detect": True,
                }
            else:
                return None

            ret = self._BackupRestoreRAM(args=args)

            if ret is not False and "data" in self.INFO:
                save_size = self._find_repeating_size(self.INFO["data"], len(self.INFO["data"]))
            else:
                save_size = 0

            if self.MODE == "DMG":
                save_size, save_type = self._DetectDmgSaveType(save_size, mbc)

            elif self.MODE == "AGB":
                if info["3d_memory"] is True:
                    save_type = None
                    save_size = 0
                else:
                    # Check for FLASH
                    save_type, save_size, save_chip = self._DetectAgbFlashSaveType(save_size)
                    save_type, save_size = self._DetectAgbNonFlashSaveType(save_type, save_size, mbc, info)

        self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
        self.INFO["last_action"] = 0
        self.INFO["action"] = None

        if self.CanPowerCycleCart() and _apoe is True and self.SKIP_POWERCYCLE is False:
            self._set_fw_variable("AUTO_POWEROFF_TIME", _apot)

        return (
            info,
            save_size,
            save_type,
            save_chip,
            sram_unstable,
            cart_types,
            cart_type_id,
            cfi_s,
            cfi,
            flash_id,
            detected_size,
        )

    def CheckBatterylessSRAM(self) -> dict[str, int] | Literal[False]:
        bl_size: int | None = None
        bl_offset: int | None = None
        if self.MODE == "AGB":
            buffer: bytearray = self.ReadROM(0, 0x180)
            header: AGBHeader = RomFileAGB(buffer).GetHeader()
            if header["game_code"] in ("GMBC", "PNES"):
                bl_size = 0x10000
                state_id1 = 0x57A731D7
                state_id2 = 0x57A731D8
                state_id3 = 0x57A731D9
                if struct.unpack("<I", self.ReadROM(0x400000 - 0x40000, 4))[0] in (
                    state_id1,
                    state_id2,
                    state_id3,
                ):
                    bl_offset = 0x400000 - 0x40000
                elif struct.unpack("<I", self.ReadROM(0x800000 - 0x40000, 4))[0] in (
                    state_id1,
                    state_id2,
                    state_id3,
                ):
                    bl_offset = 0x800000 - 0x40000
                elif struct.unpack("<I", self.ReadROM(0x1000000 - 0x40000, 4))[0] in (
                    state_id1,
                    state_id2,
                    state_id3,
                ):
                    bl_offset = 0x1000000 - 0x40000
                elif struct.unpack("<I", self.ReadROM(0x2000000 - 0x40000, 4))[0] in (
                    state_id1,
                    state_id2,
                    state_id3,
                ):
                    bl_offset = 0x2000000 - 0x40000
                dprint("Detected Goomba Color or PocketNES Batteryless ROM by Lesserkuma")
            else:
                boot_vector: int = (struct.unpack("<I", buffer[0:3] + bytearray([0]))[0] + 2) << 2
                batteryless_loader: bytearray = self.ReadROM(boot_vector, 0x2000)
                try:
                    if bytearray(b"<3 from Maniac") in batteryless_loader:
                        payload_size: int = struct.unpack(
                            "<H",
                            batteryless_loader[batteryless_loader.index(bytearray(b"<3 from Maniac")) :][0x0E:0x10],
                        )[0]
                        if payload_size == 0:
                            payload_size = 0x414
                        bl_offset = batteryless_loader.index(bytearray(b"<3 from Maniac")) + boot_vector + 0x10
                        payload: bytearray = self.ReadROM(bl_offset - payload_size, payload_size)
                        bl_size = struct.unpack("<I", payload[0x8:0xC])[0]
                        dprint(
                            "Detected Batteryless SRAM ROM made with the Automatic batteryless saving patcher for GBA by metroid-maniac",
                        )
                        if bl_size not in (0x2000, 0x8000, 0x10000, 0x20000):
                            dprint(
                                f"{ANSI.YELLOW:s}Warning: Unsupported Batteryless SRAM size value detected: 0x{bl_size:X}{ANSI.RESET:s}",
                            )
                    elif bytearray([0x02, 0x13, 0xA0, 0xE3]) in batteryless_loader:
                        if (
                            bytearray([0x09, 0x04, 0xA0, 0xE3]) in batteryless_loader
                            or bytearray([0x09, 0x14, 0xA0, 0xE3]) in batteryless_loader
                            or bytearray([0x09, 0x24, 0xA0, 0xE3]) in batteryless_loader
                            or bytearray([0x09, 0x34, 0xA0, 0xE3]) in batteryless_loader
                        ):
                            bl_size = 0x20000
                        else:
                            bl_size = 0x10000
                        base_addr: int = batteryless_loader.index(bytearray([0x02, 0x13, 0xA0, 0xE3]))
                        addr_value: int = batteryless_loader[base_addr - 8]
                        addr_rotate_right: int = batteryless_loader[base_addr - 7] * 2
                        addr_shift: int = batteryless_loader[base_addr - 3] << 1
                        address: int = (addr_value >> addr_rotate_right) | (
                            addr_value << (32 - addr_rotate_right)
                        ) & 0xFFFFFFFF
                        address = address << addr_shift
                        if address < 32 * 1024 * 1024 and address > 0x1000:
                            bl_offset = address
                            dprint("Detected Chinese bootleg Batteryless SRAM ROM")
                        else:
                            dprint(
                                "Bad offset with Chinese bootleg Batteryless SRAM ROM:",
                                hex(address),
                            )
                except Exception as e:
                    dprint(
                        "An error occured while trying to determine the Batteryless SRAM method.\n",
                        e,
                    )
                    bl_offset = None
                    bl_size = None
        if bl_offset is None or bl_size is None:
            dprint("No Batteryless SRAM routine detected")
            return False
        dprint(f"bl_offset=0x{bl_offset:X}, bl_size=0x{bl_size:X}")
        return {"bl_offset": bl_offset, "bl_size": bl_size}

    def ReadFlashSaveID(self) -> tuple[int, str] | Literal[False]:
        # Check if actually SRAM/FRAM
        dprint("Checking Flash ID of Save Memory")
        test_data: bytearray = self._cart_read(0, 0x10, agb_save_flash=True)
        if test_data is False or len(test_data) < 5:
            return False
        test1: int = test_data[4]
        self._cart_write_flash([[0x0004, test1 ^ 0xFF]])
        test_data = self._cart_read(0, 0x10, agb_save_flash=True)
        if test_data is False or len(test_data) < 5:
            return False
        test2: int = test_data[4]
        self._cart_write_flash([[0x0004, test1]])
        if test1 != test2:
            dprint(
                f"Seems to be SRAM/FRAM, not FLASH (value 0x{test1:02X} was changed to 0x{test2:02X} by SRAM access at address 0x4)",
                test1,
                test2,
            )
            return False

        # Read Chip ID
        temp5555: int | Literal[False] = self._cart_read(0x5555, agb_save_flash=True)
        temp2AAA: int | Literal[False] = self._cart_read(0x2AAA, agb_save_flash=True)
        temp0000: int | Literal[False] = self._cart_read(0x0000, agb_save_flash=True)
        if temp5555 is False or temp2AAA is False or temp0000 is False:
            return False

        cmds: list[list[int]] = [[0x5555, 0xAA], [0x2AAA, 0x55], [0x5555, 0x90]]
        self._cart_write_flash(cmds)
        agb_chip_raw: bytearray = self._cart_read(0, 2, agb_save_flash=True)
        if agb_chip_raw is False or len(agb_chip_raw) < 2:
            return False
        agb_flash_chip: int = struct.unpack(">H", agb_chip_raw)[0]
        cmds = [[0x5555, 0xAA], [0x2AAA, 0x55], [0x5555, 0xF0]]
        self._cart_write_flash(cmds)
        time.sleep(0.01)
        self._cart_write_flash([[0, 0xF0]])
        time.sleep(0.01)

        if not AgbSaveTypes().IsValidFlashChipIndex(agb_flash_chip):
            # Restore SRAM values
            cmds = [[0x5555, temp5555], [0x2AAA, temp2AAA], [0x0000, temp0000]]
            self._cart_write_flash(cmds)
            agb_flash_chip_name: str = __("Unknown flash chip ID") + f" (0x{agb_flash_chip:04X})"
        else:
            agb_flash_chip_name = AgbSaveTypes().GetFlashChipName(agb_flash_chip)

        dprint(agb_flash_chip_name)
        return (agb_flash_chip, agb_flash_chip_name)

    def ReadROM(
        self,
        address: int,
        length: int,
        skip_init: bool = False,
        max_length: int = 64,
    ) -> bytearray:
        max_length = min(max_length, self.MAX_BUFFER_READ)
        num: int = math.ceil(length / max_length)
        dprint(f"Reading 0x{length:X} bytes from cartridge ROM at 0x{address:X} in {num:d} iteration(s)")
        length = min(length, max_length)

        buffer = bytearray()
        if not skip_init:
            self._set_fw_variable("TRANSFER_SIZE", length)
            if self.MODE == "DMG":
                self._set_fw_variable("ADDRESS", address)
                self._set_fw_variable("DMG_ACCESS_MODE", 1)  # MODE_ROM_READ
            elif self.MODE == "AGB":
                self._set_fw_variable("ADDRESS", address >> 1)

        if self.MODE == "DMG":
            command = "DMG_CART_READ"
        elif self.MODE == "AGB":
            command = "AGB_CART_READ"
        else:
            raise NotImplementedError

        for n in range(num):
            self._write(self.DEVICE_CMD[command])
            temp: int | bytearray | Literal[False] = self._read(length)
            if temp is not False and isinstance(temp, int):
                temp = bytearray([temp])
            if temp is False or len(temp) != length:
                dprint(
                    f"Error while trying to read 0x{length:X} bytes from cartridge ROM at 0x{address:X} in iteration {n:d} of {num:d} (response: {temp!s:s})",
                )
                return bytearray()
            buffer += temp
            if (
                self.INFO["action"]
                in (
                    self.ACTIONS["ROM_READ"],
                    self.ACTIONS["SAVE_READ"],
                    self.ACTIONS["ROM_WRITE_VERIFY"],
                )
                and not self.NO_PROG_UPDATE
            ):
                self.SetProgress({"action": "READ", "bytes_added": len(temp)})

        return buffer

    def ReadROM_3DMemory(self, address: int, length: int, max_length: int = 64) -> bytearray:
        max_length = min(max_length, self.MAX_BUFFER_READ)
        buffer_size = 0x1000
        num = math.ceil(length / max_length)
        dprint(f"Reading 0x{length:X} bytes from cartridge ROM in {num:d} iteration(s)")
        length = min(length, max_length)

        self._set_fw_variable("TRANSFER_SIZE", length)
        self._set_fw_variable("BUFFER_SIZE", buffer_size)
        self._set_fw_variable("ADDRESS", address >> 1)

        buffer = bytearray()
        error = False
        for _ in range(int(num / (buffer_size / length))):  # 32
            for _ in range(int(buffer_size / length)):  # 0x1000/0x200=8
                self._write(self.DEVICE_CMD["AGB_CART_READ_3D_MEMORY"])
                temp = self._read(length)
                if isinstance(temp, int):
                    temp = bytearray([temp])
                if temp is False or len(temp) != length:
                    error = True
                    temp = bytearray(length)
                buffer += temp
                if self.INFO["action"] == self.ACTIONS["ROM_READ"] and not self.NO_PROG_UPDATE:
                    self.SetProgress({"action": "READ", "bytes_added": length})
            self._write(0)

        if error:
            return bytearray()
        return buffer

    def ReadROM_GBAMP(self, address: int, length: int, max_length: int = 64) -> bytearray:
        max_length = min(max_length, self.MAX_BUFFER_READ)
        dprint("GBAMP ROM Read Mode", hex(address), hex(length), hex(max_length))
        addr: int = (address >> 13 << 16) | (address & 0x1FFF)
        dprint(f"0x{address:07X} → 0x{addr:07X}")
        return self.ReadROM(address=addr, length=length, max_length=max_length)

    def ReadRAM(
        self,
        address: int,
        length: int,
        command: int | None = None,
        max_length: int = 64,
    ) -> bytearray:
        max_length = min(max_length, self.MAX_BUFFER_READ)
        num: int = math.ceil(length / max_length)
        dprint(f"Reading 0x{length:X} bytes from cartridge RAM in {num:d} iteration(s)")
        length = min(length, max_length)
        buffer = bytearray()
        self._set_fw_variable("TRANSFER_SIZE", length)

        if self.MODE == "DMG":
            self._set_fw_variable("ADDRESS", 0xA000 + address)
            self._set_fw_variable("DMG_ACCESS_MODE", 3)  # MODE_RAM_READ
            self._set_fw_variable("DMG_READ_CS_PULSE", 1)
            if command is None:
                command = self.DEVICE_CMD["DMG_CART_READ"]
        elif self.MODE == "AGB":
            self._set_fw_variable("ADDRESS", address)
            if command is None:
                command = self.DEVICE_CMD["AGB_CART_READ_SRAM"]
        else:
            msg = "Cartridge mode must be selected before reading RAM"
            raise RuntimeError(msg)

        for _ in range(num):
            self._write(command)
            temp: int | bytearray | Literal[False] = self._read(length)
            if isinstance(temp, int):
                temp = bytearray([temp])
            if temp is False or len(temp) != length:
                return bytearray()
            buffer += temp
            if not self.NO_PROG_UPDATE:
                self.SetProgress({"action": "READ", "bytes_added": len(temp)})

        if self.MODE == "DMG":
            self._set_fw_variable("DMG_READ_CS_PULSE", 0)

        return buffer

    def ReadRAM_MBC7(self, address: int, length: int) -> bytearray:
        max_length = 32
        num = math.ceil(length / max_length)
        dprint(f"Reading 0x{length:X} bytes from cartridge EEPROM in {num:d} iteration(s)")
        length = min(length, max_length)
        buffer = bytearray()
        self._set_fw_variable("TRANSFER_SIZE", length)
        self._set_fw_variable("ADDRESS", address)
        for _ in range(num):
            self._write(self.DEVICE_CMD["DMG_MBC7_READ_EEPROM"])
            temp: int | bytearray | Literal[False] = self._read(length)
            if temp is False:
                self.ERROR = True
                break
            if isinstance(temp, int):
                temp = bytearray([temp])
            buffer += temp
            if not self.NO_PROG_UPDATE:
                self.SetProgress({"action": "READ", "bytes_added": len(temp)})

        return buffer

    def ReadRAM_TAMA5(self) -> bytearray:
        dprint("Reading 0x20 bytes from cartridge RAM")
        buffer = bytearray()
        npu: bool = self.NO_PROG_UPDATE
        self.NO_PROG_UPDATE = True
        self._set_fw_variable("DMG_READ_CS_PULSE", 1)
        self._set_fw_variable("DMG_WRITE_CS_PULSE", 1)

        # Read save state
        for i in range(0x20):
            self._cart_write(0xA001, 0x06, sram=True)  # register select and address (high)
            self._cart_write(0xA000, i >> 4 | 0x01 << 1, sram=True)  # bit 0 = higher ram address, rest = command
            self._cart_write(0xA001, 0x07, sram=True)  # address (low)
            self._cart_write(0xA000, i & 0x0F, sram=True)  # bits 0-3 = lower ram address
            self._cart_write(0xA001, 0x0D, sram=True)  # data out (high)
            value1, value2 = None, None
            while value1 is None or value1 != value2:
                value2: int | Literal[False] | None = value1
                value1: int | Literal[False] | None = self._cart_read(0xA000)
            data_h: int | Literal[False] = value1
            self._cart_write(0xA001, 0x0C, sram=True)  # data out (low)

            value1, value2 = None, None
            while value1 is None or value1 != value2:
                value2 = value1
                value1 = self._cart_read(0xA000)
            data_l: int | Literal[False] = value1

            data: int = ((data_h & 0xF) << 4) | (data_l & 0xF)
            buffer.append(data)
            self.SetProgress({"action": "UPDATE_POS", "abortable": False, "pos": i + 1})

        self._set_fw_variable("DMG_READ_CS_PULSE", 0)

        self.NO_PROG_UPDATE: bool = npu
        return buffer

    def WriteRAM(
        self,
        address: int,
        buffer: bytes | bytearray | memoryview,
        command: int | None = None,
        max_length: int = 256,
    ) -> bool:
        max_length = min(max_length, self.MAX_BUFFER_WRITE)
        length: int = len(buffer)
        num: int = math.ceil(length / max_length)
        dprint(f"Writing 0x{length:X} bytes to cartridge RAM in {num:d} iteration(s)")
        length = min(length, max_length)

        self._set_fw_variable("TRANSFER_SIZE", length)
        if self.MODE == "DMG":
            self._set_fw_variable("ADDRESS", 0xA000 + address)
            self._set_fw_variable("DMG_ACCESS_MODE", 4)  # MODE_RAM_WRITE
            self._set_fw_variable("DMG_WRITE_CS_PULSE", 1)
            if command is None:
                command = self.DEVICE_CMD["DMG_CART_WRITE_SRAM"]
        elif self.MODE == "AGB":
            self._set_fw_variable("ADDRESS", address)
            if command is None:
                command = self.DEVICE_CMD["AGB_CART_WRITE_SRAM"]
        else:
            msg = "Cartridge mode must be selected before writing RAM"
            raise RuntimeError(msg)

        for i in range(num):
            self._write(command)
            self._write(buffer[i * length : i * length + length], wait=True)
            # self._read(1)
            if self.INFO["action"] == self.ACTIONS["SAVE_WRITE"] and not self.NO_PROG_UPDATE:
                self.SetProgress({"action": "WRITE", "bytes_added": length})

        if self.MODE == "DMG":
            self._set_fw_variable("ADDRESS", 0)
            self._set_fw_variable("DMG_WRITE_CS_PULSE", 0)

        return True

    def WriteFlash_MBC6(
        self,
        address: int,
        buffer: bytes | bytearray | memoryview,
        mapper: MBC6FlashMapper,
    ) -> bool | None:
        length: int = len(buffer)
        max_length = 128
        num: int = math.ceil(length / max_length)
        length = min(length, max_length)
        dprint(f"Write 0x{length:X} bytes to cartridge FLASH in {num:d} iteration(s)")

        skip_write = False
        for i in range(num):
            self._set_fw_variable("TRANSFER_SIZE", length)
            self._set_fw_variable("ADDRESS", address)
            dprint(f"Now in iteration {i:d}")

            if buffer[i * length : i * length + length] == bytearray([0xFF] * length):
                skip_write = True
                address += length
                continue

            cmds = [
                [0x2000, 0x01],
                [0x3000, 0x02],
                [0x7555, 0xAA],
                [0x4AAA, 0x55],
                [0x7555, 0xA0],
            ]
            self._cart_write_flash(cmds)
            mapper.SelectBankFlash(mapper.GetROMBank())
            self._write(self.DEVICE_CMD["DMG_MBC6_MMSA_WRITE_FLASH"])
            self._write(buffer[i * length : i * length + length])
            ret = self._read(1)
            if ret not in (0x01, 0x03):
                dprint(
                    f"Flash write error (response = {ret!s:s}) in iteration {i:d} while trying to write 0x{length:X} bytes",
                )
                if self.CANCEL_ARGS.get("from_user"):
                    return False
                self.CANCEL_ARGS.update(
                    {
                        "info_type": "msgbox_critical",
                        "info_msg": __(
                            "Flash write error (response = {response}) in iteration {iteration} while trying to write {length} bytes",
                            response=str(ret),
                            iteration=i,
                            length=length,
                        ),
                    },
                )
                self.CANCEL = True
                self.ERROR = True
                return False
            self._cart_write(address + length - 1, 0x00)
            hp = 100
            while hp > 0:
                sr: int | Literal[False] = self._cart_read(address + length - 1)
                if sr == 0x80:
                    break
                time.sleep(0.001)
                hp -= 1

            if hp == 0:
                dprint(
                    f"Flash write timeout (response = {ret!s:s}) in iteration {i:d} while trying to write 0x{length:X} bytes",
                )
                if self.CANCEL_ARGS.get("from_user"):
                    return False
                self.CANCEL_ARGS.update(
                    {
                        "info_type": "msgbox_critical",
                        "info_msg": __(
                            "Flash write timeout (response = {response}) in iteration {iteration} while trying to write {length} bytes",
                            response=str(ret),
                            iteration=i,
                            length=length,
                        ),
                    },
                )
                self.CANCEL = True
                self.ERROR = True
                return False

            address += length
            if self.INFO["action"] == self.ACTIONS["SAVE_WRITE"] and not self.NO_PROG_UPDATE:
                self.SetProgress({"action": "WRITE", "bytes_added": length})
        self._cart_write(address - 1, 0xF0)
        self.SKIPPING: bool = skip_write
        return None

    def WriteEEPROM_MBC7(self, address: int, buffer: bytes | bytearray | memoryview) -> None:
        length = len(buffer)
        max_length = 32
        num = math.ceil(length / max_length)
        length = min(length, max_length)
        dprint(f"Write 0x{length:X} bytes to cartridge EEPROM in {num:d} iteration(s)")
        self._set_fw_variable("TRANSFER_SIZE", length)
        self._set_fw_variable("ADDRESS", address)
        for i in range(num):
            self._write(self.DEVICE_CMD["DMG_MBC7_WRITE_EEPROM"])
            self._write(buffer[i * length : i * length + length])
            response = self._read(1)
            dprint("Response:", response)
            if self.INFO["action"] == self.ACTIONS["SAVE_WRITE"] and not self.NO_PROG_UPDATE:
                self.SetProgress({"action": "WRITE", "bytes_added": length})

    def WriteRAM_TAMA5(self, buffer: bytes | bytearray | memoryview) -> None:
        npu: bool = self.NO_PROG_UPDATE
        self.NO_PROG_UPDATE = True

        for i in range(0x20):
            self._cart_write(0xA001, 0x05, sram=True)  # data in (high)
            self._cart_write(0xA000, buffer[i] >> 4, sram=True)
            self._cart_write(0xA001, 0x04, sram=True)  # data in (low)
            self._cart_write(0xA000, buffer[i] & 0xF, sram=True)
            self._cart_write(0xA001, 0x06, sram=True)  # register select and address (high)
            self._cart_write(0xA000, i >> 4 | 0x00 << 1, sram=True)  # bit 0 = higher ram address, rest = command
            self._cart_write(0xA001, 0x07, sram=True)  # address (low)
            self._cart_write(0xA000, i & 0x0F, sram=True)  # bits 0-3 = lower ram address
            value1, value2 = None, None
            while value1 is None or value1 != value2:
                value2 = value1
                value1 = self._cart_read(0xA000)
            self.SetProgress({"action": "UPDATE_POS", "abortable": False, "pos": i + 1})

        self.NO_PROG_UPDATE = npu

    def WriteROM(  # noqa: PLR0913, PLR0917
        self,
        address: int,
        buffer: bytes | bytearray | memoryview,
        flash_buffer_size: int | Literal[False] = False,
        skip_init: bool = False,
        rumble_stop: bool = False,
        max_length: int = 0x400,
    ) -> bool | None:
        max_length = min(max_length, self.MAX_BUFFER_WRITE)
        length: int = len(buffer)
        num: int = math.ceil(length / max_length)
        dprint(f"Writing 0x{length:X} bytes to Flash ROM in {num:d} iteration(s)")
        if length == 0:
            dprint("Length is zero?")
            return False
        length = min(length, max_length)

        skip_write = False
        ret = 0
        num_of_chunks: int = math.ceil(flash_buffer_size / length)
        pos = 0

        if not skip_init:
            self._set_fw_variable("TRANSFER_SIZE", length)
            if flash_buffer_size is not False:
                self._set_fw_variable("BUFFER_SIZE", flash_buffer_size)

        for i in range(num):
            data = bytearray(buffer[i * length : i * length + length])
            if (num_of_chunks == 1 or flash_buffer_size == 0) and (data == bytearray([0xFF] * len(data))):
                skip_init = False
                skip_write = True
            elif skip_write:
                skip_write = False

            if not skip_write:
                if not skip_init:
                    if self.MODE == "DMG":
                        self._set_fw_variable("ADDRESS", address)
                    elif self.MODE == "AGB":
                        self._set_fw_variable("ADDRESS", address >> 1)

                    skip_init = True

                if ret != 0x03:
                    self._write(self.DEVICE_CMD["FLASH_PROGRAM"])
                ret = self._write(data, wait=True)

                if ret not in (0x01, 0x03):
                    dprint(
                        f"Flash error at 0x{address:X} in iteration {i:d} of {num:d} while trying to write a total of 0x{len(buffer):X} bytes (response = {ret!s:s})",
                    )
                    self.ERROR_ARGS = {"iteration": i}
                    self.SKIPPING = False
                    return False
                pos += len(data)

                if rumble_stop and flash_buffer_size > 0 and pos % flash_buffer_size == 0:
                    self._cart_write(address=0xC4, value=0x00, flashcart=True)
                    self._cart_write(address=0xC6, value=0x00, flashcart=True)
                    rumble_stop = False

            address += length
            if ((pos % length) * 10 == 0) and (
                self.INFO["action"] in (self.ACTIONS["ROM_WRITE"], self.ACTIONS["SAVE_WRITE"])
                and not self.NO_PROG_UPDATE
            ):
                self.SetProgress({"action": "WRITE", "bytes_added": length, "skipping": skip_write})

        self.SKIPPING = skip_write
        return None

    def WriteROM_GBMEMORY(
        self,
        address: int,
        buffer: bytes | bytearray | memoryview,
        bank: int,
    ) -> bool | None:
        length: int = len(buffer)
        max_length = 128
        num: int = math.ceil(length / max_length)
        length = min(length, max_length)
        dprint(f"Writing 0x{length:X} bytes to GB-Memory Flash ROM in {num:d} iteration(s)")

        skip_write = False
        for i in range(num):
            self._set_fw_variable("TRANSFER_SIZE", length)
            self._set_fw_variable("ADDRESS", address)
            dprint(f"Now in iteration {i:d}")

            if buffer[i * length : i * length + length] == bytearray([0xFF] * length):
                skip_write = True
                address += length
                continue

            # Enable flash chip access
            self._cart_write_flash(
                [
                    [0x120, 0x09],
                    [0x121, 0xAA],
                    [0x122, 0x55],
                    [0x13F, 0xA5],
                ],
            )
            # Re-Enable writes to MBC registers
            self._cart_write_flash(
                [
                    [0x120, 0x11],
                    [0x13F, 0xA5],
                ],
            )
            # Bank 1 for commands
            self._cart_write_flash(
                [
                    [0x2100, 0x01],
                ],
            )

            # Write setup
            self._cart_write_flash(
                [
                    [0x120, 0x0F],
                    [0x125, 0x55],
                    [0x126, 0x55],
                    [0x127, 0xAA],
                    [0x13F, 0xA5],
                ],
            )
            self._cart_write_flash(
                [
                    [0x120, 0x0F],
                    [0x125, 0x2A],
                    [0x126, 0xAA],
                    [0x127, 0x55],
                    [0x13F, 0xA5],
                ],
            )
            self._cart_write_flash(
                [
                    [0x120, 0x0F],
                    [0x125, 0x55],
                    [0x126, 0x55],
                    [0x127, 0xA0],
                    [0x13F, 0xA5],
                ],
            )

            # Set bank back
            self._cart_write_flash(
                [
                    [0x2100, bank],
                ],
            )

            # Disable writes to MBC registers
            self._cart_write_flash(
                [
                    [0x120, 0x10],
                    [0x13F, 0xA5],
                ],
            )

            # Undo Wakeup
            self._cart_write_flash(
                [
                    [0x120, 0x08],
                    [0x13F, 0xA5],
                ],
            )

            self._write(self.DEVICE_CMD["DMG_MBC6_MMSA_WRITE_FLASH"])
            self._write(buffer[i * length : i * length + length])
            ret = self._read(1)
            if ret not in (0x01, 0x03):
                return False

            self._cart_write(address + length - 1, 0xFF)
            while True:
                sr = self._cart_read(address + length - 1)
                if sr & 0x80 == 0x80:
                    break
                time.sleep(0.001)

            address += length
            if self.INFO["action"] == self.ACTIONS["ROM_WRITE"] and not self.NO_PROG_UPDATE:
                self.SetProgress({"action": "WRITE", "bytes_added": length})

        self._cart_write(address - 1, 0xF0)
        self.SKIPPING = skip_write
        return None

    def WriteROM_DMG_MBC5_32M_FLASH(
        self,
        address: int,
        buffer: bytes | bytearray | memoryview,
        bank: int,
    ) -> bool | None:
        del bank
        length: int = len(buffer)
        max_length = 128
        num: int = math.ceil(length / max_length)
        length = min(length, max_length)
        dprint(f"Writing 0x{length:X} bytes to Flash ROM in {num:d} iteration(s)")

        skip_write = False
        for i in range(num):
            self._set_fw_variable("TRANSFER_SIZE", length)
            self._set_fw_variable("ADDRESS", address)
            dprint(f"Now in iteration {i:d}")

            if buffer[i * length : i * length + length] == bytearray([0xFF] * length):
                skip_write = True
                address += length
                continue

            self._cart_write(0x4000, 0xFF)
            self._cart_write_flash(
                [
                    [address, 0xE0],
                    [address, length - 1],
                    [address, 0x00],
                ],
            )
            self._write(self.DEVICE_CMD["DMG_MBC6_MMSA_WRITE_FLASH"])
            self._write(buffer[i * length : i * length + length])
            ret = self._read(1)
            if ret not in (0x01, 0x03):
                self.CANCEL_ARGS.update(
                    {
                        "info_type": "msgbox_critical",
                        "info_msg": __(
                            "Flash write error (response = {response}) in iteration {iteration} while trying to write {length} bytes",
                            response=str(ret),
                            iteration=i,
                            length=length,
                        ),
                    },
                )
                self.CANCEL = True
                self.ERROR = True
                return False

            self._cart_write_flash(
                [
                    [address, 0x0C],
                    [address, length - 1],
                    [address, 0x00],
                ],
            )
            lives = 100
            sr = 0
            while lives > 0:
                self._cart_write(0x4000, 0x70)
                sr = self._cart_read(address + length - 1)
                dprint(f"sr=0x{sr:X}")
                if sr & 0x80 == 0x80:
                    break
                lives -= 1
            self._cart_write(0x4000, 0xFF)

            if lives == 0:
                self.CANCEL_ARGS.update(
                    {
                        "info_type": "msgbox_critical",
                        "info_msg": __(
                            "Flash write error (response = {response}) in iteration {iteration} while trying to write {length} bytes",
                            response=str(sr),
                            iteration=i,
                            length=length,
                        ),
                    },
                )
                self.CANCEL = True
                self.ERROR = True
                return False

            address += length
            if self.INFO["action"] == self.ACTIONS["ROM_WRITE"] and not self.NO_PROG_UPDATE:
                self.SetProgress({"action": "WRITE", "bytes_added": length})

        self._cart_write(address - 1, 0xFF)
        self.SKIPPING = skip_write
        return None

    def WriteROM_DMG_DatelOrbitV2(
        self,
        address: int,
        buffer: bytes | bytearray | memoryview,
        bank: int,
    ) -> bool:
        length: int = len(buffer)
        dprint(f"Writing 0x{length:X} bytes to Datel Orbit V2 cartridge")
        for i in range(length):
            self._cart_write(0x7FE1, 2)
            self._cart_write(0x5555, 0xAA)
            self._cart_write(0x2AAA, 0x55)
            self._cart_write(0x5555, 0xA0)
            self._cart_write(0x7FE1, bank)
            self._cart_write(address + i, buffer[i])
            if self.INFO["action"] == self.ACTIONS["ROM_WRITE"] and not self.NO_PROG_UPDATE:
                self.SetProgress({"action": "WRITE", "bytes_added": 1})
        return True

    def WriteROM_DMG_EEPROM(
        self,
        address: int,
        buffer: bytes | bytearray | memoryview,
        bank: int,
        eeprom_buffer_size: int = 0x80,
    ) -> bool | None:
        length: int = len(buffer)
        max_length: Literal[256, 1024] = 256 if not self.CanPowerCycleCart() or self.BAUDRATE == 1000000 else 1024
        num: int = math.ceil(length / max_length)
        length = min(length, max_length)
        dprint(f"Writing 0x{length:X} bytes to EEPROM in {num:d} iteration(s)")

        for i in range(num):
            self._set_fw_variable("BUFFER_SIZE", eeprom_buffer_size)
            self._set_fw_variable("TRANSFER_SIZE", length)
            self._set_fw_variable("ADDRESS", address)
            dprint(f"Now in iteration {i:d}")

            self._write(self.DEVICE_CMD["DMG_EEPROM_WRITE"])
            self._write(buffer[i * length : i * length + length])
            ret = self._read(1)
            if ret not in (0x01, 0x03):
                self.CANCEL_ARGS.update(
                    {
                        "info_type": "msgbox_critical",
                        "info_msg": __(
                            "EEPROM write error (response = {response}) in iteration {iteration} while trying to write {length} bytes",
                            response=str(ret),
                            iteration=i,
                            length=length,
                        ),
                    },
                )
                self.CANCEL = True
                self.ERROR = True
                return False

            address += length
            if self.INFO["action"] == self.ACTIONS["ROM_WRITE"] and not self.NO_PROG_UPDATE:
                self.SetProgress({"action": "WRITE", "bytes_added": length})

        commands = [
            [0x0006, 0x01],
            [0x5555, 0xAA],
            [0x2AAA, 0x55],
            [0x5555, 0xF0],
            [0x0006, bank],
        ]
        for command in commands:
            self._cart_write(command[0], command[1])
        return None

    def CheckROMStable(self) -> bool:
        if not self.IsConnected():
            msg = "Couldn't access the the device."
            raise ConnectionError(msg)
        if self.CanPowerCycleCart():
            self.CartPowerOn()

        buffer1: bytearray = self.ReadROM(0x80, 0x40)
        time.sleep(0.1)
        buffer2: bytearray = self.ReadROM(0x80, 0x40)
        return buffer1 == buffer2

    def CompareCRC32(  # noqa: PLR0913, PLR0917
        self,
        buffer: bytes | bytearray | memoryview,
        offset: int,
        length: int,
        address: int,
        flashcart: Flashcart | None = None,
        max_length: int = 0x20000,
        reset: bool = False,
        mbc: DMG_Mapper | None = None,
        bank: int = 0,
    ) -> bool | tuple[int, int]:
        left: int = length
        chunk_pos = 0
        verified = False
        while left > 0:
            chunk_len: int = min(left, max_length)
            chunk_from: int = offset + chunk_pos
            chunk_to: int = offset + chunk_pos + chunk_len
            dprint(
                f"Running CRC32 verification, comparing between source 0x{chunk_from:X}~0x{chunk_to:X} and target 0x{address + chunk_pos:X}~0x{address + chunk_pos + chunk_len:X}",
            )
            crc32_expected: int = zlib.crc32(buffer[chunk_from:chunk_to])

            for i in range(2 if (reset is True and flashcart is not None) else 1):  # for retrying with reset
                if self.MODE == "DMG":
                    self._set_fw_variable("ADDRESS", address + chunk_pos)
                elif self.MODE == "AGB":
                    self._set_fw_variable("ADDRESS", (address + chunk_pos) >> 1)
                self._write(self.DEVICE_CMD["CALC_CRC32"])
                self._write(bytearray(struct.pack(">I", chunk_len)))
                temp: bytearray | Literal[False] = self._read(4)
                crc32_calculated: int | Literal[False] = False if temp is False else struct.unpack(">I", temp)[0]
                if crc32_expected != crc32_calculated:
                    if i == 0 and flashcart is not None:
                        flashcart.Reset(full_reset=True)
                        if mbc is not None:
                            mbc.SelectBankROM(bank)
                        verified = (crc32_expected, crc32_calculated)
                        continue
                    return (crc32_expected, crc32_calculated)
                verified = True
                break

            left -= chunk_len
            chunk_pos += chunk_len

        return verified

    def _ReadFlashCFI(
        self,
        supported_carts: list[Any],
        flash_types: list[int],
        flash_id_cmds: list[dict[str, Any]],
        read_cfi_cmds: list[Any],
    ) -> tuple[Any, str]:
        """Read and parse Common Flash Interface data for a detected chip."""
        cfi_buffer: bytearray | None
        cfi_buffer_raw = bytearray()
        try:
            if flash_types and "read_cfi" in supported_carts[flash_types[0]]["commands"]:
                read_cfi_cmd = supported_carts[flash_types[0]]["commands"]["read_cfi"]
                reset_cmd = supported_carts[flash_types[0]]["commands"]["reset"]
            else:
                read_cfi_cmd = [[0, 152]] if not read_cfi_cmds else [read_cfi_cmds[0]]
                reset_cmd = flash_id_cmds[0]["reset"]
            self._cart_write_flash(read_cfi_cmd, flashcart=self.MODE == "AGB")
            raw_buffer = self._cart_read(0, 0x400)
            cfi_buffer = bytearray() if raw_buffer is False else raw_buffer
            cfi_buffer_raw = bytearray(cfi_buffer)
            self._cart_write_flash(reset_cmd, flashcart=self.MODE == "AGB")

            if ".dev" in AppInfo.VERSION_PEP440 or AppContext.DEBUG:
                with (Path(AppContext.CONFIG_PATH) / "debug_cfi.bin").open("wb") as file:
                    file.write(cfi_buffer)

            found = False
            for offset, stride in ((0x20, 2), (0x10, 1)):
                magic = "".join(chr(cfi_buffer[offset + index * stride]) for index in range(3))
                dprint(
                    "CFI magic:",
                    hex(offset),
                    hex(offset + stride),
                    hex(offset + 2 * stride),
                    "=",
                    magic,
                )
                swaps: tuple[tuple[int, int], ...] = ()
                if magic == "QRY":  # D0D1 not swapped
                    found = True
                elif magic == "RQZ":  # D0D1 swapped
                    swaps = ((0, 1),)
                    found = True
                elif magic == "\x92\x91\x9a":  # D0D1+D6D7 swapped
                    swaps = ((0, 1), (6, 7))
                    found = True

                for swap in swaps:
                    for index, value in enumerate(cfi_buffer):
                        cfi_buffer[index] = CFI.swap_bits(value, swap)

                if magic == "\x92\x91\x9a" and (".dev" in AppInfo.VERSION_PEP440 or AppContext.DEBUG):
                    with (Path(AppContext.CONFIG_PATH) / "debug_cfi_d0d1+d6d7.bin").open("wb") as file:
                        file.write(cfi_buffer)
                if found:
                    break
            if not found:
                cfi_buffer = None
        except Exception:
            cfi_buffer = None

        if cfi_buffer is None or len(cfi_buffer) < 0x400:
            return False, ""
        cfi = CFI().Parse(cfi_buffer_raw)
        return cfi, cfi["info"] if isinstance(cfi, dict) else ""

    def _detect_flash_size(
        self,
        supported_carts: list[Any],
        flash_types: list[int],
        cfi: object,
        callbacks: FlashcartCallbacks,
    ) -> tuple[int, int]:
        flash_type_id = 0
        detected_size = 0
        if not flash_types:
            return flash_type_id, detected_size

        flash_type_id = flash_types[0]
        flashcart = Flashcart(config=supported_carts[flash_type_id], fncptr=callbacks)
        if self.MODE == "DMG":
            supported_types = self.GetSupportedCartridgesDMG()
        elif self.MODE == "AGB":
            supported_types = self.GetSupportedCartridgesAGB()
        else:
            raise NotImplementedError

        first_type = supported_types[1][flash_type_id]
        if "flash_size" not in first_type:
            return flash_type_id, detected_size
        size = first_type["flash_size"]
        size_undetected = any(
            "flash_size" in supported_types[1][candidate] and size != supported_types[1][candidate]["flash_size"]
            for candidate in flash_types
        )
        if not size_undetected:
            return flash_type_id, detected_size

        if first_type.get("flash_bank_select_type") == 1:
            flashcart.SelectBankROM(0)
            size_check = self.ReadROM(0, 0x1000) + self.ReadROM(0x1FFF000, 0x1000)
            num_banks = 1
            while num_banks < (flashcart.GetFlashSize() // 0x2000000) + 1:
                dprint(f"Checking bank {num_banks:d}")
                flashcart.SelectBankROM(num_banks)
                buffer = self.ReadROM(0, 0x1000) + self.ReadROM(0x1FFF000, 0x1000)
                if buffer == size_check:
                    break
                num_banks <<= 1
            detected_size = 0x2000000 * num_banks
            for candidate in flash_types:
                if detected_size == supported_types[1][candidate]["flash_size"]:
                    dprint(f"Detected {num_banks:d} flash banks")
                    flash_type_id = candidate
                    break
            flashcart.SelectBankROM(0)
        elif isinstance(cfi, dict) and "device_size" in cfi:
            for candidate in flash_types:
                if (
                    "flash_size" in supported_types[1][candidate]
                    and cfi["device_size"] == supported_types[1][candidate]["flash_size"]
                ):
                    flash_type_id = candidate
                    break
        elif self.MODE == "AGB":
            header = self.ReadROM(0, 0x180)
            size_check = header[0xA0 : 0xA0 + 16]
            current_address = 0x10000
            while current_address < 0x2000000:
                buffer = self.ReadROM(current_address + 0xA0, 64)[:16]
                if buffer == size_check:
                    break
                current_address *= 2
            for candidate in flash_types:
                if (
                    "flash_size" in supported_types[1][candidate]
                    and current_address == supported_types[1][candidate]["flash_size"]
                ):
                    flash_type_id = candidate
                    break
        return flash_type_id, detected_size

    def _MatchDetectedFlashTypes(
        self,
        supported_carts: list[Any],
        flash_id_methods: list[Any],
        flash_id_cmds: list[Any],
        we_pins: list[str],
        flash_types: list[int],
    ) -> None:
        for flash_type_index in range(1, len(supported_carts)):
            cart_type = supported_carts[flash_type_index]
            if not isinstance(cart_type, dict):
                continue
            if "flash_ids" not in cart_type or len(cart_type["flash_ids"]) == 0:
                continue
            if "commands" not in cart_type or len(cart_type["commands"]) == 0:
                continue
            found = False
            if flash_id_methods:
                for we, command_index, flash_id, _, cmd_rfi in flash_id_methods:
                    if cmd_rfi != cart_type["commands"]["read_identifier"]:
                        continue
                    if self.MODE == "DMG" and "write_pin" in cart_type and cart_type["write_pin"] != we_pins[we]:
                        continue
                    fcm_flash_ids = list(map(list, {tuple(sublist) for sublist in cart_type["flash_ids"]}))
                    for fcm_flash_id in fcm_flash_ids:
                        if fcm_flash_id == flash_id[: len(fcm_flash_id)]:
                            if self.MODE == "DMG":
                                dprint(
                                    "“{:s}” matches with Flash ID “{:s}” ({:s}/{:X}/{:X})".format(
                                        cart_type["names"][0],
                                        " ".join(format(x, "02X") for x in fcm_flash_id),
                                        we_pins[we],
                                        flash_id_cmds[command_index]["read_identifier"][0][0],
                                        flash_id_cmds[command_index]["read_identifier"][0][1],
                                    ),
                                )
                            elif self.MODE == "AGB":
                                dprint(
                                    "“{:s}” matches with Flash ID “{:s}” ({:X})/{:X}".format(
                                        cart_type["names"][0],
                                        " ".join(format(x, "02X") for x in fcm_flash_id),
                                        flash_id_cmds[command_index]["read_identifier"][0][0],
                                        flash_id_cmds[command_index]["read_identifier"][0][1],
                                    ),
                                )
                            found = True
                            flash_types.append(flash_type_index)
                            if "reset" in cart_type["commands"]:
                                self._cart_write_flash(cart_type["commands"]["reset"], flashcart=True)
                            break
                    if found:
                        break

    def _RestoreFlashDetectionMode(self, read_method: int) -> None:
        if self.MODE == "DMG":
            self._write(self.DEVICE_CMD["SET_VOLTAGE_5V"], wait=self.FW["fw_ver"] >= 12)
            self._set_fw_variable("FLASH_WE_PIN", 1)  # back to WE=WR
        elif self.MODE == "AGB":
            self.SetAGBReadMethod(read_method)

    def _SearchFlashIdentifiers(self, context: _FlashIdSearchContext) -> str:
        dprint("Trying to find the Flash ID")
        write_enable_options: dict[str, list[int]] = (
            {"DMG": [1, 2], "AGB": [0]} if self.SupportsAudioAsWe() else {"DMG": [1], "AGB": [0]}
        )
        flash_id_text = context.flash_id_text
        for write_enable in write_enable_options[context.mode]:
            if self.MODE == "DMG":
                self._set_fw_variable("FLASH_WE_PIN", write_enable)
            for command_index, command in enumerate(context.flash_id_commands):
                self._cart_write_flash(command["reset"], flashcart=True)
                self._cart_write_flash(command["read_identifier"], flashcart=True)
                result: bytearray = self._cart_read(0, 8)
                self._cart_write_flash(command["reset"], flashcart=True)

                if result is False or context.rom == result:
                    continue
                context.flash_id_methods.append(
                    [
                        write_enable - 1,
                        command_index,
                        list(result),
                        context.cfi_buffer,
                        command["read_identifier"],
                    ],
                )
                if self.MODE == "DMG":
                    flash_id_text += "[{:s}/{:4X}/{:2X}] {:s}\n".format(
                        context.write_enable_pins[write_enable - 1].ljust(5),
                        command["read_identifier"][0][0],
                        command["read_identifier"][0][1],
                        " ".join(format(value, "02X") for value in result),
                    )
                else:
                    flash_id_text += "[{:6X}/{:4X}] {:s}\n".format(
                        command["read_identifier"][0][0],
                        command["read_identifier"][0][1],
                        " ".join(format(value, "02X") for value in result),
                    )
                command["read_cfi"] = [command["read_identifier"][0][0], 0x98]
                context.read_cfi_commands.append(command["read_cfi"])

        dprint(f"Found {len(context.flash_id_methods):d} result(s)")
        self._cart_write_flash([[0, 0xFF]], flashcart=True)
        self._cart_write_flash([[0, 0xF0]], flashcart=True)
        return flash_id_text

    def _PrepareFlashDetectionMode(self, mode: DeviceMode, limit_voltage: bool) -> bytearray:
        if mode == "DMG":
            voltage_command = "SET_VOLTAGE_3_3V" if limit_voltage else "SET_VOLTAGE_5V"
            self._write(self.DEVICE_CMD[voltage_command], wait=self.FW["fw_ver"] >= 12)
            self._write(self.DEVICE_CMD["SET_MODE_DMG"], wait=self.FW["fw_ver"] >= 12)
        else:
            self.SetAGBReadMethod(0)
            self._write(self.DEVICE_CMD["SET_MODE_AGB"], wait=self.FW["fw_ver"] >= 12)
        rom = self._cart_read(0, 8)
        return bytearray() if rom is False else rom

    def _ProbeSpecialFlashCart(self, cart_type: dict[str, Any]) -> bool | None:
        if "m29w640" in cart_type:
            self._cart_write_flash(cart_type["commands"]["reset"], flashcart=self.MODE == "AGB")
            rom1: bytearray = self._cart_read(0, 8)
            self._cart_write_flash(
                cart_type["commands"]["read_identifier"],
                flashcart=self.MODE == "AGB",
            )
            rom2: bytearray = self._cart_read(0, 8)
            matched = rom1 != rom2 and list(rom2[: len(cart_type["flash_ids"][0])]) == cart_type["flash_ids"][0]
            if matched:
                dprint("Found a M29W640 cartridge")
                self._cart_write_flash(cart_type["commands"]["reset"], flashcart=self.MODE == "AGB")
            return matched

        if self.MODE == "DMG" and cart_type["command_set"] == "BLAZE_XPLODER":
            self._cart_read(0x102, 1)
            self._cart_write(6, 1)
            self._cart_write_flash(cart_type["commands"]["reset"])
            self._cart_write(6, 0)
            rom1 = self._cart_read(0x4000, 8)
            self._cart_write(6, 1)
            self._cart_write_flash(cart_type["commands"]["read_identifier"])
            rom2 = self._cart_read(0x4000, 8)
            matched = rom1 != rom2 and list(rom2[: len(cart_type["flash_ids"][0])]) == cart_type["flash_ids"][0]
            if matched:
                dprint("Found a BLAZE Xploder GB")
                self._cart_write_flash(cart_type["commands"]["reset"])
            return matched

        if self.MODE == "DMG" and cart_type["command_set"] == "DATEL_ORBITV2":
            rom1 = self._cart_read(cart_type["read_identifier_at"], 10)
            for command in cart_type["commands"]["unlock_read"]:
                self._cart_read(command[0], 1)
            self._cart_write_flash(cart_type["commands"]["unlock"])
            self._cart_write_flash(cart_type["commands"]["read_identifier"])
            rom2 = self._cart_read(cart_type["read_identifier_at"], 10)
            matched = rom1 != rom2 and list(rom2[: len(cart_type["flash_ids"][0])]) == cart_type["flash_ids"][0]
            if matched:
                dprint("Found a GameShark or Action Replay")
                self._cart_write_flash(cart_type["commands"]["reset"])
            return matched

        if self.MODE == "DMG" and cart_type["command_set"] == "GBMEMORY":
            rom1 = self._cart_read(0, 8)
            self._cart_write_flash(cart_type["commands"]["unlock"])
            self._cart_write_flash(cart_type["commands"]["read_identifier"])
            rom2 = self._cart_read(0, 8)
            matched = rom1 != rom2 and list(rom2[: len(cart_type["flash_ids"][0])]) == cart_type["flash_ids"][0]
            if matched:
                dprint("Found a GB-Memory Cartridge")
                self._cart_write_flash(cart_type["commands"]["reset"])
            return matched

        if self.MODE == "DMG" and "dmg-mbc5-32m-flash" in cart_type and self.SupportsAudioAsWe():
            self._set_we_pin_audio()
            self._cart_write_flash(cart_type["commands"]["unlock"], flashcart=False)
            self._cart_write_flash(cart_type["commands"]["reset"])
            rom1 = self._cart_read(0, 8)
            self._cart_write_flash(cart_type["commands"]["read_identifier"])
            rom2 = self._cart_read(0, 8)
            matched = rom1 != rom2 and list(rom2[: len(cart_type["flash_ids"][0])]) == cart_type["flash_ids"][0]
            if matched:
                dprint("Found a DMG-MBC5-32M-FLASH Development Cartridge")
                self._cart_write_flash(cart_type["commands"]["reset"])
            else:
                self._cart_write_flash([[0, 0xFF]], flashcart=True)
                self._cart_write_flash([[0, 0xF0]], flashcart=True)
                self._set_we_pin_wr()
            return matched

        if self.MODE == "AGB" and cart_type["command_set"] == "GBAMP":
            rom1 = self._cart_read(0x1E8F << 1, 2) + self._cart_read(0x168F << 1, 2)
            for command in cart_type["commands"]["unlock_read"]:
                self._cart_read(command[0] << 1)
            self._cart_write_flash(cart_type["commands"]["read_identifier"], flashcart=True)
            rom2 = self._cart_read(0x1E8F << 1, 2) + self._cart_read(0x168F << 1, 2)
            matched = rom1 != rom2 and list(rom2[: len(cart_type["flash_ids"][0])]) == cart_type["flash_ids"][0]
            if matched:
                dprint("Found a GBA Movie Player v2")
                self._cart_write_flash(cart_type["commands"]["reset"], flashcart=True)
            return matched

        if self.MODE == "DMG" and cart_type["command_set"] == "BUNG_16M" and self.SupportsAudioAsWe():
            self._set_we_pin_audio()
            rom1 = self._cart_read(0, 4)
            self._cart_write(0x2000, 0x02, flashcart=False)
            self._cart_write(0x6AAA, 0xAA, flashcart=True)
            self._cart_write(0x2000, 0x01, flashcart=False)
            self._cart_write(0x5554, 0x55, flashcart=True)
            self._cart_write(0x2000, 0x02, flashcart=False)
            self._cart_write(0x6AAA, 0x90, flashcart=True)
            rom2 = self._cart_read(0, 4)
            matched = rom1 != rom2 and list(rom2[: len(cart_type["flash_ids"][0])]) == cart_type["flash_ids"][0]
            if matched:
                dprint("Found a BUNG Doctor GB Card 16M")
                self._cart_write(0x2000, 0x02, flashcart=False)
                self._cart_write(0x6AAA, 0xAA, flashcart=True)
                self._cart_write(0x2000, 0x01, flashcart=False)
                self._cart_write(0x5554, 0x55, flashcart=True)
                self._cart_write(0x2000, 0x02, flashcart=False)
                self._cart_write(0x6AAA, 0xF0, flashcart=True)
                self._cart_write(0x2000, 0x00, flashcart=False)
            else:
                self._cart_write_flash([[0, 0xFF]], flashcart=True)
                self._cart_write_flash([[0, 0xF0]], flashcart=True)
                self._set_we_pin_wr()
            return matched

        return None

    def DetectFlash(self, limitVoltage: bool = False) -> tuple[Any, ...]:
        mode = self.MODE
        if mode is None:
            msg = "Cartridge mode must be selected before detecting flash"
            raise RuntimeError(msg)
        supported_carts: list[Any] = list(self.SUPPORTED_CARTS[mode].values())
        fc_fncptr: FlashcartCallbacks = {
            "cart_write_fncptr": self._cart_write,
            "cart_write_fast_fncptr": self._cart_write_flash,
            "cart_read_fncptr": self.ReadROM,
            "cart_powercycle_fncptr": self.CartPowerCycleOrAskReconnect,
            "progress_fncptr": self.SetProgress,
            "set_we_pin_wr": self._set_we_pin_wr,
            "set_we_pin_audio": self._set_we_pin_audio,
        }

        cfi_buffer: bytearray | None = bytearray()
        flash_id_s = ""
        flash_types = []
        flash_id_methods = []
        read_method = self.AGB_READ_METHOD

        rom = self._PrepareFlashDetectionMode(mode, limitVoltage)

        dprint("Resetting and unlocking all cart types")
        cmds = []
        cmds_reset = []
        cmds_unlock_read = []

        for cart_type in sorted(
            supported_carts,
            key=lambda c: "m29w640" not in c,
        ):  # m29w640 first because of corruption risk
            f = supported_carts.index(cart_type)
            if "command_set" not in cart_type:
                continue
            if "manual_select" in cart_type and cart_type["manual_select"] is True:
                continue
            special_match = self._ProbeSpecialFlashCart(cart_type)
            if special_match is not None:
                if special_match:
                    flash_types.append(f)
                    break
                continue

            if "commands" in cart_type:
                c = {
                    "reset": [],
                    "read_identifier": [],
                    "read_cfi": [],
                }
                if "reset" in cart_type["commands"]:
                    c["reset"] = cart_type["commands"]["reset"]
                    if cart_type["commands"]["reset"] not in cmds_reset:
                        cmds_reset.append(cart_type["commands"]["reset"])
                if (
                    "unlock_read" in cart_type["commands"]
                    and cart_type["commands"]["unlock_read"] not in cmds_unlock_read
                ):
                    cmds_unlock_read.append(cart_type["commands"]["unlock_read"])
                if "unlock" in cart_type["commands"]:
                    if cart_type["commands"]["unlock"] not in cmds_reset:
                        cmds_reset.append(cart_type["commands"]["unlock"])
                    if cart_type["commands"]["reset"] not in cmds_reset:
                        cmds_reset.append(cart_type["commands"]["reset"])
                if "read_identifier" in cart_type["commands"]:
                    c["read_identifier"] = cart_type["commands"]["read_identifier"]
                if "read_cfi" in cart_type["commands"]:
                    c["read_cfi"] = cart_type["commands"]["read_cfi"]
                if len(c["read_identifier"]) > 0:
                    if self.MODE == "DMG" and cart_type["commands"]["read_identifier"][0][0] > 0x7000:
                        continue
                    if c not in cmds:
                        found = False
                        for t in cmds:
                            if c["read_identifier"] == t["read_identifier"]:
                                found = True
                        if not found:
                            cmds.append(c)

        for c in cmds_reset:
            for cmd in c:
                dprint(f"Resetting by writing to 0x{cmd[0]:X}=0x{cmd[1]:X}")
                self._cart_write(cmd[0], cmd[1], flashcart=True)

        flash_id_cmds = sorted(cmds, key=lambda x: x["read_identifier"][0][0])
        read_cfi_cmds = []

        rom_s: str = " ".join(format(x, "02X") for x in rom)
        if self.MODE == "DMG":
            flash_id_s: str = "[     ROM     ] " + rom_s + "\n"
            we_pins: list[str] = ["WR", "AUDIO"]
        else:
            flash_id_s = "[    ROM    ] " + rom_s + "\n"
            we_pins = [""]

        if len(flash_types) == 0:
            flash_id_s = self._SearchFlashIdentifiers(
                _FlashIdSearchContext(
                    mode,
                    rom,
                    flash_id_cmds,
                    cfi_buffer,
                    read_cfi_cmds,
                    flash_id_methods,
                    flash_id_s,
                    we_pins,
                ),
            )

        self._MatchDetectedFlashTypes(
            supported_carts,
            flash_id_methods,
            flash_id_cmds,
            we_pins,
            flash_types,
        )

        dprint(
            "Compatible flash types:",
            [(index, supported_carts[index]["names"][0]) for index in flash_types],
        )

        cfi, cfi_s = self._ReadFlashCFI(supported_carts, flash_types, flash_id_cmds, read_cfi_cmds)

        flash_type_id, detected_size = self._detect_flash_size(
            supported_carts,
            flash_types,
            cfi,
            fc_fncptr,
        )

        self._RestoreFlashDetectionMode(read_method)

        return (flash_types, flash_type_id, flash_id_s, cfi_s, cfi, detected_size)

    @staticmethod
    def _find_repeating_size(
        data: bytes | bytearray | memoryview,
        max_size: int,
        min_size: int = 0x20,
    ) -> int:
        offset = max_size
        while offset >= min_size:
            offset = int(offset / 2)
            if data[0:offset] != data[offset : offset * 2]:
                offset = offset * 2
                break
        return offset

    def GetDumpReport(self) -> str:
        from .DumpReport import DumpReport  # noqa: PLC0415 - avoid the DumpReport/LK_Device import cycle

        return DumpReport.generate(self.INFO["dump_info"], self)

    def GetReadErrors(self) -> int:
        return self.READ_ERRORS

    #################################################################

    def DoTransfer(
        self,
        mode: int,
        fncSetProgress: ProgressCallback | Literal[False] | None,
        args: dict[str, Any],
    ) -> None:
        from . import DataTransfer  # noqa: PLC0415 - keep the Qt worker optional until a transfer starts

        args["mode"] = mode
        args["port"] = self
        if self.WORKER is None:
            self.WORKER = DataTransfer.DataTransfer(args)
            if fncSetProgress not in (False, None):
                self.WORKER.updateProgress.connect(fncSetProgress)
        else:
            self.WORKER.setConfig(args)
        self.WORKER.start()

    def BackupROM(
        self,
        fncSetProgress: ProgressCallback | Literal[False] | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        if args is None:
            args = {}
        self.DoTransfer(1, fncSetProgress, args)

    def BackupRAM(
        self,
        fncSetProgress: ProgressCallback | Literal[False] | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        if args is None:
            args = {}
        if fncSetProgress is False:
            args["mode"] = 2
            args["port"] = self
            self._BackupRestoreRAM(args=args)
        else:
            self.DoTransfer(2, fncSetProgress, args)

    def RestoreRAM(
        self,
        fncSetProgress: ProgressCallback | Literal[False] | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        if args is None:
            args = {}
        self.DoTransfer(3, fncSetProgress, args)

    def FlashROM(
        self,
        fncSetProgress: ProgressCallback | Literal[False] | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        if args is None:
            args = {}
        self.DoTransfer(4, fncSetProgress, args)

    def DetectCartridge(
        self,
        fncSetProgress: ProgressCallback | Literal[False] | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        if args is None:
            args = {}
        self.DoTransfer(5, fncSetProgress, args)

    #################################################################

    def _BackupROM(self, args: dict[str, Any]) -> ROMBackupResult:
        self._thread_worker_auto_poweroff_start()
        try:
            return self._BackupROM_Worker(args)
        finally:
            self._thread_worker_auto_poweroff_finish()

    def _CalculateROMChecksums(
        self,
        buffer: bytearray,
        file: BinaryIO | None,
        mbc: _ROMBackupMapper,
    ) -> None:
        """Populate ROM metadata and hashes after a successful read."""
        self.SetProgress({"action": "CALC_CHECKSUMS"})
        if "header" not in self.INFO["dump_info"]:
            self.INFO["dump_info"]["header"] = {}
        if self.MODE == "DMG":
            if mbc.GetName() == "MMM01":
                self.INFO["dump_info"]["header"].update(
                    RomFileDMG(buffer[-0x8000 : -0x8000 + 0x180]).GetHeader(unchanged=True),
                )
            elif mbc.GetName() not in ("Sachen", "Xploder GB", "Datel Orbit V2"):
                self.INFO["dump_info"]["header"].update(RomFileDMG(buffer[:0x180]).GetHeader(unchanged=True))
            self.SetProgress({"action": "CALC_CHECKSUMS", "type": __("ROM checksum")})
            self.INFO["rom_checksum_calc"] = mbc.CalcChecksum(buffer)
        elif self.MODE == "AGB":
            self.INFO["dump_info"]["header"].update(RomFileAGB(buffer[:0x180]).GetHeader())

            save_library = "N/A"
            try:
                identifiers = (
                    b"SRAM_",
                    b"EEPROM_V",
                    b"FLASH_V",
                    b"FLASH512_V",
                    b"FLASH1M_V",
                    b"AGB_8MDACS_DL_V",
                )
                for identifier in identifiers:
                    position = buffer.find(identifier)
                    if position > 0:
                        raw_version = buffer[position : position + 0x20]
                        save_library = raw_version[: raw_version.index(0x00)].decode("ascii", "replace")
                        break
            except ValueError:
                save_library = "N/A"
            self.INFO["dump_info"]["agb_savelib"] = save_library
            self.INFO["dump_info"]["agb_save_flash_id"] = None
            if "FLASH" in save_library:
                try:
                    agb_save_flash_id = self.ReadFlashSaveID()
                    if agb_save_flash_id is not False and len(agb_save_flash_id) == 2:
                        self.INFO["dump_info"]["agb_save_flash_id"] = agb_save_flash_id
                except Exception:
                    print(__("Error querying the flash save chip."))
                    device = self._serial_device()
                    device.reset_input_buffer()
                    device.reset_output_buffer()

            self.INFO["dump_info"].pop("eeprom_data", None)
            if "EEPROM" in save_library and len(buffer) == 0x2000000:
                padding_byte = buffer[0x1FFFEFF]
                dprint(
                    f"Replacing unmapped ROM data of cartridge (32 MiB ROM + EEPROM save type) with the original padding byte of 0x{padding_byte:02X}.",
                )
                self.INFO["dump_info"]["eeprom_data"] = buffer[0x1FFFF00:0x2000000]
                buffer[0x1FFFF00:0x2000000] = bytearray([padding_byte] * 0x100)
                if file is not None:
                    file.seek(0x1FFFF00)
                    file.write(buffer[0x1FFFF00:0x2000000])

        self.SetProgress({"action": "CALC_CHECKSUMS", "type": __("MD5 hash")})
        self.INFO["file_md5"] = hashlib.md5(buffer).hexdigest()
        self.SetProgress({"action": "CALC_CHECKSUMS", "type": __("SHA-1 hash")})
        self.INFO["file_sha1"] = hashlib.sha1(buffer).hexdigest()
        self.SetProgress({"action": "CALC_CHECKSUMS", "type": __("SHA-256 hash")})
        self.INFO["file_sha256"] = hashlib.sha256(buffer).hexdigest()
        self.SetProgress({"action": "CALC_CHECKSUMS", "type": __("CRC32 checksum")})
        self.INFO["file_crc32"] = zlib.crc32(buffer) & 0xFFFFFFFF
        self.INFO["dump_info"]["hash_md5"] = self.INFO["file_md5"]
        self.INFO["dump_info"]["hash_sha1"] = self.INFO["file_sha1"]
        self.INFO["dump_info"]["hash_sha256"] = self.INFO["file_sha256"]
        self.INFO["dump_info"]["hash_crc32"] = self.INFO["file_crc32"]

    def _process_rom_backup_result(
        self,
        args: dict[str, Any],
        buffer: bytearray,
        file: BinaryIO | None,
        mbc: _ROMBackupMapper,
    ) -> bool:
        if "bl_offset" in args:
            return True

        if self.MODE == "DMG" and len(args["path"]) > 0 and mbc.HasHiddenSector():
            hidden_sector = mbc.ReadHiddenSector()
            if hidden_sector is False:
                msg = __(
                    "An error occured while trying to read the hidden sector data of the {gb_memory_cartridge}.",
                    gb_memory_cartridge="NP GB-Memory Cartridge",
                )
                print(ANSI.RED + msg + ANSI.RESET)
                self.SetProgress(
                    {
                        "action": "ABORT",
                        "info_type": "msgbox_critical",
                        "info_msg": msg,
                        "abortable": False,
                    },
                )
                self.CANCEL = True
                self.ERROR = True
                if file is not None:
                    file.close()
                return False
            if file is not None:
                file.close()
            file = Path(args["path"]).with_suffix(".map").open("wb")
            self.INFO["hidden_sector"] = hidden_sector
            self.INFO["dump_info"]["gbmem"] = hidden_sector
            gbmem_parsed = (GBMemoryMap()).ParseMapData(buffer_map=hidden_sector, buffer_rom=buffer)
            if gbmem_parsed:
                self.INFO["dump_info"]["gbmem_parsed"] = gbmem_parsed
            file.write(hidden_sector)
            file.close()
            gbmp = self.INFO["dump_info"]["gbmem_parsed"]
            if isinstance(gbmp, list) and len(args["path"]) > 2:
                for entry in gbmp[1:]:
                    if entry["header"] == {} or entry["header"]["logo_correct"] is False:
                        continue
                    settings = args.get("settings")
                    name = generate_filename(mode="DMG", header=entry["header"], settings=settings)
                    output_path = Path(f"{Path(args['path']).with_suffix('')} - {name}")
                    with output_path.open("wb") as output_file:
                        output_file.write(buffer[entry["rom_offset"] : entry["rom_offset"] + entry["rom_size"]])
        else:
            self.INFO.pop("hidden_sector", None)
            self.INFO["dump_info"].pop("gbmem", None)
            self.INFO["dump_info"].pop("gbmem_parsed", None)

        self.INFO["loop_detected"] = False
        loop_size = len(buffer)
        while loop_size > 0x4000:
            loop_size >>= 1
            if buffer[0:0x4000] != buffer[loop_size : loop_size + 0x4000]:
                break
            if buffer[0:loop_size] == buffer[loop_size : loop_size * 2]:
                self.INFO["loop_detected"] = loop_size

        self._CalculateROMChecksums(buffer, file, mbc)
        return True

    def _configure_rom_read_pullups(
        self,
        args: dict[str, Any],
        cart_type: dict[str, Any],
        flashcart: object,
    ) -> None:
        if self.FW["fw_ver"] >= 8:
            if flashcart and "enable_pullups" in cart_type:
                self._write(self.DEVICE_CMD["ENABLE_PULLUPS"], wait=True)
                self.SetAGBReadMethod(0)
                dprint("Pullups enabled")
                if (self.FW["pcb_name"] == "GBFlash" and self.FW["pcb_ver"] < 13) or (self.FW["pcb_name"] == "Joey Jr"):
                    print(
                        ANSI.YELLOW
                        + __(
                            "Note: This cartridge may not be fully compatible with your {device_name}.",
                            device_name=self.FW["pcb_name"],
                        )
                        + ANSI.RESET,
                    )
            else:
                self._write(self.DEVICE_CMD["DISABLE_PULLUPS"], wait=True)
                dprint("Pullups disabled")

        if self.FW["fw_ver"] >= 12 and self.MODE == "DMG":
            enable_pullup_wr = (
                2
                if (
                    cart_type.get("enable_pullup_wr") is True
                    or ("force_wr_pullup" in args and args["force_wr_pullup"] is True)
                )
                else 0
            )
            self._set_fw_variable("PULLUPS_ENABLED", enable_pullup_wr)

    def _apply_legacy_flashcart_compatibility(self, cart_type: dict[str, Any], flashcart: object) -> None:
        if self.FW["fw_ver"] >= 8 or not flashcart or "enable_pullups" not in cart_type:
            return
        print(
            ANSI.YELLOW
            + __(
                "Note: This flashcart profile may not be fully compatible with your {device_name} running an old or legacy firmware version.",
                device_name=self.GetName(),
            )
            + ANSI.RESET,
        )
        del cart_type["enable_pullups"]

    def _PrepareBackupFlashcart(
        self,
        device_mode: DeviceMode,
        cart_type_index: int,
    ) -> tuple[dict[str, Any], Flashcart | Literal[False]]:
        supported_carts = list(self.SUPPORTED_CARTS[device_mode].values())
        cart_type: Any = copy.deepcopy(supported_carts[cart_type_index])
        flashcart: Flashcart | Literal[False] = False
        if not isinstance(cart_type, str):
            cart_type["_index"] = 0
            for i in range(len(list(self.SUPPORTED_CARTS[device_mode].keys()))):
                if i == cart_type_index:
                    try:
                        cart_type["_index"] = cart_type["names"].index(
                            list(self.SUPPORTED_CARTS[device_mode].keys())[i],
                        )
                        callbacks: FlashcartCallbacks = {
                            "cart_write_fncptr": self._cart_write,
                            "cart_write_fast_fncptr": self._cart_write_flash,
                            "cart_read_fncptr": self.ReadROM,
                            "cart_powercycle_fncptr": self.CartPowerCycleOrAskReconnect,
                            "progress_fncptr": self.SetProgress,
                            "set_we_pin_wr": self._set_we_pin_wr,
                            "set_we_pin_audio": self._set_we_pin_audio,
                        }
                        flashcart = Flashcart(config=cart_type, fncptr=callbacks)
                    except Exception:
                        logger.exception("Failed to initialize the selected flash-cart profile")
        if not isinstance(cart_type, dict):
            cart_type = {}
        return cart_type, flashcart

    def _InitializeVastFameRead(self) -> None:
        addr_reorder = [[-1 for _ in range(16)] for _ in range(4)]
        value_reorder = [[-1 for _ in range(8)] for _ in range(4)]
        for mode in range(16):
            # Set SRAM mode
            self._cart_write(0xFFF8, 0x99, sram=True)
            self._cart_write(0xFFF9, 0x02, sram=True)
            self._cart_write(0xFFFA, 0x05, sram=True)
            self._cart_write(0xFFFB, 0x02, sram=True)
            self._cart_write(0xFFFC, 0x03, sram=True)

            self._cart_write(0xFFFD, 0x00, sram=True)
            self._cart_write(0xFFFE, mode, sram=True)

            self._cart_write(0xFFF8, 0x99, sram=True)
            self._cart_write(0xFFF9, 0x03, sram=True)
            self._cart_write(0xFFFA, 0x62, sram=True)
            self._cart_write(0xFFFB, 0x02, sram=True)
            self._cart_write(0xFFFC, 0x56, sram=True)

            # Blank SRAM
            for address_bit in range(16):
                self._cart_write(1 << address_bit, 0, sram=True)
            self._cart_write(0x8000, 0, sram=True)

            # Get address reordering for SRAM writes (repeats every 4 modes so only check first 4)
            if mode < 4:
                for source_bit in range(16):
                    self._cart_write(1 << source_bit, 0xAA, sram=True)
                    for target_bit in range(16):
                        value = self._cart_read(1 << target_bit, 1, agb_save_flash=True)[0]
                        if value != 0:
                            addr_reorder[mode][target_bit] = source_bit
                            break
                    self._cart_write(1 << source_bit, 0, sram=True)  # reblank SRAM
                addr_reorder[mode].reverse()

            # Get value reordering for SRAM writes/reads (assumes upper bit of address is never reordered)
            if mode % 4 == 0:
                for source_bit in range(8):
                    self._cart_write(0x8000, 1 << source_bit, sram=True)
                    value = self._cart_read(0x8000, 1, agb_save_flash=True)[0]
                    for target_bit in range(8):
                        if (1 << target_bit) == value:
                            value_reorder[mode // 4][target_bit] = source_bit
                            break
                value_reorder[mode // 4].reverse()

        self.INFO["dump_info"]["vf_addr_reorder"] = addr_reorder
        self.INFO["dump_info"]["vf_value_reorder"] = value_reorder

    def _ResetROMReadState(
        self,
        mbc: _FlashResetMapper,
        cart_type: dict[str, Any],
        flashcart: Flashcart | Literal[False],
        dmg_read_method: int,
        agb_read_method: int,
    ) -> None:
        if self.MODE == "DMG":
            if mbc.ResetBeforeBankChange(0) is True:
                dprint("Resetting the MBC")
                self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
            mbc.SelectBankROM(0)
            self.SetDMGReadMethod(dmg_read_method)
        elif self.MODE == "AGB":
            if flashcart and "flash_bank_select_type" in cart_type and cart_type["flash_bank_select_type"] > 0:
                flashcart.SelectBankROM(0)
            self.SetAGBReadMethod(agb_read_method)

    def _PrepareROMRead(
        self,
        mode: DeviceMode,
        args: dict[str, Any],
        cart_type: dict[str, Any],
        flashcart: Flashcart | Literal[False],
    ) -> _ROMReadConfiguration | None:
        mbc: Any = None
        size = 0
        rom_banks = 1
        rom_bank_size = 0x2000000
        buffer_len = 0x4000
        is_3d_memory = mode == "AGB" and cart_type.get("command_set") == "3DMEMORY"

        if mode == "DMG":
            self.INFO["dump_info"]["rom_size"] = args["rom_size"]
            self.INFO["dump_info"]["mapper_type"] = args["mbc"]
            self.INFO["dump_info"]["dmg_read_method"] = self.DMG_READ_METHODS[self.DMG_READ_METHOD]

            self.INFO["mapper_raw"] = args["mbc"]
            if not self.IsSupportedMbc(args["mbc"]):
                msg = __(
                    "This cartridge uses a mapper that is not supported by FlashGBX using your {device_name}.",
                    device_name=self.GetFullName(),
                )
                self.SetProgress(
                    {
                        "action": "ABORT",
                        "info_type": "msgbox_critical",
                        "info_msg": msg,
                        "abortable": False,
                    },
                )
                return None

            if "verify_mbc" in args and args["verify_mbc"] is not None:
                mbc = args["verify_mbc"]
            else:
                mbc = DMG_Mapper().GetInstance(
                    args=args,
                    cart_write_fncptr=self._cart_write,
                    cart_read_fncptr=self._mapper_cart_read,
                    cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
                    clk_toggle_fncptr=self._clk_toggle,
                )

            if self._get_fw_variable("CART_MODE") != 1:
                self._write(self.DEVICE_CMD["SET_MODE_DMG"], wait=self.FW["fw_ver"] >= 12)

            self._set_fw_variable("DMG_WRITE_CS_PULSE", 0)
            self._set_fw_variable("DMG_READ_CS_PULSE", 0)
            if mbc.GetName() == "TAMA5":
                self._set_fw_variable("DMG_WRITE_CS_PULSE", 1)
                self._set_fw_variable("DMG_READ_CS_PULSE", 1)
                mbc.EnableMapper()
                self._set_fw_variable("DMG_READ_CS_PULSE", 0)
            elif mbc.GetName() == "Sachen":
                start_bank = int(args["rom_size"] / 0x4000)
                mbc.SetStartBank(start_bank)
            else:
                mbc.EnableMapper()

            rom_size = args["rom_size"]
            rom_banks = mbc.GetROMBanks(rom_size)
            rom_bank_size = mbc.GetROMBankSize()
            size = mbc.GetROMSize()
        else:
            self.INFO["dump_info"]["mapper_type"] = None
            if self._get_fw_variable("CART_MODE") != 2:
                self._write(self.DEVICE_CMD["SET_MODE_AGB"], wait=self.FW["fw_ver"] >= 12)
                self.ReadROM(0, 4)  # dummy read

            buffer_len = 0x10000
            size = args.get("agb_rom_size", 32 * 1024 * 1024)
            self.INFO["dump_info"]["rom_size"] = size
            if is_3d_memory:
                self.INFO["dump_info"]["agb_read_method"] = "3D Memory"
            elif flashcart and cart_type.get("command_set") == "GBAMP":
                self.INFO["dump_info"]["agb_read_method"] = "GBA Movie Player"
                if "verify_write" not in args:
                    self.CartPowerCycleOrAskReconnect()
                flashcart.Unlock()
                buffer_len = 0x4000
            elif flashcart and cart_type.get("command_set") == "VASTFAME":
                self._InitializeVastFameRead()
                self.INFO["dump_info"]["agb_read_method"] = self.AGB_READ_METHODS[self.AGB_READ_METHOD]
            else:
                self.INFO["dump_info"]["agb_read_method"] = self.AGB_READ_METHODS[self.AGB_READ_METHOD]

            if flashcart and "flash_bank_size" in cart_type:
                if "verify_write" in args:
                    rom_banks = math.ceil(len(args["verify_write"]) / cart_type["flash_bank_size"])
                else:
                    rom_banks = math.ceil(size / cart_type["flash_bank_size"])
                rom_bank_size = cart_type["flash_bank_size"]

        return _ROMReadConfiguration(mbc, size, rom_banks, rom_bank_size, buffer_len, is_3d_memory)

    @staticmethod
    def _GetROMReadRange(
        args: dict[str, Any],
        size: int,
        rom_bank_size: int,
        rom_banks: int,
    ) -> _ROMReadRange:
        buffer_position = 0
        start_address = 0
        end_address = size
        start_bank = 0
        end_bank = rom_banks
        if "verify_write" in args:
            buffer_position = args["verify_from"]
            start_address = buffer_position
            end_address = args["verify_from"] + args["verify_len"]
            start_bank = math.floor(buffer_position / rom_bank_size)
            end_bank = math.ceil((buffer_position + args["verify_len"]) / rom_bank_size)
        elif "bl_offset" in args:
            if args.get("bl_layout") in (1, 2):
                args["bl_size"] <<= 1
            buffer_position = args["bl_offset"]
            start_address = buffer_position
            end_address = args["bl_offset"] + args["bl_size"]
            start_bank = math.floor(buffer_position / rom_bank_size)
            end_bank = math.ceil((buffer_position + args["bl_size"]) / rom_bank_size)
        return _ROMReadRange(buffer_position, start_address, end_address, start_bank, end_bank)

    def _AbortROMReadIfCanceled(self, file: BinaryIO | None) -> bool:
        if not self.CANCEL:
            return False
        cancel_args = {"action": "ABORT", "abortable": False}
        cancel_args.update(self.CANCEL_ARGS)
        self.CANCEL_ARGS = {}
        self.ERROR_ARGS = {}
        self.SetProgress(cancel_args)
        try:
            if file is not None:
                file.close()
        except Exception:
            logger.exception("Failed to close the canceled ROM-read output file")
        if self.CanPowerCycleCart():
            self.CartPowerCycle()
        return True

    def _VerifyROMReadChunk(
        self,
        args: dict[str, Any],
        chunk: bytearray,
        buffer: bytearray,
        position: int,
    ) -> int | None:
        expected = args["verify_write"][position - len(chunk) : position]
        if chunk[: len(expected)] != expected:
            if ".dev" in AppInfo.VERSION_PEP440 or AppContext.DEBUG:
                dprint(f"Writing 0x{len(chunk):X} bytes to debug_verify_0x{position - len(chunk):X}.bin")
                debug_path = Path(AppContext.CONFIG_PATH) / f"debug_verify_0x{position - len(chunk):X}.bin"
                with debug_path.open("ab") as debug_file:
                    debug_file.write(chunk)

            for index in range(position):
                is_mismatch = (
                    index < len(args["verify_write"]) - 1
                    and index < position - 1
                    and args["verify_write"][index] != buffer[index]
                )
                if not is_mismatch:
                    continue
                if args["rtc_area"] is True and index in (0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9):
                    dprint(f"Skipping RTC area at 0x{index:X}")
                else:
                    dprint(f"Mismatch during verification at 0x{index:X}")
                    return index
        else:
            dprint(f"Verification successful between 0x{position - len(chunk):X} and 0x{position:X}")
        self.SetProgress({"action": "UPDATE_POS", "pos": args["verify_from"] + position})
        return None

    def _HandleIncompleteROMRead(self, context: _IncompleteROMReadContext) -> _IncompleteROMReadResult:
        args, buffer_len, pos_total, max_length, lives = context
        error_position = pos_total
        if "verify_write" in args:
            error_position += args["verify_base_pos"]
            self.SetProgress({"action": "UPDATE_POS", "pos": args["verify_from"] + pos_total})
        else:
            self.SetProgress({"action": "UPDATE_POS", "pos": pos_total})

        err_text = (
            ANSI.YELLOW
            + __(
                "Note: Incomplete transfer detected. Resuming from {address}...",
                address=f"0x{error_position:X}",
            )
            + ANSI.RESET
        )
        if (max_length >> 1) < 64:
            dprint(f"Failed to receive 0x{buffer_len:X} bytes from the device at position 0x{error_position:X}.")
            max_length = 64
        elif lives >= 20 and error_position != 0:
            dprint(f"Failed to receive 0x{buffer_len:X} bytes from the device at position 0x{error_position:X}.")
        else:
            dprint(
                f"Failed to receive 0x{buffer_len:X} bytes from the device at position 0x{error_position:X}. Decreasing maximum transfer buffer size to 0x{max_length >> 1:X}.",
            )
            max_length >>= 1
            self.MAX_BUFFER_READ = max_length
            err_text += "\n" + __("Buffer size adjusted to {size} bytes.", size=max_length)
        if ".dev" in AppInfo.VERSION_PEP440 and not AppContext.DEBUG:
            print(err_text)

        self.INFO["dump_info"]["transfer_size"] = max_length
        device = self._serial_device()
        device.reset_input_buffer()
        device.reset_output_buffer()
        lives -= 1
        if lives != 0:
            return _IncompleteROMReadResult(max_length, lives, abort_verification=False)

        self.CANCEL_ARGS.update(
            {
                "info_type": "msgbox_critical",
                "info_msg": __(
                    "An error occured while reading from the cartridge. When connecting the device, avoid passive USB hubs and try different USB ports/cables.",
                ),
            },
        )
        self.CANCEL = True
        self.ERROR = True
        return _IncompleteROMReadResult(
            max_length,
            lives,
            abort_verification="verify_write" in args,
        )

    @staticmethod
    def _OpenROMBackupFile(path: str) -> BinaryIO | None:
        if not path:
            return None
        return Path(path).open("wb")

    def _BackupROM_Worker(self, args: dict[str, Any]) -> ROMBackupResult:
        device_mode = self._require_cartridge_mode("reading ROM")
        file = self._OpenROMBackupFile(args["path"])

        self.FAST_READ = True
        agb_read_method = self.AGB_READ_METHOD
        dmg_read_method = self.DMG_READ_METHOD
        pos = 0
        cart_type, flashcart = self._PrepareBackupFlashcart(device_mode, args["cart_type"])

        self._apply_legacy_flashcart_compatibility(cart_type, flashcart)

        self.INFO["dump_info"]["timestamp"] = datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
        self.INFO["dump_info"]["file_name"] = args["path"]
        self.INFO["dump_info"]["file_size"] = args["rom_size"]
        self.INFO["dump_info"]["cart_type"] = args["cart_type"]
        self.INFO["dump_info"]["system"] = self.MODE

        configuration = self._PrepareROMRead(device_mode, args, cart_type, flashcart)
        if configuration is None:
            return False
        _mbc, size, rom_banks, rom_bank_size, buffer_len, is_3dmemory = configuration

        if self.ERROR:
            return None

        if "verify_write" in args:
            size = len(args["verify_write"])
            buffer_len = min(buffer_len, size)
        else:
            method = "SAVE_READ" if "bl_offset" in args else "ROM_READ"
            pos = 0
            self.SetProgress({"action": "INITIALIZE", "method": method, "size": size})
            self.INFO["action"] = self.ACTIONS[method]
            self._configure_rom_read_pullups(args, cart_type, flashcart)

        buffer = bytearray(size)
        max_length = self.MAX_BUFFER_READ
        dprint(f"Max buffer size: 0x{max_length:X}")
        max_length = min(max_length, 4096) if is_3dmemory else min(max_length, 8192)
        self.INFO["dump_info"]["transfer_size"] = max_length
        pos_total = 0
        # dprint("ROM banks:", rom_banks)

        read_range = self._GetROMReadRange(args, size, rom_bank_size, rom_banks)
        buffer_pos, start_address, end_address, start_bank, rom_banks = read_range

        dprint(
            f"start_address=0x{start_address:X}, end_address=0x{end_address:X}, start_bank=0x{start_bank:X}, rom_banks=0x{rom_banks:X}, buffer_len=0x{buffer_len:X}, max_length=0x{max_length:X}",
        )
        bank = start_bank
        while bank < rom_banks:
            # ↓↓↓ Switch ROM bank
            if self.MODE == "DMG":
                if _mbc.ResetBeforeBankChange(bank) is True:
                    dprint("Resetting the MBC")
                    self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
                (start_address, bank_size) = _mbc.SelectBankROM(bank)
                end_address = start_address + bank_size
                buffer_len = min(buffer_len, _mbc.GetROMBankSize())
                if "verify_write" in args:
                    buffer_len = min(buffer_len, bank_size, len(args["verify_write"]))
                    end_address = start_address + bank_size
                    start_address += buffer_pos % rom_bank_size
                    end_address = min(end_address, start_address + args["verify_len"])
            elif self.MODE == "AGB":
                if "verify_write" in args:
                    buffer_len = min(buffer_len, len(args["verify_write"]))
                if flashcart and "flash_bank_select_type" in cart_type and cart_type["flash_bank_select_type"] > 0:
                    flashcart.SelectBankROM(bank)
                    temp = end_address - start_address
                    start_address %= cart_type["flash_bank_size"]
                    end_address = min(cart_type["flash_bank_size"], start_address + temp)
            # ↑↑↑ Switch ROM bank

            skip_init = False
            pos = start_address
            lives = 20

            while pos < end_address:
                temp = bytearray()
                if self._AbortROMReadIfCanceled(file):
                    return None

                if is_3dmemory:
                    temp = self.ReadROM_3DMemory(address=pos, length=buffer_len, max_length=max_length)
                elif flashcart and cart_type["command_set"] == "GBAMP":
                    temp = self.ReadROM_GBAMP(address=pos, length=buffer_len, max_length=max_length)
                else:
                    if self.FW["fw_ver"] >= 10 and "verify_write" in args:
                        if self.MODE == "AGB":
                            self.SetAGBReadMethod(0)
                        if self.MODE == "DMG":
                            self.SetDMGReadMethod(2)
                        temp = self.ReadROM(
                            address=pos,
                            length=buffer_len,
                            skip_init=skip_init,
                            max_length=max_length,
                        )
                        if self.MODE == "AGB":
                            self.SetAGBReadMethod(agb_read_method)
                        if self.MODE == "DMG":
                            self.SetDMGReadMethod(dmg_read_method)
                    else:
                        # Normal read
                        temp = self.ReadROM(
                            address=pos,
                            length=buffer_len,
                            skip_init=skip_init,
                            max_length=max_length,
                        )
                    skip_init = True

                if len(temp) != buffer_len:
                    skip_init = False
                    incomplete = self._HandleIncompleteROMRead(
                        _IncompleteROMReadContext(args, buffer_len, pos_total, max_length, lives),
                    )
                    max_length = incomplete.max_length
                    lives = incomplete.lives
                    if incomplete.abort_verification:
                        return False
                    continue
                lives = max(lives, 20)

                if file is not None:
                    if "bl_layout" in args and args["bl_layout"] == 1:
                        file.write(temp[0x0000:0x2000])
                    elif "bl_layout" in args and args["bl_layout"] == 2:
                        file.write(temp[0x2000:0x4000])
                    else:
                        file.write(temp)
                buffer[pos_total : pos_total + len(temp)] = temp
                pos_total += len(temp)

                if "verify_write" in args:
                    mismatch = self._VerifyROMReadChunk(args, temp, buffer, pos_total)
                    if mismatch is not None:
                        return mismatch
                else:
                    self.SetProgress({"action": "UPDATE_POS", "pos": pos_total})

                pos += buffer_len

            bank += 1

        if "verify_write" in args:
            return min(pos_total, len(args["verify_write"]))

        if not self._process_rom_backup_result(args, buffer, file, _mbc):
            return False

        if file is not None:
            file.close()

        self._ResetROMReadState(_mbc, cart_type, flashcart, dmg_read_method, agb_read_method)

        # Clean up
        self.INFO["last_action"] = self.INFO["action"]
        self.INFO["action"] = None
        self.INFO["last_path"] = args["path"]
        self._thread_worker_auto_poweroff_finish()
        self.SetProgress({"action": "FINISHED"})
        return True

    def WriteRTC(self, args: dict[str, Any]) -> bool | None:
        if self.CanPowerCycleCart():
            self.CartPowerOn()

        if self.MODE == "DMG":
            _mbc: DMG_Mapper = DMG_Mapper().GetInstance(
                args=args,
                cart_write_fncptr=self._cart_write,
                cart_read_fncptr=self._mapper_cart_read,
                cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
                clk_toggle_fncptr=self._clk_toggle,
            )
            if not _mbc.HasRTC():
                return False
            ret = _mbc.WriteRTCDict(args["rtc_dict"])
        elif self.MODE == "AGB":
            _agb_gpio = AGB_GPIO(
                args={"rtc": True},
                cart_write_fncptr=self._cart_write,
                cart_read_fncptr=self._mapper_cart_read,
                cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
                clk_toggle_fncptr=self._clk_toggle,
            )
            if _agb_gpio.HasRTC() is not True:
                return False
            ret = _agb_gpio.WriteRTCDict(args["rtc_dict"])
        else:
            raise NotImplementedError
        return ret

    def _BackupRestoreRAM(self, args: dict[str, Any]) -> bool | None:
        self._thread_worker_auto_poweroff_start()
        try:
            return self._BackupRestoreRAM_Worker(args)
        finally:
            self._thread_worker_auto_poweroff_finish()

    def _prepare_save_cart_type(self, args: dict[str, Any], mode: DeviceMode) -> dict[str, Any] | None:
        if "cart_type" not in args or not args["cart_type"] or args["cart_type"] < 0:
            return None

        supported_carts = list(self.SUPPORTED_CARTS[mode].values())
        cart_type = copy.deepcopy(supported_carts[args["cart_type"]])
        if self.FW["fw_ver"] >= 12 and mode == "DMG":
            # Joey Jr bug workaround
            enable_pullup_wr = (
                2
                if (
                    ("enable_pullup_wr" in cart_type and cart_type["enable_pullup_wr"] is True)
                    or ("force_wr_pullup" in args and args["force_wr_pullup"] is True)
                )
                else 0
            )
            self._set_fw_variable("PULLUPS_ENABLED", enable_pullup_wr)
        return cart_type

    def _configure_dmg_save_mapper(
        self,
        args: dict[str, Any],
        mbc: DMG_Mapper,
        save_size: int,
        buffer_len: int,
    ) -> tuple[int, int, bool]:
        empty_data_byte = 0x00
        audio_low = False
        mapper_name = mbc.GetName()
        if mapper_name == "TAMA5":
            self._set_fw_variable("DMG_WRITE_CS_PULSE", 1)
            self._set_fw_variable("DMG_READ_CS_PULSE", 1)
            mbc.EnableMapper()
            self._set_fw_variable("DMG_READ_CS_PULSE", 0)
            buffer_len = 0x20
        elif mapper_name == "MBC7":
            buffer_len = save_size
        elif mapper_name == "MBC6":
            empty_data_byte = 0xFF
            audio_low = True
            self._set_fw_variable("FLASH_METHOD", 0x04)  # FLASH_METHOD_DMG_MBC6
            self._set_fw_variable("FLASH_WE_PIN", 0x01)  # WR
            mbc.EnableFlash(enable=True, enable_write=(args["mode"] == 3))
        elif mapper_name == "Xploder GB":
            empty_data_byte = 0xFF
            self._set_fw_variable("FLASH_PULSE_RESET", 0)
            self._set_fw_variable("FLASH_DOUBLE_DIE", 0)
            self._write(self.DEVICE_CMD["SET_FLASH_CMD"])
            self._write(0x00)  # FLASH_COMMAND_SET_NONE
            self._write(0x01)  # FLASH_METHOD_UNBUFFERED
            self._write(0x01)  # FLASH_WE_PIN_WR
            commands = [
                [0x5555, 0xAA],
                [0x2AAA, 0x55],
                [0x5555, 0xA0],
            ]
            for i in range(6):
                if i >= len(commands):
                    self._write(bytearray(struct.pack(">I", 0)) + bytearray(struct.pack(">H", 0)))
                else:
                    self._write(
                        bytearray(struct.pack(">I", commands[i][0])) + bytearray(struct.pack(">H", commands[i][1])),
                    )
            if self.FW["fw_ver"] >= 12:
                self.wait_for_ack()
            self._set_fw_variable("FLASH_COMMANDS_BANK_1", 1)
            self._write(self.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"])
            self._write(1)  # number of commands
            self._write(bytearray(struct.pack(">I", 0x0006)))  # address/value
            self._write(0)  # type = address
            ret = self._read(1)
            if ret != 0x01:
                print("Error in DMG_SET_BANK_CHANGE_CMD:", ret)
        else:
            mbc.EnableMapper()
        return buffer_len, empty_data_byte, audio_low

    def _configure_dmg_save_transfer(self, args: dict[str, Any]) -> _DMGSaveConfiguration | None:
        self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)  # fixes Taobao FRAM cart save data
        mbc = DMG_Mapper().GetInstance(
            args=args,
            cart_write_fncptr=self._cart_write,
            cart_read_fncptr=self._mapper_cart_read,
            cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
            clk_toggle_fncptr=self._clk_toggle,
        )
        if not self.IsSupportedMbc(args["mbc"]):
            msg = __(
                "This cartridge uses a mapper that is not supported by FlashGBX using your {device_name}.",
                device_name=self.GetFullName(),
            )
            self.SetProgress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": msg,
                    "abortable": False,
                },
            )
            return None

        save_size = args["save_size"] if "save_size" in args else DmgSaveTypes(mbc=args["save_type"]).GetSize()
        if save_size is None:
            return None

        ram_banks = mbc.GetRAMBanks(save_size)
        buffer_len = min(0x200, mbc.GetRAMBankSize())
        self._write(self.DEVICE_CMD["SET_MODE_DMG"], wait=self.FW["fw_ver"] >= 12)
        self._set_fw_variable("DMG_WRITE_CS_PULSE", 0)
        self._set_fw_variable("DMG_READ_CS_PULSE", 0)

        buffer_len, empty_data_byte, audio_low = self._configure_dmg_save_mapper(
            args,
            mbc,
            save_size,
            buffer_len,
        )
        extra_size = mbc.GetRTCBufferSize() if args["rtc"] is True else 0

        # Check for DMG-MBC5-32M-FLASH
        self._cart_write(0x2000, 0x00)
        self._cart_write(0x4000, 0x90)
        flash_id = self._cart_read(0x4000, 2)
        if flash_id == bytearray([0xB0, 0x88]):
            audio_low = True
        self._cart_write(0x4000, 0xF0)
        self._cart_write(0x4000, 0xFF)
        self._cart_write(0x2000, 0x01)
        if audio_low:
            dprint("DMG-MBC5-32M-FLASH Development Cartridge detected")
            self._set_fw_variable("FLASH_WE_PIN", 0x01)
            self.SetPin(["PIN_AUDIO"], set_high=False)
        self._cart_write(0x4000, 0x00)
        mbc.EnableRAM(enable=True)

        return _DMGSaveConfiguration(
            mbc=mbc,
            buffer_len=buffer_len,
            save_size=save_size,
            ram_banks=ram_banks,
            empty_data_byte=empty_data_byte,
            extra_size=extra_size,
            audio_low=audio_low,
        )

    def _configure_agb_save_transfer(
        self,
        args: dict[str, Any],
        cart_type: dict[str, Any] | None,
    ) -> _AGBSaveConfiguration | None:
        self._write(self.DEVICE_CMD["SET_MODE_AGB"], wait=self.FW["fw_ver"] >= 12)
        self._write(self.DEVICE_CMD["SET_VOLTAGE_3_3V"], wait=self.FW["fw_ver"] >= 12)
        buffer_len = 0x2000
        save_size = args["save_size"] if "save_size" in args else AgbSaveTypes().GetSize(args["save_type"])
        if save_size is None:
            return None

        ram_banks = math.ceil(save_size / 0x10000)
        agb_flash_chip = 0
        empty_data_byte = 0x00
        sram_5 = 0

        if args["save_type"] in (1, 2):  # EEPROM
            buffer_len = 64 if args["mode"] == 3 else 256
        elif args["save_type"] in (4, 5):  # FLASH
            empty_data_byte = 0xFF
            ret = self.ReadFlashSaveID()
            if ret is False:
                if not args.get("detect"):
                    self.SetProgress(
                        {
                            "action": "ABORT",
                            "info_type": "msgbox_critical",
                            "info_msg": __("Couldn't detect the save data flash chip type."),
                            "abortable": False,
                        },
                    )
                return None
            buffer_len = 0x1000
            (agb_flash_chip, _) = ret
            if agb_flash_chip in (0xBF4B, 0xBF5B, 0xFFFF):  # Bootlegs
                buffer_len = 0x800
            elif agb_flash_chip == 0xBF6D:
                buffer_len = 0x8000
        elif args["save_type"] == 6:  # DACS
            empty_data_byte = 0xFF
            ram_banks = 1
            self._cart_write(0, 0x90)
            flash_id = self._cart_read(0, 4)
            self._cart_write(0, 0x50)
            self._cart_write(0, 0xFF)
            if flash_id != bytearray([0xB0, 0x00, 0x9F, 0x00]):
                dprint(
                    "Warning: Unknown DACS flash chip ID ({:s})".format(
                        " ".join(format(x, "02X") for x in flash_id),
                    ),
                )
                if not args.get("detect"):
                    self.SetProgress(
                        {
                            "action": "ABORT",
                            "info_type": "msgbox_critical",
                            "info_msg": __(
                                "Couldn't detect the DACS flash chip.\nUnknown Flash ID: {flash_id}",
                                flash_id=" ".join(format(x, "02X") for x in flash_id),
                            ),
                            "abortable": False,
                        },
                    )
                return None
            buffer_len = 0x2000

            flash_cmds = [["PA", 0x70], ["PA", 0x10], ["PA", "PD"]]
            self._set_fw_variable("FLASH_SHARP_VERIFY_SR", 1)
            self._write(self.DEVICE_CMD["SET_FLASH_CMD"])
            self._write(0x02)  # FLASH_COMMAND_SET_INTEL
            self._write(0x01)  # FLASH_METHOD_UNBUFFERED
            self._write(0x00)  # unset
            for i in range(6):
                if i > len(flash_cmds) - 1:  # skip
                    self._write(bytearray(struct.pack(">I", 0)) + bytearray(struct.pack(">H", 0)))
                else:
                    address = flash_cmds[i][0]
                    value = flash_cmds[i][1]
                    if not isinstance(address, int):
                        address = 0
                    if not isinstance(value, int):
                        value = 0
                    address >>= 1
                    dprint(f"Setting command #{i:d} to 0x{address:X}=0x{value:X}")
                    self._write(bytearray(struct.pack(">I", address)) + bytearray(struct.pack(">H", value)))
            if self.FW["fw_ver"] >= 12:
                self.wait_for_ack()

        if cart_type is not None and cart_type.get("flash_bank_select_type") == 1:
            sram_value = self._cart_read(address=5, length=1, agb_save_flash=True)
            if sram_value is False:
                return None
            sram_5 = struct.unpack("B", sram_value)[0]
            self._cart_write(address=5, value=1, sram=True)

        commands = [
            [[None], [None]],  # No save
            [
                bytearray([self.DEVICE_CMD["AGB_CART_READ_EEPROM"], 1]),
                bytearray([self.DEVICE_CMD["AGB_CART_WRITE_EEPROM"], 1]),
            ],  # 4K EEPROM
            [
                bytearray([self.DEVICE_CMD["AGB_CART_READ_EEPROM"], 2]),
                bytearray([self.DEVICE_CMD["AGB_CART_WRITE_EEPROM"], 2]),
            ],  # 64K EEPROM
            [
                self.DEVICE_CMD["AGB_CART_READ_SRAM"],
                self.DEVICE_CMD["AGB_CART_WRITE_SRAM"],
            ],  # 256K SRAM
            [
                self.DEVICE_CMD["AGB_CART_READ_SRAM"],
                bytearray(
                    [
                        self.DEVICE_CMD["AGB_CART_WRITE_FLASH_DATA"],
                        2 if agb_flash_chip == 0x1F3D else 1,
                    ],
                ),
            ],  # 512K FLASH
            [
                self.DEVICE_CMD["AGB_CART_READ_SRAM"],
                bytearray([self.DEVICE_CMD["AGB_CART_WRITE_FLASH_DATA"], 1]),
            ],  # 1M FLASH
            [False, False],  # 8M DACS
            [
                self.DEVICE_CMD["AGB_CART_READ_SRAM"],
                self.DEVICE_CMD["AGB_CART_WRITE_SRAM"],
            ],  # 512K SRAM
            [
                self.DEVICE_CMD["AGB_CART_READ_SRAM"],
                self.DEVICE_CMD["AGB_CART_WRITE_SRAM"],
            ],  # 1M SRAM
        ]
        command = commands[args["save_type"]][args["mode"] - 2]
        extra_size = 0x10 if args["rtc"] is True else 0
        return _AGBSaveConfiguration(
            buffer_len=buffer_len,
            save_size=save_size,
            ram_banks=ram_banks,
            flash_chip=agb_flash_chip,
            sram_5=sram_5,
            command=command,
            empty_data_byte=empty_data_byte,
            extra_size=extra_size,
        )

    def _WriteDACSSaveChunk(self, sector_address: int, pos: int, data: bytearray) -> bool:
        """Erase a DACS sector when needed and write one save-data chunk."""
        erasable_sectors = {
            *(0x1F00000 + offset * 0x10000 for offset in range(16)),
            0x1FF2000,
            0x1FF4000,
            0x1FF6000,
            0x1FF8000,
            0x1FFA000,
            0x1FFC000,
        }
        if sector_address in erasable_sectors:
            dprint(f"DACS: Now at sector 0x{sector_address:X}")
            commands = [
                [
                    [0, 0x50],
                    [sector_address, 0x20],
                    [sector_address, 0xD0],
                ],
            ]
            if sector_address == 0x1F00000:
                commands.insert(
                    0,
                    [
                        [0, 0x50],
                        [0, 0x60],
                        [0, 0xD0],
                    ],
                )
            elif sector_address == 0x1FFC000:
                commands[:0] = [
                    [
                        [0, 0x50],
                        [0, 0x60],
                        [0, 0xD0],
                    ],
                    [
                        [0, 0x50],
                        [sector_address, 0x60],
                        [sector_address, 0xDC],
                    ],
                ]

            for command in commands:
                dprint("Executing DACS commands:", command)
                self._cart_write_flash(commands=command, flashcart=True)
                lives = 20
                while True:
                    time.sleep(0.1)
                    raw_status = self._cart_read(sector_address, 2)
                    if raw_status is not False and len(raw_status) >= 2:
                        status = struct.unpack("<H", raw_status)[0]
                        dprint(f"DACS: Status Register Check: 0x{status:X} == 0x80? {status & 0xE0 == 0x80!s:s}")
                        if status & 0xE0 == 0x80:
                            break
                    lives -= 1
                    if lives == 0:
                        self.SetProgress(
                            {
                                "action": "ABORT",
                                "info_type": "msgbox_critical",
                                "info_msg": __(
                                    "An error occured while writing to the DACS cartridge. Please make sure that the cartridge contacts are clean, re-connect the device and try again from the beginning.",
                                ),
                                "abortable": False,
                            },
                        )
                        return False

        if sector_address < 0x1FFE000:
            dprint(f"DACS: Writing to area 0x{0x1F00000 + pos:X}-0x{0x1F00000 + pos + len(data) - 1:X}")
            self.WriteROM(address=0x1F00000 + pos, buffer=data)
        else:
            dprint(
                f"DACS: Skipping read-only area 0x{0x1F00000 + pos:X}-0x{0x1F00000 + pos + len(data) - 1:X}",
            )
        return True

    def _VerifySaveWrite(
        self,
        args: dict[str, Any],
        mbc: _SaveMapper,
        buffer: bytearray,
        buffer_offset: int,
    ) -> bool | None:
        """Read save data back and compare it with the requested contents."""
        if self.MODE == "DMG":
            mbc.SelectBankRAM(0)
        self.SetProgress(
            {
                "action": "INITIALIZE",
                "method": "SAVE_WRITE_VERIFY",
                "size": buffer_offset,
            },
        )

        verify_args = copy.copy(args)
        end_address = buffer_offset
        if self.MODE == "AGB" and args["save_type"] == 6:  # DACS
            end_address = min(len(buffer), 0xFE000)

        path = args["path"]
        verify_args.update({"mode": 2, "verify_write": buffer, "path": None})
        self.ReadROM(0, 4)  # dummy read
        self.INFO["data"] = None
        if not self._BackupRestoreRAM(verify_args):
            return None

        verified_data = self.INFO.get("data")
        if not isinstance(verified_data, (bytes, bytearray, memoryview)):
            return False
        if self.MODE == "DMG" and mbc.GetName() == "MBC2":
            verified_data = bytearray(verified_data)
            for index in range(len(verified_data)):
                verified_data[index] &= 0x0F
                buffer[index] &= 0x0F

        args["path"] = path
        if self.MODE == "AGB" and self.INFO.get("ereader") is True:
            buffer[0xFF80:0x10000] = verified_data[0xFF80:0x10000]
            buffer[0x1FF80:0x20000] = verified_data[0xFF80:0x10000]

        if verified_data[:end_address] == buffer[:end_address]:
            return True

        differences = []
        difference_count = 0
        time_start = time.time()
        for index, (actual, expected) in enumerate(zip(verified_data, buffer[:end_address], strict=False)):
            if time.time() > time_start + 10:
                self.SetProgress(
                    {
                        "action": "ABORT",
                        "info_type": "msgbox_critical",
                        "info_msg": __("The save data was written completely, but didn't pass the verification check."),
                        "abortable": False,
                    },
                )
                return False
            if actual != expected:
                difference_count += 1
                if len(differences) < 10:
                    differences.append(f"- 0x{index:06X}: {actual:02X}≠{expected:02X}")
                elif len(differences) == 10:
                    differences.append("(" + __("more than 10 differences found") + ")")

        self.SetProgress(
            {
                "action": "ABORT",
                "info_type": "msgbox_critical",
                "info_msg": ___(
                    "The save data was written completely, but {count} byte ({percent}%) didn't pass the verification check.",
                    "The save data was written completely, but {count} bytes ({percent}%) didn't pass the verification check.",
                    n=difference_count,
                    count=difference_count,
                    percent=f"{difference_count / len(verified_data) * 100:.2f}",
                )
                + "\n\n"
                + "\n".join(differences),
                "abortable": False,
            },
        )
        return False

    def _ResetSaveTransferHardware(
        self,
        mbc: _SaveMapper,
        cart_type: dict[str, Any] | None,
        buffer: bytearray,
        audio_low: bool,
    ) -> None:
        """Restore mapper and device pins after a save transfer."""
        if self.MODE == "DMG":
            mbc.SelectBankRAM(0)
            mbc.EnableRAM(enable=False)
            self._set_fw_variable("DMG_READ_CS_PULSE", 0)
            if audio_low:
                self._set_fw_variable("FLASH_WE_PIN", 0x02)
                self.SetPin(["PIN_AUDIO"], set_high=True)
            self._write(
                self.DEVICE_CMD["SET_ADDR_AS_INPUTS"],
                wait=self.FW["fw_ver"] >= 12,
            )
        elif self.MODE == "AGB" and cart_type is not None and cart_type.get("flash_bank_select_type") == 1:
            self._cart_write(address=5, value=0, sram=True)
            self._cart_write(address=5, value=buffer[5], sram=True)

    def _finish_save_backup(
        self,
        args: dict[str, Any],
        mbc: _SaveMapper,
        cart_type: dict[str, Any] | None,
        buffer: bytearray,
        sram_5: int,
    ) -> tuple[bool, bool]:
        self.INFO["transferred"] = len(buffer)
        rtc_buffer = None
        if args["rtc"] is True:
            self.NO_PROG_UPDATE = True
            if self.MODE == "DMG" and mbc.HasRTC():
                mbc.LatchRTC()
                rtc_buffer = mbc.ReadRTC()
            elif self.MODE == "AGB":
                agb_gpio = AGB_GPIO(
                    args={"rtc": True},
                    cart_write_fncptr=self._cart_write,
                    cart_read_fncptr=self._mapper_cart_read,
                    cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
                    clk_toggle_fncptr=self._clk_toggle,
                )
                if self.FW["fw_ver"] >= 12:
                    self._write(self.DEVICE_CMD["AGB_READ_GPIO_RTC"])
                    rtc_buffer = self._read(8)
                    if rtc_buffer is not False and len(rtc_buffer) == 8 and agb_gpio.HasRTC(rtc_buffer) is True:
                        rtc_buffer = rtc_buffer[1:]
                        rtc_buffer.append(agb_gpio.RTCReadStatus())  # 24h mode = 0x40, reset flag = 0x80
                        rtc_buffer.extend(struct.pack("<Q", int(time.time())))
                    else:
                        rtc_buffer = None
                elif agb_gpio.HasRTC() is True:
                    rtc_buffer = agb_gpio.ReadRTC(buffer=rtc_buffer)
            self.NO_PROG_UPDATE = False
            if not isinstance(rtc_buffer, (bytes, bytearray, memoryview)):
                rtc_buffer = bytearray()
            elif not isinstance(rtc_buffer, bytearray):
                rtc_buffer = bytearray(rtc_buffer)
            self.SetProgress({"action": "UPDATE_POS", "pos": len(buffer) + len(rtc_buffer)})

        if self.MODE == "AGB" and cart_type is not None and cart_type.get("flash_bank_select_type") == 1:
            buffer[5] = sram_5

        if self.MODE == "DMG" and args["save_type"] == 0x204:
            for bank in range(8, 64):
                mbc.SelectBankROM(bank)
                buffer += self.ReadROM(0x4000, 0x4000)
                self.SetProgress({"action": "UPDATE_POS", "pos": len(buffer)})

        if args["path"] is not None:
            if self.MODE == "DMG" and mbc.GetName() == "MBC2":
                for index, value in enumerate(buffer):
                    buffer[index] = value & 0x0F
            with Path(args["path"]).open("wb") as file:
                file.write(buffer)
                if rtc_buffer is not None:
                    file.write(rtc_buffer)
        else:
            self.INFO["data"] = buffer

        self.INFO["file_crc32"] = zlib.crc32(buffer) & 0xFFFFFFFF
        self.INFO["file_sha1"] = hashlib.sha1(buffer).hexdigest()

        verification_only = "verify_write" in args and args["verify_write"] not in (None, False)
        verified = "verify_write" not in args or args["verify_write"] is False
        return verification_only, verified

    def _PrepareSaveTransferAction(
        self,
        args: dict[str, Any],
        parameters: _SaveTransferActionParameters,
    ) -> tuple[bytearray, int, int]:
        mbc, save_size, empty_data_byte, ram_banks, extra_size = parameters
        if args["mode"] == 2:  # Backup
            action = "SAVE_READ"
            buffer = bytearray()
            if self.MODE == "DMG" and args["save_type"] == 0x204:  # Unlicensed PHOTO!
                ram_banks = 16

        elif args["mode"] == 3:  # Restore
            action = "SAVE_WRITE"
            self.INFO["save_erase"] = args["erase"]
            if args["erase"]:
                buffer = bytearray([empty_data_byte] * save_size)
                if self.MODE == "DMG" and mbc is not None and mbc.GetName() == "Xploder GB":
                    buffer[0] = 0x00
                elif self.MODE == "DMG" and mbc is not None and mbc.GetName() == "MAC-GBD":
                    buffer[0x11B2:0x11D7] = bytearray.fromhex(
                        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF4D616769631115",
                    )
                    buffer[0x11D7:0x11FC] = buffer[0x11B2:0x11D7]
            else:
                if args["path"] is None:
                    source_buffer = args["buffer"] if "buffer" in args else self.INFO["data"]
                    if not isinstance(source_buffer, (bytes, bytearray, memoryview)):
                        msg = "Save data must be a bytes-like object"
                        raise TypeError(msg)
                    buffer = source_buffer if isinstance(source_buffer, bytearray) else bytearray(source_buffer)
                else:
                    with Path(args["path"]).open("rb") as file:
                        buffer = bytearray(file.read())

                if self.MODE == "DMG" and args["save_type"] == 0x204:  # Unlicensed PHOTO!
                    ram_banks = 16
                    if len(buffer) <= 0x20000:
                        save_size = len(buffer)
                        args["save_type"] = 0x04

                # Fill too small file
                if not (self.MODE == "AGB" and args["save_type"] == 6):  # Not DACS
                    while len(buffer) < save_size:
                        buffer += bytearray(buffer)

        else:
            msg = f"Unsupported save transfer mode: {args['mode']!r}"
            raise ValueError(msg)

        if not (args["mode"] == 2 and "verify_write" in args and args["verify_write"]):
            self.INFO["action"] = self.ACTIONS[action]
            self.SetProgress(
                {
                    "action": "INITIALIZE",
                    "method": action,
                    "size": save_size + extra_size,
                },
            )
        return buffer, ram_banks, save_size

    def _FinishSaveRestore(
        self,
        args: dict[str, Any],
        mbc: _SaveMapper,
        buffer: bytearray,
        buffer_offset: int,
    ) -> bool | None:
        self.INFO["transferred"] = len(buffer)
        if args["rtc"] is True:
            advance = "rtc_advance" in args and args["rtc_advance"]
            self.SetProgress({"action": "UPDATE_RTC", "method": "write"})
            if self.MODE == "DMG":
                if "erase" in args and args["erase"] is True:
                    buffer += bytearray([0] * mbc.GetRTCBufferSize())
                mbc.WriteRTC(buffer[-mbc.GetRTCBufferSize() :], advance=advance)
            elif self.MODE == "AGB":
                if "erase" in args and args["erase"] is True:
                    buffer += bytearray([0xFF] * 0x10)
                agb_gpio = AGB_GPIO(
                    args={"rtc": True},
                    cart_write_fncptr=self._cart_write,
                    cart_read_fncptr=self._mapper_cart_read,
                    cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
                    clk_toggle_fncptr=self._clk_toggle,
                )
                agb_gpio.WriteRTC(buffer[-0x10:], advance=advance)

        if (
            self.MODE == "DMG"
            and args["save_type"] == 0x204
            and "cart_type" in args
            and args["cart_type"] != -1
            and len(buffer) > 0x20000
            and args["cart_type"] is not None
        ):
            self._FlashROM(
                {
                    "buffer": buffer[0x20000:0x100000],
                    "path": "",
                    "cart_type": args["cart_type"],
                    "override_voltage": False,
                    "prefer_chip_erase": False,
                    "fast_read_mode": True,
                    "verify_write": False,
                    "fix_header": False,
                    "fix_bootlogo": False,
                    "mbc": 252,
                    "bl_offset": 0x20000,
                    "bl_size": 0xE0000,
                    "bl_layout": 0,
                    "bl_save": True,
                    "flash_offset": 0x20000,
                    "flash_size": 0xE0000,
                    "mode": 4,
                    "photo_mode": True,
                },
            )
            args["verify_write"] = False

        self.SetProgress({"action": "UPDATE_POS", "pos": len(buffer), "force_update": True})
        if "verify_write" in args and args["verify_write"] is True and args["erase"] is not True:
            return self._VerifySaveWrite(args, mbc, buffer, buffer_offset)
        return True

    def _ReadSaveChunk(
        self,
        parameters: _SaveReadParameters,
    ) -> bytearray:
        args, mbc, bank, pos, buffer_len, command, max_length = parameters
        if self.MODE == "DMG" and mbc.GetName() == "MBC7":
            return self.ReadRAM_MBC7(address=pos, length=buffer_len)
        if self.MODE == "DMG" and mbc.GetName() == "MBC6" and bank > 7:  # MBC6 flash save memory
            return self.ReadROM(address=pos, length=buffer_len, skip_init=False, max_length=max_length)
        if self.MODE == "DMG" and mbc.GetName() == "TAMA5":
            return self.ReadRAM_TAMA5()
        if self.MODE == "DMG" and mbc.GetName() == "Xploder GB":
            return self.ReadROM(
                address=0x20000 + pos,
                length=buffer_len,
                skip_init=False,
                max_length=max_length,
            )
        if self.MODE == "AGB" and args["save_type"] in (1, 2):  # EEPROM
            return self.ReadRAM(
                address=int(pos / 8),
                length=buffer_len,
                command=command,
                max_length=max_length,
            )
        if self.MODE == "AGB" and args["save_type"] == 6:  # DACS
            return self.ReadROM(
                address=0x1F00000 + pos,
                length=buffer_len,
                skip_init=False,
                max_length=max_length,
            )

        data = self.ReadRAM(address=pos, length=buffer_len, command=command, max_length=max_length)
        if self.MODE == "DMG" and mbc.GetName() == "MBC2":
            for index, value in enumerate(data):
                data[index] = value & 0x0F
        return data

    def _PrepareSaveBank(self, context: _SaveBankContext) -> _SaveBankRange:
        args, mbc, bank, save_size, buffer_length, buffer_offset, agb_flash_chip = context
        if self.MODE == "DMG":
            if mbc.GetName() == "MBC6" and bank > 7:
                self._set_fw_variable("DMG_ROM_BANK", bank - 8)
                start_address, bank_size = mbc.SelectBankFlash(bank - 8)
                end_address = start_address + bank_size
                buffer_length = 0x2000
                if args["mode"] == 3 and (buffer_offset - 0x8000) % 0x20000 == 0:
                    dprint(f"Erasing flash sector at position 0x{buffer_offset:X}")
                    mbc.EraseFlashSector()
            elif mbc.GetName() == "Xploder GB":
                self._set_fw_variable("DMG_ROM_BANK", bank + 8)
                start_address, bank_size = mbc.SelectBankRAM(bank)
                end_address = min(save_size, start_address + bank_size)
            else:
                self._set_fw_variable("DMG_WRITE_CS_PULSE", 1 if mbc.WriteWithCSPulse() else 0)
                start_address, bank_size = mbc.SelectBankRAM(bank)
                end_address = min(save_size, start_address + bank_size)
            return _SaveBankRange(start_address, end_address, buffer_length)

        start_address = 0
        bank_size = 0x10000
        if args["save_type"] == 6:  # DACS
            bank_size = min(save_size, 0x100000)
            buffer_length = 0x2000
        end_address = min(save_size, bank_size)
        if save_size <= bank_size:
            return _SaveBankRange(start_address, end_address, buffer_length)

        if args["save_type"] == 8 or agb_flash_chip in (0xBF4B, 0xBF5B, 0xFFFF, 0xBF6D):
            dprint(f"Switching to bootleg save bank {bank:d}")
            self._cart_write(0x1000000, bank)
        elif args["save_type"] == 5:
            dprint(f"Switching to FLASH bank {bank:d}")
            self._cart_write_flash(
                [
                    [0x5555, 0xAA],
                    [0x2AAA, 0x55],
                    [0x5555, 0xB0],
                    [0, bank],
                ],
            )
        else:
            dprint("Unknown bank switching method")
        time.sleep(0.05)
        return _SaveBankRange(start_address, end_address, buffer_length)

    def _AbortSaveTransferIfCanceled(self) -> bool:
        if not self.CANCEL:
            return False
        cancel_args = {"action": "ABORT", "abortable": False}
        cancel_args.update(self.CANCEL_ARGS)
        self.CANCEL_ARGS = {}
        self.ERROR_ARGS = {}
        self.SetProgress(cancel_args)
        if self.CanPowerCycleCart():
            self.CartPowerCycle()
        return True

    def _WriteSaveChunk(self, parameters: _SaveWriteParameters) -> bool:
        args, mbc, bank, pos, buffer, buffer_offset, buffer_len, command, agb_flash_chip = parameters
        chunk = buffer[buffer_offset : buffer_offset + buffer_len]
        if self.MODE == "DMG" and mbc.GetName() == "MBC7":
            self.WriteEEPROM_MBC7(address=pos, buffer=chunk)
        elif self.MODE == "DMG" and mbc.GetName() == "MBC6" and bank > 7:  # MBC6 flash save memory
            if self.FW["fw_ver"] > 1:
                self.WriteROM(address=pos, buffer=chunk)
                self._cart_write(pos + buffer_len - 1, 0xF0)
            else:
                self.WriteFlash_MBC6(address=pos, buffer=chunk, mapper=mbc)
        elif self.MODE == "DMG" and mbc.GetName() == "TAMA5":
            self.WriteRAM_TAMA5(buffer=chunk)
        elif self.MODE == "DMG" and mbc.GetName() == "Xploder GB":
            self.WriteROM_DMG_EEPROM(address=pos, buffer=chunk, bank=bank + 8)
        elif self.MODE == "AGB" and args["save_type"] in (1, 2):  # EEPROM
            self.WriteRAM(address=int(pos / 8), buffer=chunk, command=command)
        elif self.MODE == "AGB" and args["save_type"] in (4, 5):  # FLASH
            sector_address = pos % 0x10000
            if agb_flash_chip == 0x1F3D:  # Atmel AT29LV512
                self.WriteRAM(address=int(pos / 128), buffer=chunk, command=command)
            else:
                dprint(f"Erasing flash save sector; pos=0x{pos:X}, sector_address=0x{sector_address:X}")
                commands = [
                    [0x5555, 0xAA],
                    [0x2AAA, 0x55],
                    [0x5555, 0x80],
                    [0x5555, 0xAA],
                    [0x2AAA, 0x55],
                    [sector_address, 0x30],
                ]
                self._cart_write_flash(commands)
                status_register = 0
                lives = 50
                while True:
                    time.sleep(0.01)
                    status_register = self._cart_read(sector_address, 2, agb_save_flash=True)
                    if isinstance(status_register, bytearray) and len(status_register) == 2:
                        status_register = struct.unpack(">H", status_register)[0]
                    elif not isinstance(status_register, int):
                        status_register = 0
                    dprint(
                        f"Data Check: 0x{status_register:X} == 0xFFFF? {status_register == 0xFFFF!s:s}",
                    )
                    if status_register == 0xFFFF:
                        break
                    lives -= 1
                    if lives == 0:
                        errmsg = __(
                            "Error: Save data flash sector at {address} didn't erase successfully (SR={status_register}).",
                            address=f"0x{bank * 0x10000 + pos:X}",
                            status_register=f"0x{status_register:04X}",
                        )
                        print(ANSI.RED + errmsg + ANSI.RESET)
                        break
                if chunk != bytearray([0xFF] * buffer_len):
                    if "ereader" in self.INFO and self.INFO["ereader"] is True and sector_address == 0xF000:
                        self.WriteRAM(
                            address=pos,
                            buffer=buffer[buffer_offset : buffer_offset + 0xF80],
                            command=command,
                            max_length=0x80,
                        )
                    else:
                        self.WriteRAM(address=pos, buffer=chunk, command=command)
        elif self.MODE == "AGB" and args["save_type"] == 6:  # DACS
            sector_address = pos + 0x1F00000
            if not self._WriteDACSSaveChunk(sector_address, pos, chunk):
                return False
        else:
            self.WriteRAM(address=pos, buffer=chunk, command=command)
        return True

    def _BackupRestoreRAM_Worker(self, args: dict[str, Any]) -> bool | None:
        mode = self._require_cartridge_mode("accessing save data")
        self.FAST_READ = False
        if "rtc" not in args:
            args["rtc"] = False

        # Prepare some stuff
        command: Any = None
        empty_data_byte = 0x00
        extra_size = 0
        audio_low = False

        # Initialization
        ram_banks = 0
        buffer_len = 0
        sram_5 = 0
        temp: Any = None
        buffer = bytearray()
        save_size = 0
        agb_flash_chip = 0
        start_address = 0
        end_address = 0
        _mbc: Any = None

        cart_type = self._prepare_save_cart_type(args, mode)

        self._set_fw_variable("STATUS_REGISTER_MASK", 0x80)
        self._set_fw_variable("STATUS_REGISTER_VALUE", 0x80)

        if self.MODE == "DMG":
            configuration = self._configure_dmg_save_transfer(args)
            if configuration is None:
                return False
            (
                _mbc,
                buffer_len,
                save_size,
                ram_banks,
                empty_data_byte,
                extra_size,
                audio_low,
            ) = configuration

        elif self.MODE == "AGB":
            configuration = self._configure_agb_save_transfer(args, cart_type)
            if configuration is None:
                return False
            (
                buffer_len,
                save_size,
                ram_banks,
                agb_flash_chip,
                sram_5,
                command,
                empty_data_byte,
                extra_size,
            ) = configuration

        transfer_parameters = _SaveTransferActionParameters(
            mbc=_mbc,
            save_size=save_size,
            empty_data_byte=empty_data_byte,
            ram_banks=ram_banks,
            extra_size=extra_size,
        )
        buffer, ram_banks, save_size = self._PrepareSaveTransferAction(args, transfer_parameters)

        # Main loop
        buffer_offset = 0
        max_length = 64 if self.FW["pcb_name"] in ("GBxCart RW", "") else self.MAX_BUFFER_READ
        for bank in range(ram_banks):
            start_address, end_address, buffer_len = self._PrepareSaveBank(
                _SaveBankContext(
                    args,
                    _mbc,
                    bank,
                    save_size,
                    buffer_len,
                    buffer_offset,
                    agb_flash_chip,
                ),
            )

            dprint(
                f"start_address=0x{start_address:X}, end_address=0x{end_address:X}, buffer_len=0x{buffer_len:X}, buffer_offset=0x{buffer_offset:X}",
            )
            pos = start_address
            while pos < end_address:
                if self._AbortSaveTransferIfCanceled():
                    return None

                if args["mode"] == 2:  # Backup
                    in_temp = [bytearray(), bytearray()]
                    xe = 2 if args.get("verify_read") else 1  # Read twice for detecting instabilities
                    _read_failed = False
                    for x in range(xe):
                        if x == 1:
                            self.NO_PROG_UPDATE = True
                        else:
                            self.NO_PROG_UPDATE = False

                        in_temp[x] = self._ReadSaveChunk(
                            _SaveReadParameters(args, _mbc, bank, pos, buffer_len, command, max_length),
                        )

                        if len(in_temp[x]) != buffer_len:
                            if (max_length >> 1) < 64:
                                dprint(
                                    f"Received 0x{len(in_temp[x]):X} bytes instead of 0x{buffer_len:X} bytes from the device at position 0x{len(buffer):X}!",
                                )
                                max_length = 64
                            else:
                                dprint(
                                    f"Received 0x{len(in_temp[x]):X} bytes instead of 0x{buffer_len:X} bytes from the device at position 0x{len(buffer):X}! Decreasing maximum transfer buffer length to 0x{max_length >> 1:X}.",
                                )
                                max_length >>= 1
                                _read_failed = True
                            device = self._serial_device()
                            device.reset_input_buffer()
                            device.reset_output_buffer()
                            if _read_failed:
                                break

                    if _read_failed:
                        continue

                    if xe == 2 and in_temp[0] != in_temp[1]:
                        print(ANSI.RED + __("Error: Inconsistent reads detected.") + ANSI.RESET)
                        print(f"- Read #1: {in_temp[0].hex()}")
                        print(f"- Read #2: {in_temp[1].hex()}")
                        if not args.get("detect"):
                            self.SetProgress(
                                {
                                    "action": "ABORT",
                                    "info_type": "msgbox_critical",
                                    "info_msg": __(
                                        "Failed to read save data consistently. Please ensure that the cartridge contacts are clean.",
                                    ),
                                    "abortable": False,
                                },
                            )
                        return False

                    temp = in_temp[0]
                    buffer += temp
                    self.SetProgress({"action": "UPDATE_POS", "pos": len(buffer)})

                elif args["mode"] == 3:  # Restore
                    write_parameters = _SaveWriteParameters(
                        args,
                        _mbc,
                        bank,
                        pos,
                        buffer,
                        buffer_offset,
                        buffer_len,
                        command,
                        agb_flash_chip,
                    )
                    if not self._WriteSaveChunk(write_parameters):
                        return False
                    self.SetProgress({"action": "UPDATE_POS", "pos": buffer_offset + buffer_len})

                pos += buffer_len
                buffer_offset += buffer_len

        verified = False
        if args["mode"] == 2:  # Backup
            verification_only, verified = self._finish_save_backup(args, _mbc, cart_type, buffer, sram_5)
            if verification_only:
                return True

        elif args["mode"] == 3:  # Restore
            verification_result = self._FinishSaveRestore(args, _mbc, buffer, buffer_offset)
            if verification_result is not True:
                return verification_result
            verified = verification_result

        self._ResetSaveTransferHardware(_mbc, cart_type, buffer, audio_low)

        # Clean up
        self.INFO["last_action"] = self.INFO["action"]
        self.INFO["action"] = None
        self.INFO["last_path"] = args["path"]
        self._thread_worker_auto_poweroff_finish()
        self.SetProgress({"action": "FINISHED", "verified": verified})
        return True

    def _FlashROM(self, args: dict[str, Any]) -> bool | None:
        self._thread_worker_auto_poweroff_start()
        try:
            return self._FlashROM_Worker(args)
        finally:
            self._thread_worker_auto_poweroff_finish()

    @staticmethod
    def _create_flashcart(cart_type: dict[str, Any], callbacks: FlashcartCallbacks) -> Flashcart:
        match cart_type["command_set"]:
            case "GBMEMORY":
                return Flashcart_DMG_MMSA(config=cart_type, fncptr=callbacks)
            case "GBAMP":
                return Flashcart_AGB_GBAMP(config=cart_type, fncptr=callbacks)
            case "BUNG_16M":
                return Flashcart_DMG_BUNG_16M(config=cart_type, fncptr=callbacks)
            case _:
                return Flashcart(config=cart_type, fncptr=callbacks)

    def _configure_flash_command_set(self, command_set_type: str) -> int | None:
        match command_set_type:
            case "AMD":
                dprint("Using AMD command set")
                return 0x01
            case "INTEL":
                self._set_fw_variable("FLASH_SHARP_VERIFY_SR", 0)
                dprint("Using Intel command set")
                return 0x02
            case "SHARP":
                self._set_fw_variable("FLASH_SHARP_VERIFY_SR", 1)
                dprint("Using Sharp/Intel command set")
                return 0x02
            case "GBMEMORY" | "DMG-MBC5-32M-FLASH":
                dprint("Using GB-Memory command set")
                return 0x00
            case "BLAZE_XPLODER" | "DATEL_ORBITV2" | "EEPROM" | "GBAMP" | "BUNG_16M":
                return 0x00
            case _:
                self.SetProgress(
                    {
                        "action": "ABORT",
                        "info_type": "msgbox_critical",
                        "info_msg": __("This flashcart profile is currently not supported for ROM writing."),
                        "abortable": False,
                    },
                )
                return None

    def _configure_flash_write_pin(self, flashcart: Flashcart) -> int:
        write_enable = 0
        if flashcart.WEisWR():
            write_enable = 0x01  # FLASH_WE_PIN_WR
            self._write(write_enable)
            dprint("Using WR as WE")
        elif flashcart.WEisAUDIO():
            write_enable = 0x02  # FLASH_WE_PIN_AUDIO
            self._write(write_enable)
            dprint("Using AUDIO as WE")
        elif flashcart.WEisWR_RESET():
            write_enable = 0x03  # FLASH_WE_PIN_WR_RESET
            self._write(write_enable)
            dprint("Using WR+RESET as WE")
        else:
            self._write(write_enable)  # unset
        return write_enable

    def _send_flash_commands(self, flash_cmds: Sequence[Sequence[int | str | None]]) -> None:
        for i in range(6):
            if i >= len(flash_cmds):
                self._write(bytearray(struct.pack(">I", 0)) + bytearray(struct.pack(">H", 0)))
                continue

            address = flash_cmds[i][0]
            value = flash_cmds[i][1]
            if not isinstance(address, int):
                address = 0
            if not isinstance(value, int):
                value = 0
            if self.MODE == "AGB":
                address >>= 1
            dprint(f"Setting command #{i:d} to 0x{address:X}=0x{value:X}")
            self._write(bytearray(struct.pack(">I", address)) + bytearray(struct.pack(">H", value)))

        if self.FW["fw_ver"] >= 12:
            self.wait_for_ack()

    def _load_flash_commands(
        self,
        cart_type: dict[str, Any],
        flashcart: Flashcart,
        flash_buffer_size: int | Literal[False],
    ) -> tuple[str, int] | None:
        flash_cmds: list[list[int | str | None]] = []
        command_set_type = flashcart.GetCommandSetType()
        flash_command_set = self._configure_flash_command_set(command_set_type)
        if flash_command_set is None:
            return None

        self._set_fw_variable("FLASH_DOUBLE_DIE", int(flashcart.HasDoubleDie() and self.FW["fw_ver"] >= 5))

        we = 0x00
        if command_set_type == "GBMEMORY" and self.FW["fw_ver"] < 2:
            self._set_fw_variable("FLASH_WE_PIN", 0x01)
            dprint("Using legacy GB-Memory mode")
        elif command_set_type == "DMG-MBC5-32M-FLASH" and self.FW["fw_ver"] < 12:
            self._set_fw_variable("FLASH_WE_PIN", 0x02)
        else:
            self._write(self.DEVICE_CMD["SET_FLASH_CMD"])
            self._write(flash_command_set)

            if flashcart.IsF2A():
                self._write(0x05)  # FLASH_METHOD_AGB_FLASH2ADVANCE
                flash_cmds = [
                    ["SA", 0xE8],
                    ["SA", "BS"],
                    ["PA", "PD"],
                    ["SA", 0xD0],
                    ["SA", 0xFF],
                ]
                dprint(f"Using Flash2Advance mode with a buffer of {flash_buffer_size:d} bytes")
            elif command_set_type == "GBMEMORY" and self.FW["fw_ver"] >= 2:
                self._write(0x03)  # FLASH_METHOD_DMG_MMSA
                dprint("Using GB-Memory mode")
            elif command_set_type == "DATEL_ORBITV2" and self.FW["fw_ver"] >= 12:
                self._write(0x09)  # FLASH_METHOD_DMG_DATEL_ORBITV2
                dprint("Using Datel Orbit V2 mode")
            elif command_set_type == "DMG-MBC5-32M-FLASH" and self.FW["fw_ver"] >= 12:
                self._write(0x0A)  # FLASH_METHOD_DMG_E201264
                dprint("Using E201264 mode")
            elif command_set_type == "GBAMP" and self.FW["fw_ver"] >= 12:
                self._write(0x0B)  # FLASH_METHOD_AGB_GBAMP
                dprint("Using GBAMP mode")
            elif command_set_type == "BUNG_16M" and self.FW["fw_ver"] >= 12:
                self._write(0x0C)  # FLASH_METHOD_DMG_BUNG_16M
                dprint("Using BUNG Doctor GB Card 16M mode")
            elif flashcart.SupportsBufferWrite() and flash_buffer_size > 0:
                self._write(0x02)  # FLASH_METHOD_BUFFERED
                flash_cmds = flashcart.GetCommands("buffer_write")
                dprint(f"Using buffered writing with a buffer of {flash_buffer_size:d} bytes")
            elif flashcart.SupportsPageWrite() and flash_buffer_size > 0 and self.FW["fw_ver"] >= 12:
                self._write(0x08)  # FLASH_METHOD_PAGED
                flash_cmds = flashcart.GetCommands("page_write")
                dprint(f"Using paged writing with a buffer of {flash_buffer_size:d} bytes")
            elif flashcart.SupportsSingleWrite():
                self._write(0x01)  # FLASH_METHOD_UNBUFFERED
                flash_cmds = flashcart.GetCommands("single_write")
                dprint("Using single writing")
            else:
                self.SetProgress(
                    {
                        "action": "ABORT",
                        "info_type": "msgbox_critical",
                        "info_msg": __("This flashcart profile is currently not supported for ROM writing."),
                        "abortable": False,
                    },
                )
                return None

            we = self._configure_flash_write_pin(flashcart)
            self._send_flash_commands(flash_cmds)

            if self.FW["fw_ver"] >= 6:
                if "flash_commands_on_bank_1" in cart_type and cart_type["flash_commands_on_bank_1"] is True:
                    self._set_fw_variable("FLASH_COMMANDS_BANK_1", 1)
                    self._write(self.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"])
                    if "bank_switch" in cart_type["commands"]:
                        self._write(len(cart_type["commands"]["bank_switch"]))  # number of commands
                        for command in cart_type["commands"]["bank_switch"]:
                            address = command[0]
                            value = command[1]
                            if value == "ID":
                                self._write(bytearray(struct.pack(">I", address)))  # address
                                self._write(0)  # type = address
                            else:
                                self._write(bytearray(struct.pack(">I", value)))  # value
                                self._write(1)  # type = value
                        ret = self._read(1)
                        if ret != 0x01:
                            print("Error in DMG_SET_BANK_CHANGE_CMD:", ret)
                    else:
                        self._write(0, wait=True)
                else:
                    self._set_fw_variable("FLASH_COMMANDS_BANK_1", 0)
                    self._write(self.DEVICE_CMD["DMG_SET_BANK_CHANGE_CMD"])
                    self._write(0, wait=True)
            elif "flash_commands_on_bank_1" in cart_type and cart_type["flash_commands_on_bank_1"] is True:
                self._set_fw_variable("FLASH_COMMANDS_BANK_1", 1)
            else:
                self._set_fw_variable("FLASH_COMMANDS_BANK_1", 0)

            if self.FW["fw_ver"] >= 12:
                if "status_register_mask" in cart_type:
                    self._set_fw_variable("STATUS_REGISTER_MASK", cart_type["status_register_mask"])
                    self._set_fw_variable("STATUS_REGISTER_VALUE", cart_type["status_register_value"])
                else:
                    self._set_fw_variable("STATUS_REGISTER_MASK", 0x80)
                    self._set_fw_variable("STATUS_REGISTER_VALUE", 0x80)

        if self.FW["fw_ver"] >= 12:
            self._set_fw_variable("AGB_IRQ_ENABLED", 1 if "set_irq_high" in cart_type else 0)

        return command_set_type, we

    def _prepare_flash_data(self, args: dict[str, Any], mode: DeviceMode) -> tuple[bytearray, int]:
        if "buffer" in args:
            source_buffer = args["buffer"]
            if not isinstance(source_buffer, (bytes, bytearray, memoryview)):
                msg = "ROM data must be a bytes-like object"
                raise TypeError(msg)
            data_import = source_buffer if isinstance(source_buffer, bytearray) else bytearray(source_buffer)
        else:
            with Path(args["path"]).open("rb") as file:
                data_import = bytearray(file.read())

        flash_offset = args.get("flash_offset", 0)  # Batteryless SRAM or Transfer Resume
        if "start_addr" in args and args["start_addr"] > 0:
            data_import = bytearray(b"\xff" * args["start_addr"]) + data_import

        # Batteryless SRAM
        if "bl_layout" in args and args["bl_layout"] in (1, 2):
            args["bl_size"] <<= 1
            args["flash_size"] <<= 1
            bl_data_import = bytearray(b"\xff" * args["bl_size"])

            if args["bl_layout"] == 1:
                for i in range(args["bl_size"] // 0x4000):
                    bl_data_import[i * 0x4000 : i * 0x4000 + 0x2000] = data_import[i * 0x2000 : i * 0x2000 + 0x2000]
            elif args["bl_layout"] == 2:
                for i in range(args["bl_size"] // 0x4000):
                    bl_data_import[i * 0x4000 + 0x2000 : i * 0x4000 + 0x2000 + 0x2000] = data_import[
                        i * 0x2000 : i * 0x2000 + 0x2000
                    ]
            data_import = bl_data_import

        # Pad data
        if data_import:
            if len(data_import) < 0x400:
                data_import += bytearray([0xFF] * (0x400 - len(data_import)))
            if len(data_import) % 0x4000 > 0:
                data_import += bytearray([0xFF] * (0x4000 - len(data_import) % 0x4000))

            # Skip writing the last 256 bytes of 32 MiB ROMs with EEPROM save type
            if mode == "AGB" and len(data_import) == 0x2000000:
                temp_ver = "N/A"
                try:
                    ids = (
                        b"SRAM_",
                        b"EEPROM_V",
                        b"FLASH_V",
                        b"FLASH512_V",
                        b"FLASH1M_V",
                        b"AGB_8MDACS_DL_V",
                    )
                    for ident in ids:
                        temp_pos = data_import.find(ident)
                        if temp_pos > 0:
                            version_bytes = data_import[temp_pos : temp_pos + 0x20]
                            temp_ver = version_bytes[: version_bytes.index(0x00)].decode("ascii", "replace")
                            break
                except ValueError:
                    temp_ver = "N/A"
                if "EEPROM" in temp_ver:
                    print(
                        ANSI.YELLOW
                        + __(
                            "Note: The last 256 bytes of this 32 MiB ROM will not be written as this area is reserved by the EEPROM save type.",
                        )
                        + ANSI.RESET,
                    )
                    data_import = data_import[:0x1FFFF00]

        # Fix bootlogo and header
        if "fix_bootlogo" in args and isinstance(args["fix_bootlogo"], bytearray):
            dstr = "".join(format(x, "02X") for x in args["fix_bootlogo"])
            dprint("Replacing bootlogo data with", dstr)
            if mode == "DMG":
                data_import[0x104:0x134] = args["fix_bootlogo"]
            else:
                data_import[0x04:0xA0] = args["fix_bootlogo"]
        if args.get("fix_header"):
            dprint("Fixing header checksums")
            header = (
                RomFileDMG(data_import[0:0x200]).FixHeader()
                if mode == "DMG"
                else RomFileAGB(data_import[0:0x200]).FixHeader()
            )
            if header is not None:
                data_import[0:0x200] = header

        return data_import, flash_offset

    def _check_flashcart_firmware(self, cart_type: dict[str, Any]) -> bool:
        if (
            cart_type["type"] == "DMG"
            and "write_pin" in cart_type
            and cart_type["write_pin"] == "WR+RESET"
            and self.FW["fw_ver"] < 2
        ) or (
            self.FW["fw_ver"] < 2
            and ("pulse_reset_after_write" in cart_type and cart_type["pulse_reset_after_write"] is True)
        ):
            self.SetProgress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": __("This flashcart profile requires at least firmware version L2."),
                    "abortable": False,
                },
            )
            return False

        if (
            self.FW["fw_ver"] < 3
            and ("command_set" in cart_type and cart_type["command_set"] == "SHARP")
            and ("buffer_write" in cart_type["commands"])
        ):
            print(
                ANSI.YELLOW
                + __(
                    "Note: Update your {device_name} firmware to version L3 or higher for a better transfer rate with this flashcart profile.",
                    device_name=self.DEVICE_NAME,
                )
                + ANSI.RESET,
            )
            del cart_type["commands"]["buffer_write"]

        if self.FW["fw_ver"] < 5 and (
            "flash_commands_on_bank_1" in cart_type and cart_type["flash_commands_on_bank_1"] is True
        ):
            self.SetProgress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": __("This flashcart profile requires at least firmware version L5."),
                    "abortable": False,
                },
            )
            return False
        if self.FW["fw_ver"] < 5 and ("double_die" in cart_type and cart_type["double_die"] is True):
            print(
                ANSI.YELLOW
                + __(
                    "Note: Update your {device_name} firmware to version L5 or higher for a better transfer rate with this flashcart profile.",
                    device_name=self.DEVICE_NAME,
                )
                + ANSI.RESET,
            )
            del cart_type["commands"]["buffer_write"]

        if self.FW["fw_ver"] < 8 and "enable_pullups" in cart_type and cart_type["enable_pullups"] is True:
            print(
                ANSI.YELLOW
                + __(
                    "Note: This flashcart profile may not be fully compatible with your {device_name} running an old or legacy firmware version.",
                    device_name=self.GetName(),
                )
                + ANSI.RESET,
            )
            del cart_type["enable_pullups"]

        if self.FW["fw_ver"] < 12 and "set_irq_high" in cart_type and cart_type["set_irq_high"] is True:
            print(
                ANSI.YELLOW
                + __(
                    "Note: This flashcart profile may not be fully compatible with your {device_name} until updated to a newer firmware version.",
                    device_name=self.GetName(),
                )
                + ANSI.RESET,
            )
            del cart_type["set_irq_high"]
        if self.FW["fw_ver"] < 12 and "status_register_mask" in cart_type:
            if self.FW["pcb_name"] in ("GBxCart RW", ""):
                message = __(
                    "This flashcart profile requires a different firmware version. Please update your GBxCart RW firmware using the older FlashGBX v4.3 and try again.",
                )
            else:
                message = __("This flashcart profile requires at least firmware version L12.")
            self.SetProgress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": message,
                    "abortable": False,
                },
            )
            return False

        if self.FW["fw_ver"] < 14 and "set_audio_high" in cart_type and cart_type["set_audio_high"] is True:
            self.SetProgress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": __("This flashcart profile requires at least firmware version L14."),
                    "abortable": False,
                },
            )
            return False
        return True

    def _set_flashcart_profile_index(
        self,
        cart_type: dict[str, Any],
        mode: DeviceMode,
        selected_index: int,
    ) -> None:
        cart_type["_index"] = 0
        profile_names = list(self.SUPPORTED_CARTS[mode])
        if not 0 <= selected_index < len(profile_names):
            return
        try:
            cart_type["_index"] = cart_type["names"].index(profile_names[selected_index])
        except Exception:
            logger.exception("Failed to resolve the selected flash-cart profile index")

    def _set_flash_voltage(self, args: dict[str, Any], flashcart: Flashcart) -> float:
        if args["override_voltage"] is not False:
            active_voltage = args["override_voltage"]
            if active_voltage == 5:
                self._write(self.DEVICE_CMD["SET_VOLTAGE_5V"], wait=self.FW["fw_ver"] >= 12)
            else:
                self._write(self.DEVICE_CMD["SET_VOLTAGE_3_3V"], wait=self.FW["fw_ver"] >= 12)
        elif flashcart.GetVoltage() == 3.3:
            active_voltage = 3.3
            self._write(self.DEVICE_CMD["SET_VOLTAGE_3_3V"], wait=self.FW["fw_ver"] >= 12)
        elif flashcart.GetVoltage() == 5:
            active_voltage = 5
            self._write(self.DEVICE_CMD["SET_VOLTAGE_5V"], wait=self.FW["fw_ver"] >= 12)
        else:
            active_voltage = flashcart.GetVoltage()

        # Voltage-locked devices ignore the SET_VOLTAGE_* writes above; their
        # slot voltage is determined by the platform mode.
        if self.CanSetVoltageByAutoswitch() and not self.CanSetVoltageByCode():
            return 5 if self.MODE == "DMG" else 3.3
        return active_voltage

    @staticmethod
    def _pad_flash_data_for_sector_erase(args: dict[str, Any], flashcart: Flashcart, data_import: bytearray) -> None:
        if flashcart.SupportsChipErase() or not flashcart.SupportsSectorErase() or not args["prefer_chip_erase"]:
            return

        print(
            ANSI.YELLOW
            + __("Note: Chip erase mode is not supported for this flashcart profile. Sector erase mode will be used.")
            + "\n"
            + ANSI.RESET,
        )
        if data_import != bytearray([0xFF] * len(data_import)):
            return

        flash_size = flashcart.GetFlashSize()
        if flash_size is False or len(data_import) >= flash_size:
            return

        pad_len = flash_size - len(data_import)
        while pad_len > 0x2000000:
            data_import += bytearray([0xFF] * 0x2000000)
            pad_len -= 0x2000000
        data_import += bytearray([0xFF] * (flash_size - len(data_import)))

    def _AbortFlashVerification(self) -> None:
        cancel_args = {"action": "ABORT", "abortable": False}
        cancel_args.update(self.CANCEL_ARGS)
        self.CANCEL_ARGS = {}
        self.ERROR_ARGS = {}
        self.SetProgress(cancel_args)
        if self.CanPowerCycleCart():
            self.CartPowerCycle()
        if self.FW["fw_ver"] >= 12:
            self._set_fw_variable("AGB_IRQ_ENABLED", 0)

    def _StoreFlashVerificationErrors(
        self,
        context: _FlashVerificationContext,
        broken_sectors: list[list[int]],
    ) -> bool:
        if not broken_sectors:
            return False
        self.INFO["broken_sectors"] = broken_sectors
        self.INFO["verify_error_params"] = {"rom_size": len(context.data_import)}
        if self.MODE == "DMG":
            self.INFO["verify_error_params"]["mapper_name"] = context.mbc.GetName()
            self.INFO["verify_error_params"]["mapper_selection_type"] = (
                1 if context.flashcart.GetMBC() == "manual" else 2
            )
            self.INFO["verify_error_params"]["mapper_max_size"] = context.mbc.GetMaxROMSize()
        return True

    def _verify_flash_write(self, context: _FlashVerificationContext) -> bool | None:
        args = context.args
        cart_type = context.cart_type
        flashcart = context.flashcart
        data_import = context.data_import
        flash_offset = context.flash_offset
        active_voltage = context.active_voltage
        verify_sectors = context.verify_sectors
        rom_bank_size = context.rom_bank_size
        _mbc = context.mbc
        buffer_len = context.buffer_len
        temp: Any = None
        pos_from = 0
        verify_len = 0
        buffer_pos = 0
        start_address = 0
        end_address = 0
        start_bank = 0
        end_bank = 0

        # ↓↓↓ Flash verify
        verified = False
        crc32_errors = 0
        if "broken_sectors" in self.INFO:
            del self.INFO["broken_sectors"]
        if "verify_write" in args and args["verify_write"] is True:
            self.SetProgress(
                {
                    "action": "INITIALIZE",
                    "method": "ROM_WRITE_VERIFY",
                    "size": len(data_import),
                    "flash_offset": flash_offset,
                    "voltage": active_voltage,
                },
            )
            if ".dev" in AppInfo.VERSION_PEP440 or AppContext.DEBUG:
                (Path(AppContext.CONFIG_PATH) / "debug_verify.bin").write_bytes(b"")

            current_bank: int | None = None
            broken_sectors = []

            for sector in verify_sectors:
                if self.CANCEL:
                    self._AbortFlashVerification()
                    return None

                if sector[0] >= len(data_import):
                    break

                verified = False
                if self.FW["fw_ver"] >= 10 and not (flashcart and cart_type["command_set"] == "GBAMP"):
                    if self.MODE == "AGB":
                        dprint("Verifying sector:", hex(sector[0]), hex(sector[1]))
                        buffer_pos = sector[0]
                        start_address = buffer_pos
                        end_address = sector[0] + sector[1]
                        start_bank = math.floor(buffer_pos / rom_bank_size)
                        end_bank = math.ceil((buffer_pos + sector[1]) / rom_bank_size)
                    elif self.MODE == "DMG":
                        dprint("Verifying sector:", hex(sector[0]), hex(sector[1]))
                        buffer_pos = sector[0]
                        start_bank = math.floor(buffer_pos / rom_bank_size)
                        end_bank = math.ceil((buffer_pos + sector[1]) / rom_bank_size)

                    bank = start_bank
                    while bank < end_bank:
                        # ↓↓↓ Switch ROM bank
                        if self.MODE == "DMG":
                            if _mbc.ResetBeforeBankChange(bank) is True:
                                dprint("Resetting the MBC")
                                self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
                            (start_address, bank_size) = _mbc.SelectBankROM(bank)
                            verify_len = bank_size
                            if flashcart.PulseResetAfterWrite() and bank == 0:
                                if self.FW["fw_ver"] < 2 and "OFW_GB_CART_MODE" in self.DEVICE_CMD:
                                    self._write(self.DEVICE_CMD["OFW_GB_CART_MODE"])
                                else:
                                    self._write(
                                        self.DEVICE_CMD["SET_MODE_DMG"],
                                        wait=self.FW["fw_ver"] >= 12,
                                    )
                                self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
                            self._set_fw_variable("DMG_ROM_BANK", bank)

                            buffer_len = min(buffer_len, bank_size)
                            if "start_addr" in flashcart.CONFIG and bank == 0:
                                start_address = flashcart.CONFIG["start_addr"]
                            end_address = start_address + bank_size
                            start_address += buffer_pos % rom_bank_size
                            end_address = min(end_address, start_address + sector[1])
                            pos_from = bank * start_address

                        elif self.MODE == "AGB":
                            if (
                                "flash_bank_select_type" in cart_type and cart_type["flash_bank_select_type"] > 0
                            ) and bank != current_bank:
                                flashcart.Reset(full_reset=True)
                                flashcart.SelectBankROM(bank)
                                temp = end_address - start_address
                                start_address %= cart_type["flash_bank_size"]
                                end_address = min(
                                    cart_type["flash_bank_size"],
                                    start_address + temp,
                                )
                                current_bank = bank
                            verify_len = sector[1]
                            pos_from = sector[0]
                        # ↑↑↑ Switch ROM bank

                        dprint(
                            f"Verifying ROM bank #{bank} at 0x{pos_from:x} (physical 0x{start_address:X}, 0x{verify_len:X} bytes)",
                        )

                        verified = False
                        if self.FW["fw_ver"] >= 12 and sector[1] >= verify_len and crc32_errors < 5:
                            verified = self.CompareCRC32(
                                buffer=data_import,
                                offset=pos_from,
                                length=verify_len,
                                address=start_address,
                                flashcart=flashcart,
                                reset=False,
                                mbc=_mbc,
                                bank=bank,
                            )
                            if verified is True:
                                dprint(f"CRC32 verification successful between 0x{pos_from:X} and 0x{verify_len:X}")
                                self.SetProgress(
                                    {
                                        "action": "UPDATE_POS",
                                        "pos": pos_from + verify_len,
                                    },
                                )
                            elif isinstance(verified, tuple) and len(verified) == 2:
                                crc32_errors += 1
                                dprint(
                                    f"Mismatch during CRC32 verification at 0x{pos_from:X}",
                                    "Errors:",
                                    crc32_errors,
                                )
                                verified = False
                                break

                            if verified is False:
                                break

                        else:
                            self.SetProgress({"action": "UPDATE_POS", "pos": buffer_pos})

                        bank += 1

                if not verified:
                    verify_args = copy.copy(args)
                    verify_args.update(
                        {
                            "verify_write": data_import[sector[0] : sector[0] + sector[1]],
                            "rom_size": len(data_import),
                            "verify_from": sector[0],
                            "path": "",
                            "rtc_area": flashcart.HasRTC(),
                            "verify_mbc": _mbc,
                        },
                    )
                    verify_args["verify_base_pos"] = sector[0]
                    verify_args["verify_len"] = len(verify_args["verify_write"])
                    verify_args["rom_size"] = len(verify_args["verify_write"])

                    self.NO_PROG_UPDATE = True
                    self.ReadROM(0, 4)  # dummy read
                    self.NO_PROG_UPDATE = False
                    start_address = 0
                    end_address = buffer_pos

                    verified_size = self._BackupROM(verify_args)
                    if isinstance(verified_size, int):
                        dprint(
                            'args["verify_len"]=0x{:X}, verified_size=0x{:X}'.format(
                                verify_args["verify_len"],
                                verified_size,
                            ),
                        )
                    if self.CANCEL or self.ERROR:
                        self._AbortFlashVerification()
                        return None
                    if (verified_size is not True) and (verify_args["verify_len"] != verified_size):
                        if verified_size is None:
                            dprint(
                                "Verification failed! Sector: {sector}",
                                sector=str(sector),
                            )
                        else:
                            dprint(
                                "Verification failed at {address}! Sector: {sector}",
                                address=f"0x{sector[0] + verified_size:X}",
                                sector=str(sector),
                            )
                        if sector not in broken_sectors:
                            broken_sectors.append(sector)
                        continue
                    dprint(
                        f"Verification between 0x{sector[0]:X} and 0x{sector[0] + sector[1]:X} successful by normal reading.",
                    )
                    verified = True

            self.SetProgress(
                {
                    "action": "UPDATE_POS",
                    "pos": len(data_import),
                    "force_update": True,
                    "skipping": len(data_import) > self.POS,
                },
            )
            if self._StoreFlashVerificationErrors(context, broken_sectors):
                verified = False
        # ↑↑↑ Flash verify
        return verified

    def _configure_flashcart_for_write(
        self,
        args: dict[str, Any],
        cart_type: dict[str, Any],
        flashcart: Flashcart,
        data_import: bytearray,
        mode: DeviceMode,
    ) -> _FlashConfiguration | None:
        enable_pullup_wr = 0
        mbc_instance: Any = None
        end_bank = 0
        rom_bank_size = 0

        # ↓↓↓ Flashcart configuration
        if self.FW["fw_ver"] >= 8 and "enable_pullups" in cart_type:
            if cart_type["enable_pullups"] is True:
                self._write(self.DEVICE_CMD["ENABLE_PULLUPS"], wait=True)
                dprint("Pullups enabled")
            else:
                self._write(self.DEVICE_CMD["DISABLE_PULLUPS"], wait=True)
                dprint("Pullups disabled")
        if self.FW["fw_ver"] >= 12:
            if mode == "DMG":
                # Joey Jr bug workaround
                enable_pullup_wr = (
                    2
                    if (
                        ("enable_pullup_wr" in cart_type and cart_type["enable_pullup_wr"] is True)
                        or ("force_wr_pullup" in args and args["force_wr_pullup"] is True)
                    )
                    else 0
                )
                self._set_fw_variable("PULLUPS_ENABLED", enable_pullup_wr)
            elif mode == "AGB":
                self._set_fw_variable("AGB_IRQ_ENABLED", 1 if "set_irq_high" in cart_type else 0)

        errmsg_mbc_selection = ""
        if mode == "DMG":
            self._write(self.DEVICE_CMD["SET_MODE_DMG"], wait=self.FW["fw_ver"] >= 12)
            mbc = flashcart.GetMBC()
            if mbc is not False and isinstance(mbc, int):
                args["mbc"] = mbc
                dprint(f"Using forced mapper type 0x{mbc:02X} for flashing")
            elif mbc == "manual":
                dprint("Using manually selected mapper type 0x{:02X} for flashing".format(args["mbc"]))
            else:
                args["mbc"] = 0
                dprint("Using default mapper type 0x{:02X} for flashing".format(args["mbc"]))
            if args["mbc"] == 0:
                args["mbc"] = 0x19  # MBC5 default

            if not self.IsSupportedMbc(args["mbc"]):
                msg = __(
                    "This cartridge uses a mapper that is not supported by FlashGBX using your {device_name}.",
                    device_name=self.GetFullName(),
                )
                self.SetProgress(
                    {
                        "action": "ABORT",
                        "info_type": "msgbox_critical",
                        "info_msg": msg,
                        "abortable": False,
                    },
                )
                return None

            mbc_instance = DMG_Mapper().GetInstance(
                args=args,
                cart_write_fncptr=self._cart_write,
                cart_read_fncptr=self._mapper_cart_read,
                cart_powercycle_fncptr=self.CartPowerCycleOrAskReconnect,
                clk_toggle_fncptr=self._clk_toggle,
            )

            self._set_fw_variable("FLASH_PULSE_RESET", 1 if flashcart.PulseResetAfterWrite() else 0)

            end_bank = math.ceil(len(data_import) / mbc_instance.GetROMBankSize())
            rom_bank_size = mbc_instance.GetROMBankSize()

            mbc_instance.EnableMapper()

            if flashcart.GetMBC() == "manual":
                errmsg_mbc_selection += (
                    "\n"
                    + __("- Check mapper type used:")
                    + " "
                    + mbc_instance.GetName()
                    + " ("
                    + c__("Mapper Type", "manual selection")
                    + ")"
                )
            else:
                errmsg_mbc_selection += (
                    "\n"
                    + __("- Check mapper type used:")
                    + " "
                    + mbc_instance.GetName()
                    + " ("
                    + c__("Mapper Type", "forced by selected flashcart profile")
                    + ")"
                )
            if len(data_import) > mbc_instance.GetMaxROMSize():
                errmsg_mbc_selection += "\n" + __(
                    "- Check mapper type ROM size limit: likely up to {max_size}",
                    max_size=Formatter.file_size(mbc_instance.GetMaxROMSize()),
                )

        elif mode == "AGB":
            self._write(self.DEVICE_CMD["SET_MODE_AGB"], wait=self.FW["fw_ver"] >= 12)
            if flashcart and "flash_bank_size" in cart_type:
                end_bank = math.ceil(len(data_import) / cart_type["flash_bank_size"])
            else:
                end_bank = 1
            rom_bank_size = cart_type["flash_bank_size"] if flashcart and "flash_bank_size" in cart_type else 33554432

        flash_buffer_size = flashcart.GetBufferSize()
        # ↑↑↑ Flashcart configuration
        return _FlashConfiguration(
            mbc=mbc_instance,
            end_bank=end_bank,
            rom_bank_size=rom_bank_size,
            enable_pullup_wr=enable_pullup_wr,
            error_message=errmsg_mbc_selection,
            buffer_size=flash_buffer_size,
        )

    def _plan_flash_sectors(
        self,
        args: dict[str, Any],
        flashcart: Flashcart,
        data_import: bytearray,
        rom_bank_size: int,
        flash_offset: int,
    ) -> _FlashSectorPlan | None:
        smallest_sector_size: int | Literal[False] = 0x2000
        sector_offsets: list[list[int]] = []
        write_sectors: list[list[int]] = []
        delta_state_new: list[list[int]] | None = None
        json_file: str | Path = ""
        sector_map = flashcart.GetSectorMap()
        if sector_map is not None and sector_map is not False:
            smallest_sector_size = flashcart.GetSmallestSectorSize()
            sector_offsets = flashcart.GetSectorOffsets(
                rom_size=flashcart.GetFlashSize(default=len(data_import)),
                rom_bank_size=rom_bank_size,
            )
            if sector_offsets:
                flash_capacity = sector_offsets[-1][0] + sector_offsets[-1][1]
                if flash_capacity < len(data_import) and not (
                    flashcart.SupportsChipErase() and args["prefer_chip_erase"]
                ):
                    sector_offsets = flashcart.GetSectorOffsets(
                        rom_size=len(data_import),
                        rom_bank_size=rom_bank_size,
                    )

            sector_offsets_hash = base64.urlsafe_b64encode(
                hashlib.sha1(str(sector_offsets).encode("UTF-8")).digest(),
            ).decode("ASCII", "ignore")[:4]

            if len(sector_offsets) > 1:
                delta_path = Path(args["path"])
                source_path = delta_path.with_name(delta_path.stem.removesuffix(".delta") + delta_path.suffix)
                if delta_path.stem.endswith(".delta") and source_path.exists():
                    delta_state_new = []
                    with source_path.open("rb") as source_file:
                        for s_from, s_size in sector_offsets:
                            s_to = s_from + s_size
                            if data_import[s_from:s_to] != source_file.read(s_size):
                                sector_state = [
                                    s_from,
                                    s_size,
                                    zlib.crc32(data_import[s_from:s_to]) & 0xFFFFFFFF,
                                ]
                                delta_state_new.append(sector_state)
                                dprint("Sector differs:", sector_state)
                    write_sectors = copy.copy(delta_state_new)
                    json_file = delta_path.with_name(f"{delta_path.stem}_{sector_offsets_hash}.json")
                    if json_file.exists():
                        with json_file.open("rb") as state_file:
                            try:
                                delta_state_old = json.loads(state_file.read().decode("UTF-8-SIG"))
                            except json.JSONDecodeError, UnicodeDecodeError:
                                delta_state_old = []
                            for old_sector in delta_state_old:
                                if old_sector in write_sectors:
                                    write_sectors.remove(old_sector)
                                    dprint("Skipping sector:", old_sector)
                                else:
                                    write_sector_ranges = [sector[:-1] for sector in write_sectors]
                                    if old_sector[:-1] not in write_sector_ranges:
                                        write_sectors.append(old_sector)
                                        dprint("Forcing sector:", old_sector)

                    if not write_sectors:
                        self.SetProgress(
                            {
                                "action": "ABORT",
                                "info_type": "msgbox_critical",
                                "info_msg": __(
                                    "No flash sectors were found that would need to be updated for delta flashing.",
                                ),
                                "abortable": False,
                            },
                        )
                        return None

                elif "flash_sectors" in args and len(args["flash_sectors"]) > 0:
                    write_sectors = args["flash_sectors"]

                elif flash_offset > 0:  # Batteryless SRAM
                    flash_size = args.get("flash_size", len(data_import) - flash_offset)
                    if "flash_size" in args:
                        data_import = data_import[:flash_size]
                    batteryless_sectors = []
                    for sector in sector_offsets:
                        if flash_offset > sector[0]:
                            continue
                        if flash_offset + flash_size < sector[0]:
                            break
                        batteryless_sectors.append(sector)
                    write_sectors = batteryless_sectors
                    if "bl_save" in args:
                        data_import = bytearray([0] * batteryless_sectors[0][0]) + data_import
                else:
                    write_sectors = [sector for sector in sector_offsets if sector[0] < len(data_import)]

            dprint("Sectors to update:", write_sectors)

        return _FlashSectorPlan(
            data_import=data_import,
            smallest_sector_size=smallest_sector_size,
            sector_offsets=sector_offsets,
            write_sectors=write_sectors,
            delta_state=delta_state_new,
            state_path=json_file,
            has_sector_map=sector_map is not False,
        )

    def _PrepareGBMemoryMap(
        self,
        args: dict[str, Any],
        mbc: _HiddenSectorMapper,
        data_import: bytearray,
    ) -> bytearray | None:
        """Load or generate the hidden-sector map used by GB-Memory carts."""
        if self.MODE != "DMG" or mbc.GetName() != "G-MMC1":
            return bytearray()
        if "buffer_map" not in args:
            map_path = Path(args["path"]).with_suffix(".map")
            if map_path.exists():
                with map_path.open("rb") as file:
                    args["buffer_map"] = file.read()
            else:
                rom_data = data_import or bytearray([0xFF] * 0x180)
                try:
                    old_map = mbc.ReadHiddenSector()
                    if not isinstance(old_map, (bytes, bytearray, memoryview)):
                        old_map = None
                    args["buffer_map"] = GBMemoryMap(rom=rom_data, oldmap=old_map).GetMapData()
                except Exception:
                    print(traceback.format_exc())
                    print(
                        ANSI.RED
                        + __(
                            "An error occured while trying to generate the hidden sector data for the {gb_memory_cartridge}.",
                            gb_memory_cartridge="NP GB-Memory cartridge",
                        )
                        + ANSI.RESET,
                    )
                    args["buffer_map"] = False

                if args["buffer_map"] is False:
                    self.SetProgress(
                        {
                            "action": "ABORT",
                            "info_type": "msgbox_critical",
                            "info_msg": __(
                                "The {gb_memory_cartridge} requires extra hidden sector data. As it couldn't be auto-generated, please provide your own at the following path:",
                                gb_memory_cartridge="NP GB-Memory cartridge",
                            )
                            + " "
                            + str(map_path),
                            "abortable": False,
                        },
                    )
                    return None
                dprint("Hidden sector data:", args["buffer_map"])
                if ".dev" in AppInfo.VERSION_PEP440 or AppContext.DEBUG:
                    with (Path(AppContext.CONFIG_PATH) / "debug_mmsa_map.bin").open("wb") as file:
                        file.write(args["buffer_map"])
        dprint("Hidden sector data loaded")
        return bytearray(args["buffer_map"])

    def _CheckFlashID(self, cart_type: dict[str, Any], flashcart: Flashcart, command_set_type: str) -> bool:
        """Verify a configured flash ID or unlock carts without an ID."""
        if "flash_ids" not in cart_type:
            return flashcart.Unlock() is not False

        verified, flash_id = flashcart.VerifyFlashID()
        if verified or command_set_type == "BLAZE_XPLODER":
            return True
        if self.VOLTAGE_FALLBACK_PENDING:
            self.VOLTAGE_FALLBACK_TRIGGERED = True
            self.ERROR = True
            self.CANCEL = True
            return False
        print(
            ANSI.YELLOW
            + __(
                "Note: This cartridge's Flash ID ({flash_id}) doesn't match the flashcart profile selection.",
                flash_id=" ".join(format(value, "02X") for value in flash_id),
            )
            + ANSI.RESET,
        )
        return True

    def _EraseFlashForWrite(
        self,
        args: dict[str, Any],
        flashcart: Flashcart,
        flash_offset: int,
        has_sector_map: bool,
    ) -> bool | None:
        """Choose and perform chip erase, returning whether it was used."""
        if flashcart.SupportsChipErase() and flash_offset <= 0:
            use_chip_erase = not (
                flashcart.SupportsSectorErase() and args["prefer_chip_erase"] is False and has_sector_map
            )
            if use_chip_erase:
                dprint("Erasing the entire flash chip")
                if flashcart.ChipErase() is False:
                    return None
            return use_chip_erase
        if flashcart.SupportsSectorErase():
            return False
        self.SetProgress(
            {
                "action": "ABORT",
                "info_type": "msgbox_critical",
                "info_msg": __("No erase method available."),
                "abortable": False,
            },
        )
        return None

    def _WriteGBMemoryMap(self, flashcart: Flashcart_DMG_MMSA, data_map_import: bytearray) -> bool:
        """Write a GB-Memory hidden sector after the main ROM contents."""
        if flashcart.EraseHiddenSector(buffer=data_map_import) is False:
            return False
        status = self.WriteROM_GBMEMORY(address=0, buffer=data_map_import[0:128], bank=1)
        if status is not False:
            return True
        self.SetProgress(
            {
                "action": "ABORT",
                "info_type": "msgbox_critical",
                "info_msg": __(
                    "An error occured while writing the hidden sector. Please make sure that the cartridge contacts are clean, re-connect the device and try again from the beginning.",
                ),
                "abortable": False,
            },
        )
        return False

    def _SaveFlashDeltaState(
        self,
        delta_state: list[list[int]] | None,
        chip_erase: bool,
        state_path: str | Path,
    ) -> None:
        """Persist delta-write state when sector data remains reusable."""
        if delta_state is None or chip_erase or "broken_sectors" in self.INFO:
            return
        try:
            with Path(state_path).open("wb") as file:
                file.write(json.dumps(delta_state).encode("UTF-8-SIG"))
        except PermissionError:
            print(__("Error: Couldn't update write-protected file “{file}”", file=state_path))

    def _RestoreFirstROMBank(
        self,
        mbc: _FlashResetMapper,
        cart_type: dict[str, Any],
        flashcart: Flashcart,
    ) -> None:
        """Return a flash cartridge to its first ROM bank after writing."""
        if self.MODE == "DMG":
            if mbc.ResetBeforeBankChange(0) is True:
                dprint("Resetting the MBC")
                self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
            mbc.SelectBankROM(0)
            self._set_fw_variable("DMG_ROM_BANK", 0)
        elif self.MODE == "AGB" and cart_type.get("flash_bank_select_type", 0) > 0:
            flashcart.SelectBankROM(0)

    def _reset_flash_after_write(self, cart_type: dict[str, Any], flashcart: Flashcart) -> None:
        flashcart.Reset(full_reset=True)
        if self.FW["fw_ver"] >= 14 and "set_audio_high" in cart_type:
            self._set_fw_variable("DMG_AUDIO_ENABLED", 0)

    def _prepare_flash_write(self, args: dict[str, Any], mode: DeviceMode) -> _FlashWritePreparation | None:
        data_import, flash_offset = self._prepare_flash_data(args, mode)
        supported_carts = list(self.SUPPORTED_CARTS[mode].values())
        cart_type = copy.deepcopy(supported_carts[args["cart_type"]])
        try:
            cart_name = cart_type["names"][0]
        except IndexError, KeyError, TypeError:
            cart_name = c__("Flashcart Profile", "Unknown")

        if not isinstance(cart_type, dict) or not self._check_flashcart_firmware(cart_type):
            return None
        if self.CanPowerCycleCart():
            self.CartPowerOn()
        self._set_flashcart_profile_index(cart_type, mode, args["cart_type"])

        callbacks: FlashcartCallbacks = {
            "cart_write_fncptr": self._cart_write,
            "cart_write_fast_fncptr": self._cart_write_flash,
            "cart_read_fncptr": self.ReadROM,
            "cart_powercycle_fncptr": self.CartPowerCycleOrAskReconnect,
            "progress_fncptr": self.SetProgress,
            "set_we_pin_wr": self._set_we_pin_wr,
            "set_we_pin_audio": self._set_we_pin_audio,
        }
        flashcart = self._create_flashcart(cart_type, callbacks)
        active_voltage = self._set_flash_voltage(args, flashcart)
        self._pad_flash_data_for_sector_erase(args, flashcart, data_import)

        configuration = self._configure_flashcart_for_write(args, cart_type, flashcart, data_import, mode)
        if configuration is None:
            return None
        mbc, end_bank, rom_bank_size, enable_pullup_wr, error_message, flash_buffer_size = configuration

        data_map_import = self._PrepareGBMemoryMap(args, mbc, data_import)
        if data_map_import is None:
            return None
        flash_commands = self._load_flash_commands(cart_type, flashcart, flash_buffer_size)
        if flash_commands is None:
            return None
        command_set_type, write_enable_pin = flash_commands

        if mode == "DMG" and cart_type.get("flash_commands_on_bank_1") is True:
            dprint("Setting ROM bank 1")
            mbc.SelectBankROM(1)
        if write_enable_pin != 0x00:
            self._set_fw_variable("FLASH_WE_PIN", write_enable_pin)
        if self.FW["fw_ver"] >= 14 and "set_audio_high" in cart_type:
            self._set_fw_variable("DMG_AUDIO_ENABLED", 1)
        if not self._CheckFlashID(cart_type, flashcart, command_set_type):
            return None

        sector_plan = self._plan_flash_sectors(args, flashcart, data_import, rom_bank_size, flash_offset)
        if sector_plan is None:
            return None
        (
            data_import,
            smallest_sector_size,
            sector_offsets,
            write_sectors,
            delta_state,
            state_path,
            has_sector_map,
        ) = sector_plan

        if "photo_mode" not in args:
            self.SetProgress(
                {
                    "action": "INITIALIZE",
                    "method": "ROM_WRITE",
                    "size": len(data_import),
                    "flash_offset": flash_offset,
                    "sector_count": max(1, len(write_sectors)),
                    "voltage": active_voltage,
                },
            )
            self.INFO["action"] = self.ACTIONS["ROM_WRITE"]

        chip_erase = self._EraseFlashForWrite(args, flashcart, flash_offset, has_sector_map)
        if chip_erase is None:
            return None
        verify_sectors: list[list[int]] = []
        if chip_erase:
            write_sectors = [[0, len(data_import)]]
            verify_sectors.extend([[offset, 0x20000] for offset in range(0, len(data_import), 0x20000)])
        elif not write_sectors:
            write_sectors = sector_offsets

        if "photo_mode" not in args:
            self.SetProgress(
                {
                    "action": "INITIALIZE",
                    "method": "ROM_WRITE",
                    "size": len(data_import),
                    "flash_offset": flash_offset,
                    "sector_count": len(write_sectors),
                    "voltage": active_voltage,
                },
            )
            self.INFO["action"] = self.ACTIONS["ROM_WRITE"]
        self.SetProgress({"action": "UPDATE_POS", "pos": flash_offset})

        if smallest_sector_size is not False:
            buffer_len = smallest_sector_size
        elif mode == "DMG":
            buffer_len = mbc.GetROMBankSize()
            if mbc.HasFlashBanks():
                mbc.SelectBankFlash(0)
        else:
            buffer_len = 0x2000
        dprint(f"Transfer buffer length is 0x{buffer_len:X}")

        if not write_sectors:
            self.SetProgress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": __("Couldn't start writing ROM because the flash cart couldn't be detected properly."),
                    "abortable": False,
                },
            )
            return None

        return _FlashWritePreparation(
            cart_name=cart_name,
            cart_type=cart_type,
            flashcart=flashcart,
            data_import=data_import,
            data_map_import=data_map_import,
            flash_offset=flash_offset,
            active_voltage=active_voltage,
            mbc=mbc,
            end_bank=end_bank,
            rom_bank_size=rom_bank_size,
            enable_pullup_wr=enable_pullup_wr,
            error_message=error_message,
            flash_buffer_size=flash_buffer_size,
            command_set_type=command_set_type,
            sector_offsets=sector_offsets,
            write_sectors=write_sectors,
            delta_state=delta_state,
            state_path=state_path,
            chip_erase=chip_erase,
            buffer_len=buffer_len,
            verify_sectors=verify_sectors,
        )

    def _AbortFlashWriteIfCanceled(self) -> bool:
        if not self.CANCEL:
            return False
        cancel_args = {"action": "ABORT", "abortable": False}
        cancel_args.update(self.CANCEL_ARGS)
        self.CANCEL_ARGS = {}
        self.ERROR_ARGS = {}
        self.SetProgress(cancel_args)
        if self.CanPowerCycleCart():
            self.CartPowerCycle()
        if self.FW["fw_ver"] >= 12:
            self._set_fw_variable("AGB_IRQ_ENABLED", 0)
        return True

    def _GetFlashWriteStatusRegister(self, flashcart: Flashcart, sector_erase_result: object) -> str:
        status_register: Any = c__("Status Register", "Unknown")
        if self.FW["fw_ver"] < 12:
            return str(status_register)
        if sector_erase_result is False:
            status_register = flashcart.LAST_SR
            if isinstance(status_register, int):
                if status_register < 0x100:
                    status_register = (
                        f"0x{status_register:02X} ({status_register >> 4:04b} {status_register & 0xF:04b})"
                    )
                else:
                    status_register = (
                        f"0x{status_register:04X} ({status_register >> 8:08b} {status_register & 0xFF:08b})"
                    )
            return str(status_register)

        lives = 3
        while lives > 0:
            dprint("Retrieving last status register value...")
            device = self._serial_device()
            device.reset_input_buffer()
            device.reset_output_buffer()
            status_register = self._get_fw_variable("STATUS_REGISTER")
            if status_register not in (False, None):
                if status_register < 0x100:
                    status_register = (
                        f"0x{status_register:02X} ({status_register >> 4:04b} {status_register & 0xF:04b})"
                    )
                else:
                    status_register = (
                        f"0x{status_register:04X} ({status_register >> 8:08b} {status_register & 0xFF:08b})"
                    )
                break
            dprint("Erroneous response:", status_register)
            lives -= 1
        if lives == 0:
            status_register = c__("Status Register", "Timeout")
        return str(status_register)

    def _PrepareFlashSector(
        self,
        sector: list[int],
        first_sector_written: bool,
        sector_offsets: list[list[int]],
        rom_bank_size: int,
    ) -> _FlashSectorState | None:
        retry_hp = 15 if not first_sector_written else 100
        dprint("Writing sector:", hex(sector[0]), hex(sector[1]))
        buffer_pos = sector[0]
        start_address = buffer_pos
        end_address = sector[0] + sector[1]
        if sector[:2] not in sector_offsets:
            if self.MODE == "AGB":
                dprint("Sector not found for delta writing:", sector)
            else:
                print("Sector not found for delta writing:", sector)
            return None
        sector_pos = sector_offsets.index(sector[:2])
        start_bank = math.floor(buffer_pos / rom_bank_size)
        end_bank = math.ceil((buffer_pos + sector[1]) / rom_bank_size)
        return _FlashSectorState(
            retry_hp,
            buffer_pos,
            start_address,
            end_address,
            sector_pos,
            start_bank,
            end_bank,
        )

    def _FinishFlashWrite(
        self,
        args: dict[str, Any],
        mode: DeviceMode,
        preparation: _FlashWritePreparation,
        buffer_len: int,
    ) -> bool | None:
        self.SetProgress({"action": "UPDATE_POS", "pos": len(preparation.data_import), "force_update": True})
        if preparation.command_set_type == "GBMEMORY" and (
            not isinstance(preparation.flashcart, Flashcart_DMG_MMSA)
            or not self._WriteGBMemoryMap(preparation.flashcart, preparation.data_map_import)
        ):
            return False

        self._reset_flash_after_write(preparation.cart_type, preparation.flashcart)
        verification_context = _FlashVerificationContext(
            args=args,
            cart_type=preparation.cart_type,
            flashcart=preparation.flashcart,
            data_import=preparation.data_import,
            flash_offset=preparation.flash_offset,
            active_voltage=preparation.active_voltage,
            verify_sectors=preparation.verify_sectors,
            rom_bank_size=preparation.rom_bank_size,
            mbc=preparation.mbc,
            buffer_len=buffer_len,
        )
        verified = self._verify_flash_write(verification_context)
        if verified is None:
            return None

        self._SaveFlashDeltaState(preparation.delta_state, preparation.chip_erase, preparation.state_path)
        self._RestoreFirstROMBank(preparation.mbc, preparation.cart_type, preparation.flashcart)
        self.SetMode(mode)

        if "photo_mode" not in args:
            self.INFO["last_action"] = self.INFO["action"]
            self.INFO["action"] = None
            self._thread_worker_auto_poweroff_finish()
            self.SetProgress({"action": "FINISHED", "verified": verified})
        return True

    def _WriteFlashChunk(self, parameters: _FlashChunkParameters) -> tuple[DeviceWriteResult, int]:
        command_set_type, pos, data_import, buffer_pos, buffer_len, bank, flash_buffer_size, skip_init, rumble = (
            parameters
        )
        data = data_import[buffer_pos : buffer_pos + buffer_len]
        if command_set_type == "GBMEMORY" and self.FW["fw_ver"] < 2:
            status = self.WriteROM_GBMEMORY(address=pos, buffer=data, bank=bank)
        elif command_set_type == "GBMEMORY" and self.FW["fw_ver"] >= 2:
            status = self.WriteROM(
                address=pos,
                buffer=data,
                flash_buffer_size=flash_buffer_size,
                skip_init=(skip_init and not self.SKIPPING),
            )
        elif command_set_type == "DMG-MBC5-32M-FLASH" and self.FW["fw_ver"] < 12:
            status = self.WriteROM_DMG_MBC5_32M_FLASH(address=pos, buffer=data, bank=bank)
        elif command_set_type == "EEPROM":
            status = self.WriteROM_DMG_EEPROM(address=pos, buffer=data, bank=bank, eeprom_buffer_size=256)
        elif command_set_type == "BLAZE_XPLODER":
            status = self.WriteROM_DMG_EEPROM(address=pos, buffer=data, bank=bank)
        elif command_set_type == "DATEL_ORBITV2" and self.FW["fw_ver"] >= 12:
            status = self.WriteROM(
                address=(pos | (bank << 24)),
                buffer=data,
                flash_buffer_size=flash_buffer_size,
                skip_init=(skip_init and not self.SKIPPING),
            )
        elif command_set_type == "DATEL_ORBITV2":
            status = self.WriteROM_DMG_DatelOrbitV2(address=pos, buffer=data, bank=bank)
        else:
            max_buffer_write = self.MAX_BUFFER_WRITE
            if len(data_import) == 0x1FFFF00 and buffer_pos + buffer_len > len(data_import):
                # 32 MiB ROM + EEPROM cart
                max_buffer_write = 256
                buffer_len = buffer_pos + buffer_len - len(data_import)
                data = data_import[buffer_pos : buffer_pos + buffer_len]
            status = self.WriteROM(
                address=pos,
                buffer=data,
                flash_buffer_size=flash_buffer_size,
                skip_init=(skip_init and not self.SKIPPING),
                rumble_stop=rumble,
                max_length=max_buffer_write,
            )
        return status, buffer_len

    @staticmethod
    def _FlashcartHasRumble(flashcart: Flashcart) -> bool:
        return "rumble" in flashcart.CONFIG and flashcart.CONFIG["rumble"] is True

    def _PowerOnIfSupported(self) -> None:
        if self.CanPowerCycleCart():
            self.CartPowerOn()

    def _SelectFlashWriteBank(self, context: _FlashBankContext) -> _FlashBankState:
        (
            mbc,
            flashcart,
            cart_type,
            bank,
            current_bank,
            start_address,
            end_address,
            buffer_position,
            rom_bank_size,
            sector_size,
            buffer_length,
        ) = context
        if self.MODE == "DMG":
            if mbc.ResetBeforeBankChange(bank) is True:
                dprint("Resetting the MBC")
                self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
            start_address, bank_size = mbc.SelectBankROM(bank)
            if flashcart.PulseResetAfterWrite() and bank == 0:
                if self.FW["fw_ver"] < 2 and "OFW_GB_CART_MODE" in self.DEVICE_CMD:
                    self._write(self.DEVICE_CMD["OFW_GB_CART_MODE"])
                else:
                    self._write(self.DEVICE_CMD["SET_MODE_DMG"], wait=self.FW["fw_ver"] >= 12)
                self._write(self.DEVICE_CMD["DMG_MBC_RESET"], wait=True)
            self._set_fw_variable("DMG_ROM_BANK", bank)
            buffer_length = min(buffer_length, bank_size)
            if "start_addr" in flashcart.CONFIG and bank == 0:
                start_address = flashcart.CONFIG["start_addr"]
            end_address = start_address + bank_size
            start_address += buffer_position % rom_bank_size
            end_address = min(end_address, start_address + sector_size)
        elif "flash_bank_select_type" in cart_type and cart_type["flash_bank_select_type"] > 0 and bank != current_bank:
            flashcart.Reset(full_reset=True)
            flashcart.SelectBankROM(bank)
            remaining_size = end_address - start_address
            start_address %= cart_type["flash_bank_size"]
            end_address = min(cart_type["flash_bank_size"], start_address + remaining_size)
            current_bank = bank
        return _FlashBankState(start_address, end_address, current_bank, buffer_length)

    def _TrySkipMatchingFlashSector(self, context: _FlashMatchContext) -> _FlashMatchResult:
        args = context.args
        preparation = context.preparation
        flashcart = preparation.flashcart
        cart_type = preparation.cart_type
        sector = context.sector
        sector_size = sector[1]
        if (
            preparation.chip_erase
            or self.FW["fw_ver"] < 12
            or args.get("compare_sectors") is not True
            or cart_type["command_set"] == "GBAMP"
        ):
            return _FlashMatchResult(
                skipped=False,
                canceled=False,
                bank=context.bank,
                current_bank=context.current_bank,
                start_address=context.start_address,
                end_address=context.end_address,
                buffer_position=context.buffer_position,
                sector_size=sector_size,
                buffer_length=context.buffer_length,
            )

        bank = context.bank
        current_bank = context.current_bank
        start_address = context.start_address
        end_address = context.end_address
        buffer_length = context.buffer_length
        match_position = context.buffer_position
        verified = False
        pos = start_address
        while bank < context.end_bank:
            start_address, end_address, current_bank, buffer_length = self._SelectFlashWriteBank(
                _FlashBankContext(
                    preparation.mbc,
                    flashcart,
                    cart_type,
                    bank,
                    current_bank,
                    start_address,
                    end_address,
                    context.buffer_position,
                    preparation.rom_bank_size,
                    sector_size,
                    buffer_length,
                ),
            )
            pos = start_address
            verified = self.CompareCRC32(
                buffer=preparation.data_import,
                offset=match_position,
                length=end_address - pos,
                address=pos,
                flashcart=flashcart,
                reset=True,
                mbc=preparation.mbc,
                bank=bank,
            )
            self.SetProgress({"action": "UPDATE_POS", "pos": match_position})
            if verified is not True:
                break
            match_position += end_address - pos
            bank += 1

        if verified is not True:
            return _FlashMatchResult(
                skipped=False,
                canceled=False,
                bank=context.bank,
                current_bank=current_bank,
                start_address=start_address,
                end_address=end_address,
                buffer_position=context.buffer_position,
                sector_size=sector_size,
                buffer_length=buffer_length,
            )

        dprint(f"Skipping sector #{context.sector_position:d}, because the CRC32 matched")
        if self.MODE == "DMG" and flashcart.FlashCommandsOnBank1():
            preparation.mbc.SelectBankROM(bank)
        self.NO_PROG_UPDATE = True
        sector_erase_result = flashcart.SectorErase(
            pos=pos,
            buffer_pos=context.buffer_position,
            skip=True,
        )
        self.NO_PROG_UPDATE = False
        if self._AbortFlashWriteIfCanceled():
            return _FlashMatchResult(
                skipped=False,
                canceled=True,
                bank=bank,
                current_bank=current_bank,
                start_address=start_address,
                end_address=end_address,
                buffer_position=context.buffer_position,
                sector_size=sector_size,
                buffer_length=buffer_length,
            )
        if sector_erase_result:
            sector_size = sector_erase_result
            dprint(f"Next sector size: 0x{sector_size:X}")
        return _FlashMatchResult(
            skipped=True,
            canceled=False,
            bank=bank,
            current_bank=current_bank,
            start_address=start_address,
            end_address=end_address,
            buffer_position=context.buffer_position + sector_size,
            sector_size=sector_size,
            buffer_length=buffer_length,
        )

    def _PrepareFlashSectorForWrite(
        self,
        preparation: _FlashWritePreparation,
        sector: list[int],
        first_sector_written: bool,
        current_state: _FlashSectorState,
    ) -> _FlashSectorState | None:
        if preparation.chip_erase:
            return current_state
        return self._PrepareFlashSector(
            sector,
            first_sector_written,
            preparation.sector_offsets,
            preparation.rom_bank_size,
        )

    def _StartFlashROMWrite(self, args: dict[str, Any]) -> tuple[DeviceMode, _FlashWritePreparation | None]:
        mode = self._require_cartridge_mode("writing ROM")
        self.FAST_READ = True
        return mode, self._prepare_flash_write(args, mode)

    def _FlashROM_Worker(self, args: dict[str, Any]) -> bool | None:
        mode, preparation = self._StartFlashROMWrite(args)
        if preparation is None:
            return False
        (
            cart_name,
            cart_type,
            flashcart,
            data_import,
            _data_map_import,
            _flash_offset,
            _active_voltage,
            _mbc,
            end_bank,
            rom_bank_size,
            enable_pullup_wr,
            errmsg_mbc_selection,
            flash_buffer_size,
            command_set_type,
            sector_offsets,
            write_sectors,
            _delta_state_new,
            _json_file,
            chip_erase,
            buffer_len,
            verify_sectors,
        ) = preparation

        pos = 0
        rumble = self._FlashcartHasRumble(flashcart)
        sector_pos = 0

        current_bank: int | None = 0
        start_bank: int = 0
        start_address: int = 0
        buffer_pos: int = 0
        retry_hp: int = 0
        first_sector_written = False
        end_address: int = len(data_import)
        dprint("ROM banks:", end_bank)

        for sector in write_sectors:
            sector_size: int = sector[1]
            sector_state = self._PrepareFlashSectorForWrite(
                preparation,
                sector,
                first_sector_written,
                _FlashSectorState(
                    retry_hp,
                    buffer_pos,
                    start_address,
                    end_address,
                    sector_pos,
                    start_bank,
                    end_bank,
                ),
            )
            if sector_state is None:
                continue
            (
                retry_hp,
                buffer_pos,
                start_address,
                end_address,
                sector_pos,
                start_bank,
                end_bank,
            ) = sector_state

            bank = start_bank

            # ↓↓↓ Check if data matches already
            match_result = self._TrySkipMatchingFlashSector(
                _FlashMatchContext(
                    args,
                    preparation,
                    sector,
                    bank,
                    end_bank,
                    current_bank,
                    start_address,
                    end_address,
                    buffer_pos,
                    sector_pos,
                    buffer_len,
                ),
            )
            (
                matching_sector_skipped,
                match_check_canceled,
                bank,
                current_bank,
                start_address,
                end_address,
                buffer_pos,
                sector_size,
                buffer_len,
            ) = match_result
            if match_check_canceled:
                return None
            if matching_sector_skipped:
                continue
            # ↑↑↑ Check if data matches already

            while bank < end_bank:
                if self._AbortFlashWriteIfCanceled():
                    return None

                status = None
                # ↓↓↓ Switch ROM bank
                start_address, end_address, current_bank, buffer_len = self._SelectFlashWriteBank(
                    _FlashBankContext(
                        _mbc,
                        flashcart,
                        cart_type,
                        bank,
                        current_bank,
                        start_address,
                        end_address,
                        buffer_pos,
                        rom_bank_size,
                        sector[1],
                        buffer_len,
                    ),
                )
                # ↑↑↑ Switch ROM bank

                skip_init = False
                pos = start_address
                dprint(f"buffer_pos=0x{buffer_pos:X}, start_address=0x{start_address:X}, end_address=0x{end_address:X}")

                while pos < end_address:
                    if self._AbortFlashWriteIfCanceled():
                        return None

                    if buffer_pos >= len(data_import):
                        break

                    # ↓↓↓ Sector erase
                    se_ret = None
                    if chip_erase is False and (
                        sector_pos < len(sector_offsets) and buffer_pos == sector_offsets[sector_pos][0]
                    ):
                        ts_se_start = time.time()
                        dprint(f"Erasing sector #{sector_pos:d} at position 0x{buffer_pos:X} (0x{pos:X})")
                        self.SetProgress(
                            {
                                "action": "UPDATE_POS",
                                "pos": buffer_pos,
                                "force_update": True,
                            },
                        )
                        self._PowerOnIfSupported()

                        sector_pos += 1
                        if self.MODE == "DMG" and flashcart.FlashCommandsOnBank1():
                            _mbc.SelectBankROM(bank)
                        self.NO_PROG_UPDATE = True
                        se_ret = flashcart.SectorErase(pos=pos, buffer_pos=buffer_pos, skip=False)
                        self.NO_PROG_UPDATE = False
                        if self.CANCEL_ARGS.get("from_user"):
                            continue
                        if sector not in verify_sectors:
                            verify_sectors.append(sector)

                        ts_se_elapsed = time.time() - ts_se_start
                        if se_ret:
                            self.SetProgress(
                                {
                                    "action": "UPDATE_POS",
                                    "pos": buffer_pos,
                                    "sector_pos": sector_pos,
                                    "sector_erase_time": ts_se_elapsed,
                                    "force_update": True,
                                },
                            )
                            sector_size = se_ret
                            dprint(f"Next sector size: 0x{sector_size:X}")
                        skip_init = False
                    # ↑↑↑ Sector erase

                    if se_ret is not False:
                        status, buffer_len = self._WriteFlashChunk(
                            _FlashChunkParameters(
                                command_set_type,
                                pos,
                                data_import,
                                buffer_pos,
                                buffer_len,
                                bank,
                                flash_buffer_size,
                                skip_init,
                                rumble,
                            ),
                        )

                    if status is False or se_ret is False:
                        self.CANCEL = True
                        self.ERROR = True
                        sr = self._GetFlashWriteStatusRegister(flashcart, se_ret)
                        dprint("Last status register value:", sr)

                        if self.CANCEL_ARGS.get("from_user"):
                            break
                        device = self.DEVICE
                        if device is None or not device.is_open:
                            self.CANCEL_ARGS.update(
                                {
                                    "info_type": "msgbox_critical",
                                    "info_msg": __(
                                        "An error occured while writing {buffer_len} bytes at position {buffer_pos} ({file_size}). Please re-connect the device and try again from the beginning.",
                                        buffer_len=f"0x{buffer_len:X}",
                                        buffer_pos=f"0x{buffer_pos:X}",
                                        file_size=Formatter.file_size(buffer_pos, as_int=False),
                                    )
                                    + "\n\n"
                                    + __(
                                        "Troubleshooting advice:\n"
                                        "- Clean cartridge contacts\n"
                                        "- Check soldering if it's a DIY cartridge\n"
                                        "- Avoid passive USB hubs and try different USB ports/cables\n"
                                        "- Check flashcart profile selection",
                                    )
                                    + errmsg_mbc_selection
                                    + "\n\n"
                                    + __("Status Register:")
                                    + " "
                                    + sr,
                                },
                            )
                            break
                        if chip_erase:
                            retry_hp = 0
                        if "iteration" in self.ERROR_ARGS and self.ERROR_ARGS["iteration"] > 0:
                            retry_hp -= 5
                            if retry_hp <= 0:
                                self.CANCEL_ARGS.update(
                                    {
                                        "info_type": "msgbox_critical",
                                        "info_msg": __(
                                            "Unstable connection detected while writing {buffer_len} bytes in iteration {iteration} at position {buffer_pos} ({file_size}). Please re-connect the device and try again from the beginning.",
                                            buffer_len=f"0x{buffer_len:X}",
                                            iteration=self.ERROR_ARGS["iteration"],
                                            buffer_pos=f"0x{buffer_pos:X}",
                                            file_size=Formatter.file_size(buffer_pos, as_int=False),
                                        )
                                        + "\n\n"
                                        + __(
                                            "Troubleshooting advice:\n"
                                            "- Clean cartridge contacts\n"
                                            "- Check soldering if it's a DIY cartridge\n"
                                            "- Avoid passive USB hubs and try different USB ports/cables",
                                        )
                                        + "\n\n"
                                        + __("Status Register:")
                                        + " "
                                        + sr,
                                    },
                                )
                                continue
                        else:
                            retry_hp -= 10
                            if retry_hp <= 0:
                                enable_pullup_wr_str = " (+ WR pullup)" if enable_pullup_wr == 2 else ""
                                self.CANCEL_ARGS.update(
                                    {
                                        "info_type": "msgbox_critical",
                                        "info_msg": __(
                                            "An error occured while writing {buffer_len} bytes at position {buffer_pos} ({file_size}). Please re-connect the device and try again from the beginning.",
                                            buffer_len=f"0x{buffer_len:X}",
                                            buffer_pos=f"0x{buffer_pos:X}",
                                            file_size=Formatter.file_size(buffer_pos, as_int=False),
                                        )
                                        + "\n\n"
                                        + __(
                                            "Troubleshooting advice:\n"
                                            "- Clean cartridge contacts\n"
                                            "- Check soldering if it's a DIY cartridge\n"
                                            "- Avoid passive USB hubs and try different USB ports/cables\n"
                                            "- Check cartridge ROM storage size (at least {rom_size} is required){errmsg}\n"
                                            "- Check flashcart profile used: {cart_name}{enable_pullup_wr_str}\n"
                                            "- The cartridge may also be incompatible with your {device_name} device",
                                            rom_size=Formatter.file_size(len(data_import), as_int=False),
                                            errmsg=errmsg_mbc_selection,
                                            cart_name=cart_name,
                                            enable_pullup_wr_str=enable_pullup_wr_str,
                                            device_name=self.GetFullNameLabel(),
                                        )
                                        + "\n\n"
                                        + __("Status Register:")
                                        + " "
                                        + sr,
                                        "abortable": False,
                                    },
                                )
                                continue

                        rev_buffer_pos = sector_offsets[sector_pos - 1][0]
                        buffer_pos = rev_buffer_pos
                        bank = start_bank
                        sector_pos -= 1
                        err_text = __(
                            "Write error! Retrying from {address}...",
                            address=f"0x{rev_buffer_pos:X}",
                        )
                        print(ANSI.YELLOW + err_text + ANSI.RESET)
                        dprint(f"Bank {bank:d} | HP: {retry_hp:d}/100")
                        pos = end_address
                        status = False

                        self.SetProgress(
                            {
                                "action": "ERROR",
                                "abortable": True,
                                "pos": buffer_pos,
                                "text": err_text,
                            },
                        )
                        delay = 0.5  # + (100-retry_hp)/100
                        if self.CanPowerCycleCart():
                            self.CartPowerOff()
                            time.sleep(delay)
                            self.CartPowerOn()
                            if self.MODE == "DMG" and _mbc.HasFlashBanks():
                                _mbc.SelectBankFlash(bank)
                        time.sleep(delay)
                        if self.DEVICE is None:
                            raise ConnectionAbortedError(
                                __(
                                    "A critical connection error occured while writing {buffer_len} bytes at position {buffer_pos} ({file_size}). Please re-connect the device and try again from the beginning.",
                                    buffer_len=f"0x{buffer_len:X}",
                                    buffer_pos=f"0x{buffer_pos:X}",
                                    size=f"0x{buffer_len:X}",
                                    pos=f"0x{buffer_pos:X}",
                                    file_size=Formatter.file_size(buffer_pos, as_int=False),
                                ),
                            )

                        self.ERROR = False
                        if self.CANCEL_ARGS.get("from_user"):
                            self.CANCEL_ARGS.update(
                                {
                                    "info_type": "msgbox_warning",
                                    "info_msg": __("The erroneous process has been stopped."),
                                    "abortable": False,
                                },
                            )
                            break
                        self.CANCEL = False
                        self.CANCEL_ARGS = {}
                        device = self._serial_device()
                        device.reset_input_buffer()
                        device.reset_output_buffer()
                        self._cart_write(pos, 0xF0)
                        self._cart_write(pos, 0xFF)
                        if flashcart.Unlock() is False:
                            return False
                        continue

                    skip_init = True

                    buffer_pos += buffer_len
                    pos += buffer_len
                    self.SetProgress({"action": "UPDATE_POS", "pos": buffer_pos})

                if status is not False:
                    bank += 1
            first_sector_written = True

        return self._FinishFlashWrite(args, mode, preparation, buffer_len)

    #################################################################

    def TransferData(
        self,
        args: dict[str, Any],
        signal: ProgressSignal | ProgressCallback,
    ) -> bool | None:
        self.ERROR = False
        self.CANCEL = False
        self.CANCEL_ARGS = {}
        self.READ_ERRORS = 0
        self.WRITE_ERRORS = 0

        if self.IsConnected():
            if self.CanPowerCycleCart():
                self.CartPowerOn()

            ret = False
            self.SIGNAL = signal
            try:
                temp = copy.copy(args)
                if "buffer" in temp:
                    temp["buffer"] = "(0x{:x} bytes of data)".format(len(temp["buffer"]))
                dprint("args:", temp)
                del temp
                self.NO_PROG_UPDATE = False
                if args["mode"] == 1:
                    ret: bool | int | None = self._BackupROM(args)
                elif args["mode"] == 2 or args["mode"] == 3:
                    ret = self._BackupRestoreRAM(args)
                elif args["mode"] == 4:
                    voltage_fallback = args.get("voltage_fallback", False)
                    ask_voltage_fallback = args.get("ask_voltage_fallback", False)
                    if voltage_fallback:
                        self.VOLTAGE_FALLBACK_PENDING = True
                        self.VOLTAGE_FALLBACK_TRIGGERED = False
                    try:
                        ret = self._FlashROM(args)
                    finally:
                        self.VOLTAGE_FALLBACK_PENDING = False
                    if voltage_fallback and self.VOLTAGE_FALLBACK_TRIGGERED:
                        self.VOLTAGE_FALLBACK_TRIGGERED = False
                        self.CANCEL = False
                        self.ERROR = False
                        self.CANCEL_ARGS = {}
                        self.ERROR_ARGS = {}
                        if ask_voltage_fallback:
                            self.USER_ANSWER = None
                            self.SetProgress(
                                {
                                    "action": "USER_ACTION",
                                    "user_action": "RETRY_5V",
                                    "title": __("Retry at 5V?"),
                                    "msg": __(
                                        "Writing at 3.3V failed. Do you want to retry at 5V? Some cartridges of the same kind require 5V for successful writing, but please note that 5V can be unsafe for some flash chips.",
                                    ),
                                },
                            )
                            while self.USER_ANSWER is None:
                                dprint("Waiting for the user to confirm retrying the ROM write at 5V.")
                                time.sleep(1)
                            if self.USER_ANSWER is not True:
                                self.USER_ANSWER = None
                                self.SetProgress({"action": "ABORT", "abortable": False})
                                ret = False
                            else:
                                self.USER_ANSWER = None
                                args["override_voltage"] = voltage_fallback
                                args["voltage_fallback"] = False
                                print(ANSI.YELLOW + __("Note: Writing at 3.3V failed. Retrying at 5V...") + ANSI.RESET)
                                self.SetProgress(
                                    {
                                        "action": "UPDATE_INFO",
                                        "abortable": False,
                                        "text": __("Switching voltage..."),
                                        "pos": 0,
                                        "size": 0,
                                    },
                                )
                                # if self.CanPowerCycleCart(): self.CartPowerCycle()
                                ret = self._FlashROM(args)
                        else:
                            args["override_voltage"] = voltage_fallback
                            args["voltage_fallback"] = False
                            print(ANSI.YELLOW + __("Note: Writing at 3.3V failed. Retrying at 5V...") + ANSI.RESET)
                            self.SetProgress(
                                {
                                    "action": "UPDATE_INFO",
                                    "abortable": False,
                                    "text": __("Switching voltage..."),
                                    "pos": 0,
                                    "size": 0,
                                },
                            )
                            # if self.CanPowerCycleCart(): self.CartPowerCycle()
                            ret = self._FlashROM(args)
                elif args["mode"] == 5:
                    ret = self._DetectCartridge(args)
                elif args["mode"] == 0xFF:
                    self.Debug()
                self.USER_ANSWER = None
                if self.FW is None:
                    return False
                if self.FW["fw_ver"] >= 2 and self.FW["pcb_name"] == "GBxCart RW":
                    if ret is True and "OFW_DONE_LED_ON" in self.DEVICE_CMD:
                        self._write(self.DEVICE_CMD["OFW_DONE_LED_ON"])
                    elif self.ERROR is True and "OFW_ERROR_LED_ON" in self.DEVICE_CMD:
                        self._write(self.DEVICE_CMD["OFW_ERROR_LED_ON"])

            except SerialTimeoutException:
                self.VOLTAGE_FALLBACK_PENDING = False
                print(__("Connection timed out. Please reconnect the device."))
                return False
            except PortNotOpenError:
                self.VOLTAGE_FALLBACK_PENDING = False
                print(__("Connection closed."))
                return False

            return True
        return None
