"""What to ask before showing products of a kind, as reviewed data.

"I need a sofa" says what they want and nothing about which one. A card of
short questions - the kind of piece, the budget, colours, the feel, the style -
lets the search pick the few that suit them instead of the first few it finds
(CLAUDE.md 10.4). Which questions a product family is asked, and the choices
that are not live facts, are reviewed here - never written by a model and never
listed in code (CLAUDE.md 3.1). Budget bands, colours and styles are counted
from the live catalog when a card is built, so they are not listed at all.

A kind of piece is a subcategory, optionally with a seat count; a feel is a few
words that only rank. The catalog has no material field (CLAUDE.md 5), so a
feel orders cards by how well their descriptions match and never filters, and
nothing here claims what any product is made of.

The loader mirrors the other registries: read the versioned file, validate its
shape, and reject any subcategory the commerce taxonomy does not approve.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.arabic import parse_arabic_labels
from app.taxonomy.registry import CommerceTaxonomy

DEFAULT_BRIEFS_PATH: Final[Path] = Path(__file__).parent / "briefs_v1.yaml"

MAX_CHOICE_LABEL_CHARS: Final[int] = 24
"""A chip, not a sentence."""

MAX_FEEL_WORDS_CHARS: Final[int] = 60
"""A few descriptive words for ranking, not a query."""

MIN_CHOICES: Final[int] = 2
MAX_CHOICES: Final[int] = 8
"""A question with one answer is not a question; past eight it is a menu."""


class BriefQuestionKind(StrEnum):
    """What a card question asks. Each maps onto one part of the search."""

    TYPE = "type"
    """The kind of piece: a subcategory, and a seat count where it matters."""

    BUDGET = "budget"
    """A price band, counted from the store's live prices."""

    COLOUR = "colour"
    """Colours they like - a preference that ranks, never a filter (12.4)."""

    FEEL = "feel"
    """A fabric, finish or detail - words that rank, never filter."""

    STYLE = "style"
    """Styles they like - a preference that ranks, never a filter (12.4)."""


@dataclass(frozen=True, slots=True)
class KindChoice:
    """One kind of piece: a subcategory, and a seat count when it names one."""

    label: str
    subcategory: str
    seats: int | None = None
    min_seats: int | None = None

    @property
    def key(self) -> str:
        """Stable within a card: what a tapped chip sends back."""
        if self.seats is not None:
            return f"{self.subcategory}:{self.seats}"
        if self.min_seats is not None:
            return f"{self.subcategory}:{self.min_seats}+"
        return self.subcategory


@dataclass(frozen=True, slots=True)
class FeelChoice:
    label: str
    words: str
    """What ranks: descriptive words, in the language product names use."""


@dataclass(frozen=True, slots=True)
class Brief:
    """The card for one product family."""

    name: str
    subcategories: tuple[str, ...]
    ask: tuple[BriefQuestionKind, ...]
    categories: tuple[str, ...] = ()
    """Categories whose search named no kind, which this card settles by
    asking the kind first."""
    noun: str | None = None
    kinds: tuple[KindChoice, ...] = ()
    feel_label: str | None = None
    feels: tuple[FeelChoice, ...] = ()


class Briefs:
    """Every product family's card, keyed by the subcategories it covers."""

    def __init__(
        self, version: str, briefs: tuple[Brief, ...], arabic: Mapping[str, str] | None = None
    ) -> None:
        self._version = version
        self._briefs = briefs
        self._arabic: Mapping[str, str] = dict(arabic or {})
        self._by_type = {sub: brief for brief in briefs for sub in brief.subcategories}
        self._by_category = {cat: brief for brief in briefs for cat in brief.categories}

    @property
    def version(self) -> str:
        return self._version

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(brief.name for brief in self._briefs)

    def for_search(self, category: str, subcategory: str | None) -> Brief | None:
        """The card for a search: its kind's card, or - when no kind was
        named, as in "I need a table" - the card that asks the kind."""
        if subcategory is not None:
            return self._by_type.get(subcategory)
        return self._by_category.get(category)

    def for_type(self, subcategory: str | None) -> Brief | None:
        """The card for a product type, or None when it has no card.

        No subcategory means no reviewed type, and a card for an unknown piece
        would ask about the wrong things.
        """
        if subcategory is None:
            return None
        return self._by_type.get(subcategory)

    def arabic(self, label: str) -> str | None:
        """How one of the cards' labels - a kind, a feel, a feel's title, a noun -
        reads to a customer answered in Arabic. None only where a test built
        the cards by hand; the loader requires every one."""
        return self._arabic.get(label)

    def __repr__(self) -> str:
        return f"Briefs(version={self._version!r}, briefs={len(self._briefs)})"


