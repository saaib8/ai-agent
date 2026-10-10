"""The card of questions for a product search, and the search its answers make.

"I need a sofa", "find me a sofa" or "show me sofas" gets one card - what
kind, the budget, colours, the feel, the style - before anything is shown, so
the few products that follow are the ones that suit them rather than the first
few the catalog returns (CLAUDE.md 10.4). Everything on the card leads to real products:

* the questions and the kinds of piece come from reviewed data
  (`app/taxonomy/briefs_v1.yaml`), never from a model;
* budget bands, colours and styles are counted from the store's live catalog
  for those types, and a kind the store does not stock is not offered;
* whatever they already said is not asked again, and a question left with a
  single possible answer is not asked at all.

Answers are keys, read back through the card the session remembers. The type
and the budget become requirements - they tapped them - and colours, styles
and the feel become preferences that only rank (CLAUDE.md 12.4). A feel has no
field to filter on anyway: the catalog records no material (CLAUDE.md 5).

Deterministic: no model is consulted to build a card or to read one.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Final

from app.core.logging import get_logger
from app.repositories.products import BriefFacts, ProductRepository
from app.schemas.agent_state import ActiveSearchState, AgentStateV1
from app.schemas.discovery import (
    DimensionConstraint,
    DimensionConstraintKind,
    PriceConstraint,
    ProductSearchRequest,
    SeatingCapacityConstraint,
)
from app.schemas.language import ReplyLanguage
from app.schemas.product_brief import (
    MAX_BRIEF_COLOURS,
    MAX_BRIEF_STYLES,
    MAX_OPENING_ASKED,
    BriefBudgetOption,
    BriefChip,
    BriefChoice,
    BriefFeelOption,
    BriefKindOption,
    BriefMode,
    BriefPeopleOption,
    BriefQuestionView,
    BriefRoomOption,
    BriefSpaceOption,
    PendingBrief,
    ProductBrief,
)
from app.schemas.query import (
    ConstraintStrength,
    RankingLean,
    ResolvedSearch,
    SemanticPreference,
)
from app.schemas.retailer import RetailerContext
from app.schemas.search_action import BriefAnswerAction
from app.services.chip_wording import (
    BUDGET_BETWEEN,
    BUDGET_OVER,
    BUDGET_UNDER,
    CARD_QUESTIONS,
    CARD_SKIP,
    CARD_SUBMIT,
    CARD_SUBMIT_ANY,
    PEOPLE_OR_MORE,
    SEATS_AT_LEAST,
    SEATS_EXACTLY,
    SEATS_FOR,
    SIZE_BOUND,
    SIZE_PAIR,
    SIZE_ROLE,
    SPACE_ANY,
    SPACE_FOR,
    SPACE_UPTO,
    SUGGESTED,
    currency_word,
)
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.briefs import (
    OPENING_QUESTIONS,
    Brief,
    BriefQuestionKind,
    Briefs,
    KindChoice,
)
from app.taxonomy.dimensions import DimensionRole
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.words import customer_words, plural_words

logger = get_logger(__name__)

MAX_COLOUR_CHOICES: Final[int] = 8
MAX_STYLE_CHOICES: Final[int] = 6
"""The colours and styles most of these products carry - enough to find
theirs, few enough to scan."""

# How wide the wall or spot is that the piece goes in (cm), offered as tappable
# answers to "how wide is the spot where it'll go?". The space orders the cards
# - what suits it first, anything wider last - and filters nothing (CLAUDE.md
# 10.10), so these are sizes of walls and spots, not of pieces. The key is also
# the gate: a kind appears here only where the dimension registry maps
# OVERALL_WIDTH to the along-wall `length` axis, so the space is compared with
# the side that stands against it. A dining table is excluded on purpose: the
# registry maps its OVERALL_WIDTH to its depth. A reviewed heuristic, fixed so
# the card stays a plain lookup; it will want revisiting per retailer.
SPACE_WIDTHS: Final[dict[str, tuple[int, ...]]] = {
    "sofa": (250, 300, 400, 500),
    "center-table": (120, 160, 200),
    "service-table": (50, 70, 90),
    "tv-table": (200, 300, 400),
    "console": (120, 180, 250),
    "wardrobe": (200, 300, 400),
}


@dataclass(frozen=True, slots=True)
class BuiltBrief:
    """A card to draw, and what the session must remember to read it."""

    card: ProductBrief
    pending: PendingBrief


class ProductBriefBuilder:
    """Builds a product family's card from the live catalog, and reads it back."""

    def __init__(
        self,
        repository: ProductRepository,
        briefs: Briefs,
        attributes: CatalogAttributes,
        taxonomy: CommerceTaxonomy,
    ) -> None:
        self._repository = repository
        self._briefs = briefs
        self._attributes = attributes
        self._taxonomy = taxonomy

    async def build(
        self,
        resolved: ResolvedSearch,
        state: AgentStateV1,
        context: RetailerContext,
        *,
        mode: BriefMode,
        language: ReplyLanguage = ReplyLanguage.EN,
        opening: bool = False,
        prefilled: bool = False,
        only: tuple[BriefQuestionKind, ...] = (),
    ) -> BuiltBrief | None:
        """The card for this search, or None when there is nothing to ask.

        A search for a kind of product always gets its card: "I need a bed"
        said again later in a long chat is a new need, and a card they left
        unanswered an hour ago is no reason to skip the questions now. Only
        what they already said is left off it. The folded card beside results
        they asked for without questions is offered once per family, so it
        does not follow every list around.

        None when the type has no card, when everything it would ask is
        already known, or for a folded card already offered. Then the search
        simply runs.

        Worded in `language`; the keys a tap sends back are the same in either.

        An `opening` offers every question of the family's opening that is
        still open - not known, never asked before, never the budget - and the
        reply writer chooses which two to ask. Every product has one: a type
        no card covers is asked the default opening.

        A `prefilled` card is Narrow down beside results: every question of
        the family, each opened showing what the search already uses, and its
        answers replace those values rather than add to them. `only` keeps it
        to the questions they asked to narrow by ("by size" is the space).
        """
        request = resolved.request
        briefs = state.product_brief
        if opening:
            opening_brief = self._briefs.for_opening(
                request.commerce_category, request.commerce_subcategory
            )
            if opening_brief is None:
                # A kind not chosen by its look: shown at once, nothing asked.
                return None
            brief = opening_brief
            candidates = tuple(
                kind
                for kind in brief.opening
                if _opening_key(brief.name, kind) not in briefs.asked
            )
        elif prefilled:
            brief = self._briefs.for_narrowing(
                request.commerce_category, request.commerce_subcategory
            )
            candidates = tuple(kind for kind in brief.ask if not only or kind in only)
        else:
            found = self._briefs.for_search(request.commerce_category, request.commerce_subcategory)
            if found is None or (mode is BriefMode.NARROW and found.name in briefs.shown):
                return None
            brief, candidates = found, found.ask
        open_questions = [
            kind
            for kind in candidates
            if prefilled or not _answered(kind, brief, resolved, state)
        ]
        if not open_questions:
            return None

        asking_type = BriefQuestionKind.TYPE in open_questions
        subcategory = request.commerce_subcategory
        # No kind named means the kind is asked: the card for a whole
        # category always asks it, and its facts cover every kind offered.
        types = (
            tuple(kind.subcategory for kind in brief.kinds)
            if brief.kinds and (asking_type or subcategory is None)
            else (subcategory,)
            if subcategory
            else ()
        )
        facts = await self._repository.brief_facts(types, context)

        number = briefs.cards + 1
        questions: list[BriefQuestionView] = []
        kinds: tuple[BriefKindOption, ...] = ()
        budgets: tuple[BriefBudgetOption, ...] = ()
        spaces: tuple[BriefSpaceOption, ...] = ()
        rooms: tuple[BriefRoomOption, ...] = ()
        people: tuple[BriefPeopleOption, ...] = ()
        colours: tuple[str, ...] = ()
        styles: tuple[str, ...] = ()
        feels: tuple[BriefFeelOption, ...] = ()
        for kind in open_questions:
            match kind:
                case BriefQuestionKind.ROOM:
                    rooms = tuple(
                        BriefRoomOption(key=room.key, room=room.room) for room in self._briefs.rooms
                    )
                    question = _question(
                        kind,
                        [(r.key, self._word(r.label, language)) for r in self._briefs.rooms],
                        language=language,
                    )
                case BriefQuestionKind.PEOPLE:
                    people = _people(brief, facts, subcategory)
                    question = _question(
                        kind,
                        [(option.key, _people_label(option, language)) for option in people],
                        language=language,
                    )
                case BriefQuestionKind.TYPE:
                    kinds = self._kinds(brief, facts)
                    question = _question(
                        kind,
                        [(k.key, self._word(_kind_label(brief, k), language)) for k in kinds],
                        language=language,
                    )
                case BriefQuestionKind.BUDGET:
                    budgets = _budgets(facts)
                    question = _question(
                        kind,
                        [(b.key, _band_label(b, language)) for b in budgets],
                        language=language,
                    )
                case BriefQuestionKind.SPACE:
                    # Built from the pinned subcategory, not the kinds being
                    # asked: a sofa card knows it is a sofa, so sofa's ceilings
                    # are offered even while the seat count is still open. A
                    # card that has not pinned one ("I need a table") offers no
                    # space question - which ceiling would it use? - and the
                    # helper returns nothing, so the question is dropped below.
                    spaces = _spaces(subcategory)
                    question = _question(
                        kind,
                        [(s.key, _space_label(s, language)) for s in spaces],
                        language=language,
                    )
                case BriefQuestionKind.COLOUR:
                    colours = self._approved(AttributeFamily.COLOR, facts.colors)[
                        :MAX_COLOUR_CHOICES
                    ]
                    colour_labels = [
                        (c, self._value_label(AttributeFamily.COLOR, c, language)) for c in colours
                    ]
                    question = _question(
                        kind,
                        colour_labels,
                        MAX_BRIEF_COLOURS,
                        language=language,
                    )
                case BriefQuestionKind.STYLE:
                    styles = self._approved(AttributeFamily.STYLE, facts.styles)[:MAX_STYLE_CHOICES]
                    style_labels = [
                        (s, self._value_label(AttributeFamily.STYLE, s, language)) for s in styles
                    ]
                    question = _question(
                        kind,
                        style_labels,
                        MAX_BRIEF_STYLES,
                        language=language,
                    )
                case BriefQuestionKind.FEEL:
                    feels = tuple(
                        BriefFeelOption(key=f"feel-{index}", words=feel.words)
                        for index, feel in enumerate(brief.feels, start=1)
                    )
                    labels = [self._word(feel.label, language) for feel in brief.feels]
                    question = _question(
                        kind,
                        [(option.key, label) for option, label in zip(feels, labels, strict=True)],
                        label=self._word(brief.feel_label, language) if brief.feel_label else None,
                        language=language,
                    )
            if question is not None:
                questions.append(question)
        if not questions:
            return None
        if prefilled:
            questions = [
                question.model_copy(
                    update={
                        "selected": _selected(
                            question,
                            resolved,
                            kinds=kinds,
                            budgets=budgets,
                            spaces=spaces,
                            people=people,
                            feels=feels,
                        )
                    }
                )
                for question in questions
            ]

        # The family's own word, unless they already named a narrower kind:
        # "Show me rugs" for carpets, but "Show me sectional sofas" once they
        # asked for a sectional.
        family_word = brief.noun is not None and (asking_type or not brief.kinds)
        noun = (
            brief.noun
            if family_word and brief.noun
            else plural_words(customer_words(subcategory or request.commerce_category))
        )
        if language is ReplyLanguage.AR:
            # Both family nouns and narrower product types have registry-owned
            # display names; the search itself keeps its canonical key.
            arabic_noun = (
                self._briefs.arabic(noun)
                if family_word
                else self._taxonomy.arabic(subcategory or request.commerce_category)
            )
            submit = (
                CARD_SUBMIT[language].format(noun=arabic_noun)
                if arabic_noun
                else CARD_SUBMIT_ANY[language]
            )
            skip = CARD_SKIP[language].format(noun=arabic_noun) if arabic_noun else submit
            # A custom registry without a display name keeps its original noun.
            noun = arabic_noun or noun
        else:
            submit = CARD_SUBMIT[language].format(noun=noun)
            skip = CARD_SKIP[language].format(noun=noun)
        card = ProductBrief(
            card=number,
            mode=mode,
            noun=noun,
            questions=tuple(questions),
            submit_label=submit,
            skip_label=skip,
        )
        pending = PendingBrief(
            card=number,
            name=brief.name,
            base=resolved,
            kinds=kinds,
            budgets=budgets,
            spaces=spaces,
            colours=colours,
            styles=styles,
            feels=feels,
            rooms=rooms,
            people=people,
            must_ask=brief.must_ask if opening else None,
            opening=opening,
            # Only what it showed ticked: a typed "under 3,000" no band names
            # stays unless they choose a band to replace it.
            replaces=tuple(
                q.kind for q in questions if q.kind in _REPLACEABLE and q.selected
            ),
            narrowing=prefilled,
        )
        logger.info(
            "product_brief_built",
            store_id=context.store_id,
            brief=brief.name,
            mode=mode.value,
            opening=opening,
            questions=[question.kind.value for question in questions],
        )
        return BuiltBrief(card=card, pending=pending)

    def chips(self, search: ActiveSearchState, language: ReplyLanguage) -> tuple[BriefChip, ...]:
        """What the search on screen is using, as removable chips: the seats,
        the budget, a size, the colours and styles."""
        request = search.request
        chips: list[BriefChip] = []
        capacity = request.seating_capacity
        if capacity is not None and capacity.min_capacity is not None:
            exact = capacity.min_capacity == capacity.max_capacity
            wording = SEATS_EXACTLY if exact else SEATS_AT_LEAST
            label = wording[language].format(people=capacity.min_capacity)
            chips.append(BriefChip(facet="seats", label=label))
        elif search.seat_preference is not None:
            # A head count that orders, never filters: said as who it is for.
            label = SEATS_FOR[language].format(people=search.seat_preference)
            chips.append(BriefChip(facet="seats", label=label))
        if request.price is not None:
            band = BriefBudgetOption(
                key="price",
                currency=request.price.currency,
                min_amount=request.price.min_amount,
                max_amount=request.price.max_amount,
            )
            chips.append(BriefChip(facet="price", label=_band_label(band, language)))
        if request.dimensions or request.planar_dimensions is not None:
            chips.append(BriefChip(facet="size", label=_size_label(request, language)))
        if search.lean is not None and search.lean.space_cm is not None:
            label = SPACE_FOR[language].format(cm=_cm(search.lean.space_cm))
            chips.append(BriefChip(facet="space", label=label))
        for family, name, strict in (
            (AttributeFamily.COLOR, "colour", request.colors_any_of),
            (AttributeFamily.STYLE, "style", request.styles_all_of),
        ):
            chips.extend(
                BriefChip(
                    facet=f"{name}:{value}", label=self._value_label(family, value, language)
                )
                for value in _values(search, family, strict)
            )
        lean = search.lean
        if lean is not None:
            # Learned from what they liked, picked or asked more like of -
            # shown so they can see it, and take it off (phase 5).
            for learned_family, learned_name, learned in (
                (AttributeFamily.COLOR, "colour", lean.learned_colours),
                (AttributeFamily.STYLE, "style", lean.learned_styles),
            ):
                chips.extend(
                    BriefChip(
                        facet=f"learned:{learned_name}:{value}",
                        label=SUGGESTED[language].format(
                            value=self._value_label(learned_family, value, language)
                        ),
                    )
                    for value in learned
                )
        return tuple(chips)

    async def store_colours(self, context: RetailerContext, limit: int) -> tuple[str, ...]:
        """The store's own colours, most common first, approved by the registry.

        For a room's colour question: the chips should be real catalogue values,
        the same approval path the brief's own colour question uses, so a colour
        the data holds but the vocabulary does not is never offered. Counted
        store-wide - a room spans categories - and capped."""
        facts = await self._repository.facet_counts(context)
        return self._approved(AttributeFamily.COLOR, facts.colors)[:limit]

    def checks_room_on_pick(self, subcategory: str | None) -> bool:
        """Whether a pick of this kind is checked against the room (a bed)."""
        return self._briefs.checks_room_on_pick(subcategory)

    def by_look(self, subcategory: str | None) -> bool:
        """Whether this kind is chosen by its look - a mattress is not."""
        return self._briefs.by_look(subcategory)

    def arabic_names(self, family: AttributeFamily, values: tuple[str, ...]) -> tuple[str, ...]:
        """Approved values as they read in Arabic, in the same order - the
        reviewed registry's names, the value itself only where it has none."""
        return tuple(self._attributes.arabic(family, value) or value for value in values)

    def _word(self, label: str, language: ReplyLanguage) -> str:
        """One of the cards' reviewed labels, as it reads in `language`."""
        if language is ReplyLanguage.EN:
            return label
        return self._briefs.arabic(label) or label

    def _value_label(self, family: AttributeFamily, value: str, language: ReplyLanguage) -> str:
        """An approved colour or style as a chip reads it: its reviewed Arabic
        name, or the value with underscores as spaces."""
        if language is ReplyLanguage.AR and (name := self._attributes.arabic(family, value)):
            return name
        return _value_label(value)

    def answers_card(self, resolved: ResolvedSearch, state: AgentStateV1) -> bool:
        """Whether this search is their typed answer to the card on screen.

        The same product family, now saying something the card asked: "grey,
        a 3-seater" under the sofa card. Asking again would put the rest of
        the same card in front of them twice. A new family is a new card, and
        the need said again with nothing added is asked again, as any need is.
        """
        pending = state.product_brief.pending
        if pending is None:
            return False
        request = resolved.request
        brief = (
            self._briefs.for_opening(request.commerce_category, request.commerce_subcategory)
            if pending.opening
            else self._briefs.for_search(request.commerce_category, request.commerce_subcategory)
        )
        if brief is None or brief.name != pending.name:
            return False
        if pending.opening:
            # Anything they say about the same piece while the opening is on
            # screen is their answer - asking again would ask twice.
            return True
        if pending.narrowing:
            # Narrow down is answered by tapping it; words are a new need or a
            # refinement, read as such.
            return False
        return any(
            _answered(kind, brief, resolved, state)
            for kind in brief.ask
            if not _answered(kind, brief, pending.base, state)
        )

    @staticmethod
    def typed_head_count(
        resolved: ResolvedSearch, pending: PendingBrief, head_count: int | None
    ) -> ResolvedSearch:
        """A head count typed in reply to the opening, read as the tap reads it.

        "There are four of us" answers the same question as tapping 4, so it
        must find the same sofas: an order, not a filter, unless no single
        piece seats them (CLAUDE.md 17.1). Only a count the decision heard as
        people: "a 3-seater" names the piece, and stays a requirement. Anything
        else they typed is kept.
        """
        capacity = resolved.request.seating_capacity
        if not pending.opening or capacity is None or head_count is None:
            return resolved
        if capacity.min_capacity != head_count or capacity.max_capacity not in (
            None,
            head_count,
        ):
            return resolved
        option = next((p for p in pending.people if p.people == capacity.min_capacity), None)
        if option is None or option.combine:
            return resolved
        return resolved.model_copy(
            update={
                "request": resolved.request.model_copy(update={"seating_capacity": None}),
                "semantics": resolved.semantics.model_copy(
                    update={"seating_min": None, "seating_max": None}
                ),
                "seat_preference": option.people,
            }
        )

    def answer(self, action: BriefAnswerAction, pending: PendingBrief) -> BriefAnswer | None:
        """The search their answers make, and the room they named - or None
        when they name something the card did not offer: a stale card, or a
        key it never showed."""
        if action.card != pending.card:
            return None
        search = _cleared(pending.base, pending.replaces)
        room: str | None = None
        if action.room is not None:
            chosen_room = next((r for r in pending.rooms if r.key == action.room), None)
            if chosen_room is None:
                return None
            room = chosen_room.room
        if action.people is not None:
            head_count = next((p for p in pending.people if p.key == action.people), None)
            if head_count is None:
                return None
            search = _with_people(search, head_count)
        if action.piece is not None:
            kind = next((k for k in pending.kinds if k.key == action.piece), None)
            if kind is None:
                return None
            search = _with_kind(search, kind)
        if action.budget is not None:
            band = next((b for b in pending.budgets if b.key == action.budget), None)
            if band is None:
                return None
            search = _with_budget(search, band)
        if action.space is not None:
            # After the kind, so the ceiling is gated on the subcategory they
            # actually chose: a width that is trusted for a sofa is dropped if
            # they picked an L-shape instead (_with_space).
            option = next((s for s in pending.spaces if s.key == action.space), None)
            if option is None:
                return None
            search = _with_space(search, option)
        if not set(action.colours) <= set(pending.colours) or not set(action.styles) <= set(
            pending.styles
        ):
            return None
        for question, family, field, values in (
            (BriefQuestionKind.COLOUR, AttributeFamily.COLOR, "colors_any_of", action.colours),
            (BriefQuestionKind.STYLE, AttributeFamily.STYLE, "styles_all_of", action.styles),
        ):
            search = (
                _with_ticked(search, pending.base, family, field, values)
                if question in pending.replaces
                else _with_leanings(search, family, values)
            )
        if action.feel is not None:
            feel = next((f for f in pending.feels if f.key == action.feel), None)
            if feel is None:
                return None
            if feel.words.casefold() not in (search.semantic_text or "").casefold():
                search = search.model_copy(
                    update={"semantic_text": _joined(search.semantic_text, feel.words)}
                )
        # A fresh set: exclusions from paging never carry into a changed
        # request (CLAUDE.md 13.5).
        fresh = search.model_copy(
            update={"request": search.request.model_copy(update={"exclude_product_ids": ()})}
        )
        return BriefAnswer(search=fresh, room=room)

    # ── building ────────────────────────────────────────────────────────────

    def _kinds(self, brief: Brief, facts: BriefFacts) -> tuple[BriefKindOption, ...]:
        """The kinds this store really stocks, the one it has most of first.

        A seat count counts only products whose capacity was reviewed - an
        unverified one satisfies no seat choice (CLAUDE.md 31). Kinds the store
        holds as many of keep their reviewed order.
        """
        stocked: list[BriefKindOption] = []
        for kind in sorted(brief.kinds, key=lambda k: -_stock(k, facts)):
            if _stock(kind, facts) == 0:
                continue
            category = self._category_of(kind.subcategory)
            if category is None:
                continue
            stocked.append(
                BriefKindOption(
                    key=kind.key,
                    commerce_category=category,
                    commerce_subcategory=kind.subcategory,
                    seats=kind.seats,
                    min_seats=kind.min_seats,
                )
            )
        return tuple(stocked)

    def _category_of(self, subcategory: str) -> str | None:
        return next(
            (
                category
                for category in sorted(self._taxonomy.categories)
                if self._taxonomy.is_pair(category, subcategory)
            ),
            None,
        )

    def _approved(
        self, family: AttributeFamily, counts: tuple[tuple[str, int], ...]
    ) -> tuple[str, ...]:
        """Stored values the vocabulary approves, most products first, once each.

        Counted after spelling is normalised, so "light grey" and "Light Grey"
        stored by different merchants count as one colour.
        """
        totals: dict[str, int] = {}
        for raw, count in counts:
            canonical = self._attributes.canonical(family, raw)
            if canonical is not None:
                totals[canonical] = totals.get(canonical, 0) + count
        return tuple(sorted(totals, key=lambda value: -totals[value]))


