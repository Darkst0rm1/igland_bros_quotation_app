"""Is the background worker running? Answered through the shared database.

The employee application, the customer portal and the worker run on three
different machines with no shared filesystem — Streamlit Community Cloud,
a Render web service and a Render background worker. The only thing all three
already share is the PostgreSQL database, so that is where the heartbeat lives.

The file-based signal it replaces still works and is kept for local development,
where one machine genuinely does run everything. In production a file the
employee app cannot read is worse than no signal at all: the page would say
"Not configured" while delivery ran perfectly, or look healthy while the worker
was dead.

Deliberately small. A heartbeat that grows fields becomes a monitoring system,
and this one has to be safe to render on a page a salesperson is looking at:
counts and timestamps, never a host, a path, a process id or an error.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from modules.models import WorkerHeartbeat

log = logging.getLogger(__name__)

#: The logical worker this deployment runs. One row, updated in place — "is the
#: worker running" is a question about the role, not about which container
#: happens to be serving it today.
DEFAULT_SERVICE = "quotation-worker"

STATUS_HEALTHY = "HEALTHY"
STATUS_DEGRADED = "DEGRADED"

#: Fallback when nothing is configured. Wider than the poll interval on purpose:
#: a worker that misses a single sweep has not stopped, and an indicator that
#: cries wolf is one people learn to ignore.
DEFAULT_STALE_SECONDS = 900


@dataclass(frozen=True)
class HeartbeatView:
    """What an employee may be shown about the worker.

    No field carries a host, a path, a worker identity or an error message —
    a page cannot render what the shape does not hold.
    """

    is_configured: bool = False
    is_healthy: bool = False
    degraded: bool = False
    last_success_at: dt.datetime | None = None
    last_attempt_at: dt.datetime | None = None
    summary: str = ""
    environment: str = ""
    age_seconds: int | None = None

    @property
    def is_stale(self) -> bool:
        return self.is_configured and not self.is_healthy

    @property
    def label(self) -> str:
        if not self.is_configured:
            return "Not configured"
        if not self.is_healthy:
            return "Not running recently"
        return "Degraded" if self.degraded else "Running"

    @property
    def detail(self) -> str:
        if not self.is_configured:
            return (
                "The delivery worker has never reported. Messages will be "
                "queued and sent once it is running."
            )
        if not self.is_healthy:
            return (
                "The delivery worker has not reported recently. Messages will "
                "be queued and sent when it next runs."
            )
        if self.degraded:
            return (
                "The worker is running but reported a problem on its last pass."
            )
        return "The delivery worker is running normally."


# --------------------------------------------------------------------------- #
# Writing — the worker
# --------------------------------------------------------------------------- #

def record_sweep(
    session: Session,
    *,
    healthy: bool,
    summary: str = "",
    service_name: str = DEFAULT_SERVICE,
    environment: str = "",
    now: dt.datetime | None = None,
) -> WorkerHeartbeat:
    """Record that a sweep happened. Called by the worker after every pass.

    ``last_attempt_at`` moves on every sweep; ``last_success_at`` only when the
    sweep was clean. Staleness is measured against the second, so a worker
    looping on failures reads as stale rather than healthy — which is the
    honest answer, because nothing is being delivered.

    Upserts on ``service_name``. Two workers racing to create the first row
    collide on the unique index; the loser re-reads and updates, so the outcome
    is one row either way.
    """
    now = now or dt.datetime.now(dt.UTC)
    summary = (summary or "")[:200]
    environment = (environment or _environment())[:40]

    row = session.execute(
        select(WorkerHeartbeat).where(
            WorkerHeartbeat.service_name == service_name
        )
    ).scalar_one_or_none()

    if row is None:
        row = WorkerHeartbeat(service_name=service_name)
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            # Another worker created it between the select and the insert.
            session.rollback()
            row = session.execute(
                select(WorkerHeartbeat).where(
                    WorkerHeartbeat.service_name == service_name
                )
            ).scalar_one()

    row.status = STATUS_HEALTHY if healthy else STATUS_DEGRADED
    row.last_attempt_at = now
    if healthy:
        row.last_success_at = now
    row.summary = summary
    row.environment = environment
    row.updated_at = now
    session.flush()
    return row


def _environment() -> str:
    from modules.config import get_settings

    return get_settings().app_env


# --------------------------------------------------------------------------- #
# Reading — the employee application
# --------------------------------------------------------------------------- #

def read(
    session: Session,
    *,
    service_name: str = DEFAULT_SERVICE,
    stale_after_seconds: int | None = None,
    now: dt.datetime | None = None,
) -> HeartbeatView:
    """What the worker last reported, as an employee may see it.

    A missing row means the worker has never run against this database, which
    is a genuinely different state from "ran, but a while ago" — the first is a
    deployment that is not finished, the second is one that has stopped.
    """
    now = now or dt.datetime.now(dt.UTC)
    threshold = stale_after_seconds or _stale_threshold()

    row = session.execute(
        select(WorkerHeartbeat).where(
            WorkerHeartbeat.service_name == service_name
        )
    ).scalar_one_or_none()

    if row is None:
        return HeartbeatView(is_configured=False)

    last_success = _aware(row.last_success_at)
    age = int((now - last_success).total_seconds()) if last_success else None

    return HeartbeatView(
        is_configured=True,
        is_healthy=age is not None and age <= threshold,
        degraded=row.status == STATUS_DEGRADED,
        last_success_at=last_success,
        last_attempt_at=_aware(row.last_attempt_at),
        summary=row.summary or "",
        environment=row.environment or "",
        age_seconds=age,
    )


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite hands back naive datetimes; compare in UTC regardless."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _stale_threshold() -> int:
    from modules.config import get_settings

    return getattr(
        get_settings(), "worker_stale_after_seconds", DEFAULT_STALE_SECONDS
    )


def table_exists(session: Session) -> bool:
    """Whether the heartbeat table has been migrated in. For the preflight."""
    from sqlalchemy import inspect

    try:
        return inspect(session.get_bind()).has_table(WorkerHeartbeat.__tablename__)
    except Exception:  # noqa: BLE001
        return False
