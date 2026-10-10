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

import re
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
    build_correction_instructions,
    build_instructions,
    version_for,
)
from app.schemas.agent_turn import (
    MAX_RESPONSE_CHARS,
    CustomerResponse,
    CustomerTurnInput,
    CustomerTurnResult,
)
from app.schemas.conversation import ConversationRole
from app.schemas.language import ReplyLanguage
from app.schemas.next_step import ANY_NEXT_STEP, NextStepKind
from app.schemas.response import (
    DeterministicResponse,
    DeterministicResponseKind,
    ResponseGroundingView,
    ResponseInput,
    ResponseOutcomeKind,
    ResponseRoute,
    ResponseViolation,
    TypeMixView,
)
from app.schemas.screen import CustomerVisibleScreenView
from app.schemas.text_choice import TextReplyChoice, closes_on_a_question
from app.services.arabic_wording import in_language
from app.services.next_step import already_asks
from app.services.numeric_guard import (
    build_allowance,
    bundle_counts,
    bundle_stretch_figures,
    check_numeric_policy,
    chosen_seating_counts,
    guidance_figures,
    picks_counts,
    picks_figures,
    screen_figures,
    seating_counts,
    seating_figures,
    swap_offer_figures,
)
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
    FINISHING_TOUCH_OFFER_WORDING,
    SIDE_NOTICE_WORDING,
    compose,
    fallback_for,
)

logger = get_logger(__name__)


def _ends_on_a_question(
    response: CustomerResponse,
    result: CustomerTurnResult,
    language: ReplyLanguage = ReplyLanguage.EN,
) -> CustomerResponse:
    """Never a dead end: every reply leaves the customer a question to answer.

    The reply model is asked to close on the turn's next step; when it did not
    - or the reply is a fixed sentence (a failure, a fallback) - the next step's
    own question is added, and failing that a plain "what next?". Digit-free,
    so nothing added here can trip the number check (CLAUDE.md 10.2).
    """
    # A turn with its own question - a question card, a room question, cards of
    # what goes with a pick - already leaves them something to answer on screen.
    if already_asks(result):
        return response
    as_written = response
    if closes_on_a_question(response.message, response.follow_up_question):
        if response.choices or result.next_step is None:
            return response
        # Its own question with no answers to tap, beside a next step: the
        # chips shown are the step's, so the question must be too - the
        # reply's is swapped for it rather than left under answers to
        # something else ("How many will sit?" over "Show me more").
        as_written, response = response, response.model_copy(
            update={
                "message": _without_closing_question(response.message),
                "follow_up_question": None,
            }
        )
        if as_written.message.rstrip().endswith(
            in_language(result.next_step.question, language)
        ):
            return as_written
    question = in_language(
        result.next_step.question if result.next_step is not None else ANY_NEXT_STEP, language
    )
    message = f"{response.message.rstrip()} {question}".lstrip()
    if len(message) > MAX_RESPONSE_CHARS:
        return as_written
    logger.info(
        "reply_question_added",
        next_step=str(result.next_step.kind) if result.next_step else None,
        replaced_own_question=response is not as_written,
    )
    return response.model_copy(update={"message": message})


_CLOSING_QUESTION = re.compile(r"(?<=[.!?\u061f])\s+[^.!?\u061f]*[?\u061f][\"'\u201d\u00bb)]*\s*$")


def _without_closing_question(message: str) -> str:
    """The message without its last sentence, when that sentence asks."""
    if (match := _CLOSING_QUESTION.search(message)) is not None:
        return message[: match.start()]
    # The whole message is one question: nothing of it is left.
    return "" if closes_on_a_question(message) else message


