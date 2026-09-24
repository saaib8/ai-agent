"""The deterministic, screen-driven search-follow-up path.

The coordinator wiring (`_apply_search_action`) is exercised end-to-end against
the live stack; these lock in the pure pieces a live test would not catch
cheaply: that the synthesised decision a search action stands in for is a valid
`SEARCH` with no payload (a payload-less search is deliberately allowed), that
the exclusion accumulator dedupes and stays bounded by construction — it is
written onto the request past the field validator, so nothing else backstops it
— and that the action union discriminates by `kind`.
"""

from __future__ import annotations

import pytest
from app.schemas.agent_decision import AgentAction, CommercialReason
from app.schemas.chat import ChatRequest
from app.schemas.discovery import MAX_EXCLUDED_PRODUCT_IDS
from app.schemas.search_action import (
    ExcludeProductAction,
    MoreOptionsAction,
    SearchActionRequest,
)
from app.services.turn_coordinator import _merge_exclusions, _search_action_decision
from pydantic import TypeAdapter, ValidationError


def test_a_search_action_stands_in_for_a_valid_payload_less_search() -> None:
    """A synthesised SEARCH must be valid without a new-search proposal: the
    query to run is the one already in state, not one carried on the decision."""
    decision = _search_action_decision()

    assert decision.action is AgentAction.SEARCH
    assert decision.commercial_reason is CommercialReason.CUSTOMER_REQUEST
    assert decision.new_search is None
    assert decision.reference is None


def test_the_action_union_discriminates_by_kind() -> None:
    adapter: TypeAdapter[SearchActionRequest] = TypeAdapter(SearchActionRequest)

    more = adapter.validate_python({"kind": "more_options"})
    exclude = adapter.validate_python({"kind": "exclude", "ordinal": 2})

    assert isinstance(more, MoreOptionsAction)
    assert isinstance(exclude, ExcludeProductAction)


def test_an_exclusion_names_a_positive_ordinal() -> None:
    with pytest.raises(ValidationError):
        ExcludeProductAction(ordinal=0)


# ── the exclusion accumulator ─────────────────────────────────────────────────


def test_merging_preserves_insertion_order_and_dedupes() -> None:
    """A product already excluded is not added twice, and order is kept so the
    bound below drops the oldest, not an arbitrary one."""
    assert _merge_exclusions((1, 2, 3), (3, 4, 2, 5)) == (1, 2, 3, 4, 5)


def test_merging_accumulates_monotonically() -> None:
    """Each round keeps what the last excluded and adds to it — which is what
    lets 'show me more' keep moving instead of circling the same products."""
    first = _merge_exclusions((), (10, 11, 12))
    second = _merge_exclusions(first, (13, 14))

    assert first == (10, 11, 12)
    assert set(first) <= set(second)
    assert second == (10, 11, 12, 13, 14)


def test_merging_stays_within_the_request_ceiling() -> None:
    """The result is written onto the request past its field validator, so the
    cap must hold here. When it is reached the oldest exclusions fall away."""
    current = tuple(range(MAX_EXCLUDED_PRODUCT_IDS))
    merged = _merge_exclusions(current, (MAX_EXCLUDED_PRODUCT_IDS, MAX_EXCLUDED_PRODUCT_IDS + 1))

    assert len(merged) == MAX_EXCLUDED_PRODUCT_IDS
    assert merged[-1] == MAX_EXCLUDED_PRODUCT_IDS + 1
    assert 0 not in merged  # the two oldest made room for the two newest
    assert 1 not in merged


# ── the transport contract ────────────────────────────────────────────────────


def test_a_turn_carries_at_most_one_structured_action() -> None:
    """A turn is either a room edit or a search follow-up. Both present would
    have one silently ignored, so the boundary rejects it."""
    with pytest.raises(ValidationError):
        ChatRequest(
            session_id="s1",
            store_id=50,
            message="not this one",
            bundle_action={"kind": "list_alternatives", "bundle_ordinal": 1},
            search_action={"kind": "more_options"},
        )


def test_a_search_action_alone_is_accepted() -> None:
    request = ChatRequest(
        session_id="s1",
        store_id=50,
        message="show me different options",
        search_action={"kind": "more_options"},
    )

    assert isinstance(request.search_action, MoreOptionsAction)
    assert request.bundle_action is None
