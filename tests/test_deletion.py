"""Quotation deletion, restoration, and the markdown money escape."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules import approval_service, quotation_service, reporting_service
from modules.authorization import PermissionDenied, load_auth_user
from modules.constants import Perm, QuotationStatus
from modules.customer_service import create_customer
from modules.models import Quotation
from modules.utilities import escape_markdown, format_money, format_pack_price
from modules.validation import CustomerInput

JAN = dt.date(2026, 1, 15)


# --------------------------------------------------------------------------- #
# Markdown escaping
# --------------------------------------------------------------------------- #

class TestMarkdownMoneyEscape:
    """Streamlit renders LaTeX in markdown, so a dollar sign opens a maths
    span. Two on one line is every price comparison the application draws."""

    def test_dollar_is_escaped(self):
        assert escape_markdown("$5.98") == r"\$5.98"

    def test_the_tier_hint_survives_intact(self):
        """The exact string from the quotation editor. Unescaped, everything
        between the first and second dollar renders as italic maths and both
        signs vanish."""
        raw = (
            f"Available: Standard {format_pack_price(D('5.98'), 'USD')} · "
            f"Three Containers {format_pack_price(D('5.80'), 'USD')} · "
            f"Eight Containers {format_pack_price(D('5.62'), 'USD')}"
        )
        assert raw.count("$") == 3

        escaped = escape_markdown(raw)
        assert escaped.count(r"\$") == 3
        # No bare dollar remains to open a maths span.
        assert "$" not in escaped.replace(r"\$", "")
        for figure in ("5.9800", "5.8000", "5.6200"):
            assert figure in escaped

    def test_currencies_without_a_dollar_are_untouched(self):
        text = f"Total {format_money(D('12.50'), 'EUR')}"
        assert escape_markdown(text) == text

    def test_escaping_is_display_only(self):
        """format_money itself must stay clean — it feeds PDFs, Word, Excel
        and dataframes, where a backslash would print literally."""
        assert format_money(D("5.98"), "USD") == "$5.98"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def sales(make_auth_user):
    return make_auth_user("SALES", username="alice")


@pytest.fixture
def admin(make_auth_user):
    return make_auth_user("SYS_ADMIN", username="root")


@pytest.fixture
def customer(session, sales):
    made = create_customer(
        session, sales,
        CustomerInput(customer_number="CUST-0001", company_name="Bunzl Canada"),
    )
    session.commit()
    return made


@pytest.fixture
def draft(session, sales, customer):
    quotation = quotation_service.create_draft(
        session, sales, customer.id, quote_date=JAN
    )
    session.commit()
    return quotation


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #

class TestDeleteDraft:
    def test_owner_can_delete_their_draft(self, session, sales, draft):
        assert quotation_service.delete_quotation(session, sales, draft) == 1
        session.commit()
        assert draft.deleted_at is not None

    def test_deleted_draft_leaves_the_history_query(self, session, sales, draft):
        quotation_service.delete_quotation(session, sales, draft)
        session.commit()

        live = session.query(Quotation).filter(Quotation.deleted_at.is_(None)).all()
        assert draft not in live

    def test_deletion_is_audited_with_what_was_removed(self, session, sales, draft):
        from modules.models import AuditLog

        quotation_service.delete_quotation(session, sales, draft, reason="duplicate")
        session.commit()

        entry = (
            session.query(AuditLog)
            .filter(AuditLog.action == "QUOTATION_DELETED")
            .one()
        )
        assert entry.entity_id == draft.id
        assert entry.reason == "duplicate"
        assert entry.old_value_json["status"] == "DRAFT"

    def test_a_salesperson_cannot_delete_someone_elses_draft(
        self, session, make_auth_user, draft
    ):
        other = make_auth_user("SALES", username="bob")
        with pytest.raises(PermissionDenied):
            quotation_service.delete_quotation(session, other, draft)

    def test_deleting_twice_is_harmless(self, session, sales, draft):
        assert quotation_service.delete_quotation(session, sales, draft) == 1
        first = draft.deleted_at
        session.commit()

        assert quotation_service.delete_quotation(session, sales, draft) == 0
        session.commit()
        assert draft.deleted_at == first, "the original deletion time was overwritten"


class TestDeleteIssued:
    """An issued quotation is a record of what a customer was actually sent."""

    @pytest.fixture
    def issued(self, session, draft):
        draft.status = QuotationStatus.SENT_TO_CUSTOMER
        draft.is_locked = True
        draft.issued_at = dt.datetime.now(dt.UTC)
        session.commit()
        return draft

    def test_salesperson_is_refused(self, session, sales, issued):
        with pytest.raises(PermissionDenied, match="delete_any"):
            quotation_service.delete_quotation(session, sales, issued)

    def test_the_refusal_points_at_cancelling(self, session, sales, issued):
        with pytest.raises(PermissionDenied, match="cancel"):
            quotation_service.delete_quotation(session, sales, issued)

    def test_administrator_may_delete_it(self, session, admin, issued):
        assert quotation_service.delete_quotation(session, admin, issued) == 1
        session.commit()
        assert issued.deleted_at is not None

    def test_the_immutability_guard_permits_the_write(self, session, admin, issued):
        """deleted_at is in the locked-quotation writable set; if it were not,
        this would raise ImmutableRecordError on flush."""
        quotation_service.delete_quotation(session, admin, issued)
        session.commit()  # must not raise


class TestRevisionFamily:
    @pytest.fixture
    def family(self, session, sales, admin, draft):
        from modules import revision_service

        draft.status = QuotationStatus.SENT_TO_CUSTOMER
        draft.is_locked = True
        draft.issued_at = dt.datetime.now(dt.UTC)
        session.commit()
        rev1 = revision_service.create_revision(
            session, admin, draft, reason="price change"
        )
        session.commit()
        return draft, rev1

    def test_deleting_takes_the_whole_family(self, session, admin, family):
        original, revision = family
        assert quotation_service.delete_quotation(session, admin, revision) == 2
        session.commit()
        assert original.deleted_at is not None
        assert revision.deleted_at is not None

    def test_no_revision_is_left_behind_as_current(self, session, admin, family):
        """Removing one revision alone would leave the rest with nothing marked
        current: absent from the history list and unopenable from it."""
        original, revision = family
        quotation_service.delete_quotation(session, admin, original)
        session.commit()

        survivors = (
            session.query(Quotation)
            .filter(
                Quotation.deleted_at.is_(None),
                Quotation.quote_number == original.quote_number,
            )
            .all()
        )
        assert survivors == []


class TestNumbersStayConsumed:
    def test_a_deleted_number_is_never_reissued(self, session, sales, customer, draft):
        """Otherwise two different documents share a number, and a customer
        holding the first would find it refers to something else."""
        gone = draft.quote_number
        quotation_service.delete_quotation(session, sales, draft)
        session.commit()

        replacement = quotation_service.create_draft(
            session, sales, customer.id, quote_date=JAN
        )
        session.commit()
        assert replacement.quote_number != gone


# --------------------------------------------------------------------------- #
# Restoring
# --------------------------------------------------------------------------- #

class TestRestore:
    def test_administrator_restores_it(self, session, sales, admin, draft):
        quotation_service.delete_quotation(session, sales, draft)
        session.commit()

        assert quotation_service.restore_quotation(session, admin, draft) == 1
        session.commit()
        assert draft.deleted_at is None

    def test_a_salesperson_cannot_restore_even_their_own(
        self, session, sales, draft
    ):
        """Restoring returns a quotation to everyone's history and reports,
        which is wider than removing your own working copy."""
        quotation_service.delete_quotation(session, sales, draft)
        session.commit()

        with pytest.raises(PermissionDenied):
            quotation_service.restore_quotation(session, sales, draft)

    def test_restoring_a_live_quotation_changes_nothing(self, session, admin, draft):
        assert quotation_service.restore_quotation(session, admin, draft) == 0
        session.commit()
        assert draft.deleted_at is None


# --------------------------------------------------------------------------- #
# Everywhere else a quotation appears
# --------------------------------------------------------------------------- #

class TestDeletedQuotationsDisappearEverywhere:
    def test_reports_exclude_it(self, session, sales, admin, draft):
        before = reporting_service.headlines(session, admin).total
        quotation_service.delete_quotation(session, sales, draft)
        session.commit()
        after = reporting_service.headlines(session, admin).total
        assert after == before - 1

    def test_the_approval_queue_excludes_it(
        self, session, sales, admin, draft, make_auth_user
    ):
        """A pending request outlives the quotation. Left in the queue, an
        approver could approve a quotation that exists nowhere else."""
        from modules.models import Approval

        session.add(Approval(quotation_id=draft.id, requested_by_id=sales.id))
        session.commit()

        manager = make_auth_user("SALES_MANAGER", username="mgr")
        assert any(q.id == draft.id for _, q in approval_service.queue(session, manager))

        quotation_service.delete_quotation(session, admin, draft)
        session.commit()

        assert not any(
            q.id == draft.id for _, q in approval_service.queue(session, manager)
        )


class TestCanDelete:
    def test_owner_of_a_draft(self, session, sales, draft):
        assert quotation_service.can_delete(sales, draft) is True

    def test_not_the_owner(self, session, make_auth_user, draft):
        other = make_auth_user("SALES", username="bob")
        assert quotation_service.can_delete(other, draft) is False

    def test_administrator_regardless_of_status(self, session, admin, draft):
        draft.is_locked = True
        draft.status = QuotationStatus.ACCEPTED
        assert quotation_service.can_delete(admin, draft) is True

    def test_pricing_admin_has_no_delete_at_all(self, session, make_auth_user, draft):
        pricer = make_auth_user("PRICING_ADMIN", username="pricer")
        assert quotation_service.can_delete(pricer, draft) is False


class TestPermissionMatrix:
    def test_sales_may_delete_drafts_but_not_issued_quotations(self, session, sales):
        assert sales.has(Perm.QUOTE_DELETE_DRAFT)
        assert not sales.has(Perm.QUOTE_DELETE_ANY)

    def test_administrator_holds_both(self, session, admin):
        assert admin.has(Perm.QUOTE_DELETE_DRAFT)
        assert admin.has(Perm.QUOTE_DELETE_ANY)
