""" "How wide is the spot where it will go?" is not asked after products
(`customer_agent.designer_space_question`, off by default; CLAUDE.md 10.9).

Off, the taste question after a list goes straight to the next one; on, it is
asked first, exactly as before. A space the customer gives is still used.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.config import CustomerAgentSettings
from app.schemas.agent_state import ActiveSearchState, AgentStateV1
from app.schemas.product_brief import BriefMode
from app.schemas.query import RankingLean
from app.schemas.taste import TasteQuestionKind, TasteState
from app.services.taste_question import choose_question
from app.taxonomy.briefs import BriefQuestionKind

from tests.unit.test_designer_taste import STYLES, _cards
from tests.unit.test_product_brief import _asked, _builder, _need
from tests.unit.test_turn_coordinator import CONTEXT, SOFAS


def _sofas_with_no_space() -> ActiveSearchState:
    """A sofa search with no wall or width known: the space question's case."""
    return ActiveSearchState(request=SOFAS, revision=1, lean=RankingLean(learned=True))


def test_the_space_question_is_off_by_default() -> None:
    assert CustomerAgentSettings().designer_space_question is False


def test_off_the_next_taste_question_is_asked_instead() -> None:
    pending = choose_question(
        _cards(), _sofas_with_no_space(), (), STYLES, TasteState(), list_revision=1, ask_space=False
    )

    assert pending is not None
    assert pending.kind is not TasteQuestionKind.SPACE
    assert pending.kind is TasteQuestionKind.WHICH


def test_on_it_is_asked_first_as_before() -> None:
    pending = choose_question(
        _cards(), _sofas_with_no_space(), (), STYLES, TasteState(), list_revision=1
    )

    assert pending is not None and pending.kind is TasteQuestionKind.SPACE


# ── "How wide a space?" on the cards ────────────────────────────────────────


def _builder_without_space() -> Any:
    from typing import cast

    from app.repositories.products import ProductRepository
    from app.services.product_brief import ProductBriefBuilder

    from tests.unit.test_product_brief import ATTRIBUTES, BRIEFS, SOFA_FACTS, TAXONOMY, FakeFacts

    return ProductBriefBuilder(
        cast(ProductRepository, FakeFacts(SOFA_FACTS)),
        BRIEFS,
        ATTRIBUTES,
        TAXONOMY,
        ask_space=False,
    )


@pytest.mark.parametrize(
    "card",
    [
        {"mode": BriefMode.ASK},
        {"mode": BriefMode.NARROW, "prefilled": True},
        {"mode": BriefMode.ASK, "opening": True},
    ],
    ids=["card", "narrow down", "opening"],
)
async def test_off_no_card_asks_how_wide_a_space(card: dict[str, Any]) -> None:
    built = await _builder_without_space().build(_need(), AgentStateV1(), CONTEXT, **card)

    assert built is not None
    assert BriefQuestionKind.SPACE not in _asked(built)


async def test_narrow_by_size_alone_offers_the_rest_rather_than_nothing() -> None:
    """ "Narrow by size" with the space question off: nothing is left to ask
    by that, so the coordinator opens the whole card instead."""
    built = await _builder_without_space().build(
        _need(),
        AgentStateV1(),
        CONTEXT,
        mode=BriefMode.NARROW,
        prefilled=True,
        only=(BriefQuestionKind.SPACE,),
    )

    assert built is None


async def test_on_the_cards_ask_it_as_before() -> None:
    builder, _ = _builder()

    built = await builder.build(
        _need(), AgentStateV1(), CONTEXT, mode=BriefMode.NARROW, prefilled=True
    )

    assert built is not None
    assert BriefQuestionKind.SPACE in _asked(built)
