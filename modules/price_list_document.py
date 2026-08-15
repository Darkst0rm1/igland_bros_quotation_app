"""The customer price list: every live variant at its standard price.

A price schedule, not a quotation. It carries no quantities, so it has no line
totals, no subtotal and no grand total — the figures on it are unit prices, and
a customer converts them into money by ordering. The quotation document is
where totals live, and it is built from :mod:`document_model`.

This exists because the schedule Noor Group issues was produced in a
spreadsheet, so the prices on it and the prices in this system could drift
without anything noticing. Rendering it from the catalogue makes that
impossible: every figure is read from ``product_prices`` for the exact variant.

**Selling price only.** Cost, FOB allocation, markup and margin have no field
in this model, exactly as in ``document_model`` — so there is no code path by
which one can reach the renderer. A test asserts it against the produced bytes.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules import settings_service
from modules.constants import PriceTierCode
from modules.models import (
    CompanySettings, PriceTier, Product, ProductPrice, ProductVariant,
)
from modules.pdf_primitives import (
    BAND, INK, MUTED, PAGE_SIZES, RULE, base_styles, escape, quantity_text,
    table_style,
)

#: Unit prices print to four places. Two would hide real money: a bundle price
#: rounded to cents moves a 2,304-bundle container by up to eleven dollars, and
#: the third and fourth places are where the freight share lands.
PRICE_EXP = Decimal("0.0001")

COLUMNS = ["product", "depth", "flute", "case", "quality", "pack", "piece"]
HEADINGS = {
    "product": "Product",
    "depth": "Depth",
    "flute": "Flute",
    "case": "Case",
    "quality": "Quality",
    "pack": "Standard Price / Pack",
    "piece": "Standard Price / Piece",
}


@dataclass(frozen=True)
class PriceListRow:
    product: str
    depth: str
    flute: str
    case_pack: int
    quality: str
    price_per_pack: Decimal
    price_per_piece: Decimal

    def cells(self) -> list[str]:
        return [
            self.product, self.depth, self.flute, str(self.case_pack),
            self.quality,
            f"${self.price_per_pack.quantize(PRICE_EXP)}",
            f"${self.price_per_piece.quantize(PRICE_EXP)}",
        ]


@dataclass(frozen=True)
class PriceListGroup:
    """One board quality. The source schedule tabulates by specification."""

    quality: str
    rows: list[PriceListRow] = field(default_factory=list)


@dataclass(frozen=True)
class PriceList:
    company_name: str
    address_lines: list[str]
    contact_line: str
    currency: str
    issued_on: dt.date
    reference: str
    revision_label: str
    groups: list[PriceListGroup]
    terms: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_revision(self) -> bool:
        return bool(self.revision_label)

    @property
    def row_count(self) -> int:
        return sum(len(g.rows) for g in self.groups)


def build(
    session: Session,
    *,
    reference: str,
    issued_on: dt.date | None = None,
    revision_label: str = "",
    tier_code: str = PriceTierCode.STANDARD.value,
    on_date: dt.date | None = None,
) -> PriceList:
    """Read every live variant's current price into a schedule.

    Grouped by board quality and ordered by size, which is how the supplier
    tabulates it and how a customer reads it. A variant with no price in force
    is omitted rather than shown blank: a schedule is an offer, and a row
    without a price is not one.
    """
    issued_on = issued_on or dt.date.today()
    on_date = on_date or issued_on

    company = session.execute(select(CompanySettings)).scalars().first()
    tier = session.scalars(
        select(PriceTier).where(PriceTier.code == tier_code)
    ).first()
    if tier is None:
        raise ValueError(f"No price tier {tier_code!r} is configured.")

    variants = session.scalars(
        select(ProductVariant).where(ProductVariant.deleted_at.is_(None))
    ).all()
    products = {
        p.id: p for p in session.scalars(
            select(Product).where(Product.deleted_at.is_(None))
        )
    }

    by_quality: dict[str, list[PriceListRow]] = {}
    for variant in variants:
        product = products.get(variant.product_id)
        if product is None:
            continue
        price = session.scalars(
            select(ProductPrice).where(
                ProductPrice.product_variant_id == variant.id,
                ProductPrice.price_tier_id == tier.id,
                ProductPrice.effective_from <= on_date,
            ).order_by(ProductPrice.effective_from.desc())
        ).first()
        if price is None or (
            price.effective_to is not None and price.effective_to < on_date
        ):
            continue

        per_piece = price.price_per_piece
        if per_piece is None and variant.case_pack:
            per_piece = price.price_per_pack / Decimal(variant.case_pack)

        by_quality.setdefault(variant.board_quality, []).append(
            PriceListRow(
                product=product.size_label,
                # quantity_text, not :g -- Decimal keeps its stored scale
                # under :g, so a depth of 2.000 prints as 2.000" rather than 2".
                depth=f'{quantity_text(product.depth_in)}"' if product.depth_in else "",
                flute=product.flute or "",
                case_pack=variant.case_pack,
                quality=variant.board_quality,
                price_per_pack=price.price_per_pack,
                price_per_piece=per_piece or Decimal("0"),
            )
        )

    # Taken from the prices themselves, not from the company default. The
    # company default is TRY while this catalogue is priced in USD, and a
    # schedule that labels dollar figures with the wrong currency is worse
    # than one that omits it.
    currencies = {
        p.currency for p in session.scalars(
            select(ProductPrice).where(ProductPrice.price_tier_id == tier.id)
        )
    }
    currency = currencies.pop() if len(currencies) == 1 else ""

    def _size(row: PriceListRow) -> Decimal:
        digits = "".join(c for c in row.product if c.isdigit() or c == ".")
        return Decimal(digits) if digits else Decimal("0")

    groups = [
        PriceListGroup(quality=quality, rows=sorted(by_quality[quality], key=_size))
        for quality in sorted(by_quality)
    ]

    address = []
    if company:
        address = [
            line for line in (
                company.address_line1, company.address_line2,
                ", ".join(p for p in (company.city, company.province) if p),
                ", ".join(p for p in (company.postal_code, company.country) if p),
            ) if line
        ]
    contact = "  ·  ".join(
        p for p in ((company.phone if company else ""),
                    (company.email if company else "")) if p
    )

    return PriceList(
        company_name=(company.trading_name or company.legal_name) if company else "",
        address_lines=address,
        contact_line=contact,
        currency=currency,
        issued_on=issued_on,
        reference=reference,
        revision_label=revision_label,
        groups=groups,
        terms=_terms(session),
    )


def _terms(session: Session) -> list[tuple[str, str]]:
    """The default terms, which the schedule carries as it always has."""
    from modules.models import TermTemplate

    rows = session.scalars(
        select(TermTemplate)
        .where(TermTemplate.is_default.is_(True), TermTemplate.is_active.is_(True))
        .order_by(TermTemplate.sort_order)
    ).all()
    return [(t.title, t.body_text) for t in rows]


def render(price_list: PriceList, page_size: str = "A4") -> bytes:
    """The schedule as PDF bytes."""
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
    )

    styles = base_styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=PAGE_SIZES.get(page_size.upper(), PAGE_SIZES["A4"]),
        leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Price list {price_list.reference}",
    )
    width = doc.width

    story: list = []

    # --- masthead ---------------------------------------------------------- #
    left = [Paragraph(escape(price_list.company_name), styles["company"])]
    for line in price_list.address_lines:
        left.append(Paragraph(escape(line), styles["company_detail"]))
    if price_list.contact_line:
        left.append(Paragraph(escape(price_list.contact_line), styles["company_detail"]))

    title = "PRICE LIST"
    right = [Paragraph(title, styles["doc_title"])]
    meta = [f"Reference {escape(price_list.reference)}"]
    if price_list.revision_label:
        meta.append(f"<b>{escape(price_list.revision_label)}</b>")
    meta.append(price_list.issued_on.strftime("%d %B %Y"))
    for line in meta:
        right.append(Paragraph(line, styles["meta"]))

    head = Table([[left, right]], colWidths=[width * 0.58, width * 0.42])
    head.setStyle(table_style(header_band=False))
    head.hAlign = "LEFT"
    story += [head, Spacer(1, 6)]

    if price_list.revision_label:
        story.append(Paragraph(
            f"<b>{escape(price_list.revision_label)}</b> — this schedule "
            f"supersedes any earlier prices issued under reference "
            f"{escape(price_list.reference)}.",
            styles["body"],
        ))
        story.append(Spacer(1, 8))

    # --- one table per board quality --------------------------------------- #
    # Sized for the Unicode family, which is wider than the base-14 fonts: at
    # the narrower widths that suited Helvetica, "Depth", "Flute" and "Case"
    # each wrapped mid-word in the header band.
    col_widths = [
        width * 0.14, width * 0.09, width * 0.08, width * 0.08,
        width * 0.24, width * 0.185, width * 0.185,
    ]
    for group in price_list.groups:
        block: list = [
            Paragraph(escape(group.quality), styles["label"]),
            Spacer(1, 2),
        ]
        data = [[Paragraph(f"<b>{escape(HEADINGS[c])}</b>", styles["cell"])
                 for c in COLUMNS]]
        for row in group.rows:
            data.append([Paragraph(escape(v), styles["cell"]) for v in row.cells()])

        table = Table(data, colWidths=col_widths, repeatRows=1)
        style = table_style()
        # Prices right-aligned: the last two columns are money and read as a
        # column of figures, not as text.
        style.add("ALIGN", (5, 0), (-1, -1), "RIGHT")
        style.add("ALIGN", (3, 0), (3, -1), "RIGHT")
        table.setStyle(style)
        block += [table, Spacer(1, 10)]
        # Kept together so a quality's heading never sits alone at a page foot.
        story.append(KeepTogether(block))

    # --- terms -------------------------------------------------------------- #
    if price_list.terms:
        story.append(Spacer(1, 4))
        story.append(Paragraph("Terms and conditions", styles["label"]))
        story.append(Spacer(1, 2))
        for title_text, body in price_list.terms:
            story.append(KeepTogether([
                Paragraph(f"<b>{escape(title_text)}</b>", styles["body"]),
                Paragraph(escape(body), styles["body"]),
                Spacer(1, 5),
            ]))

    doc.build(story)
    return buffer.getvalue()
