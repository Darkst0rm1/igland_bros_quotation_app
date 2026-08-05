"""Seed carriers and container-shipping defaults.

Idempotent. Existing shipping lines are left exactly as they are — the list is
maintained from Company Settings once the application is in use, so re-running
the seed must not undo somebody's edits.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.constants import (
    DEFAULT_CONTAINER_SIZE,
    DEFAULT_CONTAINER_TYPE,
    DEFAULT_INCOTERM,
    DEFAULT_LOADING_METHOD,
    DEFAULT_SHIPPING_LINES,
)
from modules.models import AppSetting, ShippingLine

log = logging.getLogger(__name__)


#: Defaults for a new shipment, all taken from the reference price list:
#: "FOB Çerkezköy (Türkiye) (INCOTERMS 2020)" and "Shipment with 40' HC
#: containers. Floor Loaded."
SHIPPING_SETTING_DEFAULTS: list[dict[str, object]] = [
    {
        "key": "default_incoterm",
        "value_json": DEFAULT_INCOTERM.value,
        "value_type": "string",
        "category": "shipping",
        "description": "Incoterm applied to a new shipment.",
    },
    {
        "key": "default_incoterm_place",
        "value_json": "Çerkezköy, Türkiye",
        "value_type": "string",
        "category": "shipping",
        "description": "Named place that qualifies the Incoterm.",
    },
    {
        "key": "default_origin_country",
        "value_json": "Türkiye",
        "value_type": "string",
        "category": "shipping",
        "description": "Country of origin on a new shipment.",
    },
    {
        "key": "default_port_of_loading",
        "value_json": "",
        "value_type": "string",
        "category": "shipping",
        "description": "Port of loading on a new shipment.",
    },
    {
        "key": "default_container_size",
        "value_json": DEFAULT_CONTAINER_SIZE.value,
        "value_type": "string",
        "category": "shipping",
        "description": "From the price list: shipment with 40' HC containers.",
    },
    {
        "key": "default_container_type",
        "value_json": DEFAULT_CONTAINER_TYPE.value,
        "value_type": "string",
        "category": "shipping",
        "description": "Dry containers for the white-box range.",
    },
    {
        "key": "default_loading_method",
        "value_json": DEFAULT_LOADING_METHOD.value,
        "value_type": "string",
        "category": "shipping",
        "description": "From the price list: floor loaded.",
    },
]


def seed_shipping_lines(session: Session) -> int:
    existing = {
        name.casefold()
        for name in session.execute(select(ShippingLine.name)).scalars()
    }
    added = 0
    for order, name in enumerate(DEFAULT_SHIPPING_LINES, start=1):
        if name.casefold() in existing:
            continue
        session.add(ShippingLine(name=name, sort_order=order * 10, is_active=True))
        added += 1
    session.flush()
    log.info("Shipping lines seeded: %d new", added)
    return added


def seed_shipping_settings(session: Session) -> int:
    present = {s.key for s in session.execute(select(AppSetting)).scalars()}
    added = 0
    for spec in SHIPPING_SETTING_DEFAULTS:
        if spec["key"] in present:
            continue
        session.add(AppSetting(**spec))
        added += 1
    session.flush()
    log.info("Shipping settings seeded: %d new", added)
    return added


def run(session: Session) -> None:
    seed_shipping_lines(session)
    seed_shipping_settings(session)
