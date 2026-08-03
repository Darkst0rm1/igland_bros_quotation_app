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
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
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
from modules.utilities import format_date

log = logging.getLogger(__name__)

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#5f6b7a")
RULE = colors.HexColor("#c8d0da")
BAND = colors.HexColor("#eef1f5")

PAGE_SIZES = {"A4": A4, "LETTER": LETTER}


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "company": ParagraphStyle(
            "company", parent=base["Title"], fontSize=16, leading=19,
            textColor=INK, alignment=0, spaceAfter=2,
        ),
        "company_detail": ParagraphStyle(
            "company_detail", parent=base["Normal"], fontSize=8, leading=11,
            textColor=MUTED,
        ),
        "doc_title": ParagraphStyle(
            "doc_title", parent=base["Title"], fontSize=20, leading=23,
            textColor=INK, alignment=TA_RIGHT, spaceAfter=2,
        ),
        "meta": ParagraphStyle(
            "meta", parent=base["Normal"], fontSize=9, leading=12,
            alignment=TA_RIGHT, textColor=INK,
        ),
        "label": ParagraphStyle(
            "label", parent=base["Normal"], fontSize=7.5, leading=10,
            textColor=MUTED, spaceAfter=1,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontSize=9, leading=12, textColor=INK,
        ),
        "cell": ParagraphStyle(
            "cell", parent=base["Normal"], fontSize=8, leading=10.5, textColor=INK,
        ),
        "cell_right": ParagraphStyle(
            "cell_right", parent=base["Normal"], fontSize=8, leading=10.5,
            alignment=TA_RIGHT, textColor=INK,
        ),
        "head": ParagraphStyle(
            "head", parent=base["Normal"], fontSize=7.5, leading=10,
            textColor=INK, fontName="Helvetica-Bold",
        ),
        "head_right": ParagraphStyle(
            "head_right", parent=base["Normal"], fontSize=7.5, leading=10,
            alignment=TA_RIGHT, textColor=INK, fontName="Helvetica-Bold",
        ),
        "section": ParagraphStyle(
            "section", parent=base["Normal"], fontSize=10, leading=13,
            textColor=INK, fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=4,
        ),
        "term": ParagraphStyle(
            "term", parent=base["Normal"], fontSize=8.5, leading=11.5, textColor=INK,
        ),
        "footer": ParagraphStyle(
            "footer", parent=base["Normal"], fontSize=7, leading=9,
            textColor=MUTED, alignment=TA_CENTER,
        ),
    }


def _escape(text: str) -> str:
    """Escape for ReportLab's mini-markup, keeping newlines as line breaks."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


class _QuotationTemplate(BaseDocTemplate):
    """Adds the repeating footer, page numbers and the DRAFT watermark."""

    def __init__(self, buffer: BytesIO, document: QuotationDocument, page_size) -> None:  # noqa: ANN001
        self.document_model = document
        self.styles = _styles()
        super().__init__(
            buffer,
            pagesize=page_size,
            leftMargin=16 * mm,
            rightMargin=16 * mm,
            topMargin=14 * mm,
            bottomMargin=18 * mm,
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


def _header_flowables(model: QuotationDocument, styles, width: float) -> list:  # noqa: ANN001
    left: list = []
    if model.company.logo_bytes:
        try:
            logo = Image(BytesIO(model.company.logo_bytes))
            ratio = logo.imageHeight / float(logo.imageWidth or 1)
            logo.drawWidth = 45 * mm
            logo.drawHeight = 45 * mm * ratio
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
    return [table, Spacer(1, 10)]


def _customer_flowables(model: QuotationDocument, styles, width: float) -> list:  # noqa: ANN001
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
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ])
        )
        flowables.append(table)
    flowables.append(Spacer(1, 4))
    return flowables


def _line_table(model: QuotationDocument, styles, width: float):  # noqa: ANN001
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

    # Description gets the slack; everything else shares what is left.
    weights = [
        3.2 if key == "description" else 1.6 if key == "spec" else 1.0
        for key in model.columns
    ]
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
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ])
    )
    return table


def _totals_table(model: QuotationDocument, styles, width: float):  # noqa: ANN001
    rows = []
    for total in model.totals:
        label_style = styles["head"] if total.emphasis else styles["cell"]
        value_style = styles["head_right"] if total.emphasis else styles["cell_right"]
        rows.append([
            Paragraph(_escape(total.label), label_style),
            Paragraph(_escape(total.amount), value_style),
        ])

    table = Table(rows, colWidths=[width * 0.62, width * 0.38])
    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    if rows:
        style += [
            ("LINEABOVE", (0, len(rows) - 1), (-1, len(rows) - 1), 0.8, INK),
            ("TOPPADDING", (0, len(rows) - 1), (-1, len(rows) - 1), 6),
        ]
    table.setStyle(TableStyle(style))

    wrapper = Table([["", table]], colWidths=[width * 0.5, width * 0.5])
    wrapper.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ])
    )
    return wrapper


def _signature_flowables(model: QuotationDocument, styles, width: float) -> list:  # noqa: ANN001
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
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ])
    )

    flowables = [Spacer(1, 14), table]

    if model.show_acceptance_line:
        # A printed acceptance line only. There is deliberately no electronic
        # signature and nothing that links back to this application.
        flowables += [
            Spacer(1, 16),
            Paragraph("CUSTOMER ACCEPTANCE", styles["label"]),
            Spacer(1, 14),
            Table(
                [[
                    Paragraph("Signature", styles["company_detail"]),
                    Paragraph("Name", styles["company_detail"]),
                    Paragraph("Date", styles["company_detail"]),
                ]],
                colWidths=[width / 3.0] * 3,
                style=TableStyle([
                    ("LINEABOVE", (0, 0), (-1, 0), 0.5, INK),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (0, -1), 0),
                ]),
            ),
        ]
    return flowables


def render(document: QuotationDocument, page_size: str = "A4") -> bytes:
    """Render the document and return the PDF bytes."""
    buffer = BytesIO()
    size = PAGE_SIZES.get((page_size or "A4").upper(), A4)
    # A wide column set does not fit portrait; rotating beats truncating.
    if len(document.columns) > 8:
        size = landscape(size)

    template = _QuotationTemplate(buffer, document, size)
    styles = template.styles
    width = template.width

    story: list = []
    story += _header_flowables(document, styles, width)
    story += _customer_flowables(document, styles, width)
    story.append(_line_table(document, styles, width))
    story.append(Spacer(1, 8))
    story.append(_totals_table(document, styles, width))

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
                    Spacer(1, 4),
                ])
            )

    story += _signature_flowables(document, styles, width)

    if document.thank_you_text:
        story.append(Spacer(1, 12))
        story.append(Paragraph(_escape(document.thank_you_text), styles["footer"]))

    template.build(story)
    return buffer.getvalue()
