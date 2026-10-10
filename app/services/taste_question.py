"""The soft taste question after products, and what its answer means
(docs/designer-led-shopping-plan.md, 5.2).

Decided in code from what is known: at most one question a reply, each kind at
most once a session, and never about something the customer said or something
their likes, picks and More like this already told us. How wide the spot is
first - a sofa's opening asks the room instead - then which of two feels more
like them, then which of the store's styles, then anything to avoid.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from app.schemas.agent_state import ActiveSearchState, AgentStateV1
from app.schemas.grounding import GroundedProduct
from app.schemas.query import SemanticPreference
from app.schemas.taste import (
    MAX_TASTE_OPTIONS,
    PendingTaste,
    TasteOption,
    TasteQuestionKind,
    TasteState,
)
from app.services.product_brief import SPACE_WIDTHS
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.dimensions import DimensionRole

NEITHER = "neither"
SPACE_NOT_SURE = "space:any"
MAX_STYLE_CHOICES = 6
MAX_AVOID_CHOICES = 3
"""Of colours, and of styles: a short row, not the catalogue."""
MAX_CARD_POSITION = 8
"""The cards a "which of two" may name - as far as a chip has words for."""


def choose_question(
    products: Sequence[GroundedProduct],
    search: ActiveSearchState,
    said: Sequence[SemanticPreference],
    stocked_styles: Sequence[str],
    taste: TasteState,
    *,
    list_revision: int | None,
) -> PendingTaste | None:
    """The one taste question worth asking now, beside the list at
    `list_revision`, or None."""
    colour_known, style_known = _known(search, said)
    asked = set(taste.asked)
    question = taste.asked_count + 1
    kind = search.request.commerce_subcategory
    which_settled = TasteQuestionKind.WHICH in asked or (colour_known and style_known)
    style_settled = TasteQuestionKind.STYLE in asked or style_known

    widths = SPACE_WIDTHS.get(kind or "", ())
    if widths and TasteQuestionKind.SPACE not in asked and not _space_known(search):
        return PendingTaste(
            question=question,
            kind=TasteQuestionKind.SPACE,
            commerce_subcategory=kind,
            list_revision=list_revision,
            options=(
                *(TasteOption(key=f"space:{width}", space_cm=width) for width in widths),
                TasteOption(key=SPACE_NOT_SURE),
            ),
        )
    if not which_settled:
        pair = _most_different(products)
        if pair is not None:
            return PendingTaste(
                question=question,
                kind=TasteQuestionKind.WHICH,
                commerce_subcategory=kind,
                list_revision=list_revision,
                options=(
                    *(
                        TasteOption(
                            key=f"card:{p.presented_ordinal}",
                            position=p.presented_ordinal,
                            colour=p.main_color,
                            styles=p.styles,
                        )
                        for p in pair
                    ),
                    TasteOption(key=NEITHER),
                ),
            )
    if not style_settled:
        styles = _styles_by_presence(products, stocked_styles)
        if len(styles) >= 2:
            return PendingTaste(
                question=question,
                kind=TasteQuestionKind.STYLE,
                commerce_subcategory=kind,
                list_revision=list_revision,
                options=tuple(
                    TasteOption(key=f"style:{s}", styles=(s,)) for s in styles[:MAX_STYLE_CHOICES]
                ),
            )
    if TasteQuestionKind.AVOID not in asked and which_settled and style_settled:
        options = _avoidable(products, search, said)
        if len(options) >= 2:
            return PendingTaste(
                question=question,
                kind=TasteQuestionKind.AVOID,
                commerce_subcategory=kind,
                list_revision=list_revision,
                options=options[:MAX_TASTE_OPTIONS],
            )
    return None


def on_screen(state: AgentStateV1) -> PendingTaste | None:
    """The taste question still beside the list it was asked about - None once
    another list has replaced it, so an old question never reads a later
    "the first one" as its answer."""
    pending = state.taste.pending
    if pending is None or pending.list_revision is None:
        return None
    if pending.list_revision != state.product_interaction.presented_search_revision:
        return None
    return pending


def asked(taste: TasteState, pending: PendingTaste) -> TasteState:
    """The session's taste record after asking `pending`."""
    return TasteState(
        asked=(*taste.asked, pending.kind),
        pending=pending,
        asked_count=pending.question,
    )


