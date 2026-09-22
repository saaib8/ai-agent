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

from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import (
    AgentAction,
    BundleInteractionOp,
    CommercialReason,
    FollowUpPolicy,
    ProductInteractionOp,
)
from app.schemas.agent_turn import CustomerTurnResult, TurnGrounding
from app.schemas.bundle import BundleStatus, BundleUnavailable, RoomBundle
from app.schemas.comparison import ComparisonStatus
from app.schemas.design import DesignPriority
from app.schemas.grounding import SearchOutcome
from app.schemas.resolution import DeterministicClarification
from app.schemas.response import (
    BundleGroundingView,
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

    A design handoff used to count here, back when asking *was* the whole
    outcome. Since M12E-2 it executes, so a handoff with a failure and no
    bundle is a room that did not get built - and the failure owns that turn
    rather than being hidden behind an acknowledgement.
    """
    grounding = result.grounding
    return (
        grounding.search is not None
        or grounding.product_detail is not None
        or grounding.comparison is not None
        or result.bundle_outcome is not None
        or result.bundle_change is not None
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
    lapsed = _suggestion_came_to_nothing(result)

    if grounding.clarification is not None:
        # The decision model already wrote this question, and re-wording it
        # could only change what was asked.
        return DeterministicResponse(kind=DeterministicResponseKind.MODEL_CLARIFICATION)

    if grounding.failure is not None and not _has_primary_outcome(result):
        return DeterministicResponse(
            kind=DeterministicResponseKind.HANDLED_FAILURE,
            failure_code=grounding.failure.code,
        )

    if grounding.comparison is not None:
        return _comparison(result, grounding, clarification)
    if grounding.product_detail is not None:
        return _view(
            ResponseOutcomeKind.PRODUCT_DETAIL,
            clarification,
            result=result,
            presented_count=1,
        )
    if grounding.search is not None and not lapsed:
        return _search(result, grounding, clarification)
    if result.bundle_change is not None and grounding.failure is not None:
        # The change happened and the refresh did not. Saying the change failed
        # would be false, and rolling it back to simplify the wording would
        # discard something the customer actually told us.
        return DeterministicResponse(kind=DeterministicResponseKind.BUNDLE_CHANGED_NOT_REFRESHED)
    if result.bundle_change is not None and result.bundle_outcome is None:
        # A local change: no room was chosen, so there is no package to frame
        # and nothing for a model to add that a fixed sentence does not say.
        #
        # Matched per operation rather than "lock, or else unlocked". That
        # shape told a customer who said "I already own the rug" that the piece
        # could change later - the opposite of the lock it had just been given
        # - because every operation that was not a lock borrowed the unlock
        # sentence.
        return _local_change(result, result.bundle_change)
    if isinstance(result.bundle_outcome, RoomBundle):
        # A room was selected - complete, partial or infeasible alike. Checked
        # before the handoff marker, which says only what was *asked for*:
        # letting request metadata answer for a finished room would tell the
        # customer nothing ran when something did.
        return _room_bundle(result, result.bundle_outcome, clarification)
    if isinstance(result.bundle_outcome, BundleUnavailable):
        # A deterministic refusal with its own reason. Not a failure, not a
        # catalog verdict, and not something to ask a model to explain.
        return DeterministicResponse(
            kind=DeterministicResponseKind.BUNDLE_UNAVAILABLE,
            bundle_reason=result.bundle_outcome.reason,
        )
    if grounding.design_handoff_requested and not lapsed:
        # Asked for, and nothing came of it. A clarification beside it is
        # worded on its own, from the route's `required_clarification`.
        return DeterministicResponse(kind=DeterministicResponseKind.DESIGN_HANDOFF)
    if clarification is not None and result.decision.action is not AgentAction.ANSWER:
        # No positive outcome to report: the question is the whole job.
        return _view(ResponseOutcomeKind.DETERMINISTIC_CLARIFICATION, clarification, result=result)
    return _view(ResponseOutcomeKind.ANSWER, clarification, result=result)


def _was_suggested(result: CustomerTurnResult) -> bool:
    """Whether this search was our idea rather than their request.

    A complementary piece runs the same pipeline as any other search, so by
    the time a reply is worded the two are indistinguishable without asking.
    `CommercialReason` is where that is already recorded - it exists to keep
    the motive apart from the capability, which is exactly the question here -
    so nothing new has to be invented to answer it.
    """
    return (
        result.grounding.design_handoff_requested
        and result.decision.commercial_reason is not CommercialReason.CUSTOMER_REQUEST
    )


def _suggestion_came_to_nothing(result: CustomerTurnResult) -> bool:
    """A piece we proposed, which this retailer turns out not to stock.

    Not reported, and this is the point rather than an omission. The customer
    asked for nothing here, so nothing failed and there is no result to give
    them - and a turn that says "I couldn't find any matching options" beside
    an empty screen is answering a question they never asked. The turn is
    whatever they actually did, which is what the rest of the grounding is
    about.

    Their *own* design question is a different thing and still answered: it
    carries `CUSTOMER_REQUEST`, so it never reaches here.
    """
    search = result.grounding.search
    return search is not None and not search.products and _was_suggested(result)


def _local_change(result: CustomerTurnResult, change: BundleInteractionOp) -> DeterministicResponse:
    """The fixed sentence for a room edit that chose no products.

    Exhaustive on purpose: an operation with no sentence of its own raises
    rather than borrowing another's, because borrowing is how the acquisition
    branch came to state the opposite of what it did.
    """
    match change:
        case BundleInteractionOp.LOCK:
            return DeterministicResponse(kind=DeterministicResponseKind.BUNDLE_KEPT)
        case BundleInteractionOp.UNLOCK:
            return DeterministicResponse(kind=DeterministicResponseKind.BUNDLE_UNLOCKED)
        case BundleInteractionOp.SET_ACQUISITION:
            interaction = result.decision.bundle_interaction
            assert interaction is not None, "an acquisition change carries its intent"
            assert interaction.acquisition is not None, "the contract requires one"
            return DeterministicResponse(
                kind=DeterministicResponseKind.BUNDLE_ACQUISITION_SET,
                acquisition=interaction.acquisition,
            )
        case _:
            # Replacing a product and removing a role both re-optimise, so
            # they arrive here only with an outcome or a failure. Reaching this
            # branch means one of them stopped producing either.
            raise AssertionError(f"no local wording for {change}")


def _room_bundle(
    result: CustomerTurnResult,
    bundle: RoomBundle,
    clarification: DeterministicClarification | None,
) -> ResponseGroundingView:
    """One selected room, as the response model may see it.

    Counts, enums and two bools. Everything the customer will actually read -
    the pieces, their prices, the total, the budget - is rendered by the
    application from the same verified bundle, so none of it passes through
    here (CLAUDE.md 20.4).

    `within_budget` reads the customer's own budget from state rather than from
    the bundle, which carries a spend and not a limit. An infeasible package is
    by definition outside it; anything else that was computed against a budget
    obeyed it, because the optimiser treats the ceiling as a hard constraint.
    """
    room = result.state.room_project
    budget = room.budget if room else None
    unmet = dict.fromkeys(DesignPriority, 0)
    for entry in bundle.unmet:
        unmet[entry.priority] += 1

    return ResponseGroundingView(
        kind=ResponseOutcomeKind.ROOM_BUNDLE,
        bundle=BundleGroundingView(
            status=bundle.status,
            bundle_line_count=len(bundle.lines),
            locked_line_count=sum(1 for line in bundle.lines if line.locked),
            already_owned_line_count=sum(
                1 for line in bundle.lines if line.acquisition is BundleAcquisition.ALREADY_OWNED
            ),
            required_unmet_count=unmet[DesignPriority.REQUIRED],
            recommended_unmet_count=unmet[DesignPriority.RECOMMENDED],
            optional_unmet_count=unmet[DesignPriority.OPTIONAL],
            unmet_reasons=tuple(dict.fromkeys(entry.reason for entry in bundle.unmet)),
            budget_supplied=budget is not None,
            within_budget=(
                None if budget is None else bundle.status is not BundleStatus.INFEASIBLE
            ),
            relaxed_line_count=sum(
                1
                for line in bundle.lines
                if line.relaxation_depth is not None and line.relaxation_depth > 0
            ),
        ),
        clarification_reason=clarification.reason if clarification else None,
        reference_reason=clarification.reference_reason if clarification else None,
        relative_price_reason=(clarification.relative_price_reason if clarification else None),
    )


def _view(
    kind: ResponseOutcomeKind,
    clarification: DeterministicClarification | None,
    *,
    result: CustomerTurnResult | None = None,
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
        relative_price_reason=(clarification.relative_price_reason if clarification else None),
        # Turn-wide facts, so no branch has to remember them: what the decision
        # step wants asked, and whether the room requirement is already on
        # record. Both exist to stop the reply asking twice.
        follow_up_goal=result.decision.follow_up_goal if result else None,
        seating_requirement_known=_seating_known(result),
        **fields,
    )


def _seating_known(result: CustomerTurnResult | None) -> bool:
    """Whether the customer has already said how many people use the room."""
    if result is None:
        return False
    room = result.state.room_project
    return room is not None and room.regular_seating_count is not None


_NOTICE_FOR_OP = {
    ProductInteractionOp.SELECT: SideEffectNotice.SELECTION_NOT_UPDATED,
    ProductInteractionOp.DESELECT: SideEffectNotice.SELECTION_NOT_REMOVED,
    ProductInteractionOp.FOCUS: SideEffectNotice.FOCUS_NOT_CHANGED,
}
"""Total over the interaction operations, so a new one cannot silently fall
through to no notice at all."""


def _search(
    result: CustomerTurnResult,
    grounding: TurnGrounding,
    clarification: DeterministicClarification | None,
) -> ResponseGroundingView:
    """Counts, which axes moved, and what kind of thing was searched for.

    The category comes from the search that actually executed - the request on
    `active_search` - rather than from the products it returned. That is the
    verified fact even when nothing came back, which is exactly when saying
    what was looked for matters most.
    """
    search = grounding.search
    assert search is not None
    results = search.outcome is SearchOutcome.RESULTS
    executed = result.state.active_search
    return _view(
        (ResponseOutcomeKind.SEARCH_RESULTS if results else ResponseOutcomeKind.ZERO_RESULTS),
        clarification,
        result=result,
        presented_count=search.presented_count,
        commerce_category=_words(executed.request.commerce_category if executed else None),
        commerce_subcategory=_words(executed.request.commerce_subcategory if executed else None),
        exact_match_count=search.exact_candidate_count,
        # A search reached through a design handoff is one we proposed: the
        # customer asked what would suit the piece they chose, or said nothing
        # about a second category at all. Every other search is their own
        # request, which is the difference between introducing a set and
        # reporting one.
        search_was_suggested=_was_suggested(result),
        was_relaxed=search.was_relaxed,
        # The field that moved, not the bound it moved to: the application
        # renders "I widened your 5,000 to 5,500" from the real summary.
        relaxed_fields=tuple(dict.fromkeys(item.field for item in search.relaxations)),
        dropped_roles=tuple(
            dict.fromkeys(
                dropped.role for dropped in search.dropped_constraints if dropped.role is not None
            )
        ),
    )


def _words(value: str | None) -> str | None:
    """A taxonomy key as the customer would say it.

    Mechanical and total - hyphens become spaces - so this is text
    normalisation rather than a second vocabulary to maintain beside the
    registry (CLAUDE.md 14.2). Nothing is renamed and no value is mapped to
    another, which is what keeps a display name from quietly becoming an alias.
    """
    return None if value is None else value.replace("-", " ")


def _comparison(
    result: CustomerTurnResult,
    grounding: TurnGrounding,
    clarification: DeterministicClarification | None,
) -> ResponseGroundingView:
    """Which fields differ, never by how much."""
    comparison = grounding.comparison
    assert comparison is not None
    return _view(
        ResponseOutcomeKind.COMPARISON,
        clarification,
        result=result,
        compared_count=len(comparison.products),
        comparison_differs_on=tuple(
            row.field for row in comparison.rows if row.status is ComparisonStatus.DIFFERENT
        ),
    )
