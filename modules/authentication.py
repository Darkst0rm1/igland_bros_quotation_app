"""Employee authentication.

Passwords are hashed with bcrypt and never stored, logged, audited or echoed.

Note the deployment context (docs/PHASE1_ARCHITECTURE.md §12.1): on Streamlit
Community Cloud there is no request-level rate limiting available, so the
account lockout implemented here is the only brute-force control the
application has. That is why it is in Phase 1 rather than deferred, and why the
platform's private-app viewer allowlist is treated as the real perimeter.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.audit_service import record_audit
from modules.authorization import AuthUser, load_auth_user
from modules.config import get_settings
from modules.constants import AuditAction, EntityType
from modules.models import User

log = logging.getLogger(__name__)

#: Verified against when the username does not exist, so that a failed lookup
#: costs the same time as a wrong password. Without this, response timing tells
#: an attacker which usernames are real.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equalisation", bcrypt.gensalt(rounds=12))

#: One message for every failure mode. Distinguishing "no such user" from "wrong
#: password" from "locked" turns the login form into an account oracle.
GENERIC_FAILURE = "Incorrect username or password, or the account is unavailable."


class AuthenticationError(Exception):
    """Login refused. The message is always safe to show the user."""


@dataclass(frozen=True)
class LoginResult:
    user: AuthUser
    session_id: str
    login_at: datetime


# --------------------------------------------------------------------------- #
# Password handling
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    rounds = get_settings().bcrypt_rounds
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # A malformed or truncated hash in the database must read as "no", not
        # crash the login page.
        return False


def validate_password_strength(password: str, username: str = "") -> list[str]:
    """Return a list of problems; empty means acceptable."""
    problems: list[str] = []
    if len(password) < 10:
        problems.append("must be at least 10 characters")
    if not any(c.islower() for c in password):
        problems.append("must contain a lowercase letter")
    if not any(c.isupper() for c in password):
        problems.append("must contain an uppercase letter")
    if not any(c.isdigit() for c in password):
        problems.append("must contain a digit")
    if username and username.lower() in password.lower():
        problems.append("must not contain your username")
    return problems


def generate_temporary_password(length: int = 14) -> str:
    """A URL-safe random password for an administrator reset."""
    return secrets.token_urlsafe(length)[:length]


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

def _find_user(session: Session, identifier: str) -> User | None:
    """Look up by username or email, case-insensitively."""
    ident = identifier.strip().lower()
    if not ident:
        return None
    return session.execute(
        select(User).where(
            func.lower(User.username) == ident if "@" not in ident
            else func.lower(User.email) == ident
        )
    ).scalar_one_or_none()


def _is_locked(user: User, now: datetime) -> bool:
    if user.locked_until is None:
        return False
    locked_until = user.locked_until
    if locked_until.tzinfo is None:  # SQLite returns naive datetimes
        locked_until = locked_until.replace(tzinfo=UTC)
    return locked_until > now


def authenticate(session: Session, identifier: str, password: str) -> LoginResult:
    """Verify credentials and return the session identity.

    Raises :class:`AuthenticationError` with :data:`GENERIC_FAILURE` for every
    rejection — unknown user, wrong password, disabled account, locked account.
    The audit log records which it actually was; the user is told nothing.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    user = _find_user(session, identifier)

    # Each rejection commits its audit row before raising. The caller runs
    # inside session_scope(), which rolls back on exception — without an
    # explicit commit here the record of the attempt would be discarded along
    # with it, and failed logins are exactly what an administrator needs to see.
    if user is None:
        bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)  # constant-time path
        record_audit(
            session, None, AuditAction.LOGIN_FAILED, EntityType.SESSION,
            reason="unknown username", new_value={"identifier": identifier[:80]},
        )
        session.commit()
        raise AuthenticationError(GENERIC_FAILURE)

    if user.deleted_at is not None or not user.is_active:
        record_audit(
            session, None, AuditAction.LOGIN_FAILED, EntityType.USER, user.id,
            username=user.username, reason="account disabled",
        )
        session.commit()
        raise AuthenticationError(GENERIC_FAILURE)

    if _is_locked(user, now):
        record_audit(
            session, None, AuditAction.LOGIN_FAILED, EntityType.USER, user.id,
            username=user.username, reason="account locked",
        )
        session.commit()
        raise AuthenticationError(GENERIC_FAILURE)

    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        reason = "wrong password"
        if user.failed_login_count >= settings.max_failed_logins:
            user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            user.failed_login_count = 0
            reason = f"wrong password; locked for {settings.lockout_minutes} minutes"
        record_audit(
            session, None, AuditAction.LOGIN_FAILED, EntityType.USER, user.id,
            username=user.username, reason=reason,
        )
        session.commit()
        raise AuthenticationError(GENERIC_FAILURE)

    # Success.
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    session_id = secrets.token_urlsafe(24)

    auth_user = load_auth_user(session, user, session_id=session_id)
    record_audit(
        session, auth_user, AuditAction.LOGIN, EntityType.SESSION,
        session_id=session_id,
    )
    session.commit()
    log.info("Login: %s", user.username)
    return LoginResult(user=auth_user, session_id=session_id, login_at=now)


