"""Emailing a quotation to a customer: the one action that cannot be undone.

Everything else in the portal is reversible. A link can be revoked, a draft
edited, a PDF regenerated. A message that has reached a customer's inbox cannot
be recalled, and one sent to the wrong address cannot be unsent. So this module
is deliberately the most cautious in the application:

* **Issuing a link never sends anything.** Generate, preview and send are three
  separate acts, because an employee configuring a quotation should be able to
  look at the link and the email without a customer receiving either.
* **Preview creates nothing.** No token, no outbox row, no event. The link in a
  preview is a placeholder, so previewing cannot leak a capability or burn one.
* **Send re-checks everything at the moment it acts**, not at the moment the
  page was drawn. A Streamlit page can sit open for an hour while a quotation is
  superseded, approved, cancelled or revised underneath it.
* **Retry and resend are different operations with different names.** Retry
  pushes the same message at the same recipient with the same link. Resend
  revokes the old link, mints a new one and queues a new message. Conflating
  them is how somebody accidentally invalidates a link a customer is using.

Nothing here talks to a mail server. Sending means committing a delivery
intent; the worker does the rest.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.audit_service import record_audit
from modules.authorization import AuthUser, require
from modules.constants import (
    EMAIL_MESSAGE_DISPLAY_NAMES,
    EMAIL_STATUS_DISPLAY_NAMES,
    AuditAction,
    EmailFailureCategory,
    EmailMessageType,
    EmailOutboxStatus,
    EntityType,
    Perm,
    QuotationStatus,
    QuoteEventType,
)
from modules.models import (
    CustomerContact,
    EmailOutbox,
    Quotation,
    QuoteAccessToken,
    User,
)

log = logging.getLogger(__name__)

#: What a preview shows instead of a link. Deliberately not a real host and
#: not a real token: an employee looking at a preview must not be able to copy
#: something that works, and previewing must never consume a capability.
PLACEHOLDER_LINK = "https://portal.example/quote/public/[secure-link]"

#: Statuses a quotation may be emailed from. Everything else is refused with a
#: reason rather than silently disabled.
SENDABLE_STATUSES = frozenset({
    QuotationStatus.APPROVED,
    QuotationStatus.SENT_TO_CUSTOMER,
    QuotationStatus.REVISION_REQUIRED,
})

#: Addresses that are obviously not a customer. Cheap to check and catches the
#: seeded placeholder, the copied example and the developer's own inbox.
PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "example.invalid",
    "test.com", "test.invalid", "localhost", "invalid", "email.com",
    "yourcompany.com", "company.com", "domain.com", "changeme.com",
})
PLACEHOLDER_LOCALS = frozenset({
    "test", "example", "changeme", "noreply", "no-reply", "donotreply",
    "do-not-reply", "placeholder", "todo", "xxx", "email",
})

#: Top-level domains RFC 2606 reserves so they can never resolve. An address
#: under one of these is guaranteed undeliverable, whatever the name in front —
#: and it is the placeholder that actually turns up in practice, because it is
#: what documentation and seeded data use.
RESERVED_TLDS = frozenset({"invalid", "test", "example", "localhost"})

#: A sweep older than this means the queue may not be moving.
DEFAULT_HEALTH_STALE_SECONDS = 15 * 60


class SendError(RuntimeError):
    """The quotation cannot be sent. The message is safe to show an employee."""


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Blocker:
    """One reason a quotation cannot be emailed, and what to do about it."""

    code: str
    label: str
    detail: str = ""
    #: Whether an employee can fix this themselves, or needs an administrator.
    self_serviceable: bool = True


@dataclass(frozen=True)
class SendEligibility:
    """Whether this quotation may be emailed, and why not."""

    blockers: tuple[Blocker, ...] = field(default_factory=tuple)
    #: Rendered for the employee even when sending is allowed.
    warnings: tuple[Blocker, ...] = field(default_factory=tuple)

    @property
    def may_send(self) -> bool:
        return not self.blockers


def check_eligibility(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    recipient: str = "",
    message_type: EmailMessageType | None = None,
    now: dt.date | None = None,
) -> SendEligibility:
    """Every reason this quotation must not be emailed, gathered at once.

    Returns them all rather than the first, so an employee fixes everything in
    one pass instead of discovering the next problem after solving each one.
    """
    from modules import portal_readiness
    from modules.config import get_settings

    now = now or dt.date.today()
    settings = get_settings()
    blockers: list[Blocker] = []
    warnings: list[Blocker] = []

    if not user.has(Perm.QUOTE_PORTAL_SEND):
        blockers.append(Blocker(
            "permission", "You do not have permission to send quotations",
            "Sending is separate from pricing and from issuing a link.",
            self_serviceable=False,
        ))

    # --- the quotation itself -------------------------------------------- #
    if quotation.deleted_at is not None:
        blockers.append(Blocker("deleted", "This quotation has been deleted"))
    if not quotation.is_current_revision:
        blockers.append(Blocker(
            "superseded", "This is not the current revision",
            "A newer revision exists. Send that one instead.",
        ))
    if quotation.status is QuotationStatus.DRAFT:
        blockers.append(Blocker(
            "draft", "This quotation is still a draft",
            "Submit it for approval before sending it to a customer.",
        ))
    elif quotation.status is QuotationStatus.ACCEPTED:
        blockers.append(Blocker(
            "accepted", "This quotation has already been accepted",
            "Create a new revision to quote anything different. Re-inviting a "
            "customer to a quotation they have already signed would ask them "
            "to accept it twice.",
        ))
    elif quotation.status in {QuotationStatus.CANCELLED, QuotationStatus.LOST}:
        blockers.append(Blocker("closed", "This quotation is closed"))
    elif quotation.status is QuotationStatus.EXPIRED:
        blockers.append(Blocker(
            "expired", "This quotation has expired",
            "Extend the validity date or create a revision.",
        ))
    elif quotation.status not in SENDABLE_STATUSES:
        blockers.append(Blocker(
            "not_approved", "This quotation has not been approved",
            "Internal approval has to happen before a customer sees a price.",
        ))

    if quotation.valid_until is not None and quotation.valid_until < now:
        blockers.append(Blocker(
            "lapsed", "The validity date has passed",
            f"It lapsed on {quotation.valid_until:%d %b %Y}.",
        ))
    if quotation.valid_until is None:
        blockers.append(Blocker(
            "no_validity", "This quotation has no validity date",
            "The customer's link expires with the offer, so it needs one.",
        ))
    if not any(t.is_customer_visible for t in quotation.terms):
        blockers.append(Blocker(
            "no_terms", "This quotation has no customer-visible terms",
            "A customer is asked to accept terms and conditions.",
        ))
    # There is deliberately no "tax rate not set" gate. ``tax_rate_pct`` is
    # NOT NULL with a default of zero, so "never configured" is not a state
    # this schema can represent — and a genuine zero rate is normal on an
    # export sale. A check on it would either be unreachable or would refuse
    # legitimate quotations, so the honest answer is not to pretend.

    # --- company identity -------------------------------------------------- #
    readiness = portal_readiness.check(session, quotation, settings)
    if not readiness.may_issue_link:
        blockers.append(Blocker(
            "readiness", "Company details are incomplete",
            f"{len(readiness.blocking_outstanding)} required item(s) outstanding.",
            self_serviceable=False,
        ))
    elif readiness.banner_needed:
        warnings.append(Blocker(
            "readiness_partial", "Company details are incomplete",
            "The customer's email and page would show gaps.",
            self_serviceable=False,
        ))

    # --- delivery configuration -------------------------------------------- #
    if not settings.email_enabled:
        blockers.append(Blocker(
            "email_disabled", "Email delivery is switched off",
            "Messages would be queued and never sent. An administrator has to "
            "enable delivery first.",
            self_serviceable=False,
        ))
    if not settings.email_from_address:
        blockers.append(Blocker(
            "no_sender", "No sending address is configured",
            self_serviceable=False,
        ))
    if not settings.portal_base_url:
        blockers.append(Blocker(
            "no_base_url", "No portal address is configured",
            "The customer's link would have nowhere to point.",
            self_serviceable=False,
        ))

    # --- the recipient ----------------------------------------------------- #
    candidate = (recipient or quotation.contact_email or "").strip()
    problem = recipient_problem(candidate)
    if problem:
        blockers.append(Blocker("recipient", "The recipient address is not usable", problem))

    # --- what is being sent ------------------------------------------------ #
    if message_type is EmailMessageType.QUOTE_INVITATION and _already_invited(
        session, quotation
    ):
        warnings.append(Blocker(
            "already_invited", "An invitation has already been sent",
            "Sending another will replace the customer's existing link. "
            "Choose 'Revised quotation' if the quotation has changed.",
        ))

    health = worker_health()
    if not health.is_configured:
        warnings.append(Blocker(
            "worker_unknown", "Delivery worker status is unknown",
            "The message will be queued. It will be sent when the worker runs.",
            self_serviceable=False,
        ))
    elif health.is_stale:
        warnings.append(Blocker(
            "worker_stale", "The delivery worker has not run recently",
            "The message will be queued, but delivery may be delayed.",
            self_serviceable=False,
        ))

    return SendEligibility(
        blockers=tuple(blockers), warnings=tuple(warnings),
    )


def _already_invited(session: Session, quotation: Quotation) -> bool:
    return session.execute(
        select(EmailOutbox.id).where(
            EmailOutbox.quotation_id == quotation.id,
            EmailOutbox.revision_no == quotation.revision_no,
            EmailOutbox.message_type.in_((
                EmailMessageType.QUOTE_INVITATION,
                EmailMessageType.QUOTE_REVISED_INVITATION,
            )),
            EmailOutbox.status != EmailOutboxStatus.CANCELLED,
        ).limit(1)
    ).first() is not None


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RecipientOption:
    """One address an employee may choose. Never chosen for them when ambiguous."""

    email: str
    name: str = ""
    title: str = ""
    source: str = "contact"      # "quotation" | "contact"
    is_primary: bool = False

    @property
    def label(self) -> str:
        parts = [self.name or self.email]
        if self.title:
            parts.append(self.title)
        return " · ".join(parts)


def recipient_problem(address: str) -> str:
    """Why this address cannot be used, or an empty string if it can.

    Placeholder addresses are refused, not merely warned about. The seeded
    example address and a copied ``@example.com`` both look like real input, and
    a quotation sent to one is a quotation the customer never received while
    everything on screen says it went.
    """
    from modules.email_backend import InvalidRecipientError, validate_address

    candidate = (address or "").strip()
    if not candidate:
        return "No email address has been set for this customer."

    try:
        validate_address(candidate)
    except InvalidRecipientError:
        if re.search(r"[\r\n\x00]", candidate):
            return "That address contains a line break and was refused."
        return "That is not a valid email address."

    local, _, domain = candidate.rpartition("@")
    if domain.lower().rsplit(".", 1)[-1] in RESERVED_TLDS:
        return (
            f"'{domain}' cannot receive email — .{domain.rsplit('.', 1)[-1]} is a "
            "reserved domain that never resolves."
        )
    if domain.lower() in PLACEHOLDER_DOMAINS:
        return f"'{domain}' is a placeholder domain, not a real customer address."
    if local.lower() in PLACEHOLDER_LOCALS:
        return f"'{local}' looks like a placeholder rather than a person."
    return ""


def recipient_options(session: Session, quotation: Quotation) -> list[RecipientOption]:
    """Every address this quotation could reasonably go to.

    The quotation's own contact comes first, because it is what the employee
    chose when raising it. Customer contacts follow. Duplicates are collapsed on
    the address, so the same person recorded twice appears once.
    """
    options: list[RecipientOption] = []
    seen: set[str] = set()

    if quotation.contact_email:
        address = quotation.contact_email.strip()
        options.append(RecipientOption(
            email=address,
            name=(quotation.contact_name or "").strip(),
            source="quotation",
        ))
        seen.add(address.lower())

    rows = session.execute(
        select(CustomerContact)
        .where(
            CustomerContact.customer_id == quotation.customer_id,
            CustomerContact.is_active.is_(True),
            CustomerContact.email.is_not(None),
        )
        .order_by(CustomerContact.is_primary.desc(), CustomerContact.name)
    ).scalars().all()

    for contact in rows:
        address = (contact.email or "").strip()
        if not address or address.lower() in seen:
            continue
        seen.add(address.lower())
        options.append(RecipientOption(
            email=address, name=contact.name or "", title=contact.title or "",
            source="contact", is_primary=contact.is_primary,
        ))
    return options


def requires_explicit_choice(options: list[RecipientOption]) -> bool:
    """Whether the employee must pick rather than accept a default.

    With more than one candidate there is no safe guess: the primary contact is
    not always the person who asked for the quotation, and sending commercial
    terms to the wrong person at the right company is still a disclosure.
    """
    return len(options) > 1


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EmailPreview:
    """Exactly what would be sent, with the capability replaced by a placeholder."""

    message_type: EmailMessageType
    message_label: str
    subject: str
    html_body: str
    text_body: str
    recipient_email: str
    recipient_name: str
    revision_label: str
    revision_no: int
    total_display: str
    currency: str
    link_expires_on: dt.date | None

    @property
    def link_is_placeholder(self) -> bool:
        return PLACEHOLDER_LINK in self.text_body or PLACEHOLDER_LINK in self.html_body


def preview(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    message_type: EmailMessageType,
    recipient_email: str,
    recipient_name: str = "",
    previous_revision_label: str = "",
    change_summary: str = "",
    link_expires_on: dt.date | None = None,
) -> EmailPreview:
    """Render what would be sent. **Creates nothing.**

    No token is issued, no outbox row is written, no event is recorded and
    nothing is committed. The link is a fixed placeholder, so a preview can
    neither hand an employee a working capability nor consume one that a
    customer is meant to receive.
    """
    require(user, Perm.QUOTE_PORTAL_PREVIEW)

    from modules import email_notifications, email_templates

    brand = email_notifications.brand_snapshot(session)
    data = _preview_data(
        session, quotation,
        message_type=message_type,
        recipient_name=recipient_name,
        previous_revision_label=previous_revision_label,
        change_summary=change_summary,
    )

    message = email_templates.render(
        message_type,
        data=data,
        brand=email_templates.BrandSnapshot.from_dict(brand),
        recipient_email=recipient_email or "preview@example.invalid",
        recipient_name=recipient_name,
        secure_url=PLACEHOLDER_LINK if message_type in _LINK_TYPES else "",
    )

    return EmailPreview(
        message_type=message_type,
        message_label=EMAIL_MESSAGE_DISPLAY_NAMES.get(message_type, "Message"),
        subject=message.subject,
        html_body=message.html_body,
        text_body=message.text_body,
        recipient_email=recipient_email,
        recipient_name=recipient_name,
        revision_label=quotation.revision_label,
        revision_no=quotation.revision_no,
        total_display=data.get("total_display", ""),
        currency=quotation.currency,
        link_expires_on=link_expires_on or quotation.valid_until,
    )


_LINK_TYPES = frozenset({
    EmailMessageType.QUOTE_INVITATION,
    EmailMessageType.QUOTE_REVISED_INVITATION,
})


def _preview_data(
    session: Session,
    quotation: Quotation,
    *,
    message_type: EmailMessageType,
    recipient_name: str,
    previous_revision_label: str,
    change_summary: str,
) -> dict:
    """The same template data the real send builds, so a preview is not a mock."""
    from modules import email_notifications

    revised = message_type is EmailMessageType.QUOTE_REVISED_INVITATION
    return {
        **email_notifications._common(quotation),
        **email_notifications._base_totals(quotation),
        "customer_name": (
            recipient_name or quotation.contact_name
            or quotation.customer_name_snapshot or "Customer"
        ),
        "sales_representative": email_notifications._sales_rep(session, quotation),
        "preheader": (
            f"{quotation.quote_number} has been revised" if revised
            else f"{quotation.quote_number} is ready to review"
        ),
        "previous_revision_label": previous_revision_label,
        "change_summary": (change_summary or "").strip()[:400],
    }


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SendResult:
    """What a send produced. Never the plaintext token."""

    outbox_id: int
    message_type: EmailMessageType
    message_label: str
    recipient_email: str
    revision_no: int
    revision_label: str
    token_id: int
    revoked_token_ids: tuple[int, ...] = field(default_factory=tuple)
    #: True when the intent already existed, so a double click sent nothing new.
    was_already_queued: bool = False

    @property
    def headline(self) -> str:
        return "Quotation email queued" if not self.was_already_queued else (
            "This message was already queued"
        )


def send(
    session: Session,
    user: AuthUser,
    quotation_id: int,
    *,
    message_type: EmailMessageType,
    recipient_email: str,
    recipient_name: str = "",
    previous_revision_label: str = "",
    change_summary: str = "",
    expires_at: dt.datetime | None = None,
    revoke_existing: bool = False,
    expected_revision_no: int | None = None,
) -> SendResult:
    """Issue a link and queue the message carrying it. One transaction.

    Takes an id rather than an object and re-reads it: the caller is a Streamlit
    page whose objects may be minutes old, and everything checked at draw time
    has to be checked again now.

    ``expected_revision_no`` is the revision the employee was *looking at*. If
    the quotation has moved on since the page was drawn, the send is refused
    rather than silently applied to something else.

    Commits nothing itself — the caller's ``session_scope`` does — so the token,
    the outbox row, the event and the audit entry all land together or not at
    all. There is no window in which a usable link exists without a message
    intending to deliver it.
    """
    require(user, Perm.QUOTE_PORTAL_SEND)

    from modules import email_notifications, portal_service

    quotation = session.get(Quotation, quotation_id)
    if quotation is None:
        raise SendError("That quotation no longer exists.")

    if (
        expected_revision_no is not None
        and quotation.revision_no != expected_revision_no
    ):
        raise SendError(
            f"This quotation has moved to {quotation.revision_label} since the "
            "page was opened. Reload and check it before sending."
        )

    recipient = (recipient_email or "").strip()
    problem = recipient_problem(recipient)
    if problem:
        raise SendError(problem)

    eligibility = check_eligibility(
        session, user, quotation, recipient=recipient, message_type=message_type,
    )
    if not eligibility.may_send:
        first = eligibility.blockers[0]
        raise SendError(f"{first.label}. {first.detail}".strip())

    # Emailing a quotation to a customer *is* issuing it. Without this the
    # quotation stays editable after the customer has the link, and a price
    # changed afterwards would show through that same link under the old
    # email — the customer reading one figure and the system holding another.
    # Locking here makes the offer immutable from the moment it leaves.
    if not quotation.is_locked:
        from modules import revision_service

        try:
            revision_service.issue(session, user, quotation, note="Emailed to customer")
        except Exception as exc:  # noqa: BLE001
            raise SendError(
                "This quotation could not be issued, so it was not sent."
            ) from exc

    revoked: list[int] = []
    if revoke_existing:
        # A resend replaces the customer's way in. Revoking first means there is
        # never a moment when two links are live for one quotation.
        #
        # Across the whole revision family, not just this row: the link the
        # customer is actually holding was issued against the *previous*
        # revision, and looking only at this one would revoke nothing while the
        # employee was told their existing link would stop working. A superseded
        # revision does refuse its own links, but relying on that leaves the
        # revocation implicit and unrecorded.
        for token in _family_tokens(session, quotation):
            portal_service.revoke_token(session, user, token)
            revoked.append(token.id)

    token, raw = portal_service.issue_token(
        session, user, quotation, expires_at=expires_at,
    )
    link = portal_service.build_link(raw)

    # The confirmed recipient goes in here, not corrected afterwards: the
    # sealed payload binds to the address, so a row re-pointed later would hold
    # a ciphertext that no longer opens. The customer record is untouched.
    row = email_notifications.queue_invitation(
        session, quotation, link,
        revised=message_type is EmailMessageType.QUOTE_REVISED_INVITATION,
        previous_revision_label=previous_revision_label,
        change_summary=change_summary,
        recipient_email=recipient,
        recipient_name=recipient_name,
        # Bound to the token, so two clicks that each mint a token produce two
        # distinct intents rather than colliding on the idempotency key — and a
        # replayed submission that reuses one token resolves to one intent.
        discriminator=str(token.id),
    )
    if row is None:
        raise SendError("The message could not be queued.")

    # Flushed so the row has an id. Without this the caller is handed
    # ``outbox_id=None`` and cannot show the employee what it just queued —
    # and the failure is silent, because None is a perfectly good value to
    # carry around until something tries to look it up.
    session.flush()

    record_audit(
        session, user, AuditAction.EMAIL_QUEUED,
        EntityType.QUOTATION, quotation.id,
        new_value={
            "message_type": str(message_type),
            "recipient": recipient,
            "revision_no": quotation.revision_no,
            "token_id": token.id,
            "revoked_token_ids": revoked,
        },
    )
    if revoked:
        _record_event(
            session, quotation.id, QuoteEventType.EMAIL_RESENT,
            {"message_type": str(message_type), "replaced_links": len(revoked)},
        )

    log.info(
        "Queued %s for quotation %s rev %s by %s",
        message_type, quotation.quote_number, quotation.revision_no, user.username,
    )
    return SendResult(
        outbox_id=row.id,
        message_type=message_type,
        message_label=EMAIL_MESSAGE_DISPLAY_NAMES.get(message_type, "Message"),
        recipient_email=row.recipient_email,
        revision_no=quotation.revision_no,
        revision_label=quotation.revision_label,
        token_id=token.id,
        revoked_token_ids=tuple(revoked),
    )


def live_link_count(session: Session, quotation: Quotation) -> int:
    """How many links the customer could currently be holding.

    Counted across the revision family, so the warning an employee is shown
    matches what a resend will actually revoke.
    """
    return len(_family_tokens(session, quotation))


def _family_tokens(session: Session, quotation: Quotation) -> list[QuoteAccessToken]:
    """Every live token for this quotation and its sibling revisions."""
    from modules import portal_service, revision_service

    root_id = quotation.root_quotation_id or quotation.id
    try:
        family = revision_service.revisions_for(session, root_id)
    except Exception:  # noqa: BLE001 — a lookup failure must not block a send
        family = []

    seen: set[int] = set()
    tokens: list[QuoteAccessToken] = []
    for sibling in list(family) + [quotation]:
        if sibling.id in seen:
            continue
        seen.add(sibling.id)
        tokens.extend(portal_service.active_tokens(session, sibling.id))
    return tokens


def _record_event(
    session: Session, quotation_id: int, event_type: QuoteEventType, detail: dict
) -> None:
    from modules.models import QuoteEvent

    session.add(
        QuoteEvent(
            quotation_id=quotation_id, event_type=event_type, detail_json=detail,
        )
    )


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class RetryEligibility:
    """Whether a failed message can be pushed back into the queue."""

    may_retry: bool
    reason: str = ""
    #: True when the only route left is a fresh link.
    needs_resend: bool = False


def check_retry(
    session: Session, user: AuthUser, outbox_id: int
) -> RetryEligibility:
    """Whether this exact message can be tried again, unchanged."""
    from modules.constants import LINK_BEARING_MESSAGES

    if not user.has(Perm.QUOTE_PORTAL_RETRY):
        return RetryEligibility(False, "You do not have permission to retry deliveries.")

    row = session.get(EmailOutbox, outbox_id)
    if row is None:
        return RetryEligibility(False, "That message no longer exists.")
    if row.status is EmailOutboxStatus.SENT:
        return RetryEligibility(False, "That message has already been sent.")
    if row.status is EmailOutboxStatus.SENDING:
        return RetryEligibility(False, "That message is being sent right now.")

    if row.message_type in LINK_BEARING_MESSAGES:
        if not row.secure_payload:
            return RetryEligibility(
                False,
                "The secure link for this message is no longer available.",
                needs_resend=True,
            )
        if _expired(row.secure_payload_expires_at):
            return RetryEligibility(
                False,
                "The secure link for this message has expired.",
                needs_resend=True,
            )

        quotation = session.get(Quotation, row.quotation_id)
        if quotation is None or quotation.revision_no != row.revision_no:
            return RetryEligibility(
                False,
                "The quotation has been revised since this message was queued.",
                needs_resend=True,
            )
        if not _has_live_token(session, row.quotation_id):
            return RetryEligibility(
                False,
                "The customer's link has been revoked or has expired.",
                needs_resend=True,
            )

    return RetryEligibility(True)


def _expired(value: dt.datetime | None) -> bool:
    if value is None:
        return False
    aware = value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return aware < dt.datetime.now(dt.UTC)


def _has_live_token(session: Session, quotation_id: int) -> bool:
    from modules import portal_service

    return bool(portal_service.active_tokens(session, quotation_id))


def retry(session: Session, user: AuthUser, outbox_id: int) -> EmailOutbox:
    """Queue the same message again. Same recipient, revision, link and content.

    Everything that identifies the message is left alone — this is the whole
    difference from a resend. Only the delivery bookkeeping is reset.
    """
    require(user, Perm.QUOTE_PORTAL_RETRY)

    from modules import email_outbox_service

    eligibility = check_retry(session, user, outbox_id)
    if not eligibility.may_retry:
        raise SendError(eligibility.reason)

    row = session.get(EmailOutbox, outbox_id)
    before = (
        row.recipient_email, row.revision_no, row.message_type,
        row.secure_payload, row.template_data_json,
    )

    email_outbox_service.reset_failed(session, outbox_id, user=user)
    session.flush()

    after = (
        row.recipient_email, row.revision_no, row.message_type,
        row.secure_payload, row.template_data_json,
    )
    # Not defensive programming for its own sake: a retry that quietly changed
    # the recipient or the link would deliver something nobody approved, and it
    # would look identical in the history.
    if before != after:
        raise SendError("A retry must not change the message. Nothing was queued.")

    _record_event(
        session, row.quotation_id, QuoteEventType.EMAIL_RETRY_SCHEDULED,
        {"message_type": str(row.message_type)},
    )
    log.info(
        "Retry scheduled for outbox row %s by %s", outbox_id, user.username,
    )
    return row


# --------------------------------------------------------------------------- #
# Delivery history
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DeliveryRow:
    """One line of delivery history, as an employee may see it.

    No field carries ciphertext, a key version, a provider diagnostic, a raw
    exception, a storage key or the secure link — a page cannot render what the
    shape does not hold.
    """

    outbox_id: int
    message_label: str
    message_type: EmailMessageType
    recipient_email: str
    revision_label: str
    revision_no: int
    status_label: str
    status: EmailOutboxStatus
    attempts: int
    queued_at: dt.datetime
    last_attempt_at: dt.datetime | None = None
    sent_at: dt.datetime | None = None
    next_attempt_at: dt.datetime | None = None
    failure_summary: str = ""
    queued_by: str = ""
    payload_expired: bool = False

    @property
    def is_failed(self) -> bool:
        return self.status is EmailOutboxStatus.FAILED

    @property
    def is_sent(self) -> bool:
        return self.status is EmailOutboxStatus.SENT


#: Failure categories, translated. The provider's own words are never shown:
#: they quote the recipient and subject back and would then be on screen.
_FAILURE_SUMMARY = {
    EmailFailureCategory.TEMPORARY: "Temporary problem — will try again",
    EmailFailureCategory.PERMANENT: "Could not be delivered",
}


def delivery_history(
    session: Session, user: AuthUser, quotation_id: int
) -> list[DeliveryRow]:
    """Everything queued for this quotation, safe to display."""
    require(user, Perm.QUOTE_PORTAL_VIEW_DELIVERY)

    rows = session.execute(
        select(EmailOutbox)
        .where(EmailOutbox.quotation_id == quotation_id)
        .order_by(EmailOutbox.queued_at.desc(), EmailOutbox.id.desc())
    ).scalars().all()

    actors = _queued_by(session, quotation_id)
    return [_delivery_row(row, actors) for row in rows]


def _delivery_row(row: EmailOutbox, actors: dict[str, str]) -> DeliveryRow:
    from modules.constants import LINK_BEARING_MESSAGES

    payload_expired = (
        row.message_type in LINK_BEARING_MESSAGES
        and row.status is not EmailOutboxStatus.SENT
        and (not row.secure_payload or _expired(row.secure_payload_expires_at))
    )

    status_label = EMAIL_STATUS_DISPLAY_NAMES.get(row.status, "Unknown")
    if payload_expired and row.status is not EmailOutboxStatus.SENT:
        status_label = "Link expired"
    elif row.status is EmailOutboxStatus.QUEUED and row.attempts:
        status_label = "Retry scheduled"
    elif row.status is EmailOutboxStatus.FAILED:
        status_label = "Permanently failed"

    return DeliveryRow(
        outbox_id=row.id,
        message_label=EMAIL_MESSAGE_DISPLAY_NAMES.get(row.message_type, "Message"),
        message_type=row.message_type,
        recipient_email=row.recipient_email,
        revision_label=f"Rev {row.revision_no}",
        revision_no=row.revision_no,
        status_label=status_label,
        status=row.status,
        attempts=row.attempts,
        queued_at=row.queued_at,
        last_attempt_at=row.last_attempt_at,
        sent_at=row.sent_at,
        next_attempt_at=row.next_attempt_at,
        failure_summary=(
            _FAILURE_SUMMARY.get(row.failure_category, "") if row.failure_category
            else ""
        ),
        queued_by=actors.get(_actor_key(row), ""),
        payload_expired=payload_expired,
    )


def _actor_key(row: EmailOutbox) -> str:
    return f"{row.message_type}:{row.revision_no}:{row.recipient_email.lower()}"


def _queued_by(session: Session, quotation_id: int) -> dict[str, str]:
    """Who pressed Send, from the audit trail.

    Read from audit rather than stored on the outbox: the outbox is a delivery
    intent, and who authorised it is an audit question. Customer-triggered
    messages — a confirmation, an internal notice — legitimately have nobody.
    """
    from modules.models import AuditLog

    entries = session.execute(
        select(AuditLog)
        .where(
            AuditLog.entity_type == str(EntityType.QUOTATION),
            AuditLog.entity_id == quotation_id,
            AuditLog.action == str(AuditAction.EMAIL_QUEUED),
        )
        .order_by(AuditLog.id)
    ).scalars().all()

    actors: dict[str, str] = {}
    for entry in entries:
        detail = entry.new_value_json or {}
        key = (
            f"{detail.get('message_type', '')}:{detail.get('revision_no', '')}:"
            f"{str(detail.get('recipient', '')).lower()}"
        )
        if entry.username_snapshot:
            actors[key] = entry.username_snapshot
    return actors


@dataclass(frozen=True)
class DeliverySummary:
    """The compact version, for a page that is not about email.

    Quotation history needs to answer "did this reach the customer" without
    growing a queue console.
    """

    status_label: str = "Not sent"
    recipient_email: str = ""
    at: dt.datetime | None = None
    is_sent: bool = False
    is_failed: bool = False
    total_messages: int = 0

    @property
    def has_activity(self) -> bool:
        return self.total_messages > 0


def delivery_summary(
    session: Session, user: AuthUser, quotation_id: int
) -> DeliverySummary:
    """Latest delivery state only. Safe for a list view."""
    if not user.has(Perm.QUOTE_PORTAL_VIEW_DELIVERY):
        return DeliverySummary()

    rows = delivery_history(session, user, quotation_id)
    customer_facing = [
        r for r in rows
        if r.message_type in {
            EmailMessageType.QUOTE_INVITATION,
            EmailMessageType.QUOTE_REVISED_INVITATION,
        }
    ] or rows
    if not customer_facing:
        return DeliverySummary()

    latest = customer_facing[0]
    return DeliverySummary(
        status_label=latest.status_label,
        recipient_email=latest.recipient_email,
        at=latest.sent_at or latest.queued_at,
        is_sent=latest.is_sent,
        is_failed=latest.is_failed or latest.payload_expired,
        total_messages=len(rows),
    )


# --------------------------------------------------------------------------- #
# Worker health
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class WorkerHealth:
    """Whether the thing that actually delivers messages is running.

    Deliberately carries no path and no worker identity. An employee needs to
    know whether delivery is moving, not where a file lives or which process
    wrote it.
    """

    is_configured: bool = False
    is_healthy: bool = False
    last_sweep_at: dt.datetime | None = None
    degraded: bool = False

    @property
    def is_stale(self) -> bool:
        return self.is_configured and not self.is_healthy

    @property
    def label(self) -> str:
        if not self.is_configured:
            return "Not configured"
        if not self.is_healthy:
            return "Not running recently"
        return "Degraded" if self.degraded else "Running"

    @property
    def detail(self) -> str:
        if not self.is_configured:
            return (
                "Delivery worker monitoring is not set up. Messages will still "
                "be queued."
            )
        if not self.is_healthy:
            return (
                "The delivery worker has not reported recently. Messages will "
                "be queued and sent when it next runs."
            )
        if self.degraded:
            return "The worker is running but reported a problem on its last pass."
        return "The delivery worker is running normally."


def worker_health(stale_after_seconds: int | None = None) -> WorkerHealth:
    """Read the worker's health signal, if one is configured.

    Reads the file the worker touches after every sweep rather than asking the
    worker anything: it has no web surface, and giving it one so a page could
    poll it would be a lot of machinery for a timestamp.
    """
    from modules.worker import HEALTH_FILE_ENV

    path = os.environ.get(HEALTH_FILE_ENV, "").strip()
    if not path:
        return WorkerHealth(is_configured=False)

    threshold = stale_after_seconds or _stale_threshold()
    try:
        from pathlib import Path

        target = Path(path)
        if not target.is_file():
            return WorkerHealth(is_configured=True, is_healthy=False)

        content = target.read_text(encoding="utf-8").strip()
        stamp = dt.datetime.fromtimestamp(target.stat().st_mtime, tz=dt.UTC)
    except Exception:  # noqa: BLE001 — an unreadable signal is an unknown one
        return WorkerHealth(is_configured=True, is_healthy=False)

    age = (dt.datetime.now(dt.UTC) - stamp).total_seconds()
    return WorkerHealth(
        is_configured=True,
        is_healthy=age <= threshold,
        last_sweep_at=stamp,
        degraded="degraded" in content,
    )


def _stale_threshold() -> int:
    from modules.config import get_settings

    settings = get_settings()
    return getattr(
        settings, "worker_stale_after_seconds", DEFAULT_HEALTH_STALE_SECONDS
    )
