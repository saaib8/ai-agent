"""Audits live catalog commerce values against the approved global taxonomy.

Read-only and non-destructive. It reports; it never repairs. An invalid value
is a product-data matter, so this must not map it to another value, fall back
to the visual `category`, or write anything (CLAUDE.md 6.1).

Run against a development database with::

    ZORY_DB__DSN=postgresql+asyncpg://... python -m app.taxonomy.audit
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.repositories.catalog_audit import (
    CatalogAuditRepository,
    CommerceCoverage,
    CommercePairCount,
)
from app.taxonomy.registry import CommerceTaxonomy


class PairVerdict(StrEnum):
    VALID = "valid"
    UNKNOWN_CATEGORY = "unknown_category"
    UNKNOWN_SUBCATEGORY = "unknown_subcategory"
    MISSING_SUBCATEGORY = "missing_subcategory"


class AuditedPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    category: str
    subcategory: str | None
    product_count: int
    active_count: int
    verdict: PairVerdict


class TaxonomyAuditReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    taxonomy_version: str
    approved_categories: tuple[str, ...]
    coverage: CommerceCoverage
    pairs: tuple[AuditedPair, ...]

    def _of(self, verdict: PairVerdict) -> tuple[AuditedPair, ...]:
        return tuple(pair for pair in self.pairs if pair.verdict is verdict)

    @property
    def valid_pairs(self) -> tuple[AuditedPair, ...]:
        return self._of(PairVerdict.VALID)

    @property
    def invalid_categories(self) -> tuple[AuditedPair, ...]:
        return self._of(PairVerdict.UNKNOWN_CATEGORY)

    @property
    def invalid_pairs(self) -> tuple[AuditedPair, ...]:
        return self._of(PairVerdict.UNKNOWN_SUBCATEGORY)

    @property
    def missing_subcategories(self) -> tuple[AuditedPair, ...]:
        return self._of(PairVerdict.MISSING_SUBCATEGORY)

    @property
    def conforms(self) -> bool:
        """True when every classified row uses an approved category/subcategory pair."""
        return not (
            self.invalid_categories or self.invalid_pairs or self.missing_subcategories
        )

    @property
    def unused_categories(self) -> tuple[str, ...]:
        """Approved categories with no products here.

        Informational only. Absence from a dataset does not remove a category
        from the global taxonomy and says nothing about retailer support
        (CLAUDE.md 9.1).
        """
        used = {pair.category for pair in self.pairs}
        return tuple(sorted(set(self.approved_categories) - used))


def _verdict(taxonomy: CommerceTaxonomy, pair: CommercePairCount) -> PairVerdict:
    if not taxonomy.is_category(pair.category):
        return PairVerdict.UNKNOWN_CATEGORY
    if pair.subcategory is None:
        return PairVerdict.MISSING_SUBCATEGORY
    if not taxonomy.is_pair(pair.category, pair.subcategory):
        return PairVerdict.UNKNOWN_SUBCATEGORY
    return PairVerdict.VALID


class TaxonomyAuditService:
    def __init__(
        self, repository: CatalogAuditRepository, taxonomy: CommerceTaxonomy
    ) -> None:
        self._repository = repository
        self._taxonomy = taxonomy

    async def run(self) -> TaxonomyAuditReport:
        coverage = await self._repository.commerce_coverage()
        pairs = await self._repository.distinct_commerce_pairs()
        audited = tuple(
            AuditedPair(
                category=pair.category,
                subcategory=pair.subcategory,
                product_count=pair.product_count,
                active_count=pair.active_count,
                verdict=_verdict(self._taxonomy, pair),
            )
            for pair in pairs
        )
        return TaxonomyAuditReport(
            taxonomy_version=self._taxonomy.version,
            approved_categories=tuple(sorted(self._taxonomy.categories)),
            coverage=coverage,
            pairs=audited,
        )


def render(report: TaxonomyAuditReport) -> str:
    lines = [
        f"Taxonomy version: {report.taxonomy_version}",
        "",
        "COVERAGE",
        f"  total products       {report.coverage.total_products}",
        f"  classified           {report.coverage.classified_products}",
        f"  null category        {report.coverage.null_category_count}",
        f"  null subcategory     {report.coverage.null_subcategory_count}",
        "",
        f"VALID PAIRS ({len(report.valid_pairs)})",
    ]
    lines += [
        f"  {p.category:20} / {p.subcategory!s:26} "
        f"total={p.product_count:<6} active={p.active_count}"
        for p in report.valid_pairs
    ]
    for title, entries in (
        ("INVALID CATEGORIES", report.invalid_categories),
        ("INVALID CATEGORY/SUBCATEGORY COMBINATIONS", report.invalid_pairs),
        ("CLASSIFIED WITHOUT A SUBCATEGORY", report.missing_subcategories),
    ):
        lines += ["", f"{title} ({len(entries)})"]
        lines += [
            f"  {p.category:20} / {p.subcategory!s:26} total={p.product_count}"
            for p in entries
        ] or ["  none"]
    lines += [
        "",
        f"APPROVED CATEGORIES WITH NO PRODUCTS HERE ({len(report.unused_categories)})",
        "  " + (", ".join(report.unused_categories) or "none"),
        "  (informational: the global taxonomy is not defined by this dataset)",
        "",
        "RESULT: "
        + ("conforms to the approved taxonomy" if report.conforms else "DISCREPANCIES FOUND"),
    ]
    return "\n".join(lines)


async def _main() -> int:
    from app.core.config import get_settings
    from app.integrations.postgres import Database
    from app.taxonomy.registry import load_taxonomy

    settings = get_settings()
    database = Database.create(settings.db)
    try:
        async with database.session() as session:
            service = TaxonomyAuditService(
                CatalogAuditRepository(session), load_taxonomy()
            )
            report = await service.run()
    finally:
        await database.dispose()
    print(render(report))
    return 0 if report.conforms else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
