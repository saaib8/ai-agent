"""What the response model may see, and which branches ever reach it.

The design decision underneath every test here: the response model receives no
product fact at all. That is why the numeric guard can be small, why prose
cannot repeat a name or a price, and why catalog-controlled text never enters a
prompt. What crosses the boundary is counts and enum members.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    ProductInteractionIntent,
)
from app.schemas.agent_turn import CustomerResponse
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.grounding import (
    DroppedConstraint,
    GroundedProduct,
    RelaxationSummaryItem,
    SearchExecutionGrounding,
    SearchOutcome,
)
from app.schemas.product import CommerceClassification
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.query import ConstraintStrength
from app.schemas.relaxation import RelaxableField, StopReason
from app.taxonomy.dimensions import DimensionRole, UnsupportedDimensionReason
from pydantic import BaseModel

APP = Path(__file__).parents[2] / "app"


def _decision(
    action: AgentAction, interaction: ProductInteractionIntent | None = None
) -> CustomerAgentDecision:
    """A contract-valid decision for each action.

    Some actions require a payload - a comparison needs references, a detail
    needs one - so the minimum is supplied rather than letting an invalid
    decision stand in for a real turn.
    """
    payload: dict[str, Any] = {"action": action, "interaction": interaction}
    if action is AgentAction.COMPARE:
        payload["comparison_references"] = (
            PresentedOrdinal(position=1),
            PresentedOrdinal(position=2),
        )
    elif action is AgentAction.PRODUCT_DETAIL:
        payload["reference"] = PresentedOrdinal(position=1)
    return CustomerAgentDecision(**payload)


def _product(ref: int, *, ordinal: int | None = None) -> GroundedProduct:
    return GroundedProduct(
        grounding_ref=ref,
        presented_ordinal=ordinal,
        name_english="Aurora Three Seater",
        price_amount=Decimal("4299"),
        price_unit="SAR",
        image_url="https://example.test/a.jpg",
        product_url="https://example.test/a",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
        relaxation_depth=0,
    )


def _search(
    *, count: int = 2, relaxed: bool = False, dropped: bool = False
) -> SearchExecutionGrounding:
    products = tuple(_product(n, ordinal=n) for n in range(1, count + 1))
    return SearchExecutionGrounding(
        outcome=SearchOutcome.RESULTS if count else SearchOutcome.ZERO_RESULTS,
        products=products,
        eligible_count=count,
        ranked_count=count,
        selected_count=count,
        presented_count=count,
        # A widened search is one whose exact pool was too small: leaving this
        # equal to the presented count would describe a search that widened
        # with nothing to gain.
        exact_candidate_count=0 if relaxed else count,
        was_relaxed=relaxed,
        relaxations=(
            (
                RelaxationSummaryItem(
                    field=RelaxableField.PRICE_MAX,
                    strength=ConstraintStrength.APPROXIMATE,
                    original_value="5000",
                    applied_value="5500",
                ),
            )
            if relaxed
            else ()
        ),
        stop_reason=StopReason.EXACT_SUFFICIENT,
        dropped_constraints=(
            (
                DroppedConstraint(
                    role=DimensionRole.DEPTH,
                    reason=UnsupportedDimensionReason.ROLE_NOT_DEFINED,
                ),
            )
            if dropped
            else ()
        ),
    )


# ── which branches reach a model ────────────────────────────────────────────




# ── what the projection carries ─────────────────────────────────────────────




# ── the authority walk ──────────────────────────────────────────────────────


def _reachable_names(model: type[BaseModel], seen: set[type] | None = None) -> list[str]:
    seen = seen if seen is not None else set()
    if model in seen:
        return []
    seen.add(model)
    names: list[str] = []
    for name, field in model.model_fields.items():
        names.append(name)
        for arg in (field.annotation, *getattr(field.annotation, "__args__", ())):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                names.extend(_reachable_names(arg, seen))
    return names


# What the response model may and may not be told about a product changed with
# the screen-awareness pass. It may read the merchandise the customer is
# looking at - a name, a price, a capacity, a colour - because a salesperson
# beside five sofas can see them, and an agent that cannot is reduced to "here
# are five options" (CLAUDE.md 2, 15).
#
# What it may never read is *identity*: anything that could address a
# repository, name a retailer, or be emitted as a handle. The rule was never
# "the model knows nothing about products"; it was "the model cannot name one
# to the backend", and that is what these guards enforce.










# ── view invariants ─────────────────────────────────────────────────────────








# ── the response output is unchanged ────────────────────────────────────────


def test_the_existing_response_output_contract_is_reused() -> None:
    assert set(CustomerResponse.model_fields) == {
        "message",
        "referenced_grounding_refs",
        "follow_up_question",
        "choices",
    }




def test_a_follow_up_is_a_single_scalar() -> None:
    """One question maximum, structurally."""
    annotation = str(CustomerResponse.model_fields["follow_up_question"].annotation)

    assert "tuple" not in annotation and "list" not in annotation




# ── the retired carry-forward ───────────────────────────────────────────────


def test_original_request_summary_exists_nowhere() -> None:
    """Retired by decision: it had no definition in the repository, and a
    narrower application-only numeric allowance replaces it."""
    for module in APP.rglob("*.py"):
        assert "original_request_summary" not in module.read_text(), module.name


# ── a successful turn that also failed at a side effect ─────────────────────
#
# "Select the beige one and show me coffee tables" can find the tables and fail
# to resolve the selection. Answering only "here are some coffee tables" would
# tell the customer the whole request worked.
#
# `TurnGrounding` carries one `failure` field, and which layer it came from is
# recoverable: a *primary* failure leaves no primary grounding, so a failure
# arriving beside results can only be the optional interaction's.








# ── valid grounding references ──────────────────────────────────────────────


# ── a turn can succeed and still owe a question ─────────────────────────────
#
# "Select the beige one and show me coffee tables" can find the tables and
# still not know which sofa was meant. Returning only the clarification throws
# away work the customer asked for; returning only the results pretends the
# whole request succeeded. Both facts survive, and they cost one model call.


# ── ANSWER is a real outcome, not an absence ────────────────────────────────


# ── the optional follow-up yields to a required question ────────────────────


# ── the model still sees nothing new ────────────────────────────────────────








# ── the words must match the cards ──────────────────────────────────────────
#
# Three defects from the conversational UAT, all of the same shape: the reply
# described something the customer had no way to see. A search that widened
# while the five products on screen stayed put; an internal taxonomy key read
# back as if it were English; and an errand the agent chose to run reported as
# a search that had failed.






