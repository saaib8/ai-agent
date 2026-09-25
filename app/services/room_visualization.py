"""Rendering a room as a picture.

Two kinds of render share one drawing path. A package render is of the room
the session holds: its pieces, and the size and style the customer said. A
catalogue render is of pieces the customer picked while browsing, in a room
they set up. Either way every piece is read from the catalog through the
store-scoped repository, so an inactive or foreign product is simply not in
the picture. An image model draws it, and the picture is stored and served by
URL.

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

from app.core.config import CatalogSettings, SessionSettings, VisualizationSettings
from app.core.exceptions import (
    NothingToVisualizeError,
    RenderUnavailableError,
    SelectionRejectedError,
    SelectionUnavailableError,
)
from app.core.logging import get_logger
from app.integrations.image_generation import ImageGenerator, ImageReference
from app.integrations.render_store import RenderStore
from app.prompts.visualization.v1 import (
    VERSION,
    RenderPiece,
    RenderRoom,
    build_prompt,
    reference_caption,
    room_type_label,
    room_type_words,
    view_label,
)
from app.repositories.products import ProductRepository
from app.repositories.sessions import SessionStore
from app.schemas.agent_state import AgentStateV1, RoomProjectState
from app.schemas.agent_turn import CustomerResponse
from app.schemas.catalog import CatalogSelectionItem, CatalogVisualizeRequest
from app.schemas.chat import ChatPresentation, ChatResponse
from app.schemas.dimensions import DimensionStatus
from app.schemas.geometry import RoomMeasurementRole
from app.schemas.product import ProductCandidate
from app.schemas.retailer import RetailerContext
from app.schemas.visualization import (
    RenderRoomSpec,
    RenderSource,
    RenderView,
    RoomRenderItem,
    RoomRenderPresentation,
    VisualizeRequest,
)
from app.services.chat_runtime import commit_exchange, load_for_turn
from app.services.discovery import to_candidate
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes

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
        """The session's room package."""
        room = state.room_project
        if room is None or not room.bundle_items:
            raise NothingToVisualizeError(store_id=context.store_id)

        items = await self._pieces(room, context)
        if not items:
            # Every piece has gone from the catalog since the room was put
            # together. A render of an empty room would be a picture of nothing.
            raise NothingToVisualizeError(store_id=context.store_id, reason="no_available_pieces")
        return await self._draw(items, _room(room), view, context, RenderSource.PACKAGE, None)

    async def render_selection(
        self,
        selection: Sequence[CatalogSelectionItem],
        spec: RenderRoomSpec,
        view: RenderView,
        context: RetailerContext,
    ) -> RoomRenderPresentation:
        """Pieces the customer picked from the catalogue, in the room they set up.

        The ids are read back through the store-scoped repository; any the
        store no longer sells are left out, in the order the customer picked.
        """
        rows = await self._repository.get_by_ids([item.product_id for item in selection], context)
        by_id = {row.id: to_candidate(row) for row in rows}
        items = [
            (by_id[item.product_id], item.quantity)
            for item in selection
            if item.product_id in by_id
        ]
        if not items:
            raise SelectionUnavailableError(store_id=context.store_id, requested=len(selection))
        room = RenderRoom(
            room_label=room_type_words(spec.room_type),
            style=_style_words(spec.style),
            length_m=spec.length_m,
            width_m=spec.width_m,
        )
        return await self._draw(items, room, view, context, RenderSource.CATALOG, spec)

    async def _draw(
        self,
        items: Sequence[tuple[ProductCandidate, int]],
        room: RenderRoom,
        view: RenderView,
        context: RetailerContext,
        source: RenderSource,
        spec: RenderRoomSpec | None,
    ) -> RoomRenderPresentation:
        started = time.perf_counter()
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

        prompt = build_prompt(room, tuple(pieces), view)
        image = await self._generator.generate(prompt, references)
        width, height = _dimensions(image.data)
        key = f"store-{context.store_id}/{datetime.now(UTC):%Y%m%d}/{uuid.uuid4().hex}.jpg"
        url = await self._store.save(key, image.data, image.mime)

        logger.info(
            "room_rendered",
            store_id=context.store_id,
            source=source.value,
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
            source=source,
            room=spec,
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


class CatalogVisualizationRuntime:
    """A room rendered from catalogue picks, run as one committed turn.

    Like a package render it is recorded in the history in words and changes
    no state: the selection is the customer's, held on their screen, and does
    not become the room package.
    """

    def __init__(
        self,
        visualizer: RoomVisualizer,
        sessions: SessionStore,
        session_settings: SessionSettings,
        catalog_settings: CatalogSettings,
        attributes: CatalogAttributes,
    ) -> None:
        self._visualizer = visualizer
        self._sessions = sessions
        self._session_settings = session_settings
        self._catalog = catalog_settings
        self._attributes = attributes

    async def visualize(
        self, request: CatalogVisualizeRequest, context: RetailerContext
    ) -> ChatResponse:
        """Check, load, render, commit, answer.

        Everything the customer set is checked before the session is loaded,
        and the revision before an image model is paid.
        """
        spec = self._checked_room(request.room)
        self._check_items(request.items)
        loaded = await load_for_turn(
            self._sessions,
            store_id=request.store_id,
            session_id=request.session_id,
            expected_revision=request.expected_session_revision,
        )
        render = await self._visualizer.render_selection(request.items, spec, request.view, context)

        room_words = f"{_style_words(spec.style)} {room_type_label(spec.room_type).lower()}"
        dropped = len(request.items) - len(render.items)
        message = f"Here's your {room_words}, {_VIEW_PHRASES[request.view]}."
        if dropped == 1:
            message += " 1 piece you picked is no longer available, so I left it out."
        elif dropped:
            message += f" {dropped} pieces you picked are no longer available, so I left them out."
        response = CustomerResponse(message=message)
        units = sum(item.quantity for item in render.items)
        revision = await commit_exchange(
            self._sessions,
            self._session_settings,
            store_id=request.store_id,
            session_id=request.session_id,
            loaded=loaded,
            state=loaded.envelope.state,
            customer_said=(
                f"[Visualised {units} pieces picked from the catalogue in a "
                f"{spec.length_m:g} x {spec.width_m:g} m {room_words}, "
                f"{render.view_label.lower()} view]"
            ),
            response=response,
        )
        return ChatResponse(
            session_id=request.session_id,
            session_revision=revision,
            response=response,
            presentation=ChatPresentation(render=render),
        )

    def _checked_room(self, room: RenderRoomSpec) -> RenderRoomSpec:
        """The room with its style in approved spelling, or a refusal the
        customer can act on. The style is written into the prompt, so only an
        approved value may reach it."""
        low, high = self._catalog.min_room_side_m, self._catalog.max_room_side_m
        for side in (room.length_m, room.width_m):
            if not low <= side <= high:
                raise SelectionRejectedError(
                    public_message=f"Room sides must be between {low:g} and {high:g} m."
                )
        style = self._attributes.canonical(AttributeFamily.STYLE, room.style)
        if style is None:
            raise SelectionRejectedError(public_message="Please choose a style from the list.")
        return room.model_copy(update={"style": style})

    def _check_items(self, items: Sequence[CatalogSelectionItem]) -> None:
        if len(items) > self._catalog.max_products:
            raise SelectionRejectedError(
                public_message=f"Pick at most {self._catalog.max_products} products to visualise."
            )
        if any(item.quantity > self._catalog.max_quantity for item in items):
            raise SelectionRejectedError(
                public_message=f"At most {self._catalog.max_quantity} of any one product."
            )


# ── helpers ─────────────────────────────────────────────────────────────────


def _style_words(style: str) -> str:
    """An approved style token as prompt and reply words: underscores become
    spaces and the case is lowered, so a two-word style reads as two words."""
    return style.replace("_", " ").lower()


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
