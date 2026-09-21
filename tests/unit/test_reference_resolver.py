"""Resolving a safe selector into exactly one product, or refusing.

The behaviour these tests exist for is the refusals. Resolving "the second
one" is arithmetic; declining to answer "the beige one" when two products are
beige is the part that keeps the system honest, and it is the part a careless
implementation gets wrong by taking the first match.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest
from app.repositories.products import ProductRepository
from app.schemas.agent_decision import (
    ExtremumDirection,
    FocusedProduct,
    PresentedAttributeMatch,
    PresentedExtremum,
    PresentedOrdinal,
    ProductReferenceSelector,
    SoleSelectedProduct,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    ProductInteractionState,
)
from app.schemas.dimensions import RawDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import CommerceClassification, ProductRow
from app.schemas.resolution import (
    ReferenceFailureReason,
    ReferenceUnresolved,
    ResolvedProductReference,
)
from app.schemas.retailer import RetailerContext
from app.services.reference_resolver import ProductReferenceResolver
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes

CONTEXT = RetailerContext(store_id=50)
OTHER = RetailerContext(store_id=60)


def _row(
    product_id: int,
    *,
    store_id: int = 50,
    color: str | None = "Beige",
    styles: tuple[str, ...] = ("Modern",),
    price: str = "1000",
    unit: str = "SAR",
) -> ProductRow:
    return ProductRow(
        id=product_id,
        uuid=uuid4(),
        store_id=store_id,
        name_english=f"Sofa {product_id}",
        name_arabic="كنبة",
        price_amount=Decimal(price),
        price_unit=unit,
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        visual_category="3-seater-sofa",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=RawDimensions(unit="cm"),
        main_color=color,
        styles=styles,
        is_active=True,
    )


class FakeRepository:
    """Store-scoped by construction, exactly as the real one is."""

    def __init__(self, rows: list[ProductRow]) -> None:
        self.rows = rows
        self.calls: list[tuple[list[int], RetailerContext]] = []

    async def get_by_ids(
        self, product_ids: Any, context: RetailerContext
    ) -> list[ProductRow]:
        wanted = set(product_ids)
        self.calls.append((sorted(wanted), context))
        return [
            r for r in self.rows if r.id in wanted and r.store_id == context.store_id
        ]


def _resolver(rows: list[ProductRow]) -> tuple[ProductReferenceResolver, FakeRepository]:
    repository = FakeRepository(rows)
    return (
        ProductReferenceResolver(
            cast(ProductRepository, repository), load_catalog_attributes()
        ),
        repository,
    )


def _state(
    *,
    presented: tuple[int, ...] = (),
    revision: int | None = None,
    focused: int | None = None,
    selected: tuple[int, ...] = (),
) -> AgentStateV1:
    committed = revision if revision is not None else (1 if presented else None)
    # M10: a presented list implies a committed revision of at least one.
    committed = committed or None
    return AgentStateV1(
        active_search=ActiveSearchState(
            request=ProductSearchRequest(commerce_category="seating"),
            revision=committed or 0,
        ),
        product_interaction=ProductInteractionState(
            presented_product_ids=presented,
            presented_search_revision=committed,
            focused_product_id=focused,
            selected_product_ids=selected,
        ),
    )


async def _resolve(
    selector: ProductReferenceSelector,
    state: AgentStateV1,
    rows: list[ProductRow],
    context: RetailerContext = CONTEXT,
) -> Any:
    resolver, _ = _resolver(rows)
    return await resolver.resolve(selector, state, context)


def _reason(outcome: Any) -> ReferenceFailureReason:
    assert isinstance(outcome, ReferenceUnresolved), outcome
    return outcome.reason


# ── PresentedOrdinal ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("position", "expected"), [(1, 11), (2, 22), (3, 33)])
async def test_an_ordinal_counts_the_presented_list(
    position: int, expected: int
) -> None:
    outcome = await _resolve(
        PresentedOrdinal(position=position),
        _state(presented=(11, 22, 33)),
        [_row(11), _row(22), _row(33)],
    )

    assert outcome == ResolvedProductReference(product_id=expected)


async def test_an_ordinal_past_the_end_is_refused() -> None:
    outcome = await _resolve(
        PresentedOrdinal(position=4), _state(presented=(11, 22, 33)), [_row(11)]
    )

    assert _reason(outcome) is ReferenceFailureReason.ORDINAL_OUT_OF_RANGE


async def test_an_ordinal_needs_a_committed_result_set() -> None:
    """Criteria without results give nothing to count into."""
    outcome = await _resolve(
        PresentedOrdinal(position=1), _state(presented=(), revision=0), []
    )

    assert _reason(outcome) is ReferenceFailureReason.NO_PRESENTED_RESULTS


async def test_an_ordinal_over_an_empty_presented_set_is_refused() -> None:
    outcome = await _resolve(PresentedOrdinal(position=1), _state(), [])

    assert _reason(outcome) is ReferenceFailureReason.NO_PRESENTED_RESULTS


async def test_a_stale_ordinal_never_slides_to_a_neighbour() -> None:
    """Product 22 is gone. The answer is "unavailable", not product 33."""
    outcome = await _resolve(
        PresentedOrdinal(position=2),
        _state(presented=(11, 22, 33)),
        [_row(11), _row(33)],
    )

    assert _reason(outcome) is ReferenceFailureReason.PRODUCT_UNAVAILABLE


# ── FocusedProduct ──────────────────────────────────────────────────────────


async def test_a_focused_product_resolves() -> None:
    outcome = await _resolve(
        FocusedProduct(), _state(presented=(11,), focused=11), [_row(11)]
    )

    assert outcome == ResolvedProductReference(product_id=11)


async def test_no_focus_is_refused() -> None:
    outcome = await _resolve(FocusedProduct(), _state(presented=(11,)), [_row(11)])

    assert _reason(outcome) is ReferenceFailureReason.NO_FOCUSED_PRODUCT


async def test_a_stale_focus_is_unavailable() -> None:
    outcome = await _resolve(
        FocusedProduct(), _state(presented=(11,), focused=11), []
    )

    assert _reason(outcome) is ReferenceFailureReason.PRODUCT_UNAVAILABLE


async def test_focus_needs_no_presentation_lineage() -> None:
    """A focus outlives the result set it came from."""
    outcome = await _resolve(
        FocusedProduct(),
        _state(selected=(11,), focused=11),
        [_row(11)],
    )

    assert outcome == ResolvedProductReference(product_id=11)


# ── SoleSelectedProduct ─────────────────────────────────────────────────────


async def test_one_selection_resolves() -> None:
    outcome = await _resolve(
        SoleSelectedProduct(), _state(selected=(11,)), [_row(11)]
    )

    assert outcome == ResolvedProductReference(product_id=11)


async def test_no_selection_is_refused() -> None:
    outcome = await _resolve(SoleSelectedProduct(), _state(), [])

    assert _reason(outcome) is ReferenceFailureReason.NO_SELECTED_PRODUCT


async def test_several_selections_are_ambiguous_not_first_wins() -> None:
    outcome = await _resolve(
        SoleSelectedProduct(), _state(selected=(11, 22)), [_row(11), _row(22)]
    )

    assert _reason(outcome) is ReferenceFailureReason.SEVERAL_SELECTED_PRODUCTS


async def test_a_stale_selection_is_unavailable() -> None:
    outcome = await _resolve(SoleSelectedProduct(), _state(selected=(11,)), [])

    assert _reason(outcome) is ReferenceFailureReason.PRODUCT_UNAVAILABLE


# ── PresentedAttributeMatch ─────────────────────────────────────────────────


async def test_a_unique_colour_match_resolves() -> None:
    outcome = await _resolve(
        PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Beige"),
        _state(presented=(11, 22)),
        [_row(11, color="Beige"), _row(22, color="Taupe")],
    )

    assert outcome == ResolvedProductReference(product_id=11)


async def test_a_unique_style_match_resolves() -> None:
    outcome = await _resolve(
        PresentedAttributeMatch(family=AttributeFamily.STYLE, value="Japandi"),
        _state(presented=(11, 22)),
        [_row(11, styles=("Modern",)), _row(22, styles=("Japandi", "Modern"))],
    )

    assert outcome == ResolvedProductReference(product_id=22)


async def test_no_colour_match_is_refused() -> None:
    outcome = await _resolve(
        PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Beige"),
        _state(presented=(11,)),
        [_row(11, color="Taupe")],
    )

    assert _reason(outcome) is ReferenceFailureReason.NO_ATTRIBUTE_MATCH


async def test_two_colour_matches_are_ambiguous_not_first_wins() -> None:
    """"The beige one" over two beige products is a question, not a pick."""
    outcome = await _resolve(
        PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Beige"),
        _state(presented=(11, 22)),
        [_row(11, color="Beige"), _row(22, color="Beige")],
    )

    assert _reason(outcome) is ReferenceFailureReason.SEVERAL_ATTRIBUTE_MATCHES


async def test_an_unapproved_selector_value_never_reaches_a_comparison() -> None:
    outcome = await _resolve(
        PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Neon"),
        _state(presented=(11,)),
        [_row(11)],
    )

    assert _reason(outcome) is ReferenceFailureReason.UNAPPROVED_ATTRIBUTE_VALUE


async def test_a_style_token_is_matched_exactly_not_by_substring() -> None:
    outcome = await _resolve(
        PresentedAttributeMatch(family=AttributeFamily.STYLE, value="Modern"),
        _state(presented=(11, 22)),
        [_row(11, styles=("Modern_Classic",)), _row(22, styles=("Modern",))],
    )

    assert outcome == ResolvedProductReference(product_id=22)


async def test_a_stale_member_prevents_matching_against_a_shorter_list() -> None:
    """Their phrase refers to the list they saw; a shorter one may differ."""
    outcome = await _resolve(
        PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Beige"),
        _state(presented=(11, 22)),
        [_row(11, color="Beige")],
    )

    assert _reason(outcome) is ReferenceFailureReason.PRESENTED_SET_INCOMPLETE


async def test_an_attribute_match_needs_a_presented_set() -> None:
    outcome = await _resolve(
        PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Beige"),
        _state(),
        [],
    )

    assert _reason(outcome) is ReferenceFailureReason.NO_PRESENTED_RESULTS


# ── PresentedExtremum ───────────────────────────────────────────────────────


async def test_the_unique_cheapest_resolves() -> None:
    outcome = await _resolve(
        PresentedExtremum(direction=ExtremumDirection.LOWEST),
        _state(presented=(11, 22, 33)),
        [_row(11, price="2000"), _row(22, price="990"), _row(33, price="1500")],
    )

    assert outcome == ResolvedProductReference(product_id=22)


async def test_the_unique_dearest_resolves() -> None:
    outcome = await _resolve(
        PresentedExtremum(direction=ExtremumDirection.HIGHEST),
        _state(presented=(11, 22)),
        [_row(11, price="2000"), _row(22, price="990")],
    )

    assert outcome == ResolvedProductReference(product_id=11)


@pytest.mark.parametrize(
    "direction", [ExtremumDirection.LOWEST, ExtremumDirection.HIGHEST]
)
async def test_a_tie_is_ambiguous_never_the_first_ordinal(
    direction: ExtremumDirection,
) -> None:
    outcome = await _resolve(
        PresentedExtremum(direction=direction),
        _state(presented=(11, 22)),
        [_row(11, price="1000"), _row(22, price="1000")],
    )

    assert _reason(outcome) is ReferenceFailureReason.TIED_EXTREMUM


async def test_mixed_currencies_have_no_cheapest() -> None:
    """Nothing converts, so SAR 100 against USD 100 is not a comparison."""
    outcome = await _resolve(
        PresentedExtremum(direction=ExtremumDirection.LOWEST),
        _state(presented=(11, 22)),
        [_row(11, price="900", unit="SAR"), _row(22, price="100", unit="USD")],
    )

    assert _reason(outcome) is ReferenceFailureReason.MIXED_CURRENCY_PRESENTATION


async def test_a_stale_member_prevents_a_partial_extremum() -> None:
    """The cheapest of what survived may not be the cheapest they saw."""
    outcome = await _resolve(
        PresentedExtremum(direction=ExtremumDirection.LOWEST),
        _state(presented=(11, 22)),
        [_row(11, price="2000")],
    )

    assert _reason(outcome) is ReferenceFailureReason.PRESENTED_SET_INCOMPLETE


# ── retailer scope ──────────────────────────────────────────────────────────


async def test_another_retailers_product_never_resolves() -> None:
    """Indistinguishable from deleted, so a refusal reveals nothing."""
    outcome = await _resolve(
        PresentedOrdinal(position=1),
        _state(presented=(11,)),
        [_row(11, store_id=60)],
    )

    assert _reason(outcome) is ReferenceFailureReason.PRODUCT_UNAVAILABLE


@pytest.mark.parametrize(
    "selector",
    [
        PresentedOrdinal(position=1),
        FocusedProduct(),
        SoleSelectedProduct(),
        PresentedAttributeMatch(family=AttributeFamily.COLOR, value="Beige"),
        PresentedExtremum(direction=ExtremumDirection.LOWEST),
    ],
)
async def test_every_lookup_carries_the_request_scope(
    selector: ProductReferenceSelector,
) -> None:
    resolver, repository = _resolver([_row(11)])

    await resolver.resolve(
        selector, _state(presented=(11,), focused=11, selected=(11,)), CONTEXT
    )

    assert repository.calls
    assert all(context is CONTEXT for _, context in repository.calls)


def test_every_catalog_read_passes_the_request_scope() -> None:
    """Parsed, so the guard checks call sites rather than counting text."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).parents[2] / "app/services/reference_resolver.py"
    ).read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_by_ids"
    ]

    assert calls, "the resolver must read the catalog somewhere"
    for call in calls:
        assert len(call.args) == 2, ast.unparse(call)
        scope = call.args[1]
        assert isinstance(scope, ast.Name) and scope.id == "context", ast.unparse(call)


def test_the_resolver_never_names_a_store() -> None:
    """Scope arrives resolved; nothing here may choose or widen it."""
    from pathlib import Path

    source = (
        Path(__file__).parents[2] / "app/services/reference_resolver.py"
    ).read_text()
    logging_only = source.replace("store_id=context.store_id", "")

    assert "store_id" not in logging_only
    assert "RetailerContext(" not in source
