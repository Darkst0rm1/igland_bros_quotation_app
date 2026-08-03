"""Pydantic schemas.

Validation only — no I/O, no ORM, no Streamlit. These types are the boundary
between untrusted input (a spreadsheet cell, a form field) and the rest of the
application, and they are where a value stops being a string and becomes a
``Decimal``.

Money and quantity fields coerce through ``str`` deliberately: ``Decimal(0.1)``
is 0.1000000000000000055511151231257827 while ``Decimal("0.1")`` is exactly
0.1, and a price that arrives from openpyxl as a float must not carry binary
error into the database.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modules.constants import AddressType, CustomerStatus, SUPPORTED_CURRENCIES


# --------------------------------------------------------------------------- #
# Shared coercion
# --------------------------------------------------------------------------- #

def coerce_decimal(value: Any) -> Decimal | None:
    """Turn a spreadsheet cell or form value into an exact Decimal.

    Accepts int, float, Decimal and str. Strips currency symbols, thousands
    separators and whitespace, because price cells are frequently text like
    ``"$ 3.79"`` or ``"3,79"`` once a workbook has been through a few hands.
    Returns ``None`` for blanks rather than raising, so a missing optional
    price is distinguishable from a malformed one.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; never a price
        raise ValueError("expected a number, got a boolean")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,\-]", "", text)
    if not cleaned:
        raise ValueError(f"{value!r} is not a number")

    # "1,234.56" -> "1234.56";  "3,79" (comma decimal) -> "3.79"
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") == 1 and len(cleaned.split(",")[-1]) in (1, 2, 3, 4):
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")

    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"{value!r} is not a number") from exc


def clean_text(value: Any) -> str | None:
    """Collapse whitespace (including the newlines openpyxl returns) to a single
    space, and turn an empty result into ``None``."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


Money = Annotated[Decimal, Field(max_digits=18, decimal_places=6)]


# --------------------------------------------------------------------------- #
# Price-list import
# --------------------------------------------------------------------------- #

class PriceRowInput(BaseModel):
    """One data row of a price-list workbook, after header normalisation.

    Both price columns for each tier are carried verbatim. Neither is derived
    from the other and neither is corrected to match: in the reference workbook
    they legitimately disagree by up to one rounding unit on 25 of 69 pairs
    (docs/PHASE1_REFERENCE_ANALYSIS.md §1.2).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    source_row_no: int
    section_label: str | None = None

    product: str
    depth: str | None = None
    flute: str | None = None
    case_pack: int
    board_quality: str

    standard_price_per_pack: Money | None = None
    standard_price_per_piece: Money | None = None
    three_container_price_per_pack: Money | None = None
    three_container_price_per_piece: Money | None = None
    eight_container_price_per_pack: Money | None = None
    eight_container_price_per_piece: Money | None = None

    @field_validator("product", "board_quality", "depth", "flute", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return clean_text(value)

    @field_validator("case_pack", mode="before")
    @classmethod
    def _parse_case_pack(cls, value: Any) -> Any:
        parsed = coerce_decimal(value)
        if parsed is None:
            raise ValueError("case pack is required")
        if parsed != parsed.to_integral_value() or parsed <= 0:
            raise ValueError(f"case pack must be a positive whole number, got {value!r}")
        return int(parsed)

    @field_validator(
        "standard_price_per_pack", "standard_price_per_piece",
        "three_container_price_per_pack", "three_container_price_per_piece",
        "eight_container_price_per_pack", "eight_container_price_per_piece",
        mode="before",
    )
    @classmethod
    def _parse_price(cls, value: Any) -> Any:
        parsed = coerce_decimal(value)
        if parsed is not None and parsed <= 0:
            raise ValueError(f"price must be greater than zero, got {value!r}")
        return parsed

    @model_validator(mode="after")
    def _require_at_least_one_price(self) -> PriceRowInput:
        if not any(
            getattr(self, name) is not None
            for name in self.__class__.model_fields
            if name.endswith(("_per_pack", "_per_piece"))
        ):
            raise ValueError("the row has no prices in any tier")
        return self

    @property
    def natural_key(self) -> tuple[str, str | None, str | None, int, str]:
        """``(size, depth, flute, case pack, board quality)``.

        Board quality is part of the key and is always read from the row's own
        Quality column — never inferred from the section heading, because the
        reference workbook's "alternative quality" block contains two different
        qualities.
        """
        return (
            self.product.casefold(),
            (self.depth or "").casefold() or None,
            (self.flute or "").casefold() or None,
            self.case_pack,
            self.board_quality.casefold(),
        )

    def tier_prices(self) -> dict[str, tuple[Decimal | None, Decimal | None]]:
        """``{tier_code: (price_per_pack, price_per_piece)}`` for populated tiers."""
        pairs = {
            "STANDARD": (self.standard_price_per_pack, self.standard_price_per_piece),
            "THREE_CONTAINER": (
                self.three_container_price_per_pack,
                self.three_container_price_per_piece,
            ),
            "EIGHT_CONTAINER": (
                self.eight_container_price_per_pack,
                self.eight_container_price_per_piece,
            ),
        }
        return {code: pair for code, pair in pairs.items() if any(p is not None for p in pair)}


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

class CustomerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    customer_number: str = Field(min_length=1, max_length=40)
    company_name: str = Field(min_length=1, max_length=200)
    default_currency: str = "USD"
    payment_terms: str | None = Field(default=None, max_length=200)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    assigned_sales_user_id: int | None = None
    status: CustomerStatus = CustomerStatus.PROSPECT
    notes: str | None = None

    @field_validator("default_currency")
    @classmethod
    def _known_currency(cls, value: str) -> str:
        code = value.upper()
        if code not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency {value!r}")
        return code


class ContactInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    title: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=60)
    is_primary: bool = False
    is_active: bool = True

    @field_validator("email")
    @classmethod
    def _plausible_email(cls, value: str | None) -> str | None:
        """Deliberately permissive.

        This application never sends email — quotations are sent by the employee
        from their own client — so an address only has to be good enough to copy
        and paste. Rejecting unusual but valid addresses would cost more than it
        saves.
        """
        if not value:
            return None
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("that does not look like an email address")
        return value


class AddressInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    address_type: AddressType
    label: str | None = Field(default=None, max_length=80)
    line1: str | None = Field(default=None, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    province: str | None = Field(default=None, max_length=120)
    postal_code: str | None = Field(default=None, max_length=40)
    country: str | None = Field(default=None, max_length=120)
    is_default: bool = False


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #

class ProductInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    item_number: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=200)
    size_label: str = Field(min_length=1, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    length_in: Decimal | None = None
    width_in: Decimal | None = None
    depth_in: Decimal | None = None
    flute: str | None = Field(default=None, max_length=20)
    unit_of_measure: str = "PACK"
    printing_method: str | None = Field(default=None, max_length=80)
    material: str | None = Field(default=None, max_length=120)
    finish: str | None = Field(default=None, max_length=120)
    is_perforated: bool | None = None
    lock_style: str | None = Field(default=None, max_length=80)
    notes: str | None = None
    is_active: bool = True

    @field_validator("length_in", "width_in", "depth_in", mode="before")
    @classmethod
    def _parse_dimension(cls, value: Any) -> Any:
        return coerce_decimal(value)


class VariantInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    variant_item_number: str = Field(min_length=1, max_length=80)
    board_quality: str = Field(min_length=1, max_length=120)
    case_pack: int = Field(gt=0)
    num_colours: int | None = Field(default=None, ge=0, le=12)
    moq_packs: Decimal | None = None
    moq_pieces: Decimal | None = None
    spec_text_override: str | None = None
    notes: str | None = None
    is_active: bool = True

    @field_validator("moq_packs", "moq_pieces", mode="before")
    @classmethod
    def _parse_quantity(cls, value: Any) -> Any:
        parsed = coerce_decimal(value)
        if parsed is not None and parsed < 0:
            raise ValueError("minimum order quantity cannot be negative")
        return parsed


class PriceInput(BaseModel):
    """A manually entered or imported price.

    ``effective_from`` is required rather than defaulted: a price with an
    accidental date is worse than one the operator had to think about, because
    it silently changes which price a historical quotation resolves to.
    """

    product_variant_id: int
    price_tier_code: str
    price_per_pack: Money
    price_per_piece: Money | None = None
    currency: str = "USD"
    effective_from: dt.date
    effective_to: dt.date | None = None

    @field_validator("price_per_pack", "price_per_piece", mode="before")
    @classmethod
    def _parse_price(cls, value: Any) -> Any:
        parsed = coerce_decimal(value)
        if parsed is not None and parsed <= 0:
            raise ValueError("price must be greater than zero")
        return parsed

    @field_validator("currency")
    @classmethod
    def _known_currency(cls, value: str) -> str:
        code = value.upper()
        if code not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency {value!r}")
        return code

    @model_validator(mode="after")
    def _dates_in_order(self) -> PriceInput:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("the expiry date cannot be before the effective date")
        return self


class CostInput(BaseModel):
    """Internal cost, entered manually per variant.

    Zero is permitted — a variant genuinely costed at nothing is different from
    one with no cost recorded, and the latter is represented by the absence of
    a row rather than by a zero.
    """

    product_variant_id: int
    cost_per_pack: Money
    cost_per_piece: Money | None = None
    currency: str = "USD"
    effective_from: dt.date
    source_note: str | None = None

    @field_validator("cost_per_pack", "cost_per_piece", mode="before")
    @classmethod
    def _parse_cost(cls, value: Any) -> Any:
        parsed = coerce_decimal(value)
        if parsed is not None and parsed < 0:
            raise ValueError("cost cannot be negative")
        return parsed
