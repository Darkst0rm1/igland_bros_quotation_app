"""Quotation lifecycle: drafts, lines, charges, terms, totals and status.

Every function here checks permission, and every mutation recomputes totals
through :mod:`modules.calculation_engine` — the money is never computed in a
page or assembled ad hoc.

Two invariants this module maintains:

* **A line carries a snapshot of everything that priced it** — the spec fields,
  both unit prices, the tier, and the id of the exact ``product_prices`` row.
  A price list moving on afterwards cannot change what the quotation said.
* **Status only ever changes through :func:`change_status`**, which consults the
  transition table and refuses anything not on it.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules import settings_service
from modules.audit_service import record_audit, record_field_changes
from modules.authorization import (
    AuthUser,
    PermissionDenied,
    can_edit_quotation,
    require,
    require_edit_quotation,
)
from modules.calculation_engine import (
    ZERO,
    ChargeInput,
    LineInput,
    QuotationTotals,
    charge_amount,
    compute_line,
    compute_totals,
    plate_charge,
    PlateChargeInput,
    q_money,
)
from modules.constants import (
    ItemInclusion,
    STATUS_TRANSITIONS,
    STATUSES_REQUIRING_NOTE,
    AuditAction,
    ChargeType,
    EntityType,
    Perm,
    PriceTierCode,
    PricingBasis,
    QuotationStatus,
    TermSection,
    WaiverStatus,
)
from modules.models import (
    Quotation,
    QuotationCharge,
    QuotationItem,
    QuotationTerm,
    TermTemplate,
)
from modules.numbering import allocate_quote_number
from modules.pricing_service import resolve_price
from modules.repositories import (
    default_address,
    get_customer,
    get_price_tier,
    get_variant,
    margin_inputs,
    primary_contact,
)

log = logging.getLogger(__name__)


class QuotationError(ValueError):
    """A quotation operation that failed a business rule. Safe to show the user."""


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #

def create_draft(
    session: Session,
    user: AuthUser,
    customer_id: int,
    *,
    project_name: str | None = None,
    quote_date: dt.date | None = None,
    currency: str | None = None,
    sales_user_id: int | None = None,
) -> Quotation:
    """Start a draft, with the customer's details snapshotted onto it.

    The snapshot is the point: editing or renaming the customer later must not
    alter a quotation that has already been sent, so the name, contact and both
    addresses are copied here rather than looked up at print time.
    """
    require(user, Perm.QUOTE_CREATE)

    customer = get_customer(session, customer_id)
    if customer is None:
        raise QuotationError("That customer no longer exists.")

    quote_date = quote_date or dt.date.today()
    currency = (currency or customer.default_currency
                or settings_service.default_currency(session))
    validity_days = settings_service.default_validity_days(session)
    number = allocate_quote_number(
        session, settings_service.quote_number_format(session), quote_date
    )

    contact = primary_contact(customer)
    billing = default_address(customer, "BILLING")
    shipping = default_address(customer, "SHIPPING")

    quotation = Quotation(
        quote_number=number,
        revision_no=0,
        is_current_revision=True,
        status=QuotationStatus.DRAFT,
        quote_date=quote_date,
        valid_until=quote_date + dt.timedelta(days=validity_days),
        customer_id=customer.id,
        customer_contact_id=contact.id if contact else None,
        customer_name_snapshot=customer.company_name,
        contact_name=contact.name if contact else None,
        contact_email=contact.email if contact else None,
        contact_phone=contact.phone if contact else None,
        billing_address_text=billing.as_text() if billing else None,
        shipping_address_text=shipping.as_text() if shipping else None,
        project_name=project_name,
        sales_user_id=sales_user_id or user.id,
        currency=currency,
        exchange_rate=Decimal("1"),
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    session.add(quotation)
    session.flush()
    quotation.root_quotation_id = quotation.id
    session.flush()

    _apply_default_terms(session, quotation)
    recompute_totals(session, quotation)

    record_audit(
        session, user, AuditAction.QUOTATION_CREATED, EntityType.QUOTATION, quotation.id,
        new_value={
            "quote_number": quotation.quote_number,
            "customer": customer.company_name,
            "currency": currency,
            "quote_date": quote_date,
        },
    )
    log.info("Draft created: %s for %s", number, customer.company_name)
    return quotation


def _apply_default_terms(session: Session, quotation: Quotation) -> None:
    """Copy the templates marked ``is_default`` onto the quotation.

    Copied, not linked: an employee edits the wording for one customer without
    touching the master template, which is what the brief requires.
    """
    templates = session.execute(
        select(TermTemplate)
        .where(TermTemplate.is_default.is_(True), TermTemplate.is_active.is_(True))
        .order_by(TermTemplate.sort_order)
    ).scalars()

    for template in templates:
        session.add(
            QuotationTerm(
                quotation_id=quotation.id,
                term_template_id=template.id,
                section=template.section,
                title=template.title,
                body_text=template.body_text,
                sort_order=template.sort_order,
                is_customer_visible=True,
            )
        )
    session.flush()


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #

_HEADER_FIELDS = (
    "project_name", "brand", "distributor", "customer_po_ref",
    "quote_date", "valid_until", "currency", "exchange_rate",
    "internal_notes", "customer_notes", "contact_name", "contact_email",
    "contact_phone", "billing_address_text", "shipping_address_text",
    "quote_discount_pct", "tax_rate_pct",
)


def update_header(
    session: Session, user: AuthUser, quotation: Quotation, **fields
) -> Quotation:
    require_edit_quotation(user, quotation)

    unknown = set(fields) - set(_HEADER_FIELDS)
    if unknown:
        raise QuotationError(f"Cannot set {', '.join(sorted(unknown))} on a quotation.")

    before = {name: getattr(quotation, name) for name in fields}
    for name, value in fields.items():
        setattr(quotation, name, value)
    quotation.updated_by_id = user.id
    session.flush()

    recompute_totals(session, quotation)
    record_field_changes(
        session, user, AuditAction.QUOTATION_EDITED, EntityType.QUOTATION, quotation.id,
        before, {name: getattr(quotation, name) for name in fields},
    )
    return quotation


# --------------------------------------------------------------------------- #
# Line items
# --------------------------------------------------------------------------- #

def add_line(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    product_variant_id: int,
    price_tier_code: str,
    quantity_packs: Decimal,
    container_count: Decimal = ZERO,
    pricing_basis: PricingBasis = PricingBasis.PACK,
    custom_price_per_pack: Decimal | None = None,
    custom_price_reason: str | None = None,
    description_override: str | None = None,
    line_discount_pct: Decimal = ZERO,
    customer_remarks: str | None = None,
    internal_remarks: str | None = None,
) -> QuotationItem:
    """Add a product line, snapshotting its specification and price."""
    require_edit_quotation(user, quotation)

    variant = get_variant(session, product_variant_id)
    if variant is None:
        raise QuotationError("That product variant no longer exists.")

    tier = get_price_tier(session, price_tier_code)
    if tier is None:
        raise QuotationError(f"Unknown price tier {price_tier_code!r}.")

    is_custom = price_tier_code == PriceTierCode.CUSTOM.value
    price_record = None

    if is_custom:
        if custom_price_per_pack is None or custom_price_per_pack <= ZERO:
            raise QuotationError("A custom price is required for the Custom tier.")
        price_per_pack = custom_price_per_pack
        price_per_piece = (
            price_per_pack / Decimal(variant.case_pack)
        ).quantize(Decimal("0.000001"))
    else:
        resolution = resolve_price(
            session, variant.id, price_tier_code, quotation.quote_date, quotation.currency
        )
        if not resolution.found:
            # Surfaced as a warning in the editor, but a line cannot be created
            # with no price at all — there would be nothing to store.
            raise QuotationError(resolution.warnings[0].message)
        price_record = resolution.price
        price_per_pack = price_record.price_per_pack
        price_per_piece = price_record.price_per_piece

    line_no = _next_line_no(session, quotation.id)
    unit_cost = margin_inputs(
        session, variant.id, quotation.quote_date, quotation.currency
    )

    item = QuotationItem(
        quotation_id=quotation.id,
        line_no=line_no,
        sort_order=line_no,
        product_variant_id=variant.id,
        product_price_id=price_record.id if price_record else None,
        description_override=description_override,
        # --- specification snapshot ---------------------------------- #
        item_number_snapshot=variant.variant_item_number,
        size_label=variant.product.size_label,
        depth_in=variant.product.depth_in,
        flute=variant.product.flute,
        board_quality=variant.board_quality,
        case_pack=variant.case_pack,
        printing_method=variant.product.printing_method,
        num_colours=variant.num_colours,
        moq_packs=variant.moq_packs,
        # --- pricing --------------------------------------------------- #
        price_tier_id=tier.id,
        pricing_basis=pricing_basis,
        quantity_packs=quantity_packs,
        container_count=container_count,
        price_per_pack=price_per_pack,
        price_per_piece=price_per_piece,
        is_custom_price=is_custom,
        custom_price_reason=custom_price_reason,
        line_discount_pct=line_discount_pct,
        unit_cost_per_pack=unit_cost,
        customer_remarks=customer_remarks,
        internal_remarks=internal_remarks,
    )
    session.add(item)
    session.flush()

    _recompute_line(session, item)
    recompute_totals(session, quotation)

    record_audit(
        session, user, AuditAction.QUOTATION_ITEM_ADDED, EntityType.QUOTATION_ITEM,
        item.id,
        new_value={
            "quotation": quotation.quote_number,
            "line_no": line_no,
            "variant": variant.variant_item_number,
            "tier": tier.code,
            "quantity_packs": quantity_packs,
            "price_per_pack": price_per_pack,
        },
        reason="custom price" if is_custom else None,
    )
    return item


def update_line(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    item_id: int,
    **fields,
) -> QuotationItem:
    """Change a line's quantities, discount, remarks or description.

    Note what is **not** here: the price tier. Changing the tier changes the
    price, so it goes through :func:`change_line_tier`, which re-resolves and
    re-snapshots. Quantity changes never touch the tier.
    """
    require_edit_quotation(user, quotation)

    item = session.get(QuotationItem, item_id)
    if item is None or item.quotation_id != quotation.id:
        raise QuotationError("That line is not part of this quotation.")

    allowed = {
        "quantity_packs", "container_count", "pricing_basis", "line_discount_pct",
        "line_discount_amount", "description_override", "spec_text_override",
        "customer_remarks", "internal_remarks", "sort_order",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise QuotationError(f"Cannot set {', '.join(sorted(unknown))} on a line.")

    before = {name: getattr(item, name) for name in fields}
    for name, value in fields.items():
        setattr(item, name, value)
    session.flush()

    _recompute_line(session, item)
    recompute_totals(session, quotation)

    action = (
        AuditAction.QUANTITY_CHANGED
        if {"quantity_packs", "container_count"} & set(fields)
        else AuditAction.DISCOUNT_CHANGED
        if "line_discount_pct" in fields or "line_discount_amount" in fields
        else AuditAction.QUOTATION_EDITED
    )
    record_field_changes(
        session, user, action, EntityType.QUOTATION_ITEM, item.id,
        before, {name: getattr(item, name) for name in fields},
    )
    return item


def change_line_tier(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    item_id: int,
    price_tier_code: str,
    custom_price_per_pack: Decimal | None = None,
    custom_price_reason: str | None = None,
) -> QuotationItem:
    """Re-price a line at a different tier.

    This is the *only* way a line's tier changes. Nothing driven by quantity
    calls it, which is what guarantees the brief's rule that entering fewer
    containers warns rather than silently repricing.
    """
    require_edit_quotation(user, quotation)

    item = session.get(QuotationItem, item_id)
    if item is None or item.quotation_id != quotation.id:
        raise QuotationError("That line is not part of this quotation.")

    tier = get_price_tier(session, price_tier_code)
    if tier is None:
        raise QuotationError(f"Unknown price tier {price_tier_code!r}.")

    before = {
        "tier": item.tier.code if item.tier else None,
        "price_per_pack": item.price_per_pack,
    }

    if price_tier_code == PriceTierCode.CUSTOM.value:
        if custom_price_per_pack is None or custom_price_per_pack <= ZERO:
            raise QuotationError("A custom price is required for the Custom tier.")
        item.price_per_pack = custom_price_per_pack
        item.price_per_piece = (
            custom_price_per_pack / Decimal(item.case_pack or 1)
        ).quantize(Decimal("0.000001"))
        item.is_custom_price = True
        item.custom_price_reason = custom_price_reason
        item.product_price_id = None
    else:
        resolution = resolve_price(
            session, item.product_variant_id, price_tier_code,
            quotation.quote_date, quotation.currency,
        )
        if not resolution.found:
            raise QuotationError(resolution.warnings[0].message)
        item.price_per_pack = resolution.price.price_per_pack
        item.price_per_piece = resolution.price.price_per_piece
        item.product_price_id = resolution.price.id
        item.is_custom_price = False
        item.custom_price_reason = None

    item.price_tier_id = tier.id
    session.flush()

    _recompute_line(session, item)
    recompute_totals(session, quotation)

    record_audit(
        session, user,
        AuditAction.CUSTOM_PRICE_USED if item.is_custom_price else AuditAction.PRICE_CHANGED,
        EntityType.QUOTATION_ITEM, item.id,
        old_value=before,
        new_value={"tier": tier.code, "price_per_pack": item.price_per_pack},
        reason=custom_price_reason,
    )
    return item


def apply_tier_to_all(
    session: Session, user: AuthUser, quotation: Quotation, price_tier_code: str
) -> list[str]:
    """Set one tier across every line. Returns messages for lines that could not.

    A line whose variant has no price at that tier is left on its current tier
    rather than being dropped or zeroed — a partial application the user can see
    beats a silent one they cannot.
    """
    require_edit_quotation(user, quotation)

    problems: list[str] = []
    for item in list(quotation.items):
        try:
            change_line_tier(session, user, quotation, item.id, price_tier_code)
        except QuotationError as exc:
            problems.append(f"Line {item.line_no}: {exc}")
    return problems


def remove_line(
    session: Session, user: AuthUser, quotation: Quotation, item_id: int
) -> None:
    require_edit_quotation(user, quotation)

    item = session.get(QuotationItem, item_id)
    if item is None or item.quotation_id != quotation.id:
        raise QuotationError("That line is not part of this quotation.")

    snapshot = {
        "line_no": item.line_no,
        "variant": item.item_number_snapshot,
        "net_line_total": item.net_line_total,
    }
    session.delete(item)
    session.flush()

    _renumber_lines(session, quotation)
    recompute_totals(session, quotation)
    record_audit(
        session, user, AuditAction.QUOTATION_ITEM_REMOVED, EntityType.QUOTATION_ITEM,
        None, old_value=snapshot,
    )


def duplicate_line(
    session: Session, user: AuthUser, quotation: Quotation, item_id: int
) -> QuotationItem:
    require_edit_quotation(user, quotation)

    source = session.get(QuotationItem, item_id)
    if source is None or source.quotation_id != quotation.id:
        raise QuotationError("That line is not part of this quotation.")

    line_no = _next_line_no(session, quotation.id)
    copy = QuotationItem(
        quotation_id=quotation.id,
        line_no=line_no,
        sort_order=line_no,
        product_variant_id=source.product_variant_id,
        product_price_id=source.product_price_id,
        description_override=source.description_override,
        spec_text_override=source.spec_text_override,
        item_number_snapshot=source.item_number_snapshot,
        size_label=source.size_label,
        depth_in=source.depth_in,
        flute=source.flute,
        board_quality=source.board_quality,
        case_pack=source.case_pack,
        printing_method=source.printing_method,
        num_colours=source.num_colours,
        moq_packs=source.moq_packs,
        price_tier_id=source.price_tier_id,
        pricing_basis=source.pricing_basis,
        quantity_packs=source.quantity_packs,
        container_count=source.container_count,
        price_per_pack=source.price_per_pack,
        price_per_piece=source.price_per_piece,
        is_custom_price=source.is_custom_price,
        custom_price_reason=source.custom_price_reason,
        line_discount_pct=source.line_discount_pct,
        unit_cost_per_pack=source.unit_cost_per_pack,
        customer_remarks=source.customer_remarks,
        internal_remarks=source.internal_remarks,
    )
    session.add(copy)
    session.flush()

    _recompute_line(session, copy)
    recompute_totals(session, quotation)
    record_audit(
        session, user, AuditAction.QUOTATION_ITEM_ADDED, EntityType.QUOTATION_ITEM,
        copy.id, new_value={"duplicated_from_line": source.line_no},
    )
    return copy


def _next_line_no(session: Session, quotation_id: int) -> int:
    highest = session.execute(
        select(func.max(QuotationItem.line_no)).where(
            QuotationItem.quotation_id == quotation_id
        )
    ).scalar()
    return (highest or 0) + 1


def _renumber_lines(session: Session, quotation: Quotation) -> None:
    """Close gaps after a deletion, so the printed document reads 1, 2, 3."""
    items = session.execute(
        select(QuotationItem)
        .where(QuotationItem.quotation_id == quotation.id)
        .order_by(QuotationItem.sort_order, QuotationItem.line_no)
    ).scalars().all()
    for index, item in enumerate(items, start=1):
        item.line_no = index
        item.sort_order = index
    session.flush()


# --------------------------------------------------------------------------- #
# Charges
# --------------------------------------------------------------------------- #

def add_charge(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    charge_type: ChargeType,
    description: str | None = None,
    quantity: Decimal = Decimal("1"),
    rate: Decimal = ZERO,
    currency: str | None = None,
    exchange_rate: Decimal = Decimal("1"),
    is_taxable: bool = True,
    is_customer_visible: bool = True,
    internal_note: str | None = None,
    source: str = "manual",
) -> QuotationCharge:
    require_edit_quotation(user, quotation)

    charge = QuotationCharge(
        quotation_id=quotation.id,
        sort_order=len(quotation.charges) + 1,
        charge_type=charge_type,
        description=description,
        quantity_value=quantity,
        rate=rate,
        amount=charge_amount(
            ChargeInput(quantity=quantity, rate=rate, exchange_rate=exchange_rate)
        ),
        currency=(currency or quotation.currency).upper(),
        exchange_rate=exchange_rate,
        is_taxable=is_taxable,
        is_customer_visible=is_customer_visible,
        internal_note=internal_note,
        source=source,
    )
    session.add(charge)
    session.flush()

    recompute_totals(session, quotation)
    record_audit(
        session, user, AuditAction.QUOTATION_EDITED, EntityType.QUOTATION_CHARGE,
        charge.id,
        new_value={
            "charge_type": str(charge_type),
            "amount": charge.amount,
            "customer_visible": is_customer_visible,
        },
    )
    return charge


def add_plate_charge(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    number_of_sizes: int,
    number_of_colours: int,
    number_of_designs: int = 1,
    existing_plate_available: bool = False,
    is_customer_visible: bool = True,
    is_taxable: bool = True,
) -> QuotationCharge:
    """Add a printing-plate charge computed from the configurable rate.

    ``sizes x colours x designs x rate``, zero when a reusable plate exists. The
    rate comes from company settings, seeded at the workbook's USD 200 per size
    per colour but never hardcoded into the calculation.
    """
    require_edit_quotation(user, quotation)

    rate = settings_service.plate_rate(session)
    spec = PlateChargeInput(
        number_of_sizes=number_of_sizes,
        number_of_colours=number_of_colours,
        number_of_designs=number_of_designs,
        plate_cost_per_colour=rate,
        existing_plate_available=existing_plate_available,
    )
    # The calculation engine is the authority on the amount. The charge is
    # stored as quantity x rate so the document can show the workings, and the
    # two are reconciled here rather than trusting them to agree by accident.
    expected = plate_charge(spec)
    units = (
        0 if existing_plate_available
        else number_of_sizes * number_of_colours * number_of_designs
    )
    if q_money(Decimal(units) * rate) != expected:  # pragma: no cover - guard
        raise QuotationError(
            "The plate charge could not be reconciled with the configured rate."
        )

    description = (
        f"Printing plates — {number_of_sizes} size(s) x {number_of_colours} colour(s)"
        + (f" x {number_of_designs} design(s)" if number_of_designs != 1 else "")
    )
    if existing_plate_available:
        description += " (existing plates, no charge)"

    return add_charge(
        session, user, quotation,
        charge_type=ChargeType.PRINTING_PLATES,
        description=description,
        quantity=Decimal(units),
        rate=rate,
        is_taxable=is_taxable,
        is_customer_visible=is_customer_visible,
        source="plate_calculator",
    )


# --------------------------------------------------------------------------- #
# Charge waivers
#
# Waiving works for every charge whatever produced it — a hand-added setup fee,
# the plate calculator's plates, the shipment's ocean freight, and anything a
# later charge type adds — because the status is on the charge rather than on
# any of the things that create one. There is deliberately no list of waivable
# types to keep in step.
#
# The amount is never touched by any of these. Rejecting or removing a waiver
# therefore restores the exact figure rather than one somebody re-entered.
# --------------------------------------------------------------------------- #

def _waivable_charge(
    session: Session, quotation: Quotation, charge_id: int
) -> QuotationCharge:
    """The charge, if it is on this quotation and the quotation can still move.

    Deliberately *not* ``require_edit_quotation``. That allows only DRAFT and
    REVISION_REQUIRED, so a waiver requested on a draft could never be approved
    once the quotation was submitted — the request and the decision would
    deadlock at exactly the moment the decision was wanted. An issued quotation
    is still immutable, which is the rule that matters.
    """
    if quotation.is_locked:
        raise QuotationError(
            f"{quotation.display_number} has been issued. Create a revision "
            "to change what is charged."
        )

    charge = session.get(QuotationCharge, charge_id)
    if charge is None or charge.quotation_id != quotation.id:
        raise QuotationError("That charge is not part of this quotation.")
    return charge


def _record_waiver(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    charge: QuotationCharge,
    was: WaiverStatus,
    reason: str | None,
) -> None:
    recompute_totals(session, quotation)
    record_audit(
        session, user, AuditAction.QUOTATION_EDITED, EntityType.QUOTATION_CHARGE,
        charge.id,
        old_value={"waiver_status": was.value},
        new_value={
            "waiver_status": charge.waiver_status.value,
            "charge_type": str(charge.charge_type),
            "amount": charge.amount,
            "requested_by_id": charge.waiver_requested_by_id,
            "decided_by_id": charge.waiver_decided_by_id,
        },
        reason=reason,
    )
    log.info(
        "Charge %s on %s: waiver %s -> %s by %s (%s)",
        charge.id, quotation.quote_number, was.value,
        charge.waiver_status.value, user.username, charge.amount,
    )


def request_charge_waiver(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    charge_id: int,
    reason: str,
) -> QuotationCharge:
    """Ask for a charge to be waived. Changes no money.

    The charge keeps being billed until a manager decides, which is the point:
    a concession that has been asked for is not one that has been given, and
    the quotation must not quietly reflect one that has not.
    """
    require(user, Perm.CHARGE_WAIVER_REQUEST)
    charge = _waivable_charge(session, quotation, charge_id)

    if not reason or not reason.strip():
        raise QuotationError("A reason is required to request a waiver.")
    if charge.waiver_status is WaiverStatus.APPROVED:
        raise QuotationError("That charge has already been waived.")
    if charge.waiver_status is WaiverStatus.PENDING:
        raise QuotationError("A waiver for that charge is already awaiting a decision.")

    was = charge.waiver_status
    charge.waiver_status = WaiverStatus.PENDING
    charge.waiver_reason = reason.strip()
    charge.waiver_requested_by_id = user.id
    charge.waiver_requested_at = dt.datetime.now(dt.UTC)
    charge.waiver_decided_by_id = None
    charge.waiver_decided_at = None
    charge.waiver_decision_note = None
    session.flush()

    _record_waiver(session, user, quotation, charge, was, reason.strip())
    return charge


def approve_charge_waiver(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    charge_id: int,
    note: str | None = None,
) -> QuotationCharge:
    """Grant a pending waiver. This is where the money comes off."""
    require(user, Perm.CHARGE_WAIVER_APPROVE)
    charge = _waivable_charge(session, quotation, charge_id)

    if charge.waiver_status is not WaiverStatus.PENDING:
        raise QuotationError(
            "There is no waiver awaiting a decision on that charge."
        )

    was = charge.waiver_status
    charge.waiver_status = WaiverStatus.APPROVED
    charge.waiver_decided_by_id = user.id
    charge.waiver_decided_at = dt.datetime.now(dt.UTC)
    charge.waiver_decision_note = (note or "").strip() or None
    session.flush()

    _record_waiver(session, user, quotation, charge, was, charge.waiver_reason)
    return charge


def reject_charge_waiver(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    charge_id: int,
    note: str | None = None,
) -> QuotationCharge:
    """Refuse a pending waiver. The charge goes on being billed.

    The request and the refusal both stay on the charge. Clearing them would
    lose that the concession was asked for and declined, which is the part
    somebody asks about later.
    """
    require(user, Perm.CHARGE_WAIVER_APPROVE)
    charge = _waivable_charge(session, quotation, charge_id)

    if charge.waiver_status is not WaiverStatus.PENDING:
        raise QuotationError(
            "There is no waiver awaiting a decision on that charge."
        )

    was = charge.waiver_status
    charge.waiver_status = WaiverStatus.REJECTED
    charge.waiver_decided_by_id = user.id
    charge.waiver_decided_at = dt.datetime.now(dt.UTC)
    charge.waiver_decision_note = (note or "").strip() or None
    session.flush()

    _record_waiver(session, user, quotation, charge, was, charge.waiver_reason)
    return charge


def waive_charge_directly(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    charge_id: int,
    reason: str,
) -> QuotationCharge:
    """A manager waiving without waiting for a request they would approve.

    Recorded as requested *and* decided by the same person, deliberately: the
    audit history should show a one-step waiver as a one-step waiver rather
    than manufacturing a request nobody made.
    """
    require(user, Perm.CHARGE_WAIVER_APPROVE)
    charge = _waivable_charge(session, quotation, charge_id)

    if not reason or not reason.strip():
        raise QuotationError("A reason is required to waive a charge.")
    if charge.waiver_status is WaiverStatus.APPROVED:
        raise QuotationError("That charge has already been waived.")

    was = charge.waiver_status
    now = dt.datetime.now(dt.UTC)
    charge.waiver_status = WaiverStatus.APPROVED
    charge.waiver_reason = reason.strip()
    charge.waiver_requested_by_id = user.id
    charge.waiver_requested_at = now
    charge.waiver_decided_by_id = user.id
    charge.waiver_decided_at = now
    session.flush()

    _record_waiver(session, user, quotation, charge, was, reason.strip())
    return charge


def remove_charge_waiver(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    charge_id: int,
    reason: str | None = None,
) -> QuotationCharge:
    """Put an approved waiver back on the bill. Needs the approver's authority.

    Un-waiving adds money to a quotation that may already have been shown to a
    customer, so it is the same decision as granting one and takes the same
    permission.
    """
    require(user, Perm.CHARGE_WAIVER_APPROVE)
    charge = _waivable_charge(session, quotation, charge_id)

    if charge.waiver_status is not WaiverStatus.APPROVED:
        raise QuotationError("That charge is not waived.")

    was = charge.waiver_status
    charge.waiver_status = WaiverStatus.NONE
    charge.waiver_decided_by_id = user.id
    charge.waiver_decided_at = dt.datetime.now(dt.UTC)
    charge.waiver_decision_note = (reason or "").strip() or None
    session.flush()

    _record_waiver(session, user, quotation, charge, was, reason)
    return charge


def pending_waivers(
    session: Session, user: AuthUser
) -> list[tuple[QuotationCharge, Quotation]]:
    """Charges awaiting a waiver decision, for the approver's queue.

    Shaped like :func:`approval_service.queue` and filtered the same way: a
    deleted quotation leaves its pending request behind, and offering an
    approver a quotation that appears nowhere else is how a waiver gets granted
    on something already thrown away.

    Unlike quotation approval this does **not** exclude the requester's own
    quotations. Waiving is decided on the money, not on who raised it, and a
    manager waiving a charge on their own quotation is the direct-waive path
    the business asked for.
    """
    require(user, Perm.CHARGE_WAIVER_APPROVE)

    rows = session.execute(
        select(QuotationCharge, Quotation)
        .join(Quotation, QuotationCharge.quotation_id == Quotation.id)
        .where(
            QuotationCharge.waiver_status == WaiverStatus.PENDING,
            Quotation.deleted_at.is_(None),
        )
        .order_by(QuotationCharge.waiver_requested_at)
    ).all()
    return [(charge, quotation) for charge, quotation in rows]


def remove_charge(
    session: Session, user: AuthUser, quotation: Quotation, charge_id: int
) -> None:
    require_edit_quotation(user, quotation)

    charge = session.get(QuotationCharge, charge_id)
    if charge is None or charge.quotation_id != quotation.id:
        raise QuotationError("That charge is not part of this quotation.")

    snapshot = {"charge_type": str(charge.charge_type), "amount": charge.amount}
    session.delete(charge)
    session.flush()
    recompute_totals(session, quotation)
    record_audit(
        session, user, AuditAction.QUOTATION_EDITED, EntityType.QUOTATION_CHARGE,
        None, old_value=snapshot,
    )


# --------------------------------------------------------------------------- #
# Terms
# --------------------------------------------------------------------------- #

def set_terms(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    template_ids: list[int],
) -> None:
    """Replace the selected terms with copies of the chosen templates.

    Existing terms whose wording has been edited for this quotation are kept as
    they are; only additions and removals are applied. Otherwise re-ticking a
    box would quietly discard someone's edit.
    """
    require_edit_quotation(user, quotation)

    existing = {t.term_template_id: t for t in quotation.terms if t.term_template_id}
    wanted = set(template_ids)

    for template_id, term in list(existing.items()):
        if template_id not in wanted:
            session.delete(term)

    templates = session.execute(
        select(TermTemplate).where(TermTemplate.id.in_(wanted - set(existing)))
    ).scalars()
    for template in templates:
        session.add(
            QuotationTerm(
                quotation_id=quotation.id,
                term_template_id=template.id,
                section=template.section,
                title=template.title,
                body_text=template.body_text,
                sort_order=template.sort_order,
                is_customer_visible=True,
            )
        )
    session.flush()
    session.expire(quotation, ["terms"])
    record_audit(
        session, user, AuditAction.TERMS_CHANGED, EntityType.QUOTATION, quotation.id,
        new_value={"template_ids": sorted(wanted)},
    )


def edit_term(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    term_id: int,
    *,
    body_text: str,
    is_customer_visible: bool = True,
) -> QuotationTerm:
    """Reword a term for this quotation only. The master template is untouched."""
    require_edit_quotation(user, quotation)

    term = session.get(QuotationTerm, term_id)
    if term is None or term.quotation_id != quotation.id:
        raise QuotationError("That term is not part of this quotation.")

    before = {"body_text": term.body_text, "is_customer_visible": term.is_customer_visible}
    term.body_text = body_text
    term.is_customer_visible = is_customer_visible
    session.flush()

    record_field_changes(
        session, user, AuditAction.TERMS_CHANGED, EntityType.QUOTATION_TERM, term.id,
        before, {"body_text": body_text, "is_customer_visible": is_customer_visible},
    )
    return term


def add_custom_term(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    section: TermSection,
    title: str,
    body_text: str,
    is_customer_visible: bool = True,
) -> QuotationTerm:
    require_edit_quotation(user, quotation)

    term = QuotationTerm(
        quotation_id=quotation.id,
        term_template_id=None,
        section=section,
        title=title,
        body_text=body_text,
        sort_order=len(quotation.terms) + 1,
        is_customer_visible=is_customer_visible,
    )
    session.add(term)
    session.flush()
    session.expire(quotation, ["terms"])
    record_audit(
        session, user, AuditAction.TERMS_CHANGED, EntityType.QUOTATION_TERM, term.id,
        new_value={"title": title},
    )
    return term


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #

def _recompute_line(session: Session, item: QuotationItem) -> None:
    result = compute_line(
        LineInput(
            quantity_packs=item.quantity_packs,
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
            unit_cost_per_pack=item.unit_cost_per_pack,
        )
    )
    # Store the engine's quantized values, not the raw input, so a quantity
    # entered as 2000 and one loaded back as 2000.000 compare and display
    # identically — otherwise a revision diff reports a change that is only a
    # difference in trailing zeros.
    item.quantity_packs = result.quantity_packs
    item.quantity_pieces = result.quantity_pieces
    item.gross_line_total = result.gross_line_total
    item.line_discount_amount = result.line_discount_amount
    item.net_line_total = result.net_line_total
    item.line_cost_total = result.line_cost_total
    session.flush()


def recompute_totals(session: Session, quotation: Quotation) -> QuotationTotals:
    """Recalculate every line and roll up to the grand total.

    Called after any mutation. Totals are stored rather than derived on read so
    that an issued quotation keeps the figures it was issued with, and so that
    history and reports do not have to re-price anything.
    """
    session.flush()
    items = session.execute(
        select(QuotationItem)
        .where(QuotationItem.quotation_id == quotation.id)
        .order_by(QuotationItem.sort_order, QuotationItem.line_no)
    ).scalars().all()

    # Stored totals are the BASE offer: INCLUDED lines only. An OPTIONAL or
    # RECOMMENDED line costs nothing until the customer selects it, so counting
    # it here would make every employee list and report quote a figure the
    # customer was never offered. The all-options figure is derived on demand
    # from pricing_snapshot rather than stored, so it cannot drift.
    line_results = []
    for item in items:
        _recompute_line(session, item)
        if item.inclusion is not ItemInclusion.INCLUDED:
            continue
        line_results.append(
            compute_line(
                LineInput(
                    quantity_packs=item.quantity_packs,
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
                    unit_cost_per_pack=item.unit_cost_per_pack,
                )
            )
        )

    charges = session.execute(
        select(QuotationCharge).where(QuotationCharge.quotation_id == quotation.id)
    ).scalars().all()
    charge_inputs = [
        ChargeInput(
            quantity=c.quantity_value,
            rate=c.rate,
            exchange_rate=c.exchange_rate,
            is_taxable=c.is_taxable,
            is_customer_visible=c.is_customer_visible,
            is_waived=c.is_waived,
        )
        for c in charges
    ]

    totals = compute_totals(
        line_results,
        charge_inputs,
        quotation_discount_pct=quotation.quote_discount_pct or ZERO,
        tax_rate_pct=quotation.tax_rate_pct or ZERO,
    )

    quotation.subtotal = totals.subtotal
    quotation.quote_discount_amount = totals.quotation_discount
    quotation.charges_total = totals.charges_total
    quotation.tax_amount = totals.tax_amount
    quotation.grand_total = totals.grand_total
    quotation.total_cost = totals.total_cost
    quotation.gross_profit = totals.gross_profit
    quotation.gross_margin_pct = totals.gross_margin_pct
    session.flush()

    # Every mutation routes through here, so this is the one place that has to
    # invalidate the loaded collections. ``expire_on_commit`` is off — which
    # Streamlit's render-after-write flow needs — so without this a caller that
    # added a line then read ``quotation.items`` would get the collection as it
    # was before the write.
    session.expire(quotation, ["items", "charges"])
    return totals


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def change_status(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    new_status: QuotationStatus,
    note: str | None = None,
) -> Quotation:
    """The only writer of ``quotations.status``.

    Refuses any move not in the transition table, and requires a note for the
    four statuses the brief says must carry one.
    """
    require(user, Perm.QUOTE_UPDATE_STATUS)

    current = quotation.status
    if new_status == current:
        return quotation

    allowed = STATUS_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        readable = ", ".join(sorted(s.value for s in allowed)) or "nothing"
        raise QuotationError(
            f"{quotation.display_number} is {current.value} and cannot move to "
            f"{new_status.value}. Allowed from here: {readable}."
        )

    if new_status in STATUSES_REQUIRING_NOTE and not (note or "").strip():
        raise QuotationError(
            f"A note is required when marking a quotation {new_status.value}."
        )

    if new_status is QuotationStatus.CANCELLED:
        require(user, Perm.QUOTE_CANCEL)

    quotation.status = new_status
    quotation.updated_by_id = user.id
    session.flush()

    record_audit(
        session, user, AuditAction.STATUS_CHANGED, EntityType.QUOTATION, quotation.id,
        old_value={"status": current.value},
        new_value={"status": new_status.value},
        reason=note,
    )
    log.info(
        "Quotation %s: %s -> %s", quotation.quote_number, current.value, new_status.value
    )
    return quotation


def expire_overdue(session: Session, user: AuthUser, today: dt.date | None = None) -> int:
    """Move approved or sent quotations past their validity date to Expired.

    Run from the dashboard rather than a scheduler: Community Cloud sleeps when
    idle, so there is no reliable background timer to hang this on.
    """
    today = today or dt.date.today()
    candidates = session.execute(
        select(Quotation).where(
            Quotation.status.in_(
                [QuotationStatus.APPROVED, QuotationStatus.SENT_TO_CUSTOMER]
            ),
            Quotation.valid_until.is_not(None),
            Quotation.valid_until < today,
        )
    ).scalars().all()

    for quotation in candidates:
        quotation.status = QuotationStatus.EXPIRED
        record_audit(
            session, user, AuditAction.STATUS_CHANGED, EntityType.QUOTATION, quotation.id,
            old_value={"status": "APPROVED/SENT"},
            new_value={"status": QuotationStatus.EXPIRED.value},
            reason=f"validity date {quotation.valid_until} passed",
        )
    session.flush()
    return len(candidates)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_for_submission(session: Session, quotation: Quotation) -> list[str]:
    """Problems that must be fixed before a quotation can be submitted or sent.

    Distinct from pricing warnings: these are structural gaps — no lines, no
    validity date, a zero quantity — rather than commercial judgements.
    """
    problems: list[str] = []

    if not quotation.items:
        problems.append("The quotation has no product lines.")
    if quotation.valid_until is None:
        problems.append("The quotation has no valid-until date.")
    elif quotation.valid_until < quotation.quote_date:
        problems.append("The valid-until date is before the quote date.")
    if not quotation.customer_id:
        problems.append("No customer is selected.")

    for item in quotation.items:
        if item.quantity_packs is None or item.quantity_packs <= ZERO:
            problems.append(f"Line {item.line_no} has no quantity.")
        if item.price_per_pack is None or item.price_per_pack <= ZERO:
            problems.append(f"Line {item.line_no} has no price.")

    if quotation.grand_total is not None and quotation.grand_total < ZERO:
        problems.append("The grand total is negative.")

    return problems


def can_edit(user: AuthUser, quotation: Quotation) -> bool:
    return can_edit_quotation(user, quotation)


# --------------------------------------------------------------------------- #
# Deletion
# --------------------------------------------------------------------------- #

def revision_family(session: Session, quotation: Quotation) -> list[Quotation]:
    """Every revision sharing this quotation's number, oldest first.

    Deletion works on the family rather than the row. "Delete QT-2026-0001"
    means the quotation, and removing Rev 2 on its own would leave Rev 1 in the
    list with nothing marked as current — a quotation that cannot be opened
    from the history page and does not appear in it either.
    """
    root_id = quotation.root_quotation_id or quotation.id
    return list(
        session.execute(
            select(Quotation)
            .where(Quotation.root_quotation_id == root_id)
            .order_by(Quotation.revision_no)
        ).scalars().all()
    )


def can_delete(user: AuthUser, quotation: Quotation) -> bool:
    """Whether ``user`` may delete this quotation.

    An unissued draft is a working document: whoever may edit it may remove it.
    Anything issued is a record of what was actually sent to a customer, so it
    takes the stronger permission — the ordinary delete must not be able to
    make a sent quotation disappear.
    """
    if user.has(Perm.QUOTE_DELETE_ANY):
        return True
    if not user.has(Perm.QUOTE_DELETE_DRAFT):
        return False
    return can_edit_quotation(user, quotation)


def delete_quotation(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    reason: str | None = None,
) -> int:
    """Soft-delete a quotation and every revision of it. Returns the count.

    Soft throughout. The rows stay, ``deleted_at`` is set, and an administrator
    can restore them, because "delete" pressed on the wrong row is otherwise
    unrecoverable without a database restore — and the quotations here carry
    prices that were quoted to a customer.

    The quotation number is *not* released. Numbers come from a
    ``DocumentSequence`` counter rather than from a count of rows, so a deleted
    QT-2026-0007 stays spent and no later quotation can be issued under a
    number a customer has already seen on a different document.

    Already-deleted revisions are skipped rather than refreshed, so the
    ``deleted_at`` of an earlier deletion is preserved.
    """
    if not can_delete(user, quotation):
        if quotation.is_locked or quotation.status is not QuotationStatus.DRAFT:
            raise PermissionDenied(
                str(Perm.QUOTE_DELETE_ANY),
                f"{quotation.display_number} has been issued. Deleting it requires "
                f"the quote.delete_any permission; cancelling it may be what you want.",
            )
        raise PermissionDenied(
            str(Perm.QUOTE_DELETE_DRAFT),
            f"{quotation.display_number} is not a draft you may delete.",
        )

    family = revision_family(session, quotation)
    now = dt.datetime.now(dt.UTC)

    deleted = 0
    for revision in family:
        if revision.deleted_at is not None:
            continue
        revision.deleted_at = now
        revision.updated_by_id = user.id
        deleted += 1
        record_audit(
            session, user, AuditAction.QUOTATION_DELETED,
            EntityType.QUOTATION, revision.id,
            old_value={
                "status": str(revision.status),
                "grand_total": str(revision.grand_total),
            },
            new_value={"deleted_at": now.isoformat()},
            reason=reason,
        )

    session.flush()
    log.info(
        "Quotation %s deleted by %s (%d revision(s))",
        quotation.quote_number, user.username, deleted,
    )
    return deleted


def restore_quotation(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    reason: str | None = None,
) -> int:
    """Undo a deletion, for the whole family. Returns the count restored.

    Requires ``quote.delete_any`` even for a draft the caller could have
    deleted themselves: restoring puts a quotation back into everyone's history
    and reports, which is a wider act than removing your own working copy.
    """
    require(user, Perm.QUOTE_DELETE_ANY)

    restored = 0
    for revision in revision_family(session, quotation):
        if revision.deleted_at is None:
            continue
        was = revision.deleted_at
        revision.deleted_at = None
        revision.updated_by_id = user.id
        restored += 1
        record_audit(
            session, user, AuditAction.QUOTATION_RESTORED,
            EntityType.QUOTATION, revision.id,
            old_value={"deleted_at": was.isoformat()},
            new_value={"deleted_at": None},
            reason=reason,
        )

    session.flush()
    log.info(
        "Quotation %s restored by %s (%d revision(s))",
        quotation.quote_number, user.username, restored,
    )
    return restored
