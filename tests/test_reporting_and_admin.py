"""Reporting aggregates and their scoping, plus user and settings administration."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules import quotation_service, reporting_service, settings_service, user_service
from modules.authorization import PermissionDenied, load_auth_user
from modules.catalogue_service import create_product, create_variant, set_cost, set_price
from modules.constants import AuditAction, PriceTierCode, QuotationStatus, RoleCode
from modules.customer_service import create_customer
from modules.models import AuditLog, Role, User
from modules.numbering import NumberFormatError, validate_format
from modules.reporting_service import ReportFilters
from modules.user_service import UserError
from modules.validation import (
    CostInput,
    CustomerInput,
    PriceInput,
    ProductInput,
    VariantInput,
)

JAN = dt.date(2026, 1, 1)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def admin(make_auth_user):
    return make_auth_user(RoleCode.SYS_ADMIN.value, username="root")


@pytest.fixture
def finance(make_auth_user):
    return make_auth_user(RoleCode.FINANCE.value, username="fin")


@pytest.fixture
def pipeline(session, admin, make_user):
    """Two salespeople under one manager, with quotations in several states."""
    manager_row = make_user(RoleCode.SALES_MANAGER.value, username="mgr")
    alice_row = make_user(
        RoleCode.SALES.value, username="alice", manager_id=manager_row.id
    )
    bob_row = make_user(RoleCode.SALES.value, username="bob")
    session.commit()

    alice = load_auth_user(session, alice_row)
    bob = load_auth_user(session, bob_row)

    product = create_product(
        session, admin,
        ProductInput(item_number="WB-12", name='12" White', size_label='12" White'),
    )
    session.flush()
    variant = create_variant(
        session, admin, product.id,
        VariantInput(
            variant_item_number="WB-12-A", board_quality="WT110 HPFL115 KM135",
            case_pack=50,
        ),
    )
    set_price(
        session, admin,
        PriceInput(
            product_variant_id=variant.id,
            price_tier_code=PriceTierCode.STANDARD.value,
            price_per_pack=D("7.42"), effective_from=JAN,
        ),
    )
    customer = create_customer(
        session, admin,
        CustomerInput(customer_number="CUST-0001", company_name="Bunzl Canada"),
    )
    session.commit()

    made = {}
    plan = [
        ("alice", alice, QuotationStatus.ACCEPTED, "1000", dt.date(2026, 3, 1)),
        ("alice", alice, QuotationStatus.LOST, "500", dt.date(2026, 3, 15)),
        ("alice", alice, None, "250", dt.date(2026, 4, 1)),
        ("bob", bob, QuotationStatus.ACCEPTED, "2000", dt.date(2026, 4, 10)),
    ]
    for label, owner, final_status, packs, day in plan:
        quote = quotation_service.create_draft(
            session, owner, customer.id, quote_date=day
        )
        quotation_service.add_line(
            session, owner, quote,
            product_variant_id=variant.id,
            price_tier_code=PriceTierCode.STANDARD.value,
            quantity_packs=D(packs),
        )
        if final_status is not None:
            for step in (
                QuotationStatus.APPROVED,
                QuotationStatus.SENT_TO_CUSTOMER,
                final_status,
            ):
                quotation_service.change_status(
                    session, owner, quote, step,
                    note="outcome" if step is final_status else None,
                )
        made.setdefault(label, []).append(quote)
    session.commit()

    return {
        "manager": load_auth_user(session, manager_row),
        "alice": alice,
        "bob": bob,
        "variant": variant,
        "quotes": made,
    }


# --------------------------------------------------------------------------- #
# Headlines
# --------------------------------------------------------------------------- #

class TestHeadlines:
    def test_an_empty_database_is_all_zeros_not_an_error(self, session, admin):
        figures = reporting_service.headlines(session, admin)
        assert figures.total == 0
        assert figures.total_quoted == 0
        assert figures.conversion_rate is None

    def test_counts_by_status(self, session, admin, pipeline):
        figures = reporting_service.headlines(session, admin)
        assert figures.total == 4
        assert figures.counts[QuotationStatus.ACCEPTED.value] == 2
        assert figures.counts[QuotationStatus.LOST.value] == 1
        assert figures.counts[QuotationStatus.DRAFT.value] == 1

    def test_accepted_value_only_counts_accepted(self, session, admin, pipeline):
        figures = reporting_service.headlines(session, admin)
        # 1000 packs and 2000 packs at 7.42.
        assert figures.accepted_value == D("22260.00")

    def test_conversion_uses_decided_quotations_only(self, session, admin, pipeline):
        """A draft is not a loss, so it must not drag the rate down."""
        figures = reporting_service.headlines(session, admin)
        assert figures.conversion_rate == D("66.7")

    def test_conversion_is_none_before_anything_is_decided(
        self, session, admin, make_user
    ):
        customer = create_customer(
            session, admin,
            CustomerInput(customer_number="C1", company_name="X"),
        )
        session.commit()
        quotation_service.create_draft(session, admin, customer.id)
        session.commit()

        figures = reporting_service.headlines(session, admin)
        assert figures.total == 1
        assert figures.conversion_rate is None

    def test_mixed_currency_is_flagged(self, session, admin, pipeline):
        figures = reporting_service.headlines(session, admin)
        assert not figures.mixed_currency
        assert figures.currencies == ("USD",)


# --------------------------------------------------------------------------- #
# Scoping
# --------------------------------------------------------------------------- #

class TestScoping:
    def test_a_salesperson_sees_only_their_own(self, session, pipeline):
        figures = reporting_service.headlines(session, pipeline["alice"])
        assert figures.total == 3

    def test_a_manager_sees_their_team(self, session, pipeline):
        """Alice reports to the manager; Bob does not."""
        figures = reporting_service.headlines(session, pipeline["manager"])
        assert figures.total == 3

    def test_finance_sees_everything(self, session, finance, pipeline):
        figures = reporting_service.headlines(session, finance)
        assert figures.total == 4

    def test_scoping_applies_to_every_aggregate(self, session, pipeline):
        alice = pipeline["alice"]
        assert len(reporting_service.by_employee(session, alice)) == 1
        assert reporting_service.by_employee(session, alice).iloc[0]["Employee"] == "Alice"

    def test_a_user_with_no_view_permission_gets_nothing(
        self, session, make_auth_user, pipeline
    ):
        pricer = make_auth_user(RoleCode.PRICING_ADMIN.value)
        assert reporting_service.headlines(session, pricer).total == 0
        assert reporting_service.by_customer(session, pricer).empty


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #

class TestFilters:
    def test_date_range(self, session, admin, pipeline):
        figures = reporting_service.headlines(
            session, admin,
            ReportFilters(date_from=dt.date(2026, 4, 1), date_to=dt.date(2026, 4, 30)),
        )
        assert figures.total == 2

    def test_status_filter(self, session, admin, pipeline):
        figures = reporting_service.headlines(
            session, admin, ReportFilters(statuses=(QuotationStatus.ACCEPTED,))
        )
        assert figures.total == 2

    def test_employee_filter(self, session, admin, pipeline):
        bob_id = pipeline["bob"].id
        figures = reporting_service.headlines(
            session, admin, ReportFilters(sales_user_ids=(bob_id,))
        )
        assert figures.total == 1

    def test_tier_filter_matches_on_lines(self, session, admin, pipeline):
        figures = reporting_service.headlines(
            session, admin,
            ReportFilters(tier_codes=(PriceTierCode.EIGHT_CONTAINER.value,)),
        )
        assert figures.total == 0

    def test_filters_describe_themselves(self):
        assert ReportFilters().describe() == "no filters"
        assert "USD" in ReportFilters(currency="USD").describe()


# --------------------------------------------------------------------------- #
# Report shapes
# --------------------------------------------------------------------------- #

class TestReports:
    def test_every_report_returns_stable_columns_when_empty(self, session, admin):
        builders = [
            reporting_service.value_by_month,
            reporting_service.count_by_status,
            reporting_service.accepted_versus_lost,
            reporting_service.by_customer,
            reporting_service.by_employee,
            reporting_service.by_product_size,
            reporting_service.by_board_quality,
            reporting_service.by_price_tier,
            reporting_service.discounts_given,
            reporting_service.margin_analysis,
            reporting_service.lost_reasons,
            reporting_service.expiring,
            reporting_service.custom_price_usage,
            reporting_service.approval_turnaround,
            reporting_service.conversion_by_month,
        ]
        for builder in builders:
            frame = builder(session, admin)
            assert frame.empty
            assert len(frame.columns) >= 2, builder.__name__

    def test_value_by_month_groups_correctly(self, session, admin, pipeline):
        frame = reporting_service.value_by_month(session, admin)
        months = frame["Month"].to_list()
        assert months == ["2026-03", "2026-04"]

    def test_margin_analysis_excludes_quotations_without_costs(
        self, session, admin, pipeline
    ):
        """A quotation with no cost data has no margin; it is left out rather
        than shown at 100%."""
        assert reporting_service.margin_analysis(session, admin).empty

        set_cost(
            session, admin,
            CostInput(
                product_variant_id=pipeline["variant"].id,
                cost_per_pack=D("5.00"), effective_from=JAN,
            ),
        )
        session.commit()

        quote = pipeline["quotes"]["alice"][2]  # the draft
        quotation_service.remove_line(session, pipeline["alice"], quote, quote.items[0].id)
        quotation_service.add_line(
            session, pipeline["alice"], quote,
            product_variant_id=pipeline["variant"].id,
            price_tier_code=PriceTierCode.STANDARD.value,
            quantity_packs=D("250"),
        )
        session.commit()

        frame = reporting_service.margin_analysis(session, admin)
        assert len(frame) == 1
        assert frame.iloc[0]["Margin %"] == pytest.approx(32.61, abs=0.01)

    def test_by_board_quality_never_merges_qualities(self, session, admin, pipeline):
        frame = reporting_service.by_board_quality(session, admin)
        assert frame.iloc[0]["Board quality"] == "WT110 HPFL115 KM135"

    def test_expiring_lists_only_live_quotations(self, session, admin, pipeline):
        """An accepted or lost quotation is settled — its validity date is moot."""
        frame = reporting_service.expiring(session, admin, within_days=3650)
        assert frame.empty

    def test_filter_options_are_scoped(self, session, pipeline):
        options = reporting_service.filter_options(session, pipeline["alice"])
        assert [name for _, name in options["employees"]] == ["Alice"]


# --------------------------------------------------------------------------- #
# User administration
# --------------------------------------------------------------------------- #

class TestUserAdministration:
    def test_creating_an_account_returns_a_one_time_password(self, session, admin):
        created, temporary = user_service.create_user(
            session, admin,
            username="Carol", email="Carol@Igland.invalid",
            employee_name="Carol Smith", role_codes=[RoleCode.SALES.value],
        )
        session.commit()
        assert created.username == "carol"       # normalised
        assert created.email == "carol@igland.invalid"
        assert created.must_change_password
        assert temporary and temporary not in created.password_hash

    def test_the_temporary_password_is_not_audited(self, session, admin):
        _, temporary = user_service.create_user(
            session, admin, username="carol", email="c@x.invalid",
            employee_name="Carol", role_codes=[],
        )
        session.commit()
        blob = " ".join(
            str(row) for row in session.query(
                AuditLog.new_value_json, AuditLog.old_value_json, AuditLog.reason
            ).all()
        )
        assert temporary not in blob

    def test_a_duplicate_username_is_refused(self, session, admin, make_user):
        make_user(RoleCode.SALES.value, username="alice")
        session.commit()
        with pytest.raises(UserError, match="already in use"):
            user_service.create_user(
                session, admin, username="ALICE", email="new@x.invalid",
                employee_name="Someone", role_codes=[],
            )

    def test_a_non_administrator_cannot_create_accounts(self, session, make_auth_user):
        manager = make_auth_user(RoleCode.SALES_MANAGER.value)
        with pytest.raises(PermissionDenied):
            user_service.create_user(
                session, manager, username="x", email="x@x.invalid",
                employee_name="X", role_codes=[],
            )

    def test_an_administrator_cannot_disable_themselves(
        self, session, admin, make_user
    ):
        """This is how installations end up with nobody able to administer them."""
        row = session.get(User, admin.id)
        with pytest.raises(UserError, match="your own account"):
            user_service.update_user(
                session, admin, row.id,
                email=row.email, employee_name=row.employee_name,
                job_title=None, manager_id=None, is_active=False,
            )

    def test_the_last_administrator_cannot_be_disabled(
        self, session, admin, make_user, seeded
    ):
        other_admin_row = make_user(RoleCode.SYS_ADMIN.value, username="root2")
        session.commit()
        other_admin = load_auth_user(session, other_admin_row)

        # Two admins: one can be disabled.
        user_service.update_user(
            session, other_admin, admin.id,
            email="a@x.invalid", employee_name="Root", job_title=None,
            manager_id=None, is_active=False,
        )
        session.commit()

        # Now only one remains, and it is the actor's own account.
        with pytest.raises(UserError, match="your own account"):
            user_service.update_user(
                session, other_admin, other_admin.id,
                email=other_admin_row.email, employee_name="Root2",
                job_title=None, manager_id=None, is_active=False,
            )

    def test_nobody_can_be_their_own_manager(self, session, admin):
        row = session.get(User, admin.id)
        with pytest.raises(UserError, match="own manager"):
            user_service.update_user(
                session, admin, row.id,
                email=row.email, employee_name=row.employee_name,
                job_title=None, manager_id=row.id, is_active=True,
            )

    def test_roles_can_be_changed_and_are_audited(self, session, admin, make_user):
        target = make_user(RoleCode.SALES.value, username="alice")
        session.commit()

        user_service.set_roles(
            session, admin, target.id,
            [RoleCode.SALES.value, RoleCode.SALES_MANAGER.value],
        )
        session.commit()
        assert load_auth_user(session, target).has("quote.approve")

        entry = (
            session.query(AuditLog)
            .filter_by(action=AuditAction.ROLE_ASSIGNED.value)
            .one()
        )
        assert "SALES_MANAGER" in entry.new_value_json["roles"]

    def test_an_administrator_cannot_remove_their_own_admin_role(
        self, session, admin
    ):
        with pytest.raises(UserError, match="your own System Administrator role"):
            user_service.set_roles(
                session, admin, admin.id, [RoleCode.SALES.value]
            )

    def test_individual_grants_add_to_role_permissions(
        self, session, admin, make_user
    ):
        target = make_user(RoleCode.SALES.value, username="alice")
        session.commit()
        assert not load_auth_user(session, target).has("cost.view")

        user_service.set_extra_permissions(
            session, admin, target.id, ["cost.view"], reason="pricing support"
        )
        session.commit()

        refreshed = load_auth_user(session, target)
        assert refreshed.has("cost.view")
        assert not refreshed.has("quote.approve")

    def test_an_unknown_permission_is_refused(self, session, admin, make_user):
        target = make_user(RoleCode.SALES.value)
        session.commit()
        with pytest.raises(UserError, match="Unknown permission"):
            user_service.set_extra_permissions(
                session, admin, target.id, ["not.a.permission"]
            )


class TestApprovalLimits:
    def test_limits_can_be_retuned(self, session, finance):
        role = user_service.update_role_limits(
            session, finance, RoleCode.SALES.value,
            max_discount_pct=D("8"), max_quote_value=D("50000"),
            min_margin_pct=D("12"), can_override_warnings=False,
        )
        session.commit()
        assert role.max_discount_pct == D("8")

    def test_a_limit_change_widens_authority(self, session, finance, make_user):
        target = make_user(RoleCode.SALES.value, username="alice")
        session.commit()
        assert load_auth_user(session, target).limits.discount_exceeds(D("7"))

        user_service.update_role_limits(
            session, finance, RoleCode.SALES.value,
            max_discount_pct=D("20"), max_quote_value=None,
            min_margin_pct=None, can_override_warnings=False,
        )
        session.commit()
        assert not load_auth_user(session, target).limits.discount_exceeds(D("7"))

    def test_none_means_unlimited(self, session, finance):
        role = user_service.update_role_limits(
            session, finance, RoleCode.SALES.value,
            max_discount_pct=None, max_quote_value=None,
            min_margin_pct=None, can_override_warnings=True,
        )
        session.commit()
        assert role.max_discount_pct is None

    def test_an_out_of_range_percentage_is_refused(self, session, finance):
        with pytest.raises(UserError, match="between 0 and 100"):
            user_service.update_role_limits(
                session, finance, RoleCode.SALES.value,
                max_discount_pct=D("150"), max_quote_value=None,
                min_margin_pct=None, can_override_warnings=False,
            )

    def test_sales_cannot_change_limits(self, session, make_auth_user):
        sales = make_auth_user(RoleCode.SALES.value)
        with pytest.raises(PermissionDenied):
            user_service.update_role_limits(
                session, sales, RoleCode.SALES.value,
                max_discount_pct=D("99"), max_quote_value=None,
                min_margin_pct=None, can_override_warnings=True,
            )


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

class TestSettings:
    """Every test here needs the seed.

    ``settings_service`` falls back to sensible defaults when the settings row
    is absent, so without this fixture the assertions below would pass against
    an empty database and prove nothing about what was actually seeded.
    """

    @pytest.fixture(autouse=True)
    def _require_seed(self, seeded):
        return seeded

    def test_defaults_are_readable(self, session):
        assert settings_service.default_validity_days(session) == 30
        assert settings_service.plate_rate(session) == D("200.00")
        assert settings_service.tier_container_scope(session) == "quotation"
        assert settings_service.piece_pack_tolerance(session) == D("0.0001")

    def test_the_seeded_identity_is_flagged_as_placeholder(self, session):
        assert settings_service.is_placeholder_identity(session)

    def test_the_seeded_identity_has_no_placeholder_text(self, session):
        """Placeholder-ness is a flag; a customer-facing field must never carry
        text like 'Address not set'."""
        settings = settings_service.get_company_settings(session)
        assert "placeholder" not in settings.legal_name.lower()
        assert not settings.address_line1

    def test_a_setting_can_be_changed_and_is_audited(self, session, admin):
        settings_service.set_setting(
            session, admin, "max_items_per_container", 5, value_type="int"
        )
        session.commit()
        assert settings_service.max_items_per_container(session) == 5

        entry = (
            session.query(AuditLog)
            .filter_by(action=AuditAction.SETTINGS_CHANGED.value)
            .one()
        )
        assert entry.new_value_json["max_items_per_container"] == 5

    def test_only_an_administrator_may_change_settings(
        self, session, make_auth_user
    ):
        manager = make_auth_user(RoleCode.SALES_MANAGER.value)
        with pytest.raises(PermissionDenied):
            settings_service.set_setting(session, manager, "anything", 1)

    def test_a_malformed_stored_value_falls_back(self, session, admin):
        settings_service.set_setting(
            session, admin, "piece_pack_tolerance", "not a number"
        )
        session.commit()
        assert settings_service.piece_pack_tolerance(session) == D("0.0001")

    @pytest.mark.parametrize(
        "fmt", ["IGB-QT-{YYYY}-{SEQ:04d}", "{YY}{MM}-{SEQ:05d}", "Q-{SEQ}"]
    )
    def test_valid_number_formats_are_accepted(self, fmt):
        validate_format(fmt)

    @pytest.mark.parametrize("fmt", ["IGB-{YYYY}", "", "IGB-{CUSTOMER}-{SEQ}"])
    def test_invalid_number_formats_are_refused(self, fmt):
        with pytest.raises(NumberFormatError):
            validate_format(fmt)
