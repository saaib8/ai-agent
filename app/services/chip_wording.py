"""What the application's own chips say, in each reply language.

A chip has two texts: the `label` on the button and the `value` sent when it is
tapped, which becomes the customer's side of the turn and is read by the
decision model like typed words. Both follow the session's language
(docs/arabic-replies-plan.md), so an Arabic customer taps Arabic and sees their
own Arabic answer. Which chip is offered, and the action it performs, never
depend on the language: a chip that must act exactly carries its action.

One table, both languages side by side, so a chip cannot exist in one and not
the other - a test makes it total. Templates take named figures and names
("{count}", "{price}"); the Arabic may leave out a name the English uses, but
never invent one. Figures stay Western digits in both. Reviewed by a native
speaker before Arabic is switched on in production.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from app.schemas.bundle_action import BundleActionRequest
from app.schemas.language import ReplyLanguage
from app.schemas.product_action import ProductActionRequest
from app.schemas.reply_choice import ReplyChoice
from app.taxonomy.briefs import BriefQuestionKind

EN, AR = ReplyLanguage.EN, ReplyLanguage.AR


@dataclass(frozen=True, slots=True)
class ChipText:
    label: str
    value: str


class Chip(StrEnum):
    # The next step a reply ends on (app/services/next_step.py).
    WHAT_GOES_WITH_PICK = "what_goes_with_pick"
    ROOM_AROUND_PICKS = "room_around_picks"
    KEEP_BROWSING = "keep_browsing"
    ADD_TO_PICKS = "add_to_picks"
    WHAT_GOES_WITH_IT = "what_goes_with_it"
    SHOW_SIMILAR = "show_similar"
    TAKE_FIRST = "take_first"
    TAKE_SECOND = "take_second"
    SWAP_A_PIECE = "swap_a_piece"
    FINISHING_TOUCH = "finishing_touch"
    SHOW_MORE = "show_more"
    NARROW_DOWN = "narrow_down"
    FIND_A_PIECE = "find_a_piece"
    DESIGN_A_ROOM = "design_a_room"
    PRODUCT_TYPE = "product_type"
    ROOM_TYPE = "room_type"
    # A room's questions and its over-budget swap (app/services/room_presentation.py).
    CHOOSE_FOR_ME = "choose_for_me"
    STRETCH_YES = "stretch_yes"
    STRETCH_NO = "stretch_no"
    KEEP_ORIGINAL_ROOM = "keep_original_room"
    CHEAPER_PIECE = "cheaper_piece"
    ROOM_BUDGET_UNDER = "room_budget_under"
    ROOM_BUDGET_BETWEEN = "room_budget_between"
    NO_STRICT_BUDGET = "no_strict_budget"
    LEAVE_THE_PALETTE = "leave_the_palette"
    ROOM_COLOUR = "room_colour"
    SEATS_CONFIRM = "seats_confirm"
    SEATS = "seats"
    SEATS_OR_MORE = "seats_or_more"
    # Seating that takes more than one piece (app/services/seating_presentation.py).
    SEPARATE_SOFAS = "separate_sofas"
    SOFA_WITH_ARMCHAIRS = "sofa_with_armchairs"
    EITHER_SHAPE = "either_shape"
    # What goes with a pick (app/services/cross_sell.py).
    COMPANION = "companion"
    NO_THANKS = "no_thanks"


CHIPS: Mapping[Chip, Mapping[ReplyLanguage, ChipText]] = MappingProxyType(
    {
        Chip.PRODUCT_TYPE: {
            EN: ChipText("{label}", "Show me {kind}"),
            AR: ChipText("{label}", "أرني {kind}"),
        },
        Chip.ROOM_TYPE: {
            EN: ChipText("{room}", "I'd like to design a {room}"),
            AR: ChipText("{room}", "أودّ تصميم {room}"),
        },
        Chip.WHAT_GOES_WITH_PICK: {
            EN: ChipText("What goes with the {kind}", "What goes with the {kind}?"),
            # No Arabic product-type names exist yet, so the pick is not named.
            AR: ChipText("ما يناسب اختيارك", "ما الذي يناسب اختياري؟"),
        },
        Chip.ROOM_AROUND_PICKS: {
            EN: ChipText("Design a room around my picks", "Design a room around my picks"),
            AR: ChipText("صمّم غرفة حول اختياراتي", "صمّم غرفة حول اختياراتي"),
        },
        Chip.KEEP_BROWSING: {
            EN: ChipText("Keep browsing", "Show me something else"),
            AR: ChipText("تابع التصفح", "أرني شيئًا آخر"),
        },
        Chip.ADD_TO_PICKS: {
            EN: ChipText("Add it to my picks", "Add this one to my picks"),
            AR: ChipText("أضفه إلى اختياراتي", "أضف هذا إلى اختياراتي"),
        },
        Chip.WHAT_GOES_WITH_IT: {
            EN: ChipText("What goes with it", "What goes with this one?"),
            AR: ChipText("ما يناسبه", "ما الذي يناسب هذا؟"),
        },
        Chip.SHOW_SIMILAR: {
            EN: ChipText("Show similar ones", "Show me similar ones"),
            AR: ChipText("أرني المشابه", "أرني منتجات مشابهة"),
        },
        Chip.TAKE_FIRST: {
            EN: ChipText("Take the first", "I'll take the first one"),
            AR: ChipText("آخذ الأول", "سآخذ الأول"),
        },
        Chip.TAKE_SECOND: {
            EN: ChipText("Take the second", "I'll take the second one"),
            AR: ChipText("آخذ الثاني", "سآخذ الثاني"),
        },
        Chip.SWAP_A_PIECE: {
            EN: ChipText("Swap a piece", "I'd like to swap one of the pieces"),
            AR: ChipText("بدّل قطعة", "أودّ تبديل إحدى القطع"),
        },
        Chip.FINISHING_TOUCH: {
            EN: ChipText("Add a finishing touch", "What would finish the room?"),
            AR: ChipText("أضف لمسة أخيرة", "ما الذي يكمّل الغرفة؟"),
        },
        Chip.SHOW_MORE: {
            EN: ChipText("Show me more", "Show me more options"),
            AR: ChipText("أرني المزيد", "أرني خيارات أكثر"),
        },
        Chip.NARROW_DOWN: {
            EN: ChipText("Narrow them down", "Help me narrow these down"),
            AR: ChipText("ضيّق الخيارات", "ساعدني في تضييق هذه الخيارات"),
        },
        Chip.FIND_A_PIECE: {
            EN: ChipText("Find a piece", "I'm looking for a piece of furniture"),
            AR: ChipText("ابحث عن قطعة", "أبحث عن قطعة أثاث"),
        },
        Chip.DESIGN_A_ROOM: {
            EN: ChipText("Design a room", "I'd like to design a room"),
            AR: ChipText("صمّم غرفة", "أودّ تصميم غرفة"),
        },
        Chip.CHOOSE_FOR_ME: {
            EN: ChipText("Choose for me", "Choose the pieces for me"),
            AR: ChipText("اختر لي", "اختر القطع لي"),
        },
        Chip.STRETCH_YES: {
            EN: ChipText("Yes, let's stretch it", "Yes, stretch the budget to fit it"),
            AR: ChipText("نعم، نزيد الميزانية", "نعم، زد الميزانية لإضافتها"),
        },
        Chip.STRETCH_NO: {
            EN: ChipText("No, keep me in budget", "No, keep me within budget"),
            AR: ChipText("لا، التزم بميزانيتي", "لا، أبقني ضمن الميزانية"),
        },
        Chip.KEEP_ORIGINAL_ROOM: {
            EN: ChipText("Keep my original room", "Keep my original room as it was"),
            AR: ChipText("أبقِ غرفتي الأصلية", "أبقِ غرفتي الأصلية كما كانت"),
        },
        Chip.CHEAPER_PIECE: {
            EN: ChipText("Show cheaper options", "Show me cheaper options for that piece"),
            AR: ChipText("أرني خيارات أرخص", "أرني خيارات أرخص لهذه القطعة"),
        },
        Chip.ROOM_BUDGET_UNDER: {
            EN: ChipText("Under {high} SAR", "A budget under {high_plain} SAR"),
            AR: ChipText("أقل من {high} ريال", "ميزانية أقل من {high_plain} ريال"),
        },
        Chip.ROOM_BUDGET_BETWEEN: {
            EN: ChipText(
                "{low}-{high} SAR", "A budget between {low_plain} and {high_plain} SAR"
            ),
            AR: ChipText("{low}-{high} ريال", "ميزانية بين {low_plain} و{high_plain} ريال"),
        },
        Chip.NO_STRICT_BUDGET: {
            EN: ChipText("No strict limit", "No strict budget"),
            AR: ChipText("بدون حد معيّن", "ليس لدي ميزانية محددة"),
        },
        Chip.LEAVE_THE_PALETTE: {
            EN: ChipText("Leave it to you", "Leave the palette to you"),
            AR: ChipText("اختر أنت", "أترك لك اختيار الألوان"),
        },
        Chip.ROOM_COLOUR: {
            EN: ChipText("{colour}", "I'd like {colour_lower} tones"),
            AR: ChipText("{colour}", "أفضّل لون {colour}"),
        },
        Chip.SEATS_CONFIRM: {
            EN: ChipText("Yes, {count}", "Seating for {count} people"),
            AR: ChipText("نعم، {count}", "عدد الأشخاص: {count}"),
        },
        Chip.SEATS: {
            EN: ChipText("{count} people", "Seating for {count} people"),
            AR: ChipText("لـ {count}", "عدد الأشخاص: {count}"),
        },
        Chip.SEATS_OR_MORE: {
            EN: ChipText("{count}+ people", "Seating for {count} or more people"),
            AR: ChipText("لـ {count} أو أكثر", "عدد الأشخاص: {count} أو أكثر"),
        },
        Chip.SEPARATE_SOFAS: {
            EN: ChipText(
                "Separate sofas · from {price} {currency}", "Separate sofas arranged together"
            ),
            AR: ChipText("كنبات منفصلة · من {price} {currency}", "كنبات منفصلة مرتبة معًا"),
        },
        Chip.SOFA_WITH_ARMCHAIRS: {
            EN: ChipText(
                "Sofa + armchairs · from {price} {currency}",
                "A sofa with a few armchairs alongside",
            ),
            AR: ChipText(
                "كنبة + كراسي منفردة · من {price} {currency}",
                "كنبة مع بعض الكراسي المنفردة بجانبها",
            ),
        },
        Chip.EITHER_SHAPE: {
            EN: ChipText("Either - show me both", "Either is fine, show me both"),
            AR: ChipText("أيٌّ منهما - أرني الاثنين", "أيٌّ منهما يناسبني، أرني الاثنين"),
        },
        Chip.COMPANION: {
            EN: ChipText("{label}", "Show me {label_lower} to go with it"),
            AR: ChipText("{label}", "أرني ما يناسبه من {label}"),
        },
        Chip.NO_THANKS: {
            EN: ChipText("No thanks", "No thanks"),
            AR: ChipText("لا، شكرًا", "لا، شكرًا"),
        },
    }
)


def chip(
    key: Chip,
    language: ReplyLanguage,
    *,
    product_action: ProductActionRequest | None = None,
    bundle_action: BundleActionRequest | None = None,
    **figures: Any,
) -> ReplyChoice:
    """One chip, worded in `language`, performing the same action in either."""
    text = CHIPS[key][language]
    return ReplyChoice(
        label=text.label.format(**figures),
        value=text.value.format(**figures),
        product_action=product_action,
        bundle_action=bundle_action,
    )


def currency_word(currency: str, language: ReplyLanguage) -> str:
    """The currency as a chip reads it: "ريال" for riyals in Arabic, the code
    otherwise - an unfamiliar currency is never guessed into a word."""
    if language is AR and currency.upper() == "SAR":
        return "ريال"
    return currency


# ── the card of questions for a product search ──────────────────────────────

CARD_QUESTIONS: Mapping[BriefQuestionKind, Mapping[ReplyLanguage, str]] = MappingProxyType(
    {
        BriefQuestionKind.TYPE: {EN: "What kind?", AR: "أي نوع؟"},
        BriefQuestionKind.BUDGET: {EN: "Budget", AR: "الميزانية"},
        BriefQuestionKind.COLOUR: {EN: "Colours you like", AR: "الألوان التي تعجبك"},
        BriefQuestionKind.STYLE: {EN: "Style", AR: "الطراز"},
    }
)
"""The card's own question titles. The feel's title is reviewed data, per
product family."""

CARD_SUBMIT: Mapping[ReplyLanguage, str] = MappingProxyType(
    {EN: "Show me {noun}", AR: "أرني {noun}"}
)
CARD_SUBMIT_ANY: Mapping[ReplyLanguage, str] = MappingProxyType(
    {EN: "Show me {noun}", AR: "أرني النتائج"}
)
"""When the card names the searched kind rather than its family's noun: no
Arabic product-type names exist yet, so the Arabic button names none."""

BUDGET_UNDER: Mapping[ReplyLanguage, str] = MappingProxyType(
    {EN: "Under {amount} {currency}", AR: "أقل من {amount} {currency}"}
)
BUDGET_OVER: Mapping[ReplyLanguage, str] = MappingProxyType(
    {EN: "Over {amount} {currency}", AR: "أكثر من {amount} {currency}"}
)
BUDGET_BETWEEN: Mapping[ReplyLanguage, str] = MappingProxyType(
    {EN: "{low}-{high} {currency}", AR: "{low}-{high} {currency}"}
)

PIECE_PICKED: Mapping[ReplyLanguage, str] = MappingProxyType(
    {EN: "{label} · your pick", AR: "{label} · من اختياراتك"}
)
DESIGN_MY_ROOM: Mapping[ReplyLanguage, str] = MappingProxyType(
    {EN: "Design my room", AR: "صمّم غرفتي"}
)
