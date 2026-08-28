# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

import copy
import math
import os
import struct
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Literal, Protocol, TypeAlias, TypedDict, cast

from .app import AppContext
from .i18n import __, c__, format_decimal
from .Logging import dprint, logger

FlashcartProfile: TypeAlias = dict[str, Any]
FlashcartEntry: TypeAlias = FlashcartProfile | str
FlashCommand: TypeAlias = Sequence[int]
FlashCommands: TypeAlias = Sequence[FlashCommand]
ProfileCommandValue: TypeAlias = int | str | None
ProfileCommands: TypeAlias = list[list[ProfileCommandValue]]
SectorMap: TypeAlias = int | list[list[int]]
ProgressInfo: TypeAlias = dict[str, object]


class _CFIRequiredInfo(TypedDict):
    d_swap: list[tuple[int, int]]
    flash_id: bytearray
    magic: str
    vdd_min: float
    vdd_max: float
    single_write: bool
    buffer_write: bool
    sector_erase: bool
    chip_erase: bool
    tb_boot_sector: bool | str
    tb_boot_sector_raw: int
    device_size: int
    erase_sector_regions: int
    erase_sector_blocks: list[list[int]]
    info: str


class CFIInfo(_CFIRequiredInfo, total=False):
    raw: bytearray
    single_write_time_avg: int
    single_write_time_max: int
    buffer_write_time_avg: int
    buffer_write_time_max: int
    sector_erase_time_avg: int
    sector_erase_time_max: int
    chip_erase_time_avg: int
    chip_erase_time_max: int
    buffer_size: int


class CartWriteCallback(Protocol):
    def __call__(
        self,
        address: int,
        value: int,
        *,
        flashcart: bool = False,
        sram: bool = False,
    ) -> object: ...


class CartWriteFastCallback(Protocol):
    def __call__(
        self, commands: FlashCommands, *, flashcart: bool = False
    ) -> object: ...


class CartReadCallback(Protocol):
    def __call__(self, address: int, length: int) -> bytearray: ...


class FlashcartCallbacks(TypedDict):
    cart_write_fncptr: CartWriteCallback
    cart_write_fast_fncptr: CartWriteFastCallback
    cart_read_fncptr: CartReadCallback
    cart_powercycle_fncptr: Callable[[], object]
    progress_fncptr: Callable[[ProgressInfo], object]
    set_we_pin_wr: Callable[[], object]
    set_we_pin_audio: Callable[[], object]


class FlashcartMap(TypedDict):
    DMG: dict[str, FlashcartEntry]
    AGB: dict[str, FlashcartEntry]


