"""Producing, storing and retrieving the immutable accepted-quotation PDF.

The rule this module exists to keep: **an acceptance is never contingent on a
PDF.** The customer's decision is the business event. Rendering a document and
putting an object in storage are two things that can be down, slow or broken,
and none of that may stop somebody accepting a quotation or make an accepted
quotation look unaccepted afterwards.

So the acceptance and a *job* commit together, in one transaction, and the
document is produced afterwards. If that fails the quote is still accepted and
the job is still there to retry.

The second rule: **an accepted document is never rewritten.** It is what the
customer agreed to. Retrying must therefore converge on one set of bytes rather
than produce a fresh document each time, which is why the storage key is
derived from the accepted response's own identity:

* a retry lands on the same key and adopts bytes an earlier attempt wrote,
  rather than making a second document;
* the artifact row is unique per response, so two workers that both render can
  only produce one published result — the loser adopts the winner's;
* hash and size are recorded when the row is written and verified before every
  retrieval, so bytes that change underneath us are detected rather than served.

When verification fails the artifact is **quarantined**, not regenerated.
Regenerating would silently replace an agreement with a document nobody signed;
a quarantined row keeps the evidence and asks for a human.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from modules.constants import (
    ArtifactStatus,
    DocumentJobStatus,
    PortalResponseType,
)
from modules.models import (
    CompanySettings,
    PortalResponse,
    Quotation,
    QuoteDocumentArtifact,
    QuoteDocumentJob,
)
from modules.storage import StorageError, get_storage

log = logging.getLogger(__name__)

#: Retries stop here. A backend that has refused five times is broken in a way
#: another attempt will not fix, and an unbounded queue hammering it makes the
#: outage worse. The job stays FAILED and visible until somebody looks.
MAX_ATTEMPTS = 5

#: How long a worker's claim on a job is honoured. Long enough for a render,
#: short enough that a worker killed mid-job does not strand it.
LEASE_SECONDS = 300

#: Nothing outside this prefix is ever written or deleted by this module.
ARTIFACT_NAMESPACE = "quotes/accepted/"


class DocumentJobError(RuntimeError):
    """Generation failed. Internal — never shown to a customer or an employee."""


class ArtifactIntegrityError(RuntimeError):
    """Stored bytes do not match what was recorded for them.

    Carries which artifact and why, so the quarantine can be re-applied if the
    transaction that observed the mismatch has to be rolled back. Observing
    that immutable bytes changed is a fact worth keeping even when everything
    else about the attempt is discarded.
    """

    def __init__(
        self, message: str, *, artifact_id: int | None = None, reason: str = ""
    ) -> None:
        super().__init__(message)
        self.artifact_id = artifact_id
        self.reason = reason or message


@dataclass(frozen=True)
class DocumentState:
    """What an employee is told about an acceptance's document.

    Carries the status and nothing else. ``last_error`` holds a stack of
    internal detail and is deliberately not on this shape, so a page cannot
    render it by accident.
    """

    status: DocumentJobStatus
    attempts: int = 0
    generated_at: dt.datetime | None = None
    byte_size: int | None = None

    @property
    def is_ready(self) -> bool:
        return self.status is DocumentJobStatus.READY

    @property
    def is_retryable(self) -> bool:
        return self.status is DocumentJobStatus.FAILED


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #

def artifact_key(response: PortalResponse) -> str:
    """Where this acceptance's document lives. Deterministic, and ours.

    Derived only from identifiers this application issued — the response id,
    the quotation id and the revision number. No customer input reaches it, so
    it cannot be steered, and a retry always computes the same key and finds
    the object a previous attempt left.
    """
    return (
        f"{ARTIFACT_NAMESPACE}{response.quotation_id}/"
        f"r{response.revision_no}_response{response.id}.pdf"
    )


def is_within_artifact_namespace(storage_key: str) -> bool:
    """Whether a key is one this module may touch."""
    key = (storage_key or "").strip()
    if not key or not key.startswith(ARTIFACT_NAMESPACE):
        return False
    if ".." in key or key.startswith("/") or "\\" in key:
        return False
    return True


# --------------------------------------------------------------------------- #
# Enqueueing
# --------------------------------------------------------------------------- #

def enqueue(session: Session, response: PortalResponse) -> QuoteDocumentJob | None:
    """Record that this acceptance owes a document. Idempotent.

    Called inside the acceptance transaction, so the job and the response
    commit together or not at all. Returns ``None`` for anything that is not an
    acceptance — a change request has no document.
    """
    if response.response_type is not PortalResponseType.APPROVED:
        return None

    existing = session.execute(
        select(QuoteDocumentJob).where(
            QuoteDocumentJob.portal_response_id == response.id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # A job added but not yet flushed is invisible to the SELECT above, so the
    # pending set has to be checked too or a second call in the same
    # transaction would queue a duplicate and fail on the unique index.
    pending = next(
        (
            obj for obj in session.new
            if isinstance(obj, QuoteDocumentJob)
            and obj.portal_response_id == response.id
        ),
        None,
    )
    if pending is not None:
        return pending

    job = QuoteDocumentJob(
        portal_response_id=response.id,
        quotation_id=response.quotation_id,
        revision_no=response.revision_no,
        status=DocumentJobStatus.PENDING,
    )
    session.add(job)
    return job


def reconcile(session: Session, limit: int = 200) -> int:
    """Give every acceptance without a job a pending one. Idempotent.

    The safety net for acceptances that predate this feature, and for any that
    somehow escaped :func:`enqueue`. The migration runs the same statement once;
    this can be run whenever, and adds nothing when there is nothing to add.
    """
    result = session.execute(
        text(
            """
            INSERT INTO quote_document_jobs (
                portal_response_id, quotation_id, revision_no,
                status, attempts, created_at
            )
            SELECT r.id, r.quotation_id, r.revision_no,
                   'PENDING', 0, CURRENT_TIMESTAMP
            FROM portal_responses AS r
            WHERE r.response_type = 'APPROVED'
              AND NOT EXISTS (
                  SELECT 1 FROM quote_document_jobs AS j
                  WHERE j.portal_response_id = r.id
              )
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    session.flush()
    return result.rowcount or 0


