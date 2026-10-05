"""Turns a customer's words into a structured search request, or a question.

The only place an LLM is consulted in the discovery path. It has no tools, no
database, no repository and no network access beyond the provider boundary: it
reads one message and returns one typed value (CLAUDE.md 20.2).

Model output is untrusted structured input. Everything it produces passes
through deterministic validation - taxonomy first, then the domain contract -
before it can become a :class:`ProductSearchRequest`. An unapproved value is
rejected, never repaired and never quietly dropped (CLAUDE.md 14.3).

This service does not search, rank, relax or answer. It understands.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import NamedTuple

from pydantic import ValidationError

from app.core.exceptions import (
    IntegrationUnavailableError,
    LLMResponseInvalidError,
    TaxonomyValidationError,
)
from app.core.logging import get_logger
from app.core.numbers import parse_stated_amount, parse_stated_decimal
from app.integrations.llm import StructuredLLMClient
from app.prompts.query_understanding.v1 import (
    GENERIC_PROBLEM,
    PAIR_PROBLEM,
    VERSION,
    build_correction,
    build_instructions,
    build_type_check,
)
from app.schemas.dimensions import parse_unit, to_centimetres
from app.schemas.discovery import (
    DimensionConstraint,
    PlanarDimensionConstraint,
    PriceConstraint,
    ProductSearchRequest,
    ProductSort,
    SeatingCapacityConstraint,
)
from app.schemas.query import (
    AttributeInterpretation,
    ClarificationReason,
    ClarificationRequired,
    CommerceInterpretation,
    ConstraintSemantics,
    ConstraintStrength,
    DimensionConstraintSemantics,
    DimensionInterpretation,
    PlanarDimensionInterpretation,
    PlanarDimensionSemantics,
    QueryInterpretation,
    ResolvedSearch,
    SemanticPreference,
    TypeFit,
    UnresolvedAttribute,
    UnresolvedStrictRequirement,
    UnsupportedDimension,
    UnsupportedDimensionRequirement,
    UnsupportedRequirement,
    build_constrained_interpretation,
)
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.dimensions import DimensionSemantics
from app.taxonomy.registry import CommerceTaxonomy

logger = get_logger(__name__)

# A shopping request is a sentence, not a document. Anything longer is either a
# mistake or an attempt to bury instructions in padding.
MAX_MESSAGE_CHARS = 1000


def _strength(
    stated: ConstraintStrength | None, *, present: bool
) -> ConstraintStrength | None:
    """The strength to record for one constraint.

    Absent constraint -> None. Present constraint with no softening language ->
    LOCKED. The conservative default matters: treating an unqualified "under
    5000" as merely preferred would let a later policy quietly spend more than
    the customer allowed.
    """
    if not present:
        return None
    return stated or ConstraintStrength.LOCKED


class _StatedDimensions(NamedTuple):
    """What one message's measurements became, split by what can be done with them.

    The executable constraints and their strengths are built together, in one
    pass, so a measurement can never reach a request without its strength
    reaching the semantics beside it.
    """

    supported: tuple[DimensionConstraint, ...]
    semantics: tuple[DimensionConstraintSemantics, ...]
    unsupported: tuple[UnsupportedDimension, ...]
    planar: PlanarDimensionConstraint | None
    planar_semantics: PlanarDimensionSemantics | None


def _to_decimal(raw: str, *, field: str, money: bool = False) -> Decimal:
    try:
        return parse_stated_amount(raw) if money else parse_stated_decimal(raw)
    except ValueError as exc:
        raise LLMResponseInvalidError(reason=f"{field} was not a usable decimal") from exc


def _problems(exc: LLMResponseInvalidError | TaxonomyValidationError) -> tuple[str, ...]:
    """What the refused interpretation got wrong, as rules the model can act on."""
    if isinstance(exc, TaxonomyValidationError):
        return (PAIR_PROBLEM,)
    violations = exc.context.get("violations", ())
    return tuple(violations) if violations else (GENERIC_PROBLEM,)


_KIND_WORD = re.compile(r"[a-z]+")
MAX_ASKED_KIND_WORDS = 4
MAX_ASKED_KIND_CHARS = 40


def _asked_kind(
    interpretation: CommerceInterpretation, resolved_type: str, message: str
) -> str | None:
    """The customer's name for a kind narrower than the type, as plain words.

    Only spelling is normalised - case, punctuation, spacing (CLAUDE.md 14.2);
    every word must be one the customer actually wrote;
    no word is mapped onto another. Anything that is not a short run of words
    is dropped rather than repaired: the field only ever lets the reply admit a
    kind was not found, and an unreadable one is better left unsaid. Words that
    merely restate the resolved type ("bed" for `bed`) say nothing narrower.
    """
    if interpretation.type_fit is not TypeFit.BROADER_THAN_ASKED:
        return None
    words = _KIND_WORD.findall((interpretation.asked_kind or "").lower())
    if not words or len(words) > MAX_ASKED_KIND_WORDS:
        return None
    phrase = " ".join(words)
    if len(phrase) > MAX_ASKED_KIND_CHARS:
        return None
    type_words = set(_KIND_WORD.findall(resolved_type.lower()))
    if set(words) <= type_words:
        return None
    # Their own words, or nothing: a phrase the model composed could be a
    # synonym of the type, or an instruction, and the reply will say it out
    # loud. A plural "s" in the message still counts ("bunk beds").
    said = set(_KIND_WORD.findall(message.lower()))
    if not all(word in said or f"{word}s" in said or word[:-1] in said for word in words):
        return None
    return phrase


def _category_only(outcome: QueryInterpretation) -> bool:
    request = getattr(outcome, "request", None)
    return request is not None and request.commerce_subcategory is None


class QueryUnderstandingService:
    def __init__(
        self,
        client: StructuredLLMClient,
        taxonomy: CommerceTaxonomy,
        attributes: CatalogAttributes,
        dimensions: DimensionSemantics,
    ) -> None:
        self._client = client
        self._taxonomy = taxonomy
        self._attributes = attributes
        self._dimension_semantics = dimensions
        self._instructions = build_instructions(taxonomy, attributes)
        # Restricts the provider's taxonomy and attribute fields to approved
        # values. Belt and braces: _resolve still validates whatever comes back.
        self._schema = build_constrained_interpretation(taxonomy, attributes)

    async def interpret(
        self, message: str, *, store_currency: str | None = None
    ) -> QueryInterpretation:
        """Interpret one customer message. Never partially guesses.

        `store_currency` is the store's own currency, when its catalog names
        exactly one: an amount stated without a currency is in it. Without it,
        the customer is asked which currency they meant."""
        text = message.strip()
        if not text:
            return ClarificationRequired(reason=ClarificationReason.NO_COMMERCE_CATEGORY)
        if len(text) > MAX_MESSAGE_CHARS:
            raise LLMResponseInvalidError(
                public_message="That message is too long to interpret.",
                reason="message exceeds the supported length",
            )

        started = time.perf_counter()
        try:
            interpretation, outcome = await self._interpret_once(text, (), store_currency)
        except (LLMResponseInvalidError, TaxonomyValidationError) as exc:
            # One corrective attempt, told what was refused - our rule text,
            # never the message itself. An enumeration cannot stop a valid
            # category being paired with another category's product type, and
            # a model told so usually picks correctly the second time.
            problems = _problems(exc)
            logger.warning("query_understanding_retrying", problems=list(problems))
            interpretation, outcome = await self._interpret_once(text, problems, store_currency)
        if _category_only(outcome):
            interpretation, outcome = await self._check_type(
                text, interpretation, outcome, store_currency
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

        # Safe operational fields only: no raw customer text, no model response.
        logger.info(
            "query_understanding_completed",
            prompt_version=VERSION,
            model=self._client.model,
            outcome=type(outcome).__name__,
            resolved=isinstance(outcome, ResolvedSearch),
            commerce_category=interpretation.commerce_category,
            commerce_subcategory=interpretation.commerce_subcategory,
            price_extracted=interpretation.price_min is not None
            or interpretation.price_max is not None,
            capacity_extracted=interpretation.seating_capacity_min is not None
            or interpretation.seating_capacity_max is not None,
            sort=str(interpretation.sort) if interpretation.sort else None,
            clarification_reason=(
                str(outcome.reason) if isinstance(outcome, ClarificationRequired) else None
            ),
            unsupported=(
                [str(f) for f in outcome.unsupported]
                if isinstance(outcome, UnsupportedRequirement)
                else None
            ),
            unsupported_dimensions=(
                [f"{d.role}:{d.reason}" for d in outcome.unsupported_dimensions]
                if isinstance(outcome, UnsupportedDimensionRequirement)
                else None
            ),
            dimension_roles=[
                str(d.role) for d in interpretation.dimensions if d.role is not None
            ],
            elapsed_ms=elapsed_ms,
        )
        return outcome

    async def _check_type(
        self,
        text: str,
        interpretation: CommerceInterpretation,
        outcome: QueryInterpretation,
        store_currency: str | None,
    ) -> tuple[CommerceInterpretation, QueryInterpretation]:
        """One more look when an answer named a category and no type.

        Kept only when it names a type in the same category; a failure, a
        different category or a type still left null keeps the first answer -
        a genuinely broad request stays broad. At most one extra call, and
        only on the category-level answers (known issue 15)."""
        try:
            checked, checked_outcome = await self._interpret_once(
                text, (), store_currency, check=build_type_check()
            )
        except (LLMResponseInvalidError, TaxonomyValidationError, IntegrationUnavailableError):
            return interpretation, outcome
        first = getattr(outcome, "request", None)
        second = getattr(checked_outcome, "request", None)
        kept = (
            first is not None
            and second is not None
            and second.commerce_category == first.commerce_category
            and second.commerce_subcategory is not None
        )
        logger.info("query_understanding_type_checked", typed=kept)
        return (checked, checked_outcome) if kept else (interpretation, outcome)

    async def _interpret_once(
        self,
        text: str,
        problems: tuple[str, ...],
        store_currency: str | None = None,
        *,
        check: str = "",
    ) -> tuple[CommerceInterpretation, QueryInterpretation]:
        """One provider call and its validation; corrective when `problems` is
        set, a second look at one field when `check` is."""
        instructions = (
            self._instructions + build_correction(problems) if problems else self._instructions
        ) + check
        interpretation = await self._client.parse(
            instructions=instructions,
            user_input=text,
            schema=self._schema,
        )
        return interpretation, self._resolve(interpretation, text, store_currency)

    def _resolve(
        self,
        interpretation: CommerceInterpretation,
        message: str = "",
        store_currency: str | None = None,
    ) -> QueryInterpretation:
        if interpretation.multiple_product_types:
            # One request cannot serve two product families, and picking one
            # would silently drop the other. Splitting a message into several
            # searches belongs to the future conversational layer.
            return ClarificationRequired(
                reason=ClarificationReason.MULTIPLE_PRODUCT_TYPES
            )

        category = (interpretation.commerce_category or "").strip()
        if not category:
            return ClarificationRequired(
                reason=ClarificationReason.NO_COMMERCE_CATEGORY
            )

        subcategory = (interpretation.commerce_subcategory or "").strip() or None
        # Taxonomy first: an unapproved value never reaches the domain contract,
        # and these raise rather than degrading into a broader search.
        if subcategory is not None:
            self._taxonomy.validate_pair(category, subcategory)
        else:
            self._taxonomy.subcategories(category)  # raises if unapproved

        price = self._price(interpretation, store_currency)
        if isinstance(price, ClarificationRequired):
            return price

        capacity = self._capacity(interpretation)
        strict, preferences, unresolved = self._sort_attributes(interpretation.attributes)

        dimensions = self._dimensions(interpretation, subcategory)
        if isinstance(dimensions, ClarificationRequired):
            return dimensions
        try:
            request = ProductSearchRequest(
                commerce_category=category,
                commerce_subcategory=subcategory,
                price=price,
                seating_capacity=capacity,
                dimensions=dimensions.supported,
                planar_dimensions=dimensions.planar,
                colors_any_of=strict[AttributeFamily.COLOR],
                styles_all_of=strict[AttributeFamily.STYLE],
                sort=interpretation.sort or ProductSort.DEFAULT,
            )
        except ValidationError as exc:
            raise LLMResponseInvalidError(reason="request validation failed") from exc

        semantics = ConstraintSemantics(
            subcategory=_strength(
                interpretation.commerce_subcategory_strength,
                present=subcategory is not None,
            ),
            price_min=_strength(
                interpretation.price_min_strength,
                present=price is not None and price.min_amount is not None,
            ),
            price_max=_strength(
                interpretation.price_max_strength,
                present=price is not None and price.max_amount is not None,
            ),
            seating_min=_strength(
                interpretation.seating_capacity_min_strength,
                present=capacity is not None and capacity.min_capacity is not None,
            ),
            seating_max=_strength(
                interpretation.seating_capacity_max_strength,
                present=capacity is not None and capacity.max_capacity is not None,
            ),
            # Carried, not re-derived. The structured schema is the single
            # place a measurement's firmness is interpreted; reading the
            # customer's words a second time here could disagree with it.
            dimensions=dimensions.semantics,
            planar_dimension=dimensions.planar_semantics,
        )

        unsupported = tuple(dict.fromkeys(interpretation.unsupported_requirements))
        if unsupported:
            # The request is still executable; what it cannot do is honour the
            # material or size the customer named.
            return UnsupportedRequirement(
                request=request, semantics=semantics, unsupported=unsupported
            )
        if dimensions.unsupported:
            # The measurement was clear; this catalog's axes cannot answer it.
            # Asking again would not help, so this is not a clarification.
            return UnsupportedDimensionRequirement(
                request=request,
                semantics=semantics,
                unsupported_dimensions=dimensions.unsupported,
            )
        asked_kind = _asked_kind(interpretation, subcategory or category, message)
        if unresolved:
            # They were strict about something no filter can guarantee. Settle
            # that conversationally before anything else happens to the search.
            return UnresolvedStrictRequirement(
                request=request,
                semantics=semantics,
                unresolved=unresolved,
                semantic_preferences=preferences,
                semantic_text=(interpretation.semantic_text or "").strip() or None,
                asked_kind=asked_kind,
                kind_required=interpretation.only_this_kind,
            )
        return ResolvedSearch(
            request=request,
            semantics=semantics,
            semantic_preferences=preferences,
            semantic_text=(interpretation.semantic_text or "").strip() or None,
            asked_kind=asked_kind,
            kind_required=interpretation.only_this_kind,
        )

    def _to_centimetres(
        self, raw: str, unit_text: str | None, *, field: str
    ) -> Decimal | None:
        """Convert one stated measurement to centimetres, or refuse.

        Uses the shared unit vocabulary, so there is no second converter and a
        unit the catalog understands is a unit a customer may use.
        """
        unit = parse_unit(unit_text)
        if unit is None:
            return None
        return to_centimetres(_to_decimal(raw, field=field), unit)

    def _dimensions(
        self, interpretation: CommerceInterpretation, subcategory: str | None
    ) -> _StatedDimensions | ClarificationRequired:
        """Split stated measurements into supported, unsupported and planar.

        Two failures are kept apart deliberately. A number with no stated
        measurement, or no unit, is *ambiguous customer intent* - one question
        settles it. A clear measurement the registry cannot map is a *catalog*
        limitation - no question settles that.

        Nothing is recorded for an incomplete measurement: a strength attached
        to a number whose meaning we had to ask about would be a claim about a
        requirement that does not exist yet.
        """
        supported: list[DimensionConstraint] = []
        semantics: list[DimensionConstraintSemantics] = []
        unsupported: list[UnsupportedDimension] = []

        for stated in interpretation.dimensions:
            if stated.role is None:
                return ClarificationRequired(
                    reason=ClarificationReason.MISSING_DIMENSION_ROLE
                )
            values = self._dimension_values(stated)
            if values is None:
                return ClarificationRequired(
                    reason=ClarificationReason.MISSING_DIMENSION_UNIT
                )
            min_cm, max_cm, target_cm = values

            if self._dimension_semantics.source_axis(subcategory, stated.role) is None:
                # Strength travels on the refusal itself, because the request
                # will not carry this measurement at all.
                unsupported.append(
                    UnsupportedDimension(
                        role=stated.role,
                        kind=stated.kind,
                        min_cm=min_cm,
                        max_cm=max_cm,
                        target_cm=target_cm,
                        strength=stated.strength,
                        reason=self._dimension_semantics.unsupported_reason(
                            subcategory, stated.role
                        ),
                    )
                )
                continue
            try:
                supported.append(
                    DimensionConstraint(
                        role=stated.role,
                        kind=stated.kind,
                        min_cm=min_cm,
                        max_cm=max_cm,
                        target_cm=target_cm,
                        source_value=stated.max_value or stated.min_value or stated.target_value,
                        source_unit=stated.unit,
                    )
                )
            except ValidationError as exc:
                raise LLMResponseInvalidError(
                    reason="dimension constraint invalid"
                ) from exc
            semantics.append(
                DimensionConstraintSemantics(
                    role=stated.role, strength=stated.strength
                )
            )

        planar = self._planar(interpretation.planar_dimensions, subcategory)
        if isinstance(planar, ClarificationRequired):
            return planar
        constraint, planar_semantics = planar
        return _StatedDimensions(
            supported=tuple(supported),
            semantics=tuple(semantics),
            unsupported=tuple(unsupported),
            planar=constraint,
            planar_semantics=planar_semantics,
        )

    def _dimension_values(
        self, stated: DimensionInterpretation
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None] | None:
        """The stated bounds in centimetres, or ``None`` when the unit is unusable."""
        converted: list[Decimal | None] = []
        for raw, field in (
            (stated.min_value, "min_value"),
            (stated.max_value, "max_value"),
            (stated.target_value, "target_value"),
        ):
            if raw is None:
                converted.append(None)
                continue
            value = self._to_centimetres(raw, stated.unit, field=field)
            if value is None:
                return None
            converted.append(value)
        return converted[0], converted[1], converted[2]

    def _planar(
        self, stated: PlanarDimensionInterpretation | None, subcategory: str | None
    ) -> (
        tuple[PlanarDimensionConstraint | None, PlanarDimensionSemantics | None]
        | ClarificationRequired
    ):
        """The pair and its strength, together or not at all."""
        if stated is None:
            return None, None
        if not self._dimension_semantics.supports_planar(subcategory):
            # A pair means nothing where sides have no agreed order; fall back
            # to asking rather than guessing which side is which.
            return ClarificationRequired(
                reason=ClarificationReason.MISSING_DIMENSION_ROLE
            )
        first = self._to_centimetres(stated.first_value, stated.unit, field="first_value")
        second = self._to_centimetres(stated.second_value, stated.unit, field="second_value")
        if first is None or second is None:
            return ClarificationRequired(
                reason=ClarificationReason.MISSING_DIMENSION_UNIT
            )
        try:
            constraint = PlanarDimensionConstraint(
                first_cm=first, second_cm=second, source_unit=stated.unit
            )
        except ValidationError as exc:
            raise LLMResponseInvalidError(reason="planar dimensions invalid") from exc
        return constraint, PlanarDimensionSemantics(strength=stated.strength)

    def _sort_attributes(
        self, attributes: Sequence[AttributeInterpretation]
    ) -> tuple[
        dict[AttributeFamily, tuple[str, ...]],
        tuple[SemanticPreference, ...],
        tuple[UnresolvedAttribute, ...],
    ]:
        """Split colour and style mentions three ways.

        Colour and style behave differently from price and capacity here.
        Ordinary shopping language - "I want a beige sofa" - is a leaning, not
        a rule, so it becomes a preference rather than a filter. Only explicitly
        strict wording earns an exact filter, and only when the value is one the
        catalog actually records.

        A strict value the vocabulary does not contain can be neither filtered
        nor promised, so it is surfaced rather than quietly downgraded.
        """
        strict: dict[AttributeFamily, list[str]] = {
            AttributeFamily.COLOR: [],
            AttributeFamily.STYLE: [],
        }
        preferences: list[SemanticPreference] = []
        unresolved: list[UnresolvedAttribute] = []

        for attribute in attributes:
            stated = (attribute.canonical_value or "").strip() or None
            # Untrusted model output: a value must belong to the family claimed
            # for it, so a style can never arrive as a colour. Returned in the
            # registry's spelling, which is what the stored data uses.
            canonical = (
                self._attributes.canonical(attribute.family, stated) if stated else None
            )
            if stated is not None and canonical is None:
                raise LLMResponseInvalidError(
                    reason=f"{attribute.family} value outside the approved vocabulary"
                )

            if attribute.strength is not ConstraintStrength.LOCKED:
                preferences.append(
                    SemanticPreference(
                        family=attribute.family,
                        raw_value=attribute.raw_value,
                        canonical_value=canonical,
                        strength=attribute.strength,
                    )
                )
            elif canonical is not None:
                if canonical not in strict[attribute.family]:
                    strict[attribute.family].append(canonical)
            else:
                unresolved.append(
                    UnresolvedAttribute(
                        family=attribute.family, raw_value=attribute.raw_value
                    )
                )

        return (
            {family: tuple(values) for family, values in strict.items()},
            tuple(preferences),
            tuple(unresolved),
        )

    def _price(
        self, interpretation: CommerceInterpretation, store_currency: str | None = None
    ) -> PriceConstraint | ClarificationRequired | None:
        raw_min, raw_max = interpretation.price_min, interpretation.price_max
        if raw_min is None and raw_max is None:
            return None

        # A currency they named wins; otherwise the store's own, when its
        # catalog names exactly one - a catalog fact, not a guess. With
        # neither, asking is the only honest option.
        currency = (interpretation.price_currency or "").strip() or (store_currency or "")
        if not currency:
            return ClarificationRequired(
                reason=ClarificationReason.MISSING_PRICE_CURRENCY
            )
        try:
            return PriceConstraint(
                currency=currency,
                min_amount=(
                    _to_decimal(raw_min, field="price_min", money=True) if raw_min else None
                ),
                max_amount=(
                    _to_decimal(raw_max, field="price_max", money=True) if raw_max else None
                ),
            )
        except ValidationError as exc:
            raise LLMResponseInvalidError(reason="price constraint invalid") from exc

    def _capacity(
        self, interpretation: CommerceInterpretation
    ) -> SeatingCapacityConstraint | None:
        low, high = (
            interpretation.seating_capacity_min,
            interpretation.seating_capacity_max,
        )
        if low is None and high is None:
            return None
        try:
            return SeatingCapacityConstraint(min_capacity=low, max_capacity=high)
        except ValidationError as exc:
            raise LLMResponseInvalidError(reason="capacity constraint invalid") from exc
