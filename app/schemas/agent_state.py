"""Structured working memory for the future reasoning agents.

What belongs here is what a conversation needs to *remember*: the customer's
stated preferences, the task in progress, which products have been referred to,
and the system's own commercial read. Nothing else.

Three boundaries hold the design together, and each exists because crossing it
would create a second copy of something that already has an owner.

* **State remembers references; PostgreSQL remembers facts.** A product id here,
  never a name, price or URL. A remembered price is a wrong price.
* **Retailer scope is not state.** `store_id` lives on `RetailerContext`,
  supplied by application code, so no agent can change which catalog it sees.
* **Raw conversation is not state.** Messages belong to the conversation-history
  layer and reach an agent beside this object, never inside it.

Every model is frozen: a new state is built through the real constructors by
the reducer in `app/services/agent_state.py`, so every invariant is re-checked
on every transition rather than trusted to hold.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.discovery import PriceConstraint, ProductSearchRequest
from app.schemas.query import (
    ConstraintSemantics,
    SemanticPreference,
    validate_dimension_correspondence,
)

AGENT_STATE_VERSION: Literal["agent_state_v1"] = "agent_state_v1"
"""The state contract. Change the shape, change this."""

MAX_SEMANTIC_INTENT_CHARS = 200
"""A quality phrase is a few words. This bound is the only thing standing
between a durable search property and a smuggled transcript."""


def _no_duplicates(values: tuple[int, ...], field: str) -> tuple[int, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must not contain duplicate product ids")
    return values


class CustomerPreferenceState(BaseModel):
    """What the customer actually said they like, reusable across tasks.

    Only expressed preferences. A speculative inference that became durable
    state would be indistinguishable from something they told us.

    No global budget: a budget belongs to the task that has one, so there is
    exactly one per scope rather than three that can disagree. No material
    preference in V1 either - nothing can filter on it, so representing it
    would promise something the catalog cannot keep.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_preferences: tuple[SemanticPreference, ...] = ()


class ActiveSearchState(BaseModel):
    """The product discovery task in progress.

    Deliberately `ResolvedSearch` minus `semantic_text`, plus `revision`.
    Composed rather than wrapped, so the per-turn field cannot arrive by
    inheritance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ProductSearchRequest
    semantics: ConstraintSemantics = ConstraintSemantics()
    semantic_preferences: tuple[SemanticPreference, ...] = ()

    semantic_intent: str | None = None
    """Durable fuzzy wording that no structured field can hold - cosy, elegant,
    sleek.

    `SemanticPreference` covers colour and style only, so a quality word has no
    attribute family to be filed under and would otherwise vanish between
    turns: "show me cheaper ones" after "cosy modern sofas" would quietly
    become a search for modern sofas.

    It is never executable truth. Nothing filters on it, and no service reads
    its contents to make a decision.
    """

    revision: int = Field(ge=0)
    """Identifier of the most recently committed search result set.

    Zero means no results have been committed yet - criteria exist, nothing has
    been presented. One is the first presented result set, two the second.

    Not a version counter for this object: changing the criteria does not
    execute anything. It advances only when results are committed, which is why
    no update contract can set it and only
    :func:`~app.services.agent_state.commit_search_results` moves it.
    """

    @field_validator("semantic_intent")
    @classmethod
    def _normalise_intent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if len(trimmed) > MAX_SEMANTIC_INTENT_CHARS:
            raise ValueError(
                f"semantic_intent must be at most {MAX_SEMANTIC_INTENT_CHARS} characters"
            )
        return trimmed

    @model_validator(mode="after")
    def _check_dimensions(self) -> Self:
        validate_dimension_correspondence(self.request, self.semantics)
        return self


class ProductInteractionState(BaseModel):
    """Which products the conversation has referred to.

    References only. Order in `presented_product_ids` is what lets a later
    layer resolve "the second one", so it is a tuple rather than a set by
    design.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    presented_product_ids: tuple[int, ...] = ()
    presented_search_revision: int | None = None
    focused_product_id: int | None = None
    selected_product_ids: tuple[int, ...] = ()

    @field_validator("presented_product_ids", "selected_product_ids")
    @classmethod
    def _unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return _no_duplicates(value, "product id list")

    @model_validator(mode="after")
    def _focus_is_known(self) -> Self:
        """A focus on a product nobody has seen cannot be resolved.

        Selected products count as known: a customer may reach one by a path
        other than the current result list, so selection is deliberately not
        required to be a subset of what is presented.
        """
        if self.focused_product_id is None:
            return self
        known = set(self.presented_product_ids) | set(self.selected_product_ids)
        if self.focused_product_id not in known:
            raise ValueError(
                "focused_product_id must be a presented or selected product"
            )
        return self


class RoomProjectState(BaseModel):
    """A whole-room task's customer-supplied requirements.

    Minimal on purpose. Dimensions, slots, layouts and scores belong to M12,
    which will define them against real requirements rather than guesses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_type: str | None = None
    """Free-text customer context. **Never a catalog filter**: there is no
    approved room-type vocabulary, so letting this reach SQL would be an
    invented taxonomy value (CLAUDE.md 14.3)."""

    budget: PriceConstraint | None = None
    design_preferences: tuple[SemanticPreference, ...] = ()

    bundle_product_ids: tuple[int, ...] = ()
    """Products in the room bundle. Distinct from
    `ProductInteractionState.selected_product_ids`, which is a browsing
    shortlist - two different ideas that must not share a name."""

    locked_product_ids: tuple[int, ...] = ()

    @field_validator("bundle_product_ids", "locked_product_ids")
    @classmethod
    def _unique(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return _no_duplicates(value, "product id list")

    @model_validator(mode="after")
    def _locked_within_bundle(self) -> Self:
        """A locked product outside the bundle is unreachable."""
        if not set(self.locked_product_ids) <= set(self.bundle_product_ids):
            raise ValueError("locked_product_ids must be a subset of bundle_product_ids")
        return self


class PurchaseStage(StrEnum):
    EXPLORING = "exploring"
    CONSIDERING = "considering"
    HIGH_PURCHASE_INTENT = "high_purchase_intent"


class DerivedCommerceState(BaseModel):
    """The system's own commercial read, kept apart from what the customer said.

    `None` is not `EXPLORING`: absence means nothing has assessed the stage yet,
    the same way an absent `ConstraintStrength` is never read as consent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    purchase_stage: PurchaseStage | None = None


class AgentStateV1(BaseModel):
    """Structured working memory. Five domains, and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["agent_state_v1"] = AGENT_STATE_VERSION
    customer_preferences: CustomerPreferenceState = CustomerPreferenceState()
    active_search: ActiveSearchState | None = None
    product_interaction: ProductInteractionState = ProductInteractionState()
    room_project: RoomProjectState | None = None
    derived_commerce: DerivedCommerceState = DerivedCommerceState()

    @model_validator(mode="after")
    def _presented_matches_the_executed_search(self) -> Self:
        """A presented list must name the search lineage that produced it.

        Without this, "the second one" could resolve against results the
        customer can no longer see.
        """
        revision = self.product_interaction.presented_search_revision
        if revision is None:
            return self
        if self.active_search is None:
            raise ValueError(
                "presented_search_revision requires an active_search"
            )
        if revision != self.active_search.revision:
            raise ValueError(
                "presented_search_revision must equal active_search.revision"
            )
        if revision < 1:
            raise ValueError(
                "a presented result set implies a committed search revision"
            )
        return self