class Flashcart:
    def __init__(self, config: FlashcartProfile, fncptr: FlashcartCallbacks) -> None:
        self._cart_write: CartWriteCallback = fncptr["cart_write_fncptr"]
        self._cart_write_fast: CartWriteFastCallback = fncptr["cart_write_fast_fncptr"]
        self._cart_read: CartReadCallback = fncptr["cart_read_fncptr"]
        self._cart_powercycle: Callable[[], object] = fncptr["cart_powercycle_fncptr"]
        self._progress: Callable[[ProgressInfo], object] = fncptr["progress_fncptr"]
        self._set_we_pin_wr: Callable[[], object] = fncptr["set_we_pin_wr"]
        self._set_we_pin_audio: Callable[[], object] = fncptr["set_we_pin_audio"]
        self._config = config
        self._default_we: str | None = None
        self._sector_pos = 0
        self._sector_map: SectorMap | None = None
        self._cfi: CFIInfo | None = None
        self._last_status = 0x00
        if "command_set" in config:
            self._config["_command_set"] = config["command_set"]
        elif "read_identifier" in config and config["read_identifier"][0][1] == 0x90:
            self._config["_command_set"] = "INTEL"
        else:
            self._config["_command_set"] = ""
        if "write_pin" in config:
            self._default_we = config["write_pin"]

    @property
    def CONFIG(self) -> FlashcartProfile:
        """Compatibility view for existing callers; prefer typed methods internally."""
        return self._config

    @property
    def LAST_SR(self) -> int:
        """Compatibility view of the most recently read status register."""
        return self._last_status

    def CartRead(self, address: int, length: int = 0) -> bytearray:
        if self._config["type"].upper() == "AGB":
            if length % 2 == 1:
                length += 1
            if length == 0:
                length = 2
        else:
            if length == 0:
                length = 1
        return self._cart_read(address, length)

    def CartWrite(
        self,
        commands: FlashCommands,
        fast_write: bool = True,
        sram: bool = False,
    ) -> None:
        if "command_set" in self._config and self._config["command_set"] in (
            "GBMEMORY",
            "DMG-MBC5-32M-FLASH",
        ):
            fast_write = False
        if fast_write and not sram:
            self._cart_write_fast(commands, flashcart=True)
        else:
            for command in commands:
                address = command[0]
                value = command[1]
                self._cart_write(address, value, flashcart=fast_write, sram=sram)

    def GetCommandSetType(self) -> str:
        return self._config["_command_set"].upper()

    def GetName(self, index: int = 0) -> str:
        return self._config["names"][index]

    def GetFlashID(self, index: int = 0) -> list[int]:
        return self._config["flash_ids"][index]

    def GetVoltage(self) -> float:
        return self._config["voltage"]

    def GetMBC(self) -> int | str | Literal[False]:
        if (self._config["type"].upper() == "AGB") or ("mbc" not in self._config):
            return False
        mbc = self._config["mbc"]
        return mbc

    def FlashCommandsOnBank1(self) -> bool:
        return (
            "flash_commands_on_bank_1" in self._config
            and self._config["flash_commands_on_bank_1"] is True
        )

    def PulseResetAfterWrite(self) -> bool:
        return (
            "pulse_reset_after_write" in self._config
            and self._config["pulse_reset_after_write"] is True
        )

    def HasRTC(self) -> bool:
        return "rtc" in self._config and self._config["rtc"] is True

    def HasDoubleDie(self) -> bool:
        return "double_die" in self._config and self._config["double_die"] is True

    def SupportsBufferWrite(self) -> bool:
        buffer_size = self.GetBufferSize()
        if buffer_size is False:
            return False
        else:
            return "buffer_write" in self._config["commands"]

    def SupportsPageWrite(self) -> bool:
        buffer_size = self.GetBufferSize()
        if buffer_size is False:
            return False
        else:
            return "page_write" in self._config["commands"]

    def SupportsSingleWrite(self) -> bool:
        return "single_write" in self._config["commands"]

    def SupportsChipErase(self) -> bool:
        return "chip_erase" in self._config["commands"]

    def SupportsSectorErase(self) -> bool:
        return "sector_erase" in self._config["commands"]

    def IsF2A(self) -> bool:
        if "buffer_write" not in self._config["commands"]:
            return False
        for cmd in self._config["commands"]["buffer_write"]:
            if cmd[0] == "SA+2":
                return True
        return False

    def WEisWR(self) -> bool:
        if "write_pin" not in self._config:
            return False
        return self._config["write_pin"] == "WR"

    def WEisAUDIO(self) -> bool:
        if "write_pin" not in self._config:
            return False
        return self._config["write_pin"] in ("AUDIO", "VIN")

    def WEisWR_RESET(self) -> bool:
        if "write_pin" not in self._config:
            return False
        return self._config["write_pin"] == "WR+RESET"

    def GetFlashSize(
        self, default: int | Literal[False] = False
    ) -> int | Literal[False]:
        if "flash_size" not in self._config:
            return default
        return self._config["flash_size"]

    def SetFlashSize(self, size: int) -> None:
        if "flash_size" not in self._config:
            return
        self._config["flash_size"] = size

    def GetBufferSize(self) -> int | Literal[False]:
        if "buffer_size" in self._config:
            return self._config["buffer_size"]
        elif "buffer_write" in self._config["commands"]:
            if "cfi" in self._config:
                cfi = self._config["cfi"]
            else:
                cfi = self.ReadCFI()
                if cfi is False:
                    print(
                        __(
                            "CFI Error: Couldn’t retrieve buffer size from the cartridge."
                        )
                    )
                    if "single_write" in self._config["commands"]:
                        del self._config["commands"]["buffer_write"]
                        print(__("Buffered write disabled."))
                    return False
            if not "buffer_size" in cfi:
                return False
            buffer_size = cfi["buffer_size"]
            dprint("Buffer size was read from CFI data:", cfi["buffer_size"])
            self._config["buffer_size"] = buffer_size
            return buffer_size
        else:
            return False

    def GetCommands(self, key: str) -> ProfileCommands:
        if key not in self._config["commands"]:
            return []
        return self._config["commands"][key]

    def Unlock(self) -> bool:
        self.CartRead(0)  # dummy read
        if "unlock_read" in self._config["commands"]:
            for command in self._config["commands"]["unlock_read"]:
                for _ in range(command[2]):
                    temp = self.CartRead(command[0], command[1])
                    dprint(
                        f"Reading 0x{command[1]:X} bytes from cartridge at 0x{command[0]:X} = {temp!s:s}"
                    )
            time.sleep(0.001)
        if "unlock" in self._config["commands"]:
            self.CartWrite(self._config["commands"]["unlock"], fast_write=False)
            time.sleep(0.001)
        return True

    def Reset(self, full_reset: bool = False, max_address: int = 0x2000000) -> bool:
        if full_reset and "power_cycle" in self._config:
            self._cart_powercycle()
            time.sleep(0.001)
            if self.Unlock() is False:
                return False
        elif (
            full_reset
            and "reset_every" in self._config
            and "flash_size" in self._config
        ):
            for j in range(0, self._config["flash_size"], self._config["reset_every"]):
                if j >= max_address:
                    break
                dprint(f"reset_every @ 0x{j:X}")
                for command in self._config["commands"]["reset"]:
                    self.CartWrite([[j + command[0], command[1]]])
                    # time.sleep(0.01)
        elif "reset" in self._config["commands"]:
            self.CartWrite(self._config["commands"]["reset"])
        return True

    def _VerifyFlashID(self, config: FlashcartProfile) -> tuple[bool, list[int]]:
        if "read_identifier" not in config["commands"]:
            return (False, [])
        if len(config["flash_ids"]) == 0:
            return (False, [])
        if "power_cycle" in config and config["power_cycle"] is True:
            self._cart_powercycle()
        self.Reset()
        rom = list(self.CartRead(0, len(config["flash_ids"][0])))
        self.Unlock()
        self.CartWrite(config["commands"]["read_identifier"])
        time.sleep(0.001)
        read_identifier_at = 0
        if "read_identifier_at" in config:
            read_identifier_at = config["read_identifier_at"]
        cart_flash_id = list(
            self.CartRead(read_identifier_at, len(config["flash_ids"][0]))
        )
        self.Reset()
        dprint(config["names"], config["commands"]["read_identifier"])
        dprint(
            "Flash ID: {:s}".format(" ".join(format(x, "02X") for x in cart_flash_id))
        )
        verified = True
        if rom == cart_flash_id:
            dprint("ROM data matched Flash ID response.")
            verified = False
        elif cart_flash_id not in config["flash_ids"]:
            dprint("This Flash ID does not exist in flashcart handler file.")
            verified = False
        return (verified, cart_flash_id)

    def VerifyFlashID(self) -> tuple[bool, list[int]]:
        if "flash_ids_banks" in self._config:
            bank_flash_ids = cast(
                Sequence[Sequence[int]], self._config["flash_ids_banks"]
            )
            if not bank_flash_ids:
                return (False, [])
            cart_flash_ids: list[list[int]] = []
            verified = False
            for i, bank_flash_id in enumerate(bank_flash_ids):
                self.SelectBankROM(i)
                config = copy.copy(self._config)
                config["flash_ids"] = [bank_flash_id]
                del config["flash_ids_banks"]
                (verified, cart_flash_id) = self._VerifyFlashID(config)
                cart_flash_ids.append(cart_flash_id)
                if not verified:
                    return (verified, cart_flash_id)
            cart_flash_id = cart_flash_ids[0]
            self.SelectBankROM(0)
        else:
            (verified, cart_flash_id) = self._VerifyFlashID(self._config)
        return (verified, cart_flash_id)

    def ReadCFI(self) -> CFIInfo | Literal[False]:
        if self._cfi is not None:
            return self._cfi
        if "read_cfi" not in self._config["commands"]:
            if self._config["_command_set"] == "INTEL":
                self._config["commands"]["read_cfi"] = [[0, 0x98]]
            elif self._config["_command_set"] == "AMD":
                self._config["commands"]["read_cfi"] = [[0xAA, 0x98]]

        if "read_cfi" in self._config["commands"]:
            self.CartWrite(self._config["commands"]["read_cfi"])
            time.sleep(0.1)
            buffer = self.CartRead(0, 0x400)
            self.Reset()
            cfi = CFI().Parse(buffer)
            if cfi is not False:
                cfi["raw"] = buffer
            dprint(cfi)
            if cfi is not False:
                self._cfi = cfi
                self._config["cfi"] = cfi
            return cfi
        return False

    def GetSmallestSectorSize(self) -> int | Literal[False]:
        sector_map = self.GetSectorMap()
        if sector_map is False:
            return False
        if isinstance(sector_map, int):
            return sector_map
        smallest_sector_size = sector_map[0][0]
        for sector in sector_map:
            smallest_sector_size = min(smallest_sector_size, sector[0])
        return smallest_sector_size

    def GetSectorOffsets(
        self, rom_size: int = 0, rom_bank_size: int = 0x4000
    ) -> list[list[int]]:
        regions = self.GetSectorMap()
        pos = 0
        offsets: list[list[int]] = []
        if isinstance(regions, list):
            for region in regions:
                size = region[0]
                count = region[1]
                for _ in range(count):
                    offsets.append([pos, size])
                    pos += size
        elif regions is not False:
            while pos < rom_size:
                dprint("Adding extra sector:", pos, regions)
                offsets.append([pos, regions])
                pos += regions
        return offsets

    def GetSectorMap(self) -> SectorMap | Literal[False]:
        if self._sector_map is not None:
            return self._sector_map
        elif "sector_size" in self._config:
            return self._config["sector_size"]
        elif "sector_erase" in self._config["commands"]:
            if "cfi" in self._config:
                cfi = self._config["cfi"]
            else:
                cfi = self.ReadCFI()
                if cfi is False:
                    print(
                        __(
                            "CFI Error: Couldn’t retrieve sector size map from the cartridge."
                        )
                    )
                    if "chip_erase" in self._config["commands"]:
                        del self._config["commands"]["sector_erase"]
                        print(__("Sector erase mode disabled."))
                    return False
            sector_size = cfi["erase_sector_blocks"]
            if cfi["tb_boot_sector_raw"] == 0x03:
                sector_size.reverse()
            dprint(
                "Sector size map was read from CFI data:", cfi["erase_sector_blocks"]
            )
            self._config["sector_size"] = sector_size
            return sector_size
        else:
            return False

    def ChipErase(self) -> bool:
        self.Reset(full_reset=True)
        time_start = time.time()
        self._progress(
            {
                "action": "ERASE",
                "time_start": time_start,
                "time_estimated": self._config["chip_erase_timeout"],
                "abortable": False,
            }
        )
        for i in range(len(self._config["commands"]["chip_erase"])):
            addr = self._config["commands"]["chip_erase"][i][0]
            data = self._config["commands"]["chip_erase"][i][1]
            if len(self._config["commands"]["chip_erase"][i]) > 2:
                we = self._config["commands"]["chip_erase"][i][2]
            else:
                we = None

            if addr != None:
                if we == "WR":
                    self._set_we_pin_wr()
                elif we == "AUDIO":
                    self._set_we_pin_audio()
                self.CartWrite([[addr, data]])
                if we is not None:
                    if self._default_we == "WR":
                        self._set_we_pin_wr()
                    elif self._default_we == "AUDIO":
                        self._set_we_pin_audio()

            time.sleep(0.1)
            if self._config["commands"]["chip_erase_wait_for"][i][0] != None:
                addr = self._config["commands"]["chip_erase_wait_for"][i][0]
                data = self._config["commands"]["chip_erase_wait_for"][i][1]
                timeout = self._config["chip_erase_timeout"]
                while True:
                    self._progress(
                        {
                            "action": "ERASE",
                            "time_start": time_start,
                            "time_estimated": self._config["chip_erase_timeout"],
                            "abortable": False,
                        }
                    )
                    if self._config.get("wait_read_status_register"):
                        for j in range(
                            len(self._config["commands"]["read_status_register"])
                        ):
                            sr_data = self._config["commands"]["read_status_register"][
                                j
                            ][1]

                            if we == "WR":
                                self._set_we_pin_wr()
                            elif we == "AUDIO":
                                self._set_we_pin_audio()
                            self.CartWrite([[addr, sr_data]])
                            if we is not None:
                                if self._default_we == "WR":
                                    self._set_we_pin_wr()
                                elif self._default_we == "AUDIO":
                                    self._set_we_pin_audio()

                    self.CartRead(addr, 2)  # dummy read (fixes some bootlegs)
                    temp = self.CartRead(addr, 2)
                    if len(temp) < 2:
                        dprint("Communication error 1 in ChipErase():", temp)
                        return False
                    wait_for = struct.unpack("<H", temp)[0]
                    self._last_status = wait_for
                    dprint(
                        "Status Register Check: 0x{:X} & 0x{:X} == 0x{:X}? {:s}".format(
                            wait_for,
                            self._config["commands"]["chip_erase_wait_for"][i][2],
                            data,
                            str(
                                (
                                    wait_for
                                    & self._config["commands"]["chip_erase_wait_for"][
                                        i
                                    ][2]
                                )
                                == data
                            ),
                        )
                    )
                    wait_for = (
                        wait_for & self._config["commands"]["chip_erase_wait_for"][i][2]
                    )
                    if wait_for == data:
                        break
                    time.sleep(0.5)
                    timeout -= 0.5
                    if timeout <= 0:
                        self._progress(
                            {
                                "action": "ABORT",
                                "info_type": "msgbox_critical",
                                "info_msg": __(
                                    "Erasing the flash chip timed out. The last status register value was {value}.",
                                    value=f"0x{self._last_status:X}",
                                )
                                + "\n\n"
                                + __(
                                    "Please make sure that the cartridge contacts are clean, and that the selected flashcart profile and settings are correct."
                                ),
                                "abortable": False,
                            }
                        )
                        return False
        self.Reset(full_reset=True)
        return True

    def SectorErase(
        self, pos: int = 0, buffer_pos: int = 0, skip: bool = False
    ) -> int | Literal[False]:
        if not skip:
            self.Reset(full_reset=False)
            if "sector_erase" not in self._config["commands"]:
                return False
            for i in range(len(self._config["commands"]["sector_erase"])):
                addr = self._config["commands"]["sector_erase"][i][0]
                data = self._config["commands"]["sector_erase"][i][1]
                if len(self._config["commands"]["sector_erase"][i]) > 2:
                    we = self._config["commands"]["sector_erase"][i][2]
                else:
                    we = None

                if addr == "SA":
                    addr = pos
                if addr == "SA+1":
                    addr = pos + 1
                if addr == "SA+2":
                    addr = pos + 2
                if addr == "SA+16384":
                    addr = pos + 0x4000
                if addr == "SA+28672":
                    addr = pos + 0x7000
                if addr == "SA+66":
                    addr = pos + 0x42
                if addr == "SA+132":
                    addr = pos + 0x84
                if addr != None:
                    if we == "WR":
                        self._set_we_pin_wr()
                    elif we == "AUDIO":
                        self._set_we_pin_audio()
                    self.CartWrite([[addr, data]])
                    if we is not None:
                        if self._default_we == "WR":
                            self._set_we_pin_wr()
                        elif self._default_we == "AUDIO":
                            self._set_we_pin_audio()

                if self._config["commands"]["sector_erase_wait_for"][i][0] != None:
                    addr = self._config["commands"]["sector_erase_wait_for"][i][0]
                    data = self._config["commands"]["sector_erase_wait_for"][i][1]
                    if addr == "SA":
                        addr = pos
                    if addr == "SA+1":
                        addr = pos + 1
                    if addr == "SA+2":
                        addr = pos + 2
                    if addr == "SA+16384":
                        addr = pos + 0x4000
                    if addr == "SA+28672":
                        addr = pos + 0x7000
                    if addr == "SA+66":
                        addr = pos + 0x42
                    if addr == "SA+132":
                        addr = pos + 0x84
                    time.sleep(0.05)
                    timeout = 100
                    while True:
                        if (
                            "wait_read_status_register" in self._config
                            and self._config["wait_read_status_register"] == True
                        ):
                            for j in range(
                                len(self._config["commands"]["read_status_register"])
                            ):
                                sr_addr = self._config["commands"][
                                    "read_status_register"
                                ][j][0]
                                sr_data = self._config["commands"][
                                    "read_status_register"
                                ][j][1]

                                if we == "WR":
                                    self._set_we_pin_wr()
                                elif we == "AUDIO":
                                    self._set_we_pin_audio()
                                self.CartWrite([[sr_addr, sr_data]])
                                if we is not None:
                                    if self._default_we == "WR":
                                        self._set_we_pin_wr()
                                    elif self._default_we == "AUDIO":
                                        self._set_we_pin_audio()

                        self.CartRead(addr, 2)  # dummy read (fixes some bootlegs)
                        temp = self.CartRead(addr, 2)
                        if len(temp) != 2:
                            dprint("Communication error 1 in SectorErase():", temp)
                            return False
                        wait_for = self.CartRead(addr, 2)
                        if len(wait_for) != 2:
                            dprint("Communication error 2 in SectorErase():", temp)
                            return False
                        wait_for = struct.unpack("<H", wait_for)[0]
                        self._last_status = wait_for
                        dprint(
                            "Status Register Check: 0x{:X} & 0x{:X} == 0x{:X}? {:s}".format(
                                wait_for,
                                self._config["commands"]["sector_erase_wait_for"][i][2],
                                data,
                                str(
                                    wait_for
                                    & self._config["commands"]["sector_erase_wait_for"][
                                        i
                                    ][2]
                                    == data
                                ),
                            )
                        )
                        wait_for = (
                            wait_for
                            & self._config["commands"]["sector_erase_wait_for"][i][2]
                        )
                        time.sleep(0.05)
                        timeout -= 1
                        if timeout < 1:
                            dprint(
                                f"Timeout error in SectorErase(): 0x{self._last_status:X}"
                            )
                            # self._progress({"action":"ABORT", "info_type":"msgbox_critical", "info_msg":"The sector erase attempt timed out. The last status register value was 0x{:X}.\n\nPlease make sure that the cartridge contacts are clean, and that the selected flashcart profile and settings are correct.".format(self._last_status), "abortable":False})
                            return False
                        if wait_for == data:
                            break
                        self._progress(
                            {
                                "action": "SECTOR_ERASE",
                                "sector_pos": buffer_pos,
                                "time_start": time.time(),
                                "abortable": True,
                            }
                        )
                    dprint("Done waiting!")

            self.Reset(full_reset=False)

        raw_sector_map = self._config.get("sector_size")
        if raw_sector_map is None:
            return False
        if isinstance(raw_sector_map, list):
            sector_map = cast(list[list[int]], raw_sector_map)
            try:
                sector_map[self._sector_pos][1] -= 1
                if (sector_map[self._sector_pos][1] == 0) and (
                    len(sector_map) > self._sector_pos + 1
                ):
                    self._sector_pos += 1
                return sector_map[self._sector_pos][0]
            except (IndexError, TypeError) as e:
                dprint(f"Warning: Sector map is smaller than expected: {e}")
                self._sector_pos = max(0, self._sector_pos - 1)
                return False
        if isinstance(raw_sector_map, int):
            return raw_sector_map
        return False

    def HasBanks(self) -> bool:
        return "flash_bank_select_type" in self._config

    def SelectBankROM(self, index: int) -> bool:
        if "flash_bank_select_type" not in self._config:
            return False
        dprint(f"Setting flash bank to {index:d}")
        if self._config["flash_bank_select_type"] == 1:
            index = index & 0xF
            self.CartWrite([[2, index << 4]], sram=True)
            self.CartWrite([[3, 0x40]], sram=True)
            self.CartWrite([[4, 0x00]], sram=True)
            return True
        elif self._config["flash_bank_select_type"] == 2:  # Flash2Advance Ultra
            bank1 = 0 if index < 4 else 0x10
            bank2 = index % 4 * 0x400
            self.CartWrite([[0x987654 * 2, 0x5354]], fast_write=False)
            self.CartWrite([[0xE12345 * 2, 0xA55A]], fast_write=False)
            self.CartWrite([[6, bank1]], fast_write=False, sram=True)
            self.CartWrite([[0x987654 * 2, 0x5354]], fast_write=False)
            self.CartWrite([[0xB5AC97 * 2, bank2]], fast_write=False)
            self.CartWrite([[0x987654 * 2, 0x5354]], fast_write=False)
            self.CartWrite([[0xF12345 * 2, 0x9413]], fast_write=False)
            return True

        return False


