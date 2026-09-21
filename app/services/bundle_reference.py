"""Turning "the second one" into the lines it actually means.

Deterministic, and deliberately unwilling to guess. Two chairs and "the chair"
is a question, not a coin toss: resolution succeeds only when exactly one
visible card matches, and every other outcome is a typed reason for the
conversational layer to settle.

It resolves against **cards**, not raw state lines, because cards are what the
customer saw. A room of four lines rendered as three cards has a third piece
and no fourth, and counting the lines instead would silently point somewhere
else.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.exceptions import TaxonomyValidationError
from app.schemas.agent_state import RoomDesignNeedState, RoomProjectState
from app.schemas.bundle_reference import (
    BundleCategoryMatch,
    BundleItemOrdinal,
    BundleReferenceSelector,
    DesignNeedCategoryMatch,
)
from app.schemas.design import ExcludedDesignRole
from app.schemas.product import ProductCandidate
from app.schemas.resolution import (
    BundleReferenceFailureReason,
    BundleReferenceOutcome,
    BundleReferenceUnresolved,
    DesignNeedFailureReason,
    DesignNeedOutcome,
    DesignNeedUnresolved,
    ResolvedBundleReference,
    ResolvedDesignNeed,
)
from app.services.bundle_cards import BundleCardGroup, group_bundle_cards
from app.services.design_revision import deduplicated_exclusions
from app.taxonomy.registry import CommerceTaxonomy


class BundleReferenceResolver:
    """A selector and a room in, one card or a reason out."""

    def __init__(self, taxonomy: CommerceTaxonomy) -> None:
        self._taxonomy = taxonomy

    def resolve(
        self,
        selector: BundleReferenceSelector,
        room: RoomProjectState | None,
        products: Sequence[ProductCandidate],
    ) -> BundleReferenceOutcome:
        """Which card the customer meant, against the room as it stands.

        `products` are the freshly hydrated facts for the room's current
        products, used only to answer what type a card is - a category match
        needs the catalog's classification, and the remembered one would be a
        guess about a product that may have been reclassified.

        **Membership and order come from state alone.** A product that could
        not be re-read does not remove its card, shift an ordinal or reorder
        anything: the customer was shown three pieces, and the third is still
        the third whatever the catalog can currently tell us about the second.
        Hydration supplies facts, never the shape of the list.
        """
        if room is None or not room.bundle_items:
            return BundleReferenceUnresolved(
                reason=BundleReferenceFailureReason.NO_BUNDLE
            )

        cards = group_bundle_cards(room.bundle_items)
        by_id = {product.product_id: product for product in products}

        match selector:
            case BundleItemOrdinal():
                return self._by_ordinal(selector, cards, by_id, room.bundle_revision)
            case BundleCategoryMatch():
                return self._by_category(selector, cards, by_id, room.bundle_revision)

    def resolve_need(
        self, selector: DesignNeedCategoryMatch, room: RoomProjectState | None
    ) -> DesignNeedOutcome:
        """Which role in the plan they meant.

        Against the durable plan rather than the visible cards, because a role
        need not be filled: a room may want a lamp it never found one for, and
        "take the lamp out" has to reach a need with no product behind it.
        """
        needs = room.design_needs if room else ()
        if not needs:
            return DesignNeedUnresolved(reason=DesignNeedFailureReason.NO_PLAN)

        try:
            if selector.commerce_subcategory is None:
                if not self._taxonomy.is_category(selector.commerce_category):
                    raise TaxonomyValidationError(detail="unknown commerce category")
            else:
                self._taxonomy.validate_pair(
                    selector.commerce_category, selector.commerce_subcategory
                )
        except TaxonomyValidationError:
            return DesignNeedUnresolved(
                reason=DesignNeedFailureReason.UNAPPROVED_COMMERCE_TYPE
            )

        matches = [
            need
            for need in needs
            if need.commerce_category == selector.commerce_category
            and (
                selector.commerce_subcategory is None
                or need.commerce_subcategory == selector.commerce_subcategory
            )
        ]
        if not matches:
            return DesignNeedUnresolved(reason=DesignNeedFailureReason.NO_NEED_MATCH)
        if len(matches) > 1:
            return DesignNeedUnresolved(
                reason=DesignNeedFailureReason.SEVERAL_NEED_MATCHES
            )
        return ResolvedDesignNeed(need_id=matches[0].need_id)

    def resolve_exclusions(
        self,
        selectors: Sequence[DesignNeedCategoryMatch],
        room: RoomProjectState | None,
    ) -> tuple[ExcludedDesignRole, ...] | DesignNeedUnresolved:
        """Roles the revised plan must not contain, checked against the current one.

        **Deliberately not `resolve_need`'s rule.** That one deletes a single
        durable need and so must identify exactly one; this is a predicate on
        the plan about to be written - *no dining chairs in the new room* - and
        two current dining-chair needs make it doubly applicable rather than
        ambiguous. Inheriting the exactly-one rule would refuse a perfectly
        clear instruction precisely when the customer has most of the thing
        they want gone (M12E-4D 11).

        Each selector must still name at least one current role: excluding
        something the plan never had means the model misread the room, and
        passing it on would narrow a redesign for no reason.
        """
        if not selectors:
            return ()

        needs = room.design_needs if room else ()
        if not needs:
            return DesignNeedUnresolved(reason=DesignNeedFailureReason.NO_PLAN)

        resolved: list[ExcludedDesignRole] = []
        for selector in selectors:
            try:
                self._validate_role(selector)
            except TaxonomyValidationError:
                return DesignNeedUnresolved(
                    reason=DesignNeedFailureReason.UNAPPROVED_COMMERCE_TYPE
                )
            if not any(_role_matches(selector, need) for need in needs):
                return DesignNeedUnresolved(
                    reason=DesignNeedFailureReason.NO_NEED_MATCH
                )
            resolved.append(
                ExcludedDesignRole(
                    commerce_category=selector.commerce_category,
                    commerce_subcategory=selector.commerce_subcategory,
                )
            )
        return deduplicated_exclusions(resolved)

    def _validate_role(self, selector: DesignNeedCategoryMatch) -> None:
        """The approved vocabulary, exactly as every other selector checks it."""
        if selector.commerce_subcategory is None:
            if not self._taxonomy.is_category(selector.commerce_category):
                raise TaxonomyValidationError(detail="unknown commerce category")
            return
        self._taxonomy.validate_pair(
            selector.commerce_category, selector.commerce_subcategory
        )

    # ── the two ways of naming one ──────────────────────────────────────────

    @staticmethod
    def _by_ordinal(
        selector: BundleItemOrdinal,
        cards: Sequence[BundleCardGroup],
        by_id: dict[int, ProductCandidate],
        revision: int,
    ) -> BundleReferenceOutcome:
        """Position first, freshness second - and only the target's.

        Identity is settled against the canonical state list, so an unrelated
        piece the catalog cannot currently return never shifts which card
        position two means. Only then is the chosen card required to be
        readable: if their own piece is gone, that is a fact about it, and no
        other card's staleness is relevant to it either.
        """
        if selector.ordinal > len(cards):
            return BundleReferenceUnresolved(
                reason=BundleReferenceFailureReason.ORDINAL_OUT_OF_RANGE
            )
        card = cards[selector.ordinal - 1]
        if card.product_id not in by_id:
            return BundleReferenceUnresolved(
                reason=BundleReferenceFailureReason.TARGET_PRODUCT_UNAVAILABLE
            )
        return _resolved(card, selector.ordinal, revision)

    def _by_category(
        self,
        selector: BundleCategoryMatch,
        cards: Sequence[BundleCardGroup],
        by_id: dict[int, ProductCandidate],
        revision: int,
    ) -> BundleReferenceOutcome:
        """One card of a named type, or a question.

        The type is validated against the registry first. An unapproved value
        names nothing in this catalog, and matching it to whatever looks
        closest is exactly the guessing the taxonomy exists to prevent
        (CLAUDE.md 14.3).
        """
        try:
            if selector.commerce_subcategory is None:
                if not self._taxonomy.is_category(selector.commerce_category):
                    raise TaxonomyValidationError(detail="unknown commerce category")
            else:
                self._taxonomy.validate_pair(
                    selector.commerce_category, selector.commerce_subcategory
                )
        except TaxonomyValidationError:
            return BundleReferenceUnresolved(
                reason=BundleReferenceFailureReason.UNAPPROVED_COMMERCE_TYPE
            )

        if any(card.product_id not in by_id for card in cards):
            # Uniqueness cannot be proven over a room we cannot fully read. The
            # unreadable piece might have been of the kind they described, and
            # assuming otherwise would silently turn a question into a guess.
            return BundleReferenceUnresolved(
                reason=BundleReferenceFailureReason.BUNDLE_NOT_VERIFIABLE
            )

        matches = [
            (position, card)
            for position, card in enumerate(cards, start=1)
            if _is_a(by_id[card.product_id], selector)
        ]
        if not matches:
            return BundleReferenceUnresolved(
                reason=BundleReferenceFailureReason.NO_CATEGORY_MATCH
            )
        if len(matches) > 1:
            return BundleReferenceUnresolved(
                reason=BundleReferenceFailureReason.SEVERAL_CATEGORY_MATCHES
            )
        position, card = matches[0]
        return _resolved(card, position, revision)


def _is_a(product: ProductCandidate, selector: BundleCategoryMatch) -> bool:
    """Whether a card is of the named type, by the catalog's own classification."""
    commerce = product.commerce
    if commerce.category != selector.commerce_category:
        return False
    if selector.commerce_subcategory is None:
        return True
    return commerce.subcategory == selector.commerce_subcategory


def _resolved(
    card: BundleCardGroup, ordinal: int, revision: int
) -> ResolvedBundleReference:
    return ResolvedBundleReference(
        line_ids=card.line_ids,
        need_ids=card.need_ids,
        product_id=card.product_id,
        quantity=card.quantity,
        acquisition=card.acquisition,
        locked=card.locked,
        ordinal=ordinal,
        bundle_revision=revision,
    )


def _role_matches(
    selector: DesignNeedCategoryMatch, need: RoomDesignNeedState
) -> bool:
    """Every current need of that kind, not the first one found."""
    if need.commerce_category != selector.commerce_category:
        return False
    return (
        selector.commerce_subcategory is None
        or need.commerce_subcategory == selector.commerce_subcategory
    )