# ── what is already known ───────────────────────────────────────────────────


def _answered(
    kind: BriefQuestionKind, brief: Brief, resolved: ResolvedSearch, state: AgentStateV1
) -> bool:
    """Whether they already said this - in this message or earlier, on record.

    Never asked twice: "I need a grey 3-seater" is not asked its colour or
    its kind, and a colour they settled on earlier in the chat counts too.
    """
    request = resolved.request
    remembered = state.customer_preferences.semantic_preferences
    match kind:
        case BriefQuestionKind.TYPE:
            if request.seating_capacity is not None:
                return True
            return any(
                choice.subcategory == request.commerce_subcategory
                and choice.seats is None
                and choice.min_seats is None
                for choice in brief.kinds
            )
        case BriefQuestionKind.ROOM:
            room = state.room_project
            return state.customer_preferences.room is not None or bool(
                room is not None and (room.room_type or room.room_kind)
            )
        case BriefQuestionKind.PEOPLE:
            return request.seating_capacity is not None or resolved.seat_preference is not None
        case BriefQuestionKind.BUDGET:
            return request.price is not None
        case BriefQuestionKind.SPACE:
            # Already given the space or a width - "a sofa under 200 cm wide" -
            # or the type carries no trusted width: nothing to ask either way.
            return (
                (resolved.lean is not None and resolved.lean.space_cm is not None)
                or any(
                    constraint.role is DimensionRole.OVERALL_WIDTH
                    for constraint in request.dimensions
                )
                or request.commerce_subcategory not in SPACE_WIDTHS
            )
        case BriefQuestionKind.COLOUR:
            return bool(request.colors_any_of) or _leans(
                AttributeFamily.COLOR, resolved.semantic_preferences, remembered
            )
        case BriefQuestionKind.STYLE:
            return bool(request.styles_all_of) or _leans(
                AttributeFamily.STYLE, resolved.semantic_preferences, remembered
            )
        case BriefQuestionKind.FEEL:
            return _names_a_feel(resolved.semantic_text, brief)


