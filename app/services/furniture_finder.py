"""Furniture Finder: find catalog products like something in a photo.

A detector outlines the objects in the photo. When the customer picks one, a
vision model describes it in words, the description is embedded into the same
space as the product index's text documents, the index proposes neighbours in
the detected category, and PostgreSQL decides which of them this retailer
actually sells (CLAUDE.md 16).

The vision model is the only model consulted, and only as an interpreter of
pixels: its words are a query, never a fact the customer is shown, and it
cannot change the category the detector assigned (CLAUDE.md 3.3).

A pick is a **turn**, not a side channel. Its results become the list on
screen, committed to the session exactly like a search's, so the next message
- "compare the first two", "tell me about the third", "keep that one" -
resolves against what the customer is looking at. That is what separates a
finder inside the conversation from a gallery next to it.

The active search a pick leaves behind is the one "something similar to the
closest match" would start: its reviewed category, seat count and look,
built by the same `SimilarSearchBuilder` the chat uses. So "anything cheaper?"
refines a real, structured search rather than an image nobody can filter.
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Sequence

from app.core.config import FurnitureFinderSettings, SessionSettings
from app.core.exceptions import (
    EmbeddingUnavailableError,
    FinderImageNotFoundError,
    InvalidRequestError,
    VisualSearchUnavailableError,
)
from app.core.logging import get_logger
from app.integrations.detection import ObjectDetector, RawDetection
from app.integrations.embeddings import QueryEmbedder
from app.integrations.finder_index import FinderIndex, IndexMatch, category_key
from app.repositories.finder_photos import FinderPhotoStore
from app.repositories.products import ProductRepository
from app.repositories.sessions import SessionStore
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AgentStateUpdate,
    ClearSemanticIntent,
    ReplaceItems,
    SetSemanticIntent,
)
from app.schemas.chat import ChatPresentation, ChatResponse
from app.schemas.furniture_finder import (
    DetectedObject,
    FinderPhoto,
    FinderPhotoResponse,
    FinderPickRequest,
    ImageBox,
    ObjectDescription,
    view_of,
)
from app.schemas.product import ProductCandidate
from app.schemas.resolution import SimilarSearchSeed
from app.schemas.retailer import RetailerContext
from app.services.agent_state import (
    NO_RESULTS_REVISION,
    apply_update,
    commit_search_results,
)
from app.services.chat_runtime import LoadedSession, commit_exchange, load_for_turn
from app.services.finder_imaging import (
    bounded_box,
    clamp_polygon,
    object_views,
    prepare_upload,
)
from app.services.grounding_builder import to_grounded_product
from app.services.hydration import ProductHydrationService
from app.services.object_description import ObjectDescriber
from app.services.refinement_composer import SearchRefinementComposer
from app.services.similar_search import SimilarSearchBuilder

logger = get_logger(__name__)


class FurnitureFinderService:
    """Photo in, pickable objects out; object in, matching products out."""

    def __init__(
        self,
        detector: ObjectDetector,
        describer: ObjectDescriber,
        embedder: QueryEmbedder,
        index: FinderIndex,
        photos: FinderPhotoStore,
        repository: ProductRepository,
        hydration: ProductHydrationService,
        settings: FurnitureFinderSettings,
    ) -> None:
        self._detector = detector
        self._describer = describer
        self._embedder = embedder
        self._index = index
        self._photos = photos
        self._repository = repository
        self._hydration = hydration
        self._settings = settings

    @property
    def max_upload_bytes(self) -> int:
        return self._settings.max_upload_bytes

    # ── upload ──────────────────────────────────────────────────────────────

    async def upload(
        self, data: bytes, *, session_id: str, context: RetailerContext
    ) -> FinderPhotoResponse:
        """Detect what is in a photo and keep it for the pick that follows.

        The photo is prepared before anything is paid for, so an unusable
        upload is refused without a detector call.
        """
        started = time.perf_counter()
        settings = self._settings
        prepared = prepare_upload(
            data,
            max_bytes=settings.max_upload_bytes,
            min_side=settings.min_image_side,
            max_side=settings.detection_max_side,
        )
        raw = await self._detector.detect(prepared.jpeg)
        matchable = await self._repository.visual_categories(context)

        objects = _pickable(raw, matchable, prepared.width, prepared.height)
        photo = FinderPhoto(
            image_id=uuid.uuid4().hex,
            width=prepared.width,
            height=prepared.height,
            jpeg_b64=base64.b64encode(prepared.jpeg).decode("ascii"),
            objects=objects,
        )
        await self._photos.save(context.store_id, session_id, photo)
        logger.info(
            "finder_photo_detected",
            store_id=context.store_id,
            detected_count=len(raw),
            pickable_count=len(objects),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return FinderPhotoResponse(
            image_id=photo.image_id,
            width=photo.width,
            height=photo.height,
            objects=tuple(view_of(o) for o in objects),
            unmatched_count=len(raw) - len(objects),
        )

    # ── pick ────────────────────────────────────────────────────────────────

    async def load_photo(
        self, session_id: str, image_id: str, context: RetailerContext
    ) -> FinderPhoto:
        photo = await self._photos.load(context.store_id, session_id, image_id)
        if photo is None:
            raise FinderImageNotFoundError(store_id=context.store_id)
        return photo

    async def find(
        self, photo: FinderPhoto, detected: DetectedObject, context: RetailerContext
    ) -> tuple[tuple[ProductCandidate, ...], ObjectDescription]:
        """Products this store sells like the object, best first, and what it looked like.

        The index proposes; PostgreSQL disposes. A neighbour whose product is
        inactive, deleted or another retailer's is dropped, never shown from
        what the index remembers, and a product indexed twice appears once, at
        its best position.
        """
        tight, medium = object_views(
            base64.b64decode(photo.jpeg_b64),
            box=detected.box,
            polygon=detected.polygon,
            padding=self._settings.medium_crop_padding,
        )
        description = await self._describer.describe(
            category_words=detected.display_label, views=(tight, medium)
        )
        try:
            vector = await self._embedder.embed_query(
                description.query_text(detected.display_label)
            )
        except EmbeddingUnavailableError as exc:
            # No degraded answer exists: without the vector there is nothing
            # to search with, so the customer is told to try again.
            raise VisualSearchUnavailableError(reason="embedding_unavailable") from exc
        matches = await self._index.nearest(
            vector=vector,
            store_id=context.store_id,
            category=category_key(detected.label),
            top_k=self._settings.candidate_limit,
        )
        by_vector, by_url = await self._repository.ids_for_visual_matches(
            [m.vector_id for m in matches],
            [m.product_url for m in matches if m.product_url],
            context,
        )
        ordered = _resolved_order(matches, by_vector, by_url)
        products = await self._hydration.hydrate_ids(
            ordered[: self._settings.result_limit], context
        )
        logger.info(
            "finder_matches_resolved",
            store_id=context.store_id,
            neighbour_count=len(matches),
            resolved_count=len(ordered),
            presented_count=len(products),
        )
        return products, description


class FinderTurnRuntime:
    """A pick in a photo, run as one committed conversation turn."""

    def __init__(
        self,
        finder: FurnitureFinderService,
        sessions: SessionStore,
        settings: SessionSettings,
        similar: SimilarSearchBuilder,
        composer: SearchRefinementComposer,
    ) -> None:
        self._finder = finder
        self._sessions = sessions
        self._settings = settings
        self._similar = similar
        self._composer = composer

    async def pick(self, request: FinderPickRequest, context: RetailerContext) -> ChatResponse:
        """Load, find, commit, answer - in the order the chat runtime uses.

        A stale screen is refused before the embedding model or the index is
        paid for, and nothing is returned that could not be persisted: a reply
        describing a session that someone else has since moved on would put
        the wrong list behind "the second one" (M13 18, 33).
        """
        started = time.perf_counter()
        loaded = await load_for_turn(
            self._sessions,
            store_id=request.store_id,
            session_id=request.session_id,
            expected_revision=request.expected_session_revision,
        )
        photo = await self._finder.load_photo(request.session_id, request.image_id, context)
        detected = photo.object(request.object_id)
        if detected is None:
            raise InvalidRequestError(
                public_message="That item is not in this photo. Please pick another one.",
                object_id=request.object_id,
            )

        products, description = await self._finder.find(photo, detected, context)
        state = self._committed_state(loaded.envelope.state, products)
        on_screen = state is not None
        response = _response(detected, len(products))
        presentation = ChatPresentation(
            products=tuple(
                to_grounded_product(
                    product,
                    grounding_ref=position,
                    presented_ordinal=position if on_screen else None,
                )
                for position, product in enumerate(products, start=1)
            )
        )

        revision = await self._persist(
            request, loaded, state or loaded.envelope.state, detected, description, response
        )
        logger.info(
            "finder_pick_completed",
            store_id=request.store_id,
            revision_before=loaded.loaded_revision,
            revision_after=revision,
            product_count=len(products),
            committed_as_results=on_screen,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return ChatResponse(
            session_id=request.session_id,
            session_revision=revision,
            response=response,
            presentation=None if presentation.is_empty() else presentation,
        )

    def _committed_state(
        self, state: AgentStateV1, products: Sequence[ProductCandidate]
    ) -> AgentStateV1 | None:
        """The session state with these products as the list on screen.

        A presented list must belong to an active search - that is what lets
        the resolver trust "the second one" (M10). So the search criteria are
        seeded from the closest match whose reviewed classification can seed
        one, through the same builder "something similar to this" uses.

        None when there is nothing to present, or when no match carries a
        usable classification. The customer still sees the cards; they just
        cannot be addressed by position, which is the honest outcome when no
        search could honestly be recorded behind them.
        """
        if not products:
            return None
        seed = next(
            (
                outcome
                for outcome in (self._similar.build(p) for p in products)
                if isinstance(outcome, SimilarSearchSeed)
            ),
            None,
        )
        if seed is None:
            logger.warning("finder_results_unseedable", product_count=len(products))
            return None

        # The builder excludes its reference product, which is right for
        # "others like this one" and wrong here: the closest match is on
        # screen, and the customer has turned nothing down.
        resolved = seed.resolved.model_copy(
            update={"request": seed.resolved.request.model_copy(update={"exclude_product_ids": ()})}
        )
        composed = self._composer.seed_new_task(
            resolved,
            room_preferences=(state.room_project.design_preferences if state.room_project else ()),
            customer_defaults=state.customer_preferences.semantic_preferences,
            revision=state.active_search.revision if state.active_search else NO_RESULTS_REVISION,
        )
        intent = composed.candidate.semantic_intent
        promoted = apply_update(
            state,
            AgentStateUpdate(
                active_search=ActiveSearchUpdate(
                    request=composed.candidate.request,
                    semantics=composed.candidate.semantics,
                    semantic_preferences=ReplaceItems(items=composed.candidate.semantic_preferences),
                    semantic_intent=(
                        ClearSemanticIntent() if intent is None else SetSemanticIntent(value=intent)
                    ),
                )
            ),
        )
        return commit_search_results(promoted, tuple(p.product_id for p in products))

    async def _persist(
        self,
        request: FinderPickRequest,
        loaded: LoadedSession,
        state: AgentStateV1,
        detected: DetectedObject,
        description: ObjectDescription,
        response: CustomerResponse,
    ) -> int:
        """Commit the pick as an exchange, or refuse because someone else did.

        What the customer did is recorded in words, with what the item looked
        like, so "the same but in grey" has something to be the same as.
        """
        return await commit_exchange(
            self._sessions,
            self._settings,
            store_id=request.store_id,
            session_id=request.session_id,
            loaded=loaded,
            state=state,
            customer_said=_pick_utterance(detected, description),
            response=response,
        )


# ── helpers ─────────────────────────────────────────────────────────────────


def _pickable(
    raw: Sequence[RawDetection], matchable: frozenset[str], width: int, height: int
) -> tuple[DetectedObject, ...]:
    """Detections this store can match, inside the photo, numbered from one.

    Larger objects first: the sofa is what people came for, and the order is
    the order a client lists them in.
    """
    kept: list[tuple[int, RawDetection, ImageBox]] = []
    for detection in raw:
        if category_key(detection.label) not in matchable:
            continue
        box = bounded_box(*detection.box, width, height)
        if box is None:
            continue
        kept.append(((box.x2 - box.x1) * (box.y2 - box.y1), detection, box))
    kept.sort(key=lambda item: item[0], reverse=True)
    return tuple(
        DetectedObject(
            object_id=position,
            label=detection.label,
            confidence=round(detection.confidence, 3),
            box=box,
            polygon=clamp_polygon(detection.polygon, width, height),
        )
        for position, (_, detection, box) in enumerate(kept, start=1)
    )


def _resolved_order(
    matches: Sequence[IndexMatch], by_vector: dict[str, int], by_url: dict[str, int]
) -> list[int]:
    """Product ids in neighbour order, each once, at its best position."""
    ordered: list[int] = []
    seen: set[int] = set()
    for match in matches:
        product_id = by_vector.get(match.vector_id)
        if product_id is None and match.product_url:
            product_id = by_url.get(match.product_url)
        if product_id is None or product_id in seen:
            continue
        seen.add(product_id)
        ordered.append(product_id)
    return ordered


def _pick_utterance(detected: DetectedObject, description: ObjectDescription) -> str:
    label = detected.display_label
    return (
        f"[Shared a photo and picked the {label} in it: {description.summary.rstrip('. ')}] "
        f"Show me products like this {label}."
    )


def _response(detected: DetectedObject, count: int) -> CustomerResponse:
    """Fixed wording. Nothing here is a fact a model could get wrong."""
    label = detected.display_label
    if count == 0:
        return CustomerResponse(
            message=(
                f"I couldn't find anything in this catalog that looks like the {label} "
                "in your photo."
            ),
            follow_up_question="Would you like me to search for one by description instead?",
        )
    lead = (
        f"Here is the closest match to the {label} in your photo."
        if count == 1
        else f"Here are the {count} closest matches to the {label} in your photo."
    )
    return CustomerResponse(
        message=lead,
        follow_up_question="Would you like to compare any of these, or hear more about one?",
    )
