"""Store questions about a piece, and store questions asked beside a search.

"What is the warranty on the first one?" is the store's policy for a piece,
asked by its position: the decision restates it with the piece's kind and it
is looked up like any store question. "Show me beds, and is assembly free?"
answers the store question and offers the beds next, one tap away, as the
customer's own words - never a search the reply ignores.
"""

from __future__ import annotations

import pytest
from app.prompts.customer_commerce.v1 import STORE_KNOWLEDGE_SUFFIX, build_instructions
from app.schemas.agent_decision import AgentAction, CustomerAgentDecision, with_store_knowledge
from app.schemas.next_step import NextStepKind
from app.services.chat_runtime import ChatRuntime
from pydantic import ValidationError

from tests.unit.test_store_info_turn import FakeStoreKnowledge
from tests.unit.test_support_turn import FakeSupport
from tests.unit.test_turn_coordinator import _coordinator, _state, _turn

ASSEMBLY = "is assembly free for beds?"


def _answers(
    question: str = ASSEMBLY, then_search: str | None = "show me beds"
) -> CustomerAgentDecision:
    return CustomerAgentDecision(
        action=AgentAction.ANSWER, policy_question=question, then_search=then_search
    )


def test_a_search_offered_after_an_answer_needs_the_store_question() -> None:
    with pytest.raises(ValidationError, match="needs the store question"):
        CustomerAgentDecision(action=AgentAction.ANSWER, then_search="show me beds")


def test_the_model_is_shown_then_search_only_with_store_knowledge() -> None:
    assert (
        "then_search"
        in with_store_knowledge(CustomerAgentDecision).model_json_schema()["properties"]
    )
    assert "then_search" not in CustomerAgentDecision.model_json_schema()["properties"]


async def test_the_store_question_is_answered_and_the_products_are_offered_next() -> None:
    knowledge = FakeStoreKnowledge()
    coordinator, _ = _coordinator(_answers(), store_knowledge=knowledge, support=FakeSupport())

    result = await coordinator.run(_turn(_state(), "show me beds, and is assembly free?"))

    assert [question for _, question in knowledge.calls] == [ASSEMBLY]
    assert result.grounding.search is None
    assert result.next_step is not None
    assert result.next_step.kind is NextStepKind.AFTER_STORE_ANSWER
    assert [(c.label, c.value) for c in result.next_step.chips] == [
        ("show me beds", "show me beds")
    ]
    presentation = ChatRuntime.presentation(result)
    assert presentation is not None
    assert [c.value for c in presentation.choices] == ["show me beds"]


async def test_a_store_answer_alone_still_ends_on_the_answer() -> None:
    coordinator, _ = _coordinator(
        _answers(then_search=None), store_knowledge=FakeStoreKnowledge(), support=FakeSupport()
    )

    result = await coordinator.run(_turn(_state(), "is assembly free for beds?"))

    assert result.next_step is None


def test_the_prompt_reads_a_piece_policy_as_a_store_question() -> None:
    prompt = " ".join(build_instructions(store_knowledge=True).split())

    assert '"what is the warranty on the first one?"' in prompt
    assert "restated with the piece's kind, never its position" in prompt
    assert "then_search:" in prompt
    assert STORE_KNOWLEDGE_SUFFIX == "+store-knowledge.2"
    assert "then_search" not in build_instructions()
