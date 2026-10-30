"""Each sub-category's own questions, asked before its first products.

"I need a recliner" is answered with one card of that sub-category's questions
- the kind of recliner, the features that matter, the budget - from the
reviewed "ZORY Product Search" sheet (`subcategory_questions_v1.yaml`). The
sheet decides what is asked; the catalog decides the chips wherever it has the
data: the types the store stocks, the colours its products carry, budget bands
from its real prices. What the catalog cannot know - a material, a feature, a
shape - is offered in the sheet's words, which only rank (CLAUDE.md 5, 12.3).

The loader mirrors the other registries: read the versioned file, validate its
shape, and reject any sub-category or category the commerce taxonomy does not
approve. Switched off (`customer_agent.subcategory_questions`), nothing here is
read and the cards of `briefs_v1.yaml` are asked exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.exceptions import TaxonomyConfigurationError
from app.taxonomy.briefs import BriefQuestionKind
from app.taxonomy.name_match import (
    NameMatch,
    NameMatchError,
    label_terms,
    name_match,
    noun_forms,
)
from app.taxonomy.registry import CommerceTaxonomy

DEFAULT_SUBCATEGORY_QUESTIONS_PATH: Final[Path] = (
    Path(__file__).parent / "subcategory_questions_v1.yaml"
)

MAX_SHEET_CHOICES: Final[int] = 8
"""Chips shown per question: the most-stocked first where the catalog can count
them, the reviewed order otherwise."""

MAX_SHEET_TICKS: Final[int] = 3
"""Ticks a `multi` question takes together."""

MAX_SHEET_QUESTIONS: Final[int] = 3
"""Questions on one card: the sheet's three, exactly as it asks them - a
question that lists colours and patterns together stays one question."""

MAX_QUESTION_CHARS: Final[int] = 120
MAX_OPTION_CHARS: Final[int] = 60
"""A chip may be a short phrase ("Near-flat recline for sleeping"); it wraps."""

MAX_WORDS_CHARS: Final[int] = 60
"""What a chip ranks by: a few words, not a query."""


class SheetAsk(StrEnum):
    """What a question of the sheet asks, and so where its chips come from."""

    TYPE = "type"
    """The kind of piece: options that name a sub-category switch to it."""

    PEOPLE = "people"
    """How many it must seat."""

    COLOUR = "colour"
    """The catalog's own colours for the type - and, where the sheet asks
    "which color or pattern", its own words beside them, which only rank."""

    BUDGET = "budget"
    """Bands from the store's real prices."""

    DETAIL = "detail"
    """A material, feature, shape, size, pattern or use: words that rank."""

    @property
    def kind(self) -> BriefQuestionKind:
        """The card question it is drawn as. A detail is a feel - words that
        rank and never filter - so a card may hold several."""
        return _KINDS[self]

    @property
    def topic(self) -> str:
        """The subject a designer asks about, as a talking point for the agent -
        never the verbatim question, which the agent would only recite
        (CLAUDE.md 10.2). The agent asks about this in its own words."""
        return _TOPICS[self]


_KINDS: Final[dict[SheetAsk, BriefQuestionKind]] = {
    SheetAsk.TYPE: BriefQuestionKind.TYPE,
    SheetAsk.PEOPLE: BriefQuestionKind.PEOPLE,
    SheetAsk.COLOUR: BriefQuestionKind.COLOUR,
    SheetAsk.BUDGET: BriefQuestionKind.BUDGET,
    SheetAsk.DETAIL: BriefQuestionKind.FEEL,
}

_TOPICS: Final[dict[SheetAsk, str]] = {
    SheetAsk.TYPE: "the kind or style",
    SheetAsk.PEOPLE: "how many it seats",
    SheetAsk.COLOUR: "colour",
    SheetAsk.BUDGET: "budget",
    SheetAsk.DETAIL: "a detail (material, feature, shape or use)",
}

_LIVE: Final[frozenset[SheetAsk]] = frozenset({SheetAsk.COLOUR, SheetAsk.BUDGET})
"""Questions whose chips are the catalog's own. A colour question may add the
sheet's words beside its colours; a budget question lists none."""


