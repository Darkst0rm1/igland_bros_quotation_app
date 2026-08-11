"""Brand presentation for the portal, and the startup checks that guard it.

Two rules:

* **Identity comes from the database.** Legal name, address, phone, email and
  logo are read from ``CompanySettings`` — the same record the PDF uses — so
  the portal cannot show a customer something the company has not entered.
  Nothing here invents a value.
* **Presentation comes from configuration.** A display name, slogan and three
  colours may be overridden per deployment, so the portal is not wired to one
  brand. Blank means "use the company identity", which is the default.

Igland Bros trades under more than one customer-facing name. Supporting that
today needs no schema change: set the presentation values for the deployment
that serves those quotations. When brand has to vary *per quotation* rather
than per deployment, add a nullable ``quotations.brand_code`` and resolve the
profile from it, falling back to these values — the shape below does not
change, only where the lookup reads from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from modules.config import Settings
from modules.models import CompanySettings

#: Conservative: a CSS custom property value goes into a stylesheet we serve,
#: so it must not be able to close a declaration or open a comment.
_COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

#: Neutral defaults. Deliberately not any company's palette — a deployment that
#: configures nothing gets a professional, unbranded document rather than an
#: invented identity.
DEFAULT_PRIMARY = "#14212b"
DEFAULT_SECONDARY = "#46586a"
DEFAULT_ACCENT = "#1f7a3d"


class PortalConfigError(RuntimeError):
    """Raised at startup when production configuration is unusable."""


@dataclass(frozen=True)
class Brand:
    """Everything the customer page needs to present a brand."""

    name: str = ""
    slogan: str = ""
    legal_footer: str = ""
    primary: str = DEFAULT_PRIMARY
    secondary: str = DEFAULT_SECONDARY
    accent: str = DEFAULT_ACCENT
    has_logo: bool = False

    def css(self) -> str:
        """A stylesheet of custom properties, served from our own origin.

        A ``<style>`` block would need ``style-src 'unsafe-inline'``, which
        would weaken the policy for every page. A real stylesheet keeps the CSP
        at ``'self'``.
        """
        return (
            ":root{\n"
            f"  --brand-primary:{self.primary};\n"
            f"  --brand-secondary:{self.secondary};\n"
            f"  --brand-accent:{self.accent};\n"
            "}\n"
        )


def _safe_colour(value: str, fallback: str) -> str:
    value = (value or "").strip()
    return value if _COLOUR.match(value) else fallback


def resolve_brand(
    settings: Settings, company: CompanySettings | None
) -> Brand:
    """Combine database identity with configured presentation."""
    company_name = ""
    has_logo = False
    if company is not None:
        company_name = (company.trading_name or company.legal_name or "").strip()
        has_logo = bool(company.logo_key)

    legal = (settings.portal_brand_legal_footer or "").strip()
    if not legal and company is not None:
        legal = (company.legal_name or "").strip()

    return Brand(
        name=(settings.portal_brand_name or company_name).strip(),
        slogan=(settings.portal_brand_slogan or "").strip(),
        legal_footer=legal,
        primary=_safe_colour(settings.portal_brand_primary, DEFAULT_PRIMARY),
        secondary=_safe_colour(settings.portal_brand_secondary, DEFAULT_SECONDARY),
        accent=_safe_colour(settings.portal_brand_accent, DEFAULT_ACCENT),
        has_logo=has_logo,
    )


# --------------------------------------------------------------------------- #
# Startup validation
# --------------------------------------------------------------------------- #

def validate_portal_settings(settings: Settings) -> None:
    """Refuse to start a production portal that is misconfigured.

    ``PORTAL_BASE_URL`` is what Origin validation compares against and what
    customer links are built from. In production a missing or plain-HTTP value
    means state-changing requests fall back to matching the Host header, which
    a reverse proxy can be persuaded to forge — so this fails at startup rather
    than serving something weaker than intended.

    In development and tests the value is optional and the Host fallback
    applies, because there is no TLS terminator and no real customer.
    """
    if not settings.is_production:
        return

    raw = (settings.portal_base_url or "").strip()
    if not raw:
        raise PortalConfigError(
            "PORTAL_BASE_URL must be set when APP_ENV=production. It is the "
            "origin customer links point at and the value state-changing "
            "requests are validated against."
        )

    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise PortalConfigError(
            f"PORTAL_BASE_URL must use https in production (got {parsed.scheme or 'no scheme'!r}). "
            "A quotation link is a capability URL and must not travel in clear text."
        )
    if not parsed.netloc:
        raise PortalConfigError(
            f"PORTAL_BASE_URL is malformed: {raw!r}. Expected something like "
            "https://quotes.example.com"
        )
    if parsed.query or parsed.fragment:
        raise PortalConfigError(
            "PORTAL_BASE_URL must be an origin, with no query string or fragment."
        )
