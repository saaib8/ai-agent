"""Comparing two checked cards in a pop-up, and only similar ones.

A look, not a turn: the pop-up's comparison is resolved like a tick, built by
the ordinary comparison service and worded by the reply generator, and nothing
is saved. Two sofas compare; a sofa and a coffee table do not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from app.api.dependencies import card_comparison_service, resources, retailer_context_provider
from app.api.errors import register_exception_handlers
from app.api.routes.comparisons import router as comparisons_router
from app.core.config import CustomerAgentSettings
from app.core.exceptions import ComparisonRefusedError, TaxonomyConfigurationError
from app.repositories.products import ProductRepository
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    PresentedList,
    ProductInteractionState,
)
from app.schemas.agent_turn import CustomerResponse
from app.schemas.card_comparison import CardComparisonRequest, CardRef
from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import CommerceClassification, ProductRow
from app.services.card_comparison import CardComparisonService
from app.services.comparison import ProductComparisonService
from app.services.reference_resolver import ProductReferenceResolver
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.compare_groups import load_compare_groups
from app.taxonomy.dimensions import load_dimension_semantics
from app.taxonomy.registry import load_taxonomy
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tests.unit.test_chat_api import FakeRetailers, FakeSessionStore
from tests.unit.test_picks import SESSION, STORE, _stored
from tests.unit.test_reference_resolver import FakeRepository, _row
from tests.unit.test_turn_coordinator import CONTEXT

TAXONOMY = load_taxonomy()
GROUPS = load_compare_groups(taxonomy=TAXONOMY)

KINDS = {
    101: ("seating", "sofa"),
    102: ("seating", "sofa"),
    201: ("seating", "sectional-sofa"),
    301: ("tables", "center-table"),
}


def _product(product_id: int) -> ProductRow:
    category, subcategory = KINDS[product_id]
    return _row(product_id).model_copy(
        update={"commerce": CommerceClassification(category=category, subcategory=subcategory)}
    )


def _state() -> AgentStateV1:
    """Sofas and a sectional shown at revision 1, a centre table at 2."""
    return AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="tables"), revision=2
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=(301,),
            presented_search_revision=2,
            earlier_lists=(PresentedList(revision=1, product_ids=(101, 102, 201)),),
        ),
    )


class Responses:
    """The reply generator, recording what it was asked to word."""

    def __init__(self) -> None:
        self.results: list[Any] = []

    async def generate(self, turn: Any, result: Any) -> CustomerResponse:
        self.results.append(result)
        return CustomerResponse(message="The sectional takes more room; the sofa costs less.")


def _service(sessions: FakeSessionStore) -> tuple[CardComparisonService, Responses]:
    repository = cast(ProductRepository, FakeRepository([_product(pid) for pid in KINDS]))
    responses = Responses()
    return (
        CardComparisonService(
            ProductReferenceResolver(repository, load_catalog_attributes()),
            ProductComparisonService(
                repository,
                load_dimension_semantics(taxonomy=TAXONOMY),
                CustomerAgentSettings(),
            ),
            responses,  # type: ignore[arg-type]
            sessions,  # type: ignore[arg-type]
            GROUPS,
        ),
        responses,
    )


def _request(*cards: tuple[int, int]) -> CardComparisonRequest:
    return CardComparisonRequest(
        session_id=SESSION,
        store_id=STORE,
        cards=tuple(CardRef(list_revision=r, ordinal=o) for r, o in cards),
    )


# ── which products are similar ══════════════════════════════════════════════


def test_a_sofa_compares_with_a_sectional_but_not_a_coffee_table() -> None:
    assert GROUPS.comparable("sofa", "sectional-sofa")
    assert GROUPS.comparable("single-seater-sofa", "lounge-chair")
    assert GROUPS.comparable("carpet", "carpet")
    assert not GROUPS.comparable("sectional-sofa", "center-table")
    assert not GROUPS.comparable("sofa", "chair")
    assert not GROUPS.comparable(None, None)


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("lonely: [sofa]", "two or more types"),
        ("made_up: [sofa, not-a-type]", "not an approved subcategory"),
        ("a: [sofa, sofa-set]\n  b: [sofa, sofa-bed]", "already in a"),
    ],
)
def test_a_malformed_group_fails_startup(tmp_path: Path, body: str, problem: str) -> None:
    source = tmp_path / "groups.yaml"
    source.write_text(f"version: v1\ngroups:\n  {body}\n", encoding="utf-8")

    with pytest.raises(TaxonomyConfigurationError) as refused:
        load_compare_groups(source, taxonomy=TAXONOMY)

    assert problem in str(refused.value.context.get("detail"))


# ── the pop-up ══════════════════════════════════════════════════════════════


async def test_two_similar_cards_compare_and_nothing_is_saved() -> None:
    sessions = FakeSessionStore()
    stored = await _stored(sessions, _state())
    service, responses = _service(sessions)

    reply = await service.compare(_request((1, 1), (1, 3)), CONTEXT)

    assert [p.commerce.subcategory for p in reply.comparison.products] == [
        "sofa",
        "sectional-sofa",
    ]
    assert reply.message.startswith("The sectional")
    # Worded as a comparison turn is, from the same table.
    assert responses.results[0].grounding.comparison == reply.comparison
    # A look, not a turn: the session is exactly as it was.
    assert sessions.saved[(STORE, SESSION)] == stored


async def test_cards_on_different_lists_can_be_compared() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())
    service, _ = _service(sessions)

    reply = await service.compare(_request((1, 2), (1, 1)), CONTEXT)

    assert [p.name_english for p in reply.comparison.products] == ["Sofa 102", "Sofa 101"]


async def test_a_sofa_and_a_coffee_table_are_refused() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())
    service, responses = _service(sessions)

    with pytest.raises(ComparisonRefusedError) as refused:
        await service.compare(_request((1, 3), (2, 1)), CONTEXT)

    assert refused.value.context["reason"] == "dissimilar"
    assert "same kind" in refused.value.public_message
    assert responses.results == []


@pytest.mark.parametrize("cards", [((9, 1), (1, 1)), ((1, 9), (1, 1))])
async def test_a_card_no_longer_on_screen_is_refused(cards: tuple[tuple[int, int], ...]) -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())
    service, _ = _service(sessions)

    with pytest.raises(ComparisonRefusedError) as refused:
        await service.compare(_request(*cards), CONTEXT)

    assert "on screen" in refused.value.public_message


async def test_with_no_session_there_is_nothing_to_compare() -> None:
    service, _ = _service(FakeSessionStore())

    with pytest.raises(ComparisonRefusedError):
        await service.compare(_request((1, 1), (1, 2)), CONTEXT)


def test_a_comparison_is_of_two_different_cards() -> None:
    with pytest.raises(ValidationError):
        _request((1, 1), (1, 1))
    with pytest.raises(ValidationError):
        CardComparisonRequest.model_validate(
            {
                "session_id": SESSION,
                "store_id": STORE,
                "cards": [{"list_revision": 1, "ordinal": n} for n in (1, 2, 3)],
            }
        )


# ── the routes ══════════════════════════════════════════════════════════════


class Resources:
    compare_groups = GROUPS


def _app(service: CardComparisonService) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(comparisons_router, prefix="/v1")
    app.dependency_overrides[card_comparison_service] = lambda: service
    app.dependency_overrides[retailer_context_provider] = lambda: FakeRetailers()
    app.dependency_overrides[resources] = lambda: Resources()
    return app


async def _call(app: FastAPI, method: str, path: str, body: Any = None) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, json=body)


async def test_the_route_answers_with_the_table_and_the_take() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())
    service, _ = _service(sessions)

    reply = await _call(
        _app(service),
        "POST",
        "/v1/comparisons",
        {
            "session_id": SESSION,
            "store_id": STORE,
            "cards": [{"list_revision": 1, "ordinal": 1}, {"list_revision": 1, "ordinal": 3}],
        },
    )

    assert reply.status_code == 200
    body = reply.json()
    assert len(body["comparison"]["products"]) == 2
    assert body["message"]


async def test_the_route_refuses_dissimilar_cards_in_our_words() -> None:
    sessions = FakeSessionStore()
    await _stored(sessions, _state())
    service, _ = _service(sessions)

    reply = await _call(
        _app(service),
        "POST",
        "/v1/comparisons",
        {
            "session_id": SESSION,
            "store_id": STORE,
            "cards": [{"list_revision": 1, "ordinal": 1}, {"list_revision": 2, "ordinal": 1}],
        },
    )

    assert reply.status_code == 422
    assert reply.json()["error"]["code"] == "comparison_refused"


async def test_the_groups_route_lists_the_reviewed_families() -> None:
    service, _ = _service(FakeSessionStore())

    reply = await _call(_app(service), "GET", "/v1/compare-groups")

    assert reply.status_code == 200
    groups = reply.json()["groups"]
    assert groups["sectional-sofa"] == groups["sofa"] == "sofas"
    assert "center-table" not in groups