@dataclass(frozen=True, slots=True)
class SheetOption:
    """One answer as the sheet words it, and what tapping it does."""

    label: str
    arabic: str
    words: str | None
    """What it ranks by, in the language product names use: its label unless
    the sheet says otherwise. None for a type answer that only switches the
    sub-category, and for a head count, which is not words."""
    subcategory: str | None = None
    """A type answer that switches the search to this sub-category."""
    seats: int | None = None
    min_seats: int | None = None
    people: int | None = None
    """A head count, for a people question."""
    or_more: bool = False
    names: NameMatch | None = None
    """What the store's product names must say for the chip to be offered
    (`app/taxonomy/name_match.py`): its reviewed `match` terms, or its own
    label's distinctive words. None for an answer the catalog counts another
    way - a type it switches to, by its stock; a head count, by its seats."""


@dataclass(frozen=True, slots=True)
class SheetQuestion:
    ask: SheetAsk
    text: str
    arabic: str
    multi: bool = False
    options: tuple[SheetOption, ...] = ()
    """The sheet's own answers. For a colour question, the words it asks
    beside the catalog's colours ("Striped", "Marble") - none for most."""
    options_first: bool = False
    """A colour question whose sheet lists its words before its colours -
    "Which material or color" - is drawn in that order."""

    @property
    def kind(self) -> BriefQuestionKind:
        return self.ask.kind


@dataclass(frozen=True, slots=True)
class SheetRow:
    """One sub-category's questions, in card order."""

    name: str
    subcategories: tuple[str, ...]
    categories: tuple[str, ...]
    questions: tuple[SheetQuestion, ...]
    noun: str | None = None
    arabic_noun: str | None = None


class SubcategoryQuestions:
    """Every row of the sheet, keyed by the sub-categories and categories it
    answers."""

    def __init__(self, version: str, rows: tuple[SheetRow, ...]) -> None:
        self._version = version
        self._rows = rows
        self._by_type = {sub: row for row in rows for sub in row.subcategories}
        self._by_category = {category: row for row in rows for category in row.categories}

    @property
    def version(self) -> str:
        return self._version

    @property
    def rows(self) -> tuple[SheetRow, ...]:
        return self._rows

    def for_search(self, category: str, subcategory: str | None) -> SheetRow | None:
        """The questions for a search: its sub-category's, or - when it named
        no kind, as in "I need a table" - the row that asks the kind. None for
        a type the sheet does not cover, which is asked as before."""
        if subcategory is not None:
            return self._by_type.get(subcategory)
        return self._by_category.get(category)

    def __repr__(self) -> str:
        return f"SubcategoryQuestions(version={self._version!r}, rows={len(self._rows)})"


