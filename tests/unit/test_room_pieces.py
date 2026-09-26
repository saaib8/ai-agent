"""A room made of the pieces the customer chose (CLAUDE.md 10.1, 10.3).

The registry says which pieces a living room or a bedroom may hold, and how
much each matters; the store's stock says which can be offered. The questions
before a room is built come one per turn, each at most once, in a fixed order -
and nothing the customer already said is asked again.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.schemas.agent_state import RoomProjectState
from app.schemas.design import DesignCategoryNeed, DesignPriority, InteriorDesignResult
from app.schemas.discovery import PriceConstraint
from app.schemas.query import ConstraintStrength, SemanticPreference
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerCatalogCapability
from app.schemas.room_opener import RoomQuestionKind
from app.services.room_composition import (
    chosen_keys,
    composed_needs,
    default_pieces,
    next_question,
    seating_piece,
)
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import PieceTier, load_room_pieces
from app.taxonomy.seating import load_seating_semantics

TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=SEATING)
LIVING = ROOMS.template("living_room")
BEDROOM = ROOMS.template("bedroom")
assert LIVING is not None and BEDROOM is not None


def _stock(*pairs: tuple[str, str]) -> RetailerCatalogCapabilities:
    return RetailerCatalogCapabilities(
        capabilities=tuple(
            RetailerCatalogCapability(
                commerce_category=category, commerce_subcategory=subcategory, active_product_count=5
            )
            for category, subcategory in dict.fromkeys(pairs)
        )
    )


EVERYTHING = _stock(
    *(
        (piece.commerce_category, subcategory)
        for template in (LIVING, BEDROOM)
        for piece in template.pieces
        for subcategory in piece.types
    )
)

BEIGE = SemanticPreference(
    family=AttributeFamily.COLOR,
    raw_value="beige",
    canonical_value="Beige",
    strength=ConstraintStrength.PREFERRED,
)
BUDGET = PriceConstraint.at_most(Decimal("12000"), "SAR")


def _living(**fields: object) -> RoomProjectState:
    return RoomProjectState(room_kind="living_room", **fields)  # type: ignore[arg-type]


# ── the registry ────────────────────────────────────────────────────────────


def test_the_agreed_tiers_hold() -> None:
    """What the customer agreed: essentials start selected and can be removed,
    the mattress is optional, wall art and a TV table start selected in both
    rooms."""
    tiers = {piece.key: piece.tier for piece in LIVING.pieces}
    assert tiers["sofa"] is PieceTier.ESSENTIAL
    assert tiers["center-table"] is PieceTier.ESSENTIAL
    assert tiers["rug"] is PieceTier.ESSENTIAL
    bedroom = {piece.key: piece.tier for piece in BEDROOM.pieces}
    assert bedroom["bed"] is PieceTier.ESSENTIAL
    assert bedroom["mattress"] is PieceTier.OPTIONAL
    for template in (LIVING, BEDROOM):
        assert {"wall-art", "tv-table"} <= set(default_pieces(template))


def test_living_room_seating_is_built_from_the_agreed_types() -> None:
    seating = LIVING.seating
    assert seating is not None and seating.label == "Sofa"
    assert set(seating.seating_types) == {
        "sofa",
        "sectional-sofa",
        "sofa-set",
        "single-seater-sofa",
        "chair",
    }
    assert LIVING.asks_seats and not BEDROOM.asks_seats


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "rooms.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("pieces", "problem"),
    [
        (
            "- {key: x, label: X, category: seating, subcategory: chandelier, tier: essential}",
            "not an approved pair",
        ),
        ("- {key: x, label: X, category: tables, subcategory: console, tier: must}", "tier"),
        (
            "- {key: x, label: X, category: tables, subcategory: console, tier: essential}\n"
            "      - {key: x, label: Y, category: tables, subcategory: console, tier: optional}",
            "repeats a piece key",
        ),
        (
            "- {key: s, label: S, tier: essential, seating: [recliner]}",
            "no reviewed seat count",
        ),
        (
            "- {key: s, label: S, tier: essential, seating: [chair, sofa]}",
            "must seat several",
        ),
        (
            "- {key: x, label: X, category: tables, subcategory: console, tier: optional}",
            "no essential piece",
        ),
    ],
)
def test_a_malformed_registry_stops_startup(tmp_path: Path, pieces: str, problem: str) -> None:
    body = f"version: t\nrooms:\n  den:\n    pieces:\n      {pieces}\n"

    with pytest.raises(TaxonomyConfigurationError, match=problem):
        load_room_pieces(_write(tmp_path, body), taxonomy=TAXONOMY, seating=SEATING)


# ── the pieces they chose ───────────────────────────────────────────────────


def test_choose_for_me_is_every_essential_and_recommended_piece() -> None:
    chosen = chosen_keys(LIVING, None, default=True)

    assert chosen == default_pieces(LIVING)
    assert all(LIVING.piece(key).tier.starts_selected for key in chosen)  # type: ignore[union-attr]


def test_an_unknown_piece_is_dropped_never_mapped() -> None:
    assert chosen_keys(LIVING, ["sofa", "couch", "rug", "sofa"], default=False) == ("sofa", "rug")
    assert chosen_keys(LIVING, ["couch"], default=False) is None
    assert chosen_keys(LIVING, None, default=False) is None


# ── the questions, one per turn ─────────────────────────────────────────────


def test_budget_is_asked_first_then_pieces_seats_and_colour() -> None:
    room = _living()
    order = []
    for _ in range(5):
        question = next_question(room, LIVING, EVERYTHING, None)
        if question is None:
            break
        order.append(question.kind)
        room = room.model_copy(update={"questions_asked": (*room.questions_asked, question.kind)})

    assert order == [
        RoomQuestionKind.BUDGET,
        RoomQuestionKind.PIECES,
        RoomQuestionKind.SEATS,
        RoomQuestionKind.COLOUR,
    ]


def test_nothing_already_said_is_asked() -> None:
    room = _living(
        budget=BUDGET, pieces=("sofa",), regular_seating_count=4, design_preferences=(BEIGE,)
    )

    assert next_question(room, LIVING, EVERYTHING, None) is None


def test_an_unanswered_question_is_never_asked_twice() -> None:
    room = _living(questions_asked=(RoomQuestionKind.BUDGET,))

    question = next_question(room, LIVING, EVERYTHING, None)

    assert question is not None and question.kind is RoomQuestionKind.PIECES


def test_a_bedroom_never_asks_how_many_will_sit() -> None:
    room = RoomProjectState(
        room_kind="bedroom", budget=BUDGET, pieces=("bed",), design_preferences=(BEIGE,)
    )

    assert next_question(room, BEDROOM, EVERYTHING, 9) is None


def test_just_design_it_ends_the_questions() -> None:
    assert next_question(_living(questions_done=True), LIVING, EVERYTHING, None) is None


def test_a_room_already_built_asks_nothing() -> None:
    room = _living(
        design_needs=(),
        bundle_items=(),
        questions_asked=(),
    )
    assert next_question(room, LIVING, EVERYTHING, None) is not None
    built = _living(
        design_needs=(
            {  # type: ignore[arg-type]
                "need_id": 1,
                "commerce_category": "decor",
                "commerce_subcategory": "carpet",
                "priority": "required",
                "quantity": 1,
            },
        ),
        next_design_need_id=2,
    )
    assert next_question(built, LIVING, EVERYTHING, None) is None


def test_an_earlier_head_count_is_offered_only_to_confirm_the_seats() -> None:
    room = _living(budget=BUDGET, pieces=("sofa",))

    question = next_question(room, LIVING, EVERYTHING, 9)

    assert question is not None
    assert question.kind is RoomQuestionKind.SEATS
    assert question.earlier_seat_count == 9
    budget_question = next_question(_living(), LIVING, EVERYTHING, 9)
    assert budget_question is not None and budget_question.earlier_seat_count is None


def test_only_pieces_the_store_stocks_are_offered() -> None:
    stock = _stock(("seating", "sofa"), ("decor", "carpet"), ("lighting", "floor-lamp"))
    room = _living(budget=BUDGET)

    question = next_question(room, LIVING, stock, None)

    assert question is not None and question.kind is RoomQuestionKind.PIECES
    assert [piece.key for piece in question.pieces] == ["sofa", "rug", "floor-lamp"]
    assert all(piece.selected for piece in question.pieces)


# ── the room's needs ────────────────────────────────────────────────────────


def test_each_chosen_piece_is_one_need_at_its_tier() -> None:
    designed = InteriorDesignResult(
        needs=(
            DesignCategoryNeed(
                commerce_category="decor",
                commerce_subcategory="carpet",
                priority=DesignPriority.OPTIONAL,
                semantic_intent="soft and textured",
            ),
            # Not chosen: the specialist may not add a piece.
            DesignCategoryNeed(
                commerce_category="lighting",
                commerce_subcategory="chandelier",
                priority=DesignPriority.REQUIRED,
            ),
        )
    )

    needs = composed_needs(BEDROOM, ("bed", "rug", "nightstands"), EVERYTHING, designed)

    assert [(n.commerce_subcategory, n.priority, n.quantity) for n in needs] == [
        ("bed", DesignPriority.REQUIRED, 1),
        ("nightstand", DesignPriority.REQUIRED, 2),
        ("carpet", DesignPriority.RECOMMENDED, 1),
    ]
    assert needs[2].semantic_intent == "soft and textured"


def test_seating_is_left_to_the_head_count_and_an_unstocked_piece_is_skipped() -> None:
    stock = _stock(("seating", "sofa"), ("decor", "carpet"))
    keys = ("sofa", "rug", "center-table")

    needs = composed_needs(LIVING, keys, stock, InteriorDesignResult())

    assert [n.commerce_subcategory for n in needs] == ["carpet"]
    assert seating_piece(LIVING, keys, stock) is LIVING.seating
    assert seating_piece(LIVING, ("rug",), stock) is None
