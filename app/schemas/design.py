"""The boundary between the two reasoning agents.

Customer/Commerce Agent -> InteriorDesignRequest -> Interior Design Agent
Interior Design Agent   -> InteriorDesignResult  -> Customer/Commerce Agent

Typed structures, not a conversation (CLAUDE.md 3.5). The design specialist is
**internal**: nothing it writes reaches a customer directly, and the
Customer/Commerce Agent remains the only voice the customer hears (CLAUDE.md
17.1).

Two tasks, and they need different things. General advice - "what colours work
with walnut?" - needs no catalog at all, so it must not trigger a capability
query. Room planning needs to know what the retailer can actually supply before
it proposes a single category, so for that task capabilities are mandatory. The
request enforces both.

Numbers carry their provenance. A room measurement is the customer's, a product
dimension is the catalog's, and a spacing guideline is a convention true of no
particular room - and the three must stay distinguishable all the way to the
response layer, which may state the first two as fact and must not state the
third as one.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.numbers import parse_stated_decimal
from app.schemas.design_intent import (
    MAX_DESIGN_INTENT_CHARS,
    normalise_design_intent,
)
from app.schemas.discovery import PriceConstraint, SeatingCapacityConstraint
from app.schemas.geometry import RoomGeometry, WallNote
from app.schemas.query import SemanticPreference
from app.schemas.retailer import RetailerCatalogCapabilities
from app.taxonomy.registry import CommerceTaxonomy

MAX_DESIGN_QUESTION_CHARS = 1500

MAX_REGULAR_SEATING_COUNT = 200
"""The most people a room's regular seating may be planned for.

