"""A weight the customer gives is a limit the catalog answers.

"Art canvases below 3 kg" asks the database for canvases whose listed weight
is at most 3 kg - every card shown meets it, and the next page is more of
them - instead of a page the agent sorts by eye. A product with no listed
weight never counts as light enough, nothing widens the limit, and when
nothing meets it the reply can say what setting it aside would find.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import pytest
from app.agents.facts import searched_for
from app.core.config import RelaxationSettings
from app.core.exceptions import LLMResponseInvalidError
from app.integrations.llm import StructuredLLMClient
from app.repositories.products import ProductRepository
from app.schemas.agent_state import ActiveSearchState
from app.schemas.discovery import ProductSearchRequest, WeightConstraint
from app.schemas.product import EligibleProduct
from app.schemas.query import CommerceInterpretation, ConstraintSemantics, ResolvedSearch
from app.schemas.relaxation import SetAsideField
from app.schemas.retailer import RetailerContext
from app.services.controlled_search import ControlledRelaxationService
from app.services.discovery import ProductDiscoveryService
from app.services.query_understanding import QueryUnderstandingService
from app.services.relaxation import RelaxationPlanner
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.test_query_understanding import ATTRIBUTES, DIMENSIONS, TAXONOMY, FakeLLMClient
from tests.unit.test_repository_scoping import CONTEXT, RecordingSession, _sql


def _resolved(**weight: str) -> Any:
    service = QueryUnderstandingService(
        cast(StructuredLLMClient, FakeLLMClient()), TAXONOMY, ATTRIBUTES, DIMENSIONS
    )
    return service.resolve(
        CommerceInterpretation(
            commerce_category="decor", commerce_subcategory="art-canvas", **weight
        )
    )


def _canvases(weight: WeightConstraint | None = None) -> ProductSearchRequest:
    return ProductSearchRequest(
        commerce_category="decor", commerce_subcategory="art-canvas", weight=weight
    )


# ── the agent's words become a limit in kilograms ───────────────────────────


def test_below_three_kilograms_is_a_ceiling_of_three() -> None:
    outcome = _resolved(weight_max="3")

    assert isinstance(outcome, ResolvedSearch)
    assert outcome.request.weight == WeightConstraint(max_kg=Decimal(3))


@pytest.mark.parametrize(
    ("unit", "said", "kilograms"),
    [("g", "500", Decimal("0.5")), ("lb", "10", Decimal("4.5359237"))],
)
def test_another_unit_is_converted_in_code(unit: str, said: str, kilograms: Decimal) -> None:
    outcome = _resolved(weight_max=said, weight_unit=unit)

    assert outcome.request.weight.max_kg == kilograms


def test_no_weight_said_is_no_limit() -> None:
    assert _resolved().request.weight is None


def test_a_floor_above_its_ceiling_is_refused_not_guessed() -> None:
    with pytest.raises(LLMResponseInvalidError):
        _resolved(weight_min="5", weight_max="3")


def test_a_limit_needs_a_bound() -> None:
    with pytest.raises(ValidationError):
        WeightConstraint()


# ── the database answers it ─────────────────────────────────────────────────


async def test_the_limit_is_part_of_eligibility() -> None:
    session = RecordingSession()
    await ProductRepository(cast(AsyncSession, session)).search_eligible_pool(
        _canvases(WeightConstraint(min_kg=Decimal(1), max_kg=Decimal(3))), CONTEXT
    )

    sql = _sql(session.statements[0])
    assert "core_product.weight_kg >= 1" in sql
    assert "core_product.weight_kg <= 3" in sql


async def test_without_a_limit_weight_is_not_asked_about() -> None:
    session = RecordingSession()
    await ProductRepository(cast(AsyncSession, session)).search_eligible_pool(_canvases(), CONTEXT)

    assert "weight_kg >=" not in _sql(session.statements[0])
    assert "weight_kg <=" not in _sql(session.statements[0])


def test_a_search_without_weight_is_stored_as_before() -> None:
    assert "weight" not in _canvases().model_dump(mode="json")


# ── reported, never widened, and set aside when nothing meets it ────────────


def test_the_search_summary_says_the_weight_that_ran() -> None:
    active = ActiveSearchState(
        request=_canvases(WeightConstraint(max_kg=Decimal(3))),
        semantics=ConstraintSemantics(),
        revision=1,
    )

    assert searched_for(active)["weight"] == {"max_kg": "3"}


class _Catalog:
    """Canvases with no weight under 3 kg - and a few heavier ones."""

    async def eligible_pool(
        self, request: ProductSearchRequest, context: RetailerContext
    ) -> tuple[EligibleProduct, ...]:
        if request.weight is not None:
            return ()
        return tuple(EligibleProduct(product_id=n, price_amount=Decimal(100)) for n in (1, 2))


async def test_nothing_light_enough_offers_setting_the_weight_aside() -> None:
    settings = RelaxationSettings()
    service = ControlledRelaxationService(
        cast(ProductDiscoveryService, _Catalog()), RelaxationPlanner(settings), settings
    )
    resolved = ResolvedSearch(
        request=_canvases(WeightConstraint(max_kg=Decimal(3))), semantics=ConstraintSemantics()
    )

    result = await service.search(resolved, CONTEXT, explain_empty=True)

    assert result.candidates == ()
    assert [(o.field, o.eligible_count) for o in result.set_aside] == [(SetAsideField.WEIGHT, 2)]
    # Nothing widened the limit on the way.
    assert all(a.request.weight == resolved.request.weight for a in result.attempts)
