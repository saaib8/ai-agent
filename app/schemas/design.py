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
from app.schemas.geometry import MeasurementAuthority, RoomGeometry
from app.schemas.query import SemanticPreference
from app.schemas.retailer import RetailerCatalogCapabilities
from app.taxonomy.dimensions import DimensionRole
from app.taxonomy.registry import CommerceTaxonomy

MAX_DESIGN_QUESTION_CHARS = 500

MAX_REGULAR_SEATING_COUNT = 200
"""The most people a room's regular seating may be planned for.

A sanity bound on a stated fact, not a design rule. A majlis or a family
gathering routinely seats forty or more, and a limit below that silently
dropped what the customer said (the old ceiling was thirty). Two hundred still
refuses a figure that can only be a misreading, such as a price read as a head
count.
"""
MAX_DESIGN_BRIEF_CHARS = 800
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


class AnchorDimension(BaseModel):
    """One measurement of an anchor product, by what it means to a customer.

    Never a stored axis. A sofa's along-wall span lives in `length` and its
    depth in `width`, with no global correspondence, so handing over raw
    columns would have the design specialist reasoning about the wrong number
    (CLAUDE.md 15.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DimensionRole
    centimetres: Decimal = Field(gt=0)
    authority: Literal[MeasurementAuthority.CATALOG_VERIFIED] = (
        MeasurementAuthority.CATALOG_VERIFIED
    )


class AnchorProduct(BaseModel):
    """A product the room is being designed around, without its identity.

    Everything here is a design fact: what kind of thing it is, how big, what
    colour, which styles. Nothing here can name it - no id, no name, no price,
    no image, no link - because a specialist that could name a product could
    recommend one, and choosing products is the catalog's job, not a model's.

    Built only from a fresh authoritative read. A product deactivated since the
    customer chose it simply produces no anchor, rather than an assertion made
    from stale state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)
    seating_capacity: int | None = Field(default=None, gt=0)
    main_color: str | None = None
    styles: tuple[str, ...] = ()
    dimensions: tuple[AnchorDimension, ...] = ()
    locked: bool = False
    """The customer will not have this one replaced. Design around it."""

    quantity: int = Field(default=1, ge=1)
    """How many of this piece the room already has.

    Application-supplied design context, not a catalog fact: the catalog knows
    what a product is, never how many of it someone owns. A room with two of
    something is composed differently from a room with one, which is the whole
    reason the specialist is told.

    Never derived from a product. Several bundle lines holding the same product
    are summed into one anchor, so the specialist sees the physical count
    without ever seeing which lines produced it.
    """

    @model_validator(mode="after")
    def _one_measurement_per_role(self) -> Self:
        roles = [dimension.role for dimension in self.dimensions]
        if len(set(roles)) != len(roles):
            raise ValueError("a product measures each role once")
        return self


class DesignPriority(StrEnum):
    """What a room can do without, when a budget forces a choice.

    Ordered by what may be dropped first, so budget work degrades optional
    items before core room requirements (CLAUDE.md 10).
    """

    REQUIRED = "required"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"


