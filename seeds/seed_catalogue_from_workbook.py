"""Seed the product catalogue from a price-list workbook.

    python -m seeds.seed_catalogue_from_workbook "path/to/White Boxes B Flute Quotation.xlsx"
    python -m seeds.seed_catalogue_from_workbook prices.xlsx --effective-from 2026-01-01

This runs the **real importer**, not a hand-written fixture. Two reasons:

* the import path is exercised on every fresh database, so it cannot rot
  unnoticed between price-list updates;
* the seeded catalogue is guaranteed to be identical to what an operator gets
  from the Excel Import page, rather than a parallel version that drifts.

Idempotent: re-running against an unchanged workbook skips every row.

The workbook is *not* committed to the repository — it is commercial pricing
data, and the file's location differs per machine. Pass the path.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.database import session_scope  # noqa: E402
from modules.excel_importer import (  # noqa: E402
    ImportError_,
    build_plan,
    commit_plan,
    list_sheets,
    read_workbook,
)
from modules.repositories import catalogue_counts  # noqa: E402
from modules.storage import build_key, get_storage, sha256_of  # noqa: E402

log = logging.getLogger(__name__)


def run(
    workbook_path: Path,
    sheet_name: str | None = None,
    effective_from: dt.date | None = None,
    currency: str = "USD",
    store_workbook: bool = True,
) -> dict[str, int]:
    """Import a workbook and return the resulting catalogue counts."""
    effective_from = effective_from or dt.date.today()
    if not workbook_path.is_file():
        raise FileNotFoundError(f"No workbook at {workbook_path}")

    data = workbook_path.read_bytes()
    digest = sha256_of(data)

    sheets = list_sheets(str(workbook_path))
    if sheet_name is None:
        sheet_name = sheets[0]
        if len(sheets) > 1:
            log.info("Workbook has %d sheets; using %r", len(sheets), sheet_name)

    blocks, rows, terms = read_workbook(str(workbook_path), sheet_name)
    log.info(
        "Detected %d block(s): %s",
        len(blocks),
        ", ".join(
            f"header row {b.header_row}, {b.row_count} rows"
            + (f" ({b.section_label})" if b.section_label else "")
            for b in blocks
        ),
    )

    storage_key: str | None = None
    if store_workbook:
        # Keep the source file alongside the prices it produced, so any
        # historical price can be traced back to the exact workbook and row.
        storage_key = build_key("price_lists", workbook_path.name)
        get_storage().put(
            storage_key,
            data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with session_scope() as session:
        plan = build_plan(session, rows, blocks, terms, effective_from, currency)
        counts = plan.counts()
        log.info(
            "Plan: %d create, %d update, %d skip, %d duplicate, %d error",
            counts["create"], counts["update"], counts["skip"],
            counts["duplicate"], counts["error"],
        )
        for row_plan in plan.plans:
            if row_plan.is_error:
                log.warning("Row %d: %s", row_plan.row.source_row_no, row_plan.message)

        commit_plan(
            session,
            plan,
            user=None,
            file_name=workbook_path.name,
            sheet_name=sheet_name,
            storage_key=storage_key,
            sha256=digest,
            currency=currency,
        )

    with session_scope() as session:
        return catalogue_counts(session)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path, help="path to the .xlsx price list")
    parser.add_argument("--sheet", default=None, help="sheet name (default: the first)")
    parser.add_argument(
        "--effective-from",
        type=dt.date.fromisoformat,
        default=None,
        help="ISO date the imported prices take effect (default: today)",
    )
    parser.add_argument("--currency", default="USD")
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="do not keep a copy of the workbook in file storage",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")

    try:
        counts = run(
            args.workbook,
            sheet_name=args.sheet,
            effective_from=args.effective_from,
            currency=args.currency,
            store_workbook=not args.no_store,
        )
    except (FileNotFoundError, ImportError_) as exc:
        print(f"\nImport failed: {exc}\n", file=sys.stderr)
        return 1

    print("\nCatalogue after import:")
    print(f"  Products : {counts['products']}")
    print(f"  Variants : {counts['variants']}")
    print(f"  Prices   : {counts['prices']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
