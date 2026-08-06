"""Quotation History — search, filter, open and export.

The result set is restricted by a SQL predicate from
``authorization.quotation_scope_filter``, not by filtering rows after loading
them: a salesperson's "own quotations" is a WHERE clause, so a stale identifier
in session state cannot widen it.
"""

from __future__ import annotations

import datetime as dt
from io import BytesIO

import streamlit as st
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from modules import quotation_service
from modules.authorization import PermissionDenied, quotation_scope_filter
from modules.constants import STATUS_DISPLAY_NAMES, Perm, QuotationStatus
from modules.database import session_scope
from modules.models import Customer, Quotation, User
from modules.repositories import LIKE_ESCAPE, _like
from modules.session import page_header, require_page
from modules.utilities import empty_frame, escape_markdown, format_date, format_money

user = require_page(Perm.QUOTE_VIEW_OWN)
page_header("Quotation History", "Search, open and export quotations")

COLUMNS = [
    "Number", "Rev", "Customer", "Project", "Status", "Quote date",
    "Valid until", "Currency", "Total", "Salesperson",
]


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #

row_a = st.columns([3, 1.5, 1.5])
with row_a[0]:
    term = st.text_input(
        "Search",
        placeholder="Quotation number, customer, project or PO reference",
        label_visibility="collapsed",
    )
with row_a[1]:
    status_filter = st.multiselect(
        "Status",
        list(QuotationStatus),
        format_func=lambda s: STATUS_DISPLAY_NAMES[s],
        placeholder="All statuses",
    )
with row_a[2]:
    show_all_revisions = st.checkbox(
        "All revisions",
        value=False,
        help="Off shows only the current revision of each quotation.",
    )
    can_restore = user.has(Perm.QUOTE_DELETE_ANY)
    show_deleted = can_restore and st.checkbox(
        "Deleted",
        value=False,
        help=(
            "Show deleted quotations instead of live ones, so one removed by "
            "mistake can be found and restored."
        ),
    )

row_b = st.columns([1.5, 1.5, 2])
with row_b[0]:
    date_from = st.date_input("Quote date from", value=None, format="DD/MM/YYYY")
with row_b[1]:
    date_to = st.date_input("Quote date to", value=None, format="DD/MM/YYYY")
with row_b[2]:
    expiring = st.selectbox(
        "Expiry", ["Any", "Expiring within 7 days", "Expiring within 30 days", "Expired"]
    )


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #

with session_scope() as db:
    stmt = (
        select(Quotation)
        .options(selectinload(Quotation.items))
        .where(
            Quotation.deleted_at.is_not(None)
            if show_deleted
            else Quotation.deleted_at.is_(None)
        )
        .where(quotation_scope_filter(user))
        .order_by(Quotation.quote_date.desc(), Quotation.id.desc())
    )

    if not show_all_revisions:
        stmt = stmt.where(Quotation.is_current_revision.is_(True))
    if status_filter:
        stmt = stmt.where(Quotation.status.in_(status_filter))
    if date_from:
        stmt = stmt.where(Quotation.quote_date >= date_from)
    if date_to:
        stmt = stmt.where(Quotation.quote_date <= date_to)

    today = dt.date.today()
    if expiring == "Expiring within 7 days":
        stmt = stmt.where(
            Quotation.valid_until.is_not(None),
            Quotation.valid_until >= today,
            Quotation.valid_until <= today + dt.timedelta(days=7),
        )
    elif expiring == "Expiring within 30 days":
        stmt = stmt.where(
            Quotation.valid_until.is_not(None),
            Quotation.valid_until >= today,
            Quotation.valid_until <= today + dt.timedelta(days=30),
        )
    elif expiring == "Expired":
        stmt = stmt.where(
            Quotation.valid_until.is_not(None), Quotation.valid_until < today
        )

    if term:
        pattern = _like(term.strip())
        stmt = stmt.where(
            or_(
                Quotation.quote_number.ilike(pattern, escape=LIKE_ESCAPE),
                Quotation.customer_name_snapshot.ilike(pattern, escape=LIKE_ESCAPE),
                Quotation.project_name.ilike(pattern, escape=LIKE_ESCAPE),
                Quotation.customer_po_ref.ilike(pattern, escape=LIKE_ESCAPE),
                Quotation.customer_id.in_(
                    select(Customer.id).where(
                        Customer.company_name.ilike(pattern, escape=LIKE_ESCAPE)
                    )
                ),
            )
        )

    quotations = db.execute(stmt.limit(500)).scalars().all()
    salespeople = {u.id: u.employee_name for u in db.execute(select(User)).scalars()}

    rows = [
        {
            "Number": q.quote_number,
            "Rev": q.revision_no,
            "Customer": q.customer_name_snapshot or "-",
            "Project": q.project_name or "-",
            "Status": STATUS_DISPLAY_NAMES.get(q.status, str(q.status)),
            "Quote date": format_date(q.quote_date),
            "Valid until": format_date(q.valid_until),
            "Currency": q.currency,
            "Total": format_money(q.grand_total, q.currency),
            "Salesperson": salespeople.get(q.sales_user_id, "-"),
            "_id": q.id,
            "_lines": len(q.items),
            "_raw_total": q.grand_total,
            "_status": q.status,
            "_locked": q.is_locked,
            "_deletable": quotation_service.can_delete(user, q),
        }
        for q in quotations
    ]

