"""Which physical dimension each product type supports, and which axis answers it.

The customer says "wide"; the catalog stores `length`, `width` and `height`.
What "wide" means depends on the product, so the correspondence is recorded once
here rather than inferred anywhere at runtime.

This registry is application-owned deterministic truth. A model proposes a
customer-facing *role*; only this registry turns a role into a stored axis, so
no model output can ever choose a database column (CLAUDE.md 3.3).

A role that is absent is not supported. Absence never means "guess".
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict

from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.registry import CommerceTaxonomy

DEFAULT_DIMENSION_SEMANTICS_PATH: Final[Path] = (
    Path(__file__).parent / "dimension_semantics_v1.yaml"
)


class DimensionRole(StrEnum):
    """What the customer is measuring, in their terms - never a column name."""

    LENGTH = "length"
    OVERALL_WIDTH = "overall_width"
    DEPTH = "depth"
    HEIGHT = "height"


class SourceAxis(StrEnum):
    """The stored column a role resolves to. Only these three exist."""

    LENGTH = "length"
    WIDTH = "width"
    HEIGHT = "height"


class UnsupportedDimensionReason(StrEnum):
    """Why a role cannot be filtered on. Machine-readable, never prose."""

    UNRELIABLE_AXIS_MAPPING = "unreliable_axis_mapping"
    """The stored axes are not distinguishable for this product type."""

    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    """Too few products record the value for a filter to be meaningful."""

    UNSUPPORTED_PRODUCT_GEOMETRY = "unsupported_product_geometry"
    """The product's shape has no single value this role could name."""

    ROLE_NOT_DEFINED = "role_not_defined"
    """No mapping was established for this product type and role."""


class PlanarPair(BaseModel):
    """Two sides matched as a set, because their order carries no meaning."""

    model_config = ConfigDict(frozen=True)

    axes: tuple[SourceAxis, SourceAxis]
    unordered: bool


class SubcategoryDimensions(BaseModel):
    model_config = ConfigDict(frozen=True)

    roles: Mapping[DimensionRole, SourceAxis] = {}
    unsupported: Mapping[DimensionRole, UnsupportedDimensionReason] = {}
    planar_pair: PlanarPair | None = None


class DimensionSemantics:
    """Immutable view of the approved role-to-axis mappings."""

    def __init__(
        self, version: str, subcategories: Mapping[str, SubcategoryDimensions]
    ) -> None:
        self._version = version
        self._subcategories: Mapping[str, SubcategoryDimensions] = dict(subcategories)

    @property
    def version(self) -> str:
        return self._version

    @property
    def subcategories(self) -> frozenset[str]:
        return frozenset(self._subcategories)

    def supported_roles(self, subcategory: str | None) -> frozenset[DimensionRole]:
        if subcategory is None:
            return frozenset()
        entry = self._subcategories.get(subcategory)
        return frozenset(entry.roles) if entry else frozenset()

    def source_axis(self, subcategory: str | None, role: DimensionRole) -> SourceAxis | None:
        """The stored axis answering this role, or ``None`` when unsupported."""
        if subcategory is None:
            return None
        entry = self._subcategories.get(subcategory)
        return entry.roles.get(role) if entry else None

    def unsupported_reason(
        self, subcategory: str | None, role: DimensionRole
    ) -> UnsupportedDimensionReason:
        """Why this role cannot be filtered. Only meaningful when unsupported."""
        entry = self._subcategories.get(subcategory) if subcategory else None
        if entry is None:
            return UnsupportedDimensionReason.ROLE_NOT_DEFINED
        return entry.unsupported.get(role, UnsupportedDimensionReason.ROLE_NOT_DEFINED)

    def planar_pair(self, subcategory: str | None) -> PlanarPair | None:
        if subcategory is None:
            return None
        entry = self._subcategories.get(subcategory)
        return entry.planar_pair if entry else None

    def supports_planar(self, subcategory: str | None) -> bool:
        return self.planar_pair(subcategory) is not None

    def __repr__(self) -> str:
        supported = sum(1 for e in self._subcategories.values() if e.roles)
        return (
            f"DimensionSemantics(version={self._version!r}, "
            f"subcategories={len(self._subcategories)}, with_roles={supported})"
        )


