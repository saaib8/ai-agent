"""Which product types seat one person by nature, and which seat several.

Loaded and validated at startup like the other registries: every value must be
an approved subcategory, and no type may be in both lists. Nothing here reads
a product row - it describes the vocabulary, not the catalog.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict

from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.registry import CommerceTaxonomy

DEFAULT_SEATING_PATH: Final[Path] = Path(__file__).parent / "seating_v1.yaml"


class SeatingRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    one_seat: frozenset[str] = frozenset()
    multi_seat: frozenset[str] = frozenset()

    def seats_one(self, subcategory: str | None) -> bool:
        return subcategory is not None and subcategory in self.one_seat

    def seats_several(self, subcategory: str | None) -> bool:
        return subcategory is not None and subcategory in self.multi_seat


def load_seating_rules(
    path: Path | None = None, taxonomy: CommerceTaxonomy | None = None
) -> SeatingRules:
    """Load and validate the registry. Raises on anything malformed."""
    source_path = path or DEFAULT_SEATING_PATH
    try:
        document = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot load seating rules at {source_path.name}: {type(exc).__name__}"
        ) from exc
    if not isinstance(document, dict):
        raise TaxonomyConfigurationError(detail=f"{source_path.name}: top level must be a mapping")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise TaxonomyConfigurationError(
            detail=f"{source_path.name}: 'version' must be a non-empty string"
        )

    lists: dict[str, frozenset[str]] = {}
    for key in ("one_seat", "multi_seat"):
        raw = document.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(v, str) and v for v in raw):
            raise TaxonomyConfigurationError(
                detail=f"{source_path.name}: '{key}' must be a list of subcategories"
            )
        if len(raw) != len(set(raw)):
            raise TaxonomyConfigurationError(detail=f"{source_path.name}: '{key}' repeats a value")
        if taxonomy is not None:
            unknown = sorted(v for v in raw if not taxonomy.is_subcategory(v))
            if unknown:
                raise TaxonomyConfigurationError(
                    detail=f"{source_path.name}: '{key}' names unapproved subcategories {unknown}"
                )
        lists[key] = frozenset(raw)

    both = sorted(lists["one_seat"] & lists["multi_seat"])
    if both:
        raise TaxonomyConfigurationError(
            detail=f"{source_path.name}: {both} cannot seat one and several"
        )
    return SeatingRules(version=version, one_seat=lists["one_seat"], multi_seat=lists["multi_seat"])