# --------------------------------------------------------------------------- #
# Building the bytes
# --------------------------------------------------------------------------- #

def build_accepted_pdf(session: Session, response: PortalResponse) -> bytes:
    """Render the accepted document for one response.

    Assembled from the customer-safe model only. The employee document model
    never enters this path: there is no call here that could produce one, and
    the renderer would not accept it.
    """
    from modules import pricing_snapshot, settings_service
    from portal import pdf_model, pdf_renderer

    quotation = session.get(Quotation, response.quotation_id)
    if quotation is None:
        raise DocumentJobError("The quotation for this acceptance is missing.")

    # The scope is what they selected, so the line-level figures match the
    # totals that were recorded. The totals themselves come off the response.
    snapshot = pricing_snapshot.selected(
        quotation, list(response.selected_item_ids or [])
    )

    company = settings_service.get_company_settings(session)
    logo_bytes = _logo_bytes(company)

    document = pdf_model.build_accepted(
        quotation, response, snapshot,
        company_settings=company,
        logo_bytes=logo_bytes,
        sales_representative=_sales_rep(session, quotation),
        legal_footer=(company.pdf_confidentiality_text or "") if company else "",
        thank_you_text=(company.pdf_thank_you_text or "") if company else "",
    )
    return pdf_renderer.render(document)


def _logo_bytes(company: CompanySettings | None) -> bytes | None:
    """The logo, or nothing. A missing logo never stops a document."""
    if company is None or not company.logo_key:
        return None
    try:
        return get_storage().get(company.logo_key)
    except Exception:  # noqa: BLE001
        log.info("Logo unavailable for an accepted document; continuing without it")
        return None


def _sales_rep(session: Session, quotation: Quotation) -> str:
    from modules.models import User

    if not quotation.sales_user_id:
        return ""
    rep = session.get(User, quotation.sales_user_id)
    return rep.employee_name if rep else ""


# --------------------------------------------------------------------------- #
# Processing a job
# --------------------------------------------------------------------------- #

