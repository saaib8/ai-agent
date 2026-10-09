"""Ticking and unticking products: the customer's picks, changed silently.

A tick is not a conversation turn. Nothing is said and nothing is answered, so
the conversation is left exactly as it was - but the session changes, because
the agent has to know what the customer picked: "compare the two I picked" and
"what goes with it?" read the picks next turn (CLAUDE.md 19).

The first pick of its kind is the one exception, and even then the tick itself
stays silent: the response says which pick has companions worth showing, and
the client asks for them as a turn of its own. A second sofa picked to compare
with the first is simply saved.

The update is the one a typed "I'll take the second one" makes, from the same
shared function, so typing and tapping cannot record different things
(CLAUDE.md 17.1). A card is named by its position in the results on screen and
a pick by its position in the picks; both resolve against verified state, and
no product id ever arrives from the client (CLAUDE.md 20.2).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.config import CustomerAgentSettings
from app.core.exceptions import (
    PickLimitError,
    PickUnavailableError,
)
from app.core.logging import get_logger
from app.repositories.sessions import SessionStore
from app.schemas.agent_decision import ProductInteractionOp
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_updates import (
    AddItems,
    AgentStateUpdate,
    ProductInteractionUpdate,
    RemoveItems,
    ReplaceItems,
)
from app.schemas.picks import (
    DeselectPickAction,
    LikeCardAction,
    PicksRequest,
    PicksResponse,
    SelectLikedAction,
    SelectPickAction,
    UnlikeAction,
)
from app.schemas.product import ProductCandidate
from app.schemas.resolution import (
    ReferenceFailureReason,
    ReferenceUnresolved,
    ResolvedProductReference,
)
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update
from app.services.chat_runtime import commit_state, load_for_turn
from app.services.hydration import ProductHydrationService
from app.services.product_interaction import build_liked, build_picks, interaction_update
from app.services.reference_resolver import ProductReferenceResolver

logger = get_logger(__name__)

_GONE = "That product is no longer available, so it can't be picked."
_GONE_LIKE = "That product is no longer available, so it can't be liked."
_OFF_SCREEN_LIKE = "That card isn't on screen any more, so it can't be liked from here."


def _liked_card(outcome: ResolvedProductReference | ReferenceUnresolved) -> int:
    """The product a ♡ names, or the reason it names none, in our words."""
    if isinstance(outcome, ReferenceUnresolved):
        raise PickUnavailableError(
            public_message=(
                _GONE_LIKE
                if outcome.reason is ReferenceFailureReason.PRODUCT_UNAVAILABLE
                else _OFF_SCREEN_LIKE
            ),
            reason=str(outcome.reason),
        )
    return outcome.product_id


class PicksRuntime:
    """One tick or untick, applied and persisted, with the picks as they stand."""

    def __init__(
        self,
        references: ProductReferenceResolver,
        hydration: ProductHydrationService,
        sessions: SessionStore,
        settings: CustomerAgentSettings,
    ) -> None:
        self._references = references
        self._hydration = hydration
        self._sessions = sessions
        self._max_picks = settings.max_picks
        self._max_likes = settings.max_likes
        self._buttons = settings.designer_led_buttons

    async def apply(self, request: PicksRequest, context: RetailerContext) -> PicksResponse:
        """Load, change, commit, report.

        The revision is checked before anything changes, exactly as for a
        message: a tick on a list that has since been replaced would pick a
        product the customer never saw. A tick on a card already picked, or an
        untick that changes nothing, commits nothing.
        """
        loaded = await load_for_turn(
            self._sessions,
            store_id=request.store_id,
            session_id=request.session_id,
            expected_revision=request.expected_session_revision,
        )
        state = loaded.envelope.state
        updated = await self._changed(request, state, context)

        revision = loaded.envelope.session_revision
        if updated is not None:
            revision = await commit_state(
                self._sessions,
                store_id=request.store_id,
                session_id=request.session_id,
                loaded=loaded,
                state=updated,
            )
        final = updated or state
        picked = final.product_interaction.selected_product_ids
        liked = final.product_interaction.liked_product_ids if self._buttons else ()
        wanted = tuple(dict.fromkeys((*picked, *liked)))
        products = await self._hydration.hydrate_ids(wanted, context) if wanted else []
        goes_with = (
            self._goes_with(picked, products)
            if updated is not None
            and isinstance(request.action, SelectPickAction | SelectLikedAction)
            else None
        )
        logger.info(
            "picks_changed",
            store_id=context.store_id,
            action=request.action.kind,
            changed=updated is not None,
            pick_count=len(picked),
            goes_with=goes_with is not None,
        )
        return PicksResponse(
            session_id=request.session_id,
            session_revision=revision,
            picks=build_picks(final, products),
            liked=build_liked(final, products) if self._buttons else None,
            goes_with=goes_with,
        )

    @staticmethod
    def _goes_with(picked: tuple[int, ...], products: Sequence[ProductCandidate]) -> int | None:
        """The new pick's number, for the client to open it as a turn: every
        pick - a second sofa as much as the first, and a kind nothing is paired
        with - comes into the conversation (CLAUDE.md 10.4). Which companions,
        if any, are offered is that turn's business.

        None when the new pick cannot be read back - it left the catalog
        between the tick and now - so the client is never sent to a pick that
        cannot be shown.
        """
        if not picked or picked[-1] not in {product.product_id for product in products}:
            return None
        return len(picked)

    async def _changed(
        self, request: PicksRequest, state: AgentStateV1, context: RetailerContext
    ) -> AgentStateV1 | None:
        """The state after the action, or None when it changes nothing."""
        picked = state.product_interaction.selected_product_ids
        if isinstance(request.action, LikeCardAction | UnlikeAction | SelectLikedAction):
            if not self._buttons:
                raise PickUnavailableError(
                    public_message="Likes aren't available here.", reason="likes_switched_off"
                )
            return await self._liked_changed(request.action, state, context)
        match request.action:
            case SelectPickAction(ordinal=ordinal, list_revision=list_revision):
                outcome = await self._references.resolve_on_list(
                    ordinal, list_revision, state, context
                )
                if isinstance(outcome, ReferenceUnresolved):
                    raise PickUnavailableError(
                        public_message=(
                            _GONE
                            if outcome.reason is ReferenceFailureReason.PRODUCT_UNAVAILABLE
                            else None
                        ),
                        reason=str(outcome.reason),
                    )
                if outcome.product_id in picked:
                    return None
                if len(picked) >= self._max_picks:
                    raise PickLimitError(
                        public_message=(
                            f"You can keep up to {self._max_picks} picks. "
                            "Remove one to add another."
                        ),
                    )
                product_id = outcome.product_id
                op = ProductInteractionOp.SELECT
            case DeselectPickAction(pick=pick):
                if pick > len(picked):
                    raise PickUnavailableError(
                        public_message="That pick is no longer in your picks.",
                        reason="pick_out_of_range",
                    )
                # Removing needs no catalog read: a pick that has left the
                # catalog is exactly the kind a customer wants to clear.
                product_id = picked[pick - 1]
                op = ProductInteractionOp.DESELECT
        return apply_update(
            state,
            AgentStateUpdate(product_interaction=interaction_update(op, product_id, state)),
        )

    async def _liked_changed(
        self,
        action: LikeCardAction | UnlikeAction | SelectLikedAction,
        state: AgentStateV1,
        context: RetailerContext,
    ) -> AgentStateV1 | None:
        """A like, an unlike, or a pick from the liked list.

        A like is silent and never refused for being one too many: past the
        limit the oldest like is let go, since a like is a taste signal, not a
        shortlist. Selecting from the liked list is a pick like any other.
        """
        liked = state.product_interaction.liked_product_ids
        match action:
            case LikeCardAction(ordinal=ordinal, list_revision=list_revision):
                outcome = await self._references.resolve_on_list(
                    ordinal, list_revision, state, context
                )
                product_id = _liked_card(outcome)
                if product_id in liked:
                    return None
                change: AddItems[int] | RemoveItems[int] | ReplaceItems[int] = (
                    AddItems(items=(product_id,))
                    if len(liked) < self._max_likes
                    else ReplaceItems(
                        items=(*liked[len(liked) - self._max_likes + 1 :], product_id)
                    )
                )
            case UnlikeAction(liked=int(position)):
                if position > len(liked):
                    raise PickUnavailableError(
                        public_message="That is no longer in your liked list.",
                        reason="liked_out_of_range",
                    )
                change = RemoveItems(items=(liked[position - 1],))
            case UnlikeAction(ordinal=int(ordinal), list_revision=list_revision):
                product_id = _liked_card(
                    await self._references.resolve_on_list(ordinal, list_revision, state, context)
                )
                if product_id not in liked:
                    return None
                change = RemoveItems(items=(product_id,))
            case SelectLikedAction(liked=position):
                picked = state.product_interaction.selected_product_ids
                if position > len(liked):
                    raise PickUnavailableError(
                        public_message="That is no longer in your liked list.",
                        reason="liked_out_of_range",
                    )
                product_id = liked[position - 1]
                if product_id in picked:
                    return None
                if len(picked) >= self._max_picks:
                    raise PickLimitError(
                        public_message=(
                            f"You can keep up to {self._max_picks} picks. "
                            "Remove one to add another."
                        ),
                    )
                if not await self._hydration.hydrate_ids((product_id,), context):
                    raise PickUnavailableError(public_message=_GONE, reason="product_unavailable")
                return apply_update(
                    state,
                    AgentStateUpdate(
                        product_interaction=interaction_update(
                            ProductInteractionOp.SELECT, product_id, state
                        )
                    ),
                )
        return apply_update(
            state,
            AgentStateUpdate(
                product_interaction=ProductInteractionUpdate(liked_product_ids=change)
            ),
        )
