"""Retailer capabilities and the two-agent handoff.

The capability contract exists so the design specialist cannot plan a room
around product types the retailer does not sell. Everything here defends that,
plus the boundary that keeps capabilities out of agent state.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.core.exceptions import (
    UnknownCommerceCategoryError,
    UnknownCommerceSubcategoryError,
)
from app.schemas.agent_state import AgentStateV1
from app.schemas.design import (
    DesignCategoryNeed,
    DesignPriority,
    InteriorDesignRequest,
    InteriorDesignResult,
)
from app.schemas.discovery import PriceConstraint
from app.schemas.query import ConstraintStrength, SemanticPreference
from app.schemas.retailer import (
    RetailerCatalogCapabilities,
    RetailerCatalogCapability,
)
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.registry import load_taxonomy
from pydantic import ValidationError

TAXONOMY = load_taxonomy()
SAR = "SAR"


def _capabilities(*pairs: tuple[str, str | None]) -> RetailerCatalogCapabilities:
    return RetailerCatalogCapabilities(
        capabilities=tuple(
            RetailerCatalogCapability(commerce_category=c, commerce_subcategory=s)
            for c, s in pairs
        )
    )


# ── capability structure ────────────────────────────────────────────────────


def test_an_empty_capability_set_is_valid() -> None:
    """A retailer with nothing reviewed is a real state, not a crash."""
    assert RetailerCatalogCapabilities().capabilities == ()


def test_a_category_only_entry_is_accepted() -> None:
    capabilities = _capabilities(("seating", None))

    capabilities.validate_against(TAXONOMY)
    assert capabilities.supports("seating")


def test_explicit_pairs_are_accepted() -> None:
    capabilities = _capabilities(("seating", "sofa"), ("seating", "sectional-sofa"))

    capabilities.validate_against(TAXONOMY)
    assert capabilities.supports("seating", "sofa")


def test_a_category_only_entry_does_not_expand_to_its_children() -> None:
    """It asserts the category at unstated granularity, nothing more."""
    capabilities = _capabilities(("seating", None))

    assert capabilities.supports("seating") is True
    assert capabilities.supports("seating", "sofa") is False


def test_a_duplicate_capability_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _capabilities(("seating", "sofa"), ("seating", "sofa"))


def test_a_category_cannot_be_described_both_ways() -> None:
    """Broad and narrow together leave a reader unable to say what is supported."""
    with pytest.raises(ValidationError):
        _capabilities(("seating", None), ("seating", "sofa"))


def test_the_mixed_form_is_only_rejected_within_one_category() -> None:
    capabilities = _capabilities(("seating", None), ("tables", "nightstand"))

    capabilities.validate_against(TAXONOMY)
    assert capabilities.supports("seating")
    assert capabilities.supports("tables", "nightstand")


def test_capabilities_are_frozen_and_forbid_extras() -> None:
    for model in (RetailerCatalogCapability, RetailerCatalogCapabilities):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"


# ── taxonomy validity ───────────────────────────────────────────────────────


def test_an_unapproved_category_is_rejected() -> None:
    with pytest.raises(UnknownCommerceCategoryError):
        _capabilities(("living-room-furniture", None)).validate_against(TAXONOMY)


def test_an_unapproved_pair_is_rejected() -> None:
    with pytest.raises(UnknownCommerceSubcategoryError):
        _capabilities(("seating", "chandelier")).validate_against(TAXONOMY)


def test_a_superseded_subcategory_is_rejected() -> None:
    with pytest.raises(UnknownCommerceSubcategoryError):
        _capabilities(("tables", "side-table")).validate_against(TAXONOMY)


# ── the handoff ─────────────────────────────────────────────────────────────


def test_a_design_request_requires_capabilities() -> None:
    """Planning around products the retailer cannot sell must not be the default."""
    with pytest.raises(ValidationError):
        InteriorDesignRequest(room_type="living room")  # type: ignore[call-arg]


def test_a_complete_design_request_is_accepted() -> None:
    request = InteriorDesignRequest(
        room_type="living room",
        budget=PriceConstraint.at_most(Decimal("12000"), SAR),
        design_preferences=(
            SemanticPreference(
                family=AttributeFamily.STYLE,
                raw_value="warm neutral modern",
                strength=ConstraintStrength.PREFERRED,
            ),
        ),
        catalog_capabilities=_capabilities(("seating", "sofa")),
    )

    assert request.catalog_capabilities.supports("seating", "sofa")
    assert request.budget is not None


def test_a_design_result_carries_needs_only() -> None:
    """No design_preferences: the customer's preferences have one home."""
    assert set(InteriorDesignResult.model_fields) == {"needs"}
    assert "design_preferences" not in InteriorDesignResult.model_fields


def test_a_design_result_validates_its_needs_against_the_taxonomy() -> None:
    result = InteriorDesignResult(
        needs=(
            DesignCategoryNeed(
                commerce_category="seating",
                commerce_subcategory="sofa",
                priority=DesignPriority.REQUIRED,
            ),
        )
    )
    result.validate_against(TAXONOMY)

    invalid = InteriorDesignResult(
        needs=(
            DesignCategoryNeed(
                commerce_category="seating",
                commerce_subcategory="chandelier",
                priority=DesignPriority.OPTIONAL,
            ),
        )
    )
    with pytest.raises(UnknownCommerceSubcategoryError):
        invalid.validate_against(TAXONOMY)


def test_a_need_may_name_a_category_without_a_subcategory() -> None:
    result = InteriorDesignResult(
        needs=(
            DesignCategoryNeed(
                commerce_category="lighting", priority=DesignPriority.RECOMMENDED
            ),
        )
    )

    result.validate_against(TAXONOMY)


@pytest.mark.parametrize("priority", list(DesignPriority))
def test_every_priority_is_representable(priority: DesignPriority) -> None:
    need = DesignCategoryNeed(commerce_category="seating", priority=priority)

    assert need.priority is priority


def test_an_empty_result_is_valid() -> None:
    InteriorDesignResult().validate_against(TAXONOMY)


# ── boundaries ──────────────────────────────────────────────────────────────


def test_capabilities_are_not_part_of_agent_state() -> None:
    """Application-supplied context, parallel to RetailerContext."""
    assert "catalog_capabilities" not in AgentStateV1.model_fields
    assert "capabilities" not in AgentStateV1.model_fields


def test_no_capability_service_was_implemented() -> None:
    from pathlib import Path

    services = Path(__file__).parents[2] / "app/services"
    assert not (services / "catalog_capability.py").exists()
    for module in services.rglob("*.py"):
        assert "CatalogCapabilityService" not in module.read_text(), module.name


def test_capabilities_carry_no_products_or_prices() -> None:
    names = set(RetailerCatalogCapability.model_fields) | set(
        RetailerCatalogCapabilities.model_fields
    )
    for forbidden in ("price", "product", "count", "vector", "embedding"):
        assert not any(forbidden in name for name in names), forbidden
