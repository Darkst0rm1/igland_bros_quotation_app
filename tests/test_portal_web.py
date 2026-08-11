"""The public web tier: projection safety, headers, access control, rate limits.

The central claim under test is that a customer cannot see anything the
projection does not name — not in a dataclass, not in the rendered HTML, not in
an error page, and not in a log line.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import fields, is_dataclass
from decimal import Decimal as D

import pytest
from starlette.testclient import TestClient

from modules import portal_service, quotation_service
from modules.constants import ItemInclusion, QuotationStatus
from modules.portal_service import issue_token
from portal import projection
from portal.projection import FORBIDDEN_FIELDS

from tests.test_documents_and_approval import (  # noqa: F401
    _approve_and_issue,
    admin,
    manager,
    quotation,
    sales,
    variant,
)

#: Values that must never reach a customer, seeded into the quotation below.
SECRET_COST = "5.4321"
SECRET_NOTE = "INTERNAL-ONLY-MARGIN-NOTE-DO-NOT-SHOW"
SECRET_REMARK = "INTERNAL-REMARK-SUPPLIER-DISCOUNT"


@pytest.fixture(autouse=True)
def portal_user(session):
    return portal_service.ensure_portal_user(session)


@pytest.fixture
def sent(session, quotation, sales, manager):
    _approve_and_issue(session, quotation, sales, manager)
    quotation.contact_name = "Alex Buyer"
    quotation.contact_email = "buyer@bunzl.example"
    quotation.internal_notes = SECRET_NOTE
    item = quotation.items[0]
    item.internal_remarks = SECRET_REMARK
    item.unit_cost_per_pack = D(SECRET_COST)
    item.line_cost_total = D("1234.56")
    quotation.total_cost = D("1234.56")
    quotation.gross_profit = D("999.99")
    quotation.gross_margin_pct = D("42.4242")
    quotation_service.change_status(
        session, manager, quotation, QuotationStatus.SENT_TO_CUSTOMER
    )
    session.flush()
    return quotation


@pytest.fixture
def link(session, sent, sales):
    token, raw = issue_token(session, sales, sent)
    session.commit()
    return token, raw


@pytest.fixture
def client(session, monkeypatch):
    """A test client whose requests use the test's own session."""
    from contextlib import contextmanager

    from portal import app as portal_app

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(portal_app, "session_scope", _scope)
    portal_app._view_limiter.reset()
    portal_app._submit_limiter.reset()
    return TestClient(portal_app.app, base_url="http://testserver")


class TestProjectionSafety:
    def test_no_view_dataclass_declares_a_forbidden_field(self):
        """Structural: the shapes themselves have nowhere to put a cost."""
        views = [
            projection.QuoteView, projection.LineView, projection.TotalsView,
            projection.CustomerView, projection.CompanyView, projection.ChargeView,
            projection.ShippingView, projection.TermView, projection.AcceptedView,
        ]
        for view in views:
            assert is_dataclass(view)
            names = {f.name for f in fields(view)}
            leaked = names & FORBIDDEN_FIELDS
            assert not leaked, f"{view.__name__} exposes {leaked}"

    def test_the_projection_carries_decimals_not_strings(self, session, sent, link):
        token, _ = link
        totals = portal_service.compute_selection_totals(sent, [])
        view = projection.build_quote_view(sent, token, totals)
        assert isinstance(view.totals.grand_total, D)
        assert isinstance(view.lines[0].unit_price, D)
        assert isinstance(view.lines[0].line_total, D)

    def test_database_ids_are_not_published(self, session, sent, link):
        token, _ = link
        totals = portal_service.compute_selection_totals(sent, [])
        view = projection.build_quote_view(sent, token, totals)
        item_id = str(sent.items[0].id)
        for line in view.lines:
            assert line.ref != item_id
            assert len(line.ref) == 16          # opaque HMAC handle

    def test_a_line_ref_is_useless_against_another_token(self, session, sent, sales):
        first, _ = issue_token(session, sales, sent)
        second, _ = issue_token(session, sales, sent)
        item = sent.items[0]
        assert projection.line_ref(first.token_hash, item.id) != \
               projection.line_ref(second.token_hash, item.id)
        # A ref minted for one token resolves to nothing under another.
        ref = projection.line_ref(first.token_hash, item.id)
        assert projection.resolve_refs(second, sent, [ref]) == []


