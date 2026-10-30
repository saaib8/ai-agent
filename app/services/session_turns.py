"""Loading a conversation for a turn, and committing what the turn did.

Shared by every kind of turn - a chat message, a pick tapped in the tray, a
photo search, a room render - so a stale screen is refused by one rule and every
write is a compare-and-set against the revision the turn loaded.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import SessionSettings
from app.core.exceptions import SessionConflictError
from app.core.logging import get_logger
from app.repositories.sessions import SessionStore
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse
from app.schemas.conversation import ConversationMessage, ConversationRole
from app.schemas.session import SessionEnvelope, new_session
from app.services.history_summary import next_history
from app.services.shopper_cart import with_shopper_cart

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LoadedSession:
    """The session this turn runs against, and the revision it started from.

    `loaded_revision` is kept beside the envelope because it is what the write
    must compare against later. Reading it back off the envelope at save time
    would compare against whatever the envelope had become.
    """

    envelope: SessionEnvelope
    loaded_revision: int
    existed: bool


async def with_the_shoppers_cart(
    sessions: SessionStore, store_id: int, session_id: str, envelope: SessionEnvelope
) -> SessionEnvelope:
    """`envelope` holding its shopper's cart: the cart is the shopper's, not
    this chat's, so a new chat starts with it and a resumed one sees what
    another chat changed (CLAUDE.md 36). Unchanged where the id names no
    shopper or none is kept."""
    cart = await sessions.shopper_cart(store_id, session_id)
    state = with_shopper_cart(envelope.state, cart, new_conversation=envelope.session_revision == 0)
    return envelope if state is envelope.state else envelope.model_copy(update={"state": state})


async def load_for_turn(
    sessions: SessionStore,
    *,
    store_id: int,
    session_id: str,
    expected_revision: int | None,
) -> LoadedSession:
    """The stored conversation, or a fresh one, with staleness settled.

    Shared by every kind of turn - a message, or a pick in a photo - so a stale
    screen is refused by one rule wherever the customer acted on it.

    An absent key is a new conversation **only when the client is not
    claiming to have seen one**. A request carrying `expected_session_
    revision=5` against a key that has expired is not starting fresh: its
    screen shows products from a session that no longer exists, and
    creating an empty one would then resolve "the second one" against
    nothing (M13 34).
    """
    stored = await sessions.load(store_id, session_id)
    envelope = await with_the_shoppers_cart(
        sessions, store_id, session_id, stored if stored is not None else new_session()
    )

    if expected_revision is not None and expected_revision != envelope.session_revision:
        logger.info(
            "session_revision_stale",
            store_id=store_id,
            expected_revision=expected_revision,
            actual_revision=envelope.session_revision,
            existed=stored is not None,
        )
        raise SessionConflictError(
            expected_revision=expected_revision,
            actual_revision=envelope.session_revision,
        )

    return LoadedSession(
        envelope=envelope,
        loaded_revision=envelope.session_revision,
        existed=stored is not None,
    )


async def commit_state(
    sessions: SessionStore,
    *,
    store_id: int,
    session_id: str,
    loaded: LoadedSession,
    state: AgentStateV1,
) -> int:
    """Persist a change of state that was not an exchange of words.

    For a tick: the customer picked a product on screen, which the agent must
    know about, but nothing was said and nothing is answered, so the
    conversation is left exactly as it was. Compare-and-set against the loaded
    revision, like every write (M13 18, 33).
    """
    envelope = loaded.envelope.advanced(state=state, conversation=loaded.envelope.conversation)
    committed = await sessions.save_if_revision(
        store_id, session_id, expected_revision=loaded.loaded_revision, envelope=envelope
    )
    if not committed:
        raise SessionConflictError(expected_revision=loaded.loaded_revision, store_id=store_id)
    return envelope.session_revision


async def commit_exchange(
    sessions: SessionStore,
    settings: SessionSettings,
    *,
    store_id: int,
    session_id: str,
    loaded: LoadedSession,
    state: AgentStateV1,
    customer_said: str,
    response: CustomerResponse,
    agent: AgentPersona | None = None,
) -> int:
    """Persist one exchange that did not come from a typed message.

    For turns driven by a tap rather than words - a pick in a photo, a request
    to see the room. The history is language only, so the customer's side is
    recorded as the words `customer_said`, and the assistant's exactly as it
    was shown, prose and follow-up together.

    Compare-and-set against the revision this turn loaded: a turn that lost
    the race raises rather than returning a reply about a session that has
    moved on (M13 18, 33).
    """
    said = response.message
    if response.follow_up_question:
        said = f"{said} {response.follow_up_question}"
    exchange = (
        ConversationMessage(role=ConversationRole.USER, content=customer_said),
        ConversationMessage(
            role=ConversationRole.ASSISTANT,
            content=said,
            agent=agent,
            choices=tuple(c.label for c in response.choices)[:12],
        ),
    )
    envelope = loaded.envelope.advanced(
        state=state,
        conversation=next_history(loaded.envelope.conversation, exchange, settings, None),
    )
    committed = await sessions.save_if_revision(
        store_id, session_id, expected_revision=loaded.loaded_revision, envelope=envelope
    )
    if not committed:
        raise SessionConflictError(expected_revision=loaded.loaded_revision, store_id=store_id)
    return envelope.session_revision
