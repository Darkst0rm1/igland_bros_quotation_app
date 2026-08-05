"""Login, lockout, session expiry, password change and admin reset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from modules.authentication import (
    GENERIC_FAILURE,
    AuthenticationError,
    admin_reset_password,
    authenticate,
    change_password,
    hash_password,
    is_session_expired,
    revalidate,
    validate_password_strength,
    verify_password,
)
from modules.authorization import PermissionDenied, load_auth_user
from modules.constants import AuditAction, RoleCode
from modules.models import AuditLog, User

PASSWORD = "CorrectHorse9"


class TestPasswordHashing:
    def test_hash_is_not_the_password(self):
        digest = hash_password(PASSWORD)
        assert PASSWORD not in digest
        assert digest.startswith("$2")

    def test_verification(self):
        digest = hash_password(PASSWORD)
        assert verify_password(PASSWORD, digest)
        assert not verify_password("wrong", digest)

    def test_salting_makes_identical_passwords_differ(self):
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_a_corrupt_hash_reads_as_no_rather_than_crashing(self):
        assert not verify_password(PASSWORD, "not-a-bcrypt-hash")
        assert not verify_password(PASSWORD, "")


class TestPasswordStrength:
    def test_a_good_password_has_no_problems(self):
        assert validate_password_strength("CorrectHorse9") == []

    @pytest.mark.parametrize(
        ("password", "fragment"),
        [
            ("Short1A", "10 characters"),
            ("alllowercase9", "uppercase"),
            ("ALLUPPERCASE9", "lowercase"),
            ("NoDigitsAtAll", "digit"),
        ],
    )
    def test_weak_passwords_are_described(self, password, fragment):
        problems = validate_password_strength(password)
        assert any(fragment in p for p in problems)

    def test_a_password_containing_the_username_is_rejected(self):
        problems = validate_password_strength("Jsmith12345", username="jsmith")
        assert any("username" in p for p in problems)


class TestLogin:
    def test_successful_login(self, session, make_user):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        result = authenticate(session, "alice", PASSWORD)
        assert result.user.id == user.id
        assert result.user.has("quote.create")
        assert result.session_id

    def test_login_by_email(self, session, make_user):
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        result = authenticate(session, "alice@soneet.invalid", PASSWORD)
        assert result.user.username == "alice"

    def test_username_is_case_insensitive(self, session, make_user):
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        assert authenticate(session, "ALICE", PASSWORD).user.username == "alice"

    def test_last_login_is_recorded(self, session, make_user):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        assert user.last_login_at is None
        authenticate(session, "alice", PASSWORD)
        assert session.get(User, user.id).last_login_at is not None

    @pytest.mark.parametrize(
        "identifier,password",
        [("nobody", PASSWORD), ("alice", "WrongPassword1")],
    )
    def test_every_failure_gives_the_same_message(
        self, session, make_user, identifier, password
    ):
        """Distinguishing 'no such user' from 'wrong password' would turn the
        login form into an account oracle."""
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        with pytest.raises(AuthenticationError) as exc:
            authenticate(session, identifier, password)
        assert str(exc.value) == GENERIC_FAILURE

    def test_a_disabled_account_cannot_sign_in(self, session, make_user):
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD,
                  is_active=False)
        with pytest.raises(AuthenticationError) as exc:
            authenticate(session, "alice", PASSWORD)
        assert str(exc.value) == GENERIC_FAILURE

    def test_empty_credentials_are_refused(self, session, make_user):
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        with pytest.raises(AuthenticationError):
            authenticate(session, "", "")


class TestLockout:
    def test_the_account_locks_after_the_configured_failures(self, session, make_user):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        for _ in range(3):  # MAX_FAILED_LOGINS is 3 in the test environment
            with pytest.raises(AuthenticationError):
                authenticate(session, "alice", "WrongPassword1")

        session.refresh(user)
        assert user.locked_until is not None

        # The correct password is now refused too, and refused identically.
        with pytest.raises(AuthenticationError) as exc:
            authenticate(session, "alice", PASSWORD)
        assert str(exc.value) == GENERIC_FAILURE

    def test_a_successful_login_clears_the_failure_count(self, session, make_user):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        with pytest.raises(AuthenticationError):
            authenticate(session, "alice", "WrongPassword1")
        session.refresh(user)
        assert user.failed_login_count == 1

        authenticate(session, "alice", PASSWORD)
        session.refresh(user)
        assert user.failed_login_count == 0
        assert user.locked_until is None

    def test_login_works_again_once_the_lock_expires(self, session, make_user):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        user.locked_until = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
        assert authenticate(session, "alice", PASSWORD).user.username == "alice"


class TestAuditTrail:
    def test_a_successful_login_is_audited(self, session, make_user):
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        authenticate(session, "alice", PASSWORD)
        actions = session.execute(select(AuditLog.action)).scalars().all()
        assert AuditAction.LOGIN.value in actions

    def test_a_failed_login_is_audited_with_its_real_reason(self, session, make_user):
        """The user is told nothing; the audit log records which failure it was."""
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        with pytest.raises(AuthenticationError):
            authenticate(session, "alice", "WrongPassword1")

        entry = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.LOGIN_FAILED.value)
        ).scalars().one()
        assert "wrong password" in entry.reason

    def test_an_unknown_username_is_audited_without_creating_a_user(
        self, session, make_user
    ):
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        with pytest.raises(AuthenticationError):
            authenticate(session, "intruder", "whatever")
        entry = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.LOGIN_FAILED.value)
        ).scalars().one()
        assert entry.reason == "unknown username"
        assert entry.user_id is None

    @pytest.mark.parametrize(
        ("setup", "expected_reason"),
        [
            ("disabled", "account disabled"),
            ("locked", "account locked"),
            ("unknown", "unknown username"),
            ("wrong_password", "wrong password"),
        ],
    )
    def test_failed_logins_survive_the_callers_rollback(
        self, make_user, setup, expected_reason
    ):
        """app.py calls authenticate() inside session_scope(), which rolls back
        on exception. Every rejection must commit its own audit row first, or
        the attempt vanishes — which is precisely the attempt worth keeping."""
        from modules.database import session_scope

        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        identifier, password = "alice", PASSWORD
        if setup == "disabled":
            user.is_active = False
        elif setup == "locked":
            user.locked_until = datetime.now(UTC) + timedelta(hours=1)
        elif setup == "unknown":
            identifier = "intruder"
        elif setup == "wrong_password":
            password = "WrongPassword1"

        with session_scope() as db:
            db.merge(user)

        with pytest.raises(AuthenticationError):
            with session_scope() as db:
                authenticate(db, identifier, password)

        with session_scope() as db:
            reasons = db.execute(
                select(AuditLog.reason).where(
                    AuditLog.action == AuditAction.LOGIN_FAILED.value
                )
            ).scalars().all()
        assert any(expected_reason in (r or "") for r in reasons), reasons

    def test_passwords_never_appear_in_the_audit_log(self, session, make_user):
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        with pytest.raises(AuthenticationError):
            authenticate(session, "alice", "SuperSecret123")
        authenticate(session, "alice", PASSWORD)

        blob = " ".join(
            str(row) for row in session.execute(
                select(
                    AuditLog.reason, AuditLog.old_value_json, AuditLog.new_value_json
                )
            ).all()
        )
        assert "SuperSecret123" not in blob
        assert PASSWORD not in blob


class TestSessionLifecycle:
    def test_a_fresh_session_is_not_expired(self):
        assert not is_session_expired(datetime.now(UTC))

    def test_an_old_session_is_expired(self):
        assert is_session_expired(datetime.now(UTC) - timedelta(minutes=61))

    def test_a_missing_timestamp_counts_as_expired(self):
        assert is_session_expired(None)

    def test_revalidate_returns_none_once_the_account_is_disabled(
        self, session, make_user
    ):
        """Session state is a cache, not the authority — disabling a user takes
        effect on their next interaction, not their next login."""
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        auth_user = load_auth_user(session, user)
        assert revalidate(session, auth_user) is not None

        user.is_active = False
        session.commit()
        assert revalidate(session, auth_user) is None

    def test_revalidate_picks_up_a_role_change(self, session, make_user, seeded):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        auth_user = load_auth_user(session, user)
        assert not auth_user.has("quote.approve")

        user.roles = [seeded[RoleCode.SALES_MANAGER.value]]
        session.commit()
        assert revalidate(session, auth_user).has("quote.approve")


class TestPasswordChange:
    def test_changing_a_password(self, session, make_user):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        auth_user = load_auth_user(session, user)
        change_password(session, auth_user, PASSWORD, "BrandNewPass7")
        assert authenticate(session, "alice", "BrandNewPass7").user.username == "alice"

    def test_the_current_password_is_required(self, session, make_user):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        auth_user = load_auth_user(session, user)
        with pytest.raises(AuthenticationError, match="current password"):
            change_password(session, auth_user, "WrongPassword1", "BrandNewPass7")

    def test_a_weak_new_password_is_refused(self, session, make_user):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        auth_user = load_auth_user(session, user)
        with pytest.raises(AuthenticationError, match="10 characters"):
            change_password(session, auth_user, PASSWORD, "Short1A")

    def test_reusing_the_current_password_is_refused(self, session, make_user):
        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        auth_user = load_auth_user(session, user)
        with pytest.raises(AuthenticationError, match="must differ"):
            change_password(session, auth_user, PASSWORD, PASSWORD)


class TestAdminReset:
    def test_an_administrator_can_reset_and_forces_a_change(self, session, make_user):
        admin = load_auth_user(session, make_user(RoleCode.SYS_ADMIN.value,
                                                  username="root"))
        target = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)

        temporary = admin_reset_password(session, admin, target.id, reason="lost phone")
        assert temporary

        result = authenticate(session, "alice", temporary)
        assert result.user.must_change_password

    def test_a_reset_clears_an_existing_lockout(self, session, make_user):
        admin = load_auth_user(session, make_user(RoleCode.SYS_ADMIN.value,
                                                  username="root"))
        target = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        target.locked_until = datetime.now(UTC) + timedelta(hours=1)
        target.failed_login_count = 3
        session.commit()

        temporary = admin_reset_password(session, admin, target.id)
        assert authenticate(session, "alice", temporary).user.username == "alice"

    def test_a_non_administrator_cannot_reset_anyone(self, session, make_user):
        manager = load_auth_user(session, make_user(RoleCode.SALES_MANAGER.value))
        target = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        with pytest.raises(PermissionDenied):
            admin_reset_password(session, manager, target.id)

    def test_the_temporary_password_is_not_written_to_the_audit_log(
        self, session, make_user
    ):
        admin = load_auth_user(session, make_user(RoleCode.SYS_ADMIN.value,
                                                  username="root"))
        target = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        temporary = admin_reset_password(session, admin, target.id)

        blob = " ".join(
            str(row) for row in session.execute(
                select(AuditLog.reason, AuditLog.new_value_json, AuditLog.old_value_json)
            ).all()
        )
        assert temporary not in blob
