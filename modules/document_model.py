"""The quotation document, independent of how it is rendered.

The employee chooses PDF or Word at download time. Both formats are built from
the structure in this module, so they cannot disagree about what the quotation
says — a Word file contradicting the PDF of the same quotation number would be
worse than offering only one format.

**Cost and margin never enter this model.** There is no field for them, so
there is no code path by which an internal figure can reach either renderer.
A test asserts that against the produced bytes of both formats.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from modules import settings_service
from modules.constants import STATUS_DISPLAY_NAMES, ISSUED_STATUSES, QuotationStatus
from modules.models import Quotation
from modules.utilities import compose_spec_text, format_money, format_quantity

#: Columns the product table can show, and how each is derived. The set in use
#: is a company setting: the reference PDF quotes price per case in CAD, while
#: The company quotes per pack and per piece FOB, so a fixed layout would be wrong
#: for one of them.
AVAILABLE_COLUMNS: dict[str, str] = {
    "item": "Item",
    "description": "Description",
    "size": "Size",
    "board_quality": "Board quality",
    "pack_size": "Pack size",
    "moq": "MOQ",
    "quantity_packs": "Qty (packs)",
    "quantity_pieces": "Qty (pieces)",
    "containers": "Containers",
    "price_per_pack": "Price / pack",
    "price_per_piece": "Price / piece",
    "line_total": "Line total",
    "spec": "Specification",
}

DEFAULT_COLUMNS = [
    "item", "description", "size", "pack_size",
    "quantity_packs", "price_per_pack", "price_per_piece", "line_total",
]

#: Columns whose values are numeric and should be right-aligned.
NUMERIC_COLUMNS = frozenset({
    "pack_size", "moq", "quantity_packs", "quantity_pieces", "containers",
    "price_per_pack", "price_per_piece", "line_total",
})


@dataclass(frozen=True)
class DocumentCompany:
    name: str
    address_lines: list[str] = field(default_factory=list)
    phone: str = ""
    email: str = ""
    website: str = ""
    tax_number: str = ""
    logo_bytes: bytes | None = None

    @property
    def contact_line(self) -> str:
        """Phone / email / website, omitting whatever is blank.

        Blank fields are dropped rather than printed as placeholders — with no
        branding supplied, a header that quietly shows less is better than one
        that shows "Address not set" to a customer.
        """
        return "  ·  ".join(p for p in (self.phone, self.email, self.website) if p)


@dataclass(frozen=True)
class DocumentCustomer:
    company: str
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    billing_address: str = ""
    shipping_address: str = ""
    project: str = ""
    brand: str = ""
    distributor: str = ""
    purchase_order: str = ""


@dataclass(frozen=True)
class DocumentLine:
    """One row of the product table, already formatted for display."""

    values: dict[str, str]

    def cells(self, columns: list[str]) -> list[str]:
        return [self.values.get(key, "") for key in columns]


@dataclass(frozen=True)
class DocumentTotal:
    label: str
    amount: str
    emphasis: bool = False


@dataclass(frozen=True)
class DocumentTerm:
    title: str
    body: str


@dataclass(frozen=True)
class DocumentShipping:
    """The customer-facing view of the shipping arrangement.

    Freight cost appears **only** when the shipment is explicitly marked
    customer-visible. There is no field for internal freight, so no renderer
    can print it by accident.
    """

    columns: list[str]
    headings: list[str]
    rows: list[list[str]]
    summary: list[tuple[str, str]] = field(default_factory=list)
    notes: str = ""
    freight_statement: str = ""

    @property
    def numeric_indexes(self) -> list[int]:
        return [
            i for i, key in enumerate(self.columns)
            if key in {"quantity", "transit", "freight"}
        ]


@dataclass(frozen=True)
class QuotationDocument:
    quote_number: str
    revision_label: str
    quote_date: dt.date
    valid_until: dt.date | None
    status_label: str
    currency: str

    company: DocumentCompany
    customer: DocumentCustomer

    columns: list[str]
    column_headings: list[str]
    lines: list[DocumentLine]
    totals: list[DocumentTotal]
    terms: list[DocumentTerm]
    customer_notes: str = ""
    #: ``None`` unless the quotation has a shipment marked to show. Existing
    #: quotations therefore produce byte-identical documents.
    shipping: DocumentShipping | None = None

    prepared_by: str = ""
    prepared_by_title: str = ""
    approved_by: str = ""
    signature_name: str = ""
    signature_title: str = ""

    footer_text: str = ""
    confidentiality_text: str = ""
    thank_you_text: str = ""
    show_acceptance_line: bool = False

    #: Stamped DRAFT until the quotation has been approved and issued.
    is_draft: bool = True

    @property
    def title(self) -> str:
        return "QUOTATION"

    @property
    def file_stem(self) -> str:
        """Filename without extension, safe on every platform."""
        import re

        stem = f"{self.quote_number}_Rev{self.revision_label.split()[-1]}"
        if self.customer.company:
            stem += f"_{self.customer.company}"
        return re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:120]

    @property
    def numeric_column_indexes(self) -> list[int]:
        return [i for i, key in enumerate(self.columns) if key in NUMERIC_COLUMNS]


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #

#: Columns for the shipping table. Freight is included only when the shipment
#: says the customer may see it.
SHIPPING_COLUMNS: dict[str, str] = {
    "carrier": "Shipping line",
    "size": "Container size",
    "type": "Container type",
    "quantity": "Quantity",
    "loading": "Port of loading",
    "discharge": "Port of discharge",
    "transit": "Transit time",
    "freight": "Freight",
}


def _build_shipping(session: Session, quotation: Quotation) -> DocumentShipping | None:
    """The shipping section, or ``None`` when it should not appear.

    Opt-in per quotation. Anything not filled in is dropped from the table
    rather than printed empty, so a partly-completed shipment still reads
    cleanly.
    """
    from modules.constants import FREIGHT_METHOD_LABELS, FreightMethod

    shipment = quotation.shipment
    if shipment is None or not shipment.show_on_document:
        return None

    containers = sorted(shipment.containers, key=lambda c: c.sort_order)
    if not containers:
        return None

    show_freight = shipment.customer_visible_freight
    wanted = ["carrier", "size", "type", "quantity"]
    if any(c.port_of_loading for c in containers) or shipment.port_of_loading:
        wanted.append("loading")
    if any(c.port_of_discharge for c in containers) or shipment.port_of_discharge:
        wanted.append("discharge")
    if any(c.transit_days for c in containers):
        wanted.append("transit")
    if show_freight:
        wanted.append("freight")

    rows: list[list[str]] = []
    for container in containers:
        values = {
            "carrier": container.carrier_name,
            "size": container.size_label,
            "type": container.type_label,
            "quantity": format_quantity(container.container_count),
            "loading": container.port_of_loading or shipment.port_of_loading or "",
            "discharge": container.port_of_discharge or shipment.port_of_discharge or "",
            "transit": (
                f"{container.transit_days} days" if container.transit_days else ""
            ),
            "freight": (
                format_money(container.freight_cost, shipment.freight_currency)
                if show_freight else ""
            ),
        }
        rows.append([values[key] for key in wanted])

    summary: list[tuple[str, str]] = []
    if shipment.incoterm:
        place = f" {shipment.incoterm_place}" if shipment.incoterm_place else ""
        summary.append(("Incoterms", f"{shipment.incoterm.value}{place} (Incoterms 2020)"))
    if shipment.origin_country:
        summary.append(("Country of origin", shipment.origin_country))
    if shipment.final_destination:
        summary.append(("Final destination", shipment.final_destination))
    if shipment.loading_method:
        from modules.constants import LOADING_METHOD_LABELS

        summary.append(("Loading", LOADING_METHOD_LABELS[shipment.loading_method]))
    total = shipment.total_containers
    if total:
        summary.append(("Total containers", format_quantity(total)))

    # Only the *included* case makes a statement about price. Internal-only
    # freight says nothing at all, because the customer is not being told
    # anything about it.
    freight_statement = ""
    if shipment.freight_method is FreightMethod.INCLUDED:
        freight_statement = "Freight is included in the quoted prices."
    elif shipment.freight_method is FreightMethod.ADDED_SEPARATELY:
        freight_statement = (
            "Freight is quoted separately and appears in the totals above."
        )

    return DocumentShipping(
        columns=wanted,
        headings=[SHIPPING_COLUMNS[key] for key in wanted],
        rows=rows,
        summary=summary,
        notes=shipment.shipping_notes or "",
        freight_statement=freight_statement,
    )


def _column_set(session: Session) -> list[str]:
    settings = settings_service.get_company_settings(session)
    configured = (settings.pdf_column_set or {}).get("columns") if settings else None
    if not configured:
        return list(DEFAULT_COLUMNS)
    valid = [c for c in configured if c in AVAILABLE_COLUMNS]
    return valid or list(DEFAULT_COLUMNS)


def _line_values(item, currency: str) -> dict[str, str]:  # noqa: ANN001
    """Format one quotation line for display.

    Note what is read: only the snapshot fields on the line itself, never the
    live product or price. A catalogue change after issue cannot alter a
    reprinted document.
    """
    description = item.description_override or item.size_label or ""
    spec = item.spec_text_override or compose_spec_text(
        num_colours=item.num_colours,
        board_quality=item.board_quality,
    )
    if item.customer_remarks:
        description = f"{description}\n{item.customer_remarks}"

    return {
        "item": str(item.line_no),
        "description": description,
        "size": item.size_label or "",
        "board_quality": item.board_quality or "",
        "pack_size": f"{item.case_pack} / case" if item.case_pack else "",
        "moq": format_quantity(item.moq_packs) if item.moq_packs else "",
        "quantity_packs": format_quantity(item.quantity_packs),
        "quantity_pieces": format_quantity(item.quantity_pieces),
        "containers": (
            format_quantity(item.container_count) if item.container_count else ""
        ),
        "price_per_pack": format_money(item.price_per_pack, currency, decimals=4),
        "price_per_piece": format_money(item.price_per_piece, currency, decimals=4),
        "line_total": format_money(item.net_line_total, currency),
        "spec": spec,
    }


def build_document(
    session: Session,
    quotation: Quotation,
    *,
    prepared_by: str = "",
    prepared_by_title: str = "",
    approved_by: str = "",
    force_draft: bool | None = None,
) -> QuotationDocument:
    """Turn a quotation into a renderable document.

    ``force_draft`` overrides the automatic decision, which is otherwise "draft
    unless the quotation has actually been issued". The DRAFT mark is not
    cosmetic — it is what stops an unapproved quotation being mistaken for a
    firm offer.
    """
    settings = settings_service.get_company_settings(session)
    currency = quotation.currency

    logo_bytes: bytes | None = None
    if settings and settings.logo_key:
        try:
            from modules.storage import get_storage

            logo_bytes = get_storage().get(settings.logo_key)
        except Exception:  # noqa: BLE001 - a missing logo must not stop a quotation
            logo_bytes = None

    address_lines = [
        line for line in (
            settings.address_line1 if settings else None,
            settings.address_line2 if settings else None,
            " ".join(
                p for p in (
                    settings.city if settings else None,
                    settings.province if settings else None,
                    settings.postal_code if settings else None,
                ) if p
            ) or None,
            settings.country if settings else None,
        ) if line
    ]

    company = DocumentCompany(
        name=(
            (settings.trading_name or settings.legal_name) if settings
            else "Soneet"
        ),
        address_lines=address_lines,
        phone=(settings.phone or "") if settings else "",
        email=(settings.email or "") if settings else "",
        website=(settings.website or "") if settings else "",
        tax_number=(settings.tax_number or "") if settings else "",
        logo_bytes=logo_bytes,
    )

    customer = DocumentCustomer(
        company=quotation.customer_name_snapshot or "",
        contact_name=quotation.contact_name or "",
        contact_email=quotation.contact_email or "",
        contact_phone=quotation.contact_phone or "",
        billing_address=quotation.billing_address_text or "",
        shipping_address=quotation.shipping_address_text or "",
        project=quotation.project_name or "",
        brand=quotation.brand or "",
        distributor=quotation.distributor or "",
        purchase_order=quotation.customer_po_ref or "",
    )

    columns = _column_set(session)
    lines = [
        DocumentLine(values=_line_values(item, currency))
        for item in sorted(quotation.items, key=lambda i: (i.sort_order, i.line_no))
    ]

    totals: list[DocumentTotal] = [
        DocumentTotal("Subtotal", format_money(quotation.subtotal, currency))
    ]
    if quotation.quote_discount_amount:
        totals.append(
            DocumentTotal(
                f"Discount ({format_quantity(quotation.quote_discount_pct)}%)",
                f"−{format_money(quotation.quote_discount_amount, currency)}",
            )
        )

    # Only charges the customer is meant to see. Internal-only charges still
    # count toward the grand total, so they are folded into the customer-visible
    # figures rather than listed — the total the customer sees is the total they
    # will be invoiced.
    visible_charges = [c for c in quotation.charges if c.is_customer_visible]
    for charge in visible_charges:
        label = charge.description or str(charge.charge_type).replace("_", " ").title()
        totals.append(DocumentTotal(label, format_money(charge.amount, currency)))

    hidden_total = sum(
        (c.amount for c in quotation.charges if not c.is_customer_visible), Decimal("0")
    )
    if hidden_total:
        totals.append(
            DocumentTotal("Additional charges", format_money(hidden_total, currency))
        )

    if quotation.tax_amount:
        totals.append(
            DocumentTotal(
                f"Tax ({format_quantity(quotation.tax_rate_pct)}%)",
                format_money(quotation.tax_amount, currency),
            )
        )
    totals.append(
        DocumentTotal(
            f"Total ({currency})",
            format_money(quotation.grand_total, currency),
            emphasis=True,
        )
    )

    terms = [
        DocumentTerm(title=t.title, body=t.body_text)
        for t in sorted(quotation.terms, key=lambda t: t.sort_order)
        if t.is_customer_visible
    ]

    is_draft = (
        force_draft
        if force_draft is not None
        else quotation.status not in ISSUED_STATUSES
        and quotation.status is not QuotationStatus.APPROVED
    )

    return QuotationDocument(
        quote_number=quotation.quote_number,
        revision_label=quotation.revision_label,
        quote_date=quotation.quote_date,
        valid_until=quotation.valid_until,
        status_label=STATUS_DISPLAY_NAMES.get(quotation.status, str(quotation.status)),
        currency=currency,
        company=company,
        customer=customer,
        columns=columns,
        column_headings=[AVAILABLE_COLUMNS[c] for c in columns],
        lines=lines,
        totals=totals,
        terms=terms,
        customer_notes=quotation.customer_notes or "",
        shipping=_build_shipping(session, quotation),
        prepared_by=prepared_by,
        prepared_by_title=prepared_by_title,
        approved_by=approved_by,
        signature_name=(settings.signature_name or "") if settings else "",
        signature_title=(settings.signature_title or "") if settings else "",
        footer_text=(settings.pdf_footer_text or "") if settings else "",
        confidentiality_text=(
            (settings.pdf_confidentiality_text or "") if settings else ""
        ),
        thank_you_text=(settings.pdf_thank_you_text or "") if settings else "",
        show_acceptance_line=bool(settings.pdf_show_acceptance_line) if settings else False,
        is_draft=is_draft,
    )
