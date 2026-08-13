"""Immutable accepted PDF artifacts and their durable generation jobs

Two tables, because they answer two different questions. The artifact says
"these exact bytes are what the customer accepted"; the job says "we still owe
somebody a document". Folding them together would mean a row that is sometimes
a promise and sometimes a record, and the immutability rule could not then
apply to it.

The backfill is the important part of this migration. Acceptances recorded
before Phase 6A have no artifact and no job, and the retrieval path must not
invent an unofficial document on demand — so every existing accepted response
gets a PENDING job here and is produced by the ordinary worker afterwards. It
is written as an INSERT ... SELECT with a NOT EXISTS guard, so running it twice
adds nothing, and the same statement is what the application's reconciliation
uses at runtime.

Revision ID: f2a7c9d84e15
Revises: e9c3f5a17b42
Create Date: 2026-08-11

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f2a7c9d84e15"
down_revision = "e9c3f5a17b42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quote_document_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portal_response_id", sa.Integer(), nullable=False),
        sa.Column("quotation_id", sa.Integer(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("generator_version", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="READY"),
        sa.Column("quarantine_reason", sa.Text()),
        sa.ForeignKeyConstraint(
            ["portal_response_id"], ["portal_responses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["quotation_id"], ["quotations.id"], ondelete="CASCADE"
        ),
    )
    # One final document per acceptance. This is the constraint that makes two
    # concurrent workers safe: both may render, only one row can exist, and the
    # loser adopts what the winner published.
    op.create_index(
        "uq_quote_document_artifacts_response",
        "quote_document_artifacts", ["portal_response_id"], unique=True,
    )
    op.create_index(
        "uq_quote_document_artifacts_key",
        "quote_document_artifacts", ["storage_key"], unique=True,
    )
    op.create_index(
        "ix_quote_document_artifacts_quotation",
        "quote_document_artifacts", ["quotation_id"],
    )

    op.create_table(
        "quote_document_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portal_response_id", sa.Integer(), nullable=False),
        sa.Column("quotation_id", sa.Integer(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("lock_owner", sa.String(64)),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["portal_response_id"], ["portal_responses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["quotation_id"], ["quotations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "uq_quote_document_jobs_response",
        "quote_document_jobs", ["portal_response_id"], unique=True,
    )
    op.create_index(
        "ix_quote_document_jobs_status", "quote_document_jobs", ["status"]
    )
    op.create_index(
        "ix_quote_document_jobs_quotation", "quote_document_jobs", ["quotation_id"]
    )

    _backfill_jobs_for_existing_acceptances()


def _backfill_jobs_for_existing_acceptances() -> None:
    """Give every acceptance that predates this migration a pending job.

    Idempotent by the NOT EXISTS guard, so a re-run — or the application's own
    reconciliation sweep, which issues the same statement — adds nothing.
    Nothing is generated here: a migration must not depend on object storage or
    a PDF renderer being reachable, so it only records the intent.
    """
    op.execute(
        sa.text(
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
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_quote_document_jobs_quotation", table_name="quote_document_jobs")
    op.drop_index("ix_quote_document_jobs_status", table_name="quote_document_jobs")
    op.drop_index("uq_quote_document_jobs_response", table_name="quote_document_jobs")
    op.drop_table("quote_document_jobs")

    op.drop_index(
        "ix_quote_document_artifacts_quotation",
        table_name="quote_document_artifacts",
    )
    op.drop_index(
        "uq_quote_document_artifacts_key", table_name="quote_document_artifacts"
    )
    op.drop_index(
        "uq_quote_document_artifacts_response", table_name="quote_document_artifacts"
    )
    op.drop_table("quote_document_artifacts")
