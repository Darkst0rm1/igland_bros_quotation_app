"""bundle composition per product

Moves ``units_per_bundle`` from ``product_container_capacity`` to ``products``.

A bundle holds the same number of boxes whatever container it travels in, so
keyed on (product, container size, container type) it was one value stored
several times over, free to disagree with itself. It is a property of the box.

Both databases have no capacity rows at the time of writing, so this moves
nothing in practice — but the migration copies any value that does exist
rather than dropping the column blind, because a migration that silently
discards data is only safe on the databases its author happened to look at.
Where several capacity rows for one product disagree, the largest wins and the
others are lost; that is recorded here rather than being papered over, and it
cannot arise from data this application has written.

Revision ID: 8fa770f85658
Revises: baeb1e834b6e
Create Date: 2026-08-05 23:07:30.298868

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from modules.database import ExactNumeric

# revision identifiers, used by Alembic.
revision: str = '8fa770f85658'
down_revision: Union[str, Sequence[str], None] = 'baeb1e834b6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'units_per_bundle',
                ExactNumeric(precision=18, scale=3),
                nullable=True,
            )
        )

    op.execute(
        sa.text(
            """
            UPDATE products
               SET units_per_bundle = (
                   SELECT MAX(c.units_per_bundle)
                     FROM product_container_capacity AS c
                    WHERE c.product_id = products.id
                      AND c.units_per_bundle IS NOT NULL
               )
             WHERE EXISTS (
                   SELECT 1
                     FROM product_container_capacity AS c
                    WHERE c.product_id = products.id
                      AND c.units_per_bundle IS NOT NULL
               )
            """
        )
    )

    with op.batch_alter_table('product_container_capacity', schema=None) as batch_op:
        batch_op.drop_column('units_per_bundle')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('product_container_capacity', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'units_per_bundle',
                ExactNumeric(precision=18, scale=3),
                nullable=True,
            )
        )

    # Push the value back onto every capacity row of the product, which is
    # where it would have been had it been entered under the old shape.
    op.execute(
        sa.text(
            """
            UPDATE product_container_capacity
               SET units_per_bundle = (
                   SELECT p.units_per_bundle
                     FROM products AS p
                    WHERE p.id = product_container_capacity.product_id
               )
            """
        )
    )

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('units_per_bundle')
