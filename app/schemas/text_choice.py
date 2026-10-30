"""Conversational answers proposed alongside a question, without executable actions."""

from pydantic import BaseModel, ConfigDict, Field


class TextReplyChoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=200)


def validate_text_choices(choices: tuple[TextReplyChoice, ...]) -> tuple[TextReplyChoice, ...]:
    """Repeated labels or answers are not distinct choices."""
    for field in ("label", "value"):
        values = [getattr(choice, field).casefold() for choice in choices]
        if len(set(values)) != len(values):
            raise ValueError(f"reply choices must have distinct {field}s")
    return choices


QUESTION_MARKS = ("?", "\u061f")
"""A question in either script: "?" or the Arabic question mark."""


def asks_a_question(*texts: str | None) -> bool:
    return any(mark in (text or "") for text in texts for mark in QUESTION_MARKS)


def require_a_question(choices: tuple[TextReplyChoice, ...], *texts: str | None) -> None:
    """Answers need a question to answer. Checked wherever choices are written,
    so a model that breaks it is told at once - and gets its one corrective
    attempt - rather than failing later, when the reply is assembled."""
    if choices and not asks_a_question(*texts):
        raise ValueError("reply choices require a question")
