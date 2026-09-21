"""Query-understanding contracts.

Two shapes live here: what the model is asked to emit
(:class:`CommerceInterpretation`), and what the service returns
(:data:`QueryInterpretation`).

The model's shape maps only onto concepts M6 discovery can already execute.
There is deliberately no confidence score, no reasoning trace, no ranked
alternatives and no style/colour/material/room field: a field the pipeline
cannot use is a field that invites invented certainty (CLAUDE.md 12.1).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from app.core.exceptions import DimensionSemanticsMissingError
from app.schemas.discovery import (
    DimensionConstraintKind,
    ProductSearchRequest,
    ProductSort,
)
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.dimensions import DimensionRole, UnsupportedDimensionReason
from app.taxonomy.registry import CommerceTaxonomy


class ConstraintStrength(StrEnum):
    """How firmly the customer expressed a constraint.

    Three states, deliberately. No confidence score, no probability, no
    priority ordering: this records what the customer's words committed to,
    not how sure anything is.
    """

    LOCKED = "locked"
    """An explicit requirement or bound: "maximum 5000 SAR", "3-seater"."""

    PREFERRED = "preferred"
    """Explicitly a preference: "I'd prefer to stay under 5000"."""

    APPROXIMATE = "approximate"
    """Explicitly approximate: "around 5000 SAR", "roughly four seats"."""


class RequirementFamily(StrEnum):
    """Kinds of requirement the current search engine cannot enforce."""

    COLOR = "color"
    STYLE = "style"
    MATERIAL = "material"
    DIMENSIONS = "dimensions"


class DimensionConstraintSemantics(BaseModel):
    """How firmly one measurement was expressed, keyed by the role it restricts.

    Keyed by :class:`DimensionRole` rather than by position, because a query may
    hold several measurements at different strengths - "around 220 cm wide but
    it must be under 100 cm deep" - and a policy that read the wrong entry would
    widen a bound the customer locked.

    Carries no value: the numbers live on the request, and duplicating them here
    would create two figures that could disagree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: DimensionRole
    strength: ConstraintStrength


class PlanarDimensionSemantics(BaseModel):
    """How firmly a pair of sides was expressed.

    One strength for the pair, not one per side: "around 200 x 300" loosens the
    rug, not one of its edges.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strength: ConstraintStrength


class ConstraintSemantics(BaseModel):
    """How the customer expressed each relaxable constraint.

    A field is None when that constraint is absent from the request; a present
    constraint always carries a strength.

    `commerce_category` is deliberately absent and is not relaxable. The
    product family is the basic intent anchor: a customer asking for lighting
    has not asked for tables, and no future policy may decide otherwise.

    Scalar price and capacity bounds are singular, so each is one field.
    Measurements are not: a request may carry several, so they arrive as a
    collection keyed by role.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subcategory: ConstraintStrength | None = None
    price_min: ConstraintStrength | None = None
    price_max: ConstraintStrength | None = None
    seating_min: ConstraintStrength | None = None
    seating_max: ConstraintStrength | None = None
    dimensions: tuple[DimensionConstraintSemantics, ...] = ()
    """One entry per request dimension, at most one per role."""

    planar_dimension: PlanarDimensionSemantics | None = None
    """Set exactly when the request carries a planar pair."""

    @model_validator(mode="after")
    def _check_roles_unique(self) -> Self:
        roles = [entry.role for entry in self.dimensions]
        if len(roles) != len(set(roles)):
            raise ValueError("a dimension role may carry only one strength")
        return self

    def strength_for_dimension(self, role: DimensionRole) -> ConstraintStrength:
        """The recorded strength for one measurement.

        Raises rather than defaulting. A policy that silently read LOCKED for a
        role nobody recorded would look safe while hiding a broken contract;
        one that silently read PREFERRED would widen without permission.
        """
        for entry in self.dimensions:
            if entry.role is role:
                return entry.strength
        raise DimensionSemanticsMissingError(role=str(role))

    def planar_dimension_strength(self) -> ConstraintStrength | None:
        """The pair's strength, or None when the request carries no pair."""
        return None if self.planar_dimension is None else self.planar_dimension.strength


