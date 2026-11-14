"""The catalog-attribute vocabularies: colours, styles and materials.

The database is the one source (`product_attribute_value`, read by
`app/repositories/vocabulary.py`): every value with its Arabic name, kept by the
product-data pipeline, with the values still waiting for their Arabic name
(`app/taxonomy/tables.py`) - those shown in English. A value only products
carry, no table naming it, is not part of the vocabulary. Nothing else
enumerates a value - not a prompt, not an enum, not a validator.

The vocabulary knows nothing about which products carry which value, or how
many of anything a retailer stocks: that is the store's shelf, read per store.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.arabic import parse_arabic_labels


class AttributeFamily(StrEnum):
    """The catalog attributes a customer can express intent about."""

    COLOR = "color"
    STYLE = "style"
    MATERIAL = "material"
    """The product's one main material (CLAUDE.md 5): a leaning that orders,
    never a filter."""


class CatalogAttributes:
    """Immutable view of the colour, style and material vocabularies."""

    def __init__(
        self,
        version: str,
        values: Mapping[AttributeFamily, frozenset[str]],
        arabic: Mapping[AttributeFamily, Mapping[str, str]] | None = None,
    ) -> None:
        self._version = version
        self._values: Mapping[AttributeFamily, frozenset[str]] = {
            family: frozenset(values.get(family, ())) for family in AttributeFamily
        }
        self._arabic: Mapping[AttributeFamily, Mapping[str, str]] = dict(arabic or {})
        self._by_spelling: Mapping[AttributeFamily, Mapping[str, str]] = {
            family: _spelling_index(family, members) for family, members in self._values.items()
        }

    @property
    def version(self) -> str:
        return self._version

    @property
    def colors(self) -> frozenset[str]:
        return self._values[AttributeFamily.COLOR]

    @property
    def styles(self) -> frozenset[str]:
        return self._values[AttributeFamily.STYLE]

    @property
    def materials(self) -> frozenset[str]:
        return self._values[AttributeFamily.MATERIAL]

    def values(self, family: AttributeFamily) -> frozenset[str]:
        return self._values[family]

    def is_color(self, value: str) -> bool:
        return value in self.colors

    def is_style(self, value: str) -> bool:
        return value in self.styles

    def is_value(self, family: AttributeFamily, value: str) -> bool:
        """Membership in one family only, so a colour can never pass as a style."""
        return value in self._values[family]

    def canonical(self, family: AttributeFamily, value: str) -> str | None:
        """The approved value this spelling names, or None.

        Spelling only: case, spacing, underscores and hyphens are ignored, so
        "beige", "light grey" and "modern classic" find `Beige`, `Light Grey`
        and `Modern_Classic`. A different word is never mapped onto an approved
        one - "dark grey" finds nothing here, because choosing which approved
        colours it means is interpretation, not spelling (CLAUDE.md 14.2).
        """
        return self._by_spelling[family].get(spelling_key(value))

    def arabic(self, family: AttributeFamily, value: str) -> str | None:
        """How an approved value reads to a customer answered in Arabic.

        Display only - products, filters and decisions keep the English value.
        None for a value with no Arabic name - one a product carries that the
        table does not hold yet; the caller shows the English then.
        """
        return self._arabic.get(family, {}).get(value)

    def __repr__(self) -> str:
        return (
            f"CatalogAttributes(version={self._version!r}, "
            f"colors={len(self.colors)}, styles={len(self.styles)}, "
            f"materials={len(self.materials)})"
        )


def spelling_key(value: str) -> str:
    """One value's spelling with case, spacing and separators set aside."""
    return " ".join(value.replace("_", " ").replace("-", " ").casefold().split())


def _spelling_index(family: AttributeFamily, members: frozenset[str]) -> dict[str, str]:
    """Spelling key to approved value, refusing two values that read the same.

    Checked here, at load time, so an ambiguous vocabulary is a startup
    failure rather than a lookup that silently picks one of two.
    """
    index: dict[str, str] = {}
    for member in members:
        key = spelling_key(member)
        if key in index:
            raise TaxonomyConfigurationError(
                detail=f"{family} values {index[key]!r} and {member!r} differ only in spelling"
            )
        index[key] = member
    return index


def _parse_family(document: Any, key: str, *, source: str) -> frozenset[str]:
    raw = document.get(key)
    if not isinstance(raw, list) or not raw:
        raise TaxonomyConfigurationError(detail=f"{source}: '{key}' must be a non-empty list")
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise TaxonomyConfigurationError(
                detail=f"{source}: {key} contains a non-string or blank value: {value!r}"
            )
        if value in seen:
            raise TaxonomyConfigurationError(
                detail=f"{source}: {key} lists {value!r} more than once"
            )
        seen.add(value)
    return frozenset(seen)


def parse_catalog_attributes(document: Any, *, source: str) -> CatalogAttributes:
    """Validate a vocabulary document - ``version``, ``colors``, ``styles``,
    optional ``materials``, and ``arabic`` for each - into the attributes.
    Raises on anything malformed: every value named once, in Arabic, and no
    style with a space."""
    if not isinstance(document, dict):
        raise TaxonomyConfigurationError(detail=f"{source}: top level must be a mapping")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise TaxonomyConfigurationError(detail=f"{source}: 'version' must be a non-empty string")
    styles = _parse_family(document, "styles", source=source)
    if any(" " in style for style in styles):
        # Stored styles are split on commas after spaces are stripped, which is
        # only sound while no approved style contains one.
        raise TaxonomyConfigurationError(detail=f"{source}: style values must not contain spaces")
    values = {
        AttributeFamily.COLOR: _parse_family(document, "colors", source=source),
        AttributeFamily.STYLE: styles,
        AttributeFamily.MATERIAL: (
            _parse_family(document, "materials", source=source)
            if document.get("materials")
            else frozenset()
        ),
    }
    return CatalogAttributes(
        version=version,
        values=values,
        arabic=_parse_arabic(document, values, source=source),
    )


def _parse_arabic(
    document: Any, values: Mapping[AttributeFamily, frozenset[str]], *, source: str
) -> dict[AttributeFamily, dict[str, str]]:
    """Every approved value's Arabic display name - exactly one each."""
    raw = document.get("arabic")
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{source}: 'arabic' must be a mapping")
    return {
        family: parse_arabic_labels(raw.get(key), values[family], where=f"{source}: {key}")
        for family, key in (
            (AttributeFamily.COLOR, "colors"),
            (AttributeFamily.STYLE, "styles"),
            (AttributeFamily.MATERIAL, "materials"),
        )
        if values[family]
    }