def _names_a_feel(text: str | None, brief: Brief) -> bool:
    """Whether their own words already name one of the card's feels - "a
    boucle sofa" is not asked its fabric again, while "a rug for the living
    room" still is.

    Spelling only: case and accents are ignored, every word of a feel's label
    must appear, and no word is mapped onto another (CLAUDE.md 14.2).
    """
    said = set(_words_of(text or ""))
    return bool(said) and any(
        set(_words_of(feel.label)) <= said for feel in brief.feels if _words_of(feel.label)
    )


def _words_of(text: str) -> list[str]:
    plain = "".join(
        char
        for char in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(char)
    )
    return re.findall(r"[a-z]+", plain)


def _leans(family: AttributeFamily, *preferences: tuple[SemanticPreference, ...]) -> bool:
    return any(p.family is family for group in preferences for p in group)


def _stock(kind: KindChoice, facts: BriefFacts) -> int:
    rows = [(seats, count) for sub, seats, count in facts.kinds if sub == kind.subcategory]
    if kind.seats is not None:
        return sum(count for seats, count in rows if seats == kind.seats)
    if kind.min_seats is not None:
        return sum(count for seats, count in rows if seats is not None and seats >= kind.min_seats)
    return sum(count for _seats, count in rows)


# ── the budget ──────────────────────────────────────────────────────────────


