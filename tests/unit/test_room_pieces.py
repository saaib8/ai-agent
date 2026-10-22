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
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
from app.schemas.query import ConstraintStrength, SemanticPreference
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerCatalogCapability
from app.services.room_composition import (
    composed_needs,
    default_pieces,
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


SIZED = RoomGeometry(
    measurements=(
        RoomMeasurement(role=RoomMeasurementRole.ROOM_LENGTH, centimetres=Decimal(500)),
        RoomMeasurement(role=RoomMeasurementRole.ROOM_WIDTH, centimetres=Decimal(400)),
    )
)
"""A room whose size they gave - so a test about another question is not asked
the size first."""


def _living(**fields: object) -> RoomProjectState:
    fields.setdefault("geometry", SIZED)
    return RoomProjectState(room_kind="living_room", **fields)


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






# ── the questions, one per turn ─────────────────────────────────────────────


















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






