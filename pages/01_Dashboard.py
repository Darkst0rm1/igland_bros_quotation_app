"""Dashboard — the quotation pipeline at a glance.

Every figure comes from ``reporting_service``, whose queries carry the user's
scope predicate. A salesperson's dashboard covers their own quotations; a
manager's covers their team's. Nothing is filtered after loading.
"""

from __future__ import annotations

import datetime as dt

import plotly.express as px
import streamlit as st

from modules import quotation_service, reporting_service
from modules.constants import STATUS_DISPLAY_NAMES, Perm, QuotationStatus
from modules.database import session_scope
from modules.reporting_service import ReportFilters
from modules.session import page_header, require_page
from modules.utilities import format_money

user = require_page()
page_header("Dashboard", "Quotation pipeline and outcomes")

#: One sequence for every chart, so a category is the same colour wherever it
#: appears.
PALETTE = ["#2f6f9f", "#4f9d69", "#c0873b", "#a8556b", "#6b6f8f", "#7f9c4a"]


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #

with session_scope() as db:
    options = reporting_service.filter_options(db, user)

with st.expander("Filters", expanded=False):
    row_a = st.columns(4)
    with row_a[0]:
        date_from = st.date_input("From", value=None, format="DD/MM/YYYY")
    with row_a[1]:
        date_to = st.date_input("To", value=None, format="DD/MM/YYYY")
    with row_a[2]:
        chosen_currency = st.selectbox("Currency", ["All", *options["currencies"]])
    with row_a[3]:
        chosen_statuses = st.multiselect(
            "Status", list(QuotationStatus),
            format_func=lambda s: STATUS_DISPLAY_NAMES[s],
            placeholder="All",
        )

    row_b = st.columns(4)
    with row_b[0]:
        chosen_customers = st.multiselect(
            "Customer", [c[0] for c in options["customers"]],
            format_func=lambda cid: dict(options["customers"])[cid],
            placeholder="All",
        )
    with row_b[1]:
        chosen_employees = st.multiselect(
            "Employee", [e[0] for e in options["employees"]],
            format_func=lambda uid: dict(options["employees"])[uid],
            placeholder="All",
        )
    with row_b[2]:
        chosen_tiers = st.multiselect(
            "Pricing tier", [t[0] for t in options["tiers"]],
            format_func=lambda code: dict(options["tiers"])[code],
            placeholder="All",
        )
    with row_b[3]:
        chosen_variants = st.multiselect(
            "Product", [v[0] for v in options["variants"]],
            format_func=lambda vid: dict(options["variants"])[vid],
            placeholder="All",
        )

filters = ReportFilters(
    date_from=date_from,
    date_to=date_to,
    customer_ids=tuple(chosen_customers),
    sales_user_ids=tuple(chosen_employees),
    statuses=tuple(chosen_statuses),
    currency=None if chosen_currency == "All" else chosen_currency,
    tier_codes=tuple(chosen_tiers),
    product_variant_ids=tuple(chosen_variants),
)


# --------------------------------------------------------------------------- #
# Headlines
# --------------------------------------------------------------------------- #

with session_scope() as db:
    figures = reporting_service.headlines(db, user, filters)

if figures.total == 0:
    st.info(
        "No quotations match. Create one from the **Create Quotation** page, or widen "
        "the filters."
    )
    st.stop()

currency_label = figures.currencies[0] if figures.currencies else "USD"
if figures.mixed_currency:
    st.caption(
        f"Results span {len(figures.currencies)} currencies "
        f"({', '.join(figures.currencies)}). Values are added at face value — filter "
        "to one currency for a meaningful total."
    )

counts = figures.counts
metric_a = st.columns(5)
metric_a[0].metric("Total quotations", figures.total)
metric_a[1].metric("Draft", counts[QuotationStatus.DRAFT.value])
metric_a[2].metric("Pending approval", counts[QuotationStatus.PENDING_APPROVAL.value])
metric_a[3].metric("Approved", counts[QuotationStatus.APPROVED.value])
metric_a[4].metric("Sent", counts[QuotationStatus.SENT_TO_CUSTOMER.value])

metric_b = st.columns(5)
metric_b[0].metric("Accepted", counts[QuotationStatus.ACCEPTED.value])
metric_b[1].metric("Lost", counts[QuotationStatus.LOST.value])
metric_b[2].metric("Expired", counts[QuotationStatus.EXPIRED.value])
metric_b[3].metric("Rejected", counts[QuotationStatus.REJECTED_INTERNALLY.value])
metric_b[4].metric("Cancelled", counts[QuotationStatus.CANCELLED.value])

