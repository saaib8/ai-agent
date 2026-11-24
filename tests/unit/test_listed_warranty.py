"""The warranty length a store wrote in a product's description - read only
beside the word warranty, and never guessed when the text is unclear."""

from __future__ import annotations

import pytest
from app.services.listed_warranty import ListedWarranty, in_arabic, warranty_in


@pytest.mark.parametrize(
    ("english", "expected"),
    [
        ("Foam: Al Rajhi Company Warranty: Two years (does not cover misuse)", {"years": 2}),
        ("Working hours: 20,000 hours Warranty: 2 years", {"years": 2}),
        ("foam resistant to frequent use. Comes with a five-year warranty.", {"years": 5}),
        (
            "The manufacturer provides a five-year warranty, and we offer the same warranty.",
            {"years": 5},
        ),
        ("Warranty: Two years from the agent, excluding misuse", {"years": 2}),
        ("A 10 year guarantee on the frame.", {"years": 10}),
        ("warranty for 18 months against defects", {"months": 18}),
        ("Warranty: 24 months", {"years": 2}),
    ],
)
def test_a_length_beside_the_word_warranty_is_read(english: str, expected: dict[str, int]) -> None:
    assert warranty_in(english, None) == ListedWarranty(**expected)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("الضمان: ضمان سنتين ضد عيوب الصناعة", {"years": 2}),
        ("ضمان 5 سنوات على الهيكل", {"years": 5}),
        ("ضمان لمدة خمس سنوات", {"years": 5}),
        ("ضمان سنة واحدة", {"years": 1}),
        ("ضمان ٣ سنوات", {"years": 3}),
        ("ضمان 6 أشهر", {"months": 6}),
    ],
)
def test_an_arabic_description_is_read_when_there_is_no_english(
    stored: str, expected: dict[str, int]
) -> None:
    assert warranty_in(None, stored) == ListedWarranty(**expected)


@pytest.mark.parametrize(
    "text",
    [
        "Strong, durable and sturdy. Delivered in 2 days.",
        "Comes with a lifetime warranty.",
        "We guarantee 100% satisfaction.",
        "Warranty included. Dimensions: 2 years old design, 200 cm long.",
        "5-year warranty on the frame and a 1 year warranty on the fabric.",
        "A 30-day guarantee.",
        "طباعة عالية الجودة لضمان 5 سنوات من الألوان الزاهية",
        "الضمان الأقوى لمدة خمسة وعشرين سنة",
    ],
)
def test_no_length_or_two_lengths_give_none(text: str) -> None:
    """Nothing beside the word, no unit we read, or two lengths: none is given
    rather than one guessed. "لضمان" is "to ensure", never a warranty; a length
    in words we do not read is none, not a part of it."""
    assert warranty_in(text, None) is None
    assert warranty_in(None, text) is None


def test_the_english_text_is_read_before_the_stored_one() -> None:
    assert warranty_in("Warranty: 3 years", "ضمان سنتين") == ListedWarranty(years=3)
    assert warranty_in("No warranty words here.", "ضمان سنتين") == ListedWarranty(years=2)


@pytest.mark.parametrize(
    ("length", "said"),
    [
        ({"years": 1}, "سنة واحدة"),
        ({"years": 2}, "سنتين"),
        ({"years": 5}, "5 سنوات"),
        ({"years": 25}, "25 سنة"),
        ({"months": 2}, "شهرين"),
        ({"months": 6}, "6 أشهر"),
        ({"months": 18}, "18 شهرًا"),
    ],
)
def test_the_length_is_worded_as_arabic_says_it(length: dict[str, int], said: str) -> None:
    """Two is one word in Arabic: the agent is given "سنتين", never "2 سنتين"."""
    assert in_arabic(ListedWarranty(**length)) == said
