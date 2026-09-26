"""A size belongs to the product type it was given for.

60 cm for a side table says nothing about a coffee table, so a measurement never
follows the customer to another type. Each type's sizes are saved after a
search the customer asked for, and come back when they return to that type -
unless they say size no longer matters.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    build_constrained_decision,
    to_plain_decision,
)
from app.schemas.agent_state import (
    ActiveSearchState,
    AgentStateV1,
    CustomerPreferenceState,
    SavedMeasurements,
)
from app.schemas.agent_updates import (
    ActiveSearchUpdate,
    AgentStateUpdate,
    CustomerPreferenceUpdate,
    ReplaceItems,
)
from app.schemas.composition import ComposedSearch
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PlanarDimensionConstraint,
    PriceConstraint,
    ProductSearchRequest,
)
from app.schemas.query import (
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    PlanarDimensionSemantics,
    ResolvedSearch,
)
from app.schemas.refinement import (
    DimensionRefinement,
    PriceRefinement,
    PriceRefinementOp,
    RefinementOp,
    SearchRefinementDelta,
)
from app.schemas.session import SessionEnvelope, new_session
from app.services.agent_state import apply_update, commit_search_results, remember_measurements
from app.services.refinement_composer import SearchRefinementComposer
from app.taxonomy.attributes import load_catalog_attributes
from app.taxonomy.dimensions import DimensionRole, load_dimension_semantics
from app.taxonomy.registry import load_taxonomy

from tests.unit.test_turn_coordinator import _coordinator, _turn

LOCKED = ConstraintStrength.LOCKED
WIDTH, DEPTH = DimensionRole.OVERALL_WIDTH, DimensionRole.DEPTH


@pytest.fixture(scope="module")
def composer() -> SearchRefinementComposer:
    taxonomy = load_taxonomy()
    return SearchRefinementComposer(
        load_catalog_attributes(), load_dimension_semantics(taxonomy=taxonomy)
    )


def _size(value: str, role: DimensionRole = WIDTH) -> DimensionConstraint:
    return DimensionConstraint(
        role=role,
        kind=DimensionConstraintKind.MAX,
        max_cm=Decimal(value),
        source_value=value,
        source_unit="cm",
    )


def _semantics(
    subcategory: str | None,
    dimensions: tuple[DimensionConstraint, ...] = (),
    planar: PlanarDimensionConstraint | None = None,
) -> ConstraintSemantics:
    return ConstraintSemantics(
        subcategory=LOCKED if subcategory else None,
        dimensions=tuple(
            DimensionConstraintSemantics(role=d.role, strength=LOCKED) for d in dimensions
        ),
        planar_dimension=PlanarDimensionSemantics(strength=LOCKED) if planar else None,
    )


def _resolved(
    category: str,
    subcategory: str | None,
    dimensions: tuple[DimensionConstraint, ...] = (),
    planar: PlanarDimensionConstraint | None = None,
) -> ResolvedSearch:
    return ResolvedSearch(
        request=ProductSearchRequest(
            commerce_category=category,
            commerce_subcategory=subcategory,
            dimensions=dimensions,
            planar_dimensions=planar,
        ),
        semantics=_semantics(subcategory, dimensions, planar),
    )


def _active(
    category: str, subcategory: str | None, dimensions: tuple[DimensionConstraint, ...] = ()
) -> ActiveSearchState:
    resolved = _resolved(category, subcategory, dimensions)
    return ActiveSearchState(request=resolved.request, semantics=resolved.semantics, revision=1)


def _saved(subcategory: str, *dimensions: DimensionConstraint) -> SavedMeasurements:
    return SavedMeasurements(
        commerce_subcategory=subcategory,
        dimensions=dimensions,
        dimension_semantics=tuple(
            DimensionConstraintSemantics(role=d.role, strength=LOCKED) for d in dimensions
        ),
    )


def _set_width(value: str) -> SearchRefinementDelta:
    return SearchRefinementDelta(
        dimensions=(
            DimensionRefinement(
                op=RefinementOp.SET,
                role=WIDTH,
                kind=DimensionConstraintKind.MAX,
                max_value=value,
                unit="cm",
            ),
        )
    )


def _executed(state: AgentStateV1, composed: ComposedSearch) -> AgentStateV1:
    """What `_run_search` does after a customer's search has run."""
    promoted = apply_update(
        state,
        AgentStateUpdate(
            active_search=ActiveSearchUpdate(
                request=composed.candidate.request,
                semantics=composed.candidate.semantics,
                semantic_preferences=ReplaceItems(items=composed.candidate.semantic_preferences),
            )
        ),
    )
    return remember_measurements(commit_search_results(promoted, (1, 2)))