metric_c = st.columns(4)
metric_c[0].metric("Quoted value", format_money(figures.total_quoted, currency_label))
metric_c[1].metric("Accepted value", format_money(figures.accepted_value, currency_label))
metric_c[2].metric(
    "Conversion rate",
    f"{figures.conversion_rate}%" if figures.conversion_rate is not None else "—",
    help=(
        "Accepted as a share of quotations the customer decided on. Shown as — until "
        "at least one has been accepted or lost, because 0% would read as 'we lose "
        "everything' rather than 'nothing has come back yet'."
    ),
)
metric_c[3].metric(
    "Expiring within 7 days",
    figures.expiring_7,
    delta=f"{figures.expiring_30} within 30 days",
    delta_color="off",
)

if figures.expiring_7:
    st.warning(
        f"{figures.expiring_7} quotation(s) expire within 7 days — see "
        "**Reports → Expiring quotations**."
    )

if user.has(Perm.QUOTE_UPDATE_STATUS):
    with session_scope() as db:
        overdue = reporting_service.expiring(db, user, filters, within_days=0)
    if len(overdue):
        st.error(
            f"{len(overdue)} quotation(s) are past their validity date but still show "
            "as approved or sent."
        )
        if st.button("Mark them expired"):
            with session_scope() as db:
                moved = quotation_service.expire_overdue(db, user)
            st.toast(f"{moved} quotation(s) expired", icon="✅")
            st.rerun()

st.divider()


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #

with session_scope() as db:
    monthly = reporting_service.value_by_month(db, user, filters)
    by_status = reporting_service.count_by_status(db, user, filters)
    outcomes = reporting_service.accepted_versus_lost(db, user, filters)
    customers = reporting_service.by_customer(db, user, filters, limit=10)
    employees = reporting_service.by_employee(db, user, filters, limit=10)
    sizes = reporting_service.by_product_size(db, user, filters)
    tiers = reporting_service.by_price_tier(db, user, filters)


def _chart(frame, title: str, builder) -> None:  # noqa: ANN001
    """Render a chart, or say plainly that there is nothing to plot.

    An empty frame is routine here — a filter matching nothing, a month with no
    activity — and a blank axis with no explanation reads like a fault.
    """
    st.markdown(f"##### {title}")
    if frame.empty:
        st.caption("Nothing to show for the current filters.")
        return
    figure = builder(frame)
    figure.update_layout(
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        height=300,
        legend={"orientation": "h", "y": -0.2},
    )
    st.plotly_chart(figure, width="stretch")


chart_row_a = st.columns(2)
with chart_row_a[0]:
    _chart(
        monthly, "Quoted and accepted value by month",
        lambda f: px.bar(
            f, x="Month", y=["Quoted", "Accepted"], barmode="group",
            color_discrete_sequence=PALETTE,
            labels={"value": currency_label, "variable": ""},
        ),
    )
with chart_row_a[1]:
    _chart(
        by_status, "Quotations by status",
        lambda f: px.bar(
            f.sort_values("Count"), x="Count", y="Status", orientation="h",
            color_discrete_sequence=PALETTE,
        ),
    )

chart_row_b = st.columns(2)
with chart_row_b[0]:
    _chart(
        outcomes, "Accepted versus lost",
        lambda f: px.pie(
            f, names="Outcome", values="Value", hole=0.45,
            color_discrete_sequence=PALETTE,
        ),
    )
with chart_row_b[1]:
    _chart(
        tiers, "Quoted value by pricing tier",
        lambda f: px.bar(
            f, x="Tier", y="Value", color="Tier",
            color_discrete_sequence=PALETTE, labels={"Value": currency_label},
        ),
    )

chart_row_c = st.columns(2)
with chart_row_c[0]:
    _chart(
        customers, "Top customers by quoted value",
        lambda f: px.bar(
            f.sort_values("Value"), x="Value", y="Customer", orientation="h",
            color_discrete_sequence=PALETTE, labels={"Value": currency_label},
        ),
    )
with chart_row_c[1]:
    _chart(
        employees, "Quoted value by employee",
        lambda f: px.bar(
            f.sort_values("Value"), x="Value", y="Employee", orientation="h",
            color_discrete_sequence=PALETTE, labels={"Value": currency_label},
        ),
    )

_chart(
    sizes, "Most-quoted product sizes",
    lambda f: px.bar(
        f, x="Size", y="Packs", color_discrete_sequence=PALETTE,
        labels={"Packs": "Packs quoted"},
    ),
)

st.caption(
    "Figures cover the quotations you are entitled to see — your own, your team's, or "
    "all of them, depending on your role."
)
