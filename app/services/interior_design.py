"""The interior-design specialist.

The second and only other reasoning agent (CLAUDE.md 17.2). It is **internal**:
the Customer/Commerce Agent owns the conversation, and nothing this returns
reaches a customer as written. Its output is structured reasoning that
deterministic services and the commerce agent consume.

What it holds is the whole authority argument. A provider client and the
taxonomy - no repository, no search pipeline, no retailer context, no state.
It cannot look a product up, cannot see which store it is serving, and cannot
change anything it was given, because it has nothing to do any of that with
(CLAUDE.md 20.2).

One call per plan. Everything the model returns is then validated
deterministically: an invented product type is refused rather than repaired,
and a type this retailer cannot supply is dropped rather than proposed.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from app.core.exceptions import LLMResponseInvalidError, TaxonomyValidationError
from app.core.logging import get_logger
from app.integrations.llm import StructuredLLMClient
from app.prompts.interior_design.v1 import VERSION, build_instructions
from app.schemas.design import (
    DesignCategoryNeed,
    DesignTask,
    ExcludedDesignRole,
    InteriorDesignRequest,
    InteriorDesignResult,
)
from app.taxonomy.registry import CommerceTaxonomy

logger = get_logger(__name__)

MAX_COMPLEMENTARY_NEEDS = 3
"""How many roles one complementary recommendation may carry.

A shortlist, not a shopping list. **One of them is shown** - the first the
retailer can actually supply - and the rest exist because "this retailer
stocks lounge chairs" and "this retailer can offer the customer a choice of
lounge chairs" are different claims, and only running the search settles the
second (CLAUDE.md 26).

