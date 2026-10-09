"""The designer's direction for what goes with a pick, and sizes read with no
convention (docs/designer-led-shopping-plan.md, phase 4).

After a pick the designer names a direction for the suggested kind - colours
and styles that kind comes in at this store, what to avoid, a size proportion -
and the cards are ordered by it, never filtered. The customer's own words come
first. Sizes are the longer and shorter floor side, whichever column holds
them, and a kind is long and shallow only where the store's own data says so.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.core.config import SizeSettings
from app.repositories.products import CatalogOverviewRow
from app.schemas.catalog_overview import CatalogOverview, SubcategoryShelf
from app.schemas.design import (
    DesignCategoryNeed,
    DesignDirection,
    InteriorDesignRequest,
    InteriorDesignResult,
    StockedLook,
)
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import ProductSearchRequest
from app.schemas.product import EligibleProduct
from app.schemas.product_action import GoesWithPickAction
from app.schemas.query import RankingLean, ResolvedSearch
from app.schemas.relaxation import RelaxedCandidate
from app.schemas.response import DirectionView
from app.services.catalog_capability import _shelf_from_row
from app.services.interior_design import _with_stocked_direction
from app.services.product_size import floor_sides, size_distance
from app.services.response_view import route_response
from app.services.semantic_ranking import _preference_match
from app.services.turn_coordinator import _directed, _DirectionContext
from app.taxonomy.attributes import AttributeFamily

from tests.unit.test_cross_sell_shows_products import (
    RUGS,
    _engine,
    _liking,
    _values,
    _with_taste,
)
from tests.unit.test_product_actions import BED, _state, _turn
from tests.unit.test_turn_coordinator import FakeCapabilities, FakeDesign

SIZE = SizeSettings()


# ── sizes with no convention ════════════════════════════════════════════════


def _dims(length: str | None, width: str | None) -> NormalisedDimensions:
    return NormalisedDimensions(
        length_cm=Decimal(length) if length else None,
        width_cm=Decimal(width) if width else None,
        status=DimensionStatus.NORMALISED,
    )


@pytest.mark.parametrize(("length", "width"), [("220", "95"), ("95", "220")])
def test_the_longer_floor_side_is_found_whichever_column_holds_it(length: str, width: str) -> None:
    sides = floor_sides(_dims(length, width), SIZE)

    assert sides is not None and (sides.long_cm, sides.short_cm) == (220, 95)


@pytest.mark.parametrize(("length", "width"), [("1.5", "170"), ("220", None), ("5000", "90")])
def test_a_missing_or_implausible_side_is_not_read_as_a_size(
    length: str, width: str | None
) -> None:
    assert floor_sides(_dims(length, width), SIZE) is None


def test_size_distance_is_a_share_of_the_target_and_none_without_a_size() -> None:
    assert size_distance(Decimal("150"), Decimal("200")) == Decimal("0.25")
    assert size_distance(None, Decimal("200")) is None
    assert size_distance(Decimal("150"), None) is None


def _row(measured: int, elongated: int) -> CatalogOverviewRow:
    return CatalogOverviewRow(
        commerce_category="seating",
        commerce_subcategory="sofa",
        active_count=max(measured, 1),
        seat_known=0,
        seat_minimum=None,
        seat_maximum=None,
        price_minimum=Decimal("1000"),
        price_maximum=Decimal("5000"),
        colours=("Beige",),
        price_units=("SAR",),
        styles=("Modern",),
        planar_measured=measured,
        planar_elongated=elongated,
    )


@pytest.mark.parametrize(
    ("measured", "elongated", "expected"),
    [(172, 170, True), (36, 16, False), (5, 5, False), (40, 37, False)],
    ids=["sofas-99%", "beds-44%", "too-few", "just-under"],
)
def test_a_kind_is_long_and_shallow_only_where_the_stores_data_says_so(
    measured: int, elongated: int, expected: bool
) -> None:
    shelf = _shelf_from_row(_row(measured, elongated), None, SIZE)

    assert shelf.long_and_shallow is expected
    assert shelf.styles == ("Modern",)


# ── the direction names only what the shop stocks ═══════════════════════════


LOOKS = (StockedLook(commerce_subcategory="carpet", colours=("Ivory", "Grey"), styles=("Modern",)),)


def _need(direction: DesignDirection) -> DesignCategoryNeed:
    return RUGS.model_copy(update={"direction": direction})


def test_a_direction_keeps_stocked_values_in_their_stored_spelling() -> None:
    need = _need(DesignDirection(colours=("ivory", "Teal"), styles=("modern",), size_ratio=1.2))

    kept = _with_stocked_direction(need, LOOKS).direction

    assert kept is not None
    assert (kept.colours, kept.styles, kept.size_ratio) == (("Ivory",), ("Modern",), 1.2)


def test_a_direction_with_nothing_stocked_left_is_no_direction() -> None:
    need = _need(DesignDirection(colours=("Teal",)))

    assert _with_stocked_direction(need, LOOKS).direction is None


# ── applying it: the customer first, then the designer, then the pick ════════


def _resolved(*styles: str) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(commerce_category="decor", commerce_subcategory="carpet"),
        semantic_preferences=_liking(
            *((AttributeFamily.STYLE, s) for s in styles)
        ).semantic_preferences,
    )


DIRECTION = DesignDirection(
    colours=("Ivory",),
    styles=("Modern",),
    avoid_colours=("Beige",),
    size_ratio=0.66,
)


def test_the_designer_ranks_in_its_own_tier_and_replaces_the_picks_style() -> None:
    context = _DirectionContext(anchor_long_cm=Decimal("240"), expressed=frozenset())

    directed = _directed(_resolved("Scandinavian"), DIRECTION, context)

    assert _values(directed.semantic_preferences) == []
    assert directed.lean == RankingLean(
        colours=("Ivory",),
        styles=("Modern",),
        avoid_colours=("Beige",),
        size_target_cm=Decimal("158"),
    )


def test_a_style_the_customer_stated_stays_theirs() -> None:
    context = _DirectionContext(anchor_long_cm=None, expressed=frozenset({AttributeFamily.STYLE}))

    directed = _directed(_resolved("Boho"), DIRECTION, context)

    assert _values(directed.semantic_preferences) == ["Boho"]
    assert directed.lean is not None and directed.lean.size_target_cm is None


def test_the_designer_never_avoids_what_it_leans_towards() -> None:
    clash = DesignDirection(colours=("Ivory",), avoid_colours=("Ivory", "Beige"))
    context = _DirectionContext(anchor_long_cm=None, expressed=frozenset())

    directed = _directed(_resolved(), clash, context)

    assert directed.lean is not None and directed.lean.avoid_colours == ("Beige",)


def test_a_direction_of_colours_alone_leaves_no_empty_lean() -> None:
    context = _DirectionContext(anchor_long_cm=None, expressed=frozenset())

    assert _directed(_resolved(), DesignDirection(), context).lean is None


def _candidate(
    pid: int, colour: str, long_cm: str | None = None, styles: tuple[str, ...] = ()
) -> RelaxedCandidate:
    return RelaxedCandidate(
        product=EligibleProduct(
            product_id=pid,
            price_amount=Decimal("500"),
            main_color=colour,
            styles=styles,
            long_side_cm=Decimal(long_cm) if long_cm else None,
            short_side_cm=Decimal("60") if long_cm else None,
        ),
        relaxation_depth=0,
    )


def _order(search: ResolvedSearch, cards: list[RelaxedCandidate]) -> list[int]:
    return [c.product.product_id for c in sorted(cards, key=_preference_match(search, SIZE))]


def test_the_customers_colour_leads_the_designers_style() -> None:
    """Most pieces are Modern; a customer's White must still come first."""
    search = ResolvedSearch(
        request=ProductSearchRequest(commerce_category="decor"),
        semantic_preferences=_liking((AttributeFamily.COLOR, "White")).semantic_preferences,
        lean=RankingLean(styles=("Modern",)),
    )

    order = _order(search, [_candidate(1, "Beige", styles=("Modern",)), _candidate(2, "White")])

    assert order == [2, 1]


