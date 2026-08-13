"""Product images, branding and production startup validation."""
from __future__ import annotations

import datetime as dt

import pytest
from starlette.testclient import TestClient

from modules import portal_service
from modules.config import Settings
from modules.constants import QuotationStatus
from modules.portal_service import issue_token
from portal import assets
from portal.assets import MAX_IMAGE_BYTES, load_product_image, resolve_item_by_ref
from portal.branding import (
    DEFAULT_ACCENT,
    PortalConfigError,
    resolve_brand,
    validate_portal_settings,
)
from portal.projection import line_ref

from tests.test_documents_and_approval import (  # noqa: F401
    _approve_and_issue,
    admin,
    manager,
    quotation,
    sales,
    variant,
)

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture(autouse=True)
def portal_user(session):
    return portal_service.ensure_portal_user(session)


@pytest.fixture
def sent(session, quotation, sales, manager):
    _approve_and_issue(session, quotation, sales, manager)
    portal_service.quotation_service.change_status(
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
    from contextlib import contextmanager

    from portal import app as portal_app

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(portal_app, "session_scope", _scope)
    portal_app._view_limiter.reset()
    portal_app._submit_limiter.reset()
    return TestClient(portal_app.app, base_url="http://testserver")


class FakeStorage:
    def __init__(self, payload: bytes | None = None, raises: bool = False):
        self.payload, self.raises = payload, raises
        self.requested: list[str] = []

    def get(self, key: str) -> bytes:
        self.requested.append(key)
        if self.raises:
            from modules.storage import StorageError

            raise StorageError("missing")
        return self.payload or b""


def _use_storage(monkeypatch, storage):
    monkeypatch.setattr(assets, "get_storage", lambda: storage)


class TestImageValidation:
    @pytest.mark.parametrize(
        "payload,expected",
        [(PNG, "png"), (JPEG, "jpeg"), (GIF, "gif"), (WEBP, "webp")],
    )
    def test_recognised_raster_formats(self, payload, expected):
        assert assets.sniff_image_type(payload) == expected

    def test_svg_is_refused(self):
        """An SVG is a document that can carry script; it is not sanitised."""
        assert assets.sniff_image_type(SVG) is None

    @pytest.mark.parametrize("payload", [b"", b"<html>", b"%PDF-1.4", b"not an image"])
    def test_other_content_is_refused(self, payload):
        assert assets.sniff_image_type(payload) is None

    def test_the_extension_is_never_trusted(self, session, sent, monkeypatch):
        """A key ending .png holding an SVG must still be refused."""
        item = sent.items[0]
        item.variant.product.image_key = "products/evil.png"
        session.flush()
        _use_storage(monkeypatch, FakeStorage(SVG))
        assert load_product_image(item).is_placeholder is True

    def test_oversized_images_are_refused(self, session, sent, monkeypatch):
        item = sent.items[0]
        item.variant.product.image_key = "products/huge.png"
        session.flush()
        _use_storage(monkeypatch, FakeStorage(PNG + b"\x00" * MAX_IMAGE_BYTES))
        assert load_product_image(item).is_placeholder is True

    def test_a_missing_image_yields_a_placeholder(self, session, sent, monkeypatch):
        item = sent.items[0]
        item.variant.product.image_key = None
        session.flush()
        _use_storage(monkeypatch, FakeStorage())
        payload = load_product_image(item)
        assert payload.is_placeholder is True
        assert payload.media_type == "image/png"

    def test_a_storage_failure_yields_a_placeholder(self, session, sent, monkeypatch):
        item = sent.items[0]
        item.variant.product.image_key = "products/gone.png"
        session.flush()
        _use_storage(monkeypatch, FakeStorage(raises=True))
        assert load_product_image(item).is_placeholder is True


class TestImageReferences:
    def test_a_ref_from_another_token_resolves_to_nothing(self, session, sent, sales):
        first, _ = issue_token(session, sales, sent)
        second, _ = issue_token(session, sales, sent)
        ref = line_ref(first.token_hash, sent.items[0].id)
        assert resolve_item_by_ref(first, sent, ref) is not None
        assert resolve_item_by_ref(second, sent, ref) is None

    @pytest.mark.parametrize(
        "ref", ["", "   ", "../../etc/passwd", "products/secret.png",
                "0" * 15, "0" * 17, "abc!@#$%^&*()xyz"],
    )
    def test_traversal_and_arbitrary_keys_are_refused(self, session, sent, link, ref):
        token, _ = link
        assert resolve_item_by_ref(token, sent, ref) is None

    def test_the_storage_key_is_never_published(self, client, session, sent, link, monkeypatch):
        token, raw = link
        sent.items[0].variant.product.image_key = "products/internal-key-9f3.png"
        session.flush()
        _use_storage(monkeypatch, FakeStorage(PNG))
        body = client.get(f"/quote/public/{raw}").text
        assert "internal-key-9f3" not in body
        assert "products/" not in body


class TestImageRoute:
    def test_an_image_is_streamed_with_its_real_type(
        self, client, session, sent, link, monkeypatch
    ):
        token, raw = link
        sent.items[0].variant.product.image_key = "products/box.png"
        session.flush()
        _use_storage(monkeypatch, FakeStorage(PNG))

        ref = line_ref(token.token_hash, sent.items[0].id)
        response = client.get(f"/quote/public/{raw}/assets/product/{ref}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
        assert "no-store" in response.headers["cache-control"]

    def test_an_image_request_is_not_a_quotation_view(
        self, client, session, sent, link, monkeypatch
    ):
        """Eight products must not register nine views."""
        token, raw = link
        _use_storage(monkeypatch, FakeStorage(PNG))
        ref = line_ref(token.token_hash, sent.items[0].id)

        client.get(f"/quote/public/{raw}")          # one genuine view
        client.get(f"/quote/public/{raw}/assets/product/{ref}")
        client.get(f"/quote/public/{raw}/assets/product/{ref}")
        assert token.view_count == 1

    def test_a_revoked_token_cannot_fetch_images(
        self, client, session, sent, sales, link, monkeypatch
    ):
        token, raw = link
        ref = line_ref(token.token_hash, sent.items[0].id)
        portal_service.revoke_token(session, sales, token)
        session.commit()
        _use_storage(monkeypatch, FakeStorage(PNG))

        response = client.get(f"/quote/public/{raw}/assets/product/{ref}")
        assert response.status_code == 404
        assert "image" not in response.headers["content-type"]

    def test_an_expired_token_cannot_fetch_images(
        self, client, session, sent, sales, monkeypatch
    ):
        token, raw = issue_token(
            session, sales, sent,
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
        )
        session.commit()
        ref = line_ref(token.token_hash, sent.items[0].id)
        _use_storage(monkeypatch, FakeStorage(PNG))
        assert client.get(f"/quote/public/{raw}/assets/product/{ref}").status_code == 404

    def test_an_unknown_ref_returns_a_placeholder_not_an_error(
        self, client, link, monkeypatch
    ):
        _, raw = link
        _use_storage(monkeypatch, FakeStorage(PNG))
        response = client.get(f"/quote/public/{raw}/assets/product/{'a' * 16}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")

    def test_image_requests_are_rate_limited(self, client, link, monkeypatch):
        from portal import app as portal_app

        token, raw = link
        _use_storage(monkeypatch, FakeStorage(PNG))
        monkeypatch.setattr(portal_app._view_limiter, "limit", 2)
        portal_app._view_limiter.reset()
        ref = line_ref(token.token_hash, 1)
        codes = [
            client.get(f"/quote/public/{raw}/assets/product/{ref}").status_code
            for _ in range(4)
        ]
        assert codes.count(429) == 2


class TestBranding:
    @pytest.fixture
    def company(self, session):
        """The test schema is built from metadata, so no company row is seeded."""
        from modules.models import CompanySettings

        existing = session.query(CompanySettings).first()
        if existing is not None:
            return existing
        row = CompanySettings(
            legal_name="Igland Bros Packaging Inc.",
            trading_name="Igland Bros",
            email="sales@example.invalid",
        )
        session.add(row)
        session.flush()
        return row

    def test_identity_comes_from_the_database(self, session, company):
        brand = resolve_brand(Settings(), company)
        expected = (company.trading_name or company.legal_name or "").strip()
        assert brand.name == expected

    def test_configuration_overrides_only_presentation(self, session, company):
        brand = resolve_brand(
            Settings(
                portal_brand_name="Pacabro",
                portal_brand_slogan="Packaging that performs",
                portal_brand_accent="#0a5c2e",
            ),
            company,
        )
        assert brand.name == "Pacabro"
        assert brand.slogan == "Packaging that performs"
        assert brand.accent == "#0a5c2e"
        # The legal footer still comes from the company record.
        assert brand.legal_footer == (company.legal_name or "").strip()

    def test_a_malformed_colour_falls_back(self, session):
        brand = resolve_brand(Settings(portal_brand_accent="red; }"), None)
        assert brand.accent == DEFAULT_ACCENT

    def test_colours_cannot_break_out_of_the_stylesheet(self, session):
        brand = resolve_brand(
            Settings(portal_brand_primary="#fff} body{display:none"), None
        )
        assert "}" not in brand.css().split("--brand-primary:")[1].split(";")[0]

    def test_the_brand_stylesheet_is_served_as_css(self, client):
        response = client.get("/brand.css")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/css")
        assert "--brand-accent" in response.text

    def test_nothing_is_invented_when_unconfigured(self):
        brand = resolve_brand(Settings(), None)
        assert brand.name == ""
        assert brand.slogan == ""
        assert brand.has_logo is False


class TestProductionStartupValidation:
    def test_development_does_not_require_a_base_url(self):
        validate_portal_settings(Settings(app_env="development"))

    def _production(self, **kwargs):
        return Settings(
            app_env="production",
            secret_key="x" * 48,
            database_url="postgresql+psycopg://u:p@h/db",
            storage_backend="s3",
            storage_bucket="b",
            storage_access_key_id="k",
            storage_secret_access_key="s",
            **kwargs,
        )

    def test_production_requires_the_base_url(self):
        with pytest.raises(PortalConfigError, match="must be set"):
            validate_portal_settings(self._production(portal_base_url=""))

    def test_production_requires_https(self):
        with pytest.raises(PortalConfigError, match="https"):
            validate_portal_settings(
                self._production(portal_base_url="http://quotes.example.com")
            )

    @pytest.mark.parametrize("value", ["not a url", "https://", "quotes.example.com"])
    def test_production_rejects_malformed_values(self, value):
        with pytest.raises(PortalConfigError):
            validate_portal_settings(self._production(portal_base_url=value))

    def test_a_valid_https_origin_is_accepted(self):
        validate_portal_settings(
            self._production(portal_base_url="https://quotes.example.com")
        )
