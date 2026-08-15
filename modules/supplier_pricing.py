"""How a selling price is built from a supplier's factory cost.

Until now this lived in a spreadsheet. The application stored the *result* — a
price list and a cost list, imported independently — and had no idea the two
were related. That was survivable while somebody re-derived the sheet by hand,
and it is why a rounding difference in the middle of the chain went unnoticed
for as long as it did.

The build, in the order the money accumulates::

    product_cost_per_bundle = unit_cost_per_piece x pieces_per_bundle
    fob_cost_per_bundle     = total_fob_cost / bundles_per_container
    original_cost           = product_cost_per_bundle + fob_cost_per_bundle
    selling_price           = original_cost x (1 + markup_percentage)

**Markup applies to the whole original cost, freight included.** An earlier
reading of the workbook marked up only the factory cost and added freight
afterwards::

    selling = unit_cost x 1.17 x pieces_per_bundle + fob_per_bundle   # NOT THIS

The two differ by ``fob_cost_per_bundle x markup_percentage`` — five cents a
bundle on an 8 inch box, and about $120 on a full container. Freight is now
marked up like any other cost.

Nothing here is hardcoded. ``pieces_per_bundle``, ``total_fob_cost`` and
``markup_percentage`` are configuration, read through :mod:`settings_service`,
because all three move: freight moves with the market, a markup is a commercial
decision, and a bundle is whatever the supplier ships.

Markup is not margin, and the distinction costs money when it is blurred.
Markup is taken on cost, margin is earned on price: a 17% markup is a 14.53%
margin, because ``0.17 / 1.17``. ``markup_percentage`` is stored as ``0.17``
and the multiplier ``1.17`` is derived here rather than stored, so the two can
never disagree.

Every value is a ``Decimal`` and stays at full precision. Rounding happens once,
at the point of display or storage, through :func:`for_display` or the
quantizers in :mod:`calculation_engine`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from modules.calculation_engine import to_decimal

ZERO = Decimal("0")
ONE = Decimal("1")

#: Display precision for the two headline figures. Four places because the
#: third and fourth carry real money at container quantities: a bundle price
#: rounded to cents moves a 2,304-bundle container by up to $11.
DISPLAY_EXP = Decimal("0.0001")


class PricingError(ValueError):
    """An input that cannot produce a meaningful price."""


@dataclass(frozen=True)
class PriceBuild:
    """One product's price, with every intermediate kept.

    The intermediates are not decoration: a quotation that cannot show how a
    price was reached is a quotation nobody can check against the supplier's
    own sheet.
    """

    unit_cost_per_piece: Decimal
    pieces_per_bundle: Decimal
    bundles_per_container: Decimal
    total_fob_cost: Decimal
    markup_percentage: Decimal

    product_cost_per_bundle: Decimal
    fob_cost_per_bundle: Decimal
    original_cost: Decimal
    selling_price: Decimal

    @property
    def markup_multiplier(self) -> Decimal:
        """``1 + markup_percentage``. Derived, never stored."""
        return ONE + self.markup_percentage

    @property
    def gross_profit_per_bundle(self) -> Decimal:
        return self.selling_price - self.original_cost

    @property
    def margin_percentage(self) -> Decimal | None:
        """Profit as a share of the selling price. ``None`` at zero price."""
        if self.selling_price == ZERO:
            return None
        return self.gross_profit_per_bundle / self.selling_price

    @property
    def container_value(self) -> Decimal:
        """What a full container sells for."""
        return self.selling_price * self.bundles_per_container

    def for_display(self) -> dict[str, Decimal]:
        """The two figures a quotation leads with, rounded once."""
        return {
            "original_cost": self.original_cost.quantize(DISPLAY_EXP),
            "selling_price": self.selling_price.quantize(DISPLAY_EXP),
        }


def product_cost_per_bundle(
    unit_cost_per_piece: Decimal, pieces_per_bundle: Decimal
) -> Decimal:
    """``unit_cost_per_piece x pieces_per_bundle``."""
    cost = to_decimal(unit_cost_per_piece)
    pieces = to_decimal(pieces_per_bundle)
    if cost < ZERO:
        raise PricingError("Unit cost per piece cannot be negative.")
    if pieces <= ZERO:
        raise PricingError("Pieces per bundle must be greater than zero.")
    return cost * pieces


def fob_cost_per_bundle(
    total_fob_cost: Decimal, bundles_per_container: Decimal
) -> Decimal:
    """``total_fob_cost / bundles_per_container``.

    Zero freight is a legitimate answer — an ex-works sale carries none — but
    zero bundles is not, and dividing by it is the failure this guards.
    """
    total = to_decimal(total_fob_cost)
    bundles = to_decimal(bundles_per_container)
    if total < ZERO:
        raise PricingError("Total FOB cost cannot be negative.")
    if bundles <= ZERO:
        raise PricingError(
            "Bundles per container must be greater than zero — freight cannot "
            "be shared across a container that holds nothing."
        )
    return total / bundles


def original_cost(
    product_cost: Decimal, fob_cost: Decimal
) -> Decimal:
    """``product_cost_per_bundle + fob_cost_per_bundle``.

    The complete cost of one bundle delivered to the ship, before any markup.
    """
    return to_decimal(product_cost) + to_decimal(fob_cost)


def selling_price(cost: Decimal, markup_percentage: Decimal) -> Decimal:
    """``original_cost x (1 + markup_percentage)``.

    ``markup_percentage`` is a rate, not a multiplier: 17% is ``0.17``.
    """
    base = to_decimal(cost)
    markup = to_decimal(markup_percentage)
    if markup < ZERO:
        raise PricingError("Markup percentage cannot be negative.")
    return base * (ONE + markup)


def build(
    *,
    unit_cost_per_piece: Decimal,
    pieces_per_bundle: Decimal,
    bundles_per_container: Decimal,
    total_fob_cost: Decimal,
    markup_percentage: Decimal,
) -> PriceBuild:
    """Run the whole build, keeping every step."""
    unit = to_decimal(unit_cost_per_piece)
    pieces = to_decimal(pieces_per_bundle)
    bundles = to_decimal(bundles_per_container)
    fob_total = to_decimal(total_fob_cost)
    markup = to_decimal(markup_percentage)

    goods = product_cost_per_bundle(unit, pieces)
    freight = fob_cost_per_bundle(fob_total, bundles)
    cost = original_cost(goods, freight)
    price = selling_price(cost, markup)

    return PriceBuild(
        unit_cost_per_piece=unit,
        pieces_per_bundle=pieces,
        bundles_per_container=bundles,
        total_fob_cost=fob_total,
        markup_percentage=markup,
        product_cost_per_bundle=goods,
        fob_cost_per_bundle=freight,
        original_cost=cost,
        selling_price=price,
    )
