"""How a currency reads on a card or a chip, in each reply language."""

from __future__ import annotations

from app.schemas.language import ReplyLanguage


def currency_word(currency: str, language: ReplyLanguage) -> str:
    """The currency as a chip reads it: "ريال" for riyals in Arabic, the code
    otherwise - an unfamiliar currency is never guessed into a word."""
    if language is ReplyLanguage.AR and currency.upper() == "SAR":
        return "ريال"
    return currency
