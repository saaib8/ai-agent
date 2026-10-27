"""One agent's hop: model, tools, model ... until it replies or hands back.

The loop, not the model, decides what runs:

* **The allowlist is enforced here, before a call is read.** A tool another
  agent owns is answered "not permitted" and never runs, whatever the model
  asked for.
* **Tools run one at a time**, in the order asked, against the one staged
  state every agent shares - so the second call sees what the first changed.
* **Arguments that break a tool's contract get one correction per turn**: the
  model is told which rules it broke, in our words and never its own values.
  A second broken call ends the turn as unusable output.
* **Every turn has a budget** of model calls shared by every agent, and every
  hop a budget of tools. Past either, the hop ends; the team then has the agent
  word what was done (CLAUDE.md 17.1).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import JsonValue, ValidationError

from app.agents.context import ToolContext
from app.agents.figures import (
    figure_table,
    flatten,
    render,
    render_reply,
    root_of,
    shown,
    without_references,
)
from app.agents.tools.base import ToolKind, ZoryTool
from app.agents.tools.common import HandBackArgs, RespondArgs
from app.agents.tools.registry import ToolRegistry
from app.agents.trace import TraceEvent, TurnTrace, elapsed_ms
from app.agents.work import ToolRecord, ToolStatus, TurnWork
from app.core.exceptions import (
    AgentTurnIncompleteError,
    ConfigurationError,
    InvalidRequestError,
    TaxonomyValidationError,
    ZoryError,
)
from app.core.logging import get_logger
from app.integrations.chat_models import call_chat_model
from app.integrations.llm import schema_violations
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_turn import MAX_RESPONSE_CHARS

UNKNOWN_TOOL = "<unknown>"
"""What a tool name the model made up is logged as: its text is the model's."""

ROOM_FIXES = frozenset({"revise_room_piece", "remove_room_piece", "change_room"})
"""The calls that put a built room right; after them, only its reply is left
(`agents.<agent>.wording_reasoning_effort`)."""


@dataclass(frozen=True, slots=True)
class Responded:
    agent: AgentPersona
    reply: RespondArgs


@dataclass(frozen=True, slots=True)
class HandedBack:
    agent: AgentPersona
    hand_back: HandBackArgs


HopEnd = Responded | HandedBack


@dataclass(slots=True)
class TurnBudget:
    """What one turn may spend, across the supervisor and every agent."""

    max_model_calls: int
    model_calls: int = 0
    corrected: bool = False
    """Whether the turn's one correction of broken arguments is spent."""

    @property
    def left(self) -> int:
        return self.max_model_calls - self.model_calls

    @property
    def last_call(self) -> bool:
        """Whether only one model call is left - the one the reply needs."""
        return self.max_model_calls - self.model_calls <= 1

    def spend(self) -> None:
        if self.model_calls >= self.max_model_calls:
            raise AgentTurnIncompleteError(reason="model call budget spent")
        self.model_calls += 1

    def correct(self, **context: Any) -> None:
        if self.corrected:
            raise AgentTurnIncompleteError(reason="arguments invalid twice", **context)
        self.corrected = True


def json_facts(facts: Mapping[str, Any] | None) -> dict[str, JsonValue]:
    """Facts as plain JSON, once, so the model and the record see the same
    thing - a Decimal price becomes the string it is shown as."""
    if not facts:
        return {}
    loaded: dict[str, JsonValue] = json.loads(json.dumps(facts, ensure_ascii=False, default=str))
    return loaded


logger = get_logger(__name__)


