"""Durable email outbox

Sending and the business event it belongs to cannot be made atomic: one is a
network call to somebody else's server, the other is a database transaction.
Putting the send inside the transaction would either hold it open across a round
trip or email a customer about an approval that then rolled back.

So this table holds the *intent*, written in the same transaction as the event.
The worker sends afterwards. If sending fails the quotation is still accepted
and the row is still there to retry.

``secure_payload`` holds the customer's capability URL under AES-256-GCM, keyed
from the environment and bound to this quotation, revision, recipient and
purpose. It is the only place a link exists outside its own hash, it is erased
after a successful send, and a database disclosure on its own still yields
nothing usable.

No provider credentials are stored here or anywhere in the database.

Revision ID: a3d5b81c47f9
Revises: f2a7c9d84e15
Create Date: 2026-08-12

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3d5b81c47f9"
down_revision = "f2a7c9d84e15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_type", sa.String(40), nullable=False),
        sa.Column("quotation_id", sa.Integer(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("portal_response_id", sa.Integer()),
        sa.Column("recipient_email", sa.String(255), nullable=False),
        sa.Column("recipient_name", sa.String(160)),
        sa.Column("subject", sa.String(200), nullable=False, server_default=""),
        sa.Column("brand_snapshot_json", sa.JSON()),
        sa.Column("template_data_json", sa.JSON()),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("provider_message_id", sa.String(255)),
        sa.Column("failure_category", sa.String(20)),
        sa.Column("failure_code", sa.String(60)),
        sa.Column("secure_payload", sa.Text()),
        sa.Column("secure_payload_expires_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["quotation_id"], ["quotations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["portal_response_id"], ["portal_responses.id"], ondelete="SET NULL"
        ),
    )
    # The constraint that makes "at least once" survivable: enqueueing the same
    # notification twice violates this rather than emailing a customer twice.
    op.create_index(
        "uq_email_outbox_idempotency", "email_outbox", ["idempotency_key"], unique=True
    )
    # The worker's claim query: eligible rows by status and due time.
    op.create_index(
        "ix_email_outbox_status_next", "email_outbox", ["status", "next_attempt_at"]
    )
    op.create_index(
        "ix_email_outbox_quotation_type",
        "email_outbox", ["quotation_id", "message_type"],
    )
    op.create_index("ix_email_outbox_status", "email_outbox", ["status"])
    op.create_index("ix_email_outbox_quotation", "email_outbox", ["quotation_id"])
    op.create_index("ix_email_outbox_type", "email_outbox", ["message_type"])
    op.create_index(
        "ix_email_outbox_next_attempt", "email_outbox", ["next_attempt_at"]
    )


def downgrade() -> None:
    for name in (
        "ix_email_outbox_next_attempt",
        "ix_email_outbox_type",
        "ix_email_outbox_quotation",
        "ix_email_outbox_status",
        "ix_email_outbox_quotation_type",
        "ix_email_outbox_status_next",
        "uq_email_outbox_idempotency",
    ):
        op.drop_index(name, table_name="email_outbox")
    op.drop_table("email_outbox")
