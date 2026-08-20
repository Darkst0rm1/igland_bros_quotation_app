"""Formatting helpers and defensive wrappers.

Display formatting only — no value here is ever fed back into a calculation.
Money is computed in :mod:`modules.calculation_engine` and formatted here.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Callable

import pandas as pd

# --------------------------------------------------------------------------- #
# Money & number formatting
# --------------------------------------------------------------------------- #

_CURRENCY_SYMBOLS = {
    "USD": "$", "CAD": "$", "EUR": "€", "GBP": "£", "TRY": "₺",
}

#: Display format for every editable numeric field. Purely presentational —
#: ``st.number_input`` formats what it shows and hands back the same float
#: either way, so nothing stored or calculated passes through this.
#:
#: ``%g`` drops trailing zeros, which is the whole point: a discount of zero
#: reads "0" rather than "0.00", a price of 7.85 keeps two places, and 7.8565
#: keeps four. A fixed "%.4f" cannot do that — it pads every value to the
#: longest one any value might need.
#:
#: The precision is 10, not the default 6, because %g falls back to scientific
#: notation once a number exceeds its significant digits: plain "%g" renders
#: 1234567 as "1.23457e+06", which is indefensible in a quantity field. Ten
#: digits covers every quantity and price this application deals in while still
#: dropping the zeros.
NUMBER_FORMAT = "%.10g"


def format_money(
    value: Decimal | None, currency: str = "USD", *, decimals: int = 2, blank: str = "-"
) -> str:
    """Totals and line money: 2 dp with thousands separators."""
    if value is None:
        return blank
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), "")
    return f"{symbol}{value:,.{decimals}f}"


def escape_markdown(text: str) -> str:
    """Protect a string containing money from Streamlit's markdown renderer.

    ``st.caption``, ``st.markdown`` and the alert boxes render LaTeX, and a
    dollar sign opens a maths span. Two of them on one line — which is every
    price comparison this application draws — makes the text between them
    disappear into italic maths and swallows both signs::

        Available: Standard $5.98 · Three Containers $5.80 · Eight Containers $5.62
        Available: Standard 5.98 Three Containers 5.80 · Eight Containers $5.62
                                 ^^^^^^^^^^^^^^^^ rendered as maths

    The prices are still legible, which is what makes this dangerous: it reads
    as a formatting quirk rather than as two mangled figures, and the tier a
    salesperson picks from it is the price a customer is quoted.

    Applied to whole composed strings at the point of display, never inside
    :func:`format_money` — the same formatter feeds PDFs, Word documents,
    dataframes and Excel exports, where a backslash would print literally.
    """
    return text.replace("$", r"\$")


def format_pack_price(value: Decimal | None, currency: str = "USD") -> str:
    """Pack prices: 4 dp.

    The reference workbook quotes packs at 2 dp, but those are rounded displays
    of a finer underlying value, so showing 4 dp here is showing what is stored
    rather than adding false precision.
    """
    return format_money(value, currency, decimals=4)


def format_piece_price(value: Decimal | None, currency: str = "USD") -> str:
    """Piece prices: 4 dp, matching the source workbook's own precision."""
    return format_money(value, currency, decimals=4)


def format_percent(value: Decimal | None, decimals: int = 2, blank: str = "-") -> str:
    return blank if value is None else f"{value:.{decimals}f}%"


def format_quantity(value: Decimal | None, blank: str = "-") -> str:
    """Drop trailing zeros: 100.000 reads as 100, 12.500 as 12.5.

    ``Decimal.normalize()`` turns 1000.000 into ``1E+3``, and formatting that
    yields the literal string "1E+3" rather than "1,000" — which would reach a
    customer document. Integral values are therefore converted through ``int``
    rather than being formatted from the normalised Decimal.
    """
    if value is None:
        return blank
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        return f"{int(normalised):,}"
    # ``:f`` avoids scientific notation; the comma group is added separately
    # because Decimal does not support ",f" together in every Python version.
    text = f"{normalised:f}"
    whole, _, fraction = text.partition(".")
    grouped = f"{int(whole):,}"
    return f"{grouped}.{fraction}" if fraction else grouped


def format_date(value: dt.date | dt.datetime | None, blank: str = "-") -> str:
    if value is None:
        return blank
    return value.strftime("%d %b %Y")


def format_datetime(value: dt.datetime | None, blank: str = "-") -> str:
    if value is None:
        return blank
    return value.strftime("%d %b %Y %H:%M")


def days_until(target: dt.date | None, today: dt.date | None = None) -> int | None:
    if target is None:
        return None
    return (target - (today or dt.date.today())).days


# --------------------------------------------------------------------------- #
# pandas 3.0 guards
# --------------------------------------------------------------------------- #
#
# pandas 3.0 changed how empty frames behave, and empty frames are routine here:
# an import preview with every row filtered out, a report for a month with no
# quotations, a customer with no history. Both helpers below exist because the
# unguarded call raises or returns the wrong shape only when the data is empty,
# which is exactly the case that never gets exercised during development.

def safe_map(series: pd.Series, func: Callable[[Any], Any], dtype: Any = "object") -> pd.Series:
    """``Series.map`` that survives an empty series.

    On an empty datetime series pandas 3.0 attempts a float64 cast and raises.
    """
    if series.empty:
        return pd.Series([], dtype=dtype, index=series.index)
    return series.map(func)


def safe_apply_rows(
    frame: pd.DataFrame, func: Callable[[Any], Any], dtype: Any = "object"
) -> pd.Series:
    """``DataFrame.apply(axis=1)`` that always returns a Series.

    On an empty frame pandas 3.0 returns an empty *DataFrame*, so any caller
    that assigns the result to a column gets a confusing shape error instead of
    an empty column.
    """
    if frame.empty:
        return pd.Series([], dtype=dtype, index=frame.index)
    return frame.apply(func, axis=1)


def empty_frame(columns: list[str]) -> pd.DataFrame:
    """A correctly-shaped empty frame, so tables still render their headers."""
    return pd.DataFrame({c: pd.Series([], dtype="object") for c in columns})


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #

def truncate(text: str | None, length: int = 60, suffix: str = "…") -> str:
    if not text:
        return ""
    return text if len(text) <= length else text[: length - len(suffix)] + suffix


def compose_spec_text(
    material: str | None = None,
    num_colours: int | None = None,
    is_perforated: bool | None = None,
    lock_style: str | None = None,
    board_quality: str | None = None,
) -> str:
    """Build the PDF's spec column from a variant's attributes.

    Mirrors the shape of the reference PDF's single dense spec string
    ("White/Kraft 3-4C, Perforated / No-Lock"). Any variant may override the
    result with ``spec_text_override``.
    """
    parts: list[str] = []
    if material:
        parts.append(material)
    if num_colours:
        parts.append(f"{num_colours}C")
    if board_quality:
        parts.append(board_quality)

    tail: list[str] = []
    if is_perforated is not None:
        tail.append("Perforated" if is_perforated else "Non-perforated")
    if lock_style:
        tail.append(lock_style)

    head = ", ".join(parts)
    if tail:
        return f"{head}, {' / '.join(tail)}" if head else " / ".join(tail)
    return head
