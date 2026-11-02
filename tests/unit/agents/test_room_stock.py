"""Stock, where the store says it (known issue 37).

Stock never chooses a product: a room piece goes to the best look as before.
A piece wanted in more than one is the same product first, as far as the
store has it, and the closest look for the rest. A cart line never asks for
more than the store has, and the cut is said.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from app.agents.operations.cart import CartOperations, CartRefused
from app.agents.tools.shopping import in_stock, refusal_facts
from app.schemas.agent_state import AgentStateV1
from app.schemas.cart import CartRefusalReason
from app.schemas.design import DesignPriority, PieceDesign
from app.schemas.product import ProductListing
from app.services.store_cart_check import CartCheck
from pydantic import ValidationError

from tests.unit.agents import test_room_engine as room
from tests.unit.agents.test_room_engine import CONTEXT, _engine, _inputs, _need


@pytest.fixture(autouse=True)
def _no_stock_unless_said() -> Iterator[None]:
    room.STOCK.clear()
    yield
    room.STOCK.clear()


PAIR = (_need("seating", "lounge-chair", "reading chairs").model_copy(update={"quantity": 2}),)
"""Two lounge chairs: 41 (900, the first by look here) and 42 (1400)."""


def _lines(outcome: object) -> list[tuple[int, int]]:
    state = outcome.state  # type: ignore[attr-defined]
    assert state.room_project is not None
    return sorted((line.product_id, line.quantity) for line in state.room_project.bundle_items)


# ── the room ────────────────────────────────────────────────────────────────


async def test_a_pair_is_the_same_product_when_the_store_has_enough() -> None:
    room.STOCK[41] = 5
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=PAIR))

    assert _lines(outcome) == [(41, 2)]


async def test_unknown_stock_never_limits_a_piece() -> None:
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=PAIR))

    assert _lines(outcome) == [(41, 2)]


async def test_one_left_keeps_it_and_the_closest_look_fills_the_rest() -> None:
    room.STOCK[41] = 1
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=PAIR))

    assert _lines(outcome) == [(41, 1), (42, 1)]
    needs = outcome.state.room_project.design_needs  # type: ignore[union-attr]
    assert [(n.commerce_subcategory, n.quantity) for n in needs] == [
        ("lounge-chair", 1),
        ("lounge-chair", 1),
    ], "two pieces of the same kind, each with one product"
    assert outcome.facts["total"] == Decimal(2300)


async def test_stock_never_chooses_the_product() -> None:
    """42 has plenty, 41 one: the first is still 41, the better look."""
    room.STOCK.update({41: 1, 42: 9})
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=PAIR))

    assert (41, 1) in _lines(outcome)


async def test_a_short_piece_beside_others_leaves_them_as_they_were() -> None:
    room.STOCK[41] = 1
    needs = (*room.ROOM, *PAIR)
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=needs))

    assert _lines(outcome) == [(1, 1), (10, 1), (20, 1), (41, 1), (42, 1)]


async def test_a_rest_the_budget_cannot_carry_leaves_only_what_is_in_stock() -> None:
    """2000 buys two of the 900 chair, not one of each: the second is missing,
    never a third of the chair the store does not have."""
    room.STOCK[41] = 1
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(budget="2000", needs=PAIR))

    assert _lines(outcome) == [(41, 1)]


async def test_a_nice_to_have_pair_splits_alike() -> None:
    room.STOCK[41] = 1
    optional = _need("seating", "lounge-chair", "reading chairs", DesignPriority.OPTIONAL)
    pair = (optional.model_copy(update={"quantity": 2}),)
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=pair))

    assert _lines(outcome) == [(41, 1), (42, 1)]


# ── the cart ────────────────────────────────────────────────────────────────


def _cart() -> CartOperations:
    return CartOperations(max_picks=10, max_quantity=10, max_likes=20, check=None)


async def test_a_cart_line_never_asks_for_more_than_the_store_has() -> None:
    change = await _cart().add(AgentStateV1(), CONTEXT, [(41, 2)], stock={41: 1})

    assert change.added == [41]
    assert change.state.product_interaction.quantity_of(41) == 1
    assert change.refused == [CartRefused(41, "only_left", 1, asked=2)]


async def test_enough_stock_or_none_said_takes_what_was_asked() -> None:
    change = await _cart().add(AgentStateV1(), CONTEXT, [(41, 2), (42, 3)], stock={41: 5})

    assert change.refused == []
    interaction = change.state.product_interaction
    assert (interaction.quantity_of(41), interaction.quantity_of(42)) == (2, 3)


async def test_a_line_already_at_the_stock_is_already_in_the_cart() -> None:
    first = await _cart().add(AgentStateV1(), CONTEXT, [(41, 1)])

    again = await _cart().add(first.state, CONTEXT, [(41, 2)], stock={41: 1})

    assert again.already == [41]
    assert again.refused == [CartRefused(41, "only_left", 1, asked=2)]


def test_the_cut_is_told_with_how_many_were_asked_and_kept() -> None:
    facts = refusal_facts(
        [CartRefused(41, "only_left", 1, asked=2), CartRefused(42, "out_of_stock", 0)],
        {41: "lounge-chair 41", 42: "lounge-chair 42"},
    )

    assert facts == [
        {"name": "lounge-chair 41", "reason": "only_left", "kept": 1, "asked": 2},
        {"name": "lounge-chair 42", "reason": "out_of_stock", "kept": 0},
    ]


def test_stock_is_read_only_where_the_store_said_it() -> None:
    room.STOCK[41] = 3

    assert in_stock([room._product(41), room._product(42)]) == {41: 3}


def test_stock_never_reaches_a_client() -> None:
    listing = ProductListing(stock_quantity=4)

    assert "stock_quantity" not in listing.model_dump()
    with pytest.raises(ValidationError):
        ProductListing(stock_quantity=0)


# ── a product used across the room ──────────────────────────────────────────

TWO_CHAIRS = (
    _need("seating", "lounge-chair", "reading chair"),
    _need("seating", "lounge-chair", "second reading chair"),
)


async def test_two_pieces_of_one_kind_never_share_the_last_one() -> None:
    room.STOCK[41] = 1
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=TWO_CHAIRS))

    assert _lines(outcome) == [(41, 1), (42, 1)]


async def test_a_piece_found_again_never_takes_what_the_room_used_up() -> None:
    """Seen live: revising the second art piece chose the first one's print,
    of which the store had one - the room showed two, the cart took one."""
    room.STOCK[41] = 1
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=TWO_CHAIRS))
    pieces = planned.facts["pieces"]
    assert isinstance(pieces, list)
    second = next(p["piece"] for p in pieces if "42" in str(p.get("name")))

    revised = await engine.revise_piece(
        planned.state, CONTEXT, second, PieceDesign(role="second reading chair")
    )

    assert revised.committed
    assert _lines(revised) == [(41, 1), (42, 1)]


async def test_with_enough_stock_two_pieces_may_be_the_same_product() -> None:
    room.STOCK[41] = 2
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=TWO_CHAIRS))

    assert _lines(outcome) == [(41, 1), (41, 1)]


# ── after a change (QA findings on known issue 37) ──────────────────────────


async def test_a_split_pair_shares_its_budget_between_its_halves() -> None:
    room.STOCK[41] = 1
    engine, _, _ = _engine()
    pair = (PAIR[0].model_copy(update={"design": PieceDesign(role="pair", share_percent=40)}),)

    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=pair))

    needs = planned.state.room_project.design_needs  # type: ignore[union-attr]
    assert [n.design.share_percent for n in needs if n.design] == [20, 20]


async def test_a_revised_pair_is_the_same_product_first_then_the_closest() -> None:
    room.STOCK.update({41: 5, 42: 1})
    engine, _, pipeline = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=PAIR))
    assert _lines(planned) == [(41, 2)]
    pipeline.reverse = True  # the search now ranks 42 first

    revised = await engine.revise_piece(planned.state, CONTEXT, 1, PieceDesign(role="pair"))

    assert revised.committed
    assert _lines(revised) == [(41, 1), (42, 1)], "one of 42, the closest look for the other"


async def test_a_pair_swapped_to_a_last_one_keeps_it_and_adds_the_closest() -> None:
    room.STOCK.update({41: 5, 42: 1})
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=PAIR))
    options = await engine.piece_options(planned.state, CONTEXT, 1)
    names = [o["name"] for o in options.facts["options"]]  # type: ignore[union-attr]
    pick = next(n for n, name in enumerate(names, start=1) if "42" in name)

    taken = await engine.swap(options.state, CONTEXT, 1, pick, allow_over_budget=True)

    assert taken.committed
    assert _lines(taken) == [(41, 1), (42, 1)]


async def test_nora_knows_quietly_which_piece_is_the_last_of_its_product() -> None:
    room.STOCK[41] = 1
    engine, _, _ = _engine()

    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=PAIR))

    pieces = planned.facts["pieces"]
    assert isinstance(pieces, list)
    marked = {p["name"]: p.get("no_more_in_store", False) for p in pieces}
    assert marked == {"lounge-chair 41": True, "lounge-chair 42": False}


class _StoreRefuses:
    async def refusals(self, wanted, context):  # type: ignore[no-untyped-def]
        return {
            p: CartCheck(
                product_id=p,
                name_english="x",
                name_arabic=None,
                takes=0,
                reason=CartRefusalReason.OUT_OF_STOCK,
            )
            for p in wanted
        }


async def test_one_note_per_product_with_what_they_asked_for() -> None:
    cart = CartOperations(max_picks=10, max_quantity=10, max_likes=20, check=_StoreRefuses())  # type: ignore[arg-type]

    change = await cart.add(AgentStateV1(), CONTEXT, [(41, 2)], stock={41: 1})

    assert change.refused == [CartRefused(41, "out_of_stock", 0, asked=2)]


async def test_one_product_in_place_of_a_pair_keeps_the_pair() -> None:
    """Seen live: "make both nightstands the same one" left one nightstand."""
    room.STOCK.update({41: 1, 42: 9})
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=TWO_CHAIRS))
    pieces = planned.facts["pieces"]
    assert isinstance(pieces, list) and len(pieces) == 2

    changed = await engine.change(
        planned.state,
        CONTEXT,
        remove_pieces=[p["piece"] for p in pieces],
        add_products=[42],
        allow_over_budget=True,
    )

    assert changed.committed
    assert _lines(changed) == [(42, 2)]


async def test_one_last_product_in_place_of_a_pair_is_kept_beside_its_closest_match() -> None:
    room.STOCK.update({41: 1, 42: 1})
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=TWO_CHAIRS))
    pieces = planned.facts["pieces"]
    assert isinstance(pieces, list)

    changed = await engine.change(
        planned.state,
        CONTEXT,
        remove_pieces=[p["piece"] for p in pieces],
        add_products=[42],
        allow_over_budget=True,
    )

    assert _lines(changed) == [(41, 1), (42, 1)], "never one of a pair alone"


async def test_two_products_in_place_of_a_pair_take_one_each() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=PAIR))
    assert _lines(planned) == [(41, 2)]

    changed = await engine.change(
        planned.state, CONTEXT, remove_pieces=[1], add_products=[41, 42], allow_over_budget=True
    )

    assert _lines(changed) == [(41, 1), (42, 1)]


# ── a kind never bought twice (known issue 38) ──────────────────────────────

TWO_ART = (
    _need("decor", "art-canvas", "feature artwork"),
    _need("decor", "art-canvas", "companion artwork"),
)


async def test_two_pieces_of_art_are_two_different_prints() -> None:
    """Seen live on store 21: both art pieces took the same best print."""
    room.STOCK.update({50: 9, 51: 9})
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=TWO_ART))

    assert _lines(outcome) == [(50, 1), (51, 1)]


async def test_a_print_is_never_two_even_with_stock_unknown() -> None:
    engine, _, _ = _engine()

    outcome = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=TWO_ART))

    assert _lines(outcome) == [(50, 1), (51, 1)]


async def test_one_print_in_place_of_two_pieces_is_kept_beside_another() -> None:
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=TWO_ART))
    pieces = planned.facts["pieces"]
    assert isinstance(pieces, list)

    changed = await engine.change(
        planned.state,
        CONTEXT,
        remove_pieces=[p["piece"] for p in pieces],
        add_products=[51],
        allow_over_budget=True,
    )

    assert _lines(changed) == [(50, 1), (51, 1)]


# ── after a change, re-reviewed (QA findings, second round) ─────────────────


@pytest.fixture
def chair_43() -> Iterator[None]:
    """A third lounge chair, dearer than the first and cheaper than 42."""
    room.KINDS[43] = ("seating", "lounge-chair", "1000")
    yield
    room.KINDS.pop(43, None)


ART = _need("decor", "art-canvas", "art", DesignPriority.OPTIONAL)


@pytest.mark.usefixtures("chair_43")
async def test_an_approved_swap_is_what_they_were_quoted_nothing_dropped() -> None:
    """Told a total for the swap and said yes: the room saved is that room,
    the closest match for the second seat included and no piece dropped."""
    room.STOCK.update({41: 5, 43: 1})
    engine, _, pipeline = _engine()
    planned = await engine.design(
        AgentStateV1(), CONTEXT, _inputs(budget="2100", needs=(PAIR[0], ART))
    )
    pipeline.reverse = True
    options = await engine.piece_options(planned.state, CONTEXT, 1)
    names = [o["name"] for o in options.facts["options"]]  # type: ignore[union-attr]
    pick = next(n for n, name in enumerate(names, start=1) if "43" in name)

    held = await engine.swap(options.state, CONTEXT, 1, pick, allow_over_budget=False)
    taken = await engine.swap(options.state, CONTEXT, 1, pick, allow_over_budget=True)

    assert held.code == "over_budget" and held.bundle is not None
    assert taken.committed and taken.bundle is not None
    assert taken.bundle.new_spend_total == held.bundle.new_spend_total, "quoted is what is saved"
    assert _lines(taken) == [(42, 1), (43, 1), (50, 1)]


@pytest.mark.usefixtures("chair_43")
async def test_a_revised_set_of_three_never_asks_for_more_than_the_store_has() -> None:
    room.STOCK.update({41: 5, 42: 1, 43: 1})
    engine, _, pipeline = _engine()
    trio = (_need("seating", "lounge-chair", "chairs").model_copy(update={"quantity": 3}),)
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(budget="20000", needs=trio))
    pipeline.reverse = True

    revised = await engine.revise_piece(planned.state, CONTEXT, 1, PieceDesign(role="chairs"))

    lines = _lines(revised)
    assert sum(q for _, q in lines) == 3, "still three chairs"
    assert all(q <= (room.STOCK.get(p) or q) for p, q in lines), lines


@pytest.mark.usefixtures("chair_43")
async def test_what_they_turned_down_stays_turned_down_after_a_split() -> None:
    room.STOCK.update({41: 5, 42: 1})
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=PAIR))
    options = await engine.piece_options(planned.state, CONTEXT, 1)
    names = [o["name"] for o in options.facts["options"]]  # type: ignore[union-attr]
    pick = next(n for n, name in enumerate(names, start=1) if "42" in name)

    taken = await engine.swap(options.state, CONTEXT, 1, pick, allow_over_budget=True)

    needs = taken.state.room_project.design_needs  # type: ignore[union-attr]
    assert len(needs) == 2, "split"
    assert all(41 in n.rejected_product_ids for n in needs), "41 was turned down for both"
    assert 41 not in [p for p, _ in _lines(taken)], "never back as the closest match"


async def test_two_products_for_two_pieces_keep_each_pieces_own_design() -> None:
    room.STOCK[41] = 1
    engine, _, _ = _engine()
    planned = await engine.design(AgentStateV1(), CONTEXT, _inputs(needs=TWO_CHAIRS))
    pieces = planned.facts["pieces"]
    assert isinstance(pieces, list)

    changed = await engine.change(
        planned.state,
        CONTEXT,
        remove_pieces=[p["piece"] for p in pieces],
        add_products=[42, 41],
        allow_over_budget=True,
    )

    roles = sorted(
        n.design.role
        for n in changed.state.room_project.design_needs  # type: ignore[union-attr]
        if n.design
    )
    assert roles == ["reading chair", "second reading chair"]
