"""Aggregates for the dashboard and the reports.

Every query starts from :func:`base_query`, which applies
``authorization.quotation_scope_filter``. A salesperson's reports therefore
cover only their own quotations by construction — the scope is a WHERE clause
on the aggregate, not a filter applied to rows afterwards, so a total can never
include a quotation the viewer may not see.

Returns pandas DataFrames with stable columns even when empty. Empty results
are routine here — a month with no quotations, a customer with no history — and
pandas 3.0 changes how empty frames behave, so every builder goes through
:func:`_frame`, which guarantees the shape.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd
from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.orm import Session

from modules.authorization import AuthUser, quotation_scope_filter
from modules.repositories import LIKE_ESCAPE, _like
from modules.constants import QuotationStatus
from modules.models import (
    Approval,
    Customer,
    CustomerResponseLog,
    PriceTier,
    Product,
    ProductPrice,
    ProductVariant,
    Quotation,
    QuotationItem,
    QuotationShipment,
    ShipmentContainer,
    ShippingLine,
    User,
)

log = logging.getLogger(__name__)

ZERO = Decimal("0")

#: Statuses that represent a quotation the customer actually received.
SENT_STATUSES = (
    QuotationStatus.SENT_TO_CUSTOMER,
    QuotationStatus.ACCEPTED,
    QuotationStatus.LOST,
    QuotationStatus.EXPIRED,
)


@dataclass(frozen=True)
class ReportFilters:
    """Shared filter set for the dashboard and every report."""

    date_from: dt.date | None = None
    date_to: dt.date | None = None
    customer_ids: tuple[int, ...] = ()
    sales_user_ids: tuple[int, ...] = ()
    statuses: tuple[QuotationStatus, ...] = ()
    currency: str | None = None
    tier_codes: tuple[str, ...] = ()
    product_variant_ids: tuple[int, ...] = ()
    current_revision_only: bool = True

    # --- container shipping ------------------------------------------- #
    shipping_line_ids: tuple[int, ...] = ()
    container_sizes: tuple[str, ...] = ()
    container_types: tuple[str, ...] = ()
    freight_methods: tuple[str, ...] = ()
    port_of_loading: str | None = None
    port_of_discharge: str | None = None
    min_containers: Decimal | None = None
    max_transit_days: int | None = None

    def describe(self) -> str:
        parts: list[str] = []
        if self.date_from or self.date_to:
            parts.append(
                f"{self.date_from or 'start'} to {self.date_to or 'today'}"
            )
        if self.customer_ids:
            parts.append(f"{len(self.customer_ids)} customer(s)")
        if self.sales_user_ids:
            parts.append(f"{len(self.sales_user_ids)} employee(s)")
        if self.statuses:
            parts.append(f"{len(self.statuses)} status(es)")
        if self.currency:
            parts.append(self.currency)
        if self.shipping_line_ids:
            parts.append(f"{len(self.shipping_line_ids)} carrier(s)")
        if self.container_sizes:
            parts.append(f"{len(self.container_sizes)} container size(s)")
        if self.freight_methods:
            parts.append(f"{len(self.freight_methods)} freight method(s)")
        if self.port_of_loading:
            parts.append(f"from {self.port_of_loading}")
        if self.port_of_discharge:
            parts.append(f"to {self.port_of_discharge}")
        if self.min_containers:
            parts.append(f"{self.min_containers:g}+ containers")
        if self.max_transit_days:
            parts.append(f"transit under {self.max_transit_days} days")
        return " · ".join(parts) or "no filters"


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    """A DataFrame with the given columns, correct even when there are no rows.

    Without this an empty result yields a frame with no columns, and every
    downstream ``df["Value"]`` becomes a KeyError that only appears when a
    filter happens to match nothing.
    """
    if not rows:
        return pd.DataFrame({c: pd.Series([], dtype="object") for c in columns})
    return pd.DataFrame(rows, columns=columns)


# --------------------------------------------------------------------------- #
# Base query
# --------------------------------------------------------------------------- #

def base_query(user: AuthUser, filters: ReportFilters | None = None) -> Select:
    """A SELECT over quotations the user may see, with the filters applied."""
    filters = filters or ReportFilters()

    stmt = select(Quotation).where(
        Quotation.deleted_at.is_(None),
        quotation_scope_filter(user),
    )
    if filters.current_revision_only:
        stmt = stmt.where(Quotation.is_current_revision.is_(True))
    if filters.date_from:
        stmt = stmt.where(Quotation.quote_date >= filters.date_from)
    if filters.date_to:
        stmt = stmt.where(Quotation.quote_date <= filters.date_to)
    if filters.customer_ids:
        stmt = stmt.where(Quotation.customer_id.in_(filters.customer_ids))
    if filters.sales_user_ids:
        stmt = stmt.where(Quotation.sales_user_id.in_(filters.sales_user_ids))
    if filters.statuses:
        stmt = stmt.where(Quotation.status.in_(filters.statuses))
    if filters.currency:
        stmt = stmt.where(Quotation.currency == filters.currency)
    stmt = _apply_shipping_filters(stmt, filters)
    if filters.tier_codes or filters.product_variant_ids:
        line = select(QuotationItem.quotation_id)
        if filters.product_variant_ids:
            line = line.where(
                QuotationItem.product_variant_id.in_(filters.product_variant_ids)
            )
        if filters.tier_codes:
            line = line.join(
                PriceTier, QuotationItem.price_tier_id == PriceTier.id
            ).where(PriceTier.code.in_(filters.tier_codes))
        stmt = stmt.where(Quotation.id.in_(line))
    return stmt


def _apply_shipping_filters(stmt: Select, filters: ReportFilters) -> Select:
    """Restrict to quotations whose shipment matches the shipping criteria.

    Applied as an ``IN`` over quotation ids rather than a join, so a quotation
    with several container rows is not multiplied across the result and its
    value counted more than once.
    """
    container_conditions = []
    if filters.shipping_line_ids:
        container_conditions.append(
            ShipmentContainer.shipping_line_id.in_(filters.shipping_line_ids)
        )
    if filters.container_sizes:
        container_conditions.append(
            ShipmentContainer.container_size.in_(filters.container_sizes)
        )
    if filters.container_types:
        container_conditions.append(
            ShipmentContainer.container_type.in_(filters.container_types)
        )
    if filters.max_transit_days:
        container_conditions.append(
            ShipmentContainer.transit_days <= filters.max_transit_days
        )

    shipment_conditions = []
    if filters.freight_methods:
        shipment_conditions.append(
            QuotationShipment.freight_method.in_(filters.freight_methods)
        )
    if filters.port_of_loading:
        shipment_conditions.append(
            QuotationShipment.port_of_loading.ilike(
                _like(filters.port_of_loading), escape=LIKE_ESCAPE
            )
        )
    if filters.port_of_discharge:
        shipment_conditions.append(
            QuotationShipment.port_of_discharge.ilike(
                _like(filters.port_of_discharge), escape=LIKE_ESCAPE
            )
        )

    if container_conditions:
        stmt = stmt.where(
            Quotation.id.in_(
                select(QuotationShipment.quotation_id)
                .join(
                    ShipmentContainer,
                    ShipmentContainer.quotation_shipment_id == QuotationShipment.id,
                )
                .where(*container_conditions)
            )
        )
    if shipment_conditions:
        stmt = stmt.where(
            Quotation.id.in_(
                select(QuotationShipment.quotation_id).where(*shipment_conditions)
            )
        )
    if filters.min_containers is not None:
        stmt = stmt.where(
            Quotation.id.in_(
                select(QuotationShipment.quotation_id)
                .join(
                    ShipmentContainer,
                    ShipmentContainer.quotation_shipment_id == QuotationShipment.id,
                )
                .group_by(QuotationShipment.quotation_id)
                .having(
                    func.sum(ShipmentContainer.container_count)
                    >= filters.min_containers
                )
            )
        )
    return stmt


def _scoped_ids(session: Session, user: AuthUser, filters: ReportFilters) -> Select:
    """The id set the aggregates join against."""
    return base_query(user, filters).with_only_columns(Quotation.id)


# --------------------------------------------------------------------------- #
# Headline figures
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Headlines:
    counts: dict[str, int] = field(default_factory=dict)
    total_quoted: Decimal = ZERO
    accepted_value: Decimal = ZERO
    sent_count: int = 0
    accepted_count: int = 0
    lost_count: int = 0
    expiring_7: int = 0
    expiring_30: int = 0
    currencies: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def conversion_rate(self) -> Decimal | None:
        """Accepted as a share of quotations the customer actually received.

        ``None`` rather than zero when nothing has been sent yet — a conversion
        rate of 0% would read as "we lose everything" rather than "we have not
        sent anything".
        """
        decided = self.accepted_count + self.lost_count
        if decided == 0:
            return None
        return (
            Decimal(self.accepted_count) / Decimal(decided) * Decimal(100)
        ).quantize(Decimal("0.1"))

    @property
    def mixed_currency(self) -> bool:
        return len(self.currencies) > 1


def headlines(
    session: Session,
    user: AuthUser,
    filters: ReportFilters | None = None,
    today: dt.date | None = None,
) -> Headlines:
    filters = filters or ReportFilters()
    today = today or dt.date.today()
    ids = _scoped_ids(session, user, filters).subquery()

    rows = session.execute(
        select(Quotation.status, func.count(Quotation.id), func.sum(Quotation.grand_total))
        .where(Quotation.id.in_(select(ids.c.id)))
        .group_by(Quotation.status)
    ).all()

    counts = {status.value: 0 for status in QuotationStatus}
    total_quoted = ZERO
    accepted_value = ZERO
    accepted_count = lost_count = sent_count = 0

    for status, count, value in rows:
        counts[status.value] = count
        total_quoted += value or ZERO
        if status is QuotationStatus.ACCEPTED:
            accepted_value += value or ZERO
            accepted_count = count
        elif status is QuotationStatus.LOST:
            lost_count = count
        if status in SENT_STATUSES:
            sent_count += count

    def expiring_within(days: int) -> int:
        return session.execute(
            select(func.count(Quotation.id)).where(
                Quotation.id.in_(select(ids.c.id)),
                Quotation.status.in_(
                    [QuotationStatus.APPROVED, QuotationStatus.SENT_TO_CUSTOMER]
                ),
                Quotation.valid_until.is_not(None),
                Quotation.valid_until >= today,
                Quotation.valid_until <= today + dt.timedelta(days=days),
            )
        ).scalar_one()

    currencies = tuple(
        sorted(
            c for c in session.execute(
                select(Quotation.currency)
                .where(Quotation.id.in_(select(ids.c.id)))
                .distinct()
            ).scalars()
        )
    )

    return Headlines(
        counts=counts,
        total_quoted=total_quoted,
        accepted_value=accepted_value,
        sent_count=sent_count,
        accepted_count=accepted_count,
        lost_count=lost_count,
        expiring_7=expiring_within(7),
        expiring_30=expiring_within(30),
        currencies=currencies,
    )


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #

def _month_expr():  # noqa: ANN202
    """Year-month as text, portable across SQLite and PostgreSQL.

    ``strftime`` does not exist on PostgreSQL and ``to_char`` does not exist on
    SQLite, so the grouping is done in Python instead of committing to either.
    """
    return Quotation.quote_date


def value_by_month(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(Quotation.quote_date, Quotation.status, Quotation.grand_total)
        .where(Quotation.id.in_(select(ids.c.id)))
    ).all()

    buckets: dict[str, dict[str, Decimal]] = {}
    for quote_date, status, total in rows:
        key = f"{quote_date:%Y-%m}"
        bucket = buckets.setdefault(key, {"Quoted": ZERO, "Accepted": ZERO})
        bucket["Quoted"] += total or ZERO
        if status is QuotationStatus.ACCEPTED:
            bucket["Accepted"] += total or ZERO

    return _frame(
        [
            {"Month": month, "Quoted": float(v["Quoted"]), "Accepted": float(v["Accepted"])}
            for month, v in sorted(buckets.items())
        ],
        ["Month", "Quoted", "Accepted"],
    )


def count_by_status(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    from modules.constants import STATUS_DISPLAY_NAMES

    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(Quotation.status, func.count(Quotation.id))
        .where(Quotation.id.in_(select(ids.c.id)))
        .group_by(Quotation.status)
    ).all()
    return _frame(
        [
            {"Status": STATUS_DISPLAY_NAMES.get(s, str(s)), "Count": c}
            for s, c in rows
        ],
        ["Status", "Count"],
    )


def accepted_versus_lost(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(Quotation.status, func.count(Quotation.id), func.sum(Quotation.grand_total))
        .where(
            Quotation.id.in_(select(ids.c.id)),
            Quotation.status.in_([QuotationStatus.ACCEPTED, QuotationStatus.LOST]),
        )
        .group_by(Quotation.status)
    ).all()
    return _frame(
        [
            {
                "Outcome": "Accepted" if s is QuotationStatus.ACCEPTED else "Lost",
                "Count": c,
                "Value": float(v or 0),
            }
            for s, c, v in rows
        ],
        ["Outcome", "Count", "Value"],
    )


def _grouped_value(
    session: Session,
    user: AuthUser,
    filters: ReportFilters | None,
    label_column,  # noqa: ANN001
    join,  # noqa: ANN001
    label_name: str,
    limit: int = 15,
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    stmt = (
        select(label_column, func.count(Quotation.id), func.sum(Quotation.grand_total))
        .where(Quotation.id.in_(select(ids.c.id)))
        .group_by(label_column)
        .order_by(func.sum(Quotation.grand_total).desc())
        .limit(limit)
    )
    if join is not None:
        stmt = stmt.join(join[0], join[1])
    rows = session.execute(stmt).all()
    return _frame(
        [
            {label_name: label or "—", "Count": count, "Value": float(value or 0)}
            for label, count, value in rows
        ],
        [label_name, "Count", "Value"],
    )


def by_customer(
    session: Session, user: AuthUser, filters: ReportFilters | None = None, limit: int = 15
) -> pd.DataFrame:
    return _grouped_value(
        session, user, filters, Quotation.customer_name_snapshot, None, "Customer", limit
    )


def by_employee(
    session: Session, user: AuthUser, filters: ReportFilters | None = None, limit: int = 15
) -> pd.DataFrame:
    return _grouped_value(
        session, user, filters, User.employee_name,
        (User, Quotation.sales_user_id == User.id), "Employee", limit,
    )


def _line_grouped(
    session: Session,
    user: AuthUser,
    filters: ReportFilters | None,
    label_column,  # noqa: ANN001
    label_name: str,
    join=None,  # noqa: ANN001
    limit: int = 20,
) -> pd.DataFrame:
    """Aggregate over quotation *lines* rather than quotations."""
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    stmt = (
        select(
            label_column,
            func.count(QuotationItem.id),
            func.sum(QuotationItem.quantity_packs),
            func.sum(QuotationItem.net_line_total),
        )
        .where(QuotationItem.quotation_id.in_(select(ids.c.id)))
        .group_by(label_column)
        .order_by(func.sum(QuotationItem.net_line_total).desc())
        .limit(limit)
    )
    if join is not None:
        stmt = stmt.join(join[0], join[1])
    rows = session.execute(stmt).all()
    return _frame(
        [
            {
                label_name: label or "—",
                "Lines": lines,
                "Packs": float(packs or 0),
                "Value": float(value or 0),
            }
            for label, lines, packs, value in rows
        ],
        [label_name, "Lines", "Packs", "Value"],
    )


def by_product_size(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    return _line_grouped(session, user, filters, QuotationItem.size_label, "Size")


def by_board_quality(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    return _line_grouped(
        session, user, filters, QuotationItem.board_quality, "Board quality"
    )


def by_price_tier(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    return _line_grouped(
        session, user, filters, PriceTier.name, "Tier",
        join=(PriceTier, QuotationItem.price_tier_id == PriceTier.id),
    )


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def discounts_given(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(
            Quotation.quote_number, Quotation.revision_no,
            Quotation.customer_name_snapshot, Quotation.quote_discount_pct,
            Quotation.quote_discount_amount, Quotation.grand_total, Quotation.currency,
        )
        .where(
            Quotation.id.in_(select(ids.c.id)),
            Quotation.quote_discount_amount > 0,
        )
        .order_by(Quotation.quote_discount_amount.desc())
    ).all()
    return _frame(
        [
            {
                "Quotation": f"{number} Rev {rev}", "Customer": customer or "—",
                "Discount %": float(pct or 0), "Discount": float(amount or 0),
                "Total": float(total or 0), "Currency": currency,
            }
            for number, rev, customer, pct, amount, total, currency in rows
        ],
        ["Quotation", "Customer", "Discount %", "Discount", "Total", "Currency"],
    )


def margin_analysis(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    """Only quotations with costs recorded appear.

    A quotation with no cost data has no margin — it is excluded rather than
    shown at 100%, which is what a NULL cost would otherwise imply.
    """
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(
            Quotation.quote_number, Quotation.revision_no,
            Quotation.customer_name_snapshot, Quotation.grand_total,
            Quotation.total_cost, Quotation.gross_profit,
            Quotation.gross_margin_pct, Quotation.currency,
        )
        .where(
            Quotation.id.in_(select(ids.c.id)),
            Quotation.gross_margin_pct.is_not(None),
        )
        .order_by(Quotation.gross_margin_pct)
    ).all()
    return _frame(
        [
            {
                "Quotation": f"{number} Rev {rev}", "Customer": customer or "—",
                "Total": float(total or 0), "Cost": float(cost or 0),
                "Profit": float(profit or 0), "Margin %": float(margin or 0),
                "Currency": currency,
            }
            for number, rev, customer, total, cost, profit, margin, currency in rows
        ],
        ["Quotation", "Customer", "Total", "Cost", "Profit", "Margin %", "Currency"],
    )


def lost_reasons(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(
            Quotation.quote_number, Quotation.customer_name_snapshot,
            Quotation.grand_total, Quotation.currency,
            CustomerResponseLog.loss_reason, CustomerResponseLog.competitor,
            CustomerResponseLog.response_date,
        )
        .join(
            CustomerResponseLog,
            CustomerResponseLog.quotation_id == Quotation.id,
            isouter=True,
        )
        .where(
            Quotation.id.in_(select(ids.c.id)),
            Quotation.status == QuotationStatus.LOST,
        )
        .order_by(Quotation.quote_date.desc())
    ).all()
    return _frame(
        [
            {
                "Quotation": number, "Customer": customer or "—",
                "Value": float(total or 0), "Currency": currency,
                "Reason": reason or "not recorded", "Competitor": competitor or "—",
                "Date": response_date.isoformat() if response_date else "—",
            }
            for number, customer, total, currency, reason, competitor, response_date in rows
        ],
        ["Quotation", "Customer", "Value", "Currency", "Reason", "Competitor", "Date"],
    )


def expiring(
    session: Session,
    user: AuthUser,
    filters: ReportFilters | None = None,
    within_days: int = 30,
    today: dt.date | None = None,
) -> pd.DataFrame:
    today = today or dt.date.today()
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(
            Quotation.quote_number, Quotation.revision_no,
            Quotation.customer_name_snapshot, Quotation.valid_until,
            Quotation.grand_total, Quotation.currency, Quotation.status,
        )
        .where(
            Quotation.id.in_(select(ids.c.id)),
            Quotation.valid_until.is_not(None),
            Quotation.valid_until <= today + dt.timedelta(days=within_days),
            Quotation.status.in_(
                [QuotationStatus.APPROVED, QuotationStatus.SENT_TO_CUSTOMER]
            ),
        )
        .order_by(Quotation.valid_until)
    ).all()
    from modules.constants import STATUS_DISPLAY_NAMES

    return _frame(
        [
            {
                "Quotation": f"{number} Rev {rev}", "Customer": customer or "—",
                "Valid until": valid.isoformat() if valid else "—",
                "Days left": (valid - today).days if valid else None,
                "Value": float(total or 0), "Currency": currency,
                "Status": STATUS_DISPLAY_NAMES.get(status, str(status)),
            }
            for number, rev, customer, valid, total, currency, status in rows
        ],
        ["Quotation", "Customer", "Valid until", "Days left", "Value", "Currency", "Status"],
    )


def custom_price_usage(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(
            Quotation.quote_number, Quotation.customer_name_snapshot,
            QuotationItem.line_no, QuotationItem.size_label,
            QuotationItem.board_quality, QuotationItem.price_per_pack,
            QuotationItem.custom_price_reason, Quotation.currency,
        )
        .join(Quotation, QuotationItem.quotation_id == Quotation.id)
        .where(
            Quotation.id.in_(select(ids.c.id)),
            QuotationItem.is_custom_price.is_(True),
        )
        .order_by(Quotation.quote_date.desc())
    ).all()
    return _frame(
        [
            {
                "Quotation": number, "Customer": customer or "—", "Line": line_no,
                "Size": size or "—", "Board quality": quality or "—",
                "Price / pack": float(price or 0), "Currency": currency,
                "Reason": reason or "not recorded",
            }
            for number, customer, line_no, size, quality, price, reason, currency in rows
        ],
        ["Quotation", "Customer", "Line", "Size", "Board quality",
         "Price / pack", "Currency", "Reason"],
    )


def price_list_usage(session: Session, user: AuthUser) -> pd.DataFrame:
    """Which imported price list each quoted price came from.

    Answers "what were we quoting off in March?" without anyone having to open
    a spreadsheet.
    """
    rows = session.execute(
        select(
            ProductPrice.source_workbook_name,
            func.count(func.distinct(QuotationItem.id)),
            func.min(ProductPrice.effective_from),
            func.max(ProductPrice.effective_from),
        )
        .join(ProductPrice, QuotationItem.product_price_id == ProductPrice.id)
        .group_by(ProductPrice.source_workbook_name)
        .order_by(func.count(func.distinct(QuotationItem.id)).desc())
    ).all()
    return _frame(
        [
            {
                "Source": source or "manual entry",
                "Lines quoted": lines,
                "Effective from": first.isoformat() if first else "—",
                "Latest": last.isoformat() if last else "—",
            }
            for source, lines, first, last in rows
        ],
        ["Source", "Lines quoted", "Effective from", "Latest"],
    )


def approval_turnaround(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(
            Quotation.quote_number, Quotation.customer_name_snapshot,
            Approval.requested_at, Approval.decided_at, Approval.decision,
        )
        .join(Quotation, Approval.quotation_id == Quotation.id)
        .where(Quotation.id.in_(select(ids.c.id)))
        .order_by(Approval.requested_at.desc())
    ).all()

    built = []
    for number, customer, requested, decided, decision in rows:
        hours = None
        if requested and decided:
            hours = round((decided - requested).total_seconds() / 3600, 1)
        built.append(
            {
                "Quotation": number,
                "Customer": customer or "—",
                "Requested": requested.strftime("%d %b %Y %H:%M") if requested else "—",
                "Decided": decided.strftime("%d %b %Y %H:%M") if decided else "pending",
                "Hours": hours,
                "Decision": str(decision).replace("_", " ").title(),
            }
        )
    return _frame(
        built,
        ["Quotation", "Customer", "Requested", "Decided", "Hours", "Decision"],
    )


def conversion_by_month(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(Quotation.quote_date, Quotation.status)
        .where(
            Quotation.id.in_(select(ids.c.id)),
            Quotation.status.in_([QuotationStatus.ACCEPTED, QuotationStatus.LOST]),
        )
    ).all()

    buckets: dict[str, dict[str, int]] = {}
    for quote_date, status in rows:
        bucket = buckets.setdefault(
            f"{quote_date:%Y-%m}", {"Accepted": 0, "Lost": 0}
        )
        bucket["Accepted" if status is QuotationStatus.ACCEPTED else "Lost"] += 1

    return _frame(
        [
            {
                "Month": month,
                "Accepted": v["Accepted"],
                "Lost": v["Lost"],
                "Conversion %": round(
                    v["Accepted"] / (v["Accepted"] + v["Lost"]) * 100, 1
                ) if (v["Accepted"] + v["Lost"]) else None,
            }
            for month, v in sorted(buckets.items())
        ],
        ["Month", "Accepted", "Lost", "Conversion %"],
    )


# --------------------------------------------------------------------------- #
# Container shipping
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ShippingHeadlines:
    total_containers: Decimal = ZERO
    quotations_with_shipping: int = 0
    total_freight: Decimal = ZERO
    average_transit_days: Decimal | None = None
    currencies: tuple[str, ...] = ()

    @property
    def average_freight_per_container(self) -> Decimal | None:
        """``None`` rather than zero when nothing has been shipped yet."""
        if self.total_containers <= ZERO:
            return None
        return (self.total_freight / self.total_containers).quantize(Decimal("0.01"))


def shipping_headlines(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> ShippingHeadlines:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()

    rows = session.execute(
        select(
            ShipmentContainer.container_count,
            ShipmentContainer.freight_cost,
            ShipmentContainer.transit_days,
            QuotationShipment.freight_currency,
            QuotationShipment.quotation_id,
        )
        .join(
            QuotationShipment,
            ShipmentContainer.quotation_shipment_id == QuotationShipment.id,
        )
        .where(QuotationShipment.quotation_id.in_(select(ids.c.id)))
    ).all()

    total_containers = ZERO
    total_freight = ZERO
    transit_values: list[int] = []
    currencies: set[str] = set()
    quotations: set[int] = set()

    for count, freight, transit, currency, quotation_id in rows:
        count = count or ZERO
        total_containers += count
        # Quantized as it accumulates: freight is 6 dp and counts are 3 dp,
        # so the raw product carries nine and reads oddly in an export.
        total_freight += ((freight or ZERO) * count).quantize(Decimal('0.01'))
        if transit:
            transit_values.append(int(transit))
        if currency:
            currencies.add(currency)
        quotations.add(quotation_id)

    average_transit = (
        (Decimal(sum(transit_values)) / Decimal(len(transit_values))).quantize(
            Decimal("0.1")
        )
        if transit_values else None
    )

    return ShippingHeadlines(
        total_containers=total_containers,
        quotations_with_shipping=len(quotations),
        total_freight=total_freight,
        average_transit_days=average_transit,
        currencies=tuple(sorted(currencies)),
    )


def _container_grouped(
    session: Session,
    user: AuthUser,
    filters: ReportFilters | None,
    label_column,  # noqa: ANN001
    label_name: str,
    join=None,  # noqa: ANN001
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    stmt = (
        select(
            label_column,
            func.sum(ShipmentContainer.container_count),
            func.count(ShipmentContainer.id),
            func.sum(
                ShipmentContainer.container_count * ShipmentContainer.freight_cost
            ),
        )
        .join(
            QuotationShipment,
            ShipmentContainer.quotation_shipment_id == QuotationShipment.id,
        )
        .where(QuotationShipment.quotation_id.in_(select(ids.c.id)))
        .group_by(label_column)
        .order_by(func.sum(ShipmentContainer.container_count).desc())
    )
    if join is not None:
        stmt = stmt.join(join[0], join[1], isouter=True)

    rows = session.execute(stmt).all()
    return _frame(
        [
            {
                label_name: _readable(label),
                "Containers": float(containers or 0),
                "Rows": rows_count,
                "Freight": float(freight or 0),
            }
            for label, containers, rows_count, freight in rows
        ],
        [label_name, "Containers", "Rows", "Freight"],
    )


def _readable(value) -> str:  # noqa: ANN001
    """Turn an enum value into the label an operator would recognise."""
    from modules.constants import (
        CONTAINER_SIZE_LABELS,
        CONTAINER_TYPE_LABELS,
        FREIGHT_METHOD_LABELS,
        ContainerSize,
        ContainerType,
        FreightMethod,
    )

    if value is None:
        return "—"
    for enum_type, labels in (
        (ContainerSize, CONTAINER_SIZE_LABELS),
        (ContainerType, CONTAINER_TYPE_LABELS),
        (FreightMethod, FREIGHT_METHOD_LABELS),
    ):
        if isinstance(value, enum_type):
            return labels[value]
        try:
            return labels[enum_type(value)]
        except (ValueError, KeyError):
            continue
    return str(value)


def containers_by_size(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    return _container_grouped(
        session, user, filters, ShipmentContainer.container_size, "Container size"
    )


def containers_by_type(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    return _container_grouped(
        session, user, filters, ShipmentContainer.container_type, "Container type"
    )


def containers_by_shipping_line(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    """Carrier usage. Rows booked outside the managed list show as their free text."""
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(
            ShippingLine.name,
            ShipmentContainer.custom_shipping_line,
            func.sum(ShipmentContainer.container_count),
            func.sum(
                ShipmentContainer.container_count * ShipmentContainer.freight_cost
            ),
        )
        .join(
            QuotationShipment,
            ShipmentContainer.quotation_shipment_id == QuotationShipment.id,
        )
        .join(ShippingLine, ShipmentContainer.shipping_line_id == ShippingLine.id,
              isouter=True)
        .where(QuotationShipment.quotation_id.in_(select(ids.c.id)))
        .group_by(ShippingLine.name, ShipmentContainer.custom_shipping_line)
        .order_by(func.sum(ShipmentContainer.container_count).desc())
    ).all()

    merged: dict[str, dict[str, float]] = {}
    for name, custom, containers, freight in rows:
        label = name or custom or "Not stated"
        entry = merged.setdefault(label, {"Containers": 0.0, "Freight": 0.0})
        entry["Containers"] += float(containers or 0)
        entry["Freight"] += float(freight or 0)

    return _frame(
        [
            {"Shipping line": label, **values}
            for label, values in sorted(
                merged.items(), key=lambda kv: kv[1]["Containers"], reverse=True
            )
        ],
        ["Shipping line", "Containers", "Freight"],
    )


def containers_by_route(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(
            QuotationShipment.port_of_loading,
            QuotationShipment.port_of_discharge,
            func.sum(ShipmentContainer.container_count),
            func.avg(ShipmentContainer.transit_days),
        )
        .join(
            ShipmentContainer,
            ShipmentContainer.quotation_shipment_id == QuotationShipment.id,
        )
        .where(QuotationShipment.quotation_id.in_(select(ids.c.id)))
        .group_by(
            QuotationShipment.port_of_loading, QuotationShipment.port_of_discharge
        )
        .order_by(func.sum(ShipmentContainer.container_count).desc())
    ).all()
    return _frame(
        [
            {
                "Port of loading": loading or "—",
                "Port of discharge": discharge or "—",
                "Containers": float(containers or 0),
                "Average transit (days)": round(float(transit), 1) if transit else None,
            }
            for loading, discharge, containers, transit in rows
        ],
        ["Port of loading", "Port of discharge", "Containers", "Average transit (days)"],
    )


def shipments(
    session: Session, user: AuthUser, filters: ReportFilters | None = None
) -> pd.DataFrame:
    """One row per container, for the detailed shipping report."""
    ids = _scoped_ids(session, user, filters or ReportFilters()).subquery()
    rows = session.execute(
        select(
            Quotation.quote_number,
            Quotation.revision_no,
            Quotation.customer_name_snapshot,
            QuotationShipment.freight_method,
            QuotationShipment.port_of_loading,
            QuotationShipment.port_of_discharge,
            QuotationShipment.freight_currency,
            ShippingLine.name,
            ShipmentContainer.custom_shipping_line,
            ShipmentContainer.container_size,
            ShipmentContainer.container_type,
            ShipmentContainer.container_count,
            ShipmentContainer.freight_cost,
            ShipmentContainer.transit_days,
            ShipmentContainer.estimated_departure,
            ShipmentContainer.estimated_arrival,
        )
        .join(QuotationShipment, QuotationShipment.quotation_id == Quotation.id)
        .join(
            ShipmentContainer,
            ShipmentContainer.quotation_shipment_id == QuotationShipment.id,
        )
        .join(ShippingLine, ShipmentContainer.shipping_line_id == ShippingLine.id,
              isouter=True)
        .where(Quotation.id.in_(select(ids.c.id)))
        .order_by(Quotation.quote_date.desc(), ShipmentContainer.sort_order)
    ).all()

    return _frame(
        [
            {
                "Quotation": f"{number} Rev {revision}",
                "Customer": customer or "—",
                "Shipping line": line or custom or "Not stated",
                "Container size": _readable(size),
                "Container type": _readable(ctype),
                "Containers": float(count or 0),
                "Port of loading": loading or "—",
                "Port of discharge": discharge or "—",
                "Transit (days)": transit,
                "Departs": departure.isoformat() if departure else "—",
                "Arrives": arrival.isoformat() if arrival else "—",
                "Freight method": _readable(method),
                "Freight / container": float(freight or 0),
                "Currency": currency,
            }
            for (
                number, revision, customer, method, loading, discharge, currency,
                line, custom, size, ctype, count, freight, transit,
                departure, arrival,
            ) in rows
        ],
        [
            "Quotation", "Customer", "Shipping line", "Container size",
            "Container type", "Containers", "Port of loading", "Port of discharge",
            "Transit (days)", "Departs", "Arrives", "Freight method",
            "Freight / container", "Currency",
        ],
    )


def shipping_filter_options(session: Session) -> dict[str, list]:
    """Carriers and ports to populate the shipping filter widgets."""
    from modules.constants import (
        CONTAINER_SIZE_LABELS,
        CONTAINER_TYPE_LABELS,
        FREIGHT_METHOD_LABELS,
    )

    carriers = session.execute(
        select(ShippingLine.id, ShippingLine.name)
        .where(ShippingLine.deleted_at.is_(None))
        .order_by(ShippingLine.sort_order, ShippingLine.name)
    ).all()
    return {
        "carriers": [(cid, name) for cid, name in carriers],
        "sizes": [(s.value, label) for s, label in CONTAINER_SIZE_LABELS.items()],
        "types": [(c.value, label) for c, label in CONTAINER_TYPE_LABELS.items()],
        "freight_methods": [
            (m.value, label) for m, label in FREIGHT_METHOD_LABELS.items()
        ],
    }


# --------------------------------------------------------------------------- #
# Filter option sources
# --------------------------------------------------------------------------- #

def filter_options(session: Session, user: AuthUser) -> dict[str, list]:
    """Values to populate the filter widgets, restricted to the user's scope."""
    ids = _scoped_ids(session, user, ReportFilters(current_revision_only=False)).subquery()

    customers = session.execute(
        select(Customer.id, Customer.company_name)
        .where(Customer.id.in_(select(Quotation.customer_id).where(
            Quotation.id.in_(select(ids.c.id))
        )))
        .order_by(Customer.company_name)
    ).all()
    employees = session.execute(
        select(User.id, User.employee_name)
        .where(User.id.in_(select(Quotation.sales_user_id).where(
            Quotation.id.in_(select(ids.c.id))
        )))
        .order_by(User.employee_name)
    ).all()
    currencies = sorted(
        session.execute(
            select(Quotation.currency)
            .where(Quotation.id.in_(select(ids.c.id)))
            .distinct()
        ).scalars()
    )
    tiers = session.execute(
        select(PriceTier.code, PriceTier.name).order_by(PriceTier.sort_order)
    ).all()
    variants = session.execute(
        select(ProductVariant.id, Product.size_label, ProductVariant.board_quality)
        .join(Product, ProductVariant.product_id == Product.id)
        .where(ProductVariant.deleted_at.is_(None))
        .order_by(Product.size_label, ProductVariant.board_quality)
    ).all()

    return {
        "customers": [(cid, name) for cid, name in customers],
        "employees": [(uid, name) for uid, name in employees],
        "currencies": currencies,
        "tiers": [(code, name) for code, name in tiers],
        "variants": [(vid, f"{size} · {quality}") for vid, size, quality in variants],
    }
