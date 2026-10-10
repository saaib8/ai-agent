"""A photo of any size is accepted (2026-10-10).

Small photos used to be refused ("at least 480 pixels on each side"). Now none
is: a small room photo is scaled up to a working size so its renders are not
postage-stamp small, and a large one is scaled down as before. The console
shrinks a big file before upload, so the byte limit is only a memory ceiling.
"""

from __future__ import annotations

from io import BytesIO

from app.core.config import FurnitureFinderSettings, VisualizationSettings
from app.services.finder_imaging import prepare_upload
from PIL import Image


def _jpeg(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 180, 160)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_no_photo_is_too_small_by_default() -> None:
    assert VisualizationSettings.model_fields["room_photo_min_side"].default is None
    assert FurnitureFinderSettings.model_fields["min_image_side"].default is None


def test_a_tiny_photo_is_accepted() -> None:
    prepared = prepare_upload(_jpeg(120, 90), max_bytes=10_000_000, min_side=None, max_side=2048)

    assert (prepared.width, prepared.height) == (120, 90), "kept as it is without upscale_to"


def test_a_small_room_photo_is_scaled_up_to_a_working_size() -> None:
    prepared = prepare_upload(
        _jpeg(400, 300), max_bytes=10_000_000, min_side=None, max_side=2048, upscale_to=1024
    )

    assert (prepared.width, prepared.height) == (1024, 768), "aspect kept"


def test_a_large_photo_is_still_scaled_down() -> None:
    prepared = prepare_upload(
        _jpeg(4000, 3000), max_bytes=40_000_000, min_side=None, max_side=2048, upscale_to=1024
    )

    assert max(prepared.width, prepared.height) == 2048


def test_the_byte_ceiling_is_far_above_any_photo() -> None:
    assert VisualizationSettings.model_fields["room_photo_max_bytes"].default >= 40 * 1024 * 1024
    assert FurnitureFinderSettings.model_fields["max_upload_bytes"].default >= 40 * 1024 * 1024
