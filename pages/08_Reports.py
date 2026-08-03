"""Reports — management reporting with Excel export.

Each report is a function returning a DataFrame with stable columns, so the
page is a thin shell over ``reporting_service`` and the export is always
exactly the rows on screen.

Reports that expose cost or margin are gated on ``margin.view``; they are
absent from the list rather than shown empty, so nobody is left wondering
whether the figures are zero or hidden.
"""

from __future__ import annotations

import datetime as dt
from io import BytesIO

import pandas as pd
import streamlit as st

from modules import reporting_service
from modules.constants import STATUS_DISPLAY_NAMES, Perm, QuotationStatus
from modules.database import session_scope
from modules.reporting_service import ReportFilters
from modules.session import page_header, require_page

user = require_page(Perm.REPORT_VIEW)
page_header("Reports", "Management reporting, exportable to Excel")

can_see_margins = user.has(Perm.MARGIN_VIEW)


# --------------------------------------------------------------------------- #
# Report catalogue
# --------------------------------------------------------------------------- #

REPORTS: list[dict] = [
    {
        "name": "Quoted and accepted value by month",
        "fn": reporting_service.value_by_month,
        "note": "Every quotation raised in each month, and the part of it accepted.",
    },
    {
        "name": "Conversion rate by month",
        "fn": reporting_service.conversion_by_month,
        "note": (
            "Counts only quotations the customer actually decided on. A month with "
            "nothing decided shows a blank rate rather than 0%."
        ),
    },
    {
        "name": "Quotations by employee",
        "fn": reporting_service.by_employee,
        "note": "Count and value of quotations raised, highest first.",
    },
    {
        "name": "Quotations by customer",
        "fn": reporting_service.by_customer,
        "note": "Count and value by customer, highest first.",
    },
    {
        "name": "Quotations by box size",
        "fn": reporting_service.by_product_size,
        "note": "Aggregated over quotation lines rather than quotations.",
    },
    {
        "name": "Quotations by board quality",
        "fn": reporting_service.by_board_quality,
        "note": (
            "Board qualities are never merged, so the same size in two qualities "
            "appears as two rows."
        ),
    },
    {
        "name": "Quotations by pricing tier",
        "fn": reporting_service.by_price_tier,
        "note": "Which tiers the business is actually quoting at.",
    },
    {
        "name": "Discounts given",
        "fn": reporting_service.discounts_given,
        "note": "Quotations carrying a quotation-level discount, largest first.",
    },
    {
        "name": "Margin analysis",
        "fn": reporting_service.margin_analysis,
        "note": (
            "Only quotations with costs recorded appear. One with no cost data has no "
            "margin and is excluded rather than shown at 100%."
        ),
        "requires": Perm.MARGIN_VIEW,
    },
    {
        "name": "Lost quotation reasons",
        "fn": reporting_service.lost_reasons,
        "note": "From the manually recorded customer responses.",
    },
    {
        "name": "Expiring quotations",
        "fn": reporting_service.expiring,
        "note": "Approved or sent quotations reaching their validity date within 30 days.",
    },
    {
        "name": "Custom-price usage",
        "fn": reporting_service.custom_price_usage,
        "note": "Every line priced outside the published tiers, with its reason.",
    },
    {
        "name": "Price-list usage",
        "fn": lambda db, u, f: reporting_service.price_list_usage(db, u),
        "note": (
            "Which imported workbook each quoted line was priced from — answers "
            "'what were we quoting off in March?' without opening a spreadsheet."
        ),
    },
    {
        "name": "Approval turnaround",
        "fn": reporting_service.approval_turnaround,
        "note": "How long approval decisions took, newest first.",
    },
    {
        "name": "Quotations by status",
        "fn": reporting_service.count_by_status,
        "note": "The pipeline as a table.",
    },
]

available = [
    report for report in REPORTS
    if "requires" not in report or user.has(report["requires"])
]

if not can_see_margins:
    st.caption(
        "Reports containing cost or margin figures are not shown — they require the "
        "margin.view permission."
    )


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #

with session_scope() as db:
    options = reporting_service.filter_options(db, user)

chosen = st.selectbox(
    "Report", available, format_func=lambda r: r["name"]
)

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
            format_func=lambda s: STATUS_DISPLAY_NAMES[s], placeholder="All",
        )
    row_b = st.columns(3)
    with row_b[0]:
        chosen_customers = st.multiselect(
            "Customer", [c[0] for c in options["customers"]],
            format_func=lambda cid: dict(options["customers"])[cid], placeholder="All",
        )
    with row_b[1]:
        chosen_employees = st.multiselect(
            "Employee", [e[0] for e in options["employees"]],
            format_func=lambda uid: dict(options["employees"])[uid], placeholder="All",
        )
    with row_b[2]:
        chosen_tiers = st.multiselect(
            "Pricing tier", [t[0] for t in options["tiers"]],
            format_func=lambda code: dict(options["tiers"])[code], placeholder="All",
        )

filters = ReportFilters(
    date_from=date_from,
    date_to=date_to,
    customer_ids=tuple(chosen_customers),
    sales_user_ids=tuple(chosen_employees),
    statuses=tuple(chosen_statuses),
    currency=None if chosen_currency == "All" else chosen_currency,
    tier_codes=tuple(chosen_tiers),
)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

st.markdown(f"##### {chosen['name']}")
st.caption(chosen["note"])

with session_scope() as db:
    frame = chosen["fn"](db, user, filters)

st.caption(f"{len(frame)} row(s) · filters: {filters.describe()}")

if frame.empty:
    # A correctly shaped empty frame still renders its headers, so the report
    # visibly ran and returned nothing rather than appearing broken.
    st.dataframe(frame, width="stretch", hide_index=True)
    st.info("No data matches the current filters.")
else:
    st.dataframe(frame, width="stretch", hide_index=True)

if user.has(Perm.QUOTE_EXPORT):
    buffer = BytesIO()
    sheet_name = chosen["name"][:31]  # Excel's sheet-name limit
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        frame.to_excel(writer, index=False, sheet_name=sheet_name)
    st.download_button(
        "Export this report to Excel",
        data=buffer.getvalue(),
        file_name=(
            f"{chosen['name'].lower().replace(' ', '_')}_{dt.date.today():%Y%m%d}.xlsx"
        ),
        mime=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    st.caption("Exports exactly the rows shown, with the filters applied.")

st.divider()
st.caption(
    "Every report is restricted to the quotations you may see. A salesperson's "
    "figures cover their own work; a manager's cover their team."
)
