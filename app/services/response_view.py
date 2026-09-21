"""Routing one turn's outcome to whoever should word it.

Two questions, answered together because they have one answer: does this branch
call a model, and if so what may it see?

`TurnGrounding` is never serialised wholesale. It holds verified product facts
that the application renders and the model must not restate, so what crosses
the boundary is a projection carrying counts and reason codes only.

Pure and total. No I/O, no clock, no model - the same grounding always routes
the same way.
"""

from __future__ import annotations

from app.schemas.agent_decision import (
    AgentAction,
    FollowUpPolicy,
    ProductInteractionOp,
)
from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
from app.schemas.comparison import ComparisonStatus
from app.schemas.grounding import SearchOutcome
from app.schemas.resolution import DeterministicClarification
from app.schemas.response import (
    DeterministicResponse,
    DeterministicResponseKind,
    ResponseGroundingView,
    ResponseOutcomeKind,
    ResponseRoute,
    ResponseRouting,
    SideEffectNotice,
)


def route_response(result: CustomerTurnResult) -> ResponseRoute:
    """Who writes this turn's words, what they see, and what else to mention.

    Order follows what the customer actually asked for. A turn that produced
    results, a detail or a comparison is reported as that, even if an
    *optional* side effect also failed - the search is what they wanted, and
    answering only "unavailable" would throw it away.

    But the side effect is not forgotten either. `TurnGrounding` carries one
    `failure` field, and which layer it came from is recoverable: a *primary*
    failure leaves no primary grounding behind, so a failure arriving beside
    results, a detail or a comparison can only be the optional interaction's.
    That case keeps the primary outcome and adds a fixed notice; a failure with
    nothing beside it is the response-driving outcome, as before.

    The decision is read only for which interaction was attempted. Nothing
    about it reaches the model.
    """
    grounding = result.grounding
    primary = _primary_route(result)
    required = _required_clarification(result, primary)
    return ResponseRoute(
        primary=primary,
        required_clarification=required,
        side_notice=_side_notice(result),
        follow_up_allowed=(
            grounding.follow_up_policy is FollowUpPolicy.OPTIONAL and required is None
        ),
    )


def _required_clarification(
    result: CustomerTurnResult, primary: ResponseRouting
) -> DeterministicClarification | None:
    """The question this turn owes beside a primary outcome that succeeded.

    None when the clarification *is* the primary job - it is the route then,
    and carrying it twice would invite two questions out of one.
    """
    clarification = result.grounding.deterministic_clarification
    if clarification is None:
        return None
    if (
        isinstance(primary, ResponseGroundingView)
        and primary.kind is ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION
    ):
        return None
    return clarification


def _side_notice(result: CustomerTurnResult) -> SideEffectNotice | None:
    """The optional interaction that did not complete, if there was one.

    Only when something else succeeded: with no primary outcome the failure is
    already the whole answer, and a second notice would repeat it.
    """
    grounding = result.grounding
    interaction = result.decision.interaction
    if grounding.failure is None or interaction is None:
        return None
    if not _has_primary_outcome(result):
        return None
    return _NOTICE_FOR_OP[interaction.op]


def _has_primary_outcome(result: CustomerTurnResult) -> bool:
    """Whether something other than the failure actually succeeded.

    Grounding alone cannot answer this. An `ANSWER` turn produces no search,
    detail or comparison and is still a complete piece of work - so a failed
    selection beside it must not be reported as though the answer itself had
    failed. The action says which job was being done; nothing about the
    decision reaches the model.
    """
    grounding = result.grounding
    return (
        grounding.search is not None
        or grounding.product_detail is not None
        or grounding.comparison is not None
        or grounding.design_handoff_requested
        or result.decision.action is AgentAction.ANSWER
    )


def valid_grounding_refs(grounding: TurnGrounding) -> frozenset[int]:
    """The turn-local handles prose may cite.

    Taken from the grounded items themselves, never derived from a product id
    and never guessed from a count: a ref means something only because an
    application-rendered item carries it. A branch that grounded no product
    yields nothing, so any citation on it is unknown by construction.
    """
    refs: set[int] = set()
    if grounding.search is not None:
        refs |= {product.grounding_ref for product in grounding.search.products}
    if grounding.product_detail is not None:
        refs.add(grounding.product_detail.grounding_ref)
    if grounding.comparison is not None:
        refs |= {product.grounding_ref for product in grounding.comparison.products}
    return frozenset(refs)


