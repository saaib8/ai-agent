"""Pixel work for Furniture Finder. Pure functions over bytes.

Nothing here calls a provider, reads settings or touches state, so every rule
about what a usable photo is and what a crop contains is testable with a
synthetic image.

Two crops per object, because that is how the product image index is queried
upstream: a **tight** crop with everything outside the object's outline turned
white - a catalog-style cut-out, so the room's floor and walls do not pull the
match towards products photographed in similar rooms - and a **medium** crop
with some surrounding context, which carries shape cues a hard cut-out loses.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from app.core.exceptions import ImageRejectedError
from app.schemas.furniture_finder import ImageBox, Point

ACCEPTED_FORMATS = frozenset({"JPEG", "MPO", "PNG", "WEBP"})
"""MPO is how Pillow names the multi-picture JPEGs many phones write."""

MAX_PIXELS = 50_000_000
"""Checked from the header before decoding, so a small file that declares an
enormous canvas is refused rather than expanded into memory."""

_WHITE = (255, 255, 255)
_UPLOAD_QUALITY = 90
_CROP_QUALITY = 95


@dataclass(frozen=True, slots=True)
class PreparedPhoto:
    """The photo as it will be detected, stored and cropped."""

    jpeg: bytes
    width: int
    height: int


def prepare_upload(data: bytes, *, max_bytes: int, min_side: int, max_side: int) -> PreparedPhoto:
    """A customer upload as an upright RGB JPEG no larger than `max_side`.

    Orientation is applied from EXIF first: a portrait phone photo is stored
    sideways with a rotation flag, and detecting on the raw pixels would draw
    every outline on a picture the customer never saw.
    """
    if not data:
        raise ImageRejectedError(public_message="The uploaded file is empty.")
    if len(data) > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        raise ImageRejectedError(
            public_message=f"That photo is too large. Please upload one under {limit_mb} MB.",
            size_bytes=len(data),
        )
    try:
        image = Image.open(BytesIO(data))
        image_format = image.format
        if image_format not in ACCEPTED_FORMATS:
            raise ImageRejectedError(image_format=image_format)
        if image.width * image.height > MAX_PIXELS:
            raise ImageRejectedError(
                public_message="That photo's resolution is too high to process.",
                width=image.width,
                height=image.height,
            )
        rgb = _flattened(ImageOps.exif_transpose(image))
    except ImageRejectedError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        # Truncated, corrupt or not an image at all. Pillow's message is not
        # ours to show, so the public one is fixed.
        raise ImageRejectedError(error_type=type(exc).__name__) from exc

    if min(rgb.size) < min_side:
        raise ImageRejectedError(
            public_message=(
                f"That photo is too small. Please use one at least {min_side} pixels on each side."
            ),
            width=rgb.width,
            height=rgb.height,
        )
    if max(rgb.size) > max_side:
        rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return PreparedPhoto(jpeg=_jpeg(rgb, _UPLOAD_QUALITY), width=rgb.width, height=rgb.height)


def bounded_box(x1: int, y1: int, x2: int, y2: int, width: int, height: int) -> ImageBox | None:
    """The part of a box inside the image, or None when nothing of it is.

    Takes plain coordinates because its input is often a detector's, which
    has not been validated as a box yet - that is what this decides.
    """
    if x1 >= width or y1 >= height or x2 <= 0 or y2 <= 0:
        # Wholly outside. Clamping would squeeze it into a one-pixel sliver
        # at the edge, which is not the object and would still be searched.
        return None
    left = min(max(x1, 0), width - 1)
    top = min(max(y1, 0), height - 1)
    right = min(max(x2, 0), width)
    bottom = min(max(y2, 0), height)
    if right <= left or bottom <= top:
        return None
    return ImageBox(x1=left, y1=top, x2=right, y2=bottom)


def clamp_polygon(points: tuple[Point, ...], width: int, height: int) -> tuple[Point, ...]:
    return tuple(
        (min(max(x, 0), width - 1), min(max(y, 0), height - 1)) for x, y in points
    )


def object_views(
    photo_jpeg: bytes, *, box: ImageBox, polygon: tuple[Point, ...], padding: float
) -> tuple[bytes, bytes]:
    """The tight cut-out and the padded context crop, as JPEG bytes.

    An outline that covers nothing inside its own box - a degenerate polygon -
    leaves the tight crop unmasked rather than solid white: an empty cut-out
    would embed as "white rectangle" and match whatever product happens to be
    photographed on the plainest background.
    """
    image = Image.open(BytesIO(photo_jpeg)).convert("RGB")
    bounds = bounded_box(box.x1, box.y1, box.x2, box.y2, image.width, image.height)
    if bounds is None:
        raise ValueError("object box lies outside the photo")
    area = (bounds.x1, bounds.y1, bounds.x2, bounds.y2)

    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).polygon(list(polygon), fill=255)
    tight = image.crop(area)
    tight_mask = mask.crop(area)
    if tight_mask.getbbox() is not None:
        tight = Image.composite(tight, Image.new("RGB", tight.size, _WHITE), tight_mask)

    pad = int(padding * max(bounds.x2 - bounds.x1, bounds.y2 - bounds.y1))
    medium = image.crop(
        (
            max(0, bounds.x1 - pad),
            max(0, bounds.y1 - pad),
            min(image.width, bounds.x2 + pad),
            min(image.height, bounds.y2 + pad),
        )
    )
    return _jpeg(tight, _CROP_QUALITY), _jpeg(medium, _CROP_QUALITY)


def _flattened(image: Image.Image) -> Image.Image:
    """RGB, with any transparency laid over white rather than black.

    A cut-out PNG's transparent background would otherwise become black, and
    the detector would see a product on a black wall.
    """
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, _WHITE)
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def _jpeg(image: Image.Image, quality: int) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()