class CFI:
    @classmethod
    def swap_bits(cls, n: int, pair: tuple[int, int]) -> int:
        p, q = pair
        if (((n & (1 << p)) >> p) ^ ((n & (1 << q)) >> q)) == 1:
            n ^= 1 << p
            n ^= 1 << q
        return n

    def Parse(
        self, buffer: bytes | bytearray | memoryview | Literal[False]
    ) -> CFIInfo | Literal[False]:
        if buffer is False or buffer == b"":
            return False
        buffer = bytearray(buffer)
        if len(buffer) < 0x400:
            return False
        magic = f"{chr(buffer[0x20]):s}{chr(buffer[0x22]):s}{chr(buffer[0x24]):s}"

        if magic == "QRY":  # nothing swapped
            d_swap = [(0, 0)]
        elif magic == "RQZ":  # D0D1 swapped
            d_swap = [(0, 1)]
        elif magic == "\x92\x91\x9a":  # D0D1+D6D7 swapped
            d_swap = [(0, 1), (6, 7)]
        else:
            return False

        info = cast(CFIInfo, {"d_swap": d_swap})
        for pair in d_swap:
            for j in range(len(buffer)):
                buffer[j] = CFI.swap_bits(buffer[j], pair)
        try:
            info["flash_id"] = buffer[0:8]
            info["magic"] = (
                f"{chr(buffer[0x20]):s}{chr(buffer[0x22]):s}{chr(buffer[0x24]):s}"
            )

            if buffer[0x36] == 0xFF and buffer[0x48] == 0xFF:
                print(
                    __(
                        "Warning: No information about the voltage range found in CFI data."
                    )
                )
                try:
                    with open(
                        AppContext.CONFIG_PATH + os.sep + "cfi_debug.bin", "wb"
                    ) as f:
                        f.write(buffer)
                except Exception:
                    logger.exception("Failed to write CFI diagnostics")
                return False

            pri_address = (buffer[0x2A] | (buffer[0x2C] << 8)) * 2
            if (pri_address + 0x3C) >= 0x400:
                pri_address = 0x80

            info["vdd_min"] = (buffer[0x36] >> 4) + ((buffer[0x36] & 0x0F) / 10)
            info["vdd_max"] = (buffer[0x38] >> 4) + ((buffer[0x38] & 0x0F) / 10)

            if buffer[0x3E] > 0 and buffer[0x3E] < 0xFF:
                info["single_write"] = True
                info["single_write_time_avg"] = int(math.pow(2, buffer[0x3E]))
                info["single_write_time_max"] = int(
                    math.pow(2, buffer[0x46]) * info["single_write_time_avg"]
                )
            else:
                info["single_write"] = False

            if buffer[0x40] > 0 and buffer[0x40] < 0xFF:
                info["buffer_write"] = True
                info["buffer_write_time_avg"] = int(math.pow(2, buffer[0x40]))
                info["buffer_write_time_max"] = int(
                    math.pow(2, buffer[0x48]) * info["buffer_write_time_avg"]
                )
            else:
                info["buffer_write"] = False

            if buffer[0x42] > 0 and buffer[0x42] < 0xFF:
                info["sector_erase"] = True
                info["sector_erase_time_avg"] = int(math.pow(2, buffer[0x42]))
                info["sector_erase_time_max"] = int(
                    math.pow(2, buffer[0x4A]) * info["sector_erase_time_avg"]
                )
            else:
                info["sector_erase"] = False

            if buffer[0x44] > 0 and buffer[0x44] < 0xFF:
                info["chip_erase"] = True
                info["chip_erase_time_avg"] = int(math.pow(2, buffer[0x44]))
                info["chip_erase_time_max"] = int(
                    math.pow(2, buffer[0x4C]) * info["chip_erase_time_avg"]
                )
            else:
                info["chip_erase"] = False

            info["tb_boot_sector"] = False
            info["tb_boot_sector_raw"] = 0
            if (
                f"{chr(buffer[pri_address]):s}{chr(buffer[pri_address + 2]):s}{chr(buffer[pri_address + 4]):s}"
                == "PRI"
                and buffer[pri_address + 0x1E] not in (0, 0xFF)
            ):
                temp = {0x02: "As shown", 0x03: "Reversed"}
                info["tb_boot_sector_raw"] = buffer[pri_address + 0x1E]
                try:
                    info["tb_boot_sector"] = (
                        f"{temp[buffer[pri_address + 0x1E]]:s} (0x{buffer[pri_address + 0x1E]:02X})"
                    )
                except Exception:
                    info["tb_boot_sector"] = f"0x{buffer[pri_address + 0x1E]:02X}"

            info["device_size"] = int(math.pow(2, buffer[0x4E]))
            info["buffer_size"] = buffer[0x56] << 8 | buffer[0x54]
            if info["buffer_size"] > 1:
                info["buffer_write"] = True
                info["buffer_size"] = int(math.pow(2, info["buffer_size"]))
            else:
                del info["buffer_size"]
                info["buffer_write"] = False
            info["erase_sector_regions"] = buffer[0x58]
            info["erase_sector_blocks"] = []
            pos = 0
            for i in range(min(4, info["erase_sector_regions"])):
                b = (buffer[0x5C + (i * 8)] << 8 | buffer[0x5A + (i * 8)]) + 1
                t = (buffer[0x60 + (i * 8)] << 8 | buffer[0x5E + (i * 8)]) * 256
                size = b * t
                pos += size
                info["erase_sector_blocks"].append([t, b, size])

        except Exception as err:
            print(
                __(
                    "Error: Trying to parse CFI data resulted in an error: {err}",
                    err=str(err),
                )
            )
            try:
                with open(AppContext.CONFIG_PATH + os.sep + "cfi_debug.bin", "wb") as f:
                    f.write(buffer)
            except Exception as e:
                logger.exception(f"Failed to write CFI diagnostics: {e}")
            return False

        s = ""
        if info["d_swap"] != [(0, 0)]:
            s += __("Swapped pins: {pins}", pins=str(info["d_swap"])) + "\n"
        s += (
            __(
                "Device size: {size_hex} ({size_mib})",
                size_hex="0x{:07X}".format(info["device_size"]),
                size_mib=format_decimal(info["device_size"] / 1024 / 1024, precision=2)
                + __(" MiB"),
            )
            + "\n"
        )
        s += (
            __(
                "Voltage: {min_v}–{max_v} V",
                min_v=format_decimal(info["vdd_min"], precision=1),
                max_v=format_decimal(info["vdd_max"], precision=1),
            )
            + "\n"
        )
        s += (
            __(
                "Single write: {val}",
                val=c__("ROM Write Method", "Supported")
                if info["single_write"]
                else c__("ROM Write Method", "Not supported"),
            )
            + "\n"
        )
        if "buffer_size" in info:
            s += (
                __(
                    "Buffered write: {val}",
                    val=c__("ROM Write Method", "Supported")
                    if info["buffer_write"]
                    else c__("ROM Write Method", "Not supported"),
                )
                + " "
                + "({bytes})".format(bytes=str(info["buffer_size"]) + __(" Bytes"))
                + "\n"
            )
        else:
            s += (
                __(
                    "Buffered write: {val}",
                    val=c__("ROM Write Method", "Supported")
                    if info["buffer_write"]
                    else c__("ROM Write Method", "Not supported"),
                )
                + "\n"
            )
        if (
            info["chip_erase"]
            and "chip_erase_time_avg" in info
            and "chip_erase_time_max" in info
        ):
            s += (
                __(
                    "Chip erase: {avg}–{max} ms",
                    avg=str(info["chip_erase_time_avg"]),
                    max=str(info["chip_erase_time_max"]),
                )
                + "\n"
            )
        if (
            info["sector_erase"]
            and "sector_erase_time_avg" in info
            and "sector_erase_time_max" in info
        ):
            s += (
                __(
                    "Sector erase: {avg}–{max} ms",
                    avg=str(info["sector_erase_time_avg"]),
                    max=str(info["sector_erase_time_max"]),
                )
                + "\n"
            )
        if info["tb_boot_sector"] is not False:
            s += __("Sector flags: {flags}", flags=str(info["tb_boot_sector"])) + "\n"
        pos = 0
        oversize = False
        s = s[:-1]
        for i in range(info["erase_sector_regions"]):
            esb = info["erase_sector_blocks"][i]
            if oversize:
                s += "\n" + __(
                    "Region {region}: {start}–{end} @ {size} × {count} (alternative)",
                    region=str(i + 1),
                    start=f"0x{pos:07X}",
                    end=f"0x{pos + esb[2] - 1:07X}",
                    size=f"0x{esb[0]:X}" + __(" Bytes"),
                    count=str(esb[1]),
                )
            else:
                s += "\n" + __(
                    "Region {region}: {start}–{end} @ {size} × {count}",
                    region=str(i + 1),
                    start=f"0x{pos:07X}",
                    end=f"0x{pos + esb[2] - 1:07X}",
                    size=f"0x{esb[0]:X}" + __(" Bytes"),
                    count=str(esb[1]),
                )
            pos += esb[2]
            if pos >= info["device_size"]:
                pos = 0
                oversize = True
        info["info"] = s

        return info


