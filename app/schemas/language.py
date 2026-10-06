"""The language the customer is answered in (docs/arabic-replies-plan.md).

Presentation only: decisions, query understanding, taxonomy and search run in
English whatever this is. It chooses the words the customer reads - the
writer's reply, the decision's own question, the fixed sentences - and nothing
the application reasons with.
"""

from __future__ import annotations

from enum import StrEnum


class ReplyLanguage(StrEnum):
    EN = "en"
    AR = "ar"


class LanguageSource(StrEnum):
    """Why a turn is answered in the language it is - for the logs only."""

    SESSION = "session"
    """The session's language, carried from an earlier turn."""

    SCRIPT = "script"
    """The message is written in Arabic letters."""

    ARABIZI = "arabizi"
    """The decision read Arabic written in Latin letters ("abi kanaba")."""

    SWITCH = "switch"
    """The customer explicitly asked for a language."""

    LOCALE = "locale"
    """Nothing in the conversation decided it; the storefront's locale did."""

    DEFAULT = "default"
    """Nothing decided it: English."""
