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


# --------------------------------------------------------------------------- #
# When a bundle is the pack
# --------------------------------------------------------------------------- #

class TestBundleIsThePack:
    """How this catalogue is actually sold: one bundle is one pack of 50, so
    the workbook's bundles-per-container is packs-per-container directly."""

    def test_capacity_needs_no_conversion(self, session, product_with_capacity):
        product, capacity = product_with_capacity
        product.units_per_bundle = D("50")  # the case pack
        session.commit()

        assert capacity.packs_per_container == capacity.bundles_per_container

    def test_bundle_price_equals_the_pack_price(self):
        """Which is why the editor stops printing it as a second line: two
        identical figures invite a hunt for the difference between them."""
        piece, case_pack = D("0.1826"), D("50")
        assert pricing_service.bundle_price(piece, case_pack) == D("9.13")


class TestEstimatedContainerTotal:
    """Capacity fills the gap only where nothing better exists."""

    @pytest.fixture
    def quote(self, session, seeded, make_auth_user, product_with_capacity):
        from modules import quotation_service
        from modules.customer_service import create_customer
        from modules.models import PriceTier, ProductPrice
        from modules.validation import CustomerInput

        product, capacity = product_with_capacity
        product.units_per_bundle = D("50")
        variant = product.variants[0]
        tier = session.query(PriceTier).filter_by(code="STANDARD").one()
        session.add(
            ProductPrice(
                product_variant_id=variant.id, price_tier_id=tier.id,
                price_per_pack=D("9.13"), price_per_piece=D("0.1826"),
                effective_from=__import__("datetime").date(2026, 1, 1),
            )
        )
        user = make_auth_user("SALES_MANAGER", username="mgr")
        customer = create_customer(
            session, user,
            CustomerInput(customer_number="CUST-7001", company_name="Bunzl"),
        )
        session.commit()
        quotation = quotation_service.create_draft(session, user, customer.id)
        session.commit()
        return quotation, user, variant, capacity

    def test_estimated_from_capacity_when_nothing_is_stated(self, session, quote):
        from modules import quotation_service
        from modules.pricing_service import _quotation_container_total

        quotation, user, variant, _ = quote
        quotation_service.add_line(
            session, user, quotation,
            product_variant_id=variant.id, price_tier_code="STANDARD",
            quantity_packs=D("2620"),          # 2 x 1,310 packs per container
        )
        session.commit()

        total = _quotation_container_total(session, quotation, list(quotation.items))
        assert total == D("2")

    def test_a_typed_container_count_outranks_the_estimate(self, session, quote):
        """The workbook has at least one figure that cannot be right, so
        anything a person actually stated wins."""
        from modules import quotation_service
        from modules.pricing_service import _quotation_container_total

        quotation, user, variant, _ = quote
        quotation_service.add_line(
            session, user, quotation,
            product_variant_id=variant.id, price_tier_code="STANDARD",
            quantity_packs=D("2620"), container_count=D("5"),
        )
        session.commit()

        total = _quotation_container_total(session, quotation, list(quotation.items))
        assert total == D("5")

    def test_a_line_without_capacity_contributes_nothing(self, session, quote):
        """Rather than being assumed to fill a container."""
        from modules import quotation_service
        from modules.models import ProductContainerCapacity
        from modules.pricing_service import _quotation_container_total

        quotation, user, variant, capacity = quote
        session.query(ProductContainerCapacity).filter_by(id=capacity.id).delete()
        session.commit()

        quotation_service.add_line(
            session, user, quotation,
            product_variant_id=variant.id, price_tier_code="STANDARD",
            quantity_packs=D("2620"),
        )
        session.commit()

        total = _quotation_container_total(session, quotation, list(quotation.items))
        assert total == D("0")