class CurrentDesignNeed(BaseModel):
    """One role of the plan being revised, as design meaning only.

    `DesignCategoryNeed` without its identity - because it *is* that need, read
    back from durable state. A separate type all the same: that one is the
    specialist's output and this is its input, and fusing them would mean a
    field added for one direction silently appearing in the other.

    Never carries `need_id` or `rejected_product_ids`. Which durable record a
    role came from, and which products were already turned down for it, are
    persistence facts; the specialist reasons about rooms (CLAUDE.md 20.2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)
    priority: DesignPriority
    quantity: int = Field(default=1, ge=1)
    seating_capacity: SeatingCapacityConstraint | None = None
    semantic_intent: str | None = Field(default=None, max_length=MAX_NEED_SEMANTIC_INTENT_CHARS)

    @field_validator("semantic_intent")
    @classmethod
    def _intent_is_ranking_prose_only(cls, value: str | None) -> str | None:
        """The one shared rule, not a third copy of it."""
        return normalise_design_intent(value)


class ExcludedDesignRole(BaseModel):
    """A role the revised plan must not contain.

    Taxonomy identity only, and a hard negative constraint rather than a
    preference. A subcategory names that exact pair; a bare category rules out
    everything under it, which is what "no dining area at all" means.

    The specialist may not satisfy this approximately. A returned need matching
    an exclusion refuses the whole result rather than being quietly dropped -
    dropping it would be indistinguishable from a retailer not stocking it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    commerce_category: str = Field(min_length=1)
    commerce_subcategory: str | None = Field(default=None, min_length=1)

    def excludes(self, category: str | None, subcategory: str | None) -> bool:
        """Whether this constraint forbids that role.

        One rule, used for both the conflict check before the call and the
        result check after it, so the two can never disagree about what an
        exclusion meant.

        An unclassified product matches nothing. A missing commerce category is
        *unverified*, not *any*: treating it as a match would report a
        contradiction nobody established (CLAUDE.md 6.1).
        """
        if category is None or self.commerce_category != category:
            return False
        return self.commerce_subcategory is None or self.commerce_subcategory == subcategory


