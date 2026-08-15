"""Customer-facing quotation portal: secure links, selections, responses.

Framework-neutral on purpose. Nothing here imports Streamlit or FastAPI, so
both the employee application and the public service call the same code and
cannot drift on what a quotation is worth or who may see it.

Three rules this module exists to enforce:

* **Nothing the browser sends is trusted.** Selections arrive as line ids; the
  prices, quantities and totals are read back from the database and recomputed
  through :mod:`modules.calculation_engine`. A customer editing the form can
  change *which* optional lines they want, never what they cost.
* **The token is a capability, scoped to one quotation.** Lookup is
  hash -> token -> quotation, so there is no identifier to tamper with.
* **The customer is never an employee.** Status changes route through
  ``quotation_service.change_status`` as a reserved, permanently inactive
  system account, recorded in the audit trail as CUSTOMER_PORTAL.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules import quotation_service
from modules.audit_service import record_audit
from modules.authorization import AuthUser, require
from modules.calculation_engine import QuotationTotals
from modules.config import get_settings
from modules.constants import (
    AuditAction,
    EntityType,
    Perm,
    PortalResponseType,
    QuotationStatus,
    QuoteEventType,
    SELECTABLE_INCLUSIONS,
)
from modules.models import (
    PortalResponse,
    Quotation,
    QuotationItem,
    QuoteAccessToken,
    QuoteEvent,
    User,
)

log = logging.getLogger(__name__)

#: Reserved identity provisioned by migration d4b8e6c1a072.
PORTAL_USERNAME = "customer-portal-system"
#: What the audit trail shows as the actor. Deliberately not an employee name.
PORTAL_ACTOR_LABEL = "CUSTOMER_PORTAL"

#: 256 bits. Long enough that guessing is not a threat model.
TOKEN_BYTES = 32
#: Fallback link lifetime when the quotation carries no validity date.
DEFAULT_LINK_DAYS = 30

#: The only statuses from which a customer may act. A draft, an internally
#: rejected quotation, or one already decided must not be actionable.
RESPONDABLE_STATUSES = frozenset({QuotationStatus.SENT_TO_CUSTOMER})


class PortalError(RuntimeError):
    """Something the customer did cannot be honoured. Safe to show them."""


class PortalAccessError(PortalError):
    """The link is not valid: unknown, expired, revoked, or superseded."""


class PortalStateError(PortalError):
    """The link is valid but the quotation cannot be acted on right now."""


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #

def hash_token(raw_token: str) -> str:
    """SHA-256 hex of the plaintext token.

    Not bcrypt: this is a 256-bit random value, not a password, so there is no
    dictionary to slow down — and lookup must be an indexed equality match.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _default_expiry(quotation: Quotation, now: dt.datetime) -> dt.datetime:
    """Link dies with the offer, or after DEFAULT_LINK_DAYS if there is no date."""
    if quotation.valid_until is not None:
        return dt.datetime.combine(
            quotation.valid_until, dt.time.max, tzinfo=dt.UTC
        )
    return now + dt.timedelta(days=DEFAULT_LINK_DAYS)


