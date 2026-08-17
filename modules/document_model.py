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

from sqlalchemy import or_ as sa_or, select
from sqlalchemy.orm import Session

from modules import settings_service
from modules.calculation_engine import deposit_amount
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

#: ``containers`` sits directly after the quantity it is derived from: the
#: customer reads "how many packs" and immediately "how many containers that
#: fills", which is the question an export buyer actually asks. It renders as
#: empty for any line without a container count, so a quotation that does not
#: ship in containers simply shows a blank cell rather than a wrong number.
#: Board quality before size, and no separate description column.
#:
#: "Description" printed ``description_override or size_label``, and almost no
#: line carries an override — so the column repeated the size column beside it,
#: on every quotation ever produced. Meanwhile the board quality was composed
#: into a ``spec`` value that no default layout printed, which is the more
#: expensive half: WTL125 FL120 IK120 and IK135 are different products at
#: different prices on the same size, and a quotation that does not say which
#: cannot be reconciled against the order it becomes.
DEFAULT_COLUMNS = [
    "item", "board_quality", "size", "pack_size",
    "quantity_packs", "containers", "price_per_pack", "price_per_piece",
    "line_total",
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

    A **summary**, not a table. The per-container breakdown — shipping line,
    size, type, quantity, ports, transit, freight — was removed on 2026-08-16:
    it ran to a second page on a two-container shipment, and every figure a
    customer needs is already on the quotation. What they are charged is the
    freight line in the totals; what they are shipping is the line items.

    Freight cost therefore appears **only** in the totals, as a charge. There
    is no field for internal freight here, so no renderer can print it by
    accident.
    """

    summary: list[tuple[str, str]] = field(default_factory=list)
    notes: str = ""
    freight_statement: str = ""

    def __bool__(self) -> bool:
        """Empty when there is nothing left to say.

        The renderers ask ``if shipping`` rather than ``is not None`` so a
        shipment with no incoterm, no note and no freight statement prints no
        stray blank line.
        """
        return bool(self.summary or self.notes or self.freight_statement)


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

#: How a waived charge is marked wherever it is shown. One constant, because
#: the quotation, the portal and both renderers have to say the same word.
WAIVED_MARK = "WAIVED"


def _build_shipping(session: Session, quotation: Quotation) -> DocumentShipping | None:
    """The shipping summary, or ``None`` when it should not appear.

    On by default per quotation. Anything not filled in is omitted rather than
    printed empty, so a partly-completed shipment still reads cleanly.

    There is no per-container table any more. It listed the shipping line,
    size, type, quantity, ports, transit and freight for every container, ran
    onto a second page on a two-container shipment, and repeated what the
    quotation already says: the freight charge is in the totals and the goods
    are the line items. Removed 2026-08-16.

    Containers are still **queried** rather than read off
    ``shipment.containers`` for the total. The session factory sets
    ``expire_on_commit=False``, so a collection loaded before a container was
    written stays stale for the rest of the session — and the two callers that
    render most often are the page that has just added one and
    ``create_revision``, which has just copied them all. That produced a
    freight line reading "2 containers" above a stated total of one.
    """
    from modules.constants import FreightMethod
    from modules.models import ShipmentContainer

    shipment = quotation.shipment
    if shipment is None or not shipment.show_on_document:
        return None

    containers = list(
        session.execute(
            select(ShipmentContainer)
            .where(ShipmentContainer.quotation_shipment_id == shipment.id)
            .order_by(ShipmentContainer.sort_order)
        ).scalars()
    )
    if not containers:
        return None

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
    # Summed from the queried rows rather than the relationship property, so
    # the stated total cannot disagree with the freight charge in the totals,
    # which counts the same rows.
    total = sum((c.container_count for c in containers), Decimal("0"))
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

    shipping = DocumentShipping(
        summary=summary,
        notes=shipment.shipping_notes or "",
        freight_statement=freight_statement,
    )
    return shipping or None


def _column_set(session: Session) -> list[str]:
    settings = settings_service.get_company_settings(session)
    configured = (settings.pdf_column_set or {}).get("columns") if settings else None
    if not configured:
        return list(DEFAULT_COLUMNS)
    valid = [c for c in configured if c in AVAILABLE_COLUMNS]
    return valid or list(DEFAULT_COLUMNS)


def _derived_containers(session: Session, items) -> dict[int, str]:  # noqa: ANN001
    """How many containers each line's quantity fills, where that is knowable.

    Presentational only, and used solely as a fallback when nobody typed a
    container count. It is deliberately **not** written back to
    ``item.container_count``: ``pricing_service._quotation_container_total``
    ranks a typed count above a catalogue estimate when deciding which
    container tier a customer is quoted at, and promoting a workbook figure to
    "somebody's own statement" could move a quotation between the three- and
    eight-container rate. The workbook is also known to contain at least one
    capacity that cannot be right.

    Unlike everything else on the line this reads live catalogue capacity, so a
    later workbook change can alter a *reprinted draft*. It cannot alter a
    record: an accepted quotation is rendered once and stored as an immutable
    artifact, and a draft is not a record of anything.

    A line whose product has no capacity row simply gets no figure, rather than
    a guess.
    """
    from modules.models import ProductContainerCapacity
    from modules.pricing_service import containers_for_quantity
    from modules.repositories import get_variant

    derived: dict[int, str] = {}
    for item in items:
        if not item.product_variant_id or item.container_count:
            continue
        variant = get_variant(session, item.product_variant_id)
        if variant is None:
            continue
        capacity = session.execute(
            select(ProductContainerCapacity)
            .where(
                ProductContainerCapacity.product_id == variant.product_id,
                sa_or(
                    ProductContainerCapacity.product_variant_id == variant.id,
                    ProductContainerCapacity.product_variant_id.is_(None),
                ),
            )
            # This variant's own figure first. Board qualities of one size do
            # not share a container quantity, whatever geometry suggests.
            .order_by(ProductContainerCapacity.product_variant_id.is_(None))
        ).scalars().first()
        share = containers_for_quantity(item.quantity_packs, capacity)
        if share is not None:
            derived[item.id] = format_quantity(share)
    return derived


def _line_values(  # noqa: ANN001
    item, currency: str, derived_containers: str = "",
) -> dict[str, str]:
    """Format one quotation line for display.

    Note what is read: only the snapshot fields on the line itself, never the
    live product or price. A catalogue change after issue cannot alter a
    reprinted document. ``derived_containers`` is the one exception and is
    computed by the caller — see :func:`_derived_containers`.
    """
    description = item.description_override or item.size_label or ""
    spec = item.spec_text_override or compose_spec_text(
        num_colours=item.num_colours,
        board_quality=item.board_quality,
    )
    if item.customer_remarks:
        description = f"{description}\n{item.customer_remarks}"

    # The size column carries the line's identity, and with it the two things
    # that used to ride in the description column: a typed override, which is
    # by definition what the customer should see instead of the catalogue
    # name, and the customer remarks. Dropping that column without moving
    # these would have left the edit dialog offering two fields that reached
    # no document.
    size = item.description_override or item.size_label or ""
    if item.customer_remarks:
        size = f"{size}\n{item.customer_remarks}"

    return {
        "item": str(item.line_no),
        "description": description,
        "size": size,
        "board_quality": item.board_quality or "",
        "pack_size": f"{item.case_pack} / case" if item.case_pack else "",
        "moq": format_quantity(item.moq_packs) if item.moq_packs else "",
        "quantity_packs": format_quantity(item.quantity_packs),
        "quantity_pieces": format_quantity(item.quantity_pieces),
        # A count somebody typed is their statement of the shipment and wins.
        # Otherwise show what the requested quantity fills, when the catalogue
        # knows the capacity; blank when it does not, rather than a wrong number.
        "containers": (
            format_quantity(item.container_count)
            if item.container_count
            else derived_containers
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
    ordered = sorted(quotation.items, key=lambda i: (i.sort_order, i.line_no))
    derived = (
        _derived_containers(session, ordered) if "containers" in columns else {}
    )
    lines = [
        DocumentLine(
            values=_line_values(item, currency, derived.get(item.id, ""))
        )
        for item in ordered
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
    #
    # A waived charge is listed at its full amount and marked, because the
    # concession is the point: deleting the row would hide that the charge
    # applied, and discounting it would misstate what it costs.
    visible_charges = sorted(
        (c for c in quotation.charges if c.is_customer_visible),
        key=lambda c: c.sort_order,
    )
    for charge in visible_charges:
        label = charge.description or str(charge.charge_type).replace("_", " ").title()
        amount = format_money(charge.amount, currency)
        totals.append(
            DocumentTotal(
                f"{label} — {WAIVED_MARK}" if charge.is_waived else label,
                amount,
            )
        )

    hidden_total = sum(
        (
            c.amount for c in quotation.charges
            if not c.is_customer_visible and not c.is_waived
        ),
        Decimal("0"),
    )
    if hidden_total:
        totals.append(
            DocumentTotal("Additional charges", format_money(hidden_total, currency))
        )

    # Stated only when something was waived. Without a waiver it repeats what
    # the rows above already add up to; with one, the rows deliberately do not
    # add up to it, and saying so is the difference between a clear concession
    # and an arithmetic error the customer has to query.
    if any(c.is_waived for c in quotation.charges):
        totals.append(
            DocumentTotal(
                "Total charges", format_money(quotation.charges_total, currency)
            )
        )

    if quotation.tax_amount:
        # What the tax was applied on top of. Stated only when there is both a
        # charge and a tax, because with neither it repeats the subtotal line
        # immediately above it. Derived by subtraction from the grand total
        # rather than re-summed, so it cannot disagree with the total printed
        # two rows down.
        if visible_charges or hidden_total:
            totals.append(
                DocumentTotal(
                    "Subtotal before tax",
                    format_money(quotation.grand_total - quotation.tax_amount, currency),
                )
            )
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

    # Deposit and balance sit *after* the emphasised total and are derived from
    # it, so they cannot be read as components of it. The portal PDF already
    # stated the deposit and this document did not, which meant the internal
    # copy and the customer's copy of the same quotation disagreed about what
    # was payable on order.
    deposit = deposit_amount(quotation.grand_total, quotation.deposit_pct)
    if deposit:
        totals.append(
            DocumentTotal(
                f"Deposit required ({format_quantity(quotation.deposit_pct)}%)",
                format_money(deposit, currency),
            )
        )
        totals.append(
            DocumentTotal(
                "Balance due",
                format_money(quotation.grand_total - deposit, currency),
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
