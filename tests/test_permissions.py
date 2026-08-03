"""Role permissions, scoping, and the rules that no permission can bypass."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules.authorization import (
    PermissionDenied,
    can_approve_quotation,
    can_edit_quotation,
    can_view_costs,
    can_view_quotation,
    quotation_scope_filter,
    require,
    require_any,
    require_edit_quotation,
)
from modules.constants import ROLE_PERMISSIONS, Perm, QuotationStatus, RoleCode
from modules.models import Customer, Quotation


# --------------------------------------------------------------------------- #
# The matrix
# --------------------------------------------------------------------------- #

class TestRoleMatrix:
    @pytest.mark.parametrize("role_code", [r.value for r in RoleCode])
    def test_seeded_grants_match_the_matrix_in_code(self, role_code, seeded):
        role = seeded[role_code]
        granted = {p.code for p in role.permissions}
        expected = {p.value for p in ROLE_PERMISSIONS[RoleCode(role_code)]}
        assert granted == expected

    @pytest.mark.parametrize(
        ("role_code", "permission", "allowed"),
        [
            (RoleCode.SALES, Perm.QUOTE_CREATE, True),
            (RoleCode.SALES, Perm.QUOTE_APPROVE, False),
            (RoleCode.SALES, Perm.PRICE_MANAGE, False),
            (RoleCode.SALES, Perm.USER_MANAGE, False),
            (RoleCode.SALES, Perm.COST_VIEW, False),
            (RoleCode.SALES_MANAGER, Perm.QUOTE_APPROVE, True),
            (RoleCode.SALES_MANAGER, Perm.MARGIN_VIEW, True),
            (RoleCode.SALES_MANAGER, Perm.QUOTE_OVERRIDE_WARNING, True),
            (RoleCode.SALES_MANAGER, Perm.PRICE_IMPORT, False),
            (RoleCode.SALES_MANAGER, Perm.USER_MANAGE, False),
            (RoleCode.FINANCE, Perm.TAX_MANAGE, True),
            (RoleCode.FINANCE, Perm.APPROVAL_LIMITS_MANAGE, True),
            (RoleCode.FINANCE, Perm.COST_MANAGE, True),
            (RoleCode.FINANCE, Perm.QUOTE_CREATE, False),
            (RoleCode.PRICING_ADMIN, Perm.PRICE_IMPORT, True),
            (RoleCode.PRICING_ADMIN, Perm.PRODUCT_CREATE, True),
            (RoleCode.PRICING_ADMIN, Perm.QUOTE_CREATE, False),
            (RoleCode.PRICING_ADMIN, Perm.QUOTE_APPROVE, False),
            (RoleCode.SYS_ADMIN, Perm.USER_MANAGE, True),
            (RoleCode.SYS_ADMIN, Perm.SETTINGS_MANAGE, True),
            (RoleCode.SYS_ADMIN, Perm.AUDIT_VIEW_ALL, True),
        ],
    )
    def test_permission_grants(self, role_code, permission, allowed, make_auth_user):
        user = make_auth_user(role_code.value)
        assert user.has(permission) is allowed

    def test_require_raises_for_a_missing_permission(self, make_auth_user):
        user = make_auth_user(RoleCode.SALES.value)
        with pytest.raises(PermissionDenied):
            require(user, Perm.QUOTE_APPROVE)

    def test_require_passes_for_a_held_permission(self, make_auth_user):
        user = make_auth_user(RoleCode.SALES.value)
        require(user, Perm.QUOTE_CREATE)  # must not raise

    def test_require_rejects_an_anonymous_caller(self):
        with pytest.raises(PermissionDenied):
            require(None, Perm.QUOTE_CREATE)

    def test_require_any(self, make_auth_user):
        user = make_auth_user(RoleCode.SALES.value)
        require_any(user, Perm.QUOTE_APPROVE, Perm.QUOTE_CREATE)
        with pytest.raises(PermissionDenied):
            require_any(user, Perm.QUOTE_APPROVE, Perm.USER_MANAGE)

    def test_multiple_roles_union_their_permissions(self, make_auth_user):
        user = make_auth_user(RoleCode.SALES.value, RoleCode.FINANCE.value)
        assert user.has(Perm.QUOTE_CREATE)   # from Sales
        assert user.has(Perm.TAX_MANAGE)     # from Finance


# --------------------------------------------------------------------------- #
# Per-user grants
# --------------------------------------------------------------------------- #

class TestIndividualGrants:
    def test_cost_view_can_be_granted_to_a_sales_employee(
        self, session, make_user, seeded
    ):
        from sqlalchemy import select

        from modules.authorization import load_auth_user
        from modules.models import Permission

        user = make_user(RoleCode.SALES.value)
        assert not load_auth_user(session, user).has(Perm.COST_VIEW)

        cost_view = session.execute(
            select(Permission).where(Permission.code == Perm.COST_VIEW.value)
        ).scalar_one()
        user.extra_permissions.append(cost_view)
        session.commit()

        refreshed = load_auth_user(session, user)
        assert refreshed.has(Perm.COST_VIEW)
        assert can_view_costs(refreshed)
        # Granting cost visibility must not confer anything else.
        assert not refreshed.has(Perm.QUOTE_APPROVE)


# --------------------------------------------------------------------------- #
# Approval limits
# --------------------------------------------------------------------------- #

class TestApprovalLimits:
    def test_sales_limits_are_applied(self, make_auth_user):
        user = make_auth_user(RoleCode.SALES.value)
        assert user.limits.discount_exceeds(D("7"))
        assert not user.limits.discount_exceeds(D("4"))
        assert user.limits.value_exceeds(D("30000"))
        assert user.limits.margin_below(D("12"))

    def test_the_most_permissive_role_wins(self, make_auth_user):
        both = make_auth_user(RoleCode.SALES.value, RoleCode.SALES_MANAGER.value)
        # Sales caps discount at 5%, Sales Manager at 15% — 15% applies.
        assert not both.limits.discount_exceeds(D("12"))
        assert both.limits.can_override_warnings

    def test_a_null_limit_means_unlimited_and_wins(self, make_auth_user):
        user = make_auth_user(RoleCode.SALES.value, RoleCode.SYS_ADMIN.value)
        assert user.limits.max_discount_pct is None
        assert not user.limits.discount_exceeds(D("99"))
        assert not user.limits.value_exceeds(D("10000000"))

    def test_margin_check_is_inert_without_a_margin(self, make_auth_user):
        user = make_auth_user(RoleCode.SALES.value)
        assert not user.limits.margin_below(None)


# --------------------------------------------------------------------------- #
# Scoping
# --------------------------------------------------------------------------- #

@pytest.fixture
def quotations(session, make_user):
    """Two salespeople, one manager, one quotation each for the salespeople."""
    manager = make_user(RoleCode.SALES_MANAGER.value, username="manager")
    alice = make_user(RoleCode.SALES.value, username="alice", manager_id=manager.id)
    bob = make_user(RoleCode.SALES.value, username="bob")  # different team

    customer = Customer(customer_number="C1", company_name="Acme")
    session.add(customer)
    session.flush()

    made = {}
    for owner in (alice, bob):
        quote = Quotation(
            quote_number=f"IGB-QT-2026-{owner.id:04d}",
            revision_no=0,
            customer_id=customer.id,
            sales_user_id=owner.id,
            quote_date=dt.date(2026, 8, 3),
            status=QuotationStatus.DRAFT,
        )
        session.add(quote)
        session.flush()
        quote.root_quotation_id = quote.id
        made[owner.username] = quote
    session.commit()
    return {"manager": manager, "alice": alice, "bob": bob, "quotes": made}


class TestScoping:
    def test_own_scope_sees_only_own(self, session, quotations):
        from modules.authorization import load_auth_user

        alice = load_auth_user(session, quotations["alice"])
        assert can_view_quotation(alice, quotations["quotes"]["alice"])
        assert not can_view_quotation(alice, quotations["quotes"]["bob"])

    def test_team_scope_covers_direct_reports_only(self, session, quotations):
        from modules.authorization import load_auth_user

        manager = load_auth_user(session, quotations["manager"])
        assert can_view_quotation(manager, quotations["quotes"]["alice"])
        assert not can_view_quotation(manager, quotations["quotes"]["bob"])

    def test_scope_filter_is_a_sql_predicate(self, session, quotations):
        from sqlalchemy import select

        from modules.authorization import load_auth_user

        alice = load_auth_user(session, quotations["alice"])
        rows = session.execute(
            select(Quotation).where(quotation_scope_filter(alice))
        ).scalars().all()
        assert [q.sales_user_id for q in rows] == [quotations["alice"].id]

    def test_view_all_sees_everything(self, session, quotations, make_user):
        from sqlalchemy import select

        from modules.authorization import load_auth_user

        finance = load_auth_user(session, make_user(RoleCode.FINANCE.value))
        rows = session.execute(
            select(Quotation).where(quotation_scope_filter(finance))
        ).scalars().all()
        assert len(rows) == 2

    def test_no_view_permission_yields_an_empty_list_not_an_error(
        self, session, quotations, make_user
    ):
        from sqlalchemy import select

        from modules.authorization import load_auth_user

        pricing = load_auth_user(session, make_user(RoleCode.PRICING_ADMIN.value))
        rows = session.execute(
            select(Quotation).where(quotation_scope_filter(pricing))
        ).scalars().all()
        assert rows == []


# --------------------------------------------------------------------------- #
# Editing and approval
# --------------------------------------------------------------------------- #

class TestEditRules:
    def test_owner_may_edit_own_draft(self, session, quotations):
        from modules.authorization import load_auth_user

        alice = load_auth_user(session, quotations["alice"])
        assert can_edit_quotation(alice, quotations["quotes"]["alice"])

    def test_owner_may_not_edit_someone_elses_draft(self, session, quotations):
        from modules.authorization import load_auth_user

        alice = load_auth_user(session, quotations["alice"])
        assert not can_edit_quotation(alice, quotations["quotes"]["bob"])

    def test_nobody_may_edit_an_issued_quotation(self, session, quotations, make_user):
        from modules.authorization import load_auth_user

        quote = quotations["quotes"]["alice"]
        quote.status = QuotationStatus.SENT_TO_CUSTOMER
        quote.is_locked = True
        session.commit()

        for role in (RoleCode.SALES_MANAGER, RoleCode.SYS_ADMIN):
            user = load_auth_user(session, make_user(role.value))
            assert not can_edit_quotation(user, quote)

        with pytest.raises(PermissionDenied, match="Create a revision"):
            require_edit_quotation(
                load_auth_user(session, quotations["alice"]), quote
            )

    def test_approved_quotation_is_not_editable(self, session, quotations):
        from modules.authorization import load_auth_user

        quote = quotations["quotes"]["alice"]
        quote.status = QuotationStatus.APPROVED
        session.commit()
        alice = load_auth_user(session, quotations["alice"])
        assert not can_edit_quotation(alice, quote)


class TestSelfApproval:
    """Self-approval is refused by identity, before any permission is consulted.
    No role grant can route around it."""

    def test_a_manager_cannot_approve_their_own_quotation(self, session, quotations):
        from modules.authorization import load_auth_user

        manager_user = quotations["manager"]
        quote = quotations["quotes"]["alice"]
        quote.sales_user_id = manager_user.id
        session.commit()

        manager = load_auth_user(session, manager_user)
        assert manager.has(Perm.QUOTE_APPROVE)
        assert not can_approve_quotation(manager, quote, requested_by_id=manager.id)

    def test_a_system_administrator_cannot_either(self, session, quotations, make_user):
        from modules.authorization import load_auth_user

        admin_user = make_user(RoleCode.SYS_ADMIN.value, username="root")
        quote = quotations["quotes"]["alice"]
        quote.sales_user_id = admin_user.id
        session.commit()

        admin = load_auth_user(session, admin_user)
        assert admin.has(Perm.QUOTE_APPROVE)
        assert not can_approve_quotation(admin, quote, requested_by_id=admin.id)

    def test_submitting_on_behalf_of_someone_else_still_blocks_the_submitter(
        self, session, quotations
    ):
        from modules.authorization import load_auth_user

        manager = load_auth_user(session, quotations["manager"])
        quote = quotations["quotes"]["alice"]
        # The manager submitted it, so the manager cannot also decide it.
        assert not can_approve_quotation(
            manager, quote, requested_by_id=manager.id
        )

    def test_a_manager_may_approve_someone_elses(self, session, quotations):
        from modules.authorization import load_auth_user

        manager = load_auth_user(session, quotations["manager"])
        quote = quotations["quotes"]["alice"]
        assert can_approve_quotation(
            manager, quote, requested_by_id=quotations["alice"].id
        )
