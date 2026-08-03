"""Employee accounts, role assignment and approval limits.

Three protections that are enforced here rather than left to the UI:

* an administrator cannot disable or de-admin **their own** account, which is
  how installations end up with nobody able to administer them;
* the last active System Administrator cannot be removed, for the same reason;
* passwords are only ever set through :mod:`modules.authentication`, so nothing
  in this module handles a plaintext password beyond passing it straight on.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from modules.audit_service import record_audit, record_field_changes
from modules.authorization import AuthUser, require
from modules.constants import AuditAction, EntityType, Perm, RoleCode
from modules.models import Permission, Role, User

log = logging.getLogger(__name__)


class UserError(ValueError):
    """A user-management operation that failed a rule. Safe to show the user."""


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def all_users(session: Session, include_inactive: bool = True) -> list[User]:
    stmt = select(User).where(User.deleted_at.is_(None)).order_by(User.employee_name)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    return list(session.execute(stmt).scalars())


def all_roles(session: Session) -> list[Role]:
    return list(session.execute(select(Role).order_by(Role.name)).scalars())


def all_permissions(session: Session) -> list[Permission]:
    return list(
        session.execute(
            select(Permission).order_by(Permission.category, Permission.code)
        ).scalars()
    )


def _active_admin_count(session: Session, excluding: int | None = None) -> int:
    stmt = (
        select(func.count(func.distinct(User.id)))
        .join(User.roles)
        .where(
            Role.code == RoleCode.SYS_ADMIN.value,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    if excluding is not None:
        stmt = stmt.where(User.id != excluding)
    return session.execute(stmt).scalar_one()


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def create_user(
    session: Session,
    admin: AuthUser,
    *,
    username: str,
    email: str,
    employee_name: str,
    role_codes: list[str],
    job_title: str | None = None,
    manager_id: int | None = None,
) -> tuple[User, str]:
    """Create an account and return it with a one-time temporary password.

    The password is generated here, shown once by the caller and stored only as
    a bcrypt hash. It is never written to the audit log.
    """
    from modules.authentication import generate_temporary_password, hash_password

    require(admin, Perm.USER_MANAGE)

    username = (username or "").strip().lower()
    email = (email or "").strip().lower()
    if not username or not email or not employee_name.strip():
        raise UserError("Username, email and employee name are all required.")

    clash = session.execute(
        select(User).where(
            (func.lower(User.username) == username) | (func.lower(User.email) == email)
        )
    ).scalars().first()
    if clash is not None:
        field = "username" if clash.username.lower() == username else "email address"
        raise UserError(f"That {field} is already in use.")

    temporary = generate_temporary_password()
    user = User(
        username=username,
        email=email,
        employee_name=employee_name.strip(),
        job_title=job_title or None,
        manager_id=manager_id,
        password_hash=hash_password(temporary),
        must_change_password=True,
        is_active=True,
        created_by_id=admin.id,
    )
    user.roles = _resolve_roles(session, role_codes)
    session.add(user)
    session.flush()

    record_audit(
        session, admin, AuditAction.USER_CREATED, EntityType.USER, user.id,
        new_value={
            "username": user.username,
            "email": user.email,
            "employee_name": user.employee_name,
            "roles": sorted(role_codes),
        },
    )
    log.info("User created: %s", username)
    return user, temporary


def _resolve_roles(session: Session, role_codes: list[str]) -> list[Role]:
    if not role_codes:
        return []
    roles = list(
        session.execute(select(Role).where(Role.code.in_(role_codes))).scalars()
    )
    missing = set(role_codes) - {r.code for r in roles}
    if missing:
        raise UserError(f"Unknown role(s): {', '.join(sorted(missing))}")
    return roles


def update_user(
    session: Session,
    admin: AuthUser,
    user_id: int,
    *,
    email: str,
    employee_name: str,
    job_title: str | None,
    manager_id: int | None,
    is_active: bool,
) -> User:
    require(admin, Perm.USER_MANAGE)

    user = session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise UserError("That account no longer exists.")

    if not is_active:
        if user.id == admin.id:
            raise UserError(
                "You cannot disable your own account — ask another administrator."
            )
        if _is_admin(user) and _active_admin_count(session, excluding=user.id) == 0:
            raise UserError(
                "This is the last active System Administrator. Give someone else that "
                "role before disabling this account."
            )

    if user_id == manager_id:
        raise UserError("Someone cannot be their own manager.")

    before = {
        "email": user.email, "employee_name": user.employee_name,
        "job_title": user.job_title, "manager_id": user.manager_id,
        "is_active": user.is_active,
    }
    user.email = (email or "").strip().lower()
    user.employee_name = employee_name.strip()
    user.job_title = job_title or None
    user.manager_id = manager_id
    user.is_active = is_active
    session.flush()

    record_field_changes(
        session, admin,
        AuditAction.USER_DISABLED if not is_active else AuditAction.USER_EDITED,
        EntityType.USER, user.id, before,
        {
            "email": user.email, "employee_name": user.employee_name,
            "job_title": user.job_title, "manager_id": user.manager_id,
            "is_active": user.is_active,
        },
    )
    return user


def _is_admin(user: User) -> bool:
    return any(r.code == RoleCode.SYS_ADMIN.value for r in user.roles)


def set_roles(
    session: Session, admin: AuthUser, user_id: int, role_codes: list[str]
) -> User:
    require(admin, Perm.ROLE_MANAGE)

    user = session.get(User, user_id)
    if user is None:
        raise UserError("That account no longer exists.")

    was_admin = _is_admin(user)
    becoming_admin = RoleCode.SYS_ADMIN.value in role_codes

    if was_admin and not becoming_admin:
        if user.id == admin.id:
            raise UserError(
                "You cannot remove your own System Administrator role — ask another "
                "administrator."
            )
        if _active_admin_count(session, excluding=user.id) == 0:
            raise UserError(
                "This is the last active System Administrator. Give someone else that "
                "role first."
            )

    before = sorted(r.code for r in user.roles)
    user.roles = _resolve_roles(session, role_codes)
    session.flush()

    after = sorted(r.code for r in user.roles)
    if before != after:
        record_audit(
            session, admin, AuditAction.ROLE_ASSIGNED, EntityType.USER, user.id,
            old_value={"roles": before}, new_value={"roles": after},
        )
    return user


def set_extra_permissions(
    session: Session,
    admin: AuthUser,
    user_id: int,
    permission_codes: list[str],
    reason: str | None = None,
) -> User:
    """Grant permissions to one person on top of their roles.

    This is how a Sales Employee gets ``cost.view`` "when permission is granted"
    without promoting them or creating a bespoke role for every exception.
    """
    require(admin, Perm.ROLE_MANAGE)

    user = session.get(User, user_id)
    if user is None:
        raise UserError("That account no longer exists.")

    before = sorted(p.code for p in user.extra_permissions)
    permissions = list(
        session.execute(
            select(Permission).where(Permission.code.in_(permission_codes))
        ).scalars()
    )
    missing = set(permission_codes) - {p.code for p in permissions}
    if missing:
        raise UserError(f"Unknown permission(s): {', '.join(sorted(missing))}")

    user.extra_permissions = permissions
    session.flush()

    after = sorted(p.code for p in user.extra_permissions)
    if before != after:
        record_audit(
            session, admin, AuditAction.ROLE_ASSIGNED, EntityType.USER, user.id,
            old_value={"extra_permissions": before},
            new_value={"extra_permissions": after},
            reason=reason,
        )
    return user


def update_role_limits(
    session: Session,
    user: AuthUser,
    role_code: str,
    *,
    max_discount_pct: Decimal | None,
    max_quote_value: Decimal | None,
    min_margin_pct: Decimal | None,
    can_override_warnings: bool,
) -> Role:
    """Set a role's approval thresholds.

    ``None`` means unlimited. Where someone holds several roles the most
    permissive value applies, so raising a limit here can widen more people's
    authority than the role name suggests.
    """
    require(user, Perm.APPROVAL_LIMITS_MANAGE)

    role = session.execute(
        select(Role).where(Role.code == role_code)
    ).scalar_one_or_none()
    if role is None:
        raise UserError(f"Unknown role {role_code!r}.")

    for label, value in (
        ("Maximum discount", max_discount_pct),
        ("Minimum margin", min_margin_pct),
    ):
        if value is not None and not (Decimal(0) <= value <= Decimal(100)):
            raise UserError(f"{label} must be between 0 and 100 percent.")
    if max_quote_value is not None and max_quote_value < 0:
        raise UserError("Maximum quotation value cannot be negative.")

    before = {
        "max_discount_pct": role.max_discount_pct,
        "max_quote_value": role.max_quote_value,
        "min_margin_pct": role.min_margin_pct,
        "can_override_warnings": role.can_override_warnings,
    }
    role.max_discount_pct = max_discount_pct
    role.max_quote_value = max_quote_value
    role.min_margin_pct = min_margin_pct
    role.can_override_warnings = can_override_warnings
    session.flush()

    record_field_changes(
        session, user, AuditAction.SETTINGS_CHANGED, EntityType.ROLE, role.id,
        before,
        {
            "max_discount_pct": role.max_discount_pct,
            "max_quote_value": role.max_quote_value,
            "min_margin_pct": role.min_margin_pct,
            "can_override_warnings": role.can_override_warnings,
        },
        reason=f"approval limits for {role.code}",
    )
    return role
