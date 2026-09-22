"""Whether a number in generated prose came from somewhere it was allowed to.

One narrow question, and deliberately only one: *did this figure come from an
approved non-product source?* It is not a fact checker, not a unit or currency
converter, not a semantic validator and not a second model. It compares
canonical numeric tokens, and nothing else.

The reason it can be this small is that the response model never receives a
product fact. Prices, dimensions and capacities are rendered by the application
from verified grounding, so a figure the model produced has no legitimate
catalog origin - the only numbers it may echo are ones the customer just said,
or counts the application supplied.

**Provenance, not arithmetic.** A customer who said "20% cheaper" licenses
"20%", not "80%" - even though one follows from the other. Deriving a figure is
commerce arithmetic, which belongs to services that read real prices
(CLAUDE.md 3.3).

The screen-awareness pass widened *what has a source*, not what the rule is.
The response model now reads the cards the customer is looking at, so a price,
a capacity or a measurement printed on one of them has an approved origin and
may be repeated. Everything the application did not put on screen is still
unsourced: a saving, a percentage, a difference between two prices, a running
total, a remaining budget. Repeating is allowed; computing never was
(CLAUDE.md 14).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.schemas.design import DesignGuidance
from app.schemas.response import BundleGroundingView
from app.schemas.screen import CustomerVisibleScreenView

ResponseNumericAllowance = frozenset[str]
"""Canonical numeric tokens generated prose may contain.

A plain immutable set rather than a schema: it carries no structure worth
validating, and it is application-only - nothing builds it into a prompt.
"""

_NUMERIC_TOKEN = re.compile(r"\d[\d,.]*")
"""A run beginning with a digit, allowing separators inside it.

