"""The whole-room turn as the customer receives it.

Two halves, and the split is the whole design. The response model is given
counts, enums and two bools, and writes framing. The application renders the
pieces, the quantities, the verified prices and the total. Neither half can do
the other's job: the model has no figure to state, and the renderer has no
prose to write.

Most of these tests defend one of three things — that no catalog value reaches
the model, that a partial room is never described as a complete one, and that
an already-owned piece is never given a price of zero.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_state import AgentStateV1, RoomProjectState
from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
from app.schemas.bundle import (
    BundleLine,
    BundleStatus,
    BundleUnavailable,
    BundleUnavailableReason,
    RoomBundle,
    TotalUnavailableReason,
    UnmetNeed,
    UnmetReason,
)
from app.schemas.bundle_presentation import GroundedBundleItem
from app.schemas.design import DesignPriority
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.discovery import PriceConstraint
from app.schemas.grounding import TurnFailure, TurnFailureCode
from app.schemas.product import CommerceClassification, ProductCandidate
from app.schemas.response import (
    BundleGroundingView,
    DeterministicResponse,
    DeterministicResponseKind,
    ResponseGroundingView,
    ResponseOutcomeKind,
)
from app.services.bundle_presentation import build_bundle_presentation
from app.services.numeric_guard import build_allowance, bundle_counts, check_numeric_policy
from app.services.response_view import route_response
from app.services.response_wording import BUNDLE_UNAVAILABLE_WORDING, fallback_for
from pydantic import ValidationError


def product(product_id: int = 1, *, price: str = "1200.00", unit: str = "SAR") -> ProductCandidate:
    return ProductCandidate(
        product_id=product_id,
        name_english=f"Sofa {product_id}",
        name_arabic="أريكة",
        price_amount=Decimal(price),
        price_unit=unit,
        image_url=f"https://example.test/{product_id}.jpg",
        product_url=f"https://example.test/{product_id}",
        commerce=CommerceClassification(category="seating", subcategory="sofa"),
        dimensions=NormalisedDimensions(status=DimensionStatus.ABSENT),
        main_color="Beige",
        styles=("Modern",),
    )


def line(
    product_id: int = 1,
    *,
    quantity: int = 1,
    acquisition: BundleAcquisition = BundleAcquisition.TO_BUY,
    locked: bool | None = None,
    depth: int | None = 0,
    price: str = "1200.00",
    need_index: int | None = None,
) -> BundleLine:
    # Only a kept line can be already-owned: a freshly selected product is by
    # definition one the customer is buying.
    kept = locked if locked is not None else acquisition is not BundleAcquisition.TO_BUY
    return BundleLine(
        need_index=need_index,
        product=product(product_id, price=price),
        quantity=quantity,
        locked=kept,
        acquisition=acquisition,
        relaxation_depth=None if kept else depth,
    )


def unmet(
    priority: DesignPriority = DesignPriority.REQUIRED,
    reason: UnmetReason = UnmetReason.NO_CANDIDATES,
    index: int = 9,
) -> UnmetNeed:
    return UnmetNeed(need_index=index, priority=priority, shortfall=1, reason=reason)


def bundle(
    *lines: BundleLine,
    status: BundleStatus = BundleStatus.COMPLETE,
    unmet_needs: tuple[UnmetNeed, ...] = (),
    total: str | None = "1200.00",
) -> RoomBundle:
    return RoomBundle(
        lines=lines,
        status=status,
        unmet=unmet_needs,
        new_spend_total=Decimal(total) if total else None,
        currency="SAR" if total else None,
        total_unavailable=None if total else TotalUnavailableReason.NO_PRICED_LINES,
    )


def result(
    outcome: Any = None, *, budget: str | None = None, **grounding: Any
) -> CustomerTurnResult:
    room = RoomProjectState(
        budget=PriceConstraint.at_most(Decimal(budget), "SAR") if budget else None
    )
    return CustomerTurnResult(
        state=AgentStateV1(room_project=room),
        decision=CustomerAgentDecision(action=AgentAction.DESIGN_HANDOFF),
        grounding=TurnGrounding(design_handoff_requested=True, **grounding),
        bundle_outcome=outcome,
    )


def view(outcome: Any, **kwargs: Any) -> ResponseGroundingView:
    route = route_response(result(outcome, **kwargs))
    assert isinstance(route.primary, ResponseGroundingView)
    return route.primary


# ── routing ═════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "status", [BundleStatus.COMPLETE, BundleStatus.PARTIAL, BundleStatus.INFEASIBLE]
)
def test_every_real_bundle_is_framed_by_the_model(status: BundleStatus) -> None:
    unmet_needs = () if status is BundleStatus.COMPLETE else (unmet(),)
    lines = (line(locked=True),) if status is BundleStatus.INFEASIBLE else (line(),)
    outcome = bundle(*lines, status=status, unmet_needs=unmet_needs)

    primary = view(outcome, budget="15000")

    assert primary.kind is ResponseOutcomeKind.ROOM_BUNDLE
    assert primary.bundle is not None
    assert primary.bundle.status is status


def test_a_refusal_to_compute_is_answered_without_a_model() -> None:
    route = route_response(
        result(BundleUnavailable(reason=BundleUnavailableReason.LOCKED_PRICE_UNUSABLE))
    )

    assert isinstance(route.primary, DeterministicResponse)
    assert route.primary.kind is DeterministicResponseKind.BUNDLE_UNAVAILABLE
    assert route.primary.bundle_reason is BundleUnavailableReason.LOCKED_PRICE_UNUSABLE


def test_a_finished_room_outranks_the_request_marker() -> None:
    """`design_handoff_requested` says what was asked for, never what came of
    it. Letting it answer would tell the customer nothing ran."""
    primary = view(bundle(line()))

    assert primary.kind is ResponseOutcomeKind.ROOM_BUNDLE


def test_a_handoff_that_produced_nothing_still_falls_back_to_the_marker() -> None:
    route = route_response(result(None))

    assert isinstance(route.primary, DeterministicResponse)
    assert route.primary.kind is DeterministicResponseKind.DESIGN_HANDOFF


@pytest.mark.parametrize(
    "code",
    [TurnFailureCode.LOCKED_PRODUCT_UNAVAILABLE, TurnFailureCode.DESIGN_UNAVAILABLE],
)
def test_a_handled_failure_stays_a_handled_failure(code: TurnFailureCode) -> None:
    """Four different meanings, kept four different routes."""
    route = route_response(result(None, failure=TurnFailure(code=code)))

    assert isinstance(route.primary, DeterministicResponse)
    assert route.primary.kind is DeterministicResponseKind.HANDLED_FAILURE
    assert route.primary.failure_code is code


# ── what the model may see ══════════════════════════════════════════════════


def test_the_model_sees_counts_and_enums_only() -> None:
    outcome = bundle(
        line(1),
        line(2, acquisition=BundleAcquisition.ALREADY_OWNED, locked=True),
        line(3, depth=2),
        status=BundleStatus.PARTIAL,
        unmet_needs=(unmet(), unmet(DesignPriority.OPTIONAL, UnmetReason.BUDGET_EXHAUSTED)),
    )

    safe = view(outcome, budget="15000").bundle
    assert safe is not None
    assert safe.bundle_line_count == 3
    assert safe.locked_line_count == 1
    assert safe.already_owned_line_count == 1
    assert safe.relaxed_line_count == 1
    assert safe.required_unmet_count == 1
    assert safe.optional_unmet_count == 1
    assert safe.budget_supplied is True
    assert safe.within_budget is True


@pytest.mark.parametrize(
    "forbidden",
    [
        "price",
        "total",
        "currency",
        "budget_max",
        "amount",
        "product",
        "name",
        "url",
        "image",
        "rank",
        "similarity",
        "depth",
        "need_index",
        "store",
        "line_id",
        "dimension",
    ],
)
def test_no_catalog_value_can_be_named_in_the_safe_view(forbidden: str) -> None:
    for field in BundleGroundingView.model_fields:
        assert forbidden not in field, field


def test_the_safe_view_reaches_no_application_type() -> None:
    definitions = set(ResponseGroundingView.model_json_schema().get("$defs", {}))

    for forbidden in ("ProductCandidate", "BundleLine", "RoomBundle", "GroundedBundleItem"):
        assert forbidden not in definitions


def test_unmet_reasons_are_deduplicated_in_first_seen_order() -> None:
    outcome = bundle(
        line(),
        status=BundleStatus.PARTIAL,
        unmet_needs=(
            unmet(reason=UnmetReason.BUDGET_EXHAUSTED),
            unmet(DesignPriority.RECOMMENDED, UnmetReason.NO_CANDIDATES, index=8),
            unmet(DesignPriority.OPTIONAL, UnmetReason.BUDGET_EXHAUSTED, index=7),
        ),
    )

    safe = view(outcome, budget="15000").bundle
    assert safe is not None
    assert safe.unmet_reasons == (
        UnmetReason.BUDGET_EXHAUSTED,
        UnmetReason.NO_CANDIDATES,
    )


def test_no_fulfilled_counts_were_invented() -> None:
    """A bundle line does not carry the priority of the need it filled, and
    recovering it would mean re-reading a design plan this layer has not got."""
    for field in BundleGroundingView.model_fields:
        assert "fulfilled" not in field


def test_no_budget_means_no_claim_about_one() -> None:
    safe = view(bundle(line())).bundle
    assert safe is not None
    assert safe.budget_supplied is False
    assert safe.within_budget is None


def test_an_infeasible_package_is_outside_its_budget() -> None:
    safe = view(
        bundle(line(locked=True), status=BundleStatus.INFEASIBLE, unmet_needs=(unmet(),)),
        budget="15000",
    ).bundle
    assert safe is not None
    assert safe.within_budget is False


def test_a_partial_package_can_never_be_labelled_complete() -> None:
    with pytest.raises(ValidationError):
        BundleGroundingView(status=BundleStatus.COMPLETE, required_unmet_count=1)


def test_recommended_gaps_do_not_make_a_package_partial() -> None:
    safe = BundleGroundingView(
        status=BundleStatus.COMPLETE, bundle_line_count=2, recommended_unmet_count=3
    )

    assert safe.status is BundleStatus.COMPLETE


def test_only_a_room_outcome_carries_a_bundle() -> None:
    with pytest.raises(ValidationError):
        ResponseGroundingView(
            kind=ResponseOutcomeKind.ANSWER,
            bundle=BundleGroundingView(status=BundleStatus.COMPLETE),
        )
    with pytest.raises(ValidationError):
        ResponseGroundingView(kind=ResponseOutcomeKind.ROOM_BUNDLE)


# ── the numeric allowance ═══════════════════════════════════════════════════


def _allowed(message: str, safe: BundleGroundingView) -> Any:
    return build_allowance(message, counts=bundle_counts(safe))


def test_an_approved_count_may_be_stated() -> None:
    safe = BundleGroundingView(status=BundleStatus.COMPLETE, bundle_line_count=5)

    violation = check_numeric_policy(
        message="I put 5 pieces together.",
        follow_up_question=None,
        allowance=_allowed("design my living room", safe),
    )

    assert violation is None


@pytest.mark.parametrize(
    ("figure", "why"),
    [("13070", "a total"), ("1200", "a unit price"), ("220", "a dimension")],
)
def test_a_catalog_figure_is_still_refused(figure: str, why: str) -> None:
    safe = BundleGroundingView(status=BundleStatus.COMPLETE, bundle_line_count=3)

    violation = check_numeric_policy(
        message=f"That comes to SAR {figure}.",
        follow_up_question=None,
        allowance=_allowed("design my living room", safe),
    )

    assert violation is not None, why


def test_a_budget_is_sayable_only_because_the_customer_said_it() -> None:
    safe = BundleGroundingView(status=BundleStatus.COMPLETE, bundle_line_count=3)
    allowance = _allowed("design my living room under SAR 15000", safe)

    assert check_numeric_policy(
        message="I kept it under 15000.", follow_up_question=None, allowance=allowance
    ) is None
    assert check_numeric_policy(
        message="I kept it under 12000.", follow_up_question=None, allowance=allowance
    ) is not None


def test_a_product_quantity_is_not_admitted() -> None:
    """The model never learns which piece a quantity belongs to, so an isolated
    "four" would be a factual claim with no subject."""
    safe = BundleGroundingView(status=BundleStatus.COMPLETE, bundle_line_count=2)

    violation = check_numeric_policy(
        message="I included 4 of them.",
        follow_up_question=None,
        allowance=_allowed("design my living room", safe),
    )

    assert violation is not None


def test_the_allowance_names_its_fields_rather_than_sweeping_them() -> None:
    """A future numeric field on the view must not silently widen authority."""
    import ast
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/numeric_guard.py").read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "bundle_counts"
    )
    named = {node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)}

    assert named == {
        "bundle_line_count",
        "locked_line_count",
        "already_owned_line_count",
        "required_unmet_count",
        "recommended_unmet_count",
        "optional_unmet_count",
        "relaxed_line_count",
    }
    assert "model_fields" not in source
    assert "model_dump" not in source


# ── the application's half ══════════════════════════════════════════════════


def presentation(outcome: Any, *, budget: str | None = None) -> Any:
    return build_bundle_presentation(result(outcome, budget=budget))


def test_no_room_no_presentation() -> None:
    assert presentation(None) is None
    assert presentation(
        BundleUnavailable(reason=BundleUnavailableReason.LOCKED_PRICE_UNUSABLE)
    ) is None


def test_every_piece_is_rendered_with_its_verified_price() -> None:
    rendered = presentation(bundle(line(1, price="1200.00"), line(2, price="800.00")))

    assert [item.grounding_ref for item in rendered.items] == [1, 2]
    assert rendered.items[0].unit_price == Decimal("1200.00")
    assert rendered.items[0].price_unit == "SAR"
    assert rendered.items[0].name_english == "Sofa 1"


def test_four_of_one_product_is_one_card_with_a_quantity() -> None:
    rendered = presentation(bundle(line(1, quantity=4, price="500.00")))

    assert len(rendered.items) == 1
    assert rendered.items[0].quantity == 4
    assert rendered.items[0].new_spend_line_total == Decimal("2000.00")


def test_an_already_owned_piece_has_no_new_spend_and_no_fake_price() -> None:
    """Zero would read as a price, and the product does not cost nothing."""
    rendered = presentation(
        bundle(line(1, acquisition=BundleAcquisition.ALREADY_OWNED), total=None)
    )

    item = rendered.items[0]
    assert item.new_spend_line_total is None
    assert item.unit_price == Decimal("1200.00")


def test_one_product_bought_and_owned_stays_two_cards() -> None:
    """Merging them would say the customer is buying something they told us
    they already have."""
    rendered = presentation(
        bundle(
            line(1, acquisition=BundleAcquisition.TO_BUY),
            line(1, acquisition=BundleAcquisition.ALREADY_OWNED),
        )
    )

    assert len(rendered.items) == 2


def test_a_kept_piece_is_not_merged_with_a_suggested_one() -> None:
    rendered = presentation(bundle(line(1, locked=True), line(1, locked=False)))

    assert len(rendered.items) == 2
    assert {item.locked for item in rendered.items} == {True, False}


def test_identical_lines_merge_into_one_card() -> None:
    rendered = presentation(bundle(line(1, quantity=1), line(1, quantity=2)))

    assert len(rendered.items) == 1
    assert rendered.items[0].quantity == 3


@pytest.mark.parametrize("forbidden", ["product_id", "line_id", "store", "rank", "need"])
def test_a_rendered_card_carries_no_identity(forbidden: str) -> None:
    for field in GroundedBundleItem.model_fields:
        assert forbidden not in field, field


def test_the_totals_are_copied_not_recomputed() -> None:
    """A second summation could disagree with the one that decided the package."""
    rendered = presentation(bundle(line(1), line(2), total="9999.00"), budget="15000")

    assert rendered.totals.new_spend_total == Decimal("9999.00")
    assert rendered.totals.currency == "SAR"
    assert rendered.totals.budget_max_amount == Decimal("15000")
    assert rendered.totals.within_budget is True


def test_an_unavailable_total_is_reported_not_reinvented() -> None:
    rendered = presentation(
        bundle(line(1, acquisition=BundleAcquisition.ALREADY_OWNED), total=None)
    )

    assert rendered.totals.new_spend_total is None
    assert rendered.totals.total_unavailable is TotalUnavailableReason.NO_PRICED_LINES


def test_an_infeasible_room_still_renders_what_they_kept() -> None:
    rendered = presentation(
        bundle(line(1, locked=True), status=BundleStatus.INFEASIBLE, unmet_needs=()),
        budget="500",
    )

    assert rendered.status is BundleStatus.INFEASIBLE
    assert len(rendered.items) == 1
    assert rendered.totals.within_budget is False


def test_the_renderer_reaches_no_other_authority() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/bundle_presentation.py").read_text()
    for forbidden in (
        "Repository",
        "Pipeline",
        "Pinecone",
        "InteriorDesignAgent",
        "BundleOptimizer",
        "await",
        "float(",
    ):
        assert forbidden not in source, forbidden


# ── fallback wording ════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", list(BundleStatus))
def test_each_status_falls_back_to_its_own_sentence(status: BundleStatus) -> None:
    wording = fallback_for(ResponseOutcomeKind.ROOM_BUNDLE, status).lower()

    assert not any(character.isdigit() for character in wording)
    if status is BundleStatus.PARTIAL:
        for forbidden in ("complete", "everything", "full package", "ready"):
            assert forbidden not in wording
    if status is BundleStatus.INFEASIBLE:
        for forbidden in ("complete", "here's a room package"):
            assert forbidden not in wording


@pytest.mark.parametrize("reason", list(BundleUnavailableReason))
def test_every_refusal_has_safe_fixed_wording(reason: BundleUnavailableReason) -> None:
    wording = BUNDLE_UNAVAILABLE_WORDING[reason]

    assert wording
    assert not any(character.isdigit() for character in wording)
    for forbidden in (reason.value, "error", "exception", "provider", "store"):
        assert forbidden not in wording.lower()
