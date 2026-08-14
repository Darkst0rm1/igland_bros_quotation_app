"""Seed price tiers, company settings and application tunables.

Company identity is seeded as **flagged placeholders**. Nothing about the company
Bros is compiled into the application; the Company Settings page shows a banner
until ``is_placeholder`` is cleared by a real edit.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.constants import PRICE_TIER_SEED
from modules.models import AppSetting, CompanySettings, PriceTier

log = logging.getLogger(__name__)


#: Tunable thresholds. Kept out of company_settings so that adding one is a data
#: change rather than a migration.
APP_SETTING_DEFAULTS: list[dict[str, object]] = [
    {
        "key": "tier_container_scope",
        "value_json": "quotation",
        "value_type": "string",
        "category": "pricing",
        "description": (
            "Whether the three/eight-container check counts containers across the "
            "whole quotation or per line. The brief does not say which; commercially "
            "the price is earned by the order, so the default is 'quotation'. "
            "Set to 'line' if the intended reading is per line."
        ),
    },
    {
        "key": "piece_pack_tolerance",
        "value_json": "0.0001",
        "value_type": "decimal",
        "category": "pricing",
        "description": (
            "Tolerance for the 'piece price does not equal pack price / case pack' "
            "warning. One rounding unit. The reference workbook's own figures "
            "disagree by exactly this much on 25 of 69 price pairs, so a zero "
            "tolerance would flag a third of the catalogue."
        ),
    },
    {
        "key": "max_items_per_container",
        "value_json": 3,
        "value_type": "int",
        "category": "pricing",
        "description": "From the workbook: 'Containers to be filled with only three items.'",
    },
    {
        "key": "max_custom_discount_pct",
        "value_json": "25",
        "value_type": "decimal",
        "category": "approval",
        "description": (
            "A custom price more than this far below the standard price for the "
            "same variant triggers the CUSTOM_PRICE_BELOW_FLOOR warning."
        ),
    },
    {
        "key": "expiring_soon_days",
        "value_json": [7, 30],
        "value_type": "list",
        "category": "dashboard",
        "description": "Dashboard buckets for quotations approaching their expiry date.",
    },
    {
        "key": "default_lead_time_text",
        "value_json": "14-16 weeks from final artwork and structural approval",
        "value_type": "string",
        "category": "terms",
        "description": "Default lead time offered when adding the lead-time term.",
    },
    {
        "key": "password_min_length",
        "value_json": 10,
        "value_type": "int",
        "category": "security",
        "description": "Minimum password length enforced on change and reset.",
    },
]


def seed_price_tiers(session: Session) -> dict[str, PriceTier]:
    existing = {t.code: t for t in session.execute(select(PriceTier)).scalars()}
    for code, spec in PRICE_TIER_SEED.items():
        tier = existing.get(code.value)
        if tier is None:
            tier = PriceTier(code=code.value)
            session.add(tier)
            existing[code.value] = tier
        tier.name = spec["name"]
        tier.min_containers = spec["min_containers"]
        tier.requires_approval = spec["requires_approval"]
        tier.sort_order = spec["sort_order"]
        tier.is_active = True
    session.flush()
    log.info("Price tiers seeded: %d", len(existing))
    return existing


def seed_company_settings(session: Session) -> CompanySettings:
    settings = session.get(CompanySettings, 1)
    if settings is not None:
        return settings

    # Placeholder-ness is carried by the ``is_placeholder`` flag, never by text
    # inside a customer-facing field. Anything blank here is simply omitted from
    # the quotation document, so an unconfigured install produces a plain
    # header rather than printing "Address not set" at a customer.
    settings = CompanySettings(
        id=1,
        legal_name="Soneet",
        trading_name="Soneet",
        address_line1="",
        city="",
        country="",
        phone="",
        email="",
        website="",
        tax_number="",
        default_currency="USD",
        default_quote_validity_days=30,
        quote_number_format="QT-{YYYY}-{SEQ:04d}",
        # From the reference workbook's Notes row: "Printing plate charge is
        # 200 USD per size per color." Configurable from day one.
        printing_plate_rate=Decimal("200.00"),
        printing_plate_currency="USD",
        pdf_page_size="A4",
        pdf_footer_text="",
        pdf_confidentiality_text=(
            "This quotation is confidential and intended solely for the named recipient."
        ),
        pdf_thank_you_text="Thank you for your enquiry.",
        # Deliberately unset. It used to be seeded with a verbatim copy of
        # document_model.DEFAULT_COLUMNS, which made that constant dead code:
        # _column_set prefers a stored set, so every seeded deployment was
        # pinned to the column list as it stood on the day it was installed and
        # no later change to the default could ever reach it. Left as NULL, the
        # default applies and stays live; an employee choosing columns in
        # Company Settings still overrides it, which is the only time a stored
        # set means anything.
        pdf_column_set=None,
        pdf_show_acceptance_line=False,
        is_placeholder=True,
    )
    session.add(settings)
    session.flush()
    log.info("Company settings seeded (placeholder)")
    return settings


def seed_app_settings(session: Session) -> None:
    existing = {s.key for s in session.execute(select(AppSetting)).scalars()}
    added = 0
    for spec in APP_SETTING_DEFAULTS:
        if spec["key"] in existing:
            continue  # never overwrite a value an administrator has changed
        session.add(AppSetting(**spec))
        added += 1
    session.flush()
    log.info("App settings seeded: %d new", added)


def run(session: Session) -> None:
    seed_price_tiers(session)
    seed_company_settings(session)
    seed_app_settings(session)
