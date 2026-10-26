"""The deterministic foundations a room's design is searched from.

The request discovery reads beside a plan knows what the retailer stocks
without being told a store id, and the plan names product types and
priorities, never products.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from app.schemas.design import (
    DesignCategoryNeed,
    DesignPriority,
    DesignTask,
    InteriorDesignRequest,
    InteriorDesignResult,
)
from app.schemas.geometry import (
    RoomGeometry,
    RoomMeasurement,
    RoomMeasurementRole,
)
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerCatalogCapability
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.registry import load_taxonomy
from pydantic import BaseModel, ValidationError

STOCKED_WITH_RANGE = 12
"""A capability depth that is not the thing under test.

Every capability carries how many products back it. These suites are about
which *types* a plan may use, so they give each one an unremarkable range -
enough that nothing is refused for being thin, and a number no assertion here
reads.
"""


TAXONOMY = load_taxonomy()


def _capabilities(*pairs: tuple[str, str | None]) -> RetailerCatalogCapabilities:
    return RetailerCatalogCapabilities(
        capabilities=tuple(
            RetailerCatalogCapability(
                commerce_category=category,
                commerce_subcategory=subcategory,
                active_product_count=STOCKED_WITH_RANGE,
            )
            for category, subcategory in pairs
        )
    )


# ── the request carries what discovery needs ────────────────────────────────


def test_a_room_plan_requires_the_retailers_capabilities() -> None:
    """A plan around types nobody stocks is worse than no plan."""
    with pytest.raises(ValidationError, match="catalog_capabilities"):
        InteriorDesignRequest(room_type="living room")  # type: ignore[call-arg]


def test_a_room_plan_carries_the_room_as_described() -> None:
    geometry = RoomGeometry(
        measurements=(
            RoomMeasurement(
                role=RoomMeasurementRole.USABLE_WALL,
                centimetres=Decimal("320"),
            ),
        )
    )
    request = InteriorDesignRequest(
        room_type="living room",
        geometry=geometry,
        catalog_capabilities=_capabilities(("seating", "sofa")),
    )

    assert request.task is DesignTask.ROOM_PLAN
    assert request.geometry == geometry


def test_the_request_has_no_field_for_a_store() -> None:
    with pytest.raises(ValidationError):
        InteriorDesignRequest.model_validate(
            {
                "task": DesignTask.ROOM_PLAN.value,
                "catalog_capabilities": {"capabilities": []},
                "store_id": 50,
            }
        )


# ── the plan contract ───────────────────────────────────────────────────────


def test_a_plan_expresses_priority_not_products() -> None:
    result = InteriorDesignResult(
        needs=(
            DesignCategoryNeed(
                commerce_category="seating",
                commerce_subcategory="sofa",
                priority=DesignPriority.REQUIRED,
            ),
            DesignCategoryNeed(
                commerce_category="lighting",
                commerce_subcategory="floor-lamp",
                priority=DesignPriority.RECOMMENDED,
            ),
        )
    )

    result.validate_against(TAXONOMY)
    assert [n.priority for n in result.needs] == [
        DesignPriority.REQUIRED,
        DesignPriority.RECOMMENDED,
    ]


def test_an_invented_category_is_rejected() -> None:
    from app.core.exceptions import TaxonomyValidationError

    result = InteriorDesignResult(
        needs=(
            DesignCategoryNeed(
                commerce_category="soft-furnishings",
                priority=DesignPriority.REQUIRED,
            ),
        )
    )

    with pytest.raises(TaxonomyValidationError):
        result.validate_against(TAXONOMY)


def test_the_result_has_nowhere_to_put_a_product() -> None:
    def walk(model: type[BaseModel], seen: set[type] | None = None) -> list[str]:
        seen = seen if seen is not None else set()
        if model in seen:
            return []
        seen.add(model)
        names: list[str] = []
        for name, field in model.model_fields.items():
            names.append(name)
            for arg in (field.annotation, *getattr(field.annotation, "__args__", ())):
                if isinstance(arg, type) and issubclass(arg, BaseModel):
                    names.extend(walk(arg, seen))
        return names

    for forbidden in ("product_id", "store_id", "price", "url", "name", "sku"):
        assert not any(forbidden in name for name in walk(InteriorDesignResult))


def test_no_room_composition_is_hardcoded() -> None:
    """Which categories a living room calls for is the specialist's reasoning
    in M12B, not a table here.

    Checked on *subcategories*, which are the granularity a composition rule
    would be written at, and never ordinary English. A category name like
    "lighting" is also a legitimate guidance topic, so matching on those would
    flag the vocabulary rather than a mapping.
    """
    source = (Path(__file__).parents[2] / "app/schemas/design.py").read_text()
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    for category in TAXONOMY.categories:
        for subcategory in TAXONOMY.subcategories(category):
            if subcategory == category:
                # The same-named child a category keeps for stock fitting none
                # of its specific types. An ordinary word, and a legitimate
                # guidance topic.
                continue
            assert subcategory not in literals, subcategory


def test_the_catalog_attribute_vocabulary_is_not_duplicated_here() -> None:
    attributes = load_catalog_attributes()
    source = (Path(__file__).parents[2] / "app/schemas/design.py").read_text()
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert not (attributes.styles & literals)
