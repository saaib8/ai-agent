"""Whether a generated response may reach the customer.

The response layer is the boundary for its own model's output. A citation of
something that was never grounded, or a question the turn did not permit, must
not leave here for some later caller to notice - by then it is in front of the
customer.

Three checks, in a deliberate order, and only one of them is recoverable. A
figure with no approved source is a wording problem, and asking the model again
can fix it. A reference to a product that does not exist in this turn is not:
the model has misunderstood what it was shown, and a second attempt is not the
remedy. So the one correction call is reserved for numbers, and everything else
falls back.

Pure and deterministic. No model, no repository, no network.
"""

from __future__ import annotations

from app.schemas.agent_turn import CustomerResponse
from app.schemas.response import ProductBriefGroundingView, ResponseViolation, ResponseViolationKind
from app.services.numeric_guard import ResponseNumericAllowance, check_numeric_policy


def validate_response(
    response: CustomerResponse,
    *,
    valid_grounding_refs: frozenset[int],
    follow_up_allowed: bool,
    allowance: ResponseNumericAllowance,
    brief: ProductBriefGroundingView | None = None,
) -> ResponseViolation | None:
    """The first reason this response must not be returned, or None.

    Structured validation already happened - the object exists, so Pydantic
    accepted it. What remains is what a schema cannot express: whether the
    turn permitted a question, whether the citations name real items, and
    whether the figures have a source.

    Ordered cheapest and most certain first, and numeric last, so that the
    recoverable failure is only ever reported once the unrecoverable ones are
    ruled out. Reporting an unsupported number while a citation is also wrong
    would spend the correction call on a response that was going to fall back
    anyway.
    """
    if response.follow_up_question is not None and not follow_up_allowed:
        return ResponseViolation(
            kind=ResponseViolationKind.FOLLOW_UP_NOT_ALLOWED,
            field="follow_up_question",
        )

    if not _asks_the_opening(response, brief):
        # Its chips are drawn for the questions it names, so naming the wrong
        # ones would put answers under questions nobody asked.
        return ResponseViolation(kind=ResponseViolationKind.OPENING_NOT_OFFERED, field="asked")

    for ref in response.referenced_grounding_refs:
        if ref not in valid_grounding_refs:
            # Not translated to a nearby ref and not looked up: a citation the
            # model invented names nothing, and choosing a product on its
            # behalf would be inventing the reference a second time.
            return ResponseViolation(
                kind=ResponseViolationKind.UNKNOWN_GROUNDING_REF,
                detail=str(ref),
                field="referenced_grounding_refs",
            )

    numeric = check_numeric_policy(
        message=response.message,
        follow_up_question=response.follow_up_question,
        allowance=allowance,
    )
    if numeric is not None:
        return ResponseViolation(
            kind=ResponseViolationKind.UNSUPPORTED_NUMBER,
            detail=numeric.value,
            field=numeric.field,
        )
    for choice in response.choices:
        numeric = check_numeric_policy(
            message=choice.label, follow_up_question=choice.value, allowance=allowance
        )
        if numeric is not None:
            return ResponseViolation(
                kind=ResponseViolationKind.UNSUPPORTED_NUMBER,
                detail=numeric.value,
                field="choices",
            )
    return None


def _asks_the_opening(response: CustomerResponse, brief: ProductBriefGroundingView | None) -> bool:
    """Exactly as many of the offered questions as it had to choose. Where
    nothing was offered, `asked` draws nothing and is read by nothing, so a
    choice made anyway is ignored rather than costing the reply."""
    choose = brief.choose if brief is not None else 0
    if not choose:
        return True
    offered = set(brief.asks_about) if brief is not None else set()
    always = set(brief.must_ask) if brief is not None else set()
    return (
        len(response.asked) == min(choose, len(offered))
        and set(response.asked) <= offered
        and always <= set(response.asked)
    )
