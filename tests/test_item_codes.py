"""Item-code generation and the catalogue recode script."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules import item_codes
from modules.models import Product, ProductPrice, ProductVariant
from scripts import recode_catalogue


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #

class TestCodeComponents:
    def test_known_category_uses_its_prefix(self):
        assert item_codes.category_prefix("White Boxes") == "WB"

    def test_unknown_category_falls_back_to_initials(self):
        """An unplanned category yields a usable code rather than an error —
        the alternative is an import failing on a category nobody foresaw."""
        assert item_codes.category_prefix("Corrugated Trays") == "CT"

    def test_missing_category_still_produces_a_prefix(self):
        assert item_codes.category_prefix(None) == "IT"
        assert item_codes.category_prefix("") == "IT"

    @pytest.mark.parametrize(
        "label,expected",
        [
            ('7" White', "07"),
            ('10" White', "10"),
            ('20" White', "20"),
            ("7", "07"),
        ],
    )
    def test_size_is_zero_padded(self, label, expected):
        assert item_codes.size_token(label) == expected

    def test_padded_sizes_sort_in_size_order(self):
        """The whole point of the padding. The old codes sorted 10, 11, 12, 7,
        8, 9, which reads as though the small sizes are missing."""
        labels = ['7" White', '8" White', '9" White', '10" White', '20" White']
        codes = [item_codes.product_code("White Boxes", label) for label in labels]
        assert sorted(codes) == codes

    def test_fractional_size_keeps_its_point_and_still_sorts(self):
        half = item_codes.size_token('9.5" White')
        assert half == "09.5"
        assert item_codes.size_token('9" White') < half < item_codes.size_token('10"')

    def test_size_with_no_number_does_not_raise(self):
        assert item_codes.size_token("Assorted") == "00"
        assert item_codes.size_token(None) == "00"

    @pytest.mark.parametrize(
        "quality,expected",
        [
            ("WT110 HPFL115 KM135", "115"),
            ("WT110 HPFL135 KM135", "135"),
            ("WT110 HPFL160 KM135", "160"),
            ("wt110 hpfl 160 km135", "160"),
        ],
    )
    def test_board_token_is_the_hpfl_figure(self, quality, expected):
        """It is the only thing separating one board quality from another."""
        assert item_codes.board_token(quality) == expected

    def test_board_quality_written_another_way_is_slugified_not_guessed(self):
        assert item_codes.board_token("E-flute 200gsm") == "EFLUTE200GSM"

    def test_missing_board_quality_is_standard(self):
        assert item_codes.board_token(None) == "STD"


# --------------------------------------------------------------------------- #
# Codes
# --------------------------------------------------------------------------- #

class TestCodes:
    def test_product_code(self):
        assert item_codes.product_code("White Boxes", '7" White') == "WB-07"

    def test_variant_code_builds_onto_the_product(self):
        assert item_codes.variant_code("WB-07", "WT110 HPFL115 KM135", 50) == (
            "WB-07-115-50"
        )

    def test_variant_code_follows_a_disambiguated_product(self):
        """A product that had to take ``-2`` keeps its variants grouped under
        it, rather than having them sit beneath the product it collided with."""
        assert item_codes.variant_code("WB-07-2", "WT110 HPFL115 KM135", 50) == (
            "WB-07-2-115-50"
        )

    def test_generation_is_deterministic(self):
        first = item_codes.product_code("White Boxes", '13" White')
        second = item_codes.product_code("White Boxes", '13" White')
        assert first == second == "WB-13"

    def test_codes_fit_their_columns(self):
        long_category = "Extremely Long Category Name " * 4
        code = item_codes.product_code(long_category, '7" White')
        assert len(code) <= item_codes.MAX_PRODUCT_CODE

        variant = item_codes.variant_code("X" * 70, "Y" * 40, 50)
        assert len(variant) <= item_codes.MAX_VARIANT_CODE


class TestDisambiguation:
    def test_free_code_is_returned_unchanged(self):
        assert item_codes.disambiguate("WB-07", set(), 60) == "WB-07"

    def test_taken_code_gets_a_numeric_suffix(self):
        assert item_codes.disambiguate("WB-07", {"WB-07"}, 60) == "WB-07-2"

    def test_suffix_keeps_climbing(self):
        taken = {"WB-07", "WB-07-2", "WB-07-3"}
        assert item_codes.disambiguate("WB-07", taken, 60) == "WB-07-4"

    def test_suffix_fits_inside_the_column_limit(self):
        base = "A" * 60
        result = item_codes.disambiguate(base, {base}, 60)
        assert len(result) == 60
        assert result.endswith("-2")


# --------------------------------------------------------------------------- #
# The recode script
# --------------------------------------------------------------------------- #

@pytest.fixture
def catalogue(session):
    """Two sizes, three variants, on the importer's original codes."""
    products = {}
    for size in ('7" White', '10" White'):
        product = Product(
            item_number=f"{size.split(chr(34))[0]}-WHITE",
            name=size, size_label=size, category="White Boxes",
        )
        session.add(product)
        session.flush()
        products[size] = product

    qualities = {
        '7" White': ["WT110 HPFL115 KM135", "WT110 HPFL135 KM135"],
        '10" White': ["WT110 HPFL115 KM135"],
    }
    for size, product in products.items():
        for quality in qualities[size]:
            slug = quality.replace(" ", "-")
            session.add(
                ProductVariant(
                    product_id=product.id,
                    variant_item_number=f"{product.item_number}-{slug}-50",
                    board_quality=quality, case_pack=50,
                )
            )
    session.commit()
    return products