def _budgets(facts: BriefFacts) -> tuple[BriefBudgetOption, ...]:
    """Bands between the quartiles of what these products cost, rounded.

    Quartiles rather than an even split of the range, so each band holds
    about a quarter of the products: one very dear sofa would otherwise leave
    three bands nearly empty. Rounded to two significant figures, the way a
    person says a budget - "under 1,900", not "under 1,850.00".
    """
    if facts.currency is None or facts.price_quartiles is None:
        return ()
    edges: list[Decimal] = []
    for quartile in facts.price_quartiles:
        edge = _round_price(quartile)
        if edge > 0 and (not edges or edge > edges[-1]):
            edges.append(edge)
    if not edges:
        return ()
    currency = facts.currency
    bands = [BriefBudgetOption(key="budget-1", currency=currency, max_amount=edges[0])]
    for index, (low, high) in enumerate(pairwise(edges), start=2):
        bands.append(
            BriefBudgetOption(
                key=f"budget-{index}", currency=currency, min_amount=low, max_amount=high
            )
        )
    bands.append(
        BriefBudgetOption(key=f"budget-{len(edges) + 1}", currency=currency, min_amount=edges[-1])
    )
    return tuple(bands)


def _round_price(value: Decimal) -> Decimal:
    if value <= 0:
        return Decimal(0)
    quantum = Decimal(10) ** (value.adjusted() - 1)
    return (value / quantum).quantize(Decimal(1), rounding=ROUND_HALF_UP) * quantum


