"""Sending a quotation: the one action in the application that cannot be undone.

A wrong price can be revised, a bad link revoked, a PDF regenerated. A message
in a customer's inbox cannot be recalled. So the claims under test are about
what it takes to send, what sending cannot be tricked into, and the difference
between trying again and starting again.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules import (
    approval_service,
    email_backend,
    email_outbox_service as outbox,
    portal_service,
    quotation_service,
    quote_send_service as sender,
)
from modules.authorization import PermissionDenied
from modules.constants import (
    AuditAction,
    EmailMessageType,
    EmailOutboxStatus,
    ItemInclusion,
    Perm,
    PriceTierCode,
    QuotationStatus,
    QuoteEventType,
    RoleCode,
)
from modules.models import (
    AuditLog,
    CustomerContact,
    EmailOutbox,
    Quotation,
    QuotationTerm,
    QuoteAccessToken,
    QuoteEvent,
)

from tests.test_documents_and_approval import (  # noqa: F401
    admin,
    manager,
    quotation,
    sales,
    variant,
)

GOOD_ADDRESS = "dana@harbourfoods.co.uk"
OTHER_ADDRESS = "sam.ledger@harbourfoods.co.uk"


@pytest.fixture(autouse=True)
def portal_user(session):
    return portal_service.ensure_portal_user(session)


@pytest.fixture(autouse=True)
def delivery_configured(monkeypatch):
    """Sending enabled, against the capture backend. Nothing leaves the process."""
    from modules.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "email_enabled", True)
    monkeypatch.setattr(settings, "portal_base_url", "https://quotes.test.invalid")
    monkeypatch.setattr(settings, "email_from_address", "quotes@northwind.invalid")

    backend = email_backend.MemoryBackend()
    monkeypatch.setattr(email_backend, "get_backend", lambda: backend)
    monkeypatch.setattr(outbox, "get_backend", lambda: backend)
    return backend


@pytest.fixture
def sendable(session, quotation, sales, manager, variant):
    """An approved quotation with terms, a validity date and a contact."""
    line = quotation_service.add_line(
        session, sales, quotation,
        product_variant_id=variant.id,
        price_tier_code=PriceTierCode.STANDARD.value,
        quantity_packs=D("100"), description_override="Two-colour print",
    )
    line.inclusion = ItemInclusion.OPTIONAL

    quotation.contact_name = "Dana Whitfield"
    quotation.contact_email = GOOD_ADDRESS
    quotation.valid_until = dt.date.today() + dt.timedelta(days=30)
    quotation.tax_rate_pct = D("13")
    session.add(QuotationTerm(
        quotation_id=quotation.id, title="1. Payment", body_text="Net 30.",
        sort_order=0, is_customer_visible=True, section="PAYMENT_TERMS",
    ))
    session.flush()
    quotation_service.recompute_totals(session, quotation)

    approval_service.submit(session, quotation, sales, note="for sending")
    if quotation.status is QuotationStatus.DRAFT:
        quotation_service.change_status(
            session, manager, quotation, QuotationStatus.APPROVED
        )
    session.commit()
    return quotation


@pytest.fixture
def finance(session, make_auth_user):
    return make_auth_user(RoleCode.FINANCE.value, username="finance-user")


@pytest.fixture
def pricer(session, make_auth_user):
    return make_auth_user(RoleCode.PRICING_ADMIN.value, username="pricing-user")


def _send(session, user, quotation, **overrides):
    payload = dict(
        message_type=EmailMessageType.QUOTE_INVITATION,
        recipient_email=GOOD_ADDRESS,
        recipient_name="Dana Whitfield",
    )
    payload.update(overrides)
    return sender.send(session, user, quotation.id, **payload)


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #

class TestPermissionMatrix:
    @pytest.mark.parametrize(
        ("role", "send", "retry", "view"),
        [
            (RoleCode.SALES, True, False, True),
            (RoleCode.SALES_MANAGER, True, True, True),
            (RoleCode.FINANCE, False, False, True),
            (RoleCode.PRICING_ADMIN, False, False, False),
            (RoleCode.SYS_ADMIN, True, True, True),
        ],
    )
    def test_the_matrix_is_what_was_agreed(self, role, send, retry, view):
        from modules.constants import ROLE_PERMISSIONS

        granted = ROLE_PERMISSIONS[role]
        assert (Perm.QUOTE_PORTAL_SEND in granted) is send
        assert (Perm.QUOTE_PORTAL_RETRY in granted) is retry
        assert (Perm.QUOTE_PORTAL_VIEW_DELIVERY in granted) is view

    def test_pricing_admin_cannot_send(self, session, sendable, pricer):
        """Setting a price is not publishing one."""
        with pytest.raises(PermissionDenied):
            _send(session, pricer, sendable)

    def test_finance_cannot_send(self, session, sendable, finance):
        with pytest.raises(PermissionDenied):
            _send(session, finance, sendable)

    def test_finance_can_read_delivery_history(self, session, sendable, admin, finance):
        _send(session, admin, sendable)
        session.commit()

        rows = sender.delivery_history(session, finance, sendable.id)
        assert len(rows) == 1
        assert rows[0].recipient_email == GOOD_ADDRESS

    def test_pricing_admin_cannot_read_delivery_history(
        self, session, sendable, admin, pricer
    ):
        _send(session, admin, sendable)
        session.commit()

        with pytest.raises(PermissionDenied):
            sender.delivery_history(session, pricer, sendable.id)

    def test_a_summary_for_somebody_without_permission_says_nothing(
        self, session, sendable, admin, pricer
    ):
        _send(session, admin, sendable)
        session.commit()

        summary = sender.delivery_summary(session, pricer, sendable.id)
        assert not summary.has_activity
        assert summary.recipient_email == ""

    def test_sales_cannot_retry(self, session, sendable, sales, admin):
        result = _send(session, admin, sendable)
        session.commit()
        _fail_permanently(session, result.outbox_id)

        assert not sender.check_retry(session, sales, result.outbox_id).may_retry
        with pytest.raises(PermissionDenied):
            sender.retry(session, sales, result.outbox_id)

    def test_the_service_enforces_regardless_of_the_page(
        self, session, sendable, pricer
    ):
        """Hiding a button is a courtesy; the check is here."""
        import inspect

        assert "require(user, Perm.QUOTE_PORTAL_SEND)" in inspect.getsource(sender.send)
        assert "require(user, Perm.QUOTE_PORTAL_RETRY)" in inspect.getsource(sender.retry)
        assert (
            "require(user, Perm.QUOTE_PORTAL_VIEW_DELIVERY)"
            in inspect.getsource(sender.delivery_history)
        )


def _fail_permanently(session, outbox_id: int) -> EmailOutbox:
    row = session.get(EmailOutbox, outbox_id)
    row.status = EmailOutboxStatus.FAILED
    row.attempts = 3
    row.failed_at = dt.datetime.now(dt.UTC)
    from modules.constants import EmailFailureCategory

    row.failure_category = EmailFailureCategory.PERMANENT
    row.failure_code = "smtp_550"
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #

class TestEligibility:
    def test_an_approved_quotation_is_sendable(self, session, sendable, admin):
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert eligibility.may_send, [b.code for b in eligibility.blockers]

    def test_a_draft_is_refused(self, session, quotation, admin):
        eligibility = sender.check_eligibility(session, admin, quotation)
        assert not eligibility.may_send
        assert "draft" in [b.code for b in eligibility.blockers]

    @pytest.mark.parametrize(
        ("status", "code"),
        [
            (QuotationStatus.CANCELLED, "closed"),
            (QuotationStatus.LOST, "closed"),
            (QuotationStatus.EXPIRED, "expired"),
        ],
    )
    def test_closed_and_expired_quotations_are_refused(
        self, session, sendable, admin, status, code
    ):
        sendable.status = status
        session.flush()
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert code in [b.code for b in eligibility.blockers]

    def test_a_superseded_revision_is_refused(self, session, sendable, admin):
        sendable.is_current_revision = False
        session.flush()
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert "superseded" in [b.code for b in eligibility.blockers]

    def test_an_accepted_quotation_needs_a_new_revision(
        self, session, sendable, admin
    ):
        """Re-inviting somebody to a quotation they signed asks them to sign twice."""
        sendable.status = QuotationStatus.ACCEPTED
        session.flush()
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        blocker = next(b for b in eligibility.blockers if b.code == "accepted")
        assert "new revision" in blocker.detail

    def test_a_lapsed_validity_date_is_refused(self, session, sendable, admin):
        sendable.valid_until = dt.date.today() - dt.timedelta(days=1)
        session.flush()
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert "lapsed" in [b.code for b in eligibility.blockers]

    def test_a_missing_validity_date_is_refused(self, session, sendable, admin):
        sendable.valid_until = None
        session.flush()
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert "no_validity" in [b.code for b in eligibility.blockers]

    def test_missing_terms_are_refused(self, session, sendable, admin):
        for term in sendable.terms:
            term.is_customer_visible = False
        session.flush()
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert "no_terms" in [b.code for b in eligibility.blockers]

    def test_a_zero_tax_rate_is_not_treated_as_missing(
        self, session, sendable, admin
    ):
        """``tax_rate_pct`` is NOT NULL with a default of zero.

        So "never configured" is indistinguishable from "deliberately zero",
        and a zero rate is normal on an export sale. There is no gate on it,
        and this pins that decision rather than leaving it to be rediscovered.
        """
        sendable.tax_rate_pct = D("0")
        session.flush()
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert eligibility.may_send
        assert "no_tax" not in [b.code for b in eligibility.blockers]

    def test_email_being_switched_off_is_refused(
        self, session, sendable, admin, monkeypatch
    ):
        from modules.config import get_settings

        monkeypatch.setattr(get_settings(), "email_enabled", False)
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert "email_disabled" in [b.code for b in eligibility.blockers]

    def test_a_missing_portal_url_is_refused(
        self, session, sendable, admin, monkeypatch
    ):
        from modules.config import get_settings

        monkeypatch.setattr(get_settings(), "portal_base_url", "")
        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert "no_base_url" in [b.code for b in eligibility.blockers]

    def test_every_reason_is_reported_at_once(self, session, quotation, admin):
        """So an employee fixes everything in one pass."""
        quotation.valid_until = None
        quotation.contact_email = None
        for term in quotation.terms:
            term.is_customer_visible = False
        session.flush()

        eligibility = sender.check_eligibility(session, admin, quotation)
        codes = {b.code for b in eligibility.blockers}
        assert {"draft", "no_validity", "no_terms", "recipient"} <= codes


# --------------------------------------------------------------------------- #
# Recipients
# --------------------------------------------------------------------------- #

class TestRecipients:
    @pytest.mark.parametrize(
        "address",
        ["", "   ", "nope", "a@b", "user@@x.co", "dana@x.co\r\nBcc: evil@x.co"],
    )
    def test_unusable_addresses_are_refused(self, address):
        assert sender.recipient_problem(address)

    @pytest.mark.parametrize(
        "address",
        [
            "somebody@example.com", "test@test.invalid", "x@localhost",
            "noreply@realcompany.co.uk", "changeme@realcompany.co.uk",
        ],
    )
    def test_placeholder_addresses_are_refused(self, address):
        """A quotation sent to a placeholder is one the customer never received."""
        assert sender.recipient_problem(address)

    @pytest.mark.parametrize(
        "address",
        [
            "dana@harbourfoods.invalid", "dana@customer.test",
            "dana@acme.example", "dana@box.localhost",
        ],
    )
    def test_reserved_domains_are_refused(self, address):
        """RFC 2606 reserves these so they can never resolve.

        The one that actually turns up: documentation and seeded data use
        ``.invalid``, so it is what gets copied into a real customer record.
        """
        problem = sender.recipient_problem(address)
        assert "reserved" in problem

    def test_a_real_address_is_accepted(self):
        assert sender.recipient_problem(GOOD_ADDRESS) == ""

    def test_crlf_is_named_specifically(self):
        problem = sender.recipient_problem("dana@x.co\r\nBcc: evil@x.co")
        assert "line break" in problem

    def test_the_quotation_contact_comes_first(self, session, sendable):
        options = sender.recipient_options(session, sendable)
        assert options[0].email == GOOD_ADDRESS
        assert options[0].source == "quotation"

    def test_customer_contacts_are_offered(self, session, sendable):
        session.add(CustomerContact(
            customer_id=sendable.customer_id, name="Sam Ledger",
            email=OTHER_ADDRESS, title="Accounts", is_primary=True,
        ))
        session.flush()

        addresses = {o.email for o in sender.recipient_options(session, sendable)}
        assert {GOOD_ADDRESS, OTHER_ADDRESS} <= addresses

    def test_a_duplicate_address_appears_once(self, session, sendable):
        before = len(sender.recipient_options(session, sendable))
        session.add(CustomerContact(
            customer_id=sendable.customer_id, name="Dana Again",
            email=GOOD_ADDRESS.upper(), is_primary=True,
        ))
        session.flush()

        options = sender.recipient_options(session, sendable)
        assert len(options) == before
        assert [o.email for o in options].count(GOOD_ADDRESS) == 1

    def test_more_than_one_contact_requires_an_explicit_choice(
        self, session, sendable
    ):
        """The primary contact is not always who asked for the quotation."""
        session.add(CustomerContact(
            customer_id=sendable.customer_id, name="Sam Ledger",
            email=OTHER_ADDRESS, is_primary=True,
        ))
        session.flush()

        options = sender.recipient_options(session, sendable)
        assert sender.requires_explicit_choice(options)

    def test_a_single_contact_needs_no_choice(self, session, sendable):
        for contact in session.query(CustomerContact).all():
            contact.is_active = False
        session.flush()

        options = sender.recipient_options(session, sendable)
        assert len(options) == 1
        assert not sender.requires_explicit_choice(options)

    def test_an_override_is_recorded_on_the_intent(self, session, sendable, admin):
        result = _send(
            session, admin, sendable,
            recipient_email=OTHER_ADDRESS, recipient_name="Sam Ledger",
        )
        session.commit()

        row = session.get(EmailOutbox, result.outbox_id)
        assert row.recipient_email == OTHER_ADDRESS
        assert result.recipient_email == OTHER_ADDRESS

    def test_an_override_does_not_touch_the_customer_record(
        self, session, sendable, admin
    ):
        """One-time delivery is not an edit to the customer."""
        before = sendable.contact_email
        contacts_before = [
            (c.id, c.email) for c in session.query(CustomerContact).all()
        ]

        _send(session, admin, sendable, recipient_email=OTHER_ADDRESS)
        session.commit()

        assert sendable.contact_email == before
        assert [
            (c.id, c.email) for c in session.query(CustomerContact).all()
        ] == contacts_before

    def test_an_override_seals_the_link_to_the_actual_recipient(
        self, session, sendable, admin, delivery_configured
    ):
        """The payload binds to the address, so it has to be right at enqueue."""
        result = _send(session, admin, sendable, recipient_email=OTHER_ADDRESS)
        session.commit()

        assert outbox.run_once(backend=delivery_configured)["sent"] == 1
        message = delivery_configured.sent[0]
        assert message.to_email == OTHER_ADDRESS

    def test_a_bad_override_is_refused_before_anything_is_created(
        self, session, sendable, admin
    ):
        tokens_before = session.query(QuoteAccessToken).count()
        with pytest.raises(sender.SendError):
            _send(session, admin, sendable, recipient_email="nope@example.com")

        assert session.query(QuoteAccessToken).count() == tokens_before
        assert session.query(EmailOutbox).count() == 0


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #

class TestPreview:
    def test_a_preview_creates_nothing(self, session, sendable, admin):
        before = (
            session.query(QuoteAccessToken).count(),
            session.query(EmailOutbox).count(),
            session.query(QuoteEvent).count(),
        )
        sender.preview(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_INVITATION,
            recipient_email=GOOD_ADDRESS,
        )
        session.flush()

        assert (
            session.query(QuoteAccessToken).count(),
            session.query(EmailOutbox).count(),
            session.query(QuoteEvent).count(),
        ) == before

    def test_a_preview_masks_the_capability_url(self, session, sendable, admin):
        preview = sender.preview(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_INVITATION,
            recipient_email=GOOD_ADDRESS,
        )
        assert preview.link_is_placeholder
        assert sender.PLACEHOLDER_LINK in preview.text_body
        assert "[secure-link]" in preview.html_body

    def test_a_preview_cannot_reveal_a_live_token(self, session, sendable, admin, sales):
        """Even with a real link outstanding, the preview shows the placeholder."""
        _token, raw = portal_service.issue_token(session, sales, sendable)
        session.flush()

        preview = sender.preview(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_INVITATION,
            recipient_email=GOOD_ADDRESS,
        )
        assert raw not in preview.html_body
        assert raw not in preview.text_body

    def test_a_preview_shows_the_real_figures(self, session, sendable, admin):
        from modules import pricing_snapshot

        preview = sender.preview(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_INVITATION,
            recipient_email=GOOD_ADDRESS,
        )
        base = pricing_snapshot.base(sendable)
        assert f"{base.grand_total:,.2f}" in preview.total_display
        assert preview.revision_no == sendable.revision_no

    def test_a_revised_preview_uses_the_revised_template(
        self, session, sendable, admin
    ):
        preview = sender.preview(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_REVISED_INVITATION,
            recipient_email=GOOD_ADDRESS,
            previous_revision_label="Rev 0",
            change_summary="Reduced the quantity.",
        )
        assert "revised" in preview.subject.lower()
        assert "Reduced the quantity." in preview.html_body

    def test_previewing_needs_only_the_preview_permission(
        self, session, sendable, finance
    ):
        """Finance may not send, and may not preview either — that is by design."""
        with pytest.raises(PermissionDenied):
            sender.preview(
                session, finance, sendable,
                message_type=EmailMessageType.QUOTE_INVITATION,
                recipient_email=GOOD_ADDRESS,
            )


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

class TestSend:
    def test_sending_issues_a_link_and_queues_a_message(
        self, session, sendable, admin
    ):
        result = _send(session, admin, sendable)
        session.commit()

        assert session.query(QuoteAccessToken).count() == 1
        row = session.get(EmailOutbox, result.outbox_id)
        assert row.status is EmailOutboxStatus.QUEUED
        assert row.revision_no == sendable.revision_no
        assert row.secure_payload

    def test_sending_does_not_call_smtp(self, session, sendable, admin, monkeypatch):
        """The employee request queues; the worker sends."""
        import smtplib

        def refuse(*_args, **_kwargs):
            raise AssertionError("the request tried to open an SMTP connection")

        monkeypatch.setattr(smtplib, "SMTP", refuse)
        monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)

        _send(session, admin, sendable)
        session.commit()

    def test_the_result_never_carries_the_plaintext_token(
        self, session, sendable, admin
    ):
        result = _send(session, admin, sendable)
        session.commit()

        from dataclasses import asdict

        text = str(asdict(result))
        token = session.query(QuoteAccessToken).one()
        assert token.token_hash not in text
        assert not any(
            len(str(v)) > 30 and "/" in str(v) for v in asdict(result).values()
        )

    def test_sending_records_the_employee(self, session, sendable, admin):
        result = _send(session, admin, sendable)
        session.commit()

        entry = session.query(AuditLog).filter(
            AuditLog.action == str(AuditAction.EMAIL_QUEUED)
        ).one()
        assert entry.username_snapshot == admin.username
        assert entry.new_value_json["recipient"] == GOOD_ADDRESS

        history = sender.delivery_history(session, admin, sendable.id)
        assert history[0].queued_by == admin.username

    def test_sending_records_a_queued_event_not_a_view(
        self, session, sendable, admin
    ):
        _send(session, admin, sendable)
        session.commit()

        kinds = [e.event_type for e in session.query(QuoteEvent).all()]
        assert QuoteEventType.EMAIL_QUEUED in kinds
        assert QuoteEventType.VIEWED not in kinds

    def test_a_stale_revision_is_refused(self, session, sendable, admin):
        """A page left open while the quotation moved on must not send blindly."""
        with pytest.raises(sender.SendError) as caught:
            _send(session, admin, sendable, expected_revision_no=99)
        assert "moved to" in str(caught.value)

    def test_the_matching_revision_is_accepted(self, session, sendable, admin):
        result = _send(
            session, admin, sendable, expected_revision_no=sendable.revision_no
        )
        assert result.revision_no == sendable.revision_no

    def test_an_ineligible_quotation_is_refused_at_the_service(
        self, session, quotation, admin
    ):
        """Not merely disabled in the page."""
        with pytest.raises(sender.SendError):
            _send(session, admin, quotation)
        assert session.query(EmailOutbox).count() == 0

    def test_a_failure_leaves_neither_a_token_nor_a_row(
        self, session, sendable, admin, monkeypatch
    ):
        from modules import email_notifications

        def explode(*_args, **_kwargs):
            raise RuntimeError("the queue is broken")

        monkeypatch.setattr(email_notifications, "queue_invitation", explode)

        with pytest.raises(RuntimeError):
            _send(session, admin, sendable)
        session.rollback()

        assert session.query(QuoteAccessToken).count() == 0
        assert session.query(EmailOutbox).count() == 0

    def test_a_double_click_on_the_same_token_resolves_to_one_intent(
        self, session, sendable, admin
    ):
        from modules import email_notifications

        result = _send(session, admin, sendable)
        session.flush()
        again = email_notifications.queue_invitation(
            session, sendable, "https://quotes.test.invalid/quote/public/AAA",
            discriminator=str(result.token_id), recipient_email=GOOD_ADDRESS,
        )
        session.commit()

        assert again.id == result.outbox_id
        assert session.query(EmailOutbox).count() == 1

    def test_the_queued_message_is_delivered_by_the_worker(
        self, session, sendable, admin, delivery_configured
    ):
        _send(session, admin, sendable)
        session.commit()

        assert outbox.run_once(backend=delivery_configured)["sent"] == 1
        session.expire_all()
        assert sender.delivery_history(session, admin, sendable.id)[0].is_sent


# --------------------------------------------------------------------------- #
# Resend
# --------------------------------------------------------------------------- #

class TestResend:
    def test_a_resend_revokes_the_previous_link(self, session, sendable, admin):
        first = _send(session, admin, sendable)
        session.commit()

        second = _send(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_REVISED_INVITATION,
            revoke_existing=True,
        )
        session.commit()

        assert first.token_id in second.revoked_token_ids
        assert session.get(QuoteAccessToken, first.token_id).revoked_at is not None

    def test_a_resend_issues_a_new_link(self, session, sendable, admin):
        first = _send(session, admin, sendable)
        session.commit()
        second = _send(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_REVISED_INVITATION,
            revoke_existing=True,
        )
        session.commit()

        assert second.token_id != first.token_id
        live = portal_service.active_tokens(session, sendable.id)
        assert [t.id for t in live] == [second.token_id]

    def test_the_old_link_stops_working(self, session, sendable, admin):
        """The warning the employee is shown has to be true."""
        first = _send(session, admin, sendable)
        session.commit()
        raw = _raw_for(session, sendable, admin)

        _send(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_REVISED_INVITATION,
            revoke_existing=True,
        )
        session.commit()

        with pytest.raises(portal_service.PortalAccessError):
            portal_service.resolve_token(session, raw)

    def test_a_resend_creates_a_new_intent(self, session, sendable, admin):
        _send(session, admin, sendable)
        session.commit()
        _send(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_REVISED_INVITATION,
            revoke_existing=True,
        )
        session.commit()

        assert session.query(EmailOutbox).count() == 2

    def test_a_resend_records_its_own_event(self, session, sendable, admin):
        _send(session, admin, sendable)
        session.commit()
        _send(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_REVISED_INVITATION,
            revoke_existing=True,
        )
        session.commit()

        kinds = [e.event_type for e in session.query(QuoteEvent).all()]
        assert QuoteEventType.EMAIL_RESENT in kinds

    def test_sending_without_revoking_leaves_the_old_link_alone(
        self, session, sendable, admin
    ):
        first = _send(session, admin, sendable)
        session.commit()
        _send(
            session, admin, sendable,
            message_type=EmailMessageType.QUOTE_REVISED_INVITATION,
            revoke_existing=False,
        )
        session.commit()

        assert session.get(QuoteAccessToken, first.token_id).revoked_at is None


def _raw_for(session, quotation, user) -> str:
    """Issue a token and return the plaintext, for tests that need to resolve it."""
    token, raw = portal_service.issue_token(session, user, quotation)
    session.flush()
    return raw


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #

class TestRetry:
    def _failed(self, session, admin, sendable):
        result = _send(session, admin, sendable)
        session.commit()
        _fail_permanently(session, result.outbox_id)
        session.commit()
        return result

    def test_a_failed_message_can_be_retried(self, session, sendable, admin):
        result = self._failed(session, admin, sendable)
        assert sender.check_retry(session, admin, result.outbox_id).may_retry

        sender.retry(session, admin, result.outbox_id)
        session.commit()

        row = session.get(EmailOutbox, result.outbox_id)
        assert row.status is EmailOutboxStatus.QUEUED
        assert row.attempts == 0

    def test_a_retry_changes_nothing_about_the_message(
        self, session, sendable, admin
    ):
        """This is the whole difference from a resend."""
        result = self._failed(session, admin, sendable)
        row = session.get(EmailOutbox, result.outbox_id)
        before = (
            row.recipient_email, row.revision_no, row.message_type,
            row.secure_payload, dict(row.template_data_json or {}),
            row.idempotency_key,
        )

        sender.retry(session, admin, result.outbox_id)
        session.commit()
        session.expire_all()

        row = session.get(EmailOutbox, result.outbox_id)
        assert (
            row.recipient_email, row.revision_no, row.message_type,
            row.secure_payload, dict(row.template_data_json or {}),
            row.idempotency_key,
        ) == before

    def test_a_retry_issues_no_new_token(self, session, sendable, admin):
        result = self._failed(session, admin, sendable)
        tokens_before = session.query(QuoteAccessToken).count()

        sender.retry(session, admin, result.outbox_id)
        session.commit()

        assert session.query(QuoteAccessToken).count() == tokens_before

    def test_a_retry_creates_no_second_intent(self, session, sendable, admin):
        result = self._failed(session, admin, sendable)
        sender.retry(session, admin, result.outbox_id)
        session.commit()

        assert session.query(EmailOutbox).count() == 1

    def test_a_retry_records_its_own_event(self, session, sendable, admin):
        result = self._failed(session, admin, sendable)
        sender.retry(session, admin, result.outbox_id)
        session.commit()

        kinds = [e.event_type for e in session.query(QuoteEvent).all()]
        assert QuoteEventType.EMAIL_RETRY_SCHEDULED in kinds

    def test_a_sent_message_is_never_retried(
        self, session, sendable, admin, delivery_configured
    ):
        result = _send(session, admin, sendable)
        session.commit()
        outbox.run_once(backend=delivery_configured)
        session.expire_all()

        eligibility = sender.check_retry(session, admin, result.outbox_id)
        assert not eligibility.may_retry
        assert "already been sent" in eligibility.reason

    def test_an_expired_payload_cannot_be_retried(self, session, sendable, admin):
        result = self._failed(session, admin, sendable)
        row = session.get(EmailOutbox, result.outbox_id)
        row.secure_payload_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        session.flush()

        eligibility = sender.check_retry(session, admin, result.outbox_id)
        assert not eligibility.may_retry
        assert eligibility.needs_resend
        with pytest.raises(sender.SendError):
            sender.retry(session, admin, result.outbox_id)

    def test_a_missing_payload_cannot_be_retried(self, session, sendable, admin):
        result = self._failed(session, admin, sendable)
        session.get(EmailOutbox, result.outbox_id).secure_payload = None
        session.flush()

        eligibility = sender.check_retry(session, admin, result.outbox_id)
        assert not eligibility.may_retry
        assert eligibility.needs_resend

    def test_a_revoked_link_cannot_be_retried(self, session, sendable, admin):
        result = self._failed(session, admin, sendable)
        token = session.get(QuoteAccessToken, result.token_id)
        portal_service.revoke_token(session, admin, token)
        session.flush()

        eligibility = sender.check_retry(session, admin, result.outbox_id)
        assert not eligibility.may_retry
        assert eligibility.needs_resend

    def test_a_message_queued_against_an_older_revision_is_not_retried(
        self, session, sendable, admin
    ):
        """The quotation has moved on; that message is about something else.

        Staged on the outbox row rather than by bumping the quotation, because
        sending issues the quotation and the immutability guard then — correctly
        — refuses to let its revision number be edited at all.
        """
        result = self._failed(session, admin, sendable)
        row = session.get(EmailOutbox, result.outbox_id)
        row.revision_no = row.revision_no - 1
        session.flush()

        eligibility = sender.check_retry(session, admin, result.outbox_id)
        assert not eligibility.may_retry
        assert eligibility.needs_resend

    def test_a_retried_message_is_then_delivered(
        self, session, sendable, admin, delivery_configured
    ):
        result = self._failed(session, admin, sendable)
        sender.retry(session, admin, result.outbox_id)
        session.commit()

        assert outbox.run_once(backend=delivery_configured)["sent"] == 1


# --------------------------------------------------------------------------- #
# Delivery history
# --------------------------------------------------------------------------- #

class TestDeliveryHistory:
    def test_the_projection_carries_nothing_dangerous(
        self, session, sendable, admin
    ):
        from dataclasses import fields

        _send(session, admin, sendable)
        session.commit()

        names = {f.name for f in fields(sender.DeliveryRow)}
        for forbidden in (
            "secure_payload", "ciphertext", "key_version", "last_error",
            "template_data", "brand_snapshot", "storage_key", "provider_message_id",
            "idempotency_key", "token", "link",
        ):
            assert forbidden not in names

    def test_no_row_leaks_the_sealed_payload(self, session, sendable, admin):
        from dataclasses import asdict

        _send(session, admin, sendable)
        session.commit()
        row = session.query(EmailOutbox).one()

        shown = str(asdict(sender.delivery_history(session, admin, sendable.id)[0]))
        assert row.secure_payload not in shown
        assert "sb1." not in shown

    def test_a_failure_shows_a_category_not_provider_text(
        self, session, sendable, admin
    ):
        result = _send(session, admin, sendable)
        session.commit()
        row = _fail_permanently(session, result.outbox_id)
        row.failure_code = "smtp_550"
        session.commit()

        entry = sender.delivery_history(session, admin, sendable.id)[0]
        assert entry.failure_summary == "Could not be delivered"
        assert "550" not in entry.failure_summary
        assert GOOD_ADDRESS not in entry.failure_summary

    @pytest.mark.parametrize(
        ("setup", "expected"),
        [
            ("queued", "Queued"),
            ("retry", "Retry scheduled"),
            ("sent", "Sent"),
            ("failed", "Permanently failed"),
            ("expired", "Link expired"),
        ],
    )
    def test_status_labels_are_unambiguous(
        self, session, sendable, admin, delivery_configured, setup, expected
    ):
        result = _send(session, admin, sendable)
        session.commit()
        row = session.get(EmailOutbox, result.outbox_id)

        if setup == "retry":
            row.attempts = 2
            row.next_attempt_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
        elif setup == "sent":
            outbox.run_once(backend=delivery_configured)
            session.expire_all()
        elif setup == "failed":
            _fail_permanently(session, result.outbox_id)
        elif setup == "expired":
            row.secure_payload = None
        session.commit()

        assert sender.delivery_history(session, admin, sendable.id)[0].status_label == (
            expected
        )

    def test_the_summary_reports_the_latest_customer_message(
        self, session, sendable, admin
    ):
        _send(session, admin, sendable)
        session.commit()

        summary = sender.delivery_summary(session, admin, sendable.id)
        assert summary.has_activity
        assert summary.recipient_email == GOOD_ADDRESS
        assert summary.status_label == "Queued"

    def test_the_summary_is_empty_before_anything_is_sent(
        self, session, sendable, admin
    ):
        summary = sender.delivery_summary(session, admin, sendable.id)
        assert not summary.has_activity
        assert summary.status_label == "Not sent"

    def test_customer_response_notifications_appear_in_the_history(
        self, session, sendable, admin, manager, delivery_configured
    ):
        """Phase 6B's transactional messages are visible to employees here."""
        quotation_service.change_status(
            session, manager, sendable, QuotationStatus.SENT_TO_CUSTOMER
        )
        session.flush()
        token, _raw = portal_service.issue_token(session, admin, sendable)
        session.commit()

        portal_service.approve(
            session, token, customer_name="Dana Whitfield",
            customer_email=GOOD_ADDRESS, accepted_terms=True,
        )
        session.commit()

        kinds = {
            r.message_type
            for r in sender.delivery_history(session, admin, sendable.id)
        }
        assert EmailMessageType.CUSTOMER_APPROVAL_CONFIRMATION in kinds
        assert EmailMessageType.INTERNAL_APPROVAL_NOTICE in kinds

    def test_events_do_not_multiply_across_worker_sweeps(
        self, session, sendable, admin, delivery_configured
    ):
        """A quiet sweep must not add a row to the quotation's history."""
        _send(session, admin, sendable)
        session.commit()
        outbox.run_once(backend=delivery_configured)
        session.expire_all()

        before = session.query(QuoteEvent).count()
        for _ in range(3):
            outbox.run_once(backend=delivery_configured)
        session.expire_all()

        assert session.query(QuoteEvent).count() == before


