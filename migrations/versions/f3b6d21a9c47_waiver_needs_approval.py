"""Waiving a charge needs a manager

``is_waived`` shipped this morning as a single boolean, which made waiving
available to anyone who could edit the quotation. Giving money away needs a
second person: an employee asks, a manager decides, and the charge is billed in
full until it is.

So the boolean becomes a status. ``APPROVED`` is the only value that takes
money off — a ``PENDING`` waiver is still charged, shown to employees and never
to the customer, because a concession that has been asked for is not one that
has been given.

The decision is recorded beside it rather than only in the audit log: who
asked, who decided, when, why, and what they said. "Why was this customer not
billed for the dies" is asked long after the person who decided has moved on,
and it should be answerable from the quotation.

Backfilled truthfully: an existing waived charge becomes ``APPROVED``, because
it *was* in force. There were none in production when this ran, but a
development database may differ and silently un-waiving a charge would change
a total.

Revision ID: f3b6d21a9c47
Revises: e5a2c8b71d64
Create Date: 2026-08-17

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3b6d21a9c47"
down_revision = "e5a2c8b71d64"
branch_labels = None
depends_on = None

# native_enum=False, matching models._enum and every other enum column in this
# schema. Without it this created a real PostgreSQL enum type while the ORM
# treated the column as a string — a disagreement PostgreSQL tolerates for
# single-row inserts and rejects for multi-row ones. a7c41e2f9b83 converts the
# databases that already ran this; the change here is so that a database built
# from scratch never grows the type in the first place.
#
# .create() and .drop() below become no-ops for a non-native enum, which is
# what should always have happened here.
_STATUS = sa.Enum(
    "NONE", "PENDING", "APPROVED", "REJECTED", name="waiverstatus",
    native_enum=False, length=40,
)


def upgrade() -> None:
    bind = op.get_bind()
    _STATUS.create(bind, checkfirst=True)

    op.add_column(
        "quotation_charges",
        sa.Column(
            "waiver_status", _STATUS, nullable=False, server_default="NONE"
        ),
    )
    op.add_column("quotation_charges", sa.Column("waiver_reason", sa.Text()))
    op.add_column(
        "quotation_charges",
        sa.Column("waiver_requested_by_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "quotation_charges",
        sa.Column("waiver_requested_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "quotation_charges",
        sa.Column("waiver_decided_by_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "quotation_charges",
        sa.Column("waiver_decided_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "quotation_charges", sa.Column("waiver_decision_note", sa.Text())
    )
    op.create_foreign_key(
        "fk_quotation_charges_waiver_requested_by_id_users",
        "quotation_charges", "users", ["waiver_requested_by_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_quotation_charges_waiver_decided_by_id_users",
        "quotation_charges", "users", ["waiver_decided_by_id"], ["id"],
    )

    # A charge that was waived stays waived. Un-waiving it here would move a
    # quotation's total without anybody deciding to.
    op.execute(
        sa.text(
            "UPDATE quotation_charges SET waiver_status = 'APPROVED' "
            "WHERE is_waived"
        )
    )
    op.drop_column("quotation_charges", "is_waived")


def downgrade() -> None:
    op.add_column(
        "quotation_charges",
        sa.Column(
            "is_waived", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.execute(
        sa.text(
            "UPDATE quotation_charges SET is_waived = true "
            "WHERE waiver_status = 'APPROVED'"
        )
    )
    op.drop_constraint(
        "fk_quotation_charges_waiver_decided_by_id_users",
        "quotation_charges", type_="foreignkey",
    )
    op.drop_constraint(
        "fk_quotation_charges_waiver_requested_by_id_users",
        "quotation_charges", type_="foreignkey",
    )
    for column in (
        "waiver_decision_note", "waiver_decided_at", "waiver_decided_by_id",
        "waiver_requested_at", "waiver_requested_by_id", "waiver_reason",
        "waiver_status",
    ):
        op.drop_column("quotation_charges", column)
    _STATUS.drop(op.get_bind(), checkfirst=True)
