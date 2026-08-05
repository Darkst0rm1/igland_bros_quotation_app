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

    # --- Uploads ----------------------------------------------------------
    max_upload_mb: int = Field(default=10, ge=1, le=100)
    allowed_upload_extensions: str = ".xlsx,.xls,.png,.jpg,.jpeg,.pdf"

    # ------------------------------------------------------------------ #
    # Derived / validated
    # ------------------------------------------------------------------ #

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
        if self.storage_backend == "s3" and not (
            self.storage_bucket and self.storage_access_key_id and self.storage_secret_access_key
        ):
            problems.append(
                "STORAGE_BUCKET, STORAGE_ACCESS_KEY_ID and STORAGE_SECRET_ACCESS_KEY are "
                "required when STORAGE_BACKEND=s3"
            )

        if problems:
            raise ValueError(
                "Invalid production configuration:\n  - " + "\n  - ".join(problems)
            )
        return self

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

    def redacted(self) -> dict[str, Any]:
        """Settings safe to display on a diagnostics screen."""
        secret_fields = {
            "secret_key",
            "storage_secret_access_key",
            "storage_access_key_id",
            "database_url",
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
    settings = Settings(**_streamlit_secrets())
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    return settings
