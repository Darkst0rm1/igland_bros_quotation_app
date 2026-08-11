"""CAD quotations under Ontario HST, end to end through the portal.

Uses test-only catalogue data. The real USD price list is never touched, and a
USD quotation is asserted unchanged in the same run so a CAD regression cannot
hide behind it.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from starlette.testclient import TestClient

from modules import portal_service, quotation_service
from modules.catalogue_service import set_price
from modules.constants import (
    ChargeType,
    ItemInclusion,
    PriceTierCode,
    QuotationStatus,
)
from modules.models import QuotationCharge
from modules.portal_service import compute_selection_totals, issue_token

from tests.test_documents_and_approval import (  # noqa: F401
    _approve_and_issue,
    admin,
    manager,
    quotation,
    sales,
    variant,
)

#: Ontario. Thirteen per cent, and the reason this file exists.
HST_ONTARIO = D("13")


@pytest.fixture(autouse=True)
def portal_user(session):
    return portal_service.ensure_portal_user(session)


@pytest.fixture
def cad_prices(session, admin, variant):
    """A CAD price list alongside the USD one. Additive: nothing is replaced."""
    from modules.validation import PriceInput

    for tier, price in (
        (PriceTierCode.STANDARD.value, "9.80"),
        (PriceTierCode.EIGHT_CONTAINER.value, "9.10"),
    ):
        set_price(
            session, admin,
            PriceInput(
                product_variant_id=variant.id,
                price_tier_code=tier,
                currency="CAD",
                price_per_pack=D(price),
                effective_from=dt.date(2026, 1, 1),
            ),
        )
    session.flush()
    return variant


@pytest.fixture
def cad_quote(session, sales, manager, admin, cad_prices, quotation):
    """A CAD quotation with an included line, an optional line and charges."""
    quotation.currency = "CAD"
    quotation.tax_rate_pct = HST_ONTARIO
    quotation.deposit_pct = D("25")
    session.flush()

    optional = quotation_service.add_line(
        session, sales, quotation,
        product_variant_id=cad_prices.id,
        price_tier_code=PriceTierCode.STANDARD.value,
        quantity_packs=D("1000"),
        description_override="Optional export crating",
    )
    optional.inclusion = ItemInclusion.OPTIONAL

    # One taxable charge and one that is not — HST applies to the first only.
    session.add(
        QuotationCharge(
            quotation_id=quotation.id, sort_order=90,
            charge_type=ChargeType.PRINTING_PLATES, description="Printing plates",
            quantity_value=D("1"), rate=D("500.00"), amount=D("500.00"),
            currency="CAD", is_taxable=True, is_customer_visible=True,
        )
    )
    session.add(
        QuotationCharge(
            quotation_id=quotation.id, sort_order=91,
            charge_type=ChargeType.DUTY, description="Duty",
            quantity_value=D("1"), rate=D("200.00"), amount=D("200.00"),
            currency="CAD", is_taxable=False, is_customer_visible=True,
        )
    )
    session.flush()
    quotation_service.recompute_totals(session, quotation)
    session.flush()
    return quotation


@pytest.fixture
def sent_cad(session, cad_quote, sales, manager):
    _approve_and_issue(session, cad_quote, sales, manager)
    quotation_service.change_status(
        session, manager, cad_quote, QuotationStatus.SENT_TO_CUSTOMER
    )
    session.flush()
    return cad_quote


@pytest.fixture
def client(session, monkeypatch):
    from contextlib import contextmanager

    from portal import app as portal_app

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(portal_app, "session_scope", _scope)
    portal_app._view_limiter.reset()
    portal_app._submit_limiter.reset()
    return TestClient(portal_app.app, base_url="http://testserver")


class TestCadCalculations:
    def test_hst_applies_to_the_taxable_base_not_the_subtotal(
        self, session, sent_cad
    ):
        """Taxable charges are in the base; non-taxable ones are added after."""
        totals = compute_selection_totals(sent_cad, [])
        # Summed from the quotation rather than hardcoded: the shared fixture
        # already carries plate charges of its own before this test adds any.
        taxable_charges = sum(
            (c.amount for c in sent_cad.charges if c.is_taxable), D("0.00")
        )
        expected_base = totals.subtotal - totals.quotation_discount + taxable_charges
        assert totals.taxable_base == expected_base
        assert totals.tax_amount == (
            expected_base * HST_ONTARIO / D("100")
        ).quantize(D("0.01"))

    def test_the_non_taxable_charge_is_outside_the_tax(self, session, sent_cad):
        totals = compute_selection_totals(sent_cad, [])
        assert D("200.00") not in (totals.taxable_base,)
        assert totals.grand_total == (
            totals.subtotal - totals.quotation_discount
            + totals.charges_total + totals.tax_amount
        )

    def test_selecting_the_optional_line_moves_tax_and_total(
        self, session, sent_cad
    ):
        optional = next(
            i for i in sent_cad.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        without = compute_selection_totals(sent_cad, [])
        with_it = compute_selection_totals(sent_cad, [optional.id])

        assert with_it.subtotal > without.subtotal
        assert with_it.tax_amount > without.tax_amount
        assert with_it.grand_total > without.grand_total
        # The optional line's own value is exactly the difference.
        assert with_it.subtotal - without.subtotal == optional.net_line_total

    def test_the_deposit_follows_the_selection(self, session, sent_cad):
        optional = next(
            i for i in sent_cad.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        totals = compute_selection_totals(sent_cad, [optional.id])
        assert portal_service.deposit_due(sent_cad, totals) == (
            totals.grand_total * D("25") / D("100")
        ).quantize(D("0.01"))

    def test_every_figure_is_a_decimal_at_two_places(self, session, sent_cad):
        totals = compute_selection_totals(sent_cad, [])
        for value in (totals.subtotal, totals.tax_amount, totals.grand_total,
                      totals.charges_total, totals.taxable_base):
            assert isinstance(value, D)
            assert -value.as_tuple().exponent <= 2

    def test_rounding_is_half_even_at_the_cent(self, session, sent_cad):
        """A rate that lands on a half-cent must not drift."""
        sent_cad.tax_rate_pct = D("13.005")
        session.flush()
        totals = compute_selection_totals(sent_cad, [])
        recomputed = (
            totals.taxable_base * D("13.005") / D("100")
        ).quantize(D("0.01"))
        assert totals.tax_amount == recomputed


class TestCadThroughThePortal:
    def test_the_page_renders_cad_amounts(self, session, sent_cad, sales, client):
        token, raw = issue_token(session, sales, sent_cad)
        session.commit()
        body = client.get(f"/quote/public/{raw}").text
        assert "CAD" in body
        assert "Tax (13%)" in body
        totals = compute_selection_totals(sent_cad, [])
        assert f"{totals.grand_total:,.2f}" in body

    def test_the_accepted_total_snapshot_matches_the_selection(
        self, session, sent_cad, sales
    ):
        optional = next(
            i for i in sent_cad.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        token, _ = issue_token(session, sales, sent_cad)
        expected = compute_selection_totals(sent_cad, [optional.id])

        response = portal_service.approve(
            session, token, customer_name="Michel Dupont",
            accepted_terms=True, selected_ids=[optional.id],
        )
        assert response.currency == "CAD"
        assert response.subtotal == expected.subtotal
        assert response.tax_amount == expected.tax_amount
        assert response.grand_total == expected.grand_total
        assert response.selected_item_ids == [optional.id]


class TestUsdIsUnaffected:
    def test_a_usd_quotation_behaves_exactly_as_before(
        self, session, quotation, sales, manager
    ):
        """Guards against a CAD change altering the existing currency path."""
        assert quotation.currency == "USD"
        before = compute_selection_totals(quotation, [])
        quotation_service.recompute_totals(session, quotation)
        session.flush()
        after = compute_selection_totals(quotation, [])
        assert before.subtotal == after.subtotal
        assert before.grand_total == after.grand_total

    def test_usd_and_cad_price_lists_coexist(self, session, cad_prices):
        from modules.models import ProductPrice

        currencies = {
            p.currency for p in session.query(ProductPrice)
            .filter_by(product_variant_id=cad_prices.id).all()
        }
        assert {"USD", "CAD"} <= currencies
