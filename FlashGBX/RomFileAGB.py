# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import hashlib
import json
import re
import string
import struct
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from .app import AppContext
from .i18n import __
from .Logging import dprint, logger  # pyright: ignore[reportAttributeAccessIssue]

if TYPE_CHECKING:
    from PIL.Image import Image as PillowImage  # pyright: ignore[reportMissingImports]

try:
    from PIL import Image  # pyright: ignore[reportMissingImports]
except ImportError:
    Image = None  # pyright: ignore[reportAssignmentType]
    logger.exception("Pillow image support is unavailable for Game Boy Advance ROMs")

AGBHeader = dict[str, Any]
AGBDatabaseEntry = dict[str, Any]
RomSource = str | Path | bytes | bytearray | None

_PRINTABLE_CHARS = frozenset(string.printable)
_TRAILING_NULLS = re.compile(r"(\x00+)$")
_REPEATED_HEADER_CHARS = re.compile(r"((_)_+|(\x00)\x00+|(\s)\s+)")
_NO_CARTRIDGE_HEADER_DIGESTS = (
    bytes.fromhex("4FE93EEEBC5593FE2E231A3986CE86C95C1100DD"),
    bytes.fromhex("A503A1B5F5DDBEFC87C79B1359F7E1A5CFE0AC9F"),
    bytes.fromhex("4686E381B24A2DB07DE83D452FA31E8A044B3A50"),
)
_NO_CARTRIDGE_LOGO_DIGESTS = (
    bytes.fromhex("2BDC7DEF6C481FBFEEB880B1D0FDF6575D6A39BE"),
    bytes.fromhex("09B90E535E8550F890A4F477137E4559A5C0A445"),
)
_NINTENDO_LOGO_DIGEST = bytes.fromhex("17DAA0FEC02FC33C0F6ABB549A8B80B6613B48EE")
_VAST_FAME_SIGNATURE = bytes.fromhex("B4009FE59910A0E30010C0E5AC009FE5")


def _clean_header_text(raw: bytes | bytearray, *, remove_newlines: bool = False) -> str:
    text = bytes(raw).decode("ascii", "replace")
    text = _TRAILING_NULLS.sub("", text)
    text = _REPEATED_HEADER_CHARS.sub(r"\2\3\4", text).replace("\x00", "_")
    text = "".join(character for character in text if character in _PRINTABLE_CHARS)
    return text.replace("\n", "") if remove_newlines else text


