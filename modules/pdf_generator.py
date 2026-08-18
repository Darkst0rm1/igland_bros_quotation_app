"""PDF renderer, ReportLab.

Consumes :class:`~modules.document_model.QuotationDocument` and nothing else —
no ORM, no session, no money arithmetic. That is what keeps this file and
:mod:`modules.docx_generator` producing the same content.

ReportLab rather than WeasyPrint: WeasyPrint needs GTK/Pango native libraries
that are not installable on the target Windows machine, and a renderer that
only works in production is one nobody can test.
"""

from __future__ import annotations

import logging
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    LongTable,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from modules.document_model import QuotationDocument
from modules.pdf_primitives import (
    BAND,
    INK,
    MUTED,
    PAGE_SIZES,
    RULE,
    base_styles,
    escape,
    money_block,
)
from modules.utilities import format_date

log = logging.getLogger(__name__)

#: Relative column widths for the line table. Anything not listed gets 1.0.
COLUMN_WEIGHTS = {
    "description": 2.9,
    "spec": 1.6,
    "item": 0.8,
    "size": 1.15,
    "depth": 0.7,
    "board_quality": 1.9,
    "pack_size": 0.7,
    "quantity_packs": 0.95,
    "quantity_pieces": 0.95,
    "price_per_pack": 1.05,
    "price_per_piece": 1.05,
    "line_total": 1.35,
}


#: The shared styles, unchanged. Named privately so the rest of this
#: module reads as it always did.
_styles = base_styles
_escape = escape

#: Scales tried, in order, to get a quotation onto one page.
#:
#: Only ever applied when it actually achieves that. A quotation with forty
#: lines was never going to fit, and shrinking its type would make it harder to
#: read for no gain — so a failed attempt is discarded and the full-size render
#: is what gets returned.
_FIT_SCALES = (0.94, 0.88, 0.82, 0.76, 0.70)

#: Style attributes that are lengths and may be scaled. Colours, alignments and
#: font names are left alone.
_SCALABLE = ("fontSize", "leading", "spaceBefore", "spaceAfter")


def _scaled_styles(scale: float = 1.0):  # noqa: ANN201
    """The shared styles, optionally tightened.

    ``base_styles`` builds fresh ParagraphStyle objects on every call, so
    mutating them here cannot leak into the customer PDF renderer or a later
    render at a different scale.
    """
    styles = _styles()
    if scale == 1.0:
        return styles
    for style in styles.values():
        for attribute in _SCALABLE:
            value = getattr(style, attribute, None)
            if value:
                setattr(style, attribute, value * scale)
    return styles


class _QuotationTemplate(BaseDocTemplate):
    """Adds the repeating footer, page numbers and the DRAFT watermark."""

    def __init__(
        self,
        buffer: BytesIO,
        document: QuotationDocument,
        page_size,  # noqa: ANN001
        scale: float = 1.0,
    ) -> None:
        self.document_model = document
        self.styles = _scaled_styles(scale)
        # The footer needs room whatever the scale, so the bottom margin gives
        # back less than the rest.
        super().__init__(
            buffer,
            pagesize=page_size,
            leftMargin=16 * mm * scale,
            rightMargin=16 * mm * scale,
            topMargin=14 * mm * scale,
            bottomMargin=18 * mm * max(scale, 0.9),
            title=f"{document.quote_number} {document.revision_label}",
            author=document.company.name,
            subject="Quotation",
        )
        frame = Frame(
            self.leftMargin, self.bottomMargin,
            self.width, self.height, id="body",
        )
        self.addPageTemplates(
            [PageTemplate(id="quotation", frames=[frame], onPage=self._decorate)]
        )

    def _decorate(self, canvas, doc) -> None:  # noqa: ANN001
        canvas.saveState()
        model = self.document_model

        if model.is_draft:
            self._watermark(canvas, doc)

        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.4)
        footer_y = self.bottomMargin - 6 * mm
        canvas.line(
            self.leftMargin, footer_y + 8 * mm,
            self.leftMargin + self.width, footer_y + 8 * mm,
        )

        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        left_bits = [model.company.name, model.company.contact_line]
        canvas.drawString(
            self.leftMargin, footer_y + 4 * mm,
            "  ·  ".join(b for b in left_bits if b)[:150],
        )
        canvas.drawRightString(
            self.leftMargin + self.width, footer_y + 4 * mm,
            f"{model.quote_number} {model.revision_label}  ·  Page {doc.page}",
        )
        if model.confidentiality_text:
            canvas.drawString(
                self.leftMargin, footer_y + 0.5 * mm,
                model.confidentiality_text[:180],
            )
        canvas.restoreState()

    def _watermark(self, canvas, doc) -> None:  # noqa: ANN001
        """Diagonal DRAFT across the page.

        Drawn under the content by being painted first on each page, at low
        opacity, so it marks the document without making it unreadable.
        """
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 96)
        canvas.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.13))
        canvas.translate(self.pagesize[0] / 2, self.pagesize[1] / 2)
        canvas.rotate(38)
        canvas.drawCentredString(0, 0, "DRAFT")
        canvas.restoreState()


