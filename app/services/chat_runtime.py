"""One chat exchange: load, let the team work, check, persist, answer.

The order is the safety property:

**The client's revision is checked before anything is spent.** A request whose
screen is already stale is refused before any model or catalog call.

**The agents work on a staged copy.** Nothing they do is stored until their
reply has passed its checks; a turn that fails leaves the stored session
exactly as it was, and the customer gets the fixed reply.

**Nothing is returned that could not be persisted.** If another request
committed first, this reply describes a session that no longer exists and is
discarded rather than handed back as though it took effect.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.agents.context import ToolContext
from app.agents.deadline import within
from app.agents.reply import customer_response, fallback, repaired, unsupported_figure
from app.agents.screen import presentation as build_presentation
from app.agents.screen_actions import tap_of
from app.agents.services import AgentServices
from app.agents.team import AgentTeam, TeamResult, TeamTurn
from app.agents.tools.common import RespondArgs
from app.agents.trace import TraceEvent, TurnTrace
from app.core.config import SessionSettings
from app.core.exceptions import (
    AgentTurnIncompleteError,
    IntegrationUnavailableError,
    LLMResponseInvalidError,
    SessionConflictError,
)
from app.core.logging import get_logger
from app.repositories.sessions import SessionStore
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_turn import CustomerResponse
from app.schemas.cart import CartView
from app.schemas.chat import ChatPresentation, ChatRequest, ChatResponse, WorkflowView
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole
from app.schemas.language import ReplyLanguage
from app.schemas.picks import LikedView, PickView
from app.schemas.product import ProductCandidate
from app.schemas.retailer import RetailerContext
from app.services.agent_state import keep_screen
from app.services.card_labels import card_labels
from app.services.history_summary import Fold, HistorySummarizer, next_history
from app.services.product_display import displayed
from app.services.product_interaction import build_liked, build_picks
from app.services.session_turns import LoadedSession, load_for_turn

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AgentTurn:
    """What the team's turn left: the state to store and the reply to show."""

    state: AgentStateV1
    response: CustomerResponse
    presentation: ChatPresentation | None
    owner: AgentPersona
    handoffs: tuple[AgentPersona, ...]
    language: ReplyLanguage
    picks: tuple[PickView, ...] | None
    liked: tuple[LikedView, ...] | None
    cart: CartView | None
    fold: Fold | None = None
    """The oldest messages' summary, made beside the agents, for the save to
    apply - None when nothing was due or none could be made."""


