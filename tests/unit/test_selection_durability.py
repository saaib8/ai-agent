"""What the customer has chosen, and why it used to disappear.

Reconstructed from a real session. They searched sofas, compared two, chose
one, were offered rugs, chose a rug - and the session ended holding only the
sofa. Meanwhile the agent said "I've got the 5th option as your choice" and
later "you've selected a sofa and a rug", neither of which was true, and when
asked to see them answered that the cards were not shown in this turn.

Three defects, one section each:

1. a complement used the piece they settled on to ask the specialist and then
   discarded it, so the choice was never recorded;
2. the reply described selections from the conversation rather than from
   state, so it could report one that had not happened;
3. there was no way to put their choices back on screen.
"""

from __future__ import annotations

from typing import Any

from app.schemas.agent_decision import (
    AgentAction,
    CommercialReason,
    CustomerAgentDecision,
    DesignAnchorIntent,
    DesignScope,
)
from app.schemas.design import DesignCategoryNeed, DesignPriority, InteriorDesignResult
from app.schemas.grounding import TurnFailureCode
from app.schemas.product_reference import PresentedOrdinal
from app.schemas.resolution import ResolvedProductReference
from app.schemas.response import ResponseGroundingView, ResponseOutcomeKind
from app.services.response_view import route_response

from tests.unit.test_turn_coordinator import (
    FakeDesign,
    FakeHydration,
    FakeReferences,
    _coordinator,
    _state,
    _turn,
)

CHOSEN = 10


def _complement_decision(**kwargs: Any) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.DESIGN_HANDOFF,
        design_scope=DesignScope.COMPLEMENT,
        commercial_reason=CommercialReason.PURCHASE_PROGRESSION,
        design_anchor=DesignAnchorIntent(reference=PresentedOrdinal(position=1)),
        **kwargs,
    )


async def _complement_turn(
    *, design: Any = None, presented: tuple[int, ...] = (10, 11)
) -> tuple[Any, dict[str, Any]]:
    coordinator, parts = _coordinator(
        _complement_decision(),
        references=FakeReferences(default=ResolvedProductReference(product_id=CHOSEN)),
        hydration=FakeHydration(available=presented),
        design=design if design is not None else FakeDesign(InteriorDesignResult()),
    )
    return await coordinator.run(_turn(_state(presented=presented, selected=()))), parts


# ── 1. the piece they settled on is kept ────────────────────────────────────


async def test_settling_on_a_piece_records_it() -> None:
    """The defect: it was used to ask the specialist, then forgotten."""
    result, _ = await _complement_turn()

    assert CHOSEN in result.state.product_interaction.selected_product_ids
    assert result.state.product_interaction.focused_product_id == CHOSEN


async def test_it_is_kept_even_when_the_suggestion_finds_nothing() -> None:
    """Whether our idea works out is our business. Their choice is theirs.

    The specialist has a role to suggest and the search for it comes back
    empty, so the turn shows no cards - and the piece they settled on is still
    recorded (CLAUDE.md 51).
    """
    from tests.unit.test_design_qa_and_crosssell import RoleAwareDiscovery, ThinCatalogPipeline

    coordinator, _ = _coordinator(
        _complement_decision(),
        references=FakeReferences(default=ResolvedProductReference(product_id=CHOSEN)),
        hydration=FakeHydration(available=(10, 11)),
        design=FakeDesign(
            InteriorDesignResult(
                needs=(
                    DesignCategoryNeed(
                        commerce_category="decor",
                        commerce_subcategory="carpet",
                        priority=DesignPriority.RECOMMENDED,
                    ),
                )
            )
        ),
        design_discovery=RoleAwareDiscovery(),
        pipeline=ThinCatalogPipeline(stocked="nothing-at-all"),  # type: ignore[arg-type]
    )

    result = await coordinator.run(_turn(_state(presented=(10, 11), selected=())))

    assert result.grounding.search is None, "the suggestion found nothing"
    assert CHOSEN in result.state.product_interaction.selected_product_ids


