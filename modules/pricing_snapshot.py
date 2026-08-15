"""One pricing path, in Decimal, for every surface that shows money.

The employee screens, the customer portal and the PDF must never disagree about
what a quotation is worth, so none of them does arithmetic. They all call
:func:`price` and read the same typed snapshot.

Three scopes, because a quotation with optional lines has three legitimate
totals and calling any of them simply "the total" is how they get confused:

``BASE``
    INCLUDED lines only. The minimum offer — what the customer owes if they
    select nothing. This is what the stored columns on ``quotations`` hold.

``ALL_OPTIONS``
    Every line, including OPTIONAL and RECOMMENDED. The ceiling, and only ever
    shown labelled as such. Never stored: it is derivable, and a stored copy is
    one more thing to drift.

``SELECTED``
    INCLUDED plus exactly the lines a customer chose. What an acceptance is
    recorded at, snapshotted onto ``portal_responses`` so a later revision can
    never restate what somebody agreed to.

RECOMMENDED is selectable, not included. It may be highlighted on the customer
page, but it does not touch the base total until the customer picks it —
otherwise "recommended" would be a way of quietly raising the price.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from modules.calculation_engine import (
    ChargeInput,
    LineInput,
    LineResult,
    QuotationTotals,
    compute_line,
    compute_totals,
    q_money,
)
from modules.constants import SELECTABLE_INCLUSIONS, ItemInclusion
from modules.models import PortalResponse, Quotation, QuotationItem

ZERO = Decimal("0")

#: The three figures, and the only wording any surface may use for them. A
#: quotation with optional lines has three legitimate totals, and the whole
#: point of separating them is lost if one of them is ever labelled "Total".
BASE_LABEL = "Base Total"
ALL_OPTIONS_LABEL = "Total with All Options"
ACCEPTED_LABEL = "Accepted Total"

BASE_HELP = "Included items only. What the customer owes if they select nothing."
ALL_OPTIONS_HELP = (
    "Included items plus every optional and recommended item. Derived on "
    "demand, never stored, and never what the customer is being offered at."
)
ACCEPTED_HELP = (
    "What the customer actually accepted: included items plus exactly the "
    "options they chose. Recorded at acceptance and never recalculated."
)


class PriceScope(StrEnum):
    BASE = "BASE"
    ALL_OPTIONS = "ALL_OPTIONS"
    SELECTED = "SELECTED"


@dataclass(frozen=True)
class PricedLine:
    """One line, priced. Costs are deliberately absent from this shape."""

    item_id: int
    inclusion: ItemInclusion
    is_selected: bool
    quantity_packs: Decimal
    quantity_pieces: Decimal
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class PricingSnapshot:
    """Every figure a surface needs, as Decimal. Formatting happens elsewhere."""

    scope: PriceScope
    currency: str
    lines: tuple[PricedLine, ...]
    subtotal: Decimal
    discount: Decimal
    charges_total: Decimal
    charges_customer_visible: Decimal
    taxable_base: Decimal
    tax_rate_pct: Decimal
    tax_amount: Decimal
    grand_total: Decimal
    deposit_pct: Decimal
    deposit_due: Decimal
    #: What the unselected selectable lines would add to the subtotal.
    optional_available: Decimal = ZERO
    selected_item_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def counted_lines(self) -> tuple[PricedLine, ...]:
        return tuple(ln for ln in self.lines if ln.is_selected)


def _line_input(item: QuotationItem) -> LineInput:
    return LineInput(
        quantity_packs=item.quantity_packs,
        quantity_pieces=item.quantity_pieces,
        case_pack=item.case_pack or 1,
        price_per_pack=item.price_per_pack,
        price_per_piece=item.price_per_piece,
        pricing_basis=item.pricing_basis,
        line_discount_pct=item.line_discount_pct,
        line_discount_amount=(
            item.line_discount_amount
            if item.line_discount_amount and item.line_discount_pct == ZERO
            else None
        ),
    )


def counts_toward(
    item: QuotationItem, scope: PriceScope, chosen: set[int]
) -> bool:
    """Whether this line contributes money under the given scope."""
    if item.inclusion is ItemInclusion.INCLUDED:
        return True
    if scope is PriceScope.ALL_OPTIONS:
        return True
    if scope is PriceScope.SELECTED:
        return item.id in chosen
    return False        # BASE: selectable lines cost nothing until chosen


def normalise_selection(
    quotation: Quotation, selected_ids: list[int] | None
) -> list[int]:
    """Reduce whatever a caller passed to ids that are selectable *here*.

    Unknown, non-numeric, duplicated, already-included and foreign ids are
    dropped rather than rejected: a stale form should reprice, not error.
    """
    if not selected_ids:
        return []
    allowed = {
        i.id for i in quotation.items if i.inclusion in SELECTABLE_INCLUSIONS
    }
    seen: set[int] = set()
    ordered: list[int] = []
    for raw in selected_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value in allowed and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def price(
    quotation: Quotation,
    scope: PriceScope = PriceScope.BASE,
    selected_ids: list[int] | None = None,
) -> PricingSnapshot:
    """Price a quotation under one scope. The single arithmetic entry point.

    Reads every figure from the stored line, never from a caller's input: a
    selection says *which* lines count, never what they cost.
    """
    chosen = set(normalise_selection(quotation, selected_ids))
    items = sorted(quotation.items, key=lambda i: (i.sort_order, i.line_no))

    counted: list[LineResult] = []
    priced: list[PricedLine] = []
    optional_available = ZERO

    for item in items:
        result = compute_line(_line_input(item))
        included = counts_toward(item, scope, chosen)
        if included:
            counted.append(result)
        elif item.inclusion in SELECTABLE_INCLUSIONS:
            optional_available += result.net_line_total

        priced.append(
            PricedLine(
                item_id=item.id,
                inclusion=item.inclusion,
                is_selected=included,
                quantity_packs=result.quantity_packs,
                quantity_pieces=result.quantity_pieces,
                unit_price=(
                    result.price_per_piece
                    if item.pricing_basis.value == "PIECE" else result.price_per_pack
                ),
                line_total=result.net_line_total,
            )
        )

    charges = [
        ChargeInput(
            quantity=c.quantity_value, rate=c.rate, exchange_rate=c.exchange_rate,
            is_taxable=c.is_taxable, is_customer_visible=c.is_customer_visible,
        )
        for c in sorted(quotation.charges, key=lambda c: c.sort_order)
    ]

    totals: QuotationTotals = compute_totals(
        counted,
        charges=charges,
        quotation_discount_pct=quotation.quote_discount_pct or ZERO,
        quotation_discount_amount=quotation.quote_discount_amount or None,
        tax_rate_pct=quotation.tax_rate_pct or ZERO,
    )

    from modules.calculation_engine import deposit_amount

    deposit_pct = quotation.deposit_pct or ZERO
    deposit = deposit_amount(totals.grand_total, deposit_pct)

    return PricingSnapshot(
        scope=scope,
        currency=quotation.currency,
        lines=tuple(priced),
        subtotal=totals.subtotal,
        discount=totals.quotation_discount,
        charges_total=totals.charges_total,
        charges_customer_visible=totals.charges_customer_visible,
        taxable_base=totals.taxable_base,
        tax_rate_pct=quotation.tax_rate_pct or ZERO,
        tax_amount=totals.tax_amount,
        grand_total=totals.grand_total,
        deposit_pct=deposit_pct,
        deposit_due=deposit,
        optional_available=q_money(optional_available),
        selected_item_ids=tuple(sorted(chosen)),
    )


def base(quotation: Quotation) -> PricingSnapshot:
    """The minimum offer: INCLUDED lines only."""
    return price(quotation, PriceScope.BASE)


def all_options(quotation: Quotation) -> PricingSnapshot:
    """The ceiling: every selectable line taken. Always label it as such."""
    return price(quotation, PriceScope.ALL_OPTIONS)


def selected(quotation: Quotation, selected_ids: list[int] | None) -> PricingSnapshot:
    """What a customer's current choices come to."""
    return price(quotation, PriceScope.SELECTED, selected_ids)


