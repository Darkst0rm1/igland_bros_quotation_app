"""Typed access to company settings and application tunables.

Three layers, most specific wins (docs/PHASE1_ARCHITECTURE.md §11.2):

``st.secrets`` / ``.env`` → infrastructure and secrets, never here
``company_settings``      → branding and document defaults, one row
``app_settings``          → tunable thresholds, key/value

Callers ask for a named setting and get a correctly typed value with a sensible
fallback. Nothing else in the application should be reading these tables
directly, so that a missing or malformed setting produces one predictable
default rather than a different failure at each call site.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.audit_service import record_audit
from modules.authorization import AuthUser, require
from modules.constants import AuditAction, EntityType, Perm
from modules.models import AppSetting, CompanySettings

log = logging.getLogger(__name__)


class SettingsError(ValueError):
    """A settings change that failed validation. Safe to show the user."""


# --------------------------------------------------------------------------- #
# Company settings
# --------------------------------------------------------------------------- #

def get_company_settings(session: Session) -> CompanySettings | None:
    return session.get(CompanySettings, 1)


def company_name(session: Session) -> str:
    settings = get_company_settings(session)
    if settings is None:
        return "Igland Bros"
    return settings.trading_name or settings.legal_name


def is_placeholder_identity(session: Session) -> bool:
    """True while the seeded placeholder company details are still in place.

    The Company Settings page shows a banner until this clears, and the
    quotation document simply omits fields that are blank rather than printing
    placeholder text at a customer.
    """
    settings = get_company_settings(session)
    return settings is None or settings.is_placeholder


def default_currency(session: Session) -> str:
    settings = get_company_settings(session)
    return settings.default_currency if settings else "USD"


def default_validity_days(session: Session) -> int:
    settings = get_company_settings(session)
    return int(settings.default_quote_validity_days) if settings else 30


def quote_number_format(session: Session) -> str:
    from modules.numbering import DEFAULT_FORMAT

    settings = get_company_settings(session)
    return settings.quote_number_format if settings else DEFAULT_FORMAT


def plate_rate(session: Session) -> Decimal:
    """USD 200 per size per colour, from the reference workbook — configurable."""
    settings = get_company_settings(session)
    return settings.printing_plate_rate if settings else Decimal("200.00")


def plate_currency(session: Session) -> str:
    settings = get_company_settings(session)
    return settings.printing_plate_currency if settings else "USD"


# --------------------------------------------------------------------------- #
# App settings
# --------------------------------------------------------------------------- #

def _raw(session: Session, key: str) -> Any:
    row = session.execute(
        select(AppSetting).where(AppSetting.key == key)
    ).scalar_one_or_none()
    return row.value_json if row else None


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    value = _raw(session, key)
    return default if value is None else value


def get_decimal(session: Session, key: str, default: Decimal) -> Decimal:
    """A tunable that must be exact. A malformed stored value falls back rather
    than propagating a string into the money path."""
    value = _raw(session, key)
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        log.warning("Setting %s is not a number (%r); using %s", key, value, default)
        return default


def get_int(session: Session, key: str, default: int) -> int:
    value = _raw(session, key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("Setting %s is not an integer (%r); using %s", key, value, default)
        return default


def get_str(session: Session, key: str, default: str) -> str:
    value = _raw(session, key)
    return default if value is None else str(value)


def get_list(session: Session, key: str, default: list[Any]) -> list[Any]:
    value = _raw(session, key)
    return list(value) if isinstance(value, list) else default


# --- named accessors, so no caller repeats a key string ------------------- #

def tier_container_scope(session: Session) -> str:
    """``quotation`` or ``line``.

    The brief does not say whether the three/eight-container check counts
    containers across the whole quotation or per line. Commercially the price is
    earned by the order, so the default is ``quotation``; this setting exists so
    the reading can be flipped without a code change.
    """
    scope = get_str(session, "tier_container_scope", "quotation").lower()
    return scope if scope in {"quotation", "line"} else "quotation"


def piece_pack_tolerance(session: Session) -> Decimal:
    """One rounding unit at the workbook's 4 dp piece precision.

    The reference file's own pack and piece columns disagree by exactly this
    much on 25 of 69 price pairs, so a zero tolerance would flag more than a
    third of the catalogue and be ignored within a week.
    """
    return get_decimal(session, "piece_pack_tolerance", Decimal("0.0001"))


def max_items_per_container(session: Session) -> int:
    """From the workbook: "Containers to be filled with only three items"."""
    return get_int(session, "max_items_per_container", 3)


def max_custom_discount_pct(session: Session) -> Decimal:
    """How far below the standard price a custom price may go before it trips
    the CUSTOM_PRICE_BELOW_FLOOR warning."""
    return get_decimal(session, "max_custom_discount_pct", Decimal("25"))


def expiring_soon_days(session: Session) -> list[int]:
    return [int(d) for d in get_list(session, "expiring_soon_days", [7, 30])]


def default_lead_time_text(session: Session) -> str:
    return get_str(
        session,
        "default_lead_time_text",
        "14-16 weeks from final artwork and structural approval",
    )


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def set_setting(
    session: Session,
    user: AuthUser,
    key: str,
    value: Any,
    *,
    value_type: str = "string",
    category: str = "general",
    description: str | None = None,
) -> AppSetting:
    require(user, Perm.SETTINGS_MANAGE)

    row = session.execute(
        select(AppSetting).where(AppSetting.key == key)
    ).scalar_one_or_none()

    old = row.value_json if row else None
    if row is None:
        row = AppSetting(
            key=key, value_json=value, value_type=value_type,
            category=category, description=description,
        )
        session.add(row)
    else:
        row.value_json = value
    row.updated_by_id = user.id
    session.flush()

    record_audit(
        session, user, AuditAction.SETTINGS_CHANGED, EntityType.APP_SETTING, row.id,
        old_value={key: old}, new_value={key: value},
    )
    return row