def load_subcategory_questions(
    path: Path | None = None, taxonomy: CommerceTaxonomy | None = None
) -> SubcategoryQuestions:
    """Load and validate the sheet's questions. Raises on anything malformed."""
    source = path or DEFAULT_SUBCATEGORY_QUESTIONS_PATH
    try:
        document: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TaxonomyConfigurationError(
            detail=f"cannot read sub-category questions at {source}: {type(exc).__name__}"
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
    raw = document.get("rows")
    if not isinstance(raw, dict) or not raw:
        raise TaxonomyConfigurationError(detail=f"{source.name}: 'rows' must be a mapping")
    rows: list[SheetRow] = []
    claimed: set[str] = set()
    claimed_categories: set[str] = set()
    for name, entry in raw.items():
        where = f"{source.name}: {name}"
        if not isinstance(name, str) or not name:
            raise TaxonomyConfigurationError(detail=f"{source.name}: each row must be named")
        row = _row(name, entry, where, taxonomy)
        for taken in (claimed & set(row.subcategories), claimed_categories & set(row.categories)):
            if taken:
                # One type, one row: two would ask a sofa two different things.
                raise TaxonomyConfigurationError(
                    detail=f"{where}: {sorted(taken)[0]} already has a row"
                )
        claimed.update(row.subcategories)
        claimed_categories.update(row.categories)
        rows.append(row)
    return SubcategoryQuestions(version=version, rows=tuple(rows))


def _row(name: str, raw: Any, where: str, taxonomy: CommerceTaxonomy | None) -> SheetRow:
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: must be a mapping")
    _only(raw, {"for", "for_category", "noun", "questions"}, where)
    subcategories = _keys(raw.get("for"), f"{where}: for", taxonomy, subcategory=True)
    categories = _keys(raw.get("for_category", []), f"{where}: for_category", taxonomy)
    if not subcategories and not categories:
        raise TaxonomyConfigurationError(detail=f"{where}: names no sub-category or category")
    questions = raw.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_SHEET_QUESTIONS:
        raise TaxonomyConfigurationError(
            detail=f"{where}: 'questions' lists 1-{MAX_SHEET_QUESTIONS} questions"
        )
    noun, arabic_noun = None, None
    if raw.get("noun") is not None:
        noun, arabic_noun = _texts(raw["noun"], f"{where}: noun", 30)
    # Every name of the type carries its own nouns, so they tell no chip apart.
    nouns = noun_forms(*subcategories, *categories, *((noun,) if noun else ()))
    parsed = tuple(
        _question(question, f"{where}: question {index}", taxonomy, nouns)
        for index, question in enumerate(questions, start=1)
    )
    single = [question.ask for question in parsed if question.ask is not SheetAsk.DETAIL]
    if len(single) != len(set(single)):
        raise TaxonomyConfigurationError(
            detail=f"{where}: asks the type, people, colour or budget twice"
        )
    if categories and SheetAsk.TYPE not in single:
        # A search that named no kind is settled by asking it, never guessed.
        raise TaxonomyConfigurationError(
            detail=f"{where}: a row for a whole category must ask the type"
        )
    return SheetRow(
        name=name,
        subcategories=subcategories,
        categories=categories,
        questions=parsed,
        noun=noun,
        arabic_noun=arabic_noun,
    )


def _question(
    raw: Any, where: str, taxonomy: CommerceTaxonomy | None, nouns: frozenset[str]
) -> SheetQuestion:
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: must be a mapping")
    _only(raw, {"ask", "q", "multi", "options", "options_first"}, where)
    try:
        ask = SheetAsk(str(raw.get("ask")))
    except ValueError as exc:
        raise TaxonomyConfigurationError(detail=f"{where}: unknown 'ask'") from exc
    text, arabic = _texts(raw.get("q"), f"{where}: q", MAX_QUESTION_CHARS)
    multi = raw.get("multi", False)
    if not isinstance(multi, bool):
        raise TaxonomyConfigurationError(detail=f"{where}: 'multi' is true or false")
    if multi and ask is not SheetAsk.DETAIL:
        # Colours always take a few; a type, a head count or a budget one.
        raise TaxonomyConfigurationError(detail=f"{where}: only a detail may be 'multi'")
    options = raw.get("options")
    options_first = raw.get("options_first", False)
    if not isinstance(options_first, bool):
        raise TaxonomyConfigurationError(detail=f"{where}: 'options_first' is true or false")
    if options_first and (ask is not SheetAsk.COLOUR or options is None):
        raise TaxonomyConfigurationError(
            detail=f"{where}: only a colour question's own options may come first"
        )
    if ask in _LIVE and (ask is SheetAsk.BUDGET or options is None):
        if options is not None:
            raise TaxonomyConfigurationError(
                detail=f"{where}: a {ask.value} question's chips are the catalog's own"
            )
        return SheetQuestion(ask=ask, text=text, arabic=arabic)
    if not isinstance(options, list) or len(options) < 2:
        raise TaxonomyConfigurationError(detail=f"{where}: needs at least two options")
    parsed = tuple(
        _option(option, ask, f"{where}: option {index}", taxonomy, nouns)
        for index, option in enumerate(options, start=1)
    )
    labels = [option.label.casefold() for option in parsed]
    if len(labels) != len(set(labels)):
        raise TaxonomyConfigurationError(detail=f"{where}: an option is listed twice")
    return SheetQuestion(
        ask=ask,
        text=text,
        arabic=arabic,
        multi=multi,
        options=parsed,
        options_first=options_first,
    )


def _option(
    raw: Any,
    ask: SheetAsk,
    where: str,
    taxonomy: CommerceTaxonomy | None,
    nouns: frozenset[str],
) -> SheetOption:
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: must be a mapping")
    allowed = {"en", "ar", "words"}
    if ask is SheetAsk.TYPE:
        allowed |= {"subcategory", "seats", "min_seats"}
    if ask is SheetAsk.PEOPLE:
        allowed |= {"people", "or_more"}
    subcategory = raw.get("subcategory")
    ranks = subcategory is None and ask is not SheetAsk.PEOPLE
    if ranks:
        # Only an answer that ranks is looked for in names: a type it switches
        # to is offered by its stock, a head count by its seats.
        allowed |= {"match"}
    _only(raw, allowed, where)
    label, arabic = _texts(raw, where, MAX_OPTION_CHARS)
    words = raw.get("words", label if ranks else None)
    if words is not None and (
        not isinstance(words, str) or not words.strip() or len(words) > MAX_WORDS_CHARS
    ):
        raise TaxonomyConfigurationError(
            detail=f"{where}: 'words' must be 1-{MAX_WORDS_CHARS} characters"
        )
    if subcategory is not None and (
        not isinstance(subcategory, str)
        or (taxonomy is not None and not taxonomy.is_subcategory(subcategory))
    ):
        raise TaxonomyConfigurationError(
            detail=f"{where}: {subcategory} is not an approved subcategory"
        )
    seats, min_seats = _count(raw, "seats", where), _count(raw, "min_seats", where)
    if (seats is not None or min_seats is not None) and subcategory is None:
        raise TaxonomyConfigurationError(detail=f"{where}: a seat count needs a subcategory")
    if seats is not None and min_seats is not None:
        raise TaxonomyConfigurationError(detail=f"{where}: 'seats' or 'min_seats', not both")
    people = _count(raw, "people", where)
    if ask is SheetAsk.PEOPLE and people is None:
        raise TaxonomyConfigurationError(detail=f"{where}: a people answer needs 'people'")
    or_more = raw.get("or_more", False)
    if not isinstance(or_more, bool):
        raise TaxonomyConfigurationError(detail=f"{where}: 'or_more' is true or false")
    return SheetOption(
        label=label,
        arabic=arabic,
        words=words.strip() if words is not None else None,
        subcategory=subcategory,
        seats=seats,
        min_seats=min_seats,
        people=people,
        or_more=or_more,
        names=_names(raw, label, words, where, nouns) if ranks else None,
    )


def _names(
    raw: dict[str, Any], label: str, words: str | None, where: str, nouns: frozenset[str]
) -> NameMatch:
    """What product names must say for the chip to be offered: the reviewed
    `match` terms, or the distinctive words of what it ranks by."""
    terms = raw.get("match")
    if terms is None:
        terms = list(label_terms(words or label, nouns))
        if not terms:
            raise TaxonomyConfigurationError(
                detail=f"{where}: '{label}' has no word a name could carry; give it 'match'"
            )
    elif not isinstance(terms, list):
        raise TaxonomyConfigurationError(detail=f"{where}: 'match' must be a list of terms")
    try:
        return name_match(terms)
    except NameMatchError as exc:
        raise TaxonomyConfigurationError(detail=f"{where}: 'match' {exc}") from exc


def _texts(raw: Any, where: str, limit: int) -> tuple[str, str]:
    """The English and Arabic wording of one label: both, each 1-`limit`."""
    if not isinstance(raw, dict):
        raise TaxonomyConfigurationError(detail=f"{where}: needs 'en' and 'ar'")
    texts = []
    for language in ("en", "ar"):
        text = raw.get(language)
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            raise TaxonomyConfigurationError(
                detail=f"{where}: '{language}' must be 1-{limit} characters"
            )
        texts.append(text.strip())
    return texts[0], texts[1]


def _keys(
    raw: Any, where: str, taxonomy: CommerceTaxonomy | None, *, subcategory: bool = False
) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(key, str) and key for key in raw):
        raise TaxonomyConfigurationError(detail=f"{where}: must be a list of names")
    for key in raw:
        approved = (
            taxonomy is None
            or (subcategory and taxonomy.is_subcategory(key))
            or (not subcategory and taxonomy.is_category(key))
        )
        if not approved:
            noun = "subcategory" if subcategory else "category"
            raise TaxonomyConfigurationError(detail=f"{where}: {key} is not an approved {noun}")
    return tuple(raw)


def _count(raw: dict[str, Any], field: str, where: str) -> int | None:
    value: object = raw.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TaxonomyConfigurationError(detail=f"{where}: '{field}' must be a positive count")
    return value


def _only(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise TaxonomyConfigurationError(detail=f"{where}: unknown field {sorted(unknown)[0]}")
