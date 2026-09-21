"""The taxonomy registry: one source of truth, validated deterministically."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.exceptions import (
    TaxonomyConfigurationError,
    UnknownCommerceCategoryError,
    UnknownCommerceSubcategoryError,
)
from app.taxonomy.registry import DEFAULT_TAXONOMY_PATH, CommerceTaxonomy, load_taxonomy

# The approved global taxonomy, restated here independently of the YAML so the
# test is a genuine check on the file rather than a mirror of it.
APPROVED: dict[str, set[str]] = {
    "seating": {
        "chair", "dining-chair", "lounge-chair", "office-chair", "recliner",
        "stool", "outdoor-chair", "single-seater-sofa", "sofa", "sectional-sofa",
        "sofa-bed", "sofa-set", "chaise-lounge",
    },
    "bedroom": {"bed", "dressing-table", "wardrobe", "dresser"},
    "bedding": {"bedspread", "pillow", "mattresses", "mattress-pad", "comforter"},
    "tables": {"service-table", "center-table", "nightstand", "console", "tv-table"},
    "dining": {"dining-table", "dining-set"},
    "office": {"office-table"},
    "storage": {"storage-box", "laundry-basket", "shelve"},
    "decor": {
        "carpet", "flower-pot-and-plant", "statue-and-antique", "candle",
        "candlestick", "vase", "flower", "wall-clock", "decorative-hanger",
        "art-canvas", "mirror",
    },
    "lighting": {
        "lighting", "table-lamp", "floor-lamp", "wall-lighting",
        "outdoor-lighting", "chandelier", "pendant-lighting",
    },
    "kitchen-appliances": {"coffee-maker", "cooking-appliance", "food-processor"},
    "kitchenware": {"cooking-pot", "serving-utensil-and-tray", "cup", "plate"},
    "fitness": {
        "weight-bench-flat", "weight-bench-adjustable", "stationary-bike",
        "treadmill", "dumbbell", "elliptical-machine", "kettlebells",
        "medicine-ball", "power-rack", "yoga-mat", "leg-press-machine",
        "chest-press-machine", "jump-rope", "air-bike", "barbell",
        "boxing-gloves", "weight-plates",
    },
}

ALL_PAIRS = [(c, s) for c, subs in APPROVED.items() for s in sorted(subs)]


@pytest.fixture(scope="module")
def taxonomy() -> CommerceTaxonomy:
    return load_taxonomy()


def test_the_packaged_taxonomy_loads(taxonomy: CommerceTaxonomy) -> None:
    assert taxonomy.version == "v1"
    assert DEFAULT_TAXONOMY_PATH.exists()


def test_every_approved_parent_category_loads(taxonomy: CommerceTaxonomy) -> None:
    assert taxonomy.categories == frozenset(APPROVED)


@pytest.mark.parametrize(("category", "subcategory"), ALL_PAIRS)
def test_every_approved_pair_validates(
    taxonomy: CommerceTaxonomy, category: str, subcategory: str
) -> None:
    assert taxonomy.is_pair(category, subcategory)
    taxonomy.validate_pair(category, subcategory)


def test_no_category_carries_extra_subcategories(taxonomy: CommerceTaxonomy) -> None:
    for category, expected in APPROVED.items():
        assert taxonomy.subcategories(category) == frozenset(expected), category


def test_unknown_category_is_rejected(taxonomy: CommerceTaxonomy) -> None:
    assert not taxonomy.is_category("living-room-furniture")
    with pytest.raises(UnknownCommerceCategoryError):
        taxonomy.validate_pair("living-room-furniture", "sofa")


def test_unknown_subcategory_is_rejected(taxonomy: CommerceTaxonomy) -> None:
    with pytest.raises(UnknownCommerceSubcategoryError):
        taxonomy.validate_pair("seating", "luxury-couch")


@pytest.mark.parametrize(
    ("category", "subcategory"),
    [("seating", "chandelier"), ("lighting", "sofa"), ("tables", "bed")],
)
def test_a_valid_subcategory_under_the_wrong_category_is_rejected(
    taxonomy: CommerceTaxonomy, category: str, subcategory: str
) -> None:
    """The hierarchy constrains the pair, not just the tokens (CLAUDE.md 14.4)."""
    assert not taxonomy.is_pair(category, subcategory)
    with pytest.raises(UnknownCommerceSubcategoryError):
        taxonomy.validate_pair(category, subcategory)


@pytest.mark.parametrize(
    "superseded",
    ["l-shape-sofa", "side-table", "lampshade", "floor-stand"],
)
def test_superseded_tokens_are_not_canonical(
    taxonomy: CommerceTaxonomy, superseded: str
) -> None:
    for category in taxonomy.categories:
        assert not taxonomy.is_pair(category, superseded), (category, superseded)


@pytest.mark.parametrize("preserved", [("bedding", "mattresses"), ("storage", "shelve")])
def test_exact_v1_tokens_are_preserved(
    taxonomy: CommerceTaxonomy, preserved: tuple[str, str]
) -> None:
    assert taxonomy.is_pair(*preserved)


def test_subcategories_of_an_unknown_category_raises(taxonomy: CommerceTaxonomy) -> None:
    """So an empty result can only ever mean 'approved but empty'."""
    with pytest.raises(UnknownCommerceCategoryError):
        taxonomy.subcategories("not-a-category")


def test_the_registry_knows_nothing_about_inventory(taxonomy: CommerceTaxonomy) -> None:
    """Vocabulary only: no counts, no store, no availability (CLAUDE.md 9.1)."""
    surface = {name for name in dir(taxonomy) if not name.startswith("_")}
    assert surface == {
        "categories", "is_category", "is_pair", "subcategories",
        "validate_pair", "version",
    }


def test_no_synonym_or_visual_category_dependency() -> None:
    """Correctness must not rest on an alias table or the visual category."""
    source = (Path(__file__).parents[2] / "app" / "taxonomy").rglob("*.py")
    for module in source:
        text = module.read_text()
        assert "alias" not in text.lower() or "aliases" not in text.lower()
        assert "visual_map" not in text
    yaml_text = DEFAULT_TAXONOMY_PATH.read_text()
    assert "alias" not in yaml_text.lower()


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ("[]", "top level must be a mapping"),
        ("categories:\n  seating: [sofa]\n", "version"),
        ("version: v1\n", "categories"),
        ("version: v1\ncategories: {}\n", "categories"),
        ("version: v1\ncategories:\n  seating: []\n", "at least one subcategory"),
        ("version: v1\ncategories:\n  seating: sofa\n", "at least one subcategory"),
        ("version: v1\ncategories:\n  Seating: [sofa]\n", "slug"),
        ("version: v1\ncategories:\n  seating: [Sofa]\n", "slug"),
        ("version: v1\ncategories:\n  seating: [sofa, sofa]\n", "more than once"),
        ("version: v1\ncategories:\n  seating: [123]\n", "slug"),
        ("version: ''\ncategories:\n  seating: [sofa]\n", "version"),
        ("::: not yaml :::\n", "YAML"),
    ],
)
def test_malformed_registry_is_rejected(
    tmp_path: Path, document: str, reason: str
) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(document)

    with pytest.raises(TaxonomyConfigurationError, match=reason):
        load_taxonomy(path)


def test_a_missing_registry_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TaxonomyConfigurationError, match="cannot read"):
        load_taxonomy(tmp_path / "absent.yaml")


def test_a_valid_custom_registry_loads(tmp_path: Path) -> None:
    """Adding a category is a one-file change; nothing else enumerates them."""
    path = tmp_path / "custom.yaml"
    path.write_text("version: v9\ncategories:\n  outdoor: [parasol, hammock]\n")

    custom = load_taxonomy(path)

    assert custom.version == "v9"
    assert custom.categories == frozenset({"outdoor"})
    assert custom.is_pair("outdoor", "hammock")
