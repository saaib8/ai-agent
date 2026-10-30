"""Furniture Finder: find catalog products like something in a photo.

The main platform's own finder does the looking (CLAUDE.md 16). The photo is
uploaded to it as the shopper, its detector outlines what is in the photo, and
when the customer picks a piece it compares that piece's pixels with every
product photo the store has. PostgreSQL then decides which of the proposals
this retailer actually sells, in the order the platform ranked them. No model
of ours is consulted.

The shopper is the widget session the gateway forwarded - a member or an
anonymous visitor - so the platform scopes and records the scan as theirs. A
local run with no forwarded session signs in an anonymous visitor of its own,
one per session; anywhere else, no session means no finder.

What is offered on a photo follows the platform's own finder: outlines it is
less sure of are dropped, an outline drawn twice for one piece is kept once,
the pieces people shop for come first (confidence weighted by size), and only
so many are offered. Pieces this store sells nothing like are left out too.

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
import hashlib
import json
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.agents.facts import product_facts
from app.core.config import FurnitureFinderSettings, SessionSettings
from app.core.exceptions import (
    DetectionUnavailableError,
    FinderAccessRefusedError,
    FinderImageNotFoundError,
    InvalidRequestError,
    VisualSearchUnavailableError,
    ZoryError,
    ZoryTokenRejectedError,
)
from app.core.logging import get_logger
from app.integrations.zory_finder import (
    RawDetection,
    ZoryFinderClient,
    ZoryMatch,
    ZoryScan,
    category_key,
)
from app.repositories.finder_photos import FinderPhotoStore
from app.repositories.products import ProductRepository
from app.repositories.sessions import SessionStore
from app.repositories.stores import StoreRepository
from app.schemas.agent_persona import AgentPersona
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
    ShopperCredentials,
    view_of,
)
from app.schemas.language import ReplyLanguage
from app.schemas.product import ProductCandidate
from app.schemas.reply_choice import ReplyChoice
from app.schemas.resolution import SimilarSearchSeed
from app.schemas.retailer import RetailerContext
from app.services.agent_state import (
    NO_RESULTS_REVISION,
    apply_update,
    commit_search_results,
)
from app.services.card_labels import card_labels
from app.services.finder_imaging import bounded_box, clamp_polygon, prepare_upload
from app.services.grounding_builder import to_grounded_product
from app.services.hydration import ProductHydrationService
from app.services.media_wording import photo_reply
from app.services.product_display import ProductDisplayWords, displayed
from app.services.refinement_composer import SearchRefinementComposer
from app.services.reply_language import session_language
from app.services.session_turns import LoadedSession, commit_exchange, load_for_turn
from app.services.similar_search import SimilarSearchBuilder
from app.taxonomy.compare_groups import CompareGroups

if TYPE_CHECKING:
    from app.agents.writer import ReplyWriter

logger = get_logger(__name__)

_VISITOR_TOKEN_FALLBACK_TTL_S = 600
"""How long a visitor token is kept when its expiry cannot be read."""
_VISITOR_TOKEN_MARGIN_S = 60


class FurnitureFinderService:
    """Photo in, pickable objects out; object in, matching products out."""

    def __init__(
        self,
        zory: ZoryFinderClient,
        photos: FinderPhotoStore,
        repository: ProductRepository,
        hydration: ProductHydrationService,
        stores: StoreRepository,
        sessions: SessionStore,
        settings: FurnitureFinderSettings,
        *,
        local_visitors: bool = False,
        arabic_replies: bool = False,
    ) -> None:
        self._zory = zory
        self._photos = photos
        self._repository = repository
        self._hydration = hydration
        self._stores = stores
        self._sessions = sessions
        self._settings = settings
        self._local_visitors = local_visitors and settings.local_widget_key is not None
        """Only a local run may act as a visitor nobody forwarded."""
        self._arabic_replies = arabic_replies

    @property
    def max_upload_bytes(self) -> int:
        return self._settings.max_upload_bytes

    # ── upload ──────────────────────────────────────────────────────────────

    async def upload(
        self,
        data: bytes,
        *,
        session_id: str,
        context: RetailerContext,
        shopper: ShopperCredentials | None = None,
    ) -> FinderPhotoResponse:
        """Detect what is in a photo and keep it for the pick that follows.

        The photo is prepared before anything is asked of the platform, so an
        unusable upload is refused without a scan.
        """
        started = time.perf_counter()
        settings = self._settings
        prepared = prepare_upload(
            data,
            max_bytes=settings.max_upload_bytes,
            min_side=settings.min_image_side,
            max_side=settings.detection_max_side,
            upscale_short_side_to=settings.min_short_side,
        )
        try:
            scan = await self._scan(prepared.jpeg, session_id, context, shopper)
        except (FinderAccessRefusedError, DetectionUnavailableError) as exc:
            raise _worded(exc, await self._language(context, session_id)) from exc
        width, height = scan.size or (prepared.width, prepared.height)
        matchable = await self._repository.visual_categories(context)
        objects, unmatched = _pickable(scan.detections, matchable, width, height, settings)
        photo = FinderPhoto(
            image_id=uuid.uuid4().hex,
            width=width,
            height=height,
            scan_id=scan.scan_id,
            stored_image=scan.stored_image,
            objects=objects,
        )
        await self._photos.save(context.store_id, session_id, photo)
        logger.info(
            "finder_photo_detected",
            store_id=context.store_id,
            scan_id=scan.scan_id,
            detected_count=len(scan.detections),
            pickable_count=len(objects),
            unmatched_count=unmatched,
            forwarded_shopper=shopper is not None,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return FinderPhotoResponse(
            image_id=photo.image_id,
            width=photo.width,
            height=photo.height,
            objects=tuple(view_of(o) for o in objects),
            unmatched_count=unmatched,
        )

    async def _scan(
        self,
        jpeg: bytes,
        session_id: str,
        context: RetailerContext,
        shopper: ShopperCredentials | None,
    ) -> ZoryScan:
        if shopper is not None:
            # The shopper's own session: never retried as anyone else.
            return await self._zory.scan(shopper, jpeg)
        visitor = await self._visitor(context, session_id)
        try:
            return await self._zory.scan(visitor, jpeg)
        except ZoryTokenRejectedError:
            # Our own visitor's token expired: sign in again, once.
            await self._photos.forget_visitor(context.store_id, session_id)
            visitor = await self._visitor(context, session_id)
            return await self._zory.scan(visitor, jpeg)

    async def _visitor(self, context: RetailerContext, session_id: str) -> ShopperCredentials:
        """The anonymous visitor a local run acts as for this session.

        Refused anywhere but a local run: a deployed finder acts only as the
        shopper the gateway forwarded, never as someone of its own making.
        """
        if not self._local_visitors or self._settings.local_widget_key is None:
            raise FinderAccessRefusedError(reason="no_shopper_session")
        kept = await self._photos.load_visitor(context.store_id, session_id)
        if kept is not None:
            logger.info("finder_local_visitor_reused", store_id=context.store_id)
            return kept
        store = await self._stores.get_active(context.store_id)
        if store is None:
            raise FinderAccessRefusedError(reason="store_unknown")
        visitor = await self._zory.anonymous_visitor(
            widget_key=self._settings.local_widget_key,
            store_public_id=str(store.uuid),
            anonymous_id=_anonymous_id(context.store_id, session_id),
        )
        await self._photos.save_visitor(
            context.store_id, session_id, visitor, _token_ttl_s(visitor)
        )
        logger.info("finder_local_visitor_signed_in", store_id=context.store_id)
        return visitor

    async def _language(self, context: RetailerContext, session_id: str) -> ReplyLanguage:
        """The session's reply language, for a refusal worded in it."""
        try:
            envelope = await self._sessions.load(context.store_id, session_id)
        except ZoryError:
            envelope = None
        stored = envelope.state.reply_language if envelope is not None else None
        return session_language(stored, enabled=self._arabic_replies)

    # ── pick ────────────────────────────────────────────────────────────────

    async def load_photo(
        self, session_id: str, image_id: str, context: RetailerContext
    ) -> FinderPhoto:
        photo = await self._photos.load(context.store_id, session_id, image_id)
        if photo is None:
            raise FinderImageNotFoundError(store_id=context.store_id)
        return photo

    async def find(
        self,
        photo: FinderPhoto,
        detected: DetectedObject,
        context: RetailerContext,
        shopper: ShopperCredentials | None = None,
    ) -> tuple[ProductCandidate, ...]:
        """Products this store sells like the piece, best first.

        The platform proposes, from the copy of the photo its detector saw;
        PostgreSQL disposes. A proposal whose product is inactive, deleted or
        another retailer's is dropped, never shown from what the platform's
        index remembers, and a product proposed twice appears once, at its
        best position.
        """
        matches = await self._zory.recommend(
            shopper,
            image_url=f"{self._settings.stored_image_base_url.rstrip('/')}/{photo.stored_image}",
            category=detected.label,
            box=detected.box,
            polygon=detected.polygon,
            size=(photo.width, photo.height),
            top_k=self._settings.candidate_limit,
            store_id=context.store_id,
        )
        by_vector, by_url = await self._repository.ids_for_visual_matches(
            [m.pinecone_id for m in matches],
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
            scan_id=photo.scan_id,
            proposed_count=len(matches),
            resolved_count=len(ordered),
            presented_count=len(products),
        )
        return tuple(products)


