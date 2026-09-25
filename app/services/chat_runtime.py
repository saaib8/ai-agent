"""One chat exchange, as five ordered phases.

These are the phases the runtime graph sequences. They live here rather than
inside graph nodes so that orchestration stays a thin wrapper: a node calls one
of these and stores its result, and every rule about *what* a phase does is
ordinary application code that can be tested without a graph (CLAUDE.md 18).

The order is the safety property, not a style preference.

**The client's revision is checked before anything is spent.** A request whose
screen is already stale is refused before the decision model, the design
specialist, PostgreSQL, the index and the response model - all of which cost
money and none of which would be answering the question the customer thought
they were asking.

**Nothing persists until the customer-visible reply exists.** A turn that
failed before producing one leaves the stored session exactly as it was, so a
retry starts from the same place rather than from a half-applied state.

**Nothing is returned that could not be persisted.** If another request
committed first, the reply computed here describes a session that no longer
exists; it is discarded rather than handed back as though it had taken effect.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.config import SessionSettings
from app.core.exceptions import SessionConflictError
from app.core.logging import get_logger
from app.repositories.sessions import SessionStore
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import (
    CustomerResponse,
    CustomerTurnInput,
    CustomerTurnResult,
)
from app.schemas.chat import ChatPresentation, ChatRequest, ChatResponse
from app.schemas.conversation import (
    ConversationContext,
    ConversationMessage,
    ConversationRole,
)
from app.schemas.grounding import GroundedProduct
from app.schemas.retailer import RetailerContext
from app.schemas.session import SessionEnvelope, new_session
from app.services.bundle_presentation import build_bundle_presentation
from app.services.response_generator import CustomerResponseGenerator
from app.services.turn_coordinator import CustomerTurnCoordinator

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


class ChatRuntime:
    """The phases of one exchange. Sequencing belongs to the caller."""

    def __init__(
        self,
        coordinator: CustomerTurnCoordinator,
        responses: CustomerResponseGenerator,
        sessions: SessionStore,
        settings: SessionSettings,
    ) -> None:
        self._coordinator = coordinator
        self._responses = responses
        self._sessions = sessions
        self._settings = settings

    # ── 1. load ─────────────────────────────────────────────────────────────

    async def load_session(self, request: ChatRequest) -> LoadedSession:
        """The stored conversation, or a fresh one, with staleness settled."""
        return await load_for_turn(
            self._sessions,
            store_id=request.store_id,
            session_id=request.session_id,
            expected_revision=request.expected_session_revision,
        )

    # ── 2. run the turn ─────────────────────────────────────────────────────

    def turn_input(
        self, request: ChatRequest, context: RetailerContext, loaded: LoadedSession
    ) -> CustomerTurnInput:
        """What the domain engine is given.

        The current message travels beside the history and never inside it, so
        nothing downstream has to work out which entry is "now". The stored
        conversation is prior turns only, which is exactly what was persisted.
        """
        return CustomerTurnInput(
            message=request.message,
            conversation=loaded.envelope.conversation,
            state=loaded.envelope.state,
            context=context,
            bundle_action=request.bundle_action,
            search_action=request.search_action,
        )

    async def run_turn(self, turn: CustomerTurnInput) -> CustomerTurnResult:
        """One call to the coordinator, which remains the domain engine.

        No branching on the decision, no action routing, no direct call to any
        specialist. Everything a turn does was settled in M11 and M12; this is
        the runtime handing it a request (M13 22).
        """
        return await self._coordinator.run(turn)

    # ── 3. render ───────────────────────────────────────────────────────────

    async def render(self, turn: CustomerTurnInput, result: CustomerTurnResult) -> CustomerResponse:
        """The customer-visible reply, by the existing response architecture.

        Always produces one: the generator routes the turn itself and words
        deterministic branches without a model, falling back to fixed wording
        when a provider call fails. What comes back is therefore what actually
        happened, which is what gets persisted (M13 12).
        """
        return await self._responses.generate(turn, result)

    @staticmethod
    def presentation(result: CustomerTurnResult) -> ChatPresentation | None:
        """What the client draws, built from verified facts only.

        Assembled from the turn's own grounding and the room the optimiser
        chose. The response model contributes nothing here: it cites handles,
        and the application renders the cards those handles name, so a price in
        the text and a price on a card cannot disagree (CLAUDE.md 20.4).
        """
        grounding = result.grounding
        products: tuple[GroundedProduct, ...] = ()
        if grounding.search is not None:
            products = grounding.search.products
        elif grounding.selection is not None:
            products = grounding.selection.products
        elif grounding.product_detail is not None:
            products = (grounding.product_detail,)

        built = ChatPresentation(
            products=products,
            comparison=grounding.comparison,
            room=build_bundle_presentation(result),
        )
        return None if built.is_empty() else built

    # ── 4. persist ──────────────────────────────────────────────────────────

    def next_conversation(
        self, loaded: LoadedSession, request: ChatRequest, response: CustomerResponse
    ) -> ConversationContext:
        """Prior history plus this completed exchange, bounded.

        Language only. Products, prices, rooms and preferences are already
        recorded as structured facts in the agent state or read fresh from the
        catalog; repeating them here would create a second, ageing copy that a
        later turn could read instead of the truth (M13 11).

        The assistant's turn is stored **as the customer received it**, prose
        and follow-up together. They are separate fields so a client can render
        the question its own way; they are one utterance to the person reading
        them, and storing only the prose left the next turn unable to see the
        question it was being answered.

        That is what made "yes please" meaningless: the agent had offered to
        look for rugs, the offer was not in the history, and it asked what kind
        of furniture they wanted (M16 5).
        """
        said = response.message
        if response.follow_up_question:
            said = f"{said} {response.follow_up_question}"
        messages = (
            *loaded.envelope.conversation.messages,
            ConversationMessage(role=ConversationRole.USER, content=request.message),
            ConversationMessage(role=ConversationRole.ASSISTANT, content=said),
        )
        return ConversationContext(
            messages=trim_history(messages, self._settings.max_history_messages)
        )

    async def persist(
        self,
        request: ChatRequest,
        loaded: LoadedSession,
        result: CustomerTurnResult,
        response: CustomerResponse,
    ) -> int:
        """Commit the turn, or refuse because someone else already did.

        The comparison is against the revision this request *loaded*, so two
        requests that both read revision 4 cannot both write revision 5. The
        loser raises rather than returning: its answer resolved the customer's
        words against a room that has since changed (M13 18, 33).
        """
        envelope = loaded.envelope.advanced(
            state=result.state,
            conversation=self.next_conversation(loaded, request, response),
        )
        committed = await self._sessions.save_if_revision(
            request.store_id,
            request.session_id,
            expected_revision=loaded.loaded_revision,
            envelope=envelope,
        )
        if not committed:
            raise SessionConflictError(
                expected_revision=loaded.loaded_revision,
                store_id=request.store_id,
            )
        return envelope.session_revision

    # ── 5. answer ───────────────────────────────────────────────────────────

    @staticmethod
    def public_response(
        request: ChatRequest,
        revision: int,
        response: CustomerResponse,
        presentation: ChatPresentation | None,
    ) -> ChatResponse:
        return ChatResponse(
            session_id=request.session_id,
            session_revision=revision,
            response=response,
            presentation=presentation,
        )

    # ── the whole exchange ──────────────────────────────────────────────────

    async def run(self, request: ChatRequest, context: RetailerContext) -> ChatResponse:
        """All five phases in order.

        The sequence a graph node-walk performs, kept here so the same ordering
        is exercised whether or not a graph is driving it.
        """
        started = time.perf_counter()
        loaded = await self.load_session(request)

        turn = self.turn_input(request, context, loaded)
        result = await self.run_turn(turn)
        response = await self.render(turn, result)
        presentation = self.presentation(result)

        revision = await self.persist(request, loaded, result, response)
        self._log(request, loaded, revision, presentation, started)
        return self.public_response(request, revision, response, presentation)

    @staticmethod
    def _log(
        request: ChatRequest,
        loaded: LoadedSession,
        revision: int,
        presentation: ChatPresentation | None,
        started: float,
    ) -> None:
        """Shape of the exchange only.

        No message, no history, no reply prose, no product names or prices, no
        session payload. `session_id` is the customer's conversation handle and
        is not logged; `store_id` and the revisions describe the turn without
        naming anyone (CLAUDE.md 22).
        """
        logger.info(
            "chat_turn_completed",
            store_id=request.store_id,
            revision_before=loaded.loaded_revision,
            revision_after=revision,
            new_session=not loaded.existed,
            had_expected_revision=request.expected_session_revision is not None,
            product_count=len(presentation.products) if presentation else 0,
            has_comparison=bool(presentation and presentation.comparison is not None),
            has_room=bool(presentation and presentation.room is not None),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )


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
    envelope = stored if stored is not None else new_session()

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
    messages = (
        *loaded.envelope.conversation.messages,
        ConversationMessage(role=ConversationRole.USER, content=customer_said),
        ConversationMessage(role=ConversationRole.ASSISTANT, content=said),
    )
    envelope = loaded.envelope.advanced(
        state=state,
        conversation=ConversationContext(
            messages=trim_history(messages, settings.max_history_messages)
        ),
    )
    committed = await sessions.save_if_revision(
        store_id, session_id, expected_revision=loaded.loaded_revision, envelope=envelope
    )
    if not committed:
        raise SessionConflictError(expected_revision=loaded.loaded_revision, store_id=store_id)
    return envelope.session_revision


def trim_history(
    messages: tuple[ConversationMessage, ...], limit: int
) -> tuple[ConversationMessage, ...]:
    """The most recent messages, in order, without orphaning a reply.

    Trimmed **after** the completed turn is appended, so the exchange that just
    happened is always kept. Oldest first out, which is what keeps the window
    chronological.

    Whole exchanges where possible: a history beginning with an assistant
    message would show an answer to a question nobody can see, so the orphan is
    dropped too. That can leave the window one under the limit, which is the
    right trade - the bound is a ceiling, not a quota to fill.
    """
    if len(messages) <= limit:
        return messages
    kept = messages[len(messages) - limit :]
    if kept and kept[0].role is ConversationRole.ASSISTANT:
        kept = kept[1:]
    return kept