def _widths(state: AgentStateV1) -> dict[str, list[Decimal | None]]:
    return {
        saved.commerce_subcategory: [d.max_cm for d in saved.dimensions]
        for saved in state.customer_preferences.measurements_by_type
    }


# ── a type change leaves sizes behind ───────────────────────────────────────


def test_a_size_never_follows_the_customer_to_another_type(
    composer: SearchRefinementComposer,
) -> None:
    """A side-table width once left 2 of 41 coffee tables."""
    result = composer.refine_taxonomy(
        _active("tables", "service-table", (_size("60"),)),
        commerce_category="tables",
        commerce_subcategory="center-table",
    )

    assert isinstance(result, ComposedSearch)
    assert result.candidate.request.dimensions == ()
    assert result.candidate.semantics.dimensions == ()
    # A coffee table could have taken a width, so there is no refusal reason.
    assert [(d.role, d.reason) for d in result.dropped_constraints] == [(WIDTH, None)]


def test_a_type_that_cannot_take_the_size_says_why(composer: SearchRefinementComposer) -> None:
    result = composer.refine_taxonomy(
        _active("seating", "sofa", (_size("200"),)),
        commerce_category="seating",
        commerce_subcategory="sofa-set",
    )

    assert isinstance(result, ComposedSearch)
    assert result.dropped_constraints[0].role is WIDTH
    assert result.dropped_constraints[0].reason is not None


def test_returning_to_a_type_brings_its_sizes_back(composer: SearchRefinementComposer) -> None:
    result = composer.refine_taxonomy(
        _active("seating", "chair"),
        commerce_category="seating",
        commerce_subcategory="sofa",
        saved_measurements=_saved("sofa", _size("200")),
    )

    assert isinstance(result, ComposedSearch)
    assert result.candidate.request.dimensions == (_size("200"),)
    assert [s.role for s in result.candidate.semantics.dimensions] == [WIDTH]
    assert result.earlier_sizes_applied is True


def test_a_size_stated_now_replaces_the_saved_one(composer: SearchRefinementComposer) -> None:
    result = composer.refine_taxonomy(
        _active("seating", "chair"),
        commerce_category="seating",
        commerce_subcategory="sofa",
        delta=_set_width("180"),
        saved_measurements=_saved("sofa", _size("200")),
    )

    assert isinstance(result, ComposedSearch)
    assert [d.max_cm for d in result.candidate.request.dimensions] == [Decimal("180")]
    assert result.earlier_sizes_applied is False


def test_a_size_restated_in_the_same_message_is_not_reported_as_dropped(
    composer: SearchRefinementComposer,
) -> None:
    """Reporting it would have the reply say the cards are not held to it."""
    result = composer.refine_taxonomy(
        _active("tables", "service-table", (_size("60"),)),
        commerce_category="tables",
        commerce_subcategory="center-table",
        delta=_set_width("100"),
    )

    assert isinstance(result, ComposedSearch)
    assert [d.max_cm for d in result.candidate.request.dimensions] == [Decimal("100")]
    assert result.dropped_constraints == ()


