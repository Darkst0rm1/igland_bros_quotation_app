"""Company Settings — identity, document defaults and tunable thresholds.

Nothing about Igland Bros is compiled into the application. Everything here is
data, and anything left blank is omitted from the quotation document rather
than printed as a placeholder.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import streamlit as st

from modules import settings_service, shipping_service
from modules.audit_service import record_audit
from modules.authorization import PermissionDenied
from modules.constants import (
    CONTAINER_SIZE_LABELS,
    CONTAINER_TYPE_LABELS,
    LOADING_METHOD_LABELS,
    SUPPORTED_CURRENCIES,
    AuditAction,
    ContainerSize,
    ContainerType,
    EntityType,
    Incoterm,
    LoadingMethod,
    Perm,
)
from modules.database import session_scope
from modules.document_model import AVAILABLE_COLUMNS, DEFAULT_COLUMNS
from modules.numbering import NumberFormatError, render, validate_format
from modules.session import page_header, require_page
from modules.shipping_service import ShippingError
from modules.storage import StorageError, build_key, get_storage, validate_upload

user = require_page(Perm.SETTINGS_MANAGE)
page_header("Company Settings", "Identity, document defaults and thresholds")

with session_scope() as db:
    settings = settings_service.get_company_settings(db)
    if settings is None:
        st.error(
            "Company settings have not been seeded. Run `python -m seeds.bootstrap`."
        )
        st.stop()
    current = {
        "legal_name": settings.legal_name or "",
        "trading_name": settings.trading_name or "",
        "address_line1": settings.address_line1 or "",
        "address_line2": settings.address_line2 or "",
        "city": settings.city or "",
        "province": settings.province or "",
        "postal_code": settings.postal_code or "",
        "country": settings.country or "",
        "phone": settings.phone or "",
        "email": settings.email or "",
        "website": settings.website or "",
        "tax_number": settings.tax_number or "",
        "logo_key": settings.logo_key,
        "signature_name": settings.signature_name or "",
        "signature_title": settings.signature_title or "",
        "default_currency": settings.default_currency,
        "default_quote_validity_days": int(settings.default_quote_validity_days),
        "quote_number_format": settings.quote_number_format,
        "printing_plate_rate": settings.printing_plate_rate,
        "printing_plate_currency": settings.printing_plate_currency,
        "pdf_page_size": settings.pdf_page_size,
        "pdf_footer_text": settings.pdf_footer_text or "",
        "pdf_confidentiality_text": settings.pdf_confidentiality_text or "",
        "pdf_thank_you_text": settings.pdf_thank_you_text or "",
        "pdf_show_acceptance_line": bool(settings.pdf_show_acceptance_line),
        "columns": (
            (settings.pdf_column_set or {}).get("columns") or list(DEFAULT_COLUMNS)
        ),
        "is_placeholder": settings.is_placeholder,
    }
    carriers = [
        {
            "id": line.id, "name": line.name,
            "is_active": line.is_active, "sort_order": line.sort_order,
        }
        for line in shipping_service.shipping_lines(db, include_inactive=True)
    ]
    shipping_defaults = {
        "incoterm": settings_service.default_incoterm(db),
        "incoterm_place": settings_service.default_incoterm_place(db),
        "origin_country": settings_service.default_origin_country(db),
        "port_of_loading": settings_service.default_port_of_loading(db),
        "container_size": settings_service.default_container_size(db),
        "container_type": settings_service.default_container_type(db),
        "loading_method": settings_service.default_loading_method(db),
    }
    tunables = {
        "tier_container_scope": settings_service.tier_container_scope(db),
        "piece_pack_tolerance": settings_service.piece_pack_tolerance(db),
        "max_items_per_container": settings_service.max_items_per_container(db),
        "max_custom_discount_pct": settings_service.max_custom_discount_pct(db),
    }

if current["is_placeholder"]:
    st.warning(
        "These are the seeded placeholder details. Fill in what applies and save — "
        "anything left blank is omitted from the quotation document rather than "
        "printed at a customer."
    )

identity_tab, document_tab, thresholds_tab, shipping_tab = st.tabs(
    ["Company identity", "Document defaults", "Thresholds", "Shipping"]
)


def _save(**fields) -> None:
    from modules.models import CompanySettings

    try:
        with session_scope() as db:
            row = db.get(CompanySettings, 1)
            before = {key: getattr(row, key) for key in fields}
            for key, value in fields.items():
                setattr(row, key, value)
            row.is_placeholder = False
            row.updated_by_id = user.id
            record_audit(
                db, user, AuditAction.SETTINGS_CHANGED, EntityType.COMPANY_SETTINGS, 1,
                old_value=before, new_value=fields,
            )
    except PermissionDenied as exc:
        st.error(str(exc))
    else:
        st.toast("Settings saved", icon="✅")
        st.rerun()


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

with identity_tab:
    with st.form("company_identity"):
        col_a, col_b = st.columns(2)
        with col_a:
            legal_name = st.text_input("Legal name *", value=current["legal_name"])
            trading_name = st.text_input("Trading name", value=current["trading_name"])
            address_line1 = st.text_input(
                "Address line 1", value=current["address_line1"]
            )
            address_line2 = st.text_input(
                "Address line 2", value=current["address_line2"]
            )
            city = st.text_input("City", value=current["city"])
        with col_b:
            province = st.text_input("Province / state", value=current["province"])
            postal_code = st.text_input(
                "Postal / ZIP code", value=current["postal_code"]
            )
            country = st.text_input("Country", value=current["country"])
            tax_number = st.text_input("Tax number", value=current["tax_number"])

        contact_a, contact_b, contact_c = st.columns(3)
        with contact_a:
            phone = st.text_input("Phone", value=current["phone"])
        with contact_b:
            email = st.text_input("Email", value=current["email"])
        with contact_c:
            website = st.text_input("Website", value=current["website"])

        sign_a, sign_b = st.columns(2)
        with sign_a:
            signature_name = st.text_input(
                "Signatory name", value=current["signature_name"],
                help="Used under 'Prepared by' when the document carries no other name.",
            )
        with sign_b:
            signature_title = st.text_input(
                "Signatory title", value=current["signature_title"]
            )

        identity_saved = st.form_submit_button("Save identity", type="primary")

    if identity_saved:
        if not legal_name.strip():
            st.error("A legal name is required — it heads every quotation.")
        else:
            _save(
                legal_name=legal_name.strip(),
                trading_name=trading_name.strip() or None,
                address_line1=address_line1.strip(),
                address_line2=address_line2.strip(),
                city=city.strip(), province=province.strip(),
                postal_code=postal_code.strip(), country=country.strip(),
                phone=phone.strip(), email=email.strip(), website=website.strip(),
                tax_number=tax_number.strip(),
                signature_name=signature_name.strip() or None,
                signature_title=signature_title.strip() or None,
            )

    st.divider()
    st.markdown("##### Logo")
    st.caption(
        "Optional. Without one the document header is typographic — a deliberate "
        "design, not a degraded one."
    )
    if current["logo_key"]:
        try:
            st.image(get_storage().get(current["logo_key"]), width=220)
        except StorageError:
            st.warning("The stored logo could not be read.")

    uploaded_logo = st.file_uploader("Upload a logo", type=["png", "jpg", "jpeg"])
    if uploaded_logo is not None and st.button("Save logo"):
        data = uploaded_logo.getvalue()
        try:
            safe_name = validate_upload(data, uploaded_logo.name)
            key = build_key("logos", safe_name)
            get_storage().put(key, data, uploaded_logo.type)
        except StorageError as exc:
            st.error(str(exc))
        else:
            _save(logo_key=key)


# --------------------------------------------------------------------------- #
# Document defaults
# --------------------------------------------------------------------------- #

with document_tab:
    with st.form("document_defaults"):
        doc_a, doc_b = st.columns(2)
        with doc_a:
            default_currency = st.selectbox(
                "Default currency", SUPPORTED_CURRENCIES,
                index=SUPPORTED_CURRENCIES.index(current["default_currency"])
                if current["default_currency"] in SUPPORTED_CURRENCIES else 0,
            )
            validity_days = st.number_input(
                "Default quotation validity (days)", min_value=1, max_value=365,
                value=current["default_quote_validity_days"],
            )
            number_format = st.text_input(
                "Quotation number format", value=current["quote_number_format"],
                help=(
                    "Placeholders: {YYYY} {YY} {MM} {SEQ} — {SEQ:04d} pads to four "
                    "digits. Including a year restarts the sequence each year."
                ),
            )
        with doc_b:
            page_size = st.selectbox(
                "Page size", ["A4", "LETTER"],
                index=0 if (current["pdf_page_size"] or "A4").upper() == "A4" else 1,
            )
            plate_rate = st.number_input(
                "Printing-plate rate (per size per colour)",
                min_value=0.0, step=10.0, format="%.2f",
                value=float(current["printing_plate_rate"]),
            )
            plate_currency = st.selectbox(
                "Plate rate currency", SUPPORTED_CURRENCIES,
                index=SUPPORTED_CURRENCIES.index(current["printing_plate_currency"])
                if current["printing_plate_currency"] in SUPPORTED_CURRENCIES else 0,
            )

        st.markdown("###### Product table columns")
        st.caption(
            "The reference PDF quotes price per case; Igland quotes per pack and per "
            "piece. The column set is configuration rather than a fixed layout, and "
            "applies to both the PDF and the Word document."
        )
        chosen_columns = st.multiselect(
            "Columns, in order", list(AVAILABLE_COLUMNS),
            default=current["columns"],
            format_func=lambda key: AVAILABLE_COLUMNS[key],
        )

        confidentiality = st.text_input(
            "Confidentiality wording (document footer)",
            value=current["pdf_confidentiality_text"],
        )
        thank_you = st.text_input("Closing line", value=current["pdf_thank_you_text"])
        footer_text = st.text_input(
            "Extra footer text", value=current["pdf_footer_text"]
        )
        acceptance_line = st.checkbox(
            "Include a printed customer acceptance line",
            value=current["pdf_show_acceptance_line"],
            help=(
                "A printed signature block only. There is no electronic signature and "
                "nothing linking back to this application."
            ),
        )

        document_saved = st.form_submit_button("Save document defaults", type="primary")

    if document_saved:
        try:
            validate_format(number_format)
        except NumberFormatError as exc:
            st.error(str(exc))
        else:
            if not chosen_columns:
                st.error("The product table needs at least one column.")
            else:
                _save(
                    default_currency=default_currency,
                    default_quote_validity_days=int(validity_days),
                    quote_number_format=number_format.strip(),
                    pdf_page_size=page_size,
                    printing_plate_rate=Decimal(str(plate_rate)),
                    printing_plate_currency=plate_currency,
                    pdf_column_set={"columns": chosen_columns},
                    pdf_confidentiality_text=confidentiality.strip(),
                    pdf_thank_you_text=thank_you.strip(),
                    pdf_footer_text=footer_text.strip(),
                    pdf_show_acceptance_line=acceptance_line,
                )

    try:
        validate_format(current["quote_number_format"])
        preview = render(current["quote_number_format"], 1, dt.date.today())
        st.caption(f"The next quotation number will look like: **{preview}**")
    except NumberFormatError as exc:
        st.error(f"The saved number format is invalid: {exc}")


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #

with thresholds_tab:
    st.caption(
        "These drive the pricing warnings. They are stored as data, so changing one "
        "is not a code release."
    )

    with st.form("thresholds"):
        scope = st.selectbox(
            "Container check counts across",
            ["quotation", "line"],
            index=0 if tunables["tier_container_scope"] == "quotation" else 1,
            help=(
                "Whether a three- or eight-container tier compares against the whole "
                "quotation or each line. Commercially the price is earned by the "
                "order, so the default is the whole quotation."
            ),
        )
        tolerance = st.number_input(
            "Piece / pack price tolerance",
            min_value=0.0, max_value=1.0, step=0.0001, format="%.4f",
            value=float(tunables["piece_pack_tolerance"]),
            help=(
                "How far a piece price may differ from pack ÷ case pack before it is "
                "flagged. The reference workbook's own columns disagree by 0.0001 on "
                "25 of its 69 price pairs, so a zero tolerance would flag a third of "
                "the catalogue."
            ),
        )
        mix_limit = st.number_input(
            "Maximum products per container", min_value=1, max_value=50,
            value=tunables["max_items_per_container"],
            help="From the price list: 'Containers to be filled with only three items.'",
        )
        custom_floor = st.number_input(
            "Custom price may go this far below standard (%)",
            min_value=0.0, max_value=100.0, step=1.0,
            value=float(tunables["max_custom_discount_pct"]),
            help="Beyond this, the line raises a blocking warning and needs approval.",
        )
        thresholds_saved = st.form_submit_button("Save thresholds", type="primary")

    if thresholds_saved:
        try:
            with session_scope() as db:
                settings_service.set_setting(
                    db, user, "tier_container_scope", scope,
                    value_type="string", category="pricing",
                )
                settings_service.set_setting(
                    db, user, "piece_pack_tolerance", f"{tolerance:.4f}",
                    value_type="decimal", category="pricing",
                )
                settings_service.set_setting(
                    db, user, "max_items_per_container", int(mix_limit),
                    value_type="int", category="pricing",
                )
                settings_service.set_setting(
                    db, user, "max_custom_discount_pct", f"{custom_floor:g}",
                    value_type="decimal", category="approval",
                )
        except PermissionDenied as exc:
            st.error(str(exc))
        else:
            st.toast("Thresholds saved", icon="✅")
            st.rerun()

    st.divider()
    st.caption(
        "Approval limits per role are on the **Users & Permissions** page. Tax and "
        "exchange rates are maintained by Finance."
    )


# --------------------------------------------------------------------------- #
# Shipping
# --------------------------------------------------------------------------- #

with shipping_tab:
    can_manage_lines = user.has(Perm.SHIPPING_LINE_MANAGE)

    st.markdown("##### Shipping lines")
    st.caption(
        "The carriers offered when building a container row. A quotation may also "
        "name a carrier that is not on this list."
    )
    st.dataframe(
        [
            {
                "Shipping line": c["name"],
                "Active": "Yes" if c["is_active"] else "No",
                "Order": str(c["sort_order"]),
            }
            for c in carriers
        ],
        width="stretch",
        hide_index=True,
    )

    if not can_manage_lines:
        st.info("Managing shipping lines requires the shipping_line.manage permission.")
    else:
        add_col, edit_col = st.columns(2)

        with add_col:
            st.markdown("###### Add a shipping line")
            with st.form("add_shipping_line"):
                new_name = st.text_input("Name *", placeholder="e.g. Hapag-Lloyd")
                new_order = st.number_input(
                    "Display order", min_value=0, max_value=999, value=100, step=10
                )
                line_added = st.form_submit_button("Add", type="primary")

            if line_added:
                try:
                    with session_scope() as db:
                        shipping_service.create_shipping_line(
                            db, user, new_name, sort_order=int(new_order)
                        )
                except (ShippingError, PermissionDenied) as exc:
                    st.error(str(exc))
                else:
                    st.toast("Shipping line added", icon="✅")
                    st.rerun()

        with edit_col:
            st.markdown("###### Edit a shipping line")
            if not carriers:
                st.caption("None recorded yet.")
            else:
                target = st.selectbox(
                    "Shipping line", carriers, format_func=lambda c: c["name"]
                )
                with st.form("edit_shipping_line"):
                    edit_name = st.text_input("Name", value=target["name"])
                    edit_order = st.number_input(
                        "Display order", min_value=0, max_value=999,
                        value=int(target["sort_order"]), step=10,
                    )
                    edit_active = st.checkbox(
                        "Offered when building a container",
                        value=target["is_active"],
                        help=(
                            "Turning this off hides the carrier from new quotations. "
                            "Existing quotations keep the carrier they were booked "
                            "with."
                        ),
                    )
                    line_saved = st.form_submit_button("Save", type="primary")

                if line_saved:
                    try:
                        with session_scope() as db:
                            shipping_service.update_shipping_line(
                                db, user, target["id"],
                                name=edit_name, is_active=edit_active,
                                sort_order=int(edit_order),
                            )
                    except (ShippingError, PermissionDenied) as exc:
                        st.error(str(exc))
                    else:
                        st.toast("Shipping line saved", icon="✅")
                        st.rerun()

                if st.button(f"Remove {target['name']}"):
                    try:
                        with session_scope() as db:
                            shipping_service.delete_shipping_line(
                                db, user, target["id"]
                            )
                    except (ShippingError, PermissionDenied) as exc:
                        st.error(str(exc))
                    else:
                        st.toast("Shipping line removed", icon="✅")
                        st.rerun()
                st.caption(
                    "Removing a carrier hides it from new quotations but leaves "
                    "historical ones intact — it is a soft delete, so a quotation "
                    "booked with it still reads correctly."
                )

    st.divider()
    st.markdown("##### Defaults for a new shipment")
    st.caption(
        "Applied when shipping is first added to a quotation, and editable there. "
        "Seeded from the price list: FOB Çerkezköy, 40 ft high-cube dry containers, "
        "floor loaded."
    )

    with st.form("shipping_defaults"):
        default_a, default_b = st.columns(2)
        with default_a:
            default_incoterm = st.selectbox(
                "Incoterms", list(Incoterm),
                index=list(Incoterm).index(shipping_defaults["incoterm"]),
            )
            default_place = st.text_input(
                "Named place", value=shipping_defaults["incoterm_place"]
            )
            default_origin = st.text_input(
                "Country of origin", value=shipping_defaults["origin_country"]
            )
            default_port = st.text_input(
                "Port of loading", value=shipping_defaults["port_of_loading"]
            )
        with default_b:
            default_size = st.selectbox(
                "Container size", list(ContainerSize),
                index=list(ContainerSize).index(shipping_defaults["container_size"]),
                format_func=lambda s: CONTAINER_SIZE_LABELS[s],
            )
            default_type = st.selectbox(
                "Container type", list(ContainerType),
                index=list(ContainerType).index(shipping_defaults["container_type"]),
                format_func=lambda c: CONTAINER_TYPE_LABELS[c],
            )
            default_loading = st.selectbox(
                "Loading method", list(LoadingMethod),
                index=list(LoadingMethod).index(shipping_defaults["loading_method"]),
                format_func=lambda m: LOADING_METHOD_LABELS[m],
            )
        defaults_saved = st.form_submit_button("Save shipping defaults", type="primary")

    if defaults_saved:
        try:
            with session_scope() as db:
                for key, value in (
                    ("default_incoterm", default_incoterm.value),
                    ("default_incoterm_place", default_place.strip()),
                    ("default_origin_country", default_origin.strip()),
                    ("default_port_of_loading", default_port.strip()),
                    ("default_container_size", default_size.value),
                    ("default_container_type", default_type.value),
                    ("default_loading_method", default_loading.value),
                ):
                    settings_service.set_setting(
                        db, user, key, value, value_type="string", category="shipping"
                    )
        except PermissionDenied as exc:
            st.error(str(exc))
        else:
            st.toast("Shipping defaults saved", icon="✅")
            st.rerun()
