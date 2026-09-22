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
    DesignTask,
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
            RetailerCatalogCapability(
                commerce_category=c, commerce_subcategory=s, active_product_count=12
            )
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


def test_a_room_plan_request_is_accepted() -> None:
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
        task=DesignTask.ROOM_PLAN,
        catalog_capabilities=_capabilities(("seating", "sofa")),
    )

    assert request.catalog_capabilities is not None
    assert request.catalog_capabilities.supports("seating", "sofa")
    assert request.budget is not None


def test_a_design_result_carries_guidance_and_needs() -> None:
    """No design_preferences: the customer's preferences have one home."""
    assert set(InteriorDesignResult.model_fields) == {"guidance", "needs"}
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


def test_the_capability_service_exists_and_owns_no_vocabulary() -> None:
    """M12A built it. It reports what the retailer stocks, and the registry
    still decides what any of those names mean."""
    import inspect

    from app.services.catalog_capability import CatalogCapabilityService

    parameters = [
        name
        for name in inspect.signature(CatalogCapabilityService.__init__).parameters
        if name != "self"
    ]

    assert parameters == ["repository", "taxonomy"]


def test_capabilities_carry_no_products_or_prices() -> None:
    """Depth is allowed; inventory is not.

    `active_product_count` is the one field naming a product, and it names a
    quantity of them rather than any one of them. So the guard checks the
    fields that would actually be inventory, and pins that count as the sole
    exception rather than dropping "product" from the list and letting a
    `product_url` through later.
    """
    names = set(RetailerCatalogCapability.model_fields) | set(
        RetailerCatalogCapabilities.model_fields
    )
    for forbidden in ("price", "vector", "embedding", "url", "name", "id"):
        assert not any(forbidden in name for name in names), forbidden

    naming_a_product = {name for name in names if "product" in name}
    assert naming_a_product == {"active_product_count"}


# ── per-need design intent ──────────────────────────────────────────────────
#
# The gap M12C.1 closed: cross-sell knew which *kinds* of thing a room needed
# and nothing about which of them to put first. This field carries that, and
# only that — it reaches the query embedding and no filter, no bound and no
# widening policy.


def _need(intent: str | None = None) -> DesignCategoryNeed:
    return DesignCategoryNeed(
        commerce_category="seating",
        commerce_subcategory="lounge-chair",
        priority=DesignPriority.REQUIRED,
        semantic_intent=intent,
    )


def test_a_need_may_carry_its_own_design_character() -> None:
    assert _need("visually light and comfortable for prolonged reading").semantic_intent


def test_design_intent_is_optional() -> None:
    """Most needs have nothing particular to say, and that is not a gap."""
    assert _need().semantic_intent is None


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_design_intent_becomes_absent(blank: str) -> None:
    assert _need(blank).semantic_intent is None


def test_design_intent_is_trimmed() -> None:
    assert _need("  low-profile and understated  ").semantic_intent == (
        "low-profile and understated"
    )


@pytest.mark.parametrize(
    ("hostile", "why"),
    [
        ("under 2000 SAR", "a price"),
        ("no wider than 220 cm", "a measurement"),
        ("seats 4 people", "a seat count"),
        ("product 164846", "an identifier"),
        ("store 50 only", "a retailer"),
        ("2 of these", "a quantity"),
    ],
)
def test_a_figure_in_a_design_intent_is_refused_not_stripped(
    hostile: str, why: str
) -> None:
    """Every prohibited structured fact is a number, and each already has a
    typed home. Removing the digits would leave wording the design never asked
    for; keeping them would put an unprovenanced figure into rankable text."""
    with pytest.raises(ValidationError):
        _need(hostile)

    assert why


def test_design_intent_is_bounded() -> None:
    """A phrase, not a transcript."""
    with pytest.raises(ValidationError):
        _need("light " * 200)


def test_the_need_carries_exactly_these_fields() -> None:
    assert set(DesignCategoryNeed.model_fields) == {
        "commerce_category",
        "commerce_subcategory",
        "priority",
        "seating_capacity",
        "semantic_intent",
        "quantity",
    }


def test_design_intent_is_not_a_semantic_preference() -> None:
    """Colour and style keep their own typed home. This field is prose about
    character, and the two must not become interchangeable (CLAUDE.md 12.4)."""
    assert "SemanticPreference" not in str(
        DesignCategoryNeed.model_fields["semantic_intent"].annotation
    )


# ── it is execution context, never durable state ────────────────────────────


def test_no_state_domain_holds_a_design_intent() -> None:
    """Per-plan, per-need context. The M11 conversational `semantic_intent` is
    a different thing with a different lifetime, and reusing it as room-design
    state would make a transient ranking hint durable."""
    from app.schemas.agent_state import RoomProjectState

    assert "semantic_intent" not in RoomProjectState.model_fields
    assert "design_intent" not in str(AgentStateV1.model_fields)
    assert "DesignCategoryNeed" not in set(
        AgentStateV1.model_json_schema().get("$defs", {})
    )