def _claim(session: Session, job: QuoteDocumentJob, owner: str) -> bool:
    """Take a short lease on a job, or report that somebody else holds it.

    A compare-and-swap rather than a read-then-write: two workers reading the
    same unclaimed job would both see it free. The UPDATE naming the previous
    lease state can only succeed for one of them.
    """
    now = dt.datetime.now(dt.UTC)
    stale_before = now - dt.timedelta(seconds=LEASE_SECONDS)

    result = session.execute(
        text(
            """
            UPDATE quote_document_jobs
               SET lock_owner = :owner, locked_at = :now
             WHERE id = :job_id
               AND (locked_at IS NULL OR locked_at < :stale)
            """
        ),
        {"owner": owner, "now": now, "job_id": job.id, "stale": stale_before},
    )
    if not result.rowcount:
        return False
    session.expire(job)
    return True


def _release(session: Session, job: QuoteDocumentJob) -> None:
    job.lock_owner = None
    job.locked_at = None


def process_job(session: Session, job: QuoteDocumentJob) -> DocumentJobStatus:
    """Produce the document for one job, or leave it retryable.

    **Owns its transaction.** Failure is handled by rolling back, so a caller
    must not have other uncommitted work in this session — see
    :func:`run_pending_jobs`, which gives every job a session of its own.

    Never raises for an expected failure. A broken renderer or an unreachable
    bucket is recorded on the job and reported as a status; the usual caller is
    a background task with nobody to tell.
    """
    if job.status is DocumentJobStatus.READY:
        return job.status

    owner = secrets.token_hex(8)
    if not _claim(session, job, owner):
        log.info("Document job is already claimed by another worker; skipping")
        return job.status

    job_id = job.id
    try:
        artifact = _produce(session, job)
    except Exception as exc:  # noqa: BLE001 — every failure is a retry, not a crash
        # Captured before the rollback discards it. A mismatch that was
        # observed happened, whatever becomes of this attempt.
        quarantine = (
            (exc.artifact_id, exc.reason)
            if isinstance(exc, ArtifactIntegrityError) and exc.artifact_id
            else None
        )
        session.rollback()

        if quarantine is not None:
            spoiled = session.get(QuoteDocumentArtifact, quarantine[0])
            if spoiled is not None:
                spoiled.status = ArtifactStatus.QUARANTINED
                spoiled.quarantine_reason = quarantine[1][:2000]

        fresh = session.get(QuoteDocumentJob, job_id)
        if fresh is None:
            return DocumentJobStatus.FAILED
        fresh.attempts += 1
        fresh.last_attempt_at = dt.datetime.now(dt.UTC)
        fresh.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        fresh.status = (
            DocumentJobStatus.FAILED
            if fresh.attempts >= MAX_ATTEMPTS
            else DocumentJobStatus.PENDING
        )
        _release(session, fresh)
        # Committed, not merely flushed: an attempt count that evaporates when
        # the caller's transaction ends is not a bounded retry, it is an
        # unbounded one that looks bounded.
        session.commit()
        log.warning(
            "Accepted-document job %s attempt %s did not complete",
            fresh.id, fresh.attempts,
        )
        return fresh.status

    # Bookkeeping after the work, not before: adopting another worker's
    # artifact rolls back inside _produce, and an attempt counter incremented
    # beforehand would vanish with it.
    fresh = session.get(QuoteDocumentJob, job_id)
    fresh.attempts += 1
    fresh.last_attempt_at = dt.datetime.now(dt.UTC)
    fresh.status = DocumentJobStatus.READY
    fresh.completed_at = dt.datetime.now(dt.UTC)
    fresh.last_error = None
    _release(session, fresh)
    session.commit()
    log.info(
        "Accepted document ready for response %s (%s bytes)",
        fresh.portal_response_id, artifact.byte_size,
    )
    return fresh.status