def _primary_route(result: CustomerTurnResult) -> ResponseRouting:
    """The job this turn is mainly doing.

    A successful outcome is never displaced by a question about something
    else: a search that found products is a search, and the unresolved
    selection beside it rides along as `required_clarification`. Only a turn
    with nothing else to show routes to the clarification itself.
    """
    grounding = result.grounding
    clarification = grounding.deterministic_clarification

    if grounding.clarification is not None:
        # The decision model already wrote this question, and re-wording it
        # could only change what was asked.
        return DeterministicResponse(
            kind=DeterministicResponseKind.MODEL_CLARIFICATION
        )

    if grounding.failure is not None and not _has_primary_outcome(result):
        return DeterministicResponse(
            kind=DeterministicResponseKind.HANDLED_FAILURE,
            failure_code=grounding.failure.code,
        )

    if grounding.comparison is not None:
        return _comparison(grounding, clarification)
    if grounding.product_detail is not None:
        return _view(
            ResponseOutcomeKind.PRODUCT_DETAIL, clarification, presented_count=1
        )
    if grounding.search is not None:
        return _search(grounding, clarification)
    if grounding.design_handoff_requested:
        # Nothing ran, so there is nothing to frame. A clarification beside it
        # is worded on its own, from the route's `required_clarification`.
        return DeterministicResponse(kind=DeterministicResponseKind.DESIGN_HANDOFF)
    if clarification is not None and result.decision.action is not AgentAction.ANSWER:
        # No positive outcome to report: the question is the whole job.
        return _view(ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION, clarification)
    return _view(ResponseOutcomeKind.ANSWER, clarification)


def _view(
    kind: ResponseOutcomeKind,
    clarification: DeterministicClarification | None,
    **fields: object,
) -> ResponseGroundingView:
    """A view of one outcome, carrying any question it also owes.

    The reasons are copied from what the coordinator found, never derived here:
    the response layer reports a question, it does not decide there is one.
    """
    return ResponseGroundingView(
        kind=kind,
        clarification_reason=clarification.reason if clarification else None,
        reference_reason=clarification.reference_reason if clarification else None,
        relative_price_reason=(
            clarification.relative_price_reason if clarification else None
        ),
        **fields,
    )


_NOTICE_FOR_OP = {
    ProductInteractionOp.SELECT: SideEffectNotice.SELECTION_NOT_UPDATED,
    ProductInteractionOp.DESELECT: SideEffectNotice.SELECTION_NOT_REMOVED,
    ProductInteractionOp.FOCUS: SideEffectNotice.FOCUS_NOT_CHANGED,
}
"""Total over the interaction operations, so a new one cannot silently fall
through to no notice at all."""


def _search(
    grounding: TurnGrounding, clarification: DeterministicClarification | None
) -> ResponseGroundingView:
    """Counts and which axes moved. Never a figure, never a product."""
    search = grounding.search
    assert search is not None
    results = search.outcome is SearchOutcome.RESULTS
    return _view(
        (
            ResponseOutcomeKind.SEARCH_RESULTS
            if results
            else ResponseOutcomeKind.ZERO_RESULTS
        ),
        clarification,
        presented_count=search.presented_count,
        was_relaxed=search.was_relaxed,
        # The field that moved, not the bound it moved to: the application
        # renders "I widened your 5,000 to 5,500" from the real summary.
        relaxed_fields=tuple(
            dict.fromkeys(item.field for item in search.relaxations)
        ),
        dropped_roles=tuple(
            dict.fromkeys(
                dropped.role
                for dropped in search.dropped_constraints
                if dropped.role is not None
            )
        ),
    )


def _comparison(
    grounding: TurnGrounding, clarification: DeterministicClarification | None
) -> ResponseGroundingView:
    """Which fields differ, never by how much."""
    comparison = grounding.comparison
    assert comparison is not None
    return _view(
        ResponseOutcomeKind.COMPARISON,
        clarification,
        compared_count=len(comparison.products),
        comparison_differs_on=tuple(
            row.field
            for row in comparison.rows
            if row.status is ComparisonStatus.DIFFERENT
        ),
    )

