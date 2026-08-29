# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

import datetime
import hashlib
import struct
import zlib
from typing import Any, Literal, NamedTuple, TypeAlias, TypedDict, cast, overload

from .app import AppInfo
from .CartridgeTypes import DmgSaveTypes, RomSizes
from .i18n import __
from .Logging import dprint
from .RomFileDMG import RomFileDMG

ByteBuffer: TypeAlias = bytes | bytearray | memoryview  # noqa: UP040
MapBuffer: TypeAlias = ByteBuffer | Literal[False]  # noqa: UP040
HeaderData: TypeAlias = dict[str, Any]  # noqa: UP040

_MAP_FORMAT = "=24sHH12s44s18s8sH8s6sH"
_MAP_DATA_SIZE = struct.calcsize(_MAP_FORMAT)
_SINGLE_GAME_FORMAT = "=HH12s44s18s8s"
_MENU_ITEM_KEYS = (
    "menu_index",
    "f_offset",
    "b_offset",
    "f_size",
    "b_size",
    "game_code",
    "title",
    "title_gfx",
    "timestamp",
    "kiosk_id",
    "padding",
    "comment",
)
_MENU_TITLES: frozenset[str] = frozenset(
    (
        "NP M-MENU MENU",
        "DMG MULTI MENU ",
        "GBMEM-MENU MMSA",
        "GBMEM-MENU 256M",
    ),
)


class RawParsedMapData(TypedDict):
    """Metadata stored in a GB-Memory hidden sector before ROM decoding."""

    mapper_params: str
    f_size: int
    b_size: int
    game_code: bytes
    title: bytes
    timestamp: bytes
    kiosk_id: bytes
    write_count: int
    cart_id: str
    padding: bytes
    unknown: int


class ParsedMapData(TypedDict):
    """Decoded metadata stored in a GB-Memory hidden sector."""

    mapper_params: str
    f_size: int
    b_size: int
    game_code: str
    title: str
    timestamp: str
    kiosk_id: str
    write_count: int
    cart_id: str
    padding: bytes
    unknown: int


class ParsedMenuMapData(ParsedMapData):
    """The first item returned when a multi-game menu is parsed."""

    num_games: int


class ParsedMenuEntryBase(TypedDict):
    """Common metadata for one game listed in a GB-Memory menu."""

    menu_index: int
    f_offset: int
    b_offset: int
    f_size: int
    b_size: int
    game_code: str
    title: str
    timestamp: str
    kiosk_id: str
    comment: bytes
    rom_offset: int
    rom_size: int
    header: HeaderData


class ParsedMenuEntry(ParsedMenuEntryBase, total=False):
    """Optional checksums and database metadata for a menu entry."""

    crc32: int
    sha1: str
    sha256: str
    md5: str
    db_entry: HeaderData


ParsedMapResult: TypeAlias = (  # noqa: UP040
    RawParsedMapData | ParsedMapData | list[ParsedMapData | ParsedMenuEntry] | Literal[False]
)

_MapValues: TypeAlias = tuple[  # noqa: UP040
    bytes,
    int,
    int,
    bytes,
    bytes,
    bytes,
    bytes,
    int,
    bytes,
    bytes,
    int,
]
_MenuValues: TypeAlias = tuple[  # noqa: UP040
    int,
    int,
    int,
    int,
    int,
    bytes,
    bytes,
    bytes,
    bytes,
    bytes,
    bytes,
    bytes,
]


class _MenuLayout(NamedTuple):
    base_offset: int
    item_stride: int
    format: str


class _RawMenuItem(TypedDict):
    menu_index: int
    f_offset: int
    b_offset: int
    f_size: int
    b_size: int
    game_code: bytes
    title: bytes
    title_gfx: bytes
    timestamp: bytes
    kiosk_id: bytes
    padding: bytes
    comment: bytes