def _produce(session: Session, job: QuoteDocumentJob) -> QuoteDocumentArtifact:
    """Get to a verified artifact for this job, however many attempts it takes.

    Four paths, in order of preference, all converging on one row:

    1. an artifact already exists and its object verifies — done, nothing to do;
    2. the object exists but no row does — a previous attempt died between the
       put and the commit, so adopt those bytes rather than write new ones;
    3. neither exists — render, store, record;
    4. another worker won the race to record — adopt its row.
    """
    response = session.get(PortalResponse, job.portal_response_id)
    if response is None:
        raise DocumentJobError("The acceptance for this job no longer exists.")

    existing = artifact_for_response(session, response.id)
    if existing is not None:
        verify(session, existing)      # raises, and quarantines, on mismatch
        return existing

    key = artifact_key(response)
    if not is_within_artifact_namespace(key):
        raise DocumentJobError("Refusing to write outside the artifact namespace.")

    storage = get_storage()

    data: bytes | None = None
    if storage.exists(key):
        # Crash recovery. The bytes are ours — the key is derived from our own
        # identifiers — so adopting them keeps one document per acceptance
        # instead of producing a second one that differs only by timestamp.
        try:
            data = storage.get(key)
        except StorageError:
            data = None
        if data is not None:
            log.info("Adopting an accepted document left by an earlier attempt")

    wrote_object = False
    if data is None:
        data = build_accepted_pdf(session, response)
        storage.put(key, data, "application/pdf")
        wrote_object = True

    digest = hashlib.sha256(data).hexdigest()
    artifact = QuoteDocumentArtifact(
        portal_response_id=response.id,
        quotation_id=response.quotation_id,
        revision_no=response.revision_no,
        storage_key=key,
        sha256=digest,
        byte_size=len(data),
        generator_version=_generator_version(),
        status=ArtifactStatus.READY,
    )
    session.add(artifact)
    try:
        session.flush()
    except IntegrityError:
        # Another worker recorded one first. Its row is the published one; ours
        # never existed. The object at the key is one of the two renders — both
        # are this acceptance — and the winner's recorded hash is authoritative.
        session.rollback()
        winner = artifact_for_response(session, response.id)
        if winner is None:
            raise
        log.info("Another worker published this accepted document first; adopting it")
        verify(session, winner)
        return winner
    except Exception:
        # Deliberately **not** queued for cleanup. The key is deterministic, so
        # an object with no row is not litter — it is where the next attempt
        # resumes from, and deleting it would throw away the bytes that make a
        # retry cheap and identical. If the job is eventually abandoned the
        # object remains: a few kilobytes for a document nobody received is the
        # cheaper of the two failures.
        if wrote_object:
            log.info("Accepted document was stored but not recorded; a retry "
                     "will adopt it")
        raise

    return artifact


def _generator_version() -> str:
    from portal.pdf_model import GENERATOR_VERSION

    return GENERATOR_VERSION


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #

def artifact_for_response(
    session: Session, response_id: int
) -> QuoteDocumentArtifact | None:
    return session.execute(
        select(QuoteDocumentArtifact).where(
            QuoteDocumentArtifact.portal_response_id == response_id
        )
    ).scalar_one_or_none()


def job_for_response(session: Session, response_id: int) -> QuoteDocumentJob | None:
    return session.execute(
        select(QuoteDocumentJob).where(
            QuoteDocumentJob.portal_response_id == response_id
        )
    ).scalar_one_or_none()


def verify(session: Session, artifact: QuoteDocumentArtifact) -> bytes:
    """Return the stored bytes, having proved they are the recorded ones.

    Hash *and* size, not either alone: a size check is cheap and catches
    truncation immediately, and the hash catches everything else. A mismatch
    quarantines the row and refuses — it never falls back to regenerating,
    because bytes that changed under an immutable record are a fact to
    investigate, not a glitch to paper over.
    """
    if artifact.status is ArtifactStatus.QUARANTINED:
        raise ArtifactIntegrityError(
            "This document is quarantined.", artifact_id=artifact.id,
            reason=artifact.quarantine_reason or "already quarantined",
        )

    def refuse(reason: str, message: str) -> ArtifactIntegrityError:
        _quarantine(session, artifact, reason)
        return ArtifactIntegrityError(
            message, artifact_id=artifact.id, reason=reason
        )

    try:
        data = get_storage().get(artifact.storage_key)
    except StorageError as exc:
        raise refuse(
            "stored object is missing", "The stored document is missing."
        ) from exc

    if len(data) != artifact.byte_size:
        raise refuse(
            f"size mismatch: stored {len(data)}, recorded {artifact.byte_size}",
            "The stored document has the wrong size.",
        )

    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise refuse("sha256 mismatch", "The stored document failed its checksum.")

    return data