def issue_token(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    expires_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> tuple[QuoteAccessToken, str]:
    """Mint a link for this quotation and return ``(row, plaintext)``.

    The plaintext is returned **once** and never stored. Losing it means
    issuing a new link, which is the intended trade: a database disclosure
    hands out no working URLs.
    """
    # Publishing to a customer is its own act, not a consequence of being able
    # to produce a PDF or edit a price.
    require(user, Perm.QUOTE_PORTAL_LINK_ISSUE)
    now = now or dt.datetime.now(dt.UTC)

    raw = secrets.token_urlsafe(TOKEN_BYTES)
    token = QuoteAccessToken(
        quotation_id=quotation.id,
        token_hash=hash_token(raw),
        expires_at=expires_at or _default_expiry(quotation, now),
        created_by_id=getattr(user, "id", None),
    )
    session.add(token)
    session.flush()

    _record_event(session, quotation.id, QuoteEventType.LINK_ISSUED, token.id)
    record_audit(
        session, user, AuditAction.QUOTE_LINK_ISSUED,
        EntityType.QUOTATION, quotation.id,
        new_value={"token_id": token.id, "expires_at": str(token.expires_at)},
    )
    return token, raw


def build_link(raw_token: str) -> str:
    """The URL a customer opens. Built from configuration, never from input."""
    base = (get_settings().portal_base_url or "").rstrip("/")
    return f"{base}/quote/public/{raw_token}" if base else f"/quote/public/{raw_token}"


def issue_and_queue_invitation(
    session: Session,
    user: AuthUser,
    quotation: Quotation,
    *,
    revised: bool = False,
    previous_revision_label: str = "",
    change_summary: str = "",
    expires_at: dt.datetime | None = None,
) -> tuple[QuoteAccessToken, str]:
    """Mint a link and queue the invitation carrying it, in one transaction.

    These two belong together: issuing is the only moment the plaintext token
    exists, so it is also the only moment an invitation can be prepared. The
    email service seals it immediately, and this function returns the plaintext
    once — the caller may show it, and nothing stores it.

    Both happen or neither does. A link issued without its invitation is a link
    the customer never receives; an invitation queued without a link is a
    message that can never be sent.
    """
    from modules import email_notifications

    token, raw = issue_token(session, user, quotation, expires_at=expires_at)
    email_notifications.queue_invitation(
        session, quotation, build_link(raw),
        revised=revised,
        previous_revision_label=previous_revision_label,
        change_summary=change_summary,
        # A reissued link is a new message, not a duplicate of the earlier one.
        discriminator=str(token.id),
    )
    return token, raw


def revoke_token(session: Session, user: AuthUser, token: QuoteAccessToken) -> None:
    """Kill a link immediately. Idempotent."""
    require(user, Perm.QUOTE_PORTAL_LINK_REVOKE)
    if token.revoked_at is not None:
        return
    token.revoked_at = dt.datetime.now(dt.UTC)
    session.flush()
    _record_event(session, token.quotation_id, QuoteEventType.LINK_REVOKED, token.id)
    record_audit(
        session, user, AuditAction.QUOTE_LINK_REVOKED,
        EntityType.QUOTATION, token.quotation_id,
        new_value={"token_id": token.id},
    )


def active_tokens(session: Session, quotation_id: int) -> list[QuoteAccessToken]:
    now = dt.datetime.now(dt.UTC)
    rows = session.execute(
        select(QuoteAccessToken).where(QuoteAccessToken.quotation_id == quotation_id)
    ).scalars().all()
    return [t for t in rows if t.is_usable(now)]


def resolve_token(session: Session, raw_token: str) -> QuoteAccessToken:
    """Find the token behind a URL, or refuse.

    Deliberately does **not** record a view: resolving happens on POSTs too,
    and inflating the view count when a customer submits a form would make the
    "first viewed" timestamp meaningless.

    Every failure raises the same exception type with a generic message — the
    caller must not tell a probing client whether a token existed.
    """
    if not raw_token or len(raw_token) < 16:
        raise PortalAccessError("This quotation link is not valid.")

    token = session.execute(
        select(QuoteAccessToken).where(
            QuoteAccessToken.token_hash == hash_token(raw_token)
        )
    ).scalar_one_or_none()

    if token is None:
        raise PortalAccessError("This quotation link is not valid.")
    if not token.is_usable():
        _record_event(
            session, token.quotation_id, QuoteEventType.ACCESS_DENIED, token.id,
            detail={"reason": "revoked" if token.revoked_at else "expired"},
        )
        raise PortalAccessError(
            "This quotation link has expired. Please contact us for a current copy."
        )

    quotation = session.get(Quotation, token.quotation_id)
    if quotation is None or quotation.deleted_at is not None:
        raise PortalAccessError("This quotation is no longer available.")
    if not quotation.is_current_revision:
        _record_event(
            session, token.quotation_id, QuoteEventType.ACCESS_DENIED, token.id,
            detail={"reason": "superseded"},
        )
        raise PortalAccessError(
            "This quotation has been superseded by a newer version."
        )
    return token


def record_view(session: Session, token: QuoteAccessToken) -> None:
    """Note that the customer opened the page. No IP, no user agent."""
    now = dt.datetime.now(dt.UTC)
    if token.first_viewed_at is None:
        token.first_viewed_at = now
    token.last_viewed_at = now
    token.view_count = (token.view_count or 0) + 1
    session.flush()
    _record_event(session, token.quotation_id, QuoteEventType.VIEWED, token.id)


def record_download(
    session: Session,
    token: QuoteAccessToken | None,
    *,
    accepted: bool,
    quotation: Quotation | None = None,
) -> None:
    """Note that a PDF was taken. Deliberately **not** a view.

    ``view_count`` and ``first_viewed_at`` are left alone: a customer who
    downloads without opening the page has not viewed it, and counting it as
    one would make "first viewed" a worse answer to the only question anybody
    asks of it. The selection is not recorded either — it was temporary, and
    persisting it would turn a read into a write.
    """
    quotation_id = token.quotation_id if token is not None else (
        quotation.id if quotation is not None else None
    )
    if quotation_id is None:
        return
    _record_event(
        session, quotation_id, QuoteEventType.PDF_DOWNLOADED,
        token.id if token is not None else None,
        detail={"kind": "accepted" if accepted else "draft"},
    )


def _record_event(
    session: Session,
    quotation_id: int,
    event_type: QuoteEventType,
    token_id: int | None = None,
    detail: dict | None = None,
) -> QuoteEvent:
    event = QuoteEvent(
        quotation_id=quotation_id,
        access_token_id=token_id,
        event_type=event_type,
        detail_json=detail,
    )
    session.add(event)
    session.flush()
    return event


# --------------------------------------------------------------------------- #
# Anti-forgery / anti-replay
# --------------------------------------------------------------------------- #

def issue_submission_nonce(token: QuoteAccessToken) -> tuple[str, str]:
    """Return ``(nonce, signature)`` to embed in a submission form.

    The signature binds the nonce to this specific token using the application
    secret, so a form cannot be forged from another origin or replayed against
    a different quotation. One-time use is enforced separately, by the unique
    index on ``portal_responses.submission_nonce``.
    """
    nonce = secrets.token_hex(16)
    return nonce, _sign_nonce(token.token_hash, nonce)


def _sign_nonce(token_hash: str, nonce: str) -> str:
    secret = get_settings().secret_key.encode("utf-8")
    return hmac.new(secret, f"{token_hash}:{nonce}".encode("utf-8"), hashlib.sha256).hexdigest()


def verify_submission_nonce(token: QuoteAccessToken, nonce: str, signature: str) -> None:
    """Raise unless the signature was produced for this token and nonce."""
    if not nonce or not signature:
        raise PortalError("This form has expired. Please reload the page and try again.")
    expected = _sign_nonce(token.token_hash, nonce)
    if not hmac.compare_digest(expected, signature):
        raise PortalError("This form has expired. Please reload the page and try again.")


# --------------------------------------------------------------------------- #
# The download capability
# --------------------------------------------------------------------------- #

#: Names the purpose inside the signed payload. A signature is only ever valid
#: for the thing it was issued for, so a capability to read cannot become a
#: capability to act.
DOWNLOAD_PURPOSE = "download"

#: Short, but long enough to survive a customer reading the page before
#: clicking. Downloading is read-only and idempotent, so the nonce may be used
#: more than once inside the window — unlike a submission nonce, whose whole
#: job is to be spent exactly once.
DOWNLOAD_NONCE_TTL_SECONDS = 30 * 60


def issue_download_nonce(
    token: QuoteAccessToken,
    quotation: Quotation,
    *,
    now: dt.datetime | None = None,
) -> tuple[str, str]:
    """Return ``(nonce, signature)`` for a PDF download of this exact revision.

    Bound to four things, all inside the signed payload: the purpose, this
    token, the revision on screen, and an expiry. Changing any of them changes
    the signature, so a nonce issued for one quotation is meaningless against
    another, and one issued for a revision the customer has since been sent a
    replacement for stops working.
    """
    now = now or dt.datetime.now(dt.UTC)
    expires = int((now + dt.timedelta(seconds=DOWNLOAD_NONCE_TTL_SECONDS)).timestamp())
    nonce = f"{DOWNLOAD_PURPOSE}.{quotation.revision_no}.{expires}.{secrets.token_hex(8)}"
    return nonce, _sign_purpose_nonce(token.token_hash, nonce)


def _sign_purpose_nonce(token_hash: str, nonce: str) -> str:
    """Sign a purpose-bearing nonce.

    A deliberately different message shape from :func:`_sign_nonce`. The two
    cannot collide, so a download signature presented to the approval path
    fails to verify and vice versa — the separation is arithmetic, not a check
    somebody has to remember to write.
    """
    secret = get_settings().secret_key.encode("utf-8")
    message = f"purpose:{token_hash}:{nonce}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_download_nonce(
    token: QuoteAccessToken,
    quotation: Quotation,
    nonce: str,
    signature: str,
    *,
    now: dt.datetime | None = None,
) -> None:
    """Raise unless this nonce authorises a download of this revision, now.

    Every rejection raises the same error with the same wording: which of the
    four bindings failed is not something a caller needs and not something a
    prober should learn.
    """
    refusal = PortalError(
        "This download link has expired. Please reload the page and try again."
    )
    if not nonce or not signature:
        raise refusal

    parts = nonce.split(".")
    if len(parts) != 4:
        raise refusal
    purpose, revision, expires, _random = parts

    if purpose != DOWNLOAD_PURPOSE:
        raise refusal

    # Verify the signature before trusting any field parsed out of the nonce.
    expected = _sign_purpose_nonce(token.token_hash, nonce)
    if not hmac.compare_digest(expected, signature):
        raise refusal

    try:
        if int(revision) != quotation.revision_no:
            raise refusal
        if int(expires) <= (now or dt.datetime.now(dt.UTC)).timestamp():
            raise refusal
    except ValueError:
        raise refusal from None


