"""File storage adapter.

Streamlit Community Cloud rebuilds the container on every redeploy, dependency
change and wake-from-sleep, so its filesystem is a cache and nothing more.
Uploaded price-list workbooks and generated quotation PDFs are durable business
records — an issued quotation must still be reproducible in three years — so in
production they live in object storage and the database holds a key, never a
path.

Local development uses :class:`LocalStorage` and needs no cloud account.
Production sets ``STORAGE_BACKEND=s3``; the S3 API is spoken by Supabase
Storage, Cloudflare R2 and AWS S3 alike, so the choice of provider is not
load-bearing.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from modules.config import get_settings

log = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Raised for rejected uploads and backend failures alike."""


# --------------------------------------------------------------------------- #
# Filename hygiene
# --------------------------------------------------------------------------- #

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: Magic-byte prefixes. Extension checks alone are trivially bypassed by
#: renaming, and an .xlsx is a zip archive that openpyxl will happily open — so
#: the container type is verified before anything parses the contents.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".xlsx": (b"PK\x03\x04",),
    ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
}


def safe_filename(name: str, max_length: int = 120) -> str:
    """Reduce a client-supplied filename to something safe to store.

    Strips directory components, normalises Unicode, replaces anything outside
    ``[A-Za-z0-9._-]`` and refuses names that resolve to nothing. The result is
    only ever used as the tail of a generated key — the identifying part of a
    key is always a value this application chose.
    """
    base = Path(name).name
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = _UNSAFE.sub("_", base).strip("._-")
    if not base:
        raise StorageError("Filename is empty after sanitisation")
    if len(base) > max_length:
        stem, dot, suffix = base.rpartition(".")
        keep = max_length - len(suffix) - 1
        base = f"{stem[:keep]}{dot}{suffix}" if dot else base[:max_length]
    return base


def validate_upload(data: bytes, filename: str) -> str:
    """Check extension, size and magic bytes. Returns the sanitised filename.

    Raises :class:`StorageError` with a message safe to show the user.
    """
    settings = get_settings()
    clean = safe_filename(filename)
    suffix = Path(clean).suffix.lower()

    if suffix not in settings.allowed_extensions:
        allowed = ", ".join(sorted(settings.allowed_extensions))
        raise StorageError(f"File type '{suffix or '(none)'}' is not allowed. Permitted: {allowed}")

    if len(data) > settings.max_upload_bytes:
        raise StorageError(
            f"File is {len(data) / 1024 / 1024:.1f} MB; the limit is {settings.max_upload_mb} MB"
        )
    if not data:
        raise StorageError("File is empty")

    expected = _MAGIC.get(suffix)
    if expected and not any(data.startswith(sig) for sig in expected):
        raise StorageError(
            f"File content does not match its '{suffix}' extension and was rejected"
        )

    return clean


def build_key(prefix: str, filename: str, identifier: int | str | None = None) -> str:
    """Compose a storage key: ``prefix/YYYY/MM/<id>_<timestamp>_<name>``.

    The client-supplied name is kept only as a human-readable tail so that a
    downloaded file is recognisable; uniqueness comes from the identifier and
    timestamp, never from the upload.
    """
    now = datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    lead = f"{identifier}_" if identifier is not None else ""
    return f"{prefix}/{now:%Y/%m}/{lead}{stamp}_{safe_filename(filename)}"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class Storage(ABC):
    """Four methods. Everything else in the application depends only on these."""

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str | None = None) -> str: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class LocalStorage(Storage):
    """Filesystem-backed. Development and tests only."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """Resolve a key inside the storage root, refusing traversal.

        ``..`` segments in a key would otherwise let a crafted value read or
        overwrite files outside the storage area.
        """
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root):
            raise StorageError(f"Refusing to access a path outside the storage root: {key!r}")
        return candidate

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise StorageError(f"No stored file for key {key!r}")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        path.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()


class ObjectStorage(Storage):
    """S3-compatible object storage: Supabase Storage, Cloudflare R2, AWS S3."""

    def __init__(
        self,
        bucket: str,
        access_key: str,
        secret_key: str,
        endpoint_url: str = "",
        region: str = "auto",
    ) -> None:
        import boto3  # imported lazily so local development never needs boto3

        if not bucket:
            raise StorageError("STORAGE_BUCKET is not configured")
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=endpoint_url or None,
            region_name=region or None,
        )

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        extra = {"ContentType": content_type} if content_type else {}
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return key

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - botocore raises many shapes
            raise StorageError(f"No stored object for key {key!r}") from exc
        return response["Body"].read()

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001
            return False
        return True


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    """Return the configured storage backend.

    Note that no public URL accessor is exposed. Generated PDFs and uploaded
    price lists are served to the user through ``st.download_button`` after a
    permission check, never by handing out a link — a presigned URL would be a
    capability that outlives the session and escapes role checks entirely.
    """
    settings = get_settings()
    if settings.storage_backend == "s3":
        log.info("Using object storage (bucket=%s)", settings.storage_bucket)
        return ObjectStorage(
            bucket=settings.storage_bucket,
            access_key=settings.storage_access_key_id,
            secret_key=settings.storage_secret_access_key,
            endpoint_url=settings.storage_endpoint_url,
            region=settings.storage_region,
        )
    log.info("Using local storage (%s)", settings.local_storage_root)
    return LocalStorage(settings.local_storage_root)


def reset_storage_cache() -> None:
    """Used by tests, which point storage at a temporary directory."""
    get_storage.cache_clear()
