"""Check a deployment before it is trusted with a customer's quotation.

Everything here is read-only or self-cleaning. It writes one temporary object
and deletes it; it touches no business data; and it **never sends email** — the
SMTP check inspects configuration shape only, because a preflight that mails
somebody is a preflight nobody runs twice.

Nothing is printed that could be pasted into a support ticket by mistake. Every
connection string, key, password and token is reduced to `set` / `MISSING` or a
redacted host, so the output is safe to share.

    python -m scripts.preflight

Exit codes: 0 all clear, 1 something must be fixed before going live.
"""
from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass, field

#: Anything matching these is never printed, whatever else happens.
_SECRET_PATTERN = re.compile(
    r"(password|secret|token|api[_-]?key|access[_-]?key|payload_keys)",
    re.IGNORECASE,
)

PASS = "pass"
WARN = "warn"
FAIL = "fail"

_MARK = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


@dataclass
class Report:
    """Accumulated results. Only ``fail`` decides the exit code."""

    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, status: str, label: str, detail: str = "") -> None:
        self.rows.append((status, label, redact(detail)))

    def ok(self, label: str, detail: str = "") -> None:
        self.add(PASS, label, detail)

    def warn(self, label: str, detail: str = "") -> None:
        self.add(WARN, label, detail)

    def fail(self, label: str, detail: str = "") -> None:
        self.add(FAIL, label, detail)

    @property
    def failed(self) -> bool:
        return any(status == FAIL for status, _, _ in self.rows)

    def render(self) -> str:
        lines = []
        for status, label, detail in self.rows:
            line = f"[{_MARK[status]}] {label}"
            if detail:
                line += f"\n            {detail}"
            lines.append(line)
        return "\n".join(lines)


def redact(text: str) -> str:
    """Strip anything that looks like a credential out of a message.

    Belt and braces: nothing in this file deliberately prints a secret, but a
    driver's exception text quotes the connection string it failed on, and that
    is exactly the string with the password in it.
    """
    if not text:
        return ""
    # postgresql://user:password@host/db  ->  postgresql://***@host/db
    text = re.sub(r"://[^/@\s]+:[^/@\s]+@", "://***@", text)
    # key=value pairs whose key looks sensitive
    text = re.sub(
        r"(?i)\b(\w*(?:password|secret|token|key)\w*)\s*[=:]\s*\S+",
        r"\1=***",
        text,
    )
    return text


def describe(value: str | None) -> str:
    """`set` or `MISSING` — never the value itself."""
    return "set" if (value or "").strip() else "MISSING"


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def check_required_settings(report: Report, settings) -> None:  # noqa: ANN001
    """Everything production refuses to start without."""
    required = {
        "DATABASE_URL": settings.database_url,
        "SECRET_KEY": settings.secret_key,
        "STORAGE_BUCKET": settings.storage_bucket,
        "STORAGE_ACCESS_KEY_ID": settings.storage_access_key_id,
        "STORAGE_SECRET_ACCESS_KEY": settings.storage_secret_access_key,
        "PORTAL_BASE_URL": settings.portal_base_url,
        "EMAIL_PAYLOAD_KEYS": settings.email_payload_keys,
    }
    missing = [name for name, value in required.items() if not (value or "").strip()]

    for name, value in required.items():
        # Names, states, never values.
        if _SECRET_PATTERN.search(name):
            detail = describe(value)
        else:
            detail = describe(value)
        if name in missing:
            report.fail(f"{name}", detail)
        else:
            report.ok(f"{name}", detail)

    if settings.app_env != "production":
        report.warn(
            "APP_ENV", f"{settings.app_env} — production checks are relaxed"
        )
    else:
        report.ok("APP_ENV", "production")

    if settings.secret_key == "dev-only-insecure-key-change-me":
        report.fail("SECRET_KEY", "still the sample value")


def check_portal_url(report: Report, settings) -> None:  # noqa: ANN001
    """The customer's link points here. A guess is worse than a refusal."""
    url = (settings.portal_base_url or "").strip()
    if not url:
        report.fail(
            "Portal URL",
            "PORTAL_BASE_URL is empty. Set it to the service's public origin "
            "before starting the portal.",
        )
        return

    if not url.startswith("https://"):
        report.fail("Portal URL", f"must be https — got {url.split(':')[0]}://…")
        return
    if url.rstrip("/").count("/") < 2 or len(url) < len("https://a.b"):
        report.fail("Portal URL", "does not look like a complete origin")
        return
    if "r2.cloudflarestorage.com" in url:
        report.fail(
            "Portal URL",
            "this is the object-storage endpoint, not the portal address",
        )
        return
    if url.endswith("/"):
        report.warn("Portal URL", "has a trailing slash; it will be stripped")
    else:
        report.ok("Portal URL", url)


