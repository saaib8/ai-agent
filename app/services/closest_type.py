"""The closest stocked type to an unstocked request.

The salesperson's "we don't carry that, but here's the nearest thing" judgement,
made a safe, typed model step. The model brings the judgement; the catalog brings
the facts. It is given only the types this store actually stocks near the request
and can answer with nothing else - the response schema is restricted to that list
and the answer is re-checked afterwards, so it can neither invent a type nor name
one the store does not carry (CLAUDE.md 3.3, 14).

Every failure is quiet. A provider outage, an answer the model somehow returns
outside the list, or an honest "nothing here is close" all resolve to ``None`` -
no substitute - and the turn falls back to its ordinary honest reply. This layer
never raises at the customer (CLAUDE.md 21.1).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.exceptions import (
    IntegrationUnavailableError,
    LLMRequestError,
    LLMResponseInvalidError,
)
from app.core.logging import get_logger
from app.integrations.llm import StructuredLLMClient
from app.prompts.closest_type.v1 import (
    VERSION,
    build_correction,
    build_instructions,
    render_request,
)
from app.schemas.retailer import RetailerContext
from app.schemas.substitution import build_constrained_closest_type
from app.taxonomy.registry import CommerceTaxonomy

logger = get_logger(__name__)

_HANDLED_LLM_FAILURES = (
    IntegrationUnavailableError,
    LLMRequestError,
    LLMResponseInvalidError,
)
"""Provider outcomes that mean "no substitute", not an error. The recovery is
best-effort: if the model cannot answer, the turn keeps its honest zero result."""


class ClosestTypeResolver:
    """Which stocked type is closest to a type the store does not carry."""

    def __init__(self, client: StructuredLLMClient, taxonomy: CommerceTaxonomy) -> None:
        self._client = client
        self._taxonomy = taxonomy
        self._instructions = build_instructions()
        self._correction = build_correction()

    async def closest(
        self,
        *,
        asked_subcategory: str,
        commerce_category: str,
        offered: Sequence[str],
        context: RetailerContext,
    ) -> str | None:
        """The closest of ``offered`` to ``asked_subcategory``, or ``None``.

        ``offered`` are the subcategories this store actually stocks in the same
        category (the asked one excluded). ``None`` comes back three ways, all
        meaning "no substitute": nothing was offered, the model judged none of
        them close, or it could not answer. The caller keeps its honest reply in
        every one of them.
        """
        options = tuple(dict.fromkeys(offered))
        if not options:
            return None

        schema = build_constrained_closest_type(options)
        for instructions in (self._instructions, self._correction):
            try:
                choice = await self._client.parse(
                    instructions=instructions,
                    user_input=render_request(asked_subcategory, options),
                    schema=schema,
                )
            except _HANDLED_LLM_FAILURES as exc:
                logger.warning(
                    "closest_type_unavailable",
                    error=type(exc).__name__,
                    store_id=context.store_id,
                )
                return None

            picked = choice.closest_subcategory
            if picked is None:
                # A deliberate decline, not a failure: nothing here is close, so
                # the turn stays honest rather than forcing a bad substitute.
                logger.info(
                    "closest_type_declined",
                    store_id=context.store_id,
                    asked=asked_subcategory,
                    offered_count=len(options),
                )
                return None
            # Deterministic re-check even though the schema was constrained: a
            # Literal cannot express "valid under this category" (CLAUDE.md 14.4),
            # and this is the real guarantee if the schema ever loosens.
            if picked in options and self._taxonomy.is_pair(commerce_category, picked):
                logger.info(
                    "closest_type_picked",
                    store_id=context.store_id,
                    asked=asked_subcategory,
                    picked=picked,
                    prompt_version=VERSION,
                )
                return picked
            logger.warning(
                "closest_type_invalid_pick",
                store_id=context.store_id,
                asked=asked_subcategory,
            )
        return None
