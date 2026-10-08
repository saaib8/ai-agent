"""Which products are similar enough to compare, as reviewed data.

A comparison is between two things that do the same job - two sofas, two
rugs - never a sofa and a coffee table. Every type compares with its own type;
the reviewed groups (`compare_groups_v1.yaml`) let a few types that answer the
same need compare with each other too, such as a sofa and an L-shaped
sectional. Loaded and validated against the commerce taxonomy at startup, like
the other registries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.registry import CommerceTaxonomy

DEFAULT_COMPARE_GROUPS_PATH: Final[Path] = Path(__file__).parent / "compare_groups_v1.yaml"


class CompareGroups:
    """The comparison family of every type: its group, or the type itself."""

    def __init__(self, version: str, groups: Mapping[str, str]) -> None:
        self._version = version
        self._groups = dict(groups)

    @property
    def version(self) -> str:
        return self._version

    @property
    def grouped(self) -> dict[str, str]:
        """Grouped types and their group, for a client to disable what cannot
        be compared before it asks. Types absent here are their own family."""
        return dict(self._groups)

    def family(self, subcategory: str | None) -> str | None:
        """The family a type compares within, or None for an unclassified
        product, which has nothing reviewed to be compared on."""
        if subcategory is None:
            return None
        return self._groups.get(subcategory, subcategory)

    def comparable(self, first: str | None, second: str | None) -> bool:
        family = self.family(first)
        return family is not None and family == self.family(second)

    def all_comparable(self, subcategories: Sequence[str | None]) -> bool:
        """Whether every product compares with the first - so with each other,
        since families are an equivalence."""
        first, *others = subcategories
        return all(self.comparable(first, other) for other in others)

    def __repr__(self) -> str:
        return f"CompareGroups(version={self._version!r}, grouped={len(self._groups)})"


def load_compare_groups(
    path: Path | None = None, taxonomy: CommerceTaxonomy | None = None
) -> CompareGroups:
    """Load and validate the groups. Raises on anything malformed."""
    source = path or DEFAULT_COMPARE_GROUPS_PATH
    try:
        document: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot read compare groups at {source}: {type(exc).__name__}"
        ) from exc
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
    raw = document.get("groups")
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{source.name}: 'groups' must be a mapping")

    groups: dict[str, str] = {}
    for name, members in raw.items():
        where = f"{source.name}: {name}"
        if not isinstance(name, str) or not name:
            raise TaxonomyConfigurationError(detail=f"{source.name}: each group must be named")
        if (
            not isinstance(members, list)
            or len(members) < 2
            or not all(isinstance(m, str) for m in members)
        ):
            # One type is already its own family; a group of one says nothing.
            raise TaxonomyConfigurationError(detail=f"{where}: a group needs two or more types")
        for member in members:
            if taxonomy is not None and not taxonomy.is_subcategory(member):
                raise TaxonomyConfigurationError(
                    detail=f"{where}: {member} is not an approved subcategory"
                )
            if member in groups:
                raise TaxonomyConfigurationError(
                    detail=f"{where}: {member} is already in {groups[member]}"
                )
            groups[member] = name
    return CompareGroups(version=version, groups=groups)
