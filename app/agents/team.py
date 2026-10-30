"""The team: a supervisor that delegates, and four agents that act.

A LangGraph graph whose nodes are the supervisor and the four agents. The
supervisor reads the message and transfers the turn to an agent; the agent
works through its tools and either replies or hands the turn back with a
typed reason, and the supervisor sends it on. Every agent shares the turn's
staged state and tool record - one memory - and the next agent reads exactly
what the last one did.

Bounded, always: a number of handoffs, model calls, tools per hop, and a
deadline for the whole turn. Out of handoffs or calls, or with arguments broken
twice, the agent who was working says what was done, and the work its tools
finished is kept; if it cannot - or past the deadline - the caller answers with
its fixed reply and nothing changed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import SQLAlchemyError

from app.agents.context import ToolContext
from app.agents.deadline import within
from app.agents.loop import AgentLoop, HandedBack, Responded, TurnBudget, json_facts
from app.agents.reply import unsupported_figure
from app.agents.screen_actions import Tap
from app.agents.tools.common import HandBackReason, RespondArgs
from app.agents.tools.shelf import stocked_kinds
from app.agents.trace import TraceEvent, TurnTrace, elapsed_ms
from app.agents.views import (
    agent_messages,
    assistant_said,
    done_so_far,
    earlier_summary,
    turn_view,
)
from app.agents.work import ToolRecord, ToolStatus, TurnWork
from app.core.config import AgentsSettings
from app.core.exceptions import AgentTurnIncompleteError, LLMResponseInvalidError, ZoryError
from app.integrations.chat_models import call_chat_model
from app.prompts.agents.v2 import SUPERVISOR, agent_instructions, reply_correction
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import AgentStateV1
from app.schemas.conversation import ConversationContext, ConversationMessage, ConversationRole

SUPERVISOR_ROLE = "supervisor"
FINISH = "finish"


class Need(StrEnum):
    """Why the supervisor transfers the turn - a typed task, never a note."""

    SHOPPING = "shopping"
    DESIGN = "design"
    SALES = "sales"
    SUPPORT = "support"
    GREETING = "greeting"


def _transfer(agent: AgentPersona, does: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": f"transfer_to_{agent.value}",
            "description": does,
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": {"need": {"type": "string", "enum": [n.value for n in Need]}},
                "required": ["need"],
                "additionalProperties": False,
            },
        },
    }


SUPERVISOR_TOOLS = [
    _transfer(AgentPersona.SARA, "Shopping: finding, comparing, the cart."),
    _transfer(AgentPersona.NORA, "Interior design and whole rooms."),
    _transfer(AgentPersona.SALMAN, "What goes with a pick, completing a room, value."),
    _transfer(AgentPersona.FAYSAL, "The store: delivery, returns, warranty, contacts."),
    {
        "type": "function",
        "function": {
            "name": FINISH,
            "description": (
                "Nothing more needs doing: the named agent replies to the customer with what "
                "was done this turn, using no further tools."
            ),
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "reply_by": {"type": "string", "enum": [a.value for a in AgentPersona]}
                },
                "required": ["reply_by"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass(frozen=True, slots=True)
class TeamTurn:
    """One customer turn, as the team is given it."""

    message: str
    conversation: ConversationContext
    state: AgentStateV1
    context: ToolContext
    tap: Tap | None = None


@dataclass(slots=True)
class TeamResult:
    work: TurnWork
    reply: RespondArgs
    owner: AgentPersona
    handoffs: tuple[AgentPersona, ...]
    view: dict[str, Any] = field(default_factory=dict)
    """The last view an agent was shown - the replying agent's."""
    views: tuple[dict[str, Any], ...] = ()
    """Every view an agent was shown this turn: a figure any of them held may
    be said."""


class _GraphState(TypedDict, total=False):
    turn: TeamTurn
    work: TurnWork
    budget: TurnBudget
    trace: TurnTrace
    view: dict[str, Any]
    views: list[dict[str, Any]]
    next: str
    handoffs: list[AgentPersona]
    reply: Responded | None
    last: AgentPersona | None
    handed_back: HandBackReason | None
    need: str | None
    asked_again: bool


