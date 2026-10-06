"""Arabic display names in the reviewed registries (docs/arabic-replies-plan.md).

Each registry whose labels a customer reads on a chip carries an `arabic:`
section mapping every English label to how it reads in Arabic. Display only:
keys, filters and decisions keep the English. One rule for all of them, checked
here at load time - every label has exactly one Arabic name and no name is left
over - so an Arabic customer can never be shown an English chip, or a chip for
something the registry no longer has.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from app.core.exceptions import TaxonomyConfigurationError


def parse_arabic_labels(raw: Any, labels: Collection[str], *, where: str) -> dict[str, str]:
    """The Arabic for each of `labels`, from a registry's `arabic` mapping."""
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: 'arabic' must be a mapping")
    missing = sorted(set(labels) - set(raw))
    unknown = sorted(set(raw) - set(labels))
    if missing or unknown:
        raise TaxonomyConfigurationError(
            detail=f"{where}: arabic is missing {missing} and names unknown {unknown}"
        )
    names: dict[str, str] = {}
    for label, name in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise TaxonomyConfigurationError(
                detail=f"{where}: arabic for {label!r} must be a non-empty string"
            )
        names[label] = name.strip()
    return names
