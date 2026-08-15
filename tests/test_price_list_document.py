"""The customer price list.

It exists so the schedule a customer receives cannot drift from the catalogue.
The prices on it are read from ``product_prices`` for the exact variant, so the
tests that matter are the ones asserting that, and the one asserting nothing
internal comes with them.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from modules import price_list_document as pld
from tests.test_documents_and_approval import (  # noqa: F401
    admin, manager, sales, variant,
)

TODAY = dt.date(2026, 8, 14)


def _text_of(pdf: bytes) -> str:
    """The PDF's actual text.

    Not a search of the raw bytes. ReportLab compresses text streams, so a
    substring check against them finds nothing whatever the document says —
    which means an assertion that something is *absent* passes for the wrong
    reason, and would keep passing if costs were printed on every page. That
    is worse than having no assertion at all.
    """
    from io import BytesIO

    from pypdf import PdfReader

    return "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages
    )


@pytest.fixture
def catalogue(session, admin, variant):
    """Two board qualities of one size, priced differently."""
    from modules.catalogue_service import create_variant, set_price
    from modules.validation import PriceInput, VariantInput

    heavy = create_variant(
        session, admin, variant.product_id,
        VariantInput(
            variant_item_number="WB-12-HEAVY",
            board_quality="WTL125 FL135 IK135",
            case_pack=50,
        ),
    )
    session.flush()
    set_price(session, admin, PriceInput(
        product_variant_id=heavy.id, price_tier_code="STANDARD",
        price_per_pack=D("8.4142"), price_per_piece=D("0.168284"),
        effective_from=dt.date(2026, 8, 7),
    ))
    session.flush()
    return variant, heavy


class TestItReadsStoredPrices:
    def test_each_row_carries_the_price_stored_for_that_variant(
        self, session, catalogue
    ):
        from sqlalchemy import select

        from modules.models import PriceTier, ProductPrice

        listing = pld.build(session, reference="10001", issued_on=TODAY)
        tier = session.scalars(
            select(PriceTier).where(PriceTier.code == "STANDARD")).first()

        seen = 0
        for group in listing.groups:
            for row in group.rows:
                stored = session.scalars(
                    select(ProductPrice).where(
                        ProductPrice.price_tier_id == tier.id,
                        ProductPrice.effective_to.is_(None),
                    )
                ).all()
                match = [
                    p for p in stored
                    if p.variant.board_quality == row.quality
                    and p.variant.case_pack == row.case_pack
                ]
                assert match, f"no stored price for {row.quality}"
                assert row.price_per_pack == match[0].price_per_pack
                seen += 1
        assert seen == listing.row_count > 0

    def test_the_variant_is_identified_by_its_full_specification(
        self, session, catalogue
    ):
        """Size alone is not enough: two qualities of one size price apart."""
        listing = pld.build(session, reference="10001", issued_on=TODAY)
        qualities = {g.quality for g in listing.groups}
        assert "WTL125 FL135 IK135" in qualities
        assert len(qualities) >= 2

        rows = [r for g in listing.groups for r in g.rows]
        heavy = next(r for r in rows if r.quality == "WTL125 FL135 IK135")
        assert heavy.price_per_pack == D("8.4142")
        assert heavy.case_pack == 50
        assert heavy.flute == "B"

    def test_a_variant_with_no_price_in_force_is_omitted(
        self, session, admin, catalogue
    ):
        """A schedule is an offer; a row without a price is not one."""
        from modules.catalogue_service import create_variant
        from modules.validation import VariantInput

        create_variant(
            session, admin, catalogue[0].product_id,
            VariantInput(
                variant_item_number="WB-12-UNPRICED",
                board_quality="UNPRICED SPEC", case_pack=50,
            ),
        )
        session.flush()
        listing = pld.build(session, reference="10001", issued_on=TODAY)
        assert "UNPRICED SPEC" not in {g.quality for g in listing.groups}


class TestNothingInternalEscapes:
    def test_the_model_has_no_field_for_cost_or_margin(self):
        """Same guarantee document_model gives, asserted the same way."""
        fields = set(pld.PriceListRow.__dataclass_fields__)
        assert not {f for f in fields if "cost" in f or "margin" in f or "markup" in f}

    def test_the_rendered_bytes_carry_no_internal_figure(
        self, session, catalogue
    ):
        """The real assertion: against the produced PDF, not the model.

        8.4142 sells; 7.1917 is its original cost and 0.4667 the freight inside
        it. Neither may appear, nor the markup.
        """
        listing = pld.build(session, reference="10001", issued_on=TODAY)
        text = _text_of(pld.render(listing))
        for internal in ("7.1917", "0.4667", "markup", "Markup", "Original Cost"):
            assert internal not in text, f"{internal!r} reached the PDF"


class TestStorage:
    def test_it_is_kept_apart_from_accepted_quotation_artifacts(self):
        """``quotes/accepted/`` holds immutable evidence of one acceptance.

        A schedule is neither immutable nor about one customer, and mixing the
        two would put a re-issuable document in the namespace whose whole
        guarantee is that nothing in it changes.
        """
        assert pld.STORAGE_PREFIX == "price-lists/"
        assert not pld.STORAGE_PREFIX.startswith("quotes/")

    def test_the_key_is_stable_for_a_reference_and_revision(
        self, session, catalogue
    ):
        """Re-issuing replaces rather than accumulating.

        The schedule is the current offer; the history of what a price was
        lives in ``product_prices``, which is append-only and dated.
        """
        first = pld.build(
            session, reference="10001", issued_on=TODAY, revision_label="Revised")
        again = pld.build(
            session, reference="10001", issued_on=TODAY, revision_label="Revised")
        assert pld.storage_key(first) == pld.storage_key(again)
        assert pld.storage_key(first) == "price-lists/2026/08/10001_revised.pdf"

    def test_a_revision_gets_its_own_key(self, session, catalogue):
        original = pld.build(session, reference="10001", issued_on=TODAY)
        revised = pld.build(
            session, reference="10001", issued_on=TODAY, revision_label="Revised")
        assert pld.storage_key(original) != pld.storage_key(revised)
        assert pld.storage_key(original).endswith("10001_original.pdf")

    def test_publishing_stores_the_exact_bytes(self, session, catalogue, monkeypatch):
        written: dict[str, bytes] = {}

        class Fake:
            def put(self, key, data, content_type=None):  # noqa: ANN001
                written[key] = data
                return key

        from modules import storage

        monkeypatch.setattr(storage, "get_storage", lambda: Fake())
        listing = pld.build(
            session, reference="10001", issued_on=TODAY, revision_label="Revised")
        pdf = pld.render(listing)
        key = pld.publish(listing, pdf)
        assert written[key] == pdf

    def test_rendering_alone_writes_nothing(self, session, catalogue, monkeypatch):
        """A preview must not commit anything to storage."""
        from modules import storage

        def _boom():
            raise AssertionError("render() reached storage")

        monkeypatch.setattr(storage, "get_storage", _boom)
        pld.render(pld.build(session, reference="10001", issued_on=TODAY))


class TestTurkishRenders:
    def test_turkish_characters_survive_into_the_pdf(self, session, catalogue):
        """The base-14 fonts print s-cedilla and dotless-i as black boxes.

        The company is Turkish and its address contains both, so this reached
        every document the system produced until pdf_primitives registered a
        Unicode family.
        """
        from sqlalchemy import select

        from modules.models import CompanySettings

        company = session.execute(select(CompanySettings)).scalars().first()
        company.address_line1 = "Kayabaşı Mah., Veysel Karani Cad."
        company.city = "Başakşehir"
        company.country = "Türkiye"
        company.is_placeholder = False
        session.flush()

        text = _text_of(pld.render(
            pld.build(session, reference="10001", issued_on=TODAY)
        ))
        for word in ("Kayabaşı", "Başakşehir", "Türkiye"):
            assert word in text, f"{word!r} did not survive rendering"


class TestPresentation:
    def test_prices_print_to_four_places(self, session, catalogue):
        rows = [r for g in pld.build(
            session, reference="10001", issued_on=TODAY).groups for r in g.rows]
        for row in rows:
            pack = row.cells()[5]
            assert pack.startswith("$")
            assert len(pack.split(".")[1]) == 4, pack

    def test_a_price_on_an_exact_tie_rounds_half_up(self, session, admin, variant):
        """``Decimal.quantize`` defaults to half-*even*, the engine says half-up.

        A 17% markup on a six-decimal cost puts a 5 in the fifth place often
        enough to matter — three of the eighteen prices in the live catalogue
        land on an exact tie — and under the default they printed a hundredth
        of a cent below what the system holds. A document that rounds by a
        different rule than the quotation it is quoted against is a query
        waiting to happen.
        """
        from modules.catalogue_service import set_price
        from modules.validation import PriceInput

        set_price(session, admin, PriceInput(
            product_variant_id=variant.id, price_tier_code="STANDARD",
            price_per_pack=D("5.166850"), price_per_piece=D("0.103337"),
            effective_from=dt.date(2026, 8, 8),
        ))
        session.flush()

        listing = pld.build(session, reference="10001", issued_on=TODAY)
        row = next(
            r for g in listing.groups for r in g.rows
            if r.price_per_pack == D("5.166850")
        )
        assert row.cells()[5] == "$5.1669", "rounded half-even, not half-up"
        assert D("5.166850").quantize(D("0.0001")) == D("5.1668"), (
            "the default really is half-even; this test would be vacuous "
            "if that ever changed"
        )

    def test_it_is_marked_as_a_revision_when_one(self, session, catalogue):
        listing = pld.build(
            session, reference="10001", issued_on=TODAY, revision_label="Revised",
        )
        assert listing.is_revision
        text = _text_of(pld.render(listing))
        assert "Revised" in text

    def test_the_reference_is_preserved(self, session, catalogue):
        listing = pld.build(session, reference="10001", issued_on=TODAY)
        assert listing.reference == "10001"

    def test_it_renders_to_a_pdf(self, session, catalogue):
        pdf = pld.render(pld.build(session, reference="10001", issued_on=TODAY))
        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 1500

    def test_rows_are_ordered_by_size(self, session, admin, catalogue):
        listing = pld.build(session, reference="10001", issued_on=TODAY)
        for group in listing.groups:
            sizes = [r.product for r in group.rows]
            assert sizes == sorted(
                sizes,
                key=lambda s: float("".join(c for c in s if c.isdigit() or c == ".") or 0),
            )

    def test_a_schedule_has_no_totals(self, session, catalogue):
        """It carries no quantities, so there is nothing to total.

        Money on a quotation comes from document_model; this is unit prices.
        """
        listing = pld.build(session, reference="10001", issued_on=TODAY)
        assert not hasattr(listing, "grand_total")
        assert not hasattr(listing, "subtotal")
