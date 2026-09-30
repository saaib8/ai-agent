"""Choosing the closest stocked type when the exact one is absent.

A customer asks for a product type this store does not stock at all - a recliner
where the shop carries chairs and lounge chairs, a candle where it carries
candlesticks. Instead of a dead end, a good salesperson offers the closest thing
they *do* have. This is that judgement, made a typed model step.

The judgement is the model's, but the vocabulary is not. The model is given only
the types this store actually stocks near the request, and its answer is
restricted - by schema and then re-checked deterministically - to exactly that
list. It cannot invent a type, and it cannot name one the store does not carry
(CLAUDE.md 3.3, 14). When nothing is genuinely close, it says so, and the turn
stays honest rather than forcing a bad substitute.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model


class ClosestTypeChoice(BaseModel):
    """The closest stocked subcategory to an unstocked request, or none close."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    closest_subcategory: str | None = None
    """One of the offered stocked subcategories, or ``None`` when nothing among
    them genuinely serves the same purpose. A bad substitute is worse than an
    honest "we don't carry that", so declining is a first-class answer."""


def build_constrained_closest_type(offered: tuple[str, ...]) -> type[ClosestTypeChoice]:
    """`ClosestTypeChoice` whose pick is restricted to the types actually offered.

    Built per request rather than at startup, which every other constrained
    schema in the system is: the offered set is what *this* store stocks near
    the request, read live from the catalog, not the global registry. As with
    query understanding, restricting the schema makes an unstocked or invented
    value unrepresentable rather than merely discouraged - and deterministic
    validation still runs afterwards, because a `Literal` cannot express that a
    value is valid under *this* category (CLAUDE.md 14.3, 14.4).

    ``offered`` must be non-empty: with nothing to offer there is nothing to
    choose, and the caller settles that before ever building a schema.
    """
    if not offered:
        raise ValueError("a constrained closest-type schema needs at least one option")
    return create_model(
        "ConstrainedClosestTypeChoice",
        __base__=ClosestTypeChoice,
        closest_subcategory=(Literal[offered] | None, None),
    )
