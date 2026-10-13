"""What a card's chip must find in product names before it is offered.

The sheet's materials, features, shapes and sizes have no column in the
catalog (CLAUDE.md 5), so whether a store has anything a chip describes can be
read only from how its products are named: "Queen Fabric Storage Bed with
Gas-Lift Base". A chip is offered where some of the store's products of that
type carry its words - "Bed with lift-up storage" where names say gas-lift -
and left off where none do, so the card stays close to what the store sells.
A question whose names back too few chips to be a choice is still asked, in
the sheet's own chips (`ProductBriefBuilder`).

Names decide only whether a chip is worth offering. A product is never said to
be made of or to have what a chip names, and nothing is filtered on it: the
chip still only ranks (CLAUDE.md 10.13, 12.3).

Each chip's words are reviewed data: its `match` terms in
`subcategory_questions_v1.yaml`, or, where none are given, the distinctive
words of its own English label. Matching is deliberately plain - whole words,
an optional plural, a hyphen or space between words - and is the same in
PostgreSQL, which counts, and in Python, which tests the patterns: both read
the patterns built here, over names folded by the same tables.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

ENGLISH_FOLD_FROM: Final[str] = "àáâäãåèéêëìíîïòóôöõùúûüçñ"
ENGLISH_FOLD_TO: Final[str] = "aaaaaaeeeeiiiiooooouuuucn"
"""Accents an English name may carry and the plain letter it is read as, so
"Bouclé" and "Boucle" are one word. Applied after lower-casing."""

ARABIC_FOLD_FROM: Final[str] = "أإآٱىةـ"
ARABIC_FOLD_TO: Final[str] = "اااايه"
"""Arabic spellings read as one: every alef form as bare alef, alef maqsura as
ya, ta marbuta as ha. The tatweel - the last letter, with nothing to become -
is dropped, exactly as PostgreSQL's `translate` drops it."""

MAX_MATCH_TERMS: Final[int] = 12
"""A handful of words names use for one thing - "Animals and birds" is any of
the animals a store's figurines are named after - never a vocabulary."""
MAX_MATCH_TERM_CHARS: Final[int] = 60
"""As long as a chip's label may be, since a label's own words are its default."""

_ENGLISH_TERM: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:[ -][a-z0-9]+)*$")
_ARABIC_TERM: Final[re.Pattern[str]] = re.compile(r"^[ء-ي]+(?: [ء-ي]+)*$")

_WORD_EDGE_OPEN: Final[str] = "(^|[^a-z0-9])"
_WORD_EDGE_CLOSE: Final[str] = "($|[^a-z0-9])"
_BETWEEN_WORDS: Final[str] = "[ -]*"

STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "by",
        "design",
        "for",
        "in",
        "look",
        "of",
        "on",
        "or",
        "shape",
        "style",
        "the",
        "to",
        "type",
        "with",
    }
)
"""Words a label carries that say nothing a product name would: "Bed with
storage drawers" looks for "storage drawers". "Without", "no" and "armless"
are kept on purpose - a name rarely says what a piece lacks, so such a chip is
offered only where its own `match` terms say what to look for."""


@dataclass(frozen=True, slots=True)
class NameMatch:
    """The words one chip looks for, and the patterns that find them.

    Any term found is enough. `english` is matched against the lower-cased,
    accent-folded English name; `arabic` against the folded Arabic name. A
    pattern is None when no term is in that language.
    """

    terms: tuple[str, ...]
    english: str | None
    arabic: str | None


class NameMatchError(ValueError):
    """A term that cannot be matched as written. The loader reports it."""


def fold_english(text: str) -> str:
    """An English name as the patterns read it - the same folding as SQL."""
    return text.lower().translate(_ENGLISH_TABLE)


def fold_arabic(text: str) -> str:
    """An Arabic name as the patterns read it - the same folding as SQL."""
    return text.translate(_ARABIC_TABLE)


