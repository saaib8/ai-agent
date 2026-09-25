"""How many people a seating type seats by its nature, as reviewed domain data.

A chair or a single-seater sofa seats one person by definition, but the catalog
records that as ``NULL`` rather than ``1``; a sofa, a set, a sectional or a sofa
bed always seats two or more. This is where those reviewed facts live -
versioned, cross-validated against the commerce taxonomy, never guessed in code
or by a model (CLAUDE.md 6.2, 31).

It answers three questions:

* when a product of a type has no recorded seat count, how many does a reviewer
  say it seats? (a combination fills a seat with it)
* does the type seat exactly one? (it never carries a seat filter)
* does it always seat several? ("one seat" of it is a misreading)

A type the review has not settled - ``recliner``, where some seat 2-3 - is in
neither list, and absence means unknown, never one. The loader mirrors the
dimension-semantics registry: read the versioned file, validate its shape, and
reject any type that is not an approved *seating* subcategory.
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
    """Reviewed seat facts, keyed by approved seating subcategory."""

    def __init__(
        self,
        version: str,
        implied: Mapping[str, int],
        multi_seat: frozenset[str] = frozenset(),
    ) -> None:
        self._version = version
        self._implied = dict(implied)
        self._multi_seat = multi_seat

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

    def seats_one(self, subcategory: str | None) -> bool:
        """Every product of this type seats exactly one person."""
        return self.implied_capacity(subcategory) == 1

    def seats_several(self, subcategory: str | None) -> bool:
        """Every product of this type seats two or more."""
        return subcategory is not None and subcategory in self._multi_seat

    def __repr__(self) -> str:
        return (
            f"SeatingSemantics(version={self._version!r}, implied={len(self._implied)}, "
            f"multi_seat={len(self._multi_seat)})"
        )


def load_seating_semantics(
    path: Path | None = None, taxonomy: CommerceTaxonomy | None = None
) -> SeatingSemantics:
    """Load and validate the seating registry. Raises on anything malformed.

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

    implied = _implied_capacity(document.get("implied_capacity"), source, taxonomy)
    multi_seat = _multi_seat(document.get("multi_seat", []), source, taxonomy)

    one_seat_and_several = sorted(t for t in multi_seat if implied.get(t) == 1)
    if one_seat_and_several:
        raise TaxonomyConfigurationError(
            detail=f"{source.name}: {one_seat_and_several} cannot seat one and several"
        )
    return SeatingSemantics(version=version, implied=implied, multi_seat=multi_seat)


def _implied_capacity(
    raw: Any, source: Path, taxonomy: CommerceTaxonomy | None
) -> dict[str, int]:
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
        _require_seating(key, source, taxonomy)
        implied[key] = value
    return implied


def _multi_seat(raw: Any, source: Path, taxonomy: CommerceTaxonomy | None) -> frozenset[str]:
    if not isinstance(raw, list) or not all(isinstance(v, str) and v for v in raw):
        raise TaxonomyConfigurationError(
            detail=f"{source.name}: 'multi_seat' must be a list of subcategories"
        )
    if len(raw) != len(set(raw)):
        raise TaxonomyConfigurationError(detail=f"{source.name}: 'multi_seat' repeats a value")
    for value in raw:
        _require_seating(value, source, taxonomy)
    return frozenset(raw)


def _require_seating(subcategory: str, source: Path, taxonomy: CommerceTaxonomy | None) -> None:
    if taxonomy is not None and not taxonomy.is_pair(SEATING_CATEGORY, subcategory):
        raise TaxonomyConfigurationError(
            detail=f"{source.name}: {subcategory!r} is not an approved seating subcategory"
        )
