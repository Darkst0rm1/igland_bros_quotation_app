"""The outbox: queue a message inside a transaction, send it outside one.

The rule the whole design exists for: **external sending never happens inside
the business transaction.** An SMTP call inside the approval transaction would
either hold a database transaction open across a network round trip to somebody
else's server, or send a customer a confirmation for an approval that then
rolled back. Neither is acceptable, and no amount of care at the call site fixes
it — the two systems simply cannot commit together.

So the sequence is: write the business event and the delivery intent in one
transaction, commit, return, and let a worker send afterwards. If the worker
fails, the acceptance is still an acceptance.

The direction of the guarantee is worth stating plainly, because it is not
symmetric:

* **Before commit**, a failure to *enqueue* rolls the business event back. A
  required notification that cannot even be recorded means something is wrong
  enough that the event should not stand.
* **After commit**, a failure to *deliver* changes nothing about the event. The
  quotation is accepted whether or not the email arrived.

Delivery is at-least-once, honestly. The idempotency key stops the same
notification being queued twice, and a leased row is not picked up by a second
worker. What cannot be eliminated is the window between a provider accepting a
message and this database recording that it did: a process killed in between
leaves a row that will be retried, and the customer gets two copies. See
:func:`process_one` for what narrows it and why it cannot be closed with SMTP.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
import secrets
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from modules.constants import (
    INTERNAL_MESSAGES,
    LINK_BEARING_MESSAGES,
    EMAIL_MESSAGE_DISPLAY_NAMES,
    EMAIL_STATUS_DISPLAY_NAMES,
    AuditAction,
    EmailFailureCategory,
    EmailMessageType,
    EmailOutboxStatus,
    EntityType,
    QuoteEventType,
)
from modules.email_backend import (
    EmailBackend,
    EmailDeliveryError,
    get_backend,
)
from modules.models import EmailOutbox, PortalResponse, Quotation

log = logging.getLogger(__name__)

#: Backoff schedule, in seconds, indexed by attempt. Bounded at the top so a
#: message never disappears into an hours-long wait, and jittered so a provider
#: coming back up is not hit by every queued message at the same instant.
BACKOFF_SECONDS = (60, 300, 900, 1800, 3600)
MAX_BACKOFF_SECONDS = 3600
#: Up to this fraction of the delay is added at random.
JITTER_RATIO = 0.25

#: What the sealed invitation payload is bound to, alongside quotation,
#: revision and recipient. A payload sealed to send an invitation cannot be
#: opened as anything else.
SEAL_PURPOSE = "email-invitation"


class OutboxError(RuntimeError):
    """A message could not be queued. Raised before commit, so it rolls back."""


@dataclass(frozen=True)
class OutboxEntry:
    """What an employee may be shown about a queued message.

    Deliberately narrow. ``failure_code`` is a short token like ``smtp_451``,
    never the provider's own text, which quotes the subject and recipient back
    and would then be displayed. There is no field for the sealed payload, the
    template data or the rendered body — a page cannot render what the shape
    does not carry.
    """

    id: int
    message_type: EmailMessageType
    message_label: str
    status: EmailOutboxStatus
    status_label: str
    recipient_email: str
    subject: str
    revision_no: int
    attempts: int
    queued_at: dt.datetime
    sent_at: dt.datetime | None = None
    last_attempt_at: dt.datetime | None = None
    next_attempt_at: dt.datetime | None = None
    failure_category: EmailFailureCategory | None = None
    failure_code: str | None = None

    @property
    def is_retry_scheduled(self) -> bool:
        return (
            self.status is EmailOutboxStatus.QUEUED
            and self.attempts > 0
            and self.next_attempt_at is not None
        )

    @property
    def is_permanently_failed(self) -> bool:
        return self.status is EmailOutboxStatus.FAILED


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #

def idempotency_key(
    message_type: EmailMessageType,
    *,
    quotation_id: int,
    revision_no: int,
    recipient: str,
    discriminator: str = "",
) -> str:
    """A stable name for "this exact notification".

    Two enqueues that agree on all of these are the same message, and the
    unique index turns the second into a no-op rather than a second email to a
    customer. ``discriminator`` separates messages that would otherwise collide
    — an internal notice going to three addresses, or a resend the employee
    deliberately asked for.

    Hashed rather than concatenated so a long recipient or a strange quote
    number cannot overflow the column or smuggle a separator.
    """
    raw = "|".join((
        str(message_type),
        str(int(quotation_id)),
        str(int(revision_no)),
        (recipient or "").strip().lower(),
        discriminator or "",
    ))
    return f"{message_type.value[:24]}-{hashlib.sha256(raw.encode()).hexdigest()[:40]}"


# --------------------------------------------------------------------------- #
# Enqueueing
# --------------------------------------------------------------------------- #

def enqueue(
    session: Session,
    *,
    message_type: EmailMessageType,
    quotation: Quotation,
    recipient_email: str,
    recipient_name: str = "",
    subject: str = "",
    brand: dict | None = None,
    template_data: dict | None = None,
    portal_response: PortalResponse | None = None,
    secure_url: str | None = None,
    discriminator: str = "",
    now: dt.datetime | None = None,
) -> EmailOutbox | None:
    """Record the intent to send. Call inside the business transaction.

    Returns the row, or the existing one when this message was already queued.
    Raises :class:`OutboxError` for anything that makes the message
    unsendable — an invalid address, a missing link on an invitation — so a
    caller inside a transaction rolls the business event back rather than
    committing an event whose required notification can never go out.
    """
    from modules.email_backend import InvalidRecipientError, validate_address

    now = now or dt.datetime.now(dt.UTC)

    try:
        recipient = validate_address(recipient_email)
    except InvalidRecipientError:
        raise OutboxError(
            "That recipient address is not valid, so the message cannot be queued."
        ) from None

    needs_link = message_type in LINK_BEARING_MESSAGES
    if needs_link and not secure_url:
        raise OutboxError(f"{message_type} needs a customer link to be queued.")
    if not needs_link and secure_url:
        raise OutboxError(f"{message_type} must never carry a customer link.")

    key = idempotency_key(
        message_type,
        quotation_id=quotation.id,
        revision_no=quotation.revision_no,
        recipient=recipient,
        discriminator=discriminator,
    )

    existing = session.execute(
        select(EmailOutbox).where(EmailOutbox.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        log.info("Message already queued for this quotation and recipient; reusing it")
        return existing

    # A row added but not yet flushed is invisible to the SELECT above, so the
    # pending set has to be checked too — otherwise two enqueues in one
    # transaction both insert and the unique index fails the whole commit.
    pending = next(
        (
            obj for obj in session.new
            if isinstance(obj, EmailOutbox) and obj.idempotency_key == key
        ),
        None,
    )
    if pending is not None:
        return pending

    sealed, expires_at = _seal_link(
        secure_url, quotation=quotation, recipient=recipient, now=now,
    )

    row = EmailOutbox(
        message_type=message_type,
        quotation_id=quotation.id,
        revision_no=quotation.revision_no,
        portal_response_id=portal_response.id if portal_response is not None else None,
        recipient_email=recipient,
        recipient_name=(recipient_name or "").strip()[:160] or None,
        subject=(subject or "")[:200],
        brand_snapshot_json=brand or {},
        template_data_json=template_data or {},
        idempotency_key=key,
        status=EmailOutboxStatus.QUEUED,
        next_attempt_at=now,
        queued_at=now,
        secure_payload=sealed,
        secure_payload_expires_at=expires_at,
    )
    session.add(row)

    _record_event(
        session, quotation.id, QuoteEventType.EMAIL_QUEUED,
        detail={"message_type": str(message_type)},
    )
    return row


def _seal_link(
    secure_url: str | None,
    *,
    quotation: Quotation,
    recipient: str,
    now: dt.datetime,
) -> tuple[str | None, dt.datetime | None]:
    """Encrypt the capability URL, or return nothing to store.

    The portal stores only a hash of each token, so nothing else in the database
    can reproduce a working link. This keeps that true: the ciphertext is
    useless without a key held in the environment, and it is bound to this
    quotation, revision, recipient and purpose so it cannot be replayed into
    another row.
    """
    if not secure_url:
        return None, None

    from modules.config import get_settings
    from modules.secret_box import SecretBoxError, binding, seal

    try:
        payload = seal(
            secure_url,
            aad=binding(
                quotation_id=quotation.id,
                revision_no=quotation.revision_no,
                recipient=recipient,
                purpose=SEAL_PURPOSE,
            ),
        )
    except SecretBoxError as exc:
        # Before commit, so the business transaction rolls back. Queueing an
        # invitation whose link can never be recovered would leave a customer
        # waiting for an email that can never be sent.
        raise OutboxError(
            "The customer link could not be secured, so the message was not queued."
        ) from _drop(exc)

    ttl = dt.timedelta(hours=get_settings().email_payload_ttl_hours)
    return payload, now + ttl


def _drop(exc: BaseException) -> None:
    """End the chain. A traceback here holds the plaintext URL in its frames."""
    return None


def _record_event(
    session: Session,
    quotation_id: int,
    event_type: QuoteEventType,
    detail: dict | None = None,
) -> None:
    """Note something on the quotation's timeline.

    Reuses the portal's event table so email and link activity read as one
    history. **Never a view**: an email leaving the building says nothing about
    whether the customer opened anything, and counting it as a view would make
    "first viewed" meaningless.
    """
    from modules.models import QuoteEvent

    session.add(
        QuoteEvent(
            quotation_id=quotation_id, event_type=event_type, detail_json=detail,
        )
    )


# --------------------------------------------------------------------------- #
# Claiming
# --------------------------------------------------------------------------- #

def _due_statement(now: dt.datetime, limit: int):  # noqa: ANN202
    return (
        select(EmailOutbox)
        .where(
            EmailOutbox.status == EmailOutboxStatus.QUEUED,
            (EmailOutbox.next_attempt_at.is_(None))
            | (EmailOutbox.next_attempt_at <= now),
        )
        .order_by(EmailOutbox.next_attempt_at, EmailOutbox.id)
        .limit(limit)
    )


def claim_batch(
    session: Session,
    *,
    owner: str,
    limit: int = 20,
    lease_seconds: int = 300,
    now: dt.datetime | None = None,
) -> list[EmailOutbox]:
    """Take a batch of due messages, marking them SENDING.

    On PostgreSQL the select takes a row lock and skips what another worker
    holds, so two workers never claim the same row. SQLite has no such clause
    and does not need one — it serialises writers, and the test suite runs a
    single worker at a time — so the same query runs without it and the
    compare-and-swap below is what decides.

    A row whose lease has expired is claimable again: a worker killed
    mid-send must not strand a message forever.
    """
    now = now or dt.datetime.now(dt.UTC)
    statement = _due_statement(now, limit)

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)

    candidates = list(session.execute(statement).scalars().all())
    candidates += _expired_leases(session, now, limit)

    claimed: list[EmailOutbox] = []
    for row in candidates[:limit]:
        if _claim(session, row, owner=owner, lease_seconds=lease_seconds, now=now):
            claimed.append(row)
    return claimed


def _expired_leases(
    session: Session, now: dt.datetime, limit: int
) -> list[EmailOutbox]:
    """Rows a worker claimed and never finished."""
    return list(
        session.execute(
            select(EmailOutbox)
            .where(
                EmailOutbox.status == EmailOutboxStatus.SENDING,
                EmailOutbox.lease_expires_at.is_not(None),
                EmailOutbox.lease_expires_at < now,
            )
            .order_by(EmailOutbox.id)
            .limit(limit)
        ).scalars().all()
    )


def _claim(
    session: Session,
    row: EmailOutbox,
    *,
    owner: str,
    lease_seconds: int,
    now: dt.datetime,
) -> bool:
    """Compare-and-swap a row into SENDING. False if somebody else got it.

    A read-then-write would let two workers both see a free row. The UPDATE
    naming the state it expects can only succeed for one of them.
    """
    expires = now + dt.timedelta(seconds=lease_seconds)
    result = session.execute(
        text(
            """
            UPDATE email_outbox
               SET status = 'SENDING', lease_owner = :owner, lease_expires_at = :expires
             WHERE id = :row_id
               AND (
                     status = 'QUEUED'
                  OR (status = 'SENDING' AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < :now)
               )
            """
        ),
        {"owner": owner, "expires": expires, "row_id": row.id, "now": now},
    )
    if not result.rowcount:
        return False
    session.expire(row)
    return True


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

def process_one(
    session: Session,
    row: EmailOutbox,
    *,
    backend: EmailBackend | None = None,
    now: dt.datetime | None = None,
) -> EmailOutboxStatus:
    """Render and send one claimed message.

    **The at-least-once boundary, stated honestly.** The provider is asked to
    accept the message, and only then is the row marked SENT. A process killed
    between those two points leaves a row that a later sweep retries, and the
    recipient receives two copies. Nothing in SMTP closes that window: there is
    no way to ask a server "did you already take this?".

    What narrows it: the row is leased, so only one worker is sending at a time;
    the message carries a Message-ID we generate, which well-behaved servers use
    to collapse duplicates; and the idempotency key stops the same notification
    being *queued* twice, which is the far more common cause of duplicates. A
    provider with an idempotency header would close the rest, and the backend
    interface has room for one — but SMTP does not offer it, so the residual
    duplicate on a mid-send crash is real and documented rather than hidden.
    """
    now = now or dt.datetime.now(dt.UTC)
    backend = backend or get_backend()

    row.attempts += 1
    row.last_attempt_at = now

    try:
        message = _build_message(session, row)
    except Exception as exc:  # noqa: BLE001
        # Rendering failures are permanent: the same data will fail the same
        # way next time, and retrying it five times helps nobody.
        return _fail(
            session, row,
            category=EmailFailureCategory.PERMANENT,
            code=getattr(exc, "code", "render_failed"),
            now=now,
        )

    try:
        result = backend.send(message)
    except EmailDeliveryError as exc:
        return _fail(
            session, row,
            category=(
                EmailFailureCategory.TEMPORARY if exc.temporary
                else EmailFailureCategory.PERMANENT
            ),
            code=exc.code,
            now=now,
        )
    except Exception:  # noqa: BLE001 — an unexpected backend fault is temporary
        log.exception("The email backend raised an unexpected error")
        return _fail(
            session, row,
            category=EmailFailureCategory.TEMPORARY, code="backend_error", now=now,
        )

    row.status = EmailOutboxStatus.SENT
    row.sent_at = now
    row.failure_category = None
    row.failure_code = None
    row.next_attempt_at = None
    row.lease_owner = None
    row.lease_expires_at = None
    row.provider_message_id = (result.message_id or "")[:255] or None
    # The link has been delivered; the ciphertext has no further purpose. Erased
    # rather than left to expire, so the window in which it exists at all is the
    # shortest it can be.
    _erase_payload(row)

    _record_event(
        session, row.quotation_id, QuoteEventType.EMAIL_SENT,
        detail={"message_type": str(row.message_type)},
    )
    log.info(
        "Sent %s for quotation %s (attempt %s)",
        row.message_type, row.quotation_id, row.attempts,
    )
    return row.status


def _fail(
    session: Session,
    row: EmailOutbox,
    *,
    category: EmailFailureCategory,
    code: str,
    now: dt.datetime,
) -> EmailOutboxStatus:
    """Record a failure and decide whether it will be tried again."""
    from modules.config import get_settings

    max_attempts = get_settings().email_max_attempts
    row.failure_category = category
    row.failure_code = (code or "unknown")[:60]
    row.lease_owner = None
    row.lease_expires_at = None

    give_up = (
        category is EmailFailureCategory.PERMANENT or row.attempts >= max_attempts
    )
    if give_up:
        row.status = EmailOutboxStatus.FAILED
        row.failed_at = now
        row.next_attempt_at = None
        _record_event(
            session, row.quotation_id, QuoteEventType.EMAIL_FAILED,
            detail={"message_type": str(row.message_type), "code": row.failure_code},
        )
        log.warning(
            "Giving up on %s for quotation %s after %s attempt(s): %s",
            row.message_type, row.quotation_id, row.attempts, row.failure_code,
        )
    else:
        row.status = EmailOutboxStatus.QUEUED
        row.next_attempt_at = now + dt.timedelta(seconds=backoff_seconds(row.attempts))
        log.info(
            "Will retry %s for quotation %s after attempt %s",
            row.message_type, row.quotation_id, row.attempts,
        )
    return row.status


def backoff_seconds(attempt: int, *, jitter: bool = True) -> int:
    """Delay before attempt ``attempt + 1``, bounded and jittered.

    Bounded because an unbounded exponential eventually schedules a retry days
    out, by which time a quotation may have expired. Jittered because otherwise
    every message queued during an outage retries in the same second when the
    provider returns, and the thundering herd puts it back down.
    """
    index = max(0, min(attempt - 1, len(BACKOFF_SECONDS) - 1))
    base = min(BACKOFF_SECONDS[index], MAX_BACKOFF_SECONDS)
    if not jitter:
        return base
    return int(base + random.uniform(0, base * JITTER_RATIO))


def _erase_payload(row: EmailOutbox) -> None:
    """Drop the sealed link. Cryptographic erasure: the ciphertext is gone."""
    row.secure_payload = None
    row.secure_payload_expires_at = None


def _build_message(session: Session, row: EmailOutbox):  # noqa: ANN202
    """Render the row into a message, opening the sealed link if it has one."""
    from modules import email_templates
    from modules.config import get_settings
    from modules.secret_box import SecretBoxError, binding, open_sealed

    secure_url = ""
    if row.message_type in LINK_BEARING_MESSAGES:
        if not row.secure_payload:
            raise EmailDeliveryError(
                "The secure link for this invitation is no longer available.",
                temporary=False, code="link_unavailable",
            )
        if (
            row.secure_payload_expires_at is not None
            and _aware(row.secure_payload_expires_at) < dt.datetime.now(dt.UTC)
        ):
            _erase_payload(row)
            raise EmailDeliveryError(
                "The secure link for this invitation has expired.",
                temporary=False, code="link_expired",
            )
        try:
            secure_url = open_sealed(
                row.secure_payload,
                aad=binding(
                    quotation_id=row.quotation_id,
                    revision_no=row.revision_no,
                    recipient=row.recipient_email,
                    purpose=SEAL_PURPOSE,
                ),
            )
        except SecretBoxError as exc:
            raise EmailDeliveryError(
                "The secure link could not be opened.",
                temporary=False, code="link_unsealable",
            ) from _drop(exc)

    settings = get_settings()
    return email_templates.render(
        row.message_type,
        data=dict(row.template_data_json or {}),
        brand=email_templates.BrandSnapshot.from_dict(row.brand_snapshot_json),
        recipient_email=row.recipient_email,
        recipient_name=row.recipient_name or "",
        secure_url=secure_url,
        reply_to=(
            settings.email_reply_to
            if row.message_type not in INTERNAL_MESSAGES else ""
        ),
    )


def _aware(value: dt.datetime) -> dt.datetime:
    """SQLite hands back naive datetimes; compare in UTC regardless."""
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


# --------------------------------------------------------------------------- #
# Running the queue
# --------------------------------------------------------------------------- #

def run_once(
    *,
    owner: str | None = None,
    limit: int | None = None,
    backend: EmailBackend | None = None,
) -> dict[str, int]:
    """Claim and send one batch, each message in its own transaction.

    One transaction per message on purpose: a failure rolls back, and a shared
    transaction would take the messages that already succeeded down with it.
    """
    from modules.config import get_settings
    from modules.database import session_scope

    settings = get_settings()
    if not settings.email_enabled:
        # Rows keep accumulating. Turning sending on should deliver what waited,
        # which is why this is a skip rather than a cancel.
        return {"claimed": 0, "sent": 0, "failed": 0, "skipped": 1}

    owner = owner or worker_identity()
    limit = limit or settings.worker_batch_size

    try:
        with session_scope() as session:
            claimed = claim_batch(
                session, owner=owner, limit=limit,
                lease_seconds=settings.worker_lease_seconds,
            )
            ids = [row.id for row in claimed]
    except Exception:  # noqa: BLE001
        log.exception("The email outbox could not be read")
        return {"claimed": 0, "sent": 0, "failed": 0, "skipped": 0}

    counts = {"claimed": len(ids), "sent": 0, "failed": 0, "skipped": 0}
    for row_id in ids:
        try:
            with session_scope() as session:
                row = session.get(EmailOutbox, row_id)
                if row is None or row.status is EmailOutboxStatus.SENT:
                    continue
                status = process_one(session, row, backend=backend)
            if status is EmailOutboxStatus.SENT:
                counts["sent"] += 1
            elif status is EmailOutboxStatus.FAILED:
                counts["failed"] += 1
        except Exception:  # noqa: BLE001
            log.exception("An outbox message could not be processed")
    return counts


def worker_identity() -> str:
    """Short, unique, and not a hostname — which can be an internal address."""
    return f"w-{secrets.token_hex(6)}"


def expire_stale_payloads(session: Session, now: dt.datetime | None = None) -> int:
    """Erase sealed links whose resend window has closed.

    The ciphertext is what makes a queued invitation resendable. Once the window
    is over it is only a liability, so it goes — even for rows still queued,
    which then fail with ``link_expired`` rather than sending a link nobody
    intended to send days later.
    """
    now = now or dt.datetime.now(dt.UTC)
    rows = session.execute(
        select(EmailOutbox).where(
            EmailOutbox.secure_payload.is_not(None),
            EmailOutbox.secure_payload_expires_at.is_not(None),
        )
    ).scalars().all()

    erased = 0
    for row in rows:
        if _aware(row.secure_payload_expires_at) < now:
            _erase_payload(row)
            erased += 1
    if erased:
        session.flush()
        log.info("Erased %s expired invitation payload(s)", erased)
    return erased


# --------------------------------------------------------------------------- #
# What Phase 6C will show
# --------------------------------------------------------------------------- #

def entries_for_quotation(session: Session, quotation_id: int) -> list[OutboxEntry]:
    """Employee-safe view of what has been queued for one quotation."""
    rows = session.execute(
        select(EmailOutbox)
        .where(EmailOutbox.quotation_id == quotation_id)
        .order_by(EmailOutbox.queued_at.desc(), EmailOutbox.id.desc())
    ).scalars().all()
    return [_entry(row) for row in rows]


def entry_for(session: Session, outbox_id: int) -> OutboxEntry | None:
    row = session.get(EmailOutbox, outbox_id)
    return _entry(row) if row is not None else None


def _entry(row: EmailOutbox) -> OutboxEntry:
    return OutboxEntry(
        id=row.id,
        message_type=row.message_type,
        message_label=EMAIL_MESSAGE_DISPLAY_NAMES.get(row.message_type, "Message"),
        status=row.status,
        status_label=EMAIL_STATUS_DISPLAY_NAMES.get(row.status, "Unknown"),
        recipient_email=row.recipient_email,
        subject=row.subject or "",
        revision_no=row.revision_no,
        attempts=row.attempts,
        queued_at=row.queued_at,
        sent_at=row.sent_at,
        last_attempt_at=row.last_attempt_at,
        next_attempt_at=row.next_attempt_at,
        failure_category=row.failure_category,
        failure_code=row.failure_code,
    )


def reset_failed(session: Session, outbox_id: int, user=None) -> OutboxEntry | None:  # noqa: ANN001
    """Put a failed message back in the queue. The service behind a 6C button.

    A sent message is never reset: it went out, and re-queueing it would send a
    customer a second copy of something they already have.
    """
    row = session.get(EmailOutbox, outbox_id)
    if row is None:
        return None
    if row.status is EmailOutboxStatus.SENT:
        raise OutboxError("That message has already been sent.")

    if row.message_type in LINK_BEARING_MESSAGES and not row.secure_payload:
        raise OutboxError(
            "The secure link for this invitation is no longer available. "
            "Issue a new customer link and send it again."
        )

    row.status = EmailOutboxStatus.QUEUED
    row.attempts = 0
    row.failure_category = None
    row.failure_code = None
    row.failed_at = None
    row.next_attempt_at = dt.datetime.now(dt.UTC)
    row.lease_owner = None
    row.lease_expires_at = None
    session.flush()

    if user is not None:
        from modules.audit_service import record_audit

        record_audit(
            session, user, AuditAction.EMAIL_QUEUED,
            EntityType.QUOTATION, row.quotation_id,
            new_value={"message_type": str(row.message_type), "retry": True},
        )
    return _entry(row)
