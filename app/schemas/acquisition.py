"""Whether a bundle line is money still to spend.

A leaf module with no imports, and that is the point. The concept is shared by
two layers that cannot import each other: the optimiser's contracts
(`app/schemas/bundle.py`) and durable room state (`app/schemas/agent_state.py`).
State reaching for the bundle module would close the cycle
`bundle -> design_discovery -> resolution -> agent_decision -> agent_state`, and
defining the enum twice would let the two copies drift.
"""

from __future__ import annotations

from enum import StrEnum


class BundleAcquisition(StrEnum):
    """Whether a bundle line is money still to spend."""

    TO_BUY = "to_buy"
    """Counts toward the room's budget at its current catalog price."""

    ALREADY_OWNED = "already_owned"
    """The customer has it. Zero new spend, and its price is irrelevant to the
    arithmetic - which is why an unusable price on one of these is tolerated
    while the same price on a `TO_BUY` line is not.

    Never inferred. Not from a lock, not from a category, not from a price, not
    from a product name: a lock proves preservation and says nothing about
    purchase, so the value is always supplied by the layer that heard the
    customer say it.
    """
