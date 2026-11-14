"""The vocabulary, built from the backend's tables as they are (CLAUDE.md 14.1).

Only the tables are read, never the products: a value a product carries that
no table names (written outside the pipeline, such as "Other") is not offered,
searched or leaned to; the product itself is still found and shown as stored.

The product-data pipeline owns these tables and checks every row before it
writes one: a type's name is the value products carry, spelling twins are one
row, every row has an Arabic name, and rows are only ever added. So nothing
here refuses a row. What this module adds is what the tables cannot say:

* a value still waiting for its Arabic name (`vocabulary_pending`) is live, and
  reads in English until its row arrives;
* a row that could not be used at all - a style with a space, which would split
  stored styles wrongly, or a type whose family is gone - is left out and
  logged, never a reason to stop.

`VocabularyTables` is what was read, plain and serialisable: it is the copy
saved as the last good one, and its fingerprint is the vocabulary's version.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.exceptions import CatalogUnavailableError
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes, spelling_key
from app.taxonomy.registry import CommerceTaxonomy

ValueKind = Literal["colour", "style", "material"]

FAMILY_OF_KIND: Mapping[ValueKind, AttributeFamily] = {
    "colour": AttributeFamily.COLOR,
    "style": AttributeFamily.STYLE,
    "material": AttributeFamily.MATERIAL,
}
"""The table's `kind` for each attribute family."""

WAITING_TYPE = "type"
"""`vocabulary_pending.kind` for a product type."""


class _Row(BaseModel):
    model_config = ConfigDict(frozen=True)


class FamilyRow(_Row):
    name: str
    arabic: str = ""


class KindRow(_Row):
    name: str
    family: str | None
    """The family's name, or None when the row's family is not in the table."""
    arabic: str = ""


class ValueRow(_Row):
    kind: ValueKind
    name: str
    arabic: str = ""


class WaitingRow(_Row):
    kind: str
    name: str
    family: str = ""


class VocabularyTables(_Row):
    """The rows as read, in table order (oldest first)."""

    families: tuple[FamilyRow, ...] = ()
    kinds: tuple[KindRow, ...] = ()
    values: tuple[ValueRow, ...] = ()
    waiting: tuple[WaitingRow, ...] = ()

    @property
    def version(self) -> str:
        """A fingerprint of the content, so caches keyed by the version change
        exactly when the vocabulary does."""
        digest = hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()
        return "db-" + digest[:12]


@dataclass(frozen=True, slots=True)
class BuiltVocabulary:
    taxonomy: CommerceTaxonomy
    attributes: CatalogAttributes
    skipped: tuple[str, ...]
    """Rows left out, each with why - for the log."""
    in_english: dict[str, list[str]]
    """Live values with no Arabic name yet, by family - for the log."""


def build_vocabulary(tables: VocabularyTables) -> BuiltVocabulary:
    """The taxonomy and the attributes from the tables. Refuses nothing but a
    vocabulary with no type, colour or style at all, which cannot serve a
    customer (`CatalogUnavailableError`, so the last good copy is used)."""
    skipped: list[str] = []
    in_english: dict[str, list[str]] = {}
    taxonomy = _taxonomy(tables, skipped, in_english)
    attributes = _attributes(tables, taxonomy.version, skipped, in_english)
    if not taxonomy.categories or not attributes.colors or not attributes.styles:
        raise CatalogUnavailableError(
            detail="the vocabulary tables hold no types, colours or styles"
        )
    return BuiltVocabulary(taxonomy, attributes, tuple(skipped), in_english)


def _taxonomy(
    tables: VocabularyTables, skipped: list[str], in_english: dict[str, list[str]]
) -> CommerceTaxonomy:
    families = {row.name.strip(): row for row in tables.families if row.name.strip()}
    kinds: dict[str, set[str]] = {}
    arabic = {name: row.arabic.strip() for name, row in families.items() if row.arabic.strip()}
    seen: set[str] = set()
    for row in tables.kinds:
        name = row.name.strip()
        if not name or name in seen:
            continue
        if row.family not in families:
            skipped.append(f"type {name!r}: its family is not in the table")
            continue
        seen.add(name)
        kinds.setdefault(row.family, set()).add(name)
        if row.arabic.strip():
            arabic[name] = row.arabic.strip()
    for waiting in tables.waiting:
        name = waiting.name.strip()
        if waiting.kind != WAITING_TYPE or not name or name in seen:
            continue
        if waiting.family not in families:
            skipped.append(
                f"waiting type {name!r}: its family {waiting.family!r} is not in the table"
            )
            continue
        seen.add(name)
        kinds.setdefault(waiting.family, set()).add(name)
    # A family with no type yet holds nothing to search; it joins with its first.
    categories = {family: frozenset(names) for family, names in kinds.items()}
    missing = sorted(name for names in categories.values() for name in names if name not in arabic)
    if missing:
        in_english["type"] = missing
    return CommerceTaxonomy(version=tables.version, categories=categories, arabic=arabic)


def _attributes(
    tables: VocabularyTables,
    version: str,
    skipped: list[str],
    in_english: dict[str, list[str]],
) -> CatalogAttributes:
    values: dict[AttributeFamily, dict[str, str]] = {family: {} for family in AttributeFamily}
    arabic: dict[AttributeFamily, dict[str, str]] = {family: {} for family in AttributeFamily}

    def add(kind: str, name: str, name_arabic: str = "") -> None:
        family = FAMILY_OF_KIND.get(kind)  # type: ignore[call-overload]
        name = name.strip()
        if family is None or not name:
            return
        if family is AttributeFamily.STYLE and " " in name:
            # Stored styles are split on commas after spaces are stripped, so a
            # style with a space could never be matched.
            skipped.append(f"style {name!r}: a style name has no spaces")
            return
        # Two spellings of one value would make it ambiguous; the first stays.
        if spelling_key(name) in values[family]:
            return
        values[family][spelling_key(name)] = name
        if name_arabic.strip():
            arabic[family][name] = name_arabic.strip()

    for row in tables.values:
        add(row.kind, row.name, row.arabic)
    for waiting in tables.waiting:
        add(waiting.kind, waiting.name)
    attributes = CatalogAttributes(
        version,
        {family: frozenset(by_key.values()) for family, by_key in values.items()},
        arabic,
    )
    for family in AttributeFamily:
        missing = sorted(
            v for v in attributes.values(family) if attributes.arabic(family, v) is None
        )
        if missing:
            in_english[str(family)] = missing
    return attributes
