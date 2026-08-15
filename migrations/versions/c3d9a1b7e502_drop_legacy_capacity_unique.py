"""Drop the per-product capacity constraint left behind by b7c1e4f8a903

That migration meant to drop it and named it literally. SQLAlchemy's naming
convention had truncated and hashed the real name --
``uq_product_container_capacity_product_id_container_size_26e5`` -- so the drop
matched nothing, and a ``try/except`` swallowed the failure. The constraint
survived and rejected the first variant-specific capacity row inserted against
it, which is how it was found.

Separate migration rather than an edit to that one: it is already applied to
production, and a forward-only fix does not depend on a downgrade path working.

Idempotent, and discovers the name from the columns rather than assuming it.

Revision ID: c3d9a1b7e502
Revises: b7c1e4f8a903
Create Date: 2026-08-14

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d9a1b7e502"
down_revision = "b7c1e4f8a903"
branch_labels = None
depends_on = None

_COLUMNS = {"product_id", "container_size", "container_type"}
_TABLE = "product_container_capacity"
_FALLBACK = "uq_capacity_product_fallback"
_VARIANT = "uq_capacity_variant_size_type"

#: Never dropped here. Both were created by b7c1e4f8a903 and the fallback
#: covers exactly the three columns below — matching on columns alone caught it
#: and removed the very index this schema relies on to keep one product-wide
#: row unique.
_OURS = {_FALLBACK, _VARIANT}


def _legacy_names(inspector) -> list[str]:  # noqa: ANN001
    """Unique constraints or indexes over exactly the three columns, minus ours.

    Both kinds are checked: the original may be materialised as either,
    depending on the backend and on whether a batch rewrite has carried it.
    """
    names: list[str] = []
    for constraint in inspector.get_unique_constraints(_TABLE):
        name = constraint["name"]
        if name and name not in _OURS and set(constraint["column_names"]) == _COLUMNS:
            names.append(name)
    for index in inspector.get_indexes(_TABLE):
        name = index["name"]
        if (
            index.get("unique")
            and name
            and name not in _OURS
            and name not in names
            and set(index["column_names"]) == _COLUMNS
        ):
            names.append(name)
    return names


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_unique_constraints(_TABLE)}
    for name in _legacy_names(inspector):
        if name in existing:
            op.drop_constraint(name, _TABLE, type_="unique")
        else:
            op.drop_index(name, table_name=_TABLE)

    # Restore the fallback if an earlier run of this migration removed it.
    inspector = sa.inspect(op.get_bind())
    if _FALLBACK not in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.create_index(
            _FALLBACK, _TABLE,
            ["product_id", "container_size", "container_type"], unique=True,
            sqlite_where=sa.text("product_variant_id IS NULL"),
            postgresql_where=sa.text("product_variant_id IS NULL"),
        )


def downgrade() -> None:
    # Deliberately not recreated. Restoring it would reject the variant-specific
    # rows this schema now exists to hold, so the downgrade would fail on any
    # database carrying real data. b7c1e4f8a903's own downgrade clears those
    # rows first and restores an equivalent constraint under a short name.
    pass