st.caption(f"{len(rows)} quotation{'s' if len(rows) != 1 else ''}")

if not rows:
    st.dataframe(empty_frame(COLUMNS), width="stretch", hide_index=True)
    if show_deleted:
        st.info("Nothing has been deleted.")
    else:
        st.info(
            "No quotations match. Create one from the **Create Quotation** page, or "
            "clear the filters."
        )
    st.stop()

st.dataframe(
    [{k: r[k] for k in COLUMNS} for r in rows], width="stretch", hide_index=True
)

total_value = sum((r["_raw_total"] or 0) for r in rows)
currencies = {r["Currency"] for r in rows}
if len(currencies) == 1:
    # Only meaningful when everything shown is in one currency; summing across
    # currencies would produce a number that means nothing.
    st.caption(
        escape_markdown(f"Total value shown: {format_money(total_value, currencies.pop())}")
    )
else:
    st.caption(
        f"Results span {len(currencies)} currencies, so no total is shown."
    )


# --------------------------------------------------------------------------- #
# Open / act
# --------------------------------------------------------------------------- #

st.divider()
open_col, export_col = st.columns([2, 1])

with open_col:
    picked = st.selectbox(
        "Open a quotation",
        rows,
        format_func=lambda r: (
            f"{r['Number']} Rev {r['Rev']} — {r['Customer']} — {r['Total']}"
        ),
    )
    if st.button("Open", type="primary", disabled=show_deleted):
        st.session_state["active_quotation_id"] = picked["_id"]
        st.query_params["quote_id"] = str(picked["_id"])
        st.switch_page("pages/02_Create_Quotation.py")
    if show_deleted:
        st.caption("Restore a quotation before opening it.")

with export_col:
    if user.has(Perm.QUOTE_EXPORT):
        buffer = BytesIO()
        import pandas as pd

        frame = pd.DataFrame([{k: r[k] for k in COLUMNS} for r in rows])
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            frame.to_excel(writer, index=False, sheet_name="Quotations")
        st.download_button(
            "Export to Excel",
            data=buffer.getvalue(),
            file_name=f"quotations_{dt.date.today():%Y%m%d}.xlsx",
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            width="stretch",
        )
        st.caption("Exports exactly the rows shown, with the filters applied.")


# --------------------------------------------------------------------------- #
# Delete / restore
# --------------------------------------------------------------------------- #

if show_deleted:
    st.divider()
    st.markdown("##### Restore a deleted quotation")
    st.caption(
        "Puts it back into the history, the reports and the approval queue exactly "
        "as it was. Nothing was thrown away when it was deleted."
    )
    if st.button(f"Restore {picked['Number']}", type="primary"):
        try:
            with session_scope() as db:
                target = db.get(Quotation, picked["_id"])
                count = quotation_service.restore_quotation(db, user, target)
        except PermissionDenied as exc:
            st.error(str(exc))
        else:
            st.toast(
                f"{picked['Number']} restored ({count} revision(s))", icon="✅"
            )
            st.rerun()

elif picked["_deletable"]:
    st.divider()
    with st.expander(f"Delete {picked['Number']}"):
        with session_scope() as db:
            family = quotation_service.revision_family(
                db, db.get(Quotation, picked["_id"])
            )
            revision_count = len(family)

        # Deleting one revision of a family would leave the rest with nothing
        # marked current, so the whole family goes. Saying so up front matters:
        # the row on screen says "Rev 2" and the user is about to remove three.
        if revision_count > 1:
            st.warning(
                f"{picked['Number']} has {revision_count} revisions. All of them "
                f"will be deleted together — a quotation is deleted as a whole, "
                f"not one revision at a time."
            )

        if picked["_locked"] or picked["_status"] is not QuotationStatus.DRAFT:
            st.warning(
                f"This quotation is **{picked['Status']}**, not a draft. If it has "
                f"been sent, consider cancelling it instead — a cancelled quotation "
                f"stays in the history as a record of what the customer was quoted."
            )

        st.caption(
            "Deletion is reversible: the quotation is hidden rather than erased, "
            "and an administrator can restore it. The quotation number stays "
            f"used — nothing later will be issued as {picked['Number']}."
        )
        reason = st.text_input(
            "Reason (optional)", placeholder="Recorded in the audit log",
            key="delete_reason",
        )
        confirmed = st.checkbox(
            f"Yes, delete {picked['Number']}", key="delete_confirm"
        )
        if st.button("Delete", type="primary", disabled=not confirmed):
            try:
                with session_scope() as db:
                    target = db.get(Quotation, picked["_id"])
                    count = quotation_service.delete_quotation(
                        db, user, target, reason=reason.strip() or None
                    )
            except PermissionDenied as exc:
                st.error(str(exc))
            else:
                st.session_state.pop("delete_confirm", None)
                if st.session_state.get("active_quotation_id") == picked["_id"]:
                    # Otherwise the editor reopens a quotation that is now gone.
                    st.session_state.pop("active_quotation_id", None)
                st.toast(
                    f"{picked['Number']} deleted ({count} revision(s))", icon="🗑️"
                )
                st.rerun()

elif user.has(Perm.QUOTE_DELETE_DRAFT):
    st.divider()
    st.caption(
        f"{picked['Number']} cannot be deleted from here: it is "
        f"{picked['Status'].lower()} rather than a draft of yours. Deleting an "
        "issued quotation requires the quote.delete_any permission."
    )
