"""Whether a figure in generated prose was allowed to be there.

The guard asks one question - *did this number come from an approved
non-product source?* - and the tests are mostly about what it deliberately does
NOT do. It performs no arithmetic, so a customer who said "20% cheaper" has not
licensed "80%"; it converts no currencies or units; it looks nothing up.

It can be this small because the response model never receives a product fact.
"""

from __future__ import annotations

import pytest
from app.services.numeric_guard import (
    build_allowance,
    canonical_number,
    check_numeric_policy,
    numbers_in,
)


def _check(prose: str, allowance: frozenset[str]) -> str | None:
    violation = check_numeric_policy(
        message=prose, follow_up_question=None, allowance=allowance
    )
    return violation.value if violation else None


# ── normalization ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("3000", "3000"),
        ("3,000", "3000"),
        ("3000.0", "3000"),
        ("3000.00", "3000"),
        ("3,000.00", "3000"),
        ("20", "20"),
        ("2.5", "2.5"),
        ("2.50", "2.5"),
        ("0.10", "0.1"),
        ("+3000", "3000"),
        ("-3000", "3000"),
        ("٣٠٠٠", "3000"),
    ],
    ids=lambda v: v,
)
def test_equal_values_normalise_equal(raw: str, canonical: str) -> None:
    assert canonical_number(raw) == canonical


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [("1st", "1"), ("2nd", "2"), ("3rd", "3"), ("4th", "4"), ("11th", "11")],
)
def test_ordinal_suffixes_normalise_to_their_number(raw: str, canonical: str) -> None:
    assert canonical_number(raw) == canonical


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("20%", {"20"}),
        ("SAR 3,000", {"3000"}),
        ("220cm", {"220"}),
        ("$3000", {"3000"}),
    ],
)
def test_a_wrapper_does_not_change_the_value(text: str, expected: set[str]) -> None:
    """A percent sign, a currency or a unit is not part of the number."""
    assert numbers_in(text) == expected


def test_a_range_dash_is_not_a_minus_sign() -> None:
    """Reading "3,000-4,000" as one negative figure would hide 4,000 from the
    check entirely - which is why the sign is dropped rather than parsed."""
    assert numbers_in("between 3,000-4,000") == {"3000", "4000"}


def test_a_sentence_final_number_is_still_found() -> None:
    assert numbers_in("I found 3 options.") == {"3"}


@pytest.mark.parametrize("text", ["no numbers here", "", "..."])
def test_text_without_numbers_yields_nothing(text: str) -> None:
    assert numbers_in(text) == frozenset()


def test_nothing_is_converted() -> None:
    """No FX, no units, no arithmetic - there is no such machinery."""
    assert canonical_number("3000") != canonical_number("3000.01")
    assert numbers_in("2 m") == {"2"}
    assert numbers_in("200 cm") == {"200"}


# ── the allowance ───────────────────────────────────────────────────────────


def test_the_customers_own_figure_is_allowed() -> None:
    allowance = build_allowance("under SAR 3,000")

    assert _check("that fits your 3000 budget", allowance) is None
    assert _check("that fits your 3,000 budget", allowance) is None


def test_a_nearby_figure_they_never_said_is_rejected() -> None:
    allowance = build_allowance("under SAR 3,000")

    assert _check("I found some around 3,500", allowance) == "3500"


def test_a_percentage_they_stated_is_allowed() -> None:
    allowance = build_allowance("make it 20% cheaper")

    assert _check("about 20% less", allowance) is None


def test_a_derivable_percentage_is_still_rejected() -> None:
    """80 follows from 20 arithmetically. That is exactly the reasoning this
    layer must not do - real prices belong to services that read them."""
    allowance = build_allowance("make it 20% cheaper")

    assert _check("that is 80% of the price", allowance) == "80"


def test_the_presented_count_is_allowed() -> None:
    allowance = build_allowance("show me sofas", presented_count=3)

    assert _check("I found 3 options", allowance) is None


def test_a_count_that_was_not_established_is_rejected() -> None:
    allowance = build_allowance("show me sofas", presented_count=3)

    assert _check("I found 4 options", allowance) == "4"


def test_an_ordinal_within_the_visible_range_is_allowed() -> None:
    allowance = build_allowance("show me sofas", presented_count=3)

    assert _check("the 2nd option suits a small room", allowance) is None
    assert _check("the second entry", allowance) is None, "words are not scanned"


def test_an_ordinal_beyond_the_visible_range_is_rejected() -> None:
    allowance = build_allowance("show me sofas", presented_count=3)

    assert _check("the 4th option", allowance) == "4"


