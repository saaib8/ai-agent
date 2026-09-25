"""The approved controlled catalog-attribute vocabularies: colours and styles.

``catalog_attributes_v1.yaml`` is the single source of truth. Nothing else
enumerates a colour or a style - not a prompt, not an enum, not a validator.
Adding a value is a one-file change, exactly as it is for the commerce taxonomy
(CLAUDE.md 14.1).

The registry defines vocabulary only. It knows nothing about which products
carry which colour, or how many of anything a retailer stocks.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.exceptions import TaxonomyConfigurationError

DEFAULT_ATTRIBUTES_PATH: Final[Path] = Path(__file__).parent / "catalog_attributes_v1.yaml"


class AttributeFamily(StrEnum):
    """The catalog attributes a customer can express intent about."""

    COLOR = "color"
    STYLE = "style"


class CatalogAttributes:
    """Immutable view of the approved colour and style vocabularies."""

    def __init__(self, version: str, values: Mapping[AttributeFamily, frozenset[str]]) -> None:
        self._version = version
        self._values: Mapping[AttributeFamily, frozenset[str]] = dict(values)
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
        return self._by_spelling[family].get(_spelling_key(value))

    def __repr__(self) -> str:
        return (
            f"CatalogAttributes(version={self._version!r}, "
            f"colors={len(self.colors)}, styles={len(self.styles)})"
        )


def _spelling_key(value: str) -> str:
    """One value's spelling with case, spacing and separators set aside."""
    return " ".join(value.replace("_", " ").replace("-", " ").casefold().split())


def _spelling_index(family: AttributeFamily, members: frozenset[str]) -> dict[str, str]:
    """Spelling key to approved value, refusing two values that read the same.

    Checked here, at load time, so an ambiguous vocabulary is a startup
    failure rather than a lookup that silently picks one of two.
    """
    index: dict[str, str] = {}
    for member in members:
        key = _spelling_key(member)
        if key in index:
            raise TaxonomyConfigurationError(
                detail=f"{family} values {index[key]!r} and {member!r} differ only in spelling"
            )
        index[key] = member
    return index


def _parse_family(document: Any, key: str, *, source: str) -> frozenset[str]:
    raw = document.get(key)
    if not isinstance(raw, list) or not raw:
        raise TaxonomyConfigurationError(
            detail=f"{source}: '{key}' must be a non-empty list"
        )
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


def load_catalog_attributes(path: Path | None = None) -> CatalogAttributes:
    """Load and validate the vocabularies. Raises on anything malformed."""
    source = path or DEFAULT_ATTRIBUTES_PATH
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot read catalog attributes at {source}: {type(exc).__name__}"
        ) from exc
    try:
        document = yaml.safe_load(text)
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

    styles = _parse_family(document, "styles", source=source.name)
    if any(" " in style for style in styles):
        # Stored styles are split on commas after spaces are stripped, which is
        # only sound while no approved style contains one.
        raise TaxonomyConfigurationError(
            detail=f"{source.name}: style values must not contain spaces"
        )
    return CatalogAttributes(
        version=version,
        values={
            AttributeFamily.COLOR: _parse_family(document, "colors", source=source.name),
            AttributeFamily.STYLE: styles,
        },
    )
