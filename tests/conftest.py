"""Shared test fixtures.

The environment is configured **before** any project module is imported, so the
settings singleton picks up the temporary database rather than the developer's
``igland.db``. bcrypt rounds are lowered to keep the suite fast; nothing else
about the configuration differs from development.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="igland_tests_"))

os.environ["APP_ENV"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["LOCAL_STORAGE_ROOT"] = str(_TMP / "uploads")
os.environ["GENERATED_QUOTES_DIR"] = str(_TMP / "generated")
os.environ["STORAGE_BACKEND"] = "local"
os.environ["BCRYPT_ROUNDS"] = "10"
os.environ["MAX_FAILED_LOGINS"] = "3"
os.environ["LOCKOUT_MINUTES"] = "15"
os.environ["SESSION_TIMEOUT_MINUTES"] = "60"

from modules import models  # noqa: E402,F401  (registers every table on Base)
from modules.authorization import AuthUser, load_auth_user  # noqa: E402
from modules.database import Base, get_engine, get_session  # noqa: E402
from modules.models import Role, User  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    return get_engine()


def _stamp_alembic_head(engine) -> None:
    """Record the current migration revision on the test database.

    ``create_all`` builds the schema without going through Alembic, so nothing
    writes ``alembic_version``. ``app.py`` refuses to start against a database
    whose revision does not match the code — correctly — so the test database
    has to look like a migrated one.
    """
    from sqlalchemy import text

    from modules.database import schema_revisions

    _, head = schema_revisions()
    if head is None:  # no migrations generated yet
        return
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version "
                 "(version_num VARCHAR(32) NOT NULL)")
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head}
        )


@pytest.fixture(autouse=True)
def clean_database(engine):
    """A fresh, empty schema for every test.

    Dropping and recreating rather than rolling back, because several code paths
    under test commit internally (authentication, sequence allocation) and a
    rollback-based fixture would silently not isolate them.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _stamp_alembic_head(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session():
    db = get_session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def seeded(session):
    """Permissions, roles, price tiers, settings and term templates."""
    from seeds import seed_reference_data, seed_roles_permissions, seed_term_templates

    permissions = seed_roles_permissions.seed_permissions(session)
    roles = seed_roles_permissions.seed_roles(session, permissions)
    seed_reference_data.run(session)
    seed_term_templates.run(session)
    session.commit()
    return roles


@pytest.fixture
def make_user(session, seeded):
    """Create a user with the given role codes and return the ORM row."""
    from modules.authentication import hash_password

    counter = {"n": 0}

    def _make(
        *role_codes: str,
        username: str | None = None,
        password: str = "CorrectHorse9",
        manager_id: int | None = None,
        is_active: bool = True,
    ) -> User:
        counter["n"] += 1
        name = username or f"user{counter['n']}"
        user = User(
            username=name,
            email=f"{name}@igland.invalid",
            employee_name=name.title(),
            password_hash=hash_password(password),
            is_active=is_active,
            manager_id=manager_id,
        )
        user.roles = [seeded[code] for code in role_codes]
        session.add(user)
        session.commit()
        return user

    return _make


@pytest.fixture
def make_auth_user(session, make_user):
    """Create a user and return the resolved :class:`AuthUser` identity."""

    def _make(*role_codes: str, **kwargs) -> AuthUser:
        user = make_user(*role_codes, **kwargs)
        return load_auth_user(session, user, session_id="test-session")

    return _make
