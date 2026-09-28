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
    IntegrationUnavailableError,
    PickLimitError,
    PickUnavailableError,
)
from app.core.logging import get_logger
from app.repositories.sessions import SessionStore
from app.schemas.agent_decision import ProductInteractionOp
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_updates import AgentStateUpdate
from app.schemas.picks import (
    DeselectPickAction,
    PicksRequest,
    PicksResponse,
    SelectPickAction,
)
from app.schemas.product import ProductCandidate
from app.schemas.resolution import ReferenceFailureReason, ReferenceUnresolved
from app.schemas.retailer import RetailerContext
from app.services.agent_state import apply_update
from app.services.catalog_capability import CatalogCapabilityService
from app.services.chat_runtime import commit_state, load_for_turn
from app.services.hydration import ProductHydrationService
from app.services.product_interaction import build_picks, interaction_update
from app.services.reference_resolver import ProductReferenceResolver
from app.taxonomy.complements import Complements

logger = get_logger(__name__)

_GONE = "That product is no longer available, so it can't be picked."


class PicksRuntime:
    """One tick or untick, applied and persisted, with the picks as they stand."""

    def __init__(
        self,
        references: ProductReferenceResolver,
        hydration: ProductHydrationService,
        sessions: SessionStore,
        settings: CustomerAgentSettings,
        *,
        complements: Complements | None = None,
        capabilities: CatalogCapabilityService | None = None,
    ) -> None:
        self._references = references
        self._hydration = hydration
        self._sessions = sessions
        self._max_picks = settings.max_picks
        self._complements = complements
        """The reviewed pairings, to know whether a new pick has companions.
        None where ticks never lead to a cross-sell."""
        self._capabilities = capabilities

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
        products = await self._hydration.hydrate_ids(picked, context) if picked else []
        goes_with = (
            await self._goes_with(picked, products, context)
            if updated is not None and isinstance(request.action, SelectPickAction)
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
            goes_with=goes_with,
        )

    async def _goes_with(
        self,
        picked: tuple[int, ...],
        products: Sequence[ProductCandidate],
        context: RetailerContext,
    ) -> int | None:
        """The new pick's number, when it is the first of its kind and the
        store sells something that goes with it.

        "Its kind" is what goes with it rather than its exact type: a
        sectional picked after a sofa has the same companions, so it is a
        second option being weighed, not a new piece of the room. Companion
        types already picked do not count - they chose one already.
        """
        if self._complements is None or self._capabilities is None or not picked:
            return None
        newest = picked[-1]
        by_id = {product.product_id: product for product in products}
        product = by_id.get(newest)
        if product is None:
            return None
        others = [p.commerce.subcategory for p in products if p.product_id != newest]
        companions = self._complements.for_type(product.commerce.subcategory)
        if any(self._complements.for_type(other) == companions for other in others if other):
            return None
        wanted = [c for c in companions if c.commerce_subcategory not in others]
        if not wanted:
            return None
        try:
            capabilities = await self._capabilities.capabilities(context)
        except IntegrationUnavailableError:
            logger.warning("picks_capabilities_unavailable", store_id=context.store_id)
            return None
        if not any(
            capabilities.supports(c.commerce_category, c.commerce_subcategory) for c in wanted
        ):
            return None
        return len(picked)

    async def _changed(
        self, request: PicksRequest, state: AgentStateV1, context: RetailerContext
    ) -> AgentStateV1 | None:
        """The state after the action, or None when it changes nothing."""
        picked = state.product_interaction.selected_product_ids
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
