"""Building the room form and reading its answers back (docs/room-form-plan.md).

The form asks what every room shares - the budget (required), the room's size
and its look - leaving off what is already known. Its chips are fixed amounts
and sizes from settings and the looks this store stocks for the room's main
piece; nothing is worked out from the store's prices. Answers come back as
keys the form offered, or the shopper's own words: a key it did not offer is
ignored, a figure is read only where it can be read without guessing, and
anything else is handed to Nora as their words.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.core.config import RoomFormSettings
from app.core.numbers import parse_stated_amount
from app.schemas.card_labels import CardLabels
from app.schemas.catalog_overview import CatalogOverview
from app.schemas.discovery import PriceConstraint
from app.schemas.geometry import RoomGeometry, RoomMeasurement, RoomMeasurementRole
from app.schemas.language import ReplyLanguage
from app.schemas.room_form import (
    MAX_COLOURS_CHOSEN,
    MAX_STYLES_CHOSEN,
    YOU_DECIDE,
    RoomForm,
    RoomFormAction,
    RoomFormChoice,
    RoomFormDimensions,
    RoomFormQuestion,
    RoomFormRecord,
)
from app.services.product_display import ProductDisplayWords, price_words
from app.taxonomy.attributes import AttributeFamily

_SIZES = ("small", "medium", "large")

_TIMES = "\u00d7"
"""The multiplication sign, as in "3 \u00d7 3 m"."""
_FIGURE = re.compile(r"\d[\d,.]*\s*[kK]?")


@dataclass(frozen=True, slots=True)
class FormKnown:
    """What the room already has on record, left off the form. The colours and
    the style are apart: "a blue bedroom" says the colours and leaves the style
    to ask."""

    budget: bool = False
    size: bool = False
    colours: bool = False
    styles: bool = False


def build_room_form(
    *,
    number: int,
    settings: RoomFormSettings,
    labels: CardLabels,
    words: ProductDisplayWords,
    language: ReplyLanguage,
    overview: CatalogOverview,
    main_kinds: Sequence[str],
    known: FormKnown,
    budget_missing: bool = False,
) -> tuple[RoomForm, RoomFormRecord] | None:
    """The form for what is still unknown, and its record - or None when the
    budget, the size, the colours and the style are all known already."""
    currency = overview.currency
    questions: list[RoomFormQuestion] = []
    budgets: dict[str, Decimal] = {}
    sizes: dict[str, Decimal] = {}
    colours: tuple[str, ...] = ()
    style_keys: tuple[str, ...] = ()
    if not known.budget:
        unit = currency or ""
        for amount in settings.budget_amounts:
            budgets[str(amount)] = Decimal(amount)
        questions.append(
            RoomFormQuestion(
                kind="budget",
                label=labels.room_form_budget,
                choices=tuple(
                    RoomFormChoice(
                        key=key,
                        label=price_words(value, unit, language) if unit else f"{value:,}",
                    )
                    for key, value in budgets.items()
                ),
                placeholder=labels.room_form_budget_box,
                required=True,
            )
        )
    if not known.size:
        sides = (settings.small_room_cm, settings.medium_room_cm, settings.large_room_cm)
        worded = (
            labels.room_form_size_small,
            labels.room_form_size_medium,
            labels.room_form_size_large,
        )
        for key, side in zip(_SIZES, sides, strict=True):
            sizes[key] = Decimal(side)
        questions.append(
            RoomFormQuestion(
                kind="size",
                label=labels.room_form_size,
                choices=tuple(
                    RoomFormChoice(
                        key=key, label=template.replace("{size}", _square(side, language))
                    )
                    for key, side, template in zip(_SIZES, sides, worded, strict=True)
                ),
                dimensions=RoomFormDimensions(
                    length_label=labels.room_form_length,
                    width_label=labels.room_form_width,
                    unit=labels.room_form_metres,
                ),
            )
        )
    if not known.colours:
        colours = _stocked(overview, main_kinds, settings.max_looks, "colours")
        if colours:
            questions.append(
                RoomFormQuestion(
                    kind="colours",
                    label=labels.room_form_colours,
                    choices=tuple(
                        RoomFormChoice(
                            key=colour, label=words.value(AttributeFamily.COLOR, colour, language)
                        )
                        for colour in colours
                    ),
                    max_choices=min(MAX_COLOURS_CHOSEN, len(colours)),
                )
            )
    if not known.styles:
        styles = _stocked(overview, main_kinds, settings.max_looks, "styles")
        style_keys = (*styles, YOU_DECIDE)
        questions.append(
            RoomFormQuestion(
                kind="style",
                label=labels.room_form_style,
                choices=(
                    *(
                        RoomFormChoice(
                            key=style, label=words.value(AttributeFamily.STYLE, style, language)
                        )
                        for style in styles
                    ),
                    RoomFormChoice(key=YOU_DECIDE, label=labels.room_form_you_decide),
                ),
                max_choices=MAX_STYLES_CHOSEN,
                placeholder=labels.room_form_look_box,
            )
        )
    if not questions:
        return None
    form = RoomForm(
        number=number,
        questions=tuple(questions),
        submit_label=labels.room_form_submit,
        budget_needed=labels.room_form_budget_needed,
        budget_missing=budget_missing and not known.budget,
    )
    record = RoomFormRecord(
        number=number,
        currency=currency,
        budgets=budgets,
        sizes=sizes,
        colours=colours,
        styles=style_keys,
        looks_answered=known.colours and known.styles,
    )
    return form, record


@dataclass(frozen=True, slots=True)
class RoomFormAnswers:
    """What the answers say, read only as far as they can be without guessing."""

    budget: PriceConstraint | None
    geometry: RoomGeometry | None
    colours: tuple[str, ...]
    """The approved colours chosen."""
    styles: tuple[str, ...]
    """The approved styles chosen; "you decide" is not one."""
    look_left_to_nora: bool
    budget_words: str | None
    """A typed budget that could not be read as one amount."""
    look_words: str | None

    @property
    def budget_missing(self) -> bool:
        return self.budget is None and self.budget_words is None


def read_room_form(record: RoomFormRecord, action: RoomFormAction) -> RoomFormAnswers | None:
    """The answers to the form on record, or None when they answer another
    (an old form). A key the form did not offer is ignored."""
    if action.form != record.number:
        return None
    budget: PriceConstraint | None = None
    budget_words: str | None = None
    if action.budget is not None and action.budget in record.budgets and record.currency:
        budget = PriceConstraint(currency=record.currency, max_amount=record.budgets[action.budget])
    elif action.budget_text is not None:
        amount = _amount(action.budget_text)
        if amount is not None and record.currency:
            budget = PriceConstraint(currency=record.currency, max_amount=amount)
        else:
            budget_words = action.budget_text
    geometry: RoomGeometry | None = None
    if action.size is not None and action.size in record.sizes:
        side = record.sizes[action.size]
        geometry = _rectangle(side, side)
    elif action.length_m is not None and action.width_m is not None:
        geometry = _rectangle(action.length_m * 100, action.width_m * 100)
    styles = tuple(key for key in action.styles if key in record.styles)
    return RoomFormAnswers(
        budget=budget,
        geometry=geometry,
        colours=tuple(key for key in action.colours if key in record.colours),
        styles=tuple(key for key in styles if key != YOU_DECIDE),
        look_left_to_nora=YOU_DECIDE in styles,
        budget_words=budget_words,
        look_words=action.look_text,
    )


def _stocked(
    overview: CatalogOverview,
    main_kinds: Sequence[str],
    limit: int,
    values: Literal["colours", "styles"],
) -> tuple[str, ...]:
    """The colours or styles this store stocks for the room's main piece, most
    widely stocked kinds first; across every kind when the room's main piece is
    not reviewed."""
    shelves = [
        s
        for s in overview.shelves
        if s.commerce_subcategory is not None
        and (not main_kinds or s.commerce_subcategory in main_kinds)
    ]
    shelves.sort(key=lambda s: -s.active_count)
    stocked = list(dict.fromkeys(v for shelf in shelves for v in getattr(shelf, values)))
    return tuple(stocked[:limit])


def _square(side_cm: int, language: ReplyLanguage) -> str:
    metres = Decimal(side_cm) / 100
    figure = f"{metres.normalize():f}"
    unit = "م" if language is ReplyLanguage.AR else "m"
    return f"{figure} {_TIMES} {figure} {unit}"


def _amount(text: str) -> Decimal | None:
    """One amount in what they typed ("8000", "8,000 SAR", "8k"), or None."""
    figures = _FIGURE.findall(text)
    if len(figures) != 1:
        return None
    try:
        amount = parse_stated_amount(figures[0].replace(" ", ""))
    except ValueError:
        return None
    return amount if amount > 0 else None


def _rectangle(length_cm: Decimal, width_cm: Decimal) -> RoomGeometry:
    return RoomGeometry(
        measurements=(
            RoomMeasurement(role=RoomMeasurementRole.ROOM_LENGTH, centimetres=length_cm),
            RoomMeasurement(role=RoomMeasurementRole.ROOM_WIDTH, centimetres=width_cm),
        )
    )
