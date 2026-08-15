"""When a quotation's freight configuration needs saying out loud.

Three figures are easy to conflate and this module keeps them apart:

**Recorded freight**
    ``QuotationShipment.total_freight`` — the sum of the container rows. It
    exists as soon as a container is given a cost, whatever happens next.

**Billable freight**
    The single derived ``QuotationCharge`` with ``source='shipment'``, which
    ``shipping_service.sync_freight`` creates only under
    ``ADDED_SEPARATELY``. Under the other two methods there is no charge, so
    billable freight is zero however much was recorded.

**Freight in the grand total**
    What the customer pays. Equal to billable freight, because every charge
    counts toward the total.

The two configurations below are the ones where recorded and billable freight
disagree in a way nobody chose on purpose, or where the customer is charged for
something the document does not mention. Both are legal, neither is a defect,
and both are invisible unless something says so — which is what this is for.

The internal FOB allocation (``settings_service.total_fob_cost``, $700 a
container) is none of these three. It is an input to the selling price and
never becomes a charge, so it is deliberately absent from every figure here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.constants import CHARGE_SOURCE_SHIPMENT, FreightMethod
from modules.models import Quotation
from modules.utilities import format_money

#: Recorded, but the method means none of it is billed.
RECORDED_NOT_BILLED = "FREIGHT_RECORDED_NOT_BILLED"
#: Billed, but the document does not show the shipping it is for.
BILLED_BUT_HIDDEN = "FREIGHT_BILLED_BUT_HIDDEN"


@dataclass(frozen=True)
class FreightWarning:
    code: str
    message: str


def recorded_freight(quotation: Quotation) -> Decimal:
    """What the container rows come to, billed or not."""
    shipment = quotation.shipment
    return shipment.total_freight if shipment is not None else Decimal("0.00")


def billable_freight(quotation: Quotation) -> Decimal:
    """What the customer is actually charged for freight."""
    return sum(
        (c.amount for c in quotation.charges if c.source == CHARGE_SOURCE_SHIPMENT),
        Decimal("0.00"),
    )


def warnings_for(session: Session, quotation: Quotation) -> list[FreightWarning]:
    """Every freight configuration on this quotation worth stating.

    ``session`` is unused today and taken anyway, so a rule that needs to read
    settings or capacity later does not change every caller.
    """
    del session  # documented above

    shipment = quotation.shipment
    if shipment is None:
        return []

    found: list[FreightWarning] = []
    recorded = recorded_freight(quotation)

    if recorded > 0 and shipment.freight_method is FreightMethod.INCLUDED:
        found.append(
            FreightWarning(
                RECORDED_NOT_BILLED,
                f"Freight of "
                f"{format_money(recorded, shipment.freight_currency)} is "
                f"recorded but will not be added to the customer total "
                f"because the freight method is set to Included.",
            )
        )

    if (
        shipment.freight_method is FreightMethod.ADDED_SEPARATELY
        and recorded > 0
        and not shipment.show_on_document
    ):
        found.append(
            FreightWarning(
                BILLED_BUT_HIDDEN,
                "Freight is included in the customer total but the shipping "
                "details are hidden from the document.",
            )
        )

    return found