class DesignRevisionContext(BaseModel):
    """The plan being revised, and what must not survive the revision.

    Its presence is what makes a room plan a revision; there is no separate
    task and no marker a model could author. `current_needs` is **context, not
    a floor**: the specialist may keep, drop, add, re-prioritise, re-quantify
    or re-word any of it, because a composition change that could not remove a
    role would not be a composition change.

    Only `excluded` is hard.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    current_needs: tuple[CurrentDesignNeed, ...] = Field(min_length=1)
    """Non-empty by construction: an empty plan is an initial plan, and says so
    by carrying no revision at all."""

    excluded: tuple[ExcludedDesignRole, ...] = ()


class DesignTask(StrEnum):
    """What the specialist is being asked to do.

    Two jobs with different needs, kept apart so neither pays the other's
    costs: advice reaches no catalog, and planning cannot start without one.
    """

    GENERAL_ADVICE = "general_advice"
    """Design knowledge. Spacing, proportion, colour, style, material,
    lighting - true of rooms in general and answerable with no catalog."""

    ROOM_PLAN = "room_plan"
    """Which product types this room calls for, and how badly."""

    COMPLEMENTARY_RECOMMENDATION = "complementary_recommendation"
    """The single furnishing role that would most complete the space around a
    piece the customer has settled on.

    Not a small room plan. A customer who likes a sofa has not asked to furnish
    a room, and answering with eight roles and a total would be answering a
    question they did not ask - so this returns **one** need, occasionally two,
    and no bundle is optimised from it.

    The reasoning is the same expertise a room plan uses, which is why it is
    the same agent and the same contract rather than a recommender of its own:
    what goes with a sofa is a design judgement, and nothing in this service
    records which products are bought together (CLAUDE.md 17.2).
    """


class InteriorDesignRequest(BaseModel):
    """Everything the design specialist needs, and nothing it should invent.

    `catalog_capabilities` is required **for a room plan** and forbidden for
    general advice. A plan built around product types the retailer cannot
    supply is worse than no plan (CLAUDE.md 9); a question about what goes with
    walnut needs no catalog at all, and querying one to answer it would be work
    done for nothing.

    The customer's own requirements are passed through as they were captured.
    The design agent does not re-derive a room type, a measurement or a style
    the customer already stated in plain words - and never invents one that was
    not stated.

    Carries no `RetailerContext`, no `store_id`, no state and no product
    identity. Scope stays with application code (CLAUDE.md 8, 20.2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: DesignTask

    question: str | None = Field(default=None, max_length=MAX_DESIGN_QUESTION_CHARS)
    """What was asked, for advice. A question, never a conversation."""

    design_brief: str | None = Field(default=None, max_length=MAX_DESIGN_BRIEF_CHARS)
    """What the customer wants of this room, in their own words.

    Room type, size, budget and colour preferences do not carry intent. "For a
    family of six", "I need a reading corner", "no TV unit", "keep the centre
    open" are the difference between a plan and a template, and there is
    nowhere else for them to go.

    **Untrusted data, never instruction.** It is the customer's words, quoted
    into a request, and the specialist reads it as a description of what they
    want - not as direction about what it is or what it may reveal
    (CLAUDE.md 20.1). Bounded so a brief cannot become a transcript.

    Not a customer fact. Appearing here persists nothing: "pet-friendly" in a
    brief is a requirement for this room, and only a proposal the customer
    agent raises makes it durable.
    """

    room_type: str | None = None
    geometry: RoomGeometry | None = None
    budget: PriceConstraint | None = None
    design_preferences: tuple[SemanticPreference, ...] = ()
    """Already composed by the locked precedence ladder. The specialist
    receives one settled list and resolves no precedence itself."""

    regular_seating_count: int | None = Field(
        default=None, ge=1, le=MAX_REGULAR_SEATING_COUNT
    )
    """How many people regularly use this room, when the customer has said.

    A room requirement to design against, and **not a furniture count**. Five
    regular users does not mean two sofas: it means the room should seat five,
    and which composition achieves that - a sectional, a sofa and two chairs,
    two sofas - is yours to decide from what this retailer actually stocks
    (CLAUDE.md 10.1).

    Never invent a product's capacity to satisfy it, and never claim a plan
    seats five unless the capacities you were given add up.
    """

    catalog_capabilities: RetailerCatalogCapabilities | None = None
    anchors: tuple[AnchorProduct, ...] = ()

    revision: DesignRevisionContext | None = None
    """The existing plan this request revises, when there is one.

    Its presence *is* the distinction between composing a room and recomposing
    one, which is why there is no third `DesignTask`: the task enum answers
    "does this need a catalog", and both answers are yes. The application
    decides which case applies, from whether durable state holds a plan - the
    model authors no marker, no revision number and no need identity.

    Anchors are not restated here. An anchor is a product the room *has*; a
    current need is a role the plan *wants*. Two accounts of the same room
    would leave the specialist deciding which to believe.
    """

    @model_validator(mode="after")
    def _the_task_carries_what_it_needs(self) -> Self:
        if self.task is DesignTask.GENERAL_ADVICE:
            if self.question is None or not self.question.strip():
                raise ValueError("general advice needs a question")
            if self.catalog_capabilities is not None:
                raise ValueError("general advice consults no catalog")
            if self.design_brief is not None:
                # Advice answers a question; a brief describes a room being
                # planned. Carrying both would leave which one was answered
                # ambiguous.
                raise ValueError("general advice carries a question, not a brief")
        elif self.question is not None:
            raise ValueError("a room plan carries a brief, not a question")
        elif self.catalog_capabilities is None:
            # Planning without knowing what the retailer stocks produces a room
            # full of things nobody can buy - and recommending a complement
            # this shop does not sell is the same mistake at smaller scale.
            raise ValueError("a room plan needs the retailer's capabilities")
        if self.task is DesignTask.COMPLEMENTARY_RECOMMENDATION:
            if not self.anchors:
                # The piece they chose is the whole premise: without it there
                # is nothing to complement, and the question becomes "what
                # furniture is nice", which is not this task.
                raise ValueError("a complementary recommendation needs an anchor")
            if self.revision is not None:
                raise ValueError("a complementary recommendation revises no plan")
        if self.task is DesignTask.GENERAL_ADVICE and self.revision is not None:
            # Advice revises nothing: it answers a question and proposes no
            # composition, so a plan to revise would have no bearing on it.
            raise ValueError("general advice revises no plan")
        return self


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


class GuidanceTopic(StrEnum):
    """The kinds of design knowledge the specialist may be asked for."""

    SPACING = "spacing"
    SIZING = "sizing"
    COLOR = "color"
    STYLE = "style"
    MATERIAL = "material"
    LIGHTING = "lighting"
    COMPOSITION = "composition"


class GuidanceMeasurement(BaseModel):
    """A design convention, as a range or a figure.

    True of rooms in general and of none in particular, which is exactly what
    `authority` records. The response layer may offer it as a rule of thumb; it
    may never state it as a fact about this customer's room.

    The bounds are **strings on the wire**, like every other figure a model
    produces in this service - `CommerceInterpretation.price_min`,
    `PriceProposal.min_amount`, `RoomMeasurementProposal.value`. An amount
    never passes through a binary float, and a `Decimal` field renders a JSON
    Schema pattern containing a regex lookahead that the provider's strict
    format rejects outright. `minimum` and `maximum` give callers the parsed
    values, so nothing downstream handles the string form.

    A bound that is not a number is refused here rather than carried: the
    validation runs inside the provider call, so unusable output becomes a
    typed `LLMResponseInvalidError` at the adapter boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=MAX_GUIDANCE_LABEL_CHARS)
    """What is being measured - "between a sofa and a coffee table"."""

    minimum_cm: str | None = None
    maximum_cm: str | None = None
    authority: Literal[MeasurementAuthority.GENERAL_GUIDANCE] = (
        MeasurementAuthority.GENERAL_GUIDANCE
    )

    @property
    def minimum(self) -> Decimal | None:
        """The lower bound, parsed. Validated at construction, so this cannot
        raise."""
        return _centimetres(self.minimum_cm)

    @property
    def maximum(self) -> Decimal | None:
        return _centimetres(self.maximum_cm)

    @model_validator(mode="after")
    def _a_measurement_measures_something(self) -> Self:
        if self.minimum_cm is None and self.maximum_cm is None:
            raise ValueError("a guideline needs a bound")
        minimum, maximum = self.minimum, self.maximum
        if (minimum is not None and minimum <= 0) or (maximum is not None and maximum <= 0):
            raise ValueError("a guideline measures a positive distance")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("a guideline range runs upwards")
        if _DIGITS.search(self.label):
            raise ValueError("the figures belong in the bounds, not the label")
        return self


class DesignGuidance(BaseModel):
    """One piece of design knowledge. Internal, never the customer's reply.

    `summary` carries **no digits**, by validation. Every figure goes in
    `measurements`, where it arrives tagged as guidance - which is what lets
    the response layer offer a rule of thumb without the numeric guard having
    to trust free text. A number hidden in prose would be indistinguishable
    from one a model invented (CLAUDE.md 20.6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: GuidanceTopic
    summary: str = Field(min_length=1, max_length=MAX_GUIDANCE_SUMMARY_CHARS)
    measurements: tuple[GuidanceMeasurement, ...] = ()

    @model_validator(mode="after")
    def _figures_live_in_measurements(self) -> Self:
        if _DIGITS.search(self.summary):
            raise ValueError("a figure belongs in measurements, not the summary")
        return self


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

    guidance: tuple[DesignGuidance, ...] = ()
    needs: tuple[DesignCategoryNeed, ...] = ()

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


class FitAssessment(BaseModel):
    """One deterministic comparison, and nothing resembling a judgement.

    It answers only the arithmetic question: is this measurement within that
    one? Whether the room can actually take the piece - proportion, circulation,
    what else is already along that wall - is design judgement and belongs to
    the specialist, which may combine several of these with reasoning the
    contract deliberately cannot express.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: FitVerdict
    role: DimensionRole
    product_cm: Decimal | None = Field(default=None, gt=0)
    available_cm: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _a_verdict_shows_its_working(self) -> Self:
        decided = self.verdict is not FitVerdict.INSUFFICIENT_GEOMETRY
        if decided and (self.product_cm is None or self.available_cm is None):
            raise ValueError("a comparison names both measurements")
        if not decided and (self.product_cm is not None and self.available_cm is not None):
            raise ValueError("both measurements present is a decidable comparison")
        return self