Three is enough to survive a thin category twice and small enough that the
specialist still has to choose. It is not licence to furnish a room nobody
asked about: the roles beyond the first are fallbacks, discarded once one
works.
"""


class InteriorDesignAgent:
    """A design request in, structured design reasoning out."""

    def __init__(self, client: StructuredLLMClient, taxonomy: CommerceTaxonomy) -> None:
        self._client = client
        self._taxonomy = taxonomy
        self._instructions = build_instructions(taxonomy)

    async def plan(self, request: InteriorDesignRequest) -> InteriorDesignResult:
        """One design task, answered once.

        The request is serialised as JSON rather than interpolated field by
        field, so the boundary is the type: whatever `InteriorDesignRequest`
        permits is exactly what the specialist sees, and a field added later
        cannot be forgotten here. It travels as the user turn because it
        carries the customer's own brief, which is untrusted data and never
        part of the instructions (CLAUDE.md 20.1).
        """
        started = time.perf_counter()
        proposed = await self._client.parse(
            instructions=self._instructions,
            user_input=request.model_dump_json(exclude_none=True),
            schema=InteriorDesignResult,
        )
        result = self._validate(proposed, request)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        # Shape of the reasoning only: no brief, no guidance text, no room
        # measurements (CLAUDE.md 22).
        logger.info(
            "interior_design_completed",
            prompt_version=VERSION,
            model=self._client.model,
            task=str(request.task),
            guidance_count=len(result.guidance),
            proposed_need_count=len(proposed.needs),
            need_count=len(result.needs),
            has_geometry=request.geometry is not None,
            anchor_count=len(request.anchors),
            elapsed_ms=elapsed_ms,
        )
        return result

    def _validate(
        self, result: InteriorDesignResult, request: InteriorDesignRequest
    ) -> InteriorDesignResult:
        """Deterministic checks on everything the model returned.

        Two failures that look alike and are handled differently.

        An **invented product type** is a contract violation: the vocabulary
        was supplied, and a value outside it must never be accepted, persisted
        or quietly replaced with the nearest approved one (CLAUDE.md 14.3). The
        whole result is refused.

        A type **this retailer cannot supply** is not a violation at all. The
        room may genuinely want one; this shop simply has none, so the need is
        dropped as unfulfillable while the rest of the plan stands. Rejecting
        the whole plan over it would discard good reasoning because of a
        stocking decision.
        """
        try:
            result.validate_against(self._taxonomy)
        except TaxonomyValidationError as exc:
            logger.warning("interior_design_invented_taxonomy_value", detail=str(exc))
            raise LLMResponseInvalidError(
                reason="design result used an unapproved product type"
            ) from exc

        if request.task is DesignTask.GENERAL_ADVICE:
            return self._advice_only(result)

        if request.task is DesignTask.COMPLEMENTARY_RECOMMENDATION:
            return self._complementary(result, request)

        violated = self._excluded_role(result, request)
        if violated is not None:
            # Refused, never dropped. Dropping it here would be indistinguishable
            # from the capability filter below removing a type the retailer does
            # not stock - and the customer would be told their redesign happened
            # when the one thing they ruled out had been quietly deleted
            # instead of honoured (M12E-4D 23).
            logger.warning(
                "interior_design_excluded_role_returned",
                commerce_category=violated.commerce_category,
                commerce_subcategory=violated.commerce_subcategory,
            )
            raise LLMResponseInvalidError(reason="design result contained an excluded role")

        return self._fulfillable(result, request)

    @staticmethod
    def _excluded_role(
        result: InteriorDesignResult, request: InteriorDesignRequest
    ) -> ExcludedDesignRole | None:
        """The first hard negative constraint the plan broke, if any.

        Checked **before** `_fulfillable`, which drops rather than refuses.
        """
        revision = request.revision
        if revision is None:
            return None
        for role in revision.excluded:
            for need in result.needs:
                if role.excludes(need.commerce_category, need.commerce_subcategory):
                    return role
        return None

    def _advice_only(self, result: InteriorDesignResult) -> InteriorDesignResult:
        """Advice answers a question; it proposes nothing to buy.

        A question about what goes with walnut is not a shopping request, and
        an advice request carries no capabilities - so any need returned here
        could not be checked against what the retailer stocks even if it were
        wanted. Dropped rather than passed on unverifiable.
        """
        if not result.needs:
            return result
        logger.warning("interior_design_needs_on_advice", dropped=len(result.needs))
        return InteriorDesignResult(guidance=result.guidance)

    def _complementary(
        self, result: InteriorDesignResult, request: InteriorDesignRequest
    ) -> InteriorDesignResult:
        """One next role, with fallbacks - not a room.

        Order is preserved exactly as the specialist produced it, because the
        order *is* the design judgement: the caller shows the first role it can
        fill and never reorders them. Trimming takes from the end for the same
        reason (CLAUDE.md 26, 27).

        Trimmed rather than refused when the specialist overreaches: the first
        need is still the answer to what was asked, and discarding good
        reasoning because it came with extras would leave the customer with
        nothing. Capability filtering runs first, so what survives is both
        wanted and stocked.
        """
        fulfillable = self._fulfillable(result, request)
        beside = self._not_the_anchors_own_kind(fulfillable.needs, request)
        kept = beside[:MAX_COMPLEMENTARY_NEEDS]
        if len(fulfillable.needs) > len(kept):
            logger.info(
                "interior_design_complement_trimmed",
                proposed=len(fulfillable.needs),
                kept=len(kept),
            )
        return InteriorDesignResult(guidance=fulfillable.guidance, needs=kept)

    @staticmethod
    def _not_the_anchors_own_kind(
        needs: Sequence[DesignCategoryNeed], request: InteriorDesignRequest
    ) -> tuple[DesignCategoryNeed, ...]:
        """Roles the piece they chose does not already fill.

        A complement is what goes *beside* something. A customer who picked a
        dining chair and was offered dining chairs has been shown the thing
        they already decided on, and the search behind it was doomed anyway -
        the need carried a seating capacity meant for a room, which no single
        chair satisfies, so it returned nothing and the turn died (M18 1).

        Deterministic rather than left to the specialist, which is told this
        and did it anyway. Matched on the pair, so a chair does not block every
        kind of seating - only chairs.

        More of the same is a real request, and it is a different one: "another
        like this" is a search for alternatives, which the customer asks for in
        their own words.
        """
        if not request.anchors:
            return tuple(needs)
        already = {
            (anchor.commerce_category, anchor.commerce_subcategory)
            for anchor in request.anchors
        }
        beside = tuple(
            need
            for need in needs
            if (need.commerce_category, need.commerce_subcategory) not in already
        )
        if len(beside) != len(needs):
            logger.info(
                "complement_dropped_the_anchors_own_kind",
                proposed=len(needs),
                kept=len(beside),
            )
        return beside

    def _fulfillable(
        self, result: InteriorDesignResult, request: InteriorDesignRequest
    ) -> InteriorDesignResult:
        """Only needs this retailer can actually supply."""
        capabilities = request.catalog_capabilities
        assert capabilities is not None, "a room plan carries capabilities"

        kept: list[DesignCategoryNeed] = []
        dropped: list[str] = []
        for need in result.needs:
            if capabilities.supports(need.commerce_category, need.commerce_subcategory):
                kept.append(need)
            else:
                dropped.append(f"{need.commerce_category}/{need.commerce_subcategory}")

        if dropped:
            # Worth seeing: a plan losing most of its needs usually means the
            # retailer's range is narrower than the room wants, which is a
            # merchandising fact rather than a defect.
            logger.info(
                "interior_design_unfulfillable_needs",
                dropped=dropped,
                kept=len(kept),
            )
        return InteriorDesignResult(guidance=result.guidance, needs=tuple(kept))