class Flashcart_AGB_GBAMP(Flashcart):
    def SectorErase(
        self, pos: int = 0, buffer_pos: int = 0, skip: bool = False
    ) -> int | Literal[False]:
        ret: int | Literal[False] = False
        for i in range(4):
            sector = pos >> 13 << 16 | (pos & 0x1FFF) + (i * 4)
            ret = super().SectorErase(sector, buffer_pos, skip)
            if ret is False:
                break
        return ret

    def VerifyFlashID(self) -> tuple[bool, list[int]]:
        self._cart_powercycle()
        verified = False
        self.Unlock()
        rom = list(self.CartRead(0x1E8F << 1, 2) + self.CartRead(0x168F << 1, 2))
        self.CartWrite(self._config["commands"]["read_identifier"], fast_write=True)
        cart_flash_id = list(
            self.CartRead(0x1E8F << 1, 2) + self.CartRead(0x168F << 1, 2)
        )
        if rom != cart_flash_id and cart_flash_id == self._config["flash_ids"][0]:
            self.CartWrite(self._config["commands"]["reset"], fast_write=True)
            verified = True
        dprint(verified, rom, cart_flash_id)
        return (verified, cart_flash_id)


class Flashcart_DMG_BUNG_16M(Flashcart):
    def SupportsSectorErase(self) -> bool:
        return False

    def SupportsChipErase(self) -> bool:
        return True

    def ChipErase(self, pos: int = 0, buffer_pos: int = 0, skip: bool = False) -> bool:
        time_start = time.time()
        self._progress(
            {"action": "ERASE", "time_start": time_start, "abortable": False}
        )

        self.CartWrite([[0x2000, 0x02]], fast_write=False)
        self.CartWrite([[0x6AAA, 0xAA]], fast_write=True)
        self.CartWrite([[0x2000, 0x01]], fast_write=False)
        self.CartWrite([[0x5554, 0x55]], fast_write=True)
        self.CartWrite([[0x2000, 0x02]], fast_write=False)
        self.CartWrite([[0x6AAA, 0x80]], fast_write=True)
        self.CartWrite([[0x2000, 0x02]], fast_write=False)
        self.CartWrite([[0x6AAA, 0xAA]], fast_write=True)
        self.CartWrite([[0x2000, 0x01]], fast_write=False)
        self.CartWrite([[0x5554, 0x55]], fast_write=True)
        self.CartWrite([[0x2000, 0x02]], fast_write=False)
        self.CartWrite([[0x6AAA, 0x10]], fast_write=True)

        lives = 10
        while lives > 0:
            raw = self.CartRead(0)
            sr = raw[0] if raw else 0
            self._last_status = sr
            dprint(
                f"Status Register Check: 0x{sr:X} & 0x{0x80:X} == 0x{0x80:X}? {(sr & 0x80) == 0x80!s:s}"
            )
            if (sr & 0x80) == 0x80:
                break
            time.sleep(0.5)
            lives -= 1
        if lives == 0:
            self._progress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": __(
                        "Erasing the flash chip timed out. The last status register value was {value}.",
                        value=f"0x{self._last_status:X}",
                    )
                    + "\n\n"
                    + __(
                        "Please make sure that the cartridge contacts are clean, and that the selected flashcart profile and settings are correct."
                    ),
                    "abortable": False,
                }
            )
            return False

        self.Reset()
        return True

    def Reset(self, full_reset: bool = False, max_address: int = 0x2000000) -> bool:
        self.CartWrite([[0x2000, 0x02]], fast_write=False)
        self.CartWrite([[0x6AAA, 0xAA]], fast_write=True)
        self.CartWrite([[0x2000, 0x01]], fast_write=False)
        self.CartWrite([[0x5554, 0x55]], fast_write=True)
        self.CartWrite([[0x2000, 0x02]], fast_write=False)
        self.CartWrite([[0x6AAA, 0xF0]], fast_write=True)
        return True

    def VerifyFlashID(self) -> tuple[bool, list[int]]:
        rom = list(self.CartRead(0, 4))
        self.CartWrite([[0x2000, 0x02]], fast_write=False)
        self.CartWrite([[0x6AAA, 0xAA]], fast_write=True)
        self.CartWrite([[0x2000, 0x01]], fast_write=False)
        self.CartWrite([[0x5554, 0x55]], fast_write=True)
        self.CartWrite([[0x2000, 0x02]], fast_write=False)
        self.CartWrite([[0x6AAA, 0x90]], fast_write=True)
        cart_flash_id = list(self.CartRead(0, 4))
        verified = False
        if rom != cart_flash_id and cart_flash_id == self._config["flash_ids"][0]:
            self.Reset()
            verified = True
        return (verified, cart_flash_id)


