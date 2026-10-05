"""The agent loop: look at a weak result, then decide the next move.

Phase 2 of the agent-loop plan (CLAUDE.md 14.8). When a customer's search for a
type the store stocks found nothing within their limits - after relaxation and
after the seating recoveries - the model sees what happened and may try a
related stocked type with every other limit kept, then choose what to present.

It is deliberately narrow, and every rule lives in code, not in the prompt:

- the model can name only the stocked siblings of the asked type in the same
  category (13.3), never one already tried, and a request for several seats is
  never offered a one-seat type (27.1);
- a try changes the type and nothing else: price, seat count, strict colour
  and style, wishes and their words all carry; sizes stay with the type they
  were given for and are reported as dropped (13.5);
- a try is executed, never committed - state changes only for the result the
  caller finally presents;
- a type is presented only if a try found products; anything unusable from the
  model, or any failure, ends the loop with the original reply (21.1).

The loop answers which type to show and nothing else. Whether a product exists,
its price and its fit to the limits stay with the search pipeline.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from pydantic import BaseModel

from app.core.exceptions import (
    IntegrationUnavailableError,
    LLMRequestError,
    LLMResponseInvalidError,
)
from app.core.logging import get_logger
from app.integrations.llm import StructuredLLMClient
from app.prompts.agent_loop.v1 import VERSION, build_instructions
from app.schemas.agent_loop import Finish, TryType, build_move_schema
from app.schemas.catalog_overview import CatalogOverview
from app.schemas.composition import ComposedSearch
from app.schemas.grounding import SearchExecutionGrounding
from app.schemas.query import ConstraintStrength
from app.schemas.resolution import ProductSearchExecutionResult
from app.schemas.retailer import RetailerContext
from app.services.retype import composed_as_type
from app.services.search_pipeline import ProductSearchPipeline
from app.services.stock_fit import seats_wanted
from app.taxonomy.compare_groups import CompareGroups
from app.taxonomy.seating import SeatingSemantics
from app.taxonomy.words import customer_words

logger = get_logger(__name__)

_HANDLED_FAILURES = (IntegrationUnavailableError, LLMRequestError, LLMResponseInvalidError)


@dataclass(frozen=True, slots=True)
class LoopTry:
    """One type tried: the search that would present it, and what it found."""

    commerce_subcategory: str
    composed: ComposedSearch
    execution: ProductSearchExecutionResult
    """Committed as it is if chosen - never run a second time."""

    @property
    def grounding(self) -> SearchExecutionGrounding:
        return self.execution.grounding

    @property
    def found(self) -> int:
        """Products that met every limit exactly - none widened, no strict
        colour or style lifted. Only these make a type worth presenting: the
        reply says the change of kind loosened nothing (CLAUDE.md 14.8)."""
        return self.grounding.exact_candidate_count


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    chosen: LoopTry | None
    """The try to present, or None to keep the original reply."""

    tried: tuple[LoopTry, ...] = ()
    steps: int = 0


class WeakSearchLoop:
    """Up to `max_tries` tries of a related stocked type, then a choice."""

    def __init__(
        self,
        client: StructuredLLMClient,
        pipeline: ProductSearchPipeline,
        seating: SeatingSemantics | None,
        stand_ins: CompareGroups | None,
        *,
        max_tries: int,
    ) -> None:
        self._client = client
        self._pipeline = pipeline
        self._seating = seating
        self._stand_ins = stand_ins
        """Which types do the same job, as reviewed data - the comparison
        groups (`compare_groups_v1.yaml`): a sofa and an L-shape do, a coffee
        table and a side table do not. Without them nothing is offered."""
        self._max_tries = max_tries
        self._instructions = build_instructions()

    async def explore(
        self,
        composed: ComposedSearch,
        original: SearchExecutionGrounding,
        overview: CatalogOverview,
        context: RetailerContext,
    ) -> LoopOutcome:
        request = composed.resolved.request
        asked = request.commerce_subcategory
        if asked is None or composed.resolved.kind_required:
            # A category asked broadly has no "kind" to stand in for; a kind
            # they insisted on ("L-shaped, nothing else") is not ours to swap.
            return LoopOutcome(chosen=None)
        if request.dimensions or request.planar_dimensions is not None:
            # A size they gave may be the whole gap, and another kind would
            # only drop it quietly. Which limit to set aside is their choice,
            # and the original reply already offers it (CLAUDE.md 13.5).
            return LoopOutcome(chosen=None)
        candidates = self._candidates(composed, overview)
        if not candidates:
            return LoopOutcome(chosen=None)

        started = time.perf_counter()
        tried: list[LoopTry] = []
        steps = 0
        chosen: LoopTry | None = None
        while True:
            can_try = tuple(
                c for c in candidates if c not in {t.commerce_subcategory for t in tried}
            )
            if len(tried) >= self._max_tries:
                can_try = ()
            can_present = tuple(t.commerce_subcategory for t in tried if t.found)
            if not can_try and not can_present:
                break
            steps += 1
            move = await self._next_move(
                composed, original, overview, tried, can_try, can_present, context
            )
            if move is None:
                break
            if isinstance(move, Finish):
                chosen = next(
                    (t for t in tried if t.commerce_subcategory == move.present and t.found),
                    None,
                )
                break
            if move.commerce_subcategory not in can_try:
                # Unrepresentable in the schema; re-checked because the schema
                # is the provider's promise and this is ours.
                logger.warning("agent_loop_invalid_move", store_id=context.store_id)
                break
            attempt = await self._try(composed, move, context)
            if attempt is None:
                break
            tried.append(attempt)

        logger.info(
            "agent_loop_completed",
            store_id=context.store_id,
            prompt_version=VERSION,
            asked=asked,
            tried=[t.commerce_subcategory for t in tried],
            found=[t.found for t in tried],
            chosen=chosen.commerce_subcategory if chosen else None,
            steps=steps,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return LoopOutcome(chosen=chosen, tried=tuple(tried), steps=steps)

    def _candidates(self, composed: ComposedSearch, overview: CatalogOverview) -> tuple[str, ...]:
        """The stocked siblings of the asked type that could meet their limits.

        Judged on reviewed data and catalog facts before the model sees
        anything, so no step is spent on a type that cannot serve
        (CLAUDE.md 14.8, 27.1):
        - it does the same job, by the reviewed comparison groups;
        - its cheapest piece is above their ceiling, or its dearest below their
          floor - compared only when the store's currency is theirs;
        - it seats one and they need several, or no single piece of it is
          recorded as seating that many.
        """
        request = composed.resolved.request
        wanted = seats_wanted(request)
        price = request.price
        priced = price is not None and price.currency == overview.currency
        if self._stand_ins is None:
            return ()
        return tuple(
            shelf.commerce_subcategory
            for shelf in overview.shelves_in(request.commerce_category)
            if shelf.commerce_subcategory is not None
            and shelf.commerce_subcategory != request.commerce_subcategory
            and self._stand_ins.comparable(
                request.commerce_subcategory, shelf.commerce_subcategory
            )
            and not (
                priced
                and price is not None
                and (
                    (price.max_amount is not None and shelf.price_minimum > price.max_amount)
                    or (price.min_amount is not None and shelf.price_maximum < price.min_amount)
                )
            )
            and not (
                wanted > 1
                and (
                    (
                        self._seating is not None
                        and self._seating.seats_one(shelf.commerce_subcategory)
                    )
                    or shelf.max_seats is None
                    or shelf.max_seats < wanted
                )
            )
        )

    async def _next_move(
        self,
        composed: ComposedSearch,
        original: SearchExecutionGrounding,
        overview: CatalogOverview,
        tried: list[LoopTry],
        can_try: tuple[str, ...],
        can_present: tuple[str, ...],
        context: RetailerContext,
    ) -> TryType | Finish | None:
        schema = build_move_schema(can_try, can_present)
        try:
            answer = await self._client.parse(
                instructions=self._instructions,
                user_input=_observation(composed, original, overview, tried, can_try),
                schema=schema,
            )
        except _HANDLED_FAILURES as exc:
            logger.warning(
                "agent_loop_unavailable", store_id=context.store_id, error=type(exc).__name__
            )
            return None
        return answer.move

    async def _try(
        self, composed: ComposedSearch, move: TryType, context: RetailerContext
    ) -> LoopTry | None:
        variant = composed_as_type(composed, move.commerce_subcategory)
        try:
            execution = await self._pipeline.execute(
                variant.resolved,
                context,
                dropped_constraints=variant.dropped_constraints,
            )
        except _HANDLED_FAILURES:
            logger.warning("agent_loop_try_unavailable", store_id=context.store_id)
            return None
        return LoopTry(
            commerce_subcategory=move.commerce_subcategory,
            composed=variant,
            execution=execution,
        )


class _Observation(BaseModel):
    """What the model sees: facts in words, no ids, no product rows."""

    asked_kind: str
    their_limits: list[str]
    their_words: str | None
    asked_kind_found: str
    setting_one_limit_aside_would_find: list[str]
    stocked_kinds_in_family: list[str]
    tried: list[str]
    you_may_still_try: list[str]


def _observation(
    composed: ComposedSearch,
    original: SearchExecutionGrounding,
    overview: CatalogOverview,
    tried: list[LoopTry],
    can_try: tuple[str, ...],
) -> str:
    request = composed.resolved.request
    shelves = {
        shelf.commerce_subcategory: shelf
        for shelf in overview.shelves_in(request.commerce_category)
        if shelf.commerce_subcategory is not None
    }

    def shelf_line(subcategory: str) -> str:
        shelf = shelves[subcategory]
        seats = f", seats up to {shelf.max_seats}" if shelf.max_seats else ""
        return (
            f"{subcategory}: {shelf.active_count} products, "
            f"{shelf.price_minimum}-{shelf.price_maximum}{seats}"
        )

    observation = _Observation(
        asked_kind=request.commerce_subcategory or request.commerce_category,
        their_limits=_limits(composed),
        their_words=composed.resolved.semantic_text,
        asked_kind_found=f"{len(original.products)} products within those limits",
        setting_one_limit_aside_would_find=[
            f"{option.field}"
            + (f" ({option.role})" if option.role else "")
            + f": {option.eligible_count}"
            + (f", nearest price {option.nearest_price}" if option.nearest_price else "")
            for option in original.set_aside
        ],
        stocked_kinds_in_family=[
            shelf_line(s)
            for s in shelves
            if s in can_try or any(t.commerce_subcategory == s for t in tried)
        ],
        tried=[f"{t.commerce_subcategory}: {t.found} products met every limit" for t in tried],
        you_may_still_try=list(can_try),
    )
    return json.dumps(observation.model_dump(), ensure_ascii=False)


def _limits(composed: ComposedSearch) -> list[str]:
    """Their limits in plain words - what every try keeps."""
    request = composed.resolved.request
    limits: list[str] = []
    if request.price is not None:
        price = request.price
        if price.max_amount is not None:
            limits.append(f"at most {price.max_amount} {price.currency}")
        if price.min_amount is not None:
            limits.append(f"at least {price.min_amount} {price.currency}")
    capacity = request.seating_capacity
    if capacity is not None and capacity.min_capacity is not None:
        limits.append(f"seats at least {capacity.min_capacity}")
    if request.colors_any_of:
        limits.append("only these colours: " + ", ".join(request.colors_any_of))
    if request.styles_all_of:
        limits.append("only this style: " + ", ".join(request.styles_all_of))
    wishes = [
        p.canonical_value or p.raw_value
        for p in composed.candidate.semantic_preferences
        if p.strength is not ConstraintStrength.LOCKED
    ]
    if wishes:
        limits.append("would like (not required): " + ", ".join(wishes))
    if request.dimensions or request.planar_dimensions:
        limits.append(
            f"a size for the {customer_words(request.commerce_subcategory or '')} "
            "(it does not carry to another kind)"
        )
    return limits
