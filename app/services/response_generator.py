"""Turning a finished turn into words for the customer.

The work is already done when this runs. The coordinator executed the turn and
committed the state, so nothing here can change what happened - this layer only
says what it was. A provider failure, an unsafe reply, a fallback: none of them
touches `CustomerTurnResult.state`, because this service holds no reducer and
no state to hold.

Three rules shape it.

**The model is given no product fact.** Names, prices, sizes and links are
rendered by the application from verified grounding, so prose that cannot see
them cannot misstate them. That is why the numeric guard can be small.

**Most branches never reach a model.** A question the decision model already
wrote is passed through; a handled failure and a design handoff are worded from
constants. A model call is for turns where there is genuinely something to
phrase.

**One retry, for one reason.** A figure with no source is a wording problem and
asking again can fix it. A citation of a product that was never grounded means
the model misunderstood what it was shown, and repeating the request is not the
remedy - that falls back instead.
"""

from __future__ import annotations

import time

from pydantic import ValidationError

from app.core.exceptions import (
    IntegrationUnavailableError,
    LLMRequestError,
    LLMResponseInvalidError,
)
from app.core.logging import get_logger
from app.integrations.llm import StructuredLLMClient
from app.prompts.customer_commerce.response_v1 import (
    VERSION,
    build_correction_instructions,
    build_instructions,
)
from app.schemas.agent_turn import (
    CustomerResponse,
    CustomerTurnInput,
    CustomerTurnResult,
)
from app.schemas.response import (
    DeterministicResponse,
    DeterministicResponseKind,
    ResponseGroundingView,
    ResponseInput,
    ResponseOutcomeKind,
    ResponseRoute,
    ResponseViolation,
)
from app.services.numeric_guard import build_allowance, bundle_counts
from app.services.response_validation import validate_response
from app.services.response_view import route_response, valid_grounding_refs
from app.services.response_wording import (
    BUNDLE_ACQUISITION_WORDING,
    BUNDLE_CHANGED_NOT_REFRESHED_WORDING,
    BUNDLE_KEPT_WORDING,
    BUNDLE_UNAVAILABLE_WORDING,
    BUNDLE_UNLOCKED_WORDING,
    DESIGN_HANDOFF_WORDING,
    DETERMINISTIC_FALLBACK,
    FAILURE_WORDING,
    SIDE_NOTICE_WORDING,
    compose,
    fallback_for,
)

logger = get_logger(__name__)

_HANDLED_PROVIDER_FAILURES = (
    IntegrationUnavailableError,
    LLMRequestError,
    LLMResponseInvalidError,
)
"""Provider outcomes that become a fallback rather than an exception.

The turn already succeeded and its state is already committed. Raising here
would lose the customer's reply over a failure that has nothing to do with what
they asked for, so the application says something safe instead. An unexpected
error still propagates - this is not a universal wrapper (CLAUDE.md 21).
"""


