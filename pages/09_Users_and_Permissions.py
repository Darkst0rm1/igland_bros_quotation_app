"""Users & Permissions — employee accounts, roles, grants and approval limits."""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from modules import user_service
from modules.authentication import AuthenticationError, admin_reset_password
from modules.authorization import PermissionDenied
from modules.constants import ROLE_DISPLAY_NAMES, Perm, RoleCode
from modules.database import session_scope
from modules.session import page_header, require_page
from modules.user_service import UserError
from modules.utilities import format_datetime, format_money

user = require_page(Perm.USER_MANAGE)
page_header("Users & Permissions", "Employee accounts, roles and approval limits")

can_manage_roles = user.has(Perm.ROLE_MANAGE)
can_manage_limits = user.has(Perm.APPROVAL_LIMITS_MANAGE)

ROLE_VALUES = {r.value for r in RoleCode}

with session_scope() as db:
    people = [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "employee_name": u.employee_name,
            "job_title": u.job_title or "",
            "manager_id": u.manager_id,
            "is_active": u.is_active,
            "must_change_password": u.must_change_password,
            "last_login_at": u.last_login_at,
            "locked_until": u.locked_until,
            "roles": sorted(r.code for r in u.roles),
            "extra_permissions": sorted(p.code for p in u.extra_permissions),
        }
        for u in user_service.all_users(db)
    ]
    roles = [
        {
            "code": r.code, "name": r.name,
            "max_discount_pct": r.max_discount_pct,
            "max_quote_value": r.max_quote_value,
            "min_margin_pct": r.min_margin_pct,
            "can_override_warnings": r.can_override_warnings,
        }
        for r in user_service.all_roles(db)
    ]
    permissions = [
        {"code": p.code, "category": p.category}
        for p in user_service.all_permissions(db)
    ]

names = {p["id"]: p["employee_name"] for p in people}


def _role_label(code: str) -> str:
    if code in ROLE_VALUES:
        return ROLE_DISPLAY_NAMES[RoleCode(code)]
    return code


accounts_tab, roles_tab, limits_tab = st.tabs(
    ["Accounts", "Roles & grants", "Approval limits"]
)


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

with accounts_tab:
    st.dataframe(
        [
            {
                "Name": p["employee_name"],
                "Username": p["username"],
                "Email": p["email"],
                "Roles": ", ".join(_role_label(c) for c in p["roles"]) or "none",
                "Active": "Yes" if p["is_active"] else "No",
                "Last login": format_datetime(p["last_login_at"]),
                "Locked": "Yes" if p["locked_until"] else "",
                "Must change password": "Yes" if p["must_change_password"] else "",
            }
            for p in people
        ],
        width="stretch",
        hide_index=True,
    )

    with st.expander("Add an employee"):
        with st.form("new_user"):
            new_a, new_b = st.columns(2)
            with new_a:
                new_name = st.text_input("Employee name *")
                new_username = st.text_input("Username *")
                new_email = st.text_input("Email *")
            with new_b:
                new_title = st.text_input("Job title")
                new_roles = st.multiselect(
                    "Roles", [r["code"] for r in roles], format_func=_role_label
                )
                new_manager = st.selectbox(
                    "Manager", [None, *names],
                    format_func=lambda uid: "None" if uid is None else names[uid],
                )
            created = st.form_submit_button("Create account", type="primary")

        if created:
            try:
                with session_scope() as db:
                    _, temporary = user_service.create_user(
                        db, user,
                        username=new_username, email=new_email,
                        employee_name=new_name, job_title=new_title or None,
                        role_codes=new_roles, manager_id=new_manager,
                    )
            except (UserError, PermissionDenied) as exc:
                st.error(str(exc))
            else:
                st.success(
                    f"Account created. Temporary password: **{temporary}**\n\n"
                    "Shown once and stored only as a hash. Hand it over in person or "
                    "by another channel — it must be changed at first login."
                )

    st.divider()
    st.markdown("##### Edit an account")
    target = st.selectbox(
        "Employee", people,
        format_func=lambda p: f"{p['employee_name']} ({p['username']})",
    )

    with st.form("edit_user"):
        edit_a, edit_b = st.columns(2)
        with edit_a:
            edit_name = st.text_input("Employee name", value=target["employee_name"])
            edit_email = st.text_input("Email", value=target["email"])
        with edit_b:
            edit_title = st.text_input("Job title", value=target["job_title"])
            manager_options = [None, *[uid for uid in names if uid != target["id"]]]
            edit_manager = st.selectbox(
                "Manager", manager_options,
                index=(
                    manager_options.index(target["manager_id"])
                    if target["manager_id"] in manager_options else 0
                ),
                format_func=lambda uid: "None" if uid is None else names[uid],
            )
        edit_active = st.checkbox("Active", value=target["is_active"])
        saved = st.form_submit_button("Save account", type="primary")

    if saved:
        try:
            with session_scope() as db:
                user_service.update_user(
                    db, user, target["id"],
                    email=edit_email, employee_name=edit_name,
                    job_title=edit_title or None, manager_id=edit_manager,
                    is_active=edit_active,
                )
        except (UserError, PermissionDenied) as exc:
            st.error(str(exc))
        else:
            st.toast("Account saved", icon="✅")
            st.rerun()

    st.markdown("##### Reset a password")
    st.caption(
        "Generates a temporary password and forces a change at next login. It is "
        "never emailed by this application and never appears in the audit log."
    )
    reset_reason = st.text_input("Reason", key="reset_reason", placeholder="Optional")
    if st.button("Reset this employee's password"):
        try:
            with session_scope() as db:
                temporary = admin_reset_password(
                    db, user, target["id"], reason=reset_reason or None
                )
        except (AuthenticationError, PermissionDenied) as exc:
            st.error(str(exc))
        else:
            st.success(
                f"Temporary password for **{target['employee_name']}**: "
                f"**{temporary}** — shown once."
            )


