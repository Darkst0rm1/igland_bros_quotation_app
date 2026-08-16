"""Row-level edit and delete on the Lines tab.

The buttons live on the page, but everything they guarantee is in the service
layer, and that is where it is asserted: the page passes a line id and catches
what comes back. What matters is that acting on one line cannot touch another,
that a second click on delete cannot take a neighbour, and that the dialog's
save path reaches the same validation, repricing and audit trail the old
"Change a line" form did.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules import quotation_service
from modules.authorization import PermissionDenied
from modules.catalogue_service import create_product, create_variant, set_price
from modules.constants import PriceTierCode, PricingBasis, RoleCode
from modules.customer_service import create_customer
from modules.models import QuotationItem
from modules.quotation_service import QuotationError
from modules.validation import CustomerInput, PriceInput, ProductInput, VariantInput

JAN = dt.date(2026, 1, 1)
QUOTE_DAY = dt.date(2026, 8, 16)


@pytest.fixture
def admin(make_auth_user):
    return make_auth_user(RoleCode.SYS_ADMIN.value)


@pytest.fixture
def sales(make_auth_user):
    return make_auth_user(RoleCode.SALES.value)


def _variant(session, admin, size, price):
    product = create_product(
        session, admin,
        ProductInput(
            item_number=f"WB-{size}", name=f'{size}" White',
            size_label=f'{size}" White', flute="B", depth_in=D("2"),
        ),
    )
    session.flush()
    variant = create_variant(
        session, admin, product.id,
        VariantInput(
            variant_item_number=f"WB-{size}-A",
            board_quality="WT110 HPFL115 KM135", case_pack=50,
        ),
    )
    for tier in (PriceTierCode.STANDARD, PriceTierCode.THREE_CONTAINER):
        set_price(
            session, admin,
            PriceInput(
                product_variant_id=variant.id, price_tier_code=tier.value,
                price_per_pack=D(price), effective_from=JAN,
            ),
        )
    session.flush()
    return variant


@pytest.fixture
def three_lines(session, admin):
    """Three lines at $10, $20 and $30 a pack, 100 packs each."""
    customer = create_customer(
        session, admin,
        CustomerInput(customer_number="CUST-0200", company_name="Row Actions Ltd"),
    )
    session.flush()
    quote = quotation_service.create_draft(
        session, admin, customer.id, quote_date=QUOTE_DAY
    )
    for size, price in (("12", "10.00"), ("16", "20.00"), ("18", "30.00")):
        quotation_service.add_line(
            session, admin, quote,
            product_variant_id=_variant(session, admin, size, price).id,
            price_tier_code=PriceTierCode.STANDARD.value,
            quantity_packs=D("100"),
        )
    session.commit()
    return quote


def _by_line_no(quotation):
    return sorted(quotation.items, key=lambda i: i.line_no)


class TestDelete:
    def test_it_removes_only_the_line_asked_for(self, session, admin, three_lines):
        middle = _by_line_no(three_lines)[1]
        survivors = {
            i.id: i.net_line_total for i in _by_line_no(three_lines) if i.id != middle.id
        }

        quotation_service.remove_line(session, admin, three_lines, middle.id)
        session.commit()

        remaining = {i.id: i.net_line_total for i in three_lines.items}
        assert remaining == survivors
        assert middle.id not in remaining

    def test_the_totals_move_by_exactly_the_removed_line(
        self, session, admin, three_lines
    ):
        before = three_lines.grand_total
        middle = _by_line_no(three_lines)[1]
        removed = middle.net_line_total

        quotation_service.remove_line(session, admin, three_lines, middle.id)
        session.commit()

        assert three_lines.grand_total == before - removed == D("4000.00")

    def test_the_remaining_lines_are_renumbered_without_reordering(
        self, session, admin, three_lines
    ):
        """Line 3 becomes line 2; it does not become the old line 2."""
        first, _, third = _by_line_no(three_lines)
        third_id, third_total = third.id, third.net_line_total

        quotation_service.remove_line(session, admin, three_lines, _by_line_no(three_lines)[1].id)
        session.commit()

        rows = _by_line_no(three_lines)
        assert [r.line_no for r in rows] == [1, 2]
        assert rows[0].id == first.id
        assert rows[1].id == third_id
        assert rows[1].net_line_total == third_total

    def test_a_second_click_cannot_take_a_neighbour(
        self, session, admin, three_lines
    ):
        """The double-click guard, and it is structural rather than a flag.

        The page passes a line id, never a row position, so a delete that
        arrives after the line has gone finds nothing and says so. Had it
        passed an index, the second click would have deleted whichever line
        shifted up into that slot.
        """
        target = _by_line_no(three_lines)[1]
        quotation_service.remove_line(session, admin, three_lines, target.id)
        session.commit()
        surviving = {i.id for i in three_lines.items}

        with pytest.raises(QuotationError, match="not part of this quotation"):
            quotation_service.remove_line(session, admin, three_lines, target.id)

        assert {i.id for i in three_lines.items} == surviving

    def test_a_line_from_another_quotation_is_refused(
        self, session, admin, three_lines
    ):
        other = quotation_service.create_draft(
            session, admin, three_lines.customer_id, quote_date=QUOTE_DAY
        )
        session.flush()
        with pytest.raises(QuotationError, match="not part of this quotation"):
            quotation_service.remove_line(
                session, admin, other, _by_line_no(three_lines)[0].id
            )
        assert len(three_lines.items) == 3

    def test_sales_cannot_delete_from_someone_else_s_quotation(
        self, session, sales, three_lines
    ):
        with pytest.raises(PermissionDenied):
            quotation_service.remove_line(
                session, sales, three_lines, _by_line_no(three_lines)[0].id
            )
        assert len(three_lines.items) == 3


class TestEdit:
    def test_it_changes_only_the_line_asked_for(self, session, admin, three_lines):
        first, middle, third = _by_line_no(three_lines)
        untouched = (first.net_line_total, third.net_line_total)

        quotation_service.update_line(
            session, admin, three_lines, middle.id, quantity_packs=D("200")
        )
        session.commit()

        assert middle.net_line_total == D("4000.00")
        assert (first.net_line_total, third.net_line_total) == untouched

    def test_the_totals_recalculate_immediately(self, session, admin, three_lines):
        middle = _by_line_no(three_lines)[1]
        quotation_service.update_line(
            session, admin, three_lines, middle.id, quantity_packs=D("200")
        )
        session.commit()
        # 1,000 + 4,000 + 3,000
        assert three_lines.subtotal == three_lines.grand_total == D("8000.00")

    def test_every_field_the_dialog_offers_is_accepted(
        self, session, admin, three_lines
    ):
        """The dialog's save path, exactly as the page calls it."""
        target = _by_line_no(three_lines)[0]
        quotation_service.update_line(
            session, admin, three_lines, target.id,
            quantity_packs=D("250"),
            container_count=D("2"),
            line_discount_pct=D("10"),
            pricing_basis=PricingBasis.PIECE,
            description_override="Printed one colour",
            customer_remarks="Delivered in two drops",
        )
        session.commit()

        assert target.quantity_packs == D("250")
        assert target.container_count == D("2")
        assert target.line_discount_pct == D("10")
        assert target.pricing_basis is PricingBasis.PIECE
        assert target.description_override == "Printed one colour"
        assert target.customer_remarks == "Delivered in two drops"
        assert target.net_line_total == D("2250.00")   # 250 x 10 less 10%

    def test_an_unknown_field_is_refused_rather_than_written(
        self, session, admin, three_lines
    ):
        """The allow-list is what stops the dialog inventing a column."""
        target = _by_line_no(three_lines)[0]
        with pytest.raises(QuotationError, match="Cannot set"):
            quotation_service.update_line(
                session, admin, three_lines, target.id, product_variant_id=99
            )

    def test_changing_the_tier_reprices_that_line_alone(
        self, session, admin, three_lines
    ):
        first, middle, third = _by_line_no(three_lines)
        others = (first.price_per_pack, third.price_per_pack)

        quotation_service.change_line_tier(
            session, admin, three_lines, middle.id,
            PriceTierCode.THREE_CONTAINER.value,
        )
        session.commit()

        # ``expire_on_commit`` is off, so ``item.tier`` still holds the object
        # loaded before the foreign key moved. Expire the row and read it back
        # rather than trusting a relationship this session has already cached.
        session.expire(middle)
        assert middle.tier.code == PriceTierCode.THREE_CONTAINER.value
        assert (first.price_per_pack, third.price_per_pack) == others

    def test_a_custom_price_needs_a_price(self, session, admin, three_lines):
        """Validation the dialog surfaces without closing itself."""
        target = _by_line_no(three_lines)[0]
        with pytest.raises(QuotationError, match="custom price is required"):
            quotation_service.change_line_tier(
                session, admin, three_lines, target.id,
                PriceTierCode.CUSTOM.value, custom_price_per_pack=None,
            )

    def test_a_custom_price_and_its_reason_are_recorded(
        self, session, admin, three_lines
    ):
        target = _by_line_no(three_lines)[0]
        quotation_service.change_line_tier(
            session, admin, three_lines, target.id,
            PriceTierCode.CUSTOM.value,
            custom_price_per_pack=D("8.50"),
            custom_price_reason="Matched a competitor",
        )
        session.commit()

        assert target.is_custom_price
        assert target.price_per_pack == D("8.50")
        assert target.custom_price_reason == "Matched a competitor"
        assert target.net_line_total == D("850.00")

    def test_editing_is_audited(self, session, admin, three_lines):
        from sqlalchemy import select

        from modules.models import AuditLog

        target = _by_line_no(three_lines)[0]
        before = len(session.execute(select(AuditLog)).scalars().all())
        quotation_service.update_line(
            session, admin, three_lines, target.id, quantity_packs=D("500")
        )
        session.commit()
        after = session.execute(select(AuditLog)).scalars().all()
        assert len(after) > before
        assert any(row.entity_id == target.id for row in after)

    def test_sales_cannot_edit_someone_else_s_quotation(
        self, session, sales, three_lines
    ):
        target = _by_line_no(three_lines)[0]
        with pytest.raises(PermissionDenied):
            quotation_service.update_line(
                session, sales, three_lines, target.id, quantity_packs=D("999")
            )
        assert target.quantity_packs == D("100")

    def test_a_line_that_has_been_deleted_cannot_be_edited(
        self, session, admin, three_lines
    ):
        """Two dialogs open on the same line, one saved after the other deleted."""
        target = _by_line_no(three_lines)[0]
        line_id = target.id
        quotation_service.remove_line(session, admin, three_lines, line_id)
        session.commit()

        with pytest.raises(QuotationError, match="not part of this quotation"):
            quotation_service.update_line(
                session, admin, three_lines, line_id, quantity_packs=D("1")
            )
        assert session.get(QuotationItem, line_id) is None


class TestDuplicate:
    def test_it_copies_one_line_and_leaves_the_rest(
        self, session, admin, three_lines
    ):
        middle = _by_line_no(three_lines)[1]
        before = three_lines.grand_total

        quotation_service.duplicate_line(session, admin, three_lines, middle.id)
        session.commit()

        assert len(three_lines.items) == 4
        assert three_lines.grand_total == before + middle.net_line_total
        copies = [i for i in three_lines.items if i.size_label == middle.size_label]
        assert len(copies) == 2
        assert copies[0].id != copies[1].id
