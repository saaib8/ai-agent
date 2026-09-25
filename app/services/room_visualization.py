"""Rendering the room package as a picture.

The render is of the room the session holds - never of products a client
names. Every piece is read from the catalog through the store-scoped
repository, so an inactive or foreign product is simply not in the picture;
the room's size and style are what the customer said. An image model draws
it, and the picture is stored and served by URL.

A render is a turn of the conversation, like a pick in a photo: it is
recorded in the history, in words, so the agent knows the customer has seen
their room, and it moves the session revision. It changes no state - a
picture is not a fact about the room.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import Protocol

from PIL import Image, UnidentifiedImageError

from app.core.config import SessionSettings, VisualizationSettings
from app.core.exceptions import NothingToVisualizeError, RenderUnavailableError
from app.core.logging import get_logger
from app.integrations.image_generation import ImageGenerator, ImageReference
from app.integrations.render_store import RenderStore
from app.prompts.visualization.v1 import (
    VERSION,
    RenderPiece,
    RenderRoom,
    build_prompt,
    reference_caption,
    view_label,
)
from app.repositories.products import ProductRepository
from app.repositories.sessions import SessionStore
from app.schemas.agent_state import AgentStateV1, RoomProjectState
from app.schemas.agent_turn import CustomerResponse
from app.schemas.chat import ChatPresentation, ChatResponse
from app.schemas.dimensions import DimensionStatus
from app.schemas.geometry import RoomMeasurementRole
from app.schemas.product import ProductCandidate
from app.schemas.retailer import RetailerContext
from app.schemas.visualization import (
    RenderView,
    RoomRenderItem,
    RoomRenderPresentation,
    VisualizeRequest,
)
from app.services.chat_runtime import commit_exchange, load_for_turn
from app.services.discovery import to_candidate
from app.taxonomy.attributes import AttributeFamily

logger = get_logger(__name__)

_VIEW_PHRASES = {
    RenderView.CORNER: "seen from the corner",
    RenderView.EYE_LEVEL: "at eye level",
    RenderView.ISOMETRIC: "from above",
    RenderView.TOP_DOWN: "from directly overhead",
}


class PhotoFetcher(Protocol):
    async def fetch(self, url: str) -> bytes | None: ...


class RoomVisualizer:
    def __init__(
        self,
        repository: ProductRepository,
        photos: PhotoFetcher,
        generator: ImageGenerator,
        store: RenderStore,
        settings: VisualizationSettings,
    ) -> None:
        self._repository = repository
        self._photos = photos
        self._generator = generator
        self._store = store
        self._settings = settings

    async def render(
        self, state: AgentStateV1, view: RenderView, context: RetailerContext
    ) -> RoomRenderPresentation:
        started = time.perf_counter()
        room = state.room_project
        if room is None or not room.bundle_items:
            raise NothingToVisualizeError(store_id=context.store_id)

        items = await self._pieces(room, context)
        if not items:
            # Every piece has gone from the catalog since the room was put
            # together. A render of an empty room would be a picture of nothing.
            raise NothingToVisualizeError(store_id=context.store_id, reason="no_available_pieces")

        photos = await self._photos_for(items)
        pieces: list[RenderPiece] = []
        references: list[ImageReference] = []
        for (product, quantity), photo in zip(items, photos, strict=True):
            number = len(references) + 1 if photo is not None else None
            piece = RenderPiece(
                reference=number,
                name=product.name_english,
                kind=_kind(product),
                size_cm=_size(product),
                quantity=quantity,
            )
            pieces.append(piece)
            if photo is not None:
                references.append(ImageReference(data=photo, caption=reference_caption(piece)))

        prompt = build_prompt(_room(room), tuple(pieces), view)
        image = await self._generator.generate(prompt, references)
        width, height = _dimensions(image.data)
        key = f"store-{context.store_id}/{datetime.now(UTC):%Y%m%d}/{uuid.uuid4().hex}.jpg"
        url = await self._store.save(key, image.data, image.mime)

        logger.info(
            "room_rendered",
            store_id=context.store_id,
            view=view.value,
            piece_count=len(pieces),
            reference_count=len(references),
            provider=image.provider,
            prompt_version=VERSION,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return RoomRenderPresentation(
            image_url=url,
            width=width,
            height=height,
            view=view,
            view_label=view_label(view),
            items=tuple(
                RoomRenderItem(
                    name_english=product.name_english,
                    image_url=product.image_url,
                    product_url=product.product_url,
                    quantity=quantity,
                )
                for product, quantity in items
            ),
        )

    async def _pieces(
        self, room: RoomProjectState, context: RetailerContext
    ) -> list[tuple[ProductCandidate, int]]:
        """Package lines as products and counts, in bundle order.

        A product on two lines - one sofa filling two roles - is one piece
        with the lines' units added: the picture shows what will be in the
        room, not how the plan accounts for it.
        """
        quantities: dict[int, int] = {}
        for line in room.bundle_items:
            quantities[line.product_id] = quantities.get(line.product_id, 0) + line.quantity
        rows = await self._repository.get_by_ids(list(quantities), context)
        by_id = {row.id: to_candidate(row) for row in rows}
        return [(by_id[pid], qty) for pid, qty in quantities.items() if pid in by_id]

    async def _photos_for(
        self, items: Sequence[tuple[ProductCandidate, int]]
    ) -> list[bytes | None]:
        """Photos for as many pieces as a render can take, fetched together."""
        limit = self._settings.max_references
        fetched = await asyncio.gather(
            *(self._photos.fetch(product.image_url) for product, _ in items[:limit])
        )
        return [*fetched, *([None] * (len(items) - len(fetched)))]


class VisualizationTurnRuntime:
    """A request to see the room, run as one committed conversation turn."""

    def __init__(
        self, visualizer: RoomVisualizer, sessions: SessionStore, settings: SessionSettings
    ) -> None:
        self._visualizer = visualizer
        self._sessions = sessions
        self._settings = settings

    async def visualize(self, request: VisualizeRequest, context: RetailerContext) -> ChatResponse:
        """Load, render, commit, answer.

        The revision is checked before an image model is paid: a screen that
        is out of date would render a room the customer is no longer looking
        at.
        """
        loaded = await load_for_turn(
            self._sessions,
            store_id=request.store_id,
            session_id=request.session_id,
            expected_revision=request.expected_session_revision,
        )
        state = loaded.envelope.state
        render = await self._visualizer.render(state, request.view, context)
        room_label = _room_label(state.room_project)
        response = CustomerResponse(
            message=f"Here's your {room_label}, {_VIEW_PHRASES[request.view]}."
        )
        revision = await commit_exchange(
            self._sessions,
            self._settings,
            store_id=request.store_id,
            session_id=request.session_id,
            loaded=loaded,
            state=state,
            customer_said=(
                f"[Asked to see the room package visualised, {render.view_label.lower()} view]"
            ),
            response=response,
        )
        return ChatResponse(
            session_id=request.session_id,
            session_revision=revision,
            response=response,
            presentation=ChatPresentation(render=render),
        )


# ── helpers ─────────────────────────────────────────────────────────────────


def _room(room: RoomProjectState) -> RenderRoom:
    length = width = None
    if room.geometry is not None:
        measured_length = room.geometry.one(RoomMeasurementRole.ROOM_LENGTH)
        measured_width = room.geometry.one(RoomMeasurementRole.ROOM_WIDTH)
        if measured_length is not None and measured_width is not None:
            length = float(measured_length.centimetres) / 100
            width = float(measured_width.centimetres) / 100
    styles = [
        preference.canonical_value.replace("_", " ").lower()
        for preference in room.design_preferences
        if preference.family is AttributeFamily.STYLE and preference.canonical_value
    ]
    return RenderRoom(
        room_label=_room_label(room),
        style=", ".join(dict.fromkeys(styles)) or None,
        length_m=length,
        width_m=width,
    )


def _room_label(room: RoomProjectState | None) -> str:
    """What the customer called the room. Free text, used only as words."""
    label = (room.room_type or "").strip().lower() if room is not None else ""
    return label or "room"


def _kind(product: ProductCandidate) -> str:
    kind = product.commerce.subcategory or product.commerce.category or "piece"
    return kind.replace("-", " ")


def _size(product: ProductCandidate) -> tuple[float, ...]:
    """Centimetre sizes the catalog could normalise, and nothing else.

    An unrecognised unit gives no size at all rather than a guessed one; the
    photo still shows the piece.
    """
    dims = product.dimensions
    if dims.status is not DimensionStatus.NORMALISED:
        return ()
    return tuple(float(value) for value in (dims.length_cm, dims.width_cm, dims.height_cm) if value)


def _dimensions(data: bytes) -> tuple[int, int]:
    """The rendered image's size - and proof it is an image at all."""
    try:
        with Image.open(BytesIO(data)) as image:
            return image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise RenderUnavailableError(reason="undecodable_image") from exc
