"""The room bundle as durable state: lines, not ids.

V1 stored two id tuples, which could not say how many of something the room
wanted, whether it was being bought, or whether the customer had asked to keep
it. V2 stores lines. Most of these tests defend the three properties that make
that safe:

* **ids are never reused** — a counter, not `max + 1`, so a stale reference
  stays dangling rather than quietly resolving to a different product;
* **the revision tracks content** — a no-op cannot make the bundle look edited;
* **a line holds no catalog fact** — PostgreSQL is product truth, and a
  remembered price is a wrong price.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_decision import CustomerStateProposal
from app.schemas.agent_state import (
    AGENT_STATE_VERSION,
    AgentStateV1,
    BundleItemState,
    BundleItemStatus,
    RoomProjectState,
)
from app.schemas.agent_updates import (
    AddBundleLine,
    AgentStateUpdate,
    BundleLineSpec,
    RemoveBundleLine,
    ReplaceBundle,
    RoomProjectUpdate,
    SetBundleLineAcquisition,
    SetBundleLineQuantity,
    SetBundleLineStatus,
)
from app.schemas.agent_view import RoomProjectView
from app.services.agent_state import apply_update
from app.services.agent_view import project_state
from app.services.design_facts import project_anchors
from pydantic import ValidationError

APP = Path(__file__).parents[2] / "app"


def spec(
    product_id: int,
    *,
    quantity: int = 1,
    acquisition: BundleAcquisition = BundleAcquisition.TO_BUY,
    status: BundleItemStatus = BundleItemStatus.SUGGESTED,
    need_id: int | None = None,
) -> BundleLineSpec:
    return BundleLineSpec(
        product_id=product_id,
        quantity=quantity,
        acquisition=acquisition,
        status=status,
        need_id=need_id,
    )


def update(*operations: object) -> AgentStateUpdate:
    return AgentStateUpdate(room_project=RoomProjectUpdate(bundle_operations=tuple(operations)))


def committed(*specs: BundleLineSpec) -> AgentStateV1:
    return apply_update(AgentStateV1(), update(ReplaceBundle(added=specs)))


def room(state: AgentStateV1) -> RoomProjectState:
    assert state.room_project is not None
    return state.room_project


# ── version ─────────────────────────────────────────────────────────────────


def test_the_state_contract_is_v3() -> None:
    assert AGENT_STATE_VERSION == "agent_state_v4"
    assert AgentStateV1().schema_version == "agent_state_v4"


def test_a_v1_payload_is_refused_rather_than_partially_read() -> None:
    """V1 named fields this shape no longer has. Reading one as V2 would drop a
    customer's bundle silently."""
    with pytest.raises(ValidationError):
        AgentStateV1.model_validate({"schema_version": "agent_state_v1"})

    with pytest.raises(ValidationError):
        RoomProjectState.model_validate({"bundle_product_ids": [1, 2], "locked_product_ids": [1]})


def test_no_migration_machinery_was_written() -> None:
    """The bump costs nothing because nothing persists state. Writing a
    migration for data that does not exist would be inventing a requirement."""
    source = (APP / "services/agent_state.py").read_text()

    for forbidden in ("migrate", "agent_state_v1", "upgrade", "legacy"):
        assert forbidden not in source, forbidden


# ── the line ────────────────────────────────────────────────────────────────


def test_a_line_carries_what_an_id_could_not() -> None:
    assert set(BundleItemState.model_fields) == {
        "line_id",
        "product_id",
        "quantity",
        "acquisition",
        "status",
        "need_id",
    }


def test_acquisition_is_required_on_a_line() -> None:
    """A lock proves preservation and says nothing about purchase."""
    with pytest.raises(ValidationError):
        BundleItemState(line_id=1, product_id=1, quantity=1, status=BundleItemStatus.SUGGESTED)  # type: ignore[call-arg]


def test_acquisition_is_required_on_a_spec_too() -> None:
    with pytest.raises(ValidationError):
        BundleLineSpec(product_id=1)  # type: ignore[call-arg]


def test_only_two_active_statuses_exist() -> None:
    """A third would behave like one of these: "accepted" either survives
    re-optimisation, and is `LOCKED`, or it does not, and is `SUGGESTED`."""
    assert {s.value for s in BundleItemStatus} == {"suggested", "locked"}
    for absent in ("ACCEPTED", "REJECTED", "REPLACED"):
        assert absent not in {s.name for s in BundleItemStatus}