def _header_flowables(model: QuotationDocument, styles, width: float, scale: float = 1.0) -> list:  # noqa: ANN001
    left: list = []
    if model.company.logo_bytes:
        try:
            logo = Image(BytesIO(model.company.logo_bytes))
            ratio = logo.imageHeight / float(logo.imageWidth or 1)
            logo.drawWidth = 45 * mm * scale
            logo.drawHeight = 45 * mm * scale * ratio
            left.append(logo)
            left.append(Spacer(1, 3))
        except Exception:  # noqa: BLE001 - a bad logo must not stop a quotation
            log.warning("Logo could not be rendered; continuing without it")

    left.append(Paragraph(_escape(model.company.name), styles["company"]))
    for line in model.company.address_lines:
        left.append(Paragraph(_escape(line), styles["company_detail"]))
    if model.company.contact_line:
        left.append(Paragraph(_escape(model.company.contact_line), styles["company_detail"]))
    if model.company.tax_number:
        left.append(
            Paragraph(_escape(f"Tax no. {model.company.tax_number}"), styles["company_detail"])
        )

    meta_rows = [
        ("Quotation no.", f"{model.quote_number}  {model.revision_label}"),
        ("Quote date", format_date(model.quote_date)),
    ]
    if model.valid_until:
        meta_rows.append(("Valid until", format_date(model.valid_until)))
    meta_text = "<br/>".join(
        f"<font color='#5f6b7a'>{_escape(label)}:</font> <b>{_escape(value)}</b>"
        for label, value in meta_rows
    )

    right = [
        Paragraph(model.title, styles["doc_title"]),
        Paragraph(meta_text, styles["meta"]),
    ]

    table = Table([[left, right]], colWidths=[width * 0.55, width * 0.45])
    table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])
    )
    return [table, Spacer(1, 10 * scale)]


def _customer_flowables(model: QuotationDocument, styles, width: float, scale: float = 1.0) -> list:  # noqa: ANN001
    def block(label: str, value: str):  # noqa: ANN202
        return [
            Paragraph(label.upper(), styles["label"]),
            Paragraph(_escape(value or "—"), styles["body"]),
        ]

    contact_bits = [
        b for b in (
            model.customer.contact_name,
            model.customer.contact_email,
            model.customer.contact_phone,
        ) if b
    ]

    first_row = [
        block("Prepared for", model.customer.company),
        block("Contact", "\n".join(contact_bits)),
        block("Project", model.customer.project),
    ]
    second: list = []
    if model.customer.brand:
        second.append(block("Brand", model.customer.brand))
    if model.customer.distributor:
        second.append(block("Distributor", model.customer.distributor))
    if model.customer.purchase_order:
        second.append(block("PO reference", model.customer.purchase_order))
    if model.customer.billing_address:
        second.append(block("Billing address", model.customer.billing_address))
    if model.customer.shipping_address:
        second.append(block("Shipping address", model.customer.shipping_address))

    flowables: list = []
    for row in (first_row, second):
        if not row:
            continue
        while len(row) < 3:
            row.append([])
        table = Table([row], colWidths=[width / 3.0] * 3)
        table.setStyle(
            TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * scale),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6 * scale),
            ])
        )
        flowables.append(table)
    flowables.append(Spacer(1, 4 * scale))
    return flowables


