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

One call, and at most one corrective call. An answer that breaks a rule is
sent back once with the rules it broke - our own rule text, never the
customer's words - because a model told which field it got wrong usually fixes
it, and "please rephrase" costs the customer far more than one extra call. A
second failure is not retried again: it propagates, and the turn falls back to
a conversational reply. Outages are never retried here.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from functools import cache

from app.core.exceptions import LLMResponseInvalidError
from app.core.logging import get_logger
from app.integrations.llm import StructuredLLMClient
from app.prompts.customer_commerce.v1 import (
    VERSION,
    build_correction,
    build_instructions,
    describe_unusable,
)
from app.schemas.agent_decision import (
    CustomerAgentDecision,
    build_constrained_decision,
    to_plain_decision,
)
from app.schemas.agent_turn import DecisionInput
from app.taxonomy.attributes import CatalogAttributes

logger = get_logger(__name__)


@cache
def _constrained_schema(attributes: CatalogAttributes) -> type[CustomerAgentDecision]:
    """Built once per vocabulary object and reused.

    The service is constructed per request; rebuilding a nested schema of
    generated models on every turn would cost time for an answer that never
    changes while the process runs. The registry is loaded once at startup, so
    this holds exactly one entry.
    """
    return build_constrained_decision(attributes)


class CustomerAgentDecisionService:
    """Decides what one customer turn should do. Executes none of it."""

    def __init__(
        self, client: StructuredLLMClient, attributes: CatalogAttributes | None = None
    ) -> None:
        """`attributes` restricts every colour and style the model can write.

        With it, the instructions list the approved vocabulary and the response
        schema only admits those values, so "Dark Grey" cannot be written and
        then fail downstream. Without it, the decision is unconstrained.
        """
        self._client = client
        self._instructions = build_instructions(attributes)
        self._schema: type[CustomerAgentDecision] = (
            _constrained_schema(attributes) if attributes is not None else CustomerAgentDecision
        )

    async def decide(
        self, decision_input: DecisionInput, *, problems: Sequence[str] = ()
    ) -> CustomerAgentDecision:
        """One turn in, one decision out.

        The input is serialised as JSON rather than interpolated field by field
        so that the boundary is the type: whatever `DecisionInput` permits is
        exactly what the provider sees, and a field added to it later cannot be
        forgotten here (CLAUDE.md 20.3).

        It is sent as the user turn, never merged into the instructions,
        because it carries the customer's untrusted words.

        **One corrective attempt.** An answer that breaks a rule is sent back
        once, with the rules it broke - our own rule text, never the
        customer's words or the refused answer. A model that misread one field
        usually gets it right when told which; a turn that ends in "please
        rephrase" costs the customer far more than one extra call. Outages are
        not retried here: the SDK already retries transport failures.

        `problems` makes this call the corrective attempt itself - used when a
        valid decision could not be *applied* downstream. It gets no further
        retry, so a turn never costs more than three decisions.
        """
        payload = decision_input.model_dump_json(exclude_none=True)
        if problems:
            return await self._attempt(payload, tuple(problems))
        try:
            return await self._attempt(payload, ())
        except LLMResponseInvalidError as exc:
            found = describe_unusable(
                exc.context.get("reason"), exc.context.get("violations", ())
            )
            logger.warning("customer_decision_retrying", problems=list(found))
            return await self._attempt(payload, found)

    async def _attempt(self, payload: str, problems: tuple[str, ...]) -> CustomerAgentDecision:
        """One provider call; a corrective one when `problems` is not empty."""
        instructions = (
            self._instructions + build_correction(problems) if problems else self._instructions
        )
        started = time.perf_counter()
        try:
            decision = to_plain_decision(
                await self._client.parse(
                    instructions=instructions,
                    user_input=payload,
                    schema=self._schema,
                )
            )
        except LLMResponseInvalidError as exc:
            if not problems:
                raise
            # The corrective attempt failed too. Marked, so the caller knows
            # the decision itself already had its second chance.
            raise LLMResponseInvalidError(
                reason=exc.context.get("reason"),
                violations=exc.context.get("violations", ()),
                stage="decision",
            ) from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        # Shape of the decision only: no customer text, no history, no prompt,
        # no selector internals (CLAUDE.md 22).
        logger.info(
            "customer_decision_completed",
            prompt_version=VERSION,
            model=self._client.model,
            corrective=bool(problems),
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
