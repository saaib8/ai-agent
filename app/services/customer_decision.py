"""The Customer/Commerce Agent's decision step.

One structured model call per turn, and nothing else. It reads the safe
projection of the conversation and returns what the turn should do; every fact
needed to actually do it comes from deterministic services afterwards
(CLAUDE.md 3.3, 17.1).

The dependency list is the authority argument. This service holds a provider
client and nothing more - no repository, no search pipeline, no resolver, no
state reducer, no retailer context. It cannot query the catalog, cannot mutate
state and cannot widen its own scope, because it has nothing to do it with
(CLAUDE.md 20.2).

Exactly one call. There is no repair prompt, no second attempt and no critic
pass: a decision the model got wrong is not improved by asking it again with
its own output, and a failed call is the orchestration layer's to handle, not
this service's to paper over with an invented decision.
"""

from __future__ import annotations

import time

from app.core.logging import get_logger
from app.integrations.llm import StructuredLLMClient
from app.prompts.customer_commerce.v1 import VERSION, build_instructions
from app.schemas.agent_decision import CustomerAgentDecision
from app.schemas.agent_turn import DecisionInput

logger = get_logger(__name__)


class CustomerAgentDecisionService:
    """Decides what one customer turn should do. Executes none of it."""

    def __init__(self, client: StructuredLLMClient) -> None:
        self._client = client
        self._instructions = build_instructions()

    async def decide(self, decision_input: DecisionInput) -> CustomerAgentDecision:
        """One turn in, one decision out.

        The input is serialised as JSON rather than interpolated field by field
        so that the boundary is the type: whatever `DecisionInput` permits is
        exactly what the provider sees, and a field added to it later cannot be
        forgotten here (CLAUDE.md 20.3).

        It is sent as the user turn, never merged into the instructions,
        because it carries the customer's untrusted words.
        """
        payload = decision_input.model_dump_json(exclude_none=True)

        started = time.perf_counter()
        decision = await self._client.parse(
            instructions=self._instructions,
            user_input=payload,
            schema=CustomerAgentDecision,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        # Shape of the decision only: no customer text, no history, no prompt,
        # no selector internals (CLAUDE.md 22).
        logger.info(
            "customer_decision_completed",
            prompt_version=VERSION,
            model=self._client.model,
            action=str(decision.action),
            commercial_reason=(
                str(decision.commercial_reason) if decision.commercial_reason else None
            ),
            follow_up_policy=str(decision.follow_up_policy),
            blocking_clarification=decision.clarification is not None,
            has_interaction=decision.interaction is not None,
            has_new_search=decision.new_search is not None,
            has_refinement=decision.refinement is not None,
            taxonomy_change_requested=decision.taxonomy_change_requested,
            has_state_proposal=decision.state_proposal is not None,
            has_commerce_proposal=decision.commerce_proposal is not None,
            reference_count=len(decision.comparison_references)
            + (1 if decision.reference is not None else 0),
            elapsed_ms=elapsed_ms,
        )
        return decision
