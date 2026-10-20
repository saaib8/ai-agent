"""Figures a shop writes in words or as "k" amounts are its own figures."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.schemas.store_knowledge import KnowledgePassage
from app.services.numeric_guard import build_allowance, check_numeric_policy, policy_figures
from app.services.written_numbers import written_figures

QUESTION = "is delivery free in Riyadh for a 1000 SAR order?"
MESSY = KnowledgePassage(
    title="policies.pdf",
    page=1,
    text=(
        "delivery in riyadh is free if u spend more than one thousand two hundred riyals "
        "otherwise its 89. returns: fourteen days. refund in about ten working days. "
        "bank transfer only for orders over 10k."
    ),
)


@pytest.mark.parametrize(
    ("text", "figures"),
    [
        ("returns within fourteen days", {14}),
        ("free above one thousand two hundred riyals", {1200}),
        ("one hundred and fifty SAR", {150}),
        ("twenty-five days", {25}),
        ("two to four working days", {2, 4}),
        ("orders over 10k", {10000}),
        ("up to 12.5K SAR", {12500}),
    ],
)
def test_words_and_k_amounts_are_read_exactly(text: str, figures: set[int]) -> None:
    assert set(written_figures(text)) == {Decimal(f) for f in figures}


@pytest.mark.parametrize(
    "text", ["takes up to a week", "a few days", "2 to 4 working days", "10kg box"]
)
def test_nothing_is_read_where_no_figure_is_written_in_words(text: str) -> None:
    assert written_figures(text) == ()


def test_a_figure_the_shop_wrote_in_words_is_sayable_in_digits() -> None:
    allowance = build_allowance(QUESTION, figures=policy_figures((MESSY,)))

    reply = (
        "No - delivery in Riyadh is free above 1,200 SAR, so it costs 89 SAR. "
        "Refunds take about 10 working days, returns 14 days."
    )

    assert check_numeric_policy(message=reply, follow_up_question=None, allowance=allowance) is None


def test_a_k_amount_is_sayable_in_full() -> None:
    allowance = build_allowance(QUESTION, figures=policy_figures((MESSY,)))

    reply = "Bank transfer is only for orders over 10,000 SAR."

    assert check_numeric_policy(message=reply, follow_up_question=None, allowance=allowance) is None


def test_a_figure_the_shop_never_wrote_is_still_refused() -> None:
    allowance = build_allowance(QUESTION, figures=policy_figures((MESSY,)))

    reply = "Delivery in Riyadh costs 99 SAR below 1,500 SAR."

    assert check_numeric_policy(message=reply, follow_up_question=None, allowance=allowance)
