"""Soneet Quotation Application — entry point.

Run with::

    streamlit run app.py

This module is the authentication gate. Pages are registered explicitly through
``st.navigation`` rather than relying on Streamlit's ``pages/`` auto-discovery,
for two reasons:

* auto-discovery renders the sidebar and can execute a page **before** any
  authentication code runs;
* it offers no way to hide a page a user has no permission to open.

The filenames still follow the numbering in the brief; only the registration
mechanism differs. Login is not a registered page — it is this gate — because a
registered login page would appear in the sidebar and be navigable away from.
"""

from __future__ import annotations

import logging

import streamlit as st

from modules.authentication import (
    AuthenticationError,
    authenticate,
    change_password,
    logout,
)
from modules.config import get_settings, secrets_status
from modules.constants import Perm
from modules.database import reset_engine_cache, schema_revisions, session_scope
from modules.session import (
    clear_session,
    current_user,
    pop_expiry_notice,
    set_current_user,
)

log = logging.getLogger(__name__)

st.set_page_config(
    page_title="Soneet Quotations",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --------------------------------------------------------------------------- #
# Startup checks
# --------------------------------------------------------------------------- #

def _database_identity() -> str:
    """Which database this process is actually talking to, without credentials.

    Printed on the failure screen. Without it, "the database has no schema" is
    ambiguous between *the configured database is empty* and *no configuration
    was found, so it fell back to SQLite* — which look identical from the
    outside and have completely different fixes.
    """
    from sqlalchemy.engine import make_url

    try:
        url = make_url(get_settings().database_url)
    except Exception:  # noqa: BLE001
        return "unreadable"
    if url.drivername.startswith("sqlite"):
        return f"sqlite (file: {url.database or ':memory:'})"
    return f"{url.drivername} on {url.host or '?'}/{url.database or '?'}"


def _sync_permissions() -> None:
    """Reconcile the database's permissions with this code's, once per process.

    The schema check above catches a database that is behind on *migrations*.
    It cannot catch one that is behind on *permissions*, because permissions
    are reference data and a migration does not carry them — the revision
    matches, the check passes, and a feature added since the last seed is
    simply not drawn for anybody. That is how the container-shipping tab came
    to render its empty state above an "add a container" form that no user,
    including the administrator, had the permission to see.

    Failures here are logged and swallowed. A permission that could not be
    written is a feature that stays hidden; it is not a reason to refuse to
    start, and on a read-only replica it would be the wrong call entirely.
    """
    if st.session_state.get("_permissions_synced"):
        return
    try:
        from seeds.seed_roles_permissions import sync_permissions

        with session_scope() as db:
            changed = sync_permissions(db)
        if changed:
            log.info("Permissions brought up to date: %s", ", ".join(changed))
    except Exception:  # pragma: no cover - defensive
        log.exception("Could not sync permissions; some features may stay hidden")
    else:
        st.session_state["_permissions_synced"] = True


def _startup_check() -> tuple[bool, str | None]:
    """Verify the database schema matches this code.

    Deliberately **not** wrapped in ``st.cache_resource``. Caching here would
    also cache a *failure*, and a failure is precisely the state an operator is
    about to fix by editing the secrets — a cached one can only be cleared by a
    full redeploy, which makes the app look broken after it has been corrected.

    Success is remembered in session state so the query does not repeat on every
    rerun; failure re-evaluates, and drops the settings and engine caches first
    so a corrected secret is picked up on the next page load.
    """
    if st.session_state.get("_schema_verified"):
        return True, None

    settings = get_settings()
    applied, expected = schema_revisions()
    where = _database_identity()

    problem: str | None = None
    if applied is None:
        problem = (
            f"The database has no schema yet.\n\n"
            f"**Connected to:** `{where}`\n\n"
            f"**Secrets:** `{secrets_status()}`\n\n"
            "If the database above is not the one you expect, the `DATABASE_URL` "
            "secret is not reaching the application — the Secrets line tells you "
            "what was actually found. Otherwise apply the migrations:\n\n"
            "```\nalembic upgrade head\npython -m seeds.bootstrap\n```"
        )
    elif expected is not None and applied != expected:
        problem = (
            f"The database schema is at revision `{applied}` but this code expects "
            f"`{expected}`.\n\n**Connected to:** `{where}`\n\n"
            "Apply the outstanding migrations:\n\n```\nalembic upgrade head\n```"
        )

    if problem is None:
        _sync_permissions()
        st.session_state["_schema_verified"] = True
        log.info("Startup OK (env=%s, schema=%s, db=%s)", settings.app_env, applied, where)
        return True, None

    # Drop the cached settings and engine so an edited secret takes effect on
    # the next run rather than requiring a redeploy.
    reset_engine_cache()
    log.warning("Startup check failed against %s: %s", where, problem.splitlines()[0])
    return False, problem


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

def render_login() -> bool:
    """Draw the login form. Returns True when a sign-in succeeded.

    The caller performs the rerun. Calling ``st.rerun()`` from inside a
    container (``st.columns``, ``st.form``, ``st.sidebar``) unwinds the script
    without closing that container's context, and the next run then reports the
    form as nested inside itself.
    """
    left, middle, right = st.columns([1, 1.4, 1])
    with middle:
        st.title("📦 Soneet Quotations")
        st.caption("Quotation system — internal use only")

        if pop_expiry_notice():
            st.info(
                f"You were signed out after "
                f"{get_settings().session_timeout_minutes} minutes of inactivity."
            )

        with st.form("login_form"):
            identifier = st.text_input("Username or email", autocomplete="username")
            password = st.text_input(
                "Password", type="password", autocomplete="current-password"
            )
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")

        signed_in = False
        if submitted:
            if not identifier or not password:
                st.error("Enter your username and password.")
            else:
                try:
                    with session_scope() as db:
                        result = authenticate(db, identifier, password)
                except AuthenticationError as exc:
                    # One message for every failure mode; the audit log records
                    # which it actually was.
                    st.error(str(exc))
                else:
                    set_current_user(result.user)
                    signed_in = True

        st.divider()
        st.caption(
            "This application is for Soneet employees. Customers do not have "
            "accounts and quotations are sent to them outside the system."
        )

    return signed_in


def render_forced_password_change(user) -> bool:  # noqa: ANN001
    """Returns True when the session should be reset (password changed or signed out)."""
    finished = False
    left, middle, right = st.columns([1, 1.4, 1])
    with middle:
        st.title("Change your password")
        st.info(
            "Your password was set by an administrator and must be changed before "
            "you can continue."
        )
        with st.form("forced_password_change"):
            current = st.text_input("Current (temporary) password", type="password")
            new = st.text_input("New password", type="password")
            confirm = st.text_input("Confirm new password", type="password")
            submitted = st.form_submit_button("Update password", type="primary")

        if submitted:
            if new != confirm:
                st.error("The two new passwords do not match.")
            else:
                try:
                    with session_scope() as db:
                        change_password(db, user, current, new)
                except AuthenticationError as exc:
                    st.error(str(exc))
                else:
                    st.success("Password updated. Please sign in again.")
                    clear_session()
                    finished = True

        if st.button("Sign out"):
            with session_scope() as db:
                logout(db, user)
            clear_session()
            finished = True

    return finished


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #

#: (file, title, icon, required permission or None for "any authenticated user")
PAGE_SPECS: dict[str, list[tuple[str, str, str, Perm | None]]] = {
    "Quotations": [
        ("pages/01_Dashboard.py", "Dashboard", ":material/dashboard:", None),
        ("pages/02_Create_Quotation.py", "Create Quotation", ":material/note_add:", Perm.QUOTE_CREATE),
        ("pages/03_Quotation_History.py", "Quotation History", ":material/history:", Perm.QUOTE_VIEW_OWN),
        ("pages/04_Approval_Queue.py", "Approval Queue", ":material/task_alt:", Perm.QUOTE_APPROVE),
        ("pages/12_Customer_Portal.py", "Customer Portal", ":material/link:", Perm.QUOTE_PORTAL_PREVIEW),
    ],
    "Master data": [
        ("pages/05_Customers.py", "Customers", ":material/apartment:", Perm.CUSTOMER_VIEW),
        ("pages/06_Products_and_Pricing.py", "Products & Pricing", ":material/inventory_2:", Perm.PRODUCT_VIEW),
        ("pages/07_Excel_Import.py", "Excel Import", ":material/upload_file:", Perm.PRICE_IMPORT),
    ],
    "Insight": [
        ("pages/08_Reports.py", "Reports", ":material/analytics:", Perm.REPORT_VIEW),
    ],
    "Administration": [
        ("pages/09_Users_and_Permissions.py", "Users & Permissions", ":material/group:", Perm.USER_MANAGE),
        ("pages/10_Company_Settings.py", "Company Settings", ":material/settings:", Perm.SETTINGS_MANAGE),
        ("pages/11_Audit_Log.py", "Audit Log", ":material/receipt_long:", Perm.AUDIT_VIEW_OWN),
    ],
}


def visible_page_specs(user) -> dict[str, list[tuple[str, str, str, Perm | None]]]:  # noqa: ANN001
    """Which pages this user may open, as plain data.

    Separated from :func:`build_navigation` so the filtering decision can be
    tested without a Streamlit script-run context — ``st.Page`` silently returns
    a half-initialised object when constructed outside one.

    A hidden page is a courtesy, not a control: the page itself calls
    ``require_page`` and every service it invokes re-checks permission from the
    database.
    """
    visible: dict[str, list[tuple[str, str, str, Perm | None]]] = {}
    for group, specs in PAGE_SPECS.items():
        allowed = [
            spec for spec in specs
            if spec[3] is None or user.has(spec[3])
        ]
        if allowed:
            visible[group] = allowed
    return visible


def build_navigation(user) -> dict[str, list[st.Page]]:  # noqa: ANN001
    """Turn the visible specs into registered Streamlit pages."""
    return {
        group: [
            st.Page(
                path, title=title, icon=icon,
                default=path.endswith("01_Dashboard.py"),
            )
            for path, title, icon, _ in specs
        ]
        for group, specs in visible_page_specs(user).items()
    }


def render_sidebar_identity(user) -> bool:  # noqa: ANN001
    """Returns True when the user asked to sign out."""
    signed_out = False
    with st.sidebar:
        st.markdown(f"**{user.employee_name}**")
        st.caption(user.role_label)
        if get_settings().app_env != "production":
            st.caption(f":orange[{get_settings().app_env}]")
        if st.button("Sign out", width="stretch"):
            with session_scope() as db:
                logout(db, user)
            clear_session()
            signed_out = True
    return signed_out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def _run_standalone(render, title: str) -> None:  # noqa: ANN001
    """Render a single screen with the page menu hidden.

    Streamlit falls back to auto-discovering the ``pages/`` directory whenever
    ``st.navigation`` is not called. Returning early from ``main()`` — which is
    what the startup-error, login and change-password screens all do — therefore
    used to leave the sidebar listing every page in the application to someone
    who has not signed in. The pages defend themselves (each calls
    ``require_page``), but advertising the whole menu to an anonymous visitor is
    not something to leave in place.

    Registering exactly one hidden page keeps ``st.navigation`` in control on
    every path through this function.
    """
    st.navigation([st.Page(render, title=title)], position="hidden").run()


def main() -> None:
    healthy, problem = _startup_check()
    if not healthy:
        def _startup_error() -> None:
            st.error("The application cannot start.")
            st.markdown(problem or "")

        _run_standalone(_startup_error, "Soneet")
        return

    # Every st.rerun() in this application is issued here, at the top level of
    # the script, never from inside a column, form or sidebar context.
    user = current_user()
    if user is None:
        signed_in = False

        def _login() -> None:
            nonlocal signed_in
            signed_in = render_login()

        _run_standalone(_login, "Sign in")
        if signed_in:
            st.rerun()
        return

    if user.must_change_password:
        finished = False

        def _change_password() -> None:
            nonlocal finished
            finished = render_forced_password_change(user)

        _run_standalone(_change_password, "Change your password")
        if finished:
            st.rerun()
        return

    navigation = build_navigation(user)
    if not navigation:
        st.error(
            "Your account has no permissions assigned, so there is nothing to show. "
            "Ask your system administrator to assign you a role."
        )
        if render_sidebar_identity(user):
            st.rerun()
        return

    if render_sidebar_identity(user):
        st.rerun()
        return

    st.navigation(navigation).run()


# Streamlit executes the entrypoint script with ``__name__ == "__main__"``, so
# this guard runs the app normally while leaving the module importable — tests
# and tooling can read PAGE_SPECS or call build_navigation() without rendering
# a login form into a bare Streamlit context and corrupting its element stack.
if __name__ == "__main__":
    main()