class ChatRuntime:
    def __init__(
        self,
        team: AgentTeam,
        services: AgentServices,
        sessions: SessionStore,
        settings: SessionSettings,
        *,
        deadline_s: float = 40.0,
        room_deadline_s: float | None = None,
        arabic_replies: bool = False,
        summarizer: HistorySummarizer | None = None,
    ) -> None:
        self._deadline_s = deadline_s
        self._room_deadline_s = room_deadline_s if room_deadline_s is not None else deadline_s
        self._team = team
        self._services = services
        self.on_event: Callable[[TraceEvent], None] | None = None
        """Told of each step as the agents take it - set by a streaming
        caller, None for an ordinary request."""
        self._sessions = sessions
        self._settings = settings
        self._arabic = arabic_replies
        self._summarizer = summarizer

    async def load_session(self, request: ChatRequest) -> LoadedSession:
        return await load_for_turn(
            self._sessions,
            store_id=request.store_id,
            session_id=request.session_id,
            expected_revision=request.expected_session_revision,
        )

    async def run_agents(
        self, request: ChatRequest, context: RetailerContext, loaded: LoadedSession
    ) -> AgentTurn:
        """The team's turn - and, beside it, the summary of the oldest
        messages when the conversation is about to outgrow its window."""
        if self._summarizer is None:
            return await self._team_turn(request, context, loaded)
        folding = asyncio.create_task(self._summarizer.fold(loaded.envelope.conversation))
        try:
            turn = await self._team_turn(request, context, loaded)
        except BaseException:
            folding.cancel()
            if folding.done() and not folding.cancelled():
                folding.exception()  # retrieved, so a failed fold is not reported twice
            raise
        return replace(turn, fold=await self._collected(folding))

    async def _collected(self, folding: asyncio.Task[Fold | None]) -> Fold | None:
        """The summary, if it is ready soon after the team: the customer never
        waits on it for more than `summary.wait_after_turn_s`. Not ready, it is
        cancelled and the next turn folds instead - nothing is dropped meanwhile."""
        try:
            return await asyncio.wait_for(folding, self._settings.summary.wait_after_turn_s)
        except TimeoutError:
            logger.info("history_summary_skipped", reason="not ready after the turn")
            return None
        except Exception:
            # A defect in a side step must not discard a reply the agents have
            # finished: logged in full, and the turn is saved unfolded.
            logger.exception("history_summary_defect")
            return None

    async def _team_turn(
        self, request: ChatRequest, context: RetailerContext, loaded: LoadedSession
    ) -> AgentTurn:
        pre_turn = loaded.envelope.state
        language = self._language(request, pre_turn)
        tap = tap_of(request)
        turn = TeamTurn(
            message=request.message,
            conversation=loaded.envelope.conversation,
            state=pre_turn,
            context=ToolContext(
                retailer=context,
                services=self._services,
                language=language,
                tap_cards=tap.cards if tap is not None else (),
                tapped=tap.event.button if tap is not None else None,
                customer_said=request.message if tap is None else None,
            ),
            tap=tap,
        )
        trace = TurnTrace(
            store_id=context.store_id, path="tap" if tap else "typed", listener=self.on_event
        )
        try:
            # One deadline for all of it: the agents, and the reply's rewrite.
            result, reply = await within(
                self._worked(turn, trace),
                short_s=self._deadline_s,
                long_s=self._room_deadline_s,
                extended=lambda: trace.room_design,
            )
        except TimeoutError:
            trace.finish(owner=None, fallback="turn deadline")
            return await self._finished(
                pre_turn,
                pre_turn,
                fallback(language.value),
                None,
                AgentPersona.SARA,
                (),
                language,
                context,
            )
        except LLMResponseInvalidError as exc:
            # Our failure to understand, never the customer's: the fixed
            # reply, with nothing the turn did kept.
            trace.finish(owner=None, fallback=str(exc.context.get("reason", exc.code)))
            return await self._finished(
                pre_turn,
                pre_turn,
                fallback(language.value),
                None,
                AgentPersona.SARA,
                (),
                language,
                context,
            )
        state = result.work.staged
        if reply.show == "unchanged":
            # Nothing this turn found was right to show: the screen, and what
            # "the second one" means, stay as they were.
            state = keep_screen(state, pre_turn)
        # The language is the replying agent's call - the language it answered
        # in - and the cards and buttons follow it; a store's locale still wins.
        if self._arabic and request.locale is None and reply.language is not None:
            language = ReplyLanguage(reply.language)
        if self._arabic and language is not pre_turn.reply_language:
            state = state.model_copy(update={"reply_language": language})
        trace.finish(owner=result.owner.value)
        screen = build_presentation(
            result.work, state, customer_response(reply).choices, language, reply.show
        )
        return await self._finished(
            pre_turn, state, reply, screen, result.owner, result.handoffs, language, context
        )

    async def _worked(self, turn: TeamTurn, trace: TurnTrace) -> tuple[TeamResult, RespondArgs]:
        result = await self._team.run(turn, trace)
        return result, await self._checked(turn, result, trace)

    async def _checked(self, turn: TeamTurn, result: TeamResult, trace: TurnTrace) -> RespondArgs:
        """The reply, once its figures are all ones a tool or the customer gave."""
        said = (turn.tap.said,) if turn.tap is not None else ()
        broke = unsupported_figure(
            result.reply,
            result.work,
            turn.message,
            turn.conversation,
            result.views,
            also_said=said,
        )
        if broke is None:
            return result.reply
        logger.warning("agent_reply_figure_unsupported", field=broke.field)
        try:
            corrected = await self._team.rewrite(
                turn,
                result,
                f"the figure {broke.value} came from no tool and no customer message - write "
                "figures as {key} from figures, and work out any sum with calculate",
                trace,
            )
        except (AgentTurnIncompleteError, LLMResponseInvalidError):
            # The rewrite itself failed: what the agents did still stands, and
            # the first reply loses only its unsupported sentences.
            corrected = result.reply
        again = unsupported_figure(
            corrected,
            result.work,
            turn.message,
            turn.conversation,
            result.views,
            also_said=said,
        )
        if again is None:
            return corrected
        # Twice a figure nobody gave: the sentences that carry one go, and
        # the rest of the reply stands. Only a reply with nothing left ends on
        # the fixed words.
        kept = repaired(
            corrected,
            result.work,
            turn.message,
            turn.conversation,
            result.views,
            also_said=said,
        )
        if kept is None:
            raise LLMResponseInvalidError(reason="reply figures unsupported twice")
        logger.warning("agent_reply_figure_sentences_dropped", field=again.field)
        return kept

    async def _finished(
        self,
        pre_turn: AgentStateV1,
        state: AgentStateV1,
        reply: RespondArgs,
        screen: ChatPresentation | None,
        owner: AgentPersona,
        handoffs: tuple[AgentPersona, ...],
        language: ReplyLanguage,
        context: RetailerContext,
    ) -> AgentTurn:
        picks, liked, cart = await self._tray(pre_turn, state, language, context)
        return AgentTurn(
            state=state,
            response=customer_response(reply),
            presentation=screen,
            owner=owner,
            handoffs=handoffs,
            language=language,
            picks=picks,
            liked=liked,
            cart=cart,
        )

    async def _tray(
        self,
        pre_turn: AgentStateV1,
        state: AgentStateV1,
        language: ReplyLanguage,
        context: RetailerContext,
    ) -> tuple[tuple[PickView, ...] | None, tuple[LikedView, ...] | None, CartView | None]:
        """The picks, likes and cart after the turn, from one fresh read."""
        interaction = state.product_interaction
        wanted = tuple(
            dict.fromkeys((*interaction.selected_product_ids, *interaction.liked_product_ids))
        )
        products: tuple[ProductCandidate, ...] = ()
        if wanted:
            try:
                products = tuple(await self._services.hydration.hydrate_ids(wanted, context))
            except IntegrationUnavailableError:
                return None, None, None
        display = self._services.display
        picks = build_picks(state, products, display, language)
        liked = build_liked(state, products, display, language)
        cart = None
        if self._services.cart is not None:
            try:
                cart = await self._services.cart.view(
                    state, picks, context, language, before=pre_turn
                )
            except IntegrationUnavailableError:
                cart = None
        return picks, liked, cart

    async def persist(self, request: ChatRequest, loaded: LoadedSession, turn: AgentTurn) -> int:
        """Commit the turn, or refuse because another request already did."""
        envelope = loaded.envelope.advanced(
            state=turn.state,
            conversation=self.next_conversation(loaded, request, turn),
        )
        committed = await self._sessions.save_if_revision(
            request.store_id,
            request.session_id,
            expected_revision=loaded.loaded_revision,
            envelope=envelope,
        )
        if not committed:
            raise SessionConflictError(
                expected_revision=loaded.loaded_revision, store_id=request.store_id
            )
        return envelope.session_revision

    def next_conversation(
        self, loaded: LoadedSession, request: ChatRequest, turn: AgentTurn
    ) -> ConversationContext:
        said = turn.response.message
        if turn.response.follow_up_question:
            said = f"{said} {turn.response.follow_up_question}"
        tap = tap_of(request)
        customer = (
            tap.recorded if tap is not None and not request.message.strip() else request.message
        )
        exchange = (
            ConversationMessage(role=ConversationRole.USER, content=customer or "…"),
            ConversationMessage(
                role=ConversationRole.ASSISTANT,
                content=said,
                agent=turn.owner,
                choices=offered(turn),
            ),
        )
        return next_history(loaded.envelope.conversation, exchange, self._settings, turn.fold)

    def public_response(self, request: ChatRequest, revision: int, turn: AgentTurn) -> ChatResponse:
        return ChatResponse(
            session_id=request.session_id,
            session_revision=revision,
            response=turn.response,
            presentation=displayed(turn.presentation, self._services.display, turn.language),
            picks=turn.picks,
            liked=turn.liked,
            cart=turn.cart,
            reply_language=turn.language if self._arabic else None,
            agent=turn.owner,
            workflow=WorkflowView(owner=turn.owner, handoffs=turn.handoffs),
            labels=card_labels(turn.language if self._arabic else None),
        )

    async def run(self, request: ChatRequest, context: RetailerContext) -> ChatResponse:
        """Every phase, in order - the sequence the graph walks."""
        started = time.perf_counter()
        loaded = await self.load_session(request)
        turn = await self.run_agents(request, context, loaded)
        revision = await self.persist(request, loaded, turn)
        logger.info(
            "chat_turn_completed",
            store_id=request.store_id,
            revision_before=loaded.loaded_revision,
            revision_after=revision,
            owner=turn.owner.value,
            handoffs=[a.value for a in turn.handoffs],
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return self.public_response(request, revision, turn)

    def _language(self, request: ChatRequest, state: AgentStateV1) -> ReplyLanguage:
        if not self._arabic:
            return ReplyLanguage.EN
        return request.locale or state.reply_language or ReplyLanguage.EN


def offered(turn: AgentTurn) -> tuple[str, ...]:
    """Every answer the customer could tap beside the reply, by its label: the
    reply's chips, and the store's support buttons code drew."""
    support = turn.presentation.support if turn.presentation is not None else None
    channels = support.channels if support is not None else ()
    labels = (*(c.label for c in turn.response.choices), *(c.label for c in channels))
    return tuple(dict.fromkeys(labels))[:12]
