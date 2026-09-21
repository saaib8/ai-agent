"""Whether a generated response may reach the customer.

The response layer is the boundary for its own model's output. An invented
citation must not leave here for a later caller to notice - by then it is in
front of someone.

The distinction every test turns on: **only an unsupported number earns the one
correction call.** A figure with no source is a wording problem and asking again
can fix it. A reference to a product that was never grounded means the model
misunderstood what it was shown, and repeating the request is not the remedy.
"""

from __future__ import annotations

import pytest
from app.schemas.agent_turn import CustomerResponse
from app.schemas.response import ResponseViolation, ResponseViolationKind
from app.services.numeric_guard import build_allowance
from app.services.response_validation import validate_response

SEARCH_REFS = frozenset({1, 2, 3})
DETAIL_REF = frozenset({1})
NO_REFS: frozenset[int] = frozenset()


def _validate(
    response: CustomerResponse,
    *,
    refs: frozenset[int] = SEARCH_REFS,
    follow_up_allowed: bool = False,
    message: str = "show me sofas",
    presented_count: int = 3,
) -> ResponseViolation | None:
    return validate_response(
        response,
        valid_grounding_refs=refs,
        follow_up_allowed=follow_up_allowed,
        allowance=build_allowance(message, presented_count=presented_count),
    )


# ── grounding references ────────────────────────────────────────────────────


def test_citing_a_grounded_item_is_valid() -> None:
    response = CustomerResponse(
        message="The second one suits a smaller room.", referenced_grounding_refs=(2,)
    )

    assert _validate(response) is None


def test_citing_nothing_is_valid() -> None:
    """Prose need not point at anything."""
    assert _validate(CustomerResponse(message="Here are some options.")) is None


def test_citing_an_item_that_was_never_grounded_is_rejected() -> None:
    response = CustomerResponse(message="Look at it.", referenced_grounding_refs=(4,))

    violation = _validate(response)

    assert violation is not None
    assert violation.kind is ResponseViolationKind.UNKNOWN_GROUNDING_REF
    assert violation.detail == "4"
    assert violation.field == "referenced_grounding_refs"


def test_a_detail_turn_offers_exactly_its_own_handle() -> None:
    good = CustomerResponse(message="It is a good fit.", referenced_grounding_refs=(1,))
    bad = CustomerResponse(message="It is a good fit.", referenced_grounding_refs=(2,))

    assert _validate(good, refs=DETAIL_REF) is None
    violation = _validate(bad, refs=DETAIL_REF)
    assert violation is not None
    assert violation.kind is ResponseViolationKind.UNKNOWN_GROUNDING_REF


def test_a_branch_with_no_grounded_products_permits_no_citation() -> None:
    response = CustomerResponse(message="Sure.", referenced_grounding_refs=(1,))

    violation = _validate(response, refs=NO_REFS)

    assert violation is not None
    assert violation.kind is ResponseViolationKind.UNKNOWN_GROUNDING_REF


def test_an_unknown_ref_is_never_translated_to_a_nearby_one() -> None:
    """Choosing a product on the model's behalf would invent the reference a
    second time."""
    response = CustomerResponse(message="That one.", referenced_grounding_refs=(9,))

    violation = _validate(response)

    assert violation is not None
    assert violation.detail == "9", "reported, not repaired"


# ── the follow-up gate ──────────────────────────────────────────────────────


def test_a_question_the_turn_did_not_permit_is_rejected() -> None:
    response = CustomerResponse(
        message="Here are some options.", follow_up_question="Shall I narrow it down?"
    )

    violation = _validate(response, follow_up_allowed=False)

    assert violation is not None
    assert violation.kind is ResponseViolationKind.FOLLOW_UP_NOT_ALLOWED
    assert violation.field == "follow_up_question"


def test_a_permitted_question_is_valid() -> None:
    response = CustomerResponse(
        message="Here are some options.", follow_up_question="Shall I narrow it down?"
    )

    assert _validate(response, follow_up_allowed=True) is None


# ── only numbers earn the second call ───────────────────────────────────────


def test_an_unsupported_number_permits_the_correction_call() -> None:
    response = CustomerResponse(message="I found some around 4,200.")

    violation = _validate(response)

    assert violation is not None
    assert violation.kind is ResponseViolationKind.UNSUPPORTED_NUMBER
    assert violation.detail == "4200"
    assert violation.permits_correction is True


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            CustomerResponse(message="That one.", referenced_grounding_refs=(4,)),
            ResponseViolationKind.UNKNOWN_GROUNDING_REF,
        ),
        (
            CustomerResponse(message="Here.", follow_up_question="More?"),
            ResponseViolationKind.FOLLOW_UP_NOT_ALLOWED,
        ),
    ],
    ids=["unknown ref", "unpermitted question"],
)
def test_no_other_violation_permits_a_correction_call(
    response: CustomerResponse, expected: ResponseViolationKind
) -> None:
    violation = _validate(response)

    assert violation is not None
    assert violation.kind is expected
    assert violation.permits_correction is False


def test_exactly_one_violation_kind_is_recoverable() -> None:
    recoverable = {
        kind for kind in ResponseViolationKind
        if ResponseViolation(kind=kind).permits_correction
    }

    assert recoverable == {ResponseViolationKind.UNSUPPORTED_NUMBER}


# ── ordering ────────────────────────────────────────────────────────────────


def test_an_unknown_ref_is_reported_before_a_bad_number() -> None:
    """Both are wrong, and the response is going to fall back either way.

    Reporting the number first would spend the correction call on a response
    that could never be returned.
    """
    response = CustomerResponse(
        message="I found some around 4,200.", referenced_grounding_refs=(4,)
    )

    violation = _validate(response)

    assert violation is not None
    assert violation.kind is ResponseViolationKind.UNKNOWN_GROUNDING_REF
    assert violation.permits_correction is False


def test_an_unpermitted_question_is_reported_before_anything_else() -> None:
    response = CustomerResponse(
        message="Around 4,200.",
        referenced_grounding_refs=(4,),
        follow_up_question="More?",
    )

    violation = _validate(response)

    assert violation is not None
    assert violation.kind is ResponseViolationKind.FOLLOW_UP_NOT_ALLOWED


def test_a_clean_response_passes_every_check() -> None:
    response = CustomerResponse(
        message="I found 3 options; the 2nd suits a smaller room.",
        referenced_grounding_refs=(2,),
    )

    assert _validate(response) is None


# ── the validator is pure ───────────────────────────────────────────────────


def test_the_validator_is_pure() -> None:
    import ast
    from pathlib import Path

    source = (
        Path(__file__).parents[2] / "app/services/response_validation.py"
    ).read_text()
    tree = ast.parse(source)

    assert not any(isinstance(node, ast.Await) for node in ast.walk(tree))
    for forbidden in ("Repository", "client", "product_id", "RetailerContext"):
        assert forbidden not in source, forbidden
