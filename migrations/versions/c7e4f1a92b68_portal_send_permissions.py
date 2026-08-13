"""Send, retry and delivery-visibility permissions

Emailing a quotation is a different act from issuing a link. A link can be
generated, previewed and discarded; a sent message has left the building. So it
gets its own permission rather than riding on ``quote.portal_link_issue``, and
retrying a stuck delivery gets a third — narrower still, because a retry cannot
change who receives what.

Granted here in SQL as well as in the seeder. The seeder reconciles grants to
the matrix, but it is run by hand, and a deployment that upgrades without
running it would leave every salesperson unable to send. Doing it in the
migration means the permission arrives with the code that needs it.

Idempotent throughout: every insert is guarded by NOT EXISTS, so re-running
adds nothing and the seeder afterwards agrees with what is already here.

Revision ID: c7e4f1a92b68
Revises: a3d5b81c47f9
Create Date: 2026-08-12

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7e4f1a92b68"
down_revision = "a3d5b81c47f9"
branch_labels = None
depends_on = None

#: code -> (category, description)
PERMISSIONS = {
    "quote.portal_send": ("Quotations", "Quote Portal Send"),
    "quote.portal_retry": ("Quotations", "Quote Portal Retry"),
    "quote.portal_view_delivery": ("Quotations", "Quote Portal View Delivery"),
}

#: Which roles get what. Finance sees delivery status and nothing more: they
#: need to know a quotation reached the customer without being able to send one.
#: Pricing Administrator appears nowhere — setting a price is not publishing.
GRANTS = {
    "SALES": ("quote.portal_send", "quote.portal_view_delivery"),
    "SALES_MANAGER": (
        "quote.portal_send", "quote.portal_retry", "quote.portal_view_delivery",
    ),
    "FINANCE": ("quote.portal_view_delivery",),
    "SYS_ADMIN": (
        "quote.portal_send", "quote.portal_retry", "quote.portal_view_delivery",
    ),
}


def upgrade() -> None:
    for code, (category, description) in PERMISSIONS.items():
        op.execute(
            sa.text(
                """
                INSERT INTO permissions (code, category, description)
                SELECT :code, :category, :description
                WHERE NOT EXISTS (
                    SELECT 1 FROM permissions WHERE code = :code
                )
                """
            ).bindparams(code=code, category=category, description=description)
        )

    for role_code, codes in GRANTS.items():
        for code in codes:
            op.execute(
                sa.text(
                    """
                    INSERT INTO role_permissions (role_id, permission_id)
                    SELECT r.id, p.id
                    FROM roles AS r, permissions AS p
                    WHERE r.code = :role_code
                      AND p.code = :code
                      AND NOT EXISTS (
                          SELECT 1 FROM role_permissions AS rp
                          WHERE rp.role_id = r.id AND rp.permission_id = p.id
                      )
                    """
                ).bindparams(role_code=role_code, code=code)
            )


def downgrade() -> None:
    # Grants first: the foreign key would refuse the permission rows otherwise.
    op.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id IN (
                SELECT id FROM permissions WHERE code IN (
                    'quote.portal_send', 'quote.portal_retry',
                    'quote.portal_view_delivery'
                )
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM permissions WHERE code IN (
                'quote.portal_send', 'quote.portal_retry',
                'quote.portal_view_delivery'
            )
            """
        )
    )
