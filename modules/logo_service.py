"""Accepting a company logo from an employee, safely.

An uploaded image is untrusted input that will later be served to customers, so
nothing about it is taken on faith: not the filename, not the browser's declared
MIME type, not the header bytes on their own.

The accepted image is **re-encoded**, never stored as supplied. That is the step
that does the most work: it proves the bytes decode, discards EXIF and colour
profiles along with any GPS coordinates or camera serial the marketing team's
photograph carried, drops trailing data appended after the image, and produces
one canonical format the portal can serve without sniffing.

Replacement is ordered so a failure cannot lose the logo: upload the new object,
commit the database reference, and only then retire the old object. The reverse
order risks a live company record pointing at something already deleted.
"""
from __future__ import annotations

import io
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from modules.audit_service import record_audit
from modules.authorization import AuthUser, require
from modules.constants import AuditAction, EntityType, Perm
from modules.models import CompanySettings, StorageCleanup
from modules.storage import StorageError, get_storage

log = logging.getLogger(__name__)

#: Raster only, and only formats with no scripting or animation story worth
#: arguing about. SVG is absent deliberately: it is a document, not a picture.
ACCEPTED_FORMATS = {"PNG", "JPEG", "WEBP"}

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_DIMENSION = 4000
MIN_DIMENSION = 32
#: A guard against decompression bombs — a few kilobytes of PNG can describe a
#: billion pixels, which is an out-of-memory kill rather than a bad upload.
MAX_TOTAL_PIXELS = 12_000_000

#: What we store, whatever arrives. PNG keeps transparency, which a logo needs.
CANONICAL_FORMAT = "PNG"
CANONICAL_MEDIA_TYPE = "image/png"

#: Nothing outside this prefix may ever be deleted by the cleanup sweep. The
#: queue is written by this module alone, but a bad row — a bug, a bad
#: migration, a hand-edit — must not become a way to delete arbitrary objects.
LOGO_NAMESPACE = "branding/"


class LogoError(ValueError):
    """The upload was refused. The message is safe to show an employee."""


