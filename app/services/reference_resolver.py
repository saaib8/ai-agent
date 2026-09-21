"""Turning "the second one" into a product, deterministically.

This is where a model's safe selector becomes an authoritative id, and the
whole authority argument rests on it resolving against state the application
wrote itself. The model says *how to find* a product; nothing it emits is
trusted to identify one (CLAUDE.md 3.3, 20.2).

Three rules decide every outcome here.

**Never guess.** A selector that matches two products, or none, is a question
for the customer. Taking the first match would answer confidently and
sometimes wrongly, which is worse than asking.

**Facts are read fresh.** A remembered price is a wrong price, so attribute and
extremum selectors hydrate the presented set from PostgreSQL rather than
trusting anything the conversation carries.

**Scope is structural.** Every lookup goes through the repository with the
request's `RetailerContext`, so a product another retailer owns simply is not
there - indistinguishable from deleted, deliberately, so a refusal reveals
nothing about another store's catalog.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.repositories.products import ProductRepository
from app.schemas.agent_decision import (
    ExtremumDirection,
    FocusedProduct,
    PresentedAttributeMatch,
    PresentedExtremum,
    PresentedOrdinal,
    ProductReferenceSelector,
    SoleSelectedProduct,
)
from app.schemas.agent_state import AgentStateV1
from app.schemas.product import ProductRow
from app.schemas.resolution import (
    ReferenceFailureReason,
    ReferenceOutcome,
    ReferenceUnresolved,
    ResolvedProductReference,
)
from app.schemas.retailer import RetailerContext
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes

logger = get_logger(__name__)


def _unresolved(reason: ReferenceFailureReason) -> ReferenceUnresolved:
    return ReferenceUnresolved(reason=reason)


class ProductReferenceResolver:
    """Selector plus state plus catalog -> exactly one product, or a reason."""

    def __init__(
        self, repository: ProductRepository, attributes: CatalogAttributes
    ) -> None:
        self._repository = repository
        self._attributes = attributes

    async def resolve(
        self,
        selector: ProductReferenceSelector,
        state: AgentStateV1,
        context: RetailerContext,
    ) -> ReferenceOutcome:
        outcome = await self._dispatch(selector, state, context)
        logger.info(
            "product_reference_resolved",
            store_id=context.store_id,
            selector=type(selector).__name__,
            resolved=isinstance(outcome, ResolvedProductReference),
            reason=(
                str(outcome.reason)
                if isinstance(outcome, ReferenceUnresolved)
                else None
            ),
        )
        return outcome

    async def _dispatch(
        self,
        selector: ProductReferenceSelector,
        state: AgentStateV1,
        context: RetailerContext,
    ) -> ReferenceOutcome:
        match selector:
            case PresentedOrdinal():
                return await self._by_ordinal(selector, state, context)
            case FocusedProduct():
                return await self._focused(state, context)
            case SoleSelectedProduct():
                return await self._sole_selected(state, context)
            case PresentedAttributeMatch():
                return await self._by_attribute(selector, state, context)
            case PresentedExtremum():
                return await self._by_extremum(selector, state, context)

    # ── position ────────────────────────────────────────────────────────────

    async def _by_ordinal(
        self,
        selector: PresentedOrdinal,
        state: AgentStateV1,
        context: RetailerContext,
    ) -> ReferenceOutcome:
        presented = _presented(state)
        if presented is None:
            return _unresolved(ReferenceFailureReason.NO_PRESENTED_RESULTS)
        if selector.position > len(presented):
            return _unresolved(ReferenceFailureReason.ORDINAL_OUT_OF_RANGE)
        # Their counting, not ours: position one is the first thing shown.
        return await self._verify(presented[selector.position - 1], context)

    # ── conversational memory ───────────────────────────────────────────────

    async def _focused(
        self, state: AgentStateV1, context: RetailerContext
    ) -> ReferenceOutcome:
        """Focus outlives a result set, so this needs no presentation lineage.

        M10 already constrains a focus to a presented or selected product, so
        there is no lineage to re-check here - only whether the product is
        still there.
        """
        focused = state.product_interaction.focused_product_id
        if focused is None:
            return _unresolved(ReferenceFailureReason.NO_FOCUSED_PRODUCT)
        return await self._verify(focused, context)

    async def _sole_selected(
        self, state: AgentStateV1, context: RetailerContext
    ) -> ReferenceOutcome:
        """A selection may outlive the search that presented it."""
        selected = state.product_interaction.selected_product_ids
        if not selected:
            return _unresolved(ReferenceFailureReason.NO_SELECTED_PRODUCT)
        if len(selected) > 1:
            return _unresolved(ReferenceFailureReason.SEVERAL_SELECTED_PRODUCTS)
        return await self._verify(selected[0], context)

    # ── what they can see ───────────────────────────────────────────────────

    async def _by_attribute(
        self,
        selector: PresentedAttributeMatch,
        state: AgentStateV1,
        context: RetailerContext,
    ) -> ReferenceOutcome:
        """"The beige one" - matched against fresh facts, not remembered ones."""
        if not self._attributes.is_value(selector.family, selector.value):
            return _unresolved(ReferenceFailureReason.UNAPPROVED_ATTRIBUTE_VALUE)
        rows = await self._presented_rows(state, context)
        if isinstance(rows, ReferenceUnresolved):
            return rows

        matches = [row for row in rows if _has_attribute(row, selector)]
        if not matches:
            return _unresolved(ReferenceFailureReason.NO_ATTRIBUTE_MATCH)
        if len(matches) > 1:
            return _unresolved(ReferenceFailureReason.SEVERAL_ATTRIBUTE_MATCHES)
        return ResolvedProductReference(product_id=matches[0].id)

    async def _by_extremum(
        self,
        selector: PresentedExtremum,
        state: AgentStateV1,
        context: RetailerContext,
    ) -> ReferenceOutcome:
        """"The cheaper one" - an extreme of a verified fact, never a judgement."""
        rows = await self._presented_rows(state, context)
        if isinstance(rows, ReferenceUnresolved):
            return rows

        if len({row.price_unit for row in rows}) > 1:
            # Nothing converts between currencies, so there is no cheapest.
            return _unresolved(ReferenceFailureReason.MIXED_CURRENCY_PRESENTATION)

        prices = [row.price_amount for row in rows]
        target = (
            min(prices)
            if selector.direction is ExtremumDirection.LOWEST
            else max(prices)
        )
        at_extreme = [row for row in rows if row.price_amount == target]
        if len(at_extreme) > 1:
            # Two products share it; picking the earlier one is a coin toss.
            return _unresolved(ReferenceFailureReason.TIED_EXTREMUM)
        return ResolvedProductReference(product_id=at_extreme[0].id)

    # ── shared catalog reads ────────────────────────────────────────────────

    async def _presented_rows(
        self, state: AgentStateV1, context: RetailerContext
    ) -> list[ProductRow] | ReferenceUnresolved:
        """Every product the customer was shown, read fresh, or nothing.

        Partial is refused. Their phrase refers to the list in front of them,
        and interpreting it against a shorter one could land on a different
        product than they meant.
        """
        presented = _presented(state)
        if presented is None:
            return _unresolved(ReferenceFailureReason.NO_PRESENTED_RESULTS)
        rows = await self._repository.get_by_ids(list(presented), context)
        if len(rows) != len(presented):
            logger.warning(
                "presented_set_incomplete",
                store_id=context.store_id,
                presented_count=len(presented),
                readable_count=len(rows),
            )
            return _unresolved(ReferenceFailureReason.PRESENTED_SET_INCOMPLETE)
        # Presentation order, which `get_by_ids` does not preserve.
        by_id = {row.id: row for row in rows}
        return [by_id[product_id] for product_id in presented]

    async def _verify(
        self, product_id: int, context: RetailerContext
    ) -> ReferenceOutcome:
        """A remembered id is a claim; the catalog decides whether it holds.

        Scoped by construction: the repository only returns products this
        store owns, so another retailer's id resolves to nothing rather than
        to a product.
        """
        rows = await self._repository.get_by_ids([product_id], context)
        if not rows:
            return _unresolved(ReferenceFailureReason.PRODUCT_UNAVAILABLE)
        return ResolvedProductReference(product_id=product_id)


def _presented(state: AgentStateV1) -> tuple[int, ...] | None:
    """The committed presented list, or None when there is not one.

    A null `presented_search_revision` means no result set has been committed.
    M10 already guarantees that a non-null one matches the active search, so
    there is no second lineage check to make here - and no new lineage field
    to invent.
    """
    interaction = state.product_interaction
    if interaction.presented_search_revision is None:
        return None
    return interaction.presented_product_ids or None


def _has_attribute(row: ProductRow, selector: PresentedAttributeMatch) -> bool:
    if selector.family is AttributeFamily.COLOR:
        return row.main_color == selector.value
    return selector.value in row.styles

