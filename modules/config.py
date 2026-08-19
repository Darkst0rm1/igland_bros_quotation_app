"""Application configuration.

Resolution order, most specific first:

1. ``st.secrets``      — Streamlit Community Cloud production secrets
2. process environment — including anything loaded from ``.env``
3. defaults declared below

Reading ``st.secrets`` first is what lets a single code path serve both local
development (``.env`` + SQLite + local files) and Community Cloud (secrets panel
+ managed PostgreSQL + object storage) without branching at every call site.

Nothing here is ever written to the audit log or surfaced in the UI.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env before Settings reads the environment. Harmless when absent, which
# is the normal case on Community Cloud.
load_dotenv(PROJECT_ROOT / ".env", override=False)


#: Why the last attempt to read st.secrets turned out the way it did. Recorded
#: because a silent fallback to defaults is indistinguishable from a correctly
#: configured app, and that ambiguity is expensive to debug from the outside.
_SECRETS_STATUS: str = "not attempted"


def _streamlit_secrets() -> dict[str, Any]:
    """Return st.secrets as a plain dict, or {} when unavailable.

    Accessing ``st.secrets`` outside a Streamlit runtime — in pytest, in Alembic,
    in a seeding script — raises rather than returning empty, and on Community
    Cloud a missing secrets file does the same. Both are non-fatal here: the
    environment is the fallback.

    The outcome is recorded in :data:`_SECRETS_STATUS` so the startup screen can
    report it. Values are never recorded, only key names.
    """
    global _SECRETS_STATUS

    try:
        import streamlit as st

        found = {str(k): v for k, v in st.secrets.items()}
    except Exception as exc:  # noqa: BLE001 - any failure means "no secrets"
        _SECRETS_STATUS = f"unavailable ({type(exc).__name__})"
        return {}

    if not found:
        _SECRETS_STATUS = "readable but empty"
        return found

    # Key names only. A section header shows up as a key whose value is a
    # mapping, which is the usual cause of a setting appearing to be ignored.
    sections = [k for k, v in found.items() if hasattr(v, "items")]
    _SECRETS_STATUS = f"{len(found)} key(s): {', '.join(sorted(found))}"
    if sections:
        _SECRETS_STATUS += (
            f" — note {', '.join(sections)} "
            f"{'is a section' if len(sections) == 1 else 'are sections'}; "
            "settings must be at the top level, not nested under a [header]"
        )
    return found


def secrets_status() -> str:
    """Human-readable account of what was found in st.secrets. Never values."""
    return _SECRETS_STATUS


def database_kind() -> str:
    """Which *sort* of database this process reached. No host, no name.

    For the startup failure screen, which renders before anyone has signed in —
    it is the first thing an unauthenticated visitor sees when configuration is
    wrong. That screen has to answer one question, "did DATABASE_URL arrive at
    all, or are we on the SQLite fallback", and answering it does not require
    naming the server.
    """
    from sqlalchemy.engine import make_url

    try:
        url = make_url(get_settings().database_url)
    except Exception:  # noqa: BLE001
        return "unreadable"
    if url.drivername.startswith("sqlite"):
        return "sqlite — the local fallback, not a configured database"
    return f"{url.drivername} (a configured server)"


def database_identity() -> str:
    """Which database this process is talking to, without credentials.

    Driver, host and database name — never the user, the password or the query
    string. Naming the host is what lets an administrator confirm the employee
    app and the Render worker share one database, which is the whole reason this
    exists; ``database_kind`` is the version for anywhere less privileged.

    ADMINISTRATORS ONLY. The single caller is the SYS_ADMIN diagnostic.
    """
    from sqlalchemy.engine import make_url

    try:
        url = make_url(get_settings().database_url)
    except Exception:  # noqa: BLE001
        return "unreadable"
    if url.drivername.startswith("sqlite"):
        return f"sqlite (file: {url.database or ':memory:'})"
    return f"{url.drivername} on {url.host or '?'}/{url.database or '?'}"


def delivery_status() -> dict[str, str]:
    """Whether outbound email is configured, as presence and never as content.

    Every entry is a verdict — "on", "set", "missing" — derived from a value but
    never containing one. Safe to render in the UI, screenshot and paste into a
    ticket, which is the point: the failure this exists for is a setting that
    looks present in the panel and never arrives, and diagnosing that from the
    outside otherwise means reading secrets aloud.
    """
    s = get_settings()
    mark = lambda present: "set" if present else "missing"  # noqa: E731
    return {
        "EMAIL_ENABLED": "on" if s.email_enabled else "OFF",
        "EMAIL_BACKEND": s.email_backend,
        "EMAIL_FROM_ADDRESS": mark(s.email_from_address),
        "EMAIL_REPLY_TO": mark(s.email_reply_to),
        "SMTP_HOST": mark(s.smtp_host),
        "SMTP_PORT": str(s.smtp_port),
        "SMTP_SECURITY": s.smtp_security,
        "SMTP_USERNAME": mark(s.smtp_username),
        "SMTP_PASSWORD": mark(s.smtp_password),
        "EMAIL_PAYLOAD_KEYS": mark(s.email_payload_keys),
    }


class Settings(BaseSettings):
    """Typed application settings.

    Field names are case-insensitive against both st.secrets and the environment.
    """

    model_config = SettingsConfigDict(
        env_file=None,  # dotenv is loaded above, before instantiation
        case_sensitive=False,
        extra="ignore",
    )

    # --- Environment ------------------------------------------------------
    app_env: Literal["development", "production"] = "development"
    log_level: str = "INFO"
    tz: str = "Europe/Istanbul"

    # --- Database ---------------------------------------------------------
    database_url: str = "sqlite:///./soneet.db"

    # --- Session & authentication ----------------------------------------
    secret_key: str = "dev-only-insecure-key-change-me"
    session_timeout_minutes: int = Field(default=60, ge=5, le=1440)
    bcrypt_rounds: int = Field(default=12, ge=10, le=16)
    max_failed_logins: int = Field(default=5, ge=1, le=20)
    lockout_minutes: int = Field(default=15, ge=1, le=1440)

    # --- Storage ----------------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_root: Path = PROJECT_ROOT / "uploads"
    generated_quotes_dir: Path = PROJECT_ROOT / "generated_quotes"

    storage_bucket: str = ""
    storage_endpoint_url: str = ""
    storage_region: str = "auto"
    storage_access_key_id: str = ""
    storage_secret_access_key: str = ""

    # --- Customer portal (the separate public service) ---------------------
    #: Public base URL the customer's link points at, e.g.
    #: https://quotes.iglandbros.com. Used to build links and to validate the
    #: Origin of state-changing requests. Never hardcoded anywhere.
    portal_base_url: str = ""
    #: Shown on the portal's "link not available" page so a customer who
    #: hits an expired link knows who to contact. Optional.
    portal_support_email: str = ""
    #: How long a customer link stays live when the quotation carries no
    #: validity date of its own.
    portal_link_days: int = Field(default=30, ge=1, le=365)

    # --- Portal branding ---------------------------------------------------
    # Identity — legal name, address, phone, email, logo — always comes from
    # CompanySettings in the database. These override only how the brand is
    # *presented*, so the portal is not wired to one company. Blank means "use
    # the company identity", which is the default.
    #
    # Migration path when brand needs to vary per quotation rather than per
    # deployment: add a nullable ``quotations.brand_code`` and resolve the
    # profile from it, falling back to these values. No portal code changes
    # shape; only the lookup gains a row to read.
    portal_brand_name: str = ""
    portal_brand_slogan: str = ""
    portal_brand_legal_footer: str = ""
    portal_brand_primary: str = ""
    portal_brand_secondary: str = ""
    portal_brand_accent: str = ""
    #: Fixed-window rate limits. Generous enough that a customer refreshing or
    #: sharing the link with a colleague is never affected.
    portal_view_rate_per_minute: int = Field(default=60, ge=1, le=1000)
    portal_submit_rate_per_hour: int = Field(default=10, ge=1, le=200)

    # --- Email delivery ----------------------------------------------------
    #: The backend actually used. "memory" captures and sends nothing (tests),
    #: "console" logs a redacted summary and cannot open a socket (development),
    #: "smtp" is the only one that reaches the internet.
    email_backend: Literal["memory", "console", "smtp"] = "console"
    #: Master switch. With this off, messages are still queued — the durable
    #: intent is the business record — but the worker leaves them alone. That is
    #: deliberately not the same as deleting them: turning sending back on
    #: should deliver what accumulated, not silently drop it.
    email_enabled: bool = False

    email_from_address: str = ""
    email_from_name: str = ""
    #: Where a customer's reply goes, when it should not go to the From address.
    email_reply_to: str = ""
    #: Internal notifications — approvals and change requests — land here.
    #: Comma-separated; blank means internal notifications are not queued.
    email_internal_recipients: str = ""

    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    #: "starttls" upgrades a plain connection, "tls" connects wrapped. There is
    #: deliberately no "none": an unencrypted SMTP session carries the
    #: credentials and the customer's link in clear text.
    smtp_security: Literal["starttls", "tls"] = "starttls"
    smtp_timeout_seconds: int = Field(default=20, ge=1, le=120)

    #: Versioned keys for sealing the capability payload an invitation needs:
    #: ``v1:base64key,v2:base64key``. Rotation means adding a version and
    #: pointing EMAIL_PAYLOAD_KEY_VERSION at it; the old key stays so rows
    #: queued under it can still be opened.
    email_payload_keys: str = ""
    email_payload_key_version: str = ""
    #: How long a sealed invitation payload stays openable. After this the row
    #: can no longer be sent or resent and the ciphertext is erased, so a link
    #: cannot be resurrected from an old queue row indefinitely.
    email_payload_ttl_hours: int = Field(default=72, ge=1, le=720)

    #: Delivery attempts before a message is given up on and shown as failed.
    email_max_attempts: int = Field(default=6, ge=1, le=20)

    # --- Background worker -------------------------------------------------
    #: Seconds between sweeps in continuous mode.
    worker_poll_seconds: int = Field(default=30, ge=5, le=3600)
    #: How many rows each subsystem takes per sweep.
    worker_batch_size: int = Field(default=20, ge=1, le=500)
    #: How long a worker's claim on a row is honoured before another may take it.
    worker_lease_seconds: int = Field(default=300, ge=30, le=3600)
    #: A health signal older than this is reported to employees as stale. Wider
    #: than the poll interval on purpose: a worker that misses one sweep has not
    #: stopped, and an indicator that cries wolf gets ignored.
    worker_stale_after_seconds: int = Field(default=900, ge=60, le=86400)

    # --- Uploads ----------------------------------------------------------
    max_upload_mb: int = Field(default=10, ge=1, le=100)
    allowed_upload_extensions: str = ".xlsx,.xls,.png,.jpg,.jpeg,.pdf"

    # ------------------------------------------------------------------ #
    # Derived / validated
    # ------------------------------------------------------------------ #

    @field_validator("database_url")
    @classmethod
    def _use_the_installed_driver(cls, value: str) -> str:
        """Point a bare PostgreSQL URL at psycopg 3, which is what is installed.

        Every provider — Neon, Supabase, Render, AWS — hands out a connection
        string beginning ``postgresql://``. SQLAlchemy reads that as "use
        psycopg2", which this project does not ship: ``requirements.txt`` pins
        ``psycopg[binary]``, version 3.

        The result is a ``ModuleNotFoundError: No module named 'psycopg2'`` at
        the moment of connecting — during a deploy's migration step, with a
        traceback that says nothing about the URL being the cause. Which driver
        the application uses is its own business, not something an operator
        should have to know to paste a connection string correctly.

        ``postgres://`` is handled too: it is the older form and several
        providers still emit it.
        """
        url = (value or "").strip()
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @field_validator("allowed_upload_extensions")
    @classmethod
    def _normalise_extensions(cls, value: str) -> str:
        parts = [p.strip().lower() for p in value.split(",") if p.strip()]
        return ",".join(p if p.startswith(".") else f".{p}" for p in parts)

    @model_validator(mode="after")
    def _check_production_requirements(self) -> Settings:
        """Fail loudly at import time rather than subtly at runtime.

        A production deployment running on the sample SECRET_KEY, on SQLite
        (whose file the Community Cloud container discards on every redeploy),
        or with S3 selected but unconfigured, is a misconfiguration that must
        stop the app rather than silently lose data.
        """
        if self.app_env != "production":
            return self

        problems: list[str] = []
        if self.secret_key == "dev-only-insecure-key-change-me" or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set to a unique value of at least 32 characters")
        if self.database_url.startswith("sqlite"):
            problems.append(
                "DATABASE_URL must point at PostgreSQL in production — the Community Cloud "
                "filesystem is ephemeral and a SQLite file is discarded on every redeploy"
            )
        if self.storage_backend == "local":
            problems.append(
                "STORAGE_BACKEND must be 's3' in production — uploaded price lists and "
                "generated PDFs are durable records and cannot live on an ephemeral disk"
            )
        if self.storage_backend == "s3":
            # Named individually rather than as a group. The combined message
            # sent an operator hunting for STORAGE_BUCKET when it was already
            # set and only the two keys were missing.
            absent = [
                name for name, value in (
                    ("STORAGE_BUCKET", self.storage_bucket),
                    ("STORAGE_ACCESS_KEY_ID", self.storage_access_key_id),
                    ("STORAGE_SECRET_ACCESS_KEY", self.storage_secret_access_key),
                ) if not value
            ]
            if absent:
                problems.append(
                    f"{', '.join(absent)} "
                    f"{'is' if len(absent) == 1 else 'are'} required when "
                    "STORAGE_BACKEND=s3"
                )

        problems += self._email_problems()

        if problems:
            raise ValueError(
                "Invalid production configuration:\n  - " + "\n  - ".join(problems)
            )
        return self

    def _email_problems(self) -> list[str]:
        """Fail closed: sending enabled but unconfigured must stop the process.

        Only checked when sending is actually enabled. A production deployment
        that has not turned email on yet is a normal state, not a fault — the
        outbox fills up and waits, which is the behaviour the durable queue
        exists to provide.
        """
        if not self.email_enabled:
            return []

        problems: list[str] = []
        if self.email_backend != "smtp":
            problems.append(
                "EMAIL_BACKEND must be 'smtp' when EMAIL_ENABLED is true in "
                "production — 'memory' and 'console' deliver nothing, so a "
                "customer would never receive their quotation"
            )
        if not self.email_from_address:
            problems.append("EMAIL_FROM_ADDRESS is required when email is enabled")
        if self.email_backend == "smtp":
            if not self.smtp_host:
                problems.append("SMTP_HOST is required when EMAIL_BACKEND=smtp")
            # No check for a password: some relays authorise by IP or by
            # submission certificate, and demanding one would block a valid
            # setup. The transport security check below is the one that matters.
        if not self.email_payload_keys:
            problems.append(
                "EMAIL_PAYLOAD_KEYS is required when email is enabled — the "
                "invitation carries a capability URL, which is never stored in "
                "clear text"
            )
        return problems

    # ------------------------------------------------------------------ #
    # Convenience accessors
    # ------------------------------------------------------------------ #

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def allowed_extensions(self) -> frozenset[str]:
        return frozenset(self.allowed_upload_extensions.split(","))

    @property
    def internal_recipients(self) -> tuple[str, ...]:
        """Internal notification addresses, split and cleaned."""
        return tuple(
            part.strip()
            for part in (self.email_internal_recipients or "").split(",")
            if part.strip()
        )

    def redacted(self) -> dict[str, Any]:
        """Settings safe to display on a diagnostics screen."""
        secret_fields = {
            "secret_key",
            "storage_secret_access_key",
            "storage_access_key_id",
            "database_url",
            # Credentials and key material. A diagnostics screen is exactly the
            # sort of place these leak from — it is meant to be looked at.
            "smtp_password",
            "smtp_username",
            "email_payload_keys",
        }
        out: dict[str, Any] = {}
        for name in self.__class__.model_fields:
            value = getattr(self, name)
            out[name] = "***" if name in secret_fields and value else value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached because Streamlit re-runs the whole script on every interaction and
    re-parsing configuration each time would be wasteful.
    """
    # Lowercased deliberately. ``case_sensitive=False`` governs the *environment*
    # source only; keyword arguments are matched against field names exactly, and
    # ``extra="ignore"`` then drops anything that does not match — silently. A
    # secrets panel written in the documented UPPERCASE style therefore reached
    # this call and was discarded in full.
    #
    # It went unnoticed because Streamlit also promotes secrets to os.environ,
    # where the case-insensitive environment source picks them up — but only for
    # str/int/float. Booleans are excluded there on purpose, so EMAIL_ENABLED was
    # the one setting with no second path in, and fell back to its default False
    # while every neighbouring string setting worked.
    settings = Settings(**{k.lower(): v for k, v in _streamlit_secrets().items()})
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    return settings
