"""Audit Log — who did what, and when.

Scope is a WHERE clause: ``audit.view_all`` sees everything, everyone else sees
only their own actions. The trail is append-only — there is no edit or delete
here by design, because a log anyone can rewrite is not a log.
"""

from __future__ import annotations

import datetime as dt
import json
from io import BytesIO

import pandas as pd
import streamlit as st
from sqlalchemy import select

from modules.audit_service import audit_query
from modules.constants import AuditAction, EntityType, Perm
from modules.database import session_scope
from modules.models import AuditLog, User
from modules.session import page_header, require_page
from modules.utilities import empty_frame, truncate

user = require_page(Perm.AUDIT_VIEW_OWN)
page_header("Audit Log", "Every recorded action, oldest values alongside new")

sees_everything = user.has(Perm.AUDIT_VIEW_ALL)
if not sees_everything:
    st.caption("You can see your own actions. A wider view requires audit.view_all.")

COLUMNS = ["When", "User", "Action", "Record", "Reason"]


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #

row_a = st.columns(4)
with row_a[0]:
    date_from = st.date_input("From", value=None, format="DD/MM/YYYY")
with row_a[1]:
    date_to = st.date_input("To", value=None, format="DD/MM/YYYY")
with row_a[2]:
    chosen_actions = st.multiselect(
        "Action", list(AuditAction),
        format_func=lambda a: a.value.replace("_", " ").title(),
        placeholder="All",
    )
with row_a[3]:
    chosen_types = st.multiselect(
        "Record type", list(EntityType),
        format_func=lambda e: e.value.replace("_", " ").title(),
        placeholder="All",
    )

row_b = st.columns([2, 1, 1])
with session_scope() as db:
    people = (
        [(u.id, u.employee_name) for u in db.execute(select(User)).scalars()]
        if sees_everything else []
    )
with row_b[0]:
    chosen_users = st.multiselect(
        "User", [p[0] for p in people],
        format_func=lambda uid: dict(people)[uid],
        placeholder="All", disabled=not sees_everything,
    )
with row_b[1]:
    record_id = st.number_input("Record ID", min_value=0, value=0, step=1)
with row_b[2]:
    limit = st.selectbox("Show", [200, 500, 1000, 5000], index=0)


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #

with session_scope() as db:
    stmt = audit_query(user)
    if date_from:
        stmt = stmt.where(AuditLog.occurred_at >= dt.datetime.combine(
            date_from, dt.time.min
        ))
    if date_to:
        stmt = stmt.where(AuditLog.occurred_at <= dt.datetime.combine(
            date_to, dt.time.max
        ))
    if chosen_actions:
        stmt = stmt.where(AuditLog.action.in_([a.value for a in chosen_actions]))
    if chosen_types:
        stmt = stmt.where(AuditLog.entity_type.in_([e.value for e in chosen_types]))
    if chosen_users:
        stmt = stmt.where(AuditLog.user_id.in_(chosen_users))
    if record_id:
        stmt = stmt.where(AuditLog.entity_id == record_id)

    entries = list(db.execute(stmt.limit(limit)).scalars())
    rows = [
        {
            "When": e.occurred_at.strftime("%d %b %Y %H:%M:%S"),
            "User": e.username_snapshot or "system",
            "Action": e.action.replace("_", " ").title(),
            "Record": (
                f"{(e.entity_type or '').replace('_', ' ').title()}"
                f"{f' #{e.entity_id}' if e.entity_id else ''}"
            ) or "—",
            "Reason": truncate(e.reason, 80) or "",
            "_old": e.old_value_json,
            "_new": e.new_value_json,
            "_id": e.id,
        }
        for e in entries
    ]

st.caption(f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")

if not rows:
    st.dataframe(empty_frame(COLUMNS), width="stretch", hide_index=True)
    st.info("Nothing matches these filters.")
    st.stop()

st.dataframe(
    [{k: r[k] for k in COLUMNS} for r in rows], width="stretch", hide_index=True
)


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #

st.divider()
picked = st.selectbox(
    "Inspect an entry",
    rows,
    format_func=lambda r: f"{r['When']} · {r['User']} · {r['Action']}",
)

detail_a, detail_b = st.columns(2)
with detail_a:
    st.markdown("**Before**")
    if picked["_old"]:
        st.json(picked["_old"])
    else:
        st.caption("Nothing recorded — this action created something, or had no prior value.")
with detail_b:
    st.markdown("**After**")
    if picked["_new"]:
        st.json(picked["_new"])
    else:
        st.caption("Nothing recorded — this action removed something, or had no new value.")

if picked["Reason"]:
    st.info(f"Reason given: {picked['Reason']}")

st.caption(
    "Field-level entries record only what actually changed, so a save that altered "
    "nothing leaves no entry. Passwords are never recorded in any form."
)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

if user.has(Perm.QUOTE_EXPORT):
    frame = pd.DataFrame(
        [
            {
                **{k: r[k] for k in COLUMNS},
                "Before": json.dumps(r["_old"]) if r["_old"] else "",
                "After": json.dumps(r["_new"]) if r["_new"] else "",
            }
            for r in rows
        ]
    )
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        frame.to_excel(writer, index=False, sheet_name="Audit log")
    st.download_button(
        "Export to Excel",
        data=buffer.getvalue(),
        file_name=f"audit_log_{dt.date.today():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
