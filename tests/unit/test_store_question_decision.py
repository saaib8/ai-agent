"""`store_question`: shown to the model only where store knowledge is on.

Off, the decision step is asked exactly what it was asked before - the same
instructions and the same schema - so a deployment without the store's
documents cannot be changed by this feature at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.prompts.customer_commerce.v1 import STORE_KNOWLEDGE_SUFFIX, build_instructions
from app.schemas.agent_decision import (
    AgentAction,
    CustomerAgentDecision,
    with_store_knowledge,
)
from app.schemas.agent_turn import DecisionInput
from app.services.customer_decision import CustomerAgentDecisionService
from app.taxonomy.briefs import BriefQuestionKind
from pydantic import BaseModel, ValidationError

QUESTION = "how long does delivery to Jeddah take?"


class _FakeClient:
    def __init__(self, decision: CustomerAgentDecision) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return "decision-model-under-test"

    async def parse(self, *, instructions: str, user_input: str, schema: type[BaseModel]) -> Any:
        self.calls.append({"instructions": instructions, "schema": schema})
        return self.decision


def _transport(**fields: Any) -> CustomerAgentDecision:
    return with_store_knowledge(CustomerAgentDecision)(action=AgentAction.ANSWER, **fields)


async def test_off_the_model_is_asked_exactly_what_it_was_before() -> None:
    client = _FakeClient(CustomerAgentDecision(action=AgentAction.ANSWER))

    await CustomerAgentDecisionService(client).decide(DecisionInput(message=QUESTION))

    call = client.calls[0]
    assert call["instructions"] == build_instructions()
    assert "THE STORE ITSELF" not in call["instructions"]
    assert "store_question" not in call["schema"].model_json_schema()["properties"]


async def test_on_the_model_is_told_about_and_offered_store_question() -> None:
    client = _FakeClient(_transport(store_question=QUESTION))

    decision = await CustomerAgentDecisionService(client, store_knowledge=True).decide(
        DecisionInput(message=QUESTION)
    )

    call = client.calls[0]
    assert call["instructions"] == build_instructions(store_knowledge=True)
    assert "THE STORE ITSELF" in call["instructions"]
    assert "store_question" in call["schema"].model_json_schema()["properties"]
    assert type(decision) is CustomerAgentDecision
    assert decision.store_question == QUESTION


def test_the_prompt_version_names_the_section() -> None:
    service = CustomerAgentDecisionService(
        _FakeClient(CustomerAgentDecision(action=AgentAction.ANSWER)), store_knowledge=True
    )
    assert STORE_KNOWLEDGE_SUFFIX in service._version


def test_a_question_about_the_store_is_an_answer() -> None:
    with pytest.raises(ValidationError):
        CustomerAgentDecision(action=AgentAction.SEARCH, store_question=QUESTION)


def test_a_question_about_the_store_narrows_nothing_on_screen() -> None:
    with pytest.raises(ValidationError):
        CustomerAgentDecision(
            action=AgentAction.ANSWER,
            store_question=QUESTION,
            narrow_by=(BriefQuestionKind.BUDGET,),
        )


def test_a_restated_question_is_bounded() -> None:
    with pytest.raises(ValidationError):
        CustomerAgentDecision(action=AgentAction.ANSWER, store_question="x" * 301)
