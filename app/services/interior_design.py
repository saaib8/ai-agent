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


class InteriorDesignAgent:
    """A design request in, structured design reasoning out."""

    def __init__(
        self, client: StructuredLLMClient, taxonomy: CommerceTaxonomy
    ) -> None:
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
            raise LLMResponseInvalidError(
                reason="design result contained an excluded role"
            )

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
        logger.warning(
            "interior_design_needs_on_advice", dropped=len(result.needs)
        )
        return InteriorDesignResult(guidance=result.guidance)

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
