"""The supplier price build.

The rule under test, and the reason this module exists: **markup applies to the
whole original cost, freight included.** An earlier reading marked up only the
factory cost and added freight afterwards, which is cheaper by
``fob_per_bundle x markup`` on every bundle sold.
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from modules import supplier_pricing as sp


def _build(**kw):
    args = dict(
        unit_cost_per_piece=D("0.0683"),
        pieces_per_bundle=D("50"),
        bundles_per_container=D("2304"),
        total_fob_cost=D("700"),
        markup_percentage=D("0.17"),
    )
    args.update(kw)
    return sp.build(**args)


class TestTheWorkedExample:
    """The 8 inch box, stated by the business as the reference case."""

    def test_fob_is_shared_across_the_bundles_in_a_container(self):
        assert _build().fob_cost_per_bundle == D("700") / D("2304")

    def test_original_cost_is_goods_plus_freight(self):
        build = _build()
        assert build.product_cost_per_bundle == D("3.415")
        assert build.original_cost.quantize(D("0.000001")) == D("3.718819")

    def test_selling_price_marks_up_the_whole_cost(self):
        """4.35101875 exactly, which is 4.351019 at six places.

        The brief quotes 4.351018 — a truncation of the same number, not a
        different one. Both display as 4.3510, which is what reaches a
        customer, and the full-precision value is what is kept internally.
        """
        build = _build()
        # Not compared to a literal: 700/2304 does not terminate, so the value
        # carries the context's 28 significant digits and no short decimal
        # equals it. Rounding is where the comparison belongs.
        assert build.selling_price.quantize(D("0.000001")) == D("4.351019")
        assert build.selling_price.quantize(D("0.01")) == D("4.35")

    def test_the_two_headline_figures_display_at_four_places(self):
        assert _build().for_display() == {
            "original_cost": D("3.7188"),
            "selling_price": D("4.3510"),
        }

    def test_it_is_not_the_old_formula(self):
        """The regression this exists to prevent.

        Marking up the goods alone and adding freight after gives 4.2994 — five
        cents a bundle less, and about $120 a container.
        """
        build = _build()
        old = (
            build.unit_cost_per_piece
            * build.markup_multiplier
            * build.pieces_per_bundle
            + build.fob_cost_per_bundle
        )
        assert old.quantize(D("0.0001")) == D("4.2994")
        assert build.selling_price > old
        # Quantized on both sides: Decimal carries 28 significant digits, so
        # two routes to one number can differ in the last place.
        gap = build.selling_price - old
        expected = build.fob_cost_per_bundle * build.markup_percentage
        assert gap.quantize(D("0.00000001")) == expected.quantize(D("0.00000001"))


class TestInputsAreConfiguration:
    def test_zero_fob_is_allowed_and_leaves_only_the_goods(self):
        """An ex-works sale carries no freight. That is a price, not an error."""
        build = _build(total_fob_cost=D("0"))
        assert build.fob_cost_per_bundle == D("0")
        assert build.original_cost == D("3.415")
        assert build.selling_price.quantize(D("0.0001")) == D("3.9956")

    def test_a_different_markup(self):
        build = _build(markup_percentage=D("0.25"))
        assert build.markup_multiplier == D("1.25")
        assert build.selling_price == build.original_cost * D("1.25")

    def test_zero_markup_sells_at_cost(self):
        build = _build(markup_percentage=D("0"))
        assert build.selling_price == build.original_cost
        assert build.gross_profit_per_bundle == D("0")

    def test_a_different_bundle_size(self):
        """25 to a bundle halves the goods cost per bundle; freight is untouched."""
        build = _build(pieces_per_bundle=D("25"))
        assert build.product_cost_per_bundle == D("1.7075")
        assert build.fob_cost_per_bundle == D("700") / D("2304")

    def test_markup_is_not_margin(self):
        """17% on cost is 14.53% on price, and conflating them loses money."""
        build = _build(total_fob_cost=D("0"))
        assert build.markup_percentage == D("0.17")
        assert build.margin_percentage.quantize(D("0.0001")) == D("0.1453")


class TestCapacityChangesThePrice:
    def test_the_same_size_at_two_capacities_prices_differently(self):
        """Why capacity had to move from the product to the variant.

        Both are 8 inch boxes. IK135 fits 2,160 bundles in a container where
        IK90 and IK120 fit 2,304, so it carries more freight per bundle.
        """
        wide = _build(bundles_per_container=D("2304"))
        narrow = _build(bundles_per_container=D("2160"))
        assert narrow.fob_cost_per_bundle > wide.fob_cost_per_bundle
        assert narrow.selling_price > wide.selling_price


class TestValidation:
    def test_negative_unit_cost_is_refused(self):
        with pytest.raises(sp.PricingError, match="cannot be negative"):
            _build(unit_cost_per_piece=D("-0.01"))

    def test_zero_pieces_per_bundle_is_refused(self):
        with pytest.raises(sp.PricingError, match="greater than zero"):
            _build(pieces_per_bundle=D("0"))

    @pytest.mark.parametrize("bundles", [D("0"), D("-1")])
    def test_a_container_holding_nothing_cannot_share_freight(self, bundles):
        """The division-by-zero guard, stated as the business problem it is."""
        with pytest.raises(sp.PricingError, match="greater than zero"):
            _build(bundles_per_container=bundles)

    def test_negative_fob_is_refused(self):
        with pytest.raises(sp.PricingError, match="cannot be negative"):
            _build(total_fob_cost=D("-1"))

    def test_negative_markup_is_refused(self):
        with pytest.raises(sp.PricingError, match="cannot be negative"):
            _build(markup_percentage=D("-0.1"))

    def test_zero_unit_cost_is_permitted(self):
        """Free of charge is a decision somebody may make deliberately."""
        build = _build(unit_cost_per_piece=D("0"))
        assert build.product_cost_per_bundle == D("0")
        assert build.original_cost == build.fob_cost_per_bundle


class TestPrecision:
    def test_nothing_is_rounded_before_the_end(self):
        """700 / 2304 does not terminate; rounding it early loses money.

        Rounded to cents at the intermediate step the bundle price comes out
        a hundredth low, which is $23 on a full container.
        """
        build = _build()
        assert build.fob_cost_per_bundle != D("0.30")
        assert build.original_cost != D("3.72")
        early = (D("3.415") + D("0.30")) * D("1.17")
        assert (build.selling_price - early) > D("0.004")

    def test_every_value_is_decimal_not_float(self):
        build = _build()
        for value in (
            build.product_cost_per_bundle, build.fob_cost_per_bundle,
            build.original_cost, build.selling_price, build.container_value,
        ):
            assert isinstance(value, D)

    def test_a_float_input_does_not_poison_the_result(self):
        """0.1 + 0.2 arithmetic must not reach a customer's price."""
        build = _build(unit_cost_per_piece=0.0683)
        assert build.product_cost_per_bundle == D("3.415")

    def test_container_value_is_the_price_times_the_bundles(self):
        build = _build()
        assert build.container_value == build.selling_price * D("2304")


class TestSettingsAreTheSourceOfTheInputs:
    def test_the_three_inputs_read_from_settings(self, session):
        from modules import settings_service

        assert settings_service.pieces_per_bundle(session) == D("50")
        assert settings_service.total_fob_cost(session) == D("700")
        assert settings_service.markup_percentage(session) == D("0.17")

    def test_markup_is_stored_as_a_rate_not_a_multiplier(self, session):
        """1.17 is a multiplier and calling it a margin has misled once already."""
        from modules import settings_service

        rate = settings_service.markup_percentage(session)
        assert rate < D("1")
        assert sp.build(
            unit_cost_per_piece=D("1"), pieces_per_bundle=D("1"),
            bundles_per_container=D("1"), total_fob_cost=D("0"),
            markup_percentage=rate,
        ).markup_multiplier == D("1.17")
