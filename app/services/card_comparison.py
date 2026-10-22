"""The checked cards, compared in a pop-up.

The customer checks "Compare" on two or more cards; the pop-up shows the side-by-side
table and a line introducing it. It is a look, not a turn: nothing is
added to the conversation and the session is not written, so closing the
pop-up leaves the chat exactly as it was.

Only similar products are compared - sofas with sofas, never a sofa and a
coffee table - by the reviewed families (`compare_groups_v1.yaml`). The client greys
out dissimilar cards before asking; this service refuses them regardless,
because a client's checkbox is not a rule (CLAUDE.md 20.2).

Cards are resolved exactly as a tick is, against the result lists the session
remembers; the table comes from the ordinary comparison service, so the pop-up and a
typed "compare the first two" can never disagree about the facts.
"""

from __future__ import annotations

from app.core.exceptions import ComparisonRefusedError
from app.core.logging import get_logger
from app.repositories.sessions import SessionStore
from app.schemas.card_comparison import CardComparisonRequest, CardComparisonResponse
from app.schemas.resolution import (
    ComparisonFailureReason,
    ComparisonUnavailable,
    ReferenceUnresolved,
)
from app.schemas.retailer import RetailerContext
from app.services.comparison import ProductComparisonService
from app.services.reference_resolver import ProductReferenceResolver
from app.taxonomy.compare_groups import CompareGroups

logger = get_logger(__name__)

_GONE = "One of those products isn't on screen any more. Please pick from the latest results."
_TWICE = "Pick different products to compare."

COMPARE_WORDS = "Compare these"
_SIDE_BY_SIDE = "Here they are side by side."
"""What the reply is told the customer asked. Never recorded anywhere."""


class CardComparisonService:
    def __init__(
        self,
        references: ProductReferenceResolver,
        comparison: ProductComparisonService,
        sessions: SessionStore,
        groups: CompareGroups,
    ) -> None:
        self._references = references
        self._comparison = comparison
        self._sessions = sessions
        self._groups = groups

    async def compare(
        self, request: CardComparisonRequest, context: RetailerContext
    ) -> CardComparisonResponse:
        envelope = await self._sessions.load(request.store_id, request.session_id)
        if envelope is None:
            raise ComparisonRefusedError(public_message=_GONE, reason="no_session")
        state = envelope.state

        product_ids: list[int] = []
        for card in request.cards:
            outcome = await self._references.resolve_on_list(
                card.ordinal, card.list_revision, state, context
            )
            if isinstance(outcome, ReferenceUnresolved):
                raise ComparisonRefusedError(public_message=_GONE, reason=str(outcome.reason))
            product_ids.append(outcome.product_id)
        if len(set(product_ids)) != len(product_ids):
            raise ComparisonRefusedError(public_message=_TWICE, reason="same_product")

        comparison = await self._comparison.compare(product_ids, context)
        if isinstance(comparison, ComparisonUnavailable):
            raise ComparisonRefusedError(
                public_message=_refusal(comparison), reason=str(comparison.reason)
            )
        subcategories = [product.commerce.subcategory for product in comparison.products]
        first = subcategories[0]
        if not self._groups.all_comparable(subcategories):
            logger.info("card_comparison_refused", store_id=context.store_id, reason="dissimilar")
            raise ComparisonRefusedError(reason="dissimilar")

        # The table is the answer; the conversation's agents word comparisons
        # asked for in the chat.
        logger.info(
            "card_comparison_completed",
            store_id=context.store_id,
            family=self._groups.family(first),
            compared_count=len(product_ids),
        )
        return CardComparisonResponse(comparison=comparison, message=_SIDE_BY_SIDE)


def _refusal(outcome: ComparisonUnavailable) -> str:
    """In the customer's words, why these cannot be compared."""
    if outcome.reason is ComparisonFailureReason.TOO_MANY_PRODUCTS:
        return f"You can compare up to {outcome.allowed_maximum} products at a time."
    if outcome.reason is ComparisonFailureReason.DUPLICATE_PRODUCT:
        return _TWICE
    return _GONE
