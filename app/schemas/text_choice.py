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
