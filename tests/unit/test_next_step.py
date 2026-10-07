"""Every reply leaves the customer a next step (CLAUDE.md 10.2).

The application decides the one next step a turn offers - compare two options
they picked, what goes with a pick, a room around their picks, which of two
compared they lean towards - and the reply closes on its question. When the
reply comes back without one, the question is added, so no reply ends on a dead
end. A turn that already asks something keeps its own question.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    BlockingClarification,
    BlockingClarificationReason,
    CustomerAgentDecision,
)
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse, CustomerTurnResult, TurnGrounding
from app.schemas.bundle import BundleStatus, RoomBundle, TotalUnavailableReason
from app.schemas.next_step import ANY_NEXT_STEP, QUESTIONS, NextStep, NextStepKind
from app.schemas.picks import PickView
from app.schemas.product_action import CompanionOffer, ComparePicksAction, GoesWithPickAction
from app.schemas.resolution import DeterministicClarification
from app.schemas.retailer import RetailerCatalogCapabilities, RetailerCatalogCapability
from app.services.chat_runtime import ChatRuntime
from app.services.next_step import next_step
from app.services.numeric_guard import picks_counts, picks_figures
from app.services.response_generator import _ends_on_a_question
from app.taxonomy.complements import load_complements
from app.taxonomy.registry import load_taxonomy

from tests.unit.test_turn_coordinator import _product

COMPLEMENTS = load_complements(taxonomy=load_taxonomy())


def _pick(number: int, kind: str | None, name: str = "Sofa", price: str = "1000") -> PickView:
    return PickView(
        pick=number,
        name_english=name,
        image_url="https://example.test/x.jpg",
        price_amount=Decimal(price),
        price_unit="SAR",
        kind=kind,
    )


def _result(**fields: Any) -> CustomerTurnResult:
    grounding = fields.pop("grounding", TurnGrounding())
    return CustomerTurnResult(
        state=AgentStateV1(),
        decision=CustomerAgentDecision(action=AgentAction.ANSWER),
        grounding=grounding,
        **fields,
    )


def _kind(result: CustomerTurnResult) -> NextStepKind | None:
    step = next_step(result, COMPLEMENTS)
    return step.kind if step else None


# ── which next step ─────────────────────────────────────────────────────────


def test_a_second_option_of_the_same_kind_is_cross_sold_never_compared() -> None:
    """The screenshot: a second sofa set picked beside the first is offered what
    goes with it - comparing the two is theirs to ask for."""
    picks = (_pick(1, "sofa set"), _pick(2, "sofa set"))

    step = next_step(_result(picks=picks, selection_added=True), COMPLEMENTS)

    assert step is not None and step.kind is NextStepKind.AFTER_PICKS
    goes_with = step.chips[0].product_action
    assert isinstance(goes_with, GoesWithPickAction) and goes_with.pick == 2
    assert not any(isinstance(c.product_action, ComparePicksAction) for c in step.chips)


def test_picks_without_a_new_second_option_offer_what_goes_with_them() -> None:
    """ "ok", "thank you", "what have I chosen?", a removal - with picks on record."""
    picks = (_pick(1, "sofa"), _pick(2, "center table"))

    step = next_step(_result(picks=picks), COMPLEMENTS)

    assert step is not None and step.kind is NextStepKind.AFTER_PICKS
    goes_with = step.chips[0].product_action
    assert isinstance(goes_with, GoesWithPickAction) and goes_with.pick == 2
    assert "Design a room around my picks" in [chip.label for chip in step.chips]


def test_a_pick_with_no_pairings_is_never_offered_an_empty_goes_with() -> None:
    """A floor lamp has no reviewed companions: offering "what goes with it"
    would come back empty, so the chips offer a room and more browsing."""
    step = next_step(_result(picks=(_pick(1, "floor lamp"),)), COMPLEMENTS)

    assert step is not None and step.kind is NextStepKind.ROOM_AROUND_PICKS
    assert "goes with" not in step.question
    assert not any(chip.product_action for chip in step.chips)
    assert [chip.label for chip in step.chips] == ["Design a room around my picks", "Keep browsing"]


@pytest.mark.parametrize(
    ("grounding", "extra", "kind"),
    [
        ({"product_detail": "detail"}, {}, NextStepKind.AFTER_DETAIL),
        ({"comparison": "comparison"}, {}, NextStepKind.AFTER_COMPARISON),
        ({}, {"bundle_outcome": "room"}, NextStepKind.AFTER_ROOM),
        ({}, {}, NextStepKind.START),
    ],
)
def test_each_situation_has_its_next_step(
    grounding: dict[str, Any], extra: dict[str, Any], kind: NextStepKind
) -> None:
    from app.schemas.comparison import ProductComparisonResult
    from app.services.grounding_builder import to_grounded_product

    built: dict[str, Any] = {}
    if "product_detail" in grounding:
        built["product_detail"] = to_grounded_product(
            _product(5), grounding_ref=1, presented_ordinal=1, relaxation_depth=0
        )
    if "comparison" in grounding:
        built["comparison"] = ProductComparisonResult.model_construct()
    fields: dict[str, Any] = {"grounding": TurnGrounding.model_construct(**built)}
    if "bundle_outcome" in extra:
        fields["bundle_outcome"] = RoomBundle(
            status=BundleStatus.COMPLETE,
            total_unavailable=TotalUnavailableReason.NO_PRICED_LINES,
        )

    assert _kind(_result(**fields)) is kind


def test_a_turn_that_asks_its_own_question_gets_no_second_one() -> None:
    companions = (CompanionOffer(category="decor", subcategory="carpet", label="rugs"),)

    assert next_step(_result(picks=(_pick(1, "sofa"),), companions=companions), COMPLEMENTS) is None


@pytest.mark.parametrize("reason", list(BlockingClarificationReason))
@pytest.mark.parametrize("deterministic", [False, True])
def test_only_the_product_type_question_gets_piece_choices(
    reason: BlockingClarificationReason,
    deterministic: bool,
) -> None:
    result = _result(
        grounding=(
            TurnGrounding(deterministic_clarification=DeterministicClarification(reason=reason))
            if deterministic
            else TurnGrounding(
                clarification=BlockingClarification(reason=reason, question="Which?")
            )
        )
    )
    capabilities = RetailerCatalogCapabilities(
        capabilities=(
            RetailerCatalogCapability(
                commerce_category="tables",
                commerce_subcategory="center-table",
                active_product_count=3,
            ),
        )
    )
    step = next_step(result, COMPLEMENTS, capabilities=capabilities)

    if reason is BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE:
        assert step is not None and step.kind is NextStepKind.CHOOSE_PIECE
        assert step.chips[0].label == "Center table"
        assert step.chips[0].value == "Show me center table"
        reply = CustomerResponse(message="Which?")
        assert _ends_on_a_question(reply, result.model_copy(update={"next_step": step})) == reply
    else:
        assert step is None


def test_piece_suggestions_are_bounded_varied_and_independent_of_catalog_order() -> None:
    result = _result(
        grounding=TurnGrounding(
            clarification=BlockingClarification(
                reason=BlockingClarificationReason.INSUFFICIENT_PRODUCT_TYPE, question="Which type?"
            )
        )
    )
    items = tuple(
        RetailerCatalogCapability(
            commerce_category=category, commerce_subcategory=subcategory, active_product_count=count
        )
        for category, subcategory, count in (
            ("seating", "sofa", 100),
            ("seating", "chair", 90),
            ("seating", "recliner", 80),
            ("seating", "sofa-set", 70),
            ("seating", "stool", 60),
            ("seating", "lounge-chair", 50),
            ("bedroom", "bed", 10),
            ("tables", None, 5),
        )
    )
    step = next_step(
        result, COMPLEMENTS, capabilities=RetailerCatalogCapabilities(capabilities=items)
    )
    reversed_step = next_step(
        result,
        COMPLEMENTS,
        capabilities=RetailerCatalogCapabilities(capabilities=tuple(reversed(items))),
    )
    assert step is not None and step == reversed_step
    assert len(step.chips) == 6
    assert [choice.label for choice in step.chips[:3]] == ["Sofa", "Bed", "Tables"]
    assert next_step(result, COMPLEMENTS, capabilities=RetailerCatalogCapabilities()) is None


# ── the guarantee ───────────────────────────────────────────────────────────


def test_a_reply_without_a_question_gets_the_next_steps_question() -> None:
    """ "All set - your picks are saved" alone was the dead end in the screenshot."""
    step = NextStep(kind=NextStepKind.AFTER_PICKS)
    reply = CustomerResponse(message="All set - your shortlist is saved.")

    closed = _ends_on_a_question(reply, _result(next_step=step))

    assert closed.message == (
        "All set - your shortlist is saved. " + QUESTIONS[NextStepKind.AFTER_PICKS]
    )


def test_a_reply_that_already_asks_is_left_as_it_is() -> None:
    reply = CustomerResponse(message="Shall I compare them side by side?")

    assert _ends_on_a_question(reply, _result(next_step=NextStep(kind=NextStepKind.START))) == reply


def test_a_follow_up_question_counts_as_asking() -> None:
    reply = CustomerResponse(message="Here they are.", follow_up_question="Which colour?")

    assert _ends_on_a_question(reply, _result()) == reply


def test_with_no_next_step_a_plain_what_next_is_added() -> None:
    closed = _ends_on_a_question(CustomerResponse(message="Done."), _result())

    assert closed.message == f"Done. {ANY_NEXT_STEP}"


@pytest.mark.parametrize("question", [*QUESTIONS.values(), ANY_NEXT_STEP])
def test_every_added_question_is_a_digit_free_question(question: str) -> None:
    """Added after the number check, so it must never carry a figure."""
    assert question.endswith("?")
    assert not any(ch.isdigit() for ch in question)


# ── chips and figures ───────────────────────────────────────────────────────


def test_the_next_steps_chips_are_drawn_when_the_turn_has_none() -> None:
    step = next_step(_result(picks=(_pick(1, "sofa"),)), COMPLEMENTS)

    presentation = ChatRuntime.presentation(_result(picks=(_pick(1, "sofa"),), next_step=step))

    assert presentation is not None
    assert [c.label for c in presentation.choices] == [c.label for c in step.chips]  # type: ignore[union-attr]


def test_the_picks_tray_figures_may_be_said() -> None:
    """ "Your 6-seater set" names a number on the tray, not an invention."""
    figures = picks_figures(
        (_pick(1, "sofa set", name="6 Seater Corner Set 230x330", price="9450"),)
    )

    assert set(figures) == {Decimal("9450"), Decimal("6"), Decimal("230"), Decimal("330")}


def test_the_picks_counts_may_be_said() -> None:
    """ "You now have 2 sofa sets - shall I compare the 2?" reads the tray."""
    tray = (_pick(1, "sofa set"), _pick(2, "sofa set"), _pick(3, "rug"))

    assert sorted(picks_counts(tray)) == [1, 2, 3]
    assert picks_counts(None) == ()


def test_a_turn_with_its_own_question_on_screen_gets_nothing_added() -> None:
    """ "Tap whatever matters to you below" above a question card is already
    a question - a "what next?" after it would be a second one."""
    companions = (CompanionOffer(category="decor", subcategory="carpet", label="rugs"),)
    reply = CustomerResponse(message="Here are a few rugs that go with it.")

    assert _ends_on_a_question(reply, _result(companions=companions)) == reply