def validate_dimension_correspondence(
    request: ProductSearchRequest, semantics: ConstraintSemantics
) -> None:
    """Every request dimension has exactly one strength, and no strength is orphaned.

    The request and its semantics are deliberately separate structures
    (CLAUDE.md 13.2), which means their correspondence is an invariant somebody
    has to enforce. Enforcing it here, at construction, is what lets
    :meth:`ConstraintSemantics.strength_for_dimension` refuse to guess.

    Public because two contracts need exactly this rule: the per-turn
    :class:`ResolvedSearch` and the durable `ActiveSearchState`. One
    implementation, so the two cannot drift.
    """
    wanted = sorted(constraint.role for constraint in request.dimensions)
    recorded = sorted(entry.role for entry in semantics.dimensions)
    if wanted != recorded:
        raise ValueError(
            "every request dimension needs exactly one recorded strength"
        )
    if (request.planar_dimensions is None) != (semantics.planar_dimension is None):
        raise ValueError("a planar pair and its strength must be present together")


class AttributeInterpretation(BaseModel):
    """One colour or style the customer mentioned, as the model read it.

    `canonical_value` is set only when the customer's wording clearly names an
    approved value. It stays null for anything the vocabulary does not contain
    - "red", "warm neutral" - which is preserved rather than forced onto the
    nearest approved token.
    """

    model_config = ConfigDict(frozen=True)

    family: AttributeFamily
    raw_value: str = Field(min_length=1, description="The customer's own words for it.")
    canonical_value: str | None = Field(
        default=None,
        description=(
            "The approved value, when their wording names one exactly. Null "
            "for anything else - never the nearest match."
        ),
    )
    strength: ConstraintStrength = Field(
        description=(
            "locked only when they were explicitly strict ('only', 'must be', "
            "'nothing else'). Ordinary wanting is preferred; a hedge is "
            "approximate."
        )
    )


class DimensionInterpretation(BaseModel):
    """One measurement the customer stated, as the model read it.

    `role` is null when they gave a number without saying which measurement it
    was - "a sofa under 200 cm" - which is a question to ask, not a gap to fill.
    """

    model_config = ConfigDict(frozen=True)

    role: DimensionRole | None = Field(
        default=None,
        description=(
            "Which measurement they meant: overall_width for how wide it is "
            "across the front, depth for front-to-back, height for how tall, "
            "length for how long a table is. Null if they gave a number but "
            "never said which measurement."
        ),
    )
    kind: DimensionConstraintKind = Field(
        description=(
            "max for 'under' or 'no more than', min for 'at least', range for "
            "'between A and B', target for 'around' or 'about'."
        )
    )
    min_value: str | None = Field(
        default=None, description="Lower bound as a plain decimal string, no unit."
    )
    max_value: str | None = Field(
        default=None, description="Upper bound as a plain decimal string, no unit."
    )
    target_value: str | None = Field(
        default=None, description="The figure to sit near, for a target."
    )
    unit: str | None = Field(
        default=None,
        description=(
            "The unit they used: cm, mm, m, in, ft. Null if they gave a bare "
            "number with no unit - never assume one."
        ),
    )
    strength: ConstraintStrength = Field(
        default=ConstraintStrength.LOCKED,
        description=(
            "locked for a plain requirement, preferred if they softened it, "
            "approximate if they said the figure itself was loose."
        ),
    )


class PlanarDimensionInterpretation(BaseModel):
    """Two sides given together, as in "a rug 200 x 300 cm"."""

    model_config = ConfigDict(frozen=True)

    first_value: str = Field(description="The first side, a plain decimal string.")
    second_value: str = Field(description="The second side, a plain decimal string.")
    unit: str | None = Field(default=None, description="cm, mm, m, in or ft.")
    strength: ConstraintStrength = ConstraintStrength.LOCKED