class TestRecodeScript:
    def test_plan_covers_every_row(self, session, catalogue):
        products, variants = recode_catalogue.plan(session)
        assert len(products) == 2
        assert len(variants) == 3

    def test_plan_writes_nothing(self, session, catalogue):
        recode_catalogue.plan(session)
        session.rollback()
        assert catalogue['7" White'].item_number == "7-WHITE"

    def test_apply_rewrites_products_and_variants(self, session, catalogue):
        products, variants = recode_catalogue.plan(session)
        recode_catalogue.apply(session, products, variants)
        session.commit()

        assert catalogue['7" White'].item_number == "WB-07"
        assert catalogue['10" White'].item_number == "WB-10"
        codes = sorted(
            v.variant_item_number
            for v in session.query(ProductVariant).all()
        )
        assert codes == ["WB-07-115-50", "WB-07-135-50", "WB-10-115-50"]

    def test_running_twice_changes_nothing_the_second_time(self, session, catalogue):
        products, variants = recode_catalogue.plan(session)
        first = recode_catalogue.apply(session, products, variants)
        session.commit()
        assert first == 5

        products, variants = recode_catalogue.plan(session)
        second = recode_catalogue.apply(session, products, variants)
        session.commit()
        assert second == 0

    def test_rename_into_a_code_another_row_still_holds(self, session, catalogue):
        """The two-pass write exists for this case.

        Parking every row on a placeholder first means a rename can land on a
        code that is occupied at the moment the pass begins, which a single
        pass would fail on even though the end state is perfectly consistent.
        """
        seven = catalogue['7" White']
        ten = catalogue['10" White']
        seven.item_number, ten.item_number = "WB-10", "WB-07"
        session.commit()

        products, variants = recode_catalogue.plan(session)
        recode_catalogue.apply(session, products, variants)
        session.commit()

        assert seven.item_number == "WB-07"
        assert ten.item_number == "WB-10"

    def test_two_products_generating_one_code_are_kept_apart(self, session):
        """Same size, same category, differing in something the code does not
        carry. Neither is dropped and neither overwrites the other."""
        for suffix in ("A", "B"):
            product = Product(
                item_number=f"OLD-{suffix}", name='7" White',
                size_label='7" White', category="White Boxes",
            )
            session.add(product)
        session.commit()

        products, variants = recode_catalogue.plan(session)
        recode_catalogue.apply(session, products, variants)
        session.commit()

        codes = sorted(p.item_number for p in session.query(Product).all())
        assert codes == ["WB-07", "WB-07-2"]

    def test_soft_deleted_products_are_recoded_too(self, session, catalogue):
        """They still hold their codes against the unique index, so leaving
        them out would let a live product collide with one."""
        catalogue['7" White'].deleted_at = dt.datetime.now(dt.UTC)
        session.commit()

        products, _ = recode_catalogue.plan(session)
        assert len(products) == 2

    def test_quotation_lines_keep_the_code_they_were_quoted_under(
        self, session, catalogue, make_auth_user
    ):
        """A customer holding a PDF must be able to quote its codes back at us,
        so recoding the catalogue must not reach into quotation history."""
        from modules import quotation_service
        from modules.customer_service import create_customer
        from modules.models import PriceTier
        from modules.validation import CustomerInput

        user = make_auth_user("SALES")  # brings the seeded tiers with it
        variant = session.query(ProductVariant).first()
        tier = session.query(PriceTier).filter_by(code="STANDARD").one()
        session.add(
            ProductPrice(
                product_variant_id=variant.id, price_tier_id=tier.id,
                price_per_pack=D("3.79"), price_per_piece=D("0.0758"),
                effective_from=dt.date(2026, 1, 1),
            )
        )
        customer = create_customer(
            session, user,
            CustomerInput(customer_number="CUST-9001", company_name="Bunzl Canada"),
        )
        session.commit()

        quotation = quotation_service.create_draft(session, user, customer.id)
        item = quotation_service.add_line(
            session, user, quotation,
            product_variant_id=variant.id,
            price_tier_code="STANDARD",
            quantity_packs=D("100"),
        )
        session.commit()
        quoted_code = item.item_number_snapshot
        assert quoted_code == variant.variant_item_number

        products, variants = recode_catalogue.plan(session)
        recode_catalogue.apply(session, products, variants)
        session.commit()

        session.refresh(item)
        assert item.item_number_snapshot == quoted_code
        assert variant.variant_item_number != quoted_code


class TestImporterUsesTheScheme:
    def test_importer_mints_codes_in_the_new_scheme(self):
        """A price list imported tomorrow must not reintroduce the old codes
        alongside the recoded catalogue."""
        from modules.excel_importer import _item_number_for, _variant_item_number
        from modules.validation import PriceRowInput

        parsed = PriceRowInput(
            source_row_no=4,
            product='14" White',
            board_quality="WT110 HPFL160 KM135",
            case_pack=50,
            standard_price_per_pack=D("6.50"),
            standard_price_per_piece=D("0.13"),
        )
        product_code = _item_number_for(parsed)
        assert product_code == "WB-14"
        assert _variant_item_number(parsed, product_code) == "WB-14-160-50"
