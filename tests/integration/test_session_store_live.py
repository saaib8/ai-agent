"""Session persistence against a real Redis.

The compare-and-set is the reason this file exists. A fake can be written to
return whatever the test wants; only a real server proves that `WATCH` aborts a
transaction when the key it is watching changes, and that is the single
mechanism preventing one request from overwriting a turn another already
committed.

The roundtrip matters for a different reason: `AgentStateV1` is full of
`Decimal`, enums and tuples, and JSON has none of those. A state that survives
serialisation as *something* but not as *itself* would quietly turn a price
into a float or a tuple into a list, and the first sign of it would be a
comparison failing somewhere far away.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from app.core.config import SessionSettings
from app.core.exceptions import (
    InvalidRequestError,
    SessionStateInvalidError,
)
from app.repositories.sessions import SessionStore, session_key
from app.schemas.acquisition import BundleAcquisition
from app.schemas.agent_state import AgentStateV1, BundleItemStatus
from app.schemas.agent_updates import (
    AgentStateUpdate,
    DesignNeedSpec,
    PlannedBundleLineSpec,
    ReplaceDesignPlan,
    RoomProjectUpdate,
)
from app.schemas.conversation import (
    ConversationContext,
    ConversationMessage,
    ConversationRole,
)
from app.schemas.design import DesignPriority
from app.schemas.discovery import PriceConstraint
from app.schemas.session import (
    SESSION_ENVELOPE_VERSION,
    SessionEnvelope,
    new_session,
)
from app.services.agent_state import apply_update
from redis.asyncio import Redis

from tests.conftest import INTEGRATION_REDIS_URL

pytestmark = pytest.mark.integration

STORE, OTHER_STORE = 7001, 7002
SESSION = "sess-abc.123"


async def _reachable(client: Redis) -> bool:
    try:
        await client.ping()
    except Exception:
        return False
    return True


@pytest_asyncio.fixture
async def client() -> AsyncIterator[Redis]:
    redis: Redis = Redis.from_url(
        INTEGRATION_REDIS_URL, socket_connect_timeout=2, decode_responses=True
    )
    if not await _reachable(redis):
        await redis.aclose()
        pytest.skip(f"no Redis at {INTEGRATION_REDIS_URL}")
    # A dedicated database, emptied around each test: these keys are this
    # suite's alone, and a leftover session would make a CAS test pass for the
    # wrong reason.
    await redis.flushdb()
    try:
        yield redis
    finally:
        await redis.flushdb()
        await redis.aclose()


@pytest.fixture
def store(client: Redis) -> SessionStore:
    return SessionStore(client, SessionSettings(ttl_s=60))


def a_room() -> AgentStateV1:
    """State with something of every awkward type in it."""
    return apply_update(
        AgentStateV1(),
        AgentStateUpdate(
            room_project=RoomProjectUpdate(
                room_type="living room",
                budget=PriceConstraint.at_most(Decimal("12000.50"), "SAR"),
                bundle_operations=(
                    ReplaceDesignPlan(
                        needs=(
                            DesignNeedSpec(
                                commerce_category="seating",
                                commerce_subcategory="sofa",
                                priority=DesignPriority.REQUIRED,
                                quantity=2,
                                semantic_intent="low and soft",
                            ),
                            DesignNeedSpec(
                                commerce_category="decor",
                                commerce_subcategory="carpet",
                                priority=DesignPriority.OPTIONAL,
                                quantity=1,
                            ),
                        ),
                        added=(
                            PlannedBundleLineSpec(
                                product_id=10,
                                quantity=2,
                                acquisition=BundleAcquisition.ALREADY_OWNED,
                                status=BundleItemStatus.LOCKED,
                                need_index=0,
                            ),
                        ),
                    ),
                ),
            )
        ),
    )


def an_envelope(revision: int = 0) -> SessionEnvelope:
    return SessionEnvelope(
        envelope_version=SESSION_ENVELOPE_VERSION,
        session_revision=revision,
        state=a_room(),
        conversation=ConversationContext(
            messages=(
                ConversationMessage(role=ConversationRole.USER, content="furnish my living room"),
                ConversationMessage(role=ConversationRole.ASSISTANT, content="Here is a room."),
            )
        ),
    )


# ── roundtrip ═══════════════════════════════════════════════════════════════


async def test_a_session_survives_redis_exactly(store: SessionStore) -> None:
    """Equality, not "looks similar": Decimal, enums and tuples all included."""
    original = an_envelope()

    assert await store.save_if_revision(
        STORE, SESSION, expected_revision=0, envelope=an_envelope(1)
    )
    loaded = await store.load(STORE, SESSION)

    assert loaded is not None
    assert loaded.state == original.state
    assert loaded.conversation == original.conversation
    assert loaded.session_revision == 1

    room = loaded.state.room_project
    assert room is not None
    assert room.budget is not None
    assert isinstance(room.budget.max_amount, Decimal), "not a float after JSON"
    assert room.budget.max_amount == Decimal("12000.50")
    assert room.bundle_items[0].status is BundleItemStatus.LOCKED
    assert room.bundle_items[0].acquisition is BundleAcquisition.ALREADY_OWNED
    assert isinstance(room.design_needs, tuple)


async def test_an_absent_session_is_a_new_conversation(store: SessionStore) -> None:
    assert await store.load(STORE, "never-seen") is None


# ── compare-and-set ═════════════════════════════════════════════════════════


async def test_the_expected_revision_must_still_be_stored(
    store: SessionStore,
) -> None:
    await store.save_if_revision(STORE, SESSION, expected_revision=0, envelope=an_envelope(1))

    # A second writer that loaded revision 0 has been overtaken.
    refused = await store.save_if_revision(
        STORE, SESSION, expected_revision=0, envelope=an_envelope(1)
    )

    assert refused is False
    stored = await store.load(STORE, SESSION)
    assert stored is not None and stored.session_revision == 1


async def test_a_concurrent_write_between_load_and_save_is_refused(
    store: SessionStore, client: Redis
) -> None:
    """The race the transaction exists for.

    The interfering write lands while this request is mid-transaction, which is
    exactly when a read-then-write would have overwritten it.
    """
    await store.save_if_revision(STORE, SESSION, expected_revision=0, envelope=an_envelope(1))

    # Someone else commits revision 2 while we still believe in revision 1.
    await client.set(session_key(STORE, SESSION), an_envelope(2).model_dump_json())

    refused = await store.save_if_revision(
        STORE, SESSION, expected_revision=1, envelope=an_envelope(2)
    )

    assert refused is False
    stored = await store.load(STORE, SESSION)
    assert stored is not None and stored.session_revision == 2


async def test_only_one_of_two_first_requests_creates_the_session(
    store: SessionStore,
) -> None:
    """Both see no key; both try to write revision 1 (M13 19)."""
    first = await store.save_if_revision(
        STORE, SESSION, expected_revision=0, envelope=an_envelope(1)
    )
    second = await store.save_if_revision(
        STORE, SESSION, expected_revision=0, envelope=an_envelope(1)
    )

    assert first is True
    assert second is False


async def test_an_expired_session_is_a_conflict_not_a_fresh_start(
    store: SessionStore,
) -> None:
    """Expecting revision 3 and finding nothing is not permission to create it."""
    refused = await store.save_if_revision(
        STORE, SESSION, expected_revision=3, envelope=an_envelope(4)
    )

    assert refused is False
    assert await store.load(STORE, SESSION) is None


# ── scope ═══════════════════════════════════════════════════════════════════


async def test_one_session_id_under_two_retailers_is_two_conversations(
    store: SessionStore,
) -> None:
    """Structural: the key differs, so nothing depends on the contents."""
    assert session_key(STORE, SESSION) != session_key(OTHER_STORE, SESSION)

    await store.save_if_revision(STORE, SESSION, expected_revision=0, envelope=an_envelope(1))

    assert await store.load(OTHER_STORE, SESSION) is None
    ours = await store.load(STORE, SESSION)
    assert ours is not None and ours.session_revision == 1


@pytest.mark.parametrize(
    "session_id",
    ["with:colon", "with space", "with*glob", "", "../escape", "a" * 129],
)
def test_an_unaddressable_session_id_is_refused(session_id: str) -> None:
    """Restricted rather than escaped: `:` separates the parts of a key."""
    with pytest.raises(InvalidRequestError):
        session_key(STORE, session_id)


# ── what cannot be read ═════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        '{"session_revision": 1}',  # no state, no envelope version
        '{"envelope_version": "session_v99", "session_revision": 1,'
        ' "state": {}, "conversation": {}}',
        '{"envelope_version": "session_v1", "session_revision": 1,'
        ' "state": {"schema_version": "agent_state_v1"}, "conversation": {}}',
    ],
)
async def test_unreadable_state_refuses_rather_than_resetting(
    store: SessionStore, client: Redis, payload: str
) -> None:
    """Never a silent empty session: that would discard a room the customer
    built and then reinterpret "the second one" against nothing."""
    await client.set(session_key(STORE, SESSION), payload)

    with pytest.raises(SessionStateInvalidError):
        await store.load(STORE, SESSION)


async def test_a_refusal_leaves_the_stored_payload_untouched(
    store: SessionStore, client: Redis
) -> None:
    await client.set(session_key(STORE, SESSION), "not json at all")

    with pytest.raises(SessionStateInvalidError):
        await store.load(STORE, SESSION)

    assert await client.get(session_key(STORE, SESSION)) == "not json at all"


# ── expiry ══════════════════════════════════════════════════════════════════


async def test_saving_sets_the_configured_expiry(client: Redis, store: SessionStore) -> None:
    await store.save_if_revision(STORE, SESSION, expected_revision=0, envelope=an_envelope(1))

    ttl = await client.ttl(session_key(STORE, SESSION))

    assert 0 < ttl <= 60


async def test_a_later_turn_refreshes_the_expiry(client: Redis) -> None:
    """An active conversation must not expire mid-way through."""
    brief = SessionStore(client, SessionSettings(ttl_s=60))
    await brief.save_if_revision(STORE, SESSION, expected_revision=0, envelope=an_envelope(1))
    await client.expire(session_key(STORE, SESSION), 5)

    await brief.save_if_revision(STORE, SESSION, expected_revision=1, envelope=an_envelope(2))

    assert await client.ttl(session_key(STORE, SESSION)) > 5


async def test_a_brand_new_session_is_revision_zero_and_empty() -> None:
    fresh = new_session()

    assert fresh.session_revision == 0
    assert fresh.state == AgentStateV1()
    assert fresh.conversation.messages == ()
