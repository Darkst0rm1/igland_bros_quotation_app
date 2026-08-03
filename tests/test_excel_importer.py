"""Price-list import: detection, normalisation, diffing and commit.

Most tests run against a **synthetic workbook built in code** that reproduces
the reference file's structural quirks exactly — header not on row 1, two
blocks, board quality changing partway down the second block, embedded newlines
in the price headers. That keeps the suite deterministic and runnable on a
machine that does not have the real file.

The last class runs against the real workbook when it is present, and asserts
the catalogue it should produce.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook

from modules.excel_importer import (
    ImportError_,
    build_plan,
    commit_plan,
    detect_blocks,
    extract_terms,
    list_sheets,
    normalise_header,
    read_workbook,
)
from modules.constants import ImportJobStatus, ImportRowAction, ImportRowStatus
from modules.models import ImportJob, ImportRow, ProductPrice
from modules.repositories import (
    catalogue_counts,
    find_variant_by_natural_key,
    get_effective_price,
    price_history,
)

JAN = dt.date(2026, 1, 1)
JUL = dt.date(2026, 7, 1)

HEADERS = [
    "Product", "Depth", "Flute", "Case", "Quality",
    "Standard\nPrice/Pack", "Standard\nPrice/Pcs",
    "3 containers\nPrice/Pack", "3 containers\nPrice/Pcs",
    "8 containers\nPrice/Pack", "8 containers\nPrice/Pcs",
]

MAIN_ROWS = [
    ('7" White', '2"', "B", 50, "WT110 HPFL115 KM135", 3.79, 0.0758, 3.68, 0.0736, 3.56, 0.0713),
    ('8" White', '2"', "B", 50, "WT110 HPFL115 KM135", 4.49, 0.0899, 4.36, 0.0872, 4.22, 0.0845),
    ('9" White', '2"', "B", 50, "WT110 HPFL115 KM135", 4.66, 0.0932, 4.52, 0.0904, 4.38, 0.0876),
]

#: Mirrors the real file: the "alternative quality" block contains TWO
#: qualities, changing partway down.
ALT_ROWS = [
    ('7" White', '2"', "B", 50, "WT110 HPFL135 KM135", 3.99, 0.0798, 3.87, 0.0774, 3.75, 0.075),
    ('8" White', '2"', "B", 50, "WT110 HPFL135 KM135", 4.73, 0.0946, 4.59, 0.0918, 4.45, 0.0889),
    ('14" White', '2"', "B", 50, "WT110 HPFL160 KM135", 10.26, 0.2051, 9.95, 0.199, 9.64, 0.1928),
    ('20" White', '2"', "B", 50, "WT110 HPFL160 KM135", 18.32, 0.3664, 17.77, 0.3554, 17.22, 0.3444),
]

TERMS_FOOTER = [
    ("Payment Terms", "Payment upon receipt."),
    ("Printing", "Flexo Printing to be Applied."),
    ("Delivery Terms", "FOB Çerkezköy (Türkiye) (INCOTERMS 2020)"),
    ("Loading Notes", "Shipment with 40' HC containers. Floor Loaded."),
    (None, "Containers to be filled with only three items."),
    ("Validity", "Valid through July '26"),
    ("Notes", "Printing plate charge is 200 USD per size per color."),
]


def build_workbook(main_rows=None, alt_rows=None) -> BytesIO:
    """A workbook shaped like the reference file.

    Deliberately puts a title bar on row 1 so the header is on row 2, and a
    section-label row before the second block.
    """
    main_rows = MAIN_ROWS if main_rows is None else main_rows
    alt_rows = ALT_ROWS if alt_rows is None else alt_rows

    wb = Workbook()
    ws = wb.active
    ws.title = "White Boxes B Flute"

    ws.append(["WHITE BOXES B FLUTE", None, None, None, None, "BULK"])  # row 1
    ws.append(HEADERS)                                                   # row 2
    for row in main_rows:
        ws.append(list(row))
    ws.append([])
    for label, value in TERMS_FOOTER:
        ws.append([label, value])
    ws.append([])

    if alt_rows:
        ws.append(["alternative quality"])
        ws.append(["WHITE BOXES B FLUTE", None, None, None, None, "BULK"])
        ws.append(HEADERS)
        for row in alt_rows:
            ws.append(list(row))
        ws.append([])
        for label, value in TERMS_FOOTER:
            ws.append([label, value])

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


# --------------------------------------------------------------------------- #
# Header normalisation
# --------------------------------------------------------------------------- #

class TestHeaderNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Standard\nPrice/Pack", "standard_price_per_pack"),
            ("Standard\nPrice/Pcs", "standard_price_per_piece"),
            ("3 containers\nPrice/Pack", "three_container_price_per_pack"),
            ("3 containers\nPrice/Pcs", "three_container_price_per_piece"),
            ("8 containers\nPrice/Pack", "eight_container_price_per_pack"),
            ("8 containers\nPrice/Pcs", "eight_container_price_per_piece"),
        ],
    )
    def test_the_six_price_headers_from_the_reference_file(self, raw, expected):
        assert normalise_header(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Product", "product"), ("Depth", "depth"), ("Flute", "flute"),
            ("Case", "case_pack"), ("Case Pack", "case_pack"), ("Quality", "board_quality"),
        ],
    )
    def test_identity_headers(self, raw, expected):
        assert normalise_header(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["  standard   price / pack  ", "STANDARD\r\nPRICE/PACK", "Standard\n\nPrice/Pack"],
    )
    def test_whitespace_case_and_line_endings_are_irrelevant(self, raw):
        assert normalise_header(raw) == "standard_price_per_pack"

    def test_a_future_tier_needs_no_code_change(self):
        assert normalise_header("12 containers\nPrice/Pack") == "twelve_container_price_per_pack"

    def test_singular_container_is_accepted(self):
        assert normalise_header("1 container Price/Pcs") == "one_container_price_per_piece"

    @pytest.mark.parametrize("raw", ["BULK", "WHITE BOXES B FLUTE", "", None, "Notes"])
    def test_unrecognised_headers_return_none(self, raw):
        assert normalise_header(raw) is None


# --------------------------------------------------------------------------- #
# Block detection
# --------------------------------------------------------------------------- #

class TestBlockDetection:
    def test_finds_both_blocks_with_the_header_not_on_row_one(self):
        blocks, _rows, _terms = read_workbook(build_workbook())
        assert len(blocks) == 2
        assert blocks[0].header_row == 2
        assert blocks[0].row_count == len(MAIN_ROWS)
        assert blocks[1].row_count == len(ALT_ROWS)

    def test_the_section_label_is_captured(self):
        blocks, _rows, _terms = read_workbook(build_workbook())
        assert blocks[0].section_label is None
        assert blocks[1].section_label == "alternative quality"

    def test_a_block_stops_at_the_terms_footer(self):
        blocks, _rows, _terms = read_workbook(build_workbook())
        assert blocks[0].last_data_row == 2 + len(MAIN_ROWS)

    def test_all_eleven_columns_are_mapped(self):
        blocks, _rows, _terms = read_workbook(build_workbook())
        assert set(blocks[0].columns.values()) == {
            "product", "depth", "flute", "case_pack", "board_quality",
            "standard_price_per_pack", "standard_price_per_piece",
            "three_container_price_per_pack", "three_container_price_per_piece",
            "eight_container_price_per_pack", "eight_container_price_per_piece",
        }

    def test_a_single_block_workbook_still_works(self):
        blocks, rows, _terms = read_workbook(build_workbook(alt_rows=[]))
        assert len(blocks) == 1
        assert len(rows) == len(MAIN_ROWS)

    def test_a_sheet_with_no_table_is_rejected_clearly(self):
        wb = Workbook()
        wb.active.append(["Some notes", "and nothing else"])
        buffer = BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        with pytest.raises(ImportError_, match="No price table"):
            read_workbook(buffer)

    def test_sheet_names_are_listed(self):
        assert list_sheets(build_workbook()) == ["White Boxes B Flute"]

    def test_an_unknown_sheet_name_is_rejected(self):
        with pytest.raises(ImportError_, match="no sheet named"):
            read_workbook(build_workbook(), sheet_name="Nope")


# --------------------------------------------------------------------------- #
# Board quality
# --------------------------------------------------------------------------- #

class TestBoardQualityIsPerRow:
    """The single most important property of this importer.

    The 'alternative quality' section of the reference file contains two
    different qualities. Inferring quality from the section heading would merge
    them and silently misprice every 14"-and-above quotation.
    """

    def test_the_alternative_block_yields_two_qualities(self):
        _blocks, rows, _terms = read_workbook(build_workbook())
        alt = [r for r in rows if r.section_label == "alternative quality"]
        assert {r.parsed.board_quality for r in alt} == {
            "WT110 HPFL135 KM135",
            "WT110 HPFL160 KM135",
        }

    def test_quality_is_read_from_the_row_not_the_heading(self):
        _blocks, rows, _terms = read_workbook(build_workbook())
        by_size = {
            (r.parsed.product, r.section_label): r.parsed.board_quality
            for r in rows if r.ok
        }
        assert by_size[('7" White', None)] == "WT110 HPFL115 KM135"
        assert by_size[('7" White', "alternative quality")] == "WT110 HPFL135 KM135"
        assert by_size[('14" White', "alternative quality")] == "WT110 HPFL160 KM135"

    def test_the_natural_key_includes_quality(self):
        _blocks, rows, _terms = read_workbook(build_workbook())
        sevens = [r.parsed.natural_key for r in rows if r.parsed.product == '7" White']
        assert len(sevens) == 2
        assert len(set(sevens)) == 2, "same size, different quality must not collide"


# --------------------------------------------------------------------------- #
# Row validation
# --------------------------------------------------------------------------- #

class TestRowValidation:
    def test_valid_rows_parse(self):
        _blocks, rows, _terms = read_workbook(build_workbook())
        assert all(r.ok for r in rows)
        assert len(rows) == len(MAIN_ROWS) + len(ALT_ROWS)

    def test_both_price_columns_are_kept_verbatim(self):
        """Neither column is derived from or corrected against the other."""
        _blocks, rows, _terms = read_workbook(build_workbook())
        seven = next(r.parsed for r in rows if r.parsed.product == '7" White')
        assert seven.eight_container_price_per_pack == D("3.56")
        assert seven.eight_container_price_per_piece == D("0.0713")
        # 3.56 / 50 is 0.0712, not 0.0713 — the workbook value survives intact.
        assert seven.eight_container_price_per_piece != (
            seven.eight_container_price_per_pack / 50
        )

    @pytest.mark.parametrize(
        ("bad_row", "fragment"),
        [
            (('7" W', '2"', "B", 0, "Q", 3.79, 0.0758, 3.68, 0.0736, 3.56, 0.0713), "case_pack"),
            (('7" W', '2"', "B", "abc", "Q", 3.79, 0.0758, 3.68, 0.0736, 3.56, 0.0713), "case_pack"),
            (('7" W', '2"', "B", 50, "Q", None, None, None, None, None, None), "no prices"),
            (('7" W', '2"', "B", 50, "Q", -1, None, None, None, None, None), "greater than zero"),
        ],
    )
    def test_bad_rows_are_reported_not_raised(self, bad_row, fragment):
        _blocks, rows, _terms = read_workbook(build_workbook(main_rows=[bad_row], alt_rows=[]))
        assert len(rows) == 1
        assert not rows[0].ok
        assert fragment in rows[0].error

    def test_one_bad_row_does_not_stop_the_others(self):
        rows_in = list(MAIN_ROWS)
        rows_in.insert(1, ('BAD', '2"', "B", 0, "Q", 1, 1, 1, 1, 1, 1))
        _blocks, rows, _terms = read_workbook(build_workbook(main_rows=rows_in, alt_rows=[]))
        assert sum(1 for r in rows if r.ok) == len(MAIN_ROWS)
        assert sum(1 for r in rows if not r.ok) == 1

    def test_prices_written_as_text_are_accepted(self):
        row = ('7" W', '2"', "B", "50", "Q", "$3.79", "0.0758", "3,68", None, None, None)
        _blocks, rows, _terms = read_workbook(build_workbook(main_rows=[row], alt_rows=[]))
        assert rows[0].ok, rows[0].error
        assert rows[0].parsed.standard_price_per_pack == D("3.79")
        assert rows[0].parsed.three_container_price_per_pack == D("3.68")


# --------------------------------------------------------------------------- #
# Terms footer
# --------------------------------------------------------------------------- #

class TestTermsExtraction:
    def test_labelled_terms_are_found(self):
        _blocks, _rows, terms = read_workbook(build_workbook())
        assert terms["Payment Terms"] == "Payment upon receipt."
        assert terms["Notes"].startswith("Printing plate charge is 200 USD")

    def test_a_continuation_row_joins_its_predecessor(self):
        _blocks, _rows, terms = read_workbook(build_workbook())
        assert "Floor Loaded." in terms["Loading Notes"]
        assert "only three items" in terms["Loading Notes"]

    def test_the_next_blocks_header_is_not_read_as_a_term(self):
        _blocks, _rows, terms = read_workbook(build_workbook())
        assert "Product" not in terms

    def test_utf8_survives(self):
        _blocks, _rows, terms = read_workbook(build_workbook())
        assert "Çerkezköy" in terms["Delivery Terms"]
        assert "Türkiye" in terms["Delivery Terms"]


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

@pytest.fixture
def workbook_plan(session, seeded):
    """Parse the synthetic workbook and build a plan against an empty catalogue."""

    def _plan(effective_from=JAN, source=None):
        blocks, rows, terms = read_workbook(source or build_workbook())
        return blocks, rows, terms, build_plan(session, rows, blocks, terms, effective_from)

    return _plan


class TestPlanning:
    def test_everything_is_a_create_on_an_empty_catalogue(self, workbook_plan):
        *_ , plan = workbook_plan()
        counts = plan.counts()
        assert counts["create"] == len(MAIN_ROWS) + len(ALT_ROWS)
        assert counts["update"] == counts["skip"] == counts["error"] == 0

    def test_a_repeated_natural_key_in_one_file_is_a_duplicate(self, workbook_plan):
        duplicated = list(MAIN_ROWS) + [MAIN_ROWS[0]]
        *_, plan = workbook_plan(source=build_workbook(main_rows=duplicated, alt_rows=[]))
        counts = plan.counts()
        assert counts["duplicate"] == 1
        assert counts["create"] == len(MAIN_ROWS)
        duplicate = next(p for p in plan.plans if p.status is ImportRowStatus.DUPLICATE)
        assert "already appears on row" in duplicate.message

    def test_the_same_size_at_two_qualities_is_not_a_duplicate(self, workbook_plan):
        *_, plan = workbook_plan()
        assert plan.counts()["duplicate"] == 0

    def test_planning_writes_nothing(self, session, workbook_plan):
        workbook_plan()
        assert catalogue_counts(session)["products"] == 0


# --------------------------------------------------------------------------- #
# Commit
# --------------------------------------------------------------------------- #

@pytest.fixture
def importer(session, seeded):
    """Import the synthetic workbook and return a helper for further imports."""

    def _import(effective_from=JAN, source=None, file_name="prices.xlsx", user=None):
        blocks, rows, terms = read_workbook(source or build_workbook())
        plan = build_plan(session, rows, blocks, terms, effective_from)
        job = commit_plan(session, plan, user, file_name, "White Boxes B Flute")
        session.commit()
        return job

    return _import


class TestCommit:
    def test_the_catalogue_is_created(self, session, importer):
        job = importer()
        assert job.status is ImportJobStatus.COMMITTED
        counts = catalogue_counts(session)
        # 5 distinct sizes; 7 variants; 3 tiers each.
        assert counts["products"] == 5
        assert counts["variants"] == len(MAIN_ROWS) + len(ALT_ROWS)
        assert counts["prices"] == (len(MAIN_ROWS) + len(ALT_ROWS)) * 3

    def test_one_product_carries_two_quality_variants(self, session, importer):
        importer()
        from modules.repositories import find_product_by_size, variants_for_product

        product = find_product_by_size(session, '7" White')
        qualities = {v.board_quality for v in variants_for_product(session, product.id)}
        assert qualities == {"WT110 HPFL115 KM135", "WT110 HPFL135 KM135"}

    def test_prices_carry_their_provenance(self, session, importer):
        job = importer()
        price = session.query(ProductPrice).first()
        assert price.import_job_id == job.id
        assert price.source_workbook_name == "prices.xlsx"
        assert price.source_row_no is not None

    def test_a_row_record_is_written_for_every_row(self, session, importer):
        job = importer()
        rows = session.query(ImportRow).filter_by(import_job_id=job.id).all()
        assert len(rows) == len(MAIN_ROWS) + len(ALT_ROWS)
        assert all(r.action is ImportRowAction.CREATE for r in rows)

    def test_the_section_label_is_recorded_for_audit(self, session, importer):
        job = importer()
        labels = {
            r.section_label
            for r in session.query(ImportRow).filter_by(import_job_id=job.id)
        }
        assert labels == {None, "alternative quality"}

    def test_the_summary_records_the_blocks_and_terms(self, session, importer):
        job = importer()
        assert len(job.summary_json["blocks"]) == 2
        assert "Payment Terms" in job.summary_json["terms_found"]


class TestIdempotency:
    def test_reimporting_the_same_file_changes_nothing(self, session, importer):
        importer()
        before = catalogue_counts(session)

        job = importer(file_name="again.xlsx")
        assert job.rows_created == 0
        assert job.rows_updated == 0
        assert job.rows_skipped == len(MAIN_ROWS) + len(ALT_ROWS)
        assert catalogue_counts(session) == before

    def test_reimporting_at_a_later_date_with_no_changes_still_skips(
        self, session, importer
    ):
        importer(effective_from=JAN)
        before = catalogue_counts(session)
        job = importer(effective_from=JUL, file_name="july.xlsx")
        assert job.rows_updated == 0
        assert catalogue_counts(session) == before


class TestPriceHistory:
    @staticmethod
    def _changed_workbook(new_pack: float):
        rows = list(MAIN_ROWS)
        first = list(rows[0])
        first[5] = new_pack
        rows[0] = tuple(first)
        return build_workbook(main_rows=rows, alt_rows=[])

    def test_a_changed_price_supersedes_rather_than_overwrites(self, session, importer):
        importer(source=build_workbook(alt_rows=[]))
        before = catalogue_counts(session)["prices"]

        job = importer(
            effective_from=JUL, source=self._changed_workbook(4.15), file_name="v2.xlsx"
        )
        assert job.rows_updated == 1
        # Exactly one new row: the tiers that did not change are left alone.
        assert catalogue_counts(session)["prices"] == before + 1

    def test_the_old_price_still_resolves_for_its_own_dates(self, session, importer):
        importer(source=build_workbook(alt_rows=[]))
        importer(effective_from=JUL, source=self._changed_workbook(4.15), file_name="v2.xlsx")

        variant = find_variant_by_natural_key(
            session, '7" White', '2"', "B", 50, "WT110 HPFL115 KM135"
        )
        assert get_effective_price(
            session, variant.id, "STANDARD", dt.date(2026, 3, 15)
        ).price_per_pack == D("3.79")
        assert get_effective_price(
            session, variant.id, "STANDARD", dt.date(2026, 6, 30)
        ).price_per_pack == D("3.79")
        assert get_effective_price(
            session, variant.id, "STANDARD", JUL
        ).price_per_pack == D("4.15")

    def test_the_superseded_row_is_closed_the_day_before(self, session, importer):
        importer(source=build_workbook(alt_rows=[]))
        importer(effective_from=JUL, source=self._changed_workbook(4.15), file_name="v2.xlsx")

        variant = find_variant_by_natural_key(
            session, '7" White', '2"', "B", 50, "WT110 HPFL115 KM135"
        )
        history = price_history(session, variant.id, "STANDARD")
        assert len(history) == 2
        assert history[0].effective_from == JUL and history[0].effective_to is None
        assert history[1].effective_to == dt.date(2026, 6, 30)

    def test_untouched_tiers_keep_a_single_open_row(self, session, importer):
        importer(source=build_workbook(alt_rows=[]))
        importer(effective_from=JUL, source=self._changed_workbook(4.15), file_name="v2.xlsx")

        variant = find_variant_by_natural_key(
            session, '7" White', '2"', "B", 50, "WT110 HPFL115 KM135"
        )
        three = price_history(session, variant.id, "THREE_CONTAINER")
        assert len(three) == 1
        assert three[0].effective_to is None

    def test_backdating_is_flagged_in_the_preview(self, session, importer):
        """Rewriting a price that already starts later would require mutating a
        frozen row. The operator is told at preview time, not at commit time."""
        importer(effective_from=JUL, source=build_workbook(alt_rows=[]))

        blocks, rows, terms = read_workbook(self._changed_workbook(4.15))
        plan = build_plan(session, rows, blocks, terms, JAN)

        assert plan.counts()["error"] == len(MAIN_ROWS)
        message = next(p.message for p in plan.plans if p.is_error)
        assert "already effective from 2026-07-01" in message

    def test_backdating_writes_nothing_even_if_committed(self, session, importer):
        importer(effective_from=JUL, source=build_workbook(alt_rows=[]))
        before = catalogue_counts(session)

        blocks, rows, terms = read_workbook(self._changed_workbook(4.15))
        plan = build_plan(session, rows, blocks, terms, JAN)
        job = commit_plan(session, plan, None, "backdated.xlsx", "White Boxes B Flute")
        session.commit()

        assert job.rows_failed == len(MAIN_ROWS)
        assert job.rows_created == job.rows_updated == 0
        assert catalogue_counts(session) == before

    def test_backdating_is_refused_at_the_write_layer_too(self, session, importer):
        """Defence in depth: even a plan that skipped the preview check cannot
        create two overlapping open-ended price rows."""
        from modules.excel_importer import _write_prices
        from modules.repositories import price_tier_map

        importer(effective_from=JUL, source=build_workbook(alt_rows=[]))
        variant = find_variant_by_natural_key(
            session, '7" White', '2"', "B", 50, "WT110 HPFL115 KM135"
        )
        blocks, rows, terms = read_workbook(self._changed_workbook(4.15))
        parsed = next(r.parsed for r in rows if r.parsed.product == '7" White')
        job = session.query(ImportJob).first()

        with pytest.raises(ValueError, match="effective date after"):
            _write_prices(
                session, variant, parsed, JAN, price_tier_map(session), job, "USD"
            )

    def test_a_failed_commit_leaves_no_partial_catalogue(
        self, session, seeded, monkeypatch
    ):
        """The transaction boundary: a failure partway through must not leave
        some variants repriced and others not."""
        import modules.excel_importer as importer_module

        blocks, rows, terms = read_workbook(build_workbook(alt_rows=[]))
        plan = build_plan(session, rows, blocks, terms, JAN)

        calls = {"n": 0}
        original = importer_module._write_prices

        def explode_on_third(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated failure partway through")
            return original(*args, **kwargs)

        monkeypatch.setattr(importer_module, "_write_prices", explode_on_third)

        with pytest.raises(RuntimeError):
            commit_plan(session, plan, None, "broken.xlsx", "White Boxes B Flute")

        assert catalogue_counts(session) == {"products": 0, "variants": 0, "prices": 0}
        failed = session.query(ImportJob).filter_by(status=ImportJobStatus.FAILED).all()
        assert len(failed) == 1
        assert "simulated failure" in failed[0].error_text


# --------------------------------------------------------------------------- #
# The real workbook
# --------------------------------------------------------------------------- #

REAL_WORKBOOK = Path.home() / "Downloads" / "White Boxes B Flute Quotation.xlsx"


@pytest.mark.skipif(
    not REAL_WORKBOOK.is_file(),
    reason="the reference workbook is not present on this machine",
)
class TestReferenceWorkbook:
    """Asserts the catalogue the real file should produce.

    The expected figures come from cell-by-cell inspection recorded in
    docs/PHASE1_REFERENCE_ANALYSIS.md: 12 distinct sizes, 23 variants
    (11 x HPFL115, 7 x HPFL135, 5 x HPFL160), 69 prices.
    """

    def test_structure_matches_the_documented_analysis(self):
        blocks, rows, terms = read_workbook(str(REAL_WORKBOOK))
        assert [b.header_row for b in blocks] == [2, 26]
        assert [b.row_count for b in blocks] == [11, 12]
        assert blocks[1].section_label == "alternative quality"
        assert len(rows) == 23
        assert all(r.ok for r in rows)

    def test_the_three_board_qualities_are_kept_distinct(self):
        _blocks, rows, _terms = read_workbook(str(REAL_WORKBOOK))
        qualities = {r.parsed.board_quality for r in rows}
        assert qualities == {
            "WT110 HPFL115 KM135",
            "WT110 HPFL135 KM135",
            "WT110 HPFL160 KM135",
        }

    def test_twenty_inch_exists_only_at_the_heaviest_quality(self):
        _blocks, rows, _terms = read_workbook(str(REAL_WORKBOOK))
        twenties = [r.parsed for r in rows if r.parsed.product == '20" White']
        assert len(twenties) == 1
        assert twenties[0].board_quality == "WT110 HPFL160 KM135"

    def test_it_imports_to_twelve_products_twentythree_variants_sixtynine_prices(
        self, session, seeded
    ):
        blocks, rows, terms = read_workbook(str(REAL_WORKBOOK))
        plan = build_plan(session, rows, blocks, terms, JAN)
        commit_plan(session, plan, None, REAL_WORKBOOK.name, "White Boxes B Flute")
        session.commit()

        assert catalogue_counts(session) == {
            "products": 12,
            "variants": 23,
            "prices": 69,
        }

    def test_reimporting_the_real_file_is_idempotent(self, session, seeded):
        for name in ("first.xlsx", "second.xlsx"):
            blocks, rows, terms = read_workbook(str(REAL_WORKBOOK))
            plan = build_plan(session, rows, blocks, terms, JAN)
            commit_plan(session, plan, None, name, "White Boxes B Flute")
            session.commit()

        assert catalogue_counts(session)["prices"] == 69