def test_with_none_of_their_colours_stocked_the_designers_decide() -> None:
    search = ResolvedSearch(
        request=ProductSearchRequest(commerce_category="decor"),
        semantic_preferences=_liking((AttributeFamily.COLOR, "Emerald")).semantic_preferences,
        lean=RankingLean(colours=("Ivory",)),
    )

    assert _order(search, [_candidate(1, "Grey"), _candidate(2, "Ivory")]) == [2, 1]


def test_what_to_avoid_and_the_size_order_the_cards_and_hide_none() -> None:
    search = ResolvedSearch(
        request=ProductSearchRequest(commerce_category="decor"),
        lean=RankingLean(avoid_colours=("Beige",), size_target_cm=Decimal("200")),
    )
    cards = [
        _candidate(1, "Beige", "200"),
        _candidate(2, "Grey", None),
        _candidate(3, "Grey", "390"),
        _candidate(4, "Grey", "210"),
    ]

    assert _order(search, cards) == [4, 3, 2, 1]


# ── end to end, after a pick ════════════════════════════════════════════════


class ShelfCapabilities(FakeCapabilities):
    async def overview(self, context: Any) -> CatalogOverview:
        return CatalogOverview(
            store_id=50,
            shelves=(
                SubcategoryShelf(
                    commerce_category="decor",
                    commerce_subcategory="carpet",
                    active_count=40,
                    price_minimum=Decimal("300"),
                    price_maximum=Decimal("3000"),
                    colours=("Grey", "Ivory"),
                    styles=("Modern",),
                ),
            ),
        )