class FinderTurnRuntime:
    """A pick in a photo, run as one committed conversation turn."""

    def __init__(
        self,
        finder: FurnitureFinderService,
        sessions: SessionStore,
        settings: SessionSettings,
        similar: SimilarSearchBuilder,
        composer: SearchRefinementComposer,
        *,
        arabic_replies: bool = False,
        compare_groups: CompareGroups | None = None,
        display: ProductDisplayWords | None = None,
        writer: ReplyWriter | None = None,
    ) -> None:
        self._arabic_replies = arabic_replies
        self._display = display
        self._writer = writer
        """Words the reply in an agent's voice; None keeps the fixed words."""
        """Words each match's card text in the reply's language; None leaves
        the cards in English, as before."""
        self._compare_groups = compare_groups
        """Which matches compare with which, so "compare the first two" is only
        offered when they can be. None: never offered across different types."""
        self._finder = finder
        self._sessions = sessions
        self._settings = settings
        self._similar = similar
        self._composer = composer

    def _first_two_compare(self, products: Sequence[ProductCandidate]) -> bool:
        kinds = [product.commerce.subcategory for product in products[:2]]
        if len(kinds) < 2:
            return False
        if self._compare_groups is not None:
            return self._compare_groups.all_comparable(kinds)
        return kinds[0] is not None and kinds[0] == kinds[1]

    async def pick(
        self,
        request: FinderPickRequest,
        context: RetailerContext,
        shopper: ShopperCredentials | None = None,
    ) -> ChatResponse:
        """Load, find, commit, answer - in the order the chat runtime uses.

        A stale screen is refused before the platform is asked anything, and
        nothing is returned that could not be persisted: a reply describing a
        session that someone else has since moved on would put the wrong list
        behind "the second one" (M13 18, 33).
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

        language = session_language(
            loaded.envelope.state.reply_language, enabled=self._arabic_replies
        )
        try:
            products = await self._finder.find(photo, detected, context, shopper)
        except (FinderAccessRefusedError, VisualSearchUnavailableError) as exc:
            raise _worded(exc, language) from exc
        state = self._committed_state(loaded.envelope.state, products)
        on_screen = state is not None
        comparable = self._first_two_compare(products)
        response = photo_reply(
            detected.display_label, len(products), language, comparable=comparable
        )
        if self._writer is not None:
            response = await self._writer.write(
                happened=(
                    f"[The customer picked the {detected.display_label} in their photo; the "
                    "matches the photo search found are now on screen as cards]"
                ),
                facts={
                    "photo_item": detected.display_label,
                    # Numbered only when they are the cards on screen.
                    "matches": [
                        product_facts(p, n if on_screen else None)
                        for n, p in enumerate(products, start=1)
                    ],
                    "first_two_compare": comparable,
                },
                # When the matches could not become the cards on screen, the
                # earlier list is not shown to the agent as if it still were.
                state=state or _without_cards(loaded.envelope.state),
                conversation=loaded.envelope.conversation,
                retailer=context,
                language=language,
                fixed=response,
            )
        presentation = ChatPresentation(
            choices=tuple(ReplyChoice(label=c.label, value=c.value) for c in response.choices),
            products=tuple(
                to_grounded_product(
                    product,
                    grounding_ref=position,
                    presented_ordinal=position if on_screen else None,
                )
                for position, product in enumerate(products, start=1)
            ),
            # Committed as the results on screen, so ticks and "Not this one"
            # count into them; otherwise they are cards with no position.
            product_source="search" if on_screen and products else None,
            list_revision=(
                state.product_interaction.presented_search_revision
                if state is not None and products
                else None
            ),
        )

        revision = await self._persist(
            request, loaded, state or loaded.envelope.state, detected, response
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
            presentation=(
                None
                if presentation.is_empty()
                else displayed(presentation, self._display, language)
            ),
            reply_language=language if self._arabic_replies else None,
            labels=card_labels(language if self._arabic_replies else None),
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
                    semantic_preferences=ReplaceItems(
                        items=composed.candidate.semantic_preferences
                    ),
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
        response: CustomerResponse,
    ) -> int:
        """Commit the pick as an exchange, or refuse because someone else did.

        What the customer did is recorded in words, so the conversation knows
        which piece of the photo the cards on screen are like.
        """
        return await commit_exchange(
            self._sessions,
            self._settings,
            store_id=request.store_id,
            session_id=request.session_id,
            loaded=loaded,
            state=state,
            customer_said=_pick_utterance(detected),
            response=response,
            # Furniture Finder is Sara's (CLAUDE.md 25).
            agent=AgentPersona.SARA,
        )


# ── helpers ─────────────────────────────────────────────────────────────────


def _without_cards(state: AgentStateV1) -> AgentStateV1:
    interaction = state.product_interaction.model_copy(
        update={"presented_product_ids": (), "presented_search_revision": None}
    )
    return state.model_copy(update={"product_interaction": interaction})


def _pickable(
    raw: Sequence[RawDetection],
    matchable: frozenset[str],
    width: int,
    height: int,
    settings: FurnitureFinderSettings,
) -> tuple[tuple[DetectedObject, ...], int]:
    """The pieces offered on a photo, numbered from one, and how many were not.

    The platform finder's own rules, in its order: outlines it is less sure of
    are dropped; an outline overlapping a surer one is the same piece labelled
    twice; what is left is ranked by confidence weighted by size - the sofa,
    not the four cushions on it - and capped. A piece this store sells nothing
    like is counted, never offered: tapping it could only ever find nothing.
    """
    kept: list[tuple[RawDetection, ImageBox]] = []
    for detection in sorted(raw, key=lambda d: d.confidence, reverse=True):
        if detection.confidence < settings.min_confidence:
            continue
        box = bounded_box(*detection.box, width, height)
        if box is None:
            continue
        if any(_overlap(box, other) > settings.duplicate_overlap for _, other in kept):
            continue
        kept.append((detection, box))

    offered = [(d, b) for d, b in kept if category_key(d.label) in matchable]
    unmatched = len(kept) - len(offered)
    area = float(width * height) or 1.0
    offered.sort(key=lambda item: item[0].confidence * (_area(item[1]) / area) ** 0.5, reverse=True)
    return (
        tuple(
            DetectedObject(
                object_id=position,
                label=detection.label,
                confidence=round(detection.confidence, 3),
                box=box,
                polygon=clamp_polygon(detection.polygon, width, height),
            )
            for position, (detection, box) in enumerate(offered[: settings.max_objects], start=1)
        ),
        unmatched,
    )


def _area(box: ImageBox) -> int:
    return (box.x2 - box.x1) * (box.y2 - box.y1)


def _overlap(a: ImageBox, b: ImageBox) -> float:
    """Intersection over union of two boxes."""
    width = min(a.x2, b.x2) - max(a.x1, b.x1)
    height = min(a.y2, b.y2) - max(a.y1, b.y1)
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def _resolved_order(
    matches: Sequence[ZoryMatch], by_vector: dict[str, int], by_url: dict[str, int]
) -> list[int]:
    """Product ids in the platform's order, each once, at its best position."""
    ordered: list[int] = []
    seen: set[int] = set()
    for match in matches:
        product_id = by_vector.get(match.pinecone_id)
        if product_id is None and match.product_url:
            product_id = by_url.get(match.product_url)
        if product_id is None or product_id in seen:
            continue
        seen.add(product_id)
        ordered.append(product_id)
    return ordered