def _line_table(model: QuotationDocument, styles, width: float, scale: float = 1.0):  # noqa: ANN001
    numeric = set(model.numeric_column_indexes)

    header = [
        Paragraph(_escape(h), styles["head_right"] if i in numeric else styles["head"])
        for i, h in enumerate(model.column_headings)
    ]
    body = [
        [
            Paragraph(
                _escape(cell),
                styles["cell_right"] if i in numeric else styles["cell"],
            )
            for i, cell in enumerate(line.cells(model.columns))
        ]
        for line in model.lines
    ]

    # Description gets the slack; everything else shares what is left. Money
    # columns are given extra over the 1.0 default because a wrapped figure
    # ("$13,545.0" above "0") reads as a different number at a glance.
    weights = [COLUMN_WEIGHTS.get(key, 1.0) for key in model.columns]
    total_weight = sum(weights) or 1
    col_widths = [width * w / total_weight for w in weights]

    table = LongTable(
        [header, *body],
        colWidths=col_widths,
        repeatRows=1,      # the header reappears on every page
        splitByRow=True,   # split between rows, never through one
    )
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            # Scaled with the type. Row padding is the single largest
            # consumer of height on a short quotation -- 16pt a row before
            # a word is set -- so a compact render that left it alone
            # bought almost nothing.
            ("TOPPADDING", (0, 0), (-1, -1), 8 * scale),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8 * scale),
            ("LEFTPADDING", (0, 0), (-1, -1), 7 * scale),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7 * scale),
        ])
    )
    return table


def _shipping_flowables(model: QuotationDocument, styles, width: float) -> list:  # noqa: ANN001
    """The shipping summary: incoterms, origin, loading, container count.

    No table. The per-container breakdown was removed on 2026-08-16 — it
    listed shipping line, size, type, quantity, ports, transit and freight for
    every container, spilled onto a second page on a two-container shipment,
    and told the customer nothing the line items and the freight charge in the
    totals do not. What remains is one grey line of trade terms, which is why
    the section heading went with the table: a heading over a single sentence
    is furniture.
    """
    del width  # the summary is a paragraph; it needs no column arithmetic

    shipping = model.shipping
    if not shipping:
        return []

    flowables: list = []

    if shipping.summary:
        summary_text = "  ·  ".join(
            f"<font color='#5f6b7a'>{_escape(label)}:</font> {_escape(value)}"
            for label, value in shipping.summary
        )
        flowables.append(Paragraph(summary_text, styles["term"]))
    if shipping.freight_statement:
        flowables.append(Paragraph(_escape(shipping.freight_statement), styles["term"]))
    if shipping.notes:
        flowables.append(Spacer(1, 3))
        flowables.append(Paragraph(_escape(shipping.notes), styles["term"]))
    return flowables


def _totals_table(model: QuotationDocument, styles, width: float, scale: float = 1.0):  # noqa: ANN001
    """The money block, right-aligned, with the grand total carrying the weight.

    Delegates to the shared primitive so the customer PDF and this one place
    their totals identically — including the column-width trap documented
    there, which is invisible to a text-extraction test.
    """
    return money_block(
        [(t.label, t.amount, t.emphasis) for t in model.totals], styles, width,
        scale,
    )


