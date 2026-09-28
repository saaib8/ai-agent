"""The closest stocked type when the exact one is absent - a safe model step.

The model brings the judgement; it can invent nothing. It is given only the
types the store stocks near the request, its pick is re-checked against that
list and the taxonomy, and every way of not-answering (a decline, an off-list
value, a provider outage) resolves to "no substitute" so the turn stays honest.
"""

from __future__ import annotations

from typing import Any, cast

from app.core.exceptions import IntegrationUnavailableError, LLMResponseInvalidError
from app.integrations.llm import StructuredLLMClient
from app.schemas.retailer import RetailerContext
from app.schemas.substitution import ClosestTypeChoice
from app.services.closest_type import ClosestTypeResolver
from app.taxonomy.registry import load_taxonomy

TAXONOMY = load_taxonomy()
CONTEXT = RetailerContext(store_id=50)


class FakeLLM:
    """Returns the given picks in order, then declines. Ignores the schema so a
    test can hand back an off-list value the constrained schema would reject -
    which is exactly the belt-and-braces case the resolver re-validates."""

    def __init__(self, *picks: str | None, error: Exception | None = None) -> None:
        self._picks = list(picks)
        self._error = error
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return "fake-model"

    async def parse(self, *, instructions: str, user_input: str, schema: type) -> Any:
        self.calls.append({"instructions": instructions, "user_input": user_input})
        if self._error is not None:
            raise self._error
        value = self._picks.pop(0) if self._picks else None
        return ClosestTypeChoice(closest_subcategory=value)


def _resolver(
    *picks: str | None, error: Exception | None = None
) -> tuple[ClosestTypeResolver, FakeLLM]:
    llm = FakeLLM(*picks, error=error)
    return ClosestTypeResolver(cast(StructuredLLMClient, llm), TAXONOMY), llm


async def _closest(
    resolver: ClosestTypeResolver,
    offered: tuple[str, ...],
    *,
    asked: str = "recliner",
    category: str = "seating",
) -> str | None:
    return await resolver.closest(
        asked_subcategory=asked,
        commerce_category=category,
        offered=offered,
        context=CONTEXT,
    )


async def test_it_picks_a_stocked_sibling() -> None:
    resolver, llm = _resolver("lounge-chair")

    assert await _closest(resolver, ("chair", "lounge-chair", "sofa")) == "lounge-chair"
    assert len(llm.calls) == 1


async def test_it_declines_when_nothing_is_close() -> None:
    """A null pick is a deliberate, valid answer, not a failure: the turn stays
    honest rather than forcing a bad substitute."""
    resolver, llm = _resolver(None)

    assert await _closest(resolver, ("chair", "sofa")) is None
    assert len(llm.calls) == 1


async def test_an_empty_offer_never_reaches_the_model() -> None:
    """No stocked siblings, nothing to choose: settled before any provider call."""
    resolver, llm = _resolver("chair")

    assert await _closest(resolver, ()) is None
    assert llm.calls == []


async def test_a_pick_outside_the_offered_list_is_refused() -> None:
    """Even if the schema ever loosened, a value the store does not stock in the
    offered set is caught deterministically and not shown."""
    resolver, _ = _resolver("wardrobe")  # not among the offered seating types

    assert await _closest(resolver, ("chair", "lounge-chair")) is None


async def test_it_retries_once_then_takes_the_valid_pick() -> None:
    resolver, llm = _resolver("wardrobe", "lounge-chair")  # first off-list, then valid

    assert await _closest(resolver, ("chair", "lounge-chair")) == "lounge-chair"
    assert len(llm.calls) == 2


async def test_two_bad_picks_end_in_no_substitute() -> None:
    resolver, llm = _resolver("wardrobe", "bed")  # both off-list

    assert await _closest(resolver, ("chair", "lounge-chair")) is None
    assert len(llm.calls) == 2


async def test_a_provider_outage_is_no_substitute_not_an_error() -> None:
    resolver, _ = _resolver(error=IntegrationUnavailableError())

    assert await _closest(resolver, ("chair", "lounge-chair")) is None


async def test_unusable_provider_output_is_no_substitute() -> None:
    resolver, _ = _resolver(error=LLMResponseInvalidError())

    assert await _closest(resolver, ("chair", "lounge-chair")) is None