def load_briefs(path: Path | None = None, taxonomy: CommerceTaxonomy | None = None) -> Briefs:
    """Load and validate the cards. Raises on anything malformed."""
    source = path or DEFAULT_BRIEFS_PATH
    try:
        document: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot read briefs at {source}: {type(exc).__name__}"
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
    raw = document.get("briefs")
    if not isinstance(raw, dict) or not raw:
        raise TaxonomyConfigurationError(detail=f"{source.name}: 'briefs' must be a mapping")

    briefs: list[Brief] = []
    claimed: set[str] = set()
    claimed_categories: set[str] = set()
    for name, entry in raw.items():
        if not isinstance(name, str) or not name:
            raise TaxonomyConfigurationError(detail=f"{source.name}: each card must be named")
        brief = _brief(name, entry, f"{source.name}: {name}", taxonomy)
        taken = claimed.intersection(brief.subcategories)
        if taken:
            # One type, one card: two would ask a sofa two different things.
            raise TaxonomyConfigurationError(
                detail=f"{source.name}: {name}: {sorted(taken)[0]} already has a card"
            )
        claimed.update(brief.subcategories)
        taken_categories = claimed_categories.intersection(brief.categories)
        if taken_categories:
            raise TaxonomyConfigurationError(
                detail=f"{source.name}: {name}: {sorted(taken_categories)[0]} already has a card"
            )
        claimed_categories.update(brief.categories)
        briefs.append(brief)
    labels = {
        label
        for brief in briefs
        for label in (
            brief.noun,
            brief.feel_label,
            *(kind.label for kind in brief.kinds),
            *(feel.label for feel in brief.feels),
        )
        if label is not None
    }
    arabic = parse_arabic_labels(document.get("arabic"), labels, where=source.name)
    return Briefs(version=version, briefs=tuple(briefs), arabic=arabic)


def _brief(name: str, raw: Any, where: str, taxonomy: CommerceTaxonomy | None) -> Brief:
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: must be a mapping")
    subcategories = _subcategories(raw.get("for"), where, taxonomy)
    ask = _ask(raw.get("ask"), where)
    kinds = _kinds(raw.get("type"), subcategories, where, taxonomy)
    if (BriefQuestionKind.TYPE in ask) != bool(kinds):
        raise TaxonomyConfigurationError(
            detail=f"{where}: kinds are listed exactly when the type is asked"
        )
    feel_label, feels = _feel(raw.get("feel"), where)
    if (BriefQuestionKind.FEEL in ask) != bool(feels):
        raise TaxonomyConfigurationError(
            detail=f"{where}: feels are listed exactly when the feel is asked"
        )
    categories = _categories(raw.get("for_category"), where, taxonomy)
    if categories and BriefQuestionKind.TYPE not in ask:
        # A search that named no kind is settled by asking it, never guessed.
        raise TaxonomyConfigurationError(
            detail=f"{where}: a card for a whole category must ask the kind"
        )
    noun = raw.get("noun")
    if noun is not None and (not isinstance(noun, str) or not noun.strip() or len(noun) > 30):
        raise TaxonomyConfigurationError(detail=f"{where}: 'noun' must be 1-30 characters")
    return Brief(
        name=name,
        subcategories=subcategories,
        ask=ask,
        categories=categories,
        noun=noun.strip() if isinstance(noun, str) else None,
        kinds=kinds,
        feel_label=feel_label,
        feels=feels,
    )


def _subcategories(raw: Any, where: str, taxonomy: CommerceTaxonomy | None) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or not all(isinstance(s, str) for s in raw):
        raise TaxonomyConfigurationError(detail=f"{where}: 'for' needs a list of subcategories")
    if len(raw) != len(set(raw)):
        raise TaxonomyConfigurationError(detail=f"{where}: 'for' repeats a subcategory")
    for subcategory in raw:
        if taxonomy is not None and not taxonomy.is_subcategory(subcategory):
            raise TaxonomyConfigurationError(
                detail=f"{where}: {subcategory} is not an approved subcategory"
            )
    return tuple(raw)


