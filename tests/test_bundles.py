"""Bundle composition: the price per bundle and the container estimate.

Both need the same input — how many boxes are in a bundle — and neither the
price list nor the capacity workbook states it. Every test here is really
about the same rule: where it is unset, the derived figure is absent, never
guessed and never zero.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from modules import pricing_service
from modules.catalogue_service import create_product, update_product
from modules.constants import ContainerSize, ContainerType
from modules.models import Product, ProductContainerCapacity, ProductVariant
from modules.validation import ProductInput


# --------------------------------------------------------------------------- #
# Bundle price
# --------------------------------------------------------------------------- #

class TestBundlePrice:
    def test_price_is_pieces_times_the_piece_price(self):
        assert pricing_service.bundle_price(D("0.1826"), D("250")) == D("45.65")

    def test_unset_bundle_gives_no_price(self):
        """Not zero. Zero is a price and would print as one."""
        assert pricing_service.bundle_price(D("0.1826"), None) is None

    def test_unset_piece_price_gives_no_price(self):
        assert pricing_service.bundle_price(None, D("250")) is None

    def test_a_zero_bundle_gives_no_price(self):
        assert pricing_service.bundle_price(D("0.1826"), D("0")) is None

    def test_result_is_quantised_to_money(self):
        """One rounding, at the end. Deriving from the pack price instead would
        round on the way in and again on the way out."""
        price = pricing_service.bundle_price(D("0.075833"), D("300"))
        assert price == D("22.75")
        assert price.as_tuple().exponent == -2


# --------------------------------------------------------------------------- #
# Container estimate
# --------------------------------------------------------------------------- #

@pytest.fixture
def product_with_capacity(session, seeded):
    product = Product(
        item_number="WB-14", name='14" White', size_label='14" White',
        category="White Boxes",
    )
    session.add(product)
    session.flush()
    session.add(
        ProductVariant(
            product_id=product.id, variant_item_number="WB-14-115-50",
            board_quality="WT110 HPFL115 KM135", case_pack=50,
        )
    )
    capacity = ProductContainerCapacity(
        product_id=product.id,
        container_size=ContainerSize.FORTY_FT_HC,
        container_type=ContainerType.DRY,
        bundles_per_container=D("1310"),
    )
    session.add(capacity)
    session.commit()
    return product, capacity


class TestContainerEstimate:
    def test_no_estimate_without_a_bundle_size(self, session, product_with_capacity):
        """The workbook counts containers in bundles and a quotation counts in
        packs. Nothing bridges the two but the bundle size."""
        _, capacity = product_with_capacity
        assert capacity.packs_per_container is None
        assert pricing_service.containers_for_quantity(D("2000"), capacity) is None

    def test_estimate_once_the_bundle_size_is_known(
        self, session, product_with_capacity
    ):
        product, capacity = product_with_capacity
        product.units_per_bundle = D("50")
        session.commit()

        # 1310 bundles x 50 boxes = 65,500 boxes = 1,310 packs of 50.
        assert capacity.pieces_per_container == D("65500.000")
        assert capacity.packs_per_container == D("1310")

        containers = pricing_service.containers_for_quantity(D("2620"), capacity)
        assert containers == D("2")

    def test_partial_container_is_not_rounded_away(
        self, session, product_with_capacity
    ):
        product, capacity = product_with_capacity
        product.units_per_bundle = D("50")
        session.commit()

        containers = pricing_service.containers_for_quantity(D("655"), capacity)
        assert containers == D("0.5")

    def test_no_capacity_row_gives_no_estimate(self):
        assert pricing_service.containers_for_quantity(D("2000"), None) is None

    def test_no_quantity_gives_no_estimate(self, session, product_with_capacity):
        _, capacity = product_with_capacity
        assert pricing_service.containers_for_quantity(None, capacity) is None


# --------------------------------------------------------------------------- #
# Editing it
# --------------------------------------------------------------------------- #

class TestBundleSizeOnTheProduct:
    """It lives on the product, not on the capacity row: a bundle holds the
    same count whatever container it travels in."""

    def test_it_is_a_product_column(self):
        assert hasattr(Product, "units_per_bundle")
        assert not hasattr(ProductContainerCapacity, "units_per_bundle")

    def test_it_can_be_set_when_creating(self, session, make_auth_user):
        admin = make_auth_user("SYS_ADMIN")
        product = create_product(
            session, admin,
            ProductInput(
                item_number="WB-99", name='99" White', size_label='99" White',
                units_per_bundle=D("250"),
            ),
        )
        session.commit()
        assert product.units_per_bundle == D("250")

    def test_zero_is_stored_as_unset(self, session, make_auth_user):
        """The form's number input has no blank, so 0 is how "not settled" is
        expressed. Stored as zero it would make the bundle price zero."""
        admin = make_auth_user("SYS_ADMIN")
        product = create_product(
            session, admin,
            ProductInput(
                item_number="WB-98", name='98" White', size_label='98" White',
                units_per_bundle=D("0"),
            ),
        )
        session.commit()
        assert product.units_per_bundle is None

    def test_a_negative_bundle_is_refused(self):
        with pytest.raises(ValueError):
            ProductInput(
                item_number="WB-97", name="x", size_label="x",
                units_per_bundle=D("-5"),
            )

    def test_editing_it_is_audited(self, session, make_auth_user):
        from modules.models import AuditLog

        admin = make_auth_user("SYS_ADMIN")
        product = create_product(
            session, admin,
            ProductInput(item_number="WB-96", name="x", size_label='96" White'),
        )
        session.commit()

        update_product(
            session, admin, product.id,
            ProductInput(
                item_number="WB-96", name="x", size_label='96" White',
                units_per_bundle=D("250"),
            ),
        )
        session.commit()

        entry = (
            session.query(AuditLog)
            .filter(AuditLog.action == "PRODUCT_EDITED")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert "units_per_bundle" in entry.new_value_json


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

class TestCapacityImportDoesNotInventBundles:
    def test_import_leaves_a_hand_entered_bundle_size_alone(
        self, session, make_auth_user, product_with_capacity
    ):
        """A capacity import must not erase a figure somebody typed into the
        catalogue — the workbook has nothing to say about bundle contents."""
        from modules import capacity_importer

        product, _ = product_with_capacity
        product.units_per_bundle = D("250")
        session.commit()

        admin = make_auth_user("SYS_ADMIN")
        plan = capacity_importer.CapacityPlan(
            header_row=3,
            rows=[
                capacity_importer.CapacityRow(
                    source_row_no=4,
                    product_label='14" White',
                    bundles_per_container=D("1400"),
                )
            ],
            container_size=ContainerSize.FORTY_FT_HC,
            container_type=ContainerType.DRY,
        )
        capacity_importer.commit(session, admin, plan, file_name="test.xlsx")
        session.commit()

        assert product.units_per_bundle == D("250")

    def test_a_workbook_that_states_it_writes_it_to_the_product(
        self, session, make_auth_user, product_with_capacity
    ):
        from modules import capacity_importer

        product, _ = product_with_capacity
        admin = make_auth_user("SYS_ADMIN")
        plan = capacity_importer.CapacityPlan(
            header_row=3,
            rows=[
                capacity_importer.CapacityRow(
                    source_row_no=4,
                    product_label='14" White',
                    bundles_per_container=D("1400"),
                )
            ],
            container_size=ContainerSize.FORTY_FT_HC,
            container_type=ContainerType.DRY,
        )
        capacity_importer.commit(
            session, admin, plan, file_name="test.xlsx", units_per_bundle=D("100")
        )
        session.commit()

        assert product.units_per_bundle == D("100")
