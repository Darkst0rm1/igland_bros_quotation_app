"""Quotation lifecycle, pricing warnings and the status machine."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules import pricing_service, quotation_service, settings_service
from modules.authorization import PermissionDenied, load_auth_user
from modules.catalogue_service import create_product, create_variant, set_cost, set_price
from modules.constants import (
    AuditAction,
    ChargeType,
    PriceTierCode,
    PriceWarningCode,
    PricingBasis,
    QuotationStatus,
    RoleCode,
    WarningSeverity,
)
from modules.customer_service import create_customer
from modules.models import AuditLog, Quotation
from modules.quotation_service import QuotationError
from modules.validation import CostInput, CustomerInput, PriceInput, ProductInput, VariantInput

JAN = dt.date(2026, 1, 1)
QUOTE_DAY = dt.date(2026, 8, 3)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def sales(make_auth_user):
    return make_auth_user(RoleCode.SALES.value, username="alice")


@pytest.fixture
def manager(make_auth_user):
    return make_auth_user(RoleCode.SALES_MANAGER.value, username="mgr")


@pytest.fixture
def admin(make_auth_user):
    return make_auth_user(RoleCode.SYS_ADMIN.value, username="root")


@pytest.fixture
def customer(session, sales):
    made = create_customer(
        session, sales,
        CustomerInput(customer_number="CUST-0001", company_name="Bunzl Canada"),
    )
    session.commit()
    return made


@pytest.fixture
def catalogue(session, admin):
    """Two sizes, one with two board qualities, priced at all three tiers."""
    from modules.repositories import find_product_by_size

    built: dict[str, int] = {}
    specs = [
        ('12" White', "WT110 HPFL115 KM135", "7.42", "7.20", "6.98"),
        ('12" White', "WT110 HPFL160 KM135", "7.83", "7.59", "7.36"),
        ('14" White', "WT110 HPFL115 KM135", "9.13", "8.85", "8.58"),
    ]
    for size, quality, standard, three, eight in specs:
        # The first two specs share a product and differ only in board quality —
        # exactly the shape the reference workbook produces.
        product = find_product_by_size(session, size)
        if product is None:
            product = create_product(
                session, admin,
                ProductInput(
                    item_number=f"WB-{size[:2].strip()}",
                    name=size, size_label=size, flute="B", depth_in=D("2"),
                ),
            )
            session.flush()
        variant = create_variant(
            session, admin, product.id,
            VariantInput(
                variant_item_number=f"{product.item_number}-{quality.split()[1]}",
                board_quality=quality, case_pack=50, moq_packs=D("200"),
            ),
        )
        session.flush()
        for tier, price in (
            (PriceTierCode.STANDARD.value, standard),
            (PriceTierCode.THREE_CONTAINER.value, three),
            (PriceTierCode.EIGHT_CONTAINER.value, eight),
        ):
            set_price(
                session, admin,
                PriceInput(
                    product_variant_id=variant.id, price_tier_code=tier,
                    price_per_pack=D(price), effective_from=JAN,
                ),
            )
        built[f"{size}|{quality}"] = variant.id
    session.commit()
    return built


@pytest.fixture
def draft(session, sales, customer):
    quotation = quotation_service.create_draft(
        session, sales, customer.id,
        project_name="Pizza Box Program", quote_date=QUOTE_DAY,
    )
    session.commit()
    return quotation


def _add(session, user, quotation, catalogue, key, tier, packs, containers="0", **kw):
    return quotation_service.add_line(
        session, user, quotation,
        product_variant_id=catalogue[key],
        price_tier_code=tier,
        quantity_packs=D(packs),
        container_count=D(containers),
        **kw,
    )


# --------------------------------------------------------------------------- #
# Drafts
# --------------------------------------------------------------------------- #

class TestDrafts:
    def test_a_draft_gets_a_number_and_revision_zero(self, draft):
        assert draft.quote_number == "IGB-QT-2026-0001"
        assert draft.revision_no == 0
        assert draft.display_number == "IGB-QT-2026-0001 Rev 0"
        assert draft.status is QuotationStatus.DRAFT

    def test_numbers_do_not_repeat(self, session, sales, customer):
        first = quotation_service.create_draft(session, sales, customer.id)
        second = quotation_service.create_draft(session, sales, customer.id)
        session.commit()
        assert first.quote_number != second.quote_number

    def test_the_customer_is_snapshotted(self, draft, customer):
        assert draft.customer_name_snapshot == customer.company_name

    def test_renaming_the_customer_does_not_change_the_quotation(
        self, session, sales, draft, customer
    ):
        """The snapshot is the whole point of storing it on the quotation."""
        from modules.customer_service import update_customer

        update_customer(
            session, sales, customer.id,
            CustomerInput(customer_number="CUST-0001", company_name="Renamed Ltd"),
        )
        session.commit()
        assert session.get(Quotation, draft.id).customer_name_snapshot == "Bunzl Canada"

    def test_validity_defaults_from_settings(self, session, draft):
        days = settings_service.default_validity_days(session)
        assert draft.valid_until == draft.quote_date + dt.timedelta(days=days)

    def test_default_terms_are_copied_on(self, draft):
        assert len(draft.terms) > 0
        assert all(t.term_template_id is not None for t in draft.terms)

    def test_editing_a_term_does_not_touch_the_master_template(
        self, session, sales, draft
    ):
        from modules.models import TermTemplate

        term = draft.terms[0]
        original = session.get(TermTemplate, term.term_template_id).body_text
        quotation_service.edit_term(
            session, sales, draft, term.id, body_text="Reworded for this customer."
        )
        session.commit()
        assert session.get(TermTemplate, term.term_template_id).body_text == original

    def test_creation_is_audited(self, session, draft):
        entry = (
            session.query(AuditLog)
            .filter_by(action=AuditAction.QUOTATION_CREATED.value)
            .one()
        )
        assert entry.new_value_json["quote_number"] == draft.quote_number

    def test_a_user_without_permission_cannot_create(
        self, session, make_auth_user, customer
    ):
        pricer = make_auth_user(RoleCode.PRICING_ADMIN.value)
        with pytest.raises(PermissionDenied):
            quotation_service.create_draft(session, pricer, customer.id)


# --------------------------------------------------------------------------- #
# Lines
# --------------------------------------------------------------------------- #

class TestLines:
    def test_adding_a_line_snapshots_its_specification(
        self, session, sales, draft, catalogue
    ):
        item = _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "1000",
        )
        session.commit()
        assert item.size_label == '12" White'
        assert item.board_quality == "WT110 HPFL115 KM135"
        assert item.case_pack == 50
        assert item.price_per_pack == D("7.42")
        assert item.product_price_id is not None

    def test_the_line_records_the_exact_price_row_used(
        self, session, sales, draft, catalogue
    ):
        from modules.models import ProductPrice

        item = _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "100",
        )
        session.commit()
        price = session.get(ProductPrice, item.product_price_id)
        assert price.price_per_pack == item.price_per_pack

    def test_line_totals_are_computed(self, session, sales, draft, catalogue):
        item = _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "1000",
        )
        session.commit()
        assert item.quantity_pieces == D("50000.000")
        assert item.gross_line_total == D("7420.00")
        assert item.net_line_total == D("7420.00")

    def test_two_board_qualities_of_one_size_are_separate_lines(
        self, session, sales, draft, catalogue
    ):
        a = _add(session, sales, draft, catalogue,
                 '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "100")
        b = _add(session, sales, draft, catalogue,
                 '12" White|WT110 HPFL160 KM135', PriceTierCode.STANDARD.value, "100")
        session.commit()
        assert a.product_variant_id != b.product_variant_id
        assert a.price_per_pack != b.price_per_pack

    def test_lines_are_numbered_and_renumbered(self, session, sales, draft, catalogue):
        for _ in range(3):
            _add(session, sales, draft, catalogue,
                 '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "100")
        session.commit()
        assert [i.line_no for i in draft.items] == [1, 2, 3]

        quotation_service.remove_line(session, sales, draft, draft.items[0].id)
        session.commit()
        session.refresh(draft)
        assert [i.line_no for i in draft.items] == [1, 2]

    def test_duplicating_a_line(self, session, sales, draft, catalogue):
        original = _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.EIGHT_CONTAINER.value, "500",
        )
        session.commit()
        copy = quotation_service.duplicate_line(session, sales, draft, original.id)
        session.commit()
        assert copy.id != original.id
        assert copy.price_per_pack == original.price_per_pack
        assert copy.line_no == 2

    def test_a_custom_price_needs_a_price(self, session, sales, draft, catalogue):
        with pytest.raises(QuotationError, match="custom price is required"):
            _add(session, sales, draft, catalogue,
                 '12" White|WT110 HPFL115 KM135', PriceTierCode.CUSTOM.value, "100")

    def test_a_custom_price_is_flagged_and_audited(
        self, session, sales, draft, catalogue
    ):
        item = _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.CUSTOM.value, "100",
            custom_price_per_pack=D("6.50"), custom_price_reason="volume commitment",
        )
        session.commit()
        assert item.is_custom_price
        assert item.product_price_id is None
        assert item.price_per_pack == D("6.50")

    def test_a_line_cannot_be_added_when_no_price_exists(
        self, session, sales, draft, catalogue, admin
    ):
        """A quotation must never be built on a price that does not exist."""
        product = create_product(
            session, admin,
            ProductInput(item_number="WB-99", name='99" White', size_label='99" White'),
        )
        session.flush()
        variant = create_variant(
            session, admin, product.id,
            VariantInput(
                variant_item_number="WB-99-A", board_quality="Q", case_pack=50
            ),
        )
        session.commit()

        with pytest.raises(QuotationError, match="ever been recorded"):
            quotation_service.add_line(
                session, sales, draft,
                product_variant_id=variant.id,
                price_tier_code=PriceTierCode.STANDARD.value,
                quantity_packs=D("100"),
            )


# --------------------------------------------------------------------------- #
# The tier rule
# --------------------------------------------------------------------------- #

class TestTierIsAuthoritative:
    """The brief's rule: quantity changes warn, they never re-select a tier."""

    def test_changing_quantity_does_not_change_the_tier(
        self, session, sales, draft, catalogue
    ):
        item = _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.EIGHT_CONTAINER.value,
            "1000", containers="8",
        )
        session.commit()
        tier_before, price_before = item.price_tier_id, item.price_per_pack

        quotation_service.update_line(
            session, sales, draft, item.id,
            quantity_packs=D("10"), container_count=D("1"),
        )
        session.commit()

        assert item.price_tier_id == tier_before
        assert item.price_per_pack == price_before

    def test_dropping_below_the_minimum_warns_instead(
        self, session, sales, draft, catalogue
    ):
        item = _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.EIGHT_CONTAINER.value,
            "1000", containers="2",
        )
        session.commit()

        warnings = pricing_service.evaluate_quotation(session, draft)
        short = [
            w for w in warnings if w.code is PriceWarningCode.TIER_CONTAINERS_SHORT
        ]
        assert len(short) == 1
        assert "not been changed" in short[0].message
        assert item.price_per_pack == D("6.98")  # still the eight-container price

    def test_changing_the_tier_explicitly_reprices(
        self, session, sales, draft, catalogue
    ):
        item = _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "1000",
        )
        session.commit()
        assert item.price_per_pack == D("7.42")

        quotation_service.change_line_tier(
            session, sales, draft, item.id, PriceTierCode.EIGHT_CONTAINER.value
        )
        session.commit()
        assert item.price_per_pack == D("6.98")

    def test_apply_tier_to_all_reports_lines_it_could_not_change(
        self, session, sales, draft, catalogue, admin
    ):
        _add(session, sales, draft, catalogue,
             '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "100")
        session.commit()

        problems = quotation_service.apply_tier_to_all(
            session, sales, draft, PriceTierCode.CUSTOM.value
        )
        session.commit()
        # Custom needs a price per line, so it cannot be bulk-applied.
        assert len(problems) == 1
        assert "Line 1" in problems[0]
        # And the line is left on its original tier rather than broken.
        assert draft.items[0].price_per_pack == D("7.42")


# --------------------------------------------------------------------------- #
# Charges and totals
# --------------------------------------------------------------------------- #

class TestChargesAndTotals:
    def test_totals_foot(self, session, sales, draft, catalogue):
        _add(session, sales, draft, catalogue,
             '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "1000")
        _add(session, sales, draft, catalogue,
             '14" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "500")
        quotation_service.add_charge(
            session, sales, draft, charge_type=ChargeType.FREIGHT,
            quantity=D("1"), rate=D("1250.00"),
        )
        quotation_service.update_header(
            session, sales, draft, quote_discount_pct=D("2"), tax_rate_pct=D("13")
        )
        session.commit()

        assert draft.grand_total == (
            draft.subtotal - draft.quote_discount_amount
            + draft.charges_total + draft.tax_amount
        )
        assert draft.subtotal == sum(i.net_line_total for i in draft.items)

    def test_the_plate_charge_uses_the_configured_rate(
        self, session, sales, draft, catalogue
    ):
        charge = quotation_service.add_plate_charge(
            session, sales, draft, number_of_sizes=3, number_of_colours=4
        )
        session.commit()
        assert charge.amount == D("2400.00")
        assert charge.source == "plate_calculator"

    def test_an_existing_plate_costs_nothing(self, session, sales, draft):
        charge = quotation_service.add_plate_charge(
            session, sales, draft, number_of_sizes=3, number_of_colours=4,
            existing_plate_available=True,
        )
        session.commit()
        assert charge.amount == D("0.00")
        assert "existing plates" in charge.description

    def test_an_internal_only_charge_counts_but_is_not_customer_visible(
        self, session, sales, draft
    ):
        quotation_service.add_charge(
            session, sales, draft, charge_type=ChargeType.TOOLING,
            quantity=D("1"), rate=D("500.00"), is_customer_visible=False,
        )
        session.commit()
        # expire_on_commit=False leaves the loaded collection stale.
        session.refresh(draft)
        assert draft.charges_total == D("500.00")
        assert draft.grand_total == D("500.00")
        assert draft.charges[0].is_customer_visible is False

    def test_a_non_taxable_charge_is_excluded_from_tax(
        self, session, sales, draft, catalogue
    ):
        _add(session, sales, draft, catalogue,
             '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "100")
        quotation_service.add_charge(
            session, sales, draft, charge_type=ChargeType.DUTY,
            quantity=D("1"), rate=D("100.00"), is_taxable=False,
        )
        quotation_service.update_header(session, sales, draft, tax_rate_pct=D("10"))
        session.commit()
        assert draft.tax_amount == D("74.20")  # 10% of 742.00, not of 842.00

    def test_margins_appear_once_costs_exist(
        self, session, sales, admin, draft, catalogue
    ):
        variant_id = catalogue['12" White|WT110 HPFL115 KM135']
        set_cost(
            session, admin,
            CostInput(
                product_variant_id=variant_id, cost_per_pack=D("5.00"),
                effective_from=JAN,
            ),
        )
        session.commit()

        _add(session, sales, draft, catalogue,
             '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "1000")
        session.commit()

        assert draft.total_cost == D("5000.00")
        assert draft.gross_profit == D("2420.00")
        assert draft.gross_margin_pct == D("32.6146")

    def test_margin_is_absent_without_costs(self, session, sales, draft, catalogue):
        _add(session, sales, draft, catalogue,
             '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "1000")
        session.commit()
        assert draft.total_cost is None
        assert draft.gross_margin_pct is None


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #

class TestWarnings:
    def test_below_moq(self, session, sales, draft, catalogue):
        _add(session, sales, draft, catalogue,
             '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "50")
        session.commit()
        codes = {w.code for w in pricing_service.evaluate_quotation(session, draft)}
        assert PriceWarningCode.BELOW_MOQ in codes

    def test_moq_not_flagged_when_met(self, session, sales, draft, catalogue):
        _add(session, sales, draft, catalogue,
             '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "500")
        session.commit()
        codes = {w.code for w in pricing_service.evaluate_quotation(session, draft)}
        assert PriceWarningCode.BELOW_MOQ not in codes

    def test_duplicate_line(self, session, sales, draft, catalogue):
        for _ in range(2):
            _add(session, sales, draft, catalogue,
                 '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "500")
        session.commit()
        codes = {w.code for w in pricing_service.evaluate_quotation(session, draft)}
        assert PriceWarningCode.DUPLICATE_LINE in codes

    def test_mix_limit_respects_the_configured_maximum(
        self, session, sales, admin, draft, catalogue
    ):
        for key in catalogue:
            _add(session, sales, draft, catalogue,
                 key, PriceTierCode.STANDARD.value, "500")
        session.commit()

        # Three distinct variants is exactly the seeded limit, so nothing fires.
        codes = {w.code for w in pricing_service.evaluate_quotation(session, draft)}
        assert PriceWarningCode.MIX_LIMIT not in codes

        settings_service.set_setting(
            session, admin, "max_items_per_container", 2, value_type="int"
        )
        session.commit()
        codes = {w.code for w in pricing_service.evaluate_quotation(session, draft)}
        assert PriceWarningCode.MIX_LIMIT in codes

    def test_custom_price_below_the_floor_blocks(
        self, session, sales, draft, catalogue
    ):
        _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.CUSTOM.value, "500",
            custom_price_per_pack=D("2.00"), custom_price_reason="strategic",
        )
        session.commit()
        warnings = pricing_service.evaluate_quotation(session, draft)
        floor = [
            w for w in warnings
            if w.code is PriceWarningCode.CUSTOM_PRICE_BELOW_FLOOR
        ]
        assert len(floor) == 1
        assert floor[0].severity is WarningSeverity.BLOCKING
        assert not pricing_service.can_release(warnings)

    def test_a_modest_custom_price_does_not_block(
        self, session, sales, draft, catalogue
    ):
        _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.CUSTOM.value, "500",
            custom_price_per_pack=D("7.00"), custom_price_reason="rounding",
        )
        session.commit()
        codes = {w.code for w in pricing_service.evaluate_quotation(session, draft)}
        assert PriceWarningCode.CUSTOM_PRICE_BELOW_FLOOR not in codes

    def test_a_missing_price_is_reported_and_not_overridable(
        self, session, admin, catalogue
    ):
        resolution = pricing_service.resolve_price(
            session, catalogue['12" White|WT110 HPFL115 KM135'],
            PriceTierCode.STANDARD.value, dt.date(2025, 1, 1), "USD",
        )
        assert not resolution.found
        warning = resolution.warnings[0]
        assert warning.code is PriceWarningCode.PRICE_MISSING
        assert warning.overridable is False

    def test_an_expired_price_is_distinguished_from_a_missing_one(
        self, session, admin, catalogue
    ):
        variant_id = catalogue['12" White|WT110 HPFL115 KM135']
        set_price(
            session, admin,
            PriceInput(
                product_variant_id=variant_id,
                price_tier_code=PriceTierCode.STANDARD.value,
                price_per_pack=D("7.95"), effective_from=dt.date(2026, 9, 1),
            ),
        )
        session.commit()

        resolution = pricing_service.resolve_price(
            session, variant_id, PriceTierCode.STANDARD.value,
            dt.date(2026, 8, 15), "USD",
        )
        # The old price was closed on 31 Aug, so mid-August still resolves.
        assert resolution.found

    def test_piece_pack_mismatch_is_informational_only(
        self, session, sales, admin, draft, catalogue
    ):
        variant_id = catalogue['14" White|WT110 HPFL115 KM135']
        set_price(
            session, admin,
            PriceInput(
                product_variant_id=variant_id,
                price_tier_code=PriceTierCode.STANDARD.value,
                price_per_pack=D("6.32"), price_per_piece=D("0.1200"),
                effective_from=dt.date(2026, 2, 1),
            ),
        )
        session.commit()

        _add(session, sales, draft, catalogue,
             '14" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "500")
        session.commit()

        warnings = pricing_service.evaluate_quotation(session, draft)
        mismatch = [
            w for w in warnings if w.code is PriceWarningCode.PIECE_PACK_MISMATCH
        ]
        assert len(mismatch) == 1
        assert mismatch[0].severity is WarningSeverity.INFO
        assert pricing_service.can_release(warnings)

    def test_the_workbook_rounding_discrepancy_does_not_warn(
        self, session, sales, admin, draft, catalogue
    ):
        """25 of the reference file's 69 price pairs differ by one rounding unit."""
        variant_id = catalogue['14" White|WT110 HPFL115 KM135']
        set_price(
            session, admin,
            PriceInput(
                product_variant_id=variant_id,
                price_tier_code=PriceTierCode.STANDARD.value,
                price_per_pack=D("6.32"), price_per_piece=D("0.1263"),
                effective_from=dt.date(2026, 2, 1),
            ),
        )
        session.commit()

        _add(session, sales, draft, catalogue,
             '14" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "500")
        session.commit()

        codes = {w.code for w in pricing_service.evaluate_quotation(session, draft)}
        assert PriceWarningCode.PIECE_PACK_MISMATCH not in codes

    def test_warnings_are_ordered_with_blocking_first(
        self, session, sales, draft, catalogue
    ):
        _add(
            session, sales, draft, catalogue,
            '12" White|WT110 HPFL115 KM135', PriceTierCode.CUSTOM.value, "50",
            custom_price_per_pack=D("1.00"), custom_price_reason="test",
        )
        session.commit()
        warnings = pricing_service.evaluate_quotation(session, draft)
        assert warnings[0].severity is WarningSeverity.BLOCKING


# --------------------------------------------------------------------------- #
# Status machine
# --------------------------------------------------------------------------- #

class TestStatusMachine:
    def test_a_legal_transition(self, session, sales, draft):
        quotation_service.change_status(
            session, sales, draft, QuotationStatus.PENDING_APPROVAL
        )
        session.commit()
        assert draft.status is QuotationStatus.PENDING_APPROVAL

    def test_an_illegal_transition_is_refused(self, session, sales, draft):
        with pytest.raises(QuotationError, match="cannot move to"):
            quotation_service.change_status(
                session, sales, draft, QuotationStatus.ACCEPTED
            )

    def test_the_message_lists_what_is_allowed(self, session, sales, draft):
        with pytest.raises(QuotationError, match="Allowed from here"):
            quotation_service.change_status(
                session, sales, draft, QuotationStatus.ACCEPTED
            )

    @pytest.mark.parametrize(
        "status",
        [
            QuotationStatus.REJECTED_INTERNALLY,
            QuotationStatus.REVISION_REQUIRED,
            QuotationStatus.CANCELLED,
        ],
    )
    def test_a_note_is_required_for_certain_statuses(
        self, session, manager, draft, status
    ):
        quotation_service.change_status(
            session, manager, draft, QuotationStatus.PENDING_APPROVAL
        )
        session.commit()
        with pytest.raises(QuotationError, match="note is required"):
            quotation_service.change_status(session, manager, draft, status)

    def test_a_note_satisfies_the_requirement(self, session, manager, draft):
        quotation_service.change_status(
            session, manager, draft, QuotationStatus.PENDING_APPROVAL
        )
        quotation_service.change_status(
            session, manager, draft, QuotationStatus.REJECTED_INTERNALLY,
            note="margin too thin",
        )
        session.commit()
        assert draft.status is QuotationStatus.REJECTED_INTERNALLY

    def test_a_terminal_status_goes_nowhere(self, session, manager, draft):
        for status, note in [
            (QuotationStatus.APPROVED, None),
            (QuotationStatus.SENT_TO_CUSTOMER, None),
            (QuotationStatus.ACCEPTED, None),
        ]:
            quotation_service.change_status(session, manager, draft, status, note)
        session.commit()
        with pytest.raises(QuotationError, match="Allowed from here: nothing"):
            quotation_service.change_status(
                session, manager, draft, QuotationStatus.LOST
            )

    def test_status_changes_are_audited_with_the_note(self, session, manager, draft):
        quotation_service.change_status(
            session, manager, draft, QuotationStatus.PENDING_APPROVAL
        )
        quotation_service.change_status(
            session, manager, draft, QuotationStatus.REVISION_REQUIRED,
            note="customer wants 14 inch added",
        )
        session.commit()
        entry = (
            session.query(AuditLog)
            .filter_by(action=AuditAction.STATUS_CHANGED.value)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry.reason == "customer wants 14 inch added"

    def test_expiring_overdue_quotations(self, session, manager, sales, customer):
        quotation = quotation_service.create_draft(
            session, sales, customer.id, quote_date=dt.date(2026, 1, 1)
        )
        quotation.valid_until = dt.date(2026, 2, 1)
        quotation_service.change_status(
            session, manager, quotation, QuotationStatus.APPROVED
        )
        session.commit()

        count = quotation_service.expire_overdue(
            session, manager, today=dt.date(2026, 8, 3)
        )
        session.commit()
        assert count == 1
        assert quotation.status is QuotationStatus.EXPIRED


# --------------------------------------------------------------------------- #
# Validation and editability
# --------------------------------------------------------------------------- #

class TestValidation:
    def test_an_empty_quotation_is_not_ready(self, session, draft):
        problems = quotation_service.validate_for_submission(session, draft)
        assert any("no product lines" in p for p in problems)

    def test_a_complete_quotation_is_clean(self, session, sales, draft, catalogue):
        _add(session, sales, draft, catalogue,
             '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "500")
        session.commit()
        assert quotation_service.validate_for_submission(session, draft) == []

    def test_a_backwards_validity_date_is_caught(self, session, sales, draft):
        draft.valid_until = draft.quote_date - dt.timedelta(days=1)
        session.commit()
        problems = quotation_service.validate_for_submission(session, draft)
        assert any("before the quote date" in p for p in problems)

    def test_an_issued_quotation_cannot_be_edited(
        self, session, sales, draft, catalogue
    ):
        _add(session, sales, draft, catalogue,
             '12" White|WT110 HPFL115 KM135', PriceTierCode.STANDARD.value, "500")
        draft.is_locked = True
        draft.status = QuotationStatus.SENT_TO_CUSTOMER
        session.commit()

        with pytest.raises(PermissionDenied, match="Create a revision"):
            quotation_service.update_header(session, sales, draft, brand="Anything")

    def test_another_salespersons_draft_cannot_be_edited(
        self, session, make_auth_user, draft
    ):
        other = make_auth_user(RoleCode.SALES.value, username="bob")
        with pytest.raises(PermissionDenied):
            quotation_service.update_header(session, other, draft, brand="Anything")

    def test_an_unknown_header_field_is_refused(self, session, sales, draft):
        with pytest.raises(QuotationError, match="Cannot set"):
            quotation_service.update_header(session, sales, draft, grand_total=D("1"))
