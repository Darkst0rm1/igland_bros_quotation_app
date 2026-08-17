"""Approval Queue — quotations awaiting an internal decision.

The queue only lists work this user can actually action. Their own submissions
and their own quotations are filtered out here **and** refused at decision time
— self-approval is blocked by identity, before any permission is consulted, so
no role grant can route around it.
"""

from __future__ import annotations

import streamlit as st

from modules import approval_service, pricing_service, quotation_service
from modules.approval_service import ApprovalError
from modules.authorization import PermissionDenied, can_view_costs
from modules.constants import (
    CHARGE_TYPE_DISPLAY_NAMES,
    STATUS_DISPLAY_NAMES,
    Perm,
)
from modules.database import session_scope
from modules.models import Quotation, User
from modules.session import page_header, require_page
from modules.utilities import empty_frame, format_date, format_money

user = require_page(Perm.QUOTE_APPROVE)
page_header("Approval Queue", "Quotations awaiting your decision")

show_costs = can_view_costs(user)


# --------------------------------------------------------------------------- #
# Charge waivers
#
# First on the page, and deliberately before the ``st.stop()`` that ends it
# when no quotation is awaiting a decision: a manager with waivers to decide
# and no quotations to approve would otherwise be told nothing is waiting for
# them while money sits unbilled.
# --------------------------------------------------------------------------- #

if user.has(Perm.CHARGE_WAIVER_APPROVE):
    with session_scope() as db:
        waiver_rows = [
            {
                "charge_id": charge.id,
                "quotation_id": quotation.id,
                "Quotation": quotation.display_number,
                "Customer": quotation.customer_name_snapshot or "-",
                "Charge": (
                    charge.description
                    or CHARGE_TYPE_DISPLAY_NAMES.get(
                        charge.charge_type, str(charge.charge_type)
                    )
                ),
                "Amount": format_money(charge.amount, charge.currency),
                "Requested by": (
                    requester.employee_name if requester else "-"
                ),
                "Requested": (
                    charge.waiver_requested_at.strftime("%d %b %Y %H:%M")
                    if charge.waiver_requested_at else "-"
                ),
                "Reason": charge.waiver_reason or "-",
            }
            for charge, quotation in quotation_service.pending_waivers(db, user)
            for requester in [db.get(User, charge.waiver_requested_by_id)]
        ]

    st.markdown("##### Charge waivers awaiting your decision")
    if waiver_rows:
        st.caption(
            f"{len(waiver_rows)} charge(s) requested to be waived. Each is "
            f"still being billed until you decide."
        )
        st.dataframe(
            [
                {k: v for k, v in row.items() if not k.endswith("_id")}
                for row in waiver_rows
            ],
            width="stretch", hide_index=True,
        )
        picked = st.selectbox(
            "Open the quotation to decide",
            waiver_rows,
            format_func=lambda r: f"{r['Quotation']} — {r['Charge']} {r['Amount']}",
            key="waiver_pick",
        )
        if st.button("Open quotation", key="waiver_open"):
            st.session_state["active_quotation_id"] = picked["quotation_id"]
            st.switch_page("pages/02_Create_Quotation.py")
    else:
        st.caption("No charge waivers are awaiting a decision.")
    st.divider()


with session_scope() as db:
    rows = []
    for approval, quotation in approval_service.queue(db, user):
        submitter = db.get(User, approval.requested_by_id)
        warnings = pricing_service.evaluate_quotation(db, quotation)
        rows.append(
            {
                "approval_id": approval.id,
                "quotation_id": quotation.id,
                "Quotation": quotation.display_number,
                "Customer": quotation.customer_name_snapshot or "-",
                "Project": quotation.project_name or "-",
                "Total": format_money(quotation.grand_total, quotation.currency),
                "Submitted by": submitter.employee_name if submitter else "-",
                "Submitted": approval.requested_at.strftime("%d %b %Y %H:%M"),
                "Status": STATUS_DISPLAY_NAMES.get(quotation.status, str(quotation.status)),
                "triggers": [
                    t.get("message", "") for t in (approval.triggered_rules_json or [])
                ],
                "blocking": [w.message for w in pricing_service.blocking(warnings)],
                "warnings": [w for w in warnings if not w.blocks_release],
                "margin": quotation.gross_margin_pct,
                "cost": quotation.total_cost,
                "currency": quotation.currency,
                "valid_until": quotation.valid_until,
                "lines": len(quotation.items),
            }
        )

