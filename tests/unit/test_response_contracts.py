"""What the response model may see, and which branches ever reach it.

The design decision underneath every test here: the response model receives no
product fact at all. That is why the numeric guard can be small, why prose
cannot repeat a name or a price, and why catalog-controlled text never enters a
prompt. What crosses the boundary is counts and enum members.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CommercialReason,
    CustomerAgentDecision,
    FollowUpPolicy,
    ProductInteractionIntent,
    ProductInteractionOp,
)
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse, CustomerTurnResult, TurnGrounding
from app.schemas.comparison import (
    ComparisonCell,
    ComparisonField,
    ComparisonRow,
    ComparisonStatus,
    ProductComparisonResult,
)
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.grounding import (
    DroppedConstraint,
    GroundedProduct,
    RelaxationSummaryItem,
    SearchExecutionGrounding,
    SearchOutcome,
    TurnFailure,
    TurnFailureCode,
)
from app.schemas.product import CommerceClassification
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.query import ConstraintStrength
from app.schemas.relaxation import RelaxableField, StopReason
from app.schemas.resolution import (
    DeterministicClarification,
    ReferenceFailureReason,
    SearchRequirementClarificationReason,
)
from app.schemas.response import (
    DeterministicResponse,
    DeterministicResponseKind,
    ResponseGroundingView,
    ResponseInput,
    ResponseOutcomeKind,
    SideEffectNotice,
)
from app.services.response_view import route_response, valid_grounding_refs
from app.taxonomy.dimensions import DimensionRole, UnsupportedDimensionReason
from pydantic import BaseModel, ValidationError

APP = Path(__file__).parents[2] / "app"


def _result(
    grounding: TurnGrounding,
    *,
    action: AgentAction = AgentAction.ANSWER,
    interaction: ProductInteractionIntent | None = None,
) -> CustomerTurnResult:
    """A turn result around one grounding.

    The action matters: an `ANSWER` turn produces no positive grounding and is
    still a complete piece of work, so routing cannot infer success from
    grounding alone. Only the action and the interaction op are ever read, and
    neither reaches the model.
    """
    return CustomerTurnResult(
        state=AgentStateV1(),
        decision=_decision(action, interaction),
        grounding=grounding,
    )


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


def _route(
    grounding: TurnGrounding,
    *,
    action: AgentAction = AgentAction.ANSWER,
    interaction: ProductInteractionIntent | None = None,
) -> Any:
    return route_response(_result(grounding, action=action, interaction=interaction)).primary


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


def _comparison() -> ProductComparisonResult:
    return ProductComparisonResult(
        products=(_product(1), _product(2)),
        rows=(
            ComparisonRow(
                field=ComparisonField.PRICE,
                cells=(
                    ComparisonCell(known=True, value="4299"),
                    ComparisonCell(known=True, value="3199"),
                ),
                status=ComparisonStatus.DIFFERENT,
            ),
            ComparisonRow(
                field=ComparisonField.MAIN_COLOR,
                cells=(
                    ComparisonCell(known=True, value="Beige"),
                    ComparisonCell(known=True, value="Beige"),
                ),
                status=ComparisonStatus.SAME,
            ),
        ),
    )


# ── which branches reach a model ────────────────────────────────────────────


def test_a_model_written_clarification_is_passed_through() -> None:
    """The decision model already wrote it; re-wording could only change what
    was asked."""
    routing = _route(
        TurnGrounding(
            clarification=BlockingClarification(
                reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE,
                question="Which kind of table?",
            )
        )
    )

    assert isinstance(routing, DeterministicResponse)
    assert routing.kind is DeterministicResponseKind.MODEL_CLARIFICATION


@pytest.mark.parametrize("code", list(TurnFailureCode))
def test_a_handled_failure_is_worded_deterministically(code: TurnFailureCode) -> None:
    routing = _route(TurnGrounding(failure=TurnFailure(code=code)), action=AgentAction.SEARCH)

    assert isinstance(routing, DeterministicResponse)
    assert routing.kind is DeterministicResponseKind.HANDLED_FAILURE
    assert routing.failure_code is code


def test_a_design_handoff_is_worded_deterministically() -> None:
    """Nothing ran, so there is nothing for a model to describe."""
    routing = _route(TurnGrounding(design_handoff_requested=True))

    assert isinstance(routing, DeterministicResponse)
    assert routing.kind is DeterministicResponseKind.DESIGN_HANDOFF


@pytest.mark.parametrize(
    ("grounding", "expected"),
    [
        (TurnGrounding(), ResponseOutcomeKind.ANSWER),
        (TurnGrounding(search=_search(count=2)), ResponseOutcomeKind.SEARCH_RESULTS),
        (TurnGrounding(search=_search(count=0)), ResponseOutcomeKind.ZERO_RESULTS),
        (
            TurnGrounding(product_detail=_product(1)),
            ResponseOutcomeKind.PRODUCT_DETAIL,
        ),
        (TurnGrounding(comparison=_comparison()), ResponseOutcomeKind.COMPARISON),
        (
            TurnGrounding(
                deterministic_clarification=DeterministicClarification(
                    reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
                    reference_reason=ReferenceFailureReason.SEVERAL_ATTRIBUTE_MATCHES,
                )
            ),
            ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION,
        ),
    ],
    ids=lambda v: v.value if isinstance(v, ResponseOutcomeKind) else "",
)
def test_the_model_call_branches_project_a_view(
    grounding: TurnGrounding, expected: ResponseOutcomeKind
) -> None:
    # A search that produced a question instead of products: nothing positive
    # to report, so the question is the whole job.
    action = (
        AgentAction.SEARCH
        if expected is ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION
        else AgentAction.ANSWER
    )
    routing = _route(grounding, action=action)

    assert isinstance(routing, ResponseGroundingView)
    assert routing.kind is expected


def test_a_failed_side_effect_does_not_hide_a_successful_search() -> None:
    """An optional interaction can fail beside a search that worked. The search
    is what the customer asked for, so it is what gets reported."""
    routing = _route(
        TurnGrounding(
            search=_search(count=2),
            failure=TurnFailure(code=TurnFailureCode.REFERENCE_UNRESOLVED),
        )
    )

    assert isinstance(routing, ResponseGroundingView)
    assert routing.kind is ResponseOutcomeKind.SEARCH_RESULTS


def test_the_model_facing_enum_excludes_the_bypassed_branches() -> None:
    """A model-facing enum cannot describe a job the model never does."""
    kinds = {k.value for k in ResponseOutcomeKind}

    assert kinds == {
        "answer",
        "search_results",
        "zero_results",
        "product_detail",
        "comparison",
        "room_bundle",
        "design_advice",
        # M17: the customer's own choices, shown again. Not a search - nothing
        # was looked for, so the reply must not describe finding anything.
        "selection",
        "deterministic_clarification",
    }
    for absent in ("clarify", "failure", "design_handoff", "handled_failure"):
        assert absent not in kinds


# ── what the projection carries ─────────────────────────────────────────────


def test_a_search_projects_the_cards_the_customer_can_see() -> None:
    """Counts *and* merchandise.

    The count says an ordinal is sayable; the cards say what the ordinal points
    at. Without the second, a reply can only announce that results exist
    (CLAUDE.md 2).
    """
    routing = _route(TurnGrounding(search=_search(count=2)))

    assert isinstance(routing, ResponseGroundingView)
    assert routing.presented_count == 2
    assert [card.presented_ordinal for card in routing.screen.products] == [1, 2]
    assert routing.screen.products[0].name == "Aurora Three Seater"
    assert routing.screen.products[0].price_amount == Decimal("4299")


def test_a_projected_card_carries_no_way_to_address_the_catalog() -> None:
    """The line the merchandise does not cross."""
    routing = _route(TurnGrounding(search=_search(count=2)))

    assert isinstance(routing, ResponseGroundingView)
    rendered = routing.model_dump_json()
    for forbidden in ("product_id", "store_id", "https://", "grounding_ref", "uuid"):
        assert forbidden not in rendered, forbidden


def test_relaxation_projects_the_axis_never_the_figure() -> None:
    """The customer should hear that 5,000 became 5,500 - from the application,
    rendered from the authoritative summary, not from model prose."""
    routing = _route(TurnGrounding(search=_search(count=2, relaxed=True)))

    assert isinstance(routing, ResponseGroundingView)
    assert routing.was_relaxed is True
    assert routing.relaxed_fields == (RelaxableField.PRICE_MAX,)
    rendered = routing.model_dump_json()
    assert "5000" not in rendered
    assert "5500" not in rendered


def test_dropped_constraints_project_their_role() -> None:
    routing = _route(TurnGrounding(search=_search(count=2, dropped=True)))

    assert isinstance(routing, ResponseGroundingView)
    assert routing.dropped_roles == (DimensionRole.DEPTH,)


def test_a_comparison_projects_which_fields_differ_not_the_cells() -> None:
    routing = _route(TurnGrounding(comparison=_comparison()))

    assert isinstance(routing, ResponseGroundingView)
    assert routing.compared_count == 2
    assert routing.comparison_differs_on == (ComparisonField.PRICE,)
    rendered = routing.model_dump_json()
    assert "4299" not in rendered and "3199" not in rendered
    assert "Beige" not in rendered


def test_a_clarification_projects_every_reason_it_has() -> None:
    routing = _route(
        TurnGrounding(
            deterministic_clarification=DeterministicClarification(
                reason=SearchRequirementClarificationReason.UNSUPPORTED_REQUIREMENT
            )
        )
    )

    assert isinstance(routing, ResponseGroundingView)
    assert routing.clarification_reason is (
        SearchRequirementClarificationReason.UNSUPPORTED_REQUIREMENT
    )


def test_a_product_detail_projects_one_product_and_no_facts() -> None:
    routing = _route(TurnGrounding(product_detail=_product(1)))

    assert isinstance(routing, ResponseGroundingView)
    assert routing.presented_count == 1
    assert "Aurora" not in routing.model_dump_json()


def test_the_projection_is_pure() -> None:
    source = (APP / "services/response_view.py").read_text()
    tree = ast.parse(source)

    assert not any(isinstance(node, ast.Await) for node in ast.walk(tree))
    for forbidden in ("Repository", "client", "RetailerContext", "datetime"):
        assert forbidden not in source, forbidden


def test_turn_grounding_is_never_serialised_wholesale() -> None:
    """A projection, not a pass-through: `TurnGrounding` holds verified product
    facts the model must not restate."""
    assert "TurnGrounding" not in str(ResponseInput.model_fields)
    assert "grounding" in ResponseInput.model_fields
    annotation = str(ResponseInput.model_fields["grounding"].annotation)
    assert "ResponseGroundingView" in annotation


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


@pytest.mark.parametrize(
    "forbidden",
    [
        "product_id",
        "uuid",
        "store_id",
        "retailer",
        "revision",
        "image_url",
        "product_url",
        "pinecone_id",
        "line_id",
        "need_id",
        "schema_version",
        "similarity",
        "relaxation_depth",
        "grounding_ref",
    ],
)
def test_the_response_input_reaches_no_product_or_retailer_fact(
    forbidden: str,
) -> None:
    for name in _reachable_names(ResponseInput):
        assert forbidden not in name, name


@pytest.mark.parametrize(
    "forbidden",
    [
        "GroundedProduct",
        "ProductComparisonResult",
        "AgentStateV1",
        "CustomerTurnResult",
        "RetailerContext",
        "TurnFailure",
        "SearchExecutionGrounding",
    ],
)
def test_the_response_input_reaches_no_forbidden_type(forbidden: str) -> None:
    """Checked on the schema's definitions, not its prose.

    The docstrings deliberately name these types to say they are excluded, so
    a substring search over the rendered schema would match my own sentence
    rather than a reachable field.
    """
    definitions = set(ResponseInput.model_json_schema().get("$defs", {}))

    assert forbidden not in definitions


def test_an_enum_label_is_not_a_product_value() -> None:
    """`ComparisonField.PRICE` names an axis. It is metadata, not a price."""
    view = ResponseGroundingView(
        kind=ResponseOutcomeKind.COMPARISON,
        compared_count=2,
        comparison_differs_on=(ComparisonField.PRICE,),
    )

    rendered = view.model_dump(mode="json")
    assert rendered["comparison_differs_on"] == ["price"]
    assert not any(character.isdigit() for character in str(rendered["comparison_differs_on"]))


def test_the_input_is_frozen_and_closed() -> None:
    request = ResponseInput(
        message="show me sofas",
        grounding=ResponseGroundingView(kind=ResponseOutcomeKind.ANSWER),
    )

    assert ResponseInput.model_config["frozen"] is True
    with pytest.raises(ValidationError):
        ResponseInput.model_validate(
            {
                "message": "x",
                "grounding": {"kind": "answer"},
                "state": {},
            }
        )
    assert request.follow_up_allowed is False


# ── view invariants ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        (
            {"kind": ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION},
            "a clarification names its reason",
        ),
        (
            {
                "kind": ResponseOutcomeKind.ANSWER,
                "reference_reason": ReferenceFailureReason.TIED_EXTREMUM,
            },
            "a detail reason needs the reason it details",
        ),
        (
            {"kind": ResponseOutcomeKind.ZERO_RESULTS, "presented_count": 2},
            "zero results present nothing",
        ),
        (
            {"kind": ResponseOutcomeKind.SEARCH_RESULTS, "presented_count": 0},
            "results present something",
        ),
        (
            {"kind": ResponseOutcomeKind.COMPARISON, "compared_count": 1},
            "a comparison covers at least two",
        ),
        (
            {"kind": ResponseOutcomeKind.ANSWER, "compared_count": 2},
            "only a comparison carries comparison detail",
        ),
        (
            {"kind": ResponseOutcomeKind.ANSWER, "was_relaxed": True},
            "was_relaxed must match the fields",
        ),
    ],
)
def test_the_view_refuses_an_incoherent_shape(kwargs: Any, why: str) -> None:
    with pytest.raises(ValidationError):
        ResponseGroundingView(**kwargs)


def test_only_a_failure_carries_a_failure_code() -> None:
    with pytest.raises(ValidationError):
        DeterministicResponse(
            kind=DeterministicResponseKind.DESIGN_HANDOFF,
            failure_code=TurnFailureCode.SEARCH_UNAVAILABLE,
        )
    with pytest.raises(ValidationError):
        DeterministicResponse(kind=DeterministicResponseKind.HANDLED_FAILURE)


def test_the_failure_code_is_never_model_visible() -> None:
    """It selects fixed wording. It lives on the deterministic branch, which no
    model ever sees."""
    assert "failure_code" not in ResponseGroundingView.model_fields
    assert "failure_code" in DeterministicResponse.model_fields


# ── the response output is unchanged ────────────────────────────────────────


def test_the_existing_response_output_contract_is_reused() -> None:
    assert set(CustomerResponse.model_fields) == {
        "message",
        "referenced_grounding_refs",
        "follow_up_question",
    }


def test_grounding_refs_carry_no_rendering_authority() -> None:
    """Cards are rendered by the application regardless of what prose cites.

    Pinned at contract level: the field is a citation list, and nothing reads
    it to decide what appears.
    """
    response = CustomerResponse(
        message="The second one suits a smaller room.",
        referenced_grounding_refs=(2,),
    )
    grounding = TurnGrounding(search=_search(count=3))

    assert grounding.search is not None
    assert len(grounding.search.products) == 3, "all three still render"
    assert response.referenced_grounding_refs == (2,)


def test_a_follow_up_is_a_single_scalar() -> None:
    """One question maximum, structurally."""
    annotation = str(CustomerResponse.model_fields["follow_up_question"].annotation)

    assert "tuple" not in annotation and "list" not in annotation


@pytest.mark.parametrize(
    ("policy", "allowed"),
    [(FollowUpPolicy.NONE, False), (FollowUpPolicy.OPTIONAL, True)],
)
def test_the_policy_maps_to_a_boolean(policy: FollowUpPolicy, allowed: bool) -> None:
    request = ResponseInput(
        message="show me sofas",
        grounding=ResponseGroundingView(kind=ResponseOutcomeKind.ANSWER),
        follow_up_allowed=policy is FollowUpPolicy.OPTIONAL,
    )

    assert request.follow_up_allowed is allowed


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

SIDE_EFFECT_NOTICES = {
    ProductInteractionOp.SELECT: SideEffectNotice.SELECTION_NOT_UPDATED,
    ProductInteractionOp.DESELECT: SideEffectNotice.SELECTION_NOT_REMOVED,
    ProductInteractionOp.FOCUS: SideEffectNotice.FOCUS_NOT_CHANGED,
}


def _interaction(op: ProductInteractionOp) -> ProductInteractionIntent:
    return ProductInteractionIntent(op=op, reference=PresentedOrdinal(position=1))


@pytest.mark.parametrize(
    ("op", "notice"), SIDE_EFFECT_NOTICES.items(), ids=lambda v: getattr(v, "value", v)
)
def test_each_interaction_failure_has_its_own_notice(
    op: ProductInteractionOp, notice: SideEffectNotice
) -> None:
    route = route_response(
        _result(
            TurnGrounding(
                search=_search(count=3),
                failure=TurnFailure(code=TurnFailureCode.REFERENCE_UNRESOLVED),
            ),
            interaction=_interaction(op),
        )
    )

    assert route.side_notice is notice


def test_the_successful_search_remains_the_primary_outcome() -> None:
    """The notice accompanies the results; it never replaces them."""
    route = route_response(
        _result(
            TurnGrounding(
                search=_search(count=3),
                failure=TurnFailure(code=TurnFailureCode.REFERENCE_UNRESOLVED),
            ),
            interaction=_interaction(ProductInteractionOp.SELECT),
        )
    )

    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert route.primary.presented_count == 3
    assert route.side_notice is SideEffectNotice.SELECTION_NOT_UPDATED


def test_all_the_cards_still_render_beside_a_side_notice() -> None:
    grounding = TurnGrounding(
        search=_search(count=3),
        failure=TurnFailure(code=TurnFailureCode.REFERENCE_UNRESOLVED),
    )

    route = route_response(
        _result(grounding, interaction=_interaction(ProductInteractionOp.SELECT))
    )

    assert grounding.search is not None
    assert len(grounding.search.products) == 3
    assert valid_grounding_refs(grounding) == {1, 2, 3}
    assert route.side_notice is not None


@pytest.mark.parametrize(
    "grounding",
    [
        TurnGrounding(
            product_detail=_product(1),
            failure=TurnFailure(code=TurnFailureCode.REFERENCE_UNRESOLVED),
        ),
        TurnGrounding(
            comparison=_comparison(),
            failure=TurnFailure(code=TurnFailureCode.REFERENCE_UNRESOLVED),
        ),
    ],
    ids=["product_detail", "comparison"],
)
def test_other_successful_primaries_also_keep_their_notice(
    grounding: TurnGrounding,
) -> None:
    route = route_response(
        _result(grounding, interaction=_interaction(ProductInteractionOp.SELECT))
    )

    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is not ResponseOutcomeKind.ANSWER
    assert route.side_notice is SideEffectNotice.SELECTION_NOT_UPDATED


def test_a_primary_failure_still_wins_and_adds_no_second_notice() -> None:
    """Nothing else succeeded, so the failure is already the whole answer."""
    route = route_response(
        _result(
            TurnGrounding(failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)),
            action=AgentAction.SEARCH,
            interaction=_interaction(ProductInteractionOp.SELECT),
        )
    )

    assert isinstance(route.primary, DeterministicResponse)
    assert route.primary.kind is DeterministicResponseKind.HANDLED_FAILURE
    assert route.primary.failure_code is TurnFailureCode.SEARCH_UNAVAILABLE
    assert route.side_notice is None


def test_a_successful_turn_with_no_failure_has_no_notice() -> None:
    route = route_response(
        _result(
            TurnGrounding(search=_search(count=2)),
            interaction=_interaction(ProductInteractionOp.SELECT),
        )
    )

    assert route.side_notice is None


def test_a_failure_with_no_interaction_has_no_notice() -> None:
    """Nothing was attempted, so there is no side effect to report."""
    route = route_response(
        _result(
            TurnGrounding(
                search=_search(count=2),
                failure=TurnFailure(code=TurnFailureCode.REFERENCE_UNRESOLVED),
            )
        )
    )

    assert route.side_notice is None


def test_a_side_notice_is_never_shown_to_the_model() -> None:
    """It lives on the application route, not on the model-facing view."""
    assert "side_notice" not in ResponseGroundingView.model_fields
    assert "interaction" not in str(ResponseGroundingView.model_fields)
    assert "side_notice" not in str(ResponseInput.model_json_schema())


def test_a_notice_needs_no_extra_model_call() -> None:
    """It is an enum member selecting fixed wording, not generated text."""
    for notice in SideEffectNotice:
        assert notice.value.replace("_", " ").isascii()
        assert not any(character.isdigit() for character in notice.value)


def test_the_notice_map_covers_every_interaction_op() -> None:
    """A new operation must choose its notice rather than silently getting
    none."""
    from app.services.response_view import _NOTICE_FOR_OP

    assert set(_NOTICE_FOR_OP) == set(ProductInteractionOp)


def test_a_side_notice_does_not_reopen_the_follow_up() -> None:
    """`TurnGrounding` already forces NONE whenever a failure is present."""
    grounding = TurnGrounding(
        search=_search(count=2),
        failure=TurnFailure(code=TurnFailureCode.REFERENCE_UNRESOLVED),
    )

    assert grounding.follow_up_policy is FollowUpPolicy.NONE


# ── valid grounding references ──────────────────────────────────────────────


def test_search_refs_come_from_the_grounded_products() -> None:
    assert valid_grounding_refs(TurnGrounding(search=_search(count=3))) == {1, 2, 3}


def test_a_product_detail_ref_is_its_own_handle() -> None:
    assert valid_grounding_refs(TurnGrounding(product_detail=_product(1))) == {1}


def test_comparison_refs_come_from_the_compared_products() -> None:
    assert valid_grounding_refs(TurnGrounding(comparison=_comparison())) == {1, 2}


@pytest.mark.parametrize(
    "grounding",
    [
        TurnGrounding(),
        TurnGrounding(search=_search(count=0)),
        TurnGrounding(design_handoff_requested=True),
        TurnGrounding(
            deterministic_clarification=DeterministicClarification(
                reason=BlockingClarificationReason.COMPARISON_TARGETS
            )
        ),
    ],
    ids=["answer", "zero results", "design handoff", "clarification"],
)
def test_a_branch_with_no_grounded_products_offers_no_refs(
    grounding: TurnGrounding,
) -> None:
    assert valid_grounding_refs(grounding) == frozenset()


def test_refs_are_never_derived_from_product_identity() -> None:
    """A ref means something because a rendered item carries it, not because a
    product exists.

    The module counts selections now, so `selected_product_ids` appears - a
    length, never an element. What must stay absent is any path from an id to
    a ref, so the check is on the refs themselves.
    """
    import ast

    source = (APP / "services/response_view.py").read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "grounding_ref":
            # Read off a grounded item, never computed.
            assert isinstance(node.value, ast.Name | ast.Attribute)

    assert "grounding_ref" in source
    for forbidden in ("product_id=", ".product_id", "product_ids[", "product_ids)["):
        assert forbidden not in source, forbidden


# ── a turn can succeed and still owe a question ─────────────────────────────
#
# "Select the beige one and show me coffee tables" can find the tables and
# still not know which sofa was meant. Returning only the clarification throws
# away work the customer asked for; returning only the results pretends the
# whole request succeeded. Both facts survive, and they cost one model call.

AMBIGUOUS_SELECTION = DeterministicClarification(
    reason=BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE,
    reference_reason=ReferenceFailureReason.SEVERAL_ATTRIBUTE_MATCHES,
)
MISSING_CURRENCY = DeterministicClarification(
    reason=BlockingClarificationReason.MISSING_PRICE_CURRENCY
)


def test_a_successful_search_survives_a_clarification_beside_it() -> None:
    route = route_response(
        _result(
            TurnGrounding(search=_search(count=3), deterministic_clarification=AMBIGUOUS_SELECTION),
            action=AgentAction.SEARCH,
        )
    )

    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert route.primary.presented_count == 3
    assert route.required_clarification is AMBIGUOUS_SELECTION


def test_the_primary_view_carries_the_question_it_owes() -> None:
    """One model call does both jobs, so the reasons travel with the framing."""
    route = route_response(
        _result(
            TurnGrounding(search=_search(count=3), deterministic_clarification=AMBIGUOUS_SELECTION),
            action=AgentAction.SEARCH,
        )
    )

    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.clarification_reason is (
        BlockingClarificationReason.AMBIGUOUS_PRODUCT_REFERENCE
    )
    assert route.primary.reference_reason is (ReferenceFailureReason.SEVERAL_ATTRIBUTE_MATCHES)


def test_every_card_still_renders_beside_a_required_question() -> None:
    grounding = TurnGrounding(
        search=_search(count=3), deterministic_clarification=AMBIGUOUS_SELECTION
    )

    route = route_response(_result(grounding, action=AgentAction.SEARCH))

    assert grounding.search is not None
    assert len(grounding.search.products) == 3
    assert valid_grounding_refs(grounding) == {1, 2, 3}
    assert route.required_clarification is not None


@pytest.mark.parametrize(
    ("grounding", "action", "expected"),
    [
        (
            TurnGrounding(search=_search(count=0), deterministic_clarification=AMBIGUOUS_SELECTION),
            AgentAction.SEARCH,
            ResponseOutcomeKind.ZERO_RESULTS,
        ),
        (
            TurnGrounding(
                product_detail=_product(1),
                deterministic_clarification=AMBIGUOUS_SELECTION,
            ),
            AgentAction.PRODUCT_DETAIL,
            ResponseOutcomeKind.PRODUCT_DETAIL,
        ),
        (
            TurnGrounding(
                comparison=_comparison(),
                deterministic_clarification=AMBIGUOUS_SELECTION,
            ),
            AgentAction.COMPARE,
            ResponseOutcomeKind.COMPARISON,
        ),
        (
            TurnGrounding(deterministic_clarification=AMBIGUOUS_SELECTION),
            AgentAction.ANSWER,
            ResponseOutcomeKind.ANSWER,
        ),
    ],
    ids=["zero results", "product detail", "comparison", "answer"],
)
def test_every_primary_outcome_survives_a_clarification(
    grounding: TurnGrounding, action: AgentAction, expected: ResponseOutcomeKind
) -> None:
    route = route_response(_result(grounding, action=action))

    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is expected
    assert route.required_clarification is AMBIGUOUS_SELECTION


def test_a_comparison_keeps_its_order_beside_a_question() -> None:
    grounding = TurnGrounding(
        comparison=_comparison(), deterministic_clarification=AMBIGUOUS_SELECTION
    )

    route = route_response(_result(grounding, action=AgentAction.COMPARE))

    assert grounding.comparison is not None
    assert [p.grounding_ref for p in grounding.comparison.products] == [1, 2]
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.compared_count == 2
    assert route.required_clarification is not None


def test_a_proposal_clarification_behaves_the_same_way() -> None:
    """Not interaction-specific: a room budget with no currency is the same
    shape of problem, and the search still stands."""
    route = route_response(
        _result(
            TurnGrounding(search=_search(count=2), deterministic_clarification=MISSING_CURRENCY),
            action=AgentAction.SEARCH,
        )
    )

    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert route.required_clarification is MISSING_CURRENCY
    assert route.primary.clarification_reason is (
        BlockingClarificationReason.MISSING_PRICE_CURRENCY
    )


def test_a_pure_clarification_is_not_carried_twice() -> None:
    """When the question *is* the job it is the route, and holding it again
    beside itself would invite two questions out of one."""
    route = route_response(
        _result(
            TurnGrounding(deterministic_clarification=AMBIGUOUS_SELECTION),
            action=AgentAction.SEARCH,
        )
    )

    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION
    assert route.required_clarification is None


def test_a_design_handoff_keeps_both_its_acknowledgement_and_its_question() -> None:
    """The only reason a handoff turn reaches a model before M12 - and only to
    word the question, never to describe a capability that does not exist."""
    route = route_response(
        _result(
            TurnGrounding(
                design_handoff_requested=True,
                deterministic_clarification=AMBIGUOUS_SELECTION,
            ),
            action=AgentAction.DESIGN_HANDOFF,
        )
    )

    assert isinstance(route.primary, DeterministicResponse)
    assert route.primary.kind is DeterministicResponseKind.DESIGN_HANDOFF
    assert route.required_clarification is AMBIGUOUS_SELECTION


def test_a_pure_design_handoff_still_owes_nothing() -> None:
    route = route_response(
        _result(
            TurnGrounding(design_handoff_requested=True),
            action=AgentAction.DESIGN_HANDOFF,
        )
    )

    assert route.required_clarification is None


def test_a_primary_failure_asks_no_question() -> None:
    """The coordinator already suppresses lower-priority clarifications here."""
    route = route_response(
        _result(
            TurnGrounding(failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)),
            action=AgentAction.SEARCH,
        )
    )

    assert isinstance(route.primary, DeterministicResponse)
    assert route.primary.kind is DeterministicResponseKind.HANDLED_FAILURE
    assert route.required_clarification is None
    assert route.follow_up_allowed is False


# ── ANSWER is a real outcome, not an absence ────────────────────────────────


def test_an_answer_with_a_failed_side_effect_is_still_an_answer() -> None:
    """Grounding alone cannot tell a complete answer from a failed search, so
    routing reads the action. An `ANSWER` turn grounds nothing and is still
    work the customer asked for."""
    route = route_response(
        _result(
            TurnGrounding(failure=TurnFailure(code=TurnFailureCode.REFERENCE_UNRESOLVED)),
            action=AgentAction.ANSWER,
            interaction=_interaction(ProductInteractionOp.SELECT),
        )
    )

    # The isinstance below is the assertion: a handled failure would have
    # routed to `DeterministicResponse` instead.
    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.ANSWER
    assert route.side_notice is SideEffectNotice.SELECTION_NOT_UPDATED
    assert route.follow_up_allowed is False


def test_an_answer_with_a_clarification_remains_an_answer() -> None:
    route = route_response(
        _result(
            TurnGrounding(deterministic_clarification=AMBIGUOUS_SELECTION),
            action=AgentAction.ANSWER,
        )
    )

    assert isinstance(route.primary, ResponseGroundingView)
    assert route.primary.kind is ResponseOutcomeKind.ANSWER
    assert route.required_clarification is AMBIGUOUS_SELECTION
    assert route.follow_up_allowed is False


def test_a_failed_search_is_still_a_failure() -> None:
    """The action is read to recognise success, never to manufacture it."""
    route = route_response(
        _result(
            TurnGrounding(failure=TurnFailure(code=TurnFailureCode.SEARCH_UNAVAILABLE)),
            action=AgentAction.SEARCH,
        )
    )

    assert isinstance(route.primary, DeterministicResponse)


# ── the optional follow-up yields to a required question ────────────────────


def test_an_ordinary_turn_may_still_offer_an_optional_question() -> None:
    route = route_response(
        _result(
            TurnGrounding(search=_search(count=2), follow_up_policy=FollowUpPolicy.OPTIONAL),
            action=AgentAction.SEARCH,
        )
    )

    assert route.follow_up_allowed is True
    assert route.required_clarification is None


def test_a_required_question_leaves_no_room_for_an_optional_one() -> None:
    """One question per turn, and the required one outranks an invitation."""
    route = route_response(
        _result(
            TurnGrounding(
                search=_search(count=2),
                deterministic_clarification=AMBIGUOUS_SELECTION,
            ),
            action=AgentAction.SEARCH,
        )
    )

    assert route.required_clarification is not None
    assert route.follow_up_allowed is False


# ── the model still sees nothing new ────────────────────────────────────────


def test_the_composite_route_widened_no_model_authority() -> None:
    """Approved reason enums, counts, and controlled taxonomy - no values.

    The sales pass added three fields so a reply can be about something rather
    than announcing that results exist. Each names a *kind* or a subject:
    the category searched for, whether a requirement is already known, and
    what an optional question should cover. None carries a figure, a product
    or an identity.

    Two more followed the conversational UAT, and neither loosens that. An
    exact-match *count* is a count, of the same kind as `presented_count`,
    and says how many products met the request the customer actually made.
    `search_was_suggested` is a bool about whose idea the search was. Neither
    names a product, a price or a bound.
    """
    assert set(ResponseGroundingView.model_fields) == {
        "kind",
        "presented_count",
        "commerce_category",
        "commerce_subcategory",
        "exact_match_count",
        # Counts of cards in a colour/style the customer asked or wished for,
        # so a reply cannot call black tables red. Counts, never a value.
        "wished_colour_matches",
        "wished_style_matches",
        "search_was_suggested",
        # M17: what the customer has actually settled on, and whether this
        # turn added to it. Counts and a bool - no product, no identity - so a
        # reply can answer "what have I chosen?" from fact instead of from the
        # conversation, which is how it once reported a rug nobody kept.
        "selected_count",
        "selected_kinds",
        "selection_changed",
        "seating_requirement_known",
        "follow_up_goal",
        "was_relaxed",
        "relaxed_fields",
        "dropped_roles",
        "compared_count",
        "comparison_differs_on",
        "bundle",
        "guidance",
        "screen",
        "clarification_reason",
        "reference_reason",
        "relative_price_reason",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "required_clarification",
        "side_notice",
        "action",
        "interaction",
        "commercial_reason",
        "purchase_stage",
        "decision",
        "state",
    ],
)
def test_the_route_only_fields_stay_off_the_model_view(forbidden: str) -> None:
    """Checked on field names, not on the schema's prose.

    The enum docstrings legitimately mention "the decision model" and "safe
    state", so a substring search over the rendered schema would match my own
    sentences rather than a reachable field.
    """
    assert forbidden not in ResponseGroundingView.model_fields
    assert forbidden not in _reachable_names(ResponseInput)


def test_a_reason_cannot_appear_without_a_question_behind_it() -> None:
    """The response layer reports a clarification; it never invents one."""
    with pytest.raises(ValidationError):
        ResponseGroundingView(
            kind=ResponseOutcomeKind.SEARCH_RESULTS,
            presented_count=2,
            reference_reason=ReferenceFailureReason.TIED_EXTREMUM,
        )


# ── the words must match the cards ──────────────────────────────────────────
#
# Three defects from the conversational UAT, all of the same shape: the reply
# described something the customer had no way to see. A search that widened
# while the five products on screen stayed put; an internal taxonomy key read
# back as if it were English; and an errand the agent chose to run reported as
# a search that had failed.


def _searched_result(
    subcategory: str = "sofa",
    *,
    count: int = 2,
    relaxed: bool = False,
    suggested: bool = False,
    reason: CommercialReason | None = None,
) -> CustomerTurnResult:
    """A turn whose search ran, built the way the application builds one."""
    from app.schemas.agent_state import ActiveSearchState
    from app.schemas.discovery import ProductSearchRequest

    decision = _decision(AgentAction.SEARCH)
    if suggested or reason is not None:
        decision = decision.model_copy(
            update={"commercial_reason": reason or CommercialReason.PURCHASE_PROGRESSION}
        )
    return CustomerTurnResult(
        state=AgentStateV1(
            active_search=ActiveSearchState(
                request=ProductSearchRequest(
                    commerce_category="seating", commerce_subcategory=subcategory
                ),
                revision=1,
            )
        ),
        decision=decision,
        grounding=TurnGrounding(
            search=_search(count=count, relaxed=relaxed),
            design_handoff_requested=suggested or reason is not None,
        ),
    )


def _searched(*args: Any, **kwargs: Any) -> ResponseGroundingView:
    view = route_response(_searched_result(*args, **kwargs)).primary
    assert isinstance(view, ResponseGroundingView)
    return view


def test_a_category_reaches_the_model_as_words_not_as_a_registry_key() -> None:
    """The UAT defect: a customer who had asked about sofas was told there were
    no matching "lounge-chair options".

    The key is an internal identifier, and a model shown one writes it back
    verbatim - so it never sees one.
    """
    view = _searched("lounge-chair")

    assert view.commerce_subcategory == "lounge chair"
    assert "-" not in view.model_dump_json()


def test_the_words_rename_nothing() -> None:
    """Mechanical, so the registry stays the only vocabulary (CLAUDE.md 14.2).

    A display *name* would be an alias by another route: a second place a
    product type is spelled, free to drift from the one that is authoritative.
    """
    from app.taxonomy.registry import load_taxonomy

    taxonomy = load_taxonomy()
    for category in taxonomy.categories:
        for subcategory in taxonomy.subcategories(category):
            view = _searched(subcategory)

            assert view.commerce_subcategory is not None
            assert view.commerce_subcategory.replace(" ", "-") == subcategory


def test_a_view_refuses_a_key_that_reached_it_unconverted() -> None:
    """The projection is one call site today. The contract holds whatever
    number of call sites it grows."""
    with pytest.raises(ValidationError, match="words, not a key"):
        ResponseGroundingView(
            kind=ResponseOutcomeKind.SEARCH_RESULTS,
            presented_count=1,
            exact_match_count=1,
            commerce_subcategory="lounge-chair",
        )


def test_a_widened_search_carries_what_the_customer_can_check() -> None:
    """How many products met the request as they made it.

    Without this the reply could only say a bound had moved - true of the
    pipeline, unverifiable on screen, and read by the customer as a change to
    products that had not changed (CLAUDE.md 13.4).
    """
    widened = _searched(count=5, relaxed=True)

    assert widened.was_relaxed is True
    assert widened.exact_match_count == 0
    assert widened.presented_count == 5


def test_an_unwidened_search_says_everything_matched() -> None:
    exact = _searched(count=2)

    assert exact.was_relaxed is False
    assert exact.exact_match_count == exact.presented_count


def test_a_suggestion_that_found_something_is_still_a_search() -> None:
    """Only the empty case is withheld. Products we proposed are on screen and
    have to be introduced, so the turn reports them like any other."""
    found = _searched(count=3, suggested=True)

    assert found.kind is ResponseOutcomeKind.SEARCH_RESULTS
    assert found.search_was_suggested is True
    assert found.presented_count == 3


def test_a_suggested_search_is_marked_as_ours_not_theirs() -> None:
    """A complementary suggestion runs the same pipeline as any other search.

    By the time a reply is worded the two are indistinguishable without this,
    which is how "I couldn't find any matching options in this search" came to
    be said about a search the customer never asked for.
    """
    assert _searched(count=3, suggested=True).search_was_suggested is True
    assert _searched(count=3).search_was_suggested is False
    assert (
        _searched(count=3, reason=CommercialReason.CUSTOMER_REQUEST
        ).search_was_suggested
        is False
    ), "their own design question is not a suggestion of ours"


def test_a_suggestion_that_found_nothing_is_not_reported_at_all() -> None:
    """The case the customer actually hit.

    They said they liked the second sofa. The reply told them nothing matched
    for a product type they had never mentioned, beside an empty screen. They
    asked for nothing, so nothing failed - and the turn is what they did.
    """
    empty = _searched("lounge-chair", count=0, suggested=True)

    assert empty.kind is ResponseOutcomeKind.ANSWER
    assert empty.presented_count == 0
    assert empty.commerce_subcategory is None, "no kind of thing is on screen"
    assert empty.search_was_suggested is False, "an answer ran no search"


def test_a_lapsed_suggestion_is_not_reported_as_a_failed_handoff_either() -> None:
    """The branch it would otherwise fall into says the planning failed. That
    is the same false report in different words."""
    route = route_response(
        _searched_result("lounge-chair", count=0, suggested=True)
    ).primary

    assert not isinstance(route, DeterministicResponse)


def test_their_own_design_question_is_still_answered_when_nothing_matches() -> None:
    """The distinction the whole rule turns on.

    "What would go with this?" is their question, so an empty answer is owed
    to them. Silence there would be a different defect, not the same fix.
    """
    asked = _searched(
        "lounge-chair", count=0, reason=CommercialReason.CUSTOMER_REQUEST
    )

    assert asked.kind is ResponseOutcomeKind.ZERO_RESULTS
    assert asked.commerce_subcategory == "lounge chair"


@pytest.mark.parametrize(
    "kind",
    [
        ResponseOutcomeKind.ANSWER,
        ResponseOutcomeKind.PRODUCT_DETAIL,
        ResponseOutcomeKind.COMPARISON,
    ],
)
def test_only_a_search_carries_search_provenance(kind: ResponseOutcomeKind) -> None:
    """A detail, a comparison and a plain answer ran no search, so neither
    field means anything on them."""
    fields: dict[str, Any] = {"kind": kind, "presented_count": 1}
    if kind is ResponseOutcomeKind.COMPARISON:
        fields["compared_count"] = 2

    with pytest.raises(ValidationError, match="search provenance"):
        ResponseGroundingView(**fields, exact_match_count=1)
    with pytest.raises(ValidationError, match="search provenance"):
        ResponseGroundingView(**fields, search_was_suggested=True)


def test_an_unwidened_search_cannot_claim_fewer_exact_matches_than_it_shows() -> None:
    """Nothing widened, so every product on screen came from the exact pool.

    The invariant that stops the new count drifting into decoration: a figure
    the reply quotes has to agree with the cards beside it.
    """
    with pytest.raises(ValidationError, match="only exact matches"):
        ResponseGroundingView(
            kind=ResponseOutcomeKind.SEARCH_RESULTS,
            presented_count=5,
            exact_match_count=1,
        )