class UnsupportedDimension(BaseModel):
    """A measurement the customer asked for that this catalog cannot answer.

    Their intent was clear; the stored axes for that product type are not
    reliable enough to filter on. Asking again cannot repair that, so this is
    not a clarification.
    """

    model_config = ConfigDict(frozen=True)

    role: DimensionRole
    kind: DimensionConstraintKind
    min_cm: Decimal | None = None
    max_cm: Decimal | None = None
    target_cm: Decimal | None = None
    strength: ConstraintStrength = ConstraintStrength.LOCKED
    """Preserved as provenance. Nothing may relax on it - the measurement was
    never applied, so there is no result to widen."""

    reason: UnsupportedDimensionReason


class SemanticPreference(BaseModel):
    """A colour or style the customer leaned towards without ruling others out.

    Preserved for semantic ranking, which does not exist yet. It is never a
    filter: excluding every product that is not Beige because someone said
    they wanted a beige sofa would throw away what they might have chosen.
    """

    model_config = ConfigDict(frozen=True)

    family: AttributeFamily
    raw_value: str
    canonical_value: str | None = None
    strength: ConstraintStrength


class UnresolvedAttribute(BaseModel):
    """A strict requirement the catalog cannot currently guarantee."""

    model_config = ConfigDict(frozen=True)

    family: AttributeFamily
    raw_value: str


class CommerceInterpretation(BaseModel):
    """The strict structured output the model must produce.

    Every field is optional because the customer may not have said anything
    about it. Absence means "not stated", never "assume a default".

    Amounts are strings rather than numbers so money never passes through a
    binary float on its way to :class:`~decimal.Decimal`.
    """

    model_config = ConfigDict(frozen=True)

    commerce_category: str | None = Field(
        default=None,
        description=(
            "The commerce category from the approved taxonomy, or null when the "
            "customer has not said enough to determine one."
        ),
    )
    commerce_subcategory: str | None = Field(
        default=None,
        description=(
            "A subcategory listed under that exact category in the approved "
            "taxonomy, or null when the customer was not that specific."
        ),
    )
    price_min: str | None = Field(
        default=None,
        description=(
            "Minimum price as a plain decimal string, e.g. '2500'. "
            "No currency symbols or thousands separators."
        ),
    )
    price_max: str | None = Field(
        default=None,
        description=(
            "Maximum price as a plain decimal string, e.g. '6000'. "
            "No currency symbols or thousands separators."
        ),
    )
    price_currency: str | None = Field(
        default=None,
        description=(
            "The currency code the customer stated, e.g. 'SAR'. Null when they "
            "named an amount without a currency. Never guess one."
        ),
    )
    seating_capacity_min: int | None = Field(
        default=None, description="Minimum number of seats the customer asked for."
    )
    seating_capacity_max: int | None = Field(
        default=None, description="Maximum number of seats the customer asked for."
    )
    sort: ProductSort | None = Field(
        default=None,
        description=(
            "Only when the customer explicitly asked for cheapest or most "
            "expensive. Null otherwise."
        ),
    )
    commerce_subcategory_strength: ConstraintStrength | None = Field(
        default=None,
        description=(
            "How firmly they asked for that specific product type. Set only "
            "when they softened it ('I'd prefer', 'something like'). Null "
            "otherwise, which is read as a firm requirement."
        ),
    )
    price_min_strength: ConstraintStrength | None = Field(
        default=None,
        description="How firmly the minimum price was expressed. Null unless softened.",
    )
    price_max_strength: ConstraintStrength | None = Field(
        default=None,
        description="How firmly the maximum price was expressed. Null unless softened.",
    )
    seating_capacity_min_strength: ConstraintStrength | None = Field(
        default=None,
        description="How firmly the minimum seat count was expressed. Null unless softened.",
    )
    seating_capacity_max_strength: ConstraintStrength | None = Field(
        default=None,
        description="How firmly the maximum seat count was expressed. Null unless softened.",
    )
    attributes: list[AttributeInterpretation] = Field(
        default_factory=list,
        description=(
            "Every colour and style the customer mentioned, whether or not the "
            "approved vocabulary contains it. Empty when they mentioned none."
        ),
    )
    dimensions: list[DimensionInterpretation] = Field(
        default_factory=list,
        description=(
            "Every explicit measurement they stated, with a number they could "
            "be quoted on. Empty when they gave none. A vague word such as "
            "compact, small or low-profile is not a measurement."
        ),
    )
    planar_dimensions: PlanarDimensionInterpretation | None = Field(
        default=None,
        description=(
            "Two sides given together as 'A x B', typical of rugs. Null "
            "otherwise."
        ),
    )
    unsupported_requirements: list[RequirementFamily] = Field(
        default_factory=list,
        description=(
            "Requirements the customer stated explicitly that are none of the "
            "fields above: a colour, a decor style, a material, or a size or "
            "dimension. List each kind they named. Empty when they named none."
        ),
    )
    semantic_text: str | None = Field(
        default=None,
        description=(
            "The customer's own wording for what they are drawn to, with the "
            "parts already captured by the fields above left out: no prices, "
            "no measurements, no seat counts. 'a warm neutral sofa under 5000' "
            "gives 'warm neutral sofa'. Null when nothing descriptive remains."
        ),
    )
    multiple_product_types: bool = Field(
        default=False,
        description=(
            "True when the customer asked for two or more different kinds of "
            "product in one message, so a single search cannot cover it."
        ),
    )