COLUMNS = [
    "Quotation", "Customer", "Project", "Total", "Submitted by", "Submitted", "Status"
]

st.caption(f"{len(rows)} awaiting decision")

if not rows:
    st.dataframe(empty_frame(COLUMNS), width="stretch", hide_index=True)
    st.success(
        "Nothing is waiting for you. Quotations you raised yourself never appear "
        "here — another manager has to decide those."
    )
    st.stop()

st.dataframe(
    [{k: r[k] for k in COLUMNS} for r in rows], width="stretch", hide_index=True
)

st.divider()

picked = st.selectbox(
    "Review a quotation",
    rows,
    format_func=lambda r: f"{r['Quotation']} — {r['Customer']} — {r['Total']}",
)

detail_a, detail_b, detail_c, detail_d = st.columns(4)
detail_a.metric("Total", picked["Total"])
detail_b.metric("Lines", picked["lines"])
detail_c.metric("Valid until", format_date(picked["valid_until"]))
if show_costs and picked["margin"] is not None:
    detail_d.metric("Gross margin", f"{picked['margin']:.2f}%")
else:
    detail_d.metric("Gross margin", "—")
    if show_costs:
        st.caption("No costs are recorded for these products, so margin is unavailable.")

st.markdown("##### Why this needs approval")
for message in picked["triggers"]:
    st.info(message)
if not picked["triggers"]:
    st.caption("No rules recorded against this request.")

if picked["blocking"]:
    st.error(
        "Blocking problems — approving requires an override reason:\n"
        + "\n".join(f"- {b}" for b in picked["blocking"])
    )
for warning in picked["warnings"]:
    st.warning(f"{warning.icon} {warning.message}")

if st.button("Open the full quotation"):
    st.session_state["active_quotation_id"] = picked["quotation_id"]
    st.query_params["quote_id"] = str(picked["quotation_id"])
    st.switch_page("pages/02_Create_Quotation.py")

st.divider()
st.markdown("##### Decision")

comments = st.text_area("Comments", key="approval_comments")
override_reason = ""
if picked["blocking"]:
    override_reason = st.text_input(
        "Override reason (required to approve past the blocking problems above)",
        key="override_reason",
    )

approve_col, return_col, reject_col = st.columns(3)


def _decide(action: str) -> None:
    try:
        with session_scope() as db:
            quotation = db.get(Quotation, picked["quotation_id"])
            if action == "approve":
                approval_service.approve(
                    db, quotation, user, picked["approval_id"],
                    comments=comments or None,
                    override_reason=override_reason or None,
                )
            elif action == "return":
                approval_service.return_for_revision(
                    db, quotation, user, picked["approval_id"], comments
                )
            else:
                approval_service.reject(
                    db, quotation, user, picked["approval_id"], comments
                )
    except (ApprovalError, PermissionDenied) as exc:
        st.error(str(exc))
    else:
        st.toast(f"Quotation {action}d", icon="✅")
        st.rerun()


with approve_col:
    if st.button("Approve", type="primary", width="stretch"):
        _decide("approve")
with return_col:
    if st.button("Return for revision", width="stretch"):
        if not comments.strip():
            st.error("A reason is required to return a quotation for revision.")
        else:
            _decide("return")
with reject_col:
    if st.button("Reject", width="stretch"):
        if not comments.strip():
            st.error("A reason is required to reject a quotation.")
        else:
            _decide("reject")

st.caption(
    "Approving releases the quotation for a final document. Returning it for revision "
    "sends it back to the salesperson with your comments."
)
