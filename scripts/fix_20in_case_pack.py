"""Correct the 20 inch case pack, and the pack price that was derived from it.

The price list gives the 20 inch as ``Case 50`` with a pack price of 18.32 and
a piece price of 0.3664 — exactly 50 x the piece price. The pack actually
holds **25**, so that pack price was worked out on the wrong multiplier and is
double what it should be.

The piece price is the sound figure and is left alone. It continues the size
curve smoothly (14 inch 0.2051, 16 inch 0.2622, 18 inch 0.3128, 20 inch
0.3664) where 0.7328 — the alternative reading, in which 18.32 is right for 25
boxes — would make a 20 inch box cost 2.3 times an 18 inch for 1.23 times the
area.

Correcting the case pack also clears the container-capacity anomaly. At 25 a
container holds 1,512 x 25 = 37,800 boxes, against 44,500 for the 18 inch and
the roughly 36,000 that area scaling predicts, and a full container comes to
13,850 USD — inside the 11,960 to 13,950 band the other eleven sizes occupy.
The workbook figure of 1,512 was right all along; the case pack was not.

The case pack is not edited in place. ``catalogue_service`` refuses that, and
rightly: board quality and case pack are what identify a variant, and every
quotation line snapshots the case pack it was sold under, so changing it would
alter what past quotations meant. A replacement variant is created at the
correct pack with the correct prices, and the old one is deactivated. It is
kept rather than deleted — it records what the price list actually said.

    python -m scripts.fix_20in_case_pack             # dry run
    python -m scripts.fix_20in_case_pack --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from decimal import Decimal

from sqlalchemy import select

from modules.authorization import load_auth_user
from modules.catalogue_service import create_variant, set_price
from modules.database import session_scope
from modules.models import (
    PriceTier,
    Product,
    ProductContainerCapacity,
    ProductPrice,
    ProductVariant,
    QuotationItem,
    User,
)
from modules.validation import PriceInput, VariantInput

SIZE_LABEL = '20" White'
TRUE_CASE_PACK = 25


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url")
    parser.add_argument(
        "--effective-from",
        default=dt.date.today().isoformat(),
        help="Date the corrected prices take effect (default: today).",
    )
    args = parser.parse_args(argv)

    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    effective_from = dt.date.fromisoformat(args.effective_from)

    with session_scope() as session:
        product = session.execute(
            select(Product).where(Product.size_label == SIZE_LABEL)
        ).scalar_one_or_none()
        if product is None:
            print(f"No product with size label {SIZE_LABEL!r}.")
            return 1

        variants = list(
            session.execute(
                select(ProductVariant).where(
                    ProductVariant.product_id == product.id
                )
            ).scalars()
        )

        print(f"\n{SIZE_LABEL}  (product {product.item_number})")
        print(f"  bundle size   {product.units_per_bundle} -> {TRUE_CASE_PACK}")

        planned: list[tuple] = []
        for variant in variants:
            new_code = variant.variant_item_number.replace(
                f"-{variant.case_pack}", f"-{TRUE_CASE_PACK}"
            )
            print(f"  case pack     {variant.case_pack} -> {TRUE_CASE_PACK}")
            print(
                f"  variant       {variant.variant_item_number} retired, "
                f"replaced by {new_code}"
            )

            rows = session.execute(
                select(ProductPrice, PriceTier)
                .join(PriceTier)
                .where(
                    ProductPrice.product_variant_id == variant.id,
                    ProductPrice.effective_to.is_(None),
                )
                .order_by(PriceTier.sort_order)
            ).all()
            for price, tier in rows:
                corrected = (
                    price.price_per_piece * Decimal(TRUE_CASE_PACK)
                ).quantize(Decimal("0.000001"))
                print(
                    f"  {tier.code:18} pack {price.price_per_pack} -> {corrected}"
                    f"   (piece {price.price_per_piece} unchanged)"
                )
                planned.append((variant, tier.code, corrected, price.price_per_piece,
                                price.currency))

        capacity = session.execute(
            select(ProductContainerCapacity).where(
                ProductContainerCapacity.product_id == product.id
            )
        ).scalars().first()
        if capacity is not None and capacity.is_anomalous:
            print(
                f"  capacity      {capacity.bundles_per_container:,.0f} bundles "
                f"— anomaly flag cleared (correct once the pack is 25)"
            )

        if not args.apply:
            print("\nDry run. Re-run with --apply.")
            return 0

        admin = session.execute(
            select(User).where(User.username == "admin")
        ).scalar_one_or_none() or session.execute(select(User)).scalars().first()
        user = load_auth_user(session, admin)

        replacements: dict[int, ProductVariant] = {}
        for variant in variants:
            if any(
                item.product_variant_id == variant.id
                for item in session.execute(select(QuotationItem)).scalars()
            ):
                print(
                    f"  REFUSED: {variant.variant_item_number} is used by a "
                    f"quotation line; retiring it would need a decision."
                )
                return 1

            replacement = create_variant(
                session, user, product.id,
                VariantInput(
                    variant_item_number=variant.variant_item_number.replace(
                        f"-{variant.case_pack}", f"-{TRUE_CASE_PACK}"
                    ),
                    board_quality=variant.board_quality,
                    case_pack=TRUE_CASE_PACK,
                    num_colours=variant.num_colours,
                    moq_packs=variant.moq_packs,
                    moq_pieces=variant.moq_pieces,
                    spec_text_override=variant.spec_text_override,
                    notes=(
                        "Replaces "
                        f"{variant.variant_item_number}, which recorded a case pack "
                        "of 50 from the price list. The pack holds 25."
                    ),
                    is_active=True,
                ),
            )
            replacements[variant.id] = replacement

            # Retired, not deleted: it is the record of what the sheet said.
            variant.is_active = False
            variant.notes = (
                f"Retired: the case pack of {variant.case_pack} came from the price "
                f"list but the pack holds {TRUE_CASE_PACK}. Replaced by "
                f"{replacement.variant_item_number}."
            )

        for variant, tier_code, pack, piece, currency in planned:
            set_price(
                session, user,
                PriceInput(
                    product_variant_id=replacements[variant.id].id,
                    price_tier_code=tier_code,
                    price_per_pack=pack,
                    price_per_piece=piece,
                    currency=currency,
                    effective_from=effective_from,
                ),
            )

        product.units_per_bundle = Decimal(TRUE_CASE_PACK)

        if capacity is not None:
            capacity.is_anomalous = False
            capacity.anomaly_note = (
                "Resolved: the figure is correct for a case pack of 25. It looked "
                "wrong only while the catalogue held 50, which implied 75,600 boxes "
                "per container against 44,500 for the smaller 18 inch."
            )

        print("\nApplied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
