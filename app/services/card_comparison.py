"""Two checked cards, compared in a pop-up.

The customer checks "Compare" on two cards; the pop-up shows the side-by-side
table and a short take on what differs. It is a look, not a turn: nothing is
added to the conversation and the session is not written, so closing the
pop-up leaves the chat exactly as it was.

Only similar products are compared - two sofas, never a sofa and a coffee
table - by the reviewed families (`compare_groups_v1.yaml`). The client greys
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
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.resolution import ComparisonUnavailable, ReferenceUnresolved
from app.schemas.retailer import RetailerContext
from app.services.comparison import ProductComparisonService
from app.services.reference_resolver import ProductReferenceResolver
from app.services.response_generator import CustomerResponseGenerator
from app.taxonomy.compare_groups import CompareGroups

logger = get_logger(__name__)

_GONE = "One of those products isn't on screen any more. Please pick from the latest results."
_TWICE = "Pick two different products to compare."

COMPARE_WORDS = "Compare these two"
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
        if product_ids[0] == product_ids[1]:
            raise ComparisonRefusedError(public_message=_TWICE, reason="same_product")

        comparison = await self._comparison.compare(product_ids, context)
        if isinstance(comparison, ComparisonUnavailable):
            raise ComparisonRefusedError(public_message=_GONE, reason=str(comparison.reason))
        first, second = (product.commerce.subcategory for product in comparison.products)
        if not self._groups.comparable(first, second):
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
                comparison_references=tuple(
                    PresentedOrdinal(position=card.ordinal) for card in request.cards
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
        )
        return CardComparisonResponse(comparison=comparison, message=response.message)