def check_database(report: Report, settings) -> None:  # noqa: ANN001
    """Reachable, and at the migration revision this code expects."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    try:
        url = make_url(settings.database_url)
        where = f"{url.drivername} on {url.host or 'local'}/{url.database or '?'}"
    except Exception:  # noqa: BLE001
        report.fail("Database URL", "could not be parsed")
        return

    if settings.is_production and url.drivername.startswith("sqlite"):
        report.fail("Database", "SQLite in production — data is lost on redeploy")
        return

    try:
        from modules.database import session_scope

        with session_scope() as session:
            session.execute(text("SELECT 1"))
        report.ok("Database reachable", where)
    except Exception as exc:  # noqa: BLE001
        report.fail("Database reachable", f"{type(exc).__name__}: {exc}")
        return

    _check_migration_head(report)


def _check_migration_head(report: Report) -> None:
    from modules.database import schema_revisions

    try:
        applied, expected = schema_revisions()
    except Exception as exc:  # noqa: BLE001
        report.warn("Migration revision", f"could not be read: {type(exc).__name__}")
        return

    if applied is None:
        report.fail("Migration revision", "no schema — run alembic upgrade head")
    elif expected is None:
        report.warn("Migration revision", f"applied {applied}; expected unknown")
    elif applied != expected:
        report.fail(
            "Migration revision",
            f"at {applied}, this code expects {expected} — run alembic upgrade head",
        )
    else:
        report.ok("Migration revision", f"at head ({applied})")


def check_heartbeat_table(report: Report) -> None:
    """The employee app reads worker health from here."""
    from modules.database import session_scope
    from modules.worker_heartbeat import read, table_exists

    try:
        with session_scope() as session:
            if not table_exists(session):
                report.fail(
                    "Worker heartbeat table",
                    "missing — run alembic upgrade head",
                )
                return
            view = read(session)
        if not view.is_configured:
            report.warn(
                "Worker heartbeat",
                "the worker has not reported yet — expected before it starts",
            )
        elif view.is_healthy:
            report.ok("Worker heartbeat", f"{view.label} ({view.summary})")
        else:
            report.warn("Worker heartbeat", view.label)
    except Exception as exc:  # noqa: BLE001
        report.fail("Worker heartbeat table", f"{type(exc).__name__}")


def check_storage(report: Report, settings) -> None:  # noqa: ANN001
    """Write, read back, delete. The same adapter the application uses."""
    if settings.storage_backend != "s3":
        message = "STORAGE_BACKEND is not 's3'"
        (report.fail if settings.is_production else report.warn)(
            "Object storage", message
        )
        return

    report.ok(
        "Storage config",
        f"bucket={settings.storage_bucket} region={settings.storage_region} "
        f"endpoint={'set' if settings.storage_endpoint_url else 'default AWS'}",
    )

    key = f"_preflight/{uuid.uuid4().hex}.txt"
    payload = b"soneet preflight round trip"
    try:
        from modules.storage import get_storage

        storage = get_storage()
        storage.put(key, payload, "text/plain")
        if not storage.exists(key):
            report.fail("Storage round trip", "written but not found")
            return
        if storage.get(key) != payload:
            report.fail("Storage round trip", "bytes did not match")
            return
        storage.delete(key)
        report.ok("Storage round trip", "write, read, delete all succeeded")
    except Exception as exc:  # noqa: BLE001
        # Type and a truncated message only: a botocore error quotes the signed
        # request, and the signature is derived from the secret key.
        report.fail("Storage round trip", f"{type(exc).__name__}: {str(exc)[:160]}")


def check_key_agreement(report: Report, settings) -> None:  # noqa: ANN001
    """Portal and worker must seal and open with the same key material.

    Different keys under the same version label is the failure that delivers
    nothing while looking healthy, so it is checked explicitly rather than
    discovered per message.
    """
    from modules.secret_box import (
        KeyAgreementError,
        KeyringError,
        key_fingerprint,
        verify_key_agreement,
    )

    if not settings.email_payload_keys.strip():
        (report.fail if settings.is_production else report.warn)(
            "Encryption keys",
            "EMAIL_PAYLOAD_KEYS is not set — invitations cannot be sealed",
        )
        return

    try:
        fingerprint = key_fingerprint()
    except KeyringError as exc:
        report.fail("Encryption keys", str(exc))
        return

    # The fingerprint is an HMAC of a fixed label — it identifies the key
    # without revealing it, which is what makes it safe to print here.
    report.ok("Encryption key version", fingerprint.split(":", 1)[0])

    try:
        from modules.database import session_scope

        with session_scope() as session:
            verify_key_agreement(session)
        report.ok("Key agreement", "this process matches the deployment")
    except KeyAgreementError as exc:
        report.fail("Key agreement", str(exc))
    except Exception as exc:  # noqa: BLE001
        report.warn("Key agreement", f"could not be checked: {type(exc).__name__}")


def check_email(report: Report, settings) -> None:  # noqa: ANN001
    """Shape only. **Nothing is sent.**"""
    if settings.email_enabled:
        report.warn(
            "Email delivery",
            "ENABLED — customers will receive messages. Expected to be false "
            "during the first deployment.",
        )
    else:
        report.ok(
            "Email delivery",
            "disabled — messages will queue and wait, which is the safe "
            "starting state",
        )

    report.ok("Email backend", settings.email_backend)
    if settings.email_enabled and settings.email_backend != "smtp":
        report.fail(
            "Email backend",
            f"'{settings.email_backend}' delivers nothing while enabled",
        )

    if settings.email_backend == "smtp":
        report.add(
            PASS if settings.smtp_host else (FAIL if settings.email_enabled else WARN),
            "SMTP host", describe(settings.smtp_host),
        )
        report.ok("SMTP port", str(settings.smtp_port))
        report.ok("SMTP security", settings.smtp_security)
        report.ok("SMTP username", describe(settings.smtp_username))
        report.ok("SMTP password", describe(settings.smtp_password))
        if settings.smtp_security not in {"starttls", "tls"}:
            report.fail("SMTP security", "must be starttls or tls")
        if settings.smtp_port == 465 and settings.smtp_security != "tls":
            report.warn("SMTP", "port 465 usually pairs with SMTP_SECURITY=tls")
        if settings.smtp_port == 587 and settings.smtp_security != "starttls":
            report.warn("SMTP", "port 587 usually pairs with SMTP_SECURITY=starttls")

    from modules.email_backend import InvalidRecipientError, validate_address

    if settings.email_from_address:
        try:
            validate_address(settings.email_from_address)
            report.ok("Sender address", "valid")
        except InvalidRecipientError:
            report.fail("Sender address", "not a valid address")
    elif settings.email_enabled:
        report.fail("Sender address", "EMAIL_FROM_ADDRESS is required")
    else:
        report.warn("Sender address", "not set yet")

    recipients = settings.internal_recipients
    report.ok(
        "Internal recipients",
        f"{len(recipients)} configured" if recipients else "none — no internal notices",
    )
    report.ok("Preflight sent no email", "by construction; this check is shape only")


def check_company_readiness(report: Report) -> None:
    """A customer must never receive a quotation headed by placeholder details."""
    from modules.database import session_scope
    from modules.portal_readiness import check

    try:
        with session_scope() as session:
            readiness = check(session)
    except Exception as exc:  # noqa: BLE001
        report.warn("Company readiness", f"could not be checked: {type(exc).__name__}")
        return

    outstanding = [r.label for r in readiness.outstanding]
    if readiness.is_complete:
        # Deliberately not the word "complete" on its own. ``check`` was called
        # without a quotation, so it skipped the requirements scoped to one —
        # terms and expiry. Saying "complete" here reads as "ready to send",
        # and a deployment can pass this line while an individual quotation is
        # still refused.
        report.ok(
            "Company readiness",
            "company details complete — each quotation is additionally "
            "checked for terms and an expiry date when sent",
        )
    elif readiness.may_issue_link:
        report.warn("Company readiness", f"outstanding: {', '.join(outstanding)}")
    else:
        report.fail(
            "Company readiness",
            f"sending is blocked until these are set: {', '.join(outstanding)}",
        )


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

def run() -> Report:
    """Every check, in the order somebody would fix them."""
    from modules.config import get_settings

    report = Report()
    settings = get_settings()

    check_required_settings(report, settings)
    check_portal_url(report, settings)
    check_database(report, settings)
    check_heartbeat_table(report)
    check_storage(report, settings)
    check_key_agreement(report, settings)
    check_email(report, settings)
    check_company_readiness(report)
    return report


def main() -> int:
    report = run()
    print("Soneet deployment preflight")
    print("=" * 60)
    print(report.render())
    print("=" * 60)

    failures = sum(1 for s, _, _ in report.rows if s == FAIL)
    warnings = sum(1 for s, _, _ in report.rows if s == WARN)

    if report.failed:
        print(f"\n{failures} blocking problem(s), {warnings} warning(s). "
              "Fix the failures before starting the services.")
        return 1
    print(f"\nAll checks passed with {warnings} warning(s). Safe to start.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
