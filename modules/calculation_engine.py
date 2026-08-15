"""Financial calculations. Pure functions, exact decimals, no I/O.

This module imports nothing from the project except :mod:`modules.constants`.
No ORM, no Streamlit, no database — which is what makes every rule here
testable in isolation, and why the pages are forbidden from doing money maths
themselves.

Two rules govern everything below.

**Rounding is ROUND_HALF_UP.** Python's default is ``ROUND_HALF_EVEN``
(banker's rounding), which turns 0.125 into 0.12 and 0.135 into 0.14. That is
correct for statistics and wrong for invoicing, where 0.125 must always become
0.13. Every quantize call here passes the rounding mode explicitly rather than
relying on context, so importing this module cannot change behaviour elsewhere
and nothing else can change behaviour here.

**Rounding happens at defined points only.** Unit prices carry 6 dp. Every line
and quotation money value is quantized to 2 dp *as it is produced*, so all the
addends in every total are already exact at 2 dp and no total ever needs
re-rounding. This is what makes the printed PDF columns foot: the sum of the
displayed line totals is the displayed subtotal, always.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from modules.constants import PricingBasis

# --------------------------------------------------------------------------- #
# Quantization
# --------------------------------------------------------------------------- #

ZERO = Decimal("0")
HUNDRED = Decimal("100")

EXP_MONEY = Decimal("0.01")        # line and quotation money
EXP_QUANTITY = Decimal("0.001")    # packs, pieces, containers
EXP_PERCENT = Decimal("0.0001")    # percentages
EXP_UNIT_PRICE = Decimal("0.000001")  # unit prices and unit costs


def q_money(value: Decimal) -> Decimal:
    """Quantize to 2 dp, half-up. Every stored money value passes through here."""
    return value.quantize(EXP_MONEY, rounding=ROUND_HALF_UP)


def q_quantity(value: Decimal) -> Decimal:
    return value.quantize(EXP_QUANTITY, rounding=ROUND_HALF_UP)


def q_percent(value: Decimal) -> Decimal:
    return value.quantize(EXP_PERCENT, rounding=ROUND_HALF_UP)


def q_unit_price(value: Decimal) -> Decimal:
    return value.quantize(EXP_UNIT_PRICE, rounding=ROUND_HALF_UP)


def to_decimal(value: object) -> Decimal:
    """Coerce to Decimal without ever going through binary floating point.

    A float argument is converted via ``str`` deliberately: ``Decimal(0.1)`` is
    0.1000000000000000055511151231257827, whereas ``Decimal(str(0.1))`` is
    exactly ``0.1``. Money should never arrive here as a float, but if it does,
    this is the less wrong of the two behaviours.
    """
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    return Decimal(str(value))


# --------------------------------------------------------------------------- #
# Quantity conversion
# --------------------------------------------------------------------------- #

def pieces_from_packs(quantity_packs: Decimal, case_pack: int) -> Decimal:
    """``Quantity in Pieces = Quantity in Packs x Case Pack``."""
    return q_quantity(to_decimal(quantity_packs) * Decimal(case_pack))


def packs_from_pieces(quantity_pieces: Decimal, case_pack: int) -> Decimal:
    """Inverse conversion. Returns a fractional pack count where it does not divide evenly."""
    if case_pack <= 0:
        raise ValueError("case_pack must be positive")
    return q_quantity(to_decimal(quantity_pieces) / Decimal(case_pack))


def price_per_piece_from_pack(price_per_pack: Decimal, case_pack: int) -> Decimal:
    """``Price per Piece = Price per Pack / Case Pack``.

    Used to *derive* a piece price when no imported one exists. It is not used
    to validate or correct an imported piece price: in the reference workbook
    the two columns legitimately disagree by up to one rounding unit on 25 of
    69 price pairs, because both are rounded displays of an unexposed precision
    (docs/PHASE1_REFERENCE_ANALYSIS.md §1.2).
    """
    if case_pack <= 0:
        raise ValueError("case_pack must be positive")
    return q_unit_price(to_decimal(price_per_pack) / Decimal(case_pack))


def piece_pack_mismatch(
    price_per_pack: Decimal,
    price_per_piece: Decimal,
    case_pack: int,
    tolerance: Decimal = Decimal("0.0001"),
) -> Decimal | None:
    """Return the signed discrepancy if it exceeds ``tolerance``, else ``None``.

    The default tolerance is one rounding unit at the workbook's 4 dp piece
    precision. A zero tolerance would flag more than a third of the seeded
    catalogue and be ignored by users within a week.
    """
    if case_pack <= 0:
        return None
    derived = to_decimal(price_per_pack) / Decimal(case_pack)
    delta = derived - to_decimal(price_per_piece)
    return delta if abs(delta) > tolerance else None


# --------------------------------------------------------------------------- #
# Line calculations
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LineInput:
    """Everything needed to price one quotation line."""

    quantity_packs: Decimal = ZERO
    quantity_pieces: Decimal | None = None   # derived from packs when omitted
    case_pack: int = 1
    price_per_pack: Decimal = ZERO
    price_per_piece: Decimal | None = None   # derived from pack price when omitted
    pricing_basis: PricingBasis = PricingBasis.PACK
    line_discount_pct: Decimal = ZERO
    #: An explicit discount amount overrides the percentage when not None.
    line_discount_amount: Decimal | None = None
    unit_cost_per_pack: Decimal | None = None
    #: Standard-tier pack price, for the savings calculation. None when the
    #: variant has no standard price on the quote date.
    standard_price_per_pack: Decimal | None = None


@dataclass(frozen=True)
class LineResult:
    quantity_packs: Decimal
    quantity_pieces: Decimal
    price_per_pack: Decimal
    price_per_piece: Decimal
    gross_line_total: Decimal
    line_discount_amount: Decimal
    net_line_total: Decimal
    line_cost_total: Decimal | None
    gross_profit: Decimal | None
    gross_margin_pct: Decimal | None
    savings_per_pack: Decimal | None
    total_savings: Decimal | None


def compute_line(line: LineInput) -> LineResult:
    """Price a single line.

    Quantization points, in order:

    1. ``quantity_pieces``  -> 3 dp
    2. ``gross_line_total`` -> 2 dp
    3. ``line_discount``    -> 2 dp
    4. ``net_line_total``   = gross - discount (already exact at 2 dp)

    Which of the two price columns drives step 2 is decided by
    ``pricing_basis`` and never inferred, because pack-based and piece-based
    arithmetic give different answers on this catalogue.
    """
    case_pack = int(line.case_pack) if line.case_pack else 1
    packs = q_quantity(to_decimal(line.quantity_packs))

    pieces = (
        q_quantity(to_decimal(line.quantity_pieces))
        if line.quantity_pieces is not None
        else pieces_from_packs(packs, case_pack)
    )

    price_pack = q_unit_price(to_decimal(line.price_per_pack))
    price_piece = (
        q_unit_price(to_decimal(line.price_per_piece))
        if line.price_per_piece is not None
        else price_per_piece_from_pack(price_pack, case_pack)
    )

    if line.pricing_basis is PricingBasis.PIECE:
        gross = q_money(pieces * price_piece)
    else:
        gross = q_money(packs * price_pack)

    if line.line_discount_amount is not None:
        discount = q_money(to_decimal(line.line_discount_amount))
    else:
        discount = q_money(gross * to_decimal(line.line_discount_pct) / HUNDRED)

    # A discount can reduce a line to zero but never invert it.
    discount = min(discount, gross) if gross >= ZERO else discount
    net = gross - discount

    cost_total: Decimal | None = None
    profit: Decimal | None = None
    margin: Decimal | None = None
    if line.unit_cost_per_pack is not None:
        cost_total = q_money(packs * to_decimal(line.unit_cost_per_pack))
        profit = net - cost_total
        margin = safe_margin_pct(profit, net)

    savings_per_pack: Decimal | None = None
    total_savings: Decimal | None = None
    if line.standard_price_per_pack is not None:
        savings_per_pack = q_unit_price(
            to_decimal(line.standard_price_per_pack) - price_pack
        )
        total_savings = q_money(savings_per_pack * packs)

    return LineResult(
        quantity_packs=packs,
        quantity_pieces=pieces,
        price_per_pack=price_pack,
        price_per_piece=price_piece,
        gross_line_total=gross,
        line_discount_amount=discount,
        net_line_total=net,
        line_cost_total=cost_total,
        gross_profit=profit,
        gross_margin_pct=margin,
        savings_per_pack=savings_per_pack,
        total_savings=total_savings,
    )


# --------------------------------------------------------------------------- #
# Charges
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ChargeInput:
    quantity: Decimal = Decimal("1")
    rate: Decimal = ZERO
    #: Rate multiplier when the charge is entered in a currency other than the
    #: quotation's. 1 means "same currency".
    exchange_rate: Decimal = Decimal("1")
    is_taxable: bool = True
    is_customer_visible: bool = True


def charge_amount(charge: ChargeInput) -> Decimal:
    """``quantity x rate x exchange_rate``, quantized to 2 dp."""
    return q_money(
        to_decimal(charge.quantity)
        * to_decimal(charge.rate)
        * to_decimal(charge.exchange_rate)
    )


# --------------------------------------------------------------------------- #
# Quotation totals
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class QuotationTotals:
    subtotal: Decimal
    quotation_discount: Decimal
    charges_total: Decimal
    charges_customer_visible: Decimal
    taxable_base: Decimal
    tax_amount: Decimal
    grand_total: Decimal
    total_cost: Decimal | None
    gross_profit: Decimal | None
    gross_margin_pct: Decimal | None
    total_savings: Decimal | None
    line_results: list[LineResult] = field(default_factory=list)


def compute_totals(
    lines: list[LineResult],
    charges: list[ChargeInput] | None = None,
    quotation_discount_pct: Decimal = ZERO,
    quotation_discount_amount: Decimal | None = None,
    tax_rate_pct: Decimal = ZERO,
) -> QuotationTotals:
    """Roll lines and charges up to a grand total.

    ::

        subtotal    = sum of net line totals            (exact — addends are 2 dp)
        quote_disc  = subtotal x pct / 100              -> 2 dp
        charges     = sum of charge amounts             (exact)
        tax_base    = (subtotal - quote_disc) + taxable charges
        tax         = tax_base x rate / 100             -> 2 dp
        grand_total = subtotal - quote_disc + charges + tax

    Non-taxable charges are added after tax is computed, which is the point of
    marking them non-taxable. Internal-only charges still count toward the
    grand total — they are costs the company incurs, hidden from the customer
    PDF but not from the quotation's own arithmetic.
    """
    charges = charges or []

    subtotal = sum((ln.net_line_total for ln in lines), ZERO)

    if quotation_discount_amount is not None:
        quote_discount = q_money(to_decimal(quotation_discount_amount))
    else:
        quote_discount = q_money(subtotal * to_decimal(quotation_discount_pct) / HUNDRED)
    quote_discount = min(quote_discount, subtotal) if subtotal >= ZERO else quote_discount

    amounts = [(c, charge_amount(c)) for c in charges]
    charges_total = sum((amt for _, amt in amounts), ZERO)
    charges_visible = sum(
        (amt for c, amt in amounts if c.is_customer_visible), ZERO
    )
    taxable_charges = sum((amt for c, amt in amounts if c.is_taxable), ZERO)

    taxable_base = (subtotal - quote_discount) + taxable_charges
    tax = q_money(taxable_base * to_decimal(tax_rate_pct) / HUNDRED)

    grand_total = subtotal - quote_discount + charges_total + tax

    costs = [ln.line_cost_total for ln in lines if ln.line_cost_total is not None]
    total_cost: Decimal | None = sum(costs, ZERO) if costs else None
    profit: Decimal | None = None
    margin: Decimal | None = None
    if total_cost is not None:
        net_selling = subtotal - quote_discount
        profit = net_selling - total_cost
        margin = safe_margin_pct(profit, net_selling)

    savings = [ln.total_savings for ln in lines if ln.total_savings is not None]
    total_savings: Decimal | None = sum(savings, ZERO) if savings else None

    return QuotationTotals(
        subtotal=subtotal,
        quotation_discount=quote_discount,
        charges_total=charges_total,
        charges_customer_visible=charges_visible,
        taxable_base=taxable_base,
        tax_amount=tax,
        grand_total=grand_total,
        total_cost=total_cost,
        gross_profit=profit,
        gross_margin_pct=margin,
        total_savings=total_savings,
        line_results=list(lines),
    )


# --------------------------------------------------------------------------- #
# Deposit
# --------------------------------------------------------------------------- #

def deposit_amount(grand_total: Decimal, deposit_pct: Decimal | None) -> Decimal:
    """``Grand Total x deposit rate``, to 2 dp. Balance is the remainder.

    The deposit is stored as a rate and the money derived, so it cannot drift
    out of step when the total moves — which it does whenever freight changes.
    One implementation, because three surfaces state this figure (the internal
    document, the customer portal and its PDF) and three copies of a formula
    that references the grand total is three chances for one of them to miss a
    charge.
    """
    if not deposit_pct:
        return Decimal("0.00")
    return q_money(to_decimal(grand_total) * to_decimal(deposit_pct) / HUNDRED)


# --------------------------------------------------------------------------- #
# Margin
# --------------------------------------------------------------------------- #

def safe_margin_pct(gross_profit: Decimal, net_selling_price: Decimal) -> Decimal | None:
    """``Gross Profit / Net Selling Price x 100``.

    Returns ``None`` — not zero, not infinity, not an exception — when the
    denominator is zero. A margin on a zero-value line is undefined, and
    reporting it as 0% would be a lie that survives into management reports.
    """
    net = to_decimal(net_selling_price)
    if net == ZERO:
        return None
    return q_percent(to_decimal(gross_profit) / net * HUNDRED)


def safe_markup_pct(gross_profit: Decimal, total_cost: Decimal) -> Decimal | None:
    """``Gross Profit / Total Cost x 100``. ``None`` when cost is zero."""
    cost = to_decimal(total_cost)
    if cost == ZERO:
        return None
    return q_percent(to_decimal(gross_profit) / cost * HUNDRED)


def gross_profit(net_selling_price: Decimal, total_cost: Decimal) -> Decimal:
    return q_money(to_decimal(net_selling_price) - to_decimal(total_cost))


# --------------------------------------------------------------------------- #
# Printing plates
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PlateChargeInput:
    number_of_sizes: int = 0
    number_of_colours: int = 0
    number_of_designs: int = 1
    plate_cost_per_colour: Decimal = Decimal("200")
    #: When a reusable plate already exists there is nothing to charge.
    existing_plate_available: bool = False


def plate_charge(spec: PlateChargeInput) -> Decimal:
    """``sizes x colours x designs x rate``, or zero when a plate already exists.

    The rate defaults to the reference workbook's "Printing plate charge is 200
    USD per size per color", but the operative value is always
    ``company_settings.printing_plate_rate`` — nothing here is hardcoded into
    the application's behaviour.
    """
    if spec.existing_plate_available:
        return q_money(ZERO)
    return q_money(
        Decimal(spec.number_of_sizes)
        * Decimal(spec.number_of_colours)
        * Decimal(spec.number_of_designs)
        * to_decimal(spec.plate_cost_per_colour)
    )


# --------------------------------------------------------------------------- #
# Currency
# --------------------------------------------------------------------------- #

def convert(amount: Decimal, exchange_rate: Decimal) -> Decimal:
    """Convert a money amount into the quotation currency.

    Applied only to charges entered in another currency. Prices themselves are
    never auto-converted — a price record in a different currency is simply not
    offered for that quotation, because silently converting a negotiated price
    would misrepresent what was agreed.
    """
    return q_money(to_decimal(amount) * to_decimal(exchange_rate))
