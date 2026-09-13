# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

import copy
import hashlib
import io
import json
import re
import string
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .app import AppContext
from .CartridgeTypes import DmgSaveTypes, RomSizes
from .i18n import __
from .Logging import ANSI, dprint, logger

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

try:
    from PIL import Image
except Exception:
    Image = None  # type: ignore[assignment]
    logger.exception("Pillow image support is unavailable for Game Boy ROMs")


SACHEN_OVERRIDES: dict[str, tuple[int, int, str, int, bool]] = {
    "739b4686971c0baeaf26f173acae4b2bbf007077": (0x03, 0x8793, "SACHEN 4B-001", 1, False),
    "228b404f1ccf4ddc4df235f37b6d615ebef1ef42": (0x01, 0xB180, "SACHEN 4B-002", 1, False),
    "f9b86a8f2e8b31d4c502c88075359c02b3b56801": (0x03, 0x7CFD, "SACHEN 4B-004", 1, False),
    "d6b533811a010d4d1ccc5a2c349d0f63d3f49d34": (0x03, 0x80DF, "SACHEN 4B-005", 1, False),
    "3e56cc2ddfe000ed53a79d62c8bf7f202747cd8e": (0x03, 0x55E6, "SACHEN 4B-006", 1, False),
    "193ef8e2128a2410fee9ea27c91bc4dd04741ba8": (0x03, 0x99C7, "SACHEN 4B-008", 1, False),
    "a507cbb0637ae71af2c8329ba66dc4216878e539": (0x03, 0xCDD3, "SACHEN 4B-009", 1, False),
    "18ec2b1597d7805158b2b853a700d70bce0ab3ff": (0x01, 0x3889, "SACHEN 4B-003", 1, False),
    "961ce35d3a814495cf42924230831417a9bfe09f": (0x06, 0x3934, "SACHEN 31B-001", 2, True),
    "b859611c03af5f7f503e8cb09c814a0ce8bab599": (0x06, 0x2E45, "SACHEN 31B-001", 2, True),
    "f5c6c2e6a6f2ee8629223d7c72f9dd6f320aa09d": (0x04, 0x125B, "SACHEN 8B-001", 2, True),
    "f28adf84ba568c54f94b25fa12924ed67dd17e9d": (0x04, 0x598F, "SACHEN 8B-002", 2, True),
    "1c086f94d8fd404da385ce5735f34392eeb726e1": (0x04, 0x6485, "SACHEN 8B-003", 2, True),
    "2cfde18d2c57badbc0f8df52793844563bb0a0de": (0x04, 0x02A4, "SACHEN 8B-004", 2, True),
    "4eea3c0a235cf92dc622c221d3bb733ba721fb78": (0x02, 0xA709, "SACHEN", 2, True),
    "3fe9b7abbc18956080f7df9b5e5a0c9f1863347b": (0x04, 0x929D, "SACHEN 1B-003", 2, True),
    "d89b0d5548977fd50e4620d69e0b8c6b05d48f2c": (0x04, 0x2F50, "SACHEN 4B-003", 2, True),
    "8b98b1d36b846651c02319f2dcd3f497db3947e7": (0x06, 0x9769, "SACHEN", 2, True),
    "d0aec9fbf08d7a72348e96b6756b30c1cbf62f00": (0x06, 0x0346, "SACHEN 6B-001", 2, False),
}