# --------------------------------------------------------------------------- #
# Selections and money
# --------------------------------------------------------------------------- #

def selectable_items(quotation: Quotation) -> list[QuotationItem]:
    """Lines the customer may add. Included lines are not negotiable."""
    return [i for i in quotation.items if i.inclusion in SELECTABLE_INCLUSIONS]


def normalise_selection(
    quotation: Quotation, selected_ids: list[int] | None
) -> list[int]:
    """Reduce whatever the browser sent to ids that are genuinely selectable here.

    Anything else — a line from another quotation, an included line, a
    non-numeric value — is dropped rather than rejected, because a stale form
    should not become an error page for the customer.
    """
    if not selected_ids:
        return []
    allowed = {i.id for i in selectable_items(quotation)}
    seen: set[int] = set()
    ordered: list[int] = []
    for raw in selected_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value in allowed and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def compute_selection_totals(
    quotation: Quotation, selected_ids: list[int] | None = None
) -> QuotationTotals:
    """Recalculate for a given set of optional lines.

    Delegates to :mod:`modules.pricing_snapshot`, which is the only place in
    the application that adds money up. Kept as a thin wrapper because callers
    already expect a QuotationTotals.
    """
    from modules import pricing_snapshot

    snapshot = pricing_snapshot.selected(quotation, selected_ids)
    return QuotationTotals(
        subtotal=snapshot.subtotal,
        quotation_discount=snapshot.discount,
        charges_total=snapshot.charges_total,
        charges_customer_visible=snapshot.charges_customer_visible,
        taxable_base=snapshot.taxable_base,
        tax_amount=snapshot.tax_amount,
        grand_total=snapshot.grand_total,
        total_cost=None,
        gross_profit=None,
        gross_margin_pct=None,
        total_savings=None,
    )


