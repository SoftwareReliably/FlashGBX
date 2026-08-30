# FlashGBX  # noqa: N999
# Author: Lesserkuma (github.com/Lesserkuma)

from __future__ import annotations

import email.utils
import hashlib
import io
import math
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, TypeAlias

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from .app import AppInfo

if TYPE_CHECKING:
    from collections.abc import Sequence

CameraSource: TypeAlias = bytes | bytearray | memoryview | str | PathLike[str]  # noqa: UP040
FrameData: TypeAlias = bytes | bytearray | memoryview | Literal[False] | None  # noqa: UP040
Palette: TypeAlias = tuple[int, ...]  # noqa: UP040


class PocketCamera:
    SAVE_SIZE: ClassVar[int] = 128 * 1024
    PHOTO_COUNT: ClassVar[int] = 30
    IMAGE_COUNT: ClassVar[int] = 32
    GAME_FACE_INDEX: ClassVar[int] = 30
    LAST_SEEN_INDEX: ClassVar[int] = 31
    PALETTES: ClassVar[tuple[Palette, ...]] = (
        (255, 255, 255, 176, 176, 176, 104, 104, 104, 0, 0, 0),  # Grayscale
        (208, 217, 60, 120, 164, 106, 84, 88, 84, 36, 70, 36),  # Game Boy
        (255, 255, 255, 181, 179, 189, 84, 83, 103, 9, 7, 19),  # Super Game Boy
        (240, 240, 240, 218, 196, 106, 112, 88, 52, 30, 30, 30),  # Game Boy Color (JPN)
        (240, 240, 240, 220, 160, 160, 136, 78, 78, 30, 30, 30),  # Game Boy Color (USA Gold)
        (240, 240, 240, 134, 200, 100, 58, 96, 132, 30, 30, 30),  # Game Boy Color (USA/EUR)
    )
    # CLI / argparse identifiers for PALETTES (parallel sequence, same order).
    PALETTE_NAMES: ClassVar[tuple[str, ...]] = ("grayscale", "dmg", "sgb", "cgb1", "cgb2", "cgb3")
    # Output file formats accepted by ExportPicture().
    OUTPUT_FORMATS: ClassVar[tuple[str, ...]] = ("png", "bmp", "gif", "jpg")
    DEFAULT_PALETTE: ClassVar[Palette] = PALETTES[3]
    _EMPTY_IMAGE_SHA1: ClassVar[bytes] = bytes.fromhex("ef58a812a81ab14549d8f4fb86e9ecb54a5fb723")

    DATA: bytes | None
    PALETTE: Palette
    IMAGES: list[Image.Image]
    IMAGES_DELETED: list[int]
    ORDER: list[int]

    def __init__(self) -> None:
        self.DATA = None
        self.PALETTE = self.DEFAULT_PALETTE
        self.IMAGES = []
        self.IMAGES_DELETED = []
        self.ORDER = []

    def LoadFile(self, savefile: CameraSource) -> bool:
        """Load and decode one 128 KiB Game Boy Camera save."""
        self.DATA = None
        self.IMAGES = []
        self.IMAGES_DELETED = []
        self.ORDER = []

        data = bytes(savefile) if isinstance(savefile, (bytes, bytearray, memoryview)) else Path(savefile).read_bytes()

        if len(data) != self.SAVE_SIZE:
            return False

        self.DATA = data

        # The album table maps physical slots to display positions. Deleted,
        # duplicate, and malformed entries are kept at the end of the album.
        order_raw = data[0x11D7:0x11F5]
        ordered_slots: list[int | None] = [None] * self.PHOTO_COUNT
        deleted_slots: list[int] = []
        seen_positions: set[int] = set()
        for slot, position in enumerate(order_raw):
            if position >= self.PHOTO_COUNT or position in seen_positions:
                deleted_slots.append(slot)
                continue
            ordered_slots[position] = slot
            seen_positions.add(position)

        self.ORDER = [slot for slot in ordered_slots if slot is not None]
        self.ORDER.extend(deleted_slots)
        self.IMAGES_DELETED = deleted_slots
        self.IMAGES = [self.ExtractPicture(index) for index in range(self.IMAGE_COUNT)]
        return True

    def SetPalette(self, palette: int | Sequence[int]) -> None:
        selected = self.PALETTES[palette] if isinstance(palette, int) else tuple(palette)
        if len(selected) != 12 or any(
            isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 255 for channel in selected
        ):
            msg = "A Game Boy Camera palette must contain 12 integer channels in the range 0–255"
            raise ValueError(msg)

        for image in self.IMAGES:
            image.putpalette(selected)
        self.PALETTE = selected

    def GetPicture(self, index: int) -> Image.Image:
        return self.IMAGES[index]

    def IsEmpty(self, index: int) -> bool:
        return hashlib.sha1(self.GetPicture(index).tobytes()).digest() == self._EMPTY_IMAGE_SHA1

    def IsDeleted(self, index: int) -> bool:
        return self.ORDER[index] in self.IMAGES_DELETED

    def ConvertPicture(self, buffer: bytes | bytearray | memoryview, lastseen: bool = False) -> Image.Image:
        tile_width = 16
        tile_height = 16 if lastseen else 14
        required_size = tile_width * tile_height * 16
        if len(buffer) < required_size:
            msg = f"Camera image data is too short: expected at least {required_size} bytes"
            raise ValueError(msg)

        image_height = 128 if lastseen else 112
        image = Image.new(mode="P", size=(128, image_height))
        image.putpalette(self.PALETTE)
        pixels = image.load()
        if pixels is None:
            msg = "Pillow could not allocate the camera image buffer"
            raise RuntimeError(msg)
        for tile_y in range(tile_height):
            for tile_x in range(tile_width):
                tile_position = 16 * ((tile_y * tile_width) + tile_x)
                tile = buffer[tile_position : tile_position + 16]
                for pixel_y in range(8):
                    for pixel_x in range(8):
                        high_bit = (tile[pixel_y * 2] >> (7 - pixel_x)) & 1
                        low_bit = (tile[pixel_y * 2 + 1] >> (7 - pixel_x)) & 1
                        pixels[(tile_x * 8) + pixel_x, (tile_y * 8) + pixel_y] = (low_bit << 1) | high_bit

        return image.crop((0, 0, 128, 123 if lastseen else 112))

    def ExtractGameFace(self) -> Image.Image:
        data = self._loaded_data()
        offset = 0x11FC
        return self.ConvertPicture(data[offset : offset + 0x1000])

    def ExtractLastSeen(self) -> Image.Image:
        data = self._loaded_data()
        return self.ConvertPicture(data[:0x1000], lastseen=True)

    def ExtractPicture(self, index: int) -> Image.Image:
        if not 0 <= index < self.IMAGE_COUNT:
            raise IndexError(index)
        if index == self.GAME_FACE_INDEX:
            return self.ExtractGameFace()
        if index == self.LAST_SEEN_INDEX:
            return self.ExtractLastSeen()

        data = self._loaded_data()
        slot = self.ORDER[index]
        offset = 0x2000 + (slot * 0x1000)
        return self.ConvertPicture(data[offset : offset + 0x1000])

    def ExportPicture(
        self,
        index: int,
        path: str | PathLike[str],
        scale: float = 1.0,
        frame: FrameData = False,
    ) -> None:
        pnginfo = PngInfo()
        pnginfo.add_text("Software", AppInfo.NAME)
        pnginfo.add_text("Creation Time", email.utils.formatdate())

        picture = self.GetPicture(index)
        if index == self.GAME_FACE_INDEX:
            pnginfo.add_text("Title", "Game Face")
        elif index == self.LAST_SEEN_INDEX:
            pnginfo.add_text("Title", "Last Seen Image")
        else:
            pnginfo.add_text("Title", f"Photo {index + 1:02d}")

        if frame is not False and frame is not None:
            with Image.open(io.BytesIO(bytes(frame))) as frame_image:
                framed_picture = frame_image.convert("RGB")
            if framed_picture.width >= 160 and framed_picture.height >= 144:
                left = math.floor(framed_picture.width / 2) - 64
                top = math.floor(framed_picture.height / 2) - 56
                framed_picture.paste(picture, (left, top))
                picture = framed_picture

        scale_value = float(scale)
        if not math.isfinite(scale_value) or scale_value <= 0:
            msg = "Picture scale must be a positive finite number"
            raise ValueError(msg)
        output_size = (round(picture.width * scale_value), round(picture.height * scale_value))
        if min(output_size) < 1:
            msg = "Picture scale is too small to produce an image"
            raise ValueError(msg)
        picture = picture.resize(output_size, Image.Resampling.NEAREST)

        output_path = Path(path)
        extension = output_path.suffix.lower()
        if extension in ("", ".png"):
            picture.save(output_path, format="PNG", pnginfo=pnginfo)
        elif extension == ".gif":
            picture.save(output_path)
        elif extension in (".jpg", ".jpeg"):
            picture.convert("RGB").save(output_path, quality=100, subsampling=0)
        else:
            picture.convert("RGB").save(output_path)

    def _loaded_data(self) -> bytes:
        if self.DATA is None:
            msg = "No Game Boy Camera save data is loaded"
            raise RuntimeError(msg)
        return self.DATA
