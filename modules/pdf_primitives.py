"""Layout parts shared by the employee and customer PDF renderers.

Two renderers exist because they consume two different models: the employee one
takes :class:`~modules.document_model.QuotationDocument`, the customer one takes
:class:`~portal.pdf_model.CustomerPdfDocument`. Keeping them separate is the
whole point — an internal document must not be *capable* of reaching the
customer renderer, so there is no shared entry point that accepts either.

What is safe to share is everything below the model: escaping, the palette, the
paragraph styles and the money block. None of it knows what a quotation is, so
none of it can leak one.
"""
from __future__ import annotations

from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Table, TableStyle

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#5f6b7a")
RULE = colors.HexColor("#c8d0da")
BAND = colors.HexColor("#eef1f5")

PAGE_SIZES = {"A4": A4, "LETTER": LETTER}


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #

#: The base-14 fonts every PDF library defaults to are Latin-1 only. The
#: company is Turkish, so its own address contains ş, ı and İ — none of which
#: exist in Helvetica, and each of which ReportLab renders as a filled black
#: box. Every quotation, every accepted artifact and every price list carried
#: those boxes, in the company's address block, on documents sent to customers.
#:
#: Bitstream Vera ships inside ReportLab, so this costs no new dependency and
#: is present wherever the worker runs, which a system font path would not be.
#: It covers Latin Extended-A, which is what Turkish needs.
FONT = "Vera"
FONT_BOLD = "Vera-Bold"
FONT_ITALIC = "Vera-Italic"

_FILES = {
    FONT: "Vera.ttf",
    FONT_BOLD: "VeraBd.ttf",
    FONT_ITALIC: "VeraIt.ttf",
    "Vera-BoldItalic": "VeraBI.ttf",
}


def register_fonts() -> str:
    """Register the Unicode family, once. Returns the base font name.

    Idempotent: ReportLab keeps a process-wide registry and re-registering the
    same name is wasted work in a worker that renders continuously.
    """
    import pathlib

    import reportlab
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if FONT in pdfmetrics.getRegisteredFontNames():
        return FONT

    folder = pathlib.Path(reportlab.__file__).parent / "fonts"
    for name, filename in _FILES.items():
        pdfmetrics.registerFont(TTFont(name, str(folder / filename)))

    # So <b> and <i> in paragraph markup resolve within the family rather than
    # falling back to Helvetica and reintroducing the boxes on bold text alone.
    from reportlab.lib.fonts import addMapping

    addMapping(FONT, 0, 0, FONT)
    addMapping(FONT, 1, 0, FONT_BOLD)
    addMapping(FONT, 0, 1, FONT_ITALIC)
    addMapping(FONT, 1, 1, "Vera-BoldItalic")
    return FONT