class AgentTeam:
    """Built once per process: the models, the tools and the graph. What a turn
    needs of the request - its services, its database session - travels in
    each tool call's `ToolContext`, never in what was compiled."""

    def __init__(
        self,
        supervisor: BaseChatModel,
        agents: Mapping[AgentPersona, BaseChatModel],
        settings: AgentsSettings,
        loop: AgentLoop,
        *,
        wording: Mapping[AgentPersona, BaseChatModel] | None = None,
    ) -> None:
        self._loop = loop
        self._wording = dict(wording or {})
        """Each agent's model at its wording effort, for the reply after a
        room's fixes (`agents.<agent>.wording_reasoning_effort`)."""
        self._supervisor = supervisor.bind_tools(
            SUPERVISOR_TOOLS, parallel_tool_calls=False, strict=False
        )
        self._agents = dict(agents)
        self._settings = settings
        self._tools = {a.value: sorted(t.name for t in loop.tools_for(a)) for a in AgentPersona}
        """Each agent's tools, so the supervisor sends work to one who can do it."""
        self._graph = self._build().compile()

    async def run(self, turn: TeamTurn, trace: TurnTrace) -> TeamResult:
        said = (
            turn.message,
            *((turn.tap.said,) if turn.tap is not None else ()),
            *(m.content for m in turn.conversation.messages if m.role is ConversationRole.USER),
        )
        work = TurnWork.starting(turn.state, said=said)
        view = await turn_view(turn.context.services, turn.state, turn.context)
        initial: _GraphState = {
            "turn": turn,
            "work": work,
            "budget": TurnBudget(max_model_calls=self._settings.max_model_calls),
            "trace": trace,
            "view": view,
            "views": [view],
            "handoffs": [],
            "reply": None,
            "last": None,
            "handed_back": None,
            "need": None,
            "asked_again": False,
            # Every turn starts with the supervisor, a tap as much as a message.
            "next": SUPERVISOR_ROLE,
        }
        try:
            final = await within(
                self._graph.ainvoke(initial),
                short_s=self._settings.work_deadline_s,
                long_s=self._settings.room_work_deadline_s,
                extended=lambda: trace.room_design,
            )
            reply = final.get("reply")
            if reply is None:
                raise AgentTurnIncompleteError(reason="no agent replied")
        except LLMResponseInvalidError as stopped:
            # A rule stopped the turn. The work done stays done: the agent who
            # was working says what was done and what was not, in its words.
            return await self._stopped(turn, work, view, stopped, trace)
        except TimeoutError:
            # Out of working time. Each tool's change lands whole, so what
            # finished is kept - a room they described is not lost to a slow
            # step after it - and the agent who was working says where it is.
            # The step cancelled may have been mid-query: the catalog session
            # is reset before the room is read again.
            reset = getattr(turn.context.services, "catalog_reset", None)
            if reset is not None:
                await reset()
            out_of_time = LLMResponseInvalidError(reason="out of working time")
            return await self._stopped(turn, work, view, out_of_time, trace)
        return TeamResult(
            work=final["work"],
            reply=reply.reply,
            owner=reply.agent,
            handoffs=tuple(final["handoffs"]),
            view=final["views"][-1],
            # Every brief an agent was shown: a figure any of them held is sayable.
            views=tuple(work.views) or tuple(final["views"]),
        )

    async def _stopped(
        self,
        turn: TeamTurn,
        work: TurnWork,
        first_view: dict[str, Any],
        stopped: LLMResponseInvalidError,
        trace: TurnTrace,
    ) -> TeamResult:
        """One reply-only call for the agent who was working, told why the turn
        stopped. Its own failure is the original one: the fixed reply follows."""
        agents = tuple(dict.fromkeys(r.agent for r in work.records))
        agent = (
            work.working
            or (agents[-1] if agents else None)
            or _offered_by_agent(turn.conversation)
            or AgentPersona.SARA
        )
        reason = str(stopped.context.get("reason", stopped.code))
        trace.add(TraceEvent(agent.value, "handoff", "stopped", 0, "refused", reason))
        try:
            view = (
                await turn_view(turn.context.services, work.staged, turn.context)
                if work.staged is not work.pre_turn
                else first_view
            )
            model = self._model(agent)
        except (ZoryError, SQLAlchemyError):
            # Nothing to describe the work with: the fixed reply, never a 500.
            raise stopped from None
        messages = [
            *agent_messages(
                agent_instructions(agent),
                turn.conversation,
                _said(turn),
                view,
                work,
                history=self._settings.history_messages,
            ),
            # The customer is told in plain words; how the rule was broken
            # stays internal (CLAUDE.md 20.5).
            HumanMessage(_STOPPED),
        ]
        try:
            end = await self._loop.run(
                agent,
                model,
                messages,
                work,
                turn.context,
                # Room for the one correction a broken reply is allowed.
                TurnBudget(max_model_calls=2),
                trace,
                only=frozenset({"respond"}),
            )
        except ZoryError:
            raise stopped from None
        if not isinstance(end, Responded):
            raise stopped
        return TeamResult(
            work=work,
            reply=end.reply,
            owner=agent,
            handoffs=(*(a for a in agents if a is not agent), agent),
            view=view,
            views=tuple(work.views) or (first_view,),
        )

    async def word(
        self,
        agent: AgentPersona,
        happened: str,
        facts: Mapping[str, Any],
        state: AgentStateV1,
        conversation: ConversationContext,
        context: ToolContext,
        trace: TurnTrace,
    ) -> RespondArgs | None:
        """The agent's reply to something that happened outside a chat turn -
        a photo searched, a room rendered - from its facts. Offered `respond`
        alone, its figures checked like any reply, corrected once; None when
        no usable reply came, so the caller falls back to its fixed words."""
        work = TurnWork.starting(state)
        work.record(
            ToolRecord(agent=agent, tool="event", status=ToolStatus.OK, facts=json_facts(facts))
        )
        view = await turn_view(context.services, state, context)
        messages = agent_messages(
            agent_instructions(agent),
            conversation,
            happened,
            view,
            work,
            history=self._settings.history_messages,
        )
        for attempt in range(2):
            try:
                end = await self._loop.run(
                    agent,
                    self._model(agent),
                    messages,
                    work,
                    context,
                    TurnBudget(max_model_calls=2),
                    trace,
                    only=frozenset({"respond"}),
                )
            except LLMResponseInvalidError:
                return None
            if not isinstance(end, Responded):
                return None
            broke = unsupported_figure(end.reply, work, happened, conversation, [view])
            if broke is None:
                return end.reply
            if attempt == 0:
                messages = [
                    *messages,
                    HumanMessage(
                        f"Your reply was:\n{end.reply.message}\n\n"
                        + reply_correction(f"the figure {broke.value} came from no fact")
                    ),
                ]
        return None

    async def rewrite(
        self, turn: TeamTurn, result: TeamResult, rule: str, trace: TurnTrace
    ) -> RespondArgs:
        """The replying agent's one chance to correct a reply that broke a
        rule: offered `respond` alone, told the rule, and nothing else."""
        messages = [
            *agent_messages(
                agent_instructions(result.owner),
                turn.conversation,
                _said(turn),
                result.view,
                result.work,
                history=self._settings.history_messages,
            ),
            HumanMessage(f"Your reply was:\n{result.reply.message}\n\n{reply_correction(rule)}"),
        ]
        end = await self._loop.run(
            result.owner,
            self._model(result.owner),
            messages,
            result.work,
            turn.context,
            # Room for the one correction a broken reference is allowed.
            TurnBudget(max_model_calls=2),
            trace,
            only=frozenset({"respond"}),
        )
        if not isinstance(end, Responded):
            raise AgentTurnIncompleteError(reason="no corrected reply")
        return end.reply

    # ── the graph ───────────────────────────────────────────────────────────

    def _build(self) -> StateGraph[_GraphState, None, _GraphState, _GraphState]:
        graph: StateGraph[_GraphState, None, _GraphState, _GraphState] = StateGraph(_GraphState)
        graph.add_node(SUPERVISOR_ROLE, self._supervise)
        for agent in AgentPersona:
            graph.add_node(agent.value, self._agent_node(agent))
        targets: dict[Hashable, str] = {a.value: a.value for a in AgentPersona}
        targets[SUPERVISOR_ROLE] = SUPERVISOR_ROLE
        targets[FINISH] = END
        graph.add_conditional_edges(START, _route, targets)
        graph.add_conditional_edges(SUPERVISOR_ROLE, _route, targets)
        for agent in AgentPersona:
            graph.add_conditional_edges(agent.value, _route, targets)
        return graph

    async def _supervise(self, state: _GraphState) -> dict[str, Any]:
        """Decide who takes the turn next."""
        if state.get("reply") is not None:
            return {"next": FINISH}
        handoffs = state["handoffs"]
        last = state.get("last")
        if len(handoffs) >= self._settings.max_hops or (
            last is not None and state["budget"].last_call
        ):
            # No more handoffs: the last agent replies with what was done.
            assert last is not None
            return {"next": f"{last.value}:reply"}
        turn = state["turn"]
        work = state["work"]
        state["budget"].spend()
        started = time.perf_counter()
        context: dict[str, Any] = {"agents_tools": self._tools, "view": state["view"]}
        if turn.tap is not None:
            # A tap is an event like a message: who offered the button is a
            # hint, never a rule about which tool must run.
            context["button_offered_by"] = _offered_by(turn.conversation)
        if work.records:
            context["done_this_turn"] = done_so_far(work)
        if last is not None:
            context["handed_back"] = {
                "by": last.value,
                "reason": str(state.get("handed_back") or HandBackReason.DONE),
            }
        messages: list[BaseMessage] = [
            SystemMessage(SUPERVISOR),
            *earlier_summary(turn.conversation),
            *[
                HumanMessage(
                    f"[{m.agent.value if m.agent else m.role.value}] "
                    + (assistant_said(m) if m.role is ConversationRole.ASSISTANT else m.content)
                )
                for m in _recent(turn.conversation, self._settings.history_messages)
            ],
            HumanMessage(
                "Context: "
                + json.dumps(context, ensure_ascii=False, default=str)
                + f"\n\nThe customer says:\n{_said(turn)}"
            ),
        ]
        decided = await call_chat_model(self._supervisor, messages)
        state["trace"].add(TraceEvent(SUPERVISOR_ROLE, "model", "route", elapsed_ms(started)))
        chosen = _chosen(decided.tool_calls, last)
        problem = _unusable(chosen)
        # One more chance to choose, told what was wrong - never a default -
        # while the turn can still afford the choice and the chosen agent's work.
        if problem is not None and not state.get("asked_again") and state["budget"].left >= 3:
            state["budget"].spend()
            again = await call_chat_model(
                self._supervisor, [*messages, HumanMessage(_NO_CHOICE.format(problem=problem))]
            )
            chosen = _chosen(again.tool_calls, last)
            problem = _unusable(chosen)
        if problem is not None or chosen is None:
            return {**self._fallback(state), "asked_again": True}
        agent, need, finishing = chosen
        if finishing:
            return {"next": f"{agent}:reply", "need": None, "asked_again": True}
        return {"next": agent, "need": need, "asked_again": True}

    def _fallback(self, state: _GraphState) -> dict[str, Any]:
        """No usable choice: a turn nobody has worked on goes to the agent the
        customer was talking to; otherwise the agent who did the work replies
        with it."""
        last = state.get("last")
        if last is None:
            turn = state["turn"]
            agent = _offered_by_agent(turn.conversation) or AgentPersona.SARA
            return {"next": agent.value, "need": None}
        return {"next": f"{last.value}:reply", "need": None}

    def _agent_node(self, agent: AgentPersona) -> Any:
        async def node(state: _GraphState) -> dict[str, Any]:
            turn = state["turn"]
            work = state["work"]
            handoffs = [*state["handoffs"], agent]
            reply_only = state.get("next", "").endswith(":reply")
            work.working = agent
            state["trace"].add(TraceEvent(agent.value, "handoff", "enter", 0))
            if agent is AgentPersona.NORA and _planning_a_room(work):
                # A whole room is one long careful step: the room's limits.
                state["trace"].room_design = True
            # After an earlier agent changed the screen or the cart this turn,
            # the next one sees them as they are now.
            view = (
                await turn_view(turn.context.services, work.staged, turn.context)
                if work.staged is not work.pre_turn
                else state["view"]
            )
            # Planning a room, Nora reads the kinds the store stocks (names only)
            # rather than calling store_shelf: one model step fewer.
            extra = (
                await stocked_kinds(turn.context.services, turn.context.retailer)
                if agent is AgentPersona.NORA and not reply_only and _planning_a_room(work)
                else None
            )
            messages = agent_messages(
                agent_instructions(agent),
                turn.conversation,
                _said(turn),
                view,
                work,
                history=self._settings.history_messages,
                asked_for=state.get("need"),
                extra=extra,
            )
            end = await self._loop.run(
                agent,
                self._model(agent),
                messages,
                work,
                turn.context,
                state["budget"],
                state["trace"],
                only=frozenset({"respond"}) if reply_only else None,
                wording_model=self._wording.get(agent),
            )
            views = [*state["views"], view] if view is not state["view"] else state["views"]
            if isinstance(end, Responded):
                return {
                    "reply": end,
                    "handoffs": handoffs,
                    "last": agent,
                    "next": FINISH,
                    "views": views,
                }
            assert isinstance(end, HandedBack)
            passed = {
                "handoffs": handoffs,
                "last": agent,
                "handed_back": end.hand_back.reason,
                "views": views,
            }
            colleague = end.hand_back.to
            need = _NEED_FOR.get(end.hand_back.reason)
            if (
                colleague is not None
                and need is not None
                # Never straight back to who just passed it here: that is the
                # supervisor's to settle, not a loop between two agents.
                and colleague not in (agent, state.get("last"))
                and len(handoffs) < self._settings.max_hops
                and not state["budget"].last_call
            ):
                # The agent knew who comes next: straight to them, no routing
                # call. The hop limit and the turn's budget still bound it.
                state["trace"].add(TraceEvent(agent.value, "handoff", colleague.value, 0))
                return {**passed, "next": colleague.value, "need": need}
            return {**passed, "next": SUPERVISOR_ROLE}

        return node

    def _model(self, agent: AgentPersona) -> BaseChatModel:
        model = self._agents.get(agent)
        if model is None:
            raise AgentTurnIncompleteError(reason="agent not configured", agent=agent.value)
        return model