def _anonymous_id(store_id: int, session_id: str) -> str:
    """One stable device id per store and session, revealing neither."""
    digest = hashlib.sha256(f"{store_id}:{session_id}".encode()).hexdigest()
    return f"ai-agent-{digest[:32]}"


def _token_ttl_s(visitor: ShopperCredentials) -> int:
    """How long a visitor token may be kept: until shortly before it expires.

    Read from the token's own claims without verifying them - the platform
    verifies it on every call; this only decides when to sign in again.
    """
    try:
        payload = visitor.token.get_secret_value().split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        remaining = int(claims["exp"]) - int(time.time()) - _VISITOR_TOKEN_MARGIN_S
    except (IndexError, KeyError, TypeError, ValueError):
        return _VISITOR_TOKEN_FALLBACK_TTL_S
    return max(1, remaining)


def _worded(exc: ZoryError, language: ReplyLanguage) -> ZoryError:
    """The same refusal, in the words of the customer's language."""
    arabic = getattr(exc, "public_message_ar", None)
    if language is ReplyLanguage.AR and isinstance(arabic, str):
        exc.public_message = arabic
    return exc


def _pick_utterance(detected: DetectedObject) -> str:
    label = detected.display_label
    return f"[Shared a photo and picked the {label} in it] Show me products like this {label}."
