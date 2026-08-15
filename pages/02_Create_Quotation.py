"""Create Quotation — build a quotation, or open an existing one.

This page is also the Quotation Details view (architecture §9): opened with
``?quote_id=`` it renders the same quotation read-only whenever it is locked or
the user may not edit it. One renderer, two modes — a separate detail page would
duplicate the entire line, charge and terms layout and the two copies would
drift.

No money is computed here. Every figure comes from ``quotation_service``, which
recalculates through the calculation engine on each mutation.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import streamlit as st

from modules import (
    approval_service,
    document_service,
    portal_readiness,
    portal_service,
    pricing_service,
    pricing_snapshot,
    quotation_service,
    quote_send_service,
    revision_service,
    settings_service,
    shipping_service,
)
from modules.approval_service import ApprovalError
from modules.audit_service import record_audit
from modules.authorization import PermissionDenied, can_edit_quotation, can_view_costs
from modules.constants import (
    CHARGE_TYPE_DISPLAY_NAMES,
    CONTAINER_SIZE_LABELS,
    CONTAINER_TYPE_LABELS,
    FREIGHT_METHOD_LABELS,
    LOADING_METHOD_LABELS,
    STATUS_DISPLAY_NAMES,
    SUPPORTED_CURRENCIES,
    AuditAction,
    ChargeType,
    ContainerSize,
    ContainerType,
    CustomerResponse,
    EntityType,
    FreightMethod,
    Incoterm,
    LoadingMethod,
    Perm,
    PortalResponseType,
    PricingBasis,
    QuotationStatus,
    SendMethod,
)
from modules.database import session_scope
from modules.models import CustomerResponseLog, Product, Quotation, TermTemplate
from modules.quotation_service import QuotationError
from modules.revision_service import RevisionError
from modules.shipping_service import ShippingError
from modules.repositories import (
    get_price_tiers,
    search_customers,
    search_products,
    variants_for_product,
)
from modules.session import page_header, require_page
from modules.utilities import (
    escape_markdown,
    format_date,
    format_datetime,
    format_money,
    format_percent,
    format_pack_price,
    format_quantity,
)

user = require_page(Perm.QUOTE_CREATE)

QUOTE_KEY = "active_quotation_id"


def _active_quotation_id() -> int | None:
    """From the URL if present, otherwise from session state."""
    raw = st.query_params.get("quote_id")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return st.session_state.get(QUOTE_KEY)


def _open(quotation_id: int) -> None:
    st.session_state[QUOTE_KEY] = quotation_id
    st.query_params["quote_id"] = str(quotation_id)


def _close() -> None:
    st.session_state.pop(QUOTE_KEY, None)
    st.query_params.pop("quote_id", None)


# --------------------------------------------------------------------------- #
# New quotation
# --------------------------------------------------------------------------- #

quotation_id = _active_quotation_id()

if quotation_id is None:
    page_header("Create Quotation", "Start a new quotation for a customer")

    with session_scope() as db:
        customers = [
            {"id": c.id, "label": f"{c.customer_number} — {c.company_name}"}
            for c in search_customers(db)
        ]
        validity_days = settings_service.default_validity_days(db)
        default_ccy = settings_service.default_currency(db)

    if not customers:
        st.warning(
            "There are no customers yet. Add one on the **Customers** page before "
            "raising a quotation."
        )
        st.stop()

    with st.form("new_quotation"):
        picked = st.selectbox("Customer *", [c["label"] for c in customers])
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            project = st.text_input("Project name", placeholder="Pizza Box Program")
        with col_b:
            quote_date = st.date_input("Quote date", value=dt.date.today())
        with col_c:
            currency = st.selectbox(
                "Currency",
                SUPPORTED_CURRENCIES,
                index=SUPPORTED_CURRENCIES.index(default_ccy)
                if default_ccy in SUPPORTED_CURRENCIES else 0,
            )
        st.caption(
            f"The quotation will be valid for {validity_days} days by default; you can "
            "change the date afterwards."
        )
        created = st.form_submit_button("Create draft", type="primary")

    if created:
        customer_id = next(c["id"] for c in customers if c["label"] == picked)
        try:
            with session_scope() as db:
                draft = quotation_service.create_draft(
                    db, user, customer_id,
                    project_name=project or None,
                    quote_date=quote_date,
                    currency=currency,
                )
                new_id = draft.id
        except (QuotationError, PermissionDenied) as exc:
            st.error(str(exc))
        else:
            _open(new_id)
            st.rerun()

    st.stop()


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #

with session_scope() as db:
    quotation = db.get(Quotation, quotation_id)
    if quotation is None:
        st.error("That quotation no longer exists.")
        _close()
        st.stop()
    if quotation.deleted_at is not None:
        # A stale session key or a bookmarked ?quote_id= would otherwise reopen
        # a deleted quotation and let it be edited back into circulation
        # without it appearing anywhere in the history.
        st.error(
            f"{quotation.display_number} has been deleted. An administrator can "
            "restore it from the Quotation History page."
        )
        _close()
        st.stop()

    editable = can_edit_quotation(user, quotation)
    show_costs = can_view_costs(user)

    header = {
        "id": quotation.id,
        "display_number": quotation.display_number,
        "status": quotation.status,
        "quote_date": quotation.quote_date,
        "valid_until": quotation.valid_until,
        "customer": quotation.customer_name_snapshot,
        "contact_name": quotation.contact_name or "",
        "contact_email": quotation.contact_email or "",
        "contact_phone": quotation.contact_phone or "",
        "billing": quotation.billing_address_text or "",
        "shipping": quotation.shipping_address_text or "",
        "project_name": quotation.project_name or "",
        "brand": quotation.brand or "",
        "distributor": quotation.distributor or "",
        "customer_po_ref": quotation.customer_po_ref or "",
        "currency": quotation.currency,
        "quote_discount_pct": quotation.quote_discount_pct,
        "tax_rate_pct": quotation.tax_rate_pct,
        "internal_notes": quotation.internal_notes or "",
        "customer_notes": quotation.customer_notes or "",
        "subtotal": quotation.subtotal,
        "quote_discount_amount": quotation.quote_discount_amount,
        "charges_total": quotation.charges_total,
        "tax_amount": quotation.tax_amount,
        "grand_total": quotation.grand_total,
        "total_cost": quotation.total_cost,
        "gross_profit": quotation.gross_profit,
        "gross_margin_pct": quotation.gross_margin_pct,
        "is_locked": quotation.is_locked,
    }
    lines = [
        {
            "id": i.id,
            "line_no": i.line_no,
            "size_label": i.size_label or "",
            "board_quality": i.board_quality or "",
            "case_pack": i.case_pack or 0,
            "tier": i.tier.name if i.tier else "-",
            "tier_code": i.tier.code if i.tier else "",
            "pricing_basis": i.pricing_basis,
            "quantity_packs": i.quantity_packs,
            "quantity_pieces": i.quantity_pieces,
            "container_count": i.container_count,
            "price_per_pack": i.price_per_pack,
            "price_per_piece": i.price_per_piece,
            "line_discount_pct": i.line_discount_pct,
            "net_line_total": i.net_line_total,
            "line_cost_total": i.line_cost_total,
            "is_custom_price": i.is_custom_price,
            "description_override": i.description_override or "",
            "customer_remarks": i.customer_remarks or "",
        }
        for i in quotation.items
    ]
    charges = [
        {
            "id": c.id,
            "type": CHARGE_TYPE_DISPLAY_NAMES.get(c.charge_type, str(c.charge_type)),
            "description": c.description or "",
            "quantity": c.quantity_value,
            "rate": c.rate,
            "amount": c.amount,
            "taxable": c.is_taxable,
            "visible": c.is_customer_visible,
        }
        for c in quotation.charges
    ]
    terms = [
        {
            "id": t.id,
            "title": t.title,
            "body_text": t.body_text,
            "template_id": t.term_template_id,
            "visible": t.is_customer_visible,
        }
        for t in quotation.terms
    ]
    shipment_row = shipping_service.get_shipment(db, quotation_id)
    shipment = None
    container_rows: list[dict] = []
    if shipment_row is not None:
        shipment = {
            "id": shipment_row.id,
            "incoterm": shipment_row.incoterm,
            "incoterm_place": shipment_row.incoterm_place or "",
            "origin_country": shipment_row.origin_country or "",
            "port_of_loading": shipment_row.port_of_loading or "",
            "port_of_discharge": shipment_row.port_of_discharge or "",
            "final_destination": shipment_row.final_destination or "",
            "freight_method": shipment_row.freight_method,
            "total_freight": shipment_row.total_freight,
            "freight_currency": shipment_row.freight_currency,
            "freight_taxable": shipment_row.freight_taxable,
            "loading_method": shipment_row.loading_method,
            "shipping_notes": shipment_row.shipping_notes or "",
            "show_on_document": shipment_row.show_on_document,
            "customer_visible_freight": shipment_row.customer_visible_freight,
        }
        container_rows = [
            {
                "id": c.id,
                "carrier": c.carrier_name,
                "shipping_line_id": c.shipping_line_id,
                "custom_shipping_line": c.custom_shipping_line or "",
                "container_size": c.container_size,
                "container_type": c.container_type,
                "size_label": c.size_label,
                "type_label": c.type_label,
                "container_count": c.container_count,
                "freight_cost": c.freight_cost,
                "port_of_loading": c.port_of_loading or "",
                "port_of_discharge": c.port_of_discharge or "",
                "transit_days": c.transit_days,
                "estimated_departure": c.estimated_departure,
                "estimated_arrival": c.estimated_arrival,
                "notes": c.notes or "",
                "allocations": [
                    {
                        "id": a.id,
                        "line_no": a.item.line_no if a.item else None,
                        "size_label": a.item.size_label if a.item else "",
                        "quantity_per_container": a.quantity_per_container,
                        "total_allocated_quantity": a.total_allocated_quantity,
                        "bundles_per_container": a.bundles_per_container,
                        "pieces_per_container": a.pieces_per_container,
                        "allocated_freight": a.allocated_freight,
                    }
                    for a in c.allocations
                ],
            }
            for c in sorted(shipment_row.containers, key=lambda c: c.sort_order)
        ]
    carriers = [
        {"id": line.id, "name": line.name}
        for line in shipping_service.shipping_lines(db)
    ]
    total_container_count = shipping_service.total_containers(db, quotation_id)
    warnings = pricing_service.evaluate_quotation(db, quotation)
    problems = quotation_service.validate_for_submission(db, quotation)
    tiers = [(t.code, t.name) for t in get_price_tiers(db)]
    all_templates = [
        {"id": t.id, "title": t.title, "section": str(t.section)}
        for t in db.query(TermTemplate)
        .filter(TermTemplate.is_active.is_(True))
        .order_by(TermTemplate.sort_order)
        .all()
    ]

ccy = header["currency"]
container_row_count = len(container_rows)

page_header(
    header["display_number"],
    f"{header['customer']} · {STATUS_DISPLAY_NAMES.get(header['status'], header['status'])}",
)

if not editable:
    if header["is_locked"]:
        st.info(
            "This quotation has been issued and is read-only. Create a revision to "
            "change it."
        )
    else:
        st.info("You have read-only access to this quotation.")

if st.button("← Back to new quotation"):
    _close()
    st.rerun()


# --------------------------------------------------------------------------- #
# Sticky summary
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.markdown("### Summary")
    st.metric("Grand total", format_money(header["grand_total"], ccy))
    st.caption(
        escape_markdown(
            f"Subtotal {format_money(header['subtotal'], ccy)}  \n"
            f"Discount −{format_money(header['quote_discount_amount'], ccy)}  \n"
            f"Charges {format_money(header['charges_total'], ccy)}  \n"
            f"Tax {format_money(header['tax_amount'], ccy)}"
        )
    )
    st.divider()
    st.caption(f"{len(lines)} line(s) · {len(charges)} charge(s)")
    st.caption(f"Valid until {format_date(header['valid_until'])}")

    if show_costs and header["gross_margin_pct"] is not None:
        st.divider()
        st.markdown("**Internal**")
        st.caption("Never printed on a customer document.")
        st.metric("Gross margin", f"{header['gross_margin_pct']:.2f}%")
        st.caption(
            escape_markdown(
                f"Cost {format_money(header['total_cost'], ccy)} · "
                f"Profit {format_money(header['gross_profit'], ccy)}"
            )
        )

    if warnings:
        st.divider()
        st.markdown("**Checks**")
        for w in warnings:
            st.caption(f"{w.icon} {w.message}")


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #

(
    detail_tab, lines_tab, shipping_tab, charges_tab, terms_tab,
    review_tab, tracking_tab
) = st.tabs(
    ["Details", f"Lines ({len(lines)})", f"Shipping ({container_row_count})",
     f"Charges ({len(charges)})", f"Terms ({len(terms)})",
     "Review & send", "Customer response"]
)


def _save_header(**fields) -> None:
    try:
        with session_scope() as db:
            quotation_service.update_header(
                db, user, db.get(Quotation, quotation_id), **fields
            )
    except (QuotationError, PermissionDenied) as exc:
        st.error(str(exc))
    else:
        st.toast("Saved", icon="✅")
        st.rerun()


with detail_tab:
    with st.form("quotation_header", border=False):
        col_a, col_b = st.columns(2)
        with col_a:
            st.text_input("Customer", value=header["customer"], disabled=True)
            project_name = st.text_input(
                "Project", value=header["project_name"], disabled=not editable
            )
            brand = st.text_input("Brand", value=header["brand"], disabled=not editable)
            distributor = st.text_input(
                "Distributor", value=header["distributor"], disabled=not editable
            )
            po_ref = st.text_input(
                "Customer PO reference",
                value=header["customer_po_ref"], disabled=not editable,
            )
        with col_b:
            quote_date = st.date_input(
                "Quote date", value=header["quote_date"], disabled=not editable
            )
            valid_until = st.date_input(
                "Valid until", value=header["valid_until"], disabled=not editable
            )
            discount_pct = st.number_input(
                "Quotation discount %",
                min_value=0.0, max_value=100.0, step=0.5,
                value=float(header["quote_discount_pct"] or 0),
                disabled=not editable,
            )
            tax_pct = st.number_input(
                "Tax %",
                min_value=0.0, max_value=100.0, step=0.5,
                value=float(header["tax_rate_pct"] or 0),
                disabled=not editable,
            )

        st.markdown("###### Contact and addresses")
        contact_a, contact_b = st.columns(2)
        with contact_a:
            contact_name = st.text_input(
                "Contact", value=header["contact_name"], disabled=not editable
            )
            contact_email = st.text_input(
                "Email", value=header["contact_email"], disabled=not editable
            )
            contact_phone = st.text_input(
                "Phone", value=header["contact_phone"], disabled=not editable
            )
        with contact_b:
            billing = st.text_area(
                "Billing address", value=header["billing"], height=100,
                disabled=not editable,
            )
            shipping = st.text_area(
                "Shipping address", value=header["shipping"], height=100,
                disabled=not editable,
            )
        st.caption(
            "These are a snapshot taken when the quotation was created. Editing the "
            "customer record later will not change what this quotation says."
        )

        notes_a, notes_b = st.columns(2)
        with notes_a:
            customer_notes = st.text_area(
                "Notes shown to the customer",
                value=header["customer_notes"], disabled=not editable,
            )
        with notes_b:
            internal_notes = st.text_area(
                "Internal notes (never printed)",
                value=header["internal_notes"], disabled=not editable,
            )

        saved = st.form_submit_button(
            "Save details", type="primary", disabled=not editable
        )

    if saved:
        _save_header(
            project_name=project_name or None,
            brand=brand or None,
            distributor=distributor or None,
            customer_po_ref=po_ref or None,
            quote_date=quote_date,
            valid_until=valid_until,
            quote_discount_pct=Decimal(str(discount_pct)),
            tax_rate_pct=Decimal(str(tax_pct)),
            contact_name=contact_name or None,
            contact_email=contact_email or None,
            contact_phone=contact_phone or None,
            billing_address_text=billing or None,
            shipping_address_text=shipping or None,
            customer_notes=customer_notes or None,
            internal_notes=internal_notes or None,
        )


# --------------------------------------------------------------------------- #
# Lines
# --------------------------------------------------------------------------- #

with lines_tab:
    if lines:
        table = [
            {
                "#": ln["line_no"],
                "Size": ln["size_label"],
                "Board quality": ln["board_quality"],
                "Tier": ln["tier"] + (" *" if ln["is_custom_price"] else ""),
                "Basis": "Pack" if ln["pricing_basis"] is PricingBasis.PACK else "Piece",
                "Packs": format_quantity(ln["quantity_packs"]),
                "Pieces": format_quantity(ln["quantity_pieces"]),
                "Ctnrs": format_quantity(ln["container_count"]),
                "Price/pack": format_pack_price(ln["price_per_pack"], ccy),
                "Disc %": f"{ln['line_discount_pct']:.2f}",
                "Net": format_money(ln["net_line_total"], ccy),
            }
            for ln in lines
        ]
        st.dataframe(table, width="stretch", hide_index=True)
        if any(ln["is_custom_price"] for ln in lines):
            st.caption("\\* custom price — requires approval before the document is released.")
    else:
        st.info("No lines yet. Add a product below.")

    if editable:
        st.divider()
        st.markdown("##### Add a product")

        with session_scope() as db:
            products = [
                {"id": p.id, "label": p.size_label} for p in search_products(db)
            ]

        if not products:
            st.warning("The catalogue is empty. Import a price list first.")
        else:
            pick_col, quality_col = st.columns(2)
            with pick_col:
                product_label = st.selectbox(
                    "Product", [p["label"] for p in products], key="line_product"
                )
            product_id = next(p["id"] for p in products if p["label"] == product_label)

            with session_scope() as db:
                variants = [
                    {
                        "id": v.id,
                        "label": f"{v.board_quality} · case {v.case_pack}",
                        "case_pack": v.case_pack,
                        "moq_packs": v.moq_packs,
                    }
                    for v in variants_for_product(db, product_id)
                ]
            with quality_col:
                variant_label = st.selectbox(
                    "Board quality", [v["label"] for v in variants], key="line_variant"
                )
            variant = next(v for v in variants if v["label"] == variant_label)

            with session_scope() as db:
                available = pricing_service.prices_for_picker(
                    db, variant["id"], header["quote_date"], ccy
                )
                capacity = shipping_service.container_capacity_for_product(
                    db, product_id, variant_id=variant["id"]
                )
                bundle = (
                    {
                        "per_container": capacity.bundles_per_container,
                        "container": CONTAINER_SIZE_LABELS[capacity.container_size],
                        "is_anomalous": capacity.is_anomalous,
                        "anomaly_note": capacity.anomaly_note,
                    }
                    if capacity is not None
                    else None
                )
            if available:
                st.caption(
                    escape_markdown(
                        "Available: "
                        + " · ".join(
                            f"{next(n for c, n in tiers if c == code)} "
                            f"{format_pack_price(price.price_per_pack, ccy)}"
                            for code, price in available.items()
                        )
                    )
                )
                # The bundle: how many of this size fill one container. Shown
                # next to the prices because it is what decides whether the
                # three- and eight-container tiers above are actually
                # available on the quantity being typed below.
                if bundle is not None:
                    st.caption(
                        f"Bundle: {format_quantity(bundle['per_container'])} packs "
                        f"per {bundle['container']} container"
                    )
                    if bundle["is_anomalous"]:
                        st.warning(bundle["anomaly_note"], icon="⚠️")
                else:
                    st.caption(
                        "No bundle quantity recorded for this size. Import the "
                        "container-capacity workbook to add one."
                    )
            else:
                st.warning(
                    f"No {ccy} price is in force for this variant on "
                    f"{format_date(header['quote_date'])}."
                )

            # Outside the form deliberately. A widget inside ``st.form`` does
            # not rerun until the form is submitted, so nothing below could
            # fill itself from a quantity that is still being typed. Out here
            # each keystroke reruns the script.
            #
            # Entered in PIECES, because that is the unit the customer asks in.
            # The quotation stores packs, so this divides by the variant's own
            # case pack rather than a hard-coded 50 — case pack is part of a
            # variant's identity and has differed between them before.
            #
            # Opens at the minimum order quantity, which is one container.
            # Typing over it is the point: a mixed container is a normal thing
            # to quote, and a quantity below the MOQ raises a pricing warning
            # rather than being refused. ``value`` changes only when the picked
            # variant does, so an edit survives every rerun until then.
            case_pack = int(variant["case_pack"]) or 1
            moq_packs = variant.get("moq_packs")
            moq_pieces = float(moq_packs) * case_pack if moq_packs else 0.0

            pieces = st.number_input(
                "Quantity (pieces)", min_value=0.0, step=float(case_pack),
                value=moq_pieces,
                help=(
                    "How many boxes the customer wants. Opens at the minimum "
                    "order quantity — one full container. Change it freely; a "
                    "smaller quantity is quoted with a warning, not refused."
                ),
            )
            packs = pieces / case_pack

            suggested_containers = 0.0
            per_container = float(bundle["per_container"] or 0) if bundle else 0.0
            if per_container > 0 and packs > 0:
                # Two decimals: a part container is real and the customer's
                # tier depends on it, so it must not be rounded away.
                suggested_containers = round(packs / per_container, 2)

            detail = [f"{format_quantity(Decimal(str(packs)))} packs of {case_pack}"]
            if suggested_containers:
                detail.append(f"{suggested_containers:g} container(s)")
            if moq_pieces:
                detail.append(f"MOQ {format_quantity(Decimal(str(moq_pieces)))} pieces")
            st.caption(" · ".join(detail))

            if pieces and pieces % case_pack:
                # Not refused: a customer may genuinely ask for a part pack, and
                # the line stores packs as a decimal. But it is worth saying,
                # because the price is per pack and a part pack is unusual.
                st.warning(
                    f"{format_quantity(Decimal(str(pieces)))} pieces is not a "
                    f"whole number of {case_pack}-piece packs.",
                    icon="⚠️",
                )

            with st.form("add_line"):
                form_a, form_b, form_c = st.columns(3)
                with form_a:
                    tier_code = st.selectbox(
                        "Pricing tier",
                        [c for c, _ in tiers],
                        format_func=lambda code: next(n for c, n in tiers if c == code),
                        help=(
                            "The tier you choose determines the price. Entering fewer "
                            "containers than a tier expects raises a warning; it never "
                            "changes the tier."
                        ),
                    )
                    basis = st.radio(
                        "Price on",
                        [PricingBasis.PACK, PricingBasis.PIECE],
                        format_func=lambda b: "Pack price" if b is PricingBasis.PACK else "Piece price",
                        horizontal=True,
                        help=(
                            "The two price columns are independent and can differ by a "
                            "rounding unit, so which one drives the line is recorded."
                        ),
                    )
                with form_b:
                    # No ``key``: the widget must re-initialise from ``value``
                    # when the quantity above changes. With a key, session
                    # state would win and the suggestion would go stale after
                    # the first render. Typed edits survive until the quantity
                    # moves again, which is the intended behaviour — the
                    # employee's own figure is not overwritten while they work.
                    containers = st.number_input(
                        "Containers", min_value=0.0, step=1.0,
                        value=suggested_containers,
                        help=(
                            "Filled from the quantity and this size's container "
                            "capacity. Type over it if the shipment differs — "
                            "what you enter is treated as your own statement of "
                            "the shipment and takes precedence everywhere."
                        ),
                    )
                    if suggested_containers:
                        st.caption(
                            f"{format_quantity(Decimal(str(packs)))} packs ÷ "
                            f"{format_quantity(bundle['per_container'])} per container"
                        )
                    elif bundle is None:
                        st.caption(
                            "No container capacity recorded for this size, so "
                            "this cannot be filled in automatically."
                        )
                with form_c:
                    discount = st.number_input(
                        "Line discount %", min_value=0.0, max_value=100.0, step=0.5, value=0.0
                    )
                    custom_price = st.number_input(
                        "Custom price per pack",
                        min_value=0.0, step=0.01, format="%.4f", value=0.0,
                        help="Only used when the Custom tier is selected.",
                    )
                custom_reason = st.text_input(
                    "Reason for the custom price",
                    placeholder="Required when using the Custom tier",
                )
                added = st.form_submit_button("Add line", type="primary")

            if added:
                try:
                    if packs <= 0:
                        raise QuotationError("Enter a quantity greater than zero.")
                    # Divided as Decimal, not float: 115,200 / 50 in binary
                    # floating point is not reliably 2,304, and this figure is
                    # multiplied by a price and printed for a customer.
                    quantity_packs = Decimal(str(pieces)) / Decimal(case_pack)
                    with session_scope() as db:
                        quotation_service.add_line(
                            db, user, db.get(Quotation, quotation_id),
                            product_variant_id=variant["id"],
                            price_tier_code=tier_code,
                            quantity_packs=quantity_packs,
                            container_count=Decimal(str(containers)),
                            pricing_basis=basis,
                            line_discount_pct=Decimal(str(discount)),
                            custom_price_per_pack=(
                                Decimal(str(custom_price)) if custom_price > 0 else None
                            ),
                            custom_price_reason=custom_reason or None,
                        )
                except (QuotationError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Line added", icon="✅")
                    st.rerun()

    if lines and editable:
        st.divider()
        st.markdown("##### Change a line")
        edit_line = st.selectbox(
            "Line",
            lines,
            format_func=lambda ln: f"{ln['line_no']}. {ln['size_label']} — {ln['tier']}",
            key="edit_line_pick",
        )

        edit_a, edit_b, edit_c = st.columns(3)
        with edit_a:
            new_packs = st.number_input(
                "Quantity (packs)",
                min_value=0.0, step=50.0,
                value=float(edit_line["quantity_packs"]),
                key="edit_packs",
            )
            new_containers = st.number_input(
                "Containers", min_value=0.0, step=1.0,
                value=float(edit_line["container_count"]), key="edit_containers",
                help=(
                    "Leave at zero and the document works it out from the "
                    "quantity. A figure here is your own statement of the "
                    "shipment and overrides that everywhere."
                ),
            )
        with edit_b:
            new_discount = st.number_input(
                "Line discount %", min_value=0.0, max_value=100.0, step=0.5,
                value=float(edit_line["line_discount_pct"]), key="edit_discount",
            )
            new_tier = st.selectbox(
                "Re-price at tier",
                [c for c, _ in tiers],
                index=[c for c, _ in tiers].index(edit_line["tier_code"])
                if edit_line["tier_code"] in [c for c, _ in tiers] else 0,
                format_func=lambda code: next(n for c, n in tiers if c == code),
                key="edit_tier",
            )
        with edit_c:
            new_description = st.text_input(
                "Description shown to the customer",
                value=edit_line["description_override"], key="edit_description",
            )
            new_remarks = st.text_input(
                "Customer remarks", value=edit_line["customer_remarks"], key="edit_remarks"
            )

        action_a, action_b, action_c, action_d = st.columns(4)
        with action_a:
            if st.button("Save line", type="primary"):
                try:
                    with session_scope() as db:
                        quote = db.get(Quotation, quotation_id)
                        quotation_service.update_line(
                            db, user, quote, edit_line["id"],
                            quantity_packs=Decimal(str(new_packs)),
                            container_count=Decimal(str(new_containers)),
                            line_discount_pct=Decimal(str(new_discount)),
                            description_override=new_description or None,
                            customer_remarks=new_remarks or None,
                        )
                        if new_tier != edit_line["tier_code"]:
                            quotation_service.change_line_tier(
                                db, user, quote, edit_line["id"], new_tier
                            )
                except (QuotationError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Line saved", icon="✅")
                    st.rerun()
        with action_b:
            if st.button("Duplicate"):
                with session_scope() as db:
                    quotation_service.duplicate_line(
                        db, user, db.get(Quotation, quotation_id), edit_line["id"]
                    )
                st.rerun()
        with action_c:
            if st.button("Remove"):
                with session_scope() as db:
                    quotation_service.remove_line(
                        db, user, db.get(Quotation, quotation_id), edit_line["id"]
                    )
                st.rerun()
        with action_d:
            apply_tier = st.selectbox(
                "Apply tier to all",
                ["-", *[c for c, _ in tiers]],
                format_func=lambda code: (
                    "-" if code == "-" else next(n for c, n in tiers if c == code)
                ),
                label_visibility="collapsed",
            )
            if apply_tier != "-" and st.button("Apply to all lines"):
                with session_scope() as db:
                    issues = quotation_service.apply_tier_to_all(
                        db, user, db.get(Quotation, quotation_id), apply_tier
                    )
                for issue in issues:
                    st.warning(issue)
                st.rerun()



# --------------------------------------------------------------------------- #
# Container shipping & logistics
# --------------------------------------------------------------------------- #

with shipping_tab:
    can_ship = user.has(Perm.SHIPMENT_EDIT) and editable
    can_see_freight = user.has(Perm.SHIPMENT_VIEW_FREIGHT)
    can_edit_freight = user.has(Perm.SHIPMENT_EDIT_FREIGHT) and editable

    if shipment is None:
        # The message has to match what this user can actually do. Pointing at
        # a form that is not drawn — because the permission is missing or the
        # quotation is no longer editable — reads as a broken page rather than
        # as a restriction, and sends people looking for a fault that is not
        # there.
        if can_ship:
            st.info(
                "No shipping arrangement has been recorded. Add a container below "
                "to start one — the quotation works perfectly well without it."
            )
        elif not editable:
            st.info(
                "No shipping arrangement was recorded on this quotation, and it "
                "can no longer be edited."
            )
        else:
            st.info(
                "No shipping arrangement has been recorded. Adding one requires "
                "the shipment.edit permission."
            )
    else:
        summary_a, summary_b, summary_c = st.columns(3)
        summary_a.metric("Containers", format_quantity(total_container_count))
        summary_b.metric(
            "Freight method",
            FREIGHT_METHOD_LABELS[shipment["freight_method"]].split(" (")[0],
        )
        if can_see_freight:
            summary_c.metric(
                "Total freight",
                format_money(shipment["total_freight"], shipment["freight_currency"]),
            )
        else:
            summary_c.metric("Total freight", "—")
            st.caption("Freight figures require the shipment.view_freight permission.")

        # Whether that figure reaches the quotation belongs beside the figure.
        # Only ADDED_SEPARATELY becomes a charge; the reason used to be stated
        # in a caption at the foot of Route and freight, several sections down,
        # so freight could be entered, totalled, displayed in a headline metric
        # and still be absent from the quotation with nothing on screen saying
        # so. A number that changes no total has to announce that itself.
        if can_see_freight and shipment["total_freight"] > 0:
            freight_shown = format_money(
                shipment["total_freight"], shipment["freight_currency"]
            )
            if shipment["freight_method"] is FreightMethod.ADDED_SEPARATELY:
                st.success(
                    f"{freight_shown} is on the quotation as a single freight "
                    f"charge, maintained from the container rows."
                )
            else:
                st.warning(
                    f"**This freight is not on the quotation.** {freight_shown} is "
                    f"recorded against the shipment only, because the freight "
                    f"method is *"
                    f"{FREIGHT_METHOD_LABELS[shipment['freight_method']]}*. To "
                    f"bill it, change that to *"
                    f"{FREIGHT_METHOD_LABELS[FreightMethod.ADDED_SEPARATELY]}* "
                    f"under **Route and freight** below."
                    + (
                        "" if editable else
                        " This quotation is no longer editable, so that change "
                        "needs a revision."
                    )
                )

    if container_rows:
        st.markdown("##### Containers")
        table = []
        for row in container_rows:
            entry = {
                "Shipping line": row["carrier"],
                "Size": row["size_label"],
                "Type": row["type_label"],
                "Qty": format_quantity(row["container_count"]),
                "Loading": row["port_of_loading"] or "—",
                "Discharge": row["port_of_discharge"] or "—",
                "Transit": f"{row['transit_days']} days" if row["transit_days"] else "—",
                "Departs": format_date(row["estimated_departure"]),
                "Arrives": format_date(row["estimated_arrival"]),
                "Products": len(row["allocations"]),
            }
            if can_see_freight:
                entry["Freight / container"] = format_money(
                    row["freight_cost"], shipment["freight_currency"]
                )
            table.append(entry)
        st.dataframe(table, width="stretch", hide_index=True)

    # ----------------------------------------------------------------- #
    # Add a container
    # ----------------------------------------------------------------- #

    if can_ship:
        with st.expander("Add a container", expanded=not container_rows):
            with st.form("add_container"):
                row_a, row_b, row_c = st.columns(3)
                with row_a:
                    carrier_names = [c["name"] for c in carriers] + ["Other"]
                    carrier_choice = st.selectbox("Shipping line", carrier_names)
                    custom_carrier = st.text_input(
                        "Carrier name (when Other)", placeholder="Only used for Other"
                    )
                    container_size = st.selectbox(
                        "Container size",
                        list(ContainerSize),
                        index=list(ContainerSize).index(ContainerSize.FORTY_FT_HC),
                        format_func=lambda s: CONTAINER_SIZE_LABELS[s],
                    )
                    custom_size = st.text_input("Custom size (when Custom)")
                with row_b:
                    container_type = st.selectbox(
                        "Container type",
                        list(ContainerType),
                        index=list(ContainerType).index(ContainerType.DRY),
                        format_func=lambda c: CONTAINER_TYPE_LABELS[c],
                    )
                    custom_type = st.text_input("Custom type (when Custom)")
                    container_count = st.number_input(
                        "Number of containers", min_value=1, value=1, step=1
                    )
                    freight_cost = st.number_input(
                        "Freight cost per container",
                        min_value=0.0, step=100.0, format="%.2f", value=0.0,
                        disabled=not can_edit_freight,
                        help=(
                            None if can_edit_freight
                            else "Editing freight requires the shipment.edit_freight permission."
                        ),
                    )
                with row_c:
                    port_loading = st.text_input("Port of loading")
                    port_discharge = st.text_input("Port of discharge")
                    departure = st.date_input("Estimated departure", value=None)
                    transit = st.number_input(
                        "Transit time (days)", min_value=0, value=0, step=1,
                        help="Leave at 0 to derive it from the departure and arrival dates.",
                    )
                arrival = st.date_input("Estimated arrival", value=None)
                max_items = st.number_input(
                    "Maximum product items in this container", min_value=0, value=0,
                    help=(
                        "Overrides the global setting for this row. 0 uses the global "
                        "value, which the price list sets at three."
                    ),
                )
                container_notes = st.text_input("Notes")
                added = st.form_submit_button("Add container", type="primary")

            if added:
                try:
                    line_id = next(
                        (c["id"] for c in carriers if c["name"] == carrier_choice), None
                    )
                    with session_scope() as db:
                        shipping_service.add_container(
                            db, user, db.get(Quotation, quotation_id),
                            shipping_line_id=line_id,
                            custom_shipping_line=custom_carrier or None,
                            container_size=container_size,
                            custom_container_size=custom_size or None,
                            container_type=container_type,
                            custom_container_type=custom_type or None,
                            container_count=Decimal(str(container_count)),
                            freight_cost=Decimal(str(freight_cost)),
                            port_of_loading=port_loading or None,
                            port_of_discharge=port_discharge or None,
                            estimated_departure=departure,
                            estimated_arrival=arrival,
                            transit_days=int(transit) or None,
                            maximum_product_items=int(max_items) or None,
                            notes=container_notes or None,
                        )
                except (ShippingError, QuotationError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Container added", icon="✅")
                    st.rerun()

    # ----------------------------------------------------------------- #
    # Edit / allocate
    # ----------------------------------------------------------------- #

    if container_rows and can_ship:
        st.divider()
        picked_container = st.selectbox(
            "Work on a container",
            container_rows,
            format_func=lambda r: (
                f"{r['carrier']} — {r['size_label']} {r['type_label']} "
                f"x{format_quantity(r['container_count'])}"
            ),
        )

        act_a, act_b, act_c = st.columns(3)
        with act_a:
            new_count = st.number_input(
                "Number of containers", min_value=1,
                value=int(picked_container["container_count"]), step=1,
                key="edit_container_count",
            )
        with act_b:
            new_freight = st.number_input(
                "Freight per container", min_value=0.0, step=100.0, format="%.2f",
                value=float(picked_container["freight_cost"]),
                disabled=not can_edit_freight, key="edit_container_freight",
            )
        with act_c:
            new_transit = st.number_input(
                "Transit days", min_value=0,
                value=int(picked_container["transit_days"] or 0), step=1,
                key="edit_container_transit",
            )

        button_a, button_b, button_c = st.columns(3)
        with button_a:
            if st.button("Save container", type="primary"):
                try:
                    changes = {
                        "container_count": Decimal(str(new_count)),
                        "transit_days": int(new_transit) or None,
                    }
                    if can_edit_freight:
                        changes["freight_cost"] = Decimal(str(new_freight))
                    with session_scope() as db:
                        shipping_service.update_container(
                            db, user, db.get(Quotation, quotation_id),
                            picked_container["id"], **changes,
                        )
                except (ShippingError, QuotationError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Container saved", icon="✅")
                    st.rerun()
        with button_b:
            if st.button("Duplicate container"):
                with session_scope() as db:
                    shipping_service.duplicate_container(
                        db, user, db.get(Quotation, quotation_id), picked_container["id"]
                    )
                st.rerun()
        with button_c:
            if st.button("Remove container"):
                with session_scope() as db:
                    shipping_service.remove_container(
                        db, user, db.get(Quotation, quotation_id), picked_container["id"]
                    )
                st.rerun()

        st.markdown("###### Products in this container")
        if picked_container["allocations"]:
            allocation_table = [
                {
                    "Line": a["line_no"],
                    "Product": a["size_label"],
                    "Per container": format_quantity(a["quantity_per_container"]),
                    "Bundles / container": format_quantity(a["bundles_per_container"]),
                    "Pieces / container": (
                        format_quantity(a["pieces_per_container"])
                        if a["pieces_per_container"] is not None else "unknown"
                    ),
                    "Total allocated": format_quantity(a["total_allocated_quantity"]),
                    **(
                        {"Freight share": format_money(
                            a["allocated_freight"], shipment["freight_currency"]
                        )} if can_see_freight else {}
                    ),
                }
                for a in picked_container["allocations"]
            ]
            st.dataframe(allocation_table, width="stretch", hide_index=True)
            if any(a["pieces_per_container"] is None for a in picked_container["allocations"]):
                st.caption(
                    "Pieces per container reads *unknown* where the number of units in "
                    "a bundle has not been recorded — the source workbook does not "
                    "state it, and a guessed figure could reach a customer document. "
                    "Set it on **Products & Pricing**."
                )
        else:
            st.caption("No products assigned to this container yet.")

        if lines:
            allocate_a, allocate_b = st.columns([2, 1])
            with allocate_a:
                allocate_line = st.selectbox(
                    "Assign a quotation line",
                    lines,
                    format_func=lambda ln: f"{ln['line_no']}. {ln['size_label']}",
                    key="allocate_line",
                )
            with allocate_b:
                line_derived = shipping_service.per_container_from_line(
                    allocate_line["quantity_packs"],
                    allocate_line["container_count"],
                )
                per_container = st.number_input(
                    "Quantity per container (0 = take it from the line)",
                    min_value=0.0, step=100.0, value=0.0,
                    help=(
                        f"Left at 0, this line gives "
                        f"{format_quantity(line_derived)} packs per container — "
                        f"its own quantity over its own container count. Type a "
                        f"figure here only to load this container differently."
                        if line_derived is not None else
                        "Left at 0, the figure comes from the recorded "
                        "bundles-per-container for this product, because this "
                        "line has no container count of its own to divide by."
                    ),
                )
            if st.button("Assign to container"):
                try:
                    with session_scope() as db:
                        shipping_service.allocate_product(
                            db, user, db.get(Quotation, quotation_id),
                            picked_container["id"], allocate_line["id"],
                            quantity_per_container=(
                                Decimal(str(per_container)) if per_container > 0 else None
                            ),
                        )
                except (ShippingError, QuotationError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Product assigned", icon="✅")
                    st.rerun()

    # ----------------------------------------------------------------- #
    # Route, freight method and document options
    # ----------------------------------------------------------------- #

    if shipment is not None and can_ship:
        st.divider()
        st.markdown("##### Route and freight")

        with st.form("shipment_details"):
            route_a, route_b = st.columns(2)
            with route_a:
                incoterm = st.selectbox(
                    "Incoterms", list(Incoterm),
                    index=(
                        list(Incoterm).index(shipment["incoterm"])
                        if shipment["incoterm"] else list(Incoterm).index(Incoterm.FOB)
                    ),
                )
                incoterm_place = st.text_input(
                    "Named place", value=shipment["incoterm_place"]
                )
                origin_country = st.text_input(
                    "Country of origin", value=shipment["origin_country"]
                )
                loading_method = st.selectbox(
                    "Loading method", list(LoadingMethod),
                    index=(
                        list(LoadingMethod).index(shipment["loading_method"])
                        if shipment["loading_method"]
                        else list(LoadingMethod).index(LoadingMethod.FLOOR_LOADED)
                    ),
                    format_func=lambda m: LOADING_METHOD_LABELS[m],
                )
            with route_b:
                port_of_loading = st.text_input(
                    "Port of loading", value=shipment["port_of_loading"]
                )
                port_of_discharge = st.text_input(
                    "Port of discharge", value=shipment["port_of_discharge"]
                )
                final_destination = st.text_input(
                    "Final destination", value=shipment["final_destination"]
                )
                freight_method = st.selectbox(
                    "Freight method", list(FreightMethod),
                    index=list(FreightMethod).index(shipment["freight_method"]),
                    format_func=lambda m: FREIGHT_METHOD_LABELS[m],
                    disabled=not can_edit_freight,
                )

            shipping_notes = st.text_area(
                "Shipping notes", value=shipment["shipping_notes"]
            )
            doc_a, doc_b = st.columns(2)
            with doc_a:
                show_on_document = st.checkbox(
                    "Show shipping details on the customer document",
                    value=shipment["show_on_document"],
                )
            with doc_b:
                visible_freight = st.checkbox(
                    "Show freight cost on the document",
                    value=shipment["customer_visible_freight"],
                    help=(
                        "Off by default. Internal freight never appears on a customer "
                        "document unless this is ticked."
                    ),
                )
            saved_shipment = st.form_submit_button("Save shipping details", type="primary")

        if saved_shipment:
            try:
                changes = {
                    "incoterm": incoterm,
                    "incoterm_place": incoterm_place or None,
                    "origin_country": origin_country or None,
                    "port_of_loading": port_of_loading or None,
                    "port_of_discharge": port_of_discharge or None,
                    "final_destination": final_destination or None,
                    "loading_method": loading_method,
                    "shipping_notes": shipping_notes or None,
                    "show_on_document": show_on_document,
                    "customer_visible_freight": visible_freight,
                }
                if can_edit_freight:
                    changes["freight_method"] = freight_method
                with session_scope() as db:
                    shipping_service.update_shipment(
                        db, user, db.get(Quotation, quotation_id), **changes
                    )
            except (ShippingError, QuotationError, PermissionDenied) as exc:
                st.error(str(exc))
            else:
                st.toast("Shipping details saved", icon="✅")
                st.rerun()

        if shipment["freight_method"] is FreightMethod.ADDED_SEPARATELY:
            st.caption(
                "Freight appears as a single charge on the Charges tab, maintained "
                "automatically from the container rows. Do not add a second freight "
                "charge by hand."
            )
        elif shipment["freight_method"] is FreightMethod.INCLUDED:
            st.caption(
                "Freight is recorded but not charged — the quoted prices already "
                "contain it, so it is deliberately absent from the totals."
            )
        else:
            st.caption(
                "Freight is internal only: it feeds margin and landed cost, is never "
                "charged to the customer, and never appears on the document."
            )

        if st.button("Remove the shipping arrangement"):
            with session_scope() as db:
                shipping_service.remove_shipment(
                    db, user, db.get(Quotation, quotation_id)
                )
            st.toast("Shipping removed", icon="✅")
            st.rerun()

    elif not can_ship and shipment is not None:
        st.info("You have read-only access to the shipping details.")


# --------------------------------------------------------------------------- #
# Charges
# --------------------------------------------------------------------------- #

with charges_tab:
    if charges:
        st.dataframe(
            [
                {
                    "Type": c["type"],
                    "Description": c["description"],
                    "Qty": format_quantity(c["quantity"]),
                    "Rate": format_money(c["rate"], ccy),
                    "Amount": format_money(c["amount"], ccy),
                    "Taxable": "Yes" if c["taxable"] else "No",
                    "On document": "Yes" if c["visible"] else "Internal only",
                }
                for c in charges
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No additional charges.")

    if editable:
        st.divider()
        plate_col, other_col = st.columns(2)

        with plate_col:
            st.markdown("##### Printing plates")
            with session_scope() as db:
                rate = settings_service.plate_rate(db)
                plate_ccy = settings_service.plate_currency(db)
            st.caption(
                escape_markdown(f"Rate: {format_money(rate, plate_ccy)} per size per colour.")
            )
            with st.form("plate_charge"):
                p_a, p_b, p_c = st.columns(3)
                with p_a:
                    n_sizes = st.number_input("Sizes", min_value=0, value=len(lines) or 1)
                with p_b:
                    n_colours = st.number_input("Colours", min_value=0, value=0)
                with p_c:
                    n_designs = st.number_input("Designs", min_value=1, value=1)
                existing = st.checkbox("Existing plates available (no charge)")
                plate_visible = st.checkbox("Show on the customer document", value=True)
                plate_added = st.form_submit_button("Add plate charge", type="primary")

            if plate_added:
                try:
                    with session_scope() as db:
                        quotation_service.add_plate_charge(
                            db, user, db.get(Quotation, quotation_id),
                            number_of_sizes=int(n_sizes),
                            number_of_colours=int(n_colours),
                            number_of_designs=int(n_designs),
                            existing_plate_available=existing,
                            is_customer_visible=plate_visible,
                        )
                except (QuotationError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Plate charge added", icon="✅")
                    st.rerun()

        with other_col:
            st.markdown("##### Other charge")
            with st.form("other_charge"):
                charge_type = st.selectbox(
                    "Type",
                    [t for t in ChargeType if t is not ChargeType.PRINTING_PLATES],
                    format_func=lambda t: CHARGE_TYPE_DISPLAY_NAMES[t],
                )
                description = st.text_input("Description")
                c_a, c_b = st.columns(2)
                with c_a:
                    quantity = st.number_input("Quantity", min_value=0.0, value=1.0)
                with c_b:
                    charge_rate = st.number_input(
                        "Rate", min_value=0.0, step=0.01, format="%.2f", value=0.0
                    )
                taxable = st.checkbox("Taxable", value=True)
                visible = st.checkbox(
                    "Show on the customer document", value=True, key="other_visible"
                )
                charge_added = st.form_submit_button("Add charge", type="primary")

            if charge_added:
                try:
                    with session_scope() as db:
                        quotation_service.add_charge(
                            db, user, db.get(Quotation, quotation_id),
                            charge_type=charge_type,
                            description=description or None,
                            quantity=Decimal(str(quantity)),
                            rate=Decimal(str(charge_rate)),
                            is_taxable=taxable,
                            is_customer_visible=visible,
                        )
                except (QuotationError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Charge added", icon="✅")
                    st.rerun()

        if charges:
            remove_target = st.selectbox(
                "Remove a charge",
                charges,
                format_func=lambda c: f"{c['type']} — {format_money(c['amount'], ccy)}",
            )
            if st.button("Remove charge"):
                with session_scope() as db:
                    quotation_service.remove_charge(
                        db, user, db.get(Quotation, quotation_id), remove_target["id"]
                    )
                st.rerun()


# --------------------------------------------------------------------------- #
# Terms
# --------------------------------------------------------------------------- #

with terms_tab:
    st.caption(
        "Terms are copied onto the quotation, so editing the wording here never "
        "changes the master template."
    )

    if editable:
        selected_ids = [t["template_id"] for t in terms if t["template_id"]]
        chosen = st.multiselect(
            "Terms included",
            [t["id"] for t in all_templates],
            default=selected_ids,
            format_func=lambda tid: next(
                t["title"] for t in all_templates if t["id"] == tid
            ),
        )
        if st.button("Update selected terms"):
            try:
                with session_scope() as db:
                    quotation_service.set_terms(
                        db, user, db.get(Quotation, quotation_id), chosen
                    )
            except (QuotationError, PermissionDenied) as exc:
                st.error(str(exc))
            else:
                st.toast("Terms updated", icon="✅")
                st.rerun()

    for term in terms:
        with st.container(border=True):
            st.markdown(f"**{term['title']}**")
            if editable:
                body = st.text_area(
                    "Wording", value=term["body_text"], key=f"term_{term['id']}",
                    label_visibility="collapsed",
                )
                visible = st.checkbox(
                    "Show on the customer document", value=term["visible"],
                    key=f"term_visible_{term['id']}",
                )
                if st.button("Save wording", key=f"save_term_{term['id']}"):
                    with session_scope() as db:
                        quotation_service.edit_term(
                            db, user, db.get(Quotation, quotation_id), term["id"],
                            body_text=body, is_customer_visible=visible,
                        )
                    st.rerun()
            else:
                st.write(term["body_text"])


# --------------------------------------------------------------------------- #
# Review
# --------------------------------------------------------------------------- #

with review_tab:
    total_a, total_b, total_c, total_d = st.columns(4)
    total_a.metric("Subtotal", format_money(header["subtotal"], ccy))
    total_b.metric("Charges", format_money(header["charges_total"], ccy))
    total_c.metric("Tax", format_money(header["tax_amount"], ccy))
    total_d.metric("Grand total", format_money(header["grand_total"], ccy))

    if problems:
        st.error("This quotation is not ready to be sent:")
        for problem in problems:
            st.markdown(f"- {problem}")
    else:
        st.success("No structural problems — the quotation is complete.")

    if warnings:
        st.markdown("##### Checks")
        for w in warnings:
            renderer = {
                "BLOCKING": st.error, "WARNING": st.warning, "INFO": st.info
            }[w.severity.value]
            renderer(f"{w.icon} {w.message}")
        if pricing_service.blocking(warnings):
            st.caption(
                "Blocking items must be resolved, or overridden by a manager with a "
                "reason, before a final document can be produced."
            )

    st.divider()
    st.markdown("##### Status")
    st.write(
        f"Currently **{STATUS_DISPLAY_NAMES.get(header['status'], header['status'])}**"
    )

    if user.has(Perm.QUOTE_UPDATE_STATUS):
        from modules.constants import STATUS_TRANSITIONS, STATUSES_REQUIRING_NOTE

        allowed = sorted(STATUS_TRANSITIONS.get(header["status"], frozenset()))
        if allowed:
            new_status = st.selectbox(
                "Move to",
                allowed,
                format_func=lambda s: STATUS_DISPLAY_NAMES[s],
            )
            note_required = new_status in STATUSES_REQUIRING_NOTE
            note = st.text_area(
                "Note" + (" (required)" if note_required else " (optional)"),
                key="status_note",
            )
            if st.button("Update status", type="primary"):
                try:
                    with session_scope() as db:
                        quotation_service.change_status(
                            db, user, db.get(Quotation, quotation_id),
                            new_status, note or None,
                        )
                except (QuotationError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Status updated", icon="✅")
                    st.rerun()
        else:
            st.caption("This quotation has reached a final status.")

    st.divider()
    st.markdown("##### Approval")

    with session_scope() as db:
        quote = db.get(Quotation, quotation_id)
        blockers = approval_service.release_blockers(db, quote)
        triggers = approval_service.evaluate(db, quote, user)
        open_request = approval_service.open_approval_for(db, quotation_id)
        pending = open_request is not None
        pending_since = open_request.requested_at if open_request else None

    if pending:
        st.warning(
            f"Awaiting an approval decision (submitted "
            f"{pending_since:%d %b %Y %H:%M} UTC). It must be decided by someone other "
            "than the person who raised it."
        )
    elif header["status"] is QuotationStatus.DRAFT:
        if triggers:
            st.info(
                "Submitting this quotation will require approval because:\n"
                + "\n".join(f"- {t.message}" for t in triggers)
            )
        else:
            st.success(
                "Everything on this quotation is within your authority, so submitting "
                "will approve it directly."
            )
        submit_note = st.text_area("Note for the approver (optional)", key="submit_note")
        if st.button("Submit for approval", type="primary", disabled=bool(problems)):
            try:
                with session_scope() as db:
                    approval_service.submit(
                        db, db.get(Quotation, quotation_id), user, submit_note or None
                    )
            except (ApprovalError, QuotationError, PermissionDenied) as exc:
                st.error(str(exc))
            else:
                st.toast("Submitted", icon="✅")
                st.rerun()

    st.divider()
    st.markdown("##### Document")

    if blockers:
        st.warning(
            "Only a DRAFT-marked copy is available until these are resolved:\n"
            + "\n".join(f"- {b}" for b in blockers)
        )
    else:
        st.success("This quotation can be issued as a final document.")

    st.caption(
        "Choose the format. Both are produced from the same content, so they cannot "
        "disagree. The PDF is the record of what was sent — a Word file can be edited "
        "by whoever receives it."
    )

    format_col, action_col = st.columns([1, 2])
    with format_col:
        chosen_format = st.radio(
            "Format",
            list(document_service.DocumentFormat),
            format_func=lambda f: document_service.FORMAT_LABELS[f],
            horizontal=True,
        )

    with action_col:
        if st.button("Generate document", type="primary"):
            try:
                with session_scope() as db:
                    quote = db.get(Quotation, quotation_id)
                    generated = document_service.generate(
                        db, user, quote, chosen_format
                    )
                    payload = {
                        "filename": generated.filename,
                        "mime": generated.mime_type,
                        "data": generated.data,
                        "draft": generated.is_draft,
                    }
                    # Issuing locks the quotation, so it happens only when a
                    # final document has actually been produced.
                    if not generated.is_draft and not quote.is_locked:
                        attachments = document_service.stored_documents(db, quote.id)
                        revision_service.issue(
                            db, user, quote,
                            pdf_attachment_id=attachments[0].id if attachments else None,
                        )
            except (ApprovalError, PermissionDenied, RevisionError) as exc:
                st.error(str(exc))
            else:
                st.session_state["generated_document"] = payload
                st.rerun()

    payload = st.session_state.get("generated_document")
    if payload:
        st.download_button(
            f"Download {payload['filename']}",
            data=payload["data"],
            file_name=payload["filename"],
            mime=payload["mime"],
            type="primary",
        )
        if payload["draft"]:
            st.caption("This copy is marked DRAFT and has not been archived.")
        else:
            st.caption("Archived, and recorded against this revision.")

    with session_scope() as db:
        archived = document_service.stored_documents(db, quotation_id)
        archive_rows = [
            {
                "id": a.id,
                "File": a.file_name,
                "Size": f"{(a.size_bytes or 0) / 1024:.0f} KB",
                "Generated": a.uploaded_at,
            }
            for a in archived
        ]

    if archive_rows:
        with st.expander(f"Archived documents ({len(archive_rows)})"):
            st.dataframe(
                [
                    {
                        "File": r["File"], "Size": r["Size"],
                        "Generated": r["Generated"].strftime("%d %b %Y %H:%M"),
                    }
                    for r in archive_rows
                ],
                width="stretch", hide_index=True,
            )
            re_pick = st.selectbox(
                "Re-download", archive_rows, format_func=lambda r: r["File"]
            )
            if st.button("Fetch"):
                try:
                    with session_scope() as db:
                        again = document_service.fetch(db, user, re_pick["id"])
                except (FileNotFoundError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.download_button(
                        f"Download {again.filename}",
                        data=again.data,
                        file_name=again.filename,
                        mime=again.mime_type,
                    )

    # ----------------------------------------------------------------- #
    # Customer portal — summary only
    # ----------------------------------------------------------------- #
    # A compact read-only status here for discoverability; issuing, revoking
    # and editing live on the dedicated Customer Portal page. Read-only is also
    # what keeps this safe: this page holds unsaved edits in widgets, and a
    # portal control acting on them would publish a quotation that differs from
    # what is stored.
    if user.has(Perm.QUOTE_PORTAL_PREVIEW):
        st.divider()
        st.markdown("##### Customer portal")

        with session_scope() as db:
            quote = db.get(Quotation, quotation_id)
            readiness = portal_readiness.check(db, quote)
            live_links = portal_service.active_tokens(db, quotation_id)
            selectable = portal_service.selectable_items(quote)
            latest_response = (
                sorted(quote.portal_responses, key=lambda r: r.submitted_at)[-1]
                if quote.portal_responses else None
            )
            accepted_response = next(
                (
                    r for r in sorted(quote.portal_responses, key=lambda r: r.submitted_at)
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
                for row in pricing_snapshot.totals_summary(quote, accepted_response)
            ]
            summary = quote_send_service.delivery_summary(db, user, quotation_id)
            delivery = {
                "has_activity": summary.has_activity,
                "status_label": summary.status_label,
                "recipient": summary.recipient_email or "-",
                "at": format_datetime(summary.at),
                "is_sent": summary.is_sent,
                "is_failed": summary.is_failed,
            }
            portal_summary = {
                "status": STATUS_DISPLAY_NAMES.get(quote.status, str(quote.status)),
                "deposit_pct": quote.deposit_pct,
                "optional_count": len(selectable),
                "item_count": len(quote.items),
                "link_expiry": (
                    format_datetime(live_links[0].expires_at) if live_links else ""
                ),
                "link_views": live_links[0].view_count if live_links else 0,
                "outstanding": len(readiness.outstanding),
                "may_issue": readiness.may_issue_link,
                "response": (
                    latest_response.response_type.value.replace("_", " ").title()
                    if latest_response else ""
                ),
            }

        # The three totals first: an employee reading this section is usually
        # answering "what is this worth", and the answer depends entirely on
        # which of the three is meant.
        for column, row in zip(st.columns(len(totals_rows)), totals_rows, strict=True):
            column.metric(row["label"], row["amount"], help=row["help"])

        col_status, col_config, col_link = st.columns(3)
        with col_status:
            st.caption("Quotation status")
            st.markdown(f"**{portal_summary['status']}**")
            if portal_summary["response"]:
                st.caption("Customer response")
                st.markdown(f"**{portal_summary['response']}**")
        with col_config:
            st.caption("Offered items")
            st.markdown(
                f"**{portal_summary['optional_count']} optional** of "
                f"{portal_summary['item_count']}"
            )
            st.caption("Deposit")
            st.markdown(f"**{format_percent(portal_summary['deposit_pct'])}**")
        with col_link:
            st.caption("Customer link")
            if live_links:
                st.markdown("**Active**")
                st.caption(
                    f"Expires {portal_summary['link_expiry']} · "
                    f"{portal_summary['link_views']} view(s)"
                )
            else:
                st.markdown("**None issued**")

        if portal_summary["outstanding"]:
            message = (
                f"{portal_summary['outstanding']} readiness item(s) outstanding."
            )
            if portal_summary["may_issue"]:
                st.warning(message + " Publishing is still allowed here.")
            else:
                st.error(message + " Publishing is blocked until these are complete.")
        else:
            st.success("Company details are complete.")

        # Compact on purpose. Quotation history answers "did this reach the
        # customer"; the queue console, the retry controls and the message
        # bodies live on the Customer Portal page, where somebody has gone
        # looking for them.
        if delivery["has_activity"]:
            mark = "✅" if delivery["is_sent"] else ("⚠️" if delivery["is_failed"] else "🕓")
            st.caption("Latest email")
            st.markdown(
                escape_markdown(
                    f"{mark} **{delivery['status_label']}** — {delivery['recipient']}"
                    f" · {delivery['at']}"
                )
            )
        elif user.has(Perm.QUOTE_PORTAL_VIEW_DELIVERY):
            st.caption("Latest email")
            st.markdown("**Not sent**")

        # No plaintext token is shown: only its hash is stored, so a link issued
        # earlier cannot be redisplayed by this page or any other.
        st.caption(
            "Issuing, revoking, sending and retrying live on the Customer "
            "Portal page. A link is shown once, when it is created."
        )
        if st.button("Manage Customer Portal", key="goto_portal"):
            st.session_state["portal_quote"] = {"id": quotation_id}
            st.switch_page("pages/12_Customer_Portal.py")

    if header["is_locked"]:
        st.divider()
        st.markdown("##### Revisions")
        with session_scope() as db:
            quote = db.get(Quotation, quotation_id)
            siblings = revision_service.revisions_for(db, quote.root_quotation_id)
            sibling_rows = [
                {
                    "id": r.id,
                    "label": r.revision_label,
                    "status": STATUS_DISPLAY_NAMES.get(r.status, str(r.status)),
                    "total": format_money(r.grand_total, r.currency),
                    "current": r.is_current_revision,
                }
                for r in siblings
            ]
        st.dataframe(
            [
                {
                    "Revision": r["label"], "Status": r["status"],
                    "Total": r["total"], "Current": "Yes" if r["current"] else "",
                }
                for r in sibling_rows
            ],
            width="stretch", hide_index=True,
        )

        if user.has(Perm.QUOTE_CREATE_REVISION):
            reason = st.text_input(
                "Reason for the revision", key="revision_reason",
                placeholder="Required — recorded against the new revision",
            )
            if st.button("Create a new revision"):
                try:
                    with session_scope() as db:
                        revised = revision_service.create_revision(
                            db, user, db.get(Quotation, quotation_id), reason
                        )
                        new_id = revised.id
                except (RevisionError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.session_state.pop("generated_document", None)
                    _open(new_id)
                    st.rerun()


# --------------------------------------------------------------------------- #
# Customer response — recorded manually
# --------------------------------------------------------------------------- #

with tracking_tab:
    st.caption(
        "Customers do not use this application. Everything here is recorded by an "
        "employee after sending the quotation — nothing is tracked automatically."
    )

    with session_scope() as db:
        logs = [
            {
                "Sent": format_date(log.date_sent),
                "Method": str(log.send_method or "-").replace("_", " ").title(),
                "Response": str(log.response).replace("_", " ").title(),
                "Responded": format_date(log.response_date),
                "Follow up": format_date(log.follow_up_date),
                "Competitor": log.competitor or "-",
                "Notes": log.notes or "",
            }
            for log in db.get(Quotation, quotation_id).response_logs
        ]

    if logs:
        st.dataframe(logs, width="stretch", hide_index=True)
    else:
        st.info("Nothing recorded yet.")

    if user.has(Perm.QUOTE_UPDATE_STATUS):
        with st.form("customer_response"):
            send_a, send_b, send_c = st.columns(3)
            with send_a:
                date_sent = st.date_input("Date sent", value=dt.date.today())
                send_method = st.selectbox(
                    "Sent by",
                    list(SendMethod),
                    format_func=lambda m: str(m).replace("_", " ").title(),
                )
            with send_b:
                response = st.selectbox(
                    "Customer response",
                    list(CustomerResponse),
                    format_func=lambda r: str(r).replace("_", " ").title(),
                )
                response_date = st.date_input("Response date", value=None)
            with send_c:
                follow_up = st.date_input("Follow-up date", value=None)
                competitor = st.text_input("Competitor, if known")
            loss_reason = st.text_input("Reason, if lost")
            response_notes = st.text_area("Notes")
            logged = st.form_submit_button("Record response", type="primary")

        if logged:
            try:
                with session_scope() as db:
                    db.add(
                        CustomerResponseLog(
                            quotation_id=quotation_id,
                            date_sent=date_sent,
                            sent_by_id=user.id,
                            send_method=send_method,
                            response=response,
                            response_date=response_date,
                            loss_reason=loss_reason or None,
                            competitor=competitor or None,
                            follow_up_date=follow_up,
                            notes=response_notes or None,
                            created_by_id=user.id,
                        )
                    )
                    record_audit(
                        db, user, AuditAction.CUSTOMER_RESPONSE_LOGGED,
                        EntityType.QUOTATION, quotation_id,
                        new_value={"response": str(response), "sent": str(date_sent)},
                    )
            except PermissionDenied as exc:
                st.error(str(exc))
            else:
                st.toast("Response recorded", icon="✅")
                st.rerun()
