"""What goes with what, as reviewed domain data.

When a customer opens a product they picked, the pieces offered beside it come
from here - never from a model and never from a list in code (CLAUDE.md 3.1).
A bed is offered nightstands because a person reviewed that pairing, not
because a model thought of it this turn.

Only product *types* are named. Which products fill them is Product Discovery's
job, and whether the store sells them at all is the live catalog's: a pairing
here is a permission to look, never a claim that anything is in stock
(CLAUDE.md 9.1).

The loader mirrors the other registries: read the versioned file, validate its
shape, and reject any type the commerce taxonomy does not approve, so this file
cannot drift from the vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.registry import CommerceTaxonomy

DEFAULT_COMPLEMENTS_PATH: Final[Path] = Path(__file__).parent / "complements_v1.yaml"

MAX_COMPANIONS: Final[int] = 6
"""A short, ordered list. Past this a product page becomes a catalogue."""

MAX_LABEL_CHARS: Final[int] = 30
"""A chip reads "Matching <label>"; the label has to fit on one."""


@dataclass(frozen=True, slots=True)
class Companion:
    """One type worth offering beside another, in customer words."""

    commerce_category: str
    commerce_subcategory: str
    label: str
    """Plural, lower case, as it reads on a chip: "Matching <label>"."""


class Complements:
    """Every product type's companions, in design priority order."""

    def __init__(self, version: str, companions: Mapping[str, tuple[Companion, ...]]) -> None:
        self._version = version
        self._companions = dict(companions)

    @property
    def version(self) -> str:
        return self._version

    @property
    def anchors(self) -> tuple[str, ...]:
        return tuple(self._companions)

    def for_type(self, subcategory: str | None) -> tuple[Companion, ...]:
        """The companions of one type, or none when the type has no pairings.

        No subcategory means no reviewed type, and a companion chosen for an
        unknown piece would be a guess.
        """
        if subcategory is None:
            return ()
        return self._companions.get(subcategory, ())

    def companion(self, anchor: str | None, category: str, subcategory: str) -> Companion | None:
        """The pairing, when this type really is one of the anchor's companions.

        What a chip may ask for. A client sends the type it was offered; the
        pairing is checked here rather than trusted, so a chip cannot become a
        way to run an arbitrary search.
        """
        return next(
            (
                entry
                for entry in self.for_type(anchor)
                if entry.commerce_category == category and entry.commerce_subcategory == subcategory
            ),
            None,
        )

    def __repr__(self) -> str:
        return f"Complements(version={self._version!r}, anchors={len(self._companions)})"


def load_complements(
    path: Path | None = None, taxonomy: CommerceTaxonomy | None = None
) -> Complements:
    """Load and validate the pairings. Raises on anything malformed."""
    source = path or DEFAULT_COMPLEMENTS_PATH
    try:
        document: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot read complements at {source}: {type(exc).__name__}"
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
    raw = document.get("complements")
    if not isinstance(raw, dict) or not raw:
        raise TaxonomyConfigurationError(detail=f"{source.name}: 'complements' must be a mapping")

    companions: dict[str, tuple[Companion, ...]] = {}
    for anchor, entries in raw.items():
        if not isinstance(anchor, str) or not anchor:
            raise TaxonomyConfigurationError(detail=f"{source.name}: each type must be named")
        where = f"{source.name}: {anchor}"
        if taxonomy is not None and not taxonomy.is_subcategory(anchor):
            raise TaxonomyConfigurationError(detail=f"{where}: not an approved subcategory")
        companions[anchor] = _companions(anchor, entries, where, taxonomy)
    return Complements(version=version, companions=companions)


def _companions(
    anchor: str, entries: Any, where: str, taxonomy: CommerceTaxonomy | None
) -> tuple[Companion, ...]:
    if not isinstance(entries, list) or not entries:
        raise TaxonomyConfigurationError(detail=f"{where}: needs a list of companions")
    if len(entries) > MAX_COMPANIONS:
        raise TaxonomyConfigurationError(detail=f"{where}: at most {MAX_COMPANIONS} companions")
    parsed = tuple(_companion(entry, where, taxonomy) for entry in entries)
    pairs = [(c.commerce_category, c.commerce_subcategory) for c in parsed]
    if len(pairs) != len(set(pairs)):
        raise TaxonomyConfigurationError(detail=f"{where}: repeats a companion")
    if any(c.commerce_subcategory == anchor for c in parsed):
        # "More sofas" beside a sofa is a similar-product search, which exists
        # already; offering it as a companion would be two paths to one thing.
        raise TaxonomyConfigurationError(detail=f"{where}: a type is not its own companion")
    return parsed


def _companion(raw: Any, where: str, taxonomy: CommerceTaxonomy | None) -> Companion:
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: each companion must be a mapping")
    category, subcategory, label = raw.get("category"), raw.get("subcategory"), raw.get("label")
    if not isinstance(category, str) or not isinstance(subcategory, str):
        raise TaxonomyConfigurationError(
            detail=f"{where}: a companion needs a category and a subcategory"
        )
    if taxonomy is not None and not taxonomy.is_pair(category, subcategory):
        raise TaxonomyConfigurationError(
            detail=f"{where}: {category}/{subcategory} is not an approved pair"
        )
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS:
        raise TaxonomyConfigurationError(
            detail=f"{where}: {subcategory} needs a label of 1-{MAX_LABEL_CHARS} characters"
        )
    return Companion(
        commerce_category=category,
        commerce_subcategory=subcategory,
        label=label.strip(),
    )
