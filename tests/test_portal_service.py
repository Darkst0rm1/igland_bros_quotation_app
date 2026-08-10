"""Customer portal: tokens, selections, money, responses and the guards around them."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from modules import portal_service, quotation_service
from modules.authorization import AuthUser, PermissionDenied
from modules.constants import (
    AuditAction,
    ItemInclusion,
    Perm,
    PortalResponseType,
    QuotationStatus,
    QuoteEventType,
)
from modules.models import AuditLog, PortalResponse, QuoteAccessToken, QuoteEvent, User
from modules.portal_service import (
    PortalAccessError,
    PortalError,
    PortalStateError,
    approve,
    issue_token,
    request_changes,
    resolve_token,
)

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
    """The test schema is built from metadata and stamped, so data migrations
    never run here. Provision the reserved account the same way they would."""
    return portal_service.ensure_portal_user(session)


@pytest.fixture
def sent(session, quotation, sales, manager):
    """A quotation approved, issued and sent — the only state a customer sees."""
    _approve_and_issue(session, quotation, sales, manager)
    quotation.contact_email = "buyer@bunzl.example"
    quotation.contact_name = "Alex Buyer"
    quotation_service.change_status(
        session, manager, quotation, QuotationStatus.SENT_TO_CUSTOMER
    )
    session.flush()
    return quotation


@pytest.fixture
def link(session, sent, sales):
    token, raw = issue_token(session, sales, sent)
    return token, raw


class TestTokens:
    def test_the_plaintext_token_is_never_stored(self, session, sent, sales):
        token, raw = issue_token(session, sales, sent)
        assert token.token_hash != raw
        assert len(token.token_hash) == 64          # sha256 hex
        assert len(raw) >= 32                        # 256 bits, url-safe
        # Nothing anywhere in the row echoes the plaintext.
        stored = {str(v) for v in token.__dict__.values() if v is not None}
        assert not any(raw in s for s in stored)

    def test_a_valid_token_resolves_to_its_own_quotation(self, session, link, sent):
        token, raw = link
        assert resolve_token(session, raw).quotation_id == sent.id

    def test_an_unknown_token_is_refused(self, session, link):
        with pytest.raises(PortalAccessError):
            resolve_token(session, "not-a-real-token-but-long-enough-to-pass")

    def test_a_revoked_token_is_refused(self, session, link, sales):
        token, raw = link
        portal_service.revoke_token(session, sales, token)
        with pytest.raises(PortalAccessError):
            resolve_token(session, raw)

    def test_an_expired_token_is_refused(self, session, sent, sales):
        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        token, raw = issue_token(session, sales, sent, expires_at=past)
        with pytest.raises(PortalAccessError):
            resolve_token(session, raw)

    def test_one_customer_cannot_reach_another_quotation(
        self, session, sent, sales, manager, variant, admin
    ):
        """The token is a capability scoped to exactly one quotation."""
        other = quotation_service.create_draft(
            session, sales, sent.customer_id, project_name="Other job",
            quote_date=dt.date.today(),
        )
        session.flush()
        token, raw = issue_token(session, sales, sent)
        resolved = resolve_token(session, raw)
        assert resolved.quotation_id == sent.id
        assert resolved.quotation_id != other.id

    def test_issuing_a_link_requires_permission(self, session, sent, make_auth_user):
        nobody = AuthUser(id=999, username="x", employee_name="x", email="x@y.z",
                          permissions=frozenset())
        with pytest.raises(PermissionDenied):
            issue_token(session, nobody, sent)

    def test_revoking_is_idempotent(self, session, link, sales):
        token, _ = link
        portal_service.revoke_token(session, sales, token)
        first = token.revoked_at
        portal_service.revoke_token(session, sales, token)
        assert token.revoked_at == first


class TestViewTracking:
    def test_views_are_recorded_without_personal_data(self, session, link):
        token, raw = link
        portal_service.record_view(session, token)
        portal_service.record_view(session, token)

        assert token.view_count == 2
        assert token.first_viewed_at is not None
        assert token.last_viewed_at >= token.first_viewed_at

        events = session.execute(
            select(QuoteEvent).where(QuoteEvent.event_type == QuoteEventType.VIEWED)
        ).scalars().all()
        assert len(events) == 2
        # No column exists for an IP or user agent, by design.
        for event in events:
            assert not hasattr(event, "ip_address")
            assert not hasattr(event, "user_agent")

    def test_resolving_a_token_does_not_count_as_a_view(self, session, link):
        token, raw = link
        resolve_token(session, raw)
        assert token.view_count == 0
        assert token.first_viewed_at is None


class TestSelectionsAndMoney:
    @pytest.fixture
    def with_optional(self, session, sent):
        """Make the single line optional so selection changes the total."""
        item = sent.items[0]
        item.inclusion = ItemInclusion.OPTIONAL
        session.flush()
        return sent, item

    def test_optional_lines_are_excluded_until_selected(self, session, with_optional):
        quote, item = with_optional
        without = portal_service.compute_selection_totals(quote, [])
        withit = portal_service.compute_selection_totals(quote, [item.id])
        assert without.subtotal == D("0.00")
        assert withit.subtotal > without.subtotal

    def test_tax_and_grand_total_follow_the_selection(self, session, with_optional):
        quote, item = with_optional
        quote.tax_rate_pct = D("13")
        session.flush()
        totals = portal_service.compute_selection_totals(quote, [item.id])
        # Tax applies to the taxable base — subtotal plus taxable charges —
        # not to the subtotal alone.
        assert totals.tax_amount == (
            totals.taxable_base * D("13") / D("100")
        ).quantize(D("0.01"))
        assert totals.grand_total == (
            totals.subtotal - totals.quotation_discount
            + totals.charges_total + totals.tax_amount
        )

    def test_prices_come_from_the_database_not_the_request(self, session, with_optional):
        """The browser sends ids. Nothing else it could send changes the money."""
        quote, item = with_optional
        expected = portal_service.compute_selection_totals(quote, [item.id])
        # Ids that do not belong here are dropped, not priced.
        tampered = portal_service.compute_selection_totals(
            quote, [item.id, 999999, -1, "12; DROP TABLE quotations"]
        )
        assert tampered.grand_total == expected.grand_total

    def test_an_included_line_cannot_be_deselected(self, session, sent):
        item = sent.items[0]
        assert item.inclusion is ItemInclusion.INCLUDED
        # Passing no selection still bills the included line.
        totals = portal_service.compute_selection_totals(sent, [])
        assert totals.subtotal > D("0.00")
        assert portal_service.normalise_selection(sent, [item.id]) == []

    def test_recommended_lines_are_selectable_too(self, session, sent):
        item = sent.items[0]
        item.inclusion = ItemInclusion.RECOMMENDED
        session.flush()
        assert portal_service.normalise_selection(sent, [item.id]) == [item.id]

    def test_deposit_is_derived_from_the_rate(self, session, sent):
        sent.deposit_pct = D("25")
        session.flush()
        totals = portal_service.compute_selection_totals(sent, [])
        assert portal_service.deposit_due(sent, totals) == (
            totals.grand_total * D("25") / D("100")
        ).quantize(D("0.01"))


class TestApproval:
    def test_approval_records_the_customer_and_server_computed_total(
        self, session, link, sent
    ):
        token, raw = link
        expected = portal_service.compute_selection_totals(sent, [])

        response = approve(
            session, token,
            customer_name="Alex Buyer", customer_email="alex@bunzl.example",
            job_title="Purchasing Manager", signature_name="A. Buyer",
            accepted_terms=True,
        )

        assert response.response_type is PortalResponseType.APPROVED
        assert response.customer_name == "Alex Buyer"
        assert response.job_title == "Purchasing Manager"
        assert response.signature_name == "A. Buyer"
        assert response.grand_total == expected.grand_total
        assert response.revision_no == sent.revision_no
        assert sent.status is QuotationStatus.ACCEPTED

    def test_approval_locks_the_revision(self, session, link, sent):
        token, _ = link
        approve(session, token, customer_name="Alex", accepted_terms=True)
        assert sent.is_locked is True

    def test_the_actor_is_the_portal_not_an_employee(self, session, link, sent):
        token, _ = link
        approve(session, token, customer_name="Alex", accepted_terms=True)
        session.flush()   # record_audit adds without flushing

        rows = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.CUSTOMER_APPROVED)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].username_snapshot == "CUSTOMER_PORTAL"

        status_rows = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.STATUS_CHANGED)
        ).scalars().all()
        assert status_rows[-1].username_snapshot == "CUSTOMER_PORTAL"

    def test_terms_must_be_accepted(self, session, link):
        token, _ = link
        with pytest.raises(PortalError, match="terms"):
            approve(session, token, customer_name="Alex", accepted_terms=False)

    def test_a_name_is_required(self, session, link):
        token, _ = link
        with pytest.raises(PortalError, match="name"):
            approve(session, token, customer_name="   ", accepted_terms=True)

    def test_an_already_accepted_quotation_cannot_be_accepted_again(
        self, session, link
    ):
        token, _ = link
        approve(session, token, customer_name="Alex", accepted_terms=True)
        with pytest.raises(PortalStateError, match="already been accepted"):
            approve(session, token, customer_name="Alex", accepted_terms=True)

    def test_a_draft_cannot_be_accepted(self, session, quotation, sales):
        """Invalid starting status."""
        token, raw = issue_token(session, sales, quotation)
        assert quotation.status is QuotationStatus.DRAFT
        with pytest.raises(PortalStateError):
            approve(session, token, customer_name="Alex", accepted_terms=True)

    def test_an_expired_quotation_cannot_be_accepted(self, session, link, sent):
        token, _ = link
        sent.valid_until = dt.date.today() - dt.timedelta(days=1)
        session.flush()
        with pytest.raises(PortalStateError, match="expired"):
            approve(session, token, customer_name="Alex", accepted_terms=True)

    def test_a_superseded_revision_cannot_be_accepted(self, session, link, sent):
        token, _ = link
        sent.is_current_revision = False
        session.flush()
        with pytest.raises(PortalStateError, match="superseded"):
            approve(session, token, customer_name="Alex", accepted_terms=True)

    def test_only_one_response_can_accept_a_revision(self, session, link, sent):
        """The database settles the race, not a check-then-write in Python."""
        token, _ = link
        approve(session, token, customer_name="First", accepted_terms=True)

        # Force a second acceptance row for the same revision, bypassing the
        # service guards entirely — the unique index must still refuse it.
        from sqlalchemy.exc import IntegrityError

        session.add(
            PortalResponse(
                quotation_id=sent.id, revision_no=sent.revision_no,
                response_type=PortalResponseType.APPROVED,
                customer_name="Second", accepted_terms=True, currency=sent.currency,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_a_replayed_submission_is_refused(self, session, sent, sales):
        """The same nonce cannot be recorded twice.

        A replayed POST is usually stopped earlier, by the status check — the
        first response moves the quotation out of SENT_TO_CUSTOMER. This proves
        the layer underneath that: even reaching the insert, the unique index
        refuses a second row carrying a nonce already used.
        """
        from sqlalchemy.exc import IntegrityError

        token, _ = issue_token(session, sales, sent)
        nonce, signature = portal_service.issue_submission_nonce(token)
        portal_service.verify_submission_nonce(token, nonce, signature)

        request_changes(
            session, token, customer_name="Alex", comment="Cheaper please",
            nonce=nonce,
        )
        session.flush()

        session.add(
            PortalResponse(
                quotation_id=sent.id, revision_no=sent.revision_no,
                response_type=PortalResponseType.CHANGES_REQUESTED,
                customer_name="Replay", comment="Cheaper please",
                currency=sent.currency, submission_nonce=nonce,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


class TestChangeRequests:
    def test_a_change_request_records_and_moves_the_status(self, session, link, sent):
        token, _ = link
        response = request_changes(
            session, token, customer_name="Alex",
            customer_email="alex@bunzl.example",
            comment="Please quote 40ft containers instead.",
        )
        assert response.response_type is PortalResponseType.CHANGES_REQUESTED
        assert sent.status is QuotationStatus.REVISION_REQUIRED
        assert "40ft" in response.comment

    def test_a_comment_is_required(self, session, link):
        token, _ = link
        with pytest.raises(PortalError, match="changed"):
            request_changes(session, token, customer_name="Alex", comment="  ")


class TestPortalActor:
    def test_the_system_user_exists_and_cannot_authenticate(self, session):
        actor_row = session.execute(
            select(User).where(User.username == portal_service.PORTAL_USERNAME)
        ).scalar_one()
        assert actor_row.is_active is False

        from modules.authentication import AuthenticationError, authenticate

        with pytest.raises(AuthenticationError):
            authenticate(session, portal_service.PORTAL_USERNAME, "anything")

    def test_even_a_known_password_cannot_log_the_system_user_in(self, session):
        """Inactive is checked before the password, so credentials do not help."""
        from modules.authentication import AuthenticationError, authenticate, hash_password

        actor_row = session.execute(
            select(User).where(User.username == portal_service.PORTAL_USERNAME)
        ).scalar_one()
        actor_row.password_hash = hash_password("Sup3rSecret!Portal")
        session.flush()

        with pytest.raises(AuthenticationError):
            authenticate(session, portal_service.PORTAL_USERNAME, "Sup3rSecret!Portal")

    def test_the_actor_holds_exactly_one_permission(self, session):
        actor = portal_service.portal_actor(session)
        assert actor.permissions == frozenset({Perm.QUOTE_UPDATE_STATUS})
        assert actor.username == "CUSTOMER_PORTAL"

    def test_the_portal_cannot_perform_other_transitions(self, session, sent):
        """One permission is not a licence to cancel or approve internally."""
        actor = portal_service.portal_actor(session)
        for status in (
            QuotationStatus.CANCELLED,       # needs QUOTE_CANCEL
        ):
            with pytest.raises(PermissionDenied):
                quotation_service.change_status(session, actor, sent, status, note="x")

    def test_the_portal_cannot_reach_an_illegal_status(self, session, sent):
        """The transition table still applies to the portal actor."""
        actor = portal_service.portal_actor(session)
        with pytest.raises(quotation_service.QuotationError):
            quotation_service.change_status(
                session, actor, sent, QuotationStatus.APPROVED, note="x"
            )

    def test_it_refuses_to_act_if_the_account_is_activated(self, session):
        actor_row = session.execute(
            select(User).where(User.username == portal_service.PORTAL_USERNAME)
        ).scalar_one()
        actor_row.is_active = True
        session.flush()
        with pytest.raises(PortalError, match="activated"):
            portal_service.portal_actor(session)


class TestNonce:
    def test_a_forged_signature_is_refused(self, session, link):
        token, _ = link
        nonce, signature = portal_service.issue_submission_nonce(token)
        portal_service.verify_submission_nonce(token, nonce, signature)  # genuine
        with pytest.raises(PortalError):
            portal_service.verify_submission_nonce(token, nonce, "0" * 64)

    def test_a_nonce_is_bound_to_its_own_token(self, session, sent, sales):
        first, _ = issue_token(session, sales, sent)
        second, _ = issue_token(session, sales, sent)
        nonce, signature = portal_service.issue_submission_nonce(first)
        with pytest.raises(PortalError):
            portal_service.verify_submission_nonce(second, nonce, signature)