class Flashcart_DMG_MMSA(Flashcart):
    def ReadCFI(self) -> Literal[False]:
        return False

    def GetMBC(self) -> int:
        return 0x105

    def SupportsSectorErase(self) -> bool:
        return False

    def SupportsChipErase(self) -> bool:
        return True

    def EraseHiddenSector(self, buffer: bytes | bytearray) -> bool:
        self._progress(
            {
                "action": "SECTOR_ERASE",
                "sector_pos": 0,
                "time_start": time.time(),
                "abortable": False,
            }
        )

        if self.UnlockForWriting() is False:
            return False

        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0xAA],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x2A],
            [0x126, 0xAA],
            [0x127, 0x55],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0x60],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0xAA],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x2A],
            [0x126, 0xAA],
            [0x127, 0x55],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0x04],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        lives = 10
        while lives > 0:
            self._progress(
                {
                    "action": "SECTOR_ERASE",
                    "sector_pos": 0,
                    "time_start": time.time(),
                    "abortable": False,
                }
            )
            raw = self.CartRead(0)
            sr = raw[0] if raw else 0
            self._last_status = sr
            dprint(
                f"Status Register Check: 0x{sr:X} & 0x{0x80:X} == 0x{0x80:X}? {(sr & 0x80) == 0x80!s:s}"
            )
            if (sr & 0x80) == 0x80:
                break
            time.sleep(0.5)
            lives -= 1
        if lives == 0:
            self._progress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": __(
                        "Erasing the hidden sector timed out. The last status register value was {value}.",
                        value=f"0x{self._last_status:X}",
                    )
                    + "\n\n"
                    + __(
                        "Please make sure that the cartridge contacts are clean, and that the selected flashcart profile and settings are correct."
                    ),
                    "abortable": False,
                }
            )
            return False

        # Write Hidden Sector
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0xAA],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x2A],
            [0x126, 0xAA],
            [0x127, 0x55],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0x60],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0xAA],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x2A],
            [0x126, 0xAA],
            [0x127, 0x55],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0xE0],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x2100, 0x01],
        ]
        self.CartWrite(cmds)

        # Disable writes to MBC registers
        cmds = [
            [0x120, 0x10],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        # Undo Wakeup
        cmds = [
            [0x120, 0x08],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        return True

    def ChipErase(self) -> bool:
        time_start = time.time()
        self._progress(
            {"action": "ERASE", "time_start": time_start, "abortable": False}
        )

        if self.UnlockForWriting() is False:
            return False

        # Erase Chip
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0xAA],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x2A],
            [0x126, 0xAA],
            [0x127, 0x55],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0x80],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0xAA],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x2A],
            [0x126, 0xAA],
            [0x127, 0x55],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0x10],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        lives = 10
        while lives > 0:
            self._progress(
                {"action": "ERASE", "time_start": time_start, "abortable": False}
            )
            raw = self.CartRead(0)
            sr = raw[0] if raw else 0
            self._last_status = sr
            dprint(
                f"Status Register Check: 0x{sr:X} & 0x{0x80:X} == 0x{0x80:X}? {(sr & 0x80) == 0x80!s:s}"
            )
            if (sr & 0x80) == 0x80:
                break
            time.sleep(0.5)
            lives -= 1
        if lives == 0:
            self._progress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": __(
                        "Erasing the flash chip timed out. The last status register value was {value}.",
                        value=f"0x{self._last_status:X}",
                    )
                    + "\n\n"
                    + __(
                        "Please make sure that the cartridge contacts are clean, and that the selected flashcart profile and settings are correct."
                    ),
                    "abortable": False,
                }
            )
            return False

        # Reset flash to read mode
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x40],
            [0x126, 0x80],
            [0x127, 0xF0],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)

        # Map all the flash memory before writing
        cmds = [
            [0x120, 0x04],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        return True

    def Unlock(self) -> bool:
        return self.UnlockForWriting()

    def UnlockForWriting(self) -> bool:
        time_start = time.time()
        self._progress(
            {"action": "UNLOCK", "time_start": time_start, "abortable": False}
        )

        self.CartWrite([[0x2100, 0x01]])
        # Enable Flash Chip Access
        cmds = [
            [0x120, 0x09],
            [0x121, 0xAA],
            [0x122, 0x55],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        # Re-Enable writes to MBC registers
        cmds = [
            [0x120, 0x11],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        # Disable flash chip protection
        cmds = [
            [0x120, 0x0A],
            [0x125, 0x62],
            [0x126, 0x04],
            [0x13F, 0xA5],
            [0x120, 0x02],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        self.CartWrite([[0x2100, 0x01]])

        # Suspend potential previous erase
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x00],
            [0x126, 0x00],
            [0x127, 0xB0],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)

        # Unlock Hidden Sector
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0xAA],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x2A],
            [0x126, 0xAA],
            [0x127, 0x55],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0x60],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x55],
            [0x126, 0x55],
            [0x127, 0xAA],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x2A],
            [0x126, 0xAA],
            [0x127, 0x55],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        cmds = [
            [0x120, 0x0F],
            [0x125, 0x00],
            [0x126, 0x00],
            [0x127, 0x40],
            [0x13F, 0xA5],
        ]
        self.CartWrite(cmds)
        lives = 10
        while lives > 0:
            raw = self.CartRead(0)
            sr = raw[0] if raw else 0
            self._last_status = sr
            dprint(
                f"Status Register Check: 0x{sr:X} & 0x{0x80:X} == 0x{0x80:X}? {(sr & 0x80) == 0x80!s:s}"
            )
            if (sr & 0x80) == 0x80:
                break
            self._progress(
                {"action": "UNLOCK", "time_start": time_start, "abortable": False}
            )
            time.sleep(0.5)
            lives -= 1
        if lives == 0:
            self._progress(
                {
                    "action": "ABORT",
                    "info_type": "msgbox_critical",
                    "info_msg": __(
                        "Unlocking the hidden sector timed out. The last status register value was {value}.",
                        value=f"0x{self._last_status:X}",
                    )
                    + "\n\n"
                    + __(
                        "Please make sure that the cartridge contacts are clean, and that the selected flashcart profile and settings are correct."
                    ),
                    "abortable": False,
                }
            )
            return False
        return True


