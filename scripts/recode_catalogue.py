"""Regenerate catalogue item numbers under the scheme in ``modules.item_codes``.

The codes the price-list importer originally minted were slugified product
names — ``7-WHITE`` and ``7-WHITE-WT110-HPFL115-KM135-50``. They sort wrongly
(``10-WHITE`` lands before ``7-WHITE``) and the variant codes spend thirty
characters repeating constants. This rewrites them as ``WB-07`` and
``WB-07-115-50``.

Run it against a database with ``--apply``; without that flag it prints what it
would do and writes nothing::

    python -m scripts.recode_catalogue                     # dry run
    python -m scripts.recode_catalogue --apply             # rewrite

Safe to run twice: a code already matching the scheme is left alone, and the
run reports no changes.

Quotation lines are untouched. Each line stores ``item_number_snapshot``,
taken when the line was quoted, so a quotation issued last month still shows
and prints the code it was issued under. That is deliberate — a customer
holding a PDF must be able to quote its codes back at us — and it means
recoding the catalogue cannot rewrite history.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from modules import item_codes
from modules.audit_service import record_audit
from modules.constants import AuditAction, EntityType
from modules.database import session_scope
from modules.models import Product, ProductVariant

#: The placeholder each row is parked on between the two update passes.
#: Renaming A to B while B still exists would trip the unique index even when
#: the end state is perfectly consistent, so nothing holds its final code
#: until every row has let go of its old one.
_PARK_PREFIX = "~RECODE~"


def _size_sort_key(product: Product) -> tuple:
    """Order products by size rather than by the text of their label."""
    match = re.search(r"\d+(?:\.\d+)?", product.size_label or "")
    try:
        size = Decimal(match.group()) if match else Decimal(0)
    except InvalidOperation:  # pragma: no cover - the regex precludes it
        size = Decimal(0)
    return (product.category or "", size, product.id)


def plan(session) -> tuple[list[tuple], list[tuple]]:
    """Work out every product and variant code, resolving collisions.

    Returns ``(product_changes, variant_changes)``, each a list of
    ``(row, old_code, new_code)`` covering *every* row — unchanged ones
    included, so a dry run shows the whole catalogue and not just the diff.
    """
    # Soft-deleted rows still hold their codes against the unique index, so
    # they are recoded alongside the rest rather than left to collide with a
    # live product that lands on the same code.
    products = sorted(
        session.execute(select(Product)).scalars().all(), key=_size_sort_key
    )

    product_changes: list[tuple] = []
    taken_products: set[str] = set()
    for product in products:
        code = item_codes.disambiguate(
            item_codes.product_code(product.category, product.size_label),
            taken_products,
            item_codes.MAX_PRODUCT_CODE,
        )
        taken_products.add(code)
        product_changes.append((product, product.item_number, code))

    new_by_product_id = {p.id: code for p, _, code in product_changes}

    variant_changes: list[tuple] = []
    taken_variants: set[str] = set()
    for product, _, _ in product_changes:
        variants = sorted(
            session.execute(
                select(ProductVariant).where(
                    ProductVariant.product_id == product.id
                )
            )
            .scalars()
            .all(),
            key=lambda v: (v.board_quality or "", v.case_pack or 0, v.id),
        )
        for variant in variants:
            code = item_codes.disambiguate(
                item_codes.variant_code(
                    new_by_product_id[variant.product_id],
                    variant.board_quality,
                    variant.case_pack,
                ),
                taken_variants,
                item_codes.MAX_VARIANT_CODE,
            )
            taken_variants.add(code)
            variant_changes.append(
                (variant, variant.variant_item_number, code)
            )

    return product_changes, variant_changes


def apply(session, product_changes, variant_changes, user=None) -> int:
    """Write the new codes, in two passes, auditing each real change."""
    changed = [
        (row, old, new) for row, old, new in product_changes + variant_changes
        if old != new
    ]
    if not changed:
        return 0

    for index, (row, _old, _new) in enumerate(changed):
        park = f"{_PARK_PREFIX}{index}"
        if isinstance(row, Product):
            row.item_number = park
        else:
            row.variant_item_number = park
    session.flush()

    for row, old, new in changed:
        if isinstance(row, Product):
            row.item_number = new
            entity_type, action = EntityType.PRODUCT, AuditAction.PRODUCT_EDITED
        else:
            row.variant_item_number = new
            entity_type, action = (
                EntityType.PRODUCT_VARIANT,
                AuditAction.PRODUCT_EDITED,
            )
        record_audit(
            session, user, action, entity_type, row.id,
            old_value={"item_number": old},
            new_value={"item_number": new},
            reason="Catalogue recoded to the standard item-code scheme",
            page="scripts/recode_catalogue.py",
            username=getattr(user, "username", "system"),
        )
    session.flush()
    return len(changed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the new codes. Without it, nothing is written.",
    )
    parser.add_argument(
        "--database-url",
        help="Overrides DATABASE_URL for this run.",
    )
    args = parser.parse_args(argv)

    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url

    with session_scope() as session:
        product_changes, variant_changes = plan(session)

        width = max(
            (len(old) for _, old, _ in product_changes + variant_changes),
            default=10,
        )
        print(f"\nPRODUCTS ({len(product_changes)})")
        for row, old, new in product_changes:
            mark = " " if old == new else "*"
            print(f" {mark} {old:<{width}}  ->  {new:<14}  {row.size_label}")

        print(f"\nVARIANTS ({len(variant_changes)})")
        for row, old, new in variant_changes:
            mark = " " if old == new else "*"
            print(f" {mark} {old:<{width}}  ->  {new:<14}  {row.board_quality}")

        pending = sum(
            1 for _, old, new in product_changes + variant_changes if old != new
        )
        if not args.apply:
            print(f"\nDry run — {pending} code(s) would change. Re-run with --apply.")
            return 0

        written = apply(session, product_changes, variant_changes)
        print(f"\n{written} code(s) rewritten.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
