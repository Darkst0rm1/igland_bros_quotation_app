"""Price resolution and the quotation warning rules.

**The selected tier is authoritative.** Nothing in this module changes a line's
tier. Entering fewer containers than a tier expects raises a warning and
nothing else — the brief is explicit about it, and it is enforced by keeping
tier selection out of every quantity code path.

Warnings are data, not exceptions: :func:`evaluate_quotation` returns a list
that the editor renders, the approval engine reads, and the document gate
checks. A rule firing never blocks an edit; it blocks *release*, and only when
its severity says so.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from modules import settings_service
from modules.calculation_engine import ZERO, piece_pack_mismatch, to_decimal
from modules.constants import (
    PriceTierCode,
    PriceWarningCode,
    PricingBasis,
    WarningSeverity,
)
from modules.models import ProductPrice, Quotation, QuotationItem
from modules.repositories import (
    get_effective_price,
    get_latest_price,
    get_standard_price,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceWarning:
    code: PriceWarningCode
    severity: WarningSeverity
    message: str
    line_no: int | None = None
    #: Whether a manager holding ``quote.override_warning`` may clear it with a
    #: reason. A missing price is not overridable: there is nothing to override,
    #: the line simply has no price.
    overridable: bool = True

    @property
    def blocks_release(self) -> bool:
        return self.severity is WarningSeverity.BLOCKING

    @property
    def icon(self) -> str:
        return {
            WarningSeverity.INFO: "ℹ️",
            WarningSeverity.WARNING: "⚠️",
            WarningSeverity.BLOCKING: "⛔",
        }[self.severity]


@dataclass(frozen=True)
class PriceResolution:
    """The outcome of asking for a price. ``price`` is None when none applies."""

    price: ProductPrice | None
    warnings: list[PriceWarning]

    @property
    def found(self) -> bool:
        return self.price is not None


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

def resolve_price(
    session: Session,
    product_variant_id: int,
    price_tier_code: str,
    on_date: dt.date,
    currency: str,
    line_no: int | None = None,
) -> PriceResolution:
    """Find the price for a variant and tier on a date.

    Never substitutes another tier, another currency or another date. A missing
    price is reported so the user can fix it, because silently quoting the
    nearest available price would misprice the order without anyone noticing.

    Distinguishes "expired" from "never priced": they need different messages,
    and only the first is an approval trigger rather than a hard stop.
    """
    warnings: list[PriceWarning] = []

    price = get_effective_price(
        session, product_variant_id, price_tier_code, on_date, currency
    )
    if price is not None:
        return PriceResolution(price, warnings)

    latest = get_latest_price(session, product_variant_id, price_tier_code, currency)
    if latest is None:
        warnings.append(
            PriceWarning(
                PriceWarningCode.PRICE_MISSING,
                WarningSeverity.BLOCKING,
                f"No {currency} price has ever been recorded for this product at the "
                f"{_tier_label(price_tier_code)} tier.",
                line_no=line_no,
                overridable=False,
            )
        )
    elif latest.effective_to is not None and latest.effective_to < on_date:
        warnings.append(
            PriceWarning(
                PriceWarningCode.PRICE_EXPIRED,
                WarningSeverity.BLOCKING,
                f"The {_tier_label(price_tier_code)} price for this product expired on "
                f"{latest.effective_to:%d %b %Y}. Using it requires approval.",
                line_no=line_no,
            )
        )
    else:
        warnings.append(
            PriceWarning(
                PriceWarningCode.PRICE_MISSING,
                WarningSeverity.BLOCKING,
                f"The {_tier_label(price_tier_code)} price for this product does not "
                f"take effect until {latest.effective_from:%d %b %Y}.",
                line_no=line_no,
                overridable=False,
            )
        )

    return PriceResolution(None, warnings)


def _tier_label(code: str) -> str:
    return code.replace("_", " ").title()


# --------------------------------------------------------------------------- #
# Warning rules
# --------------------------------------------------------------------------- #

def evaluate_line(
    session: Session,
    item: QuotationItem,
    quote_date: dt.date,
    currency: str,
) -> list[PriceWarning]:
    """Per-line rules: MOQ, piece/pack consistency, custom-price floor."""
    warnings: list[PriceWarning] = []
    line_no = item.line_no

    # --- below minimum order quantity ---------------------------------- #
    if item.moq_packs and item.quantity_packs and item.quantity_packs < item.moq_packs:
        warnings.append(
            PriceWarning(
                PriceWarningCode.BELOW_MOQ,
                WarningSeverity.WARNING,
                f"Line {line_no}: {item.quantity_packs:g} packs is below the minimum "
                f"of {item.moq_packs:g}. Pricing assumes the minimum run.",
                line_no=line_no,
            )
        )

    # --- piece price vs pack price ------------------------------------- #
    if item.case_pack and item.price_per_pack and item.price_per_piece:
        tolerance = settings_service.piece_pack_tolerance(session)
        delta = piece_pack_mismatch(
            item.price_per_pack, item.price_per_piece, item.case_pack, tolerance
        )
        if delta is not None:
            basis = (
                "pack price" if item.pricing_basis is PricingBasis.PACK else "piece price"
            )
            warnings.append(
                PriceWarning(
                    PriceWarningCode.PIECE_PACK_MISMATCH,
                    WarningSeverity.INFO,
                    f"Line {line_no}: the piece price differs from the pack price "
                    f"divided by {item.case_pack} by {abs(delta):.4f}. This line is "
                    f"priced on the {basis}.",
                    line_no=line_no,
                )
            )

    # --- custom price below the permitted floor ------------------------ #
    if item.is_custom_price and item.product_variant_id:
        standard = get_standard_price(
            session, item.product_variant_id, quote_date, currency
        )
        if standard is not None and standard.price_per_pack > ZERO:
            max_discount = settings_service.max_custom_discount_pct(session)
            floor = standard.price_per_pack * (
                Decimal(1) - max_discount / Decimal(100)
            )
            if to_decimal(item.price_per_pack) < floor:
                warnings.append(
                    PriceWarning(
                        PriceWarningCode.CUSTOM_PRICE_BELOW_FLOOR,
                        WarningSeverity.BLOCKING,
                        f"Line {line_no}: the custom price of {item.price_per_pack:.4f} "
                        f"is more than {max_discount:g}% below the standard price of "
                        f"{standard.price_per_pack:.4f}. Approval is required.",
                        line_no=line_no,
                    )
                )

    return warnings


def evaluate_quotation(
    session: Session, quotation: Quotation
) -> list[PriceWarning]:
    """Every rule, across the whole quotation.

    Returned in severity order — blocking first — so the editor and the approval
    queue both surface the things that actually stop release before the advisory
    ones.
    """
    warnings: list[PriceWarning] = []
    items = list(quotation.items)
    quote_date = quotation.quote_date
    currency = quotation.currency

    for item in items:
        warnings.extend(evaluate_line(session, item, quote_date, currency))

    warnings.extend(_tier_container_warnings(session, quotation, items))
    warnings.extend(_duplicate_line_warnings(items))
    warnings.extend(_mix_limit_warnings(session, items))
    warnings.extend(_duplicate_freight_warnings(session, quotation))

    order = {
        WarningSeverity.BLOCKING: 0,
        WarningSeverity.WARNING: 1,
        WarningSeverity.INFO: 2,
    }
    return sorted(warnings, key=lambda w: (order[w.severity], w.line_no or 0))


def _tier_container_warnings(
    session: Session, quotation: Quotation, items: list[QuotationItem]
) -> list[PriceWarning]:
    """A tier with a container minimum, quoted below it.

    Scope — quotation-wide or per line — is a setting. Commercially the price is
    earned by the order, so the default sums containers across the quotation.

    This never changes the tier. It says so explicitly in the message, because
    the obvious next question from anyone seeing it is "did it just reprice my
    quotation?".
    """
    from modules.repositories import price_tier_map

    tiers = price_tier_map(session)
    scope = settings_service.tier_container_scope(session)
    warnings: list[PriceWarning] = []

    # Per-line scope keeps using the line's own count: a shipment describes
    # the order as a whole, so there is nothing per-line to read from it.
    if scope == "line":
        for item in items:
            tier = _tier_for(tiers, item)
            if tier is None or not tier.min_containers:
                continue
            if to_decimal(item.container_count) < tier.min_containers:
                warnings.append(
                    PriceWarning(
                        PriceWarningCode.TIER_CONTAINERS_SHORT,
                        WarningSeverity.WARNING,
                        f"Line {item.line_no} is priced at the {tier.name} tier but "
                        f"shows {item.container_count:g} container(s), fewer than the "
                        f"{tier.min_containers} that tier expects. The tier has not "
                        f"been changed.",
                        line_no=item.line_no,
                    )
                )
        return warnings

    total_containers = _quotation_container_total(session, quotation, items)
    required = {
        tier.name: tier.min_containers
        for item in items
        if (tier := _tier_for(tiers, item)) is not None and tier.min_containers
    }
    for tier_name, minimum in required.items():
        if total_containers < minimum:
            warnings.append(
                PriceWarning(
                    PriceWarningCode.TIER_CONTAINERS_SHORT,
                    WarningSeverity.WARNING,
                    f"This quotation uses {tier_name} pricing but totals "
                    f"{total_containers:g} container(s), fewer than the {minimum} that "
                    f"tier expects. The tier has not been changed.",
                )
            )
    return warnings


def _quotation_container_total(
    session: Session, quotation: Quotation, items: list[QuotationItem]
) -> Decimal:
    """Containers on this quotation, for the tier minimums.

    The **shipment is authoritative when one exists** — it is the real shipping
    plan, with a row per container configuration. Quotations raised before
    container shipping existed have no shipment, and fall back to the per-line
    ``container_count`` they were built with, so their warnings are unchanged.
    """
    from modules.shipping_service import total_containers

    from_shipment = total_containers(session, quotation.id)
    if from_shipment > ZERO:
        return from_shipment
    return sum((to_decimal(item.container_count) for item in items), ZERO)


def _duplicate_freight_warnings(
    session: Session, quotation: Quotation
) -> list[PriceWarning]:
    """Freight entered by hand while a shipment also carries freight.

    Not merged and not silently ignored: two freight figures on one quotation
    is a decision for whoever raised it. The derived shipment charge is
    reconciled to a single row, so this can only happen when someone adds a
    second one deliberately.
    """
    from modules.constants import FreightMethod
    from modules.shipping_service import get_shipment, manual_freight_charges

    shipment = get_shipment(session, quotation.id)
    if shipment is None or shipment.total_freight <= ZERO:
        return []

    manual = manual_freight_charges(session, quotation.id)
    if not manual:
        return []

    manual_total = sum((c.amount for c in manual), ZERO)
    charged = shipment.freight_method is FreightMethod.ADDED_SEPARATELY
    return [
        PriceWarning(
            PriceWarningCode.DUPLICATE_FREIGHT,
            WarningSeverity.WARNING,
            f"This quotation has {shipment.total_freight:,.2f} of container freight "
            f"{'charged to the customer' if charged else 'recorded on the shipment'}, "
            f"and a further {manual_total:,.2f} entered as a manual freight charge. "
            "Remove one, or confirm they are genuinely separate costs.",
        )
    ]


def _tier_for(tiers: dict, item: QuotationItem):  # noqa: ANN001, ANN201
    if item.price_tier_id is None:
        return None
    return next((t for t in tiers.values() if t.id == item.price_tier_id), None)


def _duplicate_line_warnings(items: list[QuotationItem]) -> list[PriceWarning]:
    """The same variant at the same tier more than once.

    Legitimate occasionally — two delivery dates, two print designs — so this is
    a warning rather than a block.
    """
    counts = Counter(
        (item.product_variant_id, item.price_tier_id)
        for item in items
        if item.product_variant_id is not None
    )
    warnings: list[PriceWarning] = []
    for (variant_id, _tier_id), count in counts.items():
        if count < 2:
            continue
        lines = [
            str(i.line_no) for i in items if i.product_variant_id == variant_id
        ]
        warnings.append(
            PriceWarning(
                PriceWarningCode.DUPLICATE_LINE,
                WarningSeverity.WARNING,
                f"The same product and tier appears on lines {', '.join(lines)}.",
            )
        )
    return warnings


def _mix_limit_warnings(
    session: Session, items: list[QuotationItem]
) -> list[PriceWarning]:
    """More distinct products than a container is meant to hold.

    From the workbook's loading note. Advisory: it constrains how the order
    ships, not whether the price is right.
    """
    limit = settings_service.max_items_per_container(session)
    distinct = len({i.product_variant_id for i in items if i.product_variant_id})
    if limit and distinct > limit:
        return [
            PriceWarning(
                PriceWarningCode.MIX_LIMIT,
                WarningSeverity.INFO,
                f"This quotation covers {distinct} distinct products; containers are "
                f"loaded with at most {limit}. More than one container load will be "
                f"needed.",
            )
        ]
    return []


# --------------------------------------------------------------------------- #
# Helpers for the editor
# --------------------------------------------------------------------------- #

def blocking(warnings: list[PriceWarning]) -> list[PriceWarning]:
    return [w for w in warnings if w.blocks_release]


def can_release(warnings: list[PriceWarning]) -> bool:
    """Whether a final (non-draft) document may be produced.

    Only the absence of blocking warnings is checked here. The approval gate is
    a separate condition applied by ``approval_service``; both must pass.
    """
    return not blocking(warnings)


def tier_options(session: Session) -> list[tuple[str, str]]:
    """``(code, label)`` for every active tier, in display order."""
    from modules.repositories import get_price_tiers

    return [(t.code, t.name) for t in get_price_tiers(session)]


def prices_for_picker(
    session: Session,
    product_variant_id: int,
    on_date: dt.date,
    currency: str,
) -> dict[str, ProductPrice]:
    """Every tier's price for a variant, for showing the operator their options.

    Custom is excluded: it has no stored price by definition.
    """
    from modules.repositories import current_prices_for_variant

    prices = current_prices_for_variant(session, product_variant_id, on_date, currency)
    return {
        code: price
        for code, price in prices.items()
        if code != PriceTierCode.CUSTOM.value
    }
