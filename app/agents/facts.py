"""Products and results as an agent reads them: verified, id-free, short.

The model sees only what a card shows - name, kind, colour, styles, seats,
price, size - numbered as the customer sees it. Nothing here names a product
id, a URL or the store; the screen is built from the same records by code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.schemas.agent_brief import BriefProduct, CartBrief, CartLine, MoneyTotal
from app.schemas.agent_state import ActiveSearchState, AgentStateV1
from app.schemas.design import InteriorDesignResult, NextSize
from app.schemas.grounding import GroundedProduct, SearchExecutionGrounding
from app.schemas.product import ProductCandidate
from app.taxonomy.attributes import AttributeFamily
from app.taxonomy.words import customer_words


def brief_product(
    product: GroundedProduct | ProductCandidate, card: int | None, *, arabic: bool = False
) -> BriefProduct:
    """One product, as its card shows it - with its Arabic name for a reply in
    Arabic."""
    return BriefProduct(
        card=card,
        name=product.name_english,
        name_ar=(product.name_arabic or None) if arabic else None,
        kind=product.commerce.subcategory or product.commerce.category,
        price=plain(product.price_amount),
        currency=product.price_unit,
        colour=product.main_color or None,
        styles=tuple(product.styles) or None,
        seats=product.commerce.seating_capacity or None,
        size_cm=_size(product) or None,
    )


def product_facts(product: GroundedProduct | ProductCandidate, card: int | None) -> dict[str, Any]:
    """One product, as a tool reports it."""
    return brief_product(product, card).model_dump(mode="json", exclude_none=True)


def _size(product: GroundedProduct | ProductCandidate) -> dict[str, str]:
    """The piece's floor sides and height. Merchants put a floor measurement in
    either column, so a side is the longer or the shorter of the two - never a
    column's name, which says nothing of what a customer calls its width
    (CLAUDE.md 15.1)."""
    d = product.dimensions
    floor = sorted(v for v in (d.length_cm, d.width_cm) if v is not None)
    sides: dict[str, Decimal | None] = {}
    if len(floor) == 2:
        sides = {"longer_side": floor[1], "shorter_side": floor[0]}
    elif floor:
        sides = {"one_floor_side": floor[0]}
    sides["height"] = d.height_cm
    return {k: plain(v) for k, v in sides.items() if v is not None}


def listed_size(product: GroundedProduct | ProductCandidate) -> dict[str, str]:
    """The piece's listed floor sides and height, as the agents read sizes."""
    return _size(product)


def plain(value: Decimal) -> str:
    """A figure as a person writes it: 220, 92.5 - never 2.2E+2."""
    return format(value.normalize(), "f")


def cart_brief(
    products: Sequence[ProductCandidate],
    state: AgentStateV1,
    rooms: Mapping[int, str] | None = None,
    *,
    arabic: bool = False,
) -> CartBrief:
    """The cart's lines and totals, summed here - one total per currency,
    never converted - so a total said to the customer is a total code made.
    `rooms` names the room each product was bought for, where it was."""
    interaction = state.product_interaction
    by_id = {p.product_id: p for p in products}
    lines: list[CartLine] = []
    totals: dict[str, Decimal] = {}
    for pick, product_id in enumerate(interaction.selected_product_ids, start=1):
        product = by_id.get(product_id)
        if product is None:
            continue
        quantity = interaction.quantity_of(product_id)
        line_total = product.price_amount * quantity
        totals[product.price_unit] = totals.get(product.price_unit, Decimal(0)) + line_total
        lines.append(
            CartLine(
                **brief_product(product, None, arabic=arabic).model_dump(exclude={"card"}),
                pick=pick,
                quantity=quantity,
                line_total=plain(line_total),
                room=(rooms or {}).get(product_id),
            )
        )
    return CartBrief(
        lines=tuple(lines),
        items=sum(line.quantity for line in lines),
        not_priced=(len(interaction.selected_product_ids) - len(lines)) or None,
        totals=tuple(MoneyTotal(currency=c, amount=plain(a)) for c, a in totals.items()),
    )


