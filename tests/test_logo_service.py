"""Logo upload: what gets refused, and what happens when storage or the database fails."""
from __future__ import annotations

import io

import pytest
from sqlalchemy import select

from modules import logo_service
from modules.authorization import AuthUser, PermissionDenied
from modules.constants import Perm
from modules.logo_service import LogoError, prepare, save_logo
from modules.models import CompanySettings
from modules.storage import StorageError


def make_image(fmt="PNG", size=(200, 120), frames=1) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    if frames > 1:
        images = [Image.new("RGB", size, (i * 40, 80, 120)) for i in range(frames)]
        images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:])
    else:
        Image.new("RGB", size, (12, 80, 40)).save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def company(session):
    row = session.query(CompanySettings).first()
    if row is None:
        row = CompanySettings(legal_name="Igland Bros Packaging Inc.")
        session.add(row)
        session.flush()
    return row


@pytest.fixture
def admin_user(session):
    """A real user row, because the audit trail's user_id is a foreign key."""
    from modules.models import User

    row = User(
        username="logo-admin", email="logo-admin@x.invalid",
        employee_name="Logo Admin", password_hash="x" * 60, is_active=True,
    )
    session.add(row)
    session.flush()
    return AuthUser(
        id=row.id, username=row.username, employee_name=row.employee_name,
        email=row.email, permissions=frozenset({Perm.SETTINGS_MANAGE}),
    )


class FakeStorage:
    def __init__(self, fail_put=False):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_put = fail_put

    def put(self, key, data, content_type=None):
        if self.fail_put:
            raise StorageError("bucket unavailable")
        self.objects[key] = data
        return key

    def get(self, key):
        return self.objects[key]

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)

    def exists(self, key):
        return key in self.objects


@pytest.fixture
def storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(logo_service, "get_storage", lambda: fake)
    return fake


