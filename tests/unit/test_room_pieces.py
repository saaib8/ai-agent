"""A room made of the pieces the customer chose (CLAUDE.md 10.1, 10.3).

The registry says which pieces a living room or a bedroom may hold, and how
much each matters. Which of them a room gets is Nora's design (design_room).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.registry import load_taxonomy
from app.taxonomy.rooms import PieceTier, load_room_pieces
from app.taxonomy.seating import load_seating_semantics

TAXONOMY = load_taxonomy()
SEATING = load_seating_semantics(taxonomy=TAXONOMY)
ROOMS = load_room_pieces(taxonomy=TAXONOMY, seating=SEATING)
LIVING = ROOMS.template("living_room")
BEDROOM = ROOMS.template("bedroom")
assert LIVING is not None and BEDROOM is not None


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
        selected = {piece.key for piece in template.pieces if piece.tier.starts_selected}
        assert {"wall-art", "tv-table"} <= selected


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
