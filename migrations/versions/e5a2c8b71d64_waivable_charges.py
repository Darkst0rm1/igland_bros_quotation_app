"""Any charge can be waived

A waiver is not a discount and not a deletion, and the difference matters to
whoever reads the quotation afterwards. A discount changes what something
costs. Deleting the row loses the fact that the charge ever applied. A waiver
says the charge applied, at this amount, and is not being collected — the
customer sees the concession and the company can still report on what it gave
away.

One boolean on the charge, so it works for every charge type there is and
every one added later. ``amount`` is never rewritten: the original stays where
it is and each surface asks the flag whether it counts, which is what makes
un-waiving return the exact figure rather than a remembered one.

Existing charges are not waived. ``server_default`` false rather than a
backfill, so the column is correct for rows written by code that predates it.

Revision ID: e5a2c8b71d64
Revises: c3d9a1b7e502
Create Date: 2026-08-16

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5a2c8b71d64"
down_revision = "c3d9a1b7e502"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quotation_charges",
        sa.Column(
            "is_waived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("quotation_charges", "is_waived")
