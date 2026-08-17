"""Renders :class:`~portal.pdf_model.CustomerPdfDocument`, and nothing else.

Deliberately separate from :mod:`modules.pdf_generator`. That one takes the
employee document model, which is built from a session and carries whatever the
company has configured onto a quotation; this one can only be handed a model
that has no field for a cost. Two renderers with two signatures means an
internal document cannot be passed to the customer one by mistake — the type is
wrong, and there is no shared entry point that accepts either.

What is shared is everything below the model: escaping, palette, styles and the
totals block, from :mod:`modules.pdf_primitives`.

No remote resources. The only image is the logo, and it arrives as validated
bytes on the model; there is no code path here that opens a URL, a file or a
storage key.
"""
from __future__ import annotations

import logging
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    LongTable,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from modules.pdf_primitives import (
    INK,
    MUTED,
    PAGE_SIZES,
    RULE,
    base_styles,
    escape,
    money_block,
    quantity_text,
    table_style,
)
from modules.utilities import format_date, format_money
from portal.pdf_model import CustomerPdfDocument

log = logging.getLogger(__name__)

#: Refuse to hand back anything larger. A public route must not be a way to
#: make the server produce an arbitrarily large response.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

#: Column widths for the line table, relative to each other.
COLUMN_WEIGHTS = (0.55, 3.4, 1.4, 1.0, 1.15, 1.35)

ACCENT_ACCEPTED = colors.HexColor("#1f7a45")


class PdfTooLargeError(RuntimeError):
    """The rendered document exceeded the size a public route will return."""


def _money(value, currency: str) -> str:  # noqa: ANN001
    """The single presentation boundary. Everything above here is Decimal."""
    if value is None:
        return ""
    negative = value < 0
    text = format_money(abs(value), currency)
    return f"−{text}" if negative else text


class _CustomerTemplate(BaseDocTemplate):
    """Repeating footer, page numbers, and the status stamp."""

    def __init__(self, buffer: BytesIO, document: CustomerPdfDocument, page_size) -> None:  # noqa: ANN001
        self.model = document
        self.styles = base_styles()
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
            # No creator string identifying the application version: metadata
            # travels with the file to people who were never sent the link.
            creator=document.company.name or "Quotation",
        )
        frame = Frame(
            self.leftMargin, self.bottomMargin, self.width, self.height, id="body"
        )
        self.addPageTemplates(
            [PageTemplate(id="customer", frames=[frame], onPage=self._decorate)]
        )

    def _decorate(self, canvas, doc) -> None:  # noqa: ANN001
        canvas.saveState()
        model = self.model

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
        if model.legal_footer:
            canvas.drawString(
                self.leftMargin, footer_y + 0.5 * mm, model.legal_footer[:180]
            )
        canvas.restoreState()


def _header(model: CustomerPdfDocument, styles, width: float) -> list:  # noqa: ANN001
    left: list = []
    if model.company.logo_bytes:
        try:
            logo = Image(BytesIO(model.company.logo_bytes))
            ratio = logo.imageHeight / float(logo.imageWidth or 1)
            logo.drawWidth = 42 * mm
            logo.drawHeight = 42 * mm * ratio
            left.append(logo)
            left.append(Spacer(1, 3))
        except Exception:  # noqa: BLE001 — a bad logo must not stop a quotation
            log.warning("Customer PDF logo could not be rendered; continuing without it")

    left.append(Paragraph(escape(model.company.name), styles["company"]))
    for line in model.company.address_lines:
        left.append(Paragraph(escape(line), styles["company_detail"]))
    if model.company.contact_line:
        left.append(
            Paragraph(escape(model.company.contact_line), styles["company_detail"])
        )

    meta_rows = [
        ("Quotation no.", f"{model.quote_number}  {model.revision_label}"),
        ("Date", format_date(model.quote_date)),
    ]
    if model.valid_until and not model.is_accepted:
        meta_rows.append(("Valid until", format_date(model.valid_until)))
    if model.acceptance:
        meta_rows.append(
            ("Accepted", format_date(model.acceptance.accepted_at))
        )
    if model.sales_representative:
        meta_rows.append(("Your representative", model.sales_representative))

    meta_text = "<br/>".join(
        f"<font color='#5f6b7a'>{escape(label)}:</font> <b>{escape(value)}</b>"
        for label, value in meta_rows
    )

    right = [
        Paragraph(escape(model.title), styles["doc_title"]),
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


def _accepted_banner(model: CustomerPdfDocument, styles, width: float) -> list:  # noqa: ANN001
    """A visible mark that this is a record, not an offer."""
    if not model.is_accepted:
        return []

    banner = Table(
        [[Paragraph(
            "<b>ACCEPTED</b> &nbsp; " + escape(model.scope_note), styles["body"]
        )]],
        colWidths=[width],
    )
    banner.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eaf5ee")),
            ("LINEABOVE", (0, 0), (-1, 0), 1.2, ACCENT_ACCEPTED),
            ("LINEBELOW", (0, -1), (-1, -1), 1.2, ACCENT_ACCEPTED),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ])
    )
    return [banner, Spacer(1, 10)]


