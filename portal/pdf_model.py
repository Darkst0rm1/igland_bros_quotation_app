"""The only shape of a quotation that reaches the customer PDF renderer.

The same allowlist discipline as :mod:`portal.projection`, for the same reason
and against a wider surface: a PDF is a file that leaves the building, gets
forwarded, and is opened by people who were never sent the link. A field that
should not be in it is worse here than on the page.

**Nothing is derived from a formatted string.** ``document_model`` — the
employee-facing structure — holds money as pre-formatted text and is built from
a quotation with no notion of customer selections. Reading it here would mean
parsing display strings back into numbers and would create a second pricing
path. Instead this model is assembled from three typed sources:

* :class:`~modules.pricing_snapshot.PricingSnapshot` for every figure,
* :class:`~portal.projection.QuoteView` for the customer-safe descriptive text,
* :class:`~modules.models.PortalResponse` for what an acceptance recorded.

Money stays :class:`~decimal.Decimal` throughout. The renderer formats, once.

Excluded by construction, because there is no field to carry them: unit and line
costs, gross profit, margin, supplier records, internal notes and remarks,
employee approvals, the audit trail, storage keys, access tokens and database
identifiers.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from modules.constants import INCLUSION_DISPLAY_NAMES, ItemInclusion
from modules.models import CompanySettings, PortalResponse, Quotation
from modules.pricing_snapshot import PricingSnapshot

#: Bumped whenever a change would make a regenerated PDF differ from one already
#: issued. Stored alongside every accepted artifact, so an artifact produced by
#: an older template is identifiable rather than silently assumed current.
GENERATOR_VERSION = "customer-pdf/1"

#: A document beyond this many lines is not a portal case, and rendering one
#: would be an unbounded amount of work triggered by a public request.
MAX_LINES = 400

#: Names that must never appear on a customer PDF model. Asserted against the
#: dataclasses and against the extracted text of the produced bytes.
FORBIDDEN_FIELDS = frozenset({
    "unit_cost_per_pack", "line_cost_total", "total_cost",
    "gross_profit", "gross_margin_pct", "margin",
    "internal_notes", "internal_remarks",
    "standard_cost", "cost_per_pack", "supplier",
    "product_price_id", "price_tier_id", "sales_user_id",
    "created_by_id", "updated_by_id", "requires_approval",
    "token_hash", "password_hash", "storage_key", "submission_nonce",
})


class PdfKind(StrEnum):
    """Which of the two documents this is.

    They are genuinely different documents, not one with a flag: a draft is a
    live offer that reprices with the reader's ticks, an accepted PDF is a
    record of an agreement that must never change.
    """

    DRAFT = "DRAFT"
    ACCEPTED = "ACCEPTED"


@dataclass(frozen=True)
class PdfCompany:
    name: str = ""
    address_lines: tuple[str, ...] = ()
    phone: str = ""
    email: str = ""
    website: str = ""
    #: Already-validated PNG bytes, read from storage by the caller. Never a
    #: path, a key or a URL — the renderer must have no way to fetch anything.
    logo_bytes: bytes | None = None

    @property
    def contact_line(self) -> str:
        return "  ·  ".join(p for p in (self.phone, self.email, self.website) if p)


@dataclass(frozen=True)
class PdfCustomer:
    company: str = ""
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    billing_address: str = ""
    shipping_address: str = ""
    purchase_order: str = ""


@dataclass(frozen=True)
class PdfLine:
    """One row. Money is Decimal; the renderer formats it."""

    line_no: int
    description: str
    specification: str
    size: str
    pack_size: str
    quantity_packs: Decimal
    quantity_pieces: Decimal
    unit_price: Decimal
    line_total: Decimal
    inclusion: str
    inclusion_label: str
    #: Whether this line is counted in the totals of *this* document.
    is_selected: bool
    remarks: str = ""

    @property
    def is_selectable(self) -> bool:
        return self.inclusion != ItemInclusion.INCLUDED.value


@dataclass(frozen=True)
class PdfTotal:
    label: str
    amount: Decimal
    emphasis: bool = False


@dataclass(frozen=True)
class PdfTerm:
    title: str
    body: str


@dataclass(frozen=True)
class PdfShipping:
    incoterm: str = ""
    incoterm_place: str = ""
    port_of_loading: str = ""
    port_of_discharge: str = ""
    shipping_line: str = ""
    container_summary: str = ""
    notes: str = ""


@dataclass(frozen=True)
class PdfAcceptance:
    """What the customer put their name to, exactly as it was recorded.

    The signature belongs here and nowhere else. It is printed in this document
    — which is delivered over the validated portal route — and never rendered on
    the public web page, where it would be visible to anyone holding the link.
    """

    customer_name: str
    job_title: str
    customer_email: str
    signature_name: str
    accepted_at: dt.datetime
    revision_label: str
    selected_count: int


@dataclass(frozen=True)
class CustomerPdfDocument:
    """Everything the customer renderer may know, and nothing else."""

    kind: PdfKind
    quote_number: str
    revision_label: str
    quote_date: dt.date
    valid_until: dt.date | None
    currency: str
    #: "Quotation" or "Accepted quotation" — the word on the page.
    title: str
    #: Says which figures these are: base, with selections, or as accepted.
    scope_note: str

    company: PdfCompany
    customer: PdfCustomer
    lines: tuple[PdfLine, ...]
    totals: tuple[PdfTotal, ...]
    terms: tuple[PdfTerm, ...]

    customer_notes: str = ""
    shipping: PdfShipping | None = None
    acceptance: PdfAcceptance | None = None

    #: Deliberately not one of ``totals``. The money block prints its quiet rows
    #: above the emphasised total, so a deposit listed among them reads as a
    #: component of it — "subtotal plus tax plus deposit equals total" — which
    #: is a misstatement of what the customer owes. It is a share *of* the
    #: total, so it is stated separately, underneath.
    deposit_due: Decimal | None = None
    deposit_pct: Decimal | None = None

    sales_representative: str = ""
    legal_footer: str = ""
    thank_you_text: str = ""
    generator_version: str = GENERATOR_VERSION

    @property
    def is_accepted(self) -> bool:
        return self.kind is PdfKind.ACCEPTED

    @property
    def file_stem(self) -> str:
        """Filename without extension. Sanitised again by the caller."""
        import re

        revision = self.revision_label.split()[-1] if self.revision_label else "0"
        suffix = "Accepted" if self.is_accepted else "Quotation"
        stem = f"{self.quote_number}_Rev{revision}_{suffix}"
        return re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:120]


class PdfModelError(ValueError):
    """The document cannot be built. The message is safe to show a customer."""


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #

def _company(settings: CompanySettings | None, logo_bytes: bytes | None) -> PdfCompany:
    if settings is None:
        return PdfCompany(logo_bytes=logo_bytes)
    parts = [
        settings.address_line1,
        settings.address_line2,
        " ".join(
            p for p in (settings.city, settings.province, settings.postal_code) if p
        ),
        settings.country,
    ]
    return PdfCompany(
        name=(settings.trading_name or settings.legal_name or "").strip(),
        address_lines=tuple(p.strip() for p in parts if p and p.strip()),
        phone=(settings.phone or "").strip(),
        email=(settings.email or "").strip(),
        website=(settings.website or "").strip(),
        logo_bytes=logo_bytes,
    )


def _lines(quotation: Quotation, snapshot: PricingSnapshot) -> tuple[PdfLine, ...]:
    """Descriptive text off the line, every figure off the snapshot.

    The two are matched by item id rather than by position: the snapshot sorts
    by ``(sort_order, line_no)`` and a caller iterating ``quotation.items`` in
    any other order would otherwise pair a description with someone else's
    price.
    """
    priced = {ln.item_id: ln for ln in snapshot.lines}
    ordered = sorted(quotation.items, key=lambda i: (i.sort_order, i.line_no))

    if len(ordered) > MAX_LINES:
        raise PdfModelError("This quotation is too large to produce as a PDF.")

    built: list[PdfLine] = []
    for item in ordered:
        figures = priced.get(item.id)
        if figures is None:      # cannot happen; refuse rather than invent one
            raise PdfModelError("This quotation could not be prepared.")
        built.append(
            PdfLine(
                line_no=item.line_no,
                description=(
                    item.description_override
                    or item.custom_description
                    or (
                        item.variant.product.name
                        if item.variant and item.variant.product else ""
                    )
                    or ""
                ),
                specification=(item.spec_text_override or item.board_quality or ""),
                size=item.size_label or "",
                pack_size=(f"{item.case_pack} / case" if item.case_pack else ""),
                quantity_packs=figures.quantity_packs,
                quantity_pieces=figures.quantity_pieces,
                unit_price=figures.unit_price,
                line_total=figures.line_total,
                inclusion=item.inclusion.value,
                inclusion_label=INCLUSION_DISPLAY_NAMES.get(item.inclusion, ""),
                is_selected=figures.is_selected,
                # customer_remarks is written for the customer. internal_remarks
                # is not carried at all — there is no field here to put it in.
                remarks=item.customer_remarks or "",
            )
        )
    return tuple(built)


def _totals(
    quotation: Quotation, snapshot: PricingSnapshot, *, accepted: bool
) -> tuple[PdfTotal, ...]:
    """The money block, entirely from the snapshot.

    Every row is a Decimal the snapshot produced. Nothing is re-derived from
    another row, so the printed subtotal and the printed total cannot disagree
    about what they are summarising.
    """
    from portal.projection import _tax_label

    rows: list[PdfTotal] = [PdfTotal("Subtotal", snapshot.subtotal)]

    if snapshot.discount:
        rows.append(PdfTotal("Discount", -snapshot.discount))

    rows.extend(_charge_rows(quotation, snapshot.charges_customer_visible))

    if snapshot.tax_amount:
        rows.append(PdfTotal(_tax_label(snapshot.tax_rate_pct), snapshot.tax_amount))

    label = "Accepted total" if accepted else f"Total ({snapshot.currency})"
    rows.append(PdfTotal(label, snapshot.grand_total, emphasis=True))
    return tuple(rows)


def _terms(quotation: Quotation) -> tuple[PdfTerm, ...]:
    return tuple(
        PdfTerm(title=t.title, body=t.body_text)
        for t in sorted(quotation.terms, key=lambda t: t.sort_order)
        if t.is_customer_visible
    )


def _shipping(quotation: Quotation) -> PdfShipping | None:
    from portal.projection import _shipping as project_shipping

    view = project_shipping(quotation)
    if view is None:
        return None
    return PdfShipping(
        incoterm=view.incoterm,
        incoterm_place=view.incoterm_place,
        port_of_loading=view.port_of_loading,
        port_of_discharge=view.port_of_discharge,
        shipping_line=view.shipping_line,
        container_summary=view.container_summary,
        notes=view.notes,
    )


def _customer(quotation: Quotation) -> PdfCustomer:
    return PdfCustomer(
        company=quotation.customer_name_snapshot or "",
        contact_name=quotation.contact_name or "",
        contact_email=quotation.contact_email or "",
        contact_phone=quotation.contact_phone or "",
        billing_address=quotation.billing_address_text or "",
        shipping_address=quotation.shipping_address_text or "",
        purchase_order=quotation.customer_po_ref or "",
    )


def build_draft(
    quotation: Quotation,
    snapshot: PricingSnapshot,
    *,
    company_settings: CompanySettings | None = None,
    logo_bytes: bytes | None = None,
    sales_representative: str = "",
    legal_footer: str = "",
    thank_you_text: str = "",
) -> CustomerPdfDocument:
    """The document a customer downloads while deciding.

    ``snapshot`` decides the figures and which lines count, so the caller is
    the one that chose the scope — this function never picks it, and never
    reprices.
    """
    selected_count = len(
        [ln for ln in snapshot.lines if ln.is_selected and ln.inclusion is not
         ItemInclusion.INCLUDED]
    )
    if selected_count:
        scope_note = (
            f"Includes {selected_count} optional item(s) you have selected. "
            "Nothing is accepted until you submit a response."
        )
    elif snapshot.optional_available:
        scope_note = (
            "Optional items are listed but not included in the total below."
        )
    else:
        scope_note = ""

    return CustomerPdfDocument(
        kind=PdfKind.DRAFT,
        quote_number=quotation.quote_number,
        revision_label=quotation.revision_label,
        quote_date=quotation.quote_date,
        valid_until=quotation.valid_until,
        currency=snapshot.currency,
        title="QUOTATION",
        scope_note=scope_note,
        company=_company(company_settings, logo_bytes),
        customer=_customer(quotation),
        lines=_lines(quotation, snapshot),
        totals=_totals(quotation, snapshot, accepted=False),
        terms=_terms(quotation),
        customer_notes=quotation.customer_notes or "",
        shipping=_shipping(quotation),
        acceptance=None,
        deposit_due=snapshot.deposit_due or None,
        deposit_pct=snapshot.deposit_pct or None,
        sales_representative=sales_representative,
        legal_footer=legal_footer,
        thank_you_text=thank_you_text,
    )


def build_accepted(
    quotation: Quotation,
    response: PortalResponse,
    snapshot: PricingSnapshot,
    *,
    company_settings: CompanySettings | None = None,
    logo_bytes: bytes | None = None,
    sales_representative: str = "",
    legal_footer: str = "",
    thank_you_text: str = "",
) -> CustomerPdfDocument:
    """The record of an agreement.

    The totals come from the **response**, not from repricing the quotation.
    That is the point of the accepted artifact: a later revision, a corrected
    price or a changed tax rate must not restate what somebody signed. The
    snapshot supplies the per-line figures and which lines were taken; the
    response supplies the three numbers that were agreed.
    """
    accepted_totals = _accepted_totals(quotation, response, snapshot)

    return CustomerPdfDocument(
        kind=PdfKind.ACCEPTED,
        quote_number=quotation.quote_number,
        revision_label=f"Rev {response.revision_no}",
        quote_date=quotation.quote_date,
        valid_until=quotation.valid_until,
        currency=response.currency,
        title="ACCEPTED QUOTATION",
        scope_note=(
            f"Accepted by {response.customer_name} on "
            f"{response.submitted_at:%d %b %Y}. This document records the "
            "agreed scope and price."
        ),
        company=_company(company_settings, logo_bytes),
        customer=_customer(quotation),
        lines=_lines(quotation, snapshot),
        totals=accepted_totals,
        terms=_terms(quotation),
        customer_notes=quotation.customer_notes or "",
        shipping=_shipping(quotation),
        acceptance=PdfAcceptance(
            customer_name=response.customer_name,
            job_title=response.job_title or "",
            customer_email=response.customer_email or "",
            signature_name=response.signature_name or response.customer_name,
            accepted_at=response.submitted_at,
            revision_label=f"Rev {response.revision_no}",
            selected_count=len(response.selected_item_ids or []),
        ),
        deposit_due=snapshot.deposit_due or None,
        deposit_pct=snapshot.deposit_pct or None,
        sales_representative=sales_representative,
        legal_footer=legal_footer,
        thank_you_text=thank_you_text,
    )


def _charge_rows(quotation: Quotation, payable: Decimal) -> list[PdfTotal]:
    """Customer-visible charges, with waived ones marked and not counted.

    Shared by the live and the accepted document so the two cannot describe
    the same charge differently — the accepted PDF is the one a customer keeps,
    and a waiver that appeared on the quotation and not on it would be a
    concession the record does not show.

    ``payable`` is what the visible charges actually come to. It is printed
    only when a waiver makes the rows above deliberately fail to add up.
    """
    from modules.document_model import WAIVED_MARK

    rows: list[PdfTotal] = []
    waived_any = False
    for charge in sorted(quotation.charges, key=lambda c: c.sort_order):
        if not charge.is_customer_visible:
            continue
        label = (
            charge.description
            or charge.charge_type.value.replace("_", " ").title()
        )
        if charge.is_waived:
            waived_any = True
            label = f"{label} — {WAIVED_MARK}"
        rows.append(PdfTotal(label, charge.amount))

    if waived_any:
        rows.append(PdfTotal("Total charges", payable))
    return rows


def _accepted_totals(
    quotation: Quotation, response: PortalResponse, snapshot: PricingSnapshot
) -> tuple[PdfTotal, ...]:
    """Subtotal, tax and grand total as recorded; the rest from the snapshot.

    Only three figures were stored at acceptance, so the intermediate rows —
    discount, visible charges, deposit — still come from the snapshot. They are
    presentation detail that sums to the stored figures; the numbers that
    matter legally are the stored ones, and they are used verbatim.
    """
    from portal.projection import _tax_label

    rows: list[PdfTotal] = [PdfTotal("Subtotal", response.subtotal)]

    if snapshot.discount:
        rows.append(PdfTotal("Discount", -snapshot.discount))
    rows.extend(_charge_rows(quotation, snapshot.charges_customer_visible))
    if response.tax_amount:
        rows.append(PdfTotal(_tax_label(snapshot.tax_rate_pct), response.tax_amount))

    rows.append(PdfTotal("Accepted total", response.grand_total, emphasis=True))
    return tuple(rows)
