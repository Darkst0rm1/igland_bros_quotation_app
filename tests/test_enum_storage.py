"""Enum columns must be VARCHAR, in the models and in the migrations.

This exists because a bug got all the way to production that the rest of the
suite structurally cannot see. Tests run on SQLite, where ``sa.Enum`` is VARCHAR
whether or not ``native_enum`` is set — so a column declared as a *native*
PostgreSQL enum looks identical here to one declared as a string, and behaves
identically too.

On PostgreSQL it does not. ``f3b6d21a9c47`` added ``quotation_charges.waiver_status``
with a bare ``sa.Enum(...)``, creating a real enum type, while the ORM went on
treating the column as a string. Single-row inserts coerced fine; multi-row
inserts did not, because SQLAlchemy's insertmanyvalues path renders explicit
``::VARCHAR`` casts that PostgreSQL rejects against an enum column. Revising a
quotation with two or more charges failed in production and nowhere else.

No behavioural test on SQLite can catch that. These assert the *declaration*
instead, which is the part that differs.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
import sqlalchemy as sa

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"


class TestModelsDeclareStringEnums:
    def test_every_enum_column_is_non_native(self):
        """One native enum is all it takes; the failure only shows in production."""
        from modules.database import Base
        import modules.models  # noqa: F401  - registers the tables

        offenders = []
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if isinstance(column.type, sa.Enum) and column.type.native_enum:
                    offenders.append(f"{table.name}.{column.name}")
        assert not offenders, (
            "native PostgreSQL enum columns: " + ", ".join(offenders)
            + " — use models._enum(), which sets native_enum=False"
        )


class TestMigrationsDeclareStringEnums:
    """The models can be right while a migration quietly does something else.

    The migration is what actually builds the production column, so it is the
    thing worth checking.
    """

    @staticmethod
    def _enum_calls(path: pathlib.Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = (
                f.attr if isinstance(f, ast.Attribute)
                else f.id if isinstance(f, ast.Name)
                else None
            )
            if name == "Enum":
                yield node

    @pytest.mark.parametrize(
        "path", sorted(MIGRATIONS.glob("*.py")), ids=lambda p: p.name,
    )
    def test_a_migration_never_creates_a_native_enum(self, path):
        for call in self._enum_calls(path):
            kw = {k.arg: k.value for k in call.keywords}
            native = kw.get("native_enum")
            explicit_false = (
                isinstance(native, ast.Constant) and native.value is False
            )
            assert explicit_false, (
                f"{path.name} line {call.lineno}: sa.Enum without "
                "native_enum=False creates a real PostgreSQL enum type. Every "
                "other enum in this schema is VARCHAR, and the mismatch only "
                "surfaces on multi-row inserts, in production."
            )


class TestTheFixIsPresent:
    def test_a_migration_converts_waiver_status_away_from_the_enum(self):
        """Declaring it correctly is not enough — the column already exists in
        production and has to be altered."""
        sql = "\n".join(
            p.read_text(encoding="utf-8") for p in MIGRATIONS.glob("*.py")
        )
        assert "DROP TYPE IF EXISTS waiverstatus" in sql
        assert "waiver_status::text" in sql