class TestRenderedHtmlSafety:
    def test_costs_margins_and_internal_notes_never_reach_the_page(
        self, client, link, sent
    ):
        _, raw = link
        body = client.get(f"/quote/public/{raw}").text
        for secret in (SECRET_COST, SECRET_NOTE, SECRET_REMARK,
                       "1234.56", "999.99", "42.4242"):
            assert secret not in body, f"{secret!r} leaked into the page"
        for word in ("gross_profit", "margin", "unit_cost", "internal"):
            assert word not in body.lower()

    def test_the_access_token_is_not_placed_in_the_title_or_comments(
        self, client, link
    ):
        _, raw = link
        body = client.get(f"/quote/public/{raw}").text
        title = body.split("<title>")[1].split("</title>")[0]
        assert raw not in title
        for comment in [c.split("-->")[0] for c in body.split("<!--")[1:]]:
            assert raw not in comment

    def test_no_third_party_assets_are_loaded(self, client, link):
        _, raw = link
        body = client.get(f"/quote/public/{raw}").text
        for marker in ("http://", "https://cdn", "googleapis", "gstatic",
                       "google-analytics", "googletagmanager", "fonts.",
                       "<script src=", "analytics"):
            assert marker not in body, f"third-party reference {marker!r} present"

    def test_customer_content_is_escaped(self, client, session, sent, link, sales):
        """A quotation field containing markup must not become markup."""
        sent.customer_notes = "<script>alert('xss')</script>"
        session.flush()
        _, raw = link
        body = client.get(f"/quote/public/{raw}").text
        assert "<script>alert(" not in body
        assert "&lt;script&gt;" in body


class TestSecurityHeaders:
    def test_every_response_carries_the_headers(self, client, link):
        _, raw = link
        for response in (
            client.get(f"/quote/public/{raw}"),
            client.get("/quote/public/definitely-not-a-real-token-value"),
        ):
            assert "no-store" in response.headers["cache-control"]
            assert "private" in response.headers["cache-control"]
            assert response.headers["pragma"] == "no-cache"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"
            csp = response.headers["content-security-policy"]
            assert "default-src 'self'" in csp
            assert "frame-ancestors 'none'" in csp

    def test_no_cookies_are_set(self, client, link):
        _, raw = link
        response = client.get(f"/quote/public/{raw}")
        assert "set-cookie" not in {k.lower() for k in response.headers}


class TestAccessControl:
    def test_every_failure_looks_identical(self, client, session, sent, sales):
        """Unknown, malformed, revoked and expired must be indistinguishable."""
        revoked, revoked_raw = issue_token(session, sales, sent)
        portal_service.revoke_token(session, sales, revoked)
        expired, expired_raw = issue_token(
            session, sales, sent,
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
        )
        session.commit()

        bodies, codes = set(), set()
        for candidate in ("unknown-token-value-long-enough", "short", "../../etc/passwd",
                          revoked_raw, expired_raw):
            response = client.get(f"/quote/public/{candidate}")
            bodies.add(response.text)
            codes.add(response.status_code)

        assert codes == {404}
        assert len(bodies) == 1, "responses differ, disclosing which token existed"

    def test_the_root_reveals_nothing(self, client):
        response = client.get("/")
        assert response.status_code == 404
        assert "quotation" in response.text.lower()

    def test_health_reveals_only_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        body = response.text.lower()
        for leak in ("sqlite", "postgres", "database", "version", "secret", "url"):
            assert leak not in body

    def test_state_cannot_change_through_get(self, client, link, sent):
        _, raw = link
        before = sent.status
        response = client.get(f"/quote/public/{raw}/approve", follow_redirects=False)
        assert response.status_code in (303, 405)
        assert sent.status is before

    def test_a_cross_origin_post_is_refused(self, client, link, sent):
        _, raw = link
        token, _ = link
        nonce, signature = portal_service.issue_submission_nonce(token)
        before = sent.status
        response = client.post(
            f"/quote/public/{raw}/approve",
            data={"customer_name": "Mallory", "accepted_terms": "on",
                  "nonce": nonce, "signature": signature},
            headers={"origin": "https://evil.example"},
        )
        assert response.status_code == 403
        assert sent.status is before

    def test_a_post_with_no_origin_is_refused(self, client, link, sent):
        _, raw = link
        token, _ = link
        nonce, signature = portal_service.issue_submission_nonce(token)
        response = client.post(
            f"/quote/public/{raw}/approve",
            data={"customer_name": "Mallory", "accepted_terms": "on",
                  "nonce": nonce, "signature": signature},
        )
        assert response.status_code == 403
        assert sent.status is not QuotationStatus.ACCEPTED


