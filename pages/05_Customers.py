"""Customers — records, contacts and addresses.

Presentation only. Every write goes through ``customer_service``, which
re-checks permission and writes the audit row.
"""

from __future__ import annotations

import streamlit as st

from modules.authorization import PermissionDenied
from modules.constants import SUPPORTED_CURRENCIES, AddressType, CustomerStatus, Perm
from modules.customer_service import (
    CustomerError,
    add_address,
    add_contact,
    copy_billing_to_shipping,
    create_customer,
    update_address,
    update_contact,
    update_customer,
)
from modules.database import session_scope
from modules.repositories import (
    active_users,
    get_customer,
    next_customer_number,
    search_customers,
)
from modules.session import page_header, require_page
from modules.utilities import empty_frame
from modules.validation import AddressInput, ContactInput, CustomerInput

user = require_page(Perm.CUSTOMER_VIEW)
page_header("Customers", "Customer records, contacts and delivery addresses")

can_edit = user.has(Perm.CUSTOMER_EDIT)
can_create = user.has(Perm.CUSTOMER_CREATE)

STATUS_LABELS = {
    CustomerStatus.ACTIVE: "Active",
    CustomerStatus.PROSPECT: "Prospect",
    CustomerStatus.INACTIVE: "Inactive",
    CustomerStatus.ON_HOLD: "On hold",
}


def _saved(message: str) -> None:
    st.toast(message, icon="✅")
    st.rerun()


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

search_col, status_col, owner_col = st.columns([3, 1.2, 1.6])
with search_col:
    term = st.text_input(
        "Search",
        placeholder="Company, customer number, contact name or email",
        label_visibility="collapsed",
    )
with status_col:
    status_filter = st.selectbox(
        "Status",
        ["All", *[STATUS_LABELS[s] for s in CustomerStatus]],
        label_visibility="collapsed",
    )

with session_scope() as db:
    salespeople = {u.id: u.employee_name for u in active_users(db)}

with owner_col:
    owner_filter = st.selectbox(
        "Owner", ["All owners", *salespeople.values()], label_visibility="collapsed"
    )

status_code = next((s for s, label in STATUS_LABELS.items() if label == status_filter), None)
owner_id = next((i for i, n in salespeople.items() if n == owner_filter), None)

with session_scope() as db:
    rows = [
        {
            "Number": c.customer_number,
            "Company": c.company_name,
            "Status": STATUS_LABELS.get(c.status, str(c.status)),
            "Currency": c.default_currency,
            "Payment terms": c.payment_terms or "-",
            "Owner": salespeople.get(c.assigned_sales_user_id, "Unassigned"),
            "_id": c.id,
        }
        for c in search_customers(db, term or None, status_code, owner_id)
    ]

st.caption(f"{len(rows)} customer{'s' if len(rows) != 1 else ''}")

COLUMNS = ["Number", "Company", "Status", "Currency", "Payment terms", "Owner"]
if rows:
    st.dataframe(
        [{k: r[k] for k in COLUMNS} for r in rows], width="stretch", hide_index=True
    )
else:
    # A correctly shaped empty frame, so the table still renders its headers
    # instead of collapsing to nothing.
    st.dataframe(empty_frame(COLUMNS), width="stretch", hide_index=True)
    st.info("No customers match. Add one below, or clear the filters.")

st.divider()


# --------------------------------------------------------------------------- #
# Create / edit
# --------------------------------------------------------------------------- #

