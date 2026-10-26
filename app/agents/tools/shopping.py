"""Sara's tools: finding products, looking closer, comparing, and the cart.

Each tool takes what the customer meant in typed arguments - a search stated
in approved values, a card by its place on screen - and returns verified facts.
Store scope and product identity never pass through a model.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, create_model

from app.agents.context import ToolContext
from app.agents.facts import (
    cart_facts,
    product_facts,
    search_facts,
    searched_for,
)
from app.agents.operations.rooms import open_room_options
from app.agents.payloads import (
    ComparisonShown,
    ProductShown,
    SearchShown,
    SelectionShown,
)
from app.agents.services import AgentServices
from app.agents.tools.base import ToolArgs, ToolOutcome, ZoryTool
from app.agents.tools.schemas import ToolSchemas
from app.agents.work import ToolStatus, TurnWork
from app.core.exceptions import (
    IntegrationUnavailableError,
    InvalidRequestError,
    LLMResponseInvalidError,
)
from app.schemas.agent_persona import AgentPersona
from app.schemas.agent_state import (
    AgentStateV1,
)
from app.schemas.agent_updates import AgentStateUpdate, ProductInteractionUpdate
from app.schemas.comparison import MAX_COMPARED_PRODUCTS
from app.schemas.design import DesignDirection, NextSize
from app.schemas.discovery import ProductSearchRequest
from app.schemas.grounding import GroundedProduct
from app.schemas.product import ProductCandidate
from app.schemas.product_reference import PresentedOrdinal, ProductReferenceSelector
from app.schemas.query import (
    ClarificationReason,
    ClarificationRequired,
    CommerceInterpretation,
    ConstraintStrength,
    RankingLean,
    ResolvedSearch,
    UnresolvedStrictRequirement,
    UnsupportedDimensionRequirement,
    UnsupportedRequirement,
)
from app.schemas.resolution import (
    ComparisonUnavailable,
    ResolvedProductReference,
    SimilarSearchSeed,
)
from app.services.agent_state import (
    add_to_goal,
    apply_update,
    designer_size,
    remember_next_sizes,
)
from app.services.design_checks import figure_free, stocked_direction
from app.services.fit import room_fit
from app.services.grounding_builder import to_grounded_product
from app.services.query_document import ranking_words
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.words import customer_words

SARA = frozenset({AgentPersona.SARA})
SHOPPERS = frozenset({AgentPersona.SARA, AgentPersona.SALMAN})
"""Sara owns the cart; Salman adds to it when what he suggested is taken."""
SEARCHERS = SHOPPERS | {AgentPersona.NORA}
"""Nora searches too: for a piece her room could take."""


# ── shared ──────────────────────────────────────────────────────────────────


class ProductRef(ToolArgs):
    which: ProductReferenceSelector = Field(
        description=(
            "Which product: a card on screen by position (presented_ordinal), a pick in the "
            "cart (picked_ordinal), a comparison column (compared_ordinal), a liked product "
            "(liked_product), the one being discussed (focused_product), or a card by its "
            "colour/style or as the cheapest/dearest on screen."
        )
    )


async def resolve_product(
    services: AgentServices, which: ProductReferenceSelector, work: TurnWork, context: ToolContext
) -> int | ToolOutcome:
    """The product a reference names on the screen, or why not.

    A card a tapped button named is read on the list it was tapped on, which
    may be an earlier one still in the chat - never the newest list's card at
    the same position.
    """
    tapped = next(
        (
            card
            for card in context.tap_cards
            if isinstance(which, PresentedOrdinal) and card.ordinal == which.position
        ),
        None,
    )
    if tapped is not None:
        outcome = await services.references.resolve_on_list(
            tapped.ordinal, tapped.list_revision, work.pre_turn, context.retailer
        )
    else:
        outcome = await services.references.resolve(which, work.staged, context.retailer)
    if isinstance(outcome, ResolvedProductReference):
        return outcome.product_id
    return ToolOutcome(status=ToolStatus.REFUSED, code=f"reference_{outcome.reason}")


async def hydrated(
    services: AgentServices, product_ids: tuple[int, ...], context: ToolContext
) -> tuple[ProductCandidate, ...] | None:
    try:
        return tuple(await services.hydration.hydrate_ids(product_ids, context.retailer))
    except IntegrationUnavailableError:
        return None


def grounded(products: tuple[ProductCandidate, ...]) -> tuple[GroundedProduct, ...]:
    return tuple(
        to_grounded_product(p, grounding_ref=n, presented_ordinal=n)
        for n, p in enumerate(products, start=1)
    )


UNAVAILABLE = ToolOutcome(status=ToolStatus.REFUSED, code="catalog_unavailable")


def _focused(state: AgentStateV1, product_id: int) -> AgentStateV1:
    return apply_update(
        state,
        AgentStateUpdate(
            product_interaction=ProductInteractionUpdate(focused_product_id=product_id)
        ),
    )


# ── search ──────────────────────────────────────────────────────────────────


def agent_search(
    interpretation: type[CommerceInterpretation], attributes: CatalogAttributes
) -> type[ToolArgs]:
    """The search as an agent writes it: the interpretation, with colours and
    styles as plain lists of approved values - required or wanted - in place of
    the four-part `attributes` entries a model too easily leaves out or gets
    wrong. `as_interpretation` writes them back as attributes, so everything
    downstream reads one contract."""
    # Types built from the registry at startup, as the interpretation's own are.
    colours: Any = list[Literal[tuple(sorted(attributes.colors))]]  # type: ignore[misc]
    styles: Any = list[Literal[tuple(sorted(attributes.styles))]]  # type: ignore[misc]
    fields: dict[str, Any] = {
        name: (field.annotation, field)
        for name, field in interpretation.model_fields.items()
        if name != "attributes"
    }
    fields.update(
        colours_required=(
            colours,
            Field(
                default_factory=list,
                description=(
                    "Colours they insist on ('only beige', or asked for again): a filter."
                ),
            ),
        ),
        colours_wanted=(
            colours,
            Field(
                default_factory=list,
                description="Colours they would like: first in the order, nothing hidden.",
            ),
        ),
        styles_required=(
            styles,
            Field(default_factory=list, description="Styles they insist on: a filter."),
        ),
        styles_wanted=(
            styles,
            Field(
                default_factory=list,
                description="Styles they would like: first in the order, nothing hidden.",
            ),
        ),
    )
    return cast(type[ToolArgs], create_model("Search", __base__=ToolArgs, **fields))


def as_interpretation(
    search: Any, interpretation: type[CommerceInterpretation]
) -> CommerceInterpretation:
    """The agent's search in the one contract query understanding reads: each
    colour and style an approved value with its strength."""
    looks = [
        (family, value, strength)
        for family, required, wanted in (
            (AttributeFamily.COLOR, search.colours_required, search.colours_wanted),
            (AttributeFamily.STYLE, search.styles_required, search.styles_wanted),
        )
        for values, strength in (
            (required, ConstraintStrength.LOCKED),
            (wanted, ConstraintStrength.PREFERRED),
        )
        for value in values
    ]
    plain = search.model_dump(
        exclude={"colours_required", "colours_wanted", "styles_required", "styles_wanted"}
    )
    return interpretation.model_validate(
        {
            **plain,
            "attributes": [
                {
                    "family": family,
                    "raw_value": value,
                    "canonical_value": value,
                    "strength": strength,
                }
                for family, value, strength in looks
            ],
        }
    )


def direction_args(attributes: CatalogAttributes) -> type[ToolArgs]:
    """Nora's design direction in approved values only, within the bounds
    every direction is held to (CLAUDE.md 10.5)."""
    colours: Any = list[Literal[tuple(sorted(attributes.colors))]]  # type: ignore[misc]
    styles: Any = list[Literal[tuple(sorted(attributes.styles))]]  # type: ignore[misc]
    return cast(
        type[ToolArgs],
        create_model(
            "Direction",
            __base__=ToolArgs,
            colours=(colours, Field(default_factory=list, max_length=3)),
            styles=(styles, Field(default_factory=list, max_length=2)),
            avoid_colours=(colours, Field(default_factory=list, max_length=2)),
            avoid_styles=(styles, Field(default_factory=list, max_length=2)),
            size_ratio=(
                float | None,
                Field(
                    default=None,
                    ge=0.2,
                    le=3.0,
                    description=(
                        "This piece's longer side as a share of the first matched piece's: "
                        "0.66 for a table two thirds of the sofa."
                    ),
                ),
            ),
            why=(
                str,
                Field(
                    min_length=1,
                    max_length=200,
                    description="Why it sits well beside theirs, in words - no figures.",
                ),
            ),
        ),
    )


def search_args(
    interpretation: type[CommerceInterpretation], attributes: CatalogAttributes
) -> type[ToolArgs]:
    """The search tool's arguments: the agent's search, in approved values only."""
    return cast(
        type[ToolArgs],
        create_model(
            "SearchProductsArgs",
            __base__=ToolArgs,
            search=(
                agent_search(interpretation, attributes),
                Field(
                    description=(
                        "The whole search as the customer wants it now - restate every "
                        "requirement still in force, not only what changed."
                    )
                ),
            ),
            match_with=(
                list[ProductReferenceSelector],
                Field(
                    default_factory=list,
                    max_length=6,
                    description=(
                        "Products this search should go with - cards, cart picks, liked. "
                        "Nora's direction (colours, styles, what to avoid, size) orders the "
                        "results; nothing is hidden."
                    ),
                ),
            ),
            match_goal=(
                int | None,
                Field(
                    default=None,
                    ge=1,
                    description="Match the products already chosen for this goal number.",
                ),
            ),
            match_room=(
                bool,
                Field(default=False, description="Match the room being designed, its pieces."),
            ),
            direction=(
                direction_args(attributes) | None,
                Field(
                    default=None,
                    description=(
                        "Nora's design direction for this kind beside what they chose "
                        "(match_with, match_goal, match_room): colours and styles that sit "
                        "well, what would clash, and a size in proportion to the first "
                        "piece. It orders the results, never hides any. Nora's alone."
                    ),
                ),
            ),
            designer_size=(
                bool,
                Field(
                    default=False,
                    description=(
                        "Order by the size the designer gave for this kind in their room "
                        "(room.next_sizes): closest first, nothing hidden. A size the "
                        "customer requires is the search's own."
                    ),
                ),
            ),
            head_count=(
                int | None,
                Field(
                    default=None,
                    ge=1,
                    le=30,
                    description=(
                        "How many usually sit there, when they say so. It orders the "
                        "results - pieces that seat them first - and hides nothing; a seat "
                        "count they require (a 3-seater) is the search's seats instead."
                    ),
                ),
            ),
            earlier_sizes=(
                bool,
                Field(
                    default=True,
                    description=(
                        "Bring back sizes they gave for this kind before when this search "
                        "names none. False when they say any size is fine."
                    ),
                ),
            ),
            with_related_types=(
                bool,
                Field(
                    default=True,
                    description=(
                        "Also show the types usually bought instead (sofa sets and "
                        "sectionals beside sofas). False when they asked for the type alone."
                    ),
                ),
            ),
        ),
    )


_SETTLES: dict[ClarificationReason, str] = {
    ClarificationReason.MULTIPLE_PRODUCT_TYPES: (
        "one search per kind of product: search each kind on its own call, or ask "
        "which they want first"
    ),
    ClarificationReason.NO_COMMERCE_CATEGORY: (
        "a kind of product: the one the conversation makes plain, or ask which"
    ),
    ClarificationReason.MISSING_PRICE_CURRENCY: (
        "the amount's currency: the store's when the context gives one, otherwise ask"
    ),
    ClarificationReason.MISSING_DIMENSION_ROLE: (
        "which measurement the figure is (width, depth, height, length), or ask"
    ),
    ClarificationReason.MISSING_DIMENSION_UNIT: "the figure's unit, or ask",
}
"""What a search needs before it can run - for the agent to settle or ask."""


def _executable(outcome: Any) -> tuple[ResolvedSearch | None, dict[str, Any]]:
    """What can run, and what of the request it could not enforce."""
    if isinstance(outcome, ResolvedSearch):
        return outcome, {}
    if isinstance(outcome, ClarificationRequired):
        return None, {
            "missing": str(outcome.reason),
            "what_would_settle_it": _SETTLES.get(outcome.reason, "ask them"),
        }
    request, semantics = outcome.request, outcome.semantics
    if isinstance(outcome, UnresolvedStrictRequirement):
        not_enforced = [f"{u.family}: {u.raw_value}" for u in outcome.unresolved]
        prefs = outcome.semantic_preferences
        text = outcome.semantic_text
    elif isinstance(outcome, UnsupportedDimensionRequirement):
        not_enforced = [f"{d.role} ({d.reason})" for d in outcome.unsupported_dimensions]
        prefs, text = (), None
    else:
        assert isinstance(outcome, UnsupportedRequirement)
        not_enforced = [str(f) for f in outcome.unsupported]
        prefs, text = (), None
    resolved = ResolvedSearch(
        request=request, semantics=semantics, semantic_preferences=prefs, semantic_text=text
    )
    return resolved, {"not_enforced": not_enforced}


def search_outcome(run: Any, extra: dict[str, Any]) -> ToolOutcome:
    if run.grounding is None:
        return ToolOutcome(status=ToolStatus.REFUSED, code="search_unavailable")
    facts = {**search_facts(run.grounding), **extra}
    code = None if run.grounding.products else "no_products"
    return ToolOutcome(
        code=code, facts=facts, state=run.state, payload=SearchShown(grounding=run.grounding)
    )


async def _design_direction(
    services: AgentServices,
    args: Any,
    work: TurnWork,
    context: ToolContext,
    resolved: ResolvedSearch,
) -> tuple[RankingLean | None, dict[str, Any], tuple[ProductCandidate, ...], tuple[NextSize, ...]]:
    """What the search is matched with, and Nora's direction for this kind
    beside it - a ranking lean that orders and never filters (CLAUDE.md 10.5).

    The direction is hers, written as arguments; code only keeps it to the
    colours and styles the store stocks for the kind and works out the size
    from the first matched piece. No model is consulted here."""
    state = work.staged
    direction = args.direction
    if direction is not None and work.working is not AgentPersona.NORA:
        # Whether a piece goes with theirs is the designer's judgement.
        return None, {"direction": "matching_is_noras"}, (), ()
    if direction is not None and not figure_free(direction.why):
        return None, {"direction": "why_states_a_figure"}, (), ()
    ids: list[int] = []
    for which in args.match_with:
        product = await resolve_product(services, which, work, context)
        if isinstance(product, ToolOutcome):
            return None, {"match": product.code}, (), ()
        ids.append(product)
    picks = state.product_interaction.selected_product_ids
    if args.match_goal is not None:
        goal = next((g for g in state.goals if g.goal == args.match_goal), None)
        ids.extend(p for p in (goal.members if goal else ()) if p in picks)
    room = state.room_project
    if args.match_room and room is not None:
        ids.extend(line.product_id for line in room.bundle_items)
    ids = list(dict.fromkeys(ids))[:12]
    if not ids:
        return None, {}, (), ()
    request = resolved.request
    try:
        products = tuple(await services.hydration.hydrate_ids(tuple(ids), context.retailer))
    except (IntegrationUnavailableError, InvalidRequestError):
        return None, {"direction": "unavailable"}, (), ()
    matched: dict[str, Any] = {"matched_with": [p.name_english for p in products]}
    if not products or direction is None:
        return None, matched, products, ()
    look = None
    if services.looks is not None and request.commerce_subcategory:
        try:
            look = await services.looks.of(request.commerce_subcategory, context.retailer)
        except IntegrationUnavailableError:
            look = None
    if look is None:
        # What the kind comes in is unknown, so nothing can be checked and
        # nothing leans - not a sign the values are unstocked.
        return None, {**matched, "direction": "looks_unknown"}, products, ()
    kept, dropped = stocked_direction(
        DesignDirection(
            colours=tuple(direction.colours),
            styles=tuple(direction.styles),
            avoid_colours=tuple(direction.avoid_colours),
            avoid_styles=tuple(direction.avoid_styles),
            size_ratio=direction.size_ratio,
        ),
        look,
    )
    if dropped:
        matched["direction_dropped"] = (
            "values this kind is not stocked in were left out of the direction"
        )
    if kept is None:
        return None, {**matched, "direction": "none_stocked"}, products, ()
    anchor = products[0].dimensions
    longer = max((v for v in (anchor.length_cm, anchor.width_cm) if v is not None), default=None)
    target = (
        (longer * Decimal(str(kept.size_ratio))).quantize(Decimal(1))
        if longer is not None and kept.size_ratio is not None
        else None
    )
    lean = RankingLean(
        colours=kept.colours,
        styles=kept.styles,
        avoid_colours=kept.avoid_colours,
        avoid_styles=kept.avoid_styles,
        size_target_cm=target,
    )
    return (
        lean,
        {
            **matched,
            "direction": {
                "colours": list(kept.colours),
                "styles": list(kept.styles),
                "avoid": [*kept.avoid_colours, *kept.avoid_styles],
                **({"longer_side_near_cm": str(target)} if target is not None else {}),
                "why": direction.why,
            },
        },
        products,
        (),
    )


def _seat_facts(services: AgentServices, request: ProductSearchRequest) -> dict[str, Any]:
    """A fact for the agent, never a decision: this type never seats as few as
    asked, and which types seat one - "a sofa for one" may mean a single
    seater, or not; the agent reads the customer and decides."""
    seating = services.seating
    seats = request.seating_capacity
    if seating is None or seats is None or not seating.seats_several(request.commerce_subcategory):
        return {}
    if seats.max_capacity != 1 and not (seats.min_capacity == 1 and seats.max_capacity is None):
        return {}
    return {
        "type_never_seats_one": True,
        "one_seat_types": [customer_words(t) for t in seating.one_seat_types()],
    }


def _designer_questions(services: AgentServices, search: Any) -> dict[str, Any]:
    """The questions the store's designers ask for this kind, with their usual
    answers - for the agent to choose from, if it asks anything at all."""
    sheet = services.subcategory_questions
    category = getattr(search, "commerce_category", None)
    if sheet is None or not category:
        return {}
    row = sheet.for_search(category, getattr(search, "commerce_subcategory", None))
    if row is None:
        return {}
    return {
        "a_designer_might_ask": [
            {
                "question": q.text,
                **({"answers": [o.label for o in q.options][:8]} if q.options else {}),
            }
            for q in row.questions
        ]
    }


def search_tool(schemas: ToolSchemas) -> ZoryTool[Any]:
    args_model = search_args(schemas.interpretation, schemas.attributes)

    async def search(args: Any, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        interpretation = as_interpretation(args.search, schemas.interpretation)
        try:
            outcome = services.query_understanding.resolve(interpretation)
        except LLMResponseInvalidError:
            return ToolOutcome(status=ToolStatus.INVALID, code="figure_unreadable")
        resolved, extra = _executable(outcome)
        extra = {**extra, **_designer_questions(services, interpretation)}
        if resolved is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="needs_clarification", facts=extra)
        if not args.with_related_types:
            resolved = resolved.model_copy(
                update={"request": resolved.request.model_copy(update={"single_type": True})}
            )
        extra = {**extra, **_seat_facts(services, resolved.request)}
        if args.head_count is not None:
            resolved = resolved.model_copy(update={"seat_preference": args.head_count})
        # Their own words rank too, whatever the agent kept of them.
        resolved = resolved.model_copy(
            update={"customer_words": ranking_words(context.customer_said)}
        )
        lean, matched, chosen, sizes = await _design_direction(
            services, args, work, context, resolved
        )
        refused = matched.get("direction")
        if refused == "matching_is_noras":
            # Whether a piece goes with theirs is Nora's: nothing is searched
            # as matched on anyone else's word.
            return ToolOutcome(
                status=ToolStatus.REFUSED,
                code="matching_is_noras",
                facts={"hand_to": "nora", "need": "needs_design"},
            )
        if refused == "why_states_a_figure":
            return ToolOutcome(status=ToolStatus.INVALID, code="direction_why_states_a_figure")
        state = remember_next_sizes(work.staged, sizes)
        if args.designer_size:
            request = resolved.request
            size = designer_size(
                state.room_project, request.commerce_category, request.commerce_subcategory
            )
            if size is None:
                matched = {**matched, "designer_size": "none_for_this_kind"}
            else:
                # The designer's size for their room outranks a proportion of
                # the piece it was matched with.
                lean = (lean or RankingLean()).model_copy(
                    update={"size_target_cm": Decimal(size.longer_target_cm())}
                )
                matched = {
                    **matched,
                    "ordered_near_longer_side_cm": size.longer_target_cm(),
                    "size_reason": size.reason,
                }
        if lean is not None:
            resolved = resolved.model_copy(update={"lean": lean})
        extra = {**extra, **matched}
        run = await services.search.new(
            resolved,
            state,
            context.retailer,
            alongside=args.with_related_types,
            earlier_sizes=args.earlier_sizes,
        )
        if run.state is not None:
            extra = {"searched_for": searched_for(run.state.active_search), **extra}
        room = run.state.room_project if run.state is not None else None
        if run.grounding is not None and room is not None:
            extra = {**extra, **room_fit(run.grounding.products, room.geometry, chosen)}
        return search_outcome(run, extra)

    return ZoryTool(
        name="search_products",
        description=(
            "Search the store's catalog. Use for any new search or change to the search on "
            "screen (a new budget, colour, size, type). Strict words ('only', 'must') make a "
            "requirement strict; otherwise colours and styles are preferences that order "
            "the results. Name the product type (nightstand, service-table, bed) and its "
            "family is filled in for you - a nightstand is filed under tables. Returns the "
            "cards found, numbered as the customer will see them."
        ),
        args=args_model,
        owners=SEARCHERS,
        handler=search,
    )


class NoArgs(ToolArgs):
    pass


def more_results_tool(schemas: ToolSchemas) -> ZoryTool[NoArgs]:
    async def more(args: NoArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        shown = work.staged.product_interaction.presented_product_ids
        run = await services.search.again_excluding(shown, work.staged, context.retailer)
        if run is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="no_search_on_screen")
        return search_outcome(run, {})

    return ZoryTool(
        name="more_results",
        description="The next page of the search on screen, leaving out what was shown.",
        args=NoArgs,
        owners=SEARCHERS,
        handler=more,
    )


def exclude_tool(schemas: ToolSchemas) -> ZoryTool[ProductRef]:
    async def exclude(args: ProductRef, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        product = await resolve_product(services, args.which, work, context)
        if isinstance(product, ToolOutcome):
            return product
        run = await services.search.again_excluding((product,), work.staged, context.retailer)
        if run is None:
            return ToolOutcome(status=ToolStatus.REFUSED, code="no_search_on_screen")
        return search_outcome(run, {})

    return ZoryTool(
        name="leave_out_product",
        description="Run the search on screen again without one product they turned down.",
        args=ProductRef,
        owners=SEARCHERS,
        handler=exclude,
    )


def more_like_tool(schemas: ToolSchemas) -> ZoryTool[ProductRef]:
    async def more_like(args: ProductRef, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        product_id = await resolve_product(services, args.which, work, context)
        if isinstance(product_id, ToolOutcome):
            return product_id
        found = await hydrated(services, (product_id,), context)
        if not found:
            return UNAVAILABLE
        seed = services.similar_search.build(found[0])
        if not isinstance(seed, SimilarSearchSeed):
            return ToolOutcome(status=ToolStatus.REFUSED, code=f"similar_{seed.reason}")
        # "More like this" is a look they chose: its colour and styles are
        # kept as requirements, not leanings - only the approved values the
        # seed already checked. Nothing matching lifts them, as for any strict
        # colour, and the facts say so (CLAUDE.md 12.4).
        looks = seed.resolved.semantic_preferences
        request = seed.resolved.request.model_copy(
            update={
                "exclude_product_ids": (seed.reference_product_id,),
                "colors_any_of": tuple(
                    p.canonical_value
                    for p in looks
                    if p.family is AttributeFamily.COLOR and p.canonical_value
                ),
                "styles_all_of": tuple(
                    p.canonical_value
                    for p in looks
                    if p.family is AttributeFamily.STYLE and p.canonical_value
                ),
            }
        )
        resolved = seed.resolved.model_copy(update={"request": request})
        # A similar-product search is ours: it neither restores nor saves sizes.
        run = await services.search.new(
            resolved, work.staged, context.retailer, alongside=False, asked=False
        )
        asked = searched_for(run.state.active_search) if run.state is not None else {}
        return search_outcome(run, {"like": product_facts(found[0], None), "searched_for": asked})

    return ZoryTool(
        name="more_like_this",
        description="Products of the same kind leaning to one product's colour and style.",
        args=ProductRef,
        owners=SHOPPERS,
        handler=more_like,
    )


# ── a closer look ───────────────────────────────────────────────────────────


def details_tool(schemas: ToolSchemas) -> ZoryTool[ProductRef]:
    async def details(args: ProductRef, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        product_id = await resolve_product(services, args.which, work, context)
        if isinstance(product_id, ToolOutcome):
            return product_id
        found = await hydrated(services, (product_id,), context)
        if not found:
            return UNAVAILABLE
        product = to_grounded_product(found[0], grounding_ref=1)
        return ToolOutcome(
            facts={"product": product_facts(found[0], None), "arabic_name": found[0].name_arabic},
            state=_focused(work.staged, product_id),
            payload=ProductShown(product=product),
        )

    return ZoryTool(
        name="product_details",
        description="Every listed fact about one product - for 'tell me about the second one'.",
        args=ProductRef,
        owners=frozenset(AgentPersona),
        handler=details,
    )


class CompareArgs(ToolArgs):
    products: list[ProductReferenceSelector] = Field(min_length=2, max_length=MAX_COMPARED_PRODUCTS)


def compare_tool(schemas: ToolSchemas) -> ZoryTool[CompareArgs]:
    async def compare(args: CompareArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        ids: list[int] = []
        if len(context.tap_cards) >= 2:
            # Checked cards, each read on its own list, in the order checked -
            # card 2 of one list and card 2 of another are two products.
            for card in context.tap_cards:
                outcome = await services.references.resolve_on_list(
                    card.ordinal, card.list_revision, work.pre_turn, context.retailer
                )
                if not isinstance(outcome, ResolvedProductReference):
                    return ToolOutcome(
                        status=ToolStatus.REFUSED, code=f"reference_{outcome.reason}"
                    )
                if outcome.product_id not in ids:
                    ids.append(outcome.product_id)
        for which in args.products if not ids else ():
            product = await resolve_product(services, which, work, context)
            if isinstance(product, ToolOutcome):
                return product
            if product not in ids:
                ids.append(product)
        if len(ids) < 2:
            return ToolOutcome(status=ToolStatus.REFUSED, code="needs_two_products")
        result = await services.comparison.compare(ids, context.retailer)
        if isinstance(result, ComparisonUnavailable):
            return ToolOutcome(status=ToolStatus.REFUSED, code=f"comparison_{result.reason}")
        kinds = [p.commerce.subcategory for p in result.products]
        groups = services.compare_groups
        comparable = (
            groups.all_comparable(kinds)
            if groups is not None
            else kinds[0] is not None and len(set(kinds)) == 1
        )
        if not comparable:
            return ToolOutcome(status=ToolStatus.REFUSED, code="different_kinds")
        state = apply_update(
            work.staged,
            AgentStateUpdate(
                product_interaction=ProductInteractionUpdate(compared_product_ids=tuple(ids))
            ),
        )
        facts = {
            "columns": [product_facts(p, n) for n, p in enumerate(result.products, start=1)],
            "rows": [
                {
                    "field": str(row.field),
                    "values": [c.value if c.known else None for c in row.cells],
                    "status": str(row.status),
                }
                for row in result.rows
            ],
        }
        return ToolOutcome(facts=facts, state=state, payload=ComparisonShown(comparison=result))

    return ZoryTool(
        name="compare_products",
        description="A side-by-side table of two or more similar products (sofas with sofas).",
        args=CompareArgs,
        owners=SARA,
        handler=compare,
    )


# ── seating for a group ─────────────────────────────────────────────────────


# ── the cart and the liked list ─────────────────────────────────────────────


class CartItem(BaseModel):
    model_config = ProductRef.model_config
    which: ProductReferenceSelector
    quantity: int | None = Field(default=None, ge=1, le=20)


class AddToCartArgs(ToolArgs):
    items: list[CartItem] = Field(min_length=1, max_length=10)
    for_goal: int | None = Field(
        default=None, ge=1, description="The open goal these were chosen for, by its number."
    )
    instead_of_room: bool = Field(
        default=False,
        description=(
            "Only when they said to put a room piece's option in the cart rather than in the room."
        ),
    )


def add_to_cart_tool(schemas: ToolSchemas) -> ZoryTool[AddToCartArgs]:
    async def add(args: AddToCartArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        wanted: list[tuple[int, int | None]] = []
        for item in args.items:
            product = await resolve_product(services, item.which, work, context)
            if isinstance(product, ToolOutcome):
                return product
            wanted.append((product, item.quantity))
        piece = open_room_options(work.staged)
        shown = work.staged.product_interaction.presented_product_ids
        if piece is not None and not args.instead_of_room and any(p in shown for p, _ in wanted):
            # A room piece's option goes into the room in its place - the cart
            # is changed only when they say so.
            return ToolOutcome(
                status=ToolStatus.REFUSED,
                code="option_is_for_the_room",
                facts={"piece": piece, "use_instead": "swap_room_piece"},
            )
        change = await services.cart_operations.add(work.staged, context.retailer, wanted)
        if args.for_goal is not None and change.added:
            change.state = add_to_goal(change.state, args.for_goal, change.added)
        names = await _names(services, tuple(p for p, _ in wanted), context)
        facts = {
            "added": [names.get(p) for p in change.added],
            "already_in_cart": [names.get(p) for p in change.already],
            "refused_by_store": [
                {"name": names.get(r.product_id), "reason": r.reason, "kept": r.kept}
                for r in change.refused
            ],
            "cart_full": change.full,
            "cart": cart_facts(
                await hydrated(
                    services, change.state.product_interaction.selected_product_ids, context
                )
                or (),
                change.state,
            ),
        }
        code = "cart_full" if change.full and not change.added else None
        return ToolOutcome(code=code, facts=facts, state=change.state)

    return ZoryTool(
        name="add_to_cart",
        description=(
            "Put products in the customer's cart, with how many. Only when they said to "
            "add, take or buy it - liking a product is not adding it."
        ),
        args=AddToCartArgs,
        owners=SARA,
        handler=add,
    )


def remove_from_cart_tool(schemas: ToolSchemas) -> ZoryTool[ProductRef]:
    async def remove(args: ProductRef, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        product = await resolve_product(services, args.which, work, context)
        if isinstance(product, ToolOutcome):
            return product
        if product not in work.staged.product_interaction.selected_product_ids:
            return ToolOutcome(status=ToolStatus.REFUSED, code="not_in_cart")
        state = services.cart_operations.remove(work.staged, product)
        remaining = state.product_interaction.selected_product_ids
        products = await hydrated(services, remaining, context) or ()
        return ToolOutcome(facts={"cart": cart_facts(products, state)}, state=state)

    return ZoryTool(
        name="remove_from_cart",
        description="Take a product out of the cart.",
        args=ProductRef,
        owners=SARA,
        handler=remove,
    )


class LikeArgs(ProductRef):
    liked: bool = True


def like_tool(schemas: ToolSchemas) -> ZoryTool[LikeArgs]:
    async def like(args: LikeArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        product = await resolve_product(services, args.which, work, context)
        if isinstance(product, ToolOutcome):
            return product
        return ToolOutcome(
            state=services.cart_operations.like(work.staged, product, liked=args.liked)
        )

    return ZoryTool(
        name="like_product",
        description="Keep a product in their liked list (or take it out) - a taste signal.",
        args=LikeArgs,
        owners=SARA,
        handler=like,
    )


class ShowChosenArgs(ToolArgs):
    liked: bool = Field(default=False, description="The liked list instead of the cart.")


def show_chosen_tool(schemas: ToolSchemas) -> ZoryTool[ShowChosenArgs]:
    async def show(args: ShowChosenArgs, context: ToolContext, work: TurnWork) -> ToolOutcome:
        services = context.services
        interaction = work.staged.product_interaction
        ids = interaction.liked_product_ids if args.liked else interaction.selected_product_ids
        if not ids:
            return ToolOutcome(code="empty", facts={"count": 0})
        found = await hydrated(services, tuple(ids), context)
        if found is None:
            return UNAVAILABLE
        by_id = {p.product_id: p for p in found}
        ordered = tuple(by_id[i] for i in ids if i in by_id)
        facts = (
            # Numbered by place in the liked list, gaps kept, as a `liked`
            # reference reads it.
            {
                "liked": [
                    product_facts(by_id[i], n) for n, i in enumerate(ids, start=1) if i in by_id
                ]
            }
            if args.liked
            else cart_facts(ordered, work.staged)
        )
        return ToolOutcome(
            facts=facts, payload=SelectionShown(products=grounded(ordered), liked=args.liked)
        )

    return ZoryTool(
        name="show_cart",
        description="Put the products in their cart (or their liked list) on screen.",
        args=ShowChosenArgs,
        owners=SARA,
        handler=show,
    )


async def _names(
    services: AgentServices, ids: tuple[int, ...], context: ToolContext
) -> dict[int, str]:
    found = await hydrated(services, ids, context) or ()
    return {p.product_id: p.name_english for p in found}


def shopping_tools(schemas: ToolSchemas) -> list[ZoryTool[Any]]:
    return [
        search_tool(schemas),
        more_results_tool(schemas),
        exclude_tool(schemas),
        more_like_tool(schemas),
        details_tool(schemas),
        compare_tool(schemas),
        add_to_cart_tool(schemas),
        remove_from_cart_tool(schemas),
        like_tool(schemas),
        show_chosen_tool(schemas),
    ]