def test_a_saved_size_for_another_type_is_never_applied(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.refine_taxonomy(
        _active("seating", "chair"),
        commerce_category="seating",
        commerce_subcategory="sofa",
        saved_measurements=_saved("sectional-sofa", _size("300")),
    )

    assert isinstance(result, ComposedSearch)
    assert result.candidate.request.dimensions == ()
    assert result.earlier_sizes_applied is False


# ── a new search ────────────────────────────────────────────────────────────


def test_a_new_search_stating_no_size_gets_the_saved_one(
    composer: SearchRefinementComposer,
) -> None:
    result = composer.seed_new_task(
        _resolved("seating", "sofa"), saved_measurements=_saved("sofa", _size("200"))
    )

    assert result.resolved.request.dimensions == (_size("200"),)
    assert result.candidate.request.dimensions == (_size("200"),)
    assert result.earlier_sizes_applied is True


def test_a_new_search_stating_a_size_uses_exactly_that_size(
    composer: SearchRefinementComposer,
) -> None:
    """Not a blend: the saved width does not join the depth they just gave."""
    result = composer.seed_new_task(
        _resolved("seating", "sofa", (_size("90", DEPTH),)),
        saved_measurements=_saved("sofa", _size("200")),
    )

    assert [d.role for d in result.resolved.request.dimensions] == [DEPTH]
    assert result.earlier_sizes_applied is False


def test_a_saved_size_pair_comes_back_for_rugs(composer: SearchRefinementComposer) -> None:
    pair = PlanarDimensionConstraint(
        first_cm=Decimal("200"), second_cm=Decimal("300"), source_unit="cm"
    )
    saved = SavedMeasurements(
        commerce_subcategory="carpet",
        planar_dimensions=pair,
        planar_semantics=PlanarDimensionSemantics(strength=LOCKED),
    )

    result = composer.seed_new_task(_resolved("decor", "carpet"), saved_measurements=saved)

    assert result.resolved.request.planar_dimensions == pair
    assert result.earlier_sizes_applied is True


# ── what the session keeps ──────────────────────────────────────────────────


def test_an_executed_search_saves_its_sizes_against_its_type(
    composer: SearchRefinementComposer,
) -> None:
    state = _executed(
        AgentStateV1(), composer.seed_new_task(_resolved("seating", "sofa", (_size("200"),)))
    )
    state = _executed(
        state, composer.seed_new_task(_resolved("tables", "center-table", (_size("100"),)))
    )

    assert _widths(state) == {"sofa": [Decimal("200")], "center-table": [Decimal("100")]}


def test_a_search_with_no_size_forgets_that_type_only(
    composer: SearchRefinementComposer,
) -> None:
    """How "any size" sticks, without touching another type's sizes."""
    state = _executed(
        AgentStateV1(), composer.seed_new_task(_resolved("seating", "sofa", (_size("200"),)))
    )
    state = _executed(
        state, composer.seed_new_task(_resolved("tables", "center-table", (_size("100"),)))
    )
    state = _executed(state, composer.seed_new_task(_resolved("seating", "sofa")))

    assert _widths(state) == {"center-table": [Decimal("100")]}


def test_a_search_with_no_type_saves_nothing(composer: SearchRefinementComposer) -> None:
    state = _executed(AgentStateV1(), composer.seed_new_task(_resolved("tables", None)))

    assert state.customer_preferences.measurements_by_type == ()


def test_a_preference_update_keeps_the_saved_sizes(composer: SearchRefinementComposer) -> None:
    state = _executed(
        AgentStateV1(), composer.seed_new_task(_resolved("seating", "sofa", (_size("200"),)))
    )

    updated = apply_update(state, AgentStateUpdate(customer_preferences=CustomerPreferenceUpdate()))

    assert updated.customer_preferences.measurements_by_type == (
        state.customer_preferences.measurements_by_type
    )


def test_saved_sizes_hold_one_entry_per_type() -> None:
    with pytest.raises(ValueError, match="one entry per product type"):
        CustomerPreferenceState(
            measurements_by_type=(_saved("sofa", _size("200")), _saved("sofa", _size("180")))
        )


def test_a_saved_size_always_carries_its_strength() -> None:
    with pytest.raises(ValueError, match="recorded strength"):
        SavedMeasurements(commerce_subcategory="sofa", dimensions=(_size("200"),))


def test_an_empty_saved_entry_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one size"):
        SavedMeasurements(commerce_subcategory="sofa")


def test_a_session_saved_before_this_field_existed_still_loads(
    composer: SearchRefinementComposer,
) -> None:
    """The version did not move, so every live conversation must stay readable."""
    session = new_session()
    session = session.advanced(
        state=_executed(
            AgentStateV1(), composer.seed_new_task(_resolved("seating", "sofa", (_size("200"),)))
        ),
        conversation=session.conversation,
    )
    document = json.loads(session.model_dump_json())
    del document["state"]["customer_preferences"]["measurements_by_type"]

    loaded = SessionEnvelope.model_validate_json(json.dumps(document))

    assert loaded.state.customer_preferences.measurements_by_type == ()
    assert SessionEnvelope.model_validate_json(session.model_dump_json()) == session


# ── through the turn engine ─────────────────────────────────────────────────

SEARCH = CustomerAgentDecision(action=AgentAction.SEARCH)
RETYPE = CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, taxonomy_change_requested=True)
CHEAPER = SearchRefinementDelta(
    price=PriceRefinement(op=PriceRefinementOp.SET, max_amount="3000", currency="SAR")
)


async def _run(
    state: AgentStateV1,
    decision: CustomerAgentDecision,
    interpretation: ResolvedSearch | None = None,
) -> tuple[AgentStateV1, ProductSearchRequest]:
    coordinator, parts = _coordinator(decision, interpretation=interpretation)
    result = await coordinator.run(_turn(state, "x"))
    assert result.grounding.failure is None
    return result.state, parts["pipeline"].calls[-1].request


async def _sofa_then_chairs() -> AgentStateV1:
    state, _ = await _run(AgentStateV1(), SEARCH, _resolved("seating", "sofa", (_size("200"),)))
    state, executed = await _run(state, RETYPE, _resolved("seating", "chair"))
    assert executed.dimensions == ()
    return state


