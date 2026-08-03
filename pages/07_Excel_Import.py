"""Excel Import — load a price-list workbook.

The workflow is deliberately two-stage: **preview, then commit.** Everything up
to the preview is read-only, so what the operator approves is exactly what gets
written. The commit is a single transaction — the whole workbook lands or none
of it does, because a half-applied price list leaves some variants on new
prices and some on old with nothing to show which.
"""

from __future__ import annotations

import datetime as dt
from io import BytesIO

import streamlit as st

from modules.authorization import PermissionDenied
from modules.constants import SUPPORTED_CURRENCIES, ImportRowStatus, Perm
from modules.database import session_scope
from modules.excel_importer import (
    ImportError_,
    build_plan,
    commit_plan,
    list_sheets,
    read_workbook,
)
from modules.repositories import catalogue_counts
from modules.session import page_header, require_page
from modules.storage import (
    StorageError,
    build_key,
    get_storage,
    sha256_of,
    validate_upload,
)
from modules.utilities import format_date

user = require_page(Perm.PRICE_IMPORT)
page_header("Excel Import", "Load a price list and preview every row before committing")

STATE_KEY = "import_upload"

with session_scope() as db:
    before_counts = catalogue_counts(db)

st.caption(
    f"Catalogue today: {before_counts['products']} products · "
    f"{before_counts['variants']} variants · {before_counts['prices']} price records"
)


# --------------------------------------------------------------------------- #
# 1. Upload
# --------------------------------------------------------------------------- #

uploaded = st.file_uploader(
    "Price-list workbook",
    type=["xlsx", "xls"],
    help="The file is checked for type and size, and archived alongside the prices it produces.",
)

if uploaded is None:
    st.session_state.pop(STATE_KEY, None)
    with st.expander("What this importer handles", expanded=True):
        st.markdown(
            "- The header row does **not** need to be the first row, and a sheet may "
            "contain **several price blocks**.\n"
            "- Multi-line headers such as `Standard⏎Price/Pack` are normalised "
            "automatically, as are `3 containers` and `8 containers` columns.\n"
            "- **Board quality is read from each row's own Quality column**, never "
            "inferred from a section heading — a section can legitimately contain more "
            "than one quality.\n"
            "- Both the pack price and the piece price are imported exactly as written; "
            "neither is derived from or corrected against the other.\n"
            "- Existing prices are **never overwritten**. A changed price closes the old "
            "one the day before the new one starts."
        )
    st.stop()

data = uploaded.getvalue()
try:
    safe_name = validate_upload(data, uploaded.name)
except StorageError as exc:
    st.error(str(exc))
    st.stop()

st.success(f"{safe_name} · {len(data) / 1024:.0f} KB")


# --------------------------------------------------------------------------- #
# 2. Sheet and effective date
# --------------------------------------------------------------------------- #

try:
    sheets = list_sheets(BytesIO(data))
except Exception as exc:  # noqa: BLE001 - openpyxl raises a variety of types
    st.error(f"That file could not be opened as a workbook: {exc}")
    st.stop()

sheet_col, date_col, currency_col = st.columns([2, 1.2, 1])
with sheet_col:
    sheet_name = st.selectbox("Worksheet", sheets)
with date_col:
    effective_from = st.date_input(
        "Prices effective from",
        value=dt.date.today(),
        help=(
            "The date the imported prices take effect. Existing prices are closed the "
            "day before. It must be later than any price already recorded."
        ),
    )
with currency_col:
    currency = st.selectbox("Currency", SUPPORTED_CURRENCIES, index=0)

try:
    blocks, rows, terms = read_workbook(BytesIO(data), sheet_name)
except ImportError_ as exc:
    st.error(str(exc))
    st.stop()


# --------------------------------------------------------------------------- #
# 3. Detected structure
# --------------------------------------------------------------------------- #

st.markdown("##### Detected structure")
st.dataframe(
    [
        {
            "Header row": block.header_row,
            "Data rows": f"{block.first_data_row}–{block.last_data_row}",
            "Rows": block.row_count,
            "Section": block.section_label or "(none)",
            "Columns mapped": len(block.columns),
        }
        for block in blocks
    ],
    width="stretch",
    hide_index=True,
)

qualities = sorted({r.parsed.board_quality for r in rows if r.ok})
st.caption(
    f"{len(rows)} data rows · {len(qualities)} board "
    f"{'quality' if len(qualities) == 1 else 'qualities'}: {', '.join(qualities)}"
)


# --------------------------------------------------------------------------- #
# 4. Preview
# --------------------------------------------------------------------------- #