def _asked_once(response: CustomerResponse) -> CustomerResponse:
    """One question, in one place.

    The two fields are one utterance to the customer: a client renders the
    prose and then the follow-up, often as something to tap. When the model
    writes the question into both, the customer is asked the same thing twice
    in a row (M20 2).

    The follow-up field is where an optional question belongs, so the
    duplicate is removed from the prose rather than the other way round -
    dropping the field would cost a client its chip.

    Removed only on an exact match of the closing sentence, compared with
    whitespace normalised. A paraphrase is left alone: this trims a repetition
    it can prove, and never edits prose it is guessing about. A message that is
    *only* the question keeps it, because prose with nothing left is worse than
    prose that repeats.
    """
    question = response.follow_up_question
    if question is None:
        return response
    message = " ".join(response.message.split())
    asked = " ".join(question.split())
    if message == asked or not message.endswith(asked):
        return response
    trimmed = message[: -len(asked)].strip()
    if not trimmed:
        return response
    return response.model_copy(update={"message": trimmed})


def _their_own_words(turn: CustomerTurnInput) -> tuple[str, ...]:
    """Everything the customer themselves has said in this conversation.

    Their turns only. An assistant message could carry a figure the model
    produced, and admitting it would let an invented number become an approved
    source one turn later - the exact laundering the guard exists to stop.
    """
    return tuple(
        message.content
        for message in turn.conversation.messages
        if message.role is ConversationRole.USER
    )


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

    def __init__(self, client: StructuredLLMClient, *, arabic_replies: bool = False) -> None:
        self._client = client
        self._arabic_replies = arabic_replies
        """Off, every reply is written with the English instructions, exactly
        as before, whatever a session stored while it was on."""
        # Built once: a prompt is chosen per turn, never assembled per turn.
        self._instructions = {language: build_instructions(language) for language in ReplyLanguage}
        self._correction_instructions = {
            language: build_correction_instructions(language) for language in ReplyLanguage
        }

    def _language(self, result: CustomerTurnResult) -> ReplyLanguage:
        """The language this reply is written in.

        The turn settled it (`result.reply_language`). A comparison pop-up is a
        look rather than a turn and settles nothing, so it answers in the
        session's stored language.
        """
        if not self._arabic_replies:
            return ReplyLanguage.EN
        return result.reply_language or result.state.reply_language or ReplyLanguage.EN

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
        language = self._language(result)

        response, calls, used_fallback = await self._primary_response(
            turn, result, route, language
        )
        final = _ends_on_a_question(
            self._with_side_notice(_asked_once(response), route, language), result, language
        )

        self._log(route, calls, used_fallback, started, language)
        return final

    # ── the branch that decides whether a model is involved ─────────────────

    async def _primary_response(
        self,
        turn: CustomerTurnInput,
        result: CustomerTurnResult,
        route: ResponseRoute,
        language: ReplyLanguage,
    ) -> tuple[CustomerResponse, int, bool]:
        if result.next_step is not None and result.next_step.kind in (
            NextStepKind.CHOOSE_PIECE,
            NextStepKind.CHOOSE_ROOM,
        ):
            # The application supplies both this question and its stocked choices.
            return _say(result.next_step.question, language), 0, False
        primary = route.primary
        if isinstance(primary, DeterministicResponse):
            return await self._deterministic(turn, result, route, primary, language)
        return await self._generated(turn, result, route, primary, language)

    async def _deterministic(
        self,
        turn: CustomerTurnInput,
        result: CustomerTurnResult,
        route: ResponseRoute,
        primary: DeterministicResponse,
        language: ReplyLanguage,
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
                    return _say(DETERMINISTIC_FALLBACK[primary.kind], language), 0, True
                # The decision model wrote this question and it was validated
                # in its own phase. Re-wording it could only change what was
                # asked, so it is carried through untouched. Its answers are
                # tappable and become the customer's own words, so each must
                # carry only figures the customer has stated; one that does not
                # is left off rather than shown (CLAUDE.md 14).
                return CustomerResponse(
                    message=clarification.question,
                    choices=_sourced_choices(clarification.choices, turn),
                ), 0, False

            case DeterministicResponseKind.HANDLED_FAILURE:
                assert primary.failure_code is not None
                return _say(FAILURE_WORDING[primary.failure_code], language), 0, False

            case DeterministicResponseKind.DESIGN_HANDOFF:
                if route.required_clarification is None:
                    return _say(DESIGN_HANDOFF_WORDING, language), 0, False
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
                    language=language,
                )
                return (
                    CustomerResponse(
                        message=compose(
                            in_language(DESIGN_HANDOFF_WORDING, language), question.message
                        ),
                        follow_up_question=question.follow_up_question,
                        choices=question.choices,
                    ),
                    calls,
                    used_fallback,
                )

            case DeterministicResponseKind.BUNDLE_CHANGED_NOT_REFRESHED:
                return _say(BUNDLE_CHANGED_NOT_REFRESHED_WORDING, language), 0, False

            case DeterministicResponseKind.BUNDLE_KEPT:
                return _say(BUNDLE_KEPT_WORDING, language), 0, False

            case DeterministicResponseKind.BUNDLE_ACQUISITION_SET:
                # No model: the customer stated this, and a sentence that
                # re-described it could only get it wrong.
                assert primary.acquisition is not None
                return _say(BUNDLE_ACQUISITION_WORDING[primary.acquisition], language), 0, False

            case DeterministicResponseKind.BUNDLE_UNLOCKED:
                return _say(BUNDLE_UNLOCKED_WORDING, language), 0, False

            case DeterministicResponseKind.BUNDLE_UNAVAILABLE:
                # The optimiser said exactly why it could not compute. A model
                # asked to explain that would start proposing remedies nobody
                # authorised - dropping a lock, changing a budget.
                assert primary.bundle_reason is not None
                return _say(BUNDLE_UNAVAILABLE_WORDING[primary.bundle_reason], language), 0, False

            case DeterministicResponseKind.FINISHING_TOUCH_OFFER:
                # The pieces are the chips beside it; nothing was planned, so
                # the question is the whole reply.
                return _say(FINISHING_TOUCH_OFFER_WORDING, language), 0, False

    async def _generated(
        self,
        turn: CustomerTurnInput,
        result: CustomerTurnResult,
        route: ResponseRoute,
        view: ResponseGroundingView,
        language: ReplyLanguage,
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
            said_earlier=_their_own_words(turn),
            presented_count=max(view.presented_count, len(view.still_on_screen)),
            compared_count=view.compared_count,
            counts=(
                *_view_counts(view),
                # What each requirement set aside would find, when nothing met
                # them all - counted by the application, so sayable.
                *(option.eligible_count for option in view.would_find_without),
                # How many of the kind they asked for meet their request, and
                # how many cards of each kind are on screen.
                *(_type_mix_counts(view.type_mix, view.presented_count) if view.type_mix else ()),
                *(chosen_seating_counts(view.chosen_seating) if view.chosen_seating else ()),
                *((view.liked_also_picked,) if view.liked_also_picked else ()),
                # How many picks they have, and how many of the newest pick's
                # kind - the tray shows them, so "your 2 sofa sets" is a count
                # they can read, not one we invented (CLAUDE.md 10.2).
                *picks_counts(result.picks),
            ),
            # Figures the customer can read off the cards beside the reply.
            # Repeating one is reporting what is on screen; the guard still
            # refuses anything that had to be computed (CLAUDE.md 14).
            figures=(
                *screen_figures(view.screen),
                *screen_figures(CustomerVisibleScreenView(products=view.still_on_screen)),
                # The space they gave and the width the designer aims for in
                # it - worked out by code from the designer's proportion.
                *((view.space_fit.space_cm, view.space_fit.ideal_cm) if view.space_fit else ()),
                # What a room kept from shopping: their own head count and wall.
                *(
                    v
                    for v in (
                        (view.room_carried.seats, view.room_carried.wall)
                        if view.room_carried
                        else ()
                    )
                    if v is not None
                ),
                # The picks tray is on screen too: their prices and the numbers
                # in their names ("6 Seater") are theirs to read and ours to say.
                *picks_figures(result.picks),
                # The lowest real total of each shape a seating question offers.
                *(seating_figures(view.seating) if view.seating else ()),
                # The lowest real total that would fill a piece the budget
                # could not reach - the next step a partial room offers.
                *(
                    piece.cheapest_price
                    for piece in (view.bundle.missing_pieces if view.bundle else ())
                    if piece.cheapest_price is not None
                ),
                # The nearest real price to a budget nothing met.
                *(o.nearest_price for o in view.would_find_without if o.nearest_price is not None),
                # An over-budget swap's figures: the new total, the budget it
                # broke and the overage - all computed by the swap, so sayable.
                *(swap_offer_figures(view.swap_offer) if view.swap_offer else ()),
                # A just-confirmed stretch: the original budget and how far over.
                *(bundle_stretch_figures(view.bundle) if view.bundle else ()),
                # Their own budget and seat figures, as this turn read them -
                # "three seater", "لستة أشخاص" said in words, sayable in digits.
                *result.stated_figures,
                # Rules of thumb the specialist supplied as structured
                # measurements. Sayable as guidance about rooms in general,
                # never as a fact about a product (CLAUDE.md 14, 41).
                *guidance_figures(view.guidance),
            ),
        )
        refs = valid_grounding_refs(result.grounding)

        response, calls = await self._call_and_validate(
            request, allowance=allowance, refs=refs, language=language
        )
        if response is None:
            return _say(_fallback(view), language), calls, True
        # A reply that asks its own question - "shall I show you the cheapest?"
        # - keeps it: the summary called for it, and its own choices answer it
        # (`asks_its_own_question`). Only a reply that asks nothing gets the next
        # step's question and chips appended.
        return response, calls, False

    async def _generated_message(
        self,
        turn: CustomerTurnInput,
        result: CustomerTurnResult,
        view: ResponseGroundingView,
        *,
        follow_up_allowed: bool,
        language: ReplyLanguage,
    ) -> tuple[CustomerResponse, int, bool]:
        """A question with its answers, for a branch adding an acknowledgement."""
        request = ResponseInput(
            message=turn.message,
            conversation=turn.conversation,
            grounding=view,
            follow_up_allowed=follow_up_allowed,
        )
        response, calls = await self._call_and_validate(
            request,
            allowance=build_allowance(turn.message, said_earlier=_their_own_words(turn)),
            # Nothing is grounded on this branch, so nothing may be cited.
            refs=frozenset(),
            language=language,
        )
        if response is None:
            return _say(_fallback(view), language), calls, True
        return response, calls, False

    # ── the call, and the one retry it may earn ─────────────────────────────

    async def _call_and_validate(
        self,
        request: ResponseInput,
        *,
        allowance: frozenset[str],
        refs: frozenset[int],
        language: ReplyLanguage,
    ) -> tuple[CustomerResponse | None, int]:
        """At most two calls, and the second only for an unsupported figure.

        `None` means fall back. Every failure that is not a numeric one returns
        it immediately: a bad citation or a question the turn did not permit is
        a misunderstanding, not a slip of phrasing.
        """
        first = await self._parse(self._instructions[language], request)
        if first is None:
            return None, 1

        violation = validate_response(
            first,
            valid_grounding_refs=refs,
            follow_up_allowed=request.follow_up_allowed,
            allowance=allowance,
            brief=request.grounding.brief,
        )
        if violation is None:
            return first, 1
        if not violation.permits_correction:
            self._log_violation(violation, attempt=1)
            return None, 1

        self._log_violation(violation, attempt=1)
        second = await self._parse(self._correction_instructions[language], request)
        if second is None:
            return None, 2

        repeated = validate_response(
            second,
            valid_grounding_refs=refs,
            follow_up_allowed=request.follow_up_allowed,
            allowance=allowance,
            brief=request.grounding.brief,
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
        self, response: CustomerResponse, route: ResponseRoute, language: ReplyLanguage
    ) -> CustomerResponse:
        """The failed side effect, appended by the application.

        Never generated: the model is not told a side effect failed, because a
        model told about a failure starts explaining it, and there is nothing
        to explain that a fixed sentence does not say better.
        """
        if route.side_notice is None:
            return response
        notice = in_language(SIDE_NOTICE_WORDING[route.side_notice], language)
        try:
            return CustomerResponse(
                message=compose(response.message, notice),
                referenced_grounding_refs=response.referenced_grounding_refs,
                follow_up_question=response.follow_up_question,
                choices=response.choices,
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

    def _log(
        self,
        route: ResponseRoute,
        calls: int,
        used_fallback: bool,
        started: float,
        language: ReplyLanguage,
    ) -> None:
        """Shape of the turn only: no message, no history, no prose, no facts."""
        primary = route.primary
        logger.info(
            "customer_response_completed",
            prompt_version=version_for(language),
            reply_language=str(language),
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


def _type_mix_counts(mix: TypeMixView, presented: int) -> tuple[int, ...]:
    """The one count a search covering several kinds licenses: how many of
    the asked kind meet the request, when that is fewer than the cards on
    screen - "I have only one sofa that seats 5". Any other figure about a
    kind would describe the page, and read as the shop's stock."""
    matches = mix.asked_kind_matches
    if matches is None or matches >= presented:
        return ()
    return (matches,)


def _view_counts(view: ResponseGroundingView) -> tuple[int, ...]:
    """The counts this outcome licenses in prose, from whichever shape carries them.

    A whole room and a seating combination each have their own count set, listed
    field by field rather than swept from the view, so a numeric field added
    later cannot silently widen what the model may assert. A view carries at
    most one of the two.
    """
    if view.bundle is not None:
        return bundle_counts(view.bundle)
    if view.seating is not None:
        return seating_counts(view.seating)
    if view.room_question is not None:
        question = view.room_question
        return tuple(
            n
            for n in (
                question.earlier_seat_count,
                question.picked_seat_count,
                question.picked_pieces,
                question.pieces_offered,
                question.pieces_preselected,
            )
            if n
        )
    return ()


def _sourced_choices(
    choices: tuple[TextReplyChoice, ...], turn: CustomerTurnInput
) -> tuple[TextReplyChoice, ...]:
    """The question's answers, when every figure in them has a source - their
    own words, or a position among the cards on screen ("the second one").
    One unsourced answer drops them all: the rest of an either/or would no
    longer offer the choice the question asks."""
    if not choices:
        return choices
    shown = turn.state.product_interaction
    allowance = build_allowance(
        turn.message,
        said_earlier=_their_own_words(turn),
        presented_count=len(shown.presented_product_ids),
        compared_count=len(shown.compared_product_ids),
    )
    for choice in choices:
        if check_numeric_policy(
            message=choice.label, follow_up_question=choice.value, allowance=allowance
        ):
            logger.warning("clarification_choices_unsourced", offered=len(choices))
            return ()
    return choices


def _reply(message: str) -> CustomerResponse:
    """An application-written reply: no citations, no optional question."""
    return CustomerResponse(message=message)


def _say(sentence: str, language: ReplyLanguage) -> CustomerResponse:
    """One of the application's own sentences, in the reply's language."""
    return _reply(in_language(sentence, language))


def _fallback(view: ResponseGroundingView) -> str:
    """This outcome's fixed sentence, with the status or outcome it needs."""
    return fallback_for(
        view.kind,
        view.bundle.status if view.bundle else None,
        view.seating.outcome if view.seating else None,
        view.room_question.question if view.room_question else None,
    )
