"""Customer freight on the quotation: totals, document, portal, revisions.

Two shipping figures exist in this system and they are unrelated:

* ``settings_service.total_fob_cost`` -- $700 a container, spent getting goods
  to the ship. It is an input to ``supplier_pricing`` and is already inside
  every selling price. It is never a charge, never a line, and never appears
  on a customer document.
* The ocean freight the customer is quoted -- entered per container on the
  shipment, summed into ``QuotationShipment.total_freight``, and surfaced as
  exactly one derived ``QuotationCharge`` when the freight method says so.

The tests here pin the second and assert the first stays out of it.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules import (
    freight_policy,
    pricing_snapshot,
    quotation_service,
    revision_service,
    shipping_service,
)
from modules.constants import (
    CHARGE_SOURCE_SHIPMENT,
    ChargeType,
    ContainerSize,
    FreightMethod,
    ItemInclusion,
    PriceTierCode,
    RoleCode,
)
from modules.customer_service import create_customer
from modules.catalogue_service import create_product, create_variant, set_price
from modules.models import QuotationCharge, ShippingLine
from modules.validation import CustomerInput, PriceInput, ProductInput, VariantInput

JAN = dt.date(2026, 1, 1)
QUOTE_DAY = dt.date(2026, 8, 15)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def admin(make_auth_user):
    return make_auth_user(RoleCode.SYS_ADMIN.value)


@pytest.fixture
def carrier(session, seeded):
    line = ShippingLine(name="Test Line", is_active=True)
    session.add(line)
    session.commit()
    return line


def _priced_variant(session, admin, size, price, case_pack=50):
    product = create_product(
        session, admin,
        ProductInput(
            item_number=f"WB-{size}", name=f'{size}" White', size_label=f'{size}" White',
            flute="B", depth_in=D("2"),
        ),
    )
    session.flush()
    variant = create_variant(
        session, admin, product.id,
        VariantInput(
            variant_item_number=f"WB-{size}-A",
            board_quality="WT110 HPFL115 KM135", case_pack=case_pack,
        ),
    )
    set_price(
        session, admin,
        PriceInput(
            product_variant_id=variant.id,
            price_tier_code=PriceTierCode.STANDARD.value,
            price_per_pack=D(price), effective_from=JAN,
        ),
    )
    session.flush()
    return variant


@pytest.fixture
def quotation(session, admin):
    """Product subtotal $20,000.00 across two included lines."""
    customer = create_customer(
        session, admin,
        CustomerInput(customer_number="CUST-0100", company_name="Reconciliation Ltd"),
    )
    session.flush()
    quote = quotation_service.create_draft(
        session, admin, customer.id, quote_date=QUOTE_DAY
    )
    for size, price, packs in (("12", "10.00", "1200"), ("16", "8.00", "1000")):
        variant = _priced_variant(session, admin, size, price)
        quotation_service.add_line(
            session, admin, quote,
            product_variant_id=variant.id,
            price_tier_code=PriceTierCode.STANDARD.value,
            quantity_packs=D(packs),
        )
    session.commit()
    return quote


def _add_freight(session, admin, quotation, carrier, per_container, count="1",
                 method=FreightMethod.ADDED_SEPARATELY, taxable=True):
    """Record ocean freight the way the application does, and switch it on."""
    shipping_service.add_container(
        session, admin, quotation,
        shipping_line_id=carrier.id,
        container_size=ContainerSize.FORTY_FT_HC,
        container_count=D(count),
        freight_cost=D(per_container),
    )
    shipping_service.update_shipment(
        session, admin, quotation,
        freight_method=method, freight_taxable=taxable,
    )
    session.commit()


def _freight_charges(session, quotation_id):
    return session.query(QuotationCharge).filter_by(
        quotation_id=quotation_id, source=CHARGE_SOURCE_SHIPMENT
    ).all()


# --------------------------------------------------------------------------- #
# The reconciliation the business asked for
# --------------------------------------------------------------------------- #

class TestReconciliation:
    """Product $20,000 + option $1,000 + freight $4,400, tax 13%, deposit 50%."""

    @pytest.fixture
    def reconciliation(self, session, admin, quotation, carrier):
        option = _priced_variant(session, admin, "18", "10.00")
        quotation_service.add_line(
            session, admin, quotation,
            product_variant_id=option.id,
            price_tier_code=PriceTierCode.STANDARD.value,
            quantity_packs=D("100"),
        )
        session.flush()
        line = sorted(quotation.items, key=lambda i: i.line_no)[-1]
        line.inclusion = ItemInclusion.OPTIONAL
        quotation.tax_rate_pct = D("13")
        quotation.deposit_pct = D("50")
        session.flush()
        _add_freight(session, admin, quotation, carrier, "4400")
        return quotation, line

    def test_the_worked_example_reconciles_exactly(self, session, reconciliation):
        quotation, option = reconciliation
        snap = pricing_snapshot.price(
            quotation, pricing_snapshot.PriceScope.SELECTED,
            selected_ids=[option.id],
        )
        assert snap.subtotal == D("21000.00")        # 20,000 product + 1,000 option
        assert snap.charges_total == D("4400.00")    # freight, exactly once
        assert snap.taxable_base == D("25400.00")    # subtotal before tax
        assert snap.tax_amount == D("3302.00")       # 25,400 x 13%
        assert snap.grand_total == D("28702.00")
        assert snap.deposit_due == D("14351.00")     # 28,702 x 50%
        assert snap.grand_total - snap.deposit_due == D("14351.00")   # balance

    def test_the_base_offer_excludes_the_option_but_keeps_freight(
        self, session, reconciliation
    ):
        """An unselected option costs nothing; freight is not optional."""
        quotation, _ = reconciliation
        snap = pricing_snapshot.base(quotation)
        assert snap.subtotal == D("20000.00")
        assert snap.charges_total == D("4400.00")
        assert snap.taxable_base == D("24400.00")


# --------------------------------------------------------------------------- #
# Freight reaches the total, exactly once
# --------------------------------------------------------------------------- #

class TestFreightInTheTotals:
    def test_a_quotation_with_no_freight_is_unaffected(self, session, quotation):
        snap = pricing_snapshot.base(quotation)
        assert snap.charges_total == D("0")
        assert snap.grand_total == snap.subtotal == D("20000.00")

    def test_freight_is_added_once(self, session, admin, quotation, carrier):
        _add_freight(session, admin, quotation, carrier, "4400")
        assert len(_freight_charges(session, quotation.id)) == 1
        snap = pricing_snapshot.base(quotation)
        assert snap.grand_total == D("24400.00")

    def test_repeated_recalculation_does_not_accumulate(
        self, session, admin, quotation, carrier
    ):
        """The defect this shape is most prone to."""
        _add_freight(session, admin, quotation, carrier, "4400")
        for _ in range(5):
            shipping_service.sync_freight(session, admin, quotation)
            quotation_service.recompute_totals(session, quotation)
        session.commit()
        assert len(_freight_charges(session, quotation.id)) == 1
        assert quotation.grand_total == D("24400.00")

    def test_multiple_containers_multiply(self, session, admin, quotation, carrier):
        _add_freight(session, admin, quotation, carrier, "4400", count="2")
        assert pricing_snapshot.base(quotation).charges_total == D("8800.00")

    def test_the_stored_totals_match_the_snapshot(
        self, session, admin, quotation, carrier
    ):
        """Stored figures are what history, approval and reports read."""
        _add_freight(session, admin, quotation, carrier, "4400")
        snap = pricing_snapshot.base(quotation)
        assert quotation.grand_total == snap.grand_total
        assert quotation.charges_total == snap.charges_total


# --------------------------------------------------------------------------- #
# Tax treatment -- configured, not assumed
# --------------------------------------------------------------------------- #

class TestFreightTax:
    def test_freight_is_taxed_when_marked_taxable(
        self, session, admin, quotation, carrier
    ):
        quotation.tax_rate_pct = D("13")
        session.flush()
        _add_freight(session, admin, quotation, carrier, "4400", taxable=True)
        snap = pricing_snapshot.base(quotation)
        assert snap.taxable_base == D("24400.00")
        assert snap.tax_amount == D("3172.00")

    def test_freight_marked_non_taxable_is_excluded_from_the_tax_base(
        self, session, admin, quotation, carrier
    ):
        """Non-taxable charges are added *after* tax -- the point of the flag."""
        quotation.tax_rate_pct = D("13")
        session.flush()
        _add_freight(session, admin, quotation, carrier, "4400", taxable=False)
        snap = pricing_snapshot.base(quotation)
        assert snap.taxable_base == D("20000.00")
        assert snap.tax_amount == D("2600.00")
        assert snap.grand_total == D("27000.00")     # 20,000 + 2,600 + 4,400


# --------------------------------------------------------------------------- #
# The internal $700 stays internal
# --------------------------------------------------------------------------- #

class TestTheFobAllocationStaysOut:
    def test_no_charge_is_created_from_the_fob_setting(
        self, session, admin, quotation, carrier
    ):
        """$700 a container is inside the selling price and must not be billed."""
        from modules import settings_service

        assert settings_service.total_fob_cost(session) == D("700")
        _add_freight(session, admin, quotation, carrier, "4400")
        charges = session.query(QuotationCharge).filter_by(
            quotation_id=quotation.id).all()
        assert [c.amount for c in charges] == [D("4400.00")]
        assert all(c.amount != D("700") for c in charges)

    def test_freight_included_in_the_price_creates_no_charge(
        self, session, admin, quotation, carrier
    ):
        """The other direction: an INCLUDED shipment must not bill anything.

        Every charge counts toward the grand total regardless of customer
        visibility, so an 'included' charge would bill freight twice -- once
        inside the unit price and once on top.
        """
        _add_freight(session, admin, quotation, carrier, "4400",
                     method=FreightMethod.INCLUDED)
        assert _freight_charges(session, quotation.id) == []
        assert pricing_snapshot.base(quotation).grand_total == D("20000.00")


# --------------------------------------------------------------------------- #
# Money safety
# --------------------------------------------------------------------------- #

class TestMoneySafety:
    def test_freight_is_stored_as_decimal_not_text(
        self, session, admin, quotation, carrier
    ):
        _add_freight(session, admin, quotation, carrier, "4400.55")
        shipment = shipping_service.get_shipment(session, quotation.id)
        assert isinstance(shipment.total_freight, D)
        charge = _freight_charges(session, quotation.id)[0]
        assert isinstance(charge.amount, D)
        assert charge.amount == D("4400.55")

    def test_a_negative_freight_cost_is_refused(
        self, session, admin, quotation, carrier
    ):
        with pytest.raises(Exception):
            shipping_service.add_container(
                session, admin, quotation,
                shipping_line_id=carrier.id,
                container_size=ContainerSize.FORTY_FT_HC,
                container_count=D("1"),
                freight_cost=D("-100"),
            )

    def test_the_per_container_rate_is_rounded_before_it_is_multiplied(
        self, session, admin, quotation, carrier
    ):
        """A freight rate is money, so it is 2 dp before the count applies.

        1466.666 stores as 1466.67 and three containers come to 4,400.01, not
        the 4,399.998 an unrounded multiplication would give. Asserted because
        the alternative -- keeping the rate at full precision and rounding only
        the total -- is a defensible design this one deliberately is not, and a
        cent of drift against an invoice is a query.
        """
        _add_freight(session, admin, quotation, carrier, "1466.666", count="3")
        shipment = shipping_service.get_shipment(session, quotation.id)
        assert shipment.containers[0].freight_cost == D("1466.67")
        charge = _freight_charges(session, quotation.id)[0]
        assert charge.amount == D("4400.01")
        assert charge.amount.as_tuple().exponent == -2


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #

def _pdf_text(pdf: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    return "\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)


class TestTheDocument:
    def test_freight_appears_as_its_own_total_row(
        self, session, admin, quotation, carrier
    ):
        from modules import document_model

        _add_freight(session, admin, quotation, carrier, "4400")
        doc = document_model.build_document(session, quotation)
        labels = [t.label for t in doc.totals]
        amounts = [t.amount for t in doc.totals]
        assert any("freight" in lbl.lower() for lbl in labels), labels
        assert any("4,400.00" in a for a in amounts), amounts

    def test_the_rendered_pdf_carries_the_freight_and_the_total(
        self, session, admin, quotation, carrier
    ):
        from modules import document_model, pdf_generator

        _add_freight(session, admin, quotation, carrier, "4400")
        pdf = pdf_generator.render(document_model.build_document(session, quotation))
        text = _pdf_text(pdf)
        assert "4,400.00" in text, "freight is missing from the PDF"
        assert "24,400.00" in text, "the grand total does not include freight"

    def test_no_internal_figure_reaches_the_pdf(
        self, session, admin, quotation, carrier
    ):
        from modules import document_model, pdf_generator

        _add_freight(session, admin, quotation, carrier, "4400")
        text = _pdf_text(
            pdf_generator.render(document_model.build_document(session, quotation))
        )
        for internal in ("Original Cost", "markup", "Markup", "margin", "Margin"):
            assert internal not in text, f"{internal!r} reached the customer PDF"

    def test_every_container_reaches_the_table_in_the_session_that_added_them(
        self, session, admin, quotation, carrier
    ):
        """The freight line and the table have to agree on how many there are.

        ``expire_on_commit`` is off, so ``shipment.containers`` loaded once
        stays stale. Building the document in the same session that added the
        containers — which is what the page and ``create_revision`` both do —
        printed "Ocean freight — 2 containers" above a table listing one.
        """
        from modules import document_model

        shipping_service.add_container(
            session, admin, quotation, shipping_line_id=carrier.id,
            container_size=ContainerSize.FORTY_FT_HC,
            container_count=D("1"), freight_cost=D("4400"),
        )
        # Load the collection while it holds exactly one, as a page render does.
        assert len(quotation.shipment.containers) == 1
        shipping_service.add_container(
            session, admin, quotation, shipping_line_id=carrier.id,
            container_size=ContainerSize.FORTY_FT_HC,
            container_count=D("1"), freight_cost=D("4400"),
        )
        session.commit()

        doc = document_model.build_document(session, quotation)
        assert doc.shipping is not None
        assert len(doc.shipping.rows) == 2, (
            "the table lost a container the freight line still counts"
        )
        freight_row = next(t for t in doc.totals if "freight" in t.label.lower())
        assert "2 containers" in freight_row.label
        assert freight_row.amount == "$8,800.00"
        assert ("Total containers", "2") in doc.shipping.summary

    def test_a_quotation_without_freight_prints_no_freight_row(
        self, session, quotation
    ):
        from modules import document_model

        doc = document_model.build_document(session, quotation)
        assert not any("freight" in t.label.lower() for t in doc.totals)

    def test_the_container_count_is_not_printed_with_trailing_zeros(
        self, session, admin, quotation, carrier
    ):
        """``Decimal`` keeps its zeros under ``:g``: "1.000 container(s)"."""
        _add_freight(session, admin, quotation, carrier, "4400")
        charge = _freight_charges(session, quotation.id)[0]
        assert "1 container" in charge.description, charge.description
        assert "1.000" not in charge.description
        assert "container(s)" not in charge.description

    def test_two_containers_are_pluralised(
        self, session, admin, quotation, carrier
    ):
        _add_freight(session, admin, quotation, carrier, "4400", count="2")
        assert "2 containers" in _freight_charges(session, quotation.id)[0].description


class TestTheTotalsBlock:
    """Subtotal before tax, deposit and balance, as the business stated them."""

    @pytest.fixture
    def with_tax_and_deposit(self, session, admin, quotation, carrier):
        quotation.tax_rate_pct = D("13")
        quotation.deposit_pct = D("50")
        session.flush()
        _add_freight(session, admin, quotation, carrier, "4400")
        return quotation

    def _rows(self, session, quotation):
        from modules import document_model

        return {
            t.label: t.amount
            for t in document_model.build_document(session, quotation).totals
        }

    def test_every_row_the_business_asked_for_is_present(
        self, session, with_tax_and_deposit
    ):
        rows = self._rows(session, with_tax_and_deposit)
        assert rows["Subtotal"] == "$20,000.00"
        assert rows["Ocean freight — 1 container"] == "$4,400.00"
        assert rows["Subtotal before tax"] == "$24,400.00"
        assert rows["Tax (13%)"] == "$3,172.00"
        assert rows["Total (USD)"] == "$27,572.00"
        assert rows["Deposit required (50%)"] == "$13,786.00"
        assert rows["Balance due"] == "$13,786.00"

    def test_the_block_reconciles(self, session, with_tax_and_deposit):
        """Every printed figure derived from the one below or above it."""
        q = with_tax_and_deposit
        assert q.subtotal + q.charges_total == q.grand_total - q.tax_amount
        deposit = D("13786.00")
        assert deposit + (q.grand_total - deposit) == q.grand_total

    def test_the_deposit_is_a_share_of_the_total_including_freight(
        self, session, with_tax_and_deposit
    ):
        """The defect worth guarding: a deposit taken before freight is added.

        50% of 27,572 is 13,786. 50% of the same quotation without freight
        would be 11,300 -- a customer underpaying by 2,486 on deposit.
        """
        rows = self._rows(session, with_tax_and_deposit)
        assert rows["Deposit required (50%)"] == "$13,786.00"
        assert rows["Deposit required (50%)"] != "$11,300.00"

    def test_no_subtotal_before_tax_row_when_there_is_nothing_between(
        self, session, quotation
    ):
        """With no charge it would simply repeat the subtotal above it."""
        quotation.tax_rate_pct = D("13")
        session.flush()
        from modules import quotation_service as qs

        qs.recompute_totals(session, quotation)
        session.flush()
        assert "Subtotal before tax" not in self._rows(session, quotation)

    def test_no_deposit_rows_when_no_deposit_is_asked_for(
        self, session, admin, quotation, carrier
    ):
        _add_freight(session, admin, quotation, carrier, "4400")
        rows = self._rows(session, quotation)
        assert not any("Deposit" in label for label in rows)
        assert "Balance due" not in rows

    def test_the_deposit_prints_below_the_total_not_above_it(
        self, session, with_tax_and_deposit
    ):
        """Order in the *rendered* PDF, which the model alone cannot show.

        ``money_block`` drew every quiet row and then every emphasised one,
        which was indistinguishable from preserving order while the grand total
        was last. Adding a deposit below it put the deposit and balance above
        the total, where they read as two more components summing into it --
        a customer could reasonably add 24,400 + 3,172 + 13,786 + 13,786.
        Asserted against extracted text because the model's list was already in
        the right order; only the rendering was not.
        """
        from modules import document_model, pdf_generator

        text = _pdf_text(
            pdf_generator.render(
                document_model.build_document(session, with_tax_and_deposit)
            )
        )
        total_at = text.find("TOTAL (USD)")
        deposit_at = text.find("Deposit required")
        balance_at = text.find("Balance due")
        assert total_at != -1 and deposit_at != -1 and balance_at != -1
        assert total_at < deposit_at < balance_at, (
            "the deposit and balance were hoisted above the grand total"
        )

    def test_the_document_and_the_portal_agree_on_the_deposit(
        self, session, with_tax_and_deposit
    ):
        """One formula. Two surfaces used to hold their own copy of it."""
        from modules import portal_service

        snap = pricing_snapshot.base(with_tax_and_deposit)
        portal_figure = portal_service.deposit_due(
            with_tax_and_deposit,
            type("T", (), {"grand_total": with_tax_and_deposit.grand_total})(),
        )
        assert snap.deposit_due == portal_figure == D("13786.00")
        rows = self._rows(session, with_tax_and_deposit)
        assert rows["Deposit required (50%)"] == "$13,786.00"


# --------------------------------------------------------------------------- #
# Defaults, warnings and the approval gate
# --------------------------------------------------------------------------- #

class TestDefaults:
    def test_a_new_shipment_charges_freight_and_shows_the_details(
        self, session, admin, quotation, carrier
    ):
        shipping_service.add_container(
            session, admin, quotation, shipping_line_id=carrier.id,
            container_size=ContainerSize.FORTY_FT_HC,
            container_count=D("1"), freight_cost=D("4400"),
        )
        session.commit()
        shipment = shipping_service.get_shipment(session, quotation.id)
        assert shipment.freight_method is FreightMethod.ADDED_SEPARATELY
        assert shipment.show_on_document is True
        assert shipment.customer_visible_freight is True
        assert len(_freight_charges(session, quotation.id)) == 1

    def test_the_freight_column_is_on_the_document_by_default(
        self, session, admin, quotation, carrier
    ):
        """A customer billed $8,800 can see it is two containers at $4,400."""
        from modules import document_model

        _add_freight(session, admin, quotation, carrier, "4400", count="2")
        doc = document_model.build_document(session, quotation)
        assert "freight" in doc.shipping.columns
        assert "Freight" in doc.shipping.headings
        assert any("$4,400.00" in cell for row in doc.shipping.rows for cell in row)

    def test_the_column_can_still_be_turned_off(
        self, session, admin, quotation, carrier
    ):
        """The flag stays authoritative; only its default moved."""
        from modules import document_model

        _add_freight(session, admin, quotation, carrier, "4400")
        shipping_service.update_shipment(
            session, admin, quotation, customer_visible_freight=False
        )
        session.commit()
        doc = document_model.build_document(session, quotation)
        assert "freight" not in doc.shipping.columns
        # The charge in the totals is unaffected: that is what is being billed.
        assert any("4,400.00" in t.amount for t in doc.totals)

    def test_the_model_default_is_the_one_that_applies(self, session):
        """``ensure_shipment`` used to name INCLUDED, making the model dead.

        A default stated in two places is a default stated in the one that
        runs, and for the whole life of this feature that was the constructor
        rather than the column.
        """
        import inspect

        source = inspect.getsource(shipping_service.ensure_shipment)
        assert "freight_method=" not in source
        assert "show_on_document=" not in source

        from modules.models import QuotationShipment

        assert (
            QuotationShipment.__table__.c.freight_method.default.arg
            is FreightMethod.ADDED_SEPARATELY
        )
        assert QuotationShipment.__table__.c.show_on_document.default.arg is True
        assert (
            QuotationShipment.__table__.c.customer_visible_freight.default.arg is True
        )

    def test_two_containers_at_4400_come_to_8800(
        self, session, admin, quotation, carrier
    ):
        """QT-2026-0012's shape: two rows of one container, not one row of two."""
        for _ in range(2):
            shipping_service.add_container(
                session, admin, quotation, shipping_line_id=carrier.id,
                container_size=ContainerSize.FORTY_FT_HC,
                container_count=D("1"), freight_cost=D("4400"),
            )
        session.commit()
        shipment = shipping_service.get_shipment(session, quotation.id)
        assert len(shipment.containers) == 2
        assert shipment.total_freight == D("8800.00")
        assert _freight_charges(session, quotation.id)[0].amount == D("8800.00")
        assert quotation.grand_total == D("28800.00")   # 20,000 + 8,800


