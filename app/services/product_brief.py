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
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Final

from app.core.logging import get_logger
from app.repositories.products import BriefFacts, ProductRepository
from app.schemas.agent_state import AgentStateV1
from app.schemas.discovery import PriceConstraint, SeatingCapacityConstraint
from app.schemas.language import ReplyLanguage
from app.schemas.product_brief import (
    MAX_BRIEF_COLOURS,
    MAX_BRIEF_STYLES,
    BriefBudgetOption,
    BriefChoice,
    BriefFeelOption,
    BriefKindOption,
    BriefMode,
    BriefQuestionView,
    PendingBrief,
    ProductBrief,
)
from app.schemas.query import ConstraintStrength, ResolvedSearch, SemanticPreference
from app.schemas.retailer import RetailerContext
from app.schemas.search_action import BriefAnswerAction
from app.services.chip_wording import (
    BUDGET_BETWEEN,
    BUDGET_OVER,
    BUDGET_UNDER,
    CARD_QUESTIONS,
    CARD_SUBMIT,
    CARD_SUBMIT_ANY,
    currency_word,
)
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.briefs import Brief, BriefQuestionKind, Briefs, KindChoice
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.words import customer_words

logger = get_logger(__name__)

MAX_COLOUR_CHOICES: Final[int] = 8
MAX_STYLE_CHOICES: Final[int] = 6
"""The colours and styles most of these products carry - enough to find
theirs, few enough to scan."""



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
        """
        request = resolved.request
        brief = self._briefs.for_search(request.commerce_category, request.commerce_subcategory)
        briefs = state.product_brief
        if brief is None or (mode is BriefMode.NARROW and brief.name in briefs.shown):
            return None
        open_questions = [kind for kind in brief.ask if not _answered(kind, brief, resolved, state)]
        if not open_questions:
            return None

        asking_type = BriefQuestionKind.TYPE in open_questions
        subcategory = request.commerce_subcategory
        # No kind named means the kind is asked: the card for a whole
        # category always asks it, and its facts cover every kind offered.
        types = (
            tuple(kind.subcategory for kind in brief.kinds)
            if asking_type or subcategory is None
            else (subcategory,)
        )
        facts = await self._repository.brief_facts(types, context)

        number = briefs.cards + 1
        questions: list[BriefQuestionView] = []
        kinds: tuple[BriefKindOption, ...] = ()
        budgets: tuple[BriefBudgetOption, ...] = ()
        colours: tuple[str, ...] = ()
        styles: tuple[str, ...] = ()
        feels: tuple[BriefFeelOption, ...] = ()
        for kind in open_questions:
            match kind:
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

        # The family's own word, unless they already named a narrower kind:
        # "Show me rugs" for carpets, but "Show me sectional sofas" once they
        # asked for a sectional.
        family_word = brief.noun is not None and (asking_type or not brief.kinds)
        noun = (
            brief.noun
            if family_word and brief.noun
            else _plural(customer_words(subcategory or request.commerce_category))
        )
        if language is ReplyLanguage.AR:
            # No Arabic product-type names exist yet: the family's reviewed noun
            # when the card names its family, and no kind otherwise.
            arabic_noun = self._briefs.arabic(noun) if family_word else None
            submit = (
                CARD_SUBMIT[language].format(noun=arabic_noun)
                if arabic_noun
                else CARD_SUBMIT_ANY[language]
            )
            # `noun` stays what is being looked for: the family's Arabic word,
            # or the searched kind's English until Arabic type names exist.
            noun = arabic_noun or noun
        else:
            submit = CARD_SUBMIT[language].format(noun=noun)
        card = ProductBrief(
            card=number,
            mode=mode,
            noun=noun,
            questions=tuple(questions),
            submit_label=submit,
        )
        pending = PendingBrief(
            card=number,
            name=brief.name,
            base=resolved,
            kinds=kinds,
            budgets=budgets,
            colours=colours,
            styles=styles,
            feels=feels,
        )
        logger.info(
            "product_brief_built",
            store_id=context.store_id,
            brief=brief.name,
            mode=mode.value,
            questions=[question.kind.value for question in questions],
        )
        return BuiltBrief(card=card, pending=pending)

    async def store_colours(self, context: RetailerContext, limit: int) -> tuple[str, ...]:
        """The store's own colours, most common first, approved by the registry.

        For a room's colour question: the chips should be real catalogue values,
        the same approval path the brief's own colour question uses, so a colour
        the data holds but the vocabulary does not is never offered. Counted
        store-wide - a room spans categories - and capped."""
        facts = await self._repository.facet_counts(context)
        return self._approved(AttributeFamily.COLOR, facts.colors)[:limit]

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
        brief = self._briefs.for_search(request.commerce_category, request.commerce_subcategory)
        if brief is None or brief.name != pending.name:
            return False
        return any(
            _answered(kind, brief, resolved, state)
            for kind in brief.ask
            if not _answered(kind, brief, pending.base, state)
        )

    def answer(self, action: BriefAnswerAction, pending: PendingBrief) -> ResolvedSearch | None:
        """The search their answers make, or None when they name something the
        card did not offer - a stale card, or a key it never showed."""
        if action.card != pending.card:
            return None
        search = pending.base
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
        if not set(action.colours) <= set(pending.colours) or not set(action.styles) <= set(
            pending.styles
        ):
            return None
        search = _with_leanings(search, AttributeFamily.COLOR, action.colours)
        search = _with_leanings(search, AttributeFamily.STYLE, action.styles)
        if action.feel is not None:
            feel = next((f for f in pending.feels if f.key == action.feel), None)
            if feel is None:
                return None
            search = search.model_copy(
                update={"semantic_text": _joined(search.semantic_text, feel.words)}
            )
        # A fresh set: exclusions from paging never carry into a changed
        # request (CLAUDE.md 13.5).
        return search.model_copy(
            update={"request": search.request.model_copy(update={"exclude_product_ids": ()})}
        )

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
        case BriefQuestionKind.BUDGET:
            return request.price is not None
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


# ── the answers ─────────────────────────────────────────────────────────────


def _with_kind(search: ResolvedSearch, kind: BriefKindOption) -> ResolvedSearch:
    """Their kind of piece, as a requirement: they tapped it.

    A size stays only on the type it was given for - 60 cm for a sofa says
    nothing about a sofa set (CLAUDE.md 13.5).
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
        semantic_text=search.semantic_text,
        unmatched_strict=search.unmatched_strict,
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


def _kind_label(brief: Brief, option: BriefKindOption) -> str:
    return next(kind.label for kind in brief.kinds if kind.key == option.key)


def _value_label(value: str) -> str:
    """An approved value as a chip reads it: underscores become spaces."""
    return value.replace("_", " ")


def _plural(words: str) -> str:
    if words.endswith(("s", "x", "ch", "sh")):
        return f"{words}es"
    return f"{words}s"
