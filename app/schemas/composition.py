"""What deterministic search composition returns.

Three outcomes, kept apart because they are for three different audiences.

* A **composed search** is the candidate criteria plus the query to execute.
* A **clarification** is a question for the customer: they said "under 3000"
  and nothing in the conversation says which currency.
* A **defect** is the caller's problem: a relative price nobody resolved, or a
  colour the approved vocabulary does not contain. Reporting one of these as a
  clarification would ask the customer to fix a wiring mistake.

`NewTaskRequired` is a fourth, and it is not a failure at all: the customer
changed product family, so this is not a refinement of the search in progress
and the coordinator should route it through the full new-search path.

Nothing here carries prose. Deterministic services produce reason codes; the
words belong to the conversational layer (CLAUDE.md 3.3).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.schemas.agent_decision import BlockingClarificationReason
from app.schemas.agent_state import ActiveSearchState
from app.schemas.grounding import DroppedConstraint
from app.schemas.query import ResolvedSearch


class CompositionDefect(StrEnum):
    """The caller supplied something the composer must not act on.

    Distinct from a clarification on purpose. "We have not resolved the
    reference price yet" is not a question anyone can put to a customer, and
    dressing it as one would hide a sequencing bug behind a polite sentence.
    """

    NO_ACTIVE_SEARCH = "no_active_search"
    """A refinement arrived with no search in progress to refine."""

    RELATIVE_PRICE_NOT_RESOLVED = "relative_price_not_resolved"
    """A relative price reached the composer still relative.

    Turning "cheaper than that one" into a bound needs the reference product's
    current price, which is a catalog fact. The layer above resolves it into an
    absolute operation first; reaching here means it did not.
    """

    UNAPPROVED_ATTRIBUTE_VALUE = "unapproved_attribute_value"
    """A canonical colour or style the registry does not contain.

    The schema says a value is canonical; only the registry says whether it is
    (CLAUDE.md 14.3). An unapproved value is refused, never filtered on.
    """

    MALFORMED_AMOUNT = "malformed_amount"
    """A number arrived as something that is not one."""

    ONE_SEAT_ON_MULTI_SEAT_TYPE = "one_seat_on_multi_seat_type"
    """A seat count of one on a type that always seats several - "make them
    single seaters" read as a one-seat sofa. A piece for one person is its own
    product type, so the decision is corrected rather than executed."""


class ComposedSearch(BaseModel):
    """A candidate search, ready to execute but committed to nothing.

    Two representations of one decision, produced together so they cannot
    disagree: `candidate` is what becomes state if execution succeeds, and
    `resolved` is what executes. Promotion and the revision bump belong to the
    coordinator, after a search actually runs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate: ActiveSearchState
    resolved: ResolvedSearch
    dropped_constraints: tuple[DroppedConstraint, ...] = ()
    """Sizes the previous product type had that this search does not apply.

    Reported rather than dropped quietly: presenting results as satisfying a
    measurement that was never applied would be a false claim.
    """

    earlier_sizes_applied: bool = False
    """The sizes the customer gave earlier for this product type were applied
    again, so the reply says so rather than let a forgotten limit surprise
    them."""


class CompositionNeedsClarification(BaseModel):
    """One question must be answered before this search can be built."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: BlockingClarificationReason


class CompositionFailed(BaseModel):
    """The composer was asked to do something it must refuse."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    defect: CompositionDefect


class NewTaskRequired(BaseModel):
    """The product family changed, so this is not a refinement.

    The commerce category is the customer's basic intent: someone who asked
    for seating and now asks for tables has started a different task, and no
    composition may quietly carry the old one's constraints across
    (CLAUDE.md 13.3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str


CompositionOutcome = (
    ComposedSearch | CompositionNeedsClarification | CompositionFailed | NewTaskRequired
)
"""A search, a question, a refusal, or "that is a different task"."""