def _customer_block(model: CustomerPdfDocument, styles, width: float) -> list:  # noqa: ANN001
    def block(label: str, value: str):  # noqa: ANN202
        return [
            Paragraph(label.upper(), styles["label"]),
            Paragraph(escape(value or "—"), styles["body"]),
        ]

    contact_bits = [
        b for b in (
            model.customer.contact_name,
            model.customer.contact_email,
            model.customer.contact_phone,
        ) if b
    ]

    rows = [[
        block("Prepared for", model.customer.company),
        block("Contact", "\n".join(contact_bits)),
        block(
            "PO reference" if model.customer.purchase_order else "Billing address",
            model.customer.purchase_order or model.customer.billing_address,
        ),
    ]]
    if model.customer.shipping_address:
        rows.append([block("Shipping address", model.customer.shipping_address), [], []])

    flowables: list = []
    for row in rows:
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


def _line_table(model: CustomerPdfDocument, styles, width: float):  # noqa: ANN001
    """The product table.

    Selectable lines carry their status in a column of their own rather than
    only by shading: a customer printing this in black and white must still be
    able to tell what they are being charged for.
    """
    headings = ["#", "Product / service", "Included", "Qty", "Unit price", "Total"]
    header = [
        Paragraph(escape(h), styles["head_right"] if i >= 3 else styles["head"])
        for i, h in enumerate(headings)
    ]

    body = []
    unselected_rows: list[int] = []
    for index, line in enumerate(model.lines, start=1):
        detail = [f"<b>{escape(line.description)}</b>"]
        for extra in (line.specification, line.size, line.pack_size, line.remarks):
            if extra:
                detail.append(escape(extra))

        # An explicit line break rather than letting the cell wrap where it
        # likes: "Optional —" above a lone "taken" reads as a broken sentence.
        if not line.is_selectable:
            status = "Included"
        elif line.is_selected:
            status = f"{escape(line.inclusion_label)}<br/>added"
        else:
            status = f"{escape(line.inclusion_label)}<br/>not added"
            unselected_rows.append(index)

        body.append([
            Paragraph(str(line.line_no), styles["cell"]),
            Paragraph("<br/>".join(detail), styles["cell"]),
            Paragraph(status, styles["cell_right"]),
            Paragraph(quantity_text(line.quantity_packs), styles["cell_right"]),
            Paragraph(_money(line.unit_price, model.currency), styles["cell_right"]),
            Paragraph(
                _money(line.line_total, model.currency) if line.is_selected else "—",
                styles["cell_right"],
            ),
        ])

    total_weight = sum(COLUMN_WEIGHTS)
    table = LongTable(
        [header, *body],
        colWidths=[width * w / total_weight for w in COLUMN_WEIGHTS],
        repeatRows=1,      # the header reappears on every page
        splitByRow=True,   # split between rows, never through one
    )
    style = table_style()
    for row in unselected_rows:
        # Quiet, not hidden: the line stays legible so the customer can see
        # what is still available to them.
        style.add("TEXTCOLOR", (0, row), (-1, row), MUTED)
    table.setStyle(style)
    return table


def _deposit(model: CustomerPdfDocument, styles, width: float) -> list:  # noqa: ANN001
    """The deposit, stated *after* the total and as a share of it.

    Never inside the money block: that prints its quiet rows above the
    emphasised total, so a deposit among them reads as another thing being
    added on. It is part of the total, not an addition to it, and putting it
    below is the difference between a figure a customer understands and one
    they query.
    """
    if not model.deposit_due:
        return []

    share = (
        f" ({quantity_text(model.deposit_pct)}% of the total)"
        if model.deposit_pct else " of the total"
    )
    text = (
        f"<b>Deposit due on order: "
        f"{_money(model.deposit_due, model.currency)}</b>"
        f"<font color='#5f6b7a'>{escape(share)}</font>"
    )
    holder = Table([[Paragraph(text, styles["term"])]], colWidths=[width * 0.52])
    holder.hAlign = "RIGHT"      # sits under the money block it refers to
    holder.setStyle(
        TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])
    )
    return [holder]