def _band_label(band: BriefBudgetOption, language: ReplyLanguage = ReplyLanguage.EN) -> str:
    currency = currency_word(band.currency, language)
    if band.min_amount is None:
        return BUDGET_UNDER[language].format(amount=_money(band.max_amount), currency=currency)
    if band.max_amount is None:
        return BUDGET_OVER[language].format(amount=_money(band.min_amount), currency=currency)
    return BUDGET_BETWEEN[language].format(
        low=_money(band.min_amount), high=_money(band.max_amount), currency=currency
    )


def _money(amount: Decimal | None) -> str:
    assert amount is not None
    return f"{int(amount):,}"


# ── the space ─────────────────────────────────────────────────────────────────


def _spaces(subcategory: str | None) -> tuple[BriefSpaceOption, ...]:
    """How wide the spot may be, for a kind the registry trusts, plus "not
    sure". Empty for a kind whose width is not asked - no space question is
    drawn then."""
    ceilings = SPACE_WIDTHS.get(subcategory or "")
    if not ceilings:
        return ()
    bands = tuple(
        BriefSpaceOption(key=f"space-{index}", max_cm=Decimal(ceiling))
        for index, ceiling in enumerate(ceilings, start=1)
    )
    return (*bands, BriefSpaceOption(key="space-any", max_cm=None))


def _space_label(option: BriefSpaceOption, language: ReplyLanguage = ReplyLanguage.EN) -> str:
    if option.max_cm is None:
        return SPACE_ANY[language]
    return SPACE_UPTO[language].format(width=int(option.max_cm))


