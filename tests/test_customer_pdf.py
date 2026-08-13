"""The customer PDF: what it may contain, who may fetch it, and what survives.

Three claims under test, in order of how much damage getting them wrong would
do:

* an internal figure cannot reach the produced bytes — not through the model,
  not through the renderer, not through a formatted string;
* a download is read-only, purpose-bound and cannot become an approval;
* an accepted document is produced once and never rewritten, and bytes that
  stop matching what was recorded are refused rather than served.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import fields, is_dataclass
from decimal import Decimal as D

import pytest
from starlette.testclient import TestClient

from modules import (
    portal_service,
    pricing_snapshot,
    quotation_service,
    quote_document_service,
)
from modules.constants import (
    ArtifactStatus,
    DocumentJobStatus,
    ItemInclusion,
    PriceTierCode,
    QuotationStatus,
    QuoteEventType,
)
from modules.models import (
    ImmutableRecordError,
    QuoteDocumentArtifact,
    QuoteDocumentJob,
    QuoteEvent,
)
from modules.storage import StorageError, get_storage
from portal import pdf_model, pdf_renderer
from portal.pdf_model import FORBIDDEN_FIELDS

from tests.test_documents_and_approval import (  # noqa: F401
    _approve_and_issue,
    admin,
    manager,
    quotation,
    sales,
    variant,
)

SECRET_COST = "5.4321"
SECRET_NOTE = "INTERNAL-ONLY-MARGIN-NOTE-DO-NOT-SHOW"
SECRET_REMARK = "INTERNAL-REMARK-SUPPLIER-DISCOUNT"


@pytest.fixture(autouse=True)
def portal_user(session):
    return portal_service.ensure_portal_user(session)


@pytest.fixture(autouse=True)
def clean_artifact_storage():
    """Empty the artifact namespace before each test.

    The storage root outlives the database, which ``clean_database`` drops and
    recreates per test. Response ids therefore restart at 1 while the objects
    keyed on them survive — so without this a stale document from the previous
    test is found at the key this test computes and adopted as its own, and any
    test that patches the renderer silently never reaches it.

    A test-isolation problem only: in production response ids are allocated
    once and never reused, which is what makes the key safe to derive from them.
    """
    import shutil

    from modules.config import get_settings

    root = get_settings().local_storage_root / "quotes"
    shutil.rmtree(root, ignore_errors=True)
    yield
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def offered(session, quotation, sales, variant):
    """A quotation with one optional and one recommended line, plus secrets."""
    for label, inclusion in (
        ("Two-colour print", ItemInclusion.OPTIONAL),
        ("Export-grade board", ItemInclusion.RECOMMENDED),
    ):
        line = quotation_service.add_line(
            session, sales, quotation,
            product_variant_id=variant.id,
            price_tier_code=PriceTierCode.STANDARD.value,
            quantity_packs=D("100"),
            description_override=label,
        )
        line.inclusion = inclusion

    quotation.internal_notes = SECRET_NOTE
    quotation.total_cost = D("1234.56")
    quotation.gross_profit = D("999.99")
    quotation.gross_margin_pct = D("42.4242")
    first = quotation.items[0]
    first.internal_remarks = SECRET_REMARK
    first.unit_cost_per_pack = D(SECRET_COST)
    first.line_cost_total = D("543.21")
    session.flush()
    quotation_service.recompute_totals(session, quotation)
    session.flush()
    return quotation


@pytest.fixture
def sent(session, offered, sales, manager):
    _approve_and_issue(session, offered, sales, manager)
    offered.contact_name = "Dana Buyer"
    offered.contact_email = "dana@example.invalid"
    quotation_service.change_status(
        session, manager, offered, QuotationStatus.SENT_TO_CUSTOMER
    )
    session.flush()
    return offered


@pytest.fixture
def link(session, sent, sales):
    token, raw = portal_service.issue_token(session, sales, sent)
    session.commit()
    return token, raw


@pytest.fixture
def client(session, monkeypatch):
    from contextlib import contextmanager

    from portal import app as portal_app

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(portal_app, "session_scope", _scope)
    portal_app._view_limiter.reset()
    portal_app._submit_limiter.reset()
    portal_app._download_limiter.reset()
    return TestClient(portal_app.app, base_url="http://testserver")


def _download_fields(token, quotation) -> dict:
    nonce, signature = portal_service.issue_download_nonce(token, quotation)
    return {"nonce": nonce, "signature": signature}


def _text_of(data: bytes) -> str:
    from pypdf import PdfReader
    from io import BytesIO

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# --------------------------------------------------------------------------- #
# The model is an allowlist
# --------------------------------------------------------------------------- #

class TestStrictAllowlist:
    def test_no_pdf_dataclass_declares_a_forbidden_field(self):
        """Structural: the shapes have nowhere to put a cost."""
        shapes = [
            value for value in vars(pdf_model).values()
            if isinstance(value, type) and is_dataclass(value)
        ]
        assert shapes, "no dataclasses found — the check would pass vacuously"
        for shape in shapes:
            names = {f.name for f in fields(shape)}
            leaked = names & FORBIDDEN_FIELDS
            assert not leaked, f"{shape.__name__} exposes {leaked}"

    def test_the_field_set_is_pinned(self):
        """Adding a field to the customer document must be a deliberate act.

        Pinned by name, so a new ORM column cannot arrive here by being
        forwarded from somewhere — the test fails and somebody has to decide
        that a customer should see it.
        """
        assert {f.name for f in fields(pdf_model.CustomerPdfDocument)} == {
            "kind", "quote_number", "revision_label", "quote_date", "valid_until",
            "currency", "title", "scope_note", "company", "customer", "lines",
            "totals", "terms", "customer_notes", "shipping", "acceptance",
            "deposit_due", "deposit_pct", "sales_representative", "legal_footer",
            "thank_you_text", "generator_version",
        }
        assert {f.name for f in fields(pdf_model.PdfLine)} == {
            "line_no", "description", "specification", "size", "pack_size",
            "quantity_packs", "quantity_pieces", "unit_price", "line_total",
            "inclusion", "inclusion_label", "is_selected", "remarks",
        }

    def test_every_shape_is_frozen(self):
        """A document that can be edited after it is built is not a record."""
        for value in vars(pdf_model).values():
            if isinstance(value, type) and is_dataclass(value):
                assert value.__dataclass_params__.frozen, value.__name__

    def test_money_on_the_model_is_decimal_not_text(self, session, offered):
        document = pdf_model.build_draft(offered, pricing_snapshot.base(offered))
        for total in document.totals:
            assert isinstance(total.amount, D)
        for line in document.lines:
            assert isinstance(line.line_total, D)
            assert isinstance(line.unit_price, D)

    def test_no_internal_value_reaches_the_rendered_bytes(self, session, offered):
        document = pdf_model.build_draft(offered, pricing_snapshot.base(offered))
        data = pdf_renderer.render(document)
        text = _text_of(data)

        for secret in (SECRET_NOTE, SECRET_REMARK, SECRET_COST, "42.4242", "999.99"):
            assert secret not in text
            # Also absent from the raw stream, not merely from extracted text.
            assert secret.encode() not in data

    def test_the_customer_renderer_refuses_an_employee_document(
        self, session, offered
    ):
        """The internal model must not be renderable by the customer path."""
        from modules import document_model

        internal = document_model.build_document(session, offered)
        with pytest.raises((AttributeError, TypeError)):
            pdf_renderer.render(internal)


# --------------------------------------------------------------------------- #
# One pricing path, including through the PDF
# --------------------------------------------------------------------------- #

class TestPricingEquivalence:
    def test_employee_portal_and_pdf_agree(self, session, offered):
        snapshot = pricing_snapshot.base(offered)
        document = pdf_model.build_draft(offered, snapshot)
        grand = next(t for t in document.totals if t.emphasis)

        assert grand.amount == snapshot.grand_total
        assert grand.amount == offered.grand_total       # the stored base total
        assert grand.amount == portal_service.compute_selection_totals(
            offered, []
        ).grand_total

    def test_the_pdf_reprices_for_a_selection(self, session, offered):
        optional = next(
            i for i in offered.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        chosen = pricing_snapshot.selected(offered, [optional.id])
        document = pdf_model.build_draft(offered, chosen)
        grand = next(t for t in document.totals if t.emphasis)

        assert grand.amount == chosen.grand_total
        assert grand.amount > offered.grand_total
        picked = [ln for ln in document.lines if ln.is_selected]
        assert len(picked) == len(offered.items) - 1     # all but the recommended

    def test_all_options_scope_reaches_the_pdf(self, session, offered):
        ceiling = pricing_snapshot.all_options(offered)
        document = pdf_model.build_draft(offered, ceiling)
        grand = next(t for t in document.totals if t.emphasis)
        assert grand.amount == ceiling.grand_total
        assert all(line.is_selected for line in document.lines)

    def test_unselected_lines_show_no_line_total(self, session, offered):
        document = pdf_model.build_draft(offered, pricing_snapshot.base(offered))
        data = pdf_renderer.render(document)
        text = _text_of(data)
        # The em dash stands in for the amount on a line not being charged for.
        assert "not added" in text

    @pytest.mark.parametrize(
        ("currency", "rate"), [("USD", D("0")), ("CAD", D("13"))]
    )
    def test_currency_and_tax_print_correctly(self, session, offered, currency, rate):
        offered.currency = currency
        offered.tax_rate_pct = rate
        session.flush()
        quotation_service.recompute_totals(session, offered)
        session.flush()

        snapshot = pricing_snapshot.base(offered)
        document = pdf_model.build_draft(offered, snapshot)
        text = _text_of(pdf_renderer.render(document))

        assert f"TOTAL ({currency})" in text.upper()
        if rate:
            # "Tax (13%)", never "Tax (13.000000%)".
            assert "Tax (13%)" in text
            assert snapshot.tax_amount > 0
        else:
            assert "Tax (" not in text

    def test_the_deposit_is_not_a_component_of_the_total(self, session, offered):
        """It is a share of the total, and must not read as an addition to it."""
        offered.deposit_pct = D("25")
        session.flush()
        snapshot = pricing_snapshot.base(offered)
        document = pdf_model.build_draft(offered, snapshot)

        assert document.deposit_due == snapshot.deposit_due
        assert "Deposit" not in {t.label for t in document.totals}

        text = _text_of(pdf_renderer.render(document))
        assert "Deposit due on order" in text


# --------------------------------------------------------------------------- #
# The download capability
# --------------------------------------------------------------------------- #

class TestDownloadNonce:
    def test_a_download_nonce_verifies_for_its_own_token_and_revision(
        self, session, link, sent
    ):
        token, _raw = link
        nonce, signature = portal_service.issue_download_nonce(token, sent)
        portal_service.verify_download_nonce(token, sent, nonce, signature)

    def test_it_names_its_purpose(self, session, link, sent):
        token, _raw = link
        nonce, _ = portal_service.issue_download_nonce(token, sent)
        assert nonce.startswith(f"{portal_service.DOWNLOAD_PURPOSE}.")

    def test_it_expires(self, session, link, sent):
        token, _raw = link
        nonce, signature = portal_service.issue_download_nonce(token, sent)
        later = dt.datetime.now(dt.UTC) + dt.timedelta(
            seconds=portal_service.DOWNLOAD_NONCE_TTL_SECONDS + 60
        )
        with pytest.raises(portal_service.PortalError):
            portal_service.verify_download_nonce(
                token, sent, nonce, signature, now=later
            )

    def test_it_is_reusable_while_it_lasts(self, session, link, sent):
        """Downloading is read-only, so a second use is not a replay."""
        token, _raw = link
        nonce, signature = portal_service.issue_download_nonce(token, sent)
        for _ in range(3):
            portal_service.verify_download_nonce(token, sent, nonce, signature)

    def test_it_is_rejected_against_another_token(
        self, session, link, sent, sales
    ):
        token, _raw = link
        other, _other_raw = portal_service.issue_token(session, sales, sent)
        nonce, signature = portal_service.issue_download_nonce(token, sent)

        with pytest.raises(portal_service.PortalError):
            portal_service.verify_download_nonce(other, sent, nonce, signature)

    def test_it_is_rejected_against_another_revision(self, session, link, sent):
        token, _raw = link
        nonce, signature = portal_service.issue_download_nonce(token, sent)
        # Stand in a quotation reporting a different revision. The signature
        # covers the revision, so it cannot be moved to another one.
        sent.revision_no = sent.revision_no + 1
        with pytest.raises(portal_service.PortalError):
            portal_service.verify_download_nonce(token, sent, nonce, signature)

    def test_a_download_nonce_cannot_approve(self, session, link, sent):
        token, _raw = link
        nonce, signature = portal_service.issue_download_nonce(token, sent)
        with pytest.raises(portal_service.PortalError):
            portal_service.verify_submission_nonce(token, nonce, signature)

    def test_an_approval_nonce_cannot_download(self, session, link, sent):
        token, _raw = link
        nonce, signature = portal_service.issue_submission_nonce(token)
        with pytest.raises(portal_service.PortalError):
            portal_service.verify_download_nonce(token, sent, nonce, signature)

    def test_a_forged_signature_is_refused(self, session, link, sent):
        token, _raw = link
        nonce, _ = portal_service.issue_download_nonce(token, sent)
        with pytest.raises(portal_service.PortalError):
            portal_service.verify_download_nonce(token, sent, nonce, "0" * 64)

    def test_a_malformed_nonce_is_refused(self, session, link, sent):
        token, _raw = link
        for bad in ("", "download", "download.0", "a.b.c.d.e", "x" * 200):
            with pytest.raises(portal_service.PortalError):
                portal_service.verify_download_nonce(token, sent, bad, "deadbeef")


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

class TestDownloadRoute:
    def test_a_valid_download_returns_a_pdf(self, session, client, link, sent):
        token, raw = link
        response = client.post(
            f"/quote/public/{raw}/download.pdf", data=_download_fields(token, sent)
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF-")
        assert int(response.headers["content-length"]) == len(response.content)

    def test_the_headers_make_a_browser_save_it(self, session, client, link, sent):
        token, raw = link
        response = client.post(
            f"/quote/public/{raw}/download.pdf", data=_download_fields(token, sent)
        )
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert "no-store" in response.headers["cache-control"]
        assert "private" in response.headers["cache-control"]

    def test_the_filename_is_safe(self, session, client, link, sent):
        """The name is built from our own numbering, never from customer input."""
        sent.customer_name_snapshot = 'Evil"; rm -rf /\\..\\..\\etc'
        session.flush()
        token, raw = link
        response = client.post(
            f"/quote/public/{raw}/download.pdf", data=_download_fields(token, sent)
        )
        disposition = response.headers["content-disposition"]
        assert ".." not in disposition
        assert "/" not in disposition.split("filename=")[1]
        assert "\\" not in disposition.split("filename=")[1]
        assert disposition.endswith('.pdf"')

    def test_a_download_records_no_view(self, session, client, link, sent):
        token, raw = link
        before = token.view_count
        client.post(
            f"/quote/public/{raw}/download.pdf", data=_download_fields(token, sent)
        )
        session.flush()

        assert token.view_count == before
        assert token.first_viewed_at is None
        kinds = [e.event_type for e in session.query(QuoteEvent).all()]
        assert QuoteEventType.VIEWED not in kinds
        assert QuoteEventType.PDF_DOWNLOADED in kinds

    def test_a_download_does_not_persist_the_selection(
        self, session, client, link, sent
    ):
        from portal.projection import line_ref

        token, raw = link
        optional = next(
            i for i in sent.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        payload = _download_fields(token, sent)
        payload["selected"] = line_ref(token.token_hash, optional.id)

        client.post(f"/quote/public/{raw}/download.pdf", data=payload)
        session.flush()

        assert sent.portal_responses == []
        # And the stored base total is untouched by a temporary selection.
        assert sent.grand_total == pricing_snapshot.base(sent).grand_total

    def test_the_selection_changes_the_document(self, session, client, link, sent):
        from portal.projection import line_ref

        token, raw = link
        optional = next(
            i for i in sent.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        plain = client.post(
            f"/quote/public/{raw}/download.pdf", data=_download_fields(token, sent)
        )
        payload = _download_fields(token, sent)
        payload["selected"] = line_ref(token.token_hash, optional.id)
        picked = client.post(f"/quote/public/{raw}/download.pdf", data=payload)

        chosen_total = pricing_snapshot.selected(sent, [optional.id]).grand_total
        assert f"{chosen_total:,.2f}" in _text_of(picked.content)
        assert f"{chosen_total:,.2f}" not in _text_of(plain.content)

    def test_unknown_and_duplicate_references_are_ignored(
        self, session, client, link, sent
    ):
        """One documented policy: drop what does not apply, never error.

        A stale form should reprice, not become an error page for somebody who
        did nothing wrong.
        """
        from portal.projection import line_ref

        token, raw = link
        optional = next(
            i for i in sent.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        ref = line_ref(token.token_hash, optional.id)

        honest = _download_fields(token, sent)
        honest["selected"] = ref
        expected = client.post(f"/quote/public/{raw}/download.pdf", data=honest)

        tampered = _download_fields(token, sent)
        tampered["selected"] = [
            ref,
            ref,                     # duplicate
            "0" * 16,                # unknown
            "../../etc/passwd",      # nonsense
            "",                      # empty
        ]
        response = client.post(f"/quote/public/{raw}/download.pdf", data=tampered)
        assert response.status_code == 200
        chosen_total = pricing_snapshot.selected(sent, [optional.id]).grand_total
        assert f"{chosen_total:,.2f}" in _text_of(response.content)
        assert len(_text_of(response.content)) == len(_text_of(expected.content))

    def test_a_reference_from_another_quotation_is_ignored(
        self, session, client, link, sent, sales, manager, variant
    ):
        """Refs are derived from the token hash, so they do not travel."""
        from portal.projection import line_ref

        token, raw = link
        other, _other_raw = portal_service.issue_token(session, sales, sent)
        optional = next(
            i for i in sent.items if i.inclusion is ItemInclusion.OPTIONAL
        )
        foreign_ref = line_ref(other.token_hash, optional.id)

        payload = _download_fields(token, sent)
        payload["selected"] = foreign_ref
        response = client.post(f"/quote/public/{raw}/download.pdf", data=payload)

        base_total = pricing_snapshot.base(sent).grand_total
        assert f"{base_total:,.2f}" in _text_of(response.content)

    def test_a_missing_nonce_is_refused(self, session, client, link):
        _token, raw = link
        response = client.post(f"/quote/public/{raw}/download.pdf", data={})
        assert response.status_code == 403
        assert "application/pdf" not in response.headers["content-type"]

    def test_an_unknown_token_gets_the_generic_page(self, session, client, link, sent):
        token, _raw = link
        response = client.post(
            "/quote/public/not-a-real-token-value-at-all/download.pdf",
            data=_download_fields(token, sent),
        )
        assert response.status_code == 404
        assert "not available" in response.text

    def test_a_revoked_token_cannot_download(
        self, session, client, link, sent, sales
    ):
        token, raw = link
        payload = _download_fields(token, sent)
        portal_service.revoke_token(session, sales, token)
        session.flush()

        response = client.post(f"/quote/public/{raw}/download.pdf", data=payload)
        assert response.status_code == 404

    def test_a_get_does_not_download(self, session, client, link):
        _token, raw = link
        response = client.get(
            f"/quote/public/{raw}/download.pdf", follow_redirects=False
        )
        assert response.status_code == 303

    def test_the_button_appears_on_a_live_quotation(self, session, client, link):
        _token, raw = link
        page = client.get(f"/quote/public/{raw}")
        assert "download.pdf" in page.text
        assert "Download PDF" in page.text

    def test_downloads_are_rate_limited(self, session, client, link, sent):
        from portal import app as portal_app

        token, raw = link
        payload = _download_fields(token, sent)
        limit = portal_app._download_limiter.limit
        codes = {
            client.post(f"/quote/public/{raw}/download.pdf", data=payload).status_code
            for _ in range(limit + 3)
        }
        assert 429 in codes


# --------------------------------------------------------------------------- #
# The accepted artifact
# --------------------------------------------------------------------------- #

@pytest.fixture
def accepted(session, link, sent):
    """An acceptance, with the optional line taken."""
    token, raw = link
    optional = next(i for i in sent.items if i.inclusion is ItemInclusion.OPTIONAL)
    response = portal_service.approve(
        session, token,
        customer_name="Dana Buyer", job_title="Procurement Lead",
        customer_email="dana@example.invalid", signature_name="Dana R. Buyer",
        accepted_terms=True, selected_ids=[optional.id],
    )
    # Committed, because that is the state the real background task starts
    # from: the acceptance commits, the HTTP response goes out, then the job
    # runs. process_job owns its transaction and rolls back on failure.
    session.commit()
    return response, token, raw


class TestAcceptedArtifact:
    def test_acceptance_creates_a_job_in_the_same_transaction(
        self, session, accepted
    ):
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        assert job is not None
        assert job.status is DocumentJobStatus.PENDING
        assert job.revision_no == response.revision_no

    def test_enqueueing_twice_is_a_no_op(self, session, accepted):
        response, _token, _raw = accepted
        first = quote_document_service.enqueue(session, response)
        second = quote_document_service.enqueue(session, response)
        session.flush()
        assert first.id == second.id
        assert session.query(QuoteDocumentJob).count() == 1

    def test_processing_produces_a_verifiable_artifact(self, session, accepted):
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)

        assert quote_document_service.process_job(session, job) is (
            DocumentJobStatus.READY
        )
        artifact = quote_document_service.artifact_for_response(session, response.id)

        assert artifact is not None
        assert artifact.status is ArtifactStatus.READY
        assert artifact.byte_size > 0
        assert artifact.generator_version == pdf_model.GENERATOR_VERSION

        data = quote_document_service.verify(session, artifact)
        assert data.startswith(b"%PDF-")
        assert len(data) == artifact.byte_size
        assert hashlib.sha256(data).hexdigest() == artifact.sha256

    def test_the_accepted_pdf_records_what_was_agreed(self, session, accepted):
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service.process_job(session, job)
        artifact = quote_document_service.artifact_for_response(session, response.id)
        text = _text_of(quote_document_service.verify(session, artifact))

        assert "ACCEPTED" in text.upper()
        assert f"Rev {response.revision_no}" in text
        assert response.customer_name in text
        assert response.job_title in text
        assert response.customer_email in text
        assert response.signature_name in text            # the signature itself
        assert f"{response.grand_total:,.2f}" in text
        assert f"{response.subtotal:,.2f}" in text

    def test_the_accepted_total_is_taken_from_the_response(self, session, accepted):
        """Not repriced. A later change must not restate what was signed."""
        response, _token, _raw = accepted
        response.grand_total = D("1.23")
        response.subtotal = D("1.00")
        session.flush()

        document = pdf_model.build_accepted(
            session.get(type(response).quotation.property.mapper.class_,
                        response.quotation_id),
            response,
            pricing_snapshot.selected(
                session.get(
                    type(response).quotation.property.mapper.class_,
                    response.quotation_id,
                ),
                list(response.selected_item_ids or []),
            ),
        )
        grand = next(t for t in document.totals if t.emphasis)
        assert grand.amount == D("1.23")

    def test_the_key_is_derived_from_our_own_identifiers(self, session, accepted):
        response, _token, _raw = accepted
        key = quote_document_service.artifact_key(response)

        assert key.startswith(quote_document_service.ARTIFACT_NAMESPACE)
        assert str(response.id) in key
        assert quote_document_service.is_within_artifact_namespace(key)
        # Nothing the customer typed appears in it.
        assert "Dana" not in key

    def test_the_namespace_guard_refuses_a_stray_key(self):
        for bad in ("", "branding/logo.png", "quotes/accepted/../secrets",
                    "/quotes/accepted/x", "quotes/accepted/a\\b"):
            assert not quote_document_service.is_within_artifact_namespace(bad)

    def test_an_artifact_cannot_be_rewritten(self, session, accepted):
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service.process_job(session, job)
        artifact = quote_document_service.artifact_for_response(session, response.id)

        for field_name, value in (
            ("sha256", "0" * 64),
            ("byte_size", 1),
            ("storage_key", "quotes/accepted/somewhere/else.pdf"),
            ("generator_version", "tampered"),
        ):
            setattr(artifact, field_name, value)
            with pytest.raises(ImmutableRecordError):
                session.flush()
            session.rollback()

    def test_quarantining_is_allowed(self, session, accepted):
        """The one permitted change: marking a row unservable."""
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service.process_job(session, job)
        artifact = quote_document_service.artifact_for_response(session, response.id)

        artifact.status = ArtifactStatus.QUARANTINED
        artifact.quarantine_reason = "under investigation"
        session.flush()      # no exception
        assert artifact.status is ArtifactStatus.QUARANTINED

    def test_reprocessing_reuses_the_artifact(self, session, accepted):
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)

        quote_document_service.process_job(session, job)
        first = quote_document_service.artifact_for_response(session, response.id)
        original = (first.id, first.sha256, first.storage_key)

        quote_document_service.process_job(session, job)
        again = quote_document_service.artifact_for_response(session, response.id)

        assert (again.id, again.sha256, again.storage_key) == original
        assert session.query(QuoteDocumentArtifact).count() == 1


class TestIntegrityVerification:
    def _ready(self, session, accepted):
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service.process_job(session, job)
        return response, quote_document_service.artifact_for_response(
            session, response.id
        )

    def test_a_missing_object_is_refused_and_quarantined(self, session, accepted):
        response, artifact = self._ready(session, accepted)
        get_storage().delete(artifact.storage_key)

        with pytest.raises(quote_document_service.ArtifactIntegrityError):
            quote_document_service.verify(session, artifact)
        assert artifact.status is ArtifactStatus.QUARANTINED

    def test_corrupt_bytes_are_refused_and_quarantined(self, session, accepted):
        response, artifact = self._ready(session, accepted)
        # Same length, different content: only the hash can catch this.
        spoiled = b"X" * artifact.byte_size
        get_storage().put(artifact.storage_key, spoiled, "application/pdf")

        with pytest.raises(quote_document_service.ArtifactIntegrityError):
            quote_document_service.verify(session, artifact)
        assert artifact.status is ArtifactStatus.QUARANTINED
        assert "sha256" in artifact.quarantine_reason

    def test_a_truncated_object_is_refused(self, session, accepted):
        response, artifact = self._ready(session, accepted)
        get_storage().put(artifact.storage_key, b"%PDF-short", "application/pdf")

        with pytest.raises(quote_document_service.ArtifactIntegrityError):
            quote_document_service.verify(session, artifact)
        assert "size mismatch" in artifact.quarantine_reason

    def test_a_quarantined_artifact_stays_refused(self, session, accepted):
        response, artifact = self._ready(session, accepted)
        artifact.status = ArtifactStatus.QUARANTINED
        session.flush()
        with pytest.raises(quote_document_service.ArtifactIntegrityError):
            quote_document_service.verify(session, artifact)

    def test_the_route_refuses_a_corrupt_document(
        self, session, client, accepted
    ):
        response, artifact = self._ready(session, accepted)
        get_storage().put(
            artifact.storage_key, b"Y" * artifact.byte_size, "application/pdf"
        )
        _resp, token, raw = accepted

        page = client.post(
            f"/quote/public/{raw}/download.pdf",
            data=_download_fields(token, session.get(
                type(response).quotation.property.mapper.class_, response.quotation_id
            )),
        )
        assert page.status_code == 202
        assert "application/pdf" not in page.headers["content-type"]
        assert "being prepared" in page.text
        # And the customer is told nothing about why.
        assert "sha256" not in page.text
        assert "checksum" not in page.text.lower()


class TestDurableRetry:
    def test_a_failed_render_leaves_the_quote_accepted(
        self, session, accepted, monkeypatch
    ):
        response, _token, _raw = accepted
        quotation = session.get(
            type(response).quotation.property.mapper.class_, response.quotation_id
        )
        monkeypatch.setattr(
            quote_document_service, "build_accepted_pdf",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("renderer down")),
        )
        job = quote_document_service.job_for_response(session, response.id)

        assert quote_document_service.process_job(session, job) is (
            DocumentJobStatus.PENDING
        )
        assert quotation.status is QuotationStatus.ACCEPTED
        assert response.grand_total > 0
        assert quote_document_service.artifact_for_response(
            session, response.id
        ) is None

    def test_a_storage_failure_leaves_the_job_retryable(
        self, session, accepted, monkeypatch
    ):
        response, _token, _raw = accepted
        storage = get_storage()
        monkeypatch.setattr(
            type(storage), "put",
            lambda self, *a, **k: (_ for _ in ()).throw(StorageError("bucket down")),
        )
        job = quote_document_service.job_for_response(session, response.id)

        assert quote_document_service.process_job(session, job) is (
            DocumentJobStatus.PENDING
        )
        session.refresh(job)
        assert job.attempts == 1
        assert job.last_error

    def test_retries_are_bounded(self, session, accepted, monkeypatch):
        response, _token, _raw = accepted
        monkeypatch.setattr(
            quote_document_service, "build_accepted_pdf",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("still down")),
        )
        job = quote_document_service.job_for_response(session, response.id)

        for _ in range(quote_document_service.MAX_ATTEMPTS):
            quote_document_service.process_job(session, job)
            session.refresh(job)

        assert job.attempts == quote_document_service.MAX_ATTEMPTS
        assert job.status is DocumentJobStatus.FAILED

    def test_a_retry_succeeds_once_the_backend_returns(
        self, session, accepted, monkeypatch
    ):
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)

        monkeypatch.setattr(
            quote_document_service, "build_accepted_pdf",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
        )
        quote_document_service.process_job(session, job)
        session.refresh(job)
        assert job.status is DocumentJobStatus.PENDING

        monkeypatch.undo()
        assert quote_document_service.process_job(session, job) is (
            DocumentJobStatus.READY
        )

    def test_a_stored_but_unrecorded_object_is_resumed_from(
        self, session, accepted, monkeypatch
    ):
        """A crash between the put and the commit must cost one render, not two.

        The key is deterministic, so bytes with no row are not litter — they
        are where the next attempt picks up. Queueing them for deletion would
        throw away exactly what makes the retry cheap and identical.
        """
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        key = quote_document_service.artifact_key(response)

        real_flush = type(session).flush
        state = {"armed": True}

        def explode(self, *args, **kwargs):
            # Fail only the flush that records the artifact row, after the
            # object has already been written.
            if state["armed"] and any(
                isinstance(o, QuoteDocumentArtifact) for o in self.new
            ):
                state["armed"] = False
                raise RuntimeError("database went away")
            return real_flush(self, *args, **kwargs)

        monkeypatch.setattr(type(session), "flush", explode)
        quote_document_service.process_job(session, job)
        monkeypatch.undo()

        # The bytes survived the failure.
        stored = get_storage().get(key)
        assert stored.startswith(b"%PDF-")
        assert quote_document_service.artifact_for_response(
            session, response.id
        ) is None

        # And the retry adopts them rather than rendering a second document.
        def must_not_render(*_args, **_kwargs):
            raise AssertionError("re-rendered instead of resuming")

        monkeypatch.setattr(
            quote_document_service, "build_accepted_pdf", must_not_render
        )
        session.refresh(job)
        job.status = DocumentJobStatus.PENDING
        session.commit()

        assert quote_document_service.process_job(session, job) is (
            DocumentJobStatus.READY
        )
        artifact = quote_document_service.artifact_for_response(session, response.id)
        assert artifact.sha256 == hashlib.sha256(stored).hexdigest()

    def test_an_employee_retry_resets_the_count(self, session, accepted, monkeypatch):
        response, _token, _raw = accepted
        monkeypatch.setattr(
            quote_document_service, "build_accepted_pdf",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
        )
        job = quote_document_service.job_for_response(session, response.id)
        for _ in range(quote_document_service.MAX_ATTEMPTS):
            quote_document_service.process_job(session, job)
            session.refresh(job)
        assert job.status is DocumentJobStatus.FAILED

        monkeypatch.undo()
        state = quote_document_service.retry(session, response.id)
        session.refresh(job)

        assert state.status is DocumentJobStatus.PENDING
        assert job.attempts == 0
        assert job.last_error is None


class TestConcurrentWorkers:
    def test_a_claimed_job_is_skipped(self, session, accepted):
        """The lease stops two workers rendering the same document."""
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)

        assert quote_document_service._claim(session, job, "worker-a") is True
        assert quote_document_service._claim(session, job, "worker-b") is False

    def test_a_stale_lease_is_reclaimed(self, session, accepted):
        """A worker killed mid-job must not strand it forever."""
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service._claim(session, job, "worker-a")

        job.locked_at = dt.datetime.now(dt.UTC) - dt.timedelta(
            seconds=quote_document_service.LEASE_SECONDS + 60
        )
        session.flush()
        assert quote_document_service._claim(session, job, "worker-b") is True

    def test_the_loser_of_a_race_adopts_the_published_artifact(
        self, session, accepted
    ):
        """Two workers, one row. The second must not publish a rival document."""
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)

        quote_document_service.process_job(session, job)
        published = quote_document_service.artifact_for_response(session, response.id)

        # A second worker starting from scratch: release the lease and clear
        # the terminal status so it genuinely tries again.
        job.status = DocumentJobStatus.PENDING
        job.lock_owner = None
        job.locked_at = None
        session.flush()

        assert quote_document_service.process_job(session, job) is (
            DocumentJobStatus.READY
        )
        assert session.query(QuoteDocumentArtifact).count() == 1
        assert quote_document_service.artifact_for_response(
            session, response.id
        ).id == published.id

    def test_existing_bytes_are_adopted_rather_than_re_rendered(
        self, session, accepted, monkeypatch
    ):
        """A crash between put and commit must not produce a second document."""
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        key = quote_document_service.artifact_key(response)

        planted = b"%PDF-1.4 planted by an earlier attempt"
        get_storage().put(key, planted, "application/pdf")

        def must_not_render(*_args, **_kwargs):
            raise AssertionError("re-rendered instead of adopting existing bytes")

        monkeypatch.setattr(
            quote_document_service, "build_accepted_pdf", must_not_render
        )
        assert quote_document_service.process_job(session, job) is (
            DocumentJobStatus.READY
        )

        artifact = quote_document_service.artifact_for_response(session, response.id)
        assert artifact.byte_size == len(planted)
        assert artifact.sha256 == hashlib.sha256(planted).hexdigest()


class TestReconciliation:
    def test_an_acceptance_without_a_job_gets_one(self, session, accepted):
        response, _token, _raw = accepted
        session.query(QuoteDocumentJob).delete()
        session.flush()
        assert quote_document_service.job_for_response(session, response.id) is None

        added = quote_document_service.reconcile(session)

        assert added == 1
        job = quote_document_service.job_for_response(session, response.id)
        assert job is not None
        assert job.status is DocumentJobStatus.PENDING

    def test_reconciliation_is_idempotent(self, session, accepted):
        assert quote_document_service.reconcile(session) == 0
        assert quote_document_service.reconcile(session) == 0
        assert session.query(QuoteDocumentJob).count() == 1

    def test_a_change_request_is_never_enqueued(
        self, session, link, sent
    ):
        token, _raw = link
        response = portal_service.request_changes(
            session, token, customer_name="Dana", comment="Cheaper please",
        )
        session.flush()

        assert quote_document_service.enqueue(session, response) is None
        assert quote_document_service.reconcile(session) == 0
        assert session.query(QuoteDocumentJob).count() == 0


class TestEmployeeVisibleState:
    def test_states_are_reported_without_internal_detail(self, session, accepted):
        response, _token, _raw = accepted
        state = quote_document_service.state_for_response(session, response.id)
        assert state.status is DocumentJobStatus.PENDING
        assert not hasattr(state, "last_error")

        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service.process_job(session, job)

        ready = quote_document_service.state_for_response(session, response.id)
        assert ready.status is DocumentJobStatus.READY
        assert ready.is_ready
        assert ready.byte_size > 0

    def test_a_quarantined_document_reads_as_failed(self, session, accepted):
        response, _token, _raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service.process_job(session, job)
        artifact = quote_document_service.artifact_for_response(session, response.id)
        artifact.status = ArtifactStatus.QUARANTINED
        session.flush()

        state = quote_document_service.state_for_response(session, response.id)
        assert state.status is DocumentJobStatus.FAILED
        assert state.is_retryable


class TestAcceptedPageAndDownload:
    def test_the_banner_confirms_a_signature_without_showing_it(
        self, session, client, accepted
    ):
        _response, _token, raw = accepted
        page = client.get(f"/quote/public/{raw}")

        assert "Signature recorded" in page.text
        assert "Dana R. Buyer" not in page.text        # the signature itself

    def test_no_download_button_until_the_document_exists(
        self, session, client, accepted
    ):
        _response, _token, raw = accepted
        page = client.get(f"/quote/public/{raw}")
        assert "Download PDF" not in page.text
        assert "being prepared" in page.text

    def test_the_button_appears_once_it_is_ready(self, session, client, accepted):
        response, _token, raw = accepted
        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service.process_job(session, job)

        page = client.get(f"/quote/public/{raw}")
        assert "Download PDF" in page.text

    def test_a_pending_document_is_not_replaced_by_an_improvised_one(
        self, session, client, accepted
    ):
        response, token, raw = accepted
        quotation = session.get(
            type(response).quotation.property.mapper.class_, response.quotation_id
        )
        result = client.post(
            f"/quote/public/{raw}/download.pdf",
            data=_download_fields(token, quotation),
        )
        assert result.status_code == 202
        assert not result.content.startswith(b"%PDF-")

    def test_an_accepted_download_serves_the_stored_artifact(
        self, session, client, accepted
    ):
        response, token, raw = accepted
        quotation = session.get(
            type(response).quotation.property.mapper.class_, response.quotation_id
        )
        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service.process_job(session, job)
        artifact = quote_document_service.artifact_for_response(session, response.id)

        result = client.post(
            f"/quote/public/{raw}/download.pdf",
            data=_download_fields(token, quotation),
        )
        assert result.status_code == 200
        assert result.content == get_storage().get(artifact.storage_key)
        assert hashlib.sha256(result.content).hexdigest() == artifact.sha256
        assert "Accepted" in result.headers["content-disposition"]

    def test_an_accepted_download_ignores_submitted_selections(
        self, session, client, accepted
    ):
        """What was agreed is not re-derived from anything a browser sends."""
        from portal.projection import line_ref

        response, token, raw = accepted
        quotation = session.get(
            type(response).quotation.property.mapper.class_, response.quotation_id
        )
        job = quote_document_service.job_for_response(session, response.id)
        quote_document_service.process_job(session, job)
        artifact = quote_document_service.artifact_for_response(session, response.id)

        recommended = next(
            i for i in quotation.items if i.inclusion is ItemInclusion.RECOMMENDED
        )
        payload = _download_fields(token, quotation)
        payload["selected"] = line_ref(token.token_hash, recommended.id)

        result = client.post(f"/quote/public/{raw}/download.pdf", data=payload)
        assert result.content == get_storage().get(artifact.storage_key)


class TestNoRemoteResources:
    def test_the_renderer_never_reaches_the_network(self, session, offered):
        """A customer document must not depend on anything outside this process."""
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("the renderer attempted a network connection")

        original = socket.socket.connect
        socket.socket.connect = refuse
        try:
            document = pdf_model.build_draft(offered, pricing_snapshot.base(offered))
            data = pdf_renderer.render(document)
        finally:
            socket.socket.connect = original

        assert data.startswith(b"%PDF-")

    def test_the_bytes_contain_no_external_reference(self, session, offered):
        document = pdf_model.build_draft(offered, pricing_snapshot.base(offered))
        data = pdf_renderer.render(document)
        for marker in (b"http://", b"https://", b"/URI", b"file://"):
            assert marker not in data

    def test_the_model_carries_image_bytes_not_a_location(self, session, offered):
        document = pdf_model.build_draft(
            offered, pricing_snapshot.base(offered), logo_bytes=b"\x89PNG\r\n\x1a\n"
        )
        assert isinstance(document.company.logo_bytes, bytes)
        assert not hasattr(document.company, "logo_key")
        assert not hasattr(document.company, "logo_url")


class TestLargeDocuments:
    def test_a_long_quotation_produces_multiple_pages(
        self, session, offered, sales, variant
    ):
        from pypdf import PdfReader
        from io import BytesIO

        for n in range(40):
            quotation_service.add_line(
                session, sales, offered,
                product_variant_id=variant.id,
                price_tier_code=PriceTierCode.STANDARD.value,
                quantity_packs=D("100"),
                description_override=f"Line {n} " + ("description text " * 12),
            )
        session.flush()
        quotation_service.recompute_totals(session, offered)
        session.flush()

        document = pdf_model.build_draft(offered, pricing_snapshot.base(offered))
        data = pdf_renderer.render(document)
        reader = PdfReader(BytesIO(data))

        assert len(reader.pages) > 1
        # The header repeats, so every page of the table is readable on its own.
        headed = [
            p for p in reader.pages if "Product / service" in (p.extract_text() or "")
        ]
        assert len(headed) > 1
        # And the last page is not blank.
        assert (reader.pages[-1].extract_text() or "").strip()

    def test_an_oversized_quotation_is_refused(self, session, offered):
        """Bounded work: a public route must not be able to ask for unlimited."""
        snapshot = pricing_snapshot.base(offered)
        original = pdf_model.MAX_LINES
        pdf_model.MAX_LINES = 1
        try:
            with pytest.raises(pdf_model.PdfModelError):
                pdf_model.build_draft(offered, snapshot)
        finally:
            pdf_model.MAX_LINES = original

    def test_an_oversized_render_is_refused(self, session, offered):
        document = pdf_model.build_draft(offered, pricing_snapshot.base(offered))
        original = pdf_renderer.MAX_OUTPUT_BYTES
        pdf_renderer.MAX_OUTPUT_BYTES = 10
        try:
            with pytest.raises(pdf_renderer.PdfTooLargeError):
                pdf_renderer.render(document)
        finally:
            pdf_renderer.MAX_OUTPUT_BYTES = original


class TestEscaping:
    def test_markup_in_customer_text_is_escaped(self, session, offered):
        """Every string reaching the renderer was typed by somebody."""
        offered.customer_notes = "<b>bold</b> & <script>alert(1)</script>"
        offered.items[0].description_override = "12\" box <img src=x> & more"
        session.flush()

        document = pdf_model.build_draft(offered, pricing_snapshot.base(offered))
        data = pdf_renderer.render(document)      # must not raise
        text = _text_of(data)

        assert "<script>" in text or "alert(1)" in text   # printed as text
        assert data.startswith(b"%PDF-")

    def test_a_broken_logo_does_not_stop_the_document(self, session, offered):
        document = pdf_model.build_draft(
            offered, pricing_snapshot.base(offered),
            logo_bytes=b"not an image at all",
        )
        data = pdf_renderer.render(document)
        assert data.startswith(b"%PDF-")
