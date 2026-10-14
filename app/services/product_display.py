"""A product's card text, ready to show, in the reply's language.

The console and a store's widget show what this produces and decide nothing:
the name - in Arabic, the store's `name_arabic` exactly as stored - and the
kind, colour and styles in the registries' reviewed words (`commerce_v1.yaml`,
`catalog_attributes_v1.yaml`), with the seats, price and listed size worded for
the language, in Western digits (docs/arabic-replies-plan.md). A lookup and a
format, not a translation: nothing here guesses a word, and a value with no
reviewed Arabic is shown as stored.

Display only: searches, filters, comparisons and picks keep the English values,
so this changes what a customer reads and nothing the application decides.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.schemas.bundle_presentation import (
    BundleItemDisplay,
    BundleTotalsDisplay,
    GroundedBundleItem,
    GroundedBundlePresentation,
    GroundedBundleTotals,
)
from app.schemas.chat import ChatPresentation
from app.schemas.dimensions import DimensionStatus, NormalisedDimensions
from app.schemas.grounding import GroundedProduct
from app.schemas.language import ReplyLanguage
from app.schemas.product import CommerceClassification, ProductDisplay
from app.services.chip_wording import currency_word
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.words import customer_words

_SIZE_WORDS = {
    ReplyLanguage.EN: ("L", "W", "H", "cm"),
    ReplyLanguage.AR: ("الطول", "العرض", "الارتفاع", "سم"),
}


class ShownProduct(Protocol):
    """What a card is drawn from - a grounded product, a candidate, a pick's."""

    @property
    def name_english(self) -> str: ...
    @property
    def name_arabic(self) -> str: ...
    @property
    def commerce(self) -> CommerceClassification: ...
    @property
    def main_color(self) -> str | None: ...
    @property
    def styles(self) -> tuple[str, ...]: ...
    @property
    def price_amount(self) -> Decimal: ...
    @property
    def price_unit(self) -> str: ...
    @property
    def dimensions(self) -> NormalisedDimensions: ...


class ProductDisplayWords:
    """Card text from the reviewed registries, in English or Arabic."""

    def __init__(self, taxonomy: CommerceTaxonomy, attributes: CatalogAttributes) -> None:
        self._taxonomy = taxonomy
        self._attributes = attributes

    def product(self, product: ShownProduct, language: ReplyLanguage) -> ProductDisplay:
        """One product's card text in `language`."""
        commerce = product.commerce
        return ProductDisplay(
            name=(
                arabic_name(product.name_english, product.name_arabic)
                if language is ReplyLanguage.AR
                else product.name_english
            ),
            kind=self.kind(commerce.subcategory, language)
            or self.kind(commerce.category, language),
            color=(
                self.value(AttributeFamily.COLOR, product.main_color, language)
                if product.main_color
                else None
            ),
            styles=tuple(
                self.value(AttributeFamily.STYLE, style, language) for style in product.styles
            ),
            seats=(
                seat_words(commerce.seating_capacity, language)
                if commerce.seating_capacity is not None
                else None
            ),
            price=price_words(product.price_amount, product.price_unit, language),
            size=size_words(product.dimensions, language),
        )

    def bundle_item(self, item: GroundedBundleItem, language: ReplyLanguage) -> BundleItemDisplay:
        """One room piece's card text in `language`: its name, kind, seats, the
        price of one and what the line adds to the bill."""
        commerce = item.commerce
        return BundleItemDisplay(
            name=(
                arabic_name(item.name_english, item.name_arabic)
                if language is ReplyLanguage.AR
                else item.name_english
            ),
            kind=(
                self.kind(commerce.subcategory, language) or self.kind(commerce.category, language)
            ),
            seats=(
                seat_words(commerce.seating_capacity, language)
                if commerce.seating_capacity is not None
                else None
            ),
            price=price_words(item.unit_price, item.price_unit, language),
            line_total=(
                price_words(item.new_spend_line_total, item.price_unit, language)
                if item.new_spend_line_total is not None
                else None
            ),
        )

    @staticmethod
    def bundle_totals(totals: GroundedBundleTotals, language: ReplyLanguage) -> BundleTotalsDisplay:
        """The room's new spend and the budget beside it, in `language`."""
        return BundleTotalsDisplay(
            new_spend=(
                price_words(totals.new_spend_total, totals.currency, language)
                if totals.new_spend_total is not None and totals.currency is not None
                else None
            ),
            budget=(
                price_words(totals.budget_max_amount, totals.budget_currency, language)
                if totals.budget_max_amount is not None and totals.budget_currency is not None
                else None
            ),
        )

    def kind(self, key: str | None, language: ReplyLanguage) -> str | None:
        """A category's or sub-category's name: "lounge chair" / "كرسي استرخاء".
        A key with no reviewed Arabic reads in English."""
        if not key:
            return None
        if language is ReplyLanguage.AR:
            arabic = self._taxonomy.arabic(key)
            if arabic:
                return arabic
        return customer_words(key)

    def value(self, family: AttributeFamily, value: str, language: ReplyLanguage) -> str:
        """An approved colour's or style's name, read through its approved
        spelling - a stored "light grey" is the registry's own - with a style's
        underscores read as spaces. A value the registry does not approve is
        shown as stored."""
        canonical = self._attributes.canonical(family, value)
        if canonical is None:
            return value.replace("_", " ")
        if language is ReplyLanguage.AR:
            arabic = self._attributes.arabic(family, canonical)
            if arabic:
                return arabic
        return canonical.replace("_", " ")

    def labels(
        self, family: AttributeFamily, values: Sequence[str], language: ReplyLanguage
    ) -> dict[str, str]:
        """Each value's name, by the value a filter sends back."""
        return {value: self.value(family, value, language) for value in values}