def _directed_engine(direction: DesignDirection, *, on: bool = True) -> Any:
    return _engine(
        design=FakeDesign(InteriorDesignResult(needs=(_need(direction),))),
        capabilities=ShelfCapabilities(pairs=(("decor", "carpet"), ("bedroom", "wardrobe"))),
        designer_direction=on,
    )


async def test_after_a_pick_the_cards_follow_the_designers_direction() -> None:
    coordinator, design, pipeline = _directed_engine(DIRECTION)
    state = _state(picks=(BED,)).model_copy(
        update={
            "customer_preferences": _state(picks=(BED,)).customer_preferences.model_copy(
                update={"room": "bedroom"}
            )
        }
    )

    result = await coordinator.run(_turn(state, GoesWithPickAction(pick=1)))

    request: InteriorDesignRequest = design.requests[0]
    assert request.room_type == "bedroom"
    assert [look.commerce_subcategory for look in request.stocked_looks] == ["carpet"]
    # The bed's own style gives way to the designer's; nothing the customer said.
    assert pipeline.preferences[0] == ()
    assert result.direction == DirectionView(
        colours=("Ivory",), styles=("Modern",), character="soft and grounding", avoid=("Beige",)
    )
    active = result.state.active_search
    assert active is not None and active.lean is not None
    assert (active.lean.colours, active.lean.avoid_colours) == (("Ivory",), ("Beige",))
    # A bed is not long and shallow here, so its size sets no target.
    assert active.lean.size_target_cm is None
    view = route_response(result).primary
    assert getattr(view, "design_direction", None) == result.direction


async def test_a_colour_they_like_stays_theirs_beside_the_designers() -> None:
    coordinator, _, pipeline = _directed_engine(DIRECTION)
    state = _with_taste(_state(picks=(BED,)), _liking((AttributeFamily.COLOR, "Grey")))

    result = await coordinator.run(_turn(state, GoesWithPickAction(pick=1)))

    assert pipeline.preferences[0] == ("Grey",)
    active = result.state.active_search
    assert active is not None and active.lean is not None and active.lean.colours == ("Ivory",)


async def test_switched_off_a_suggestion_leans_as_before() -> None:
    coordinator, design, pipeline = _directed_engine(DIRECTION, on=False)

    result = await coordinator.run(_turn(_state(picks=(BED,)), GoesWithPickAction(pick=1)))

    assert design.requests[0].stocked_looks == ()
    assert "Ivory" not in pipeline.preferences[0]
    assert result.direction is None


class ShapedCapabilities(FakeCapabilities):
    def __init__(self, shaped: bool) -> None:
        super().__init__()
        self.shaped = shaped

    async def overview(self, context: Any) -> CatalogOverview:
        return CatalogOverview(
            store_id=50,
            shelves=(
                SubcategoryShelf(
                    commerce_category="seating",
                    commerce_subcategory="sofa",
                    active_count=40,
                    price_minimum=Decimal("900"),
                    price_maximum=Decimal("9000"),
                    long_and_shallow=self.shaped,
                ),
            ),
        )


@pytest.mark.parametrize(("shaped", "expected"), [(True, Decimal("261")), (False, None)])
async def test_a_picks_size_counts_only_where_its_kind_passes_the_shape_rule(
    shaped: bool, expected: Decimal | None
) -> None:
    from tests.unit.test_turn_coordinator import _product

    coordinator, _, _ = _engine(capabilities=ShapedCapabilities(shaped), designer_direction=True)
    sofa = _product(1).model_copy(update={"dimensions": _dims("106", "261")})

    context = await coordinator._direction_context(
        sofa, _state(picks=(BED,)), _turn(_state(), None)
    )

    assert context is not None and context.anchor_long_cm == expected
