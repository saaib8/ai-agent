# Arabic-Indic digits and separators are this file's test data, not look-alikes.
# ruff: noqa: RUF001, RUF002
"""Figures in Arabic, and figures said in words (docs/arabic-replies-plan.md, phase 4).

Three readers and one new source:

- the number check reads Arabic-Indic digits and Arabic's own separators, so
  "١٢٬٥٠٠" is one figure, not two;
- the reader of model output (`app/core/numbers.py`) maps them first, then
  applies its ambiguity rules unchanged;
- the figures query understanding read from the customer's own words this turn
  - a budget, a seat count - may be said back in digits, which fixes a reply
  quoting "6" for "لستة أشخاص" (or "3" for "three seater", on main too) being
  refused and replaced by the fallback.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.core.numbers import parse_stated_amount, parse_stated_decimal, parse_stated_percent
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse, CustomerTurnResult, TurnGrounding
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.refinement import (
    CapacityRefinement,
    PriceRefinement,
    PriceRefinementOp,
    PriceRelation,
    RefinementOp,
    RelativePriceRefinement,
    SearchRefinementDelta,
)
from app.services.numeric_guard import (
    build_allowance,
    canonical_number,
    numbers_in,
    refinement_figures,
    stated_figures,
)
from app.services.response_generator import CustomerResponseGenerator
from app.taxonomy.dimensions import DimensionRole

from tests.unit.test_response_generator import FakeClient, _search, _turn
from tests.unit.test_turn_coordinator import CONTEXT, _coordinator, _resolved, _state

AR_3000 = "٣٠٠٠"  # three thousand, Arabic-Indic digits
AR_12500 = "١٢٬٥٠٠"  # 12 (thousands sep) 500
AR_3_5 = "٣٫٥"  # 3 (decimal sep) 5
AR_COMMA = "،"  # the Arabic list comma


# ── the number check ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{AR_3000} ريال", {"3000"}),
        (f"{AR_12500} ريال", {"12500"}),
        (f"{AR_3_5} متر", {"3.5"}),
        ("1,250 ريال", {"1250"}),
        ("كنبة 3 مقاعد؟", {"3"}),
        (f"{AR_3000}{AR_COMMA} {AR_12500}", {"3000", "12500"}),
        ("بين 1,250 و 3,000 ريال", {"1250", "3000"}),
    ],
)
def test_arabic_figures_read_as_one_value_each(text: str, expected: set[str]) -> None:
    assert numbers_in(text) == frozenset(expected)


def test_the_same_figure_canonicalises_the_same_in_either_script() -> None:
    assert canonical_number(AR_3000) == canonical_number("3,000") == "3000"
    assert canonical_number(AR_12500) == canonical_number("12,500") == "12500"


def test_a_figure_they_typed_in_arabic_digits_may_be_said_in_western() -> None:
    allowed = build_allowance(f"أبي كنبة بحدود {AR_3000} ريال")

    assert numbers_in("الكنبات هنا ضمن 3,000 ريال") <= allowed


# ── what the turn read from their words ─────────────────────────────────────


def test_budget_bounds_and_seat_counts_are_their_figures() -> None:
    request = ProductSearchRequest(
        commerce_category="seating",
        commerce_subcategory="sofa",
        price=PriceConstraint(
            currency="SAR", min_amount=Decimal("1000"), max_amount=Decimal("3000")
        ),
        seating_capacity=SeatingCapacityConstraint(min_capacity=3, max_capacity=3),
    )

    assert stated_figures(request) == (
        Decimal("1000"),
        Decimal("3000"),
        Decimal(3),
        Decimal(3),
    )


def test_sizes_are_never_admitted() -> None:
    """Stored in centimetres: "2 m" said back as "200 cm" is a conversion."""
    request = ProductSearchRequest(
        commerce_category="seating",
        commerce_subcategory="sofa",
        dimensions=(
            DimensionConstraint(
                role=DimensionRole.OVERALL_WIDTH,
                kind=DimensionConstraintKind.MAX,
                max_cm=Decimal("200"),
            ),
        ),
    )

    assert stated_figures(request) == ()


def test_a_request_with_no_figures_admits_nothing() -> None:
    assert stated_figures(ProductSearchRequest(commerce_category="seating")) == ()


async def test_a_new_search_carries_what_they_said_to_the_reply() -> None:
    request = ProductSearchRequest(
        commerce_category="seating",
        commerce_subcategory="sofa",
        price=PriceConstraint(currency="SAR", max_amount=Decimal("300")),
        seating_capacity=SeatingCapacityConstraint(min_capacity=3, max_capacity=3),
    )
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.SEARCH), interpretation=_resolved(request)
    )

    result = await coordinator.run(
        _turn_input("three seater sofa under 300 SAR", _state(request=None))
    )

    assert result.stated_figures == (Decimal("300"), Decimal(3), Decimal(3))


def _turn_input(message: str, state: AgentStateV1):  # type: ignore[no-untyped-def]
    from app.schemas.agent_turn import CustomerTurnInput

    return CustomerTurnInput(message=message, state=state, context=CONTEXT)


# ── the reply that quoted them ──────────────────────────────────────────────


def _result(figures: tuple[Decimal, ...]) -> CustomerTurnResult:
    return CustomerTurnResult(
        state=AgentStateV1(),
        decision=CustomerAgentDecision(action=AgentAction.SEARCH),
        grounding=TurnGrounding(search=_search(0)),
        stated_figures=figures,
    )


REPLY = CustomerResponse(
    message="لا توجد طاولة طعام لـ 6 أشخاص بأقل من 200 ريال. هل أعرض لك الأقرب سعرًا؟"
)


async def test_a_seat_count_said_in_words_may_be_quoted_in_digits() -> None:
    client = FakeClient(REPLY)

    response = await CustomerResponseGenerator(client).generate(
        _turn("طاولة طعام لستة أشخاص بأقل من 200 ريال"),
        _result((Decimal("200"), Decimal(6), Decimal(6))),
    )

    assert response.message == REPLY.message
    assert len(client.calls) == 1


async def test_without_it_the_same_reply_is_refused() -> None:
    """The failure QA found live: "6" had no source, so the reply was refused
    twice and replaced by the English fallback."""
    client = FakeClient(REPLY)

    response = await CustomerResponseGenerator(client).generate(
        _turn("طاولة طعام لستة أشخاص بأقل من 200 ريال"), _result(())
    )

    assert response.message != REPLY.message
    assert len(client.calls) == 2


# ── the reader of model output ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (AR_3000, Decimal("3000")),
        (AR_12500, Decimal("12500")),
        ("٥k", Decimal("5000")),
        ("3,000", Decimal("3000")),
    ],
)
def test_an_amount_reads_the_same_in_either_script(raw: str, expected: Decimal) -> None:
    assert parse_stated_amount(raw) == expected


def test_an_arabic_decimal_separator_divides() -> None:
    assert parse_stated_decimal(AR_3_5) == Decimal("3.5")
    assert parse_stated_percent("٢٠%") == Decimal("20")


@pytest.mark.parametrize(
    "raw",
    [
        "٥٫٠٠٠",  # five, or five thousand
        "٢,٥",  # a decimal comma, or a thousands one
        "5.000",
        "2,5",
    ],
)
def test_the_ambiguity_rules_hold_in_either_script(raw: str) -> None:
    """A misread figure is worse than a refused one (CLAUDE.md 21.1)."""
    with pytest.raises(ValueError):
        parse_stated_amount(raw)


# ── a refinement that states a figure ───────────────────────────────────────


def _delta(**fields: object) -> SearchRefinementDelta:
    return SearchRefinementDelta(**fields)  # type: ignore[arg-type]


def test_an_absolute_budget_and_seat_count_are_their_figures() -> None:
    delta = _delta(
        price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="3,000", currency="SAR"),
        seating_capacity=CapacityRefinement(op=RefinementOp.SET, min_capacity=4),
    )

    assert refinement_figures(delta) == (Decimal("3000"), Decimal(4))


def test_a_relative_price_is_never_their_figure() -> None:
    """ "Cheaper than the second one": the bound is computed from a product."""
    delta = _delta(
        price=PriceRefinement(
            op=PriceRefinementOp.SET_RELATIVE,
            relative=RelativePriceRefinement(
                relation=PriceRelation.PERCENT_CHEAPER,
                reference=PresentedOrdinal(position=2),
                percent="20",
            ),
        )
    )

    assert refinement_figures(delta) == ()


def test_an_amount_the_reader_refuses_is_not_admitted() -> None:
    delta = _delta(
        price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="5.000", currency="SAR")
    )

    assert refinement_figures(delta) == ()


def test_a_cleared_bound_states_nothing() -> None:
    delta = _delta(seating_capacity=CapacityRefinement(op=RefinementOp.CLEAR))

    assert refinement_figures(delta) == ()


async def test_a_refinement_carries_what_they_said_to_the_reply() -> None:
    """The live failure QA found: "only ones for four people please"."""
    decision = CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH,
        refinement=_delta(seating_capacity=CapacityRefinement(op=RefinementOp.SET, min_capacity=4)),
    )
    coordinator, _ = _coordinator(decision)

    result = await coordinator.run(_turn_input("only ones for four people please", _state()))

    assert result.stated_figures == (Decimal(4),)


def test_the_arabic_percent_sign_reads_as_a_percent() -> None:
    assert parse_stated_percent("20٪") == Decimal("20")
