"""Audit trail.

Every action listed in the brief writes a row here: logins, quotation edits,
price overrides, approvals, PDF generation, status changes, imports, settings
and user administration.

:func:`record_audit` adds to the caller's session but never commits. The audit
row therefore lands in the *same transaction* as the change it describes — if
the business write rolls back, so does its audit entry, and the log can never
claim something happened that did not.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from modules.constants import AuditAction, EntityType, Perm
from modules.models import AuditLog

log = logging.getLogger(__name__)

#: Never written to an audit row, whatever a caller passes.
_REDACTED_KEYS = frozenset({
    "password", "password_hash", "new_password", "current_password",
    "temporary_password", "secret", "secret_key", "token", "api_key",
    "storage_secret_access_key", "storage_access_key_id",
})


def _sanitise(value: Any) -> Any:
    """Make a value JSON-storable and strip anything secret.

    Decimals become strings rather than floats: an audit row recording that a
    price changed to 3.79 must not record 3.7899999999999996.
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return str(Decimal(str(value)))
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            k: ("***" if str(k).lower() in _REDACTED_KEYS else _sanitise(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitise(v) for v in value]
    return str(value)


def record_audit(
    session: Session,
    user: Any | None,
    action: AuditAction | str,
    entity_type: EntityType | str | None = None,
    entity_id: int | None = None,
    *,
    old_value: Any = None,
    new_value: Any = None,
    reason: str | None = None,
    page: str | None = None,
    session_id: str | None = None,
    username: str | None = None,
) -> AuditLog:
    """Append an audit row to the current transaction.

    ``user`` is an :class:`~modules.authorization.AuthUser`, or ``None`` for
    events that happen before an identity exists (a failed login against an
    unknown username). ``username`` lets the caller record who was *attempted*
    in that case.
    """
    entry = AuditLog(
        user_id=getattr(user, "id", None),
        username_snapshot=getattr(user, "username", None) or username,
        action=str(action),
        entity_type=str(entity_type) if entity_type else None,
        entity_id=entity_id,
        old_value_json=_sanitise(old_value),
        new_value_json=_sanitise(new_value),
        reason=reason,
        page=page,
        session_id=session_id or getattr(user, "session_id", None),
    )
    session.add(entry)
    return entry


def record_field_changes(
    session: Session,
    user: Any | None,
    action: AuditAction | str,
    entity_type: EntityType | str,
    entity_id: int,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    reason: str | None = None,
    page: str | None = None,
) -> AuditLog | None:
    """Record only the fields that actually changed.

    Returns ``None`` when nothing changed, so a save that alters nothing does
    not add noise to the trail.
    """
    changed_before: dict[str, Any] = {}
    changed_after: dict[str, Any] = {}
    for key, new in after.items():
        old = before.get(key)
        if old != new:
            changed_before[key] = old
            changed_after[key] = new

    if not changed_after:
        return None

    return record_audit(
        session, user, action, entity_type, entity_id,
        old_value=changed_before, new_value=changed_after,
        reason=reason, page=page,
    )


def record_permission_denied(
    session: Session, user: Any | None, permission: str, page: str | None = None
) -> None:
    """Log a refused action.

    Worth recording separately: a burst of these is either a misconfigured role
    or someone probing, and both are things an administrator should be able to
    see.
    """
    record_audit(
        session, user, AuditAction.PERMISSION_DENIED, EntityType.SESSION,
        new_value={"permission": permission}, page=page,
    )


def audit_query(user: Any) -> Select[tuple[AuditLog]]:
    """Build the audit query a given user is entitled to run.

    ``audit.view_all`` sees everything; ``audit.view_own`` sees only their own
    actions. Scope is applied as a WHERE clause, not by filtering loaded rows.
    """
    stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc())
    if user is not None and user.has(Perm.AUDIT_VIEW_ALL):
        return stmt
    return stmt.where(AuditLog.user_id == getattr(user, "id", None))