@dataclass(frozen=True, slots=True)
class TasteAnswer:
    """What an answer means: said styles, colours for this kind's search, what
    to push down, and cards to leave out ("neither")."""

    styles: tuple[str, ...] = ()
    colours: tuple[str, ...] = ()
    avoid_colours: tuple[str, ...] = ()
    avoid_styles: tuple[str, ...] = ()
    leave_out: tuple[int, ...] = ()
    """Positions on screen of the cards neither of which felt like them."""
    space_cm: int | None = None
    """How wide the spot is: the cards are ordered to fit it."""


def answer(pending: PendingTaste, key: str) -> TasteAnswer | None:
    """The answer `key` gives to the question on screen, or None for a key it
    never offered."""
    option = next((o for o in pending.options if o.key == key), None)
    if option is None:
        return None
    match pending.kind:
        case TasteQuestionKind.WHICH if key == NEITHER:
            return TasteAnswer(
                leave_out=tuple(o.position for o in pending.options if o.position is not None)
            )
        case TasteQuestionKind.WHICH:
            return TasteAnswer(
                styles=option.styles, colours=(option.colour,) if option.colour else ()
            )
        case TasteQuestionKind.STYLE:
            return TasteAnswer(styles=option.styles)
        case TasteQuestionKind.AVOID:
            return TasteAnswer(
                avoid_colours=(option.colour,) if option.colour else (),
                avoid_styles=option.styles,
            )
        case TasteQuestionKind.SPACE:
            return TasteAnswer(space_cm=option.space_cm)


def _space_known(search: ActiveSearchState) -> bool:
    """Whether the space is already known: a wall they gave, or a width for
    the piece itself."""
    if search.lean is not None and search.lean.space_cm is not None:
        return True
    if search.request.planar_dimensions is not None:
        return True
    floor = (DimensionRole.OVERALL_WIDTH, DimensionRole.LENGTH)
    return any(constraint.role in floor for constraint in search.request.dimensions)


def _known(search: ActiveSearchState, said: Sequence[SemanticPreference]) -> tuple[bool, bool]:
    """Whether a colour, and a style, are already known - said, on the search,
    or learned from likes, picks and More like this."""
    families = {p.family for p in (*said, *search.semantic_preferences)}
    lean = search.lean
    colour = (
        AttributeFamily.COLOR in families
        or bool(search.request.colors_any_of)
        or bool(lean and lean.learned_colours)
    )
    style = (
        AttributeFamily.STYLE in families
        or bool(search.request.styles_all_of)
        or bool(lean and lean.learned_styles)
    )
    return colour, style


def _most_different(
    products: Sequence[GroundedProduct],
) -> tuple[GroundedProduct, GroundedProduct] | None:
    """The two cards that differ most in colour and style - None when no two
    differ at all, since a choice between twins teaches nothing."""
    cards = [
        p
        for p in products
        if p.presented_ordinal is not None
        and p.presented_ordinal <= MAX_CARD_POSITION
        and (p.main_color or p.styles)
    ]

    def difference(pair: tuple[GroundedProduct, GroundedProduct]) -> int:
        first, second = pair
        styles_differ = bool(first.styles or second.styles) and not (
            set(first.styles) & set(second.styles)
        )
        return int(first.main_color != second.main_color) + int(styles_differ)

    best = max(combinations(cards, 2), key=difference, default=None)
    return best if best is not None and difference(best) > 0 else None


def _styles_by_presence(products: Sequence[GroundedProduct], stocked: Sequence[str]) -> list[str]:
    """The store's styles for this kind, the ones on screen most first."""
    on_screen = Counter(style for product in products for style in product.styles)
    return sorted(stocked, key=lambda style: (-on_screen[style], style))


def _avoidable(
    products: Sequence[GroundedProduct],
    search: ActiveSearchState,
    said: Sequence[SemanticPreference],
) -> tuple[TasteOption, ...]:
    """Colours and styles on screen they have not said, required or shown
    they like."""
    liked = {p.canonical_value for p in (*said, *search.semantic_preferences)}
    liked |= {*search.request.colors_any_of, *search.request.styles_all_of}
    if search.lean is not None:
        liked |= {*search.lean.learned_colours, *search.lean.learned_styles}
    colours = [
        c
        for c, _ in Counter(p.main_color for p in products if p.main_color).most_common()
        if c not in liked
    ]
    styles = [
        s for s, _ in Counter(s for p in products for s in p.styles).most_common() if s not in liked
    ]
    return (
        *(TasteOption(key=f"colour:{c}", colour=c) for c in colours[:MAX_AVOID_CHOICES]),
        *(TasteOption(key=f"style:{s}", styles=(s,)) for s in styles[:MAX_AVOID_CHOICES]),
    )