class TestBundleShownInTheEditor:
    """The figure the operator sees beside the tier prices."""

    def test_capacity_is_found_without_naming_a_container(
        self, session, product_with_capacity
    ):
        """The line editor shows it before any container has been chosen."""
        from modules import shipping_service

        product, capacity = product_with_capacity
        found = shipping_service.container_capacity_for_product(session, product.id)
        assert found is not None
        assert found.bundles_per_container == capacity.bundles_per_container

    def test_a_product_with_no_capacity_returns_none(self, session, seeded):
        """So the editor says the quantity is unrecorded rather than showing a
        figure belonging to some other size."""
        from modules import shipping_service

        product = Product(
            item_number="WB-95", name='95" White', size_label='95" White',
        )
        session.add(product)
        session.commit()

        assert shipping_service.container_capacity_for_product(
            session, product.id
        ) is None

    def test_a_non_default_container_size_is_still_found(self, session, seeded):
        """A catalogue holding only 20 ft rows shows those rather than nothing."""
        from modules import shipping_service

        product = Product(
            item_number="WB-94", name='94" White', size_label='94" White',
        )
        session.add(product)
        session.flush()
        session.add(
            ProductContainerCapacity(
                product_id=product.id,
                container_size=ContainerSize.TWENTY_FT,
                container_type=ContainerType.DRY,
                bundles_per_container=D("640"),
            )
        )
        session.commit()

        found = shipping_service.container_capacity_for_product(session, product.id)
        assert found is not None
        assert found.bundles_per_container == D("640")

    def test_the_anomaly_flag_travels_with_the_figure(self, session, seeded):
        """The 20 inch row is shown, and shown as doubtful. A salesperson
        reading 1,512 has no way to know it is twice what it should be."""
        from modules import shipping_service

        product = Product(
            item_number="WB-20", name='20" White', size_label='20" White',
        )
        session.add(product)
        session.flush()
        session.add(
            ProductContainerCapacity(
                product_id=product.id,
                container_size=ContainerSize.FORTY_FT_HC,
                container_type=ContainerType.DRY,
                bundles_per_container=D("1512"),
                is_anomalous=True,
                anomaly_note="1,512 bundles is more than the 890 recorded for the 18 inch.",
            )
        )
        session.commit()

        found = shipping_service.container_capacity_for_product(session, product.id)
        assert found.is_anomalous is True
        assert found.anomaly_note


class TestCasePackIsPartOfIdentity:
    """The 20 inch correction ran into this guard, and the guard was right."""

    def test_case_pack_cannot_be_edited_in_place(self, session, make_auth_user):
        """A quotation line snapshots the case pack it was sold under, so
        changing it would alter what a past quotation meant. The catalogue
        makes you retire the variant and create its replacement."""
        from modules.catalogue_service import (
            CatalogueError,
            create_variant,
            update_variant,
        )
        from modules.validation import VariantInput

        admin = make_auth_user("SYS_ADMIN")
        product = Product(
            item_number="WB-93", name='93" White', size_label='93" White',
        )
        session.add(product)
        session.flush()
        variant = create_variant(
            session, admin, product.id,
            VariantInput(
                variant_item_number="WB-93-160-50",
                board_quality="WT110 HPFL160 KM135", case_pack=50,
            ),
        )
        session.commit()

        with pytest.raises(CatalogueError, match="case pack"):
            update_variant(
                session, admin, variant.id,
                VariantInput(
                    variant_item_number="WB-93-160-25",
                    board_quality="WT110 HPFL160 KM135", case_pack=25,
                ),
            )

    def test_a_replacement_variant_carries_the_corrected_pack(
        self, session, make_auth_user
    ):
        from modules.catalogue_service import create_variant
        from modules.validation import VariantInput

        admin = make_auth_user("SYS_ADMIN")
        product = Product(
            item_number="WB-92", name='92" White', size_label='92" White',
        )
        session.add(product)
        session.flush()
        old = create_variant(
            session, admin, product.id,
            VariantInput(
                variant_item_number="WB-92-160-50",
                board_quality="WT110 HPFL160 KM135", case_pack=50,
            ),
        )
        new = create_variant(
            session, admin, product.id,
            VariantInput(
                variant_item_number="WB-92-160-25",
                board_quality="WT110 HPFL160 KM135", case_pack=25,
            ),
        )
        old.is_active = False
        session.commit()

        assert new.case_pack == 25
        # The old one is kept, not deleted: it records what the price list said.
        assert old.deleted_at is None
        assert old.is_active is False
