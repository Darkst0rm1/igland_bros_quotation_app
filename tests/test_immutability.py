"""Historical records must stay true.

Covers the three guarantees the brief calls out: issued quotations are
immutable, price history is preserved rather than overwritten, and money is
never stored as a binary float.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from sqlalchemy import Float, select

from modules.database import Base
from modules.models import (
    Customer,
    ImmutableRecordError,
    PriceTier,
    Product,
    ProductPrice,
    ProductVariant,
    Quotation,
    QuotationItem,
    QuotationRevision,
    User,
)


# --------------------------------------------------------------------------- #
# No floats anywhere in the money path
# --------------------------------------------------------------------------- #

def test_no_float_columns_exist_in_the_schema():
    """A Float column is a silent correctness bug waiting for a specific value.

    Asserting on the metadata rather than reviewing by eye means a future model
    change cannot reintroduce one unnoticed.
    """
    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert offenders == [], f"Float columns found: {offenders}"


def test_decimals_round_trip_exactly_on_sqlite(session):
    """SQLAlchemy's plain Numeric degrades to float on SQLite. ExactNumeric
    stores a scaled integer instead, so the test database behaves like
    production PostgreSQL."""
    product = Product(item_number="P1", name='7" White', size_label='7" White')
    session.add(product)
    session.flush()
    variant = ProductVariant(
        product_id=product.id, variant_item_number="P1-A",
        board_quality="WT110 HPFL115 KM135", case_pack=50,
    )
    tier = PriceTier(code="STANDARD", name="Standard")
    session.add_all([variant, tier])
    session.flush()

    price = ProductPrice(
        product_variant_id=variant.id, price_tier_id=tier.id,
        price_per_pack=D("3.79"), price_per_piece=D("0.0758"),
        effective_from=dt.date(2026, 1, 1),
    )
    session.add(price)
    session.commit()
    session.expunge_all()

    loaded = session.get(ProductPrice, price.id)
    assert loaded.price_per_pack == D("3.79")
    assert loaded.price_per_piece == D("0.0758")
    assert isinstance(loaded.price_per_pack, D)


def test_sql_aggregates_still_work_on_exact_columns(session):
    """The scaled-integer representation must not break SUM or ORDER BY, which
    the reports depend on."""
    from sqlalchemy import func

    product = Product(item_number="P2", name='8" White', size_label='8" White')
    session.add(product)
    session.flush()
    variant = ProductVariant(
        product_id=product.id, variant_item_number="P2-A",
        board_quality="Q", case_pack=50,
    )
    tier = PriceTier(code="STANDARD", name="Standard")
    session.add_all([variant, tier])
    session.flush()
    for i, amount in enumerate(["1.10", "2.20", "3.30"]):
        session.add(ProductPrice(
            product_variant_id=variant.id, price_tier_id=tier.id,
            price_per_pack=D(amount), price_per_piece=D("0.01"),
            effective_from=dt.date(2026, 1, i + 1),
        ))
    session.commit()

    ordered = session.execute(
        select(ProductPrice.price_per_pack).order_by(ProductPrice.price_per_pack.desc())
    ).scalars().all()
    assert ordered == [D("3.30"), D("2.20"), D("1.10")]

    # SUM over scaled integers is the scaled sum, and the column type descales
    # the aggregate on the way out — so reports get the right Decimal directly.
    total = session.execute(select(func.sum(ProductPrice.price_per_pack))).scalar()
    assert total == D("6.60")


# --------------------------------------------------------------------------- #
# Price history
# --------------------------------------------------------------------------- #

@pytest.fixture
def priced_variant(session):
    product = Product(item_number="P9", name='12" White', size_label='12" White')
    session.add(product)
    session.flush()
    variant = ProductVariant(
        product_id=product.id, variant_item_number="P9-A",
        board_quality="WT110 HPFL115 KM135", case_pack=50,
    )
    tier = PriceTier(code="STANDARD", name="Standard")
    session.add_all([variant, tier])
    session.flush()
    price = ProductPrice(
        product_variant_id=variant.id, price_tier_id=tier.id,
        price_per_pack=D("7.42"), price_per_piece=D("0.1484"),
        effective_from=dt.date(2026, 1, 1),
    )
    session.add(price)
    session.commit()
    return variant, tier, price


class TestPriceHistory:
    def test_price_amounts_cannot_be_edited(self, session, priced_variant):
        _, _, price = priced_variant
        price.price_per_pack = D("9.99")
        with pytest.raises(ImmutableRecordError, match="append-only"):
            session.commit()
        session.rollback()

    def test_effective_to_may_be_set_to_supersede(self, session, priced_variant):
        _, _, price = priced_variant
        price.effective_to = dt.date(2026, 6, 30)
        session.commit()  # must not raise
        assert session.get(ProductPrice, price.id).effective_to == dt.date(2026, 6, 30)

    def test_superseding_keeps_both_rows(self, session, priced_variant):
        variant, tier, old = priced_variant
        old.effective_to = dt.date(2026, 6, 30)
        session.add(ProductPrice(
            product_variant_id=variant.id, price_tier_id=tier.id,
            price_per_pack=D("7.95"), price_per_piece=D("0.159"),
            effective_from=dt.date(2026, 7, 1),
        ))
        session.commit()

        rows = session.execute(
            select(ProductPrice)
            .where(ProductPrice.product_variant_id == variant.id)
            .order_by(ProductPrice.effective_from)
        ).scalars().all()
        assert len(rows) == 2
        assert rows[0].price_per_pack == D("7.42")
        assert rows[1].price_per_pack == D("7.95")

    def test_historical_lookup_returns_the_price_of_the_day(
        self, session, priced_variant
    ):
        variant, tier, old = priced_variant
        old.effective_to = dt.date(2026, 6, 30)
        session.add(ProductPrice(
            product_variant_id=variant.id, price_tier_id=tier.id,
            price_per_pack=D("7.95"), price_per_piece=D("0.159"),
            effective_from=dt.date(2026, 7, 1),
        ))
        session.commit()

        def price_on(day: dt.date) -> D:
            return session.execute(
                select(ProductPrice.price_per_pack).where(
                    ProductPrice.product_variant_id == variant.id,
                    ProductPrice.effective_from <= day,
                    (ProductPrice.effective_to.is_(None))
                    | (ProductPrice.effective_to >= day),
                )
            ).scalar_one()

        assert price_on(dt.date(2026, 3, 1)) == D("7.42")
        assert price_on(dt.date(2026, 9, 1)) == D("7.95")


# --------------------------------------------------------------------------- #
# Issued quotations
# --------------------------------------------------------------------------- #

@pytest.fixture
def issued_quotation(session):
    user = User(username="s1", email="s1@x.invalid", employee_name="S", password_hash="x")
    customer = Customer(customer_number="C1", company_name="Acme")
    session.add_all([user, customer])
    session.flush()

    quote = Quotation(
        quote_number="IGB-QT-2026-0001", revision_no=0,
        customer_id=customer.id, sales_user_id=user.id,
        quote_date=dt.date(2026, 8, 3), project_name="Pizza boxes",
        subtotal=D("1000.00"), grand_total=D("1000.00"),
    )
    session.add(quote)
    session.flush()
    quote.root_quotation_id = quote.id
    item = QuotationItem(
        quotation_id=quote.id, line_no=1,
        price_per_pack=D("3.79"), price_per_piece=D("0.0758"),
        quantity_packs=D("100"), net_line_total=D("379.00"),
    )
    session.add(item)
    session.commit()

    quote.is_locked = True
    quote.issued_at = dt.datetime.now(dt.UTC)
    session.commit()
    return quote, item


class TestIssuedQuotationImmutability:
    def test_header_cannot_be_edited(self, session, issued_quotation):
        quote, _ = issued_quotation
        quote.project_name = "Something else"
        with pytest.raises(ImmutableRecordError, match="Create a new revision"):
            session.commit()
        session.rollback()

    def test_totals_cannot_be_edited(self, session, issued_quotation):
        quote, _ = issued_quotation
        quote.grand_total = D("1.00")
        with pytest.raises(ImmutableRecordError):
            session.commit()
        session.rollback()

    def test_line_quantities_cannot_be_edited(self, session, issued_quotation):
        _, item = issued_quotation
        item.quantity_packs = D("999")
        with pytest.raises(ImmutableRecordError):
            session.commit()
        session.rollback()

    def test_lines_cannot_be_added(self, session, issued_quotation):
        quote, _ = issued_quotation
        session.add(QuotationItem(
            quotation=quote, line_no=2,
            price_per_pack=D("1"), price_per_piece=D("1"),
        ))
        with pytest.raises(ImmutableRecordError, match="added to"):
            session.commit()
        session.rollback()

    def test_lines_cannot_be_deleted(self, session, issued_quotation):
        _, item = issued_quotation
        session.delete(item)
        with pytest.raises(ImmutableRecordError, match="removed from"):
            session.commit()
        session.rollback()

    def test_status_may_still_change_after_issue(self, session, issued_quotation):
        """Recording that the customer accepted it is not editing the quotation."""
        from modules.constants import QuotationStatus

        quote, _ = issued_quotation
        quote.status = QuotationStatus.ACCEPTED
        session.commit()  # must not raise
        assert session.get(Quotation, quote.id).status == QuotationStatus.ACCEPTED

    def test_superseding_flag_may_still_change(self, session, issued_quotation):
        quote, _ = issued_quotation
        quote.is_current_revision = False
        session.commit()  # a new revision supersedes it

    def test_drafts_remain_freely_editable(self, session, issued_quotation):
        quote, _ = issued_quotation
        quote.is_locked = False
        session.commit()
        quote.project_name = "Reopened"
        session.commit()  # must not raise
        assert session.get(Quotation, quote.id).project_name == "Reopened"


class TestRevisionSnapshots:
    def test_snapshots_cannot_be_edited_or_deleted(self, session, issued_quotation):
        quote, _ = issued_quotation
        snapshot = QuotationRevision(
            root_quotation_id=quote.root_quotation_id,
            quotation_id=quote.id,
            revision_no=0,
            snapshot_json={"quote_number": quote.quote_number, "grand_total": "1000.00"},
            new_total=D("1000.00"),
        )
        session.add(snapshot)
        session.commit()

        snapshot.change_reason = "tampering"
        with pytest.raises(ImmutableRecordError, match="immutable"):
            session.commit()
        session.rollback()

        session.delete(snapshot)
        with pytest.raises(ImmutableRecordError, match="cannot be deleted"):
            session.commit()
        session.rollback()

    def test_a_customer_rename_does_not_alter_an_issued_quotation(
        self, session, issued_quotation
    ):
        """The address and contact snapshots on the quotation are what protect
        this — the customer record is free to change."""
        quote, _ = issued_quotation
        quote_id = quote.id
        session.expunge_all()

        quote = session.get(Quotation, quote_id)
        quote_snapshot_name = quote.customer_name_snapshot
        customer = session.get(Customer, quote.customer_id)
        customer.company_name = "Acme Renamed Ltd"
        session.commit()

        reloaded = session.get(Quotation, quote_id)
        assert reloaded.customer_name_snapshot == quote_snapshot_name
        assert reloaded.grand_total == D("1000.00")
