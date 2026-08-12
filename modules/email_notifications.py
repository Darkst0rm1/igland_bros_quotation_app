"""Turning a business event into the messages it should produce.

Sits between the domain services and the outbox. :mod:`modules.email_outbox_service`
knows how to queue, retry and send; this module knows *which* messages an
approval or a change request calls for, and assembles the customer-safe data
each one needs.

Kept separate so the outbox has no opinion about quotations and the domain
services have no opinion about templates. It also puts every "what does the
customer see" decision in one file, which is where a reviewer will look for it.

Everything assembled here is drawn from the same customer-safe sources the
portal and the PDF use: :mod:`modules.pricing_snapshot` for money, the company
settings for identity. There is no field on any of these payloads for a cost, a
margin, a supplier or an internal note.
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from modules.constants import (
    STATUS_DISPLAY_NAMES,
    EmailMessageType,
    ItemInclusion,
)
from modules.email_outbox_service import OutboxError, enqueue
from modules.email_templates import BrandSnapshot, date_display, money
from modules.models import CompanySettings, EmailOutbox, PortalResponse, Quotation

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Shared pieces
# --------------------------------------------------------------------------- #

def brand_snapshot(session: Session, settings=None) -> dict:  # noqa: ANN001
    """How the company looks right now, frozen onto the message.

    Identity comes from the database; only presentation can be overridden by
    configuration, which is the same rule the portal follows.
    """
    from modules import settings_service
    from modules.config import get_settings

    settings = settings or get_settings()
    company: CompanySettings | None = settings_service.get_company_settings(session)

    address = []
    if company is not None:
        parts = [
            company.address_line1,
            company.address_line2,
            " ".join(
                p for p in (company.city, company.province, company.postal_code) if p
            ),
            company.country,
        ]
        address = [p.strip() for p in parts if p and p.strip()]

    return BrandSnapshot(
        name=(
            settings.portal_brand_name
            or (company.trading_name or company.legal_name if company else "")
            or ""
        ).strip(),
        slogan=settings.portal_brand_slogan.strip(),
        address_lines=tuple(address),
        phone=(company.phone or "").strip() if company else "",
        email=(company.email or "").strip() if company else "",
        legal_footer=settings.portal_brand_legal_footer.strip(),
        primary=settings.portal_brand_primary.strip() or "#1f4e79",
    ).to_dict()


def _sales_rep(session: Session, quotation: Quotation) -> str:
    from modules.models import User

    if not quotation.sales_user_id:
        return ""
    rep = session.get(User, quotation.sales_user_id)
    return rep.employee_name if rep else ""


def _customer_recipient(quotation: Quotation) -> tuple[str, str]:
    """Who the customer-facing message goes to, and what to call them."""
    return (
        (quotation.contact_email or "").strip(),
        (quotation.contact_name or quotation.customer_name_snapshot or "").strip(),
    )


def _base_totals(quotation: Quotation) -> dict:
    """Figures for an offer: the **base** total, labelled as such.

    An invitation quotes the minimum offer. Using the all-options figure would
    quote the customer a price for things they have not chosen, and using an
    unlabelled "total" is exactly the ambiguity the three scopes exist to
    prevent.
    """
    from modules import pricing_snapshot

    snapshot = pricing_snapshot.base(quotation)
    has_optional = any(
        item.inclusion is not ItemInclusion.INCLUDED for item in quotation.items
    )
    return {
        "total_label": "Quotation total" if not has_optional else "Total as quoted",
        "total_display": money(snapshot.grand_total, snapshot.currency),
        "has_optional_items": has_optional,
    }


def _accepted_totals(response: PortalResponse, quotation: Quotation) -> dict:
    """Figures for a confirmation: the **accepted** total, read off the response.

    Never repriced. A later revision must not restate what somebody agreed to,
    and a confirmation email is the customer's copy of that agreement.
    """
    from modules import pricing_snapshot

    snapshot = pricing_snapshot.selected(
        quotation, list(response.selected_item_ids or [])
    )
    return {
        "total_label": "Accepted total",
        "total_display": money(response.grand_total, response.currency),
        "deposit_display": (
            money(snapshot.deposit_due, response.currency)
            if snapshot.deposit_due else ""
        ),
    }


def _selected_item_labels(quotation: Quotation, response: PortalResponse) -> list[str]:
    """Descriptions of the optional lines taken. Customer-facing text only."""
    chosen = set(response.selected_item_ids or [])
    return [
        (
            item.description_override
            or item.custom_description
            or (item.variant.product.name if item.variant and item.variant.product else "")
            or f"Line {item.line_no}"
        )
        for item in sorted(quotation.items, key=lambda i: (i.sort_order, i.line_no))
        if item.id in chosen
    ]


def _common(quotation: Quotation) -> dict:
    return {
        "quote_number": quotation.quote_number,
        "revision_label": quotation.revision_label,
        "project_name": quotation.project_name or "",
        "valid_until_display": date_display(quotation.valid_until),
        "customer_company": quotation.customer_name_snapshot or "",
    }


# --------------------------------------------------------------------------- #
# Customer messages
# --------------------------------------------------------------------------- #

def queue_invitation(
    session: Session,
    quotation: Quotation,
    secure_url: str,
    *,
    revised: bool = False,
    previous_revision_label: str = "",
    change_summary: str = "",
    discriminator: str = "",
) -> EmailOutbox | None:
    """Queue the message that carries the customer's link.

    Called at the moment a link is issued, because that is the only moment the
    plaintext exists. It is sealed immediately and the row is the only thing
    that survives the call.
    """
    recipient, name = _customer_recipient(quotation)
    if not recipient:
        raise OutboxError(
            "This quotation has no customer contact email, so an invitation "
            "cannot be sent."
        )

    message_type = (
        EmailMessageType.QUOTE_REVISED_INVITATION if revised
        else EmailMessageType.QUOTE_INVITATION
    )
    brand = brand_snapshot(session)
    data = {
        **_common(quotation),
        **_base_totals(quotation),
        "customer_name": name or "Customer",
        "sales_representative": _sales_rep(session, quotation),
        "preheader": (
            f"{quotation.quote_number} is ready to review"
            if not revised else f"{quotation.quote_number} has been revised"
        ),
        "previous_revision_label": previous_revision_label,
        "change_summary": (change_summary or "").strip()[:400],
    }

    return enqueue(
        session,
        message_type=message_type,
        quotation=quotation,
        recipient_email=recipient,
        recipient_name=name,
        subject=_subject_for(message_type, data, brand),
        brand=brand,
        template_data=data,
        secure_url=secure_url,
        discriminator=discriminator,
    )


def queue_approval_confirmation(
    session: Session, quotation: Quotation, response: PortalResponse
) -> EmailOutbox | None:
    """The customer's own copy of what they accepted.

    Returns ``None`` when the customer gave no address — an acceptance without
    an email is still a valid acceptance, and refusing it because we cannot send
    a receipt would be the tail wagging the dog.
    """
    recipient = (response.customer_email or quotation.contact_email or "").strip()
    if not recipient:
        log.info("No customer address for an acceptance confirmation; skipping")
        return None

    brand = brand_snapshot(session)
    data = {
        **_common(quotation),
        **_accepted_totals(response, quotation),
        "customer_name": response.customer_name or "Customer",
        "accepted_at_display": date_display(response.submitted_at),
        "selected_items": _selected_item_labels(quotation, response),
        "sales_representative": _sales_rep(session, quotation),
        "preheader": f"Your acceptance of {quotation.quote_number} is confirmed",
    }
    message_type = EmailMessageType.CUSTOMER_APPROVAL_CONFIRMATION
    return enqueue(
        session,
        message_type=message_type,
        quotation=quotation,
        recipient_email=recipient,
        recipient_name=response.customer_name or "",
        subject=_subject_for(message_type, data, brand),
        brand=brand,
        template_data=data,
        portal_response=response,
    )


def queue_changes_confirmation(
    session: Session, quotation: Quotation, response: PortalResponse
) -> EmailOutbox | None:
    """Acknowledge a change request to the customer who made it."""
    recipient = (response.customer_email or quotation.contact_email or "").strip()
    if not recipient:
        log.info("No customer address for a change-request acknowledgement; skipping")
        return None

    brand = brand_snapshot(session)
    data = {
        **_common(quotation),
        **_base_totals(quotation),
        "customer_name": response.customer_name or "Customer",
        "comment": (response.comment or "").strip()[:2000],
        "sales_representative": _sales_rep(session, quotation),
        "preheader": f"We have your change request for {quotation.quote_number}",
    }
    message_type = EmailMessageType.CUSTOMER_CHANGES_CONFIRMATION
    return enqueue(
        session,
        message_type=message_type,
        quotation=quotation,
        recipient_email=recipient,
        recipient_name=response.customer_name or "",
        subject=_subject_for(message_type, data, brand),
        brand=brand,
        template_data=data,
        portal_response=response,
    )


# --------------------------------------------------------------------------- #
# Internal messages
# --------------------------------------------------------------------------- #

def queue_internal_notice(
    session: Session,
    quotation: Quotation,
    response: PortalResponse,
    *,
    approved: bool,
) -> list[EmailOutbox]:
    """Tell the team. One row per recipient, so one bad address is one failure.

    **Never carries the customer's link.** An internal list is forwarded,
    archived and read on phones, and anyone holding a capability URL can act as
    the customer. Employees reach the quotation through the application, which
    checks who they are. :func:`modules.email_templates.render` refuses a link
    on these types, so this is enforced twice.
    """
    from modules.config import get_settings

    recipients = get_settings().internal_recipients
    if not recipients:
        return []

    brand = brand_snapshot(session)
    rep = _sales_rep(session, quotation)

    rows: list[tuple[str, str]] = [
        ("Customer", quotation.customer_name_snapshot or "—"),
        ("Quotation", f"{quotation.quote_number} {quotation.revision_label}"),
        ("Status", STATUS_DISPLAY_NAMES.get(quotation.status, str(quotation.status))),
        ("Responded by", response.customer_name or "—"),
    ]
    if response.job_title:
        rows.append(("Job title", response.job_title))
    if response.customer_email:
        rows.append(("Email", response.customer_email))
    if approved:
        rows.append((
            "Accepted total", money(response.grand_total, response.currency),
        ))
        if response.signature_name:
            rows.append(("Signed", response.signature_name))
        taken = _selected_item_labels(quotation, response)
        rows.append((
            "Optional items", ", ".join(taken) if taken else "none",
        ))
    rows.append(("Received", date_display(response.submitted_at)))
    if rep:
        rows.append(("Representative", rep))

    message_type = (
        EmailMessageType.INTERNAL_APPROVAL_NOTICE if approved
        else EmailMessageType.INTERNAL_CHANGES_NOTICE
    )
    data = {
        **_common(quotation),
        "rows": rows,
        "comment": (response.comment or "").strip()[:2000],
        "preheader": (
            f"{quotation.quote_number} accepted" if approved
            else f"{quotation.quote_number}: changes requested"
        ),
    }

    queued: list[EmailOutbox] = []
    for address in recipients:
        row = enqueue(
            session,
            message_type=message_type,
            quotation=quotation,
            recipient_email=address,
            subject=_subject_for(message_type, data, brand),
            brand=brand,
            template_data=data,
            portal_response=response,
            # Without this every internal recipient computes the same key and
            # only the first is queued.
            discriminator=address.lower(),
        )
        if row is not None:
            queued.append(row)
    return queued


def _subject_for(message_type: EmailMessageType, data: dict, brand: dict) -> str:
    """Render the subject at enqueue so an employee can see what was sent."""
    from modules.email_templates import TEMPLATES, BrandSnapshot

    _html, _text, pattern = TEMPLATES[message_type]
    return pattern.format(
        quote_number=data.get("quote_number", ""),
        revision_label=data.get("revision_label", ""),
        customer_company=data.get("customer_company", ""),
        brand_name=BrandSnapshot.from_dict(brand).name or "us",
    )[:200]


# --------------------------------------------------------------------------- #
# What a business event asks for
# --------------------------------------------------------------------------- #

def on_customer_approved(
    session: Session, quotation: Quotation, response: PortalResponse
) -> None:
    """Queue everything an acceptance calls for. Inside the transaction.

    Both messages are queued or neither is, along with the acceptance itself.
    A failure here raises, which rolls the acceptance back — deliberately: if a
    notification this important cannot even be recorded, something is wrong
    enough that the event should not stand.
    """
    queue_approval_confirmation(session, quotation, response)
    queue_internal_notice(session, quotation, response, approved=True)


def on_customer_requested_changes(
    session: Session, quotation: Quotation, response: PortalResponse
) -> None:
    """Queue everything a change request calls for. Inside the transaction."""
    queue_changes_confirmation(session, quotation, response)
    queue_internal_notice(session, quotation, response, approved=False)
