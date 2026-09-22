"""What the retailer stocks — distinct from what ZORY understands.

Three things it is easy to conflate (CLAUDE.md 9.1): the global vocabulary, the
live catalog of one store, and whether anything survives a constrained search.
This service answers only the middle one, and every test here is about keeping
it in its lane.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.schemas.retailer import (
    RetailerCatalogCapabilities,
    RetailerCatalogCapability,
    RetailerContext,
)
from app.services.catalog_capability import CatalogCapabilityService
from app.taxonomy.registry import load_taxonomy
from pydantic import ValidationError

TAXONOMY = load_taxonomy()
CONTEXT = RetailerContext(store_id=50)
OTHER = RetailerContext(store_id=60)


class FakeRepository:
    def __init__(self, types: tuple[tuple[str, str | None, int], ...]) -> None:
        self.types = types
        self.calls: list[RetailerContext] = []

    async def supported_commerce_types(
        self, context: RetailerContext
    ) -> tuple[tuple[str, str | None, int], ...]:
        self.calls.append(context)
        return self.types


STOCK_DEPTH = 7
"""What the store has of each type in these fixtures.

A count these tests do not reason about: they are about which pairs survive
taxonomy approval, and a single shared depth keeps that the only variable.
"""


def _service(*pairs: tuple[str, str | None]) -> tuple[CatalogCapabilityService, Any]:
    repository = FakeRepository(
        tuple((category, subcategory, STOCK_DEPTH) for category, subcategory in pairs)
    )
    return CatalogCapabilityService(repository, TAXONOMY), repository  # type: ignore[arg-type]


async def test_it_reports_what_the_store_stocks() -> None:
    service, _ = _service(("seating", "sofa"), ("tables", "console"))

    capabilities = await service.capabilities(CONTEXT)

    assert capabilities.supports("seating", "sofa")
    assert capabilities.supports("tables", "console")


async def test_it_is_scoped_to_the_context_it_was_given() -> None:
    service, repository = _service(("seating", "sofa"))

    await service.capabilities(OTHER)

    assert repository.calls == [OTHER]


async def test_an_unapproved_pair_never_becomes_a_capability() -> None:
    """A stale or mistaken classification must not become a claim a plan is
    then built on (CLAUDE.md 14.3)."""
    service, _ = _service(("seating", "sofa"), ("soft-furnishings", "beanbag"))

    capabilities = await service.capabilities(CONTEXT)

    assert capabilities.supports("seating", "sofa")
    assert not capabilities.supports("soft-furnishings", "beanbag")
    assert len(capabilities.capabilities) == 1


async def test_an_unapproved_subcategory_under_an_approved_category_is_dropped() -> None:
    service, _ = _service(("seating", "hammock"))

    capabilities = await service.capabilities(CONTEXT)

    assert capabilities.capabilities == ()


async def test_dropping_errs_towards_proposing_less() -> None:
    """Safe in the direction that matters: the planner proposes fewer types,
    never one that cannot be bought."""
    service, _ = _service(("lighting", "floor-lamp"), ("nonsense", "thing"))

    capabilities = await service.capabilities(CONTEXT)

    assert [c.commerce_category for c in capabilities.capabilities] == ["lighting"]


async def test_an_empty_catalog_supports_nothing() -> None:
    """Not an error, and not a reason to fall back to the global vocabulary."""
    service, _ = _service()

    capabilities = await service.capabilities(CONTEXT)

    assert capabilities.capabilities == ()
    assert not capabilities.supports("seating", "sofa")


async def test_capability_is_not_the_global_taxonomy() -> None:
    """`sofa-bed` is an approved type this store happens not to stock."""
    service, _ = _service(("seating", "sofa"))

    capabilities = await service.capabilities(CONTEXT)

    assert TAXONOMY.is_pair("seating", "sofa-bed")
    assert not capabilities.supports("seating", "sofa-bed")


async def test_a_category_only_entry_asserts_nothing_about_its_children() -> None:
    service, _ = _service(("seating", None))

    capabilities = await service.capabilities(CONTEXT)

    assert capabilities.supports("seating")
    assert not capabilities.supports("seating", "sofa")


def test_a_category_cannot_be_described_both_ways() -> None:
    """The invariant the live data must not violate: the broad form asserts
    nothing about children, so pairing it with children leaves a reader unable
    to say what is supported."""
    with pytest.raises(ValidationError, match="cannot also appear"):
        RetailerCatalogCapabilities.model_validate(
            {
                "capabilities": [
                    {
                        "commerce_category": "seating",
                        "commerce_subcategory": None,
                        "active_product_count": STOCK_DEPTH,
                    },
                    {
                        "commerce_category": "seating",
                        "commerce_subcategory": "sofa",
                        "active_product_count": STOCK_DEPTH,
                    },
                ]
            }
        )


async def test_the_result_carries_depth_but_not_inventory() -> None:
    """How many, never which ones.

    A count was once forbidden here alongside prices and identities. It is
    carried now because "supported" and "worth proposing" are different
    questions and a bare list cannot tell them apart (CLAUDE.md 9) - a
    retailer with one lounge chair supports lounge chairs and still cannot
    offer the customer a choice of them.

    Everything that would make it inventory stays out. A count names no
    product, quotes no price and identifies no row, so nothing downstream can
    turn it back into one.
    """
    service, _ = _service(("seating", "sofa"))

    capabilities = await service.capabilities(CONTEXT)

    assert capabilities.capabilities[0].active_product_count == STOCK_DEPTH
    rendered = capabilities.model_dump_json()
    for forbidden in ("price", "product_id", "product_url", "store_id", "name"):
        assert forbidden not in rendered


async def test_it_consults_no_model() -> None:
    import ast
    from pathlib import Path

    source = (
        Path(__file__).parents[2] / "app/services/catalog_capability.py"
    ).read_text()
    tree = ast.parse(source)
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert not any("llm" in name or "prompt" in name for name in imported)
    assert "redis" not in source.lower(), "no caching in M12A"


async def test_each_type_carries_its_own_depth() -> None:
    """One shared number would defeat the point.

    The distinction that matters is between a type the retailer has 41 of and
    one it has a single example of, so the count has to travel per type rather
    than as a property of the store.
    """
    repository = FakeRepository(
        (("seating", "sofa", 173), ("seating", "lounge-chair", 1))
    )
    service = CatalogCapabilityService(repository, TAXONOMY)  # type: ignore[arg-type]

    capabilities = await service.capabilities(CONTEXT)

    depths = {
        entry.commerce_subcategory: entry.active_product_count
        for entry in capabilities.capabilities
    }
    assert depths == {"sofa": 173, "lounge-chair": 1}


async def test_a_thin_type_is_still_supported() -> None:
    """Depth informs a choice; it does not remove a capability.

    Silently dropping the single lounge chair would make the catalog summary
    disagree with the catalog, and a customer asking for one directly would be
    told the retailer has none (CLAUDE.md 9.1).
    """
    repository = FakeRepository((("seating", "lounge-chair", 1),))
    service = CatalogCapabilityService(repository, TAXONOMY)  # type: ignore[arg-type]

    capabilities = await service.capabilities(CONTEXT)

    assert capabilities.supports("seating", "lounge-chair")


def test_a_capability_cannot_claim_a_type_the_store_has_none_of() -> None:
    """The type is a capability *because* something backs it, so zero is a
    contradiction rather than an empty shelf."""
    with pytest.raises(ValidationError):
        RetailerCatalogCapability(
            commerce_category="seating",
            commerce_subcategory="sofa",
            active_product_count=0,
        )
