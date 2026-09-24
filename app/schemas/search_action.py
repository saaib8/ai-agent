"""Screen-driven, deterministic follow-ups on a product search.

**Transport, and application-only.** Like `bundle_action`, these are things the
customer did on the screen — tapped "show me different options", or "not this
one" — not language a model interpreted. They carry no product id: an exclusion
names a product only by its position on screen, and the server resolves that to
a real id against verified state (CLAUDE.md 6, 20.2).

They exist because retrieval on its own is a pure function of the filters: run
it twice, get the same shelf. These give it a **memory** — re-run the current
search while excluding what the customer has already seen (or turned down) — so
"show me alternatives" returns genuinely different products. No model is
consulted, so it can never be mis-routed.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class MoreOptionsAction(BaseModel):
    """Re-run the current search, excluding everything already shown.

    The whole presented set becomes an exclusion for this run, so the customer
    sees a different page of products for the same request. Repeats accumulate:
    each round excludes what the last one showed, so "keep going" keeps moving.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["more_options"] = "more_options"


class ExcludeProductAction(BaseModel):
    """Drop one product the customer turned down, then re-run the search.

    `ordinal` is the product's position on screen. It is resolved to a real id
    server-side and added to the search's exclusions, so it does not come back —
    not this turn, and not on a later "show me more" either.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["exclude"] = "exclude"
    ordinal: int = Field(ge=1)


SearchActionRequest = Annotated[
    MoreOptionsAction | ExcludeProductAction,
    Field(discriminator="kind"),
]
"""Either follow-up, told apart by `kind`."""