class AgentLoop:
    def __init__(self, registry: ToolRegistry, *, max_tool_calls: int) -> None:
        self._registry = registry
        self._max_tool_calls = max_tool_calls

    def tools_for(self, agent: AgentPersona) -> tuple[ZoryTool[Any], ...]:
        return self._registry.for_agent(agent)

    async def run(
        self,
        agent: AgentPersona,
        model: BaseChatModel,
        messages: Sequence[BaseMessage],
        work: TurnWork,
        context: ToolContext,
        budget: TurnBudget,
        trace: TurnTrace,
        *,
        only: frozenset[str] | None = None,
        wording_model: BaseChatModel | None = None,
    ) -> HopEnd:
        """Run the agent until it replies or hands back.

        `only` narrows what is offered - a tap's owner is offered `respond`
        alone - but never widens it past the agent's allowlist. Once a room
        built this turn has had its fixes (`ROOM_FIXES`), the calls left in the
        hop - the reply, and any record kept beside it - run on `wording_model`
        when one is given; the instructions allow one round of fixes, so no
        design work is left for it.
        """
        tools = [t for t in self.tools_for(agent) if only is None or t.name in only]
        if not tools:
            raise ConfigurationError(reason="an agent hop offered no tools", agent=agent.value)
        bound = _bind(model, tools)
        offered = frozenset(t.name for t in tools)
        exchange: list[BaseMessage] = list(messages)
        tools_run = 0

        while True:
            if budget.last_call and len(tools) > 1:
                # The turn's last call is the reply's: whatever was done stands.
                tools = [t for t in tools if t.kind is ToolKind.RESPOND]
                if not tools:
                    raise AgentTurnIncompleteError(reason="model call budget spent")
                bound = _bind(model, tools)
                offered = frozenset(t.name for t in tools)
            budget.spend()
            started = time.perf_counter()
            reply = await call_chat_model(bound, exchange)
            trace.add(_model_event(agent, reply, started, model))
            exchange.append(reply)

            if reply.invalid_tool_calls:
                # Arguments that were not even JSON: the same one correction as
                # arguments that broke a rule. Each is answered, or the provider
                # refuses the next call for an unanswered tool call.
                budget.correct(agent=agent.value, reason_detail="unreadable arguments")
                for bad in reply.invalid_tool_calls:
                    exchange.append(
                        _answer(_call_id(bad.get("id")), ToolStatus.INVALID, "arguments_unreadable")
                    )
            if not reply.tool_calls:
                if reply.invalid_tool_calls:
                    continue
                # Words with no call are the reply itself.
                text = reply.text.strip()
                if not text:
                    raise AgentTurnIncompleteError(reason="empty step", agent=agent.value)
                plain, unknown = render(text, figure_table(work))
                if unknown:
                    # No call to correct: a sentence whose reference names no
                    # figure goes, rather than reach the customer as braces.
                    plain = without_references(plain)
                if not plain:
                    raise AgentTurnIncompleteError(reason="empty step", agent=agent.value)
                return Responded(agent, RespondArgs(message=plain[:MAX_RESPONSE_CHARS]))

            ended: HopEnd | None = None
            # Several calls in one step run in order, each on the state the
            # last left. A reply or a hand-back is written only once the
            # step's other results are read: sent beside them, it is answered
            # after them and asked for again.
            acting = any(
                (t := self._registry.get(c["name"])) is not None
                and t.kind not in (ToolKind.RESPOND, ToolKind.HAND_BACK)
                and not t.beside_reply
                for c in reply.tool_calls
            )
            fixed = False
            # A record kept beside the reply that did not land holds the reply
            # back, so the agent sees why rather than replying as if it had.
            beside_failed = False
            done_this_step: list[JsonValue] = []
            # A record kept beside the reply runs before it, whatever order
            # the model wrote them in.
            for call in sorted(reply.tool_calls, key=self._ends_the_hop):
                call_id = _call_id(call.get("id"))
                name = call["name"]
                if ended is not None:
                    exchange.append(_answer(call_id, ToolStatus.REFUSED, "after_reply"))
                    continue
                tool = self._registry.get(name)
                if tool is None or name not in offered:
                    logged = name if tool is not None else UNKNOWN_TOOL
                    work.record(
                        ToolRecord(
                            agent=agent,
                            tool=logged,
                            status=ToolStatus.REFUSED,
                            code="not_permitted",
                        )
                    )
                    trace.add(
                        TraceEvent(agent.value, "tool", logged, 0, "refused", "not_permitted")
                    )
                    exchange.append(_answer(call_id, ToolStatus.REFUSED, "not_permitted"))
                    continue
                try:
                    args = tool.args.model_validate(call["args"])
                except ValidationError as exc:
                    violations = schema_violations(exc)
                    # The rules broken, never the values (CLAUDE.md 21.1, 22).
                    logger.warning(
                        "agent_tool_arguments_invalid",
                        agent=agent.value,
                        tool=name,
                        rules=list(violations),
                    )
                    budget.correct(agent=agent.value, tool=name, violations=violations)
                    beside_failed = beside_failed or tool.beside_reply
                    trace.add(TraceEvent(agent.value, "tool", name, 0, "invalid"))
                    exchange.append(
                        _answer(
                            call_id,
                            ToolStatus.INVALID,
                            "arguments_invalid",
                            {"rules_broken": list(violations)},
                        )
                    )
                    continue

                if (acting or beside_failed) and tool.kind in (
                    ToolKind.RESPOND,
                    ToolKind.HAND_BACK,
                ):
                    exchange.append(_answer(call_id, ToolStatus.REFUSED, "read_the_results_first"))
                    continue
                if tool.kind is ToolKind.RESPOND:
                    assert isinstance(args, RespondArgs)
                    written, rules = render_reply(args, figure_table(work))
                    if rules:
                        # A key that names no figure, or chips the figures made
                        # alike: told which, corrected in this hop - never sent
                        # as braces or as a guess.
                        budget.correct(agent=agent.value, reason_detail="reply figures")
                        trace.add(TraceEvent(agent.value, "tool", name, 0, "invalid"))
                        exchange.append(
                            _answer(
                                call_id,
                                ToolStatus.INVALID,
                                "reply_figures",
                                {
                                    "rules_broken": list(rules),
                                    # Not to be sent again: they already ran.
                                    **({"done_already": done_this_step} if done_this_step else {}),
                                },
                            )
                        )
                        continue
                    ended = Responded(agent, written)
                    exchange.append(_answer(call_id, ToolStatus.OK))
                    continue
                if tool.kind is ToolKind.HAND_BACK:
                    assert isinstance(args, HandBackArgs)
                    ended = HandedBack(agent, args)
                    exchange.append(_answer(call_id, ToolStatus.OK))
                    continue

                if tools_run >= self._max_tool_calls:
                    trace.add(TraceEvent(agent.value, "tool", name, 0, "refused", "tool_budget"))
                    exchange.append(_answer(call_id, ToolStatus.REFUSED, "tool_budget"))
                    continue
                tools_run += 1
                exchange.append(
                    await self.execute(agent, tool, args, call_id, work, context, trace)
                )
                landed = work.records[-1].status is ToolStatus.OK
                fixed = fixed or (name in ROOM_FIXES and landed)
                beside_failed = beside_failed or (tool.beside_reply and not landed)
                done_this_step.append(name)

            if ended is not None:
                return ended
            if fixed and wording_model is not None and trace.room_design:
                # The room is built and put right: what is left is saying it,
                # which needs less thought than designing it did.
                model = wording_model
                bound = _bind(model, tools)

    def _ends_the_hop(self, call: Mapping[str, Any]) -> bool:
        tool = self._registry.get(call["name"])
        return tool is not None and tool.kind in (ToolKind.RESPOND, ToolKind.HAND_BACK)

    async def execute(
        self,
        agent: AgentPersona,
        tool: ZoryTool[Any],
        args: Any,
        call_id: str,
        work: TurnWork,
        context: ToolContext,
        trace: TurnTrace,
    ) -> ToolMessage:
        """Run one tool and record it. Also how a screen tap runs its tool,
        so tapping and asking in words execute the same code."""
        assert tool.handler is not None
        started = time.perf_counter()
        try:
            outcome = await tool.handler(args, context, work)
        except (InvalidRequestError, TaxonomyValidationError) as exc:
            # A rule said no: the model is told which, by its code, and what was
            # wrong and what would be right - so it corrects itself rather than
            # guessing again. Outages and defects are not caught - they are not
            # the model's to recover.
            why = refusal_facts(exc, context)
            work.record(
                ToolRecord(agent=agent, tool=tool.name, status=ToolStatus.REFUSED, code=exc.code)
            )
            trace.add(
                TraceEvent(agent.value, "tool", tool.name, elapsed_ms(started), "refused", exc.code)
            )
            return _answer(call_id, ToolStatus.REFUSED, exc.code, why)
        facts = json_facts(outcome.facts)
        record = ToolRecord(
            agent=agent,
            tool=tool.name,
            status=outcome.status,
            code=outcome.code,
            facts=facts,
            payload=outcome.payload,
            acted=outcome.payload is not None or outcome.state is not None,
        )
        work.record(record, outcome.state)
        trace.add(
            TraceEvent(
                agent.value,
                "tool",
                tool.name,
                elapsed_ms(started),
                outcome.status.value,
                outcome.code,
            )
        )
        root = root_of(work.records, record)
        figures = shown(flatten(facts, root)) if root is not None else {}
        return _answer(call_id, outcome.status, outcome.code, facts, figures)


