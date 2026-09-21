"""What the retailer stocks — distinct from what ZORY understands.

Three things it is easy to conflate (CLAUDE.md 9.1): the global vocabulary, the
live catalog of one store, and whether anything survives a constrained search.
This service answers only the middle one, and every test here is about keeping
it in its lane.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerContext
from app.services.catalog_capability import CatalogCapabilityService
from app.taxonomy.registry import load_taxonomy
from pydantic import ValidationError

TAXONOMY = load_taxonomy()
CONTEXT = RetailerContext(store_id=50)
OTHER = RetailerContext(store_id=60)


class FakeRepository:
    def __init__(self, pairs: tuple[tuple[str, str | None], ...]) -> None:
        self.pairs = pairs
        self.calls: list[RetailerContext] = []

    async def supported_commerce_pairs(
        self, context: RetailerContext
    ) -> tuple[tuple[str, str | None], ...]:
        self.calls.append(context)
        return self.pairs


def _service(*pairs: tuple[str, str | None]) -> tuple[CatalogCapabilityService, Any]:
    repository = FakeRepository(pairs)
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
                    {"commerce_category": "seating", "commerce_subcategory": None},
                    {"commerce_category": "seating", "commerce_subcategory": "sofa"},
                ]
            }
        )


async def test_the_result_carries_no_inventory() -> None:
    """Capability, not counts, prices or products."""
    service, _ = _service(("seating", "sofa"))

    capabilities = await service.capabilities(CONTEXT)

    rendered = capabilities.model_dump_json()
    for forbidden in ("count", "price", "product_id", "store_id"):
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
