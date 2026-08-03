"""Engine, session management and the exact-decimal column type.

Every database access in the application goes through :func:`session_scope` or
:func:`get_session`. No module outside ``repositories.py`` should be issuing
queries directly.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from functools import lru_cache
from typing import Any

from sqlalchemy import Integer, MetaData, Numeric, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from modules.config import PROJECT_ROOT, get_settings

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exact decimal storage
# --------------------------------------------------------------------------- #

class ExactNumeric(TypeDecorator):
    """A NUMERIC column that stays exact on every backend, including SQLite.

    SQLite has no decimal type. SQLAlchemy's plain ``Numeric`` therefore falls
    back to Python ``float`` there and emits a warning that "rounding errors and
    other issues may occur" — which is exactly what must not happen in the money
    path, and local development and the entire test suite run on SQLite.

    The fix is to store a **scaled integer** on SQLite: ``12.3456`` at scale 4
    becomes ``123456``. That keeps the value exact *and* keeps SQL semantics
    working, so ``SUM``, ``ORDER BY`` and range filters still behave correctly
    in reports — which storing the text representation would have broken.

    On PostgreSQL the native ``NUMERIC(precision, scale)`` is used unchanged.

    Capacity check: the widest column in the schema is ``NUMERIC(18, 8)`` (FX
    rates), whose maximum scaled value is 10^18 — inside SQLite's signed 64-bit
    integer range of ~9.22 x 10^18.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int = 18, scale: int = 2) -> None:
        self.precision = precision
        self.scale = scale
        self._factor = Decimal(10) ** scale
        self._exponent = Decimal(1).scaleb(-scale)
        super().__init__(precision=precision, scale=scale, asdecimal=True)

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Integer())
        return dialect.type_descriptor(
            Numeric(precision=self.precision, scale=self.scale, asdecimal=True)
        )

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
        if dialect.name == "sqlite":
            # quantize() raises on overflow rather than silently truncating,
            # which is the behaviour we want for money.
            return int(dec.quantize(self._exponent) * self._factor)
        return dec

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        if dialect.name == "sqlite":
            return (Decimal(int(value)) / self._factor).quantize(self._exponent)
        return value if isinstance(value, Decimal) else Decimal(str(value))


#: Column-type shorthands, matching the precision bands in
#: docs/PHASE1_ARCHITECTURE.md §4.
def money() -> ExactNumeric:
    """Line and quotation money. 2 dp — every stored total is already rounded."""
    return ExactNumeric(18, 2)


def unit_price() -> ExactNumeric:
    """Unit prices and unit costs. 6 dp, so imported 4 dp values survive intact."""
    return ExactNumeric(18, 6)


def quantity() -> ExactNumeric:
    """Packs, pieces, containers. 3 dp."""
    return ExactNumeric(18, 3)


def percentage() -> ExactNumeric:
    """Discount and margin percentages. 4 dp."""
    return ExactNumeric(9, 4)


def tax_rate() -> ExactNumeric:
    """Tax rates. 6 dp."""
    return ExactNumeric(9, 6)


def fx_rate() -> ExactNumeric:
    """Exchange rates. 8 dp."""
    return ExactNumeric(18, 8)


# --------------------------------------------------------------------------- #
# Declarative base
# --------------------------------------------------------------------------- #

#: Deterministic constraint names. Without these SQLite produces unnamed
#: constraints, and Alembic's batch mode (which rebuilds a table to alter it)
#: cannot then drop or recreate them — migrations start failing on the dev
#: database while working fine on PostgreSQL.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


# --------------------------------------------------------------------------- #
# Engine & sessions
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide engine.

    ``pool_pre_ping`` matters on Community Cloud: the container sleeps when idle
    and a managed PostgreSQL instance will have dropped the pooled connections
    by the time it wakes.
    """
    settings = get_settings()
    kwargs: dict[str, Any] = {
        "echo": False,
        "future": True,
        "pool_pre_ping": True,
    }

    if settings.is_sqlite:
        # check_same_thread=False is required because Streamlit serves each
        # session from a worker thread.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 5
        kwargs["pool_recycle"] = 900

    engine = create_engine(settings.database_url, **kwargs)

    if settings.is_sqlite:
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            # Foreign keys are OFF by default in SQLite, which would let the
            # test suite pass while production PostgreSQL rejected the same write.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    log.info(
        "Database engine created (%s)",
        "sqlite" if settings.is_sqlite else engine.dialect.name,
    )
    return engine


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,  # objects stay readable after commit, which
                                 # Streamlit's render-after-write flow needs
        autoflush=False,
        future=True,
    )


def get_session() -> Session:
    """Return a new session. The caller is responsible for closing it."""
    return get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception.

    ::

        with session_scope() as session:
            session.add(thing)
    """
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Schema version check
# --------------------------------------------------------------------------- #

def schema_revisions() -> tuple[str | None, str | None]:
    """Return ``(applied_revision, expected_revision)``.

    Community Cloud gives no shell, so migrations are applied from a developer
    machine as a deploy step. That makes it entirely possible to push code whose
    models are ahead of the deployed database. Comparing these two at startup
    turns that into one clear message instead of a scattering of "no such column"
    errors hours later.

    Auto-migrating on startup was deliberately not chosen: several containers
    can start concurrently, and a schema change is not something that should
    happen as a side effect of a page load.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, text

    expected: str | None = None
    try:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        expected = ScriptDirectory.from_config(config).get_current_head()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not determine the expected schema revision: %s", exc)

    applied: str | None = None
    engine = get_engine()
    try:
        if inspect(engine).has_table("alembic_version"):
            with engine.connect() as conn:
                applied = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read the applied schema revision: %s", exc)

    return applied, expected


def reset_engine_cache() -> None:
    """Drop the cached engine and session factory.

    Used by the test suite, which points DATABASE_URL at a fresh database per
    module, and by any tooling that changes configuration after import.
    """
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_settings.cache_clear()