# --------------------------------------------------------------------------- #
# What an employee screen shows
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TotalsRow:
    """One labelled figure, ready to render. Money is still Decimal."""

    label: str
    amount: Decimal
    currency: str
    help_text: str
    #: The figure the eye should land on. Exactly one row is primary.
    is_primary: bool = False


def totals_summary(
    quotation: Quotation,
    accepted_response: PortalResponse | None = None,
) -> tuple[TotalsRow, ...]:
    """The figures an employee screen shows for one quotation.

    Lives here rather than in a page so the labels are one string each and can
    be asserted. A page that built its own would eventually call one of these
    "Total", which is the confusion the three scopes exist to prevent.

    Before acceptance the base total leads, because that is the offer. After
    acceptance the accepted total leads and the other two stay visible as
    context — an employee still needs to see what was left on the table, but
    not in a position where it could be read as the amount owed.
    """
    base_snapshot = base(quotation)
    ceiling = all_options(quotation)

    rows = [
        TotalsRow(
            label=BASE_LABEL,
            amount=base_snapshot.grand_total,
            currency=base_snapshot.currency,
            help_text=BASE_HELP,
            is_primary=accepted_response is None,
        ),
        TotalsRow(
            label=ALL_OPTIONS_LABEL,
            amount=ceiling.grand_total,
            currency=ceiling.currency,
            help_text=ALL_OPTIONS_HELP,
        ),
    ]

    if accepted_response is not None:
        # Read straight off the response. Not recomputed from the quotation:
        # that is the whole guarantee, and recomputing it here would quietly
        # restate an agreement whenever a later revision changed a price.
        rows.insert(
            0,
            TotalsRow(
                label=ACCEPTED_LABEL,
                amount=accepted_response.grand_total,
                currency=accepted_response.currency,
                help_text=ACCEPTED_HELP,
                is_primary=True,
            ),
        )
    return tuple(rows)