_MENU_LAYOUTS: dict[str, _MenuLayout] = {
    "NP M-MENU MENU": _MenuLayout(0x1C000, 0x200, "=BBBHH12s44s384s18s8s23s16s"),
    "DMG MULTI MENU ": _MenuLayout(0x1C000, 0x800, "=BBBHH12s44s384s18s8s1559s16s"),
    "GBMEM-MENU MMSA": _MenuLayout(0x1C000, 0x200, "=BBBHH12s44s384s18s8s23s16s"),
    "GBMEM-MENU 256M": _MenuLayout(0x10010, 0x160, "=BBBHH12s44s256s18s8s7s"),
}


class GBMemoryMap:
    MAP_DATA: bytearray = bytearray([0xFF] * 0x80)
    IS_MENU: bool = False

    def __init__(
        self,
        rom: ByteBuffer | None = None,
        oldmap: MapBuffer | None = None,
    ) -> None:
        # Keep these as instance state. The class attributes above are retained
        # for backwards compatibility, but must never be mutated by an instance.
        self.MAP_DATA = bytearray([0xFF] * 0x80)
        self.IS_MENU = False
        if rom is None or not self.ImportROM(rom):
            return
        if oldmap is None or oldmap is False:
            return

        oldmap_data = bytes(oldmap)
        if len(oldmap_data) < _MAP_DATA_SIZE:
            return
        self.MAP_DATA[0x70:0x78] = oldmap_data[0x70:0x78]  # keep existing cart id
        write_count = struct.unpack("=H", oldmap_data[0x6E:0x70])[0]
        write_count = min(write_count + 1, 0xFFFF)
        self.MAP_DATA[0x6E:0x70] = struct.pack("=H", write_count)

    @staticmethod
    def _read_header(data: ByteBuffer) -> HeaderData:
        """Read a DMG header from any supported bytes-like object."""
        return cast("HeaderData", RomFileDMG(bytearray(data)).GetHeader())

    @staticmethod
    def _title_encoding(game_title: str) -> str:
        if game_title in ("GBMEM-MENU MMSA", "GBMEM-MENU 256M"):
            return "UTF-8"
        return "SHIFT-JIS"

    @staticmethod
    def _decode(value: bytes, encoding: str = "ASCII") -> str:
        return value.decode(encoding, "ignore")

    @staticmethod
    def _fixed_ascii(value: str, length: int) -> bytes:
        return value.encode("ASCII", "ignore").ljust(length, b"\xff")[:length]

    @staticmethod
    def _timestamp() -> bytes:
        return datetime.datetime.now(tz=datetime.UTC).strftime("%d/%m/%Y%H:%M:%S").encode("ASCII")

    @staticmethod
    def _rom_size_type(size: int) -> int:
        if size <= 0x20000:
            return 0b010
        if size <= 0x40000:
            return 0b011
        if size <= 0x80000:
            return 0b100
        return 0b101

    @staticmethod
    def _sram_type(
        mapper_type: int,
        ram_size_raw: int,
        game_title: str | None = None,
    ) -> int:
        if mapper_type == 2:
            return 0b010
        if game_title in _MENU_TITLES or ram_size_raw not in DmgSaveTypes():
            return 0b000

        sram_size = DmgSaveTypes(mbc=ram_size_raw).GetSize()
        return {
            0: 0b000,
            0x2000: 0b010,
            0x8000: 0b011,
            0x10000: 0b100,
            0x20000: 0b101,
        }.get(sram_size, 0b000)

    @staticmethod
    def _pack_map(
        mbc_type: int,
        rom_size: int,
        sram_type: int,
        rom_start_block: int,
        ram_start_block: int,
    ) -> int:
        value = 0
        value |= (mbc_type & 0x7) << 29
        value |= (rom_size & 0x7) << 26
        value |= (sram_type & 0x7) << 23
        value |= (rom_start_block & 0x7F) << 16
        value |= (ram_start_block & 0x7F) << 8
        return value

    @staticmethod
    def _menu_layout(game_title: str) -> _MenuLayout | None:
        return _MENU_LAYOUTS.get(game_title)

    @classmethod
    def _unpack_menu_item(
        cls,
        data: ByteBuffer,
        game_title: str,
        index: int,
    ) -> _RawMenuItem:
        layout = cls._menu_layout(game_title)
        if layout is None:
            raise ValueError(f"Unsupported GB-Memory menu title: {game_title!r}")

        start = layout.base_offset + index * layout.item_stride
        item_size = struct.calcsize(layout.format)
        values = cast(
            "_MenuValues",
            struct.unpack(layout.format, data[start : start + item_size]),
        )
        return cast("_RawMenuItem", dict(zip(_MENU_ITEM_KEYS, values)))

    def _reset_map_data(self) -> None:
        self.MAP_DATA[:] = b"\xff" * 0x80
        self.MAP_DATA[0x6E:0x70] = b"\x00" * 2
        self.MAP_DATA[0x7E:0x80] = b"\x00" * 2

    @overload
    def ParseMapData(
        self,
        buffer_map: MapBuffer,
        buffer_rom: None = None,
    ) -> RawParsedMapData | Literal[False]: ...

    @overload
    def ParseMapData(
        self,
        buffer_map: MapBuffer,
        buffer_rom: ByteBuffer,
    ) -> ParsedMapResult: ...

    def ParseMapData(
        self,
        buffer_map: MapBuffer,
        buffer_rom: ByteBuffer | None = None,
    ) -> ParsedMapResult:
        if buffer_map is False:
            return False

        map_data = bytes(buffer_map)
        if len(map_data) < _MAP_DATA_SIZE:
            return False

        values = cast("_MapValues", struct.unpack(_MAP_FORMAT, map_data[:_MAP_DATA_SIZE]))
        (
            mapper_params,
            f_size,
            b_size,
            game_code,
            title,
            timestamp,
            kiosk_id,
            write_count,
            cart_id,
            padding,
            unknown,
        ) = values
        num_games = sum(mapper_params[i * 3 : i * 3 + 3] != b"\xff\xff\xff" for i in range(1, 8))
        raw_data: RawParsedMapData = {
            "mapper_params": mapper_params.hex().upper(),
            "f_size": f_size,
            "b_size": b_size,
            "game_code": game_code,
            "title": title,
            "timestamp": timestamp,
            "kiosk_id": kiosk_id,
            "write_count": write_count,
            "cart_id": cart_id.hex().upper(),
            "padding": padding,
            "unknown": unknown,
        }
        if buffer_rom is None:
            return raw_data

        rom_data = bytearray(buffer_rom)
        rom_header = self._read_header(rom_data[:0x180])
        game_title = rom_header.get("game_title")
        if not isinstance(game_title, str):
            return False
        data: ParsedMapData = {
            **raw_data,
            "game_code": self._decode(raw_data["game_code"]),
            "title": self._decode(raw_data["title"], self._title_encoding(game_title)),
            "timestamp": self._decode(raw_data["timestamp"]),
            "kiosk_id": self._decode(raw_data["kiosk_id"]),
        }
        if game_title not in _MENU_TITLES:
            return data

        menu_data: ParsedMenuMapData = {**data, "num_games": 0}
        data_list: list[ParsedMapData | ParsedMenuEntry] = [menu_data]
        if len(rom_data) < 0x100000:
            return data_list

        for index in range(8):
            raw_item = self._unpack_menu_item(rom_data, game_title, index)
            if raw_item["menu_index"] == 0xFF:
                continue

            rom_offset = raw_item["f_offset"] * (128 * 1024)
            rom_size = raw_item["f_size"] * (128 * 1024)
            entry: ParsedMenuEntry = {
                "menu_index": raw_item["menu_index"],
                "f_offset": raw_item["f_offset"],
                "b_offset": raw_item["b_offset"],
                "f_size": raw_item["f_size"],
                "b_size": raw_item["b_size"],
                "game_code": self._decode(raw_item["game_code"]),
                "title": self._decode(raw_item["title"], self._title_encoding(game_title)),
                "timestamp": self._decode(raw_item["timestamp"]),
                "kiosk_id": self._decode(raw_item["kiosk_id"]),
                "comment": raw_item["comment"],
                "rom_offset": rom_offset,
                "rom_size": rom_size,
                "header": {},
            }
            rom_header_game = self._read_header(rom_data[rom_offset : rom_offset + rom_size])
            entry["header"] = rom_header_game
            if rom_header_game and len(rom_data) >= rom_offset + rom_size:
                rom_size_raw = rom_header_game.get("rom_size_raw")
                if isinstance(rom_size_raw, int):
                    actual_size = RomSizes().GetSize(rom_size_raw)
                    if actual_size is not None:
                        entry["rom_size"] = min(rom_size, actual_size)

                rom_bytes = rom_data[rom_offset : rom_offset + entry["rom_size"]]
                entry["crc32"] = zlib.crc32(rom_bytes) & 0xFFFFFFFF
                entry["sha1"] = hashlib.sha1(rom_bytes).hexdigest()
                entry["sha256"] = hashlib.sha256(rom_bytes).hexdigest()
                entry["md5"] = hashlib.md5(rom_bytes).hexdigest()

                db_entry = rom_header_game.get("db")
                if isinstance(db_entry, dict) and db_entry.get("rc") == entry["crc32"]:
                    entry["db_entry"] = cast("HeaderData", db_entry)
                else:
                    rom_header_game["db"] = None
            dprint(f"GB-Memory Game {index:d}: {entry!s:s}")
            data_list.append(entry)

        menu_data["num_games"] = num_games
        if not menu_data["timestamp"] and len(data_list) > 1:
            first_entry = cast("ParsedMenuEntry", data_list[1])
            menu_data["timestamp"] = first_entry["timestamp"]
            menu_data["kiosk_id"] = first_entry["kiosk_id"]
        return data_list

    def ImportROM(self, data: ByteBuffer) -> bool:
        """Generate hidden-sector map data for a ROM or GB-Memory menu ROM."""
        self._reset_map_data()
        self.IS_MENU = False
        if len(data) < 0x180:
            return False

        rom_data = bytearray(data)
        rom_header = self._read_header(rom_data[:0x180])
        game_title = rom_header.get("game_title")
        if not isinstance(game_title, str):
            return False
        self.IS_MENU = game_title in _MENU_TITLES

        if len(rom_data) < 0x20000:
            rom_data.extend(b"\xff" * (0x20000 - len(rom_data)))

        if not self.IS_MENU:
            mapper_raw = rom_header.get("mapper_raw")
            ram_size_raw = rom_header.get("ram_size_raw")
            game_code = rom_header.get("game_code")
            cgb = rom_header.get("cgb")
            if (
                not isinstance(mapper_raw, int)
                or not isinstance(ram_size_raw, int)
                or not isinstance(cgb, int)
                or not isinstance(game_code, str)
            ):
                return False

            mbc_type = self.MapperToMBCType(mapper_raw)
            rom_size = self._rom_size_type(len(rom_data))
            sram_type = self._sram_type(mbc_type, ram_size_raw)
            title = game_title
            db_entry = rom_header.get("db")
            if isinstance(db_entry, dict) and isinstance(db_entry.get("gn"), str):
                title = db_entry["gn"]

            menu_data = struct.pack(
                _SINGLE_GAME_FORMAT,
                len(rom_data) // (128 * 1024),
                {
                    0b000: 0,
                    0b001: 64,
                    0b010: 64,
                    0b011: 256,
                    0b100: 512,
                    0b101: 1024,
                }.get(sram_type, 0),
                f"{'CGB' if cgb == 0xC0 else 'DMG'} -{game_code:4s}-  ".encode("ASCII", "ignore"),
                title.encode(self._title_encoding(game_title), "ignore").ljust(0x2C, b"\x00")[:0x2C],
                self._timestamp(),
                self._fixed_ascii(AppInfo.NAME, 8),
            )
            map_raw = self._pack_map(
                mbc_type,
                rom_size,
                sram_type,
                rom_start_block=0,
                ram_start_block=0,
            )
            self.MAP_DATA[0:3] = struct.pack(">I", map_raw)[:3]
            self.MAP_DATA[0x18 : 0x18 + len(menu_data)] = menu_data
            return True

        layout = self._menu_layout(game_title)
        if layout is None:
            return False

        menu_items: list[int] = []
        rom_offset = 0
        ram_offset = 0
        for index in range(8):
            raw_item = self._unpack_menu_item(rom_data, game_title, index)
            if raw_item["menu_index"] == 0xFF:
                continue

            rom_data_offset = raw_item["f_offset"] * (128 * 1024)
            rom_data_size = raw_item["f_size"] * (128 * 1024)
            ram_data_size = self.GetBlockSizeBackup(raw_item["b_size"]) * (8 * 1024)
            rom_start_block = rom_offset // 0x8000
            rom_offset += rom_data_size
            ram_start_block = ram_offset // 0x800
            ram_offset += ram_data_size

            game_header = self._read_header(rom_data[rom_data_offset : rom_data_offset + 0x180])
            if not game_header:
                return False
            mapper_raw = game_header.get("mapper_raw")
            ram_size_raw = game_header.get("ram_size_raw")
            nested_title = game_header.get("game_title")
            if not isinstance(mapper_raw, int) or not isinstance(ram_size_raw, int):
                return False

            mbc_type = self.MapperToMBCType(mapper_raw)
            sram_type = self._sram_type(
                mbc_type,
                ram_size_raw,
                nested_title if isinstance(nested_title, str) else None,
            )
            menu_items.append(
                self._pack_map(
                    mbc_type,
                    self._rom_size_type(rom_data_size),
                    sram_type,
                    rom_start_block,
                    ram_start_block,
                ),
            )

        for index, map_raw in enumerate(menu_items):
            pos = index * 3
            self.MAP_DATA[pos : pos + 3] = struct.pack(">I", map_raw)[:3]
        self.MAP_DATA[0x54:0x66] = self._timestamp()
        self.MAP_DATA[0x66:0x6E] = self._fixed_ascii(AppInfo.NAME, 8)
        return True

    def MapperToMBCType(self, mbc: int) -> int:
        if mbc == 0x00:  # ROM only
            mbc_type = 0
        elif mbc in (0x01, 0x02, 0x03):  # MBC1
            mbc_type = 1
        elif mbc == 0x06:  # MBC2
            mbc_type = 2
        elif mbc in (0x10, 0x13):  # MBC3
            mbc_type = 3
        elif mbc in (0x19, 0x1A, 0x1B, 0x1C, 0x1E, 0x105):  # MBC5
            mbc_type = 5
        else:
            # mbc_type = False
            print(
                __(
                    "Note: The ROM is using a mapper type that may be incompatible with the {gb_memory_cartridge}.",
                    gb_memory_cartridge="GB Memory Cartridge",
                )
                + f" (0x{mbc:02X})",
            )
            mbc_type = 5
        return mbc_type

    def GetBlockSizeBackup(self, b_size: int | None = None) -> int:
        if b_size is None:
            return 4
        return {0: 0, 1: 1, 64: 1, 256: 4, 1024: 16}.get(b_size, 4)

    def IsMenu(self) -> bool:
        return self.IS_MENU

    def GetMapData(self) -> bytearray:
        # if self.MAP_DATA == bytearray([0xFF] * 0x80):
        # 	return False
        return self.MAP_DATA
