"""Turning an outbox row into a message, safely.

Two rules shape this file.

**Template choice is never data.** The mapping from message type to template
lives in :data:`TEMPLATES` as a closed dictionary keyed by an enum. There is no
path built from a string, no ``f"{name}.html"``, and the loader is pinned to one
directory — so no value that arrives from anywhere can select a file.

**Template data is text, not markup.** Autoescaping is on and nothing uses
``|safe``. Everything rendered was typed by a customer or an employee: a
description, a comment, a company name. The one value that is not escaped as
text is the secure URL, which goes into an ``href`` and is built by this
application from configuration, never from input.

The rendered result is a :class:`~modules.email_backend.EmailMessage`, which
validates its own headers — so a subject assembled here still cannot carry a
newline into the wire format.
"""
from __future__ import annotations

import datetime as dt
import logging
import pathlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from modules.constants import (
    INTERNAL_MESSAGES,
    LINK_BEARING_MESSAGES,
    EmailMessageType,
)
from modules.email_backend import EmailMessage

log = logging.getLogger(__name__)

TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates" / "email"

#: Message type -> (html template, text template, subject pattern).
#:
#: A closed mapping keyed by an enum member. Adding a message means editing this
#: dictionary; nothing at runtime can name a template that is not here, and a
#: type with no entry raises rather than falling back to something.
TEMPLATES: dict[EmailMessageType, tuple[str, str, str]] = {
    EmailMessageType.QUOTE_INVITATION: (
        "quote_invitation.html",
        "quote_invitation.txt",
        "Your quotation {quote_number} from {brand_name}",
    ),
    EmailMessageType.QUOTE_REVISED_INVITATION: (
        "quote_revised_invitation.html",
        "quote_revised_invitation.txt",
        "Revised quotation {quote_number} ({revision_label}) from {brand_name}",
    ),
    EmailMessageType.CUSTOMER_APPROVAL_CONFIRMATION: (
        "customer_approval_confirmation.html",
        "customer_approval_confirmation.txt",
        "Confirmation: you accepted quotation {quote_number}",
    ),
    EmailMessageType.CUSTOMER_CHANGES_CONFIRMATION: (
        "customer_changes_confirmation.html",
        "customer_changes_confirmation.txt",
        "We have your change request for quotation {quote_number}",
    ),
    EmailMessageType.INTERNAL_APPROVAL_NOTICE: (
        "internal_approval_notice.html",
        "internal_approval_notice.txt",
        "Accepted: {quote_number} {revision_label} — {customer_company}",
    ),
    EmailMessageType.INTERNAL_CHANGES_NOTICE: (
        "internal_changes_notice.html",
        "internal_changes_notice.txt",
        "Changes requested: {quote_number} {revision_label} — {customer_company}",
    ),
}

#: Shown under the card. Not legal text; just enough that somebody who was
#: forwarded the message knows why they have it.
CUSTOMER_FOOTER_NOTE = (
    "You are receiving this because a quotation was prepared for you. "
    "This link is personal to you — please do not forward it."
)
INTERNAL_FOOTER_NOTE = (
    "Automatic internal notification. It contains no customer access link."
)


class TemplateError(RuntimeError):
    """The message could not be rendered."""