# --------------------------------------------------------------------------- #
# Roles & grants
# --------------------------------------------------------------------------- #

with roles_tab:
    if not can_manage_roles:
        st.info("Changing roles requires the role.manage permission.")
    else:
        role_target = st.selectbox(
            "Employee", people,
            format_func=lambda p: f"{p['employee_name']} ({p['username']})",
            key="role_target",
        )

        chosen_roles = st.multiselect(
            "Roles", [r["code"] for r in roles],
            default=role_target["roles"], format_func=_role_label,
        )
        if st.button("Save roles", type="primary"):
            try:
                with session_scope() as db:
                    user_service.set_roles(db, user, role_target["id"], chosen_roles)
            except (UserError, PermissionDenied) as exc:
                st.error(str(exc))
            else:
                st.toast("Roles updated", icon="✅")
                st.rerun()

        st.divider()
        st.markdown("##### Individual permission grants")
        st.caption(
            "Granted on top of the person's roles. This is how a salesperson gets cost "
            "visibility without being promoted, and without a bespoke role being "
            "invented for every exception."
        )
        chosen_permissions = st.multiselect(
            "Extra permissions",
            [p["code"] for p in permissions],
            default=role_target["extra_permissions"],
            format_func=lambda code: next(
                f"{p['category']} · {p['code']}"
                for p in permissions if p["code"] == code
            ),
        )
        grant_reason = st.text_input("Reason for the grant", key="grant_reason")
        if st.button("Save grants"):
            try:
                with session_scope() as db:
                    user_service.set_extra_permissions(
                        db, user, role_target["id"], chosen_permissions,
                        reason=grant_reason or None,
                    )
            except (UserError, PermissionDenied) as exc:
                st.error(str(exc))
            else:
                st.toast("Grants updated", icon="✅")
                st.rerun()


# --------------------------------------------------------------------------- #
# Approval limits
# --------------------------------------------------------------------------- #

with limits_tab:
    st.dataframe(
        [
            {
                "Role": r["name"],
                "Max discount %": (
                    f"{r['max_discount_pct']:g}"
                    if r["max_discount_pct"] is not None else "unlimited"
                ),
                "Max quotation value": (
                    format_money(r["max_quote_value"], "")
                    if r["max_quote_value"] is not None else "unlimited"
                ),
                "Min margin %": (
                    f"{r['min_margin_pct']:g}"
                    if r["min_margin_pct"] is not None else "no floor"
                ),
                "May override warnings": "Yes" if r["can_override_warnings"] else "",
            }
            for r in roles
        ],
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "Anything beyond a person's limit requires approval. Where someone holds "
        "several roles the **most permissive** value applies, so raising a limit here "
        "can widen more people's authority than the role name suggests."
    )

    if not can_manage_limits:
        st.info(
            "Changing approval limits requires the approval_limits.manage permission."
        )
    else:
        limit_target = st.selectbox(
            "Role", roles, format_func=lambda r: r["name"], key="limit_role"
        )
        with st.form("role_limits"):
            limit_a, limit_b = st.columns(2)
            with limit_a:
                unlimited_discount = st.checkbox(
                    "No discount limit", value=limit_target["max_discount_pct"] is None
                )
                discount_limit = st.number_input(
                    "Maximum discount %", min_value=0.0, max_value=100.0, step=1.0,
                    value=float(limit_target["max_discount_pct"] or 0),
                    disabled=unlimited_discount,
                )
                unlimited_margin = st.checkbox(
                    "No margin floor", value=limit_target["min_margin_pct"] is None
                )
                margin_floor = st.number_input(
                    "Minimum margin %", min_value=0.0, max_value=100.0, step=1.0,
                    value=float(limit_target["min_margin_pct"] or 0),
                    disabled=unlimited_margin,
                )
            with limit_b:
                unlimited_value = st.checkbox(
                    "No value limit", value=limit_target["max_quote_value"] is None
                )
                value_limit = st.number_input(
                    "Maximum quotation value", min_value=0.0, step=1000.0,
                    value=float(limit_target["max_quote_value"] or 0),
                    disabled=unlimited_value,
                )
                may_override = st.checkbox(
                    "May override pricing warnings",
                    value=limit_target["can_override_warnings"],
                )
            limits_saved = st.form_submit_button("Save limits", type="primary")

        if limits_saved:
            try:
                with session_scope() as db:
                    user_service.update_role_limits(
                        db, user, limit_target["code"],
                        max_discount_pct=(
                            None if unlimited_discount else Decimal(str(discount_limit))
                        ),
                        max_quote_value=(
                            None if unlimited_value else Decimal(str(value_limit))
                        ),
                        min_margin_pct=(
                            None if unlimited_margin else Decimal(str(margin_floor))
                        ),
                        can_override_warnings=may_override,
                    )
            except (UserError, PermissionDenied) as exc:
                st.error(str(exc))
            else:
                st.toast("Limits updated", icon="✅")
                st.rerun()
