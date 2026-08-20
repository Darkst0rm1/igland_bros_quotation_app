"""Editable numeric fields show what was entered, not what the widget padded.

A fixed "%.4f" pads every value to the widest one any value might need, so a
discount of zero read "0.0000" and a quantity of one thousand read "1000.0000".
Employees then delete the zeros by hand, or worse, mistrust the figure.

The change is presentational and only that. ``st.number_input`` hands back the
same float whichever way it renders, so nothing here can reach a stored value,
a total, an approval threshold or a PDF. The last class in this file is the one
that proves it.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from modules.utilities import NUMBER_FORMAT, format_money, format_quantity


def shown(value: float) -> str:
    """What the widget renders for this value."""
    return NUMBER_FORMAT % value


class TestTrailingZerosAreDropped:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, "0"),
            (0.00, "0"),
            (1.0, "1"),
            (1.0000, "1"),
            (30.0, "30"),
            (1000.0, "1000"),
            (72000.0, "72000"),
        ],
    )
    def test_whole_numbers_lose_their_decimals(self, value, expected):
        assert shown(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (7.85, "7.85"),
            (7.8500, "7.85"),
            (11.99, "11.99"),
            (11.9900, "11.99"),
            (0.5, "0.5"),
            (12.5, "12.5"),
        ],
    )
    def test_two_decimal_values_keep_two(self, value, expected):
        assert shown(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (7.8565, "7.8565"),   # four meaningful places
            (7.856, "7.856"),     # three
            (0.125, "0.125"),
            (5.9390, "5.939"),    # the trailing zero is not meaningful
            (0.0001, "0.0001"),
        ],
    )
    def test_meaningful_decimals_survive(self, value, expected):
        assert shown(value) == expected


class TestNoScientificNotation:
    """Why the format is %.10g and not plain %g.

    %g switches to exponent form once a value exceeds its significant digits,
    and the default is six — so a quantity of 1,234,567 would render as
    "1.23457e+06" in an editable field.
    """

    @pytest.mark.parametrize(
        "value", [123456.0, 1234567.0, 12345678.0, 999999999.0],
    )
    def test_large_quantities_stay_readable(self, value):
        assert "e" not in shown(value).lower()
        assert shown(value) == str(int(value))

    def test_plain_percent_g_would_not_have_done(self):
        """Pinned so the precision is not "simplified" back to %g later."""
        assert "%g" % 1234567.0 == "1.23457e+06"
        assert NUMBER_FORMAT % 1234567.0 == "1234567"


class TestStreamlitAccepts:
    def test_the_format_passes_streamlit_validation(self):
        """number_input validates a format by evaluating ``float(fmt % 2)``.
        A format it rejects raises at render time, on the page, for everyone."""
        assert float(NUMBER_FORMAT % 2) == 2.0


class TestDisplayOnly:
    """Nothing above may touch a value that is stored or calculated."""

    @pytest.mark.parametrize(
        "value", [0.0, 7.85, 7.8565, 1234567.0, 11.99],
    )
    def test_the_round_trip_is_lossless_to_four_places(self, value):
        """The widget returns the float it was given; the format only decides
        how it is drawn. Four places is the precision the schema stores."""
        assert round(float(shown(value)), 4) == round(value, 4)

    def test_money_formatting_is_untouched(self):
        """Totals and documents keep their two-decimal money presentation —
        this change is about editable fields, not printed money."""
        assert format_money(Decimal("0"), "USD") == "$0.00"
        assert format_money(Decimal("8552.23"), "USD") == "$8,552.23"

    def test_quantity_formatting_is_untouched(self):
        """format_quantity already dropped trailing zeros for read-only text
        and keeps its thousands separators, which %g deliberately does not."""
        assert format_quantity(Decimal("1000.000")) == "1,000"
        assert format_quantity(Decimal("12.500")) == "12.5"
