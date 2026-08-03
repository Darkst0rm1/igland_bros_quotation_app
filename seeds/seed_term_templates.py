"""Seed reusable term templates.

Sources, both treated as *starting points* rather than fixed text:

* ``White Boxes B Flute Quotation.xlsx`` — payment, printing, delivery,
  loading, container mix and the plate charge.
* ``ECOPAC_Quotation_QT-2026-0728_BunzlPizzaBox_1.pdf`` — the shape of the
  lead-time, validity, freight, raw-material, approval, PO and overrun clauses.
  Wording is original; only the commercial subject matter is taken across.

Two deliberate choices:

* ``is_default`` is **not** set on everything. The brief is explicit that not
  every term belongs on every quotation, so only the clauses that genuinely
  always apply are pre-ticked.
* The workbook's "Valid through July '26" is **not** seeded as a validity term.
  It is a historical date from a specific quotation; the validity clause here
  refers to the quotation's own expiry date instead.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.constants import TermSection
from modules.models import TermTemplate

log = logging.getLogger(__name__)

TERM_TEMPLATES: list[dict[str, object]] = [
    {
        "code": "PAYMENT_ON_RECEIPT",
        "section": TermSection.PAYMENT_TERMS,
        "title": "Payment terms",
        "body_text": "Payment upon receipt.",
        "is_default": True,
        "sort_order": 10,
    },
    {
        "code": "VALIDITY_UNTIL_DATE",
        "section": TermSection.QUOTATION_VALIDITY,
        "title": "Quotation validity",
        "body_text": (
            "Quotation pricing is valid until the expiry date shown on this quotation."
        ),
        "is_default": True,
        "sort_order": 20,
    },
    {
        "code": "PRICING_BASIS_TIER",
        "section": TermSection.GENERAL_NOTES,
        "title": "Pricing basis",
        "body_text": (
            "Pricing is based on the quantity and pricing tier stated on this quotation."
        ),
        "is_default": True,
        "sort_order": 30,
    },
    {
        "code": "LEAD_TIME_AFTER_APPROVAL",
        "section": TermSection.LEAD_TIME,
        "title": "Lead time",
        "body_text": (
            "Lead time begins on receipt of the purchase order together with all "
            "required artwork and structural approvals."
        ),
        "is_default": True,
        "sort_order": 40,
    },
    {
        "code": "RAW_MATERIAL_ADJUSTMENT",
        "section": TermSection.RAW_MATERIAL_ADJUSTMENT,
        "title": "Raw material price adjustment",
        "body_text": (
            "Prices remain subject to changes in raw material costs until the order "
            "is confirmed."
        ),
        "is_default": True,
        "sort_order": 50,
    },
    {
        "code": "FLEXO_PRINTING",
        "section": TermSection.PRINTING,
        "title": "Printing",
        "body_text": "Flexographic printing to be applied.",
        "is_default": False,
        "sort_order": 60,
    },
    {
        "code": "PLATE_CHARGE_PER_SIZE_COLOUR",
        "section": TermSection.PRINTING_PLATE_CHARGES,
        "title": "Printing plate charges",
        "body_text": (
            "Printing plates are charged separately according to product size and "
            "number of colours."
        ),
        "is_default": False,
        "sort_order": 70,
    },
    {
        "code": "FOB_CERKEZKOY_INCOTERMS_2020",
        "section": TermSection.DELIVERY_TERMS,
        "title": "Delivery terms",
        "body_text": "FOB Çerkezköy, Türkiye (Incoterms 2020).",
        "is_default": False,
        "sort_order": 80,
    },
    {
        "code": "CONTAINER_40HC_FLOOR_LOADED",
        "section": TermSection.CONTAINER_TYPE,
        "title": "Container type and loading",
        "body_text": (
            "Shipment in 40-foot high-cube containers, floor loaded."
        ),
        "is_default": False,
        "sort_order": 90,
    },
    {
        "code": "CONTAINER_MAX_THREE_ITEMS",
        "section": TermSection.CONTAINER_MIX_LIMIT,
        "title": "Container product mix",
        "body_text": "Containers are to be filled with a maximum of three product items.",
        "is_default": False,
        "sort_order": 100,
    },
    {
        "code": "ARTWORK_APPROVAL_REQUIRED",
        "section": TermSection.ARTWORK_APPROVAL,
        "title": "Artwork approval",
        "body_text": (
            "Production is subject to receipt of final artwork approval in writing."
        ),
        "is_default": False,
        "sort_order": 110,
    },
    {
        "code": "STRUCTURAL_APPROVAL_REQUIRED",
        "section": TermSection.STRUCTURAL_APPROVAL,
        "title": "Structural approval",
        "body_text": (
            "Production is subject to receipt of structural approval in writing."
        ),
        "is_default": False,
        "sort_order": 120,
    },
    {
        "code": "PO_REQUIRED",
        "section": TermSection.PURCHASE_ORDER,
        "title": "Purchase order",
        "body_text": (
            "A purchase order is required for both product and any associated tooling "
            "before production is scheduled."
        ),
        "is_default": False,
        "sort_order": 130,
    },
    {
        "code": "MOQ_APPLIES",
        "section": TermSection.MOQ,
        "title": "Minimum order quantity",
        "body_text": "Pricing is based on the minimum order quantity stated per line.",
        "is_default": False,
        "sort_order": 140,
    },
    {
        "code": "OVERRUN_UNDERRUN_10PCT",
        "section": TermSection.OVERRUN_UNDERRUN,
        "title": "Production overrun and underrun",
        "body_text": (
            "A production underrun or overrun of up to 10% may apply and will be "
            "invoiced at the quoted unit price."
        ),
        "is_default": False,
        "sort_order": 150,
    },
    {
        "code": "FREIGHT_TBD",
        "section": TermSection.FREIGHT,
        "title": "Freight",
        "body_text": "Freight and release schedule to be confirmed.",
        "is_default": False,
        "sort_order": 160,
    },
]


def run(session: Session) -> None:
    """Insert any missing templates. Existing wording is never overwritten —
    an employee or manager may have deliberately reworded a clause."""
    existing = {t.code for t in session.execute(select(TermTemplate)).scalars()}
    added = 0
    for spec in TERM_TEMPLATES:
        if spec["code"] in existing:
            continue
        session.add(TermTemplate(**spec, is_active=True, version=1))
        added += 1
    session.flush()
    log.info("Term templates seeded: %d new, %d already present", added, len(existing))
