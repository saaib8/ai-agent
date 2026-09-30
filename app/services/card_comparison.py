"""The checked cards, compared in a pop-up.

The customer checks "Compare" on two or more cards; the pop-up shows the side-by-side
table and a short take on what differs. It is a look, not a turn: nothing is
added to the conversation and the session is not written, so closing the
pop-up leaves the chat exactly as it was.

Only similar products are compared - sofas with sofas, never a sofa and a
coffee table - by the reviewed families (`compare_groups_v1.yaml`). The client greys
out dissimilar cards before asking; this service refuses them regardless,
because a client's checkbox is not a rule (CLAUDE.md 20.2).

Cards are resolved exactly as a tick is, against the result lists the session
remembers; the table comes from the ordinary comparison service and the words
from the ordinary response generator, so the pop-up and a typed "compare the
first two" can never disagree about the facts.
"""

from __future__ import annotations

from app.core.exceptions import ComparisonRefusedError
from app.core.logging import get_logger
from app.repositories.sessions import SessionStore
from app.schemas.agent_decision import (
    AgentAction,
    CommercialReason,
    CustomerAgentDecision,
    FollowUpPolicy,
)
from app.schemas.agent_turn import CustomerTurnInput, CustomerTurnResult, TurnGrounding
from app.schemas.card_comparison import CardComparisonRequest, CardComparisonResponse
from app.schemas.product_reference import ComparedOrdinal
from app.schemas.resolution import (
    ComparisonFailureReason,
    ComparisonUnavailable,
    ReferenceUnresolved,
)
from app.schemas.retailer import RetailerContext
from app.services.comparison import ProductComparisonService
from app.services.reference_resolver import ProductReferenceResolver
from app.services.response_generator import CustomerResponseGenerator
from app.taxonomy.compare_groups import CompareGroups

logger = get_logger(__name__)

_GONE = "One of those products isn't on screen any more. Please pick from the latest results."
_TWICE = "Pick different products to compare."

COMPARE_WORDS = "Compare these"
"""What the reply is told the customer asked. Never recorded anywhere."""


class CardComparisonService:
    def __init__(
        self,
        references: ProductReferenceResolver,
        comparison: ProductComparisonService,
        responses: CustomerResponseGenerator,
        sessions: SessionStore,
        groups: CompareGroups,
    ) -> None:
        self._references = references
        self._comparison = comparison
        self._responses = responses
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
        # Families are an equivalence, so each against the first is all of them.
        first, *others = (product.commerce.subcategory for product in comparison.products)
        if not all(self._groups.comparable(first, other) for other in others):
            logger.info("card_comparison_refused", store_id=context.store_id, reason="dissimilar")
            raise ComparisonRefusedError(reason="dissimilar")

        # Worded exactly as a comparison turn is, from the same table - but
        # the result is thrown away afterwards, never persisted.
        result = CustomerTurnResult(
            state=state,
            decision=CustomerAgentDecision(
                action=AgentAction.COMPARE,
                commercial_reason=CommercialReason.CUSTOMER_REQUEST,
                follow_up_policy=FollowUpPolicy.NONE,
                # The comparison's own columns: cards from different lists
                # can share a position, and a comparison never repeats one.
                comparison_references=tuple(
                    ComparedOrdinal(position=column) for column in range(1, len(product_ids) + 1)
                ),
            ),
            grounding=TurnGrounding(comparison=comparison, follow_up_policy=FollowUpPolicy.NONE),
        )
        response = await self._responses.generate(
            CustomerTurnInput(
                message=COMPARE_WORDS,
                conversation=envelope.conversation,
                state=state,
                context=context,
            ),
            result,
        )
        logger.info(
            "card_comparison_completed",
            store_id=context.store_id,
            family=self._groups.family(first),
            compared_count=len(product_ids),
        )
        return CardComparisonResponse(comparison=comparison, message=response.message)


def _refusal(outcome: ComparisonUnavailable) -> str:
    """In the customer's words, why these cannot be compared."""
    if outcome.reason is ComparisonFailureReason.TOO_MANY_PRODUCTS:
        return f"You can compare up to {outcome.allowed_maximum} products at a time."
    if outcome.reason is ComparisonFailureReason.DUPLICATE_PRODUCT:
        return _TWICE
    return _GONE
