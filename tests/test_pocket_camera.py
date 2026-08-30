"""Tests for Game Boy Camera save decoding and picture export."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from FlashGBX.PocketCamera import PocketCamera
from FlashGBX.PocketCameraWindow import PocketCameraWindow, _parse_palette_setting


def camera_save(order: bytes | None = None) -> bytearray:
    data = bytearray(PocketCamera.SAVE_SIZE)
    data[0x11D7:0x11F5] = order if order is not None else bytes(range(PocketCamera.PHOTO_COUNT))
    return data


@pytest.mark.parametrize("buffer_type", [bytes, bytearray, memoryview])
def test_load_file_accepts_byte_buffers_and_decodes_all_images(buffer_type: type) -> None:
    camera = PocketCamera()

    assert camera.LoadFile(buffer_type(camera_save()))
    assert len(camera.IMAGES) == PocketCamera.IMAGE_COUNT
    assert camera.GetPicture(0).size == (128, 112)
    assert camera.GetPicture(PocketCamera.GAME_FACE_INDEX).size == (128, 112)
    assert camera.GetPicture(PocketCamera.LAST_SEEN_INDEX).size == (128, 123)
    assert camera.ExtractPicture(PocketCamera.LAST_SEEN_INDEX).size == (128, 123)
    assert camera.IsEmpty(0)


def test_load_file_accepts_paths_and_clears_state_after_invalid_input(tmp_path: Path) -> None:
    save_path = tmp_path / "camera.sav"
    save_path.write_bytes(camera_save())
    camera = PocketCamera()

    assert camera.LoadFile(save_path)
    assert not camera.LoadFile(b"too short")
    assert camera.DATA is None
    assert camera.IMAGES == []
    assert camera.ORDER == []


def test_malformed_album_order_is_recovered_as_deleted_photos() -> None:
    order = bytes([2, 0, 1, 0, 42, 0xFF, *range(6, 30)])
    camera = PocketCamera()

    assert camera.LoadFile(camera_save(order))
    assert camera.ORDER[:3] == [1, 2, 0]
    assert camera.ORDER[-3:] == [3, 4, 5]
    assert not any(camera.IsDeleted(index) for index in range(3))
    assert all(camera.IsDeleted(index) for index in range(27, 30))


def test_convert_picture_decodes_game_boy_bitplanes() -> None:
    tile_data = bytearray(0x1000)
    tile_data[0] = 0b10100000
    tile_data[1] = 0b11000000

    picture = PocketCamera().ConvertPicture(tile_data)

    assert [picture.getpixel((x, 0)) for x in range(4)] == [3, 2, 1, 0]


def test_palette_state_is_validated_and_isolated_per_camera() -> None:
    first = PocketCamera()
    second = PocketCamera()
    assert first.LoadFile(camera_save())
    assert second.LoadFile(camera_save())

    first.SetPalette(0)

    assert PocketCamera.PALETTES[0] == first.PALETTE
    assert PocketCamera.DEFAULT_PALETTE == second.PALETTE
    assert first.GetPicture(0).getpalette()[:12] == list(PocketCamera.PALETTES[0])
    with pytest.raises(ValueError, match="12 integer channels"):
        first.SetPalette([0] * 11)


def test_export_supports_fractional_scale_png_metadata_and_no_extension(tmp_path: Path) -> None:
    camera = PocketCamera()
    assert camera.LoadFile(camera_save())
    output_path = tmp_path / "photo"

    camera.ExportPicture(0, output_path, scale=0.5)

    with Image.open(output_path) as exported:
        assert exported.format == "PNG"
        assert exported.size == (64, 56)
        assert exported.info["Title"] == "Photo 01"
    with pytest.raises(ValueError, match="positive finite"):
        camera.ExportPicture(0, tmp_path / "invalid.png", scale=0)


def test_export_centers_picture_in_frame(tmp_path: Path) -> None:
    camera = PocketCamera()
    data = camera_save()
    data[0x2000] = 0xFF
    assert camera.LoadFile(data)
    frame_buffer = io.BytesIO()
    Image.new("RGB", (160, 144), "white").save(frame_buffer, format="PNG")
    output_path = tmp_path / "framed.png"

    camera.ExportPicture(0, output_path, frame=frame_buffer.getvalue())

    with Image.open(output_path) as exported:
        assert exported.size == (160, 144)
        assert exported.getpixel((16, 16)) != (255, 255, 255)


def test_window_palette_parser_and_batch_export_names() -> None:
    custom_palette = tuple(range(12))

    assert _parse_palette_setting(json.dumps(custom_palette)) == custom_palette
    assert _parse_palette_setting("null") is None
    assert _parse_palette_setting(json.dumps([0] * 11)) is None
    assert _parse_palette_setting(json.dumps([0] * 11 + [256])) is None
    assert PocketCameraWindow._batch_export_path(Path("album.png"), 0) == Path("album01.png")
    assert PocketCameraWindow._batch_export_path(Path("album.png"), 31) == Path("album32.png")
