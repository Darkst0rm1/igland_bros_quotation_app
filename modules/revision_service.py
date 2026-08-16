"""Issuing a quotation, and revising one that has been issued.

The rule this module exists to keep: **an issued revision never changes.**
Issuing locks the quotation and writes an immutable JSON snapshot alongside the
PDF that was sent. Editing afterwards produces Rev 1, leaving Rev 0 exactly as
the customer received it.

Without this, the immutability guards in ``models.py`` would leave an issued
quotation permanently uneditable — they refuse the write and tell the user to
create a revision, so the revision has to exist.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.audit_service import record_audit
from modules.authorization import AuthUser, require
from modules.constants import AuditAction, EntityType, Perm, QuotationStatus
from modules.models import (
    Quotation,
    QuotationCharge,
    QuotationItem,
    QuotationRevision,
    QuotationTerm,
)

log = logging.getLogger(__name__)


class RevisionError(ValueError):
    """A revision operation that failed a business rule. Safe to show the user."""


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #

def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def snapshot(quotation: Quotation) -> dict[str, Any]:
    """A complete, self-contained record of the quotation as it stands.

    Deliberately plain JSON rather than a reference to the live tables: a later
    schema change, a renamed customer or a superseded price cannot alter what
    an issued revision says.
    """
    return {
        "quote_number": quotation.quote_number,
        "revision_no": quotation.revision_no,
        "status": quotation.status.value,
        "quote_date": _plain(quotation.quote_date),
        "valid_until": _plain(quotation.valid_until),
        "currency": quotation.currency,
        "customer": {
            "name": quotation.customer_name_snapshot,
            "contact_name": quotation.contact_name,
            "contact_email": quotation.contact_email,
            "contact_phone": quotation.contact_phone,
            "billing_address": quotation.billing_address_text,
            "shipping_address": quotation.shipping_address_text,
        },
        "project": quotation.project_name,
        "brand": quotation.brand,
        "distributor": quotation.distributor,
        "purchase_order": quotation.customer_po_ref,
        "customer_notes": quotation.customer_notes,
        "totals": {
            "subtotal": _plain(quotation.subtotal),
            "quote_discount_pct": _plain(quotation.quote_discount_pct),
            "quote_discount_amount": _plain(quotation.quote_discount_amount),
            "charges_total": _plain(quotation.charges_total),
            "tax_rate_pct": _plain(quotation.tax_rate_pct),
            "tax_amount": _plain(quotation.tax_amount),
            "grand_total": _plain(quotation.grand_total),
        },
        "lines": [
            {
                "line_no": i.line_no,
                "item_number": i.item_number_snapshot,
                "size_label": i.size_label,
                "board_quality": i.board_quality,
                "case_pack": i.case_pack,
                "tier": i.tier.code if i.tier else None,
                "pricing_basis": str(i.pricing_basis),
                "quantity_packs": _plain(i.quantity_packs),
                "quantity_pieces": _plain(i.quantity_pieces),
                "container_count": _plain(i.container_count),
                "price_per_pack": _plain(i.price_per_pack),
                "price_per_piece": _plain(i.price_per_piece),
                "is_custom_price": i.is_custom_price,
                "line_discount_pct": _plain(i.line_discount_pct),
                "net_line_total": _plain(i.net_line_total),
                "description_override": i.description_override,
                "customer_remarks": i.customer_remarks,
                # Cost is deliberately absent: a revision snapshot may be shown
                # to anyone who can view the quotation, and cost visibility is a
                # separate permission.
            }
            for i in sorted(quotation.items, key=lambda i: i.line_no)
        ],
        "charges": [
            {
                "charge_type": str(c.charge_type),
                "description": c.description,
                "quantity": _plain(c.quantity_value),
                "rate": _plain(c.rate),
                "amount": _plain(c.amount),
                "is_taxable": c.is_taxable,
                "is_customer_visible": c.is_customer_visible,
            }
            for c in quotation.charges
        ],
        "terms": [
            {
                "title": t.title,
                "body_text": t.body_text,
                "is_customer_visible": t.is_customer_visible,
            }
            for t in sorted(quotation.terms, key=lambda t: t.sort_order)
        ],
        # Additive. Snapshots taken before container shipping existed simply
        # lack this key, and compare() treats a missing key as "no change".
        "shipment": _shipment_snapshot(quotation),
    }


def _shipment_snapshot(quotation: Quotation) -> dict[str, Any] | None:
    """The shipping plan as issued, or ``None`` when there is none.

    Freight cost is included here: a revision snapshot is an internal record of
    what was issued, shown only to people who can already view the quotation.
    It is the *document* that withholds internal freight, not this.
    """
    shipment = quotation.shipment
    if shipment is None:
        return None
    return {
        "incoterm": shipment.incoterm.value if shipment.incoterm else None,
        "incoterm_place": shipment.incoterm_place,
        "origin_country": shipment.origin_country,
        "port_of_loading": shipment.port_of_loading,
        "port_of_discharge": shipment.port_of_discharge,
        "final_destination": shipment.final_destination,
        "freight_method": shipment.freight_method.value,
        "total_freight": _plain(shipment.total_freight),
        "freight_currency": shipment.freight_currency,
        "loading_method": (
            shipment.loading_method.value if shipment.loading_method else None
        ),
        "shipping_notes": shipment.shipping_notes,
        "containers": [
            {
                "carrier": c.carrier_name,
                "container_size": c.size_label,
                "container_type": c.type_label,
                "container_count": _plain(c.container_count),
                "freight_cost": _plain(c.freight_cost),
                "port_of_loading": c.port_of_loading,
                "port_of_discharge": c.port_of_discharge,
                "transit_days": c.transit_days,
                "estimated_departure": _plain(c.estimated_departure),
                "estimated_arrival": _plain(c.estimated_arrival),
                "allocations": [
                    {
                        "line_no": a.item.line_no if a.item else None,
                        "quantity_per_container": _plain(a.quantity_per_container),
                        "total_allocated_quantity": _plain(a.total_allocated_quantity),
                        "bundles_per_container": _plain(a.bundles_per_container),
                    }
                    for a in c.allocations
                ],
            }
            for c in sorted(shipment.containers, key=lambda c: c.sort_order)
        ],
    }


# --------------------------------------------------------------------------- #
# Issuing
# --------------------------------------------------------------------------- #

def issue(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    pdf_attachment_id: int | None = None,
    note: str | None = None,
) -> QuotationRevision:
    """Lock the quotation and record it as issued.

    Called when the final document has been produced. After this the quotation
    is immutable; the ORM guards will refuse any change to its commercial
    content, and edits must go through :func:`create_revision`.
    """
    require(user, Perm.QUOTE_GENERATE_PDF)

    if quotation.is_locked:
        raise RevisionError(
            f"{quotation.display_number} has already been issued."
        )

    existing = session.execute(
        select(QuotationRevision).where(
            QuotationRevision.root_quotation_id == quotation.root_quotation_id,
            QuotationRevision.revision_no == quotation.revision_no,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise RevisionError(
            f"{quotation.display_number} already has an issued snapshot."
        )

    previous = _previous_revision(session, quotation)
    current = snapshot(quotation)

    record = QuotationRevision(
        root_quotation_id=quotation.root_quotation_id,
        quotation_id=quotation.id,
        revision_no=quotation.revision_no,
        snapshot_json=current,
        previous_snapshot_json=previous.snapshot_json if previous else None,
        previous_total=previous.new_total if previous else None,
        new_total=quotation.grand_total,
        change_reason=note,
        previous_pdf_attachment_id=(
            previous.new_pdf_attachment_id if previous else None
        ),
        new_pdf_attachment_id=pdf_attachment_id,
        changed_by_id=user.id,
    )
    session.add(record)

    quotation.is_locked = True
    quotation.issued_at = dt.datetime.now(dt.UTC)
    session.flush()

    record_audit(
        session, user, AuditAction.REVISION_CREATED, EntityType.QUOTATION,
        quotation.id,
        new_value={
            "revision_no": quotation.revision_no,
            "grand_total": quotation.grand_total,
            "issued": True,
        },
        reason=note,
    )
    log.info("Issued %s", quotation.display_number)
    return record


def _previous_revision(
    session: Session, quotation: Quotation
) -> QuotationRevision | None:
    return session.execute(
        select(QuotationRevision)
        .where(QuotationRevision.root_quotation_id == quotation.root_quotation_id)
        .order_by(QuotationRevision.revision_no.desc())
    ).scalars().first()


# --------------------------------------------------------------------------- #
# Revising
# --------------------------------------------------------------------------- #

def create_revision(
    session: Session, user: AuthUser, quotation: Quotation, reason: str
) -> Quotation:
    """Deep-copy an issued quotation into the next revision.

    The previous revision keeps its number, its snapshot and its PDF. The new
    one carries the same quotation number with ``revision_no + 1`` and becomes
    the current revision.
    """
    require(user, Perm.QUOTE_CREATE_REVISION)

    if not reason or not reason.strip():
        raise RevisionError("A reason is required to create a revision.")

    if not quotation.is_locked:
        raise RevisionError(
            f"{quotation.display_number} has not been issued, so it can be edited "
            "directly — a revision is not needed."
        )

    root_id = quotation.root_quotation_id or quotation.id
    highest = session.execute(
        select(Quotation)
        .where(Quotation.root_quotation_id == root_id)
        .order_by(Quotation.revision_no.desc())
    ).scalars().first()
    next_no = (highest.revision_no if highest else quotation.revision_no) + 1

    revised = Quotation(
        root_quotation_id=root_id,
        quote_number=quotation.quote_number,
        revision_no=next_no,
        is_current_revision=True,
        is_locked=False,
        status=QuotationStatus.DRAFT,
        quote_date=dt.date.today(),
        valid_until=quotation.valid_until,
        customer_id=quotation.customer_id,
        customer_contact_id=quotation.customer_contact_id,
        customer_name_snapshot=quotation.customer_name_snapshot,
        contact_name=quotation.contact_name,
        contact_email=quotation.contact_email,
        contact_phone=quotation.contact_phone,
        billing_address_text=quotation.billing_address_text,
        shipping_address_text=quotation.shipping_address_text,
        project_name=quotation.project_name,
        brand=quotation.brand,
        distributor=quotation.distributor,
        customer_po_ref=quotation.customer_po_ref,
        sales_user_id=quotation.sales_user_id,
        currency=quotation.currency,
        exchange_rate=quotation.exchange_rate,
        quote_discount_pct=quotation.quote_discount_pct,
        tax_rate_id=quotation.tax_rate_id,
        tax_rate_pct=quotation.tax_rate_pct,
        internal_notes=quotation.internal_notes,
        customer_notes=quotation.customer_notes,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    session.add(revised)
    session.flush()

    # Queried rather than read off the relationships. ``expire_on_commit`` is
    # off, so a collection loaded before the children were written can be stale
    # — and a stale read here would silently produce a revision missing its
    # charges or terms, with nothing to indicate anything was lost.
    source_items = session.execute(
        select(QuotationItem)
        .where(QuotationItem.quotation_id == quotation.id)
        .order_by(QuotationItem.line_no)
    ).scalars().all()
    source_charges = session.execute(
        select(QuotationCharge).where(QuotationCharge.quotation_id == quotation.id)
    ).scalars().all()
    source_terms = session.execute(
        select(QuotationTerm).where(QuotationTerm.quotation_id == quotation.id)
    ).scalars().all()

    for item in source_items:
        session.add(_copy_item(item, revised.id))
    for charge in source_charges:
        session.add(_copy_charge(charge, revised.id))
    for term in source_terms:
        session.add(_copy_term(term, revised.id))
    session.flush()

    _copy_shipment(session, quotation, revised)

    # The superseded revision stops being current but keeps everything else,
    # including its lock. is_current_revision is one of the few fields the
    # immutability guard allows to change on a locked quotation, precisely so
    # this is possible.
    quotation.is_current_revision = False
    session.flush()

    from modules.quotation_service import recompute_totals

    recompute_totals(session, revised)

    record_audit(
        session, user, AuditAction.REVISION_CREATED, EntityType.QUOTATION, revised.id,
        old_value={"from_revision": quotation.revision_no},
        new_value={
            "revision_no": next_no,
            "quote_number": revised.quote_number,
            "previous_total": quotation.grand_total,
        },
        reason=reason,
    )
    log.info(
        "Created %s Rev %d from Rev %d",
        revised.quote_number, next_no, quotation.revision_no,
    )
    return revised


def _copy_shipment(
    session: Session, source: Quotation, revised: Quotation
) -> None:
    """Carry the shipping plan onto the new revision.

    Queried rather than read off the relationships, for the same reason lines
    and charges are: a collection loaded earlier in this session would not
    include rows written since, and a stale read here would silently drop
    containers from the revision.

    The freight charge is deliberately not copied — it is derived, and
    ``sync_freight`` recreates it from the copied container rows.
    """
    from modules.models import (
        QuotationShipment,
        ShipmentContainer,
        ShipmentProductAllocation,
    )

    original = session.execute(
        select(QuotationShipment).where(QuotationShipment.quotation_id == source.id)
    ).scalar_one_or_none()
    if original is None:
        return

    copy = QuotationShipment(
        quotation_id=revised.id,
        incoterm=original.incoterm,
        incoterm_place=original.incoterm_place,
        origin_country=original.origin_country,
        port_of_loading=original.port_of_loading,
        port_of_discharge=original.port_of_discharge,
        final_destination=original.final_destination,
        freight_method=original.freight_method,
        total_freight=original.total_freight,
        freight_currency=original.freight_currency,
        freight_taxable=original.freight_taxable,
        loading_method=original.loading_method,
        shipping_notes=original.shipping_notes,
        show_on_document=original.show_on_document,
        customer_visible_freight=original.customer_visible_freight,
        created_by_id=revised.created_by_id,
        updated_by_id=revised.updated_by_id,
    )
    session.add(copy)
    session.flush()

    # Line numbers are stable across a revision, so allocations are re-pointed
    # by line number rather than by the superseded item id.
    new_items = {
        item.line_no: item.id
        for item in session.execute(
            select(QuotationItem).where(QuotationItem.quotation_id == revised.id)
        ).scalars()
    }

    containers = session.execute(
        select(ShipmentContainer)
        .where(ShipmentContainer.quotation_shipment_id == original.id)
        .order_by(ShipmentContainer.sort_order)
    ).scalars().all()

    for container in containers:
        new_container = ShipmentContainer(
            quotation_shipment_id=copy.id,
            sort_order=container.sort_order,
            shipping_line_id=container.shipping_line_id,
            custom_shipping_line=container.custom_shipping_line,
            container_size=container.container_size,
            custom_container_size=container.custom_container_size,
            container_type=container.container_type,
            custom_container_type=container.custom_container_type,
            container_count=container.container_count,
            freight_cost=container.freight_cost,
            freight_currency=container.freight_currency,
            port_of_loading=container.port_of_loading,
            port_of_discharge=container.port_of_discharge,
            estimated_departure=container.estimated_departure,
            estimated_arrival=container.estimated_arrival,
            transit_days=container.transit_days,
            loading_method=container.loading_method,
            maximum_product_items=container.maximum_product_items,
            notes=container.notes,
        )
        session.add(new_container)
        session.flush()

        allocations = session.execute(
            select(ShipmentProductAllocation).where(
                ShipmentProductAllocation.shipment_container_id == container.id
            )
        ).scalars().all()
        for allocation in allocations:
            old_item = session.get(QuotationItem, allocation.quotation_item_id)
            target = new_items.get(old_item.line_no) if old_item else None
            if target is None:
                continue
            session.add(
                ShipmentProductAllocation(
                    shipment_container_id=new_container.id,
                    quotation_item_id=target,
                    quantity_per_container=allocation.quantity_per_container,
                    total_allocated_quantity=allocation.total_allocated_quantity,
                    bundles_per_container=allocation.bundles_per_container,
                    pallets_per_container=allocation.pallets_per_container,
                    cases_per_container=allocation.cases_per_container,
                    pieces_per_container=allocation.pieces_per_container,
                    allocated_freight=allocation.allocated_freight,
                    notes=allocation.notes,
                )
            )
    session.flush()


def _copy_item(item: QuotationItem, quotation_id: int) -> QuotationItem:
    return QuotationItem(
        quotation_id=quotation_id,
        line_no=item.line_no,
        sort_order=item.sort_order,
        product_variant_id=item.product_variant_id,
        product_price_id=item.product_price_id,
        is_custom_product=item.is_custom_product,
        custom_description=item.custom_description,
        description_override=item.description_override,
        spec_text_override=item.spec_text_override,
        item_number_snapshot=item.item_number_snapshot,
        size_label=item.size_label,
        depth_in=item.depth_in,
        flute=item.flute,
        board_quality=item.board_quality,
        case_pack=item.case_pack,
        printing_method=item.printing_method,
        num_colours=item.num_colours,
        moq_packs=item.moq_packs,
        price_tier_id=item.price_tier_id,
        pricing_basis=item.pricing_basis,
        quantity_packs=item.quantity_packs,
        quantity_pieces=item.quantity_pieces,
        container_count=item.container_count,
        price_per_pack=item.price_per_pack,
        price_per_piece=item.price_per_piece,
        is_custom_price=item.is_custom_price,
        custom_price_reason=item.custom_price_reason,
        line_discount_pct=item.line_discount_pct,
        line_discount_amount=item.line_discount_amount,
        gross_line_total=item.gross_line_total,
        net_line_total=item.net_line_total,
        unit_cost_per_pack=item.unit_cost_per_pack,
        line_cost_total=item.line_cost_total,
        customer_remarks=item.customer_remarks,
        internal_remarks=item.internal_remarks,
        # Whether a line is offered as included, optional or recommended is part
        # of the offer, not a display setting. Omitting it here let every
        # selectable line fall back to the INCLUDED default, so a revision of a
        # quotation with optional extras silently charged for all of them — the
        # total went up and no customer had chosen anything.
        inclusion=item.inclusion,
    )


def _copy_charge(charge: QuotationCharge, quotation_id: int) -> QuotationCharge:
    return QuotationCharge(
        quotation_id=quotation_id,
        sort_order=charge.sort_order,
        charge_type=charge.charge_type,
        description=charge.description,
        quantity_value=charge.quantity_value,
        rate=charge.rate,
        amount=charge.amount,
        currency=charge.currency,
        exchange_rate=charge.exchange_rate,
        is_taxable=charge.is_taxable,
        is_customer_visible=charge.is_customer_visible,
        # A revision inherits the waiver. A concession already made to the
        # customer does not quietly reappear as a charge because the quotation
        # was reissued; taking it back is a decision, and the revision is
        # editable so it can be made explicitly.
        is_waived=charge.is_waived,
        internal_note=charge.internal_note,
        source=charge.source,
    )


def _copy_term(term: QuotationTerm, quotation_id: int) -> QuotationTerm:
    return QuotationTerm(
        quotation_id=quotation_id,
        term_template_id=term.term_template_id,
        section=term.section,
        title=term.title,
        body_text=term.body_text,
        sort_order=term.sort_order,
        is_customer_visible=term.is_customer_visible,
    )


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

def revisions_for(session: Session, root_quotation_id: int) -> list[Quotation]:
    return list(
        session.execute(
            select(Quotation)
            .where(Quotation.root_quotation_id == root_quotation_id)
            .order_by(Quotation.revision_no)
        ).scalars()
    )


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[dict]]:
    """Diff two snapshots, field by field and line by line."""
    changes: dict[str, list[dict]] = {"header": [], "totals": [], "lines": []}

    for key in ("project", "brand", "distributor", "purchase_order",
                "quote_date", "valid_until", "currency", "customer_notes"):
        if before.get(key) != after.get(key):
            changes["header"].append(
                {"field": key, "before": before.get(key), "after": after.get(key)}
            )

    before_customer = before.get("customer") or {}
    after_customer = after.get("customer") or {}
    for key in before_customer.keys() | after_customer.keys():
        if before_customer.get(key) != after_customer.get(key):
            changes["header"].append(
                {
                    "field": f"customer.{key}",
                    "before": before_customer.get(key),
                    "after": after_customer.get(key),
                }
            )

    before_totals = before.get("totals") or {}
    after_totals = after.get("totals") or {}
    for key in before_totals.keys() | after_totals.keys():
        if before_totals.get(key) != after_totals.get(key):
            changes["totals"].append(
                {
                    "field": key,
                    "before": before_totals.get(key),
                    "after": after_totals.get(key),
                }
            )

    # Lines are matched on item number and board quality rather than on line
    # number: inserting a line at the top would otherwise report every line
    # below it as changed.
    def key_of(line: dict) -> tuple:
        return (line.get("item_number"), line.get("board_quality"), line.get("tier"))

    before_lines = {key_of(line): line for line in before.get("lines", [])}
    after_lines = {key_of(line): line for line in after.get("lines", [])}

    for key in before_lines.keys() - after_lines.keys():
        changes["lines"].append({"change": "removed", "line": before_lines[key]})
    for key in after_lines.keys() - before_lines.keys():
        changes["lines"].append({"change": "added", "line": after_lines[key]})
    for key in before_lines.keys() & after_lines.keys():
        old, new = before_lines[key], after_lines[key]
        differing = {
            field: (old.get(field), new.get(field))
            for field in ("quantity_packs", "price_per_pack", "line_discount_pct",
                          "net_line_total", "container_count")
            if old.get(field) != new.get(field)
        }
        if differing:
            changes["lines"].append(
                {
                    "change": "changed",
                    "line_no": new.get("line_no"),
                    "size_label": new.get("size_label"),
                    "board_quality": new.get("board_quality"),
                    "fields": differing,
                }
            )

    return changes


def has_changes(diff: dict[str, list[dict]]) -> bool:
    return any(diff.values())
