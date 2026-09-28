"""The reviewed pairings: what goes with what.

A malformed pairing must fail the process at startup, never surface as an odd
chip later - so most of these are refusals.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.complements import (
    MAX_COMPANIONS,
    Companion,
    load_complements,
)
from app.taxonomy.registry import load_taxonomy

TAXONOMY = load_taxonomy()


def _load(tmp_path: Path, body: str) -> None:
    path = tmp_path / "complements.yaml"
    path.write_text(body, encoding="utf-8")
    load_complements(path, taxonomy=TAXONOMY)


def test_the_shipped_pairings_load_against_the_taxonomy() -> None:
    complements = load_complements(taxonomy=TAXONOMY)

    assert complements.version == "v1"
    for anchor in complements.anchors:
        assert TAXONOMY.is_subcategory(anchor)
        for companion in complements.for_type(anchor):
            assert TAXONOMY.is_pair(companion.commerce_category, companion.commerce_subcategory)


def test_a_bed_is_shown_nightstands_first() -> None:
    """The reviewed order: the first companion is the one shown as cards."""
    first = load_complements(taxonomy=TAXONOMY).for_type("bed")[0]

    assert first == Companion(
        commerce_category="tables", commerce_subcategory="nightstand", label="nightstands"
    )


@pytest.mark.parametrize("anchor", ["vase", "table-lamp", "treadmill", "cooking-pot"])
def test_decor_lighting_kitchen_and_fitness_have_no_cross_sell(anchor: str) -> None:
    assert load_complements(taxonomy=TAXONOMY).for_type(anchor) == ()


def test_an_unclassified_product_has_no_companions() -> None:
    assert load_complements(taxonomy=TAXONOMY).for_type(None) == ()


def test_a_chip_is_honoured_only_for_its_anchors_pairing() -> None:
    complements = load_complements(taxonomy=TAXONOMY)

    assert complements.companion("bed", "tables", "nightstand") is not None
    assert complements.companion("sofa", "tables", "nightstand") is None
    assert complements.companion(None, "tables", "nightstand") is None


def test_the_shared_lists_are_really_shared() -> None:
    """YAML anchors: a sofa, a sectional and a sofa set go with the same pieces."""
    complements = load_complements(taxonomy=TAXONOMY)

    assert complements.for_type("sofa") == complements.for_type("sectional-sofa")
    assert complements.for_type("sofa") == complements.for_type("sofa-set")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("complements: {bed: [{category: tables, subcategory: nightstand, label: x}]}", "version"),
        ("version: v1\n", "complements"),
        (
            "version: v1\ncomplements:\n  flying-carpet:\n"
            "    - {category: tables, subcategory: nightstand, label: nightstands}\n",
            "not an approved subcategory",
        ),
        (
            "version: v1\ncomplements:\n  bed:\n"
            "    - {category: seating, subcategory: nightstand, label: nightstands}\n",
            "not an approved pair",
        ),
        (
            "version: v1\ncomplements:\n  bed:\n"
            "    - {category: tables, subcategory: nightstand, label: nightstands}\n"
            "    - {category: tables, subcategory: nightstand, label: again}\n",
            "repeats a companion",
        ),
        (
            "version: v1\ncomplements:\n  bed:\n"
            "    - {category: bedroom, subcategory: bed, label: beds}\n",
            "not its own companion",
        ),
        (
            "version: v1\ncomplements:\n  bed:\n"
            "    - {category: tables, subcategory: nightstand}\n",
            "label",
        ),
        ("version: v1\ncomplements:\n  bed: []\n", "list of companions"),
        ("::: not yaml :::\n", "YAML|mapping"),
    ],
)
def test_a_malformed_registry_is_refused(tmp_path: Path, body: str, reason: str) -> None:
    with pytest.raises(TaxonomyConfigurationError, match=reason):
        _load(tmp_path, body)


def test_a_list_past_the_limit_is_refused(tmp_path: Path) -> None:
    entries = "".join(
        f"    - {{category: decor, subcategory: {sub}, label: {sub}s}}\n"
        for sub in ("carpet", "vase", "mirror", "candle", "flower", "wall-clock", "art-canvas")
    )
    assert len(entries.splitlines()) > MAX_COMPANIONS
    with pytest.raises(TaxonomyConfigurationError, match="at most"):
        _load(tmp_path, f"version: v1\ncomplements:\n  bed:\n{entries}")