def deposit_due(quotation: Quotation, totals: QuotationTotals) -> Decimal:
    """Deposit as money, derived from the rate so it cannot drift."""
    from modules.calculation_engine import deposit_amount

    return deposit_amount(totals.grand_total, quotation.deposit_pct)


# --------------------------------------------------------------------------- #
# The customer's decision
# --------------------------------------------------------------------------- #

def ensure_portal_user(session: Session) -> User:
    """Provision the reserved system account if it is missing. Idempotent.

    Migration ``d4b8e6c1a072`` is authoritative for deployments — it runs raw
    SQL so it cannot break when these models change. This exists for contexts
    that build the schema from metadata rather than by migrating, chiefly the
    test suite, and applies the same two rules: never adopt an active account,
    never store a usable credential.
    """
    existing = session.execute(
        select(User).where(User.username == PORTAL_USERNAME)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.is_active:
            raise PortalError(
                f"Username {PORTAL_USERNAME!r} already belongs to an active account."
            )
        return existing

    import bcrypt

    throwaway = secrets.token_urlsafe(64).encode("utf-8")[:72]
    user = User(
        username=PORTAL_USERNAME,
        email="customer-portal-system@portal.invalid",
        employee_name="Customer Portal (system)",
        job_title="System account",
        password_hash=bcrypt.hashpw(throwaway, bcrypt.gensalt(rounds=12)).decode("ascii"),
        must_change_password=False,
        is_active=False,
    )
    session.add(user)
    session.flush()
    return user


def portal_actor(session: Session) -> AuthUser:
    """The reserved system identity used for customer-initiated status changes.

    Built in memory with exactly one permission rather than loaded from the
    database. A stored grant could be widened by an unrelated role edit; this
    set cannot be. The account itself can never authenticate — ``authenticate``
    and ``revalidate`` both reject inactive users.
    """
    row = session.execute(
        select(User).where(User.username == PORTAL_USERNAME)
    ).scalar_one_or_none()
    if row is None:
        raise PortalError(
            "The customer portal system account is missing. Run database "
            "migrations (revision d4b8e6c1a072) before serving the portal."
        )
    if row.is_active:
        # Refuse rather than act as an account somebody can log into.
        raise PortalError(
            "The reserved portal account has been activated and is no longer "
            "safe to use as a system actor."
        )
    return AuthUser(
        id=row.id,
        username=PORTAL_ACTOR_LABEL,
        employee_name=PORTAL_ACTOR_LABEL,
        email="",
        permissions=frozenset({Perm.QUOTE_UPDATE_STATUS}),
    )


def assert_respondable(quotation: Quotation, now: dt.date | None = None) -> None:
    """Raise unless the customer may still act on this quotation."""
    now = now or dt.date.today()

    if quotation.status is QuotationStatus.ACCEPTED:
        raise PortalStateError("This quotation has already been accepted.")
    if quotation.status in {QuotationStatus.LOST, QuotationStatus.CANCELLED}:
        raise PortalStateError("This quotation is closed.")
    if quotation.status is QuotationStatus.EXPIRED:
        raise PortalStateError("This quotation has expired.")
    if quotation.status not in RESPONDABLE_STATUSES:
        raise PortalStateError(
            "This quotation is not open for a response. Please contact us."
        )
    if not quotation.is_current_revision:
        raise PortalStateError("This quotation has been superseded.")
    if quotation.valid_until is not None and quotation.valid_until < now:
        raise PortalStateError("This quotation has expired.")


def approve(
    session: Session,
    token: QuoteAccessToken,
    *,
    customer_name: str,
    customer_email: str = "",
    job_title: str = "",
    comment: str = "",
    signature_name: str = "",
    accepted_terms: bool = False,
    selected_ids: list[int] | None = None,
    nonce: str | None = None,
) -> PortalResponse:
    """Record acceptance, lock the revision and move the quotation to ACCEPTED.

    Ordering matters. The response row is written **first**, so the unique index
    on (quotation_id, revision_no) settles any race before a status change is
    attempted: two simultaneous approvals mean exactly one commits.
    """
    quotation = session.get(Quotation, token.quotation_id)
    if quotation is None:
        raise PortalAccessError("This quotation is no longer available.")

    assert_respondable(quotation)

    if not (customer_name or "").strip():
        raise PortalError("Please enter your name.")
    if not accepted_terms:
        raise PortalError("Please confirm you accept the quotation and its terms.")

    chosen = normalise_selection(quotation, selected_ids)
    totals = compute_selection_totals(quotation, chosen)

    response = PortalResponse(
        quotation_id=quotation.id,
        access_token_id=token.id,
        revision_no=quotation.revision_no,
        response_type=PortalResponseType.APPROVED,
        customer_name=customer_name.strip()[:160],
        job_title=(job_title or "").strip()[:120] or None,
        customer_email=(customer_email or "").strip()[:255] or None,
        comment=(comment or "").strip() or None,
        signature_name=(signature_name or customer_name).strip()[:160],
        accepted_terms=True,
        selected_item_ids=chosen,
        currency=quotation.currency,
        subtotal=totals.subtotal,
        tax_amount=totals.tax_amount,
        grand_total=totals.grand_total,
        submission_nonce=nonce,
    )
    session.add(response)
    session.flush()   # unique indexes decide the race here

    # The document this acceptance owes, recorded in the same transaction. It
    # commits with the acceptance or not at all, so an accepted quotation
    # always has a job — but producing the PDF happens afterwards, and the
    # customer never waits for a renderer or an object store to be reachable.
    from modules import email_notifications, quote_document_service

    quote_document_service.enqueue(session, response)

    # And the messages this acceptance calls for. Queued, not sent: an SMTP
    # call here would hold the transaction open across a round trip to somebody
    # else's server, or send a confirmation for an approval that then rolled
    # back. A failure to *queue* does roll the acceptance back, which is the
    # intended asymmetry — a notification that cannot be recorded at all means
    # something is wrong enough that the event should not stand.
    email_notifications.on_customer_approved(session, quotation, response)

    # The accepted revision becomes immutable; further changes need a revision.
    quotation.is_locked = True

    quotation_service.change_status(
        session, portal_actor(session), quotation, QuotationStatus.ACCEPTED,
        note=f"Accepted by {response.customer_name} via the customer portal.",
    )

    _record_event(
        session, quotation.id, QuoteEventType.APPROVED, token.id,
        detail={"response_id": response.id, "revision_no": response.revision_no},
    )
    record_audit(
        session, None, AuditAction.CUSTOMER_APPROVED,
        EntityType.QUOTATION, quotation.id,
        username=PORTAL_ACTOR_LABEL,
        new_value={
            "response_id": response.id,
            "revision_no": response.revision_no,
            "customer_name": response.customer_name,
            "customer_email": response.customer_email,
            "signature_name": response.signature_name,
            "selected_item_ids": chosen,
            "grand_total": str(response.grand_total),
            "currency": response.currency,
        },
    )
    log.info(
        "Quotation %s accepted via portal by %s (total %s %s)",
        quotation.quote_number, response.customer_name,
        response.currency, response.grand_total,
    )
    return response


def request_changes(
    session: Session,
    token: QuoteAccessToken,
    *,
    customer_name: str,
    comment: str,
    customer_email: str = "",
    attachment_key: str | None = None,
    nonce: str | None = None,
) -> PortalResponse:
    """Record a change request and move the quotation to REVISION_REQUIRED."""
    quotation = session.get(Quotation, token.quotation_id)
    if quotation is None:
        raise PortalAccessError("This quotation is no longer available.")

    assert_respondable(quotation)

    if not (customer_name or "").strip():
        raise PortalError("Please enter your name.")
    if not (comment or "").strip():
        raise PortalError("Please tell us what you would like changed.")

    response = PortalResponse(
        quotation_id=quotation.id,
        access_token_id=token.id,
        revision_no=quotation.revision_no,
        response_type=PortalResponseType.CHANGES_REQUESTED,
        customer_name=customer_name.strip()[:160],
        customer_email=(customer_email or "").strip()[:255] or None,
        comment=comment.strip(),
        accepted_terms=False,
        currency=quotation.currency,
        attachment_key=attachment_key,
        submission_nonce=nonce,
    )
    session.add(response)
    session.flush()

    from modules import email_notifications

    email_notifications.on_customer_requested_changes(session, quotation, response)

    quotation_service.change_status(
        session, portal_actor(session), quotation,
        QuotationStatus.REVISION_REQUIRED,
        note=f"Changes requested by {response.customer_name}: {response.comment}"[:2000],
    )

    _record_event(
        session, quotation.id, QuoteEventType.CHANGES_REQUESTED, token.id,
        detail={"response_id": response.id},
    )
    record_audit(
        session, None, AuditAction.CUSTOMER_REQUESTED_CHANGES,
        EntityType.QUOTATION, quotation.id,
        username=PORTAL_ACTOR_LABEL,
        new_value={
            "response_id": response.id,
            "revision_no": response.revision_no,
            "customer_name": response.customer_name,
            "customer_email": response.customer_email,
        },
    )
    return response
