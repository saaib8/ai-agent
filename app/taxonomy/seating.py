"""Reviewed seat counts for seating types the catalog leaves blank.

A chair or a single-seater sofa seats one person by definition, but the catalog
records that as ``NULL`` rather than ``1``. This is where the reviewed fact
lives - versioned domain data, cross-validated against the commerce taxonomy,
never guessed in code or by a model (CLAUDE.md 6.2, 31).

It answers exactly one question: when a product of a seating type has no recorded
seat count, how many does a reviewer say it seats? A type the review has not
settled - ``recliner``, where some seat 2-3 - is simply absent, and absence means
unknown, never one. It never overrides a count the catalog does record.

The loader mirrors the dimension-semantics registry: read the versioned file,
validate its shape, and reject any type the taxonomy does not approve, so a
stale or invented seating type cannot enter through this door.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.registry import CommerceTaxonomy

DEFAULT_SEATING_PATH: Final[Path] = Path(__file__).parent / "seating_v1.yaml"

SEATING_CATEGORY: Final[str] = "seating"
"""Implied seat counts are only meaningful for seating. Every entry is validated
as an approved *seating* subcategory, so a table type can never acquire one."""


class SeatingSemantics:
    """Reviewed implied seat counts, keyed by approved seating subcategory."""

    def __init__(self, version: str, implied: Mapping[str, int]) -> None:
        self._version = version
        self._implied = dict(implied)

    @property
    def version(self) -> str:
        return self._version

    def implied_capacity(self, subcategory: str | None) -> int | None:
        """The reviewed seat count for a piece of this type when the catalog
        records none, or ``None`` when no review has settled one.

        Never a fallback for a *missing* subcategory and never applied over a
        recorded count: it is the reviewed floor for a type whose catalog rows
        are blank, and nothing more.
        """
        if subcategory is None:
            return None
        return self._implied.get(subcategory)

    def __repr__(self) -> str:
        return f"SeatingSemantics(version={self._version!r}, implied={len(self._implied)})"


def load_seating_semantics(
    path: Path | None = None, taxonomy: CommerceTaxonomy | None = None
) -> SeatingSemantics:
    """Load and validate the implied-seat registry. Raises on anything malformed.

    When a ``taxonomy`` is given, every entry must be an approved seating
    subcategory - the same cross-check the dimension-semantics registry applies,
    so this file cannot drift from the vocabulary.
    """
    source = path or DEFAULT_SEATING_PATH
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot read seating semantics at {source}: {type(exc).__name__}"
        ) from exc
    try:
        document: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaxonomyConfigurationError(
            detail=f"{source.name} is not valid YAML: {type(exc).__name__}"
        ) from exc
    if not isinstance(document, dict):
        raise TaxonomyConfigurationError(detail=f"{source.name}: top level must be a mapping")

    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise TaxonomyConfigurationError(
            detail=f"{source.name}: 'version' must be a non-empty string"
        )

    raw = document.get("implied_capacity")
    if not isinstance(raw, dict) or not raw:
        raise TaxonomyConfigurationError(
            detail=f"{source.name}: 'implied_capacity' must be a non-empty mapping"
        )

    implied: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise TaxonomyConfigurationError(
                detail=f"{source.name}: each seating type must be a non-empty string"
            )
        # `bool` is an `int` subclass; a stray `true` must not read as 1.
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise TaxonomyConfigurationError(
                detail=f"{source.name}: implied capacity for {key!r} must be a positive integer"
            )
        if taxonomy is not None and not taxonomy.is_pair(SEATING_CATEGORY, key):
            raise TaxonomyConfigurationError(
                detail=f"{source.name}: {key!r} is not an approved seating subcategory"
            )
        implied[key] = value

    return SeatingSemantics(version=version, implied=implied)
