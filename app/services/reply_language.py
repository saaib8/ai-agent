"""Which language a session is answered in (docs/arabic-replies-plan.md).

The rule, decided with the user:

- A session is English until something Arabic is said. Arabic letters in a
  message, or Arabic written in Latin letters ("abi kanaba", read by the
  decision model), move it to Arabic.
- Arabic is sticky: an English word, a figure, a product name or a chip tap
  never moves it back. Only an explicit request ("English please") does.
- English is stored only when the customer asked for it. So an undecided
  session is English by default, and an explicit English choice is never
  undone by a stray Arabic word.
- The storefront's `locale` answers a turn nothing else decided, and is never
  stored.

Everything here is pure and deterministic. The model is consulted only for
what code cannot read - Arabizi and an explicit request - through two fields on
the decision it already makes; no extra call.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.schemas.language import LanguageSource, ReplyLanguage

MIN_ARABIC_LETTERS = 2
"""Below this, a stray Arabic letter (a pasted product name, a typo) says
nothing about the language the customer writes in."""


@dataclass(frozen=True, slots=True)
class TurnLanguage:
    stored: ReplyLanguage | None
    """What the session holds after this turn."""

    reply: ReplyLanguage
    """What this turn is answered in."""

    source: LanguageSource


def writes_arabic_script(message: str) -> bool:
    """Whether a message is written in Arabic letters.

    Letters only: digits (Western or Arabic-Indic), emoji and punctuation say
    nothing about the language. Arabic must be at least as common as Latin, so
    an English sentence quoting one Arabic product name stays English, while an
    Arabic sentence quoting "L-shape" stays Arabic.
    """
    arabic = latin = 0
    for char in message:
        if not unicodedata.category(char).startswith("L"):
            continue
        if "ARABIC" in unicodedata.name(char, ""):
            arabic += 1
        elif "LATIN" in unicodedata.name(char, ""):
            latin += 1
    return arabic >= MIN_ARABIC_LETTERS and arabic >= latin


def before_the_turn(
    stored: ReplyLanguage | None, message: str, locale: ReplyLanguage | None
) -> TurnLanguage:
    """The language a turn starts in, from what code can read alone.

    Settled before anything runs, so every path - a screen action, a
    re-decided turn, the "please say it another way" fallback - answers in it.
    """
    if writes_arabic_script(message) and stored is not ReplyLanguage.EN:
        source = LanguageSource.SESSION if stored is ReplyLanguage.AR else LanguageSource.SCRIPT
        return TurnLanguage(stored=ReplyLanguage.AR, reply=ReplyLanguage.AR, source=source)
    if stored is not None:
        return TurnLanguage(stored=stored, reply=stored, source=LanguageSource.SESSION)
    if locale is not None:
        return TurnLanguage(stored=None, reply=locale, source=LanguageSource.LOCALE)
    return TurnLanguage(stored=None, reply=ReplyLanguage.EN, source=LanguageSource.DEFAULT)


def after_the_decision(
    started: TurnLanguage,
    *,
    switch_to: ReplyLanguage | None,
    writes_arabizi: bool,
) -> TurnLanguage:
    """The language once the decision has read the message.

    An explicit request wins, either way. Arabizi moves a session to Arabic
    like Arabic letters do - never one the customer explicitly made English.
    """
    if switch_to is not None:
        return TurnLanguage(stored=switch_to, reply=switch_to, source=LanguageSource.SWITCH)
    if writes_arabizi and started.stored is None:
        return TurnLanguage(
            stored=ReplyLanguage.AR, reply=ReplyLanguage.AR, source=LanguageSource.ARABIZI
        )
    return started
