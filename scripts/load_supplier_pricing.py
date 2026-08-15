"""Load a supplier workbook: container capacity, costs and selling prices.

Written for the KIPAS offer of 7 August 2026, and it is what put the current
catalogue in place. Kept in the repository because the production data it
produced cannot be explained without it: three tables were written from one
spreadsheet, and "where did $4.3510 come from" has an answer only while this
exists.

The build, per variant::

    product_cost_per_bundle = unit_cost_per_piece x pieces_per_bundle
    fob_cost_per_bundle     = total_fob_cost / bundles_per_container
    original_cost           = product_cost_per_bundle + fob_cost_per_bundle
    selling_price           = original_cost x (1 + markup_percentage)

The arithmetic lives in :mod:`modules.supplier_pricing`, not here, so the
figures a script writes and the figures the application computes cannot
diverge. The three inputs come from settings.

Capacity is written **per variant**. The sheet states bundles-per-container per
board quality and prices from it — IK135 fits 2,160 where IK90 and IK120 fit
2,304 — so freight per bundle differs by board, and one quality's figure must
not stand for all three.

Run against a database with ``--apply``; without it, prints the plan and rolls
back::

    python -m scripts.load_supplier_pricing "PIZZA OFFER KIPAS.xlsx"
    python -m scripts.load_supplier_pricing "PIZZA OFFER KIPAS.xlsx" --apply

Prices and costs are corrected **at the effective date given**, not superseded.
Both tables are append-only and the ORM guard refuses a field change, so an
existing row for that date is deleted and rewritten. That is right for fixing a
figure that was loaded wrongly and wrong for a genuine price change: dating a
movement today would assert a change that never happened, and any quotation
priced in between would reproduce the wrong number. For a real price change,
use a later effective date and let the old row be superseded.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from decimal import Decimal as D

if __package__ is None:  # pragma: no cover - direct invocation
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Board quality -> the code used in a variant item number.
QUALITY_CODES = {
    "WTL125 FL120 IK90": "IK90",
    "WTL125 FL120 IK120": "IK120",
    "WTL125 FL135 IK135": "IK135",
}

#: Columns in the KIPAS layout, by position. The sheet has no stable header row
#: — three blocks repeat one — so rows are recognised by shape instead.
COL_SIZE, COL_QUALITY, COL_SHRINK, COL_EXW, COL_BUNDLES = 0, 2, 3, 4, 9


def read_workbook(path: str, sheet: str) -> list[dict]:
    import openpyxl

    ws = openpyxl.load_workbook(path, data_only=True)[sheet]
    rows: list[dict] = []
    for number, row in enumerate(ws.iter_rows(values_only=True), start=1):
        cells = list(row) + [None] * 18
        size = cells[COL_SIZE]
        quality = cells[COL_QUALITY]
        bundles = cells[COL_BUNDLES]
        if not isinstance(size, (int, float)) or quality not in QUALITY_CODES:
            continue
        if bundles is None:
            continue
        rows.append({
            "row": number,
            "size": int(size),
            "quality": quality,
            "variant_number": f"WB-{int(size):02d}-{QUALITY_CODES[quality]}",
            "unit_cost_per_piece": D(str(cells[COL_EXW])),
            "pieces_per_bundle": D(str(cells[COL_SHRINK])),
            "bundles_per_container": D(str(bundles)),
        })
    return rows


def run(path: str, sheet: str, effective_from: dt.date, apply: bool) -> int:
    from sqlalchemy import select

    from modules import settings_service, supplier_pricing
    from modules.authorization import load_auth_user
    from modules.catalogue_service import set_cost, set_price
    from modules.constants import ContainerSize, ContainerType, PriceTierCode
    from modules.database import session_scope
    from modules.models import (
        PriceTier, ProductContainerCapacity, ProductCost, ProductPrice,
        ProductVariant, User,
    )
    from modules.validation import CostInput, PriceInput

    rows = read_workbook(path, sheet)
    if not rows:
        print("No priced rows found. Is this the right sheet?")
        return 1

    with session_scope() as session:
        actor = session.scalars(
            select(User).where(User.is_active.is_(True)).order_by(User.id)
        ).first()
        if actor is None:
            print("No active user to attribute the change to.")
            return 1
        user = load_auth_user(session, actor)

        fob_total = settings_service.total_fob_cost(session)
        markup = settings_service.markup_percentage(session)
        tier = session.scalars(
            select(PriceTier).where(PriceTier.code == PriceTierCode.STANDARD.value)
        ).first()
        variants = {
            v.variant_item_number: v for v in session.scalars(
                select(ProductVariant).where(ProductVariant.deleted_at.is_(None))
            )
        }

        missing = [r["variant_number"] for r in rows
                   if r["variant_number"] not in variants]
        if missing:
            print(f"No live variant for: {', '.join(missing)}")
            return 1

        print(f"{len(rows)} rows from {os.path.basename(path)}")
        print(f"total_fob_cost={fob_total}  markup_percentage={markup}  "
              f"effective_from={effective_from}\n")
        header = (f"{'variant':<14}{'EXW/pc':>9}{'bdl/ctr':>9}{'FOB/bdl':>9}"
                  f"{'orig cost':>11}{'selling':>10}")
        print(header)
        print("-" * len(header))

        for row in rows:
            variant = variants[row["variant_number"]]
            build = supplier_pricing.build(
                unit_cost_per_piece=row["unit_cost_per_piece"],
                pieces_per_bundle=row["pieces_per_bundle"],
                bundles_per_container=row["bundles_per_container"],
                total_fob_cost=fob_total,
                markup_percentage=markup,
            )

            capacity = session.scalars(select(ProductContainerCapacity).where(
                ProductContainerCapacity.product_variant_id == variant.id,
                ProductContainerCapacity.container_size == ContainerSize.FORTY_FT_HC,
                ProductContainerCapacity.container_type == ContainerType.DRY,
            )).first()
            if capacity is None:
                capacity = ProductContainerCapacity(
                    product_id=variant.product_id,
                    product_variant_id=variant.id,
                    container_size=ContainerSize.FORTY_FT_HC,
                    container_type=ContainerType.DRY,
                    bundles_per_container=row["bundles_per_container"],
                )
                session.add(capacity)
            capacity.bundles_per_container = row["bundles_per_container"]
            capacity.source_workbook_name = os.path.basename(path)
            capacity.source_row_no = row["row"]
            capacity.notes = f"{row['quality']}, stated per board quality."

            for stale in session.scalars(select(ProductCost).where(
                ProductCost.product_variant_id == variant.id,
                ProductCost.effective_from == effective_from,
            )):
                session.delete(stale)
            session.flush()
            set_cost(session, user, CostInput(
                product_variant_id=variant.id,
                cost_per_pack=build.original_cost.quantize(D("0.000001")),
                cost_per_piece=(
                    build.original_cost / build.pieces_per_bundle
                ).quantize(D("0.000001")),
                currency="USD",
                effective_from=effective_from,
                source_note=f"{os.path.basename(path)}: goods + FOB share",
            ))

            for stale in session.scalars(select(ProductPrice).where(
                ProductPrice.product_variant_id == variant.id,
                ProductPrice.price_tier_id == tier.id,
                ProductPrice.effective_from == effective_from,
            )):
                session.delete(stale)
            session.flush()
            set_price(session, user, PriceInput(
                product_variant_id=variant.id,
                price_tier_code=PriceTierCode.STANDARD.value,
                price_per_pack=build.selling_price.quantize(D("0.000001")),
                price_per_piece=(
                    build.selling_price / build.pieces_per_bundle
                ).quantize(D("0.000001")),
                currency="USD",
                effective_from=effective_from,
            ))

            print(f'{row["variant_number"]:<14}'
                  f'{build.unit_cost_per_piece:>9}'
                  f'{build.bundles_per_container:>9.0f}'
                  f'{build.fob_cost_per_bundle:>9.4f}'
                  f'{build.original_cost:>11.4f}'
                  f'{build.selling_price:>10.4f}')

        print(f"\n{len(rows)} capacity rows, costs and prices")
        if not apply:
            print("\nDRY RUN — rolling back. Re-run with --apply to write.")
            session.rollback()
        else:
            print("\nAPPLIED.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workbook", help="path to the supplier .xlsx")
    parser.add_argument("--sheet", default="Sayfa1")
    parser.add_argument(
        "--effective-from", default="2026-08-07",
        help="the date the supplier's figures take effect (YYYY-MM-DD)",
    )
    parser.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    parser.add_argument("--database-url", help="override DATABASE_URL")
    args = parser.parse_args()

    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    return run(
        args.workbook, args.sheet,
        dt.date.fromisoformat(args.effective_from),
        args.apply,
    )


if __name__ == "__main__":
    raise SystemExit(main())
