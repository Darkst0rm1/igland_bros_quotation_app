"""Deferred storage cleanup queue

Deleting a storage object and committing a database change cannot be made
atomic. The order that survives failure is: write the new reference, commit it,
then retire the old object. This table is what stops that last step being
forgotten when it fails.

Revision ID: e9c3f5a17b42
Revises: d4b8e6c1a072
Create Date: 2026-08-11

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e9c3f5a17b42"
down_revision = "d4b8e6c1a072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_cleanups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("reason", sa.String(120)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
    )
    # Unique so queueing the same key twice cannot create duplicate work.
    op.create_index(
        "uq_storage_cleanups_key", "storage_cleanups", ["storage_key"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_storage_cleanups_key", table_name="storage_cleanups")
    op.drop_table("storage_cleanups")