class TestValidation:
    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
    def test_accepted_formats_are_re_encoded_to_png(self, fmt):
        result = prepare(make_image(fmt))
        assert result.original_format == fmt
        assert result.data.startswith(b"\x89PNG\r\n\x1a\n")   # canonical
        assert result.media_type == "image/png"

    def test_svg_is_refused(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script></svg>'
        with pytest.raises(LogoError, match="SVG"):
            prepare(svg)

    def test_a_spoofed_extension_cannot_smuggle_a_non_image(self):
        """Bytes decide, not the filename the browser supplied."""
        with pytest.raises(LogoError):
            prepare(b"GIF89a" + b"\x00" * 100)          # real GIF magic, unaccepted
        with pytest.raises(LogoError):
            prepare(b"%PDF-1.4\n" + b"\x00" * 100)

    def test_corrupted_bytes_are_refused(self):
        broken = bytearray(make_image("PNG"))
        del broken[40:400]                               # damage the image data
        with pytest.raises(LogoError, match="corrupted|could not be read"):
            prepare(bytes(broken))

    def test_a_png_header_on_garbage_is_refused(self):
        with pytest.raises(LogoError):
            prepare(b"\x89PNG\r\n\x1a\n" + b"nonsense" * 20)

    def test_animated_images_are_refused(self):
        with pytest.raises(LogoError):
            prepare(make_image(frames=4))

    def test_oversized_dimensions_are_refused(self):
        with pytest.raises(LogoError, match="limit is"):
            prepare(make_image(size=(5000, 100)))

    def test_tiny_images_are_refused(self):
        with pytest.raises(LogoError, match="too small"):
            prepare(make_image(size=(16, 16)))

    def test_oversized_files_are_refused(self):
        with pytest.raises(LogoError, match="limit is"):
            prepare(b"\x89PNG\r\n\x1a\n" + b"\x00" * logo_service.MAX_UPLOAD_BYTES)

    def test_an_empty_file_is_refused(self):
        with pytest.raises(LogoError, match="empty"):
            prepare(b"")

    def test_a_decompression_bomb_is_refused(self):
        """A small file describing an enormous canvas must not be decoded."""
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("L", (4000, 4000)).save(buffer, format="PNG")
        # Within the byte limit, beyond the pixel budget once combined.
        original = logo_service.MAX_TOTAL_PIXELS
        logo_service.MAX_TOTAL_PIXELS = 1_000_000
        try:
            with pytest.raises(LogoError, match="pixels"):
                prepare(buffer.getvalue())
        finally:
            logo_service.MAX_TOTAL_PIXELS = original

    def test_metadata_is_stripped_by_re_encoding(self):
        from PIL import Image

        buffer = io.BytesIO()
        image = Image.new("RGB", (200, 120), (10, 10, 10))
        exif = image.getexif()
        exif[271] = "SecretCameraMake"
        image.save(buffer, format="JPEG", exif=exif)
        assert b"SecretCameraMake" in buffer.getvalue()

        cleaned = prepare(buffer.getvalue())
        assert b"SecretCameraMake" not in cleaned.data

    def test_data_appended_after_the_image_is_dropped(self):
        polyglot = make_image("PNG") + b"<?php system($_GET[0]); ?>"
        cleaned = prepare(polyglot)
        assert b"<?php" not in cleaned.data


class TestSaving:
    def test_a_logo_is_stored_and_referenced(self, session, company, admin_user, storage):
        save_logo(session, admin_user, make_image("PNG"))
        assert company.logo_key
        assert company.logo_key in storage.objects
        assert company.logo_key.startswith("branding/")

    def test_keys_do_not_collide(self, session, company, admin_user, storage):
        keys = {logo_service.build_logo_key() for _ in range(200)}
        assert len(keys) == 200

    def test_replacing_requires_confirmation(self, session, company, admin_user, storage):
        save_logo(session, admin_user, make_image("PNG"))
        with pytest.raises(LogoError, match="Confirm"):
            save_logo(session, admin_user, make_image("PNG"))

    def test_the_old_object_survives_until_the_transaction_commits(
        self, session, company, admin_user, storage
    ):
        """A flush is not a commit. Deleting here would strand the record."""
        from modules.models import StorageCleanup

        save_logo(session, admin_user, make_image("PNG"))
        first = company.logo_key
        save_logo(session, admin_user, make_image("JPEG"), replace_existing=True)
        second = company.logo_key

        assert second != first
        # Flushed, not committed: the old object is still there, and queued.
        assert storage.deleted == []
        assert first in storage.objects
        queued = session.execute(
            select(StorageCleanup).where(StorageCleanup.storage_key == first)
        ).scalar_one()
        assert queued.storage_key == first

    def test_a_rollback_after_flush_leaves_the_old_logo_intact(
        self, session, company, admin_user, storage
    ):
        """The case that made deleting on flush wrong."""
        save_logo(session, admin_user, make_image("PNG"))
        session.commit()
        first = company.logo_key

        save_logo(session, admin_user, make_image("JPEG"), replace_existing=True)
        session.rollback()

        session.refresh(company)
        assert company.logo_key == first          # reverted, as it should
        assert first in storage.objects           # and the object still exists
        assert storage.deleted == []

    def test_the_old_object_is_deleted_after_commit(
        self, session, company, admin_user, storage
    ):
        save_logo(session, admin_user, make_image("PNG"))
        session.commit()
        first = company.logo_key

        save_logo(session, admin_user, make_image("JPEG"), replace_existing=True)
        session.commit()

        assert storage.deleted == [first]
        assert company.logo_key in storage.objects

    def test_a_failed_deletion_keeps_the_new_logo_and_stays_queued(
        self, session, company, admin_user, monkeypatch
    ):
        from modules.models import StorageCleanup

        class RefusesDelete(FakeStorage):
            def delete(self, key):
                raise StorageError("bucket unreachable")

        stubborn = RefusesDelete()
        monkeypatch.setattr(logo_service, "get_storage", lambda: stubborn)

        save_logo(session, admin_user, make_image("PNG"))
        session.commit()
        first = company.logo_key
        save_logo(session, admin_user, make_image("JPEG"), replace_existing=True)
        session.commit()

        # New logo active, database not rolled back, old key still queued.
        assert company.logo_key != first
        assert company.logo_key in stubborn.objects
        assert session.execute(
            select(StorageCleanup).where(StorageCleanup.storage_key == first)
        ).scalar_one_or_none() is not None

    def test_the_cleanup_retry_succeeds_later(
        self, session, company, admin_user, storage, monkeypatch
    ):
        from modules.models import StorageCleanup

        session.add(StorageCleanup(storage_key="branding/old.png", reason="test"))
        storage.objects["branding/old.png"] = b"x"
        session.flush()

        cleared = logo_service.retry_pending_cleanups(session)
        assert cleared == 1
        assert "branding/old.png" in storage.deleted
        assert session.query(StorageCleanup).count() == 0

    def test_retrying_is_idempotent(self, session, company, admin_user, storage):
        """Deleting something already gone must not fail or requeue."""
        from modules.models import StorageCleanup

        session.add(StorageCleanup(storage_key="branding/gone.png", reason="test"))
        session.flush()
        assert logo_service.retry_pending_cleanups(session) == 1
        assert logo_service.retry_pending_cleanups(session) == 0

    def test_queueing_the_same_key_twice_creates_one_row(self, session, storage):
        from modules.models import StorageCleanup

        logo_service._queue_cleanup(session, "branding/dup.png", reason="a")
        logo_service._queue_cleanup(session, "branding/dup.png", reason="b")
        session.flush()
        assert session.query(StorageCleanup).filter_by(
            storage_key="branding/dup.png"
        ).count() == 1

    def test_replacing_repeatedly_always_leaves_a_reachable_object(
        self, session, company, admin_user, storage
    ):
        save_logo(session, admin_user, make_image("PNG"))
        session.commit()
        for _ in range(4):
            save_logo(session, admin_user, make_image("PNG"), replace_existing=True)
            session.commit()
            assert company.logo_key in storage.objects

    def test_a_storage_failure_leaves_the_record_untouched(
        self, session, company, admin_user, monkeypatch
    ):
        monkeypatch.setattr(logo_service, "get_storage", lambda: FakeStorage(fail_put=True))
        before = company.logo_key
        with pytest.raises(LogoError, match="could not be stored"):
            save_logo(session, admin_user, make_image("PNG"))
        assert company.logo_key == before

    def test_a_database_failure_removes_the_orphaned_object(
        self, session, company, admin_user, storage, monkeypatch
    ):
        """Nothing is left in the bucket that the database does not know about."""
        def explode():
            raise RuntimeError("constraint violated")

        monkeypatch.setattr(session, "flush", explode)
        with pytest.raises(RuntimeError):
            save_logo(session, admin_user, make_image("PNG"))
        assert storage.deleted, "the uploaded object was not cleaned up"
        assert not storage.objects

    def test_permission_is_required(self, session, company, storage):
        nobody = AuthUser(
            id=None, username="sales", employee_name="Sales", email="s@x.invalid",
            permissions=frozenset({Perm.QUOTE_CREATE}),
        )
        with pytest.raises(PermissionDenied):
            save_logo(session, nobody, make_image("PNG"))


class TestPortalNeverExposesTheKey:
    def test_the_logo_route_serves_bytes_not_a_key(self, session, company, admin_user, storage):
        from portal.assets import load_company_logo

        save_logo(session, admin_user, make_image("PNG"))
        key = company.logo_key

        import portal.assets as portal_assets

        original = portal_assets.get_storage
        portal_assets.get_storage = lambda: storage
        try:
            payload = load_company_logo(key)
        finally:
            portal_assets.get_storage = original

        assert payload is not None
        assert payload.media_type == "image/png"
        assert key.encode() not in payload.content


class TestCleanupSafety:
    """Guards on the sweep: it may only ever delete retired logo objects."""

    @pytest.mark.parametrize(
        "key",
        [
            "quotations/2026/07/IGB-QT-0001.pdf",   # another namespace
            "uploads/price_lists/list.xlsx",
            "branding/../../secrets.env",           # traversal
            "/etc/passwd",
            "",
        ],
    )
    def test_an_out_of_namespace_key_is_never_deleted(
        self, session, storage, key
    ):
        from modules.models import StorageCleanup

        storage.objects[key] = b"important"
        session.add(StorageCleanup(storage_key=key, reason="planted"))
        session.flush()

        assert logo_service.retry_pending_cleanups(session) == 0
        assert key not in storage.deleted
        assert storage.objects.get(key) == b"important"

        row = session.query(StorageCleanup).filter_by(storage_key=key).one()
        assert "outside the logo namespace" in (row.last_error or "")

    def test_a_key_that_is_referenced_again_is_not_deleted(
        self, session, company, storage
    ):
        """A stale request must never remove an object something points at."""
        from modules.models import StorageCleanup

        key = "branding/2026/08/logo_reused.png"
        storage.objects[key] = b"live logo"
        company.logo_key = key                      # back in use
        session.add(StorageCleanup(storage_key=key, reason="stale"))
        session.flush()

        assert logo_service.retry_pending_cleanups(session) == 0
        assert key not in storage.deleted
        assert storage.objects[key] == b"live logo"
        # The stale request is cleared rather than retried forever.
        assert session.query(StorageCleanup).filter_by(storage_key=key).count() == 0

    def test_a_genuinely_retired_key_is_deleted(self, session, company, storage):
        from modules.models import StorageCleanup

        key = "branding/2026/08/logo_retired.png"
        storage.objects[key] = b"old"
        company.logo_key = "branding/2026/08/logo_current.png"
        session.add(StorageCleanup(storage_key=key, reason="replaced"))
        session.flush()

        assert logo_service.retry_pending_cleanups(session) == 1
        assert key in storage.deleted

    def test_two_sweeps_do_not_double_delete(self, session, company, storage):
        """The second sweep finds nothing left to do."""
        from modules.models import StorageCleanup

        key = "branding/2026/08/logo_once.png"
        storage.objects[key] = b"old"
        session.add(StorageCleanup(storage_key=key, reason="replaced"))
        session.flush()

        assert logo_service.retry_pending_cleanups(session) == 1
        assert logo_service.retry_pending_cleanups(session) == 0
        assert storage.deleted.count(key) == 1

    def test_the_database_always_points_at_an_object_that_exists(
        self, session, company, admin_user, storage
    ):
        """The invariant the whole design exists to hold."""
        save_logo(session, admin_user, make_image("PNG"))
        session.commit()
        for _ in range(3):
            save_logo(session, admin_user, make_image("PNG"), replace_existing=True)
            session.commit()
            logo_service.retry_pending_cleanups(session)
            session.commit()
            assert company.logo_key in storage.objects
