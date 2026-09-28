"""Answers to tap, for a question the agent asks.

Built from what the question is *about* - the subject the decision step
recorded - never from its wording. The writer phrases the question freely, so
a client guessing answers from the words ("style" -> style chips) breaks the
moment it says "what kind of look"; the subject does not change with the words.

Colours and styles come from the reviewed suggestions in the attribute
registry, so a chip can only ever offer an approved value.
"""

from __future__ import annotations

from app.schemas.agent_decision import BlockingClarification, FollowUpGoal
from app.schemas.chat import ReplyChoice
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes

_SEAT_COUNTS = (2, 3, 4, 5)

_LEAVE_IT_TO_YOU = ReplyChoice(label="Help me choose", value="I'm not sure yet - help me choose")


def question_choices(
    question: BlockingClarification | None, attributes: CatalogAttributes | None
) -> tuple[ReplyChoice, ...]:
    """The answers to offer beside a question asked before anything is shown."""
    if question is None:
        return ()
    return choices_for(question.subject, attributes)


def choices_for(
    subject: FollowUpGoal | None, attributes: CatalogAttributes | None
) -> tuple[ReplyChoice, ...]:
    """The answers to offer for a question about this subject, or none.

    None for a question with no recorded subject, or a subject with no small
    set of answers (a room size, how a room is used): showing no chips is
    better than showing the wrong ones.
    """
    match subject:
        case FollowUpGoal.SEATING_REQUIREMENT:
            return (
                *(ReplyChoice(label=f"{n} people", value=f"For {n} people") for n in _SEAT_COUNTS),
                ReplyChoice(label="6 or more", value="For 6 or more people"),
            )
        case FollowUpGoal.STYLE if attributes is not None:
            return _suggested(attributes, AttributeFamily.STYLE, "style")
        case FollowUpGoal.COLOR if attributes is not None:
            return _suggested(attributes, AttributeFamily.COLOR, "colours")
        case _:
            return ()


def _suggested(
    attributes: CatalogAttributes, family: AttributeFamily, noun: str
) -> tuple[ReplyChoice, ...]:
    suggested = attributes.suggested(family)
    if not suggested:
        return ()
    return (
        *(
            ReplyChoice(label=_words(value), value=f"{_words(value)} {noun}")
            for value in suggested
        ),
        _LEAVE_IT_TO_YOU,
    )


def _words(value: str) -> str:
    """`Mid_Century` as a person would write it."""
    return value.replace("_", " ")