@dataclass(frozen=True)
class BrandSnapshot:
    """How the company presented itself when a message was queued.

    Snapshotted onto the outbox row rather than read at send time: a rebrand
    between queueing and sending must not restyle a message about a quotation
    raised under the old identity.
    """

    name: str = ""
    slogan: str = ""
    address_lines: tuple[str, ...] = ()
    phone: str = ""
    email: str = ""
    legal_footer: str = ""
    primary: str = "#1f4e79"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BrandSnapshot:
        raw = raw or {}
        return cls(
            name=str(raw.get("name", "")),
            slogan=str(raw.get("slogan", "")),
            address_lines=tuple(str(x) for x in raw.get("address_lines", ())),
            phone=str(raw.get("phone", "")),
            email=str(raw.get("email", "")),
            legal_footer=str(raw.get("legal_footer", "")),
            primary=str(raw.get("primary") or "#1f4e79"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slogan": self.slogan,
            "address_lines": list(self.address_lines),
            "phone": self.phone,
            "email": self.email,
            "legal_footer": self.legal_footer,
            "primary": self.primary,
        }


def _environment():  # noqa: ANN202
    """A Jinja environment that can only see the email template directory.

    ``FileSystemLoader`` on one directory, and Jinja refuses names that climb
    out of it. Combined with the closed registry above, there is no route from
    data to an arbitrary file.
    """
    from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(
            enabled_extensions=("html",),
            # Plain-text bodies are not escaped — HTML entities in a text email
            # would be shown literally, as "&amp;". The text templates place
            # values on their own lines and never build markup.
            default_for_string=False,
        ),
        # A missing value is a bug worth failing on, not a silent blank in a
        # message to a customer.
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    return env


_ENV = None


def get_environment():  # noqa: ANN201
    global _ENV
    if _ENV is None:
        _ENV = _environment()
    return _ENV


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render(
    message_type: EmailMessageType,
    *,
    data: dict[str, Any],
    brand: BrandSnapshot,
    recipient_email: str,
    recipient_name: str = "",
    secure_url: str = "",
    reply_to: str = "",
) -> EmailMessage:
    """Render one message. Returns a validated :class:`EmailMessage`.

    ``secure_url`` is required for the two invitations and **refused** for
    everything else: an internal notification or a confirmation that carried a
    capability URL would hand the recipient the ability to act as the customer.
    That is enforced here rather than trusted to each template.
    """
    try:
        html_name, text_name, subject_pattern = TEMPLATES[message_type]
    except KeyError:
        raise TemplateError(f"No template is registered for {message_type}.") from None

    needs_link = message_type in LINK_BEARING_MESSAGES
    if needs_link and not secure_url:
        raise TemplateError(
            f"{message_type} is an invitation and needs a secure link."
        )
    if not needs_link and secure_url:
        # Refuse rather than ignore. A caller passing a link to a message that
        # must not carry one has misunderstood something, and silently dropping
        # it would hide that until somebody read a template.
        raise TemplateError(
            f"{message_type} must never carry a customer access link."
        )

    context = {
        "data": _Readonly(data),
        "brand": brand,
        "secure_url": secure_url,
        "subject": "",
        "preheader": str(data.get("preheader", ""))[:200],
        "footer_note": (
            INTERNAL_FOOTER_NOTE if message_type in INTERNAL_MESSAGES
            else CUSTOMER_FOOTER_NOTE
        ),
    }

    subject = _subject(subject_pattern, data, brand)
    context["subject"] = subject

    env = get_environment()
    try:
        html_body = env.get_template(html_name).render(**context)
        text_body = env.get_template(text_name).render(**context)
    except Exception as exc:  # noqa: BLE001
        # No context in the message: it would quote template data, which for an
        # invitation includes the secure URL.
        raise TemplateError(
            f"The {message_type} message could not be rendered."
        ) from _drop(exc)

    return EmailMessage(
        to_email=recipient_email,
        to_name=recipient_name,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        reply_to=reply_to,
    )


def _subject(pattern: str, data: dict[str, Any], brand: BrandSnapshot) -> str:
    """Fill the subject pattern from a fixed set of fields.

    Not ``pattern.format(**data)``: that would let any key in the template data
    reach the subject line, including one added later for another purpose.
    """
    values = {
        "quote_number": str(data.get("quote_number", "")),
        "revision_label": str(data.get("revision_label", "")),
        "customer_company": str(data.get("customer_company", "")),
        "brand_name": brand.name or "us",
    }
    try:
        return pattern.format(**values)
    except (KeyError, IndexError):
        raise TemplateError("The subject pattern references an unknown field.") from None


def _drop(exc: BaseException) -> None:
    """End the exception chain here.

    A Jinja traceback holds the render context in its frames, and for an
    invitation that context contains the customer's capability URL.
    """
    return None


class _Readonly:
    """Template data, resolving keys and nothing else.

    Deliberately **not** a dict subclass. Jinja resolves ``data.total`` by
    trying ``getattr`` first, and on a dict that succeeds for ``items``,
    ``pop``, ``keys`` and the rest — so a template typo renders
    ``<built-in method items>`` into a customer's email instead of failing.
    ``__getattr__`` cannot fix that, because it is only consulted when normal
    lookup has already failed.

    Wrapping instead of subclassing means there is no attribute to find but the
    data, and anything absent raises — which ``StrictUndefined`` turns into a
    loud error rather than a blank.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", dict(data))

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def __getitem__(self, name: str) -> Any:
        return self._data[name]

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Keys only. The values include the customer's capability URL.
        return f"_Readonly(keys={sorted(self._data)!r})"


# --------------------------------------------------------------------------- #
# Building template data
# --------------------------------------------------------------------------- #

def money(value: Decimal | None, currency: str) -> str:
    """Format for display, at the boundary. Everything above is Decimal."""
    from modules.utilities import format_money

    if value is None:
        return ""
    code = (currency or "").upper()
    return f"{format_money(value, code)} {code}".strip()


def date_display(value: dt.date | dt.datetime | None) -> str:
    return value.strftime("%d %b %Y") if value else ""