with session_scope() as db:
    plan = build_plan(db, rows, blocks, terms, effective_from, currency)

counts = plan.counts()
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Create", counts["create"])
c2.metric("Update", counts["update"])
c3.metric("Unchanged", counts["skip"])
c4.metric("Duplicates", counts["duplicate"])
c5.metric("Errors", counts["error"])

ACTION_LABEL = {
    ImportRowStatus.ERROR: "⛔ error",
    ImportRowStatus.DUPLICATE: "⚠ duplicate",
}


def _describe(row_plan) -> str:  # noqa: ANN001
    if row_plan.status in ACTION_LABEL:
        return ACTION_LABEL[row_plan.status]
    return {"CREATE": "＋ create", "UPDATE": "✎ update", "SKIP": "· unchanged"}[
        str(row_plan.action)
    ]


def _price_summary(row_plan) -> str:  # noqa: ANN001
    if not row_plan.price_changes:
        return "-"
    return " · ".join(
        f"{tier.replace('_', ' ').title()}: "
        + (f"{old} → {new}" if old is not None else str(new))
        for tier, (old, new) in row_plan.price_changes.items()
    )


st.markdown("##### Every row, and what will happen to it")
st.dataframe(
    [
        {
            "Row": p.row.source_row_no,
            "Action": _describe(p),
            "Product": (p.row.parsed.product if p.row.ok else "-"),
            "Board quality": (p.row.parsed.board_quality if p.row.ok else "-"),
            "Case": (p.row.parsed.case_pack if p.row.ok else "-"),
            "Section": p.row.section_label or "(main)",
            "Price changes": _price_summary(p),
            "Note": p.message or "",
        }
        for p in plan.plans
    ],
    width="stretch",
    hide_index=True,
    height=min(600, 60 + 35 * len(plan.plans)),
)

if counts["error"] or counts["duplicate"]:
    st.warning(
        f"{counts['error'] + counts['duplicate']} row(s) will be skipped and recorded "
        "with their reason. The rest can still be imported."
    )

if terms:
    with st.expander("Commercial terms found in this workbook", expanded=False):
        st.dataframe(
            [{"Label": k, "Text": v} for k, v in terms.items()],
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "Shown for reference only — these are **not** applied to the term templates. "
            "A validity date in a price list is historical and must not become a default."
        )


# --------------------------------------------------------------------------- #
# 5. Commit
# --------------------------------------------------------------------------- #

st.divider()

if counts["create"] == 0 and counts["update"] == 0:
    st.info(
        "Nothing to import — every row either matches what is already recorded or "
        "cannot be read. Re-importing an unchanged workbook is safe and does nothing."
    )
    st.stop()

st.markdown(
    f"**{counts['create']} new variant(s)** and **{counts['update']} price update(s)** "
    f"will take effect from **{format_date(effective_from)}** in **{currency}**."
)
confirmed = st.checkbox(
    "I have reviewed the rows above and want to import them",
    key="import_confirmed",
)

if st.button("Import price list", type="primary", disabled=not confirmed):
    storage_key = build_key("price_lists", safe_name)
    try:
        get_storage().put(
            storage_key,
            data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        with session_scope() as db:
            fresh_plan = build_plan(db, rows, blocks, terms, effective_from, currency)
            job = commit_plan(
                db,
                fresh_plan,
                user,
                file_name=safe_name,
                sheet_name=sheet_name,
                storage_key=storage_key,
                sha256=sha256_of(data),
                currency=currency,
            )
            summary = {
                "created": job.rows_created,
                "updated": job.rows_updated,
                "skipped": job.rows_skipped,
                "failed": job.rows_failed,
            }
    except (PermissionDenied, StorageError) as exc:
        st.error(str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator, and audited
        st.error(
            f"The import failed and **nothing was changed**: {exc}\n\n"
            "The failure has been recorded in the import history."
        )
    else:
        with session_scope() as db:
            after = catalogue_counts(db)
        st.success(
            f"Imported: {summary['created']} created, {summary['updated']} updated, "
            f"{summary['skipped']} unchanged, {summary['failed']} skipped."
        )
        delta_col1, delta_col2, delta_col3 = st.columns(3)
        delta_col1.metric(
            "Products", after["products"],
            delta=after["products"] - before_counts["products"],
        )
        delta_col2.metric(
            "Variants", after["variants"],
            delta=after["variants"] - before_counts["variants"],
        )
        delta_col3.metric(
            "Price records", after["prices"],
            delta=after["prices"] - before_counts["prices"],
        )
        st.caption(
            "The workbook has been archived, and every price records the file and row "
            "it came from."
        )
