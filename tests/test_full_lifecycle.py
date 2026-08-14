"""The whole thing, once, in order.

Every phase has its own tests and they all pass in isolation. This asks the
question none of them can: does a quotation actually make it from an internal
approval to a signed PDF in the customer's hands, with the totals and the
revision still meaning what they meant at the start?

The sequence is the real one — approve, invite, deliver, view, request changes,
revise, re-invite, accept, confirm, produce the accepted document — with the
capture backend standing in for a mail server and nothing reaching the network.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal as D

import pytest

from modules import (
    approval_service,
    email_backend,
    email_outbox_service as outbox,
    portal_service,
    pricing_snapshot,
    quotation_service,
    quote_document_service,
    quote_send_service as sender,
    revision_service,
    worker,
)
from modules.constants import (
    DocumentJobStatus,
    EmailMessageType,
    EmailOutboxStatus,
    ItemInclusion,
    PriceTierCode,
    QuotationStatus,
    QuoteEventType,
)
from modules.models import EmailOutbox, QuotationTerm, QuoteEvent

from tests.test_documents_and_approval import (  # noqa: F401
    admin,
    manager,
    quotation,
    sales,
    variant,
)

CUSTOMER = "dana@harbourfoods.co.uk"


@pytest.fixture(autouse=True)
def portal_user(session):
    return portal_service.ensure_portal_user(session)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing in this test may open a socket."""
    import socket

    def refuse(*_args, **_kwargs):
        raise AssertionError("the lifecycle attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def mail(monkeypatch):
    from modules.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "email_enabled", True)
    monkeypatch.setattr(settings, "portal_base_url", "https://quotes.test.invalid")
    monkeypatch.setattr(settings, "email_from_address", "quotes@northwind.invalid")
    monkeypatch.setattr(settings, "email_internal_recipients", "ops@northwind.invalid")

    backend = email_backend.MemoryBackend()
    monkeypatch.setattr(email_backend, "get_backend", lambda: backend)
    monkeypatch.setattr(outbox, "get_backend", lambda: backend)
    return backend


def _link_from(message) -> str:  # noqa: ANN001
    """Pull the customer's URL out of the delivered message, as they would."""
    import re

    found = re.search(r"https://\S*/quote/public/[A-Za-z0-9_-]+", message.text_body)
    assert found, "the invitation carried no link"
    return found.group(0)


def _token_from(link: str) -> str:
    return link.rsplit("/", 1)[-1]


