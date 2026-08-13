"""The only shape of a quotation a customer is ever shown.

**Allowlist, not denylist.** Every field a customer sees is named here
explicitly. ORM objects are never serialised, never passed to a template, and
never returned from a route — so a column added to ``Quotation`` or
``QuotationItem`` later cannot leak by default. Making something visible has to
be a deliberate edit to this file.

Excluded by construction, because there is no field to put them in: unit and
line costs, gross profit, margin, supplier records, internal notes and remarks,
employee approvals, the audit trail, and database identifiers.

Money stays :class:`~decimal.Decimal` all the way through. Formatting happens
in the template, at the presentation boundary, so no rounding is baked into the
projection.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass, field
from decimal import Decimal

from modules.calculation_engine import QuotationTotals
from modules.constants import (
    CONTAINER_SIZE_LABELS,
    INCLUSION_DISPLAY_NAMES,
    ItemInclusion,
)
from modules.models import (
    CompanySettings,
    PortalResponse,
    Quotation,
    QuotationItem,
    QuoteAccessToken,
)

#: Names that must never appear in a rendered page or a projection. Asserted by
#: the test suite against both the dataclasses and the final HTML.
FORBIDDEN_FIELDS = frozenset({
    "unit_cost_per_pack", "line_cost_total", "total_cost",
    "gross_profit", "gross_margin_pct", "margin",
    "internal_notes", "internal_remarks",
    "standard_cost", "cost_per_pack", "supplier",
    "product_price_id", "price_tier_id", "sales_user_id",
    "created_by_id", "updated_by_id", "requires_approval",
    "token_hash", "password_hash",
})


def line_ref(token_hash: str, item_id: int) -> str:
    """An opaque handle for one line, bound to one access token.

    Database ids are never published. This is derived from the token's own
    hash, so a reference is meaningless against any other quotation — a
    customer cannot select a line that is not on the quote in front of them,
    even by guessing.
    """
    from modules.config import get_settings

    secret = get_settings().secret_key.encode("utf-8")
    digest = hmac.new(
        secret, f"{token_hash}:{item_id}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:16]


def resolve_refs(
    token: QuoteAccessToken, quotation: Quotation, refs: list[str]
) -> list[int]:
    """Turn submitted opaque refs back into line ids for this quotation only.

    Anything that does not correspond to a selectable line here is dropped, so
    a tampered form degrades to "nothing selected" rather than erroring.
    """
    from modules.portal_service import selectable_items

    wanted = {r.strip() for r in refs if isinstance(r, str) and r.strip()}
    return [
        item.id
        for item in selectable_items(quotation)
        if line_ref(token.token_hash, item.id) in wanted
    ]


# --------------------------------------------------------------------------- #
# View objects
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CompanyView:
    name: str = ""
    address_lines: tuple[str, ...] = ()
    phone: str = ""
    email: str = ""
    website: str = ""


@dataclass(frozen=True)
class CustomerView:
    company: str = ""
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    billing_address: str = ""
    shipping_address: str = ""
    purchase_order: str = ""


@dataclass(frozen=True)
class LineView:
    ref: str
    description: str
    specification: str
    size: str
    pack_size: str
    quantity_packs: Decimal
    quantity_pieces: Decimal
    unit_price: Decimal
    line_total: Decimal
    inclusion: str            # INCLUDED / OPTIONAL / RECOMMENDED
    inclusion_label: str
    remarks: str = ""         # customer_remarks only — never internal_remarks
    #: Whether a photograph exists. The storage key itself is never projected —
    #: the page asks for the image by opaque line reference instead.
    has_image: bool = False

    @property
    def is_selectable(self) -> bool:
        return self.inclusion != ItemInclusion.INCLUDED.value


@dataclass(frozen=True)
class ChargeView:
    label: str
    amount: Decimal


@dataclass(frozen=True)
class ShippingView:
    incoterm: str = ""
    incoterm_place: str = ""
    port_of_loading: str = ""
    port_of_discharge: str = ""
    shipping_line: str = ""
    container_summary: str = ""
    notes: str = ""


@dataclass(frozen=True)
class TermView:
    title: str
    body: str


@dataclass(frozen=True)
class TotalsView:
    currency: str
    subtotal: Decimal
    discount: Decimal
    charges: Decimal
    tax_label: str
    tax_amount: Decimal
    grand_total: Decimal
    deposit_due: Decimal
    optional_available: Decimal   # what the unselected extras would add


@dataclass(frozen=True)
class AcceptedView:
    """Shown once the customer has accepted. Read-only, and exact."""

    customer_name: str
    job_title: str
    signature_name: str
    accepted_at: dt.datetime
    revision_label: str
    currency: str
    grand_total: Decimal
    selected_refs: tuple[str, ...]


@dataclass(frozen=True)
class QuoteView:
    quote_number: str
    revision_label: str
    status_label: str
    quote_date: dt.date
    valid_until: dt.date | None
    currency: str
    sales_representative: str
    company: CompanyView
    customer: CustomerView
    lines: tuple[LineView, ...]
    charges: tuple[ChargeView, ...]
    totals: TotalsView
    terms: tuple[TermView, ...]
    shipping: ShippingView | None
    customer_notes: str
    is_expired: bool
    is_read_only: bool
    accepted: AcceptedView | None = None
    selected_refs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_selectable_lines(self) -> bool:
        return any(line.is_selectable for line in self.lines)


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #

def _company(settings: CompanySettings | None) -> CompanyView:
    if settings is None:
        return CompanyView()
    parts = [
        settings.address_line1, settings.address_line2,
        " ".join(p for p in (settings.city, settings.province, settings.postal_code) if p),
        settings.country,
    ]
    return CompanyView(
        name=(settings.trading_name or settings.legal_name or "").strip(),
        address_lines=tuple(p.strip() for p in parts if p and p.strip()),
        phone=(settings.phone or "").strip(),
        email=(settings.email or "").strip(),
        website=(settings.website or "").strip(),
    )


def _line(item: QuotationItem, token_hash: str, unit_price: Decimal) -> LineView:
    variant = item.variant
    has_image = bool(variant and variant.product and variant.product.image_key)
    return LineView(
        ref=line_ref(token_hash, item.id),
        description=(
            item.description_override or item.custom_description
            or (item.variant.product.name if item.variant and item.variant.product else "")
            or ""
        ),
        specification=(item.spec_text_override or item.board_quality or ""),
        size=item.size_label or "",
        pack_size=(f"{item.case_pack} / case" if item.case_pack else ""),
        quantity_packs=item.quantity_packs,
        quantity_pieces=item.quantity_pieces,
        unit_price=unit_price,
        line_total=item.net_line_total,
        inclusion=item.inclusion.value,
        inclusion_label=INCLUSION_DISPLAY_NAMES.get(item.inclusion, ""),
        # customer_remarks is written for the customer. internal_remarks is not
        # projected at all — there is no field here to carry it.
        remarks=item.customer_remarks or "",
        has_image=has_image,
    )


def _tax_label(rate: Decimal | None) -> str:
    """"Tax (13%)", not "Tax (13.000000%)".

    ``:g`` does not strip trailing zeros from a Decimal the way it does from a
    float — the stored scale is preserved — so normalise first.
    """
    if not rate:
        return "Tax"
    # normalize() alone turns 25.0000 into 2.5E+1, so quantize back to an
    # integer when the value has no fractional part.
    trimmed = rate.normalize()
    if trimmed == trimmed.to_integral_value():
        trimmed = trimmed.quantize(Decimal(1))
    return f"Tax ({trimmed}%)"


def _shipping(quotation: Quotation) -> ShippingView | None:
    shipment = quotation.shipment
    if shipment is None or not shipment.show_on_document:
        return None
    containers = sorted(shipment.containers, key=lambda c: c.sort_order)
    summary = ", ".join(
        f"{c.container_count:g} x {CONTAINER_SIZE_LABELS.get(c.container_size, c.container_size)}"
        for c in containers
    )
    lines = {
        (c.shipping_line.name if c.shipping_line else c.custom_shipping_line or "")
        for c in containers
    }
    return ShippingView(
        incoterm=(shipment.incoterm.value if shipment.incoterm else ""),
        incoterm_place=shipment.incoterm_place or "",
        port_of_loading=shipment.port_of_loading or "",
        port_of_discharge=shipment.port_of_discharge or "",
        shipping_line=", ".join(sorted(n for n in lines if n)),
        container_summary=summary,
        notes=shipment.shipping_notes or "",
    )


def build_quote_view(
    quotation: Quotation,
    token: QuoteAccessToken,
    totals: QuotationTotals,
    *,
    company_settings: CompanySettings | None = None,
    selected_ids: list[int] | None = None,
    accepted_response: PortalResponse | None = None,
    sales_representative: str = "",
    today: dt.date | None = None,
) -> QuoteView:
    """Project one quotation into the customer-safe view.

    ``totals`` is computed by the caller through the calculation engine, so
    this function never does arithmetic on money — it only carries it.
    """
    from modules.constants import STATUS_DISPLAY_NAMES
    from modules.portal_service import deposit_due, selectable_items

    today = today or dt.date.today()
    chosen = set(selected_ids or [])
    token_hash = token.token_hash

    lines = tuple(
        _line(
            item, token_hash,
            unit_price=(
                item.price_per_piece
                if item.pricing_basis.value == "PIECE" else item.price_per_pack
            ),
        )
        for item in sorted(quotation.items, key=lambda i: i.sort_order)
    )

    # What the customer would add by ticking everything still unticked.
    optional_available = sum(
        (i.net_line_total for i in selectable_items(quotation) if i.id not in chosen),
        Decimal("0.00"),
    )

    charges = tuple(
        ChargeView(label=(c.description or c.charge_type.value.replace("_", " ").title()),
                   amount=c.amount)
        for c in sorted(quotation.charges, key=lambda c: c.sort_order)
        if c.is_customer_visible
    )

    terms = tuple(
        TermView(title=t.title, body=t.body_text)
        for t in sorted(quotation.terms, key=lambda t: t.sort_order)
        if t.is_customer_visible
    )

    accepted = None
    if accepted_response is not None:
        accepted = AcceptedView(
            customer_name=accepted_response.customer_name,
            job_title=accepted_response.job_title or "",
            signature_name=accepted_response.signature_name or "",
            accepted_at=accepted_response.submitted_at,
            revision_label=f"Rev {accepted_response.revision_no}",
            currency=accepted_response.currency,
            grand_total=accepted_response.grand_total,
            selected_refs=tuple(
                line_ref(token_hash, int(i))
                for i in (accepted_response.selected_item_ids or [])
            ),
        )

    expired = quotation.valid_until is not None and quotation.valid_until < today

    return QuoteView(
        quote_number=quotation.quote_number,
        revision_label=quotation.revision_label,
        status_label=STATUS_DISPLAY_NAMES.get(quotation.status, ""),
        quote_date=quotation.quote_date,
        valid_until=quotation.valid_until,
        currency=quotation.currency,
        sales_representative=sales_representative,
        company=_company(company_settings),
        customer=CustomerView(
            company=quotation.customer_name_snapshot or "",
            contact_name=quotation.contact_name or "",
            contact_email=quotation.contact_email or "",
            contact_phone=quotation.contact_phone or "",
            billing_address=quotation.billing_address_text or "",
            shipping_address=quotation.shipping_address_text or "",
            purchase_order=quotation.customer_po_ref or "",
        ),
        lines=lines,
        charges=charges,
        totals=TotalsView(
            currency=quotation.currency,
            subtotal=totals.subtotal,
            discount=totals.quotation_discount,
            charges=totals.charges_customer_visible,
            tax_label=_tax_label(quotation.tax_rate_pct),
            tax_amount=totals.tax_amount,
            grand_total=totals.grand_total,
            deposit_due=deposit_due(quotation, totals),
            optional_available=optional_available,
        ),
        terms=terms,
        shipping=_shipping(quotation),
        customer_notes=quotation.customer_notes or "",
        is_expired=expired,
        is_read_only=accepted is not None or expired,
        accepted=accepted,
        selected_refs=tuple(line_ref(token_hash, i) for i in sorted(chosen)),
    )