def empty_flashcarts_map() -> FlashcartMap:
    return {"DMG": {}, "AGB": {}}


def _profile_flash_ids(profile: Mapping[str, object]) -> set[tuple[int, ...]]:
    raw_flash_ids = profile.get("flash_ids")
    if not isinstance(raw_flash_ids, (list, tuple)):
        return set()

    flash_ids: set[tuple[int, ...]] = set()
    for raw_flash_id in cast(Sequence[object], raw_flash_ids):
        if not isinstance(raw_flash_id, (list, tuple)):
            continue
        values = cast(Sequence[object], raw_flash_id)
        if all(isinstance(value, int) for value in values):
            flash_ids.add(tuple(value for value in values if isinstance(value, int)))
    return flash_ids


def has_3v_compatible_profile(
    carts: Iterable[object], cart_type_index: int | None
) -> bool:
    try:
        profiles = list(carts)
    except TypeError:
        return False
    if (
        cart_type_index is None
        or cart_type_index < 0
        or cart_type_index >= len(profiles)
    ):
        return False
    selected = profiles[cart_type_index]
    if not isinstance(selected, dict):
        return False
    selected_profile = cast(Mapping[str, object], selected)
    if selected_profile.get("voltage", 5) != 5:
        return False
    selected_id_set = _profile_flash_ids(selected_profile)
    if not selected_id_set:
        return False
    selected_type = selected_profile.get("type")
    for i, profile in enumerate(profiles):
        if i == cart_type_index:
            continue
        if not isinstance(profile, dict):
            continue
        candidate = cast(Mapping[str, object], profile)
        if candidate.get("type") != selected_type:
            continue
        if candidate.get("voltage", 5) != 3.3 and not candidate.get(
            "voltage_variants", False
        ):
            continue
        if _profile_flash_ids(candidate) & selected_id_set:
            return True
    return False