def _categories(raw: Any, where: str, taxonomy: CommerceTaxonomy | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw or not all(isinstance(c, str) for c in raw):
        raise TaxonomyConfigurationError(
            detail=f"{where}: 'for_category' needs a list of categories"
        )
    if len(raw) != len(set(raw)):
        raise TaxonomyConfigurationError(detail=f"{where}: 'for_category' repeats a category")
    for category in raw:
        if taxonomy is not None and not taxonomy.is_category(category):
            raise TaxonomyConfigurationError(
                detail=f"{where}: {category} is not an approved category"
            )
    return tuple(raw)


def _ask(raw: Any, where: str) -> tuple[BriefQuestionKind, ...]:
    if not isinstance(raw, list) or not raw:
        raise TaxonomyConfigurationError(detail=f"{where}: 'ask' needs a list of questions")
    try:
        ask = tuple(BriefQuestionKind(kind) for kind in raw)
    except ValueError as exc:
        raise TaxonomyConfigurationError(detail=f"{where}: unknown question in 'ask'") from exc
    if len(ask) != len(set(ask)):
        raise TaxonomyConfigurationError(detail=f"{where}: 'ask' repeats a question")
    return ask


def _kinds(
    raw: Any,
    subcategories: tuple[str, ...],
    where: str,
    taxonomy: CommerceTaxonomy | None,
) -> tuple[KindChoice, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not MIN_CHOICES <= len(raw) <= MAX_CHOICES:
        raise TaxonomyConfigurationError(
            detail=f"{where}: 'type' needs {MIN_CHOICES}-{MAX_CHOICES} kinds"
        )
    kinds = tuple(_kind(entry, subcategories, where, taxonomy) for entry in raw)
    _unique_labels([kind.label for kind in kinds], where)
    if len({kind.key for kind in kinds}) != len(kinds):
        raise TaxonomyConfigurationError(detail=f"{where}: 'type' repeats a kind")
    return kinds


def _kind(
    raw: Any, subcategories: tuple[str, ...], where: str, taxonomy: CommerceTaxonomy | None
) -> KindChoice:
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: each kind must be a mapping")
    label = _label(raw.get("label"), where)
    subcategory = raw.get("subcategory")
    if subcategory not in subcategories:
        # A kind outside the card's own types would turn "I need a sofa" into
        # a search for something else.
        raise TaxonomyConfigurationError(
            detail=f"{where}: {label} must name one of the card's subcategories"
        )
    seats, min_seats = raw.get("seats"), raw.get("min_seats")
    for value in (seats, min_seats):
        if value is not None and (not isinstance(value, int) or value < 1):
            raise TaxonomyConfigurationError(
                detail=f"{where}: {label} needs a positive whole seat count"
            )
    if seats is not None and min_seats is not None:
        raise TaxonomyConfigurationError(
            detail=f"{where}: {label} names an exact or a minimum seat count, not both"
        )
    if (seats is not None or min_seats is not None) and (
        taxonomy is not None and not taxonomy.is_pair("seating", subcategory)
    ):
        raise TaxonomyConfigurationError(detail=f"{where}: {label}: only seating has seats")
    return KindChoice(label=label, subcategory=subcategory, seats=seats, min_seats=min_seats)


def _feel(raw: Any, where: str) -> tuple[str | None, tuple[FeelChoice, ...]]:
    if raw is None:
        return None, ()
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: 'feel' must be a mapping")
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip() or len(label) > 30:
        raise TaxonomyConfigurationError(detail=f"{where}: the feel needs a label of 1-30")
    options = raw.get("options")
    if not isinstance(options, list) or not MIN_CHOICES <= len(options) <= MAX_CHOICES:
        raise TaxonomyConfigurationError(
            detail=f"{where}: the feel needs {MIN_CHOICES}-{MAX_CHOICES} options"
        )
    feels = tuple(_feel_choice(option, where) for option in options)
    _unique_labels([feel.label for feel in feels], where)
    return label.strip(), feels


def _feel_choice(raw: Any, where: str) -> FeelChoice:
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: each feel must be a mapping")
    label = _label(raw.get("label"), where)
    words = raw.get("words")
    if not isinstance(words, str) or not words.strip() or len(words) > MAX_FEEL_WORDS_CHARS:
        raise TaxonomyConfigurationError(
            detail=f"{where}: {label} needs words of 1-{MAX_FEEL_WORDS_CHARS} characters"
        )
    return FeelChoice(label=label, words=" ".join(words.split()))


def _label(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_CHOICE_LABEL_CHARS:
        raise TaxonomyConfigurationError(
            detail=f"{where}: each choice needs a label of 1-{MAX_CHOICE_LABEL_CHARS} characters"
        )
    return raw.strip()


def _unique_labels(labels: list[str], where: str) -> None:
    if len({label.casefold() for label in labels}) != len(labels):
        raise TaxonomyConfigurationError(detail=f"{where}: two choices share a label")