A sanity bound on a stated fact, not a design rule. A majlis or a family
gathering routinely seats forty or more, and a limit below that silently
dropped what the customer said (the old ceiling was thirty). Two hundred still
refuses a figure that can only be a misreading, such as a price read as a head
count.
"""
MAX_GUIDANCE_SUMMARY_CHARS = 600
MAX_GUIDANCE_LABEL_CHARS = 120
MAX_NEED_SEMANTIC_INTENT_CHARS = MAX_DESIGN_INTENT_CHARS
"""Kept as a name because contracts and tests refer to it; the value and
the rule live in `design_intent`, shared with durable plan state."""

_DIGITS = re.compile(r"\d")
"""Prose states no figure, in guidance or in a need's design intent. Every
number belongs to a typed field that carries its provenance."""


def _centimetres(raw: str | None) -> Decimal | None:
    """One stated bound as a number, or a refusal.

    Raises inside model validation, so a bound that is not a number never
    reaches a caller as a string nobody can use.
    """
    if raw is None:
        return None
    try:
        return parse_stated_decimal(raw)
    except ValueError as exc:
        # The offending text is left out: it is model output, and this message
        # ends up in logs.
        raise ValueError("a guideline bound must be a usable number") from exc


class DesignPriority(StrEnum):
    """What a room can do without, when a budget forces a choice.

    Ordered by what may be dropped first, so budget work degrades optional
    items before core room requirements (CLAUDE.md 10).
    """

    REQUIRED = "required"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"


class DesignTask(StrEnum):
    """What a design request is for. One job remains: finding products for the
    pieces of a room Nora designed."""

    ROOM_PLAN = "room_plan"
    """The pieces of a room, searched for real products."""


class InteriorDesignRequest(BaseModel):
    """The room a plan's pieces are searched for, and nothing to invent.

    Discovery's input beside the plan: the room's size and walls (a piece too
    long for its wall is left out), its palette (each piece's own colours lean
    first) and what the retailer stocks. Carries no `RetailerContext`, no
    `store_id`, no state and no product identity. Scope stays with application
    code (CLAUDE.md 8, 20.2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: DesignTask = DesignTask.ROOM_PLAN
    room_type: str | None = None
    geometry: RoomGeometry | None = None
    layout: tuple[WallNote, ...] = ()
    """Their walls as they described them."""
    budget: PriceConstraint | None = None
    design_preferences: tuple[SemanticPreference, ...] = ()
    """The room's palette and styles, ranking only (CLAUDE.md 12.4)."""
    catalog_capabilities: RetailerCatalogCapabilities


MAX_DIRECTION_COLOURS = 3
MAX_DIRECTION_STYLES = 2
MAX_DIRECTION_AVOID = 2


class DesignDirection(BaseModel):
    """Which way a suggested piece should lean - what the designer would
    reach for on the shop floor, beside what was picked.

    For ranking only: every value orders products and none filters them, and
    the customer's own colours and styles always come first
    (docs/designer-led-shopping-plan.md, 4.2). Colours and styles are approved
    values the store stocks for that kind; anything else is dropped after the
    call, never matched to something near it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    colours: tuple[str, ...] = Field(default=(), max_length=MAX_DIRECTION_COLOURS)
    styles: tuple[str, ...] = Field(default=(), max_length=MAX_DIRECTION_STYLES)
    avoid_colours: tuple[str, ...] = Field(default=(), max_length=MAX_DIRECTION_AVOID)
    avoid_styles: tuple[str, ...] = Field(default=(), max_length=MAX_DIRECTION_AVOID)
    size_ratio: float | None = Field(default=None, ge=0.2, le=3)
    """How long the piece's longer floor side should be, as a share of the
    first anchor's - "about two-thirds of the sofa" is 0.66. A proportion, so
    the designer never states a size; code turns it into a figure from the
    anchor's real measurements, and only where they exist."""
    fits_inside_cm: int | None = Field(default=None, ge=40, le=250)
    """For a piece that goes inside the first anchor - a mattress in a bed
    frame - the width it must have, in centimetres: the size the anchor takes,
    read off its listed size and its name (king, queen, single). The cards in
    that size come first; nothing is hidden, and it is said as the designer's
    reading, never as a fact of the catalog."""

    @property
    def is_empty(self) -> bool:
        return not (
            self.colours
            or self.styles
            or self.avoid_colours
            or self.avoid_styles
            or self.size_ratio is not None
            or self.fits_inside_cm is not None
        )


class StockedLook(BaseModel):
    """The colours and styles one kind comes in at this store - the values a
    direction for it can actually find."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_subcategory: str = Field(min_length=1)
    colours: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()


MAX_PIECE_TYPES = 4
MAX_PIECE_COLOURS = 3
MAX_PIECE_STYLES = 2
MAX_PIECE_AVOID = 2
MAX_ROLE_CHARS = 40
MAX_PIECE_PLACE_CHARS = 40


class PieceDesign(BaseModel):
    """Nora's decisions about one piece of a room, beyond its type and
    priority - written by her as checked tool arguments, executed by code.

    Every value is an approved one or her words, never a product fact: which
    types may fill the piece, the colours and styles it should lean to, what
    it should avoid, the size it should be, its share of the budget and the
    wall it goes on. Ordering and spending only - discovery filters on none of
    it except a piece too long for the wall it is placed on, which is a
    measured fact (CLAUDE.md 10.6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str = Field(min_length=1, max_length=MAX_ROLE_CHARS)
    """What the piece is in the room, in her words: "main sofa", "reading chair"."""
    alternatives: tuple[str, ...] = Field(default=(), max_length=MAX_PIECE_TYPES - 1)
    """Other approved types of the same family that may fill it, after the
    need's own, in her order of preference."""
    colours: tuple[str, ...] = Field(default=(), max_length=MAX_PIECE_COLOURS)
    styles: tuple[str, ...] = Field(default=(), max_length=MAX_PIECE_STYLES)
    avoid_colours: tuple[str, ...] = Field(default=(), max_length=MAX_PIECE_AVOID)
    avoid_styles: tuple[str, ...] = Field(default=(), max_length=MAX_PIECE_AVOID)
    longer_min_cm: int | None = Field(default=None, ge=10, le=1200)
    longer_max_cm: int | None = Field(default=None, ge=10, le=1200)
    """The range its longer floor side should fall in."""
    shorter_min_cm: int | None = Field(default=None, ge=10, le=1200)
    shorter_max_cm: int | None = Field(default=None, ge=10, le=1200)
    """The range its shorter floor side should fall in - what decides a
    bed's size: every bed is about as long, single or king."""
    share_percent: int | None = Field(default=None, ge=1, le=100)
    """How much of the room's budget this piece's line should take - the
    optimiser spends toward it only after every piece has its best attributes."""
    wall: str | None = Field(default=None, max_length=MAX_PIECE_PLACE_CHARS)
    """The customer's name for the wall it goes on."""
    beside: str | None = Field(default=None, max_length=MAX_PIECE_PLACE_CHARS)

    @model_validator(mode="after")
    def _ordered_words_only(self) -> Self:
        if (
            self.longer_min_cm is not None
            and self.longer_max_cm is not None
            and self.longer_min_cm > self.longer_max_cm
        ) or (
            self.shorter_min_cm is not None
            and self.shorter_max_cm is not None
            and self.shorter_min_cm > self.shorter_max_cm
        ):
            raise ValueError("a piece's size range is out of order")
        # The wall is one of theirs as they named it ("wall 2"), set only by
        # the design check; the rest is her words.
        if any(_DIGITS.search(v or "") for v in (self.role, self.beside)):
            raise ValueError("a piece's role and place are words only")
        return self

    def size_target_cm(self) -> int | None:
        low, high = self.longer_min_cm, self.longer_max_cm
        if low is not None and high is not None:
            return (low + high) // 2
        return low if low is not None else high


class DesignCategoryNeed(BaseModel):
    """One product type a room calls for, and how badly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)
    priority: DesignPriority

    semantic_intent: str | None = Field(default=None, max_length=MAX_NEED_SEMANTIC_INTENT_CHARS)
    """This piece's qualitative character, for ranking only.

    The design reasoning a structured field cannot hold: "visually light",
    "low-profile and understated", "comfortable for prolonged reading". It
    exists because cross-sell was design-aware about *which kinds of thing* a
    room needs and not about *which of them* to put first.

    **Per need, and not the room's preferences restated.** A room may be Modern
    Contemporary in warm neutrals while its lounge chair should read as light
    and its centre table as low. Copying one room-level phrase onto every need
    would add nothing to any of them; the two live in different fields because
    they are different facts (CLAUDE.md 12.4).

    **Ranking context, never authority.** It reaches exactly one place - the
    query embedding - and can change no category, no bound, no capacity, no
    colour requirement and no retailer. A product it suits must still satisfy
    every structured constraint to be eligible at all, and no widening policy
    reads it (CLAUDE.md 16.1).

    Digits are refused for the same reason guidance prose refuses them: a price,
    a measurement, a seat count or an identifier hidden in free text would be
    indistinguishable from one a model invented, and each of those already has
    a typed home. Blank is `None` - nothing to rank by is an ordinary answer,
    and most needs have none.
    """

    quantity: int = Field(default=1, ge=1)
    """How many physical units of this piece the room calls for.

    Design reasoning, not arithmetic: a room that wants a matching pair either
    side of something asks for two, and a room that wants one asks for one. It
    is never derived from a product type, and there is no table saying which
    kinds come in pairs.

    **N units of the same selected product.** The whole point of asking for two
    is usually that they match, so the bridge does not search twice and the
    optimiser does not choose twice - one product is chosen and bought N times.
    A set sold as one catalog product is one product with quantity one, not N
    of its parts.

    **It never reaches a search.** Wanting six of something does not change
    which products qualify, so `ProductSearchRequest` has no such field and the
    pool is identical either way.

    **It claims nothing about stock.** The catalog records no inventory count,
    so this multiplies a price and asserts nothing about whether N can be had.

    Distinct from `seating_capacity`, which is how many people one piece seats.
    Two pieces seating three each is `quantity=2` with a capacity of three, and
    conflating the two would order the wrong room.
    """

    direction: DesignDirection | None = None
    """For a complement only: which way this piece should lean beside what was
    picked. Absent on a room plan's needs."""

    design: PieceDesign | None = None
    """Nora's decisions for this piece of a room: its role, other types, look,
    size, share and wall. Absent on a need nobody designed."""

    seating_capacity: SeatingCapacityConstraint | None = None
    """How many this particular piece should seat, when the design calls for it.

    A design-derived constraint about *this need*, not a headcount copied from
    the brief. "A family of six" does not mean a six-seat sofa: the specialist
    reasons about the room's seating as a whole and may distribute it across a
    sofa and a chair, which is why the figure belongs per need rather than on
    the request.

    Never inferred from a category, a subcategory, a product name, an anchor or
    a default. `None` means no verified capacity requirement for this need -
    the ordinary case for a rug or a lamp - and never zero.

    The same constraint the catalog already understands, so the search bridge
    copies it across unchanged. A stored `NULL` capacity stays *unverified*
    rather than becoming a match (CLAUDE.md 6.1).
    """

    @field_validator("semantic_intent")
    @classmethod
    def _intent_is_ranking_prose_only(cls, value: str | None) -> str | None:
        """One rule, shared with the durable plan that records this need."""
        return normalise_design_intent(value)


class PieceFitCheck(BaseModel):
    """One piece the question is about against one wall or doorway they gave,
    measured in code - a fact the designer judges with, never the judgement.

    `piece` is the piece's place among the anchors (1 for the first)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: int = Field(ge=1)
    card: int | None = Field(default=None, ge=1)
    """Its place on the customer's screen, when it is a card there - what the
    reply calls it by ("the second one")."""
    space: Literal["wall", "doorway"]
    label: str | None = None
    space_cm: Decimal = Field(gt=0)
    piece_cm: Decimal | None = None
    """Against a wall, its longer floor side; through a doorway, the smaller
    of its shorter side and its height. None when that is not listed."""
    verdict: FitVerdict
    margin_cm: Decimal | None = None


MAX_SPACE_FIT_REASON_CHARS = 240
MIN_SPACE_FIT_RATIO = 0.3
MAX_SPACE_FIT_RATIO = 1.0


MAX_PLACEMENTS = 8
MAX_NEXT_SIZES = 4


class Placement(BaseModel):
    """Where a piece goes in their room, as a designer would point: on which
    of their walls, beside what, and why. Words only - every figure belongs to
    a size or a measurement, never to a placement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    piece: str = Field(min_length=1, max_length=80)
    """The piece, by name or kind in plain words."""
    wall: str | None = Field(default=None, max_length=40)
    """One of their walls, in their words, when they described them."""
    beside: str | None = Field(default=None, max_length=80)
    note: str = Field(min_length=1, max_length=MAX_GUIDANCE_LABEL_CHARS)

    def states_a_figure(self) -> bool:
        """A figure in a placement is one nothing measured: the placement is
        dropped, never the designer's whole answer (CLAUDE.md 21.1)."""
        return any(_DIGITS.search(v or "") for v in (self.piece, self.wall, self.beside, self.note))


class NextSize(BaseModel):
    """The size the next piece of a kind should be for their room and what
    they chose - a rug under the front legs of the sofa, a coffee table two
    thirds of its length. A designer's target that orders a search, never a
    filter and never a product fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str
    commerce_subcategory: str | None = None
    longer_min_cm: int | None = Field(default=None, gt=0, le=2000)
    longer_max_cm: int | None = Field(default=None, gt=0, le=2000)
    shorter_min_cm: int | None = Field(default=None, gt=0, le=2000)
    shorter_max_cm: int | None = Field(default=None, gt=0, le=2000)
    reason: str = Field(min_length=1, max_length=MAX_GUIDANCE_LABEL_CHARS)

    def is_usable(self) -> bool:
        """A size names its longer side, keeps each range in order and puts no
        figure in its reason. One that does not is dropped on its own; the rest
        of the designer's answer stands (CLAUDE.md 21.1)."""
        if _DIGITS.search(self.reason):
            return False
        if self.longer_min_cm is None and self.longer_max_cm is None:
            return False
        return all(
            low is None or high is None or low <= high
            for low, high in (
                (self.longer_min_cm, self.longer_max_cm),
                (self.shorter_min_cm, self.shorter_max_cm),
            )
        )

    def longer_target_cm(self) -> int:
        """The middle of the longer side's range - the size a search sits near."""
        sides = [v for v in (self.longer_min_cm, self.longer_max_cm) if v is not None]
        return round(sum(sides) / len(sides))


class InteriorDesignResult(BaseModel):
    """What the design specialist concluded.

    Guidance for an advice task, category needs for a plan, and a task may
    legitimately produce both - a plan worth explaining usually comes with a
    reason.

    There is deliberately no `design_preferences` here. The customer's
    preferences already have one home, on the room project, and echoing them
    back from an agent that owns no state would create a second copy and a
    path by which a recommendation could quietly displace what the customer
    said.

    No product, no price, no inventory, no identity: this contract has nowhere
    to put any of them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    needs: tuple[DesignCategoryNeed, ...] = ()
    """For a space fit only: the proportion the piece should take."""
    placements: tuple[Placement, ...] = Field(default=(), max_length=MAX_PLACEMENTS)
    """Where each piece goes, when the room and its walls are known."""
    next_sizes: tuple[NextSize, ...] = Field(default=(), max_length=MAX_NEXT_SIZES)
    """The size the next pieces should be, for their room and what they chose."""

    def validate_against(self, taxonomy: CommerceTaxonomy) -> None:
        """Reject any need the approved vocabulary does not contain.

        Checked here rather than at construction, matching how
        :class:`~app.schemas.discovery.ProductSearchRequest` leaves its
        category to the discovery service: schemas in this service do not load
        the registry.
        """
        for need in self.needs:
            if need.commerce_subcategory is None:
                taxonomy.subcategories(need.commerce_category)  # raises if unapproved
            else:
                taxonomy.validate_pair(need.commerce_category, need.commerce_subcategory)


class FitVerdict(StrEnum):
    """One measurement against one limit the customer gave. Never "it fits".

    The names are the whole point. A sofa narrower than the wall you described
    is *within a known limit*; whether it fits your room depends on the door it
    came through, what else is along that wall, and how much floor you want to
    keep - none of which anyone has told us. Calling that `FITS` would turn one
    true comparison into a guarantee about a room nobody has measured.

    Three values because "we cannot tell" is a real answer and the commonest
    one. A question without an applicable stated measurement is refused rather
    than estimated.
    """

    WITHIN_KNOWN_LIMIT = "within_known_limit"
    """The product's measurement is within the limit the customer stated.

    True of that one constraint, and a claim about nothing else.
    """

    EXCEEDS_KNOWN_LIMIT = "exceeds_known_limit"
    """It is larger than the limit they stated. Decisive in this direction: a
    piece too wide for the wall is too wide, whatever else is unknown."""

    INSUFFICIENT_GEOMETRY = "insufficient_geometry"
    """No applicable measurement, so no comparison was made."""