def _with_space(search: ResolvedSearch, option: BriefSpaceOption) -> ResolvedSearch:
    """How wide the spot is, tapped: the space the piece goes in. It orders -
    what suits it first, anything wider last - and hides nothing, since a room
    being designed may use a piece differently (CLAUDE.md 10.10). "Any width"
    adds nothing, nor does a kind with no trusted width."""
    if option.max_cm is None or search.request.commerce_subcategory not in SPACE_WIDTHS:
        return search
    lean = (search.lean or RankingLean()).model_copy(
        update={"space_cm": option.max_cm, "space_fitted": False, "size_target_cm": None}
    )
    return search.model_copy(update={"lean": lean})


# ── the answers ─────────────────────────────────────────────────────────────


def _with_kind(search: ResolvedSearch, kind: BriefKindOption) -> ResolvedSearch:
    """Their kind of piece, as a requirement: they tapped it.

    A size stays only on the type it was given for - 60 cm for a sofa says
    nothing about a sofa set (CLAUDE.md 13.5). And the kind is the one type
    they chose: a tapped 3-seater is a 3-seat sofa, with no sets or sectionals
    shown beside it (CLAUDE.md 10.12).
    """
    request, semantics = search.request, search.semantics
    same_type = request.commerce_subcategory == kind.commerce_subcategory
    capacity = (
        SeatingCapacityConstraint.exactly(kind.seats)
        if kind.seats is not None
        else SeatingCapacityConstraint.at_least(kind.min_seats)
        if kind.min_seats is not None
        else None
    )
    request = request.model_copy(
        update={
            "commerce_category": kind.commerce_category,
            "commerce_subcategory": kind.commerce_subcategory,
            "seating_capacity": capacity,
            "single_type": True,
            "alongside_subcategories": (),
            **({} if same_type else {"dimensions": (), "planar_dimensions": None}),
        }
    )
    semantics = semantics.model_copy(
        update={
            "subcategory": ConstraintStrength.LOCKED,
            "seating_min": (
                ConstraintStrength.LOCKED if capacity and capacity.min_capacity else None
            ),
            "seating_max": (
                ConstraintStrength.LOCKED if capacity and capacity.max_capacity else None
            ),
            **({} if same_type else {"dimensions": (), "planar_dimension": None}),
        }
    )
    return ResolvedSearch(
        request=request,
        semantics=semantics,
        semantic_preferences=search.semantic_preferences,
        seat_preference=search.seat_preference,
        semantic_text=search.semantic_text,
        unmatched_strict=search.unmatched_strict,
        lean=search.lean if same_type or search.lean is None else search.lean.for_another_kind(),
    )


def _with_budget(search: ResolvedSearch, band: BriefBudgetOption) -> ResolvedSearch:
    """A band's ceiling is a limit - they chose it. Its floor is only where
    they said their money is: a cheaper piece they would like is not ruled
    out, so the floor is a preference relaxation may lower (CLAUDE.md 13)."""
    price = PriceConstraint(
        currency=band.currency, min_amount=band.min_amount, max_amount=band.max_amount
    )
    return search.model_copy(
        update={
            "request": search.request.model_copy(update={"price": price}),
            "semantics": search.semantics.model_copy(
                update={
                    "price_min": (
                        ConstraintStrength.PREFERRED if band.min_amount is not None else None
                    ),
                    "price_max": (
                        ConstraintStrength.LOCKED if band.max_amount is not None else None
                    ),
                }
            ),
        }
    )


def _with_leanings(
    search: ResolvedSearch, family: AttributeFamily, values: tuple[str, ...]
) -> ResolvedSearch:
    """Colours or styles they like, as preferences: they rank, never hide."""
    have = {p.canonical_value for p in search.semantic_preferences if p.family is family}
    added = tuple(
        SemanticPreference(
            family=family,
            raw_value=value,
            canonical_value=value,
            strength=ConstraintStrength.PREFERRED,
        )
        for value in dict.fromkeys(values)
        if value not in have
    )
    if not added:
        return search
    return search.model_copy(
        update={"semantic_preferences": (*search.semantic_preferences, *added)}
    )


def _with_ticked(
    search: ResolvedSearch,
    base: ResolvedSearch,
    family: AttributeFamily,
    field: str,
    values: tuple[str, ...],
) -> ResolvedSearch:
    """Ticked colours or styles on a card that showed them ticked: a value
    they required before stays required - re-sending Narrow down never softens
    "beige only" - the rest are liked, and an unticked one is gone."""
    required = tuple(v for v in getattr(base.request, field) if v in values)
    search = search.model_copy(
        update={"request": search.request.model_copy(update={field: required})}
    )
    return _with_leanings(search, family, tuple(v for v in values if v not in required))


def _joined(first: str | None, second: str) -> str:
    return " ".join(part for part in ((first or "").strip(), second) if part)


# ── wording ─────────────────────────────────────────────────────────────────


def _question(
    kind: BriefQuestionKind,
    choices: list[tuple[str, str]],
    max_choices: int = 1,
    *,
    label: str | None = None,
    language: ReplyLanguage = ReplyLanguage.EN,
) -> BriefQuestionView | None:
    """A question worth asking: at least two answers that lead somewhere."""
    if len(choices) < 2:
        return None
    return BriefQuestionView(
        kind=kind,
        label=label or CARD_QUESTIONS[kind][language],
        choices=tuple(BriefChoice(key=key, label=text) for key, text in choices),
        max_choices=max_choices,
    )


# ── narrowing ───────────────────────────────────────────────────────────────

_REPLACEABLE: Final[frozenset[BriefQuestionKind]] = frozenset(
    {
        BriefQuestionKind.BUDGET,
        BriefQuestionKind.SPACE,
        BriefQuestionKind.PEOPLE,
        BriefQuestionKind.COLOUR,
        BriefQuestionKind.STYLE,
    }
)
"""What a pre-filled answer replaces. Never the kind - unticked, sofas would
become all seating - nor the feel, whose words are mixed with their own."""