class RomFileAGB:
    def __init__(self, file: RomSource = None) -> None:
        self.ROMFILE_PATH: Path | None = None
        self.ROMFILE: bytearray = bytearray()
        self.DATA: AGBHeader | None = None

        if isinstance(file, (str, Path)):
            self.Open(file)
        elif isinstance(file, bytearray):
            self.ROMFILE = file
        elif isinstance(file, bytes):
            self.ROMFILE = bytearray(file)

    def Open(self, file: str | Path) -> None:
        self.ROMFILE_PATH = Path(file)
        self.Load()

    def Load(self) -> None:
        if self.ROMFILE_PATH is None:
            return
        self.ROMFILE = bytearray(self.ROMFILE_PATH.read_bytes())

    def CalcChecksumHeader(self, fix: bool = False) -> int:
        checksum = 0
        for i in range(0xA0, 0xBD):
            checksum = checksum - self.ROMFILE[i]
        checksum = (checksum - 0x19) & 0xFF

        if fix:
            self.ROMFILE[0xBD] = checksum
        return checksum

    def CalcChecksumGlobal(self) -> int:
        return zlib.crc32(self.ROMFILE) & 0xFFFFFFFF

    def FixHeader(self) -> bytearray:
        self.CalcChecksumHeader(True)
        return self.ROMFILE[0:0x200]

    def LogoToImage(
        self,
        data: bytes | bytearray,
        valid: bool = True,
    ) -> PillowImage | Literal[False]:
        if Image is None:
            return False
        if len(data) != 0x9C:
            return False
        if data in (bytes(len(data)), bytes([0xFF]) * len(data)):
            return False

        # Based on a HuffUnComp function provided by Winter1760, thank you!
        def huff_uncomp(compressed_data: bytes | bytearray) -> bytes:
            BITS = 4
            OUT_SIZE = 0xD4
            TREE = bytes(
                [
                    0x40,
                    0x00,
                    0x00,
                    0x00,
                    0x01,
                    0x81,
                    0x82,
                    0x82,
                    0x83,
                    0x0F,
                    0x83,
                    0x0C,
                    0xC3,
                    0x03,
                    0x83,
                    0x01,
                    0x83,
                    0x04,
                    0xC3,
                    0x08,
                    0x0E,
                    0x02,
                    0xC2,
                    0x0D,
                    0xC2,
                    0x07,
                    0x0B,
                    0x06,
                    0x0A,
                    0x05,
                    0x09,
                ],
            )
            encoded_data = (
                bytes([0x20 | BITS])
                + OUT_SIZE.to_bytes(3, "little")
                + bytes([len(TREE) // 2])
                + TREE
                + bytes(compressed_data)
            )
            bits = encoded_data[0] & 15
            out_size = int.from_bytes(encoded_data[1:4], "little") & 0xFFFF
            i = 6 + encoded_data[4] * 2
            node_offs = 5
            out_units = 0
            out_ready = 0
            out = b""
            while len(out) < out_size:
                in_unit = (
                    int.from_bytes(encoded_data[i : i + 2], "little")
                    | int.from_bytes(encoded_data[i ^ 2 : (i ^ 2) + 2], "little") << 16
                )
                i += 4
                for b in range(31, -1, -1):
                    node = encoded_data[node_offs]
                    node_offs &= ~1
                    node_offs += (node & 0x3F) * 2 + 2 + (in_unit >> b & 1)
                    if node << (in_unit >> b & 1) & 0x80:
                        out_ready >>= bits
                        out_ready |= encoded_data[node_offs] << 32 - bits
                        out_ready &= 0xFFFFFFFF
                        out_units += 1
                        if out_units == bits % 8 + 4:
                            out += out_ready.to_bytes(4, "little")
                            if len(out) >= out_size:
                                return out
                            out_units = 0
                            out_ready = 0
                        node_offs = 5
            return out

        def diff_16_bit_unfilter(filtered_data: bytes) -> bytearray:
            header = struct.unpack_from("<I", filtered_data)[0]
            out_size = (header >> 8) & 0xFFFF
            pos = 4
            prev = 0
            dest = bytearray()
            while pos < out_size:
                if pos + 2 > len(filtered_data):
                    break
                temp = (struct.unpack_from("<H", filtered_data, pos)[0] + prev) & 0xFFFF
                dest.extend(struct.pack("<H", temp))
                pos += 2
                prev = temp
            return dest

        try:
            logo_data = diff_16_bit_unfilter(huff_uncomp(data))
        except IndexError, struct.error:
            return False

        img = Image.new(mode="P", size=(104, 16))
        img.info["transparency"] = 0
        img.putpalette([255, 255, 255, 0 if valid else 255, 0, 0])
        pixels = img.load()
        if pixels is None:
            return False

        for tile_row in range(2):
            for tile_w in range(13):
                for tile_h in range(8):
                    for bit in range(8):
                        pos = (tile_row * 13 * 8) + (tile_w * 8) + tile_h
                        if pos >= len(logo_data):
                            break
                        pixel = (logo_data[pos] >> bit) & 1
                        x = tile_w * 8 + bit
                        y = tile_row * 8 + tile_h
                        pixels[x, y] = pixel
        return img

    def GetHeader(self, unchanged: bool = False) -> AGBHeader:
        buffer = bytearray(self.ROMFILE)
        data: AGBHeader = {}
        if len(buffer) < 0x180:
            return {}
        header_digest = hashlib.sha1(buffer[0:0x180]).digest()
        data["empty_nocart"] = header_digest in _NO_CARTRIDGE_HEADER_DIGESTS
        if not data["empty_nocart"]:
            logo_digest = hashlib.sha1(buffer[0x10:0x50]).digest()
            data["empty_nocart"] = logo_digest in _NO_CARTRIDGE_LOGO_DIGESTS

        data["empty"] = buffer[0x04:0xA0] == bytes([buffer[0x04]]) * 0x9C or data["empty_nocart"]
        if data["empty_nocart"]:
            buffer = bytearray(len(buffer))
        data["logo_correct"] = hashlib.sha1(buffer[0x04:0xA0]).digest() == _NINTENDO_LOGO_DIGEST
        temp = self.LogoToImage(buffer[0x04:0xA0], data["logo_correct"])
        if temp is not False and not data["empty"]:
            data["logo"] = temp

        data["game_title_raw"] = bytearray(buffer[0xA0:0xAC]).decode("ascii", "replace")
        game_title = _clean_header_text(buffer[0xA0:0xAC], remove_newlines=True)
        data["game_title"] = game_title
        data["game_code_raw"] = bytearray(buffer[0xAC:0xB0]).decode("ascii", "replace")
        game_code = _clean_header_text(buffer[0xAC:0xB0])
        data["game_code"] = game_code
        data["maker_code"] = _clean_header_text(buffer[0xB0:0xB2])
        data["header_checksum"] = int(buffer[0xBD])
        data["header_checksum_calc"] = self.CalcChecksumHeader()
        data["header_checksum_correct"] = data["header_checksum"] == data["header_checksum_calc"]
        if len(game_code) == 4 and game_code[0] == "M":
            data["header_sha1"] = hashlib.sha1(buffer[0x0:0x100]).hexdigest()
        else:
            data["header_sha1"] = hashlib.sha1(buffer[0x0:0x180]).hexdigest()
        data["version"] = int(buffer[0xBC])
        data["96h_correct"] = buffer[0xB2] == 0x96
        data["rom_checksum_calc"] = self.CalcChecksumGlobal()
        data["rom_size_calc"] = len(buffer)
        data["save_type"] = None
        data["save_size"] = 0

        # Vast Fame (unlicensed protected carts)
        # Initialization code always present in Vast Fame carts.
        data["vast_fame"] = buffer[0x15C:0x16C] == _VAST_FAME_SIGNATURE

        # 8M FLASH DACS
        data["dacs_8m"] = False
        if data["game_title"] == "NGC-HIKARU3" and data["game_code"] == "GHTJ" and data["header_checksum"] == 0xB3:
            data["dacs_8m"] = True

        # e-Reader
        data["ereader"] = False
        if (
            (data["game_title"] == "CARDE READER" and data["game_code"] == "PEAJ" and data["header_checksum"] == 0x9E)
            or (
                data["game_title"] == "CARDEREADER+" and data["game_code"] == "PSAJ" and data["header_checksum"] == 0x85
            )
            or (
                data["game_title"] == "CARDE READER" and data["game_code"] == "PSAE" and data["header_checksum"] == 0x95
            )
        ):
            data["ereader"] = True

        if unchanged:
            data["unchanged"] = data.copy()

        self.DATA = data
        data["db"] = self.GetDatabaseEntry()

        # 3D Memory (GBA Video 64 MB)
        database_entry = data["db"]
        data["3d_memory"] = bool(database_entry.get("3d", False)) if database_entry is not None else False

        return data

    def GetDatabaseEntry(self) -> AGBDatabaseEntry | None:
        data = self.DATA
        if data is None or not isinstance(data.get("header_sha1"), str):
            return None

        db_entry: AGBDatabaseEntry | None = None
        database_path = Path(AppContext.CONFIG_PATH) / "db_AGB.json"
        if database_path.exists():
            with database_path.open(encoding="utf-8") as database_file:
                try:
                    raw_database: object = json.load(database_file)
                except (json.JSONDecodeError, ValueError) as e:
                    print(__("Error: Database for Game Boy Advance titles is corrupted.") + "\n" + str(e))
                    return None
                if not isinstance(raw_database, dict):
                    print(__("Error: Database for Game Boy Advance titles is corrupted."))
                    return None

                raw_entry = raw_database.get(data["header_sha1"])
                if isinstance(raw_entry, dict):
                    raw_game_code = raw_entry.get("gc")
                    if not isinstance(raw_game_code, str):
                        print(__("Error: Database for Game Boy Advance titles is corrupted."))
                        return None

                    db_entry = cast("AGBDatabaseEntry", raw_entry)
                    if raw_game_code in ("ZMAJ", "ZMBJ", "ZMDE"):
                        prefix = "AGS-"
                    elif raw_game_code == "ZBBJ":
                        prefix = "NTR-"
                    elif raw_game_code == "PEAJ":
                        prefix = "PEC-"
                    elif raw_game_code in ("PSAJ", "PSAE"):
                        prefix = "PES-"
                    else:
                        prefix = "AGB-"
                    db_entry["gc"] = prefix + raw_game_code
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
                    "Error: Database for Game Boy Advance titles not found at {path}",
                    path=str(database_path),
                ),
            )
        return db_entry