def build_constrained_attribute(
    attributes: CatalogAttributes,
) -> type[AttributeInterpretation]:
    """`AttributeInterpretation` with `canonical_value` restricted to the registry.

    Both families share one enumeration because the response schema cannot
    express "a colour when family is colour". Deterministic validation checks
    the value belongs to the family that was claimed, so a style can never be
    accepted as a colour.
    """
    approved = tuple(sorted(attributes.colors | attributes.styles))
    return create_model(
        "ConstrainedAttributeInterpretation",
        __base__=AttributeInterpretation,
        canonical_value=(Literal[approved] | None, None),
    )


def build_constrained_interpretation(
    taxonomy: CommerceTaxonomy,
    attributes: CatalogAttributes,
) -> type[CommerceInterpretation]:
    """`CommerceInterpretation` with its taxonomy fields restricted to the registry.

    The enumerations are generated from the registry, so this adds no second
    source of vocabulary - change the registry and this changes with it.

    This exists because instructions alone are not a constraint. Measured on
    the golden set, the model kept answering `side-table` for "a table beside
    my sofa": a superseded value absent from the prompt's list but strongly
    present in its training. Restricting the response schema makes the value
    unrepresentable rather than merely discouraged.

    It replaces nothing. Deterministic validation still runs afterwards, and
    still has work to do: an enumeration cannot express that `chandelier` is
    invalid *under* `seating` (CLAUDE.md 14.4).
    """
    categories = tuple(sorted(taxonomy.categories))
    subcategories = tuple(
        sorted(
            {
                subcategory
                for category in taxonomy.categories
                for subcategory in taxonomy.subcategories(category)
            }
        )
    )
    return create_model(
        "ConstrainedCommerceInterpretation",
        __base__=CommerceInterpretation,
        commerce_category=(Literal[categories] | None, None),
        commerce_subcategory=(Literal[subcategories] | None, None),
        attributes=(list[build_constrained_attribute(attributes)], ...),  # type: ignore[misc]
    )


class ClarificationReason(StrEnum):
    """Why a usable search request could not be built from the message."""

    NO_COMMERCE_CATEGORY = "no_commerce_category"
    """Not enough information to determine even a product category."""

    MISSING_PRICE_CURRENCY = "missing_price_currency"
    """An amount was given with no currency, and none can be assumed."""

    MULTIPLE_PRODUCT_TYPES = "multiple_product_types"
    """Several kinds of product were asked for; one search cannot serve them."""

    MISSING_DIMENSION_ROLE = "missing_dimension_role"
    """A measurement was given without saying which measurement it was."""

    MISSING_DIMENSION_UNIT = "missing_dimension_unit"
    """A measurement was given with no unit, and none can be assumed."""