def displayed(
    presentation: ChatPresentation | None,
    words: ProductDisplayWords | None,
    language: ReplyLanguage,
) -> ChatPresentation | None:
    """A reply's cards, the product in focus, the products compared and every
    room piece and total, each with its text ready to show in `language` -
    display only, beside the English every later turn reads. Shared by every
    reply a turn sends: the chat, a photo's matches and a room's picture."""
    if words is None or presentation is None:
        return presentation

    def card(product: GroundedProduct) -> GroundedProduct:
        return product.model_copy(update={"display": words.product(product, language)})

    def room(bundle: GroundedBundlePresentation) -> GroundedBundlePresentation:
        return bundle.model_copy(
            update={
                "items": tuple(
                    item.model_copy(update={"display": words.bundle_item(item, language)})
                    for item in bundle.items
                ),
                "totals": bundle.totals.model_copy(
                    update={"display": words.bundle_totals(bundle.totals, language)}
                ),
            }
        )

    comparison = presentation.comparison
    return presentation.model_copy(
        update={
            "products": tuple(card(product) for product in presentation.products),
            "focus": card(presentation.focus) if presentation.focus is not None else None,
            "comparison": (
                comparison.model_copy(
                    update={"products": tuple(card(p) for p in comparison.products)}
                )
                if comparison is not None
                else None
            ),
            "room": room(presentation.room) if presentation.room is not None else None,
            "seating_bundles": tuple(room(b) for b in presentation.seating_bundles),
            "chosen_seating": (
                room(presentation.chosen_seating)
                if presentation.chosen_seating is not None
                else None
            ),
        }
    )


def arabic_name(name_english: str, name_arabic: str | None) -> str:
    """The name a customer answered in Arabic reads: the store's Arabic name
    exactly as stored, whatever language it was written in - the English name
    only where it is empty."""
    stored = (name_arabic or "").strip()
    return stored or name_english


def seat_words(count: int, language: ReplyLanguage) -> str:
    """ "4-seat" / "4 مقاعد" - with the Arabic singular, dual and plural forms."""
    if language is ReplyLanguage.EN:
        return f"{count}-seat"
    if count == 1:
        return "مقعد واحد"
    if count == 2:
        return "مقعدان"
    if 3 <= count <= 10:
        return f"{count} مقاعد"
    return f"{count} مقعدًا"


def price_words(amount: Decimal, unit: str, language: ReplyLanguage) -> str:
    """ "3,600 SAR" / "3,600 ريال": a thousands separator, no decimals when whole,
    and riyals named as the replies and the budget chips name them."""
    return f"{_figure(amount, 2)} {currency_word(unit, language)}"


def size_words(dimensions: NormalisedDimensions, language: ReplyLanguage) -> str | None:
    """The listed size, or None where it did not normalise - a size in an
    unknown unit is never shown as centimetres."""
    if dimensions.status is not DimensionStatus.NORMALISED:
        return None
    length, width, height, unit = _SIZE_WORDS[language]
    parts = [
        f"{word} {_figure(value, 1)}"
        for word, value in (
            (length, dimensions.length_cm),
            (width, dimensions.width_cm),
            (height, dimensions.height_cm),
        )
        if value is not None
    ]
    return f"{' · '.join(parts)} {unit}" if parts else None


def _figure(value: Decimal, places: int) -> str:
    """A figure in Western digits with a thousands separator, its decimals only
    where it has some."""
    rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral_value():
        return f"{int(rounded):,}"
    return f"{rounded:,.{places}f}".rstrip("0").rstrip(".")
