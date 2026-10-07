"""A ready answer the customer can tap instead of typing.

Its own module so a turn result can carry the next step's chips without the
chat schema - which imports the turn result - being imported back.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.bundle_action import BundleActionRequest
from app.schemas.product_action import ProductActionRequest


class ReplyChoice(BaseModel):
    """A ready answer the customer can tap instead of typing.

    Catalog options and executable actions are built by the application.
    A model's validated conversational answers are projected here as text only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=200)
    product_action: ProductActionRequest | None = None
    """The action tapping it performs, when it is one - "Matching rugs" runs
    that companion search directly rather than asking a model to read `value`.
    `value` is then the words recorded for the customer's side of the turn."""
    bundle_action: BundleActionRequest | None = None
    """The room edit tapping it performs, for the yes/no on an over-budget swap:
    the client sends this structured action, not the words, so a tap answers the
    held offer deterministically (CLAUDE.md 3.6). At most one action is set."""

    @model_validator(mode="after")
    def _one_action_at_most(self) -> Self:
        if self.product_action is not None and self.bundle_action is not None:
            raise ValueError("a chip performs at most one kind of action")
        return self
