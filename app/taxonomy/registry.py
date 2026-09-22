"""The approved global commerce taxonomy: vocabulary, loading and validation.

``commerce_v1.yaml`` is the single source of truth. Nothing else in the codebase
enumerates categories or subcategories - no Enum, no validation table, no
prompt constant, no if/elif chain. Adding a value is a one-file change
(CLAUDE.md 14.1).

The registry defines vocabulary and nothing else. It deliberately knows nothing
about product counts, `store_id`, retailer capabilities or current
availability; those are three separate concepts (CLAUDE.md 9.1).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.exceptions import (
    TaxonomyConfigurationError,
    UnknownCommerceCategoryError,
    UnknownCommerceSubcategoryError,
)

DEFAULT_TAXONOMY_PATH: Final[Path] = Path(__file__).parent / "commerce_v1.yaml"

# Approved tokens are lowercase hyphenated slugs. Enforced at load time so a
# typo in the vocabulary fails at startup rather than silently never matching.
_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class CommerceTaxonomy:
    """Immutable, queryable view of the approved commerce vocabulary."""

    def __init__(self, version: str, categories: Mapping[str, frozenset[str]]) -> None:
        self._version = version
        self._categories: Mapping[str, frozenset[str]] = dict(categories)

    @property
    def version(self) -> str:
        return self._version

    @property
    def categories(self) -> frozenset[str]:
        return frozenset(self._categories)

    def is_category(self, category: str) -> bool:
        return category in self._categories

    def subcategories(self, category: str) -> frozenset[str]:
        """Subcategories allowed under `category`.

        Raises :class:`UnknownCommerceCategoryError` for an unapproved category,
        so an empty result can only ever mean "approved but empty".
        """
        try:
            return self._categories[category]
        except KeyError:
            raise UnknownCommerceCategoryError(category=category) from None

    def is_subcategory(self, subcategory: str) -> bool:
        """Whether this is an approved subcategory under *any* category.

        Category-free on purpose. It answers "is this a product type ZORY
        understands", which is what a check on a customer's own word needs -
        "sofa five" names a kind without naming a family, and requiring the
        family would mean inferring one.

        Pair validity is a different question, and :meth:`is_pair` is still the
        only answer to it: nothing here permits a category and subcategory that
        do not belong together (CLAUDE.md 14.4).
        """
        return any(
            subcategory in self.subcategories(category) for category in self.categories
        )

    def is_pair(self, category: str, subcategory: str) -> bool:
        """True only when the subcategory is approved UNDER that category."""
        return subcategory in self._categories.get(category, frozenset())

    def validate_pair(self, category: str, subcategory: str) -> None:
        """Raise unless the pair is approved. The deterministic gate that
        structured interpretations must pass before reaching SQL (CLAUDE.md 14.3)."""
        allowed = self._categories.get(category)
        if allowed is None:
            raise UnknownCommerceCategoryError(category=category)
        if subcategory not in allowed:
            raise UnknownCommerceSubcategoryError(
                category=category, subcategory=subcategory
            )

    def __repr__(self) -> str:
        return (
            f"CommerceTaxonomy(version={self._version!r}, "
            f"categories={len(self._categories)}, "
            f"subcategories={sum(len(v) for v in self._categories.values())})"
        )


def _require_slug(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SLUG.match(value):
        raise TaxonomyConfigurationError(
            detail=f"{field} must be a lowercase hyphenated slug, got {value!r}"
        )
    return value


def _parse(document: Any, *, source: str) -> CommerceTaxonomy:
    if not isinstance(document, dict):
        raise TaxonomyConfigurationError(detail=f"{source}: top level must be a mapping")

    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise TaxonomyConfigurationError(detail=f"{source}: 'version' must be a non-empty string")

    raw_categories = document.get("categories")
    if not isinstance(raw_categories, dict) or not raw_categories:
        raise TaxonomyConfigurationError(
            detail=f"{source}: 'categories' must be a non-empty mapping"
        )

    categories: dict[str, frozenset[str]] = {}
    for raw_category, raw_subcategories in raw_categories.items():
        category = _require_slug(raw_category, field=f"{source}: category")
        if not isinstance(raw_subcategories, list) or not raw_subcategories:
            raise TaxonomyConfigurationError(
                detail=f"{source}: category {category!r} must list at least one subcategory"
            )
        seen: set[str] = set()
        for raw_subcategory in raw_subcategories:
            subcategory = _require_slug(
                raw_subcategory, field=f"{source}: {category} subcategory"
            )
            if subcategory in seen:
                raise TaxonomyConfigurationError(
                    detail=f"{source}: {category!r} lists {subcategory!r} more than once"
                )
            seen.add(subcategory)
        categories[category] = frozenset(seen)

    return CommerceTaxonomy(version=version, categories=categories)


def load_taxonomy(path: Path | None = None) -> CommerceTaxonomy:
    """Load and validate the taxonomy. Raises on anything malformed."""
    source = path or DEFAULT_TAXONOMY_PATH
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot read taxonomy at {source}: {type(exc).__name__}"
        ) from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaxonomyConfigurationError(
            detail=f"{source.name} is not valid YAML: {type(exc).__name__}"
        ) from exc
    return _parse(document, source=source.name)