def _enum(value: Any, kind: type[StrEnum], *, field: str, source: str) -> Any:
    try:
        return kind(value)
    except ValueError:
        raise TaxonomyConfigurationError(
            detail=f"{source}: {field} has unknown value {value!r}"
        ) from None


def _parse_subcategory(
    name: str, raw: Any, *, source: str
) -> SubcategoryDimensions:
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{source}: {name} must be a mapping")

    roles: dict[DimensionRole, SourceAxis] = {}
    for raw_role, spec in (raw.get("roles") or {}).items():
        role = _enum(raw_role, DimensionRole, field=f"{name} role", source=source)
        if not isinstance(spec, dict) or "source_axis" not in spec:
            raise TaxonomyConfigurationError(
                detail=f"{source}: {name}.{raw_role} needs a source_axis"
            )
        roles[role] = _enum(
            spec["source_axis"],
            SourceAxis,
            field=f"{name}.{raw_role} source_axis",
            source=source,
        )

    unsupported: dict[DimensionRole, UnsupportedDimensionReason] = {}
    for raw_role, spec in (raw.get("unsupported_roles") or {}).items():
        role = _enum(raw_role, DimensionRole, field=f"{name} unsupported role", source=source)
        if role in roles:
            raise TaxonomyConfigurationError(
                detail=f"{source}: {name}.{raw_role} is both supported and unsupported"
            )
        if not isinstance(spec, dict) or "reason" not in spec:
            raise TaxonomyConfigurationError(
                detail=f"{source}: {name}.{raw_role} needs a reason"
            )
        unsupported[role] = _enum(
            spec["reason"],
            UnsupportedDimensionReason,
            field=f"{name}.{raw_role} reason",
            source=source,
        )

    planar = None
    if (raw_planar := raw.get("planar_pair")) is not None:
        axes = raw_planar.get("axes")
        if not isinstance(axes, list) or len(axes) != 2 or axes[0] == axes[1]:
            raise TaxonomyConfigurationError(
                detail=f"{source}: {name}.planar_pair needs two distinct axes"
            )
        if raw_planar.get("mode") != "unordered":
            raise TaxonomyConfigurationError(
                detail=f"{source}: {name}.planar_pair mode must be 'unordered'"
            )
        first, second = (
            _enum(a, SourceAxis, field=f"{name}.planar_pair axis", source=source) for a in axes
        )
        planar = PlanarPair(axes=(first, second), unordered=True)

    return SubcategoryDimensions(roles=roles, unsupported=unsupported, planar_pair=planar)


def load_dimension_semantics(
    path: Path | None = None, taxonomy: CommerceTaxonomy | None = None
) -> DimensionSemantics:
    """Load and validate the registry. Raises on anything malformed."""
    source_path = path or DEFAULT_DIMENSION_SEMANTICS_PATH
    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot read dimension semantics at {source_path}: {type(exc).__name__}"
        ) from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaxonomyConfigurationError(
            detail=f"{source_path.name} is not valid YAML: {type(exc).__name__}"
        ) from exc
    if not isinstance(document, dict):
        raise TaxonomyConfigurationError(
            detail=f"{source_path.name}: top level must be a mapping"
        )

    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise TaxonomyConfigurationError(
            detail=f"{source_path.name}: 'version' must be a non-empty string"
        )

    raw_subcategories = document.get("subcategories")
    if not isinstance(raw_subcategories, dict) or not raw_subcategories:
        raise TaxonomyConfigurationError(
            detail=f"{source_path.name}: 'subcategories' must be a non-empty mapping"
        )

    known = (
        {s for c in taxonomy.categories for s in taxonomy.subcategories(c)}
        if taxonomy is not None
        else None
    )
    subcategories: dict[str, SubcategoryDimensions] = {}
    for name, raw in raw_subcategories.items():
        if known is not None and name not in known:
            raise TaxonomyConfigurationError(
                detail=f"{source_path.name}: {name!r} is not an approved commerce subcategory"
            )
        subcategories[name] = _parse_subcategory(name, raw, source=source_path.name)

    return DimensionSemantics(version=version, subcategories=subcategories)