def customer_form(existing=None) -> None:  # noqa: ANN001
    """Shared create/edit form. ``existing`` of ``None`` means create."""
    editing = existing is not None
    if editing:
        suggested = existing["customer_number"]
    else:
        with session_scope() as db:
            suggested = next_customer_number(db)

    form_key = f"customer_form_{existing['id'] if editing else 'new'}"
    with st.form(form_key):
        left, right = st.columns(2)
        with left:
            number = st.text_input("Customer number *", value=suggested)
            company = st.text_input(
                "Company name *", value=existing["company_name"] if editing else ""
            )
            status = st.selectbox(
                "Status",
                list(CustomerStatus),
                index=list(CustomerStatus).index(
                    existing["status"] if editing else CustomerStatus.PROSPECT
                ),
                format_func=lambda s: STATUS_LABELS[s],
            )
        with right:
            currency = st.selectbox(
                "Default currency",
                SUPPORTED_CURRENCIES,
                index=SUPPORTED_CURRENCIES.index(
                    existing["default_currency"] if editing else "USD"
                ),
            )
            terms = st.text_input(
                "Payment terms",
                value=existing["payment_terms"] if editing else "",
                placeholder="Payment upon receipt",
            )
            terms_days = st.number_input(
                "Agreed credit days",
                min_value=0,
                max_value=365,
                value=existing["payment_terms_days"] if editing else 0,
                help=(
                    "Quoting payment terms beyond this will require approval. "
                    "Leave at 0 if no credit has been agreed."
                ),
            )

        owner_names = ["Unassigned", *salespeople.values()]
        current_owner = (
            salespeople.get(existing["assigned_sales_user_id"], "Unassigned")
            if editing
            else "Unassigned"
        )
        owner = st.selectbox(
            "Assigned salesperson", owner_names, index=owner_names.index(current_owner)
        )
        notes = st.text_area("Internal notes", value=existing["notes"] if editing else "")

        submitted = st.form_submit_button(
            "Save changes" if editing else "Create customer", type="primary"
        )

    if not submitted:
        return

    try:
        payload = CustomerInput(
            customer_number=number,
            company_name=company,
            default_currency=currency,
            payment_terms=terms or None,
            payment_terms_days=int(terms_days) or None,
            assigned_sales_user_id=next(
                (i for i, n in salespeople.items() if n == owner), None
            ),
            status=status,
            notes=notes or None,
        )
        with session_scope() as db:
            if editing:
                update_customer(db, user, existing["id"], payload)
            else:
                create_customer(db, user, payload)
    except (ValueError, CustomerError, PermissionDenied) as exc:
        st.error(str(exc))
        return

    _saved("Customer saved")


if can_create:
    with st.expander("Add a customer", expanded=not rows):
        customer_form()


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #

if not rows:
    st.stop()

labels = {f"{r['Number']} — {r['Company']}": r["_id"] for r in rows}
chosen = st.selectbox("Open a customer", ["-", *labels], index=0)
customer_id = labels.get(chosen)

if customer_id is None:
    st.stop()

with session_scope() as db:
    customer = get_customer(db, customer_id)
    if customer is None:
        st.warning("That customer has been removed.")
        st.stop()
    header = {
        "id": customer.id,
        "company_name": customer.company_name,
        "customer_number": customer.customer_number,
        "default_currency": customer.default_currency,
        "payment_terms": customer.payment_terms or "",
        "payment_terms_days": int(customer.payment_terms_days or 0),
        "assigned_sales_user_id": customer.assigned_sales_user_id,
        "status": customer.status,
        "notes": customer.notes or "",
    }
    contacts = [
        {
            "id": c.id, "name": c.name, "title": c.title, "email": c.email,
            "phone": c.phone, "is_primary": c.is_primary, "is_active": c.is_active,
        }
        for c in customer.contacts
    ]
    addresses = [
        {
            "id": a.id, "type": a.address_type, "label": a.label, "line1": a.line1,
            "line2": a.line2, "city": a.city, "province": a.province,
            "postal_code": a.postal_code, "country": a.country,
            "is_default": a.is_default, "text": a.as_text(),
        }
        for a in customer.addresses
    ]

st.subheader(header["company_name"])

details_tab, contacts_tab, addresses_tab, quotes_tab = st.tabs(
    ["Details", f"Contacts ({len(contacts)})", f"Addresses ({len(addresses)})", "Quotations"]
)

with details_tab:
    if can_edit:
        customer_form(header)
    else:
        st.info("You have read-only access to customer records.")
        st.write(f"**Customer number** {header['customer_number']}")
        st.write(f"**Currency** {header['default_currency']}")
        st.write(f"**Payment terms** {header['payment_terms'] or '-'}")

