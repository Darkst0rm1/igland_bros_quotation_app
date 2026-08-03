"""Igland Bros Quotation Application — entry point.

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
from modules.config import get_settings
from modules.constants import Perm
from modules.database import schema_revisions, session_scope
from modules.session import (
    clear_session,
    current_user,
    pop_expiry_notice,
    set_current_user,
)

log = logging.getLogger(__name__)

st.set_page_config(
    page_title="Igland Bros — Quotations",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --------------------------------------------------------------------------- #
# Startup checks
# --------------------------------------------------------------------------- #

@st.cache_resource(show_spinner=False)
def _startup_check() -> tuple[bool, str | None]:
    """Verify the database schema matches this code. Runs once per process."""
    settings = get_settings()
    applied, expected = schema_revisions()

    if applied is None:
        return False, (
            "The database has no schema yet. Apply the migrations before starting:\n\n"
            "```\nalembic upgrade head\npython -m seeds.bootstrap\n```"
        )
    if expected is not None and applied != expected:
        return False, (
            f"The database schema is at revision `{applied}` but this code expects "
            f"`{expected}`.\n\nApply the outstanding migrations:\n\n"
            "```\nalembic upgrade head\n```"
        )
    log.info("Startup OK (env=%s, schema=%s)", settings.app_env, applied)
    return True, None


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
        st.title("📦 Igland Bros")
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
            "This application is for Igland Bros employees. Customers do not have "
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

def main() -> None:
    healthy, problem = _startup_check()
    if not healthy:
        st.error("The application cannot start.")
        st.markdown(problem or "")
        st.stop()

    # Every st.rerun() in this application is issued here, at the top level of
    # the script, never from inside a column, form or sidebar context.
    user = current_user()
    if user is None:
        if render_login():
            st.rerun()
        return

    if user.must_change_password:
        if render_forced_password_change(user):
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
