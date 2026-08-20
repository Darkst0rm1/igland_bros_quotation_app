"""Store waiver_status as VARCHAR, like every other enum in this schema.

``f3b6d21a9c47`` added the column with a bare ``sa.Enum(..., name="waiverstatus")``.
Everywhere else in this application an enum column is built by ``models._enum``,
which passes ``native_enum=False`` and produces VARCHAR plus a CHECK constraint.
The bare call does not, so on PostgreSQL it created a real enum *type* while the
ORM went on believing the column was a string.

Single-row inserts survived that disagreement, because PostgreSQL will coerce a
string literal into an enum. Multi-row inserts did not: SQLAlchemy's
``insertmanyvalues`` path renders explicit ``::VARCHAR`` casts on the bound
parameters, and PostgreSQL refuses a VARCHAR expression for an enum column —

    column "waiver_status" is of type waiverstatus
    but expression is of type character varying

The visible symptom was that creating a revision of a quotation carrying **two
or more charges** failed with a traceback, while one charge worked. Charges are
copied in a single flush, so the row count decided which path SQLAlchemy took.

The test suite could not have caught it. Tests run on SQLite, where sa.Enum is
VARCHAR whether or not native_enum is set, so the two configurations are
indistinguishable there and only PostgreSQL can tell them apart.

Revision ID: a7c41e2f9b83
Revises: f3b6d21a9c47
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7c41e2f9b83"
down_revision = "f3b6d21a9c47"
branch_labels = None
depends_on = None

_LABELS = ("NONE", "PENDING", "APPROVED", "REJECTED")
_CHECK = "ck_quotation_charges_waiver_status"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite already stores this as VARCHAR; there is nothing to convert.
        return

    # The default is dropped first and restored afterwards: it is currently
    # typed 'NONE'::waiverstatus, and a default still referencing the enum
    # would keep the type alive and fail to drop.
    op.execute(sa.text(
        "ALTER TABLE quotation_charges ALTER COLUMN waiver_status DROP DEFAULT"
    ))
    op.execute(sa.text(
        "ALTER TABLE quotation_charges ALTER COLUMN waiver_status "
        "TYPE VARCHAR(40) USING waiver_status::text"
    ))
    op.execute(sa.text(
        "ALTER TABLE quotation_charges ALTER COLUMN waiver_status "
        "SET DEFAULT 'NONE'"
    ))

    # The CHECK constraint is what native_enum=False would have created, and it
    # is the thing that keeps the column honest once the type is gone.
    allowed = ", ".join(f"'{label}'" for label in _LABELS)
    op.execute(sa.text(
        f"ALTER TABLE quotation_charges ADD CONSTRAINT {_CHECK} "
        f"CHECK (waiver_status IN ({allowed}))"
    ))

    op.execute(sa.text("DROP TYPE IF EXISTS waiverstatus"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Raw CREATE TYPE rather than sa.Enum: restoring the native type is the
    # whole point of this downgrade, and sa.Enum here would trip the guard in
    # tests/test_enum_storage.py that exists to stop anyone reintroducing one.
    labels = ", ".join(f"'{label}'" for label in _LABELS)
    op.execute(sa.text(
        "DO $$ BEGIN "
        f"CREATE TYPE waiverstatus AS ENUM ({labels}); "
        "EXCEPTION WHEN duplicate_object THEN null; END $$"
    ))

    op.execute(sa.text(
        f"ALTER TABLE quotation_charges DROP CONSTRAINT IF EXISTS {_CHECK}"
    ))
    op.execute(sa.text(
        "ALTER TABLE quotation_charges ALTER COLUMN waiver_status DROP DEFAULT"
    ))
    op.execute(sa.text(
        "ALTER TABLE quotation_charges ALTER COLUMN waiver_status "
        "TYPE waiverstatus USING waiver_status::waiverstatus"
    ))
    op.execute(sa.text(
        "ALTER TABLE quotation_charges ALTER COLUMN waiver_status "
        "SET DEFAULT 'NONE'::waiverstatus"
    ))
