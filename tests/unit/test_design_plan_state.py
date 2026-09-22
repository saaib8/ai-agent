"""The furnishing plan as durable state.

Before this, a bundle line pointed at plan position 2 of an
`InteriorDesignResult` that ceased to exist when the turn ended, so on the next
turn "give me another lamp" had no way to find the lamp's role. The plan is now
persisted, and three properties make that safe:

* **a need id is never reused**, so a stale reference to a discarded role fails
  closed instead of binding to a new one;
* **a plan and the bundle chosen from it commit together**, so there is no state
  where new needs sit beside a room picked from different ones;
* **a line cannot name a need that is gone**, so refinement always has a target.

The plan is also not the customer's words. It is what the specialist concluded,
which is why it lives here and not in `design_preferences`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.core.exceptions import InvalidRequestError
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import (
    AGENT_STATE_VERSION,
    AgentStateV1,
    BundleItemStatus,
    RoomDesignNeedState,
    RoomProjectState,
)
from app.schemas.agent_updates import (
    AgentStateUpdate,
    DesignNeedSpec,
    PlannedBundleLineSpec,
    PreservedBundleLine,
    ReplaceDesignPlan,
    RoomProjectUpdate,
)
from app.schemas.agent_view import RoomProjectView
from app.schemas.design import DesignCategoryNeed, DesignPriority
from app.schemas.design_intent import MAX_DESIGN_INTENT_CHARS
from app.schemas.discovery import MAX_EXCLUDED_PRODUCT_IDS, SeatingCapacityConstraint
from app.services.agent_state import apply_update
from app.services.agent_view import project_state
from pydantic import ValidationError

APP = Path(__file__).parents[2] / "app"


def need_spec(
    category: str = "seating",
    subcategory: str | None = "sofa",
    *,
    priority: DesignPriority = DesignPriority.REQUIRED,
    quantity: int = 1,
    seats: SeatingCapacityConstraint | None = None,
    intent: str | None = None,
) -> DesignNeedSpec:
    return DesignNeedSpec(
        commerce_category=category,
        commerce_subcategory=subcategory,
        priority=priority,
        quantity=quantity,
        seating_capacity=seats,
        semantic_intent=intent,
    )


def line_spec(
    product_id: int, *, need_index: int | None = None, **kwargs: object
) -> PlannedBundleLineSpec:
    defaults: dict[str, object] = {
        "quantity": 1,
        "acquisition": BundleAcquisition.TO_BUY,
        "status": BundleItemStatus.SUGGESTED,
    }
    return PlannedBundleLineSpec(
        product_id=product_id, need_index=need_index, **{**defaults, **kwargs}
    )


def commit(
    *,
    needs: tuple[DesignNeedSpec, ...] = (),
    added: tuple[PlannedBundleLineSpec, ...] = (),
    preserved: tuple[int, ...] = (),
    state: AgentStateV1 | None = None,
) -> AgentStateV1:
    return apply_update(
        state or AgentStateV1(),
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                bundle_operations=(
                    ReplaceDesignPlan(
                        needs=needs,
                        preserved=tuple(PreservedBundleLine(line_id=i) for i in preserved),
                        added=added,
                    ),
                )
            )
        ),
    )


def room(state: AgentStateV1) -> RoomProjectState:
    project = state.room_project
    assert isinstance(project, RoomProjectState)
    return project


# ── version ═════════════════════════════════════════════════════════════════


def test_the_state_contract_is_v3() -> None:
    assert AGENT_STATE_VERSION == "agent_state_v5"
    assert AgentStateV1().schema_version == "agent_state_v5"


@pytest.mark.parametrize("older", ["agent_state_v1", "agent_state_v2"])
def test_an_older_payload_is_refused(older: str) -> None:
    with pytest.raises(ValidationError):
        AgentStateV1.model_validate({"schema_version": older})


def test_no_migration_machinery_was_written() -> None:
    source = (APP / "services/agent_state.py").read_text()

    for forbidden in ("migrate", "agent_state_v2", "upgrade", "legacy"):
        assert forbidden not in source, forbidden


# ── the need ════════════════════════════════════════════════════════════════


def test_a_need_carries_exactly_these_fields() -> None:
    assert set(RoomDesignNeedState.model_fields) == {
        "need_id",
        "commerce_category",
        "commerce_subcategory",
        "priority",
        "quantity",
        "seating_capacity",
        "semantic_intent",
        "rejected_product_ids",
    }


def test_a_need_is_frozen_and_forbids_extras() -> None:
    assert RoomDesignNeedState.model_config["frozen"] is True
    assert RoomDesignNeedState.model_config["extra"] == "forbid"


@pytest.mark.parametrize(
    "forbidden",
    ["price", "name", "url", "image", "store", "rank", "dimension", "color", "style"],
)
def test_a_need_holds_no_catalog_fact(forbidden: str) -> None:
    """The one product reference it does hold is an exclusion list, which is an
    application decision rather than a catalog value."""
    for field in RoomDesignNeedState.model_fields:
        assert forbidden not in field, field


def test_the_specialists_intent_rule_is_the_one_that_applies() -> None:
    """One implementation, so the plan cannot accept wording the need refused."""
    plan = RoomDesignNeedState(
        need_id=1,
        commerce_category="seating",
        priority=DesignPriority.REQUIRED,
        quantity=1,
        semantic_intent="  visually light  ",
    )
    assert plan.semantic_intent == "visually light"

    for hostile in ("under 2000 SAR", "seats 4"):
        with pytest.raises(ValidationError):
            RoomDesignNeedState(
                need_id=1,
                commerce_category="seating",
                priority=DesignPriority.REQUIRED,
                quantity=1,
                semantic_intent=hostile,
            )
        with pytest.raises(ValidationError):
            DesignCategoryNeed(
                commerce_category="seating",
                priority=DesignPriority.REQUIRED,
                semantic_intent=hostile,
            )


def test_both_intent_fields_share_one_bound() -> None:
    assert (
        RoomDesignNeedState.model_fields["semantic_intent"].metadata
        == DesignCategoryNeed.model_fields["semantic_intent"].metadata
    )
    assert MAX_DESIGN_INTENT_CHARS == 200


def test_the_intent_rule_is_defined_once() -> None:
    for module in ("schemas/design.py", "schemas/agent_state.py"):
        tree = ast.parse((APP / module).read_text())
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "normalise_design_intent" in imported, module


# ── rejections ══════════════════════════════════════════════════════════════


def _need_with(rejected: tuple[int, ...]) -> RoomDesignNeedState:
    return RoomDesignNeedState(
        need_id=1,
        commerce_category="seating",
        priority=DesignPriority.REQUIRED,
        quantity=1,
        rejected_product_ids=rejected,
    )


def test_rejections_default_to_none_and_keep_their_order() -> None:
    assert _need_with(()).rejected_product_ids == ()
    assert _need_with((7, 3, 9)).rejected_product_ids == (7, 3, 9)


@pytest.mark.parametrize(
    ("rejected", "why"),
    [((1, 1), "repeated"), ((0,), "not a product id"), ((-2,), "negative")],
)
def test_an_impossible_rejection_list_is_refused(rejected: tuple[int, ...], why: str) -> None:
    with pytest.raises(ValidationError):
        _need_with(rejected)
    assert why


def test_rejections_reuse_the_bound_a_search_already_applies() -> None:
    """One answer to "how many", not two."""
    assert _need_with(tuple(range(1, MAX_EXCLUDED_PRODUCT_IDS + 1)))

    with pytest.raises(ValidationError):
        _need_with(tuple(range(1, MAX_EXCLUDED_PRODUCT_IDS + 2)))


# ── the allocator ═══════════════════════════════════════════════════════════


def test_the_first_need_takes_the_first_id() -> None:
    state = commit(needs=(need_spec(),))

    assert [n.need_id for n in room(state).design_needs] == [1]
    assert room(state).next_design_need_id == 2


def test_needs_take_ids_in_plan_order() -> None:
    state = commit(needs=(need_spec("seating"), need_spec("tables", "console")))

    assert [n.commerce_category for n in room(state).design_needs] == [
        "seating",
        "tables",
    ]
    assert [n.need_id for n in room(state).design_needs] == [1, 2]


def test_a_replanned_room_gets_entirely_fresh_need_ids() -> None:
    """So a stale reference to a discarded role fails closed."""
    state = commit(needs=(need_spec(), need_spec("tables", "console")))
    state = commit(needs=(need_spec(),), state=state)

    assert [n.need_id for n in room(state).design_needs] == [3]
    assert room(state).next_design_need_id == 4


def test_the_need_counter_never_decreases() -> None:
    state = commit(needs=(need_spec(), need_spec()))
    state = commit(needs=(), state=state)

    assert room(state).design_needs == ()
    assert room(state).next_design_need_id == 3


def test_a_need_id_is_never_reused_after_the_plan_shrinks() -> None:
    state = commit(needs=(need_spec(), need_spec(), need_spec()))
    state = commit(needs=(need_spec(),), state=state)

    assert room(state).design_needs[0].need_id == 4


# ── referential integrity ═══════════════════════════════════════════════════


def test_a_line_takes_the_id_of_the_need_it_filled() -> None:
    state = commit(
        needs=(need_spec("seating"), need_spec("tables", "console")),
        added=(line_spec(10, need_index=1), line_spec(11, need_index=0)),
    )

    by_product = {i.product_id: i.need_id for i in room(state).bundle_items}
    assert by_product == {10: 2, 11: 1}


def test_a_line_belonging_to_no_need_keeps_none() -> None:
    state = commit(needs=(need_spec(),), added=(line_spec(10, need_index=None),))

    assert room(state).bundle_items[0].need_id is None


def test_a_line_pointing_outside_the_plan_is_an_invariant_violation() -> None:
    """Not something to drop quietly: it means the wrong plan was mapped."""
    with pytest.raises(InvalidRequestError):
        commit(needs=(need_spec(),), added=(line_spec(10, need_index=3),))


def test_a_dangling_need_reference_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError):
        RoomProjectState(
            design_needs=(),
            bundle_items=(
                {
                    "line_id": 1,
                    "product_id": 5,
                    "quantity": 1,
                    "acquisition": BundleAcquisition.TO_BUY,
                    "status": BundleItemStatus.SUGGESTED,
                    "need_id": 7,
                },
            ),
            next_bundle_line_id=2,
        )


# ── atomicity ═══════════════════════════════════════════════════════════════


def test_a_plan_and_its_room_land_together() -> None:
    state = commit(
        needs=(need_spec(), need_spec("tables", "console")),
        added=(line_spec(10, need_index=0), line_spec(11, need_index=1)),
    )

    assert len(room(state).design_needs) == 2
    assert len(room(state).bundle_items) == 2
    assert all(i.need_id is not None for i in room(state).bundle_items)


def test_both_allocators_advance_in_one_step() -> None:
    state = commit(needs=(need_spec(),), added=(line_spec(10, need_index=0),))

    assert room(state).next_design_need_id == 2
    assert room(state).next_bundle_line_id == 2


def test_a_rejected_commit_leaves_the_previous_plan_whole() -> None:
    """The bounds check fires before anything is written."""
    state = commit(needs=(need_spec("seating"),), added=(line_spec(10, need_index=0),))
    before = room(state)

    with pytest.raises(InvalidRequestError):
        commit(
            needs=(need_spec("tables", "console"),),
            added=(line_spec(11, need_index=5),),
            state=state,
        )

    assert room(state).design_needs == before.design_needs
    assert room(state).bundle_items == before.bundle_items


def test_committing_a_room_advances_the_bundle_revision_once() -> None:
    state = commit(needs=(need_spec(),), added=(line_spec(10, need_index=0), line_spec(11)))

    assert room(state).bundle_revision == 1


# ── locks across a plan replacement ═════════════════════════════════════════


def _with_lock() -> AgentStateV1:
    return commit(
        needs=(need_spec(),),
        added=(
            line_spec(
                10,
                need_index=0,
                status=BundleItemStatus.LOCKED,
                quantity=2,
                acquisition=BundleAcquisition.ALREADY_OWNED,
            ),
        ),
    )


def test_a_lock_survives_a_plan_replacement_with_its_identity() -> None:
    state = _with_lock()
    locked_id = room(state).bundle_items[0].line_id

    state = commit(needs=(need_spec("tables", "console"),), preserved=(locked_id,), state=state)

    kept = room(state).bundle_items[0]
    assert kept.line_id == locked_id
    assert kept.product_id == 10
    assert kept.quantity == 2
    assert kept.acquisition is BundleAcquisition.ALREADY_OWNED
    assert kept.status is BundleItemStatus.LOCKED


def test_a_lock_loses_its_old_role_rather_than_being_rehomed() -> None:
    """The role it belonged to no longer exists, and matching it onto one of
    the new roles would be a guess."""
    state = _with_lock()
    locked_id = room(state).bundle_items[0].line_id

    state = commit(needs=(need_spec(),), preserved=(locked_id,), state=state)

    assert room(state).bundle_items[0].need_id is None


def test_only_a_locked_line_may_be_preserved() -> None:
    state = commit(needs=(need_spec(),), added=(line_spec(10, need_index=0),))
    suggested = room(state).bundle_items[0].line_id

    with pytest.raises(InvalidRequestError):
        commit(needs=(need_spec(),), preserved=(suggested,), state=state)


def test_a_preserved_line_must_already_exist() -> None:
    with pytest.raises(InvalidRequestError):
        commit(needs=(need_spec(),), preserved=(99,))


# ── boundaries ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "forbidden",
    ["need_id", "rejected", "semantic_intent", "next_design"],
)
def test_the_model_facing_view_gained_nothing(forbidden: str) -> None:
    """Durable plan internals exist now; that is no reason to show them.

    M12E-4D dropped `design_need` from this list, because the view now carries
    the plan's roles deliberately - a model asked to name what the customer
    wants removed cannot do it from a plan it has never seen. What it may see
    is pinned below; everything here stays hidden.
    """
    for field in RoomProjectView.model_fields:
        assert forbidden not in field, field


def test_the_plan_the_model_sees_is_roles_and_nothing_else() -> None:
    """Two taxonomy fields, and deliberately not a third.

    Priority, quantity, capacity and design wording say what the room *should
    be*, which is the specialist's reasoning. Supplying them here would invite
    the commerce agent to compose the replacement itself (M12E-4D 2).
    """
    from app.schemas.agent_view import DesignNeedReferenceView

    assert set(DesignNeedReferenceView.model_fields) == {
        "commerce_category",
        "commerce_subcategory",
    }


def test_the_plan_reaches_the_view_in_its_own_order() -> None:
    state = commit(
        needs=(
            need_spec("lighting", "floor-lamp"),
            need_spec("seating", "sofa"),
        )
    )

    view = project_state(state).room_project
    assert view is not None
    assert [(need.commerce_category, need.commerce_subcategory) for need in view.design_needs] == [
        ("lighting", "floor-lamp"),
        ("seating", "sofa"),
    ]


def test_an_unplanned_room_shows_no_roles() -> None:
    """Nothing to refer back to, which reads correctly as an empty list."""
    view = project_state(AgentStateV1()).room_project
    assert view is None or view.design_needs == ()


def test_the_optimizer_knows_nothing_of_durable_ids() -> None:
    """M12D stays stateless: its need_index is an execution-local position."""
    from app.schemas.bundle import BundleLine, UnmetNeed

    assert "need_index" in BundleLine.model_fields
    assert "need_index" in UnmetNeed.model_fields
    assert "need_id" not in BundleLine.model_fields
    assert "need_id" not in UnmetNeed.model_fields

    source = (APP / "services/bundle_optimizer.py").read_text()
    for forbidden in ("need_id", "RoomDesignNeedState", "AgentState"):
        assert forbidden not in source, forbidden


def test_the_plan_is_not_a_customer_preference() -> None:
    """What the specialist concluded about one role, not what the customer
    said about themselves."""
    from app.schemas.agent_state import CustomerPreferenceState

    assert "semantic_intent" not in CustomerPreferenceState.model_fields
    assert "design_needs" not in CustomerPreferenceState.model_fields
    assert "semantic_intent" not in str(RoomProjectState.model_fields["design_preferences"])


def test_the_decision_model_cannot_author_a_plan() -> None:
    from app.schemas.agent_decision import CustomerStateProposal

    for forbidden in ("need", "design_needs", "plan", "rejected"):
        for field in CustomerStateProposal.model_fields:
            assert forbidden not in field, field
