"""Streamlit session glue.

The only module that touches ``st.session_state``. Everything else receives an
:class:`~modules.authorization.AuthUser` as an argument, which keeps the
services testable without a Streamlit runtime.

The identity in session state is a cache, not an authority. Every script run
re-reads the user from the database (:func:`current_user`), so disabling an
account or changing its roles takes effect on the user's next interaction
rather than their next login.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import streamlit as st

from modules.audit_service import record_permission_denied
from modules.authentication import is_session_expired, revalidate
from modules.authorization import AuthUser, PermissionDenied
from modules.constants import Perm
from modules.database import session_scope

log = logging.getLogger(__name__)

_AUTH_KEY = "auth_user"
_LAST_SEEN_KEY = "auth_last_seen"
_EXPIRED_KEY = "auth_expired_notice"


def set_current_user(user: AuthUser) -> None:
    st.session_state[_AUTH_KEY] = user
    st.session_state[_LAST_SEEN_KEY] = datetime.now(UTC)


def clear_session(expired: bool = False) -> None:
    for key in (_AUTH_KEY, _LAST_SEEN_KEY):
        st.session_state.pop(key, None)
    if expired:
        st.session_state[_EXPIRED_KEY] = True


def pop_expiry_notice() -> bool:
    return bool(st.session_state.pop(_EXPIRED_KEY, False))


def touch() -> None:
    st.session_state[_LAST_SEEN_KEY] = datetime.now(UTC)


def current_user(refresh: bool = True) -> AuthUser | None:
    """Return the signed-in user, or ``None``.

    Applies the session timeout and, unless ``refresh`` is False, re-reads
    roles and active status from the database.
    """
    user: AuthUser | None = st.session_state.get(_AUTH_KEY)
    if user is None:
        return None

    if is_session_expired(st.session_state.get(_LAST_SEEN_KEY)):
        log.info("Session timed out for %s", user.username)
        clear_session(expired=True)
        return None

    if refresh:
        with session_scope() as db:
            revalidated = revalidate(db, user)
        if revalidated is None:
            clear_session()
            return None
        st.session_state[_AUTH_KEY] = revalidated
        user = revalidated

    touch()
    return user


# --------------------------------------------------------------------------- #
# Page guards
# --------------------------------------------------------------------------- #

def require_page(permission: Perm | str | None = None) -> AuthUser:
    """Guard at the top of every page. Returns the signed-in user.

    This is a convenience for the UI, **not** the security boundary — the
    service called by any button on the page performs its own check. It exists
    so that a user who reaches a page they should not see gets a clear message
    instead of a stack trace, and so the attempt is audited.
    """
    user = current_user()
    if user is None:
        st.warning("Your session has ended. Please sign in again.")
        st.stop()

    if permission is not None and not user.has(permission):
        with session_scope() as db:
            record_permission_denied(db, user, str(permission), page=_page_name())
        st.error(
            f"You do not have permission to view this page ({permission}). "
            "Contact your system administrator if you believe this is wrong."
        )
        st.stop()

    return user


def _page_name() -> str:
    try:
        return st.context.url.rsplit("/", 1)[-1] or "app"
    except Exception:  # noqa: BLE001 - st.context is not available in every context
        return "app"


def handle_permission_error(exc: PermissionDenied) -> None:
    """Render a refused action consistently wherever it surfaces."""
    st.error(str(exc))


def page_header(title: str, subtitle: str | None = None, icon: str = "") -> None:
    st.title(f"{icon} {title}".strip())
    if subtitle:
        st.caption(subtitle)