_NEED_FOR = {
    HandBackReason.NEEDS_SHOPPING: Need.SHOPPING.value,
    HandBackReason.NEEDS_DESIGN: Need.DESIGN.value,
    HandBackReason.NEEDS_SALES: Need.SALES.value,
    HandBackReason.NEEDS_SUPPORT: Need.SUPPORT.value,
}
"""What the colleague is asked for, when an agent passes the turn straight on."""


def _route(state: _GraphState) -> str:
    target = state.get("next", SUPERVISOR_ROLE)
    return target.split(":", 1)[0]


_STOPPED = (
    "The team ran out of steps before finishing this turn. Reply now with respond, to the "
    "customer: what they now have from this turn (done_this_turn), described as the "
    "result, and anything still to do offered plainly as their next step. Never narrate "
    "the work itself - what you looked at, tried, found or did not apply. Do not mention "
    "steps, tools or rules. Write figures as {key} from figures, or as the customer said "
    "them."
)

_NO_CHOICE = (
    "That choice cannot be used: {problem}. Call exactly one tool: transfer_to_<agent>, "
    "or finish naming who replies."
)


def _unusable(chosen: tuple[str, str | None, bool] | None) -> str | None:
    """Why a supervisor's choice cannot stand, or None when it can."""
    if chosen is None:
        return "no transfer or finish was called, or it named the agent who just handed back"
    return None


