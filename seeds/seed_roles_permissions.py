"""Seed permissions, roles, the role grants, and a bootstrap administrator.

Idempotent: safe to run repeatedly. Existing role grants are reconciled to the
matrix in :mod:`modules.constants`, so the matrix in code stays the single
source of truth and drift in the database is corrected rather than preserved.
"""

from __future__ import annotations

import logging
import secrets
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.constants import (
    PERMISSION_CATEGORIES,
    ROLE_DISPLAY_NAMES,
    ROLE_PERMISSIONS,
    Perm,
    RoleCode,
)
from modules.models import Permission, Role, User

log = logging.getLogger(__name__)

#: Approval limits per role. NULL means unlimited; the most permissive value
#: across a user's roles applies. These are starting points — Finance edits
#: them in the UI under ``approval_limits.manage``.
ROLE_LIMITS: dict[RoleCode, dict[str, object]] = {
    RoleCode.SALES: {
        "max_discount_pct": Decimal("5"),
        "max_quote_value": Decimal("25000"),
        "min_margin_pct": Decimal("15"),
        "can_override_warnings": False,
    },
    RoleCode.SALES_MANAGER: {
        "max_discount_pct": Decimal("15"),
        "max_quote_value": Decimal("250000"),
        "min_margin_pct": Decimal("10"),
        "can_override_warnings": True,
    },
    RoleCode.FINANCE: {
        "max_discount_pct": Decimal("20"),
        "max_quote_value": None,
        "min_margin_pct": Decimal("8"),
        "can_override_warnings": True,
    },
    RoleCode.PRICING_ADMIN: {
        # Does not raise quotations, so quoting limits do not apply.
        "max_discount_pct": Decimal("0"),
        "max_quote_value": Decimal("0"),
        "min_margin_pct": None,
        "can_override_warnings": False,
    },
    RoleCode.SYS_ADMIN: {
        "max_discount_pct": None,
        "max_quote_value": None,
        "min_margin_pct": None,
        "can_override_warnings": True,
    },
}

ROLE_DESCRIPTIONS: dict[RoleCode, str] = {
    RoleCode.SALES: "Creates quotations, manages own drafts, submits for approval.",
    RoleCode.SALES_MANAGER: "Approves quotations and discounts, sees team margins.",
    RoleCode.FINANCE: "Tax, FX, costs, approval limits and quotation values.",
    RoleCode.PRICING_ADMIN: "Products, price lists, tiers and Excel imports.",
    RoleCode.SYS_ADMIN: "Users, roles, company settings and the full audit log.",
}


def seed_permissions(session: Session) -> dict[str, Permission]:
    existing = {p.code: p for p in session.execute(select(Permission)).scalars()}
    for perm in Perm:
        code = perm.value
        if code in existing:
            existing[code].category = PERMISSION_CATEGORIES.get(perm, "general")
            continue
        row = Permission(
            code=code,
            category=PERMISSION_CATEGORIES.get(perm, "general"),
            description=perm.name.replace("_", " ").title(),
        )
        session.add(row)
        existing[code] = row
    session.flush()
    log.info("Permissions seeded: %d", len(existing))
    return existing


def seed_roles(session: Session, permissions: dict[str, Permission]) -> dict[str, Role]:
    existing = {r.code: r for r in session.execute(select(Role)).scalars()}

    for role_code, granted in ROLE_PERMISSIONS.items():
        role = existing.get(role_code.value)
        if role is None:
            role = Role(code=role_code.value, is_system=True)
            session.add(role)
            existing[role_code.value] = role
            # Limits are starting points, applied once. Finance edits them in
            # the UI under approval_limits.manage, and re-running the seeder
            # must not quietly undo that: an approval ceiling reverting to a
            # default would show up as quotations being waved through, with
            # nothing on screen to say why.
            for field, value in ROLE_LIMITS[role_code].items():
                setattr(role, field, value)

        role.name = ROLE_DISPLAY_NAMES[role_code]
        role.description = ROLE_DESCRIPTIONS[role_code]

        # Reconcile grants to the matrix rather than merely adding to them, so
        # a permission removed from the matrix is actually revoked.
        role.permissions = [permissions[p.value] for p in sorted(granted, key=str)]

    session.flush()
    log.info("Roles seeded: %d", len(existing))
    return existing


def seed_bootstrap_admin(
    session: Session,
    roles: dict[str, Role],
    username: str = "admin",
    email: str = "admin@example.invalid",
    employee_name: str = "System Administrator",
) -> tuple[User, str | None]:
    """Create the first administrator if no users exist at all.

    Returns ``(user, temporary_password)``. The password is random, printed once
    by the caller, never stored in plaintext and never written to the audit log;
    ``must_change_password`` forces it to be replaced at first login.

    Does nothing when any user already exists — this must not be a way to mint
    an administrator on a live system.
    """
    from modules.authentication import hash_password

    if session.execute(select(User.id).limit(1)).first() is not None:
        log.info("Users already exist; skipping bootstrap administrator")
        existing = session.execute(
            select(User).where(User.username == username)
        ).scalar_one_or_none()
        return existing, None  # type: ignore[return-value]

    temporary = secrets.token_urlsafe(12)
    user = User(
        username=username,
        email=email,
        employee_name=employee_name,
        job_title="System Administrator",
        password_hash=hash_password(temporary),
        must_change_password=True,
        is_active=True,
    )
    user.roles = [roles[RoleCode.SYS_ADMIN.value]]
    session.add(user)
    session.flush()
    log.info("Bootstrap administrator created: %s", username)
    return user, temporary


def sync_permissions(session: Session) -> list[str]:
    """Bring the database's permissions and role grants up to the code's.

    A permission added to :class:`~modules.constants.Perm` is reference data,
    not schema, so a migration does not carry it. Until this runs, the feature
    it guards is invisible: every ``user.has(...)`` returns False and the
    controls simply are not drawn. Nothing errors, which is what makes it hard
    to spot — the container-shipping tab shipped in exactly this state, with
    the tables migrated and not one person able to reach them.

    Deliberately narrower than :func:`seed_roles`: it touches permissions and
    grants only, never the approval limits, which are operator-configured.

    Returns a description of what changed, empty when already in step.
    """
    changes: list[str] = []

    existing = {p.code: p for p in session.execute(select(Permission)).scalars()}
    for perm in Perm:
        if perm.value in existing:
            continue
        row = Permission(
            code=perm.value,
            category=PERMISSION_CATEGORIES.get(perm, "general"),
            description=perm.name.replace("_", " ").title(),
        )
        session.add(row)
        existing[perm.value] = row
        changes.append(f"+permission {perm.value}")
    session.flush()

    roles = {r.code: r for r in session.execute(select(Role)).scalars()}
    for role_code, granted in ROLE_PERMISSIONS.items():
        role = roles.get(role_code.value)
        if role is None:
            continue  # seed_roles creates roles; this only reconciles grants.
        wanted = {p.value for p in granted}
        held = {p.code for p in role.permissions}
        if wanted == held:
            continue
        for code in sorted(wanted - held):
            changes.append(f"+{role_code.value}:{code}")
        for code in sorted(held - wanted):
            changes.append(f"-{role_code.value}:{code}")
        role.permissions = [existing[code] for code in sorted(wanted)]
    session.flush()

    if changes:
        log.info("Permissions synced: %s", ", ".join(changes))
    return changes


def run(session: Session) -> tuple[User, str | None]:
    permissions = seed_permissions(session)
    roles = seed_roles(session, permissions)
    return seed_bootstrap_admin(session, roles)