Deliberately loose at the edges: trailing punctuation is trimmed afterwards, so
a sentence-final "3 options." yields 3 rather than being missed.
"""

_ORDINAL_SUFFIX = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)

_THOUSANDS = str.maketrans({",": ""})
"""The group separator. Only the ASCII comma: M11 V1 is English, and
admitting other separators would mean deciding which of them group and
which divide. Unicode *digits* still normalise: `Decimal` reads them."""


def canonical_number(raw: str) -> str | None:
    """One numeric literal as a canonical decimal string, or None.

    Equal values normalise equal: `3,000`, `3000.0` and `3000.00` all become
    `3000`, and Unicode decimal digits normalise to ASCII. A sign is dropped,
    because a hyphen in
    prose is far more often a range dash than a minus, and treating "3,000-4,000"
    as a single negative figure would hide a real number from the check.

    A percent sign, a currency code or a unit is not part of the number, so
    `20%`, `SAR 3,000` and `220cm` carry the same values as `20`, `3000`, `220`.
    """
    text = raw.strip().lstrip("+-")
    text = _ORDINAL_SUFFIX.sub("", text)
    text = text.translate(_THOUSANDS)
    text = text.strip(".")
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    value = value.normalize()
    if value == value.to_integral_value():
        value = value.to_integral_value()
    return format(value, "f")


def numbers_in(text: str) -> frozenset[str]:
    """Every canonical numeric value the text contains.

    Ordinal suffixes are stripped first so "the 2nd option" yields 2, matching
    how the allowance counts positions.
    """
    without_ordinals = _ORDINAL_SUFFIX.sub("", text)
    found = {
        canonical
        for token in _NUMERIC_TOKEN.findall(without_ordinals)
        if (canonical := canonical_number(token)) is not None
    }
    return frozenset(found)


def bundle_counts(bundle: BundleGroundingView) -> tuple[int, ...]:
    """The counts a whole-room reply may state, named one by one.

    Extracted field by field on purpose. "Every integer on the view" would mean
    that adding a numeric field later silently widened what the model is
    allowed to assert, and the next such field might be a price.

    Per-product quantities are **not** here. The model never learns which piece
    a quantity belongs to, so an isolated "four" would be a factual claim with
    no subject. Quantities are rendered beside the prose, against the product
    they describe.
    """
    return (
        bundle.bundle_line_count,
        bundle.locked_line_count,
        bundle.already_owned_line_count,
        bundle.required_unmet_count,
        bundle.recommended_unmet_count,
        bundle.optional_unmet_count,
        bundle.relaxed_line_count,
    )


def screen_figures(screen: CustomerVisibleScreenView) -> tuple[Decimal | int, ...]:
    """Every figure the customer can read off the current screen.

    Field by field, for the same reason :func:`bundle_counts` is: "every number
    on the view" would mean a field added later silently widened what prose may
    assert, and the next such field might be a margin.

    Room figures are included because the application computed and rendered
    them. The budget maximum is here for the same reason - it is printed beside
    the total - while `within_budget` is a verdict rather than a figure and has
    no number to license.
    """
    figures: list[Decimal | int] = []
    for card in screen.products:
        if card.price_amount is not None:
            figures.append(card.price_amount)
        if card.seating_capacity is not None:
            figures.append(card.seating_capacity)
        if card.dimensions is not None:
            figures.extend(
                value
                for value in (
                    card.dimensions.length_cm,
                    card.dimensions.width_cm,
                    card.dimensions.height_cm,
                )
                if value is not None
            )

    if screen.comparison is not None:
        # Cells are strings the application rendered, so a cell reading
        # "220 cm" licenses 220. Parsed rather than trusted wholesale: a cell
        # the comparison could not establish carries no value to license.
        for row in screen.comparison.rows:
            for cell in row.cells:
                if cell.value is None:
                    continue
                figures.extend(
                    Decimal(number)
                    for token in _NUMERIC_TOKEN.findall(cell.value)
                    if (number := canonical_number(token)) is not None
                )

    room = screen.room
    if room is not None:
        for line in room.cards:
            figures.append(line.quantity)
            if line.unit_price is not None:
                figures.append(line.unit_price)
            if line.new_spend_line_total is not None:
                figures.append(line.new_spend_line_total)
        if room.new_spend_total is not None:
            figures.append(room.new_spend_total)
        if room.budget_max_amount is not None:
            figures.append(room.budget_max_amount)
    return tuple(figures)


def guidance_figures(guidance: Sequence[DesignGuidance]) -> tuple[Decimal, ...]:
    """Measurements the design specialist supplied as general guidance.

    Sayable because the specialist produced them as structured measurements
    rather than in prose - which is exactly why `DesignGuidance.summary`
    forbids digits. A rule of thumb about rug sizing is design knowledge, not a
    claim about any product (CLAUDE.md 41).
    """
    return tuple(
        bound
        for item in guidance
        for measurement in item.measurements
        for bound in (measurement.minimum, measurement.maximum)
        if bound is not None
    )


def build_allowance(
    message: str,
    *,
    presented_count: int = 0,
    compared_count: int = 0,
    counts: Sequence[int] = (),
    figures: Sequence[Decimal | int] = (),
) -> ResponseNumericAllowance:
    """What this turn's prose is permitted to say in figures.

    Three sources, and no others:

    * numbers in the **current** customer message, read lexically. Not from
      `ResolvedSearch`, because most branches never run query understanding,
      and not from history, because a figure mentioned three turns ago is not
      what they are asking about now. The guard needs provenance, not search
      semantics.
    * the counts the application itself established.
    * ordinal positions within whatever is on screen, so "the second option"
      is sayable and "the fourth" of three is not.

    * approved counts from a whole-room outcome, listed explicitly by
      :func:`bundle_counts` rather than swept from the view.

    * figures the application put on the customer's screen, named field by
      field by :func:`screen_figures`, plus any general design measurements the
      specialist supplied.

    No relaxation threshold, internal score or identifier is ever admitted. Nor
    is anything derived: a saving, a percentage, a price difference and a
    remaining budget all have to be computed, and the guard admits only figures
    that already exist somewhere authoritative (CLAUDE.md 14).
    """
    allowed = set(numbers_in(message))
    for figure in figures:
        allowed.add(canonical_number(str(figure)) or str(figure))
    for count in (presented_count, compared_count):
        if count > 0:
            allowed.add(canonical_number(str(count)) or str(count))
            allowed.update(str(position) for position in range(1, count + 1))
    # Approved counts admit the figure itself and no ordinals: there is no
    # numbered list of bundle items for prose to count into.
    for count in counts:
        allowed.add(canonical_number(str(count)) or str(count))
    return frozenset(allowed)


@dataclass(frozen=True, slots=True)
class NumericPolicyViolation:
    """A figure in generated prose with no approved source."""

    field: str
    value: str


def check_numeric_policy(
    *, message: str, follow_up_question: str | None, allowance: ResponseNumericAllowance
) -> NumericPolicyViolation | None:
    """The first unsupported figure, or None.

    Scans exactly the two model-generated fields. A pass-through clarification
    is not scanned: no response call produced it, and the decision model that
    wrote it was validated in its own phase.
    """
    fields: list[tuple[str, str]] = [("message", message)]
    if follow_up_question is not None:
        fields.append(("follow_up_question", follow_up_question))

    for name, text in fields:
        for value in sorted(numbers_in(text)):
            if value not in allowance:
                return NumericPolicyViolation(field=name, value=value)
    return None