def escape(text: str) -> str:
    """Escape for ReportLab's mini-markup, keeping newlines as line breaks.

    Everything reaching a renderer is untrusted: a customer typed the signature
    and comment, an employee typed the description and terms. An unescaped
    ``<`` would be read as markup and silently swallow the text after it, or
    fail the build outright.
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


def base_styles() -> dict[str, ParagraphStyle]:
    """The shared paragraph styles, by name.

    Every style names the Unicode family explicitly. Inheriting from
    ``getSampleStyleSheet`` would inherit Helvetica, which has no Turkish
    glyphs and prints them as black boxes.
    """
    register_fonts()
    base = getSampleStyleSheet()
    styles = {
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
            "cell", parent=base["Normal"], fontSize=8.8, leading=12, textColor=INK,
        ),
        "cell_right": ParagraphStyle(
            "cell_right", parent=base["Normal"], fontSize=8.8, leading=12,
            alignment=TA_RIGHT, textColor=INK,
        ),
        "head": ParagraphStyle(
            "head", parent=base["Normal"], fontSize=7.5, leading=10,
            textColor=MUTED, fontName=FONT_BOLD,
        ),
        "head_right": ParagraphStyle(
            "head_right", parent=base["Normal"], fontSize=7.5, leading=10,
            alignment=TA_RIGHT, textColor=MUTED, fontName=FONT_BOLD,
        ),
        "section": ParagraphStyle(
            "section", parent=base["Normal"], fontSize=10, leading=13,
            textColor=INK, fontName=FONT_BOLD, spaceBefore=8, spaceAfter=4,
        ),
        # The money block. Subtotal rows stay quiet so the grand total, which
        # is the number the customer is actually being asked about, carries the
        # emphasis on its own.
        "total_label": ParagraphStyle(
            "total_label", parent=base["Normal"], fontSize=9, leading=12,
            textColor=MUTED,
        ),
        "total_value": ParagraphStyle(
            "total_value", parent=base["Normal"], fontSize=9, leading=12,
            alignment=TA_RIGHT, textColor=INK,
        ),
        "grand_label": ParagraphStyle(
            "grand_label", parent=base["Normal"], fontSize=8.5, leading=12,
            textColor=MUTED, fontName=FONT_BOLD,
        ),
        "grand_value": ParagraphStyle(
            "grand_value", parent=base["Title"], fontSize=16, leading=19,
            alignment=TA_RIGHT, textColor=INK,
        ),
        "term": ParagraphStyle(
            "term", parent=base["Normal"], fontSize=8.5, leading=11.5, textColor=INK,
        ),
        "footer": ParagraphStyle(
            "footer", parent=base["Normal"], fontSize=7, leading=9,
            textColor=MUTED, alignment=TA_CENTER,
        ),
    }

    # Applied here rather than on each style above: every one inherits from the
    # sample stylesheet, whose font is Helvetica, and missing a single style
    # would put black boxes in one paragraph of an otherwise correct document.
    # The bold styles already name their own face and keep it.
    for style in styles.values():
        if style.fontName not in (FONT_BOLD, FONT_ITALIC):
            style.fontName = FONT
    return styles


def money_block(
    rows: list[tuple[str, str, bool]], styles: dict, width: float
):  # noqa: ANN201
    """The right-aligned totals block. ``rows`` is ``(label, amount, emphasis)``.

    Takes formatted strings rather than Decimals: both renderers format at their
    own presentation boundary, and a shared helper that formatted money would be
    a second place where rounding could be decided.

    ``block`` is the width this actually occupies, and every column inside is a
    fraction of ``block`` — never of ``width``. Sizing the inner columns against
    the full page width makes them overflow the wrapper cell, and ReportLab
    pushes the amount column past the right margin: the labels print, the
    figures do not. ``extract_text`` still finds the numbers in the content
    stream, so a text-presence test cannot catch it.

    **Row order is the caller's.** This used to draw every quiet row and then
    every emphasised one, which was indistinguishable from preserving order for
    as long as the grand total happened to be last. It stopped being so the
    moment a deposit and balance were added below the total: they were hoisted
    above it and read as components summing into it, which is the opposite of
    what they are. Consecutive rows of the same weight are grouped into one
    table, and the groups are stacked in the order given.
    """
    from itertools import groupby

    from reportlab.platypus import Paragraph, Spacer

    block = width * 0.52
    stacked: list = []

    for emphasised, group in groupby(rows, key=lambda row: bool(row[2])):
        run = [(label, amount) for label, amount, _ in group]
        if not emphasised:
            quiet_table = Table(
                [
                    [
                        Paragraph(escape(label), styles["total_label"]),
                        Paragraph(escape(amount), styles["total_value"]),
                    ]
                    for label, amount in run
                ],
                colWidths=[block * 0.58, block * 0.42],
            )
            quiet_table.setStyle(
                TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
                ])
            )
            stacked.append(quiet_table)
            continue

        stacked.extend(_grand_tables(run, styles, block))

    if not stacked:
        return Spacer(1, 0)

    return _wrap_money_block(stacked, width, block)


def _grand_tables(rows: list[tuple[str, str]], styles: dict, block: float) -> list:
    """The emphasised rows, one banded table each."""
    from reportlab.platypus import Paragraph

    stacked: list = []
    for label, amount in rows:
        grand_table = Table(
            [[
                Paragraph(escape(label.upper()), styles["grand_label"]),
                Paragraph(escape(amount), styles["grand_value"]),
            ]],
            colWidths=[block * 0.42, block * 0.58],
        )
        grand_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), BAND),
                ("LINEABOVE", (0, 0), (-1, 0), 1.0, INK),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ])
        )
        stacked.append(grand_table)

    return stacked


def _wrap_money_block(stacked: list, width: float, block: float):  # noqa: ANN201
    """Stack the groups and push the whole block to the right margin."""
    inner = Table([[flowable] for flowable in stacked], colWidths=[block])
    inner.setStyle(
        TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])
    )

    wrapper = Table([["", inner]], colWidths=[width - block, block])
    wrapper.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ])
    )
    return wrapper


def table_style(header_band: bool = True) -> TableStyle:
    """The line-table look: banded header, hairline rows, generous padding."""
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
    ]
    if header_band:
        commands += [
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
        ]
    return TableStyle(commands)


def quantity_text(value: Decimal | None) -> str:
    """Quantities without their stored scale: 3500, not 3500.000.

    ``Decimal.normalize()`` turns 1000.000 into ``1E+3``, which would print
    literally, so integral values go through ``int`` instead.
    """
    if value is None:
        return ""
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        return f"{int(normalised):,}"
    return f"{normalised:,f}"
