"""Picking, unpicking and focusing a product, shared by typing and tapping.

A typed "I'll take the second one" and a tick on the second card record the
same thing, so they go through the same update here (CLAUDE.md 17.1). A copy
in each path is how "show me more" once re-ran nothing when typed while the
button worked (issue 7).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.schemas.agent_decision import ProductInteractionOp
from app.schemas.agent_state import AgentStateV1
from app.schemas.agent_updates import AddItems, ProductInteractionUpdate, RemoveItems
from app.schemas.picks import LikedView, PickPosition, PickView
from app.schemas.product import ProductCandidate
from app.taxonomy.words import customer_words_or_none


def interaction_update(
    op: ProductInteractionOp, product_id: int, state: AgentStateV1
) -> ProductInteractionUpdate:
    """One atomic update per interaction.

    Deselecting needs care. If the product is focused and is *not* also
    presented, then removing the selection removes the only thing making the
    focus resolvable - so the focus must go in the same update. Doing it in two
    steps would build an intermediate state the reducer rejects, and clearing
    the focus unconditionally would drop a focus that was still perfectly valid
    because the product is still on screen.
    """
    interaction = state.product_interaction
    match op:
        case ProductInteractionOp.SELECT:
            # The piece they just chose becomes the one under discussion.
            # Without this, "show me the one I selected" kept resolving through
            # a focus set turns earlier - so a customer who corrected their
            # choice was shown the product they had just rejected (M15 3).
            return ProductInteractionUpdate(
                selected_product_ids=AddItems(items=(product_id,)),
                focused_product_id=product_id,
            )
        case ProductInteractionOp.FOCUS:
            return ProductInteractionUpdate(focused_product_id=product_id)
        case ProductInteractionOp.DESELECT:
            orphans_focus = (
                interaction.focused_product_id == product_id
                and product_id not in interaction.presented_product_ids
            )
            return ProductInteractionUpdate(
                selected_product_ids=RemoveItems(items=(product_id,)),
                clear_focus=orphans_focus,
            )


def build_picks(state: AgentStateV1, products: Sequence[ProductCandidate]) -> tuple[PickView, ...]:
    """The picks as the tray draws them, from freshly read products.

    Numbered by position in the picks, never by position among what could be
    read: a pick the catalog no longer returns is simply absent, and the rest
    keep their numbers, so "pick 2" still means the product picked second.
    """
    interaction = state.product_interaction
    by_id = {product.product_id: product for product in products}
    lists = _lists_on_screen(state)
    on_screen = (
        {
            product_id: ordinal
            for ordinal, product_id in enumerate(interaction.presented_product_ids, start=1)
        }
        if interaction.presented_search_revision is not None
        else {}
    )
    picks: list[PickView] = []
    for position, product_id in enumerate(interaction.selected_product_ids, start=1):
        product = by_id.get(product_id)
        if product is None:
            continue
        picks.append(
            PickView(
                pick=position,
                name_english=product.name_english,
                image_url=product.image_url,
                price_amount=product.price_amount,
                price_unit=product.price_unit,
                kind=customer_words_or_none(
                    product.commerce.subcategory or product.commerce.category
                ),
                presented_ordinal=on_screen.get(product_id),
                positions=_positions(product_id, lists),
                focused=interaction.focused_product_id == product_id,
            )
        )
    return tuple(picks)


def build_liked(state: AgentStateV1, products: Sequence[ProductCandidate]) -> tuple[LikedView, ...]:
    """The liked list as the tray draws it, numbered like the picks: a like
    the catalog no longer returns is absent and the rest keep their numbers."""
    interaction = state.product_interaction
    by_id = {product.product_id: product for product in products}
    lists = _lists_on_screen(state)
    liked: list[LikedView] = []
    for position, product_id in enumerate(interaction.liked_product_ids, start=1):
        product = by_id.get(product_id)
        if product is None:
            continue
        liked.append(
            LikedView(
                liked=position,
                name_english=product.name_english,
                image_url=product.image_url,
                price_amount=product.price_amount,
                price_unit=product.price_unit,
                kind=customer_words_or_none(
                    product.commerce.subcategory or product.commerce.category
                ),
                positions=_positions(product_id, lists),
                picked=product_id in interaction.selected_product_ids,
            )
        )
    return tuple(liked)


def _lists_on_screen(state: AgentStateV1) -> list[tuple[int, tuple[int, ...]]]:
    interaction = state.product_interaction
    lists = [(entry.revision, entry.product_ids) for entry in interaction.earlier_lists]
    if interaction.presented_search_revision is not None:
        lists.append((interaction.presented_search_revision, interaction.presented_product_ids))
    return lists


def _positions(
    product_id: int, lists: Sequence[tuple[int, tuple[int, ...]]]
) -> tuple[PickPosition, ...]:
    return tuple(
        PickPosition(list_revision=revision, ordinal=ordinal)
        for revision, ids in lists
        for ordinal, listed in enumerate(ids, start=1)
        if listed == product_id
    )
