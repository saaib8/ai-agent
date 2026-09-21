"""Seeding a search from one product, using only what the catalog verified.

"Something similar to this" is answered structurally: the same kind of
product, leaning towards the same colour and style. Everything else about the
reference — its name, its price, its measurements — would narrow the search on
something the customer never said.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.query import ConstraintStrength
from app.schemas.resolution import (
    SimilarSearchFailureReason,
    SimilarSearchSeed,
    SimilarSearchUnavailable,
)
from app.services.grounding_builder import to_grounded_product
from app.services.similar_search import SimilarSearchBuilder
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes
from app.taxonomy.registry import load_taxonomy

REFERENCE_ID = 165645


@pytest.fixture(scope="module")
def builder() -> SimilarSearchBuilder:
    return SimilarSearchBuilder(load_taxonomy(), load_catalog_attributes())


def _candidate(
    *,
    category: str | None = "seating",
    subcategory: str | None = "sofa",
    capacity: int | None = 3,
    color: str | None = "Beige",
    styles: tuple[str, ...] = ("Modern",),
    price: str = "2450.00",
) -> ProductCandidate:
    return ProductCandidate(
        product_id=REFERENCE_ID,
        name_english="Arezo Three Seater",
        name_arabic="كنبة",
        price_amount=Decimal(price),
        price_unit="SAR",
        image_url="https://example.test/1.jpg",
        product_url="https://example.test/1",
        commerce=CommerceClassification(
            category=category, subcategory=subcategory, seating_capacity=capacity
        ),
        dimensions=NormalisedDimensions(
            length_cm=Decimal("220"), unit=None, status=DimensionStatus.ABSENT
        ),
        main_color=color,
        styles=styles,
    )


def _seed(outcome: Any) -> SimilarSearchSeed:
    assert isinstance(outcome, SimilarSearchSeed), outcome
    return outcome


def _preferences(seed: SimilarSearchSeed, family: AttributeFamily) -> list[str | None]:
    return [
        p.canonical_value
        for p in seed.resolved.semantic_preferences
        if p.family is family
    ]


# ── the seed ════════════════════════════════════════════════════════════════


def test_the_product_type_is_preserved(builder: SimilarSearchBuilder) -> None:
    seed = _seed(builder.build(_candidate()))

    assert seed.resolved.request.commerce_category == "seating"
    assert seed.resolved.request.commerce_subcategory == "sofa"


def test_a_verified_seat_count_is_preserved(builder: SimilarSearchBuilder) -> None:
    seed = _seed(builder.build(_candidate(capacity=3)))
    capacity = seed.resolved.request.seating_capacity

    assert capacity is not None
    assert (capacity.min_capacity, capacity.max_capacity) == (3, 3)


def test_an_unknown_seat_count_is_never_invented(
    builder: SimilarSearchBuilder,
) -> None:
    """NULL means unverified, and a name or a size cannot establish it."""
    seed = _seed(builder.build(_candidate(capacity=None)))

    assert seed.resolved.request.seating_capacity is None
    assert seed.resolved.semantics.seating_min is None


def test_colour_becomes_a_preference_not_a_filter(
    builder: SimilarSearchBuilder,
) -> None:
    """Filtering would discard alternatives they never excluded."""
    seed = _seed(builder.build(_candidate(color="Beige")))

    assert seed.resolved.request.colors_any_of == ()
    assert _preferences(seed, AttributeFamily.COLOR) == ["Beige"]


def test_styles_become_preferences_not_filters(
    builder: SimilarSearchBuilder,
) -> None:
    seed = _seed(builder.build(_candidate(styles=("Modern", "Minimalist"))))

    assert seed.resolved.request.styles_all_of == ()
    assert _preferences(seed, AttributeFamily.STYLE) == ["Modern", "Minimalist"]


def test_every_preference_is_recorded_as_a_leaning(
    builder: SimilarSearchBuilder,
) -> None:
    seed = _seed(builder.build(_candidate()))

    assert all(
        p.strength is ConstraintStrength.PREFERRED
        for p in seed.resolved.semantic_preferences
    )


def test_the_reference_is_excluded_from_its_own_alternatives(
    builder: SimilarSearchBuilder,
) -> None:
    seed = _seed(builder.build(_candidate()))

    assert seed.resolved.request.exclude_product_ids == (REFERENCE_ID,)
    assert seed.reference_product_id == REFERENCE_ID


def test_nothing_fuzzy_is_invented(builder: SimilarSearchBuilder) -> None:
    seed = _seed(builder.build(_candidate()))

    assert seed.resolved.semantic_text is None


# ── what the seed must not use ══════════════════════════════════════════════


def test_the_seed_uses_no_name_price_or_measurement(
    builder: SimilarSearchBuilder,
) -> None:
    """Each would narrow the search on something never asked for."""
    seed = _seed(builder.build(_candidate(price="2450.00")))
    request = seed.resolved.request

    assert request.price is None
    assert request.dimensions == ()
    assert request.planar_dimensions is None
    rendered = seed.resolved.model_dump_json()
    assert "Arezo" not in rendered
    assert "2450" not in rendered


def test_no_vector_similarity_is_used() -> None:
    """Checked against code, not prose: the docstring says "no embedding"."""
    import ast
    from pathlib import Path

    tree = ast.parse(
        (Path(__file__).parents[2] / "app/services/similar_search.py").read_text()
    )
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            identifiers.add(node.module or "")
        elif isinstance(node, ast.Import):
            identifiers.update(alias.name for alias in node.names)

    assert identifiers, "the module must reference something"
    for identifier in identifiers:
        for forbidden in ("embed", "pinecone", "vector", "material"):
            assert forbidden not in identifier.lower(), identifier


# ── taxonomy safety ═════════════════════════════════════════════════════════


def test_a_product_with_no_classification_cannot_seed_a_search(
    builder: SimilarSearchBuilder,
) -> None:
    """Deriving one from the name is exactly what the service must not do."""
    outcome = builder.build(_candidate(category=None))

    assert isinstance(outcome, SimilarSearchUnavailable)
    assert outcome.reason is SimilarSearchFailureReason.NO_COMMERCE_CATEGORY


def test_an_unapproved_category_cannot_become_a_filter(
    builder: SimilarSearchBuilder,
) -> None:
    outcome = builder.build(_candidate(category="living-room-furniture"))

    assert isinstance(outcome, SimilarSearchUnavailable)
    assert outcome.reason is SimilarSearchFailureReason.UNAPPROVED_COMMERCE_CATEGORY


def test_an_unapproved_subcategory_cannot_become_a_filter(
    builder: SimilarSearchBuilder,
) -> None:
    """A stale token like `side-table` is refused, not passed through."""
    outcome = builder.build(_candidate(category="tables", subcategory="side-table"))

    assert isinstance(outcome, SimilarSearchUnavailable)
    assert outcome.reason is SimilarSearchFailureReason.UNAPPROVED_COMMERCE_SUBCATEGORY


def test_a_subcategory_from_another_family_is_refused(
    builder: SimilarSearchBuilder,
) -> None:
    outcome = builder.build(_candidate(category="lighting", subcategory="sofa"))

    assert isinstance(outcome, SimilarSearchUnavailable)


def test_a_category_without_a_subcategory_still_seeds(
    builder: SimilarSearchBuilder,
) -> None:
    seed = _seed(builder.build(_candidate(subcategory=None)))

    assert seed.resolved.request.commerce_subcategory is None
    assert seed.resolved.semantics.subcategory is None


# ── attribute safety ════════════════════════════════════════════════════════


def test_an_unapproved_colour_is_skipped_not_replaced(
    builder: SimilarSearchBuilder,
) -> None:
    """"Close enough" is how a search starts answering another question."""
    seed = _seed(builder.build(_candidate(color="Chartreuse")))

    assert _preferences(seed, AttributeFamily.COLOR) == []
    assert _preferences(seed, AttributeFamily.STYLE) == ["Modern"]


def test_one_unusable_style_does_not_cost_the_others(
    builder: SimilarSearchBuilder,
) -> None:
    seed = _seed(builder.build(_candidate(styles=("Modern", "NotAStyle"))))

    assert _preferences(seed, AttributeFamily.STYLE) == ["Modern"]


def test_a_product_with_no_attributes_still_seeds(
    builder: SimilarSearchBuilder,
) -> None:
    seed = _seed(builder.build(_candidate(color=None, styles=())))

    assert seed.resolved.semantic_preferences == ()
    assert seed.resolved.request.commerce_category == "seating"


# ── the builder executes nothing ════════════════════════════════════════════


def test_the_builder_runs_no_search() -> None:
    import ast
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/similar_search.py").read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        assert not isinstance(node, ast.AsyncFunctionDef)
        assert not isinstance(node, ast.Await)
    for forbidden in ("ControlledRelaxationService", "SemanticRankingService", "search("):
        assert forbidden not in source


# ── the shared grounding builder ════════════════════════════════════════════


def test_the_builder_copies_authoritative_facts() -> None:
    product = to_grounded_product(_candidate(), grounding_ref=1)

    assert product.name_english == "Arezo Three Seater"
    assert product.price_amount == Decimal("2450.00")
    assert product.price_unit == "SAR"
    assert product.product_url == "https://example.test/1"
    assert product.image_url == "https://example.test/1.jpg"
    assert product.commerce.subcategory == "sofa"
    assert product.main_color == "Beige"
    assert product.styles == ("Modern",)
    assert product.dimensions.status is DimensionStatus.ABSENT


def test_the_builder_leaves_provenance_unknown_by_default() -> None:
    product = to_grounded_product(_candidate(), grounding_ref=1)

    assert product.relaxation_depth is None
    assert product.matched_exactly is None
    assert product.presented_ordinal is None


def test_verified_provenance_can_be_supplied_without_touching_the_facts() -> None:
    plain = to_grounded_product(_candidate(), grounding_ref=1)
    searched = to_grounded_product(
        _candidate(), grounding_ref=1, presented_ordinal=2, relaxation_depth=0
    )

    assert searched.matched_exactly is True
    assert searched.presented_ordinal == 2
    assert searched.price_amount == plain.price_amount
    assert searched.name_english == plain.name_english


def test_the_builder_exposes_no_arabic_field() -> None:
    product = to_grounded_product(_candidate(), grounding_ref=1)

    assert "name_arabic" not in type(product).model_fields
    assert "كنبة" not in product.model_dump_json()


def test_every_grounded_field_is_copied_not_derived() -> None:
    """Each value is a parameter or a plain attribute read of the input.

    A builder that computed anything would be a second source of product
    truth, and the first place a fabricated fact could appear.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(
        (Path(__file__).parents[2] / "app/services/grounding_builder.py").read_text()
    )
    construction = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GroundedProduct"
    )

    assert construction.keywords
    for keyword in construction.keywords:
        value = keyword.value
        if isinstance(value, ast.Name):
            continue  # a parameter, passed straight through
        assert isinstance(value, ast.Attribute), ast.unparse(value)
        assert isinstance(value.value, ast.Name), ast.unparse(value)
        assert value.value.id == "product", ast.unparse(value)
