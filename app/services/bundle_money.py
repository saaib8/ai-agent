"""New spend, computed once for however many ways a room is rendered.

A room reaches the customer by two routes. One follows an optimisation, and its
figures come from the outcome that chose the package. The other rebuilds the
room from durable state after a local change - locking a piece, say - where no
optimiser ran and the arithmetic has to be done here.

Both must answer identically, so both answer through this. A second summation
somewhere else would eventually disagree about a mixed-currency room or about
what an already-owned piece contributes, and the customer would see two
different totals for the same room.

The rules are M12D's, unchanged: only what is being bought is summed, a total
exists only when every summed line shares one exact unit, nothing converts, and
an already-owned piece contributes nothing rather than zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.schemas.acquisition import BundleAcquisition
from app.schemas.bundle import TotalUnavailableReason


@dataclass(frozen=True, slots=True)
class SpendLine:
    """One line's contribution to new spend, however it was arrived at."""

    unit_price: Decimal
    price_unit: str
    quantity: int
    acquisition: BundleAcquisition

    @property
    def is_purchase(self) -> bool:
        return self.acquisition is BundleAcquisition.TO_BUY

    @property
    def line_total(self) -> Decimal | None:
        """What this line adds to the bill, or None when it adds nothing.

        None rather than zero for a piece the customer already owns: zero reads
        as a price, and the product does not cost nothing.
        """
        return self.unit_price * self.quantity if self.is_purchase else None


def new_spend(
    lines: Sequence[SpendLine], *, fallback_currency: str | None = None
) -> tuple[Decimal | None, str | None, TotalUnavailableReason | None]:
    """The total, its unit, and why there is none when there is none.

    `fallback_currency` names the unit to express zero in when nothing is being
    bought - a budget's currency, where one was given. Without it, zero of
    nothing is not a total and says so.
    """
    spend = [line for line in lines if line.is_purchase]
    units = {line.price_unit for line in spend}
    if len(units) > 1:
        return None, None, TotalUnavailableReason.MIXED_PRICE_UNITS
    if not spend:
        if fallback_currency is None:
            return None, None, TotalUnavailableReason.NO_PRICED_LINES
        return Decimal(0), fallback_currency, None
    total = sum((line.unit_price * line.quantity for line in spend), Decimal(0))
    return total, units.pop(), None
