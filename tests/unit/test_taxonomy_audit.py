"""The catalog audit reports conformance. It never repairs anything."""

from __future__ import annotations

from typing import cast

from app.repositories.catalog_audit import (
    CatalogAuditRepository,
    CommerceCoverage,
    CommercePairCount,
)
from app.taxonomy.audit import PairVerdict, TaxonomyAuditService, render
from app.taxonomy.registry import load_taxonomy


class _StubAuditRepository:
    def __init__(self, pairs: list[CommercePairCount], coverage: CommerceCoverage) -> None:
        self._pairs = pairs
        self._coverage = coverage

    async def distinct_commerce_pairs(self) -> list[CommercePairCount]:
        return self._pairs

    async def commerce_coverage(self) -> CommerceCoverage:
        return self._coverage


def _pair(category: str, subcategory: str | None, n: int = 5) -> CommercePairCount:
    return CommercePairCount(
        category=category, subcategory=subcategory, product_count=n, active_count=n
    )


def _service(pairs: list[CommercePairCount]) -> TaxonomyAuditService:
    coverage = CommerceCoverage(
        total_products=100,
        classified_products=sum(p.product_count for p in pairs),
        null_category_count=40,
        null_subcategory_count=41,
    )
    return TaxonomyAuditService(
        cast(CatalogAuditRepository, _StubAuditRepository(pairs, coverage)),
        load_taxonomy(),
    )


async def test_approved_pairs_are_reported_valid() -> None:
    report = await _service(
        [_pair("seating", "sofa"), _pair("tables", "nightstand")]
    ).run()

    assert report.conforms
    assert {p.verdict for p in report.pairs} == {PairVerdict.VALID}
    assert len(report.valid_pairs) == 2


async def test_an_unapproved_category_is_surfaced() -> None:
    report = await _service([_pair("living-room-furniture", "sofa")]).run()

    assert not report.conforms
    assert report.invalid_categories[0].category == "living-room-furniture"


async def test_an_unapproved_subcategory_is_surfaced() -> None:
    report = await _service([_pair("seating", "l-shape-sofa")]).run()

    assert not report.conforms
    assert report.invalid_pairs[0].subcategory == "l-shape-sofa"


async def test_a_valid_subcategory_under_the_wrong_category_is_surfaced() -> None:
    report = await _service([_pair("lighting", "sofa")]).run()

    assert not report.conforms
    assert report.invalid_pairs[0].category == "lighting"


async def test_a_classified_row_with_no_subcategory_is_surfaced() -> None:
    report = await _service([_pair("seating", None)]).run()

    assert not report.conforms
    assert report.missing_subcategories[0].category == "seating"


async def test_null_counts_are_reported() -> None:
    report = await _service([_pair("seating", "sofa")]).run()

    assert report.coverage.null_category_count == 40
    assert report.coverage.null_subcategory_count == 41
    assert report.coverage.total_products == 100


async def test_categories_absent_from_the_data_are_informational_only() -> None:
    """A category with no products is still part of the global taxonomy."""
    report = await _service([_pair("seating", "sofa")]).run()

    assert report.conforms
    assert "fitness" in report.unused_categories
    assert "seating" not in report.unused_categories


async def test_the_report_carries_the_taxonomy_version() -> None:
    assert (await _service([_pair("seating", "sofa")]).run()).taxonomy_version == "v1"


async def test_rendering_names_every_discrepancy() -> None:
    report = await _service(
        [_pair("seating", "sofa"), _pair("seating", "l-shape-sofa")]
    ).run()

    text = render(report)

    assert "DISCREPANCIES FOUND" in text
    assert "l-shape-sofa" in text
    assert "Taxonomy version: v1" in text


async def test_the_audit_exposes_no_way_to_change_anything() -> None:
    """Read-only by construction: no repair, map or write surface."""
    surface = {name for name in dir(TaxonomyAuditService) if not name.startswith("_")}
    assert surface == {"run"}