class TestWarnings:
    def test_none_on_the_default_configuration(
        self, session, admin, quotation, carrier
    ):
        _add_freight(session, admin, quotation, carrier, "4400")
        assert freight_policy.warnings_for(session, quotation) == []

    def test_recorded_but_not_billable_is_warned(
        self, session, admin, quotation, carrier
    ):
        _add_freight(session, admin, quotation, carrier, "4400",
                     method=FreightMethod.INCLUDED)
        found = freight_policy.warnings_for(session, quotation)
        assert [w.code for w in found] == [freight_policy.RECORDED_NOT_BILLED]
        assert found[0].message == (
            "Freight of $4,400.00 is recorded but will not be added to the "
            "customer total because the freight method is set to Included."
        )

    def test_billable_but_hidden_is_warned(
        self, session, admin, quotation, carrier
    ):
        _add_freight(session, admin, quotation, carrier, "4400")
        shipping_service.update_shipment(
            session, admin, quotation, show_on_document=False
        )
        session.commit()
        found = freight_policy.warnings_for(session, quotation)
        assert [w.code for w in found] == [freight_policy.BILLED_BUT_HIDDEN]
        assert found[0].message == (
            "Freight is included in the customer total but the shipping "
            "details are hidden from the document."
        )

    def test_no_warning_when_the_customer_arranges_their_own_freight(
        self, session, admin, quotation, carrier
    ):
        """Not billing is the intent there, so there is nothing to point out."""
        _add_freight(session, admin, quotation, carrier, "4400",
                     method=FreightMethod.INTERNAL_ONLY)
        assert freight_policy.warnings_for(session, quotation) == []

    def test_the_three_figures_are_reported_separately(
        self, session, admin, quotation, carrier
    ):
        """Recorded, billable, and in the grand total are not the same number."""
        _add_freight(session, admin, quotation, carrier, "4400",
                     method=FreightMethod.INCLUDED)
        assert freight_policy.recorded_freight(quotation) == D("4400.00")
        assert freight_policy.billable_freight(quotation) == D("0.00")
        assert quotation.grand_total == D("20000.00")

        shipping_service.update_shipment(
            session, admin, quotation,
            freight_method=FreightMethod.ADDED_SEPARATELY,
        )
        session.commit()
        assert freight_policy.recorded_freight(quotation) == D("4400.00")
        assert freight_policy.billable_freight(quotation) == D("4400.00")
        assert quotation.grand_total == D("24400.00")