def _bind(model: BaseChatModel, tools: Sequence[ZoryTool[Any]]) -> Any:
    """The model offered these tools - several in one step when they do not
    depend on each other, run in order - never in strict mode: the arguments
    are validated here, with one correction."""
    return model.bind_tools(
        [t.openai_schema() for t in tools],
        parallel_tool_calls=len(tools) > 1,
        strict=False,
        **({"tool_choice": tools[0].name} if len(tools) == 1 else {}),
    )


def _call_id(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise AgentTurnIncompleteError(reason="tool call without an id")
    return raw


def _answer(
    call_id: str,
    status: ToolStatus,
    code: str | None = None,
    facts: Mapping[str, JsonValue] | None = None,
    figures: Mapping[str, str] | None = None,
) -> ToolMessage:
    body: dict[str, Any] = {"status": status.value}
    if code is not None:
        body["code"] = code
    if facts:
        body["facts"] = dict(facts)
    if figures:
        # The keys to write these facts' figures by: {key} in a reply.
        body["figures"] = dict(figures)
    return ToolMessage(content=json.dumps(body, ensure_ascii=False), tool_call_id=call_id)


def _model_event(
    agent: AgentPersona, reply: AIMessage, started: float, model: BaseChatModel
) -> TraceEvent:
    usage = reply.usage_metadata
    answered = (reply.response_metadata or {}).get("model_name")
    return TraceEvent(
        agent.value,
        "model",
        "step",
        elapsed_ms(started),
        input_tokens=usage["input_tokens"] if usage else None,
        output_tokens=usage["output_tokens"] if usage else None,
        model=answered if isinstance(answered, str) else getattr(model, "model_name", None),
        reasoning=getattr(model, "reasoning_effort", None),
    )


_SAFE_DETAILS = frozenset(
    {"category", "subcategory", "role", "family", "attribute", "value", "field", "kind", "unit"}
)
"""What of a refusal's detail an agent may read: the values it sent and what
was wrong with them - never an id, a query or anything internal (CLAUDE.md
20.5)."""


def refusal_facts(exc: ZoryError, context: ToolContext) -> dict[str, JsonValue]:
    """Why a rule refused the call, and what would be right where the
    registry knows: a nightstand asked for under bedroom is told it belongs to
    tables."""
    facts: dict[str, JsonValue] = {
        key: value
        for key, value in exc.context.items()
        if key in _SAFE_DETAILS and isinstance(value, str | int) and not isinstance(value, bool)
    }
    subcategory = facts.get("subcategory")
    taxonomy = getattr(context.services, "taxonomy", None)
    if isinstance(subcategory, str) and taxonomy is not None:
        family = taxonomy.category_of(subcategory)
        if family is not None:
            facts["belongs_to"] = family
        else:
            facts["not_a_product_type_we_know"] = subcategory
    return facts
