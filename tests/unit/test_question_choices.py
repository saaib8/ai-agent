"""Answers to tap follow what a question is about, never how it is worded."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.exceptions import TaxonomyConfigurationError
from app.schemas.agent_decision import (
    BlockingClarification,
    BlockingClarificationReason,
    FollowUpGoal,
)
from app.schemas.agent_turn import CustomerResponse
from app.services.question_choices import question_choices
from app.services.response_generator import _plain_punctuation
from app.taxonomy.attributes import AttributeFamily, load_catalog_attributes

ATTRIBUTES = load_catalog_attributes()


def _asking(subject: FollowUpGoal | None) -> BlockingClarification:
    return BlockingClarification(
        reason=BlockingClarificationReason.DETAIL_BEFORE_SEARCH,
        question="Anything worded any way at all?",
        subject=subject,
    )


def test_a_seats_question_offers_head_counts() -> None:
    choices = question_choices(_asking(FollowUpGoal.SEATING_REQUIREMENT), ATTRIBUTES)

    assert [c.label for c in choices] == [
        "2 people",
        "3 people",
        "4 people",
        "5 people",
        "6 or more",
    ]
    assert choices[0].value == "For 2 people"


@pytest.mark.parametrize(
    ("subject", "family"),
    [(FollowUpGoal.STYLE, AttributeFamily.STYLE), (FollowUpGoal.COLOR, AttributeFamily.COLOR)],
)
def test_a_look_question_offers_only_approved_values(
    subject: FollowUpGoal, family: AttributeFamily
) -> None:
    choices = question_choices(_asking(subject), ATTRIBUTES)
    offered = [c.label.replace(" ", "_") for c in choices[:-1]]

    assert offered == list(ATTRIBUTES.suggested(family))
    assert all(ATTRIBUTES.is_value(family, value) for value in offered)
    assert choices[-1].label == "Help me choose"


@pytest.mark.parametrize("subject", [None, FollowUpGoal.ROOM_SIZE, FollowUpGoal.USE_CASE])
def test_no_small_set_of_answers_means_no_chips(subject: FollowUpGoal | None) -> None:
    assert question_choices(_asking(subject), ATTRIBUTES) == ()
    assert question_choices(None, ATTRIBUTES) == ()


def test_without_the_vocabulary_a_look_question_offers_nothing() -> None:
    assert question_choices(_asking(FollowUpGoal.STYLE), None) == ()


def test_a_suggestion_outside_the_vocabulary_fails_at_startup(tmp_path: Path) -> None:
    registry = tmp_path / "attributes.yaml"
    registry.write_text(
        "version: t\ncolors: [Beige]\nstyles: [Modern]\nsuggested_styles: [Classic]\n",
        encoding="utf-8",
    )

    with pytest.raises(TaxonomyConfigurationError):
        load_catalog_attributes(registry)


# ── the long dash ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "sent"),
    [
        ("Oh, new sofas\u2014lovely.", "Oh, new sofas, lovely."),
        ("Sofas \u2013 nice.", "Sofas, nice."),
        ("They seat 5\u20137 people.", "They seat 5\u20137 people."),
    ],
)
def test_a_long_dash_between_words_becomes_a_comma(written: str, sent: str) -> None:
    assert _plain_punctuation(CustomerResponse(message=written)).message == sent
