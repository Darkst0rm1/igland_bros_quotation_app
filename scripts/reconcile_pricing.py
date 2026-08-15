"""Check every stored selling price against the formula that should produce it.

Reporting only — it writes nothing, and is safe to run against production.

Two prices are compared for each variant:

* what ``product_prices`` holds, and
* what :mod:`modules.supplier_pricing` computes from the supplier's cost, that
  variant's container capacity, and the three pricing settings.

A mismatch means the catalogue and the formula have drifted: a price edited by
hand, a setting changed without a reload, or a capacity figure updated after
the price was built. That drift is invisible until a customer queries an
invoice, which is why this exists.

Optionally shows what each price would have been under the superseded formula,
which marked up the goods alone and added freight afterwards::

    python -m scripts.reconcile_pricing
    python -m scripts.reconcile_pricing --against-old-formula

The difference reconciles to ``fob_cost_per_bundle x markup_percentage`` on
every row, and measuring it against a *rounded* published price rather than the
exact figure gives a different, wrong answer — which is how it was first
reported.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal as D

if __package__ is None:  # pragma: no cover - direct invocation
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXP = D("0.0001")
TOLERANCE = D("0.000001")


def run(show_old: bool) -> int:
    from sqlalchemy import select

    from modules import settings_service, supplier_pricing
    from modules.constants import ContainerSize, ContainerType, PriceTierCode
    from modules.database import session_scope
    from modules.models import (
        PriceTier, Product, ProductContainerCapacity, ProductCost, ProductPrice,
        ProductVariant,
    )

    drift = 0
    with session_scope() as session:
        fob_total = settings_service.total_fob_cost(session)
        markup = settings_service.markup_percentage(session)
        tier = session.scalars(
            select(PriceTier).where(PriceTier.code == PriceTierCode.STANDARD.value)
        ).first()
        products = {p.id: p for p in session.scalars(select(Product))}

        print(f"total_fob_cost={fob_total}  markup_percentage={markup}\n")
        header = f"{'variant':<14}{'stored':>10}{'computed':>10}{'match':>7}"
        if show_old:
            header += f"{'old formula':>13}{'diff':>9}{'fob x mk':>10}"
        print(header)
        print("-" * len(header))

        for variant in session.scalars(
            select(ProductVariant).where(ProductVariant.deleted_at.is_(None))
        ):
            price = session.scalars(select(ProductPrice).where(
                ProductPrice.product_variant_id == variant.id,
                ProductPrice.price_tier_id == tier.id,
                ProductPrice.effective_to.is_(None),
            )).first()
            cost = session.scalars(select(ProductCost).where(
                ProductCost.product_variant_id == variant.id,
                ProductCost.effective_to.is_(None),
            )).first()
            capacity = session.scalars(select(ProductContainerCapacity).where(
                ProductContainerCapacity.product_variant_id == variant.id,
                ProductContainerCapacity.container_size == ContainerSize.FORTY_FT_HC,
                ProductContainerCapacity.container_type == ContainerType.DRY,
            )).first()
            if price is None or cost is None or capacity is None:
                print(f"{variant.variant_item_number:<14}"
                      f"{'—':>10}{'—':>10}{'skip':>7}   "
                      f"(no {'price' if price is None else 'cost' if cost is None else 'capacity'})")
                continue

            product = products[variant.product_id]
            pieces = D(variant.case_pack or 1)
            # The stored cost is goods + freight, so the goods alone are what
            # remains once this variant's own freight share is taken back out.
            fob_share = fob_total / capacity.bundles_per_container
            goods = cost.cost_per_pack - fob_share
            build = supplier_pricing.build(
                unit_cost_per_piece=goods / pieces,
                pieces_per_bundle=pieces,
                bundles_per_container=capacity.bundles_per_container,
                total_fob_cost=fob_total,
                markup_percentage=markup,
            )

            ok = abs(build.selling_price - price.price_per_pack) < TOLERANCE
            drift += not ok
            line = (f"{variant.variant_item_number:<14}"
                    f"{price.price_per_pack.quantize(EXP):>10}"
                    f"{build.selling_price.quantize(EXP):>10}"
                    f"{'yes' if ok else 'NO':>7}")
            if show_old:
                previous = (
                    build.unit_cost_per_piece * build.markup_multiplier
                    * build.pieces_per_bundle + build.fob_cost_per_bundle
                )
                difference = build.selling_price - previous
                expected = build.fob_cost_per_bundle * build.markup_percentage
                line += (f"{previous.quantize(EXP):>13}"
                         f"{difference.quantize(EXP):>+9}"
                         f"{expected.quantize(EXP):>10}")
            print(line)

        print(f"\n{'no drift' if not drift else f'{drift} price(s) DRIFTED'}")
    return 1 if drift else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--against-old-formula", action="store_true",
        help="also show the superseded calculation and the difference",
    )
    parser.add_argument("--database-url", help="override DATABASE_URL")
    args = parser.parse_args()
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    return run(args.against_old_formula)


if __name__ == "__main__":
    raise SystemExit(main())