def logout(session: Session, user: AuthUser | None, expired: bool = False) -> None:
    if user is None:
        return
    record_audit(
        session,
        user,
        AuditAction.SESSION_EXPIRED if expired else AuditAction.LOGOUT,
        EntityType.SESSION,
        session_id=user.session_id,
    )
    session.commit()


# --------------------------------------------------------------------------- #
# Session freshness
# --------------------------------------------------------------------------- #

def is_session_expired(last_seen_at: datetime | None) -> bool:
    if last_seen_at is None:
        return True
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=UTC)
    timeout = timedelta(minutes=get_settings().session_timeout_minutes)
    return datetime.now(UTC) - last_seen_at > timeout


def revalidate(session: Session, user: AuthUser) -> AuthUser | None:
    """Re-read the user from the database on every script run.

    Session state is not trusted as the source of truth for whether an account
    is still active or what it may do. Disabling a user, or changing their
    roles, takes effect on their next interaction rather than their next login.
    Returns ``None`` when the account is no longer usable.
    """
    db_user = session.get(User, user.id)
    if db_user is None or not db_user.is_active or db_user.deleted_at is not None:
        log.info("Session invalidated for %s (account disabled or removed)", user.username)
        return None
    return load_auth_user(session, db_user, session_id=user.session_id)


# --------------------------------------------------------------------------- #
# Password change / reset
# --------------------------------------------------------------------------- #

def change_password(
    session: Session,
    user: AuthUser,
    current_password: str,
    new_password: str,
) -> None:
    """Change one's own password. Requires the current password."""
    db_user = session.get(User, user.id)
    if db_user is None:
        raise AuthenticationError("Account not found.")

    if not verify_password(current_password, db_user.password_hash):
        record_audit(
            session, user, AuditAction.LOGIN_FAILED, EntityType.USER, db_user.id,
            reason="password change with wrong current password",
        )
        session.commit()
        raise AuthenticationError("Your current password is incorrect.")

    problems = validate_password_strength(new_password, db_user.username)
    if problems:
        raise AuthenticationError("The new password " + "; ".join(problems) + ".")

    if verify_password(new_password, db_user.password_hash):
        raise AuthenticationError("The new password must differ from the current one.")

    db_user.password_hash = hash_password(new_password)
    db_user.must_change_password = False
    db_user.password_changed_at = datetime.now(UTC)
    record_audit(session, user, AuditAction.PASSWORD_CHANGED, EntityType.USER, db_user.id)
    session.commit()


def admin_reset_password(
    session: Session,
    admin: AuthUser,
    target_user_id: int,
    reason: str | None = None,
) -> str:
    """Reset another user's password to a temporary value.

    Returns the temporary password for the administrator to hand over out of
    band. It is not stored anywhere, not emailed by this application, and not
    written to the audit log — only the fact of the reset is recorded.
    """
    from modules.authorization import require
    from modules.constants import Perm

    require(admin, Perm.USER_MANAGE)

    db_user = session.get(User, target_user_id)
    if db_user is None:
        raise AuthenticationError("Account not found.")

    temporary = generate_temporary_password()
    db_user.password_hash = hash_password(temporary)
    db_user.must_change_password = True
    db_user.failed_login_count = 0
    db_user.locked_until = None

    record_audit(
        session, admin, AuditAction.PASSWORD_RESET, EntityType.USER, db_user.id,
        reason=reason, new_value={"must_change_password": True},
    )
    session.commit()
    return temporary