def _selected(
    question: BriefQuestionView,
    resolved: ResolvedSearch,
    *,
    kinds: Sequence[BriefKindOption],
    budgets: Sequence[BriefBudgetOption],
    spaces: Sequence[BriefSpaceOption],
    people: Sequence[BriefPeopleOption],
    feels: Sequence[BriefFeelOption],
) -> tuple[str, ...]:
    """The keys of this question already true of the search on screen."""
    request = resolved.request
    capacity = request.seating_capacity
    offered = {choice.key for choice in question.choices}
    match question.kind:
        case BriefQuestionKind.TYPE:
            keys = [k.key for k in kinds if _is_kind(k, request.commerce_subcategory, capacity)]
        case BriefQuestionKind.BUDGET:
            price = request.price
            keys = [
                b.key
                for b in budgets
                if price is not None
                and (b.min_amount, b.max_amount) == (price.min_amount, price.max_amount)
            ]
        case BriefQuestionKind.SPACE:
            space = resolved.lean.space_cm if resolved.lean is not None else None
            width = space or _width_ceiling(request.dimensions)
            keys = [s.key for s in spaces if width is not None and s.max_cm == width]
        case BriefQuestionKind.PEOPLE:
            head_count = resolved.seat_preference or (capacity.min_capacity if capacity else None)
            keys = [
                p.key
                for p in people
                if head_count is not None and p.people == min(head_count, MAX_HEAD_COUNT_CHIP)
            ]
        case BriefQuestionKind.COLOUR:
            keys = list(_values(resolved, AttributeFamily.COLOR, request.colors_any_of))
        case BriefQuestionKind.STYLE:
            keys = list(_values(resolved, AttributeFamily.STYLE, request.styles_all_of))
        case BriefQuestionKind.FEEL:
            text = (resolved.semantic_text or "").casefold()
            keys = [f.key for f in feels if f.words.casefold() in text]
        case _:
            keys = []
    return tuple(key for key in keys if key in offered)


def _is_kind(
    kind: BriefKindOption, subcategory: str | None, capacity: SeatingCapacityConstraint | None
) -> bool:
    if kind.commerce_subcategory != subcategory:
        return False
    if kind.seats is not None:
        return capacity is not None and capacity.min_capacity == capacity.max_capacity == kind.seats
    if kind.min_seats is not None:
        return (
            capacity is not None
            and capacity.min_capacity == kind.min_seats
            and capacity.max_capacity is None
        )
    return capacity is None


def _size_label(request: ProductSearchRequest, language: ReplyLanguage) -> str:
    """The sizes the search uses, in their own figures - "Up to 220 cm wide",
    "200 x 300 cm"."""
    parts = [
        SIZE_BOUND[language][d.kind.value].format(
            cm=_cm(d.max_cm or d.min_cm or d.target_cm),
            low=_cm(d.min_cm),
            high=_cm(d.max_cm),
            role=SIZE_ROLE[language][d.role],
        )
        for d in request.dimensions
    ]
    planar = request.planar_dimensions
    if planar is not None:
        first, second = planar.sides
        parts.append(SIZE_PAIR[language].format(first=_cm(first), second=_cm(second)))
    return " · ".join(parts)