class TestApprovalGate:
    def test_submission_is_refused_until_the_warning_is_acknowledged(
        self, session, admin, quotation, carrier
    ):
        from modules import approval_service

        _add_freight(session, admin, quotation, carrier, "4400",
                     method=FreightMethod.INCLUDED)
        with pytest.raises(approval_service.ApprovalError, match="freight"):
            approval_service.submit(session, quotation, admin)

    def test_acknowledging_lets_it_through_and_is_recorded(
        self, session, admin, quotation, carrier
    ):
        from sqlalchemy import select

        from modules import approval_service
        from modules.models import AuditLog

        _add_freight(session, admin, quotation, carrier, "4400",
                     method=FreightMethod.INCLUDED)
        approval_service.submit(
            session, quotation, admin, acknowledged_freight=True
        )
        session.commit()

        reasons = [
            row.reason for row in session.execute(select(AuditLog)).scalars()
            if row.reason and "freight configuration acknowledged" in row.reason
        ]
        assert reasons, "the acknowledgement was not recorded"
        assert freight_policy.RECORDED_NOT_BILLED in reasons[0]

    def test_the_default_configuration_needs_no_acknowledgement(
        self, session, admin, quotation, carrier
    ):
        from modules import approval_service

        _add_freight(session, admin, quotation, carrier, "4400")
        approval_service.submit(session, quotation, admin)
        session.commit()
        assert quotation.grand_total == D("24400.00")