async def test_it_is_kept_even_when_the_specialist_fails() -> None:
    from app.core.exceptions import LLMResponseInvalidError

    result, _ = await _complement_turn(
        design=FakeDesign(error=LLMResponseInvalidError(reason="bad"))
    )

    assert CHOSEN in result.state.product_interaction.selected_product_ids


async def test_choosing_the_same_piece_twice_is_one_choice() -> None:
    coordinator, _ = _coordinator(
        _complement_decision(),
        references=FakeReferences(default=ResolvedProductReference(product_id=CHOSEN)),
        hydration=FakeHydration(available=(10, 11)),
        design=FakeDesign(InteriorDesignResult()),
    )

    result = await coordinator.run(_turn(_state(presented=(10, 11), selected=(CHOSEN,))))

    assert result.state.product_interaction.selected_product_ids.count(CHOSEN) == 1


# ── 2. the reply reports what was recorded, not what was said ───────────────


async def test_a_turn_that_records_a_choice_says_so() -> None:
    result, _ = await _complement_turn()

    assert result.selection_added is True
    view = route_response(result).primary
    assert isinstance(view, ResponseGroundingView)
    assert view.selection_changed is True
    assert view.selected_count >= 1


async def test_a_turn_that_records_nothing_says_nothing() -> None:
    """The claim that was not true: "I've got that as your choice" on a turn
    where nothing was kept."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        hydration=FakeHydration(available=(10, 11)),
    )

    result = await coordinator.run(_turn(_state(presented=(10, 11), selected=())))
    view = route_response(result).primary

    assert result.selection_added is False
    assert isinstance(view, ResponseGroundingView)
    assert view.selection_changed is False
    assert view.selected_count == 0


async def test_the_count_survives_a_turn_that_changes_nothing() -> None:
    """"What have I chosen?" is answered from state, so it keeps working turns
    after the choice was made."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        hydration=FakeHydration(available=(10, 11)),
    )

    result = await coordinator.run(_turn(_state(presented=(10, 11), selected=(10, 11))))
    view = route_response(result).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.selected_count == 2
    assert view.selection_changed is False


def test_the_view_names_no_product_it_counts() -> None:
    view = ResponseGroundingView(
        kind=ResponseOutcomeKind.ANSWER, selected_count=2, selection_changed=True
    )

    rendered = view.model_dump_json()
    for forbidden in ("product_id", "selected_product_ids", "https://"):
        assert forbidden not in rendered, forbidden


# ── 3. their choices can be put back on screen ──────────────────────────────


async def _show(
    selected: tuple[int, ...], available: tuple[int, ...] | None = None
) -> tuple[Any, dict[str, Any]]:
    coordinator, parts = _coordinator(
        CustomerAgentDecision(action=AgentAction.SHOW_SELECTION),
        hydration=FakeHydration(available=selected if available is None else available),
    )
    return await coordinator.run(
        _turn(_state(presented=(10, 11), selected=selected))
    ), parts


async def test_showing_the_selection_presents_their_choices() -> None:
    """The gap: "show me what I've picked" answered that the cards were not
    shown in this turn."""
    result, _ = await _show((10, 11))

    assert result.grounding.selection is not None
    assert len(result.grounding.selection.products) == 2
    assert [p.presented_ordinal for p in result.grounding.selection.products] == [1, 2]
    assert result.grounding.search is None, "nothing was searched for"


async def test_it_runs_no_search() -> None:
    """There is no query here - only a list the customer already built."""
    result, parts = await _show((10,))

    assert parts["pipeline"].calls == []
    assert parts["m7"].messages == []


async def test_it_claims_no_search_provenance() -> None:
    """A product they chose has no relaxation depth. Claiming zero would
    describe a search that never ran."""
    result, _ = await _show((10,))

    assert result.grounding.selection is not None
    product = result.grounding.selection.products[0]
    assert product.relaxation_depth is None
    assert product.matched_exactly is None


async def test_it_does_not_renumber_what_they_were_browsing() -> None:
    """"The second one" still means the second of their search. A list they
    asked to review is not a new result set."""
    result, _ = await _show((10,))

    assert result.state.product_interaction.presented_product_ids == (10, 11)