def _cm(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f") if value % 1 else str(int(value))


def _width_ceiling(dimensions: Sequence[DimensionConstraint]) -> Decimal | None:
    return next(
        (
            d.max_cm
            for d in dimensions
            if d.role is DimensionRole.OVERALL_WIDTH and d.kind is DimensionConstraintKind.MAX
        ),
        None,
    )


def _values(
    search: ResolvedSearch | ActiveSearchState, family: AttributeFamily, strict: Sequence[str]
) -> tuple[str, ...]:
    """The approved values of a family the search uses: required, then liked."""
    liked = (
        p.canonical_value
        for p in search.semantic_preferences
        if p.family is family and p.canonical_value
    )
    return tuple(dict.fromkeys((*strict, *liked)))


def _cleared(search: ResolvedSearch, kinds: Sequence[BriefQuestionKind]) -> ResolvedSearch:
    """The search with the values of `kinds` taken away, for a pre-filled
    answer to set again - so a chip they unticked is gone."""
    request, semantics = search.request, search.semantics
    preferences = search.semantic_preferences
    seat_preference = search.seat_preference
    lean = search.lean
    if BriefQuestionKind.SPACE in kinds and lean is not None and lean.space_cm is not None:
        lean = _without_space(lean)
    if BriefQuestionKind.BUDGET in kinds:
        request = request.model_copy(update={"price": None})
        semantics = semantics.model_copy(update={"price_min": None, "price_max": None})
    if BriefQuestionKind.SPACE in kinds:
        width = DimensionRole.OVERALL_WIDTH
        request = request.model_copy(
            update={"dimensions": tuple(d for d in request.dimensions if d.role is not width)}
        )
        semantics = semantics.model_copy(
            update={"dimensions": tuple(d for d in semantics.dimensions if d.role is not width)}
        )
    if BriefQuestionKind.PEOPLE in kinds:
        request = request.model_copy(update={"seating_capacity": None})
        semantics = semantics.model_copy(update={"seating_min": None, "seating_max": None})
        seat_preference = None
    for kind, family, field in (
        (BriefQuestionKind.COLOUR, AttributeFamily.COLOR, "colors_any_of"),
        (BriefQuestionKind.STYLE, AttributeFamily.STYLE, "styles_all_of"),
    ):
        if kind in kinds:
            request = request.model_copy(update={field: ()})
            preferences = tuple(p for p in preferences if p.family is not family)
    return search.model_copy(
        update={
            "request": request,
            "semantics": semantics,
            "semantic_preferences": preferences,
            "seat_preference": seat_preference,
            "lean": lean,
        }
    )


def without_facet(search: ActiveSearchState, facet: str) -> ActiveSearchState | None:
    """The search on screen with one thing it uses taken away - ✕ on a chip -
    as a fresh set of results; None for a facet it does not use."""
    request, semantics = search.request, search.semantics
    preferences, seat_preference = search.semantic_preferences, search.seat_preference
    lean = search.lean
    family_value = facet.partition(":")
    match family_value:
        case ("learned", ":", rest) if lean is not None:
            name, _, value = rest.partition(":")
            field = {"colour": "learned_colours", "style": "learned_styles"}.get(name)
            if field is None or value not in getattr(lean, field):
                return None
            lean = lean.model_copy(
                update={field: tuple(v for v in getattr(lean, field) if v != value)}
            )
        case ("price", "", ""):
            if request.price is None:
                return None
            request = request.model_copy(update={"price": None})
            semantics = semantics.model_copy(update={"price_min": None, "price_max": None})
        case ("seats", "", ""):
            if request.seating_capacity is None and seat_preference is None:
                return None
            request = request.model_copy(update={"seating_capacity": None})
            semantics = semantics.model_copy(update={"seating_min": None, "seating_max": None})
            seat_preference = None
        case ("space", "", ""):
            if lean is None or lean.space_cm is None:
                return None
            lean = _without_space(lean)
        case ("size", "", ""):
            if not request.dimensions and request.planar_dimensions is None:
                return None
            request = request.model_copy(update={"dimensions": (), "planar_dimensions": None})
            semantics = semantics.model_copy(update={"dimensions": (), "planar_dimension": None})
        case ("colour" | "style" as name, ":", value) if value:
            family = AttributeFamily.COLOR if name == "colour" else AttributeFamily.STYLE
            field = "colors_any_of" if name == "colour" else "styles_all_of"
            strict = getattr(request, field)
            kept = tuple(
                p for p in preferences if not (p.family is family and p.canonical_value == value)
            )
            if value not in strict and len(kept) == len(preferences):
                return None
            request = request.model_copy(update={field: tuple(v for v in strict if v != value)})
            preferences = kept
        case _:
            return None
    return search.model_copy(
        update={
            "request": request.model_copy(update={"exclude_product_ids": ()}),
            "semantics": semantics,
            "semantic_preferences": preferences,
            "seat_preference": seat_preference,
            "lean": lean,
        }
    )


# ── the opening ─────────────────────────────────────────────────────────────

MAX_HEAD_COUNT_CHIP: Final[int] = 6
"""The last head-count chip reads "6+": past it, a number is typed."""


@dataclass(frozen=True, slots=True)
class BriefAnswer:
    """What a tapped card makes: the search, and the room they named."""

    search: ResolvedSearch
    room: str | None = None


def opening_asked(
    card: ProductBrief, pending: PendingBrief | None, chosen: Sequence[BriefQuestionKind]
) -> tuple[BriefQuestionKind, ...]:
    """The questions an opening on screen asks: the ones the reply chose, or -
    when the reply fell back to a fixed sentence - the first of those offered,
    in the family's own order. Empty for a card, which asks them all."""
    if pending is None or not pending.opening:
        return ()
    offered = tuple(question.kind for question in card.questions)
    always = [kind for kind in pending.always_asked if kind in offered]
    if chosen and set(chosen) <= set(offered) and set(always) <= set(chosen):
        return tuple(chosen)
    others = [kind for kind in offered if kind not in always]
    fallback = set((always + others)[:OPENING_QUESTIONS])
    return tuple(kind for kind in offered if kind in fallback)


def record_asked(state: AgentStateV1, kinds: Sequence[BriefQuestionKind]) -> AgentStateV1:
    """The session after the opening on screen asked `kinds`: neither is
    asked again in its family, answered or not."""
    briefs = state.product_brief
    pending = briefs.pending
    if pending is None or not pending.opening or not kinds:
        return state
    keys = (_opening_key(pending.name, kind) for kind in kinds)
    asked = tuple(dict.fromkeys((*briefs.asked, *keys)))[-MAX_OPENING_ASKED:]
    return state.model_copy(update={"product_brief": briefs.model_copy(update={"asked": asked})})


def _opening_key(family: str, kind: BriefQuestionKind) -> str:
    """How an asked question is remembered: per family, except the room - one
    question for the whole visit, whatever they are looking for next."""
    if kind is BriefQuestionKind.ROOM:
        return kind.value
    return f"{family}:{kind.value}"


def _people(
    brief: Brief, facts: BriefFacts, subcategory: str | None
) -> tuple[BriefPeopleOption, ...]:
    """Head counts from two to "6+", for a family whose pieces seat several.

    A count no single stocked piece of the type being searched seats is a
    requirement that leads to a combination - or to another type that seats
    them in one piece (CLAUDE.md 27.1); the rest only order. None when no such
    piece has a reviewed seat count - there would be nothing to order by.
    """
    family = {subcategory} if subcategory else set(brief.subcategories)
    seats = [
        capacity
        for subcategory, capacity, count in facts.kinds
        if subcategory in family and capacity is not None and count
    ]
    if not seats:
        return ()
    largest = max(seats)
    return tuple(
        BriefPeopleOption(
            key=f"people-{people}",
            people=people,
            or_more=people == MAX_HEAD_COUNT_CHIP,
            combine=people > largest,
        )
        for people in range(2, MAX_HEAD_COUNT_CHIP + 1)
    )


def _people_label(option: BriefPeopleOption, language: ReplyLanguage) -> str:
    if option.or_more:
        return PEOPLE_OR_MORE[language].format(people=option.people)
    return str(option.people)


def _with_people(search: ResolvedSearch, option: BriefPeopleOption) -> ResolvedSearch:
    """How many usually sit there: an order, or - when no single piece seats
    them - the seat requirement a combination is built for."""
    if not option.combine:
        return search.model_copy(update={"seat_preference": option.people})
    return search.model_copy(
        update={
            "request": search.request.model_copy(
                update={"seating_capacity": SeatingCapacityConstraint.at_least(option.people)}
            ),
            "semantics": search.semantics.model_copy(
                update={"seating_min": ConstraintStrength.LOCKED}
            ),
        }
    )


def _kind_label(brief: Brief, option: BriefKindOption) -> str:
    return next(kind.label for kind in brief.kinds if kind.key == option.key)


def _value_label(value: str) -> str:
    """An approved value as a chip reads it: underscores become spaces."""
    return value.replace("_", " ")


def _without_space(lean: RankingLean) -> RankingLean | None:
    """The lean with no space, and no width suggested for one."""
    cleared = lean.model_copy(
        update={"space_cm": None, "space_fitted": False, "size_target_cm": None}
    )
    return None if cleared == RankingLean() else cleared