# --------------------------------------------------------------------------- #
# Worker health
# --------------------------------------------------------------------------- #

class TestWorkerHealth:
    def test_unconfigured_is_reported_as_such(self, monkeypatch):
        from modules.worker import HEALTH_FILE_ENV

        monkeypatch.delenv(HEALTH_FILE_ENV, raising=False)
        health = sender.worker_health()

        assert not health.is_configured
        assert health.label == "Not configured"
        assert not health.is_stale

    def test_a_recent_sweep_reads_as_running(self, tmp_path, monkeypatch):
        from modules.worker import HEALTH_FILE_ENV

        signal = tmp_path / "health.txt"
        signal.write_text("2026-08-12T00:00:00+00:00 ok storage=0\n", encoding="utf-8")
        monkeypatch.setenv(HEALTH_FILE_ENV, str(signal))

        health = sender.worker_health()
        assert health.is_configured and health.is_healthy
        assert health.label == "Running"

    def test_an_old_sweep_reads_as_stale(self, tmp_path, monkeypatch):
        import os

        from modules.worker import HEALTH_FILE_ENV

        signal = tmp_path / "health.txt"
        signal.write_text("old ok\n", encoding="utf-8")
        old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
        os.utime(signal, (old.timestamp(), old.timestamp()))
        monkeypatch.setenv(HEALTH_FILE_ENV, str(signal))

        health = sender.worker_health()
        assert health.is_stale
        assert health.label == "Not running recently"

    def test_a_degraded_sweep_is_flagged(self, tmp_path, monkeypatch):
        from modules.worker import HEALTH_FILE_ENV

        signal = tmp_path / "health.txt"
        signal.write_text("now degraded errors=email\n", encoding="utf-8")
        monkeypatch.setenv(HEALTH_FILE_ENV, str(signal))

        assert sender.worker_health().label == "Degraded"

    def test_a_missing_signal_file_is_stale_not_a_crash(self, tmp_path, monkeypatch):
        from modules.worker import HEALTH_FILE_ENV

        monkeypatch.setenv(HEALTH_FILE_ENV, str(tmp_path / "never-written.txt"))
        health = sender.worker_health()

        assert health.is_configured
        assert not health.is_healthy

    def test_health_exposes_no_path_or_worker_identity(self, tmp_path, monkeypatch):
        from dataclasses import fields

        from modules.worker import HEALTH_FILE_ENV

        signal = tmp_path / "health.txt"
        signal.write_text("now ok\n", encoding="utf-8")
        monkeypatch.setenv(HEALTH_FILE_ENV, str(signal))

        names = {f.name for f in fields(sender.WorkerHealth)}
        assert not names & {"path", "file", "owner", "worker_id", "identity"}
        assert str(tmp_path) not in sender.worker_health().detail

    def test_a_stale_worker_warns_but_does_not_block_sending(
        self, session, sendable, admin, tmp_path, monkeypatch
    ):
        """Queueing is the point of a durable outbox; it survives a stopped worker."""
        import os

        from modules.worker import HEALTH_FILE_ENV

        signal = tmp_path / "health.txt"
        signal.write_text("old ok\n", encoding="utf-8")
        old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
        os.utime(signal, (old.timestamp(), old.timestamp()))
        monkeypatch.setenv(HEALTH_FILE_ENV, str(signal))

        eligibility = sender.check_eligibility(
            session, admin, sendable, recipient=GOOD_ADDRESS
        )
        assert eligibility.may_send
        assert "worker_stale" in [w.code for w in eligibility.warnings]

        result = _send(session, admin, sendable)
        session.commit()
        assert session.get(EmailOutbox, result.outbox_id) is not None