class TestFullLifecycle:
    def test_quotation_to_signed_document(
        self, session, quotation, sales, manager, admin, variant, mail
    ):
        # ---------------------------------------------------------------- #
        # 1. A quotation with an optional line, terms and a validity date
        # ---------------------------------------------------------------- #
        optional = quotation_service.add_line(
            session, sales, quotation,
            product_variant_id=variant.id,
            price_tier_code=PriceTierCode.STANDARD.value,
            quantity_packs=D("100"), description_override="Two-colour print",
        )
        optional.inclusion = ItemInclusion.OPTIONAL
        quotation.contact_name = "Dana Whitfield"
        quotation.contact_email = CUSTOMER
        quotation.valid_until = dt.date.today() + dt.timedelta(days=30)
        quotation.tax_rate_pct = D("13")
        quotation.deposit_pct = D("25")
        session.add(QuotationTerm(
            quotation_id=quotation.id, title="1. Payment", body_text="Net 30.",
            sort_order=0, is_customer_visible=True, section="PAYMENT_TERMS",
        ))
        session.flush()
        quotation_service.recompute_totals(session, quotation)
        session.commit()

        base_at_start = pricing_snapshot.base(quotation).grand_total
        assert quotation.grand_total == base_at_start

        # ---------------------------------------------------------------- #
        # 2. Internal approval
        # ---------------------------------------------------------------- #
        approval_service.submit(session, quotation, sales, note="lifecycle")
        if quotation.status is QuotationStatus.DRAFT:
            quotation_service.change_status(
                session, manager, quotation, QuotationStatus.APPROVED
            )
        session.commit()
        assert quotation.status is QuotationStatus.APPROVED

        # A draft could not have got here.
        assert sender.check_eligibility(
            session, admin, quotation, recipient=CUSTOMER
        ).may_send

        # ---------------------------------------------------------------- #
        # 3. The employee sends the initial invitation
        # ---------------------------------------------------------------- #
        first_send = sender.send(
            session, sales, quotation.id,
            message_type=EmailMessageType.QUOTE_INVITATION,
            recipient_email=CUSTOMER, recipient_name="Dana Whitfield",
            expected_revision_no=quotation.revision_no,
        )
        session.commit()

        assert mail.sent == [], "sending must queue, not deliver"
        assert session.get(EmailOutbox, first_send.outbox_id).status is (
            EmailOutboxStatus.QUEUED
        )

        # ---------------------------------------------------------------- #
        # 4. The worker delivers it
        # ---------------------------------------------------------------- #
        assert worker.run_sweep().emails_sent == 1
        session.expire_all()

        invitation = mail.sent[0]
        assert invitation.to_email == CUSTOMER
        assert f"{base_at_start:,.2f}" in invitation.text_body
        link = _link_from(invitation)

        history = sender.delivery_history(session, admin, quotation.id)
        assert history[0].is_sent
        assert history[0].queued_by == sales.username

        # ---------------------------------------------------------------- #
        # 5. The customer opens it
        # ---------------------------------------------------------------- #
        # Sending is what puts a quotation in front of a customer, so sending
        # is what must set the status. This used to be a change_status call
        # here, and that hand-hold is exactly why the gap survived: send() only
        # locked the quotation and left it APPROVED, RESPONDABLE_STATUSES is
        # {SENT_TO_CUSTOMER}, and every real approval came back 400 while this
        # test stayed green because it made the transition itself.
        assert quotation.status is QuotationStatus.SENT_TO_CUSTOMER, (
            "send() must move the quotation into the only status a customer "
            "may respond to"
        )

        token = portal_service.resolve_token(session, _token_from(link))
        portal_service.record_view(session, token)
        session.commit()
        assert token.view_count == 1

        # ---------------------------------------------------------------- #
        # 6. They ask for changes
        # ---------------------------------------------------------------- #
        portal_service.request_changes(
            session, token, customer_name="Dana Whitfield",
            customer_email=CUSTOMER, comment="Could we reduce the quantity?",
        )
        session.commit()
        assert quotation.status is QuotationStatus.REVISION_REQUIRED

        queued_now = {r.message_type for r in _rows(session)}
        assert EmailMessageType.CUSTOMER_CHANGES_CONFIRMATION in queued_now
        assert EmailMessageType.INTERNAL_CHANGES_NOTICE in queued_now

        session.commit()
        worker.run_sweep()
        session.expire_all()
        assert {m.to_email for m in mail.sent} == {CUSTOMER, "ops@northwind.invalid"}
        # The internal notice never carries the customer's way in.
        internal = next(m for m in mail.sent if m.to_email == "ops@northwind.invalid")
        assert "quote/public" not in internal.text_body
        assert "quote/public" not in internal.html_body

        # ---------------------------------------------------------------- #
        # 7. A new revision, priced differently
        # ---------------------------------------------------------------- #
        revised = revision_service.create_revision(
            session, manager, quotation, reason="Customer asked for less"
        )
        session.commit()

        assert revised.revision_no == quotation.revision_no + 1
        assert revised.is_current_revision
        assert not quotation.is_current_revision

        target = next(
            i for i in revised.items if i.inclusion is ItemInclusion.INCLUDED
        )
        quotation_service.update_line(
            session, manager, revised, target.id, quantity_packs=D("60")
        )
        revised.contact_email = CUSTOMER
        revised.valid_until = dt.date.today() + dt.timedelta(days=30)
        session.flush()
        quotation_service.recompute_totals(session, revised)
        approval_service.submit(session, revised, manager, note="revised")
        if revised.status is QuotationStatus.DRAFT:
            quotation_service.change_status(
                session, manager, revised, QuotationStatus.APPROVED
            )
        session.commit()

        base_after_revision = pricing_snapshot.base(revised).grand_total
        assert base_after_revision != base_at_start
        assert revised.grand_total == base_after_revision

        # The superseded revision can no longer be sent.
        assert not sender.check_eligibility(
            session, admin, quotation, recipient=CUSTOMER
        ).may_send

        # ---------------------------------------------------------------- #
        # 8. The revised invitation, with a new link that kills the old one
        # ---------------------------------------------------------------- #
        second_send = sender.send(
            session, sales, revised.id,
            message_type=EmailMessageType.QUOTE_REVISED_INVITATION,
            recipient_email=CUSTOMER, recipient_name="Dana Whitfield",
            previous_revision_label=quotation.revision_label,
            change_summary="Reduced the quantity as requested.",
            revoke_existing=True,
            expected_revision_no=revised.revision_no,
        )
        session.commit()

        assert second_send.token_id != first_send.token_id
        # The customer's first link is dead. This is what the employee was warned of.
        with pytest.raises(portal_service.PortalAccessError):
            portal_service.resolve_token(session, _token_from(link))
        # Release before the worker runs: it opens its own connection, and on
        # SQLite an open read transaction here blocks its writes.
        session.commit()

        before = len(mail.sent)
        worker.run_sweep()
        session.expire_all()

        revised_message = mail.sent[before]
        assert "revised" in revised_message.subject.lower()
        assert f"{base_after_revision:,.2f}" in revised_message.text_body
        new_link = _link_from(revised_message)
        assert new_link != link

        # ---------------------------------------------------------------- #
        # 9. The customer accepts, taking the optional line
        # ---------------------------------------------------------------- #
        # Same again for the revision: resending is what makes it respondable.
        assert revised.status is QuotationStatus.SENT_TO_CUSTOMER

        new_token = portal_service.resolve_token(session, _token_from(new_link))
        chosen = [
            i.id for i in revised.items if i.inclusion is ItemInclusion.OPTIONAL
        ]
        expected_total = pricing_snapshot.selected(revised, chosen).grand_total

        response = portal_service.approve(
            session, new_token,
            customer_name="Dana Whitfield", job_title="Procurement Lead",
            customer_email=CUSTOMER, signature_name="Dana R. Whitfield",
            accepted_terms=True, selected_ids=chosen,
        )
        session.commit()

        assert revised.status is QuotationStatus.ACCEPTED
        assert response.grand_total == expected_total
        assert response.revision_no == revised.revision_no
        # What was agreed is more than the base offer, and is not the ceiling.
        assert response.grand_total > base_after_revision

        # ---------------------------------------------------------------- #
        # 10. Confirmations, and the accepted document
        # ---------------------------------------------------------------- #
        session.commit()
        before = len(mail.sent)
        result = worker.run_sweep()
        session.expire_all()

        assert result.documents_ready == 1
        delivered = [m.subject for m in mail.sent[before:]]
        assert any("accepted" in s.lower() for s in delivered)

        confirmation = next(
            m for m in mail.sent[before:] if m.to_email == CUSTOMER
        )
        assert f"{response.grand_total:,.2f}" in confirmation.text_body
        assert "Dana R. Whitfield" not in confirmation.text_body   # signature stays in the PDF

        artifact = quote_document_service.artifact_for_response(session, response.id)
        assert artifact is not None
        data = quote_document_service.verify(session, artifact)
        assert data.startswith(b"%PDF-")
        assert hashlib.sha256(data).hexdigest() == artifact.sha256

        from io import BytesIO

        from pypdf import PdfReader

        text = "\n".join(
            p.extract_text() or "" for p in PdfReader(BytesIO(data)).pages
        )
        assert "ACCEPTED" in text.upper()
        assert f"{response.grand_total:,.2f}" in text
        assert "Dana R. Whitfield" in text          # the signature, in the secured PDF
        assert f"Rev {response.revision_no}" in text

        # ---------------------------------------------------------------- #
        # 11. Everything still agrees
        # ---------------------------------------------------------------- #
        assert quote_document_service.state_for_response(
            session, response.id
        ).status is DocumentJobStatus.READY

        # No message was ever a view.
        kinds = [e.event_type for e in session.query(QuoteEvent).all()]
        assert kinds.count(QuoteEventType.VIEWED) == 1
        assert QuoteEventType.EMAIL_SENT in kinds
        assert QuoteEventType.EMAIL_RESENT in kinds

        # Every queued message reached the capture backend.
        assert all(r.status is EmailOutboxStatus.SENT for r in _rows(session))
        assert len(mail.sent) == len(_rows(session))

        # And the accepted total is still what it was, not a repricing.
        session.expire_all()
        assert session.get(type(response), response.id).grand_total == expected_total


def _rows(session):
    return session.query(EmailOutbox).order_by(EmailOutbox.id).all()