def _recent(conversation: ConversationContext, count: int) -> tuple[ConversationMessage, ...]:
    return conversation.messages[-count:] if count else ()


def _chosen(calls: list[Any], last: AgentPersona | None) -> tuple[str, str | None, bool] | None:
    """The agent the supervisor chose, the need it named and whether it only
    asked for a reply - or None when it chose nothing usable. The agent who
    just handed the turn back is not sent it again."""
    agents = {a.value for a in AgentPersona}
    for call in calls:
        name = str(call.get("name", ""))
        args = call.get("args") or {}
        if name == FINISH and args.get("reply_by") in agents:
            return str(args["reply_by"]), None, True
        if name.startswith("transfer_to_"):
            wanted = name.removeprefix("transfer_to_")
            if wanted in agents and (last is None or wanted != last.value):
                need = args.get("need")
                return wanted, str(need) if need in {n.value for n in Need} else None, False
    return None


def _offered_by_agent(conversation: ConversationContext) -> AgentPersona | None:
    """The agent whose reply the customer last read - the one whose buttons
    were on screen."""
    for message in reversed(conversation.messages):
        if message.role is ConversationRole.ASSISTANT:
            return message.agent
    return None


def _offered_by(conversation: ConversationContext) -> str | None:
    agent = _offered_by_agent(conversation)
    return agent.value if agent is not None else None


def _said(turn: TeamTurn) -> str:
    if turn.tap is None:
        return turn.message
    shown = turn.message.strip()
    return turn.tap.said + (f'\nThe button read: "{shown}"' if shown else "")


def _planning_a_room(work: TurnWork) -> bool:
    """A room is being planned: none is on record, or the one on record is not
    built yet."""
    room = work.staged.room_project
    return room is None or (not room.bundle_items and not room.in_cart)