def _quarantine(
    session: Session, artifact: QuoteDocumentArtifact, reason: str
) -> None:
    """Mark an artifact unservable. Status and reason are the only writable fields."""
    artifact.status = ArtifactStatus.QUARANTINED
    artifact.quarantine_reason = reason[:2000]
    session.flush()
    log.error(
        "Accepted document %s quarantined for response %s",
        artifact.id, artifact.portal_response_id,
    )


def state_for_response(session: Session, response_id: int) -> DocumentState:
    """Pending, Ready or Failed — what an employee may be shown.

    A quarantined artifact reads as FAILED: from the outside it is a document
    that needs somebody, which is the same action either way.
    """
    artifact = artifact_for_response(session, response_id)
    job = job_for_response(session, response_id)

    if artifact is not None and artifact.status is ArtifactStatus.READY:
        return DocumentState(
            status=DocumentJobStatus.READY,
            attempts=job.attempts if job else 0,
            generated_at=artifact.generated_at,
            byte_size=artifact.byte_size,
        )
    if artifact is not None:      # quarantined
        return DocumentState(
            status=DocumentJobStatus.FAILED,
            attempts=job.attempts if job else 0,
            generated_at=artifact.generated_at,
        )
    if job is None:
        # No job at all: an acceptance that predates the feature and has not
        # been reconciled yet. Preparing is the honest answer — reconciliation
        # will pick it up — and it is not an error the employee caused.
        return DocumentState(status=DocumentJobStatus.PENDING)
    return DocumentState(status=job.status, attempts=job.attempts)


# --------------------------------------------------------------------------- #
# Running the queue
# --------------------------------------------------------------------------- #

def run_pending_jobs(limit: int = 10, response_id: int | None = None) -> int:
    """Work the queue in its own transaction. Returns how many became ready.

    Opens its own sessions on purpose: the usual caller is a background task
    running after an HTTP response has been sent, whose request session is
    gone. Safe to run concurrently — the lease and the unique index between
    them mean overlapping workers converge rather than conflict.

    **One transaction per job.** A failing job rolls back, and sharing a
    transaction would take the jobs that already succeeded down with it.
    """
    from modules.database import session_scope

    try:
        with session_scope() as session:
            statement = (
                select(QuoteDocumentJob.id)
                .where(QuoteDocumentJob.status == DocumentJobStatus.PENDING)
                .order_by(QuoteDocumentJob.id)
                .limit(limit)
            )
            if response_id is not None:
                statement = statement.where(
                    QuoteDocumentJob.portal_response_id == response_id
                )
            job_ids = list(session.execute(statement).scalars().all())
    except Exception:  # noqa: BLE001
        log.exception("The accepted-document queue could not be read")
        return 0

    ready = 0
    for job_id in job_ids:
        try:
            with session_scope() as session:
                job = session.get(QuoteDocumentJob, job_id)
                if job is not None and process_job(session, job) is (
                    DocumentJobStatus.READY
                ):
                    ready += 1
        except Exception:  # noqa: BLE001
            # Nobody to tell: this runs after the customer already has their
            # confirmation. The job stays queued for the next sweep.
            log.exception("An accepted-document job could not be worked")
    return ready


def retry(session: Session, response_id: int) -> DocumentState:
    """Put a failed job back in the queue. For an employee-triggered retry.

    Resets the attempt count, because a person deciding to try again is new
    information — the backend was probably fixed in between.
    """
    job = job_for_response(session, response_id)
    if job is None:
        reconcile(session)
        job = job_for_response(session, response_id)
    if job is None:
        return DocumentState(status=DocumentJobStatus.PENDING)

    if job.status is not DocumentJobStatus.READY:
        job.status = DocumentJobStatus.PENDING
        job.attempts = 0
        job.last_error = None
        job.lock_owner = None
        job.locked_at = None
        session.flush()
    return state_for_response(session, response_id)
