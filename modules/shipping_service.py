"""Container shipping: shipments, container rows, allocations and freight.

The rule this module exists to hold: **freight is counted exactly once.**

Freight is owned by ``shipment_containers.freight_cost`` and summed onto the
shipment. Whether it reaches the customer's total is decided by the shipment's
:class:`~modules.constants.FreightMethod`, and only one of the three produces a
quotation charge:

===================== ============ ======================================
Freight method        Charge row?  Effect on the grand total
===================== ============ ======================================
Added separately      exactly one  included, customer-visible
Included              none         none — the price already contains it
Internal only         none         none — margin and landed cost only
===================== ============ ======================================

The reason *Included* and *Internal only* create no charge at all is that
``calculation_engine.compute_totals`` adds **every** charge to the grand total
regardless of ``is_customer_visible``. An internal-only charge is money the
customer still pays, merely un-itemised — so recording included freight that
way would quietly inflate the quotation.

The derived charge is reconciled to at most one row rather than appended, so
running the sync repeatedly is a no-op.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules import settings_service
from modules.audit_service import record_audit, record_field_changes
from modules.authorization import AuthUser, require, require_edit_quotation
from modules.calculation_engine import ZERO, q_money, q_quantity, to_decimal
from modules.constants import (
    CHARGE_SOURCE_SHIPMENT,
    DEFAULT_CONTAINER_SIZE,
    DEFAULT_CONTAINER_TYPE,
    AuditAction,
    ChargeType,
    ContainerSize,
    ContainerType,
    EntityType,
    FreightMethod,
    Incoterm,
    LoadingMethod,
    Perm,
)
from modules.models import (
    ProductContainerCapacity,
    Quotation,
    QuotationCharge,
    QuotationItem,
    QuotationShipment,
    ShipmentContainer,
    ShipmentProductAllocation,
    ShippingLine,
)

log = logging.getLogger(__name__)


class ShippingError(ValueError):
    """A shipping operation that failed a business rule. Safe to show the user."""


# --------------------------------------------------------------------------- #
# Shipping lines
# --------------------------------------------------------------------------- #

def shipping_lines(session: Session, include_inactive: bool = False) -> list[ShippingLine]:
    stmt = select(ShippingLine).where(ShippingLine.deleted_at.is_(None))
    if not include_inactive:
        stmt = stmt.where(ShippingLine.is_active.is_(True))
    return list(
        session.execute(stmt.order_by(ShippingLine.sort_order, ShippingLine.name)).scalars()
    )


def create_shipping_line(
    session: Session, user: AuthUser, name: str, sort_order: int = 100
) -> ShippingLine:
    require(user, Perm.SHIPPING_LINE_MANAGE)

    cleaned = (name or "").strip()
    if not cleaned:
        raise ShippingError("A shipping line needs a name.")

    clash = session.execute(
        select(ShippingLine).where(func.lower(ShippingLine.name) == cleaned.lower())
    ).scalars().first()
    if clash is not None:
        raise ShippingError(f"{cleaned!r} is already on the list.")

    line = ShippingLine(name=cleaned, sort_order=sort_order, created_by_id=user.id)
    session.add(line)
    session.flush()
    record_audit(
        session, user, AuditAction.SETTINGS_CHANGED, EntityType.SHIPPING_LINE, line.id,
        new_value={"name": cleaned},
    )
    return line


def update_shipping_line(
    session: Session, user: AuthUser, line_id: int, *,
    name: str, is_active: bool, sort_order: int,
) -> ShippingLine:
    require(user, Perm.SHIPPING_LINE_MANAGE)

    line = session.get(ShippingLine, line_id)
    if line is None or line.deleted_at is not None:
        raise ShippingError("That shipping line no longer exists.")

    before = {"name": line.name, "is_active": line.is_active, "sort_order": line.sort_order}
    line.name = (name or "").strip() or line.name
    line.is_active = is_active
    line.sort_order = sort_order
    session.flush()

    record_field_changes(
        session, user, AuditAction.SETTINGS_CHANGED, EntityType.SHIPPING_LINE, line.id,
        before,
        {"name": line.name, "is_active": line.is_active, "sort_order": line.sort_order},
    )
    return line


def delete_shipping_line(session: Session, user: AuthUser, line_id: int) -> ShippingLine:
    """Soft-delete. Existing container rows keep pointing at it, so a carrier
    removed from the list does not blank out historical quotations."""
    require(user, Perm.SHIPPING_LINE_MANAGE)

    line = session.get(ShippingLine, line_id)
    if line is None:
        raise ShippingError("That shipping line no longer exists.")

    line.deleted_at = dt.datetime.now(dt.UTC)
    line.is_active = False
    session.flush()
    record_audit(
        session, user, AuditAction.SETTINGS_CHANGED, EntityType.SHIPPING_LINE, line.id,
        old_value={"deleted_at": None}, new_value={"deleted_at": "set"},
    )
    return line


# --------------------------------------------------------------------------- #
# Shipments
# --------------------------------------------------------------------------- #

def get_shipment(session: Session, quotation_id: int) -> QuotationShipment | None:
    return session.execute(
        select(QuotationShipment).where(
            QuotationShipment.quotation_id == quotation_id
        )
    ).scalar_one_or_none()


def ensure_shipment(
    session: Session, user: AuthUser, quotation: Quotation
) -> QuotationShipment:
    """Fetch the quotation's shipment, creating it on first use.

    Defaults follow the reference price list: FOB, 40' high-cube dry
    containers, floor loaded, freight included.
    """
    require(user, Perm.SHIPMENT_EDIT)
    require_edit_quotation(user, quotation)

    existing = get_shipment(session, quotation.id)
    if existing is not None:
        return existing

    shipment = QuotationShipment(
        quotation_id=quotation.id,
        incoterm=settings_service.default_incoterm(session),
        incoterm_place=settings_service.default_incoterm_place(session),
        origin_country=settings_service.default_origin_country(session),
        port_of_loading=settings_service.default_port_of_loading(session),
        freight_method=FreightMethod.INCLUDED,
        freight_currency=quotation.currency,
        loading_method=settings_service.default_loading_method(session),
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    session.add(shipment)
    session.flush()
    record_audit(
        session, user, AuditAction.SHIPMENT_EDITED, EntityType.QUOTATION_SHIPMENT,
        shipment.id, new_value={"quotation": quotation.quote_number, "created": True},
    )
    return shipment


_SHIPMENT_FIELDS = (
    "incoterm", "incoterm_place", "origin_country", "port_of_loading",
    "port_of_discharge", "final_destination", "freight_method", "freight_currency",
    "freight_taxable", "loading_method", "shipping_notes", "show_on_document",
    "customer_visible_freight",
)


def update_shipment(
    session: Session, user: AuthUser, quotation: Quotation, **fields
) -> QuotationShipment:
    require(user, Perm.SHIPMENT_EDIT)
    require_edit_quotation(user, quotation)

    unknown = set(fields) - set(_SHIPMENT_FIELDS)
    if unknown:
        raise ShippingError(f"Cannot set {', '.join(sorted(unknown))} on a shipment.")

    shipment = ensure_shipment(session, user, quotation)

    # Changing how freight is charged moves money on the quotation, so it needs
    # the freight permission rather than the general shipping one.
    if "freight_method" in fields and fields["freight_method"] != shipment.freight_method:
        require(user, Perm.SHIPMENT_EDIT_FREIGHT)

    before = {name: getattr(shipment, name) for name in fields}
    for name, value in fields.items():
        setattr(shipment, name, value)
    shipment.updated_by_id = user.id
    session.flush()

    sync_freight(session, user, quotation)
    record_field_changes(
        session, user, AuditAction.SHIPMENT_EDITED, EntityType.QUOTATION_SHIPMENT,
        shipment.id, before, {name: getattr(shipment, name) for name in fields},
    )
    return shipment


def remove_shipment(session: Session, user: AuthUser, quotation: Quotation) -> None:
    """Delete the shipment and the freight charge derived from it."""
    require(user, Perm.SHIPMENT_EDIT)
    require_edit_quotation(user, quotation)

    shipment = get_shipment(session, quotation.id)
    if shipment is None:
        return

    session.delete(shipment)
    session.flush()
    _remove_derived_charge(session, quotation)

    from modules.quotation_service import recompute_totals

    recompute_totals(session, quotation)
    record_audit(
        session, user, AuditAction.SHIPMENT_EDITED, EntityType.QUOTATION_SHIPMENT,
        None, old_value={"quotation": quotation.quote_number}, new_value={"removed": True},
    )


# --------------------------------------------------------------------------- #
# Container rows
# --------------------------------------------------------------------------- #

def add_container(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    shipping_line_id: int | None = None,
    custom_shipping_line: str | None = None,
    container_size: ContainerSize = DEFAULT_CONTAINER_SIZE,
    custom_container_size: str | None = None,
    container_type: ContainerType = DEFAULT_CONTAINER_TYPE,
    custom_container_type: str | None = None,
    container_count: Decimal = Decimal("1"),
    freight_cost: Decimal = ZERO,
    freight_currency: str | None = None,
    port_of_loading: str | None = None,
    port_of_discharge: str | None = None,
    estimated_departure: dt.date | None = None,
    estimated_arrival: dt.date | None = None,
    transit_days: int | None = None,
    loading_method: LoadingMethod | None = None,
    maximum_product_items: int | None = None,
    notes: str | None = None,
) -> ShipmentContainer:
    require(user, Perm.SHIPMENT_EDIT)
    require_edit_quotation(user, quotation)

    if to_decimal(container_count) <= ZERO:
        raise ShippingError("A container row needs a count of at least one.")
    if to_decimal(freight_cost) > ZERO:
        require(user, Perm.SHIPMENT_EDIT_FREIGHT)

    shipment = ensure_shipment(session, user, quotation)

    if estimated_departure and estimated_arrival and estimated_arrival < estimated_departure:
        raise ShippingError("The arrival date is before the departure date.")

    # Derive whichever of transit/arrival was left blank, rather than making the
    # operator compute it — but never overwrite one they supplied.
    if transit_days is None and estimated_departure and estimated_arrival:
        transit_days = (estimated_arrival - estimated_departure).days
    elif estimated_arrival is None and estimated_departure and transit_days:
        estimated_arrival = estimated_departure + dt.timedelta(days=int(transit_days))

    container = ShipmentContainer(
        quotation_shipment_id=shipment.id,
        sort_order=_next_container_order(session, shipment.id),
        shipping_line_id=shipping_line_id,
        custom_shipping_line=(custom_shipping_line or None) if not shipping_line_id else None,
        container_size=container_size,
        custom_container_size=custom_container_size,
        container_type=container_type,
        custom_container_type=custom_container_type,
        container_count=q_quantity(to_decimal(container_count)),
        freight_cost=q_money(to_decimal(freight_cost)),
        freight_currency=(freight_currency or shipment.freight_currency).upper(),
        port_of_loading=port_of_loading or shipment.port_of_loading,
        port_of_discharge=port_of_discharge or shipment.port_of_discharge,
        estimated_departure=estimated_departure,
        estimated_arrival=estimated_arrival,
        transit_days=transit_days,
        loading_method=loading_method or shipment.loading_method,
        maximum_product_items=maximum_product_items,
        notes=notes,
    )
    session.add(container)
    session.flush()

    sync_freight(session, user, quotation)
    record_audit(
        session, user, AuditAction.CONTAINER_ADDED, EntityType.SHIPMENT_CONTAINER,
        container.id,
        new_value={
            "quotation": quotation.quote_number,
            "size": container.size_label,
            "type": container.type_label,
            "count": container.container_count,
            "carrier": container.carrier_name,
        },
    )
    return container


_CONTAINER_FIELDS = (
    "shipping_line_id", "custom_shipping_line", "container_size",
    "custom_container_size", "container_type", "custom_container_type",
    "container_count", "freight_cost", "freight_currency", "port_of_loading",
    "port_of_discharge", "estimated_departure", "estimated_arrival",
    "transit_days", "loading_method", "maximum_product_items", "notes", "sort_order",
)


def update_container(
    session: Session, user: AuthUser, quotation: Quotation, container_id: int, **fields
) -> ShipmentContainer:
    require(user, Perm.SHIPMENT_EDIT)
    require_edit_quotation(user, quotation)

    unknown = set(fields) - set(_CONTAINER_FIELDS)
    if unknown:
        raise ShippingError(f"Cannot set {', '.join(sorted(unknown))} on a container.")
    if "freight_cost" in fields:
        require(user, Perm.SHIPMENT_EDIT_FREIGHT)

    container = _owned_container(session, quotation, container_id)

    before = {name: getattr(container, name) for name in fields}
    for name, value in fields.items():
        setattr(container, name, value)
    if container.container_count is not None:
        container.container_count = q_quantity(to_decimal(container.container_count))
    container.freight_cost = q_money(to_decimal(container.freight_cost))
    session.flush()

    recalculate_allocations(session, container)
    sync_freight(session, user, quotation)
    record_field_changes(
        session, user, AuditAction.SHIPMENT_EDITED, EntityType.SHIPMENT_CONTAINER,
        container.id, before, {name: getattr(container, name) for name in fields},
    )
    return container


def duplicate_container(
    session: Session, user: AuthUser, quotation: Quotation, container_id: int
) -> ShipmentContainer:
    require(user, Perm.SHIPMENT_EDIT)
    require_edit_quotation(user, quotation)

    source = _owned_container(session, quotation, container_id)
    copy = ShipmentContainer(
        quotation_shipment_id=source.quotation_shipment_id,
        sort_order=source.sort_order + 1,
        shipping_line_id=source.shipping_line_id,
        custom_shipping_line=source.custom_shipping_line,
        container_size=source.container_size,
        custom_container_size=source.custom_container_size,
        container_type=source.container_type,
        custom_container_type=source.custom_container_type,
        container_count=source.container_count,
        freight_cost=source.freight_cost,
        freight_currency=source.freight_currency,
        port_of_loading=source.port_of_loading,
        port_of_discharge=source.port_of_discharge,
        estimated_departure=source.estimated_departure,
        estimated_arrival=source.estimated_arrival,
        transit_days=source.transit_days,
        loading_method=source.loading_method,
        maximum_product_items=source.maximum_product_items,
        notes=source.notes,
    )
    session.add(copy)
    session.flush()

    sync_freight(session, user, quotation)
    record_audit(
        session, user, AuditAction.CONTAINER_ADDED, EntityType.SHIPMENT_CONTAINER,
        copy.id, new_value={"duplicated_from": container_id},
    )
    return copy


def remove_container(
    session: Session, user: AuthUser, quotation: Quotation, container_id: int
) -> None:
    require(user, Perm.SHIPMENT_EDIT)
    require_edit_quotation(user, quotation)

    container = _owned_container(session, quotation, container_id)
    snapshot = {
        "size": container.size_label,
        "count": container.container_count,
        "freight": container.freight_cost,
    }
    session.delete(container)
    session.flush()

    sync_freight(session, user, quotation)
    record_audit(
        session, user, AuditAction.CONTAINER_REMOVED, EntityType.SHIPMENT_CONTAINER,
        None, old_value=snapshot,
    )


def _next_container_order(session: Session, shipment_id: int) -> int:
    """Next sort order, queried so concurrent additions do not collide."""
    highest = session.execute(
        select(func.max(ShipmentContainer.sort_order)).where(
            ShipmentContainer.quotation_shipment_id == shipment_id
        )
    ).scalar()
    return (highest or 0) + 1


def _owned_container(
    session: Session, quotation: Quotation, container_id: int
) -> ShipmentContainer:
    container = session.get(ShipmentContainer, container_id)
    shipment = get_shipment(session, quotation.id)
    if container is None or shipment is None or container.quotation_shipment_id != shipment.id:
        raise ShippingError("That container is not part of this quotation.")
    return container


def total_containers(session: Session, quotation_id: int) -> Decimal:
    """Containers across every row, for the pricing-tier check.

    Queried rather than summed off the relationship so a caller that has just
    written a row gets the current figure.
    """
    total = session.execute(
        select(func.sum(ShipmentContainer.container_count))
        .join(
            QuotationShipment,
            ShipmentContainer.quotation_shipment_id == QuotationShipment.id,
        )
        .where(QuotationShipment.quotation_id == quotation_id)
    ).scalar()
    return to_decimal(total) if total is not None else ZERO


# --------------------------------------------------------------------------- #
# Product allocation
# --------------------------------------------------------------------------- #

def container_capacity(
    session: Session,
    product_id: int,
    container_size: ContainerSize,
    container_type: ContainerType,
) -> ProductContainerCapacity | None:
    """Capacity for a product in a given container, if it has been recorded.

    Falls back to the same size in a Dry container before giving up, because the
    source data is published per size and most types share a footprint.
    """
    exact = session.execute(
        select(ProductContainerCapacity).where(
            ProductContainerCapacity.product_id == product_id,
            ProductContainerCapacity.container_size == container_size,
            ProductContainerCapacity.container_type == container_type,
        )
    ).scalar_one_or_none()
    if exact is not None:
        return exact
    return session.execute(
        select(ProductContainerCapacity).where(
            ProductContainerCapacity.product_id == product_id,
            ProductContainerCapacity.container_size == container_size,
            ProductContainerCapacity.container_type == ContainerType.DRY,
        )
    ).scalar_one_or_none()


def container_capacity_for_product(
    session: Session, product_id: int
) -> ProductContainerCapacity | None:
    """Whatever capacity is on file for a product, without naming a container.

    For the line editor, which is showing the operator a figure before any
    container has been chosen. Prefers the configured default container, then
    the largest recorded capacity, so a catalogue holding only 20 ft rows still
    shows something rather than nothing.
    """
    from modules import settings_service

    preferred = container_capacity(
        session,
        product_id,
        settings_service.default_container_size(session),
        settings_service.default_container_type(session),
    )
    if preferred is not None:
        return preferred

    return session.execute(
        select(ProductContainerCapacity)
        .where(ProductContainerCapacity.product_id == product_id)
        .order_by(ProductContainerCapacity.bundles_per_container.desc())
    ).scalars().first()


def allocate_product(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    container_id: int,
    quotation_item_id: int,
    *,
    quantity_per_container: Decimal | None = None,
    bundles_per_container: Decimal | None = None,
    pallets_per_container: Decimal | None = None,
    notes: str | None = None,
) -> ShipmentProductAllocation:
    """Assign a quotation line to a container.

    ``quantity_per_container`` is derived from the recorded container capacity
    when it is left blank and capacity exists; otherwise it must be supplied.
    Nothing is guessed.
    """
    require(user, Perm.SHIPMENT_EDIT)
    require_edit_quotation(user, quotation)

    container = _owned_container(session, quotation, container_id)
    item = session.get(QuotationItem, quotation_item_id)
    if item is None or item.quotation_id != quotation.id:
        raise ShippingError("That line is not part of this quotation.")

    capacity = None
    if item.product_variant_id is not None:
        from modules.repositories import get_variant

        variant = get_variant(session, item.product_variant_id)
        if variant is not None:
            capacity = container_capacity(
                session, variant.product_id,
                container.container_size, container.container_type,
            )

    if bundles_per_container is None and capacity is not None:
        bundles_per_container = capacity.bundles_per_container
    if quantity_per_container is None:
        if capacity is None:
            raise ShippingError(
                "No container capacity is recorded for this product, so the quantity "
                "per container cannot be derived. Enter it, or import the "
                "bundles-per-container workbook."
            )
        quantity_per_container = capacity.bundles_per_container

    existing = session.execute(
        select(ShipmentProductAllocation).where(
            ShipmentProductAllocation.shipment_container_id == container.id,
            ShipmentProductAllocation.quotation_item_id == item.id,
        )
    ).scalar_one_or_none()

    allocation = existing or ShipmentProductAllocation(
        shipment_container_id=container.id, quotation_item_id=item.id
    )
    allocation.quantity_per_container = q_quantity(to_decimal(quantity_per_container))
    allocation.bundles_per_container = (
        q_quantity(to_decimal(bundles_per_container))
        if bundles_per_container is not None else None
    )
    allocation.pallets_per_container = (
        q_quantity(to_decimal(pallets_per_container))
        if pallets_per_container is not None
        else (capacity.pallets_per_container if capacity else None)
    )
    allocation.notes = notes
    if existing is None:
        session.add(allocation)
    session.flush()

    _recalculate_allocation(session, allocation, container, item, capacity)
    session.flush()

    record_audit(
        session, user, AuditAction.SHIPMENT_EDITED, EntityType.SHIPMENT_CONTAINER,
        container.id,
        new_value={
            "line": item.line_no,
            "quantity_per_container": allocation.quantity_per_container,
            "total": allocation.total_allocated_quantity,
        },
    )
    return allocation


def _recalculate_allocation(
    session: Session,
    allocation: ShipmentProductAllocation,
    container: ShipmentContainer,
    item: QuotationItem,
    capacity: ProductContainerCapacity | None,
) -> None:
    """Derived quantities for one allocation.

    ``Total = quantity per container x number of containers``.

    Pieces and cases stay ``None`` when the bundle size is unknown — the source
    workbook does not say how many units a bundle holds, and a guessed piece
    count would flow into a customer document.
    """
    count = to_decimal(container.container_count)
    allocation.total_allocated_quantity = q_quantity(
        to_decimal(allocation.quantity_per_container) * count
    )

    units_per_bundle = (
        capacity.product.units_per_bundle
        if capacity is not None and capacity.product is not None
        else None
    )
    if allocation.bundles_per_container is not None and units_per_bundle is not None:
        pieces = to_decimal(allocation.bundles_per_container) * to_decimal(units_per_bundle)
        allocation.pieces_per_container = q_quantity(pieces)
        if item.case_pack:
            allocation.cases_per_container = q_quantity(
                pieces / Decimal(item.case_pack)
            )
        else:
            allocation.cases_per_container = None
    else:
        allocation.pieces_per_container = None
        allocation.cases_per_container = None


def _allocations_for(
    session: Session, container_id: int
) -> list[ShipmentProductAllocation]:
    """Allocations on a container, queried rather than read off the relationship.

    ``expire_on_commit`` is off, so a collection loaded before an allocation was
    written stays empty for the rest of the session. Reading it here silently
    skipped the recalculation, leaving the total allocated quantity stale after
    the container count changed.
    """
    return list(
        session.execute(
            select(ShipmentProductAllocation).where(
                ShipmentProductAllocation.shipment_container_id == container_id
            )
        ).scalars()
    )


def recalculate_allocations(session: Session, container: ShipmentContainer) -> None:
    """Refresh every allocation on a container after its count or size changed."""
    from modules.repositories import get_variant

    for allocation in _allocations_for(session, container.id):
        item = session.get(QuotationItem, allocation.quotation_item_id)
        if item is None:
            continue
        capacity = None
        if item.product_variant_id is not None:
            variant = get_variant(session, item.product_variant_id)
            if variant is not None:
                capacity = container_capacity(
                    session, variant.product_id,
                    container.container_size, container.container_type,
                )
        _recalculate_allocation(session, allocation, container, item, capacity)
    session.flush()


def remove_allocation(
    session: Session, user: AuthUser, quotation: Quotation, allocation_id: int
) -> None:
    require(user, Perm.SHIPMENT_EDIT)
    require_edit_quotation(user, quotation)

    allocation = session.get(ShipmentProductAllocation, allocation_id)
    if allocation is None:
        raise ShippingError("That allocation no longer exists.")
    _owned_container(session, quotation, allocation.shipment_container_id)

    session.delete(allocation)
    session.flush()


# --------------------------------------------------------------------------- #
# Freight
# --------------------------------------------------------------------------- #

def freight_per_container(session: Session, quotation_id: int) -> Decimal | None:
    """``Total freight / total containers``. ``None`` when there are none."""
    shipment = get_shipment(session, quotation_id)
    if shipment is None:
        return None
    containers = total_containers(session, quotation_id)
    if containers <= ZERO:
        return None
    return q_money(to_decimal(shipment.total_freight) / containers)


def apportion_freight(session: Session, quotation_id: int) -> None:
    """Spread each container's freight across the products travelling in it.

    ``Allocated freight = container share x container freight``, where the share
    is that product's quantity over the container's total allocated quantity.
    Internal only — it feeds landed cost and never a customer document.
    """
    shipment = get_shipment(session, quotation_id)
    if shipment is None:
        return

    # Queried, for the same staleness reason as sync_freight.
    containers = session.execute(
        select(ShipmentContainer).where(
            ShipmentContainer.quotation_shipment_id == shipment.id
        )
    ).scalars().all()
    for container in containers:
        allocations = _allocations_for(session, container.id)
        total = sum(
            (to_decimal(a.quantity_per_container) for a in allocations), ZERO
        )
        freight = to_decimal(container.freight_cost) * to_decimal(container.container_count)
        for allocation in allocations:
            if total <= ZERO:
                allocation.allocated_freight = None
                continue
            share = to_decimal(allocation.quantity_per_container) / total
            allocation.allocated_freight = q_money(freight * share)
    session.flush()


def _derived_charge(session: Session, quotation_id: int) -> QuotationCharge | None:
    return session.execute(
        select(QuotationCharge).where(
            QuotationCharge.quotation_id == quotation_id,
            QuotationCharge.source == CHARGE_SOURCE_SHIPMENT,
        )
    ).scalars().first()


def _remove_derived_charge(session: Session, quotation: Quotation) -> None:
    charge = _derived_charge(session, quotation.id)
    if charge is not None:
        session.delete(charge)
        session.flush()


def manual_freight_charges(session: Session, quotation_id: int) -> list[QuotationCharge]:
    """Freight charges someone entered by hand, alongside a shipment.

    Surfaced as a warning rather than merged: two freight figures on one
    quotation is a decision for the operator, not something to resolve silently
    in either direction.
    """
    return list(
        session.execute(
            select(QuotationCharge).where(
                QuotationCharge.quotation_id == quotation_id,
                QuotationCharge.charge_type == ChargeType.FREIGHT,
                QuotationCharge.source != CHARGE_SOURCE_SHIPMENT,
            )
        ).scalars()
    )


def sync_freight(
    session: Session, user: AuthUser, quotation: Quotation
) -> QuotationCharge | None:
    """Bring the shipment's freight and the quotation's charges into agreement.

    Reconciles to **at most one** charge marked ``source='shipment'``:

    * recomputes ``total_freight`` from the container rows;
    * creates, updates or deletes the single derived charge according to the
      freight method;
    * re-apportions freight across allocations for landed cost.

    Idempotent. Calling it twice changes nothing the second time, which is what
    makes double-counting structurally impossible rather than merely avoided.
    """
    shipment = get_shipment(session, quotation.id)
    if shipment is None:
        _remove_derived_charge(session, quotation)
        return None

    # Container freight is a per-container rate, so the total multiplies by the
    # number of containers on each row.
    #
    # Queried rather than read off ``shipment.containers``: the session factory
    # sets expire_on_commit=False, so a collection loaded during an earlier call
    # in the same session does not include rows written since. Reading it here
    # silently under-counted the freight — the third container row added to a
    # shipment was omitted from the total.
    total = sum(
        (
            q_money(to_decimal(freight) * to_decimal(count))
            for freight, count in session.execute(
                select(
                    ShipmentContainer.freight_cost,
                    ShipmentContainer.container_count,
                ).where(ShipmentContainer.quotation_shipment_id == shipment.id)
            ).all()
        ),
        ZERO,
    )
    previous_total = shipment.total_freight
    shipment.total_freight = total
    session.flush()

    charge = _derived_charge(session, quotation.id)

    if shipment.freight_method is FreightMethod.ADDED_SEPARATELY and total > ZERO:
        description = _freight_description(shipment)
        if charge is None:
            charge = QuotationCharge(
                quotation_id=quotation.id,
                sort_order=999,
                charge_type=ChargeType.FREIGHT,
                source=CHARGE_SOURCE_SHIPMENT,
            )
            session.add(charge)
        charge.description = description
        charge.quantity_value = Decimal("1")
        charge.rate = total
        charge.amount = total
        charge.currency = shipment.freight_currency
        charge.exchange_rate = Decimal("1")
        charge.is_taxable = shipment.freight_taxable
        charge.is_customer_visible = True
    else:
        # Included and internal-only freight must not become a charge: every
        # charge counts toward the grand total regardless of visibility.
        if charge is not None:
            session.delete(charge)
            charge = None

    session.flush()
    apportion_freight(session, quotation.id)

    from modules.quotation_service import recompute_totals

    recompute_totals(session, quotation)

    if previous_total != total:
        record_audit(
            session, user, AuditAction.FREIGHT_CHANGED,
            EntityType.QUOTATION_SHIPMENT, shipment.id,
            old_value={"total_freight": previous_total},
            new_value={
                "total_freight": total,
                "method": shipment.freight_method.value,
                "charged_to_customer": charge is not None,
            },
        )
        log.info(
            "Freight for %s: %s (%s)",
            quotation.quote_number, total, shipment.freight_method.value,
        )
    return charge


def _freight_description(shipment: QuotationShipment) -> str:
    bits = ["Ocean freight"]
    if shipment.port_of_loading and shipment.port_of_discharge:
        bits.append(f"{shipment.port_of_loading} to {shipment.port_of_discharge}")
    count = shipment.total_containers
    if count:  # relationship is fine here — description is cosmetic
        bits.append(f"{count:g} container(s)")
    return " — ".join(bits)


def landed_freight(session: Session, user: AuthUser, quotation_id: int) -> Decimal | None:
    """Freight to include in landed cost, gated on the freight permission.

    ``None`` for *Added separately*, because there the customer is already
    paying it as a charge — folding it into cost as well would understate the
    margin.
    """
    require(user, Perm.SHIPMENT_VIEW_FREIGHT)

    shipment = get_shipment(session, quotation_id)
    if shipment is None:
        return None
    if shipment.freight_method is FreightMethod.ADDED_SEPARATELY:
        return None
    return shipment.total_freight