class CustomerResponseGenerator:
    """One finished turn in, one customer-safe reply out."""

    def __init__(self, client: StructuredLLMClient) -> None:
        self._client = client
        self._instructions = build_instructions()
        self._correction_instructions = build_correction_instructions()

    async def generate(
        self, turn: CustomerTurnInput, result: CustomerTurnResult
    ) -> CustomerResponse:
        """The reply for this turn, whatever happened during it.

        Always returns something. The route decides whether a model is involved
        at all; the side notice is appended afterwards, by the application, so
        a failed side effect is reported without a model being told about it.
        """
        started = time.perf_counter()
        route = route_response(result)

        response, calls, used_fallback = await self._primary_response(turn, result, route)
        final = self._with_side_notice(response, route)

        self._log(route, calls, used_fallback, started)
        return final

    # ── the branch that decides whether a model is involved ─────────────────

    async def _primary_response(
        self, turn: CustomerTurnInput, result: CustomerTurnResult, route: ResponseRoute
    ) -> tuple[CustomerResponse, int, bool]:
        primary = route.primary
        if isinstance(primary, DeterministicResponse):
            return await self._deterministic(turn, result, route, primary)
        return await self._generated(turn, result, route, primary)

    async def _deterministic(
        self,
        turn: CustomerTurnInput,
        result: CustomerTurnResult,
        route: ResponseRoute,
        primary: DeterministicResponse,
    ) -> tuple[CustomerResponse, int, bool]:
        """A branch the application words itself.

        The one exception is a design handoff that also owes a question: the
        acknowledgement is fixed, and the question still has to be phrased, so
        that turn makes a single call for the question alone. Nothing about the
        handoff is sent with it.
        """
        match primary.kind:
            case DeterministicResponseKind.MODEL_CLARIFICATION:
                clarification = result.grounding.clarification
                if clarification is None:  # pragma: no cover - routing guarantees it
                    return _reply(DETERMINISTIC_FALLBACK[primary.kind]), 0, True
                # The decision model wrote this question and it was validated
                # in its own phase. Re-wording it could only change what was
                # asked, so it is carried through untouched and not scanned.
                return _reply(clarification.question), 0, False

            case DeterministicResponseKind.HANDLED_FAILURE:
                assert primary.failure_code is not None
                return _reply(FAILURE_WORDING[primary.failure_code]), 0, False

            case DeterministicResponseKind.DESIGN_HANDOFF:
                if route.required_clarification is None:
                    return _reply(DESIGN_HANDOFF_WORDING), 0, False
                question, calls, used_fallback = await self._generated_message(
                    turn,
                    result,
                    ResponseGroundingView(
                        kind=ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION,
                        clarification_reason=route.required_clarification.reason,
                        reference_reason=route.required_clarification.reference_reason,
                        relative_price_reason=(route.required_clarification.relative_price_reason),
                    ),
                    follow_up_allowed=False,
                )
                return (
                    _reply(compose(DESIGN_HANDOFF_WORDING, question)),
                    calls,
                    used_fallback,
                )

            case DeterministicResponseKind.BUNDLE_CHANGED_NOT_REFRESHED:
                return _reply(BUNDLE_CHANGED_NOT_REFRESHED_WORDING), 0, False

            case DeterministicResponseKind.BUNDLE_KEPT:
                return _reply(BUNDLE_KEPT_WORDING), 0, False

            case DeterministicResponseKind.BUNDLE_ACQUISITION_SET:
                # No model: the customer stated this, and a sentence that
                # re-described it could only get it wrong.
                assert primary.acquisition is not None
                return _reply(BUNDLE_ACQUISITION_WORDING[primary.acquisition]), 0, False

            case DeterministicResponseKind.BUNDLE_UNLOCKED:
                return _reply(BUNDLE_UNLOCKED_WORDING), 0, False

            case DeterministicResponseKind.BUNDLE_UNAVAILABLE:
                # The optimiser said exactly why it could not compute. A model
                # asked to explain that would start proposing remedies nobody
                # authorised - dropping a lock, changing a budget.
                assert primary.bundle_reason is not None
                return _reply(BUNDLE_UNAVAILABLE_WORDING[primary.bundle_reason]), 0, False

    async def _generated(
        self,
        turn: CustomerTurnInput,
        result: CustomerTurnResult,
        route: ResponseRoute,
        view: ResponseGroundingView,
    ) -> tuple[CustomerResponse, int, bool]:
        """A turn a model words, cited products and all."""
        request = ResponseInput(
            message=turn.message,
            conversation=turn.conversation,
            grounding=view,
            follow_up_allowed=route.follow_up_allowed,
        )
        allowance = build_allowance(
            turn.message,
            presented_count=view.presented_count,
            compared_count=view.compared_count,
            counts=bundle_counts(view.bundle) if view.bundle else (),
        )
        refs = valid_grounding_refs(result.grounding)

        response, calls = await self._call_and_validate(request, allowance=allowance, refs=refs)
        if response is None:
            return _reply(_fallback(view)), calls, True
        return response, calls, False

    async def _generated_message(
        self,
        turn: CustomerTurnInput,
        result: CustomerTurnResult,
        view: ResponseGroundingView,
        *,
        follow_up_allowed: bool,
    ) -> tuple[str, int, bool]:
        """Just the words, for a branch that composes them with its own."""
        request = ResponseInput(
            message=turn.message,
            conversation=turn.conversation,
            grounding=view,
            follow_up_allowed=follow_up_allowed,
        )
        response, calls = await self._call_and_validate(
            request,
            allowance=build_allowance(turn.message),
            # Nothing is grounded on this branch, so nothing may be cited.
            refs=frozenset(),
        )
        if response is None:
            return _fallback(view), calls, True
        return response.message, calls, False

    # ── the call, and the one retry it may earn ─────────────────────────────

    async def _call_and_validate(
        self,
        request: ResponseInput,
        *,
        allowance: frozenset[str],
        refs: frozenset[int],
    ) -> tuple[CustomerResponse | None, int]:
        """At most two calls, and the second only for an unsupported figure.

        `None` means fall back. Every failure that is not a numeric one returns
        it immediately: a bad citation or a question the turn did not permit is
        a misunderstanding, not a slip of phrasing.
        """
        first = await self._parse(self._instructions, request)
        if first is None:
            return None, 1

        violation = validate_response(
            first,
            valid_grounding_refs=refs,
            follow_up_allowed=request.follow_up_allowed,
            allowance=allowance,
        )
        if violation is None:
            return first, 1
        if not violation.permits_correction:
            self._log_violation(violation, attempt=1)
            return None, 1

        self._log_violation(violation, attempt=1)
        second = await self._parse(self._correction_instructions, request)
        if second is None:
            return None, 2

        repeated = validate_response(
            second,
            valid_grounding_refs=refs,
            follow_up_allowed=request.follow_up_allowed,
            allowance=allowance,
        )
        if repeated is None:
            return second, 2
        # Whatever it is this time, there is no third call.
        self._log_violation(repeated, attempt=2)
        return None, 2

    async def _parse(self, instructions: str, request: ResponseInput) -> CustomerResponse | None:
        """One provider call, or None when it could not be made.

        The request travels as the user turn: it carries the customer's own
        words, which are untrusted data and never part of the instructions
        (CLAUDE.md 20.1).
        """
        try:
            return await self._client.parse(
                instructions=instructions,
                user_input=request.model_dump_json(exclude_none=True),
                schema=CustomerResponse,
            )
        except _HANDLED_PROVIDER_FAILURES as exc:
            logger.warning("response_generation_unavailable", error=type(exc).__name__)
            return None

    # ── composition ─────────────────────────────────────────────────────────

    def _with_side_notice(
        self, response: CustomerResponse, route: ResponseRoute
    ) -> CustomerResponse:
        """The failed side effect, appended by the application.

        Never generated: the model is not told a side effect failed, because a
        model told about a failure starts explaining it, and there is nothing
        to explain that a fixed sentence does not say better.
        """
        if route.side_notice is None:
            return response
        notice = SIDE_NOTICE_WORDING[route.side_notice]
        try:
            return CustomerResponse(
                message=compose(response.message, notice),
                referenced_grounding_refs=response.referenced_grounding_refs,
                follow_up_question=response.follow_up_question,
            )
        except ValidationError:
            # Only reachable if the composed message exceeds the contract's
            # ceiling. The notice matters more than the framing, so the reply
            # keeps it and drops the rest rather than losing either silently.
            logger.warning("response_notice_composition_failed")
            return _reply(notice)

    def _log_violation(self, violation: ResponseViolation, *, attempt: int) -> None:
        logger.warning(
            "response_policy_violation",
            kind=str(violation.kind),
            field=violation.field,
            attempt=attempt,
            # The offending token is safe: it is a number or a handle we
            # produced the valid set for, never customer or catalog text.
            detail=violation.detail,
        )

    def _log(self, route: ResponseRoute, calls: int, used_fallback: bool, started: float) -> None:
        """Shape of the turn only: no message, no history, no prose, no facts."""
        primary = route.primary
        logger.info(
            "customer_response_completed",
            prompt_version=VERSION,
            model=self._client.model,
            outcome=str(primary.kind),
            response_calls=calls,
            regeneration_used=calls > 1,
            fallback_used=used_fallback,
            required_clarification=route.required_clarification is not None,
            side_notice=route.side_notice is not None,
            follow_up_allowed=route.follow_up_allowed,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )


def _reply(message: str) -> CustomerResponse:
    """An application-written reply: no citations, no optional question."""
    return CustomerResponse(message=message)


def _fallback(view: ResponseGroundingView) -> str:
    """This outcome's fixed sentence, bundle status included where it has one."""
    return fallback_for(view.kind, view.bundle.status if view.bundle else None)