class TestSelectionsThroughTheWeb:
    @pytest.fixture
    def optional(self, session, sent):
        sent.items[0].inclusion = ItemInclusion.OPTIONAL
        session.flush()
        return sent

    def test_preview_reprices_without_persisting(self, client, session, optional, link):
        token, raw = link
        ref = projection.line_ref(token.token_hash, optional.items[0].id)

        plain = client.get(f"/quote/public/{raw}").text
        with_pick = client.post(
            f"/quote/public/{raw}/preview", data={"selected": ref},
            headers={"origin": "http://testserver"},
        ).text

        assert plain != with_pick
        # Nothing was recorded: no response rows exist yet.
        from modules.models import PortalResponse
        assert session.query(PortalResponse).count() == 0
        assert optional.status is QuotationStatus.SENT_TO_CUSTOMER

    def test_a_forged_line_ref_changes_nothing(self, client, optional, link):
        _, raw = link
        def total_of(body: str) -> str:
            # Pages differ by their one-time nonce, so compare the figure.
            return body.split('class="total"')[1].split("</p>")[0]

        honest = client.post(
            f"/quote/public/{raw}/preview", data={"selected": []},
            headers={"origin": "http://testserver"},
        ).text
        forged = client.post(
            f"/quote/public/{raw}/preview", data={"selected": "0" * 16},
            headers={"origin": "http://testserver"},
        ).text
        assert total_of(honest) == total_of(forged)


class TestReadOnlyAfterAcceptance:
    def test_the_page_locks_and_shows_the_accepted_total(
        self, client, session, sent, link
    ):
        token, raw = link
        response = portal_service.approve(
            session, token, customer_name="Alex Buyer", accepted_terms=True,
        )
        session.commit()

        body = client.get(f"/quote/public/{raw}").text
        assert "Accepted" in body
        assert "read-only" in body.lower()
        assert "Alex Buyer" in body
        # The exact accepted total is shown.
        assert f"{response.grand_total:,.2f}" in body
        # And no way to respond again.
        assert "Approve quotation" not in body


class TestRateLimiting:
    def test_view_requests_are_limited(self, client, link, monkeypatch):
        from portal import app as portal_app

        _, raw = link
        monkeypatch.setattr(portal_app._view_limiter, "limit", 3)
        portal_app._view_limiter.reset()

        codes = [client.get(f"/quote/public/{raw}").status_code for _ in range(5)]
        assert codes.count(200) == 3
        assert codes.count(429) == 2

    def test_the_limiter_key_is_hashed_not_an_address(self, client):
        from starlette.requests import Request

        from portal.security import client_fingerprint

        scope = {
            "type": "http", "headers": [], "client": ("203.0.113.42", 1234),
            "method": "GET", "path": "/", "query_string": b"",
        }
        digest = client_fingerprint(Request(scope), "secret")
        assert "203.0.113.42" not in digest
        assert len(digest) == 16


class TestLogHygiene:
    def test_tokens_are_redacted_from_log_records(self, caplog):
        from portal.security import TokenRedactingFilter, redact

        raw = "aVeryLongLookingAccessTokenValue1234567890"
        line = f'GET /quote/public/{raw} HTTP/1.1" 200'
        assert raw not in redact(line)
        assert "[redacted]" in redact(line)

        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, line, None, None
        )
        TokenRedactingFilter().filter(record)
        assert raw not in record.msg

    def test_a_request_does_not_log_the_token(self, client, link, caplog):
        _, raw = link
        with caplog.at_level(logging.DEBUG):
            client.get(f"/quote/public/{raw}")
        # httpx is the test client itself; the service's own loggers are what
        # would write to disk in production.
        ours = [
            r.getMessage() for r in caplog.records
            if r.name.startswith(("portal", "uvicorn", "modules"))
        ]
        assert not any(raw in message for message in ours)