async def test_nothing_chosen_is_an_honest_answer() -> None:
    result, _ = await _show(())

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.NOTHING_SELECTED


async def test_choices_the_catalog_lost_are_not_invented() -> None:
    result, _ = await _show((10,), available=())

    assert result.grounding.failure is not None
    assert result.grounding.failure.code is TurnFailureCode.PRODUCT_UNAVAILABLE


def test_the_empty_answer_reads_like_an_answer() -> None:
    from app.services.response_wording import FAILURE_WORDING

    wording = FAILURE_WORDING[TurnFailureCode.NOTHING_SELECTED]

    assert "haven't picked anything out yet" in wording
    assert "wasn't able" not in wording, "not a malfunction"


# ── the kinds must match what was actually chosen ───────────────────────────
#
# From session web-2c4aaeb983d0: a 2-seater loveseat from a comparison, then
# the first centre table. The reply said "you now have 2 sofas recorded" - the
# model had a count of two and a screen full of sofas, and no nouns of its own
# (M19 2). The same turn then offered more sofas, because the specialist was
# told about the table and nothing else (M19 1).


async def test_the_reply_is_given_the_kinds_not_just_the_count() -> None:
    result, _ = await _show((10, 11))
    view = route_response(result).primary

    assert isinstance(view, ResponseGroundingView)
    assert view.selected_count == 2
    assert len(view.selected_kinds) == 2
    assert all(kind for kind in view.selected_kinds)


def test_the_kinds_and_the_count_must_agree() -> None:
    """A partial list read as a whole one is how the wrong nouns get used."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="the two counts agree"):
        ResponseGroundingView(
            kind=ResponseOutcomeKind.ANSWER,
            selected_count=2,
            selected_kinds=("sofa",),
        )


def test_the_kinds_are_customer_words() -> None:
    from app.taxonomy.words import customer_words

    assert customer_words("center-table") == "center table"
    assert "-" not in customer_words("single-seater-sofa")


async def test_a_choice_the_catalog_lost_yields_no_kinds_at_all() -> None:
    """Rather than a short list that reads as the whole basket."""
    coordinator, _ = _coordinator(
        CustomerAgentDecision(action=AgentAction.ANSWER),
        hydration=FakeHydration(available=(10,)),
    )

    result = await coordinator.run(_turn(_state(presented=(10, 11), selected=(10, 11))))

    assert result.selected_kinds == ()


async def test_the_specialist_hears_about_everything_already_chosen() -> None:
    """The defect: a customer who had a sofa and then picked a table was
    offered sofas, because the table was the only anchor."""
    design = FakeDesign(InteriorDesignResult())
    coordinator, _ = _coordinator(
        _complement_decision(),
        references=FakeReferences(default=ResolvedProductReference(product_id=CHOSEN)),
        hydration=FakeHydration(available=(10, 11)),
        design=design,
    )

    await coordinator.run(_turn(_state(presented=(10, 11), selected=(11,))))

    anchors = design.requests[0].anchors
    assert len(anchors) == 2, "the piece in question, and what they already had"


async def test_the_piece_in_question_comes_first() -> None:
    design = FakeDesign(InteriorDesignResult())
    coordinator, _ = _coordinator(
        _complement_decision(),
        references=FakeReferences(default=ResolvedProductReference(product_id=CHOSEN)),
        hydration=FakeHydration(available=(10, 11)),
        design=design,
    )

    await coordinator.run(_turn(_state(presented=(10, 11), selected=(11,))))

    assert design.requests[0].anchors[0].locked is True


async def test_no_anchor_carries_an_identity() -> None:
    design = FakeDesign(InteriorDesignResult())
    coordinator, _ = _coordinator(
        _complement_decision(),
        references=FakeReferences(default=ResolvedProductReference(product_id=CHOSEN)),
        hydration=FakeHydration(available=(10, 11)),
        design=design,
    )

    await coordinator.run(_turn(_state(presented=(10, 11), selected=(11,))))

    rendered = design.requests[0].model_dump_json()
    for forbidden in ("product_id", "store_id", "https://"):
        assert forbidden not in rendered, forbidden