class ClarificationRequired(BaseModel):
    """The customer must be asked one question before discovery can run.

    Deliberately minimal: the reason is all the future Customer Agent needs in
    order to ask. No confidence, no guessed categories, no reasoning.
    """

    model_config = ConfigDict(frozen=True)

    reason: ClarificationReason


class ResolvedSearch(BaseModel):
    """The message became a request M6 discovery can execute as-is.

    `request` is the exact executable query and carries no relaxation metadata;
    `semantics` records how the customer expressed those same constraints.
    Discovery consumes only the former and knows nothing of constraint
    strength.
    """

    model_config = ConfigDict(frozen=True)

    request: ProductSearchRequest
    semantics: ConstraintSemantics = ConstraintSemantics()
    semantic_preferences: tuple[SemanticPreference, ...] = ()
    """Colour and style leanings, for semantic ranking. Never filters."""

    semantic_text: str | None = None
    """Descriptive wording left after the structured constraints were taken out.

    Feeds the ranking query embedding and nothing else. It can never change a
    category, a bound, a colour requirement or a retailer: every
    correctness-critical value lives in `request` and `semantics`. Price
    wording measurably degrades semantic ranking (M9A), which is why the
    structured fields keep it and this does not.
    """

    @model_validator(mode="after")
    def _check_dimensions(self) -> Self:
        validate_dimension_correspondence(self.request, self.semantics)
        return self


class UnresolvedStrictRequirement(BaseModel):
    """The customer was strict about something the catalog cannot guarantee.

    "It must be red" names no approved colour, so no filter can honour it and
    no semantic ranking can promise it. Softening it to a preference would
    quietly ignore the word "must"; mapping red onto Ruby would answer a
    question they did not ask.

    A distinct outcome, excluded from relaxation, so the conversational layer
    settles it before anything is widened.
    """

    model_config = ConfigDict(frozen=True)

    request: ProductSearchRequest
    semantics: ConstraintSemantics = ConstraintSemantics()
    unresolved: tuple[UnresolvedAttribute, ...]

    @model_validator(mode="after")
    def _check_dimensions(self) -> Self:
        validate_dimension_correspondence(self.request, self.semantics)
        return self


class UnsupportedDimensionRequirement(BaseModel):
    """The customer's measurement was clear; this catalog cannot honour it.

    Distinct from a clarification: nothing the customer could say would make
    the stored axes for that product type reliable. Distinct from a resolved
    search: the request that survives does not carry the measurement, and a
    caller must not present its results as satisfying it.
    """

    model_config = ConfigDict(frozen=True)

    request: ProductSearchRequest
    semantics: ConstraintSemantics = ConstraintSemantics()
    unsupported_dimensions: tuple[UnsupportedDimension, ...]

    @model_validator(mode="after")
    def _check_dimensions(self) -> Self:
        validate_dimension_correspondence(self.request, self.semantics)
        return self


class UnsupportedRequirement(BaseModel):
    """The customer stated a requirement this engine cannot enforce yet.

    A distinct type, not a flag on :class:`ResolvedSearch`, so the executable
    request cannot be run by a caller that simply forgot to check. The request
    is still carried - colour or style being unsupported does not make the rest
    of the message worthless - but returning it under a different type forces
    the caller to say something honest about what could not be applied.
    """

    model_config = ConfigDict(frozen=True)

    request: ProductSearchRequest
    semantics: ConstraintSemantics = ConstraintSemantics()
    unsupported: tuple[RequirementFamily, ...]

    @model_validator(mode="after")
    def _check_dimensions(self) -> Self:
        validate_dimension_correspondence(self.request, self.semantics)
        return self


QueryInterpretation = (
    ResolvedSearch
    | UnresolvedStrictRequirement
    | UnsupportedDimensionRequirement
    | UnsupportedRequirement
    | ClarificationRequired
)
"""A request, a request with caveats, or a question. Never a guess."""
