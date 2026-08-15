"""Container capacity belongs to the variant, not the size

Capacity was keyed on the product, on the reasoning that it is a function of
the box's geometry and the board qualities of a given size are dimensionally
identical. The supplier's own sheet disproves that: the KIPAS workbook of
7 August 2026 gives WTL125 FL135 IK135 different container quantities from
WTL125 FL120 IK90 and IK120 at five of six sizes — 2,160 bundles against 2,304
on the 8 inch — and prices from them, so the freight share per bundle differs
by board. Keying on the product forced one quality's capacity onto all three.

``product_id`` is kept alongside. Not every capacity figure is stated per
variant: the older bundles workbook was per size, and a row that predates the
distinction is still true of every variant of that product. So the lookup
prefers a variant row and falls back to the product row, and the uniqueness
constraint moves to allow both to coexist.

Revision ID: b7c1e4f8a903
Revises: d8b3e5f0a417
Create Date: 2026-08-14

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c1e4f8a903"
down_revision = "d8b3e5f0a417"
branch_labels = None
depends_on = None

#: Short by necessity: the convention-generated name for these three columns is
#: 68 characters and PostgreSQL truncates identifiers at 63, so spelling it out
#: raises IdentifierError before it reaches the database.
_OLD_UNIQUE = "uq_capacity_product_size_type"
_NEW_UNIQUE = "uq_capacity_variant_size_type"


def upgrade() -> None:
    with op.batch_alter_table("product_container_capacity") as batch:
        batch.add_column(
            sa.Column("product_variant_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_capacity_variant", "product_variants",
            ["product_variant_id"], ["id"], ondelete="CASCADE",
        )

    op.create_index(
        "ix_product_container_capacity_product_variant_id",
        "product_container_capacity", ["product_variant_id"],
    )

    # The old constraint allowed one row per (product, size, type), which now
    # has to hold one row per variant as well as the product-wide fallback.
    #
    # Found by its columns rather than its name. SQLAlchemy's naming convention
    # truncates and hashes a long constraint name -- this one materialised as
    # ...container_size_26e5 -- so a hardcoded name silently matches nothing and
    # leaves the constraint in place to reject every variant row later.
    inspector = sa.inspect(op.get_bind())
    target = {"product_id", "container_size", "container_type"}
    for constraint in inspector.get_unique_constraints("product_container_capacity"):
        if set(constraint["column_names"]) == target and constraint["name"]:
            op.drop_constraint(
                constraint["name"], "product_container_capacity", type_="unique"
            )

    # Partial, so the product-wide row stays unique while variant rows are
    # unique per variant. Two separate indexes rather than one over a nullable
    # column, because NULLs do not collide in a unique index and the fallback
    # row would not be constrained at all.
    op.create_index(
        "uq_capacity_product_fallback", "product_container_capacity",
        ["product_id", "container_size", "container_type"], unique=True,
        sqlite_where=sa.text("product_variant_id IS NULL"),
        postgresql_where=sa.text("product_variant_id IS NULL"),
    )
    op.create_index(
        _NEW_UNIQUE, "product_container_capacity",
        ["product_variant_id", "container_size", "container_type"], unique=True,
        sqlite_where=sa.text("product_variant_id IS NOT NULL"),
        postgresql_where=sa.text("product_variant_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_NEW_UNIQUE, table_name="product_container_capacity")
    op.drop_index(
        "uq_capacity_product_fallback", table_name="product_container_capacity"
    )
    op.drop_index(
        "ix_product_container_capacity_product_variant_id",
        table_name="product_container_capacity",
    )

    # Variant-specific rows cannot survive a return to per-product capacity;
    # keeping them would violate the restored constraint the moment two
    # variants of one product disagree.
    op.execute(
        "DELETE FROM product_container_capacity WHERE product_variant_id IS NOT NULL"
    )

    with op.batch_alter_table("product_container_capacity") as batch:
        batch.drop_constraint("fk_capacity_variant", type_="foreignkey")
        batch.drop_column("product_variant_id")

    op.create_unique_constraint(
        _OLD_UNIQUE, "product_container_capacity",
        ["product_id", "container_size", "container_type"],
    )
