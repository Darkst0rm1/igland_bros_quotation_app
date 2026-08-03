"""Authorization: the identity carried through a session, and the checks on it.

The rule this module exists to enforce: **permission checks belong in the
service layer, not the page.** Hiding a button is a UX courtesy. Streamlit
re-runs the whole script on every interaction and its session state is driven
by the client, so a check that only decides whether a widget renders is not a
security boundary. ``require(user, Perm.X)`` called inside the service that
performs the action is.

The second rule: **scope narrows permission.** ``quote.view_own`` is expressed
as a SQL predicate applied to the query (:func:`quotation_scope_filter`), not as
a filter applied to results after loading them — a post-filter can be bypassed
by a stale identifier in session state, a WHERE clause cannot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import false, select, true
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from modules.constants import Perm, QuotationStatus, RoleCode
from modules.models import Quotation, User

log = logging.getLogger(__name__)


class PermissionDenied(PermissionError):
    """Raised when an authenticated user attempts something they may not do."""

    def __init__(self, permission: str, detail: str | None = None) -> None:
        self.permission = permission
        message = f"You do not have permission to do this ({permission})."
        if detail:
            message = f"{message} {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class ApprovalLimits:
    """Effective limits for a user.

    Where a user holds several roles the *most permissive* value applies, and
    ``None`` means "no limit". Combining limits any other way would make adding
    a role able to reduce someone's authority, which is not how anyone expects
    roles to behave.
    """

    max_discount_pct: Decimal | None = None
    max_quote_value: Decimal | None = None
    min_margin_pct: Decimal | None = None
    can_override_warnings: bool = False

    def discount_exceeds(self, pct: Decimal) -> bool:
        return self.max_discount_pct is not None and pct > self.max_discount_pct

    def value_exceeds(self, total: Decimal) -> bool:
        return self.max_quote_value is not None and total > self.max_quote_value

    def margin_below(self, margin_pct: Decimal | None) -> bool:
        if margin_pct is None or self.min_margin_pct is None:
            return False
        return margin_pct < self.min_margin_pct


@dataclass(frozen=True)
class AuthUser:
    """The authenticated identity, resolved once at login and held in session.

    Deliberately a plain frozen dataclass rather than a live ORM object:
    Streamlit keeps session state across script runs, and a detached ORM
    instance sitting in session state would either go stale or drag a dead
    session around. Anything that mutates must re-load the ``User`` row.
    """

    id: int
    username: str
    employee_name: str
    email: str
    job_title: str | None = None
    role_codes: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)
    limits: ApprovalLimits = field(default_factory=ApprovalLimits)
    #: The user plus their direct reports, for the ``quote.view_team`` scope.
    team_user_ids: frozenset[int] = field(default_factory=frozenset)
    session_id: str = ""
    must_change_password: bool = False

    def has(self, permission: Perm | str) -> bool:
        return str(permission) in self.permissions

    def has_any(self, *permissions: Perm | str) -> bool:
        return any(self.has(p) for p in permissions)

    def has_all(self, *permissions: Perm | str) -> bool:
        return all(self.has(p) for p in permissions)

    @property
    def is_admin(self) -> bool:
        return RoleCode.SYS_ADMIN in self.role_codes

    @property
    def role_label(self) -> str:
        from modules.constants import ROLE_DISPLAY_NAMES

        names: list[str] = []
        for code in sorted(self.role_codes):
            try:
                names.append(ROLE_DISPLAY_NAMES[RoleCode(code)])
            except (ValueError, KeyError):
                names.append(code)  # a custom role added after this code shipped
        return ", ".join(names) if names else "No role assigned"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_auth_user(session: Session, user: User, session_id: str = "") -> AuthUser:
    """Resolve a ``User`` row into the flat identity used for the session.

    Permissions are the union of role grants and any per-user grants — the
    latter is how a Sales Employee receives ``cost.view`` "when permission is
    granted" without being promoted to Sales Manager.
    """
    permissions: set[str] = set()
    role_codes: set[str] = set()

    max_discount: Decimal | None = None
    max_value: Decimal | None = None
    min_margin: Decimal | None = None
    can_override = False
    unlimited_discount = unlimited_value = no_margin_floor = False

    for role in user.roles:
        role_codes.add(role.code)
        permissions.update(p.code for p in role.permissions)
        can_override = can_override or role.can_override_warnings

        # A NULL limit on any held role means unlimited, and unlimited wins.
        if role.max_discount_pct is None:
            unlimited_discount = True
        elif max_discount is None or role.max_discount_pct > max_discount:
            max_discount = role.max_discount_pct

        if role.max_quote_value is None:
            unlimited_value = True
        elif max_value is None or role.max_quote_value > max_value:
            max_value = role.max_quote_value

        # For a *minimum* margin, more permissive means lower.
        if role.min_margin_pct is None:
            no_margin_floor = True
        elif min_margin is None or role.min_margin_pct < min_margin:
            min_margin = role.min_margin_pct

    permissions.update(p.code for p in user.extra_permissions)

    team_ids = {user.id}
    if Perm.QUOTE_VIEW_TEAM in permissions:
        reports = session.execute(
            select(User.id).where(User.manager_id == user.id)
        ).scalars()
        team_ids.update(reports)

    return AuthUser(
        id=user.id,
        username=user.username,
        employee_name=user.employee_name,
        email=user.email,
        job_title=user.job_title,
        role_codes=frozenset(role_codes),
        permissions=frozenset(permissions),
        limits=ApprovalLimits(
            max_discount_pct=None if unlimited_discount else max_discount,
            max_quote_value=None if unlimited_value else max_value,
            min_margin_pct=None if no_margin_floor else min_margin,
            can_override_warnings=can_override,
        ),
        team_user_ids=frozenset(team_ids),
        session_id=session_id,
        must_change_password=user.must_change_password,
    )


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def has_permission(user: AuthUser | None, permission: Perm | str) -> bool:
    return bool(user) and user.has(permission)


def require(user: AuthUser | None, permission: Perm | str, detail: str | None = None) -> None:
    """Raise :class:`PermissionDenied` unless ``user`` holds ``permission``.

    Every service entry point that mutates or discloses data calls this.
    """
    if user is None:
        raise PermissionDenied(str(permission), "You are not signed in.")
    if not user.has(permission):
        log.warning(
            "Permission denied: user=%s permission=%s", user.username, permission
        )
        raise PermissionDenied(str(permission), detail)


def require_any(user: AuthUser | None, *permissions: Perm | str) -> None:
    if user is None or not user.has_any(*permissions):
        raise PermissionDenied(" or ".join(str(p) for p in permissions))


# --------------------------------------------------------------------------- #
# Scoping
# --------------------------------------------------------------------------- #

def quotation_scope_filter(user: AuthUser) -> ColumnElement[bool]:
    """A WHERE clause restricting quotations to what ``user`` may see.

    Applied to every quotation query. Returning ``false()`` for a user with no
    view permission means such a user sees an empty list rather than an error,
    which is the correct behaviour for a list page.
    """
    if user.has(Perm.QUOTE_VIEW_ALL):
        return true()
    if user.has(Perm.QUOTE_VIEW_TEAM):
        return Quotation.sales_user_id.in_(user.team_user_ids)
    if user.has(Perm.QUOTE_VIEW_OWN):
        return Quotation.sales_user_id == user.id
    return false()


def can_view_quotation(user: AuthUser, quotation: Quotation) -> bool:
    if user.has(Perm.QUOTE_VIEW_ALL):
        return True
    if user.has(Perm.QUOTE_VIEW_TEAM) and quotation.sales_user_id in user.team_user_ids:
        return True
    return user.has(Perm.QUOTE_VIEW_OWN) and quotation.sales_user_id == user.id


def can_edit_quotation(user: AuthUser, quotation: Quotation) -> bool:
    """Editable only while it is an unissued draft the user is entitled to touch.

    Three conditions, all required: the quotation is not locked, it is in a
    status that permits editing, and the user either owns it or may edit any
    draft. An issued quotation is never editable by anyone — it is revised.
    """
    if quotation.is_locked:
        return False
    if quotation.status not in {QuotationStatus.DRAFT, QuotationStatus.REVISION_REQUIRED}:
        return False
    if user.has(Perm.QUOTE_EDIT_ANY_DRAFT):
        return True
    return user.has(Perm.QUOTE_EDIT_OWN_DRAFT) and quotation.sales_user_id == user.id


def require_edit_quotation(user: AuthUser, quotation: Quotation) -> None:
    if not can_edit_quotation(user, quotation):
        if quotation.is_locked:
            raise PermissionDenied(
                str(Perm.QUOTE_EDIT_OWN_DRAFT),
                f"{quotation.display_number} has been issued. Create a revision instead.",
            )
        raise PermissionDenied(
            str(Perm.QUOTE_EDIT_OWN_DRAFT),
            f"{quotation.display_number} is not an editable draft belonging to you.",
        )


def can_approve_quotation(user: AuthUser, quotation: Quotation, requested_by_id: int) -> bool:
    """Self-approval is impossible, for everyone, including System Administrators.

    The identity check comes first and is not reachable by any permission grant.
    """
    if user.id == requested_by_id or user.id == quotation.sales_user_id:
        return False
    return user.has(Perm.QUOTE_APPROVE)


def can_view_costs(user: AuthUser) -> bool:
    """Gate for every internal cost and margin figure in the UI.

    Nothing behind this gate is ever written to a customer PDF, regardless of
    the answer — the PDF generator does not receive cost data at all.
    """
    return user.has(Perm.COST_VIEW) or user.has(Perm.MARGIN_VIEW)
