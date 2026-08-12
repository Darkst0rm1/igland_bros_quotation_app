"""Customer Portal — publish a quotation, and see what the customer did with it.

Kept as its own page rather than folded into Create Quotation or Quotation
History: publishing to a customer is a distinct act with its own permissions,
and the pages it would otherwise disturb are already long.

Two things this page must never do, both of which the service layer enforces
independently:

* **Show a link that was issued earlier.** Only the hash is stored, so a
  previously issued token cannot be retrieved by anyone, including this page.
  A lost link is reissued, not recovered.
* **Count an employee's preview as a customer view.** The preview reads the
  same projection the public site renders but goes nowhere near the HTTP
  layer, so no view event is written and no submission nonce is consumed.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import streamlit as st
from sqlalchemy import select

from modules import (
    portal_readiness,
    portal_service,
    pricing_snapshot,
    quotation_service,
    quote_document_service,
)
from modules.authorization import PermissionDenied, quotation_scope_filter
from modules.config import get_settings
from modules.constants import (
    DOCUMENT_JOB_DISPLAY_NAMES,
    INCLUSION_DISPLAY_NAMES,
    ItemInclusion,
    Perm,
    PortalResponseType,
    QuotationStatus,
    STATUS_DISPLAY_NAMES,
)
from modules.database import session_scope
from modules.models import (
    PortalResponse,
    Quotation,
    QuoteAccessToken,
    QuoteEvent,
)
from modules.session import page_header, require_page
from modules.utilities import escape_markdown, format_date, format_datetime, format_money

user = require_page(Perm.QUOTE_PORTAL_PREVIEW)
page_header("Customer Portal", "Publish a quotation and track the customer's response")

settings = get_settings()


# --------------------------------------------------------------------------- #
# Pick a quotation
# --------------------------------------------------------------------------- #

with session_scope() as db:
    rows = db.execute(
        select(Quotation)
        .where(Quotation.deleted_at.is_(None))
        .where(Quotation.is_current_revision.is_(True))
        .where(quotation_scope_filter(user))
        .order_by(Quotation.quote_date.desc(), Quotation.id.desc())
        .limit(200)
    ).scalars().all()
    options = [
        {
            "id": q.id,
            "label": (
                f"{q.quote_number} {q.revision_label} — "
                f"{q.customer_name_snapshot or 'Customer'} — "
                f"{format_money(q.grand_total, q.currency)} "
                f"({STATUS_DISPLAY_NAMES.get(q.status, q.status)})"
            ),
        }
        for q in rows
    ]

if not options:
    st.info("No quotations are available to you yet.")
    st.stop()

picked = st.selectbox(
    "Quotation", options, format_func=lambda o: o["label"], key="portal_quote"
)
quote_id = picked["id"]
st.divider()


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #

with session_scope() as db:
    quotation = db.get(Quotation, quote_id)
    readiness = portal_readiness.check(db, quotation, settings)
    status = quotation.status
    is_accepted = status is QuotationStatus.ACCEPTED

with st.expander(
    "Company readiness"
    + ("" if readiness.is_complete else f" — {len(readiness.outstanding)} outstanding"),
    expanded=not readiness.is_complete,
):
    for item in readiness.requirements:
        mark = "✅" if item.satisfied else ("⛔" if item.blocking else "⚠️")
        line = f"{mark} **{item.label}**"
        if item.detail and not item.satisfied:
            line += f" — {item.detail}"
        st.markdown(escape_markdown(line))

    if not readiness.may_issue_link:
        st.error(
            "Publishing is blocked until the items marked ⛔ are complete. "
            "A customer must never receive a quotation headed by placeholder "
            "details."
        )
    elif readiness.banner_needed:
        st.warning(
            "Company details are incomplete. Previewing is allowed here because "
            "this is not a production deployment, but the customer page would "
            "show gaps."
        )


# --------------------------------------------------------------------------- #
# What the customer is offered
# --------------------------------------------------------------------------- #

st.markdown("##### Items offered")
locked_note = (
    "This quotation has been accepted and is locked. Create a revision to change it."
    if is_accepted
    else ""
)
if locked_note:
    st.info(locked_note)

with session_scope() as db:
    quotation = db.get(Quotation, quote_id)
    item_rows = [
        {
            "Line": item.line_no,
            "Description": (
                item.description_override or item.custom_description
                or (item.variant.product.name if item.variant and item.variant.product else "-")
            ),
            "Qty": float(item.quantity_packs),
            "Line total": format_money(item.net_line_total, quotation.currency),
            "Offered as": INCLUSION_DISPLAY_NAMES[item.inclusion],
            "_id": item.id,
        }
        for item in sorted(quotation.items, key=lambda i: i.sort_order)
    ]
    current_deposit = quotation.deposit_pct

    # Three figures, three names. The all-options amount is derived here and
    # never stored — a stored copy would be one more thing to drift out of step
    # with the lines it is supposed to summarise.
    accepted_response = next(
        (
            r for r in sorted(quotation.portal_responses, key=lambda r: r.submitted_at)
            if r.response_type is PortalResponseType.APPROVED
        ),
        None,
    )
    totals_rows = [
        {
            "label": row.label,
            "amount": format_money(row.amount, row.currency),
            "help": row.help_text,
            "primary": row.is_primary,
        }
        for row in pricing_snapshot.totals_summary(quotation, accepted_response)
    ]

st.dataframe(
    pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in item_rows]),
    width="stretch", hide_index=True,
)

for column, row in zip(st.columns(len(totals_rows)), totals_rows, strict=True):
    with column:
        # st.metric rather than markdown: the label sits above the figure and
        # cannot be read as part of it, which is the failure mode being guarded
        # against — an amount whose meaning is ambiguous.
        column.metric(row["label"], row["amount"], help=row["help"])
        if row["primary"]:
            st.caption("Primary amount")

if not is_accepted and user.has(Perm.QUOTE_EDIT_OWN_DRAFT):
    edit_col, deposit_col = st.columns([2, 1])
    with edit_col:
        target = st.selectbox(
            "Change how a line is offered", item_rows,
            format_func=lambda r: f"{r['Line']}. {r['Description']} ({r['Offered as']})",
            key="portal_line",
        )
        choice = st.radio(
            "Offered as",
            list(ItemInclusion),
            format_func=lambda i: INCLUSION_DISPLAY_NAMES[i],
            horizontal=True,
            index=list(ItemInclusion).index(
                ItemInclusion(
                    next(k for k, v in INCLUSION_DISPLAY_NAMES.items()
                         if v == target["Offered as"])
                )
            ),
            key="portal_inclusion",
        )
        st.caption(
            "Optional and Recommended lines are excluded from the total until "
            "the customer selects them. Nothing is recorded as accepted until "
            "they submit a response."
        )
        if st.button("Apply", key="portal_apply_inclusion"):
            try:
                with session_scope() as db:
                    item = next(
                        i for i in db.get(Quotation, quote_id).items
                        if i.id == target["_id"]
                    )
                    item.inclusion = choice
                    quotation_service.recompute_totals(db, db.get(Quotation, quote_id))
            except Exception as exc:  # noqa: BLE001 — surfaced to the employee
                st.error(str(exc))
            else:
                st.toast("Line updated", icon="✅")
                st.rerun()

    with deposit_col:
        deposit = st.number_input(
            "Deposit %", min_value=0.0, max_value=100.0,
            value=float(current_deposit or 0), step=5.0, key="portal_deposit",
        )
        if st.button("Save deposit", key="portal_save_deposit"):
            with session_scope() as db:
                db.get(Quotation, quote_id).deposit_pct = Decimal(str(deposit))
            st.toast("Deposit updated", icon="✅")
            st.rerun()


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #

st.divider()
st.markdown("##### Preview")
st.caption(
    "Exactly what the customer page would show, read from the same projection. "
    "This does not record a view or consume a submission nonce."
)

if st.button("Show customer preview", key="portal_preview"):
    with session_scope() as db:
        quotation = db.get(Quotation, quote_id)
        totals = portal_service.compute_selection_totals(quotation, [])
        selectable = portal_service.selectable_items(quotation)
        deposit = portal_service.deposit_due(quotation, totals)
        currency = quotation.currency

    left, right = st.columns([2, 1])
    with left:
        st.markdown(escape_markdown(f"**Subtotal** {format_money(totals.subtotal, currency)}"))
        st.markdown(escape_markdown(f"**Tax** {format_money(totals.tax_amount, currency)}"))
        st.markdown(escape_markdown(f"**Total** {format_money(totals.grand_total, currency)}"))
        if deposit:
            st.markdown(escape_markdown(f"**Deposit due** {format_money(deposit, currency)}"))
    with right:
        st.metric("Optional items offered", len(selectable))
        st.caption("Excluded from the total above until the customer selects them.")


# --------------------------------------------------------------------------- #
# The customer link
# --------------------------------------------------------------------------- #

st.divider()
st.markdown("##### Customer link")

with session_scope() as db:
    tokens = db.execute(
        select(QuoteAccessToken)
        .where(QuoteAccessToken.quotation_id == quote_id)
        .order_by(QuoteAccessToken.id.desc())
    ).scalars().all()
    link_rows = [
        {
            "Issued": format_datetime(t.created_at),
            "Expires": format_datetime(t.expires_at),
            "State": (
                "Revoked" if t.revoked_at else ("Live" if t.is_usable() else "Expired")
            ),
            "Views": t.view_count,
            "First viewed": format_datetime(t.first_viewed_at),
            "Last viewed": format_datetime(t.last_viewed_at),
            "_id": t.id,
            "_live": t.is_usable(),
        }
        for t in tokens
    ]

# The table is rendered after the controls below, not here: this list was read
# before the buttons ran, so drawing it now would show "none issued" in the very
# render where a link was just created.

# A freshly minted link is held for this run only, so it can be copied. It is
# never written to session state, a log, or anywhere it could outlive the page.
issue_col, revoke_col = st.columns(2)

with issue_col:
    can_issue = (
        user.has(Perm.QUOTE_PORTAL_LINK_ISSUE)
        and readiness.may_issue_link
        and status in {QuotationStatus.APPROVED, QuotationStatus.SENT_TO_CUSTOMER}
    )
    default_expiry = (
        quotation.valid_until
        or (dt.date.today() + dt.timedelta(days=settings.portal_link_days))
    )
    expiry = st.date_input(
        "Link expires", value=default_expiry, min_value=dt.date.today(),
        key="portal_expiry",
    )
    if any(r["_live"] for r in link_rows):
        st.caption(
            "A live link already exists. Issuing another leaves it working — "
            "revoke it first if the old one must stop."
        )
    if st.button("Generate customer link", type="primary", disabled=not can_issue):
        try:
            with session_scope() as db:
                quotation = db.get(Quotation, quote_id)
                _token, raw = portal_service.issue_token(
                    db, user, quotation,
                    expires_at=dt.datetime.combine(expiry, dt.time.max, tzinfo=dt.UTC),
                )
                base = (settings.portal_base_url or "").rstrip("/")
                link = f"{base}/quote/public/{raw}" if base else f"/quote/public/{raw}"
        except PermissionDenied as exc:
            st.error(str(exc))
        else:
            st.success("Link created. Copy it now — it cannot be shown again.")
            st.code(link, language=None)
            st.caption(
                "Only a hash of this link is stored, so nobody — including this "
                "page — can retrieve it later. If it is lost, issue a new one."
            )
    if not can_issue and user.has(Perm.QUOTE_PORTAL_LINK_ISSUE):
        if not readiness.may_issue_link:
            st.caption("Blocked by the readiness check above.")
        else:
            st.caption(
                "A link can be issued once the quotation is approved."
            )

with revoke_col:
    live = [r for r in link_rows if r["_live"]]
    if live and user.has(Perm.QUOTE_PORTAL_LINK_REVOKE):
        target_link = st.selectbox(
            "Revoke a live link", live,
            format_func=lambda r: f"Issued {r['Issued']} — {r['Views']} view(s)",
            key="portal_revoke_pick",
        )
        confirmed = st.checkbox(
            "Yes, revoke this link", key="portal_revoke_confirm",
            help="The customer's existing URL stops working immediately.",
        )
        if st.button("Revoke", disabled=not confirmed):
            with session_scope() as db:
                token = db.get(QuoteAccessToken, target_link["_id"])
                portal_service.revoke_token(db, user, token)
            st.session_state.pop("portal_revoke_confirm", None)
            st.toast("Link revoked", icon="🔒")
            st.rerun()
    elif live:
        st.caption("You do not have permission to revoke customer links.")


# Re-read after the actions so the table reflects anything just done.
with session_scope() as db:
    refreshed = db.execute(
        select(QuoteAccessToken)
        .where(QuoteAccessToken.quotation_id == quote_id)
        .order_by(QuoteAccessToken.id.desc())
    ).scalars().all()
    refreshed_rows = [
        {
            "Issued": format_datetime(t.created_at),
            "Expires": format_datetime(t.expires_at),
            "State": (
                "Revoked" if t.revoked_at else ("Live" if t.is_usable() else "Expired")
            ),
            "Views": t.view_count,
            "First viewed": format_datetime(t.first_viewed_at),
            "Last viewed": format_datetime(t.last_viewed_at),
        }
        for t in refreshed
    ]

if refreshed_rows:
    st.dataframe(pd.DataFrame(refreshed_rows), width="stretch", hide_index=True)
else:
    st.caption("No link has been issued for this quotation yet.")


# --------------------------------------------------------------------------- #
# What the customer did
# --------------------------------------------------------------------------- #

st.divider()
st.markdown("##### Customer response")

if not user.has(Perm.QUOTE_PORTAL_VIEW_RESPONSE):
    st.caption("You do not have permission to view customer responses.")
else:
    with session_scope() as db:
        responses = db.execute(
            select(PortalResponse)
            .where(PortalResponse.quotation_id == quote_id)
            .order_by(PortalResponse.submitted_at.desc())
        ).scalars().all()
        detail = [
            {
                "id": r.id,
                "type": r.response_type,
                "name": r.customer_name,
                "title": r.job_title or "-",
                "email": r.customer_email or "-",
                "signature": r.signature_name or "-",
                "comment": r.comment or "",
                "revision": r.revision_no,
                "total": format_money(r.grand_total, r.currency),
                "at": format_datetime(r.submitted_at),
                "selected": list(r.selected_item_ids or []),
                "document_status": quote_document_service.state_for_response(
                    db, r.id
                ).status,
                "document_retryable": quote_document_service.state_for_response(
                    db, r.id
                ).is_retryable,
            }
            for r in responses
        ]
        item_labels = {
            i.id: (
                i.description_override or i.custom_description
                or (i.variant.product.name if i.variant and i.variant.product else "-")
            )
            for i in db.get(Quotation, quote_id).items
        }

    if not detail:
        st.caption("The customer has not responded yet.")
    for entry in detail:
        accepted = entry["type"] is PortalResponseType.APPROVED
        with st.container(border=True):
            st.markdown(
                escape_markdown(
                    f"**{'Accepted' if accepted else 'Changes requested'}** "
                    f"— Rev {entry['revision']} on {entry['at']}"
                )
            )
            st.markdown(
                escape_markdown(
                    f"{entry['name']} · {entry['title']} · {entry['email']}"
                )
            )
            if accepted:
                st.markdown(escape_markdown(f"Accepted total: **{entry['total']}**"))
                st.markdown(escape_markdown(f"Signed: _{entry['signature']}_"))
                chosen = [item_labels.get(i, str(i)) for i in entry["selected"]]
                st.markdown(
                    escape_markdown(
                        "Optional items taken: "
                        + (", ".join(chosen) if chosen else "none")
                    )
                )
                # The signed PDF's state, and only its state. The reason a job
                # failed is diagnostic detail that helps nobody on this page and
                # would put internal error text in front of a salesperson.
                doc_col, retry_col = st.columns([3, 1])
                with doc_col:
                    st.caption("Signed copy")
                    st.markdown(
                        f"**{DOCUMENT_JOB_DISPLAY_NAMES[entry['document_status']]}**"
                    )
                with retry_col:
                    if (
                        entry["document_retryable"]
                        and user.has(Perm.QUOTE_PORTAL_LINK_ISSUE)
                        and st.button("Try again", key=f"retry_doc_{entry['id']}")
                    ):
                        with session_scope() as db:
                            quote_document_service.retry(db, entry["id"])
                        quote_document_service.run_pending_jobs(
                            limit=1, response_id=entry["id"]
                        )
                        st.toast("Preparing the signed copy", icon="📄")
                        st.rerun()
            if entry["comment"]:
                st.markdown(escape_markdown(f"Comment: {entry['comment']}"))

    if is_accepted and user.has(Perm.QUOTE_CREATE_REVISION):
        st.caption(
            "The accepted revision is locked. Create a new revision from "
            "Quotation History to quote anything different."
        )


# --------------------------------------------------------------------------- #
# Event history
# --------------------------------------------------------------------------- #

st.markdown("##### Portal activity")
with session_scope() as db:
    events = db.execute(
        select(QuoteEvent)
        .where(QuoteEvent.quotation_id == quote_id)
        .order_by(QuoteEvent.occurred_at, QuoteEvent.id)
    ).scalars().all()
    event_rows = [
        {
            "When": format_datetime(e.occurred_at),
            "Event": e.event_type.value.replace("_", " ").title(),
            "Detail": ", ".join(
                f"{k}: {v}" for k, v in (e.detail_json or {}).items()
            ) or "-",
        }
        for e in events
    ]

if event_rows:
    st.dataframe(pd.DataFrame(event_rows), width="stretch", hide_index=True)
else:
    st.caption("Nothing has happened on this quotation's link yet.")
