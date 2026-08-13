"""The background worker: everything that has to happen without a person.

Three queues need sweeping, and until now two of them only moved when somebody
pressed a button:

* **storage cleanups** — objects the database has stopped referencing;
* **accepted PDF jobs** — the immutable document an acceptance owes;
* **the email outbox** — messages queued by a business event.

Recovery must not depend on an employee noticing. A customer who accepts a
quotation at 11pm should have their confirmation and their signed copy without
anyone being awake, and a provider outage should heal itself when the provider
comes back.

**Imports nothing from Streamlit or FastAPI.** It runs as its own process,
against the same database, and does not care whether either application is up.
That matters for where it is deployed: Streamlit Community Cloud sleeps when
idle and is not a place a timer can live, so the worker belongs on the host that
runs the portal, on a small always-on machine, or on a scheduler invoking
``--once``.

Each subsystem is swept inside its own try block. A broken renderer must not
stop email going out, and an unreachable mail server must not stop PDFs being
produced — they fail independently, so they are run independently.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("worker")


@dataclass
class SweepResult:
    """What one pass did. Counts only — never a recipient, subject or key."""

    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    storage_cleared: int = 0
    documents_ready: int = 0
    emails_sent: int = 0
    emails_failed: int = 0
    #: Subsystems that raised. Named, not detailed: the detail is in the log.
    errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"storage={self.storage_cleared}",
            f"documents={self.documents_ready}",
            f"emails={self.emails_sent}",
        ]
        if self.emails_failed:
            parts.append(f"email_failures={self.emails_failed}")
        if self.errors:
            parts.append(f"errors={','.join(self.errors)}")
        return " ".join(parts)


# --------------------------------------------------------------------------- #
# The individual sweeps
# --------------------------------------------------------------------------- #

def sweep_storage_cleanups(limit: int) -> int:
    """Delete objects nothing references any more.

    Reuses :mod:`modules.logo_service`, which already refuses to touch anything
    outside the branding namespace and re-checks that a key is genuinely
    unreferenced before deleting it. A second implementation here would be a
    second chance to get that wrong.
    """
    from modules.database import session_scope
    from modules.logo_service import retry_pending_cleanups

    with session_scope() as session:
        return retry_pending_cleanups(session, limit=limit)


def sweep_document_jobs(limit: int) -> int:
    """Produce accepted PDFs that have not been produced yet.

    Until now this only ran as a background task on the acceptance request, so
    a request that died mid-flight left the job waiting for somebody to press
    "Try again". Sweeping it here is what makes the durable job actually
    durable.
    """
    from modules.quote_document_service import run_pending_jobs

    return run_pending_jobs(limit=limit)


def sweep_email_outbox(limit: int, owner: str) -> tuple[int, int]:
    """Send what is due. Returns ``(sent, failed)``."""
    from modules.email_outbox_service import run_once

    counts = run_once(owner=owner, limit=limit)
    return counts.get("sent", 0), counts.get("failed", 0)


def sweep_expired_payloads() -> int:
    """Erase sealed invitation links whose resend window has closed."""
    from modules.database import session_scope
    from modules.email_outbox_service import expire_stale_payloads

    with session_scope() as session:
        return expire_stale_payloads(session)


# --------------------------------------------------------------------------- #
# One pass
# --------------------------------------------------------------------------- #

def check_key_agreement() -> str | None:
    """Confirm this worker holds the same encryption key as the application.

    Returns a message describing the mismatch, or ``None`` when they agree.

    Worth failing loudly at startup rather than discovering it per message:
    with different key material the worker cannot open a single sealed
    invitation, every one fails ``link_unsealable``, and the only visible
    symptom is customers not receiving quotations. The version *label* being
    identical is not enough — that is chosen by whoever wrote the configuration,
    and is exactly what a half-applied rotation leaves matching.
    """
    from modules.database import session_scope
    from modules.secret_box import KeyAgreementError, verify_key_agreement

    try:
        with session_scope() as session:
            fingerprint = verify_key_agreement(session)
    except KeyAgreementError as exc:
        return str(exc)
    except Exception:  # noqa: BLE001 — an unreachable database is a separate fault
        log.warning("Could not verify email key agreement")
        return None

    if fingerprint:
        log.info("Email key agreement OK (%s)", fingerprint)
    return None


def run_sweep(*, batch_size: int | None = None, owner: str | None = None) -> SweepResult:
    """One pass over every queue. Never raises.

    Each subsystem is isolated: one failing is recorded and the rest still run.
    A worker that stopped sweeping email because a PDF renderer was broken would
    turn one outage into two.
    """
    from modules.config import get_settings
    from modules.email_outbox_service import worker_identity

    settings = get_settings()
    limit = batch_size or settings.worker_batch_size
    owner = owner or worker_identity()

    result = SweepResult(started_at=dt.datetime.now(dt.UTC))

    for name, action in (
        ("storage", lambda: _set(result, "storage_cleared", sweep_storage_cleanups(limit))),
        ("documents", lambda: _set(result, "documents_ready", sweep_document_jobs(limit))),
        ("payloads", sweep_expired_payloads),
        ("email", lambda: _email(result, limit, owner)),
    ):
        try:
            action()
        except Exception:  # noqa: BLE001 — one subsystem must not stop the others
            result.errors.append(name)
            log.exception("The %s sweep failed; continuing with the others", name)

    result.finished_at = dt.datetime.now(dt.UTC)
    log.info("sweep complete: %s", result.summary())
    _record_heartbeat(result)
    _write_health(result)
    return result


def _record_heartbeat(result: SweepResult) -> None:
    """Publish the sweep to the shared database.

    This is what the employee application actually reads. The file below is
    kept for local development, where one machine runs everything; in
    production the app, the portal and the worker share a database and nothing
    else, so a file-based signal would be invisible to the page that needs it.

    Failures here are logged and swallowed: a heartbeat that cannot be written
    is a monitoring problem, and refusing to sweep because of it would turn a
    cosmetic fault into an outage.
    """
    from modules.database import session_scope
    from modules.worker_heartbeat import record_sweep

    try:
        with session_scope() as session:
            record_sweep(
                session,
                healthy=result.healthy,
                summary=result.summary(),
            )
    except Exception:  # noqa: BLE001
        log.warning("Could not record the worker heartbeat; the sweep still ran")


def _set(result: SweepResult, field_name: str, value: int) -> int:
    setattr(result, field_name, value)
    return value


def _email(result: SweepResult, limit: int, owner: str) -> None:
    sent, failed = sweep_email_outbox(limit, owner)
    result.emails_sent = sent
    result.emails_failed = failed


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

#: Touched after every sweep. A host checks its mtime to answer "is the worker
#: alive" without needing a port, an HTTP server or a dependency — the worker
#: has no web surface and should not grow one just to be monitored.
HEALTH_FILE_ENV = "WORKER_HEALTH_FILE"


def _write_health(result: SweepResult) -> None:
    path = os.environ.get(HEALTH_FILE_ENV, "").strip()
    if not path:
        return
    try:
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Counts and a timestamp. Nothing identifying: this file is read by
        # monitoring, which has no business seeing who was emailed.
        target.write_text(
            f"{result.finished_at.isoformat() if result.finished_at else ''} "
            f"{'ok' if result.healthy else 'degraded'} {result.summary()}\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — monitoring must not break the worker
        log.debug("Could not write the health file")


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

class Worker:
    """Continuous mode, with a shutdown that finishes the pass it is in.

    Stopping mid-sweep would leave rows leased by a worker that no longer
    exists. They would recover when the lease expired, but making an operator
    wait five minutes for a clean restart is avoidable: the stop flag is checked
    between sweeps, not inside one.
    """

    def __init__(
        self,
        *,
        poll_seconds: int | None = None,
        batch_size: int | None = None,
        owner: str | None = None,
    ) -> None:
        from modules.config import get_settings
        from modules.email_outbox_service import worker_identity

        settings = get_settings()
        self.poll_seconds = poll_seconds or settings.worker_poll_seconds
        self.batch_size = batch_size or settings.worker_batch_size
        self.owner = owner or worker_identity()
        self._stop = threading.Event()
        self.sweeps = 0

    def request_stop(self, *_signal_args) -> None:  # noqa: ANN002
        log.info("Shutdown requested; finishing the current sweep")
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run_forever(self, max_sweeps: int | None = None) -> int:
        """Sweep until stopped. ``max_sweeps`` bounds it, for tests."""
        log.info(
            "Worker %s starting: every %ss, batches of %s",
            self.owner, self.poll_seconds, self.batch_size,
        )
        while not self._stop.is_set():
            run_sweep(batch_size=self.batch_size, owner=self.owner)
            self.sweeps += 1
            if max_sweeps is not None and self.sweeps >= max_sweeps:
                break
            # wait() rather than sleep(): a stop signal is acted on immediately
            # instead of after the remainder of the interval.
            self._stop.wait(self.poll_seconds)
        log.info("Worker %s stopped after %s sweep(s)", self.owner, self.sweeps)
        return self.sweeps


def install_signal_handlers(worker: Worker) -> None:
    """SIGINT and SIGTERM ask for a graceful stop rather than killing the pass."""
    for name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, name, None)
        if handler is not None:
            try:
                signal.signal(handler, worker.request_stop)
            except (ValueError, OSError):  # not the main thread, or unsupported
                log.debug("Could not install a %s handler", name)


def _configure_logging(level: str) -> None:
    """Structured enough to grep, and redacted.

    The portal's log filter is installed here too: it strips the token segment
    out of any URL that reaches a log line, so a stray message that quotes a
    capability URL is scrubbed before it is written.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    try:
        from portal.security import install_log_redaction

        install_log_redaction()
    except Exception:  # noqa: BLE001 — the worker must not need the portal package
        log.debug("Portal log redaction unavailable; continuing")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m modules.worker",
        description=(
            "Sweep storage cleanups, accepted-PDF jobs and the email outbox. "
            "Run with --once from a scheduler, or --loop as a service."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once", action="store_true",
        help="run a single sweep and exit (for cron or a scheduled task)",
    )
    mode.add_argument(
        "--loop", action="store_true",
        help="run continuously until interrupted",
    )
    parser.add_argument("--interval", type=int, default=None,
                        help="seconds between sweeps in --loop mode")
    parser.add_argument("--batch", type=int, default=None,
                        help="rows per subsystem per sweep")
    parser.add_argument("--max-sweeps", type=int, default=None,
                        help="stop after this many sweeps (mainly for testing)")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args(argv)

    from modules.config import get_settings

    settings = get_settings()
    _configure_logging(args.log_level or settings.log_level)

    # Before any work: a worker with the wrong key cannot deliver a single
    # invitation, and the only symptom would be customers not receiving
    # quotations. Refuse to start rather than fail quietly, message by message.
    mismatch = check_key_agreement()
    if mismatch:
        log.error("Refusing to start: %s", mismatch)
        return 2

    if not args.loop:
        result = run_sweep(batch_size=args.batch)
        # Non-zero when a subsystem failed, so a scheduler can alert on it.
        return 0 if result.healthy else 1

    worker = Worker(poll_seconds=args.interval, batch_size=args.batch)
    install_signal_handlers(worker)
    worker.run_forever(max_sweeps=args.max_sweeps)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
