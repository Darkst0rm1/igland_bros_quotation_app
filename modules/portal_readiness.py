"""Whether this deployment is fit to show a quotation to a customer.

``CompanySettings`` ships as flagged placeholders so the application is usable
before anyone has typed the company's details in. That is fine for internal
work and unacceptable the moment a document leaves the building: a customer
must never receive a quotation headed by a placeholder.

This is a gate, not a warning. In production a link cannot be issued until the
mandatory fields are filled. In development the same list is shown, previewing
is allowed, and the page is banded as incomplete — because refusing to preview
would make the software impossible to work on.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.config import Settings, get_settings
from modules.models import CompanySettings, Quotation


@dataclass(frozen=True)
class Requirement:
    """One thing that must be true before a customer sees a quotation."""

    key: str
    label: str
    satisfied: bool
    detail: str = ""
    #: False for items that should be shown but must not block publishing.
    blocking: bool = True


@dataclass(frozen=True)
class Readiness:
    requirements: tuple[Requirement, ...] = field(default_factory=tuple)
    is_production: bool = False

    @property
    def outstanding(self) -> tuple[Requirement, ...]:
        return tuple(r for r in self.requirements if not r.satisfied)

    @property
    def blocking_outstanding(self) -> tuple[Requirement, ...]:
        return tuple(r for r in self.outstanding if r.blocking)

    @property
    def is_complete(self) -> bool:
        return not self.outstanding

    @property
    def may_issue_link(self) -> bool:
        """Production blocks on missing identity; development never does."""
        if not self.is_production:
            return True
        return not self.blocking_outstanding

    @property
    def banner_needed(self) -> bool:
        """Development previews of an incomplete identity must say so."""
        return bool(self.outstanding)


def _text(value: str | None) -> bool:
    return bool((value or "").strip())


def check(
    session: Session,
    quotation: Quotation | None = None,
    settings: Settings | None = None,
) -> Readiness:
    """Assess the company record, the deployment and, optionally, one quotation."""
    settings = settings or get_settings()
    company = session.execute(select(CompanySettings)).scalars().first()

    address_complete = company is not None and all(
        _text(v) for v in (company.address_line1, company.city, company.country)
    )

    items: list[Requirement] = [
        Requirement(
            "legal_name", "Legal company name",
            company is not None and _text(company.legal_name),
            "Printed in the quotation footer and on the customer page.",
        ),
        Requirement(
            "brand_name", "Customer-facing brand name",
            company is not None and _text(company.trading_name or company.legal_name),
            "Shown as the heading on the customer page.",
        ),
        Requirement(
            "address", "Business address",
            address_complete,
            "At minimum street, city and country.",
        ),
        Requirement(
            "phone", "Phone number",
            company is not None and _text(company.phone),
        ),
        Requirement(
            "email", "Sales or support email",
            company is not None and _text(company.email),
            "Where a customer replies with questions.",
        ),
        Requirement(
            "logo", "Company logo",
            company is not None and _text(company.logo_key),
            "Without one the customer page shows the brand name as text.",
        ),
        Requirement(
            "placeholder", "Company details confirmed",
            company is not None and not company.is_placeholder,
            "The seeded record is still flagged as placeholder data.",
        ),
    ]

    # The public URL only matters where there is a real deployment to point at.
    base_url = (settings.portal_base_url or "").strip()
    items.append(
        Requirement(
            "portal_base_url", "Portal base URL",
            satisfied=bool(base_url) and base_url.startswith("https://"),
            detail="PORTAL_BASE_URL must be an https origin in production.",
            blocking=settings.is_production,
        )
    )

    if quotation is not None:
        items.append(
            Requirement(
                "terms", "Quotation terms and conditions",
                any(t.is_customer_visible for t in quotation.terms),
                "This quotation has no customer-visible terms.",
            )
        )
        items.append(
            Requirement(
                "expiry", "Quotation expiry date",
                quotation.valid_until is not None,
                "Without one the customer link falls back to a 30-day default.",
            )
        )
        # There is deliberately no tax requirement here, for the same reason
        # ``quote_send_service`` has no "tax rate not set" gate: ``tax_rate_pct``
        # is NOT NULL with a default of zero, so "never configured" is not a
        # state this schema can represent, and a genuine zero rate is normal on
        # an export sale.
        #
        # This check previously keyed off ``quotation.tax_rate_id``, and blocked
        # every send in every deployment. Nothing assigns that column —
        # ``TaxRate`` rows are never constructed anywhere in the application,
        # and ``revision_service`` only copies the value forward — so the
        # requirement could not be satisfied through any supported path. The
        # gate was removed from ``quote_send_service`` for being unreachable and
        # then survived here, reached through this very function.
        #
        # Tax reaches the customer through ``tax_rate_pct``, which the Create
        # Quotation page sets and the calculation engine, document model,
        # pricing snapshot and revisions all read.

    return Readiness(tuple(items), is_production=settings.is_production)