class RomFileDMG:
    ROMFILE_PATH: Path | None = None
    ROMFILE = bytearray()
    BATTERYLESS_SRAM_DB = None

    def __init__(self, file: str | Path | bytearray | None = None) -> None:
        self.DATA: dict = {}
        if isinstance(file, (str, Path)):
            self.Open(file)
        elif isinstance(file, bytearray):
            self.ROMFILE = file

    def Open(self, file: str | Path) -> None:
        self.ROMFILE_PATH = Path(file)
        self.Load()

    def Load(self) -> None:
        if self.ROMFILE_PATH is None:
            return
        with self.ROMFILE_PATH.open("rb") as f:
            self.ROMFILE = bytearray(f.read(0x1000))

    def CalcChecksumHeader(self, fix: bool = False) -> int:
        checksum = 0
        for i in range(0x134, 0x14D):
            checksum: int = checksum - self.ROMFILE[i] - 1
        checksum = checksum & 0xFF

        if fix:
            self.ROMFILE[0x14D] = checksum
        return checksum

    def CalcChecksumGlobal(self, fix: bool = False) -> int:
        temp1: int = self.ROMFILE[0x14E]
        temp2: int = self.ROMFILE[0x14F]
        self.ROMFILE[0x14E] = 0
        self.ROMFILE[0x14F] = 0
        checksum: int = sum(self.ROMFILE) & 0xFFFF
        if fix:
            self.ROMFILE[0x14E] = checksum >> 8
            self.ROMFILE[0x14F] = checksum & 0xFF
        else:
            self.ROMFILE[0x14E] = temp1
            self.ROMFILE[0x14F] = temp2
        return checksum

    def FixHeader(self) -> bytearray:
        self.CalcChecksumHeader(fix=True)
        self.CalcChecksumGlobal(fix=True)
        return self.ROMFILE[0:0x200]

    def LogoToImage(self, data: bytearray, valid: bool = True) -> PILImage | Literal[False]:
        if Image is None:
            return False
        img = Image.new(mode="P", size=(48, 8))
        if valid:
            img.putpalette([255, 255, 255, 0, 0, 0])
        else:
            img.putpalette([255, 255, 255, 255, 0, 0])
        img.info["transparency"] = 0
        pixels = img.load()
        if pixels is None:
            return False

        for y in range(8):
            i: int = int((y / 2) % 2) + int(y / 4) * 24
            x = 0
            ix = 0
            while True:
                x += 1
                nibble: int = (data[i] & 0xF) if (y % 2) else (data[i] >> 4)
                for b in range(3, -1, -1):
                    if (nibble >> b) & 1:
                        pixels[ix, y] = 1
                    else:
                        pixels[ix, y] = 0
                    ix += 1
                i += 2
                if x >= 12:
                    break
        return img

    def _ParseHeaderFields(self, buffer: bytearray) -> dict[str, Any]:
        data: dict[str, Any] = {}
        data["empty"] = buffer[0x104:0x134] == bytearray([buffer[0x104]] * 0x30)
        data["empty_nocart"] = buffer == bytearray([0x00] * len(buffer))
        data["logo_correct"] = hashlib.sha1(buffer[0x104:0x134]).digest() == bytearray(
            [
                0x07,
                0x45,
                0xFD,
                0xEF,
                0x34,
                0x13,
                0x2D,
                0x1B,
                0x3D,
                0x48,
                0x8C,
                0xFB,
                0xDF,
                0x03,
                0x79,
                0xA3,
                0x9F,
                0xD5,
                0x4B,
                0x4C,
            ],
        )
        data["cgb"] = int(buffer[0x143])
        data["sgb"] = int(buffer[0x146])
        data["old_lic"] = int(buffer[0x14B])
        temp = self.LogoToImage(buffer[0x104:0x134], data["logo_correct"])
        if temp is not False and not data["empty"]:
            data["logo"] = temp

        if data["cgb"] in (0x80, 0xC0):
            data["game_title_raw"] = bytearray(buffer[0x134:0x143]).decode("ascii", "replace")
        else:
            data["game_title_raw"] = bytearray(buffer[0x134:0x144]).decode("ascii", "replace")
        game_title: str = data["game_title_raw"]
        game_title = re.sub(r"(\x00+)$", "", game_title)
        game_title = re.sub(r"((_)_+|(\x00)\x00+|(\s)\s+)", "\\2\\3\\4", game_title).replace("\x00", "_")
        game_title = "".join(filter(lambda x: x in set(string.printable), game_title))
        data["game_code"] = ""
        if (
            data["cgb"] in (0x80, 0xC0)
            and len(data["game_title_raw"].rstrip("\x00")) == 15
            and data["game_title_raw"][-4:][0] in ("A", "B", "H", "K", "V")
            and data["game_title_raw"][-4:][3]
            in (
                "A",
                "B",
                "D",
                "E",
                "F",
                "I",
                "J",
                "K",
                "P",
                "S",
                "U",
                "X",
                "Y",
            )
        ):
            data["game_code"] = game_title[-4:]
            game_title = game_title[:-4].rstrip("_")
        data["game_title"] = game_title

        data["maker_code"] = format(int(buffer[0x14B]), "02X")
        if data["maker_code"] == "33":
            maker_code: str = bytearray(buffer[0x144:0x146]).decode("ascii", "replace")
            maker_code = "".join(filter(lambda x: x in set(string.printable), maker_code))
            data["maker_code_new"] = maker_code
        data["mapper_raw"] = int(buffer[0x147])
        data["mapper"] = "?"
        data["rom_size_raw"] = int(buffer[0x148])
        data["rom_size"] = "?"
        if buffer[0x148] < RomSizes().GetNumberOfTypes():
            data["rom_size"] = RomSizes().GetString(index=buffer[0x148])
        data["ram_size_raw"] = int(buffer[0x149])
        if data["mapper_raw"] == 0x05 or data["mapper_raw"] == 0x06:
            data["ram_size"] = 0x200
        else:
            data["ram_size"] = "?"
            if buffer[0x149] < DmgSaveTypes().GetNumberOfTypes():
                data["ram_size"] = DmgSaveTypes(index=buffer[0x149]).GetString()
        data["header_sha1"] = hashlib.sha1(buffer[0x0:0x180]).hexdigest()
        data["version"] = int(buffer[0x14C])
        data["header_checksum"] = int(buffer[0x14D])
        data["header_checksum_calc"] = self.CalcChecksumHeader()
        data["header_checksum_correct"] = data["header_checksum"] == data["header_checksum_calc"]
        data["rom_checksum"] = int(256 * buffer[0x14E] + buffer[0x14F])
        data["rom_checksum_calc"] = self.CalcChecksumGlobal()
        data["rom_checksum_correct"] = data["rom_checksum"] == data["rom_checksum_calc"]
        return data

    @staticmethod
    def _ApplyKnownMapperOverrides(data: dict[str, Any]) -> None:
        # MBC2
        if data["mapper_raw"] == 0x06:
            data["ram_size_raw"] = 0x100

        # MBC30
        if data["mapper_raw"] == 0x10 and data["ram_size_raw"] == 0x05:
            data["mapper_raw"] += 0x100

        # MBC6
        if data["mapper_raw"] == 0x20:
            data["ram_size_raw"] = 0x104

        # MBC7
        if data["mapper_raw"] == 0x22 and data["game_title"] in ("KORO2 KIRBY", "KIRBY TNT"):
            data["ram_size_raw"] = 0x101
        elif data["mapper_raw"] == 0x22 and data["game_title"] == "CMASTER":
            data["ram_size_raw"] = 0x102

        # TAMA5
        if data["mapper_raw"] == 0xFD:
            data["ram_size_raw"] = 0x103

        # MBC1M
        if (
            (data["mapper_raw"] == 0x03 and data["game_title"] == "MOMOCOL" and data["header_checksum"] == 0x28)
            or (data["mapper_raw"] == 0x01 and data["game_title"] == "BOMCOL" and data["header_checksum"] == 0x86)
            or (data["mapper_raw"] == 0x01 and data["game_title"] == "BOMSEL" and data["header_checksum"] == 0x9C)
            or (data["mapper_raw"] == 0x01 and data["game_title"] == "GENCOL" and data["header_checksum"] == 0x8A)
            or (
                data["mapper_raw"] == 0x01
                and data["game_title"] == "SUPERCHINESE 123"
                and data["header_checksum"] == 0xE4
            )
            or (
                data["mapper_raw"] == 0x01
                and data["game_title"] == "MORTALKOMBATI&II"
                and data["header_checksum"] == 0xB9
            )
            or (
                data["mapper_raw"] == 0x01
                and data["game_title"] == "MORTALKOMBAT DUO"
                and data["header_checksum"] == 0xA7
            )
        ):
            data["mapper_raw"] += 0x100

    def _ApplySachenOverride(self, data: dict[str, Any], buffer: bytearray) -> None:
        digest: str = hashlib.sha1(buffer[0x200:0x280]).hexdigest()
        override: tuple[int, int, str, int, bool] | None = SACHEN_OVERRIDES.get(digest)
        if digest == "d824d2b2716b08faeaa4fbd97d81945746779160":
            secondary_digest: str = hashlib.sha1(buffer[0:0x80]).hexdigest()
            override = (
                (0x04, 0x657A, "SACHEN 8B-001", 2, False)
                if secondary_digest == "20173b95fb1aac816c66f56211f496fbfb8803bc"
                else (0x03, 0x8E9F, "SACHEN 4B-007", 1, False)
            )
        if override is None:
            return

        rom_size, checksum, title, version, is_cgb = override
        data["rom_size_raw"] = rom_size
        data["mapper_raw"] = 0x204
        data["rom_checksum"] = checksum
        data["game_title"] = title
        if is_cgb:
            data["cgb"] = 0x80
        logo_offset: Literal[388, 260] = 0x184 if version == 1 else 0x104
        logo = self.LogoToImage(buffer[logo_offset : logo_offset + 0x30])
        if logo is not False:
            data["logo_sachen"] = logo

    def GetHeader(self, unchanged: bool = False) -> dict[str, Any]:
        buffer: bytearray = self.ROMFILE
        if len(buffer) < 0x180:
            return {}
        data = self._ParseHeaderFields(buffer)

        if unchanged:
            data["unchanged"] = copy.copy(data)
        else:
            self._ApplyKnownMapperOverrides(data)

            # GB-Memory (DMG-MMSA-JPN)
            if (
                (
                    data["mapper_raw"] == 0x19
                    and data["game_title"] == "NP M-MENU MENU"
                    and data["header_checksum"] == 0xD3
                )
                or (
                    data["mapper_raw"] == 0x01
                    and data["game_title"] == "DMG MULTI MENU "
                    and data["header_checksum"] == 0x36
                )
                or (data["mapper_raw"] == 0x1B and data["game_title"] == "GBMEM-MENU MMSA" and data["version"] == 0x01)
            ):
                data["rom_size_raw"] = 0x05
                data["ram_size_raw"] = 0x04
                data["mapper_raw"] = 0x105

            # M161 (Mani 4 in 1)
            elif data["mapper_raw"] == 0x10 and data["game_title"] == "TETRIS SET" and data["header_checksum"] == 0x3F:
                data["mapper_raw"] = 0x104

            # MMM01 (Mani 4 in 1)
            elif (
                (
                    data["mapper_raw"] == 0x11
                    and data["game_title"] == "BOUKENJIMA2 SET"
                    and data["header_checksum"] == 0
                )
                or (
                    data["mapper_raw"] == 0x11
                    and data["game_title"] == "BUBBLEBOBBLE SET"
                    and data["header_checksum"] == 0xC6
                )
                or (
                    data["mapper_raw"] == 0x11
                    and data["game_title"] == "GANBARUGA SET"
                    and data["header_checksum"] == 0x90
                )
                or (
                    data["mapper_raw"] == 0x11
                    and data["game_title"] == "RTYPE 2 SET"
                    and data["header_checksum"] == 0x32
                )
            ):
                data["mapper_raw"] = 0x0B

            # Unlicensed 256M Mapper
            elif (
                (data["game_title"].upper() == "GB HICOL" and data["header_checksum"] in (0x4A, 0x49, 0xE8, 0xE9))
                or (data["game_title"] == "BennVenn" and data["header_checksum"] == 0x48)
                or buffer[0x150:0x160].decode("ascii", "replace") == "256M ROM Builder"
                or (
                    data["mapper_raw"] in (0x19, 0x1B)
                    and data["game_title"] == "GBMEM-MENU 256M"
                    and data["version"] == 0x01
                )
            ):
                data["rom_size_raw"] = 0x0A
                data["ram_size_raw"] = 0x201
                data["mapper_raw"] = 0x201

            # Unlicensed Wisdom Tree Mapper
            elif hashlib.sha1(buffer[0x0:0x150]).digest() == bytearray(
                [
                    0xF5,
                    0xD2,
                    0x91,
                    0x7D,
                    0x5E,
                    0x5B,
                    0xAB,
                    0xD8,
                    0x5F,
                    0x0A,
                    0xC7,
                    0xBA,
                    0x56,
                    0xEB,
                    0x49,
                    0x8A,
                    0xBA,
                    0x12,
                    0x49,
                    0x13,
                ],
            ):  # Exodus / Joshua
                data["rom_size_raw"] = 0x02
                data["mapper_raw"] = 0x202
            elif hashlib.sha1(buffer[0x0:0x150]).digest() == bytearray(
                [
                    0xE9,
                    0xF8,
                    0x32,
                    0x78,
                    0x39,
                    0x19,
                    0xE3,
                    0xB2,
                    0xFC,
                    0x6F,
                    0xC2,
                    0x60,
                    0x30,
                    0x33,
                    0x20,
                    0xD0,
                    0x3B,
                    0x1A,
                    0xA9,
                    0xA2,
                ],
            ):  # Spiritual Warfare
                data["rom_size_raw"] = 0x03
                data["mapper_raw"] = 0x202
            elif hashlib.sha1(buffer[0x0:0x150]).digest() == bytearray(
                [
                    0xE6,
                    0xC0,
                    0x39,
                    0x7F,
                    0xA5,
                    0x99,
                    0xD6,
                    0x60,
                    0xD7,
                    0x90,
                    0x45,
                    0xB9,
                    0xF0,
                    0x64,
                    0x3B,
                    0x2A,
                    0x41,
                    0xA4,
                    0xD6,
                    0x35,
                ],
            ):  # King James Bible
                data["rom_size_raw"] = 0x05
                data["mapper_raw"] = 0x202
            elif hashlib.sha1(buffer[0x0:0x150]).digest() == bytearray(
                [
                    0x36,
                    0x89,
                    0x60,
                    0xDD,
                    0x1B,
                    0xE1,
                    0x73,
                    0x86,
                    0x8B,
                    0x24,
                    0xA3,
                    0xDC,
                    0x57,
                    0xA5,
                    0xCB,
                    0x7C,
                    0xCA,
                    0x62,
                    0xDD,
                    0x34,
                ],
            ):  # NIV Bible
                data["rom_size_raw"] = 0x06
                data["mapper_raw"] = 0x202

            # Unlicensed Xploder GB Mapper
            elif hashlib.sha1(buffer[0x104:0x150]).digest() == bytearray(
                [
                    0x06,
                    0xAC,
                    0xDC,
                    0xB6,
                    0xD1,
                    0x9B,
                    0xD9,
                    0xE3,
                    0x95,
                    0xA2,
                    0x38,
                    0xB8,
                    0x00,
                    0x97,
                    0x0D,
                    0x78,
                    0x3F,
                    0xC6,
                    0xB7,
                    0xBD,
                ],
            ):
                data["rom_size_raw"] = 0x02
                data["ram_size_raw"] = 0x203
                data["mapper_raw"] = 0x203
                data["cgb"] = 0x80
                try:
                    game_title: str = bytearray(buffer[0:0x10]).decode("ascii", "replace").replace("\xff", "")
                    game_title = re.sub(r"(\x00+)$", "", game_title)
                    game_title = re.sub(r"((_)_+|(\x00)\x00+|(\s)\s+)", "\\2\\3\\4", game_title).replace("\x00", "")
                    game_title = "".join(filter(lambda x: x in set(string.printable), game_title))
                    data["game_title"] = game_title
                except Exception:
                    logger.exception("Failed to parse the unlicensed mapper ROM title")
                data["version"] = "{:d}.{:d}.{:d}:{:c} ({:02d}:{:02d} {:02d}-{:02d}-{:02d} / {:04X})".format(
                    buffer[0xD8],
                    buffer[0xD9],
                    buffer[0xDA],
                    buffer[0xD7],
                    buffer[0xD0],
                    buffer[0xD1],
                    buffer[0xD2],
                    buffer[0xD3],
                    buffer[0xD4],
                    struct.unpack("<H", buffer[0xD5:0xD7])[0],
                ).replace("\x00", "")

            # Unlicensed Datel Orbit V2 Mapper
            elif hashlib.sha1(buffer[0x101:0x134]).digest() == bytearray(
                [
                    0xFA,
                    0x68,
                    0x5A,
                    0x37,
                    0x85,
                    0xEF,
                    0x65,
                    0x23,
                    0x2D,
                    0x6F,
                    0x23,
                    0xAC,
                    0x02,
                    0x05,
                    0x15,
                    0x20,
                    0x8B,
                    0xDE,
                    0xC5,
                    0x23,
                ],
            ):
                data["rom_size_raw"] = 0x02
                data["ram_size_raw"] = 0
                data["mapper_raw"] = 0x205
                data["cgb"] = 0x80
                try:
                    game_title = bytearray(buffer[0x134:0x150]).decode("ascii", "replace").replace("\xff", "")
                    game_title = re.sub(r"(\x00+)$", "", game_title)
                    game_title = re.sub(r"((_)_+|(\x00)\x00+|(\s)\s+)", "\\2\\3\\4", game_title).replace("\x00", "")
                    game_title = "".join(filter(lambda x: x in set(string.printable), game_title))
                    data["game_title"] = game_title
                except Exception:
                    logger.exception("Failed to parse the Datel Orbit V2 ROM title")

            # Unlicensed Datel Orbit V2 Mapper (older firmware)
            elif hashlib.sha1(buffer[0x101:0x140]).digest() == bytearray(
                [
                    0xC1,
                    0xF4,
                    0x15,
                    0x4A,
                    0xEF,
                    0xCC,
                    0x5B,
                    0xE7,
                    0xEC,
                    0x83,
                    0xA8,
                    0xBB,
                    0x7B,
                    0xC0,
                    0x95,
                    0x83,
                    0x35,
                    0xEC,
                    0x9A,
                    0xF2,
                ],
            ) or hashlib.sha1(buffer[0x101:0x140]).digest() == bytearray(
                [
                    0xC9,
                    0x50,
                    0x65,
                    0xCB,
                    0x31,
                    0x96,
                    0x26,
                    0x6C,
                    0x32,
                    0x58,
                    0xAB,
                    0x07,
                    0xA1,
                    0x9E,
                    0x0C,
                    0x10,
                    0xA6,
                    0xED,
                    0xCC,
                    0x67,
                ],
            ):
                data["rom_size_raw"] = 0x02
                data["ram_size_raw"] = 0
                data["mapper_raw"] = 0x205
                try:
                    game_title = bytearray(buffer[0x134:0x140]).decode("ascii", "replace").replace("\xff", "")
                    game_title = re.sub(r"(\x00+)$", "", game_title)
                    game_title = re.sub(r"((_)_+|(\x00)\x00+|(\s)\s+)", "\\2\\3\\4", game_title).replace("\x00", "")
                    game_title = "".join(filter(lambda x: x in set(string.printable), game_title))
                    data["game_title"] = game_title
                except Exception:
                    logger.exception("Failed to parse the legacy Datel Orbit V2 ROM title")

            # Unlicensed Sachen MMC1/MMC2
            elif len(buffer) >= 0x280:
                self._ApplySachenOverride(data, buffer)

            # GBFlash MBCX
            if data["game_title"] == "MBCX_MENU":
                data["rom_size_raw"] = 0x0A
                data["ram_size_raw"] = 0x03
                data["mapper_raw"] = 0x206

            # Photo!
            if data["game_title"] == "PHOTO":
                data["ram_size_raw"] = 0x204

        from .Mapper import DMG_Mapper  # noqa: PLC0415 - Mapper imports RomFileDMG

        if data["mapper_raw"] in DMG_Mapper().GetAllMapperIds():
            data["mapper"] = DMG_Mapper().GetMapperName(data["mapper_raw"])
        elif data["logo_correct"]:
            print(
                "{:s}{}{:s}".format(
                    ANSI.YELLOW,
                    __(
                        "Warning: Unknown mapper type value {mapper}",
                        mapper="0x{:02X}".format(data["mapper_raw"]),
                    ),
                    ANSI.RESET,
                ),
            )

        self.DATA = data
        data["db"] = self.GetDatabaseEntry()
        batteryless_sram = self.GetBatterylessSramConfig(data)
        if batteryless_sram is not None:
            data["batteryless_sram"] = batteryless_sram
        if data["db"] is not None and data["game_code"] == "" and data["db"]["gc"] != "":
            data["game_code"] = data["db"]["gc"][4:]
        return data

    def GetDatabaseEntry(self) -> dict | None:
        data = self.DATA
        db_entry = None
        database_path: Path = Path(AppContext.CONFIG_PATH) / "db_DMG.json"
        if database_path.exists():
            with database_path.open(encoding="UTF-8") as f:
                db_raw: str = f.read()
                try:
                    db: dict = json.loads(db_raw)
                except (json.JSONDecodeError, ValueError) as e:
                    print(__("Error: Database for Game Boy titles is corrupted.") + "\n" + str(e))
                    return None
                if data["header_sha1"] in db:
                    db_entry = db[data["header_sha1"]]
                else:
                    dprint(
                        __(
                            "No database entry found for this title (Header SHA1: {sha1})",
                            sha1=data["header_sha1"],
                        ),
                    )
        else:
            print(
                __(
                    "Error: Database for Game Boy titles not found at {path}",
                    path=str(database_path),
                ),
            )
        return db_entry

    @classmethod
    def GetBatterylessSramConfig(cls, header: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(header, dict):
            return None
        if "game_title_raw" not in header:
            return None

        if cls.BATTERYLESS_SRAM_DB is None:
            config_paths: list[Path] = [Path(__file__).resolve().parent / "config"]
            if AppContext.CONFIG_PATH:
                config_paths.insert(0, Path(AppContext.CONFIG_PATH))
            for config_path in config_paths:
                db_path: Path = config_path / "db_DMG_bl.json"
                if not db_path.exists():
                    continue
                try:
                    with db_path.open(encoding="UTF-8") as f:
                        cls.BATTERYLESS_SRAM_DB = json.loads(f.read())
                    break
                except Exception as e:
                    print(
                        "Error: Could not load the database of batteryless SRAM configurations.",
                        e,
                        sep="\n",
                    )
            if cls.BATTERYLESS_SRAM_DB is None:
                cls.BATTERYLESS_SRAM_DB = False

        db = cls.BATTERYLESS_SRAM_DB
        if not db:
            return None

        titles = [
            header["game_title_raw"],
            header["game_title_raw"].replace("\x00", "").rstrip(),
            header.get("game_title", ""),
        ]
        for title in titles:
            if title == "":
                continue
            if title not in db:
                continue
            entry = db[title]
            batteryless_sram = {
                "bl_offset": entry[0],
                "bl_size": entry[1],
            }
            if len(entry) >= 3:
                batteryless_sram["bl_layout"] = entry[2]
            return batteryless_sram

        return None


def from_isx(buffer: bytearray) -> bytearray:
    data_input = io.BytesIO(buffer)
    data_output = bytearray(8 * 1024 * 1024)
    rom_size = 0
    temp = 32 * 1024
    while 1:
        try:
            rec_type: int = struct.unpack("B", data_input.read(1))[0]
            if rec_type == 4:
                break
            if rec_type != 1:
                print(
                    __(
                        "Warning: Unhandled ISX record type {type} found. Converted ROM may not be working correctly.",
                        type=f"0x{rec_type:02X}",
                    ),
                )
                continue
            bank: int = struct.unpack("B", data_input.read(1))[0]
            offset: int = struct.unpack("<H", data_input.read(2))[0] % 0x4000
            realoffset: int = bank * 16 * 1024 + offset
            size: int = struct.unpack("<H", data_input.read(2))[0]
            data_output[realoffset : realoffset + size] = data_input.read(size)
            rom_size: int = max(rom_size, realoffset + size)
            temp: int = 32 * 1024
            while temp < rom_size:
                temp *= 2
        except Exception:
            print(__("Error: Couldn't convert ISX file correctly."))
            break
    return data_output[:temp]
