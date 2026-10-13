"""What a sheet chip looks for in product names (`app/taxonomy/name_match.py`).

A chip the catalog has no field for is offered only where the store's names
carry its words. These pin the matching itself - whole words with their
plural, accents and Arabic spellings read as one, hyphen or space between
words - and that a pattern is never built from anything but plain words.
PostgreSQL runs the same patterns; tests/integration proves it counts alike.
"""

from __future__ import annotations

import re

import pytest
from app.taxonomy.name_match import (
    MAX_MATCH_TERMS,
    NameMatchError,
    fold_arabic,
    fold_english,
    label_terms,
    matches,
    name_match,
    noun_forms,
)


def _finds(terms: list[str], english: str | None = None, arabic: str | None = None) -> bool:
    return matches(name_match(terms), english, arabic)


@pytest.mark.parametrize(
    ("terms", "name"),
    [
        (["drawer"], "Nightstand with 2 Drawers - Walnut"),
        (["drawers"], "TV Stand with Drawer"),
        (["arms"], "Fabric Sofa Chair - Warm Grey - Rolled Arm"),
        (["boucle"], "Bouclé Accent Chair"),
        (["bouclé"], "Boucle Accent Chair"),
        (["gas lift"], "Queen Fabric Storage Bed with Gas-Lift Base"),
        (["gas-lift"], "Gaslift Storage Bed"),
        (["reading light"], "Single Upholstered Wingback Bed with Reading Light"),
        (["wood and metal"], "Wood and Metal Desk"),
        (["43 inch"], "TV Stand for 43 Inches TVs"),
        (["click clack"], "Sleeper Sofa with Click-Clack Fold-Out Bed"),
    ],
)
def test_a_term_finds_its_word_in_a_name(terms: list[str], name: str) -> None:
    assert _finds(terms, name)


@pytest.mark.parametrize(
    ("terms", "name"),
    [
        # A whole word, never a part of one: "arms" is not "armless".
        (["arms"], "Armless Accent Chair"),
        (["arm"], "Warm Beige Sofa"),
        # A phrase's words together: a colour is not a lamp.
        (["reading light"], "Queen Bed - Light Beige"),
        (["led"], "Curled Leg Side Table"),
        (["wool"], "Woollen Throw"),
    ],
)
def test_a_term_does_not_find_another_word(terms: list[str], name: str) -> None:
    assert not _finds(terms, name)


def test_arabic_spellings_are_read_as_one() -> None:
    # Alef forms, ta marbuta and alef maqsura, and a stretched word.
    assert _finds(["طاولة"], arabic="طاوله جانبيه")
    assert _finds(["أريكة"], arabic="اريكه مزدوجة")
    assert _finds(["مخمل"], arabic="كنبة مخـمـل")
    # Inside a word: Arabic joins its articles to it.
    assert _finds(["خشب"], arabic="طاولة بالخشب الطبيعي")
    assert not _finds(["خشب"], arabic="طاولة رخام")


def test_english_and_arabic_terms_each_read_their_own_name() -> None:
    match = name_match(["velvet", "مخمل"])

    assert match.english is not None and match.arabic is not None
    assert matches(match, "Velvet Sofa", None)
    assert matches(match, "Sofa", "كنبة مخمل")
    assert not matches(match, "Linen Sofa", "كنبة كتان")


def test_folding_is_the_one_sql_applies() -> None:
    assert fold_english("BOUCLÉ Crème") == "boucle creme"
    assert fold_arabic("إطار آمن مكتبةـ") == "اطار امن مكتبه"


@pytest.mark.parametrize(
    "bad",
    [
        ["drawer.*"],
        ["(arm|leg)"],
        ["drawer\\b"],
        ["wood مخمل"],
        ["  "],
        ["x" * 61],
        [f"word{n}" for n in range(MAX_MATCH_TERMS + 1)],
        [],
    ],
)
def test_only_plain_words_become_a_pattern(bad: list[str]) -> None:
    with pytest.raises(NameMatchError):
        name_match(bad)


def test_patterns_use_only_what_postgresql_reads_alike() -> None:
    match = name_match(["gas lift", "drawers", "مخمل"])

    # No Python-only escapes: SQL reads `\b` and `(?:` differently, or not at all.
    for pattern in (match.english, match.arabic):
        assert pattern is not None
        assert "\\" not in pattern and "(?" not in pattern
        re.compile(pattern)


@pytest.mark.parametrize(
    ("label", "terms"),
    [
        ("Bed with storage drawers", ("storage drawers",)),
        ("Leather / Faux leather", ("leather", "faux leather")),
        ("Single 90\u00d7200", ("single",)),
        ("Low and deep sofa", ("low deep",)),
        # What a piece lacks stays: a name rarely says it, so it is rarely offered.
        ("Bed without headboard", ("without headboard",)),
    ],
)
def test_a_label_looks_for_its_distinctive_words(label: str, terms: tuple[str, ...]) -> None:
    assert label_terms(label, noun_forms("bed", "sofa")) == terms


def test_a_label_of_only_the_types_own_nouns_looks_for_nothing() -> None:
    assert label_terms("Wall clock", noun_forms("wall-clock")) == ()
    assert noun_forms("tv-table") >= {"tv", "table", "tables"}
