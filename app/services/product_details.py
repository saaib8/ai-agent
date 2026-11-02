"""One product's long text, made safe to give an agent.

The description is the store's words and the reviews are customers' words: an
agent quotes them and never obeys them (CLAUDE.md 20.1). Reviews are cleaned of
contact details and links, cut short, and only those with text are kept -
verified buyers first, then the newest. The rating and the review count are the
store's own totals (`ProductListing`), never recounted from what is stored here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from app.core.config import ProductDataSettings
from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.schemas.product import ProductDetailsText, ProductReview
from app.schemas.retailer import RetailerContext

logger = get_logger(__name__)

_ARABIC = re.compile(r"[\u0600-\u06ff]")
_LINK = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|sa|me|io|co|store|shop|ae)(?:/\S*)?\b",
    re.IGNORECASE,
)
"""A link, or a bare web address ("cheapsofas.com", "wa.me/966...")."""
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_.]{2,}")
"""A social handle: "@my_store"."""
_DIGIT_RUN = re.compile(r"\+?\d[\d\s().-]*\d")
_DATE = re.compile(r"^\d{1,4}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,4}$")
PHONE_MIN_DIGITS = 9
"""A phone number has at least nine digits (Saudi mobiles: 05XXXXXXXX). Fewer is
a size, a date or a count - "200 180 90 cm", "2025-01-02" - and stays."""


def _hide_phone(match: re.Match[str]) -> str:
    run = match.group(0)
    digits = sum(c.isdigit() for c in run)
    groups = re.findall(r"\d+", run)
    if digits < PHONE_MIN_DIGITS or _DATE.match(run.strip()):
        return run
    # Sizes typed together ("200 180 90 120 60") are short groups apart by
    # spaces; a phone is one long group or groups joined by +, -, . or ().
    if all(len(g) <= 3 for g in groups) and not re.search(r"[+().-]", run):
        return run
    return " "


class ProductDetailsService:
    def __init__(self, repository: ProductRepository, settings: ProductDataSettings) -> None:
        self._repository = repository
        self._settings = settings

    @property
    def description_max_chars(self) -> int:
        return self._settings.description_max_chars

    async def text_of(self, product_id: int, context: RetailerContext) -> ProductDetailsText | None:
        """The product's description and reviews, cleaned - None when the active
        store has no such product."""
        found = await self._repository.details_text(product_id, context)
        if found is None:
            return None
        return ProductDetailsText(
            description=tidy(found.description),
            description_english=tidy(found.description_english),
            reviews=shown_reviews(found.reviews, self._settings),
        )


def description_for(text: ProductDetailsText, arabic: bool, max_chars: int) -> str | None:
    """The description in the reply's language where the store has it: the
    stored text for an Arabic reply when it is Arabic, else the English one
    the import made, else whatever is stored - cut at a word."""
    stored, english = text.description, text.description_english
    chosen: str | None = stored if arabic and stored and _ARABIC.search(stored) else None
    chosen = chosen or english or stored
    return cut(chosen, max_chars) if chosen else None


def tidy(text: str | None) -> str | None:
    """Whitespace collapsed; None where nothing is left."""
    cleaned = " ".join((text or "").split())
    return cleaned or None


def cut(text: str, max_chars: int) -> str:
    """At most `max_chars`, cut at a word and marked as cut."""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return f"{head}…"


def shown_reviews(raw: Any, settings: ProductDataSettings) -> tuple[ProductReview, ...]:
    """The reviews with text an agent may read, best first.

    `raw` is `reviews_json` as stored: a list of Salla's reviews. Anything that
    is not one is skipped rather than guessed at, and logged by kind only.
    """
    entries = _entries(raw)
    reviews = [r for r in (_review(e, settings.reviews_max_chars) for e in entries) if r]
    reviews.sort(key=lambda r: (not r.verified_buyer, -_day(r.written_on)))
    return tuple(reviews[: settings.reviews_max_shown])


def _entries(raw: Any) -> Sequence[Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            logger.warning("reviews_unreadable", reason="not_json")
            return ()
    if raw is None:
        return ()
    if not isinstance(raw, list):
        logger.warning("reviews_unreadable", reason=type(raw).__name__)
        return ()
    return raw


def _review(entry: Any, max_chars: int) -> ProductReview | None:
    if not isinstance(entry, dict):
        return None
    stars = entry.get("stars")
    if isinstance(stars, bool) or not isinstance(stars, int) or not 0 <= stars <= 5:
        return None
    text = _clean(entry.get("text"))
    if text is None:
        return None
    return ProductReview(
        stars=stars or None,
        text=cut(text, max_chars),
        verified_buyer=_yes(entry.get("verified_buyer")),
        written_on=_date(entry.get("created_at")),
    )


def _clean(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    for pattern in (_EMAIL, _LINK, _HANDLE):
        text = pattern.sub(" ", text)
    return tidy(_DIGIT_RUN.sub(_hide_phone, text))


def _yes(value: Any) -> bool:
    """A verified flag as stored: true, 1 or "true"."""
    return value is True or value == 1 or (isinstance(value, str) and value.lower() == "true")


def _date(value: Any) -> str | None:
    """The day a review was written, from an ISO timestamp or Salla's
    {"date": ...} object; None where it cannot be read."""
    if isinstance(value, dict):
        value = value.get("date")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace(" ", "T", 1)).date().isoformat()
    except ValueError:
        return None


def _day(written_on: str | None) -> int:
    """A date as a day number, for newest first; an undated review sorts last."""
    if written_on is None:
        return 0
    return date.fromisoformat(written_on).toordinal()
