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


class FloorSide(StrEnum):
    """Which floor side of a piece a measurement is, whatever column holds it.

    The longer side is the longer of the two floor measurements a merchant
    gave, and the shorter the shorter - true of every product in every store,
    so reading by side needs no knowledge of how a merchant named its columns.
    The model says which side a customer meant (a sofa's width is its longer
    side, a bed's width its shorter); it never names a column."""

    LONGER = "longer"
    SHORTER = "shorter"


def side_of(role: DimensionRole, side: FloorSide | None) -> FloorSide | None:
    """The side a measurement is read on, as far as its role alone decides:
    depth is always the shorter side, length always the longer, and height no
    floor side at all, whatever a model said. A width is whichever side the
    customer meant - a sofa's is its long side, a bed's its short - and None
    when that was not said."""
    if role is DimensionRole.HEIGHT:
        return None
    if role is DimensionRole.DEPTH:
        return FloorSide.SHORTER
    if role is DimensionRole.LENGTH:
        return FloorSide.LONGER
    return side


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


_SIDE_OF_COLUMN: Final[Mapping[SourceAxis, FloorSide]] = {
    SourceAxis.LENGTH: FloorSide.LONGER,
    SourceAxis.WIDTH: FloorSide.SHORTER,
}
"""The side each reviewed column stood for: in the catalog it was reviewed on,
`length` held the long side and `width` the short."""

_ANSWERED_BY_SIDES: Final = frozenset(
    {
        UnsupportedDimensionReason.UNRELIABLE_AXIS_MAPPING,
        UnsupportedDimensionReason.ROLE_NOT_DEFINED,
    }
)
"""Refusals about which column holds which side - answered once a floor
measurement is read as the longer or shorter side instead."""


class DimensionSemantics:
    """Immutable view of the approved role-to-axis mappings.

    `by_side` reads every floor measurement as the piece's longer or shorter
    side, whatever column holds it, so a store's column habits do not matter;
    off, each role reads the column reviewed for store 50, as before."""

    def __init__(
        self,
        version: str,
        subcategories: Mapping[str, SubcategoryDimensions],
        *,
        by_side: bool = False,
    ) -> None:
        self._version = version
        self._subcategories: Mapping[str, SubcategoryDimensions] = dict(subcategories)
        self._by_side = by_side

    @property
    def by_side(self) -> bool:
        return self._by_side

    def refusal(
        self, subcategory: str | None, role: DimensionRole
    ) -> UnsupportedDimensionReason | None:
        """Why this role cannot be searched for this kind, or None when it can.

        Read by side, a floor measurement is refused only where the piece's
        shape or the data's coverage cannot answer it (a rug's single side, a
        corner set); height still reads its column, refused where unreliable."""
        if subcategory is None:
            return UnsupportedDimensionReason.ROLE_NOT_DEFINED
        if (
            self._by_side
            and role is not DimensionRole.HEIGHT
            and subcategory in self._subcategories
        ):
            # Only furniture the registry records: a mirror or a canvas has no
            # floor sides, and a type nobody has looked at is not opened by
            # default.
            reason = self.unsupported_reason(subcategory, role)
            if self.source_axis(subcategory, role) is not None or reason in _ANSWERED_BY_SIDES:
                return None
            return reason
        if self.source_axis(subcategory, role) is not None:
            return None
        return self.unsupported_reason(subcategory, role)

    def measured_by(
        self, subcategory: str | None, role: DimensionRole, side: FloorSide | None
    ) -> SourceAxis | FloorSide | None:
        """What a search compares for this role: the piece's longer or shorter
        floor side, or a stored column - None when the role is refused."""
        if self.refusal(subcategory, role) is not None:
            return None
        if self._by_side and role is not DimensionRole.HEIGHT:
            return side_of(role, side) or self._width_side(subcategory)
        return self.source_axis(subcategory, role)

    def _width_side(self, subcategory: str | None) -> FloorSide:
        """A width whose side nobody said: the side the registry reads it on
        for this kind - a dining table's or a bed's width is its short side, a
        sofa's its long one - and the long side otherwise."""
        side = self.shown_by(subcategory).get(DimensionRole.OVERALL_WIDTH)
        return side if isinstance(side, FloorSide) else FloorSide.LONGER

    def shown_by(self, subcategory: str | None) -> Mapping[DimensionRole, SourceAxis | FloorSide]:
        """How each measurement of this kind is read when a piece's sizes are
        stated - a comparison row, what the designer is told.

        By side, the reviewed column becomes the side it stands for (`length`
        the long side, `width` the short one), and a kind refused only because
        its columns were unreliable is read as length on the long side and
        width on the short. Off, the reviewed columns, as before."""
        entry = self._subcategories.get(subcategory) if subcategory else None
        if entry is None:
            return {}
        if not self._by_side:
            return dict(entry.roles)
        shown: dict[DimensionRole, SourceAxis | FloorSide] = {
            role: _SIDE_OF_COLUMN.get(column, column) for role, column in entry.roles.items()
        }
        for role, side in (
            (DimensionRole.LENGTH, FloorSide.LONGER),
            (DimensionRole.OVERALL_WIDTH, FloorSide.SHORTER),
        ):
            if entry.unsupported.get(role) is UnsupportedDimensionReason.UNRELIABLE_AXIS_MAPPING:
                shown[role] = side
        return shown

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

    def reads_pair(self, subcategory: str | None) -> bool:
        """Whether two sides given together - "160 x 200" - can be searched
        for this kind: a rug's reviewed pair, or, read by side, any recorded
        piece whose shape has a single footprint (not a corner set)."""
        if self.planar_pair(subcategory) is not None:
            return True
        return (
            self._by_side
            and subcategory in self._subcategories
            and self.refusal(subcategory, DimensionRole.OVERALL_WIDTH) is None
        )

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
    path: Path | None = None,
    taxonomy: CommerceTaxonomy | None = None,
    *,
    by_side: bool = False,
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

    return DimensionSemantics(version=version, subcategories=subcategories, by_side=by_side)