def test_a_comparison_count_is_allowed() -> None:
    allowance = build_allowance("compare these", compared_count=2)

    assert _check("Both of the 2 differ mainly on width", allowance) is None


def test_no_count_means_no_numbers_at_all() -> None:
    allowance = build_allowance("tell me about it")

    assert allowance == frozenset()
    assert _check("there are 3", allowance) == "3"


@pytest.mark.parametrize(
    "product_number",
    ["1299", "220", "4", "165645"],
    ids=["price", "dimension", "capacity", "product id"],
)
def test_no_product_figure_is_ever_admitted(product_number: str) -> None:
    """The allowance is built from the message and the counts. A price the
    application knows is not a number the model may state."""
    allowance = build_allowance("show me sofas", presented_count=2)

    assert product_number not in allowance or product_number in {"1", "2"}


def test_history_does_not_widen_the_allowance() -> None:
    """A figure mentioned three turns ago is not what they are asking now."""
    allowance = build_allowance("show me something else")

    assert _check("under your 3000 budget", allowance) == "3000"


# ── the scanned fields ──────────────────────────────────────────────────────


def test_the_follow_up_question_is_scanned_too() -> None:
    allowance = build_allowance("show me sofas", presented_count=2)

    violation = check_numeric_policy(
        message="Here are some options.",
        follow_up_question="Would you like to see something under 5000?",
        allowance=allowance,
    )

    assert violation is not None
    assert violation.field == "follow_up_question"
    assert violation.value == "5000"


def test_an_absent_follow_up_is_not_scanned() -> None:
    allowance = build_allowance("show me sofas")

    assert (
        check_numeric_policy(
            message="Here are some options.",
            follow_up_question=None,
            allowance=allowance,
        )
        is None
    )


def test_the_violation_names_the_field_and_the_value() -> None:
    violation = check_numeric_policy(
        message="around 4,200", follow_up_question=None, allowance=frozenset()
    )

    assert violation is not None
    assert violation.field == "message"
    assert violation.value == "4200"


# ── the guard is pure ───────────────────────────────────────────────────────


def test_the_guard_is_pure() -> None:
    """No model, no repository, no network, no conversion."""
    import ast
    from pathlib import Path

    source = (Path(__file__).parents[2] / "app/services/numeric_guard.py").read_text()
    tree = ast.parse(source)

    assert not any(isinstance(node, ast.Await) for node in ast.walk(tree))
    for forbidden in ("Repository", "client", "httpx", "requests", "datetime", "random"):
        assert forbidden not in source, forbidden


def test_the_guard_is_deterministic() -> None:
    allowance = build_allowance("under 3,000", presented_count=3)
    prose = "Here are 3 options within your 3000 budget."

    results = {_check(prose, allowance) for _ in range(5)}

    assert results == {None}


# ── a figure the customer typed earlier is still theirs ─────────────────────


def test_a_number_from_an_earlier_customer_turn_is_allowed() -> None:
    """The live failure: "5x5" then, after a unit question, "m".

    The reply could say nothing about a 5 by 5 room, because the only message
    in scope was the word "m" - so the answer fell back to an apology while
    the specialist's answer was sitting right there (M16 6).
    """
    allowance = build_allowance("m", said_earlier=("5x5", "how big should a rug be?"))

    assert "5" in allowance


def test_a_number_the_assistant_said_is_not_allowed() -> None:
    """Their words only. Admitting the assistant's would let a figure the
    model invented become an approved source one turn later - the laundering
    this guard exists to prevent."""
    allowance = build_allowance("m", said_earlier=("5x5",))

    assert "5" in allowance
    assert "30" not in allowance, "an assistant's 15-30 cm is not their figure"


def test_the_helper_takes_only_the_customers_turns() -> None:
    from app.schemas.agent_turn import CustomerTurnInput
    from app.schemas.conversation import (
        ConversationContext,
        ConversationMessage,
        ConversationRole,
    )
    from app.schemas.retailer import RetailerContext
    from app.services.response_generator import _their_own_words

    turn = CustomerTurnInput(
        message="m",
        conversation=ConversationContext(
            messages=(
                ConversationMessage(role=ConversationRole.USER, content="5x5"),
                ConversationMessage(
                    role=ConversationRole.ASSISTANT, content="about 15-30 cm"
                ),
            )
        ),
        context=RetailerContext(store_id=50),
    )

    assert _their_own_words(turn) == ("5x5",)


def test_nothing_else_was_widened() -> None:
    """A figure from nowhere is still a figure from nowhere."""
    allowance = build_allowance("m", said_earlier=("5x5",))

    assert "4299" not in allowance
    assert "12000" not in allowance