def cart_facts(products: Sequence[ProductCandidate], state: AgentStateV1) -> dict[str, Any]:
    """The cart as a tool reports it."""
    return cart_brief(products, state).model_dump(mode="json", exclude_none=True)


def search_facts(grounding: SearchExecutionGrounding) -> dict[str, Any]:
    """What a search found, and how far it had to widen to find it."""
    facts: dict[str, Any] = {
        "shown": len(grounding.products),
        "matching_in_store": grounding.eligible_count,
        "exact_matches": grounding.exact_candidate_count,
        # Whether "show me more" would find anything not yet seen: false means
        # these are all of them - said so, never padded with what was not asked.
        "more_after_this_page": grounding.eligible_count > len(grounding.products),
        "products": [product_facts(p, p.presented_ordinal) for p in grounding.products],
    }
    if grounding.was_relaxed:
        facts["widened"] = [
            {
                "what": str(r.field) if r.role is None else f"{r.field}:{r.role}",
                "asked": r.original_value,
                "searched": r.applied_value,
            }
            for r in grounding.relaxations
        ]
    if grounding.set_aside:
        facts["if_set_aside"] = [
            {
                "requirement": str(o.field) if o.role is None else f"{o.field}:{o.role}",
                "would_find": o.eligible_count,
                **({"from_price": o.nearest_price} if o.nearest_price is not None else {}),
            }
            for o in grounding.set_aside
        ]
    if grounding.dropped_constraints:
        facts["sizes_not_applied"] = [str(c) for c in grounding.dropped_constraints]
    if grounding.earlier_sizes_applied:
        facts["earlier_sizes_applied"] = True
    return facts


def design_notes(result: InteriorDesignResult) -> dict[str, Any]:
    """Where the designer would put each piece, and the size the next pieces
    should be - her judgement, for any agent to act on: a size orders a search
    (`size_near`), it never filters."""
    notes: dict[str, Any] = {}
    if result.placements:
        notes["placements"] = [
            p.model_dump(mode="json", exclude_none=True) for p in result.placements
        ]
    if result.next_sizes:
        notes["next_sizes"] = next_size_facts(result.next_sizes)
    return notes


def next_size_facts(sizes: Sequence[NextSize]) -> list[dict[str, Any]]:
    """The designer's sizes for the next pieces, as every agent reads them."""
    return [
        {
            "kind": customer_words(n.commerce_subcategory or n.commerce_category),
            **{
                k: v
                for k, v in n.model_dump(mode="json", exclude_none=True).items()
                if k.endswith("_cm")
            },
            "longer_side_near_cm": n.longer_target_cm(),
            "reason": n.reason,
        }
        for n in sizes
    ]


def searched_for(search: ActiveSearchState | None) -> dict[str, Any]:
    """What the search that ran asked the catalog for - the taste and sizes
    code added included - so an agent sees a colour, a style or a size it
    meant to send and did not, and searches again before showing anything
    (CLAUDE.md 17.1, check before showing)."""
    if search is None:
        return {}
    request = search.request
    wanted = [p for p in search.semantic_preferences if p.canonical_value]
    asked: dict[str, Any] = {
        "kind": customer_words(request.commerce_subcategory or request.commerce_category),
        "colours_required": list(request.colors_any_of),
        "styles_required": list(request.styles_all_of),
        "colours_wanted": [p.canonical_value for p in wanted if p.family is AttributeFamily.COLOR],
        "styles_wanted": [p.canonical_value for p in wanted if p.family is AttributeFamily.STYLE],
    }
    if request.price is not None:
        asked["price"] = request.price.model_dump(mode="json", exclude_none=True)
    if request.seating_capacity is not None:
        asked["seats"] = request.seating_capacity.model_dump(mode="json", exclude_none=True)
    if request.dimensions or request.planar_dimensions is not None:
        asked["sizes"] = True
    if search.semantic_intent:
        asked["words"] = search.semantic_intent
    return {key: value for key, value in asked.items() if value not in ([], None)}
