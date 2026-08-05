"""Item-code generation for the catalogue.

A code is derived from the attributes that actually distinguish one item from
another, so the same product always produces the same code no matter who adds
it or whether it arrives by hand or through a price-list import.

The scheme is::

    WB-07              product   category · size
    WB-07-115-50       variant   category · size · board grade · pack

``WB`` is the category, ``07`` the box size in inches zero-padded so ten sorts
after nine, ``115`` the HPFL figure that is the only thing separating one
board quality from another, and ``50`` the pack.

Constants are carried anyway. Every box in the catalogue today is B flute in
packs of fifty, so neither is currently a distinguishing feature — but a code
is printed on documents and quoted back by customers for years, and one that
has to be reinterpreted the day a 25-pack appears is worse than one that was
slightly redundant from the start.

Codes are *generated*, not enforced. ``catalogue_service`` still accepts
whatever item number a user types: a customer-specific line or a one-off
special will not fit any scheme, and refusing to store it would send that work
into a spreadsheet where nothing else can see it.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# The lengths the columns allow; a generated code is never the reason an
# insert fails.
MAX_PRODUCT_CODE = 60
MAX_VARIANT_CODE = 80

#: Category to prefix. Anything not listed falls back to the initials of the
#: category's words, so an unplanned category still yields a sane code rather
#: than an exception.
CATEGORY_PREFIXES: dict[str, str] = {
    "White Boxes": "WB",
    "Kraft Boxes": "KB",
    "Printed Boxes": "PB",
}

_FALLBACK_PREFIX = "IT"


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #

def category_prefix(category: str | None) -> str:
    """Two-or-three letter prefix for a category."""
    if not category:
        return _FALLBACK_PREFIX
    known = CATEGORY_PREFIXES.get(category.strip())
    if known:
        return known
    initials = "".join(word[0] for word in re.findall(r"[A-Za-z]+", category))
    return (initials[:3] or _FALLBACK_PREFIX).upper()


def size_token(size_label: str | None) -> str:
    """The box size, zero-padded to two digits.

    ``'7" White'`` becomes ``07`` and ``'10" White'`` becomes ``10``, so a
    plain alphabetical sort puts them in size order — the current codes do
    not, and a catalogue listing 10, 11, 12, 7, 8, 9 reads as though sizes are
    missing.

    A fractional size keeps its point (``9.5`` becomes ``09.5``), which still
    sorts correctly against the whole numbers either side of it.
    """
    if not size_label:
        return "00"
    match = re.search(r"\d+(?:\.\d+)?", size_label)
    if match is None:
        return "00"
    try:
        value = Decimal(match.group())
    except InvalidOperation:  # pragma: no cover - the regex precludes it
        return "00"
    if value == value.to_integral_value():
        return f"{int(value):02d}"
    # Trim the trailing zeros a decimal like 9.50 would otherwise keep.
    trimmed = str(value.normalize())
    whole, _, fraction = trimmed.partition(".")
    return f"{int(whole):02d}.{fraction}"


def board_token(board_quality: str | None) -> str:
    """The part of a board quality that distinguishes it from the others.

    ``WT110 HPFL115 KM135`` and ``WT110 HPFL135 KM135`` differ only in the
    HPFL figure, so that figure alone identifies the board. A quality written
    some other way is slugified whole rather than guessed at.
    """
    if not board_quality:
        return "STD"
    match = re.search(r"HPFL\s*(\d+)", board_quality, flags=re.IGNORECASE)
    if match is not None:
        return match.group(1)
    slug = re.sub(r"[^A-Za-z0-9]+", "", board_quality).upper()
    return slug[:12] or "STD"


def pack_token(case_pack: int | None) -> str:
    return str(case_pack) if case_pack else "0"


# --------------------------------------------------------------------------- #
# Codes
# --------------------------------------------------------------------------- #

def product_code(category: str | None, size_label: str | None) -> str:
    """``WB-07`` — the code for a product."""
    return f"{category_prefix(category)}-{size_token(size_label)}"[:MAX_PRODUCT_CODE]


def variant_code(
    product_item_number: str,
    board_quality: str | None,
    case_pack: int | None,
) -> str:
    """``WB-07-115-50`` — the code for a variant, built onto its product's.

    Building onto the stored product code rather than regenerating it means a
    product that had to take a disambiguated code keeps its variants grouped
    beneath it.
    """
    parts = [product_item_number, board_token(board_quality), pack_token(case_pack)]
    return "-".join(parts)[:MAX_VARIANT_CODE]


# --------------------------------------------------------------------------- #
# Uniqueness
# --------------------------------------------------------------------------- #

def disambiguate(base: str, taken: set[str], max_length: int) -> str:
    """``base``, or ``base-2``, ``base-3`` … until it is not in ``taken``.

    Two products can legitimately generate the same code — the same size in
    the same category, differing in something the code does not carry, such as
    print. The suffix keeps them apart without the caller having to invent a
    scheme on the spot.

    The suffix is appended within ``max_length``, trimming the base if it
    would otherwise overflow the column.
    """
    if base not in taken:
        return base[:max_length]
    suffix = 2
    while True:
        tail = f"-{suffix}"
        candidate = f"{base[:max_length - len(tail)]}{tail}"
        if candidate not in taken:
            return candidate
        suffix += 1