@pytest.mark.parametrize(
    ("field", "value"),
    [("line_id", 0), ("product_id", 0), ("quantity", 0), ("need_id", 0)],
)
def test_a_line_refuses_an_impossible_value(field: str, value: int) -> None:
    base = {
        "line_id": 1,
        "product_id": 1,
        "quantity": 1,
        "acquisition": BundleAcquisition.TO_BUY,
        "status": BundleItemStatus.SUGGESTED,
    }
    with pytest.raises(ValidationError):
        BundleItemState(**{**base, field: value})


# ── the allocator ───────────────────────────────────────────────────────────


def test_the_first_line_takes_the_next_id() -> None:
    state = committed(spec(10))

    assert [i.line_id for i in room(state).bundle_items] == [1]
    assert room(state).next_bundle_line_id == 2


def test_each_line_takes_the_next_id_in_turn() -> None:
    state = committed(spec(10), spec(11), spec(12))

    assert [i.line_id for i in room(state).bundle_items] == [1, 2, 3]
    assert room(state).next_bundle_line_id == 4


def test_a_removed_id_is_never_handed_out_again() -> None:
    """`max(line_id) + 1` would reuse 3 here, and a stale reference to the
    removed line would then resolve to a different product."""
    state = committed(spec(10), spec(11), spec(12))
    state = apply_update(state, update(RemoveBundleLine(line_id=3)))
    state = apply_update(state, update(AddBundleLine(line=spec(13))))

    assert [i.line_id for i in room(state).bundle_items] == [1, 2, 4]
    assert room(state).next_bundle_line_id == 5


def test_the_counter_never_decreases_even_when_the_bundle_empties() -> None:
    state = committed(spec(10), spec(11))
    state = apply_update(state, update(ReplaceBundle(added=())))

    assert room(state).bundle_items == ()
    assert room(state).next_bundle_line_id == 3


def test_a_commit_re_allocates_everything_it_does_not_preserve() -> None:
    """M12E-1 re-allocated every line on a commit. M12E-2 keeps that for
    suggestions - a re-plan may reconsider them - and exempts locks, which is
    what `preserved` is for."""
    state = committed(spec(10))
    state = apply_update(state, update(ReplaceBundle(added=(spec(10), spec(11)))))

    assert [i.line_id for i in room(state).bundle_items] == [2, 3]


# ── the revision ────────────────────────────────────────────────────────────


def _revision(state: AgentStateV1) -> int:
    return room(state).bundle_revision


@pytest.mark.parametrize(
    ("label", "operation"),
    [
        ("add", AddBundleLine(line=spec(11))),
        ("remove", RemoveBundleLine(line_id=1)),
        ("quantity", SetBundleLineQuantity(line_id=1, quantity=4)),
        (
            "acquisition",
            SetBundleLineAcquisition(line_id=1, acquisition=BundleAcquisition.ALREADY_OWNED),
        ),
        ("lock", SetBundleLineStatus(line_id=1, status=BundleItemStatus.LOCKED)),
    ],
)
def test_a_content_change_advances_the_revision(label: str, operation: object) -> None:
    state = committed(spec(10))
    before = _revision(state)

    after = apply_update(state, update(operation))

    assert _revision(after) == before + 1, label


def test_unlocking_advances_it_too() -> None:
    state = committed(spec(10, status=BundleItemStatus.LOCKED))
    before = _revision(state)

    after = apply_update(
        state, update(SetBundleLineStatus(line_id=1, status=BundleItemStatus.SUGGESTED))
    )

    assert _revision(after) == before + 1


def test_an_atomic_commit_advances_it_exactly_once() -> None:
    """Whatever it replaces: three lines out and three in is one change."""
    state = committed(spec(10), spec(11), spec(12))

    after = apply_update(state, update(ReplaceBundle(added=(spec(20), spec(21), spec(22)))))

    assert _revision(after) == _revision(state) + 1


def test_an_unrelated_state_change_leaves_the_revision_alone() -> None:
    state = committed(spec(10))

    after = apply_update(
        state, AgentStateUpdate(room_project=RoomProjectUpdate(room_type="bedroom"))
    )

    assert _revision(after) == _revision(state)
    assert room(after).room_type == "bedroom"


def test_an_operation_that_changes_nothing_does_not_advance_it() -> None:
    """Locking a locked line is not an edit, and a caller must not be able to
    make the bundle look changed by asking for a no-op."""
    state = committed(spec(10, status=BundleItemStatus.LOCKED))

    after = apply_update(
        state, update(SetBundleLineStatus(line_id=1, status=BundleItemStatus.LOCKED))
    )

    assert _revision(after) == _revision(state)