# --------------------------------------------------------------------------- #
# Revisions
# --------------------------------------------------------------------------- #

class TestRevisions:
    @pytest.fixture
    def issued(self, session, admin, quotation, carrier):
        """Freight recorded, then locked -- the state a revision starts from."""
        _add_freight(session, admin, quotation, carrier, "4400")
        revision_service.issue(session, admin, quotation)
        session.commit()
        return quotation

    def test_a_revision_carries_the_freight_and_bills_it_once(
        self, session, admin, issued
    ):
        """The derived charge is not copied -- sync_freight rebuilds it.

        Copying it *and* rebuilding it is the obvious way to bill freight twice
        on every revision, so the copy deliberately omits it.
        """
        revised = revision_service.create_revision(
            session, admin, issued, "freight correction"
        )
        session.commit()

        assert len(_freight_charges(session, revised.id)) == 1
        assert revised.grand_total == D("24400.00")

    def test_changing_the_freight_on_a_revision_leaves_the_original_alone(
        self, session, admin, issued
    ):
        """An issued quotation keeps the figures it was issued with."""
        original_total = issued.grand_total
        assert original_total == D("24400.00")

        revised = revision_service.create_revision(
            session, admin, issued, "more freight"
        )
        session.flush()
        container = shipping_service.get_shipment(session, revised.id).containers[0]
        shipping_service.update_container(
            session, admin, revised, container.id, freight_cost=D("5000")
        )
        session.commit()

        assert revised.grand_total == D("25000.00")
        assert issued.grand_total == original_total == D("24400.00")

    def test_the_issued_quotation_cannot_be_edited_at_all(
        self, session, admin, issued
    ):
        """Immutability is enforced, not merely relied upon.

        The freight configuration on an issued quotation is wrong often enough
        that the temptation to reach in and fix it is real. It is refused, and
        a revision is the only route.
        """
        from modules.authorization import PermissionDenied

        assert issued.is_locked
        with pytest.raises(PermissionDenied):
            shipping_service.update_shipment(
                session, admin, issued, freight_method=FreightMethod.INCLUDED
            )
        with pytest.raises(PermissionDenied):
            container = shipping_service.get_shipment(session, issued.id).containers[0]
            shipping_service.update_container(
                session, admin, issued, container.id, freight_cost=D("1")
            )
        assert issued.grand_total == D("24400.00")

    def test_a_revision_of_a_wrongly_configured_quotation_bills_the_freight(
        self, session, admin, quotation, carrier
    ):
        """QT-2026-0012's remedy, end to end.

        Freight recorded under INCLUDED and issued that way; the revision is
        switched to the corrected configuration and the total moves by exactly
        the recorded freight, once.
        """
        _add_freight(session, admin, quotation, carrier, "4400", count="2",
                     method=FreightMethod.INCLUDED)
        shipping_service.update_shipment(
            session, admin, quotation, show_on_document=False
        )
        revision_service.issue(session, admin, quotation)
        session.commit()

        assert quotation.grand_total == D("20000.00")
        assert freight_policy.recorded_freight(quotation) == D("8800.00")
        assert freight_policy.billable_freight(quotation) == D("0.00")

        revised = revision_service.create_revision(
            session, admin, quotation, "freight was not being charged"
        )
        session.flush()
        shipping_service.update_shipment(
            session, admin, revised,
            freight_method=FreightMethod.ADDED_SEPARATELY,
            show_on_document=True,
        )
        session.commit()

        assert freight_policy.warnings_for(session, revised) == []
        assert len(_freight_charges(session, revised.id)) == 1
        assert revised.grand_total == D("28800.00")   # 20,000 + 8,800, once
        assert quotation.grand_total == D("20000.00"), "the issued copy moved"

        from modules import document_model

        doc = document_model.build_document(session, revised)
        assert any("8,800.00" in t.amount for t in doc.totals)
        assert doc.shipping is not None, "shipping details still hidden"
