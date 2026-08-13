"""Reserved portal system user, plus submission guards against replay and races

Three things, all in service of one problem: a customer has no user row, but
``quotation_service.change_status`` — the sole writer of ``quotations.status``
— sets ``updated_by_id``, a foreign key to ``users``.

1. A reserved, permanently inactive user so that foreign key has a valid,
   clearly-labelled target. It can never authenticate: ``authenticate()``
   rejects inactive accounts, and ``revalidate()`` invalidates any session
   belonging to one, so even if credentials were added later it stays unusable.

2. ``portal_responses.submission_nonce``, unique — a replayed form POST hits a
   uniqueness violation instead of recording a second response.

3. A **partial** unique index allowing only one APPROVED response per
   (quotation, revision). This is the atomicity guarantee: two simultaneous
   approvals race, and exactly one commits. Partial, because a customer may
   legitimately send several change requests for the same revision.

Revision ID: d4b8e6c1a072
Revises: c1f7a2d40b93
Create Date: 2026-08-10

"""
from __future__ import annotations

import secrets

import sqlalchemy as sa
from alembic import op

revision = "d4b8e6c1a072"
down_revision = "c1f7a2d40b93"
branch_labels = None
depends_on = None

#: Reserved identity. Stable, and never to be reused for an employee or for any
#: other automated process — the audit trail's meaning depends on it referring
#: to exactly one thing.
PORTAL_USERNAME = "customer-portal-system"
PORTAL_EMAIL = "customer-portal-system@portal.invalid"  # .invalid is reserved (RFC 2606)
PORTAL_DISPLAY_NAME = "Customer Portal (system)"

#: Tables whose rows would make deleting the system user unsafe on downgrade.
_REFERENCE_CHECKS = (
    ("audit_logs", "user_id"),
    ("quotations", "updated_by_id"),
    ("quotations", "created_by_id"),
    ("quotations", "sales_user_id"),
    ("quote_access_tokens", "created_by_id"),
)


def _unusable_password_hash() -> str:
    """A well-formed bcrypt hash of a secret nobody holds.

    Well-formed on purpose: a junk string would make ``bcrypt.checkpw`` raise
    rather than return False, turning a login attempt into a 500. This hashes
    64 random bytes that are discarded immediately, so verification always
    returns False and never errors.
    """
    import bcrypt

    throwaway = secrets.token_urlsafe(64).encode("utf-8")[:72]  # bcrypt caps at 72
    return bcrypt.hashpw(throwaway, bcrypt.gensalt(rounds=12)).decode("ascii")


def upgrade() -> None:
    conn = op.get_bind()

    # --- 1. reserved system user, idempotently -------------------------------- #
    existing = conn.execute(
        sa.text(
            "SELECT id, is_active, deleted_at FROM users WHERE username = :username"
        ),
        {"username": PORTAL_USERNAME},
    ).mappings().first()

    if existing is None:
        conn.execute(
            sa.text(
                """
                INSERT INTO users (
                    username, email, employee_name, job_title, password_hash,
                    must_change_password, is_active, failed_login_count,
                    created_at, updated_at
                ) VALUES (
                    :username, :email, :name, :title, :pw,
                    0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "username": PORTAL_USERNAME,
                "email": PORTAL_EMAIL,
                "name": PORTAL_DISPLAY_NAME,
                "title": "System account",
                "pw": _unusable_password_hash(),
            },
        )
    elif existing["is_active"]:
        # The reserved name is taken by something that can log in. Refuse rather
        # than deactivate a real person's account or silently adopt it.
        raise RuntimeError(
            f"Cannot provision the portal system user: username "
            f"{PORTAL_USERNAME!r} already exists (id={existing['id']}) and is "
            f"ACTIVE, so it belongs to a real account. Rename that account, or "
            f"deactivate it deliberately, then re-run this migration."
        )
    # else: already provisioned and inactive — nothing to do, re-runs are safe.

    # --- 2. replay guard ------------------------------------------------------ #
    with op.batch_alter_table("portal_responses") as batch:
        batch.add_column(sa.Column("submission_nonce", sa.String(64)))
    op.create_index(
        "ix_portal_responses_submission_nonce", "portal_responses",
        ["submission_nonce"], unique=True,
    )

    # --- 3. one acceptance per revision, enforced by the database ------------- #
    op.create_index(
        "uq_portal_responses_one_approval_per_revision",
        "portal_responses",
        ["quotation_id", "revision_no"],
        unique=True,
        sqlite_where=sa.text("response_type = 'APPROVED'"),
        postgresql_where=sa.text("response_type = 'APPROVED'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_portal_responses_one_approval_per_revision", table_name="portal_responses"
    )
    op.drop_index("ix_portal_responses_submission_nonce", table_name="portal_responses")
    with op.batch_alter_table("portal_responses") as batch:
        batch.drop_column("submission_nonce")

    conn = op.get_bind()
    row = conn.execute(
        sa.text(
            "SELECT id, is_active FROM users WHERE username = :username"
        ),
        {"username": PORTAL_USERNAME},
    ).mappings().first()

    if row is None:
        return
    if row["is_active"]:
        # No longer the reserved inactive account — someone repurposed it.
        # Leaving it alone is the only safe move.
        return

    # Only remove it if nothing points at it; a foreign key from the audit trail
    # means real history would be destroyed.
    for table, column in _REFERENCE_CHECKS:
        try:
            referenced = conn.execute(
                sa.text(f"SELECT 1 FROM {table} WHERE {column} = :uid LIMIT 1"),
                {"uid": row["id"]},
            ).first()
        except Exception:  # noqa: BLE001 — table may not exist at this revision
            continue
        if referenced:
            return  # preserve it: history references this actor

    conn.execute(sa.text("DELETE FROM users WHERE id = :uid"), {"uid": row["id"]})