@dataclass(frozen=True)
class PreparedLogo:
    """A validated, re-encoded logo, not yet stored."""

    data: bytes
    width: int
    height: int
    original_format: str
    media_type: str = CANONICAL_MEDIA_TYPE

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def _sniff(data: bytes) -> str | None:
    """Format from magic bytes. The filename and browser MIME type are ignored."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def prepare(data: bytes) -> PreparedLogo:
    """Validate and re-encode an uploaded logo, or raise :class:`LogoError`.

    Pure: touches neither storage nor the database, so it can be called to show
    a preview before the employee commits to anything.
    """
    if not data:
        raise LogoError("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise LogoError(
            f"That file is {len(data) / 1024 / 1024:.1f} MB. "
            f"The limit is {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        )

    declared = _sniff(data)
    if declared is None:
        raise LogoError(
            "That file is not a PNG, JPEG or WebP image. SVG files are not "
            "accepted because they can contain scripts."
        )

    from PIL import Image, UnidentifiedImageError

    # Pillow's own bomb guard, set to our budget rather than its default.
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_TOTAL_PIXELS
    try:
        try:
            image = Image.open(io.BytesIO(data))
            image.verify()                    # structural check, consumes the file
            image = Image.open(io.BytesIO(data))   # reopen: verify() leaves it unusable
        except UnidentifiedImageError:
            raise LogoError("That image could not be read. It may be corrupted.") from None
        except Image.DecompressionBombError:
            raise LogoError(
                "That image describes far too many pixels to be a logo."
            ) from None
        except Exception:  # noqa: BLE001 — any decode failure is a refusal
            raise LogoError("That image could not be read. It may be corrupted.") from None

        if image.format not in ACCEPTED_FORMATS:
            raise LogoError(f"{image.format or 'That format'} is not accepted.")
        # The header said one thing and the decoder another: refuse rather than
        # guess which is right.
        if image.format != declared:
            raise LogoError("That file's contents do not match its format.")

        if getattr(image, "n_frames", 1) > 1:
            raise LogoError(
                "Animated images are not accepted. Please upload a single frame."
            )

        width, height = image.size
        if width * height > MAX_TOTAL_PIXELS:
            raise LogoError("That image describes far too many pixels to be a logo.")
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise LogoError(
                f"That image is {width}x{height}. The limit is "
                f"{MAX_DIMENSION}x{MAX_DIMENSION}."
            )
        if width < MIN_DIMENSION or height < MIN_DIMENSION:
            raise LogoError(
                f"That image is {width}x{height}, too small to print clearly. "
                f"Minimum {MIN_DIMENSION}x{MIN_DIMENSION}."
            )

        # Re-encode. A new image built from the pixel data carries no EXIF, no
        # colour profile, no comment blocks and nothing appended after the
        # image data.
        converted = image.convert("RGBA")
        buffer = io.BytesIO()
        converted.save(buffer, format=CANONICAL_FORMAT, optimize=True)
        encoded = buffer.getvalue()
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    return PreparedLogo(
        data=encoded, width=width, height=height, original_format=declared,
    )


def build_logo_key() -> str:
    """A key that cannot collide with an earlier logo.

    ``storage.build_key`` uses a second-resolution timestamp, which is fine for
    a price-list import and not for something replaced twice in a minute during
    a branding review. 128 bits of randomness removes the question.
    """
    now = datetime.now(UTC)
    return f"branding/{now:%Y/%m}/logo_{now:%Y%m%dT%H%M%S}_{secrets.token_hex(16)}.png"


def save_logo(
    session: Session,
    user: AuthUser,
    data: bytes,
    *,
    replace_existing: bool = False,
) -> CompanySettings:
    """Validate, store and attach a logo to the company record.

    ``replace_existing`` must be passed explicitly when a logo is already set —
    the caller has to have asked the employee to confirm.
    """
    require(user, Perm.SETTINGS_MANAGE)

    company = session.query(CompanySettings).first()
    if company is None:
        raise LogoError(
            "Company Settings has not been created yet. Save the company "
            "details first, then upload a logo."
        )

    previous_key = company.logo_key
    if previous_key and not replace_existing:
        raise LogoError(
            "A logo is already set. Confirm the replacement before uploading."
        )

    prepared = prepare(data)
    key = build_logo_key()

    storage = get_storage()
    try:
        storage.put(key, prepared.data, CANONICAL_MEDIA_TYPE)
    except StorageError as exc:
        raise LogoError(f"The logo could not be stored: {exc}") from None

    company.logo_key = key

    # The old object is queued for deletion rather than deleted here. A flush is
    # not a commit: if the caller's transaction rolls back after this point, the
    # database reverts to the old key, and an object deleted in the meantime
    # would leave the company record pointing at nothing. The queue row rolls
    # back with everything else, so the two stay consistent.
    if previous_key and previous_key != key:
        _queue_cleanup(session, previous_key, reason="logo replaced")

    try:
        session.flush()
    except Exception:
        # The reference did not persist, so the object just uploaded is orphaned.
        # Remove it rather than leave litter, then let the failure surface.
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001 — cleanup is best effort
            log.warning("Could not remove an orphaned logo object after a failed save")
        raise

    record_audit(
        session, user, AuditAction.SETTINGS_CHANGED,
        EntityType.COMPANY_SETTINGS, company.id,
        new_value={
            "logo": "replaced" if previous_key else "set",
            "width": prepared.width, "height": prepared.height,
            "original_format": prepared.original_format,
        },
    )

    # Attempt the deletion only once the transaction has actually committed.
    # If it fails, the queue row survives and the retry sweep picks it up; the
    # new logo stays active either way.
    if previous_key and previous_key != key:
        _delete_after_commit(session, previous_key)

    return company


def _queue_cleanup(session: Session, storage_key: str, *, reason: str) -> None:
    """Record that a key is no longer referenced. Idempotent on the key."""
    pending = any(
        isinstance(obj, StorageCleanup) and obj.storage_key == storage_key
        for obj in session.new
    )
    if pending:
        return
    existing = session.execute(
        select(StorageCleanup).where(StorageCleanup.storage_key == storage_key)
    ).scalar_one_or_none()
    if existing is None:
        session.add(StorageCleanup(storage_key=storage_key, reason=reason))


def _delete_after_commit(session: Session, storage_key: str) -> None:
    """Delete the object once — and only once — the transaction has committed."""

    fired = False

    def _on_commit(_session: Session) -> None:
        # Not event.remove(): removing a listener while SQLAlchemy is
        # dispatching mutates the collection it is iterating. A flag is enough,
        # and the listener dies with the session.
        nonlocal fired
        if fired:
            return
        fired = True
        try:
            get_storage().delete(storage_key)
        except Exception:  # noqa: BLE001
            # Left queued deliberately. Never surfaced to the employee: the save
            # succeeded, and the storage key is not their concern.
            log.info("Deferred logo cleanup did not complete; it stays queued")
            return
        _clear_cleanup(storage_key)

    event.listen(session, "after_commit", _on_commit)


def _clear_cleanup(storage_key: str) -> None:
    """Drop the queue row in its own transaction, after the object is gone."""
    from modules.database import session_scope

    try:
        with session_scope() as fresh:
            row = fresh.execute(
                select(StorageCleanup).where(StorageCleanup.storage_key == storage_key)
            ).scalar_one_or_none()
            if row is not None:
                fresh.delete(row)
    except Exception:  # noqa: BLE001
        log.info("Could not clear a completed storage cleanup row; harmless")


def is_within_logo_namespace(storage_key: str) -> bool:
    """Whether a key is one this module is allowed to delete.

    Rejects anything outside the logo prefix, and anything using traversal or
    an absolute path to climb out of it.
    """
    key = (storage_key or "").strip()
    if not key or not key.startswith(LOGO_NAMESPACE):
        return False
    if ".." in key or key.startswith("/") or "\\" in key:
        return False
    return True


def _still_referenced(session: Session, storage_key: str) -> bool:
    """Whether anything in the database points at this key again.

    A key can come back into use — an employee re-uploads an identical file, or
    a restore puts an old reference back. Deleting it because a stale queue row
    said so would break a live record.
    """
    referenced = session.execute(
        select(CompanySettings.id).where(CompanySettings.logo_key == storage_key)
    ).first()
    return referenced is not None


def retry_pending_cleanups(session: Session, limit: int = 50) -> int:
    """Delete objects the database has already stopped referencing.

    Safe to run repeatedly and safe to run concurrently. Three things are
    checked before anything is deleted: the key is inside the logo namespace,
    nothing references it any more, and no other worker holds the row.
    """
    statement = select(StorageCleanup).order_by(StorageCleanup.id).limit(limit)
    # Two sweeps must not both delete the same object. Postgres takes a row
    # lock and skips what another worker holds; SQLite serialises writers
    # anyway, and does not support the clause.
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)

    rows = session.execute(statement).scalars().all()

    cleared = 0
    storage = get_storage()
    for row in rows:
        row.attempts += 1
        row.last_attempt_at = datetime.now(UTC)

        if not is_within_logo_namespace(row.storage_key):
            # Never delete it, and never stop flagging it: a row like this means
            # something upstream is wrong and should be looked at.
            row.last_error = "refused: key is outside the logo namespace"
            log.warning(
                "Refusing a storage cleanup request outside the logo namespace"
            )
            continue

        if _still_referenced(session, row.storage_key):
            # The key came back into use. The request is stale, not actionable:
            # drop it rather than delete an object something now points at.
            log.info("Storage cleanup skipped: the key is referenced again")
            session.delete(row)
            continue

        try:
            storage.delete(row.storage_key)
        except Exception as exc:  # noqa: BLE001
            row.last_error = str(exc)[:500]
            continue
        session.delete(row)
        cleared += 1

    session.flush()
    return cleared
