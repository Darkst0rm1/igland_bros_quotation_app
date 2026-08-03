"""Calculation engine: rounding, line maths, totals, margin, plates.

These tests need no database and no Streamlit, which is the whole point of
keeping the arithmetic in a module that imports neither.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from modules.calculation_engine import (
    ChargeInput,
    LineInput,
    PlateChargeInput,
    compute_line,
    compute_totals,
    convert,
    gross_profit,
    packs_from_pieces,
    piece_pack_mismatch,
    pieces_from_packs,
    plate_charge,
    price_per_piece_from_pack,
    q_money,
    safe_margin_pct,
    safe_markup_pct,
    to_decimal,
)
from modules.constants import PricingBasis


# --------------------------------------------------------------------------- #
# Rounding
# --------------------------------------------------------------------------- #

class TestRounding:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0.125", "0.13"),   # banker's rounding would give 0.12
            ("0.135", "0.14"),
            ("0.145", "0.15"),   # banker's rounding would give 0.14
            ("2.675", "2.68"),
            ("0.005", "0.01"),
            ("-0.125", "-0.13"),
        ],
    )
    def test_half_up_not_half_even(self, value, expected):
        assert q_money(D(value)) == D(expected)

    def test_float_input_does_not_leak_binary_error(self):
        # Decimal(0.1) is 0.1000000000000000055511151231257827; via str it is 0.1
        assert to_decimal(0.1) == D("0.1")

    def test_none_is_zero(self):
        assert to_decimal(None) == D("0")


# --------------------------------------------------------------------------- #
# Quantity conversion
# --------------------------------------------------------------------------- #

class TestQuantityConversion:
    def test_pieces_from_packs(self):
        assert pieces_from_packs(D("1000"), 50) == D("50000.000")

    def test_packs_from_pieces_round_trip(self):
        assert packs_from_pieces(D("50000"), 50) == D("1000.000")

    def test_packs_from_pieces_allows_fractional(self):
        assert packs_from_pieces(D("75"), 50) == D("1.500")

    def test_zero_quantity(self):
        assert pieces_from_packs(D("0"), 50) == D("0.000")

    def test_case_pack_must_be_positive(self):
        with pytest.raises(ValueError):
            packs_from_pieces(D("100"), 0)
        with pytest.raises(ValueError):
            price_per_piece_from_pack(D("3.79"), 0)


# --------------------------------------------------------------------------- #
# Piece / pack consistency
# --------------------------------------------------------------------------- #

class TestPiecePackTolerance:
    """The reference workbook's own columns disagree by up to one rounding unit
    on 25 of 69 price pairs. The check has to tolerate that and still catch a
    genuine error."""

    @pytest.mark.parametrize(
        ("pack", "piece"),
        [
            ("3.56", "0.0713"),   # workbook row 3, 8-container
            ("4.49", "0.0899"),   # workbook row 4, standard
            ("6.32", "0.1263"),   # workbook row 7, 8-container
            ("7.79", "0.1559"),   # workbook row 9, 8-container
            ("6.29", "0.1259"),   # workbook row 30, standard
        ],
    )
    def test_known_workbook_discrepancies_are_tolerated(self, pack, piece):
        assert piece_pack_mismatch(D(pack), D(piece), 50) is None

    def test_exact_pairs_pass(self):
        assert piece_pack_mismatch(D("3.79"), D("0.0758"), 50) is None

    def test_real_mismatch_is_flagged(self):
        delta = piece_pack_mismatch(D("6.32"), D("0.1200"), 50)
        assert delta is not None and delta > D("0.006")

    def test_derived_piece_price(self):
        assert price_per_piece_from_pack(D("3.79"), 50) == D("0.075800")


# --------------------------------------------------------------------------- #
# Line calculations
# --------------------------------------------------------------------------- #

class TestLineCalculations:
    def test_gross_by_pack(self):
        result = compute_line(
            LineInput(quantity_packs=D("1000"), case_pack=50, price_per_pack=D("3.79"))
        )
        assert result.gross_line_total == D("3790.00")
        assert result.quantity_pieces == D("50000.000")

    def test_gross_by_piece_uses_the_piece_column(self):
        result = compute_line(
            LineInput(
                quantity_packs=D("1000"), case_pack=50,
                price_per_pack=D("3.56"), price_per_piece=D("0.0713"),
                pricing_basis=PricingBasis.PIECE,
            )
        )
        assert result.gross_line_total == D("3565.00")

    def test_basis_changes_the_answer_on_this_catalogue(self):
        """Documents why pricing_basis is stored rather than inferred."""
        shared = dict(quantity_packs=D("1000"), case_pack=50,
                      price_per_pack=D("3.56"), price_per_piece=D("0.0713"))
        by_pack = compute_line(LineInput(**shared, pricing_basis=PricingBasis.PACK))
        by_piece = compute_line(LineInput(**shared, pricing_basis=PricingBasis.PIECE))
        assert by_pack.gross_line_total == D("3560.00")
        assert by_piece.gross_line_total == D("3565.00")
        assert by_pack.gross_line_total != by_piece.gross_line_total

    def test_percentage_discount(self):
        result = compute_line(
            LineInput(quantity_packs=D("100"), case_pack=50,
                      price_per_pack=D("10.00"), line_discount_pct=D("12.5"))
        )
        assert result.gross_line_total == D("1000.00")
        assert result.line_discount_amount == D("125.00")
        assert result.net_line_total == D("875.00")

    def test_explicit_discount_amount_overrides_percentage(self):
        result = compute_line(
            LineInput(quantity_packs=D("100"), case_pack=50, price_per_pack=D("10.00"),
                      line_discount_pct=D("50"), line_discount_amount=D("75.00"))
        )
        assert result.line_discount_amount == D("75.00")
        assert result.net_line_total == D("925.00")

    def test_discount_cannot_invert_a_line(self):
        result = compute_line(
            LineInput(quantity_packs=D("10"), case_pack=50,
                      price_per_pack=D("10.00"), line_discount_pct=D("150"))
        )
        assert result.net_line_total == D("0.00")

    def test_piece_price_is_derived_when_absent(self):
        result = compute_line(
            LineInput(quantity_packs=D("1"), case_pack=50, price_per_pack=D("3.79"))
        )
        assert result.price_per_piece == D("0.075800")

    def test_savings_against_standard(self):
        result = compute_line(
            LineInput(quantity_packs=D("1000"), case_pack=50, price_per_pack=D("3.56"),
                      standard_price_per_pack=D("3.79"))
        )
        assert result.savings_per_pack == D("0.230000")
        assert result.total_savings == D("230.00")

    def test_savings_absent_without_a_standard_price(self):
        result = compute_line(
            LineInput(quantity_packs=D("1000"), case_pack=50, price_per_pack=D("3.56"))
        )
        assert result.savings_per_pack is None
        assert result.total_savings is None


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #

class TestTotals:
    @staticmethod
    def _lines():
        return [
            compute_line(LineInput(quantity_packs=D(packs), case_pack=50,
                                   price_per_pack=D(price), line_discount_pct=D("2.5")))
            for packs, price in [("333", "6.72"), ("777", "8.29"), ("101", "13.92")]
        ]

    def test_subtotal_is_the_sum_of_net_lines(self):
        lines = self._lines()
        totals = compute_totals(lines)
        assert totals.subtotal == sum(ln.net_line_total for ln in lines)

    def test_grand_total_foots_exactly(self):
        totals = compute_totals(
            self._lines(),
            charges=[
                ChargeInput(quantity=D("3"), rate=D("200"), is_taxable=False),
                ChargeInput(quantity=D("1"), rate=D("1250.50"), is_taxable=True),
            ],
            quotation_discount_pct=D("1.5"),
            tax_rate_pct=D("18"),
        )
        assert totals.grand_total == (
            totals.subtotal - totals.quotation_discount + totals.charges_total
            + totals.tax_amount
        )

    def test_non_taxable_charges_are_excluded_from_the_tax_base(self):
        lines = [compute_line(LineInput(quantity_packs=D("100"), case_pack=50,
                                        price_per_pack=D("10.00")))]
        totals = compute_totals(
            lines,
            charges=[ChargeInput(quantity=D("1"), rate=D("500"), is_taxable=False)],
            tax_rate_pct=D("20"),
        )
        assert totals.taxable_base == D("1000.00")
        assert totals.tax_amount == D("200.00")
        assert totals.grand_total == D("1700.00")

    def test_internal_only_charges_still_count_toward_the_total(self):
        lines = [compute_line(LineInput(quantity_packs=D("100"), case_pack=50,
                                        price_per_pack=D("10.00")))]
        totals = compute_totals(
            lines,
            charges=[ChargeInput(quantity=D("1"), rate=D("400"),
                                 is_customer_visible=False)],
        )
        assert totals.charges_total == D("400.00")
        assert totals.charges_customer_visible == D("0")
        assert totals.grand_total == D("1400.00")

    def test_charge_currency_conversion(self):
        charge = ChargeInput(quantity=D("2"), rate=D("100"), exchange_rate=D("1.35"))
        totals = compute_totals([], charges=[charge])
        assert totals.charges_total == D("270.00")

    def test_empty_quotation(self):
        totals = compute_totals([])
        assert totals.subtotal == D("0")
        assert totals.grand_total == D("0")
        assert totals.total_cost is None

    def test_quotation_discount_cannot_exceed_the_subtotal(self):
        lines = [compute_line(LineInput(quantity_packs=D("10"), case_pack=50,
                                        price_per_pack=D("10.00")))]
        totals = compute_totals(lines, quotation_discount_pct=D("200"))
        assert totals.quotation_discount == D("100.00")
        assert totals.grand_total == D("0.00")


# --------------------------------------------------------------------------- #
# Margin
# --------------------------------------------------------------------------- #

class TestMargin:
    def test_gross_profit_and_margin(self):
        assert gross_profit(D("1000"), D("700")) == D("300.00")
        assert safe_margin_pct(D("300"), D("1000")) == D("30.0000")

    def test_markup(self):
        assert safe_markup_pct(D("300"), D("700")) == D("42.8571")

    def test_zero_denominators_return_none_not_infinity(self):
        assert safe_margin_pct(D("100"), D("0")) is None
        assert safe_markup_pct(D("100"), D("0")) is None

    def test_line_margin_flows_into_totals(self):
        lines = [compute_line(LineInput(quantity_packs=D("100"), case_pack=50,
                                        price_per_pack=D("10.00"),
                                        unit_cost_per_pack=D("6.00")))]
        totals = compute_totals(lines)
        assert totals.total_cost == D("600.00")
        assert totals.gross_profit == D("400.00")
        assert totals.gross_margin_pct == D("40.0000")

    def test_margin_is_absent_when_no_cost_is_entered(self):
        lines = [compute_line(LineInput(quantity_packs=D("100"), case_pack=50,
                                        price_per_pack=D("10.00")))]
        totals = compute_totals(lines)
        assert totals.total_cost is None
        assert totals.gross_margin_pct is None


# --------------------------------------------------------------------------- #
# Plate charges & currency
# --------------------------------------------------------------------------- #

class TestPlateCharge:
    def test_sizes_times_colours_times_designs(self):
        assert plate_charge(PlateChargeInput(3, 4, 1, D("200"))) == D("2400.00")

    def test_multiple_designs(self):
        assert plate_charge(PlateChargeInput(2, 3, 2, D("200"))) == D("2400.00")

    def test_existing_plate_is_free(self):
        charge = plate_charge(
            PlateChargeInput(3, 4, 1, D("200"), existing_plate_available=True)
        )
        assert charge == D("0.00")

    def test_rate_is_configurable_not_hardcoded(self):
        assert plate_charge(PlateChargeInput(1, 1, 1, D("275.50"))) == D("275.50")

    def test_zero_colours(self):
        assert plate_charge(PlateChargeInput(3, 0, 1, D("200"))) == D("0.00")


def test_currency_conversion():
    assert convert(D("100.00"), D("1.3567")) == D("135.67")