def name_match(terms: Sequence[str]) -> NameMatch:
    """The patterns for a chip's reviewed terms. Raises on a term that is not
    plain words in one script, which a pattern could misread."""
    if not 1 <= len(terms) <= MAX_MATCH_TERMS:
        raise NameMatchError(f"lists 1-{MAX_MATCH_TERMS} terms")
    english: list[str] = []
    arabic: list[str] = []
    for raw in terms:
        if not isinstance(raw, str) or not 1 <= len(raw.strip()) <= MAX_MATCH_TERM_CHARS:
            raise NameMatchError(f"each term is 1-{MAX_MATCH_TERM_CHARS} characters")
        term = " ".join(raw.split())
        folded_english = fold_english(term)
        folded_arabic = " ".join(fold_arabic(term).split())
        if _ENGLISH_TERM.match(folded_english):
            english.append(folded_english)
        elif _ARABIC_TERM.match(folded_arabic):
            arabic.append(folded_arabic)
        else:
            raise NameMatchError(
                f"{raw!r} must be English letters, digits, spaces and hyphens, or Arabic letters"
            )
    return NameMatch(
        terms=tuple(dict.fromkeys((*english, *arabic))),
        english=_english_pattern(english) if english else None,
        arabic=_arabic_pattern(arabic) if arabic else None,
    )


def label_terms(source: str, nouns: Iterable[str]) -> tuple[str, ...]:
    """The words a chip's own label looks for, when it names no `match`.

    Each "/" alternative is one term - "Leather / Faux leather" finds either -
    made of its distinctive words in order: no stop word, no figure ("90x200"
    is a frame size names do not carry) and none of the sub-category's own
    nouns, which every name of that type carries anyway. Empty when nothing
    distinctive is left.
    """
    excluded = {fold_english(noun) for noun in nouns} | STOP_WORDS
    found: list[str] = []
    for alternative in source.split("/"):
        words = [
            word
            for word in re.findall(r"[a-z0-9]+", fold_english(alternative))
            if word not in excluded and not any(char.isdigit() for char in word)
        ]
        if words:
            found.append(" ".join(words))
    return tuple(dict.fromkeys(found))


def noun_forms(*names: str) -> frozenset[str]:
    """The words of sub-category and category keys, singular and plural:
    `tv-table` is "tv", "table" and "tables"."""
    forms: set[str] = set()
    for name in names:
        for word in re.findall(r"[a-z0-9]+", fold_english(name)):
            forms.update({word, _stem(word), f"{_stem(word)}s", f"{_stem(word)}es"})
    return frozenset(forms)


def matches(match: NameMatch, english_name: str | None, arabic_name: str | None) -> bool:
    """Whether one product's names carry a chip's words - what SQL counts."""
    if match.english and english_name and re.search(match.english, fold_english(english_name)):
        return True
    return bool(match.arabic and arabic_name and re.search(match.arabic, fold_arabic(arabic_name)))


def _english_pattern(terms: Sequence[str]) -> str:
    phrases = "|".join(_BETWEEN_WORDS.join(_word(w) for w in re.split(r"[ -]", t)) for t in terms)
    return f"{_WORD_EDGE_OPEN}({phrases}){_WORD_EDGE_CLOSE}"


def _arabic_pattern(terms: Sequence[str]) -> str:
    # Arabic joins its articles and prepositions to the word ("بالخشب"), so a
    # term is found inside a word, never only as a whole one.
    return "(" + "|".join(" +".join(term.split(" ")) for term in terms) + ")"


def _word(word: str) -> str:
    """A word and its plural: "drawer" finds "drawers", never "drawerless"."""
    return f"{_stem(word)}(e?s)?"


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


_ENGLISH_TABLE: Final = str.maketrans(ENGLISH_FOLD_FROM, ENGLISH_FOLD_TO)
_ARABIC_TABLE: Final = str.maketrans(
    ARABIC_FOLD_FROM[: len(ARABIC_FOLD_TO)],
    ARABIC_FOLD_TO,
    ARABIC_FOLD_FROM[len(ARABIC_FOLD_TO) :],
)