def _signature_flowables(model: QuotationDocument, styles, width: float, scale: float = 1.0) -> list:  # noqa: ANN001
    cells = [
        [
            Paragraph("PREPARED BY", styles["label"]),
            Paragraph(_escape(model.prepared_by or model.signature_name or "—"), styles["body"]),
            Paragraph(
                _escape(model.prepared_by_title or model.signature_title or ""),
                styles["company_detail"],
            ),
        ],
        [
            Paragraph("APPROVED BY", styles["label"]),
            Paragraph(_escape(model.approved_by or "—"), styles["body"]),
        ],
        [
            Paragraph("DATE", styles["label"]),
            Paragraph(format_date(model.quote_date), styles["body"]),
        ],
    ]
    table = Table([cells], colWidths=[width / 3.0] * 3)
    table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 4 * scale),
        ])
    )

    flowables = [Spacer(1, 14 * scale), table]

    if model.show_acceptance_line:
        # A printed acceptance line only. There is deliberately no electronic
        # signature and nothing that links back to this application.
        flowables += [
            Spacer(1, 16 * scale),
            Paragraph("CUSTOMER ACCEPTANCE", styles["label"]),
            Spacer(1, 14 * scale),
            Table(
                [[
                    Paragraph("Signature", styles["company_detail"]),
                    Paragraph("Name", styles["company_detail"]),
                    Paragraph("Date", styles["company_detail"]),
                ]],
                colWidths=[width / 3.0] * 3,
                style=TableStyle([
                    ("LINEABOVE", (0, 0), (-1, 0), 0.5, INK),
                    ("TOPPADDING", (0, 0), (-1, -1), 3 * scale),
                    ("LEFTPADDING", (0, 0), (0, -1), 0),
                ]),
            ),
        ]
    return flowables


def render(document: QuotationDocument, page_size: str = "A4") -> bytes:
    """Render the document, on one page where one page is achievable.

    A quotation that spills a few centimetres onto a second sheet is a worse
    document than the same quotation set slightly tighter: the reader has to
    turn over for a signature line. So the render is attempted at full size,
    and only if that takes two pages is it retried at progressively tighter
    scales — and only a retry that actually reaches one page is used.

    A genuinely long quotation is left at full size and paginates as before.
    Shrinking type on a document that was always going to run to three pages
    buys nothing and costs legibility.
    """
    full, pages = _render_at(document, page_size, 1.0)
    if pages <= 1:
        return full

    for scale in _FIT_SCALES:
        attempt, attempt_pages = _render_at(document, page_size, scale)
        if attempt_pages <= 1:
            return attempt

    return full


def _render_at(
    document: QuotationDocument, page_size: str, scale: float
) -> tuple[bytes, int]:
    """One render at one scale, with the page count it came to."""
    buffer = BytesIO()
    size = PAGE_SIZES.get((page_size or "A4").upper(), A4)
    # A wide column set does not fit portrait; rotating beats truncating.
    if len(document.columns) > 8:
        size = landscape(size)

    template = _QuotationTemplate(buffer, document, size, scale)
    styles = template.styles
    width = template.width

    story: list = []
    story += _header_flowables(document, styles, width, scale)
    story += _customer_flowables(document, styles, width, scale)
    story.append(_line_table(document, styles, width, scale))
    story.append(Spacer(1, 8 * scale))
    story.append(_totals_table(document, styles, width, scale))

    if document.customer_notes:
        story.append(Paragraph("Notes", styles["section"]))
        story.append(Paragraph(_escape(document.customer_notes), styles["term"]))

    if document.terms:
        story.append(Paragraph("Terms and conditions", styles["section"]))
        for term in document.terms:
            # Keep a term's heading with its text rather than letting a page
            # break separate them.
            story.append(
                KeepTogether([
                    Paragraph(f"<b>{_escape(term.title)}</b>", styles["term"]),
                    Paragraph(_escape(term.body), styles["term"]),
                    Spacer(1, 4 * scale),
                ])
            )

    # Below the terms, not above them. Incoterms, country of origin, loading
    # and container count are trade terms: they belong with the conditions of
    # sale rather than wedged between the total and the notes, where they
    # separated the figure from everything explaining it.
    story += _shipping_flowables(document, styles, width)

    story += _signature_flowables(document, styles, width, scale)

    if document.thank_you_text:
        story.append(Spacer(1, 12 * scale))
        story.append(Paragraph(_escape(document.thank_you_text), styles["footer"]))

    template.build(story)
    return buffer.getvalue(), template.page
