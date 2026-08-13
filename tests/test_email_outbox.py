"""The outbox as a queue: what it promises, and what it honestly does not.

The promises: a business event and its delivery intent commit together or not
at all; a message is queued once however many times it is asked for; a failure
is retried with bounded backoff and a permanent one is not; two workers do not
knowingly send the same row; a sent row is never sent again.

The one thing it does not promise is exactly-once. That boundary is tested
explicitly rather than papered over — see
:meth:`TestAtLeastOnceBoundary.test_a_crash_after_the_provider_accepted_can_duplicate`.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules import (
    email_backend,
    email_notifications,
    email_outbox_service as outbox,
    portal_service,
    quotation_service,
    worker,
)
from modules.constants import (
    INTERNAL_MESSAGES,
    LINK_BEARING_MESSAGES,
    EmailFailureCategory,
    EmailMessageType,
    EmailOutboxStatus,
    ItemInclusion,
    PriceTierCode,
    QuotationStatus,
    QuoteEventType,
)
from modules.models import EmailOutbox, QuoteEvent

from tests.test_documents_and_approval import (  # noqa: F401
    _approve_and_issue,
    admin,
    manager,
    quotation,
    sales,
    variant,
)


@pytest.fixture(autouse=True)
def portal_user(session):
    return portal_service.ensure_portal_user(session)


@pytest.fixture(autouse=True)
def email_enabled(monkeypatch):
    """Turn sending on for this module only.

    The suite defaults to off, matching production, so that tests constructing
    a production ``Settings`` for some other reason do not trip the fail-closed
    email guard. These tests are about the queue, so they need it on — and a
    real base URL, so the link that gets sealed is the shape a customer receives.
    """
    from modules.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "email_enabled", True)
    monkeypatch.setattr(settings, "portal_base_url", "https://quotes.test.invalid")
    return settings


@pytest.fixture(autouse=True)
def capture_backend(monkeypatch):
    """A fresh capture backend per test, and never a real one."""
    backend = email_backend.MemoryBackend()
    monkeypatch.setattr(email_backend, "get_backend", lambda: backend)
    monkeypatch.setattr(outbox, "get_backend", lambda: backend)
    return backend


@pytest.fixture
def offered(session, quotation, sales, variant):
    line = quotation_service.add_line(
        session, sales, quotation,
        product_variant_id=variant.id,
        price_tier_code=PriceTierCode.STANDARD.value,
        quantity_packs=D("100"), description_override="Two-colour print",
    )
    line.inclusion = ItemInclusion.OPTIONAL
    session.flush()
    quotation_service.recompute_totals(session, quotation)
    session.flush()
    return quotation


@pytest.fixture
def sent(session, offered, sales, manager):
    _approve_and_issue(session, offered, sales, manager)
    offered.contact_name = "Dana Whitfield"
    offered.contact_email = "dana@harbour.invalid"
    quotation_service.change_status(
        session, manager, offered, QuotationStatus.SENT_TO_CUSTOMER
    )
    session.commit()
    return offered


@pytest.fixture
def invited(session, sent, sales):
    """A link issued and its invitation queued, as one act."""
    token, raw = portal_service.issue_and_queue_invitation(session, sales, sent)
    session.commit()
    return token, raw


def _rows(session) -> list[EmailOutbox]:
    return session.query(EmailOutbox).order_by(EmailOutbox.id).all()


# --------------------------------------------------------------------------- #
# Enqueueing
# --------------------------------------------------------------------------- #

class TestEnqueue:
    def test_issuing_a_link_queues_its_invitation(self, session, invited):
        _token, raw = invited
        rows = _rows(session)

        assert len(rows) == 1
        row = rows[0]
        assert row.message_type is EmailMessageType.QUOTE_INVITATION
        assert row.status is EmailOutboxStatus.QUEUED
        assert row.recipient_email == "dana@harbour.invalid"
        assert row.subject
        # The link is present, and present only sealed.
        assert row.secure_payload
        assert raw not in row.secure_payload
        assert raw not in (row.subject or "")
        assert raw not in str(row.template_data_json)

    def test_the_row_binds_the_exact_revision(self, session, invited, sent):
        row = _rows(session)[0]
        assert row.revision_no == sent.revision_no
        assert row.template_data_json["revision_label"] == sent.revision_label

    def test_a_revised_invitation_is_its_own_message(self, session, sent, sales):
        portal_service.issue_and_queue_invitation(session, sales, sent)
        portal_service.issue_and_queue_invitation(
            session, sales, sent, revised=True,
            previous_revision_label="Rev 0",
            change_summary="Reduced the pallet quantity.",
        )
        session.commit()

        kinds = {row.message_type for row in _rows(session)}
        assert kinds == {
            EmailMessageType.QUOTE_INVITATION,
            EmailMessageType.QUOTE_REVISED_INVITATION,
        }

    def test_an_invitation_quotes_the_base_total(self, session, invited, sent):
        """An offer quotes the minimum, never the all-options ceiling."""
        from modules import pricing_snapshot

        row = _rows(session)[0]
        base = pricing_snapshot.base(sent)
        ceiling = pricing_snapshot.all_options(sent)

        assert f"{base.grand_total:,.2f}" in row.template_data_json["total_display"]
        assert f"{ceiling.grand_total:,.2f}" not in row.template_data_json["total_display"]
        assert row.template_data_json["has_optional_items"] is True

    def test_queueing_the_same_message_twice_is_a_no_op(self, session, sent, sales):
        first = email_notifications.queue_invitation(
            session, sent, "https://quotes.test.invalid/quote/public/A",
            discriminator="same",
        )
        session.flush()
        second = email_notifications.queue_invitation(
            session, sent, "https://quotes.test.invalid/quote/public/B",
            discriminator="same",
        )
        session.commit()

        assert first.id == second.id
        assert len(_rows(session)) == 1

    def test_a_duplicate_within_one_transaction_is_caught_before_flush(
        self, session, sent
    ):
        """An unflushed row is invisible to a SELECT; the pending set is checked."""
        a = email_notifications.queue_changes_confirmation(
            session, sent, _fake_response(sent)
        )
        b = email_notifications.queue_changes_confirmation(
            session, sent, _fake_response(sent)
        )
        session.commit()
        assert a is b
        assert len(_rows(session)) == 1

    def test_an_invalid_recipient_refuses_before_commit(self, session, sent):
        sent.contact_email = "not-an-address"
        session.flush()
        with pytest.raises(outbox.OutboxError):
            email_notifications.queue_invitation(
                session, sent, "https://quotes.test.invalid/quote/public/X"
            )

    def test_an_invitation_without_a_link_is_refused(self, session, sent):
        with pytest.raises(outbox.OutboxError):
            outbox.enqueue(
                session,
                message_type=EmailMessageType.QUOTE_INVITATION,
                quotation=sent, recipient_email="d@x.invalid",
            )

    def test_a_non_invitation_with_a_link_is_refused(self, session, sent):
        with pytest.raises(outbox.OutboxError):
            outbox.enqueue(
                session,
                message_type=EmailMessageType.INTERNAL_APPROVAL_NOTICE,
                quotation=sent, recipient_email="ops@test.invalid",
                secure_url="https://quotes.test.invalid/quote/public/X",
            )

    def test_queueing_records_an_event_that_is_not_a_view(self, session, invited):
        kinds = [e.event_type for e in session.query(QuoteEvent).all()]
        assert QuoteEventType.EMAIL_QUEUED in kinds
        assert QuoteEventType.VIEWED not in kinds


def _fake_response(quotation):  # noqa: ANN001, ANN202
    from modules.models import PortalResponse
    from modules.constants import PortalResponseType

    return PortalResponse(
        quotation_id=quotation.id, revision_no=quotation.revision_no,
        response_type=PortalResponseType.CHANGES_REQUESTED,
        customer_name="Dana", customer_email="dana@harbour.invalid",
        comment="Cheaper please", currency=quotation.currency,
    )


# --------------------------------------------------------------------------- #
# Transaction boundaries
# --------------------------------------------------------------------------- #

class TestAtomicWithTheBusinessEvent:
    def test_acceptance_queues_its_messages_in_the_same_transaction(
        self, session, invited, sent
    ):
        token, _raw = invited
        optional = next(
            i for i in sent.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        response = portal_service.approve(
            session, token, customer_name="Dana Whitfield",
            customer_email="dana@harbour.invalid", accepted_terms=True,
            selected_ids=[optional.id],
        )
        session.commit()

        kinds = {row.message_type for row in _rows(session)}
        assert EmailMessageType.CUSTOMER_APPROVAL_CONFIRMATION in kinds
        assert EmailMessageType.INTERNAL_APPROVAL_NOTICE in kinds

        confirmation = next(
            r for r in _rows(session)
            if r.message_type is EmailMessageType.CUSTOMER_APPROVAL_CONFIRMATION
        )
        assert confirmation.portal_response_id == response.id

    def test_a_confirmation_quotes_the_accepted_total_not_a_repriced_one(
        self, session, invited, sent
    ):
        token, _raw = invited
        optional = next(
            i for i in sent.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        response = portal_service.approve(
            session, token, customer_name="Dana",
            customer_email="dana@harbour.invalid", accepted_terms=True,
            selected_ids=[optional.id],
        )
        session.commit()

        row = next(
            r for r in _rows(session)
            if r.message_type is EmailMessageType.CUSTOMER_APPROVAL_CONFIRMATION
        )
        assert row.template_data_json["total_label"] == "Accepted total"
        assert f"{response.grand_total:,.2f}" in row.template_data_json["total_display"]

    def test_a_change_request_queues_its_messages(self, session, invited, sent):
        token, _raw = invited
        portal_service.request_changes(
            session, token, customer_name="Dana",
            customer_email="dana@harbour.invalid", comment="Cheaper please",
        )
        session.commit()

        kinds = {row.message_type for row in _rows(session)}
        assert EmailMessageType.CUSTOMER_CHANGES_CONFIRMATION in kinds
        assert EmailMessageType.INTERNAL_CHANGES_NOTICE in kinds

    def test_a_failed_enqueue_rolls_the_business_event_back(
        self, session, invited, sent, monkeypatch
    ):
        """A required notification that cannot be recorded stops the event."""
        token, _raw = invited

        def refuse(*_args, **_kwargs):
            raise outbox.OutboxError("cannot queue")

        monkeypatch.setattr(email_notifications, "queue_approval_confirmation", refuse)

        with pytest.raises(outbox.OutboxError):
            portal_service.approve(
                session, token, customer_name="Dana",
                customer_email="dana@harbour.invalid", accepted_terms=True,
            )
        session.rollback()

        assert sent.status is not QuotationStatus.ACCEPTED
        assert sent.portal_responses == []

    def test_no_send_happens_inside_the_business_transaction(
        self, session, invited, sent, capture_backend
    ):
        """The queue is written; the wire is untouched until a worker runs."""
        token, _raw = invited
        portal_service.approve(
            session, token, customer_name="Dana",
            customer_email="dana@harbour.invalid", accepted_terms=True,
        )
        session.commit()

        assert capture_backend.sent == []
        assert all(r.status is EmailOutboxStatus.QUEUED for r in _rows(session))

    def test_an_acceptance_without_an_address_still_succeeds(
        self, session, invited, sent
    ):
        """A receipt nobody can be sent must not block the order."""
        token, _raw = invited
        sent.contact_email = None
        session.flush()

        response = portal_service.approve(
            session, token, customer_name="Dana", accepted_terms=True,
        )
        session.commit()

        assert response.id is not None
        assert sent.status is QuotationStatus.ACCEPTED
        customer_rows = [
            r for r in _rows(session)
            if r.message_type is EmailMessageType.CUSTOMER_APPROVAL_CONFIRMATION
        ]
        assert customer_rows == []
        # The internal notice still goes: the team needs to know either way.
        assert any(r.message_type in INTERNAL_MESSAGES for r in _rows(session))


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

class TestSending:
    def test_a_queued_message_is_sent_and_marked(
        self, session, invited, capture_backend
    ):
        row = _rows(session)[0]
        assert outbox.process_one(session, row, backend=capture_backend) is (
            EmailOutboxStatus.SENT
        )
        session.commit()

        assert row.status is EmailOutboxStatus.SENT
        assert row.sent_at is not None
        assert row.attempts == 1
        assert row.provider_message_id
        assert len(capture_backend.sent) == 1

    def test_the_sent_message_carries_the_link_from_the_sealed_payload(
        self, session, invited, capture_backend
    ):
        _token, raw = invited
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        message = capture_backend.sent[0]
        assert raw in message.html_body
        assert raw in message.text_body

    def test_the_payload_is_erased_once_sent(self, session, invited, capture_backend):
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        assert row.secure_payload is None
        assert row.secure_payload_expires_at is None

    def test_sending_records_an_event_that_is_not_a_view(
        self, session, invited, capture_backend
    ):
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        kinds = [e.event_type for e in session.query(QuoteEvent).all()]
        assert QuoteEventType.EMAIL_SENT in kinds
        assert QuoteEventType.VIEWED not in kinds

    def test_a_sent_row_is_never_claimed_again(self, session, invited, capture_backend):
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        claimed = outbox.claim_batch(session, owner="w-second", limit=10)
        assert claimed == []

        counts = outbox.run_once(backend=capture_backend)
        assert counts["sent"] == 0
        assert len(capture_backend.sent) == 1

    def test_run_once_sends_the_whole_batch(self, session, invited, sent, capture_backend):
        token, _raw = invited
        portal_service.approve(
            session, token, customer_name="Dana",
            customer_email="dana@harbour.invalid", accepted_terms=True,
        )
        session.commit()

        counts = outbox.run_once(backend=capture_backend)
        session.expire_all()

        assert counts["sent"] == len(_rows(session))
        assert all(r.status is EmailOutboxStatus.SENT for r in _rows(session))

    def test_sending_is_skipped_while_email_is_disabled(
        self, session, invited, capture_backend, monkeypatch
    ):
        """Rows accumulate rather than being dropped: turning it on delivers them."""
        from modules.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "email_enabled", False)

        counts = outbox.run_once(backend=capture_backend)
        session.expire_all()

        assert counts["skipped"] == 1
        assert capture_backend.sent == []
        assert _rows(session)[0].status is EmailOutboxStatus.QUEUED


# --------------------------------------------------------------------------- #
# Failure and retry
# --------------------------------------------------------------------------- #

class TestRetry:
    def test_a_temporary_failure_is_scheduled_again(
        self, session, invited, capture_backend
    ):
        capture_backend.fail_with = email_backend.EmailDeliveryError(
            "greylisted", temporary=True, code="smtp_451",
        )
        row = _rows(session)[0]
        status = outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        assert status is EmailOutboxStatus.QUEUED
        assert row.attempts == 1
        assert row.failure_category is EmailFailureCategory.TEMPORARY
        assert row.failure_code == "smtp_451"
        assert row.next_attempt_at is not None
        assert row.sent_at is None

    def test_a_permanent_failure_is_not_retried(
        self, session, invited, capture_backend
    ):
        capture_backend.fail_with = email_backend.EmailDeliveryError(
            "no such mailbox", temporary=False, code="smtp_550",
        )
        row = _rows(session)[0]
        status = outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        assert status is EmailOutboxStatus.FAILED
        assert row.failure_category is EmailFailureCategory.PERMANENT
        assert row.next_attempt_at is None
        assert row.failed_at is not None
        assert outbox.claim_batch(session, owner="w", limit=10) == []

    def test_retries_are_bounded(self, session, invited, capture_backend):
        from modules.config import get_settings

        capture_backend.fail_with = email_backend.EmailDeliveryError(
            "still down", temporary=True, code="smtp_451",
        )
        row = _rows(session)[0]
        maximum = get_settings().email_max_attempts

        for _ in range(maximum):
            row.next_attempt_at = None       # skip the wait
            row.status = EmailOutboxStatus.QUEUED
            outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        assert row.attempts == maximum
        assert row.status is EmailOutboxStatus.FAILED

    def test_a_retry_succeeds_once_the_provider_returns(
        self, session, invited, capture_backend
    ):
        capture_backend.fail_with = email_backend.EmailDeliveryError(
            "down", temporary=True, code="smtp_451",
        )
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()
        assert row.status is EmailOutboxStatus.QUEUED

        capture_backend.fail_with = None
        row.status = EmailOutboxStatus.QUEUED
        assert outbox.process_one(session, row, backend=capture_backend) is (
            EmailOutboxStatus.SENT
        )
        assert row.failure_code is None
        assert row.attempts == 2

    def test_a_rendering_failure_is_permanent(self, session, invited, capture_backend):
        """The same data will fail the same way; retrying helps nobody."""
        row = _rows(session)[0]
        row.template_data_json = {"quote_number": "QT-1"}    # missing everything else
        session.flush()

        status = outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        assert status is EmailOutboxStatus.FAILED
        assert row.failure_category is EmailFailureCategory.PERMANENT
        assert capture_backend.sent == []

    def test_an_unexpected_backend_fault_is_temporary(
        self, session, invited, capture_backend, monkeypatch
    ):
        def explode(_message):
            raise RuntimeError("something unforeseen")

        monkeypatch.setattr(capture_backend, "send", explode)
        row = _rows(session)[0]
        status = outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        assert status is EmailOutboxStatus.QUEUED
        assert row.failure_code == "backend_error"

    def test_the_failure_code_is_a_token_not_provider_text(
        self, session, invited, capture_backend
    ):
        capture_backend.fail_with = email_backend.EmailDeliveryError(
            "550 5.1.1 <dana@harbour.invalid>: Recipient address rejected",
            temporary=False, code="smtp_550",
        )
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        assert row.failure_code == "smtp_550"
        assert "dana@harbour.invalid" not in (row.failure_code or "")
        assert "Recipient address rejected" not in (row.failure_code or "")


class TestBackoff:
    def test_the_delay_grows_and_is_bounded(self):
        delays = [outbox.backoff_seconds(n, jitter=False) for n in range(1, 12)]
        assert delays == sorted(delays)
        assert all(d <= outbox.MAX_BACKOFF_SECONDS for d in delays)
        assert delays[-1] == outbox.MAX_BACKOFF_SECONDS

    def test_the_first_retry_is_not_immediate(self):
        assert outbox.backoff_seconds(1, jitter=False) >= 60

    def test_jitter_spreads_retries_without_shortening_them(self):
        """Every message retrying in the same second re-breaks a recovering provider."""
        samples = {outbox.backoff_seconds(2) for _ in range(60)}
        base = outbox.backoff_seconds(2, jitter=False)

        assert len(samples) > 1
        assert min(samples) >= base
        assert max(samples) <= base * (1 + outbox.JITTER_RATIO) + 1


# --------------------------------------------------------------------------- #
# Leasing and concurrency
# --------------------------------------------------------------------------- #

class TestLeasing:
    def test_claiming_marks_a_row_sending(self, session, invited):
        claimed = outbox.claim_batch(session, owner="w-1", limit=10)
        session.commit()

        assert len(claimed) == 1
        assert claimed[0].status is EmailOutboxStatus.SENDING
        assert claimed[0].lease_owner == "w-1"
        assert claimed[0].lease_expires_at is not None

    def test_a_second_worker_does_not_claim_a_leased_row(self, session, invited):
        first = outbox.claim_batch(session, owner="w-1", limit=10)
        session.commit()
        second = outbox.claim_batch(session, owner="w-2", limit=10)

        assert len(first) == 1
        assert second == []

    def test_an_expired_lease_is_reclaimed(self, session, invited):
        """A worker killed mid-send must not strand a message forever."""
        claimed = outbox.claim_batch(session, owner="w-dead", limit=10)
        session.commit()

        claimed[0].lease_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
        session.commit()

        recovered = outbox.claim_batch(session, owner="w-alive", limit=10)
        assert len(recovered) == 1
        assert recovered[0].id == claimed[0].id
        assert recovered[0].lease_owner == "w-alive"

    def test_a_row_scheduled_for_later_is_not_claimed_yet(self, session, invited):
        row = _rows(session)[0]
        row.next_attempt_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)
        session.commit()

        assert outbox.claim_batch(session, owner="w-1", limit=10) == []

    def test_a_batch_is_limited(self, session, sent, sales):
        for n in range(5):
            portal_service.issue_and_queue_invitation(session, sales, sent)
        session.commit()
        assert len(_rows(session)) == 5

        claimed = outbox.claim_batch(session, owner="w-1", limit=2)
        assert len(claimed) == 2

    def test_concurrent_workers_do_not_both_send_a_row(
        self, session, invited, capture_backend
    ):
        """The compare-and-swap decides; only one worker gets the row."""
        first = outbox.claim_batch(session, owner="w-1", limit=10)
        second = outbox.claim_batch(session, owner="w-2", limit=10)

        assert len(first) == 1 and second == []

        outbox.process_one(session, first[0], backend=capture_backend)
        session.commit()
        assert len(capture_backend.sent) == 1

        # And the second worker, sweeping now, finds nothing to do.
        assert outbox.run_once(backend=capture_backend)["sent"] == 0
        assert len(capture_backend.sent) == 1

    def test_postgres_uses_skip_locked_and_sqlite_does_not(self):
        """The clause is dialect-conditional, not universal."""
        import inspect

        source = inspect.getsource(outbox.claim_batch)
        assert "skip_locked=True" in source
        assert 'dialect.name == "postgresql"' in source


class TestAtLeastOnceBoundary:
    def test_a_crash_after_the_provider_accepted_can_duplicate(
        self, session, invited, capture_backend
    ):
        """The residual duplicate, demonstrated rather than claimed away.

        The provider takes the message, then the process dies before the row is
        marked SENT. A later sweep retries and the recipient gets two copies.
        Nothing in SMTP closes this: there is no way to ask a server whether it
        already accepted something.
        """
        row = _rows(session)[0]
        row_id = row.id

        # The provider accepts...
        capture_backend.send(outbox._build_message(session, row))
        assert len(capture_backend.sent) == 1
        # ...and the process dies here, before the row is updated.
        session.rollback()

        session.expire_all()
        recovered = session.get(EmailOutbox, row_id)
        assert recovered.status is EmailOutboxStatus.QUEUED

        outbox.process_one(session, recovered, backend=capture_backend)
        session.commit()

        # Two copies delivered. This is the documented boundary.
        assert len(capture_backend.sent) == 2
        assert recovered.status is EmailOutboxStatus.SENT

    def test_what_narrows_it_is_present(self, session, invited, capture_backend):
        """A generated Message-ID lets a well-behaved server collapse duplicates."""
        import inspect

        assert "make_msgid" in inspect.getsource(email_backend.SmtpBackend._build)
        # And duplicate *queueing* — the common cause — is prevented outright.
        row = _rows(session)[0]
        assert row.idempotency_key


# --------------------------------------------------------------------------- #
# Payload lifetime
# --------------------------------------------------------------------------- #

class TestPayloadExpiry:
    def test_an_expired_payload_is_erased_by_the_sweep(self, session, invited):
        row = _rows(session)[0]
        row.secure_payload_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        session.commit()

        assert outbox.expire_stale_payloads(session) == 1
        assert row.secure_payload is None

    def test_a_live_payload_is_left_alone(self, session, invited):
        row = _rows(session)[0]
        assert outbox.expire_stale_payloads(session) == 0
        assert row.secure_payload is not None

    def test_sending_after_expiry_fails_permanently_rather_than_sending(
        self, session, invited, capture_backend
    ):
        row = _rows(session)[0]
        row.secure_payload_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        session.flush()

        status = outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        assert status is EmailOutboxStatus.FAILED
        assert row.failure_code == "link_expired"
        assert capture_backend.sent == []
        assert row.secure_payload is None

    def test_an_invitation_whose_payload_is_gone_cannot_be_resent(
        self, session, invited
    ):
        row = _rows(session)[0]
        row.secure_payload = None
        row.status = EmailOutboxStatus.FAILED
        session.flush()

        with pytest.raises(outbox.OutboxError) as caught:
            outbox.reset_failed(session, row.id)
        assert "new customer link" in str(caught.value)

    def test_a_tampered_payload_fails_permanently(
        self, session, invited, capture_backend
    ):
        row = _rows(session)[0]
        row.quotation_id = row.quotation_id     # unchanged
        row.revision_no = row.revision_no + 1   # but the binding no longer matches
        session.flush()

        status = outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        assert status is EmailOutboxStatus.FAILED
        assert row.failure_code == "link_unsealable"
        assert capture_backend.sent == []


# --------------------------------------------------------------------------- #
# What Phase 6C will show
# --------------------------------------------------------------------------- #

class TestEmployeeVisibility:
    def test_entries_report_status_without_internals(
        self, session, invited, capture_backend
    ):
        entries = outbox.entries_for_quotation(session, _rows(session)[0].quotation_id)
        assert len(entries) == 1

        entry = entries[0]
        assert entry.status is EmailOutboxStatus.QUEUED
        assert entry.status_label == "Queued"
        assert entry.message_label == "Quotation sent"
        assert entry.recipient_email == "dana@harbour.invalid"

        # No route to the payload, the template data or a raw error.
        for absent in ("secure_payload", "template_data", "brand_snapshot", "last_error"):
            assert not hasattr(entry, absent)

    def test_a_scheduled_retry_is_distinguishable_from_a_fresh_queue(
        self, session, invited, capture_backend
    ):
        capture_backend.fail_with = email_backend.EmailDeliveryError(
            "later", temporary=True, code="smtp_451",
        )
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        entry = outbox.entry_for(session, row.id)
        assert entry.status is EmailOutboxStatus.QUEUED
        assert entry.is_retry_scheduled
        assert entry.attempts == 1

    def test_a_permanent_failure_reads_as_failed(
        self, session, invited, capture_backend
    ):
        capture_backend.fail_with = email_backend.EmailDeliveryError(
            "no", temporary=False, code="smtp_550",
        )
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        entry = outbox.entry_for(session, row.id)
        assert entry.is_permanently_failed
        assert entry.status_label == "Failed"

    def test_a_failed_message_can_be_reset(self, session, invited, capture_backend):
        capture_backend.fail_with = email_backend.EmailDeliveryError(
            "later", temporary=False, code="smtp_550",
        )
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        entry = outbox.reset_failed(session, row.id)
        session.commit()

        assert entry.status is EmailOutboxStatus.QUEUED
        assert entry.attempts == 0
        assert row.failure_code is None

    def test_a_sent_message_is_never_reset(self, session, invited, capture_backend):
        row = _rows(session)[0]
        outbox.process_one(session, row, backend=capture_backend)
        session.commit()

        with pytest.raises(outbox.OutboxError):
            outbox.reset_failed(session, row.id)


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #

class TestWorker:
    def test_a_sweep_sends_what_is_queued(self, session, invited, capture_backend):
        result = worker.run_sweep()
        session.expire_all()

        assert result.healthy
        assert result.emails_sent == 1
        assert _rows(session)[0].status is EmailOutboxStatus.SENT

    def test_a_sweep_also_produces_accepted_pdfs(self, session, invited, sent):
        """The PDF job no longer waits for somebody to press a button."""
        from modules import quote_document_service

        token, _raw = invited
        response = portal_service.approve(
            session, token, customer_name="Dana",
            customer_email="dana@harbour.invalid", accepted_terms=True,
        )
        session.commit()

        result = worker.run_sweep()
        session.expire_all()

        assert result.documents_ready == 1
        assert quote_document_service.artifact_for_response(session, response.id)

    def test_one_subsystem_failing_does_not_stop_the_others(
        self, session, invited, monkeypatch, capture_backend
    ):
        def explode(*_args, **_kwargs):
            raise RuntimeError("the document renderer is down")

        monkeypatch.setattr(worker, "sweep_document_jobs", explode)
        result = worker.run_sweep()
        session.expire_all()

        assert "documents" in result.errors
        assert not result.healthy
        # Email still went out.
        assert result.emails_sent == 1
        assert _rows(session)[0].status is EmailOutboxStatus.SENT

    def test_a_storage_failure_does_not_stop_email(
        self, session, invited, monkeypatch, capture_backend
    ):
        def explode(*_args, **_kwargs):
            raise RuntimeError("storage is unreachable")

        monkeypatch.setattr(worker, "sweep_storage_cleanups", explode)
        result = worker.run_sweep()
        session.expire_all()

        assert result.errors == ["storage"]
        assert result.emails_sent == 1

    def test_every_subsystem_can_fail_independently(self, session, monkeypatch):
        for name in ("sweep_storage_cleanups", "sweep_document_jobs", "sweep_email_outbox"):
            monkeypatch.setattr(
                worker, name,
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
            )
        result = worker.run_sweep()

        assert set(result.errors) == {"storage", "documents", "email"}
        # And it still returned rather than propagating.
        assert result.finished_at is not None

    def test_one_shot_mode_returns_zero_when_healthy(self, session, capture_backend):
        assert worker.main(["--once"]) == 0

    def test_one_shot_mode_returns_non_zero_when_a_sweep_failed(
        self, session, monkeypatch
    ):
        """So a scheduler can alert on it."""
        monkeypatch.setattr(
            worker, "sweep_email_outbox",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
        )
        assert worker.main(["--once"]) == 1

    def test_continuous_mode_stops_after_the_requested_sweeps(
        self, session, capture_backend
    ):
        instance = worker.Worker(poll_seconds=5, batch_size=5)
        assert instance.run_forever(max_sweeps=2) == 2

    def test_a_stop_request_ends_the_loop_promptly(self, session, capture_backend):
        """Checked between sweeps, so a shutdown does not strand leased rows."""
        import threading
        import time

        instance = worker.Worker(poll_seconds=30, batch_size=5)
        threading.Timer(0.2, instance.request_stop).start()

        started = time.monotonic()
        sweeps = instance.run_forever()
        elapsed = time.monotonic() - started

        assert instance.stopping
        assert sweeps >= 1
        assert elapsed < 25, "the stop flag did not interrupt the poll interval"

    def test_the_worker_imports_no_web_framework(self):
        """It runs as its own process, wherever a timer can actually live."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(worker))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert "streamlit" not in imported
        assert "fastapi" not in imported

    def test_the_health_file_records_a_sweep(self, session, tmp_path, monkeypatch):
        health = tmp_path / "health.txt"
        monkeypatch.setenv(worker.HEALTH_FILE_ENV, str(health))

        worker.run_sweep()

        content = health.read_text(encoding="utf-8")
        assert "ok" in content
        assert "emails=" in content

    def test_the_health_file_says_degraded_after_a_failure(
        self, session, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(worker.HEALTH_FILE_ENV, str(tmp_path / "h.txt"))
        monkeypatch.setattr(
            worker, "sweep_email_outbox",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
        )
        worker.run_sweep()

        assert "degraded" in (tmp_path / "h.txt").read_text(encoding="utf-8")

    def test_the_health_file_names_nobody(self, session, tmp_path, monkeypatch):
        """Monitoring has no business seeing who was emailed."""
        monkeypatch.setenv(worker.HEALTH_FILE_ENV, str(tmp_path / "h.txt"))
        worker.run_sweep()

        content = (tmp_path / "h.txt").read_text(encoding="utf-8")
        assert "@" not in content
