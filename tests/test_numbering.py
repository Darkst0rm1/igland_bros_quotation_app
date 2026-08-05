"""Quotation number format, sequence allocation and revision labels."""

from __future__ import annotations

import datetime as dt

import pytest

from modules.numbering import (
    DEFAULT_FORMAT,
    NumberFormatError,
    allocate_quote_number,
    peek_next_number,
    render,
    scope_key,
    validate_format,
)

JAN = dt.date(2026, 1, 15)
DEC = dt.date(2026, 12, 31)
NEXT_YEAR = dt.date(2027, 1, 2)


class TestFormatValidation:
    def test_default_format_is_valid(self):
        validate_format(DEFAULT_FORMAT)

    @pytest.mark.parametrize(
        "fmt",
        [
            "QT-{YYYY}-{SEQ:04d}",
            "{YY}{MM}-{SEQ:05d}",
            "QUOTE-{SEQ}",
            "IGB/{YYYY}/{MM}/{SEQ:03d}",
        ],
    )
    def test_accepted_formats(self, fmt):
        validate_format(fmt)

    def test_a_format_without_seq_is_rejected(self):
        with pytest.raises(NumberFormatError, match="SEQ"):
            validate_format("QT-{YYYY}")

    def test_an_empty_format_is_rejected(self):
        with pytest.raises(NumberFormatError):
            validate_format("   ")

    def test_an_unknown_placeholder_is_rejected(self):
        with pytest.raises(NumberFormatError, match="unrecognised"):
            validate_format("QT-{CUSTOMER}-{SEQ:04d}")


class TestRendering:
    def test_default_format(self):
        assert render(DEFAULT_FORMAT, 1, JAN) == "QT-2026-0001"
        assert render(DEFAULT_FORMAT, 42, JAN) == "QT-2026-0042"
        assert render(DEFAULT_FORMAT, 12345, JAN) == "QT-2026-12345"

    def test_two_digit_year_and_month(self):
        assert render("{YY}{MM}-{SEQ:03d}", 7, JAN) == "2601-007"

    def test_unpadded_sequence(self):
        assert render("Q-{SEQ}", 9, JAN) == "Q-9"


class TestScopeKey:
    def test_year_scoped_format_restarts_each_year(self):
        assert scope_key(DEFAULT_FORMAT, JAN) == "QUOTE:2026"
        assert scope_key(DEFAULT_FORMAT, NEXT_YEAR) == "QUOTE:2027"

    def test_month_scoped_format(self):
        assert scope_key("{YYYY}{MM}-{SEQ:04d}", JAN) == "QUOTE:2026:01"

    def test_unscoped_format_runs_continuously(self):
        assert scope_key("Q-{SEQ:06d}", JAN) == "QUOTE:ALL"
        assert scope_key("Q-{SEQ:06d}", NEXT_YEAR) == "QUOTE:ALL"


class TestAllocation:
    def test_sequence_increments(self, session):
        numbers = [allocate_quote_number(session, on_date=JAN) for _ in range(3)]
        session.commit()
        assert numbers == [
            "QT-2026-0001",
            "QT-2026-0002",
            "QT-2026-0003",
        ]

    def test_no_duplicates_across_many_allocations(self, session):
        numbers = [allocate_quote_number(session, on_date=JAN) for _ in range(50)]
        session.commit()
        assert len(set(numbers)) == 50

    def test_the_counter_restarts_in_a_new_year(self, session):
        allocate_quote_number(session, on_date=DEC)
        allocate_quote_number(session, on_date=DEC)
        session.commit()
        assert allocate_quote_number(session, on_date=NEXT_YEAR) == "QT-2027-0001"
        session.commit()

    def test_years_keep_independent_counters(self, session):
        allocate_quote_number(session, on_date=JAN)
        allocate_quote_number(session, on_date=NEXT_YEAR)
        session.commit()
        assert allocate_quote_number(session, on_date=JAN) == "QT-2026-0002"
        session.commit()

    def test_a_custom_format_is_honoured(self, session):
        number = allocate_quote_number(session, fmt="IGB/{YY}/{SEQ:03d}", on_date=JAN)
        session.commit()
        assert number == "IGB/26/001"

    def test_allocation_rejects_a_bad_format_before_touching_the_sequence(self, session):
        with pytest.raises(NumberFormatError):
            allocate_quote_number(session, fmt="QT-{YYYY}", on_date=JAN)

    def test_peek_does_not_consume(self, session):
        assert peek_next_number(session, on_date=JAN) == "QT-2026-0001"
        assert peek_next_number(session, on_date=JAN) == "QT-2026-0001"
        assert allocate_quote_number(session, on_date=JAN) == "QT-2026-0001"
        session.commit()
        assert peek_next_number(session, on_date=JAN) == "QT-2026-0002"

    def test_allocation_survives_a_rolled_back_transaction(self, session):
        """A rolled-back draft may leave a gap in the sequence, but must never
        hand the same number to two quotations."""
        first = allocate_quote_number(session, on_date=JAN)
        session.commit()
        allocate_quote_number(session, on_date=JAN)
        session.rollback()
        third = allocate_quote_number(session, on_date=JAN)
        session.commit()
        assert first != third


class TestRevisionLabels:
    def test_display_number_includes_the_revision(self, session):
        import datetime

        from modules.models import Customer, Quotation, User

        user = User(username="u", email="u@x.invalid", employee_name="U", password_hash="x")
        customer = Customer(customer_number="C1", company_name="Acme")
        session.add_all([user, customer])
        session.flush()

        quote = Quotation(
            quote_number="QT-2026-0001", revision_no=0,
            customer_id=customer.id, sales_user_id=user.id,
            quote_date=datetime.date(2026, 8, 3),
        )
        session.add(quote)
        session.flush()

        assert quote.revision_label == "Rev 0"
        assert quote.display_number == "QT-2026-0001 Rev 0"
        quote.revision_no = 2
        assert quote.display_number == "QT-2026-0001 Rev 2"

    def test_the_same_number_may_not_repeat_a_revision(self, session):
        import datetime

        from sqlalchemy.exc import IntegrityError

        from modules.models import Customer, Quotation, User

        user = User(username="u", email="u@x.invalid", employee_name="U", password_hash="x")
        customer = Customer(customer_number="C1", company_name="Acme")
        session.add_all([user, customer])
        session.flush()

        for _ in range(2):
            session.add(Quotation(
                quote_number="QT-2026-0001", revision_no=0,
                customer_id=customer.id, sales_user_id=user.id,
                quote_date=datetime.date(2026, 8, 3),
            ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