def _shipping(model: CustomerPdfDocument, styles, width: float) -> list:  # noqa: ANN001
    shipping = model.shipping
    if shipping is None:
        return []

    pairs = [
        ("Incoterm", f"{shipping.incoterm} {shipping.incoterm_place}".strip()),
        ("Containers", shipping.container_summary),
        ("Shipping line", shipping.shipping_line),
        ("Port of loading", shipping.port_of_loading),
        ("Port of discharge", shipping.port_of_discharge),
    ]
    present = [(label, value) for label, value in pairs if value]
    if not present and not shipping.notes:
        return []

    flowables: list = [Paragraph("Shipping", styles["section"])]
    if present:
        flowables.append(
            Paragraph(
                "  ·  ".join(
                    f"<font color='#5f6b7a'>{escape(label)}:</font> {escape(value)}"
                    for label, value in present
                ),
                styles["term"],
            )
        )
    if shipping.notes:
        flowables.append(Spacer(1, 3))
        flowables.append(Paragraph(escape(shipping.notes), styles["term"]))
    return flowables


def _signature(model: CustomerPdfDocument, styles, width: float) -> list:  # noqa: ANN001
    """The acceptance block. Only ever present on an accepted document."""
    accepted = model.acceptance
    if accepted is None:
        return []

    def cell(label: str, value: str):  # noqa: ANN202
        return [
            Paragraph(label.upper(), styles["label"]),
            Paragraph(escape(value or "—"), styles["body"]),
        ]

    signature_style = styles["grand_value"].clone("signature")
    signature_style.alignment = 0
    signature_style.fontName = "Helvetica-Oblique"
    signature_style.fontSize = 15

    detail = Table(
        [[
            cell("Accepted by", accepted.customer_name),
            cell("Job title", accepted.job_title),
            cell("Email", accepted.customer_email),
        ]],
        colWidths=[width / 3.0] * 3,
    )
    detail.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ])
    )

    signature = Table(
        [
            [Paragraph(escape(accepted.signature_name), signature_style)],
            [Paragraph(
                escape(
                    f"Signed electronically on "
                    f"{accepted.accepted_at:%d %b %Y at %H:%M} UTC · "
                    f"{accepted.revision_label}"
                ),
                styles["company_detail"],
            )],
        ],
        colWidths=[width * 0.55],
    )
    # Without this the table is centred in the frame — ReportLab's default —
    # and the signature floats away from the name it belongs under.
    signature.hAlign = "LEFT"
    signature.setStyle(
        TableStyle([
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (0, 0), 4),
        ])
    )

    return [
        Spacer(1, 14),
        Paragraph("Acceptance", styles["section"]),
        detail,
        Spacer(1, 6),
        # Kept whole: a signature separated from the name it belongs to reads
        # as an unsigned document.
        KeepTogether([signature]),
    ]


def render(document: CustomerPdfDocument, page_size: str = "A4") -> bytes:
    """Render the customer document and return the PDF bytes."""
    buffer = BytesIO()
    size = PAGE_SIZES.get((page_size or "A4").upper(), A4)

    template = _CustomerTemplate(buffer, document, size)
    styles = template.styles
    width = template.width

    story: list = []
    story += _header(document, styles, width)
    story += _accepted_banner(document, styles, width)
    story += _customer_block(document, styles, width)

    if document.scope_note and not document.is_accepted:
        story.append(Paragraph(escape(document.scope_note), styles["term"]))
        story.append(Spacer(1, 6))

    story.append(_line_table(document, styles, width))
    story.append(Spacer(1, 8))
    story.append(
        money_block(
            [
                (t.label, _money(t.amount, document.currency), t.emphasis)
                for t in document.totals
            ],
            styles, width,
        )
    )
    story += _deposit(document, styles, width)


    if document.customer_notes:
        story.append(Paragraph("Notes", styles["section"]))
        story.append(Paragraph(escape(document.customer_notes), styles["term"]))

    if document.terms:
        first, *rest = document.terms
        # The section heading travels with the first term. On its own it can be
        # left stranded at the foot of a page with nothing under it, which reads
        # as though the terms were omitted.
        story.append(
            KeepTogether([
                Paragraph("Terms and conditions", styles["section"]),
                Paragraph(f"<b>{escape(first.title)}</b>", styles["term"]),
                Paragraph(escape(first.body), styles["term"]),
                Spacer(1, 4),
            ])
        )
        for term in rest:
            # Keep a term's heading with its text rather than letting a page
            # break separate them.
            story.append(
                KeepTogether([
                    Paragraph(f"<b>{escape(term.title)}</b>", styles["term"]),
                    Paragraph(escape(term.body), styles["term"]),
                    Spacer(1, 4),
                ])
            )

    # Below the terms, as on the internal quotation: incoterms and the
    # container summary are conditions of sale, not a footnote to the
    # total they were previously wedged against.
    story += _shipping(document, styles, width)

    story += _signature(document, styles, width)

    if document.thank_you_text:
        story.append(Spacer(1, 12))
        story.append(Paragraph(escape(document.thank_you_text), styles["footer"]))

    template.build(story)
    data = buffer.getvalue()

    if len(data) > MAX_OUTPUT_BYTES:
        raise PdfTooLargeError(
            f"Rendered document is {len(data)} bytes; the limit is {MAX_OUTPUT_BYTES}."
        )
    return data
