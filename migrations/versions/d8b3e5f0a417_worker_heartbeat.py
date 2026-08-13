"""Database-backed worker heartbeat

The original signal was a file the worker touched after each sweep, which works
when one machine runs everything. In production nothing shares a filesystem:
the employee application is on Streamlit Community Cloud, the customer portal
and the background worker are separate Render services. A file the employee app
cannot read is not a signal — the page would report "Not configured" forever
while delivery ran perfectly, or look healthy while the worker was dead.

The database is the one thing all three already share, so the heartbeat lives
here. One row per logical worker, updated in place: "is the worker running" is
a question about the role, not about which container is serving it today.

Operational facts only — status, timestamps, counts, environment. No host name,
no filesystem path, no process identity, no exception text. This is read by a
page a salesperson looks at.

Revision ID: d8b3e5f0a417
Revises: c7e4f1a92b68
Create Date: 2026-08-13

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8b3e5f0a417"
down_revision = "c7e4f1a92b68"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service_name", sa.String(60), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="HEALTHY"),
        # Staleness is measured against the last *successful* sweep, not the
        # last attempt: a worker looping on errors is not healthy merely
        # because it is still running.
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("summary", sa.String(200)),
        sa.Column("environment", sa.String(40)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    # One row per logical worker. Two instances racing to create the first row
    # collide here; the loser re-reads and updates, so the outcome is one row
    # either way.
    op.create_index(
        "uq_worker_heartbeats_service",
        "worker_heartbeats", ["service_name"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_worker_heartbeats_service", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
