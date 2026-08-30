"""Tests for Game Boy Camera save decoding and picture export."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from FlashGBX.PocketCamera import PocketCamera
from FlashGBX.PocketCameraWindow import PocketCameraWindow, _parse_palette_setting

CAMERA_CALIBRATION = bytes.fromhex("7E7D7D7E7C7A7C7A7A777368BB39")


def camera_save(order: bytes | None = None) -> bytearray:
    data = bytearray(PocketCamera.SAVE_SIZE)
    data[0x11D7:0x11F5] = order if order is not None else bytes(range(PocketCamera.PHOTO_COUNT))
    # Calibration locations and bytes observed from the attached camera. They
    # sit outside the decoded tile region and therefore make a safe, realistic fixture.
    data[0x4FF2:0x5000] = CAMERA_CALIBRATION
    data[0x11FF2:0x12000] = CAMERA_CALIBRATION
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


@pytest.mark.parametrize("last_seen", [False, True])
def test_convert_picture_rejects_truncated_camera_tiles(last_seen: bool) -> None:
    required_size = 16 * (16 if last_seen else 14) * 16

    with pytest.raises(ValueError, match=f"expected at least {required_size} bytes"):
        PocketCamera().ConvertPicture(bytes(required_size - 1), lastseen=last_seen)


def test_convert_picture_reports_image_allocation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    image = Mock()
    image.load.return_value = None
    monkeypatch.setattr("FlashGBX.PocketCamera.Image.new", Mock(return_value=image))

    with pytest.raises(RuntimeError, match="could not allocate"):
        PocketCamera().ConvertPicture(bytes(0x1000))

    image.putpalette.assert_called_once_with(PocketCamera.DEFAULT_PALETTE)


def test_extract_picture_requires_loaded_data_and_valid_index() -> None:
    camera = PocketCamera()

    with pytest.raises(RuntimeError, match="No Game Boy Camera save data"):
        camera.ExtractGameFace()
    with pytest.raises(IndexError):
        camera.ExtractPicture(-1)
    with pytest.raises(IndexError):
        camera.ExtractPicture(PocketCamera.IMAGE_COUNT)


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


def test_export_special_images_and_formats(tmp_path: Path) -> None:
    camera = PocketCamera()
    assert camera.LoadFile(camera_save())

    game_face = tmp_path / "game-face.png"
    last_seen = tmp_path / "last-seen.png"
    gif_path = tmp_path / "photo.gif"
    jpg_path = tmp_path / "photo.jpg"
    bmp_path = tmp_path / "photo.bmp"
    camera.ExportPicture(PocketCamera.GAME_FACE_INDEX, game_face)
    camera.ExportPicture(PocketCamera.LAST_SEEN_INDEX, last_seen)
    camera.ExportPicture(0, gif_path)
    camera.ExportPicture(0, jpg_path)
    camera.ExportPicture(0, bmp_path)

    with Image.open(game_face) as exported:
        assert exported.info["Title"] == "Game Face"
    with Image.open(last_seen) as exported:
        assert exported.info["Title"] == "Last Seen Image"
    for path, image_format in ((gif_path, "GIF"), (jpg_path, "JPEG"), (bmp_path, "BMP")):
        with Image.open(path) as exported:
            assert exported.format == image_format
            assert exported.size == (128, 112)


def test_export_ignores_small_frame_and_rejects_tiny_scale(tmp_path: Path) -> None:
    camera = PocketCamera()
    assert camera.LoadFile(camera_save())
    frame_buffer = io.BytesIO()
    Image.new("RGB", (159, 143), "white").save(frame_buffer, format="PNG")
    output_path = tmp_path / "unframed.png"

    camera.ExportPicture(0, output_path, frame=frame_buffer.getvalue())

    with Image.open(output_path) as exported:
        assert exported.size == (128, 112)
    with pytest.raises(ValueError, match="too small"):
        camera.ExportPicture(0, tmp_path / "tiny.png", scale=0.001)
    with pytest.raises(ValueError, match="positive finite"):
        camera.ExportPicture(0, tmp_path / "nan.png", scale=float("nan"))


def test_window_palette_parser_and_batch_export_names() -> None:
    custom_palette = tuple(range(12))

    assert _parse_palette_setting(json.dumps(custom_palette)) == custom_palette
    assert _parse_palette_setting("null") is None
    assert _parse_palette_setting(json.dumps([0] * 11)) is None
    assert _parse_palette_setting(json.dumps([0] * 11 + [256])) is None
    assert PocketCameraWindow._batch_export_path(Path("album.png"), 0) == Path("album01.png")
    assert PocketCameraWindow._batch_export_path(Path("album.png"), 31) == Path("album32.png")
