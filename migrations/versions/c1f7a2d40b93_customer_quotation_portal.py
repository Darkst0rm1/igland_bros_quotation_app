"""Customer quotation portal: optional items, secure links, events, responses

Additive only. Every existing row keeps its meaning:

* ``quotation_items.inclusion`` defaults to INCLUDED, so every line raised
  before the portal existed still counts toward the total exactly as it did.
* ``quotations.deposit_pct`` defaults to 0, which is "no deposit requested".

Nothing is dropped, renamed or back-filled with a guess, so the downgrade is a
clean reversal.

Revision ID: c1f7a2d40b93
Revises: 8fa770f85658
Create Date: 2026-08-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "c1f7a2d40b93"
down_revision = "8fa770f85658"
branch_labels = None
depends_on = None


# Enums are stored as VARCHAR + CHECK (native_enum=False) to match the rest of
# the schema, so PostgreSQL gains no new types and SQLite behaves identically.
INCLUSION = sa.Enum(
    "INCLUDED", "OPTIONAL", "RECOMMENDED",
    name="iteminclusion", native_enum=False, length=40,
)
EVENT_TYPE = sa.Enum(
    "LINK_ISSUED", "LINK_REVOKED", "VIEWED", "PDF_DOWNLOADED",
    "APPROVED", "CHANGES_REQUESTED", "ACCESS_DENIED",
    name="quoteeventtype", native_enum=False, length=40,
)
RESPONSE_TYPE = sa.Enum(
    "APPROVED", "CHANGES_REQUESTED",
    name="portalresponsetype", native_enum=False, length=40,
)

MONEY = sa.Numeric(18, 2)
PERCENTAGE = sa.Numeric(9, 4)
JSON_TYPE = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    # --- existing tables: additive columns only ---------------------------- #
    with op.batch_alter_table("quotation_items") as batch:
        batch.add_column(
            sa.Column(
                "inclusion", INCLUSION,
                nullable=False, server_default="INCLUDED",
            )
        )

    with op.batch_alter_table("quotations") as batch:
        batch.add_column(
            sa.Column(
                "deposit_pct", PERCENTAGE,
                nullable=False, server_default="0",
            )
        )

    # --- secure public access ---------------------------------------------- #
    op.create_table(
        "quote_access_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "quotation_id", sa.Integer(),
            sa.ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False,
        ),
        # SHA-256 hex of the plaintext token. The plaintext is never stored.
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("first_viewed_at", sa.DateTime(timezone=True)),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True)),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index(
        "ix_quote_access_tokens_token_hash", "quote_access_tokens",
        ["token_hash"], unique=True,
    )
    op.create_index(
        "ix_quote_access_tokens_quotation_id", "quote_access_tokens", ["quotation_id"]
    )

    # --- portal events ------------------------------------------------------ #
    op.create_table(
        "quote_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "quotation_id", sa.Integer(),
            sa.ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "access_token_id", sa.Integer(),
            sa.ForeignKey("quote_access_tokens.id", ondelete="SET NULL"),
        ),
        sa.Column("event_type", EVENT_TYPE, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("detail_json", JSON_TYPE),
    )
    op.create_index("ix_quote_events_quotation_id", "quote_events", ["quotation_id"])
    op.create_index("ix_quote_events_access_token_id", "quote_events", ["access_token_id"])
    op.create_index("ix_quote_events_event_type", "quote_events", ["event_type"])
    op.create_index(
        "ix_quote_events_quotation_type", "quote_events", ["quotation_id", "event_type"]
    )

    # --- customer responses -------------------------------------------------- #
    op.create_table(
        "portal_responses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "quotation_id", sa.Integer(),
            sa.ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "access_token_id", sa.Integer(),
            sa.ForeignKey("quote_access_tokens.id", ondelete="SET NULL"),
        ),
        sa.Column("revision_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response_type", RESPONSE_TYPE, nullable=False),
        sa.Column("customer_name", sa.String(160), nullable=False),
        sa.Column("job_title", sa.String(120)),
        sa.Column("customer_email", sa.String(255)),
        sa.Column("comment", sa.Text()),
        sa.Column("signature_name", sa.String(160)),
        sa.Column("accepted_terms", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("selected_item_ids", JSON_TYPE),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        # Server-computed snapshots. Never values posted by the browser.
        sa.Column("subtotal", MONEY, nullable=False, server_default="0"),
        sa.Column("tax_amount", MONEY, nullable=False, server_default="0"),
        sa.Column("grand_total", MONEY, nullable=False, server_default="0"),
        sa.Column("attachment_key", sa.String(500)),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_portal_responses_quotation_id", "portal_responses", ["quotation_id"])
    op.create_index("ix_portal_responses_response_type", "portal_responses", ["response_type"])


def downgrade() -> None:
    op.drop_index("ix_portal_responses_response_type", table_name="portal_responses")
    op.drop_index("ix_portal_responses_quotation_id", table_name="portal_responses")
    op.drop_table("portal_responses")

    op.drop_index("ix_quote_events_quotation_type", table_name="quote_events")
    op.drop_index("ix_quote_events_event_type", table_name="quote_events")
    op.drop_index("ix_quote_events_access_token_id", table_name="quote_events")
    op.drop_index("ix_quote_events_quotation_id", table_name="quote_events")
    op.drop_table("quote_events")

    op.drop_index("ix_quote_access_tokens_quotation_id", table_name="quote_access_tokens")
    op.drop_index("ix_quote_access_tokens_token_hash", table_name="quote_access_tokens")
    op.drop_table("quote_access_tokens")

    with op.batch_alter_table("quotations") as batch:
        batch.drop_column("deposit_pct")
    with op.batch_alter_table("quotation_items") as batch:
        batch.drop_column("inclusion")