async def test_the_size_comes_back_on_the_way_back() -> None:
    state = await _sofa_then_chairs()

    state, executed = await _run(state, RETYPE, _resolved("seating", "sofa"))

    assert executed.dimensions == (_size("200"),)
    assert _widths(state)["sofa"] == [Decimal("200")]


async def test_any_size_on_the_way_back_drops_it() -> None:
    state = await _sofa_then_chairs()
    decision = RETYPE.model_copy(update={"drop_saved_sizes": True})

    state, executed = await _run(state, decision, _resolved("seating", "sofa"))

    assert executed.dimensions == ()
    assert "sofa" not in _widths(state)


async def test_any_size_on_a_new_search_drops_it() -> None:
    state = await _sofa_then_chairs()
    decision = SEARCH.model_copy(update={"drop_saved_sizes": True})

    state, executed = await _run(state, decision, _resolved("seating", "sofa"))

    assert executed.dimensions == ()
    assert "sofa" not in _widths(state)


async def test_a_plain_refinement_ignores_drop_saved_sizes() -> None:
    state, _ = await _run(AgentStateV1(), SEARCH, _resolved("seating", "sofa", (_size("200"),)))
    decision = CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH, refinement=CHEAPER, drop_saved_sizes=True
    )

    state, executed = await _run(state, decision)

    assert executed.dimensions == (_size("200"),)
    assert _widths(state)["sofa"] == [Decimal("200")]


async def test_a_price_refinement_after_a_sizeless_search_keeps_the_saved_size() -> None:
    """A similar-product search states no size; a later "cheaper" must not save
    that silence over the size the customer gave."""
    state, _ = await _run(AgentStateV1(), SEARCH, _resolved("seating", "sofa", (_size("200"),)))
    assert state.active_search is not None
    sizeless = state.model_copy(
        update={
            "active_search": ActiveSearchState(
                request=state.active_search.request.model_copy(update={"dimensions": ()}),
                semantics=state.active_search.semantics.model_copy(update={"dimensions": ()}),
                revision=state.active_search.revision,
            )
        }
    )
    decision = CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, refinement=CHEAPER)

    state, executed = await _run(sizeless, decision)

    assert executed.dimensions == ()
    assert _widths(state)["sofa"] == [Decimal("200")]


async def test_a_size_refinement_is_saved_and_a_clear_removes_it() -> None:
    state, _ = await _run(AgentStateV1(), SEARCH, _resolved("seating", "sofa", (_size("200"),)))
    narrower = CustomerAgentDecision(action=AgentAction.REFINE_SEARCH, refinement=_set_width("180"))
    state, _ = await _run(state, narrower)
    assert _widths(state)["sofa"] == [Decimal("180")]

    clear = CustomerAgentDecision(
        action=AgentAction.REFINE_SEARCH,
        refinement=SearchRefinementDelta(
            dimensions=(DimensionRefinement(op=RefinementOp.CLEAR, role=WIDTH),)
        ),
    )
    state, executed = await _run(state, clear)

    assert executed.dimensions == ()
    assert "sofa" not in _widths(state)


async def test_a_budget_survives_the_type_change_while_the_size_stays_behind() -> None:
    budget = ProductSearchRequest(
        commerce_category="seating",
        commerce_subcategory="sofa",
        price=PriceConstraint(currency="SAR", max_amount=Decimal("5000")),
        dimensions=(_size("200"),),
    )
    state, _ = await _run(
        AgentStateV1(),
        SEARCH,
        ResolvedSearch(
            request=budget,
            semantics=_semantics("sofa", (_size("200"),)).model_copy(update={"price_max": LOCKED}),
        ),
    )

    _, executed = await _run(state, RETYPE, _resolved("seating", "sectional-sofa"))

    assert executed.commerce_subcategory == "sectional-sofa"
    assert executed.price is not None and executed.price.max_amount == Decimal("5000")
    assert executed.dimensions == ()


# ── the decision contract ───────────────────────────────────────────────────


def test_drop_saved_sizes_reaches_the_provider_schema() -> None:
    constrained = build_constrained_decision(load_catalog_attributes())

    assert "drop_saved_sizes" in json.dumps(constrained.model_json_schema())


def test_an_empty_change_list_beside_a_type_change_is_accepted() -> None:
    """Unambiguous - it says nothing else changes. Refusing it cost a live turn."""
    constrained = build_constrained_decision(load_catalog_attributes())
    payload = {"action": "refine_search", "taxonomy_change_requested": True, "refinement": {}}

    decision = to_plain_decision(constrained.model_validate(payload))

    assert decision.taxonomy_change_requested is True


def test_an_empty_change_list_alone_is_still_refused() -> None:
    with pytest.raises(ValueError, match="changes nothing"):
        CustomerAgentDecision.model_validate({"action": "refine_search", "refinement": {}})