with contacts_tab:
    if contacts:
        st.dataframe(
            [
                {
                    "Name": c["name"],
                    "Title": c["title"] or "-",
                    "Email": c["email"] or "-",
                    "Phone": c["phone"] or "-",
                    "Primary": "Yes" if c["is_primary"] else "",
                    "Active": "Yes" if c["is_active"] else "No",
                }
                for c in contacts
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No contacts recorded yet.")

    if can_edit:
        options = ["Add a new contact", *[c["name"] for c in contacts]]
        picked = st.selectbox("Edit a contact", options, key="contact_picker")
        target = next((c for c in contacts if c["name"] == picked), None)

        with st.form(f"contact_form_{target['id'] if target else 'new'}"):
            c1, c2 = st.columns(2)
            with c1:
                name = st.text_input("Name *", value=target["name"] if target else "")
                title = st.text_input(
                    "Job title", value=(target["title"] or "") if target else ""
                )
                email = st.text_input(
                    "Email", value=(target["email"] or "") if target else ""
                )
            with c2:
                phone = st.text_input(
                    "Phone", value=(target["phone"] or "") if target else ""
                )
                is_primary = st.checkbox(
                    "Primary contact", value=target["is_primary"] if target else False
                )
                is_active = st.checkbox(
                    "Active", value=target["is_active"] if target else True
                )
            saved = st.form_submit_button(
                "Save contact" if target else "Add contact", type="primary"
            )

        if saved:
            try:
                payload = ContactInput(
                    name=name, title=title or None, email=email or None,
                    phone=phone or None, is_primary=is_primary, is_active=is_active,
                )
                with session_scope() as db:
                    if target:
                        update_contact(db, user, target["id"], payload)
                    else:
                        add_contact(db, user, customer_id, payload)
            except (ValueError, CustomerError, PermissionDenied) as exc:
                st.error(str(exc))
            else:
                _saved("Contact saved")

with addresses_tab:
    if addresses:
        for address in addresses:
            kind = "Billing" if address["type"] == AddressType.BILLING else "Shipping"
            marker = " · default" if address["is_default"] else ""
            with st.container(border=True):
                st.markdown(f"**{kind}{marker}**")
                st.text(address["text"] or "(empty)")
    else:
        st.info("No addresses recorded yet.")

    if can_edit:
        if any(a["type"] == AddressType.BILLING for a in addresses):
            if st.button("Copy billing address to shipping"):
                try:
                    with session_scope() as db:
                        copy_billing_to_shipping(db, user, customer_id)
                except (CustomerError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    _saved("Shipping address created")

        options = ["Add a new address"] + [
            f"{'Billing' if a['type'] == AddressType.BILLING else 'Shipping'}"
            f" — {a['line1'] or a['label'] or a['id']}"
            for a in addresses
        ]
        picked = st.selectbox("Edit an address", options, key="address_picker")
        index = options.index(picked) - 1
        target = addresses[index] if index >= 0 else None

        with st.form(f"address_form_{target['id'] if target else 'new'}"):
            a1, a2 = st.columns(2)
            with a1:
                address_type = st.selectbox(
                    "Address type",
                    list(AddressType),
                    index=list(AddressType).index(
                        target["type"] if target else AddressType.BILLING
                    ),
                    format_func=lambda t: (
                        "Billing" if t == AddressType.BILLING else "Shipping"
                    ),
                )
                line1 = st.text_input(
                    "Address line 1", value=(target["line1"] or "") if target else ""
                )
                line2 = st.text_input(
                    "Address line 2", value=(target["line2"] or "") if target else ""
                )
                city = st.text_input("City", value=(target["city"] or "") if target else "")
            with a2:
                province = st.text_input(
                    "Province / state", value=(target["province"] or "") if target else ""
                )
                postal_code = st.text_input(
                    "Postal / ZIP code",
                    value=(target["postal_code"] or "") if target else "",
                )
                country = st.text_input(
                    "Country", value=(target["country"] or "") if target else ""
                )
                is_default = st.checkbox(
                    "Default for this type", value=target["is_default"] if target else True
                )
            saved = st.form_submit_button(
                "Save address" if target else "Add address", type="primary"
            )

        if saved:
            try:
                payload = AddressInput(
                    address_type=address_type, line1=line1 or None, line2=line2 or None,
                    city=city or None, province=province or None,
                    postal_code=postal_code or None, country=country or None,
                    is_default=is_default,
                )
                with session_scope() as db:
                    if target:
                        update_address(db, user, target["id"], payload)
                    else:
                        add_address(db, user, customer_id, payload)
            except (ValueError, CustomerError, PermissionDenied) as exc:
                st.error(str(exc))
            else:
                _saved("Address saved")

with quotes_tab:
    st.info(
        "Quotation history for this customer appears here once the quotation editor "
        "is built (Phase 3)."
    )
