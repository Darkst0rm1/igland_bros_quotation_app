"""Container capacity is a property of the variant, not of the size.

Capacity used to be keyed on the product, on the reasoning that two board
qualities of one size are dimensionally identical. The supplier's own sheet
disproves it: WTL125 FL135 IK135 fits 2,160 bundles in a container where
IK90 and IK120 fit 2,304, and the price is built from that figure. Keying on
the product forced one quality's capacity onto all three, which moved both the
container count printed for a customer and the freight inside the cost.

A product-wide row is still meaningful — the older workbook stated capacity per
size, and that is true of every variant of the product — so the lookup prefers a
variant row and falls back.
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from modules import shipping_service
from modules.constants import ContainerSize, ContainerType
from modules.models import Product, ProductContainerCapacity, ProductVariant
from tests.test_documents_and_approval import (  # noqa: F401
    admin, manager, quotation, sales, variant,
)


@pytest.fixture
def two_qualities(session, admin, variant):
    """One size, two board qualities, different container capacities."""
    from modules.catalogue_service import create_variant
    from modules.validation import VariantInput

    other = create_variant(
        session, admin, variant.product_id,
        VariantInput(
            variant_item_number="WB-12-HEAVY",
            board_quality="WTL125 FL135 IK135",
            case_pack=50,
        ),
    )
    session.flush()
    session.add_all([
        ProductContainerCapacity(
            product_id=variant.product_id, product_variant_id=variant.id,
            container_size=ContainerSize.FORTY_FT_HC,
            container_type=ContainerType.DRY,
            bundles_per_container=D("2304"),
        ),
        ProductContainerCapacity(
            product_id=variant.product_id, product_variant_id=other.id,
            container_size=ContainerSize.FORTY_FT_HC,
            container_type=ContainerType.DRY,
            bundles_per_container=D("2160"),
        ),
    ])
    session.flush()
    return variant, other


class TestLookup:
    def test_each_variant_gets_its_own_capacity(self, session, two_qualities):
        light, heavy = two_qualities
        got_light = shipping_service.container_capacity_for_product(
            session, light.product_id, variant_id=light.id
        )
        got_heavy = shipping_service.container_capacity_for_product(
            session, heavy.product_id, variant_id=heavy.id
        )
        assert got_light.bundles_per_container == D("2304")
        assert got_heavy.bundles_per_container == D("2160")

    def test_a_product_wide_row_is_used_when_the_variant_has_none(
        self, session, admin, variant
    ):
        session.add(ProductContainerCapacity(
            product_id=variant.product_id, product_variant_id=None,
            container_size=ContainerSize.FORTY_FT_HC,
            container_type=ContainerType.DRY,
            bundles_per_container=D("1500"),
        ))
        session.flush()
        got = shipping_service.container_capacity_for_product(
            session, variant.product_id, variant_id=variant.id
        )
        assert got.bundles_per_container == D("1500")

    def test_a_variant_row_outranks_the_product_wide_one(
        self, session, admin, variant
    ):
        session.add_all([
            ProductContainerCapacity(
                product_id=variant.product_id, product_variant_id=None,
                container_size=ContainerSize.FORTY_FT_HC,
                container_type=ContainerType.DRY,
                bundles_per_container=D("1500"),
            ),
            ProductContainerCapacity(
                product_id=variant.product_id, product_variant_id=variant.id,
                container_size=ContainerSize.FORTY_FT_HC,
                container_type=ContainerType.DRY,
                bundles_per_container=D("2304"),
            ),
        ])
        session.flush()
        got = shipping_service.container_capacity_for_product(
            session, variant.product_id, variant_id=variant.id
        )
        assert got.bundles_per_container == D("2304"), (
            "the variant's own figure must win; falling back to the product "
            "row applies another board quality's capacity"
        )

    def test_both_rows_may_coexist(self, session, variant):
        """The old constraint allowed one row per (product, size, type).

        b7c1e4f8a903 replaced it with two partial indexes so a variant row and
        the product-wide fallback can both exist. c3d9a1b7e502 dropped the
        original, which had survived a drop by a name that never matched.
        """
        session.add_all([
            ProductContainerCapacity(
                product_id=variant.product_id, product_variant_id=None,
                container_size=ContainerSize.FORTY_FT_HC,
                container_type=ContainerType.DRY,
                bundles_per_container=D("1500"),
            ),
            ProductContainerCapacity(
                product_id=variant.product_id, product_variant_id=variant.id,
                container_size=ContainerSize.FORTY_FT_HC,
                container_type=ContainerType.DRY,
                bundles_per_container=D("2304"),
            ),
        ])
        session.flush()  # would raise if the legacy constraint were still there


class TestPacksPerContainer:
    def test_a_variant_row_uses_that_variant_s_case_pack(
        self, session, admin, variant
    ):
        """Not the first variant of the product, which was a guess."""
        product = session.get(Product, variant.product_id)
        product.units_per_bundle = D("50")
        cap = ProductContainerCapacity(
            product_id=variant.product_id, product_variant_id=variant.id,
            container_size=ContainerSize.FORTY_FT_HC,
            container_type=ContainerType.DRY,
            bundles_per_container=D("2304"),
        )
        session.add(cap)
        session.flush()
        # 2,304 bundles x 50 pieces = 115,200 pieces, / 50 per pack.
        assert cap.pieces_per_container == D("115200")
        assert cap.packs_per_container == D("2304")


class TestItReachesTheDocument:
    def test_the_container_column_uses_the_line_s_own_capacity(
        self, session, admin, quotation, variant
    ):
        """End to end: two qualities of one size print different counts."""
        from modules import document_model

        product = session.get(Product, variant.product_id)
        product.units_per_bundle = D("50")
        session.add(ProductContainerCapacity(
            product_id=variant.product_id, product_variant_id=variant.id,
            container_size=ContainerSize.FORTY_FT_HC,
            container_type=ContainerType.DRY,
            bundles_per_container=D("500"),
        ))
        session.flush()

        values = document_model.build_document(session, quotation).lines[0].values
        # The fixture line is 1,000 packs; 500 packs fill a container.
        assert values["containers"] == "2"

    def test_the_product_wide_row_is_not_silently_preferred(
        self, session, admin, quotation, variant
    ):
        from modules import document_model

        product = session.get(Product, variant.product_id)
        product.units_per_bundle = D("50")
        session.add_all([
            ProductContainerCapacity(
                product_id=variant.product_id, product_variant_id=None,
                container_size=ContainerSize.FORTY_FT_HC,
                container_type=ContainerType.DRY,
                bundles_per_container=D("250"),
            ),
            ProductContainerCapacity(
                product_id=variant.product_id, product_variant_id=variant.id,
                container_size=ContainerSize.FORTY_FT_HC,
                container_type=ContainerType.DRY,
                bundles_per_container=D("500"),
            ),
        ])
        session.flush()
        values = document_model.build_document(session, quotation).lines[0].values
        assert values["containers"] == "2", "took the product-wide 250 instead"