def test_editing_a_line_that_is_not_there_changes_nothing() -> None:
    state = committed(spec(10))

    after = apply_update(state, update(SetBundleLineQuantity(line_id=99, quantity=4)))

    assert _revision(after) == _revision(state)
    assert room(after).bundle_items == room(state).bundle_items


# ── the reducer is immutable ────────────────────────────────────────────────


def test_the_reducer_never_touches_the_state_it_was_given() -> None:
    state = committed(spec(10))
    before = room(state).bundle_items

    apply_update(state, update(RemoveBundleLine(line_id=1)))

    assert room(state).bundle_items == before


def test_a_committed_line_keeps_everything_it_was_given() -> None:
    state = committed(
        spec(
            10,
            quantity=4,
            acquisition=BundleAcquisition.ALREADY_OWNED,
            status=BundleItemStatus.LOCKED,
        )
    )
    line = room(state).bundle_items[0]

    assert (line.product_id, line.quantity, line.need_id) == (10, 4, None)
    assert line.acquisition is BundleAcquisition.ALREADY_OWNED
    assert line.status is BundleItemStatus.LOCKED


# ── the anchor path ─────────────────────────────────────────────────────────


def test_locked_lines_are_discoverable_for_anchor_preparation() -> None:
    """What M12E-2 will read to hydrate hard locks, without a second stored
    list to disagree with the lines."""
    state = committed(
        spec(10, status=BundleItemStatus.LOCKED),
        spec(11),
        spec(12, status=BundleItemStatus.LOCKED),
    )

    assert room(state).locked_product_ids == (10, 12)


def test_the_derived_view_is_not_a_stored_field() -> None:
    state = committed(spec(10, status=BundleItemStatus.LOCKED))

    assert "locked_product_ids" not in RoomProjectState.model_fields
    assert "locked_product_ids" not in state.model_dump()["room_project"]


def test_an_anchor_never_learns_a_line_or_product_id() -> None:
    """The design agent's safe boundary is unchanged by V2."""
    from app.schemas.design import AnchorProduct

    fields = set(AnchorProduct.model_fields)
    assert "line_id" not in fields
    assert "product_id" not in fields
    assert "acquisition" not in fields
    assert callable(project_anchors)


# ── the model-facing view ───────────────────────────────────────────────────


def test_the_view_counts_lines_without_naming_products() -> None:
    state = committed(
        spec(10, status=BundleItemStatus.LOCKED),
        spec(11, acquisition=BundleAcquisition.ALREADY_OWNED),
        spec(12),
    )

    view = project_state(state).room_project
    assert view is not None
    assert view.bundle_line_count == 3
    assert view.locked_line_count == 1
    assert view.already_owned_line_count == 1


def test_four_of_one_product_is_one_line_in_the_view() -> None:
    state = committed(spec(10, quantity=4))

    view = project_state(state).room_project
    assert view is not None
    assert view.bundle_line_count == 1


@pytest.mark.parametrize(
    "forbidden", ["product_id", "line_id", "price", "store", "rank", "bundle_items"]
)
def test_the_view_carries_no_identity_or_catalog_fact(forbidden: str) -> None:
    for field in RoomProjectView.model_fields:
        assert forbidden not in field, field


def test_a_count_cannot_exceed_the_bundle_it_summarises() -> None:
    with pytest.raises(ValidationError):
        RoomProjectView(bundle_line_count=1, already_owned_line_count=2)


# ── model authority ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    ["bundle", "line_id", "product_id", "quantity", "acquisition", "status"],
)
def test_the_model_cannot_author_bundle_membership(forbidden: str) -> None:
    """Unchanged from V1 in substance: the model states customer facts, and the
    application resolves a verified reference into a line."""
    for field in CustomerStateProposal.model_fields:
        assert forbidden not in field, field


def test_proposal_mapping_never_reaches_the_bundle() -> None:
    tree = ast.parse((APP / "services/proposal_mapping.py").read_text())
    names = {
        node.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg is not None
    }

    assert "bundle_operations" not in names


# ── nothing executes yet ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "service",
    [
        "BundleOptimizer",
        "DesignDiscoveryService",
        "InteriorDesignAgent",
        "CatalogCapabilityService",
    ],
)
def test_the_coordinator_now_runs_the_whole_room_path(service: str) -> None:
    """M12E-1 asserted the opposite: state only, handoff still a marker. M12E-2
    wired it, so the guard is inverted rather than deleted - the composition is
    worth pinning in the direction it now runs."""
    source = (APP / "services/turn_coordinator.py").read_text()

    assert service in source
