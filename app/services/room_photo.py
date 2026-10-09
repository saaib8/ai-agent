"""The customer's own room photo: checked, emptied once, kept for the session.

"Upload room photo" puts the pieces the customer is looking at into a picture
of their real room. Three steps happen here, once per upload:

1. the upload is prepared exactly as a Furniture Finder photo is - upright,
   RGB, bounded in size - and refused before anything is paid for when it is
   not a usable image;
2. a vision model says whether it is a room at all; a selfie or a product shot
   is refused politely, with nothing emptied or charged;
3. an image model carries out the furniture and decor, keeping the room - its
   walls, floor, windows, curtains and fittings - as it is, and the answer is
   scaled back to the photo's exact size.

Only the emptied room is kept, in the session (one per session; a new upload
replaces it). Every render after that - first, after a swap, again - places
the pieces into it without emptying again. The customer never sees the empty
room on its own: they asked to see their pieces in it.
"""

from __future__ import annotations

import base64
import time
import uuid
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from app.core.config import VisualizationSettings
from app.core.exceptions import (
    LLMRequestError,
    LLMResponseInvalidError,
    LLMUnavailableError,
    NotARoomError,
    RenderUnavailableError,
    RoomPhotoNotFoundError,
    RoomPhotoUnavailableError,
)
from app.core.logging import get_logger
from app.integrations.image_generation import Frame, ImageGenerator, ImageReference
from app.prompts.room_photo.v1 import (
    EMPTY_ROOM_PROMPT,
    ROOM_CHECK_INSTRUCTIONS,
    ROOM_CHECK_USER,
    VERSION,
)
from app.repositories.room_photos import RoomPhotoStore
from app.schemas.retailer import RetailerContext
from app.schemas.room_photo import RoomCheck, RoomPhoto, RoomPhotoResponse
from app.services.finder_imaging import prepare_upload
from app.services.object_description import VisionClient

logger = get_logger(__name__)

_ROOM_JPEG_QUALITY = 90
"""The emptied room is drawn on again by every render, so it is kept close to
lossless; it lives in the session store, not in a reply."""


class RoomPhotoService:
    def __init__(
        self,
        checker: VisionClient,
        emptier: ImageGenerator,
        photos: RoomPhotoStore,
        settings: VisualizationSettings,
    ) -> None:
        self._checker = checker
        self._emptier = emptier
        self._photos = photos
        self._settings = settings

    @property
    def max_upload_bytes(self) -> int:
        return self._settings.room_photo_max_bytes

    async def upload(
        self, data: bytes, *, session_id: str, context: RetailerContext
    ) -> RoomPhotoResponse:
        """Check, empty and keep a room photo; answer with its handle.

        Each step is cheaper than the next, so an unusable file never reaches
        the vision model and a picture that is not a room is never emptied.
        """
        started = time.perf_counter()
        settings = self._settings
        prepared = prepare_upload(
            data,
            max_bytes=settings.room_photo_max_bytes,
            min_side=settings.room_photo_min_side,
            max_side=settings.room_photo_max_side,
        )
        await self._check(prepared.jpeg, context)
        frame = Frame(width=prepared.width, height=prepared.height)
        try:
            answer = await self._emptier.generate(
                EMPTY_ROOM_PROMPT,
                [ImageReference(data=prepared.jpeg, caption="Image 1: the room to empty")],
                frame,
            )
            emptied = fit_exactly(answer.data, frame, _ROOM_JPEG_QUALITY)
        except RenderUnavailableError as exc:
            raise RoomPhotoUnavailableError(step="empty") from exc

        photo = RoomPhoto(
            photo_id=uuid.uuid4().hex,
            width=frame.width,
            height=frame.height,
            jpeg_b64=base64.b64encode(emptied).decode("ascii"),
        )
        await self._photos.save(context.store_id, session_id, photo)
        logger.info(
            "room_photo_prepared",
            store_id=context.store_id,
            width=frame.width,
            height=frame.height,
            provider=answer.provider,
            image_bytes=len(emptied),
            prompt_version=VERSION,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return RoomPhotoResponse(
            room_photo_id=photo.photo_id, width=photo.width, height=photo.height
        )

    async def _check(self, jpeg: bytes, context: RetailerContext) -> None:
        """Refuse a picture that is not a room, before it is emptied.

        A check that cannot be made is not a "no": the customer is told to try
        again, never that their room is not a room.
        """
        try:
            check = await self._checker.parse_images(
                instructions=ROOM_CHECK_INSTRUCTIONS,
                user_input=ROOM_CHECK_USER,
                images=[jpeg],
                schema=RoomCheck,
            )
        except (LLMUnavailableError, LLMRequestError, LLMResponseInvalidError) as exc:
            logger.warning(
                "room_photo_check_failed",
                store_id=context.store_id,
                error_type=type(exc).__name__,
            )
            raise RoomPhotoUnavailableError(step="check") from exc
        if not check.is_room:
            logger.info("room_photo_not_a_room", store_id=context.store_id)
            raise NotARoomError()


async def load_room_photo(
    photos: RoomPhotoStore, *, session_id: str, photo_id: str, context: RetailerContext
) -> RoomPhoto:
    """The session's emptied room, or the customer is asked to upload it again."""
    photo = await photos.load(context.store_id, session_id, photo_id)
    if photo is None:
        raise RoomPhotoNotFoundError(store_id=context.store_id)
    return photo


def fit_exactly(data: bytes, frame: Frame, quality: int) -> bytes:
    """An image model's answer as a JPEG of exactly the photo's size.

    The models draw on their nearest canvas, never the photo's own, and each
    render after this one is drawn on this picture - so it is scaled back to
    the customer's frame, or the room would drift a little with every step.
    """
    try:
        with Image.open(BytesIO(data)) as image:
            rgb = image.convert("RGB")
            if rgb.size != (frame.width, frame.height):
                rgb = rgb.resize((frame.width, frame.height), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise RenderUnavailableError(reason="undecodable_image") from exc
