"""Customer and catalogue services: permissions, business rules, append-only history."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules.authorization import PermissionDenied, load_auth_user
from modules.catalogue_service import (
    CatalogueError,
    create_product,
    create_variant,
    set_cost,
    set_price,
    update_variant,
    withdraw_price,
)
from modules.constants import AddressType, AuditAction, CustomerStatus, RoleCode
from modules.customer_service import (
    CustomerError,
    add_address,
    add_contact,
    copy_billing_to_shipping,
    create_customer,
    update_contact,
    update_customer,
)
from modules.models import AuditLog
from modules.repositories import (
    cost_history,
    default_address,
    get_customer,
    get_effective_cost,
    get_effective_price,
    next_customer_number,
    price_history,
    primary_contact,
    search_customers,
)
from modules.validation import (
    AddressInput,
    ContactInput,
    CostInput,
    CustomerInput,
    PriceInput,
    ProductInput,
    VariantInput,
)

JAN = dt.date(2026, 1, 1)
JUL = dt.date(2026, 7, 1)


@pytest.fixture
def sales(make_auth_user):
    return make_auth_user(RoleCode.SALES.value, username="alice")


@pytest.fixture
def pricing_admin(make_auth_user):
    return make_auth_user(RoleCode.PRICING_ADMIN.value, username="pricer")


@pytest.fixture
def finance(make_auth_user):
    return make_auth_user(RoleCode.FINANCE.value, username="fin")


def _customer(number="CUST-0001", name="Acme Packaging"):
    return CustomerInput(customer_number=number, company_name=name)


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

class TestCustomers:
    def test_create_and_find(self, session, sales):
        customer = create_customer(session, sales, _customer())
        session.commit()
        assert customer.id
        assert [c.company_name for c in search_customers(session, "Acme")] == [
            "Acme Packaging"
        ]

    def test_customer_number_must_be_unique(self, session, sales):
        create_customer(session, sales, _customer())
        session.commit()
        with pytest.raises(CustomerError, match="already in use"):
            create_customer(session, sales, _customer(name="Different Co"))

    def test_uniqueness_is_case_insensitive(self, session, sales):
        create_customer(session, sales, _customer(number="CUST-0001"))
        session.commit()
        with pytest.raises(CustomerError, match="already in use"):
            create_customer(session, sales, _customer(number="cust-0001", name="Other"))

    def test_suggested_number_increments(self, session, sales):
        assert next_customer_number(session) == "CUST-0001"
        create_customer(session, sales, _customer())
        session.commit()
        assert next_customer_number(session) == "CUST-0002"

    def test_search_matches_contacts_too(self, session, sales):
        customer = create_customer(session, sales, _customer())
        session.commit()
        add_contact(
            session, sales, customer.id,
            ContactInput(name="Michel Dupont", email="michel@acme.invalid"),
        )
        session.commit()
        assert [c.id for c in search_customers(session, "michel")] == [customer.id]

    def test_search_treats_wildcards_literally(self, session, sales):
        """A term containing % must not match everything."""
        create_customer(session, sales, _customer(name="Acme Packaging"))
        create_customer(session, sales, _customer(number="CUST-0002", name="50% Cotton Ltd"))
        session.commit()
        # "%" must match a literal percent sign, not every row.
        assert [c.company_name for c in search_customers(session, "%")] == [
            "50% Cotton Ltd"
        ]
        assert len(search_customers(session, "50%")) == 1
        # "_" likewise must not act as a single-character wildcard.
        assert search_customers(session, "Acm_") == []

    def test_an_unpermitted_user_cannot_create(self, session, pricing_admin):
        with pytest.raises(PermissionDenied):
            create_customer(session, pricing_admin, _customer())

    def test_editing_is_audited_with_only_the_changed_fields(self, session, sales):
        customer = create_customer(session, sales, _customer())
        session.commit()

        update_customer(
            session, sales, customer.id,
            CustomerInput(
                customer_number="CUST-0001",
                company_name="Acme Packaging Ltd",
                status=CustomerStatus.ACTIVE,
            ),
        )
        session.commit()

        entry = (
            session.query(AuditLog)
            .filter_by(action=AuditAction.CUSTOMER_EDITED.value)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert set(entry.new_value_json) == {"company_name", "status"}
        assert entry.new_value_json["company_name"] == "Acme Packaging Ltd"

    def test_saving_with_no_changes_adds_no_audit_noise(self, session, sales):
        customer = create_customer(session, sales, _customer())
        session.commit()
        before = session.query(AuditLog).filter_by(
            action=AuditAction.CUSTOMER_EDITED.value
        ).count()

        update_customer(session, sales, customer.id, _customer())
        session.commit()

        after = session.query(AuditLog).filter_by(
            action=AuditAction.CUSTOMER_EDITED.value
        ).count()
        assert after == before

    def test_an_unknown_currency_is_rejected(self):
        with pytest.raises(ValueError):
            CustomerInput(
                customer_number="C1", company_name="X", default_currency="XYZ"
            )


class TestContactsAndAddresses:
    def test_only_one_primary_contact(self, session, sales):
        customer = create_customer(session, sales, _customer())
        session.commit()
        add_contact(session, sales, customer.id, ContactInput(name="First", is_primary=True))
        second = add_contact(
            session, sales, customer.id, ContactInput(name="Second", is_primary=True)
        )
        session.commit()

        refreshed = get_customer(session, customer.id)
        primaries = [c for c in refreshed.contacts if c.is_primary]
        assert len(primaries) == 1
        assert primaries[0].id == second.id
        assert primary_contact(refreshed).name == "Second"

    def test_promoting_a_contact_demotes_the_other(self, session, sales):
        customer = create_customer(session, sales, _customer())
        session.commit()
        first = add_contact(
            session, sales, customer.id, ContactInput(name="First", is_primary=True)
        )
        second = add_contact(session, sales, customer.id, ContactInput(name="Second"))
        session.commit()

        update_contact(session, sales, second.id, ContactInput(name="Second", is_primary=True))
        session.commit()

        assert not session.get(type(first), first.id).is_primary

    def test_an_implausible_email_is_rejected(self):
        with pytest.raises(ValueError):
            ContactInput(name="X", email="not-an-email")

    def test_copy_billing_to_shipping(self, session, sales):
        customer = create_customer(session, sales, _customer())
        session.commit()
        add_address(
            session, sales, customer.id,
            AddressInput(
                address_type=AddressType.BILLING,
                line1="12 Sanayi Caddesi", city="Çerkezköy",
                country="Türkiye", is_default=True,
            ),
        )
        session.commit()

        shipping = copy_billing_to_shipping(session, sales, customer.id)
        session.commit()

        assert shipping.address_type == AddressType.SHIPPING
        assert shipping.city == "Çerkezköy"
        assert "Çerkezköy" in shipping.as_text()

    def test_copying_without_a_billing_address_is_refused(self, session, sales):
        customer = create_customer(session, sales, _customer())
        session.commit()
        with pytest.raises(CustomerError, match="no billing address"):
            copy_billing_to_shipping(session, sales, customer.id)

    def test_one_default_per_address_type(self, session, sales):
        customer = create_customer(session, sales, _customer())
        session.commit()
        for line in ("First", "Second"):
            add_address(
                session, sales, customer.id,
                AddressInput(
                    address_type=AddressType.BILLING, line1=line, is_default=True
                ),
            )
        session.commit()

        refreshed = get_customer(session, customer.id)
        defaults = [a for a in refreshed.addresses if a.is_default]
        assert len(defaults) == 1
        assert defaults[0].line1 == "Second"
        assert default_address(refreshed, AddressType.BILLING).line1 == "Second"


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #

@pytest.fixture
def variant(session, pricing_admin):
    product = create_product(
        session, pricing_admin,
        ProductInput(
            item_number="WB-12", name='12" White', size_label='12" White',
            flute="B", depth_in=D("2"),
        ),
    )
    session.flush()
    created = create_variant(
        session, pricing_admin, product.id,
        VariantInput(
            variant_item_number="WB-12-115", board_quality="WT110 HPFL115 KM135",
            case_pack=50,
        ),
    )
    session.commit()
    return created


class TestCatalogue:
    def test_only_a_pricing_administrator_may_create_products(self, session, sales):
        with pytest.raises(PermissionDenied):
            create_product(
                session, sales,
                ProductInput(item_number="X", name="X", size_label="X"),
            )

    def test_the_same_size_may_hold_two_qualities(self, session, pricing_admin, variant):
        second = create_variant(
            session, pricing_admin, variant.product_id,
            VariantInput(
                variant_item_number="WB-12-160", board_quality="WT110 HPFL160 KM135",
                case_pack=50,
            ),
        )
        session.commit()
        assert second.id != variant.id
        assert second.product_id == variant.product_id

    def test_the_same_quality_and_case_pack_cannot_repeat(
        self, session, pricing_admin, variant
    ):
        with pytest.raises(CatalogueError, match="already has a variant"):
            create_variant(
                session, pricing_admin, variant.product_id,
                VariantInput(
                    variant_item_number="WB-12-115b",
                    board_quality="WT110 HPFL115 KM135",
                    case_pack=50,
                ),
            )

    def test_board_quality_cannot_be_edited(self, session, pricing_admin, variant):
        """Changing it would re-point every historical price and quotation line."""
        with pytest.raises(CatalogueError, match="cannot be changed"):
            update_variant(
                session, pricing_admin, variant.id,
                VariantInput(
                    variant_item_number="WB-12-115",
                    board_quality="SOMETHING ELSE",
                    case_pack=50,
                ),
            )

    def test_case_pack_cannot_be_edited(self, session, pricing_admin, variant):
        with pytest.raises(CatalogueError, match="cannot be changed"):
            update_variant(
                session, pricing_admin, variant.id,
                VariantInput(
                    variant_item_number="WB-12-115",
                    board_quality="WT110 HPFL115 KM135",
                    case_pack=100,
                ),
            )

    def test_descriptive_fields_can_be_edited(self, session, pricing_admin, variant):
        updated = update_variant(
            session, pricing_admin, variant.id,
            VariantInput(
                variant_item_number="WB-12-115",
                board_quality="WT110 HPFL115 KM135",
                case_pack=50,
                num_colours=4,
                moq_pieces=D("10000"),
            ),
        )
        session.commit()
        assert updated.num_colours == 4
        assert updated.moq_pieces == D("10000")


# --------------------------------------------------------------------------- #
# Price tiers
# --------------------------------------------------------------------------- #

class TestPriceTiers:
    def _edit(self, session, user, code, **overrides):
        from modules.catalogue_service import update_price_tier
        from modules.repositories import get_price_tier

        tier = get_price_tier(session, code)
        kwargs = {
            "name": tier.name,
            "min_containers": tier.min_containers,
            "requires_approval": tier.requires_approval,
            "sort_order": tier.sort_order,
            "is_active": tier.is_active,
        }
        kwargs.update(overrides)
        return update_price_tier(session, user, code, **kwargs)

    def test_a_pricing_administrator_may_retune_a_tier(self, session, pricing_admin):
        tier = self._edit(session, pricing_admin, "THREE_CONTAINER", min_containers=4)
        session.commit()
        assert tier.min_containers == 4

    def test_sales_cannot_edit_tiers(self, session, sales):
        with pytest.raises(PermissionDenied):
            self._edit(session, sales, "STANDARD", name="Anything")

    def test_a_seeded_tier_cannot_be_deactivated(self, session, pricing_admin):
        """The importer resolves container columns onto these codes, and existing
        prices point at them."""
        with pytest.raises(CatalogueError, match="cannot be deactivated"):
            self._edit(session, pricing_admin, "EIGHT_CONTAINER", is_active=False)

    def test_a_tier_needs_a_name(self, session, pricing_admin):
        with pytest.raises(CatalogueError, match="needs a name"):
            self._edit(session, pricing_admin, "STANDARD", name="   ")

    def test_negative_minimum_containers_is_rejected(self, session, pricing_admin):
        with pytest.raises(CatalogueError, match="cannot be negative"):
            self._edit(session, pricing_admin, "STANDARD", min_containers=-1)

    def test_editing_a_tier_is_audited(self, session, pricing_admin):
        self._edit(session, pricing_admin, "THREE_CONTAINER", min_containers=4)
        session.commit()
        entry = (
            session.query(AuditLog)
            .filter_by(action=AuditAction.SETTINGS_CHANGED.value)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry.new_value_json["min_containers"] == 4
        assert entry.old_value_json["min_containers"] == 3

    def test_a_new_tier_can_be_added(self, session, pricing_admin):
        from modules.catalogue_service import create_price_tier
        from modules.repositories import get_price_tiers

        create_price_tier(
            session, pricing_admin,
            code="twelve container", name="Twelve Containers", min_containers=12,
        )
        session.commit()

        codes = {t.code for t in get_price_tiers(session)}
        assert "TWELVE_CONTAINER" in codes

    def test_a_duplicate_tier_code_is_refused(self, session, pricing_admin):
        from modules.catalogue_service import create_price_tier

        with pytest.raises(CatalogueError, match="already exists"):
            create_price_tier(session, pricing_admin, code="STANDARD", name="Again")

    def test_a_new_container_tier_is_importable_without_a_code_change(
        self, session, pricing_admin
    ):
        """The header regex is generic, so adding the tier is enough."""
        from modules.catalogue_service import create_price_tier
        from modules.excel_importer import normalise_header

        create_price_tier(
            session, pricing_admin,
            code="TWELVE_CONTAINER", name="Twelve Containers", min_containers=12,
        )
        session.commit()
        assert (
            normalise_header("12 containers\nPrice/Pack")
            == "twelve_container_price_per_pack"
        )


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #

class TestPrices:
    def _price(self, variant_id, pack="7.42", effective_from=JAN, piece=None):
        return PriceInput(
            product_variant_id=variant_id,
            price_tier_code="STANDARD",
            price_per_pack=D(pack),
            price_per_piece=D(piece) if piece else None,
            effective_from=effective_from,
        )

    def test_a_price_can_be_recorded(self, session, pricing_admin, variant):
        price = set_price(session, pricing_admin, self._price(variant.id))
        session.commit()
        assert price.price_per_pack == D("7.42")
        # Derived from the pack price when the workbook gives no piece column.
        assert price.price_per_piece == D("0.1484")

    def test_a_stated_piece_price_is_kept_verbatim(self, session, pricing_admin, variant):
        """It may legitimately differ from pack ÷ case pack by a rounding unit."""
        price = set_price(
            session, pricing_admin, self._price(variant.id, "6.32", piece="0.1263")
        )
        session.commit()
        assert price.price_per_piece == D("0.1263")
        assert price.price_per_piece != price.price_per_pack / 50

    def test_sales_cannot_change_prices(self, session, sales, variant):
        with pytest.raises(PermissionDenied):
            set_price(session, sales, self._price(variant.id))

    def test_a_new_price_supersedes_rather_than_overwrites(
        self, session, pricing_admin, variant
    ):
        set_price(session, pricing_admin, self._price(variant.id, "7.42", JAN))
        session.commit()
        set_price(session, pricing_admin, self._price(variant.id, "7.95", JUL))
        session.commit()

        history = price_history(session, variant.id, "STANDARD")
        assert len(history) == 2
        assert history[0].effective_from == JUL and history[0].effective_to is None
        assert history[1].effective_to == dt.date(2026, 6, 30)

    def test_historical_lookup_returns_the_price_of_the_day(
        self, session, pricing_admin, variant
    ):
        set_price(session, pricing_admin, self._price(variant.id, "7.42", JAN))
        session.commit()
        set_price(session, pricing_admin, self._price(variant.id, "7.95", JUL))
        session.commit()

        assert get_effective_price(
            session, variant.id, "STANDARD", dt.date(2026, 3, 1)
        ).price_per_pack == D("7.42")
        assert get_effective_price(
            session, variant.id, "STANDARD", dt.date(2026, 9, 1)
        ).price_per_pack == D("7.95")

    def test_backdating_is_refused(self, session, pricing_admin, variant):
        set_price(session, pricing_admin, self._price(variant.id, "7.42", JUL))
        session.commit()
        with pytest.raises(CatalogueError, match="already effective from"):
            set_price(session, pricing_admin, self._price(variant.id, "7.95", JAN))

    def test_a_withdrawn_price_never_resolves(self, session, pricing_admin, variant):
        """Distinct from superseding: a withdrawn price was entered in error and
        must not be the answer for any date."""
        price = set_price(session, pricing_admin, self._price(variant.id, "74.20", JAN))
        session.commit()
        assert get_effective_price(session, variant.id, "STANDARD", JUL) is not None

        withdraw_price(session, pricing_admin, price.id, reason="typo — 74.20 not 7.42")
        session.commit()
        assert get_effective_price(session, variant.id, "STANDARD", JUL) is None

    def test_withdrawing_requires_a_reason(self, session, pricing_admin, variant):
        price = set_price(session, pricing_admin, self._price(variant.id))
        session.commit()
        with pytest.raises(CatalogueError, match="reason is required"):
            withdraw_price(session, pricing_admin, price.id, reason="  ")

    def test_a_price_change_is_audited(self, session, pricing_admin, variant):
        set_price(session, pricing_admin, self._price(variant.id))
        session.commit()
        entry = (
            session.query(AuditLog)
            .filter_by(action=AuditAction.PRICE_CHANGED.value)
            .one()
        )
        assert D(entry.new_value_json["price_per_pack"]) == D("7.42")


# --------------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------------- #

class TestCosts:
    def _cost(self, variant_id, pack="4.10", effective_from=JAN):
        return CostInput(
            product_variant_id=variant_id,
            cost_per_pack=D(pack),
            effective_from=effective_from,
        )

    def test_finance_may_record_a_cost(self, session, finance, variant):
        cost = set_cost(session, finance, self._cost(variant.id))
        session.commit()
        assert cost.cost_per_pack == D("4.10")
        assert cost.cost_per_piece == D("0.082")

    def test_a_pricing_administrator_may_also_record_costs(
        self, session, pricing_admin, variant
    ):
        set_cost(session, pricing_admin, self._cost(variant.id))
        session.commit()

    def test_sales_cannot_record_a_cost(self, session, sales, variant):
        with pytest.raises(PermissionDenied):
            set_cost(session, sales, self._cost(variant.id))

    def test_costs_are_effective_dated_like_prices(self, session, finance, variant):
        set_cost(session, finance, self._cost(variant.id, "4.10", JAN))
        session.commit()
        set_cost(session, finance, self._cost(variant.id, "4.55", JUL))
        session.commit()

        assert len(cost_history(session, variant.id)) == 2
        assert get_effective_cost(
            session, variant.id, dt.date(2026, 3, 1)
        ).cost_per_pack == D("4.10")
        assert get_effective_cost(
            session, variant.id, dt.date(2026, 9, 1)
        ).cost_per_pack == D("4.55")

    def test_no_cost_reads_as_absent_not_zero(self, session, variant):
        """Margin must be unavailable rather than reported as 100%."""
        assert get_effective_cost(session, variant.id) is None

    def test_backdating_a_cost_is_refused(self, session, finance, variant):
        set_cost(session, finance, self._cost(variant.id, "4.10", JUL))
        session.commit()
        with pytest.raises(CatalogueError, match="already effective from"):
            set_cost(session, finance, self._cost(variant.id, "4.55", JAN))

    def test_a_negative_cost_is_rejected(self, variant):
        with pytest.raises(ValueError):
            CostInput(
                product_variant_id=variant.id,
                cost_per_pack=D("-1"),
                effective_from=JAN,
            )
