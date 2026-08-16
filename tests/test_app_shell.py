"""Smoke tests for the Streamlit shell.

These exercise the one part the service-level tests cannot reach: the actual
``app.py`` script — the login gate, the forced password change, and the
role-filtered navigation. Streamlit's own ``AppTest`` harness runs the real
script headlessly, so this is the app users get, not a re-implementation.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from modules.constants import RoleCode

APP = "app.py"
PASSWORD = "CorrectHorse9"


def _run(**session_state):
    app = AppTest.from_file(APP, default_timeout=60)
    for key, value in session_state.items():
        app.session_state[key] = value
    return app.run()


def _text(app) -> str:
    """Everything rendered, flattened, for substring assertions."""
    parts: list[str] = []
    for collection in (app.markdown, app.title, app.caption, app.info,
                       app.error, app.warning, app.success):
        parts.extend(str(element.value) for element in collection)
    return "\n".join(parts)


class TestStartup:
    def test_the_app_runs_without_raising(self):
        app = _run()
        assert not app.exception, app.exception

    def test_a_signed_out_visitor_gets_the_login_form(self):
        app = _run()
        labels = [i.label for i in app.text_input]
        assert "Username or email" in labels
        assert "Password" in labels
        assert any("Sign in" in b.label for b in app.button)

    def test_the_login_screen_states_that_it_is_internal_only(self):
        assert "Customers do not have" in _text(_run())

    def test_a_failed_startup_check_is_not_cached(self, engine):
        """The failure state is exactly what an operator is about to fix by
        editing the secrets. Caching it would mean the app still reports the
        old problem after the fix, recoverable only by a full redeploy."""
        from modules.database import Base

        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            from sqlalchemy import text

            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

        broken = _run()
        assert any("cannot start" in e.value for e in broken.error)

        # Restore the schema, exactly as applying the migration would.
        Base.metadata.create_all(engine)
        from tests.conftest import _stamp_alembic_head

        _stamp_alembic_head(engine)

        # A fresh run must recover without a process restart.
        recovered = _run()
        assert not recovered.error
        assert any("Username or email" == i.label for i in recovered.text_input)

    def test_the_failure_names_the_database_it_reached(self, engine):
        """'No schema' is ambiguous between an empty database and a missing
        DATABASE_URL falling back to SQLite. The message must distinguish them."""
        from sqlalchemy import text

        from modules.database import Base

        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

        app = _run()
        rendered = _text(app)
        assert "Connected to:" in rendered
        assert "sqlite" in rendered

    def test_no_page_content_leaks_before_sign_in(self):
        """The gate must run before any page module does."""
        rendered = _text(_run())
        for page_title in ("Approval Queue", "Company Settings", "Audit Log"):
            assert page_title not in rendered


class TestLogin:
    def test_bad_credentials_are_refused_with_the_generic_message(self, make_user):
        from modules.authentication import GENERIC_FAILURE

        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        app = _run()
        app.text_input[0].set_value("alice").run()
        app.text_input[1].set_value("WrongPassword1").run()
        app.button[0].click().run()

        assert any(GENERIC_FAILURE in e.value for e in app.error)

    def test_missing_credentials_are_refused(self, make_user):
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        app = _run()
        app.button[0].click().run()
        assert any("Enter your username" in e.value for e in app.error)

    def test_a_valid_sign_in_reaches_the_application(self, make_user):
        make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        app = _run()
        app.text_input[0].set_value("alice").run()
        app.text_input[1].set_value(PASSWORD).run()
        app.button[0].click().run()

        assert not app.exception, app.exception
        assert "auth_user" in app.session_state
        assert app.session_state["auth_user"].username == "alice"


class TestForcedPasswordChange:
    def test_a_temporary_password_forces_a_change_before_anything_else(
        self, session, make_user
    ):
        from modules.authorization import load_auth_user

        from datetime import UTC, datetime

        user = make_user(RoleCode.SALES.value, username="alice", password=PASSWORD)
        user.must_change_password = True
        session.commit()

        # Both keys must be set before the first run: a missing last-seen
        # timestamp reads as an expired session and clears the identity.
        app = _run(
            auth_user=load_auth_user(session, user),
            auth_last_seen=datetime.now(UTC),
        )

        assert not app.exception, app.exception
        assert any("Change your password" in t.value for t in app.title)
        # No navigation is offered until the password has been replaced.
        assert "Dashboard" not in _text(app)


class TestRoleFilteredNavigation:
    """A user must never be shown a page they cannot open. The page itself and
    every service it calls still re-check, but the menu should not tease.

    These assert on ``visible_page_specs``, the pure filtering step, rather than
    on constructed ``st.Page`` objects — ``st.Page`` returns a half-initialised
    object outside a script-run context, so inspecting one here would test the
    harness rather than the application.
    """

    @staticmethod
    def _titles(user) -> set[str]:
        from app import visible_page_specs

        return {
            title
            for specs in visible_page_specs(user).values()
            for _, title, _, _ in specs
        }

    def test_sales_sees_quoting_pages_only(self, make_auth_user):
        titles = self._titles(make_auth_user(RoleCode.SALES.value))
        assert {"Dashboard", "Create Quotation", "Quotation History"} <= titles
        assert "Approval Queue" not in titles
        assert "Excel Import" not in titles
        assert "Users & Permissions" not in titles
        assert "Company Settings" not in titles

    def test_a_manager_additionally_sees_the_approval_queue(self, make_auth_user):
        titles = self._titles(make_auth_user(RoleCode.SALES_MANAGER.value))
        assert "Approval Queue" in titles
        assert "Users & Permissions" not in titles

    def test_a_pricing_administrator_sees_import_but_not_quoting(self, make_auth_user):
        titles = self._titles(make_auth_user(RoleCode.PRICING_ADMIN.value))
        assert "Excel Import" in titles
        assert "Products & Pricing" in titles
        assert "Create Quotation" not in titles
        assert "Approval Queue" not in titles

    def test_a_system_administrator_sees_everything(self, make_auth_user):
        from app import PAGE_SPECS

        titles = self._titles(make_auth_user(RoleCode.SYS_ADMIN.value))
        expected = {title for specs in PAGE_SPECS.values() for _, title, _, _ in specs}
        assert titles == expected

    def test_every_page_spec_names_a_file_that_exists(self):
        from pathlib import Path

        from app import PAGE_SPECS

        missing = [
            path for specs in PAGE_SPECS.values()
            for path, _, _, _ in specs
            if not (Path(__file__).parent.parent / path).is_file()
        ]
        assert missing == []

    def test_a_signed_in_user_gets_the_sidebar_and_a_page(self, session, make_user):
        """One end-to-end run of the real script, signed in."""
        from datetime import UTC, datetime

        from modules.authorization import load_auth_user

        user = make_user(RoleCode.SALES.value, password=PASSWORD)
        app = AppTest.from_file(APP, default_timeout=60)
        app.session_state["auth_user"] = load_auth_user(session, user)
        app.session_state["auth_last_seen"] = datetime.now(UTC)
        app.run()

        assert not app.exception, app.exception
        assert any("Sign out" in b.label for b in app.button)
        # The default page rendered rather than the login form.
        assert any("Dashboard" in t.value for t in app.title)


class TestMasterDataPages:
    """The Phase 2 pages render for a permitted user and refuse an unpermitted one."""

    @staticmethod
    def _run_page(path: str, session, user):  # noqa: ANN001
        from datetime import UTC, datetime

        from modules.authorization import load_auth_user

        app = AppTest.from_file(path, default_timeout=60)
        app.session_state["auth_user"] = load_auth_user(session, user)
        app.session_state["auth_last_seen"] = datetime.now(UTC)
        return app.run()

    @pytest.mark.parametrize(
        ("path", "role"),
        [
            ("pages/05_Customers.py", RoleCode.SALES),
            ("pages/06_Products_and_Pricing.py", RoleCode.PRICING_ADMIN),
            ("pages/07_Excel_Import.py", RoleCode.PRICING_ADMIN),
        ],
    )
    def test_a_permitted_user_gets_the_page(self, session, make_user, path, role):
        app = self._run_page(path, session, make_user(role.value))
        assert not app.exception, app.exception

    @pytest.mark.parametrize(
        ("path", "role"),
        [
            ("pages/06_Products_and_Pricing.py", RoleCode.SALES),  # view only
            ("pages/05_Customers.py", RoleCode.PRICING_ADMIN),     # no customer.view
            ("pages/07_Excel_Import.py", RoleCode.SALES),          # no price.import
        ],
    )
    def test_pages_do_not_crash_for_other_roles(self, session, make_user, path, role):
        """Either the page renders read-only, or the guard stops it cleanly.
        Neither outcome may be a traceback."""
        app = self._run_page(path, session, make_user(role.value))
        assert not app.exception, app.exception

    def test_the_import_page_explains_itself_before_a_file_is_chosen(
        self, session, make_user
    ):
        app = self._run_page(
            "pages/07_Excel_Import.py", session, make_user(RoleCode.PRICING_ADMIN.value)
        )
        rendered = _text(app)
        assert "Board quality is read from each row" in rendered
        assert "never overwritten" in rendered

    def test_an_empty_catalogue_says_so_rather_than_showing_zeros(
        self, session, make_user
    ):
        app = self._run_page(
            "pages/06_Products_and_Pricing.py",
            session,
            make_user(RoleCode.PRICING_ADMIN.value),
        )
        assert "The catalogue is empty" in _text(app)

    def test_an_empty_catalogue_still_renders_every_tab(self, session, make_user):
        """Tabs share one script run, so an early st.stop() in the middle tab
        would silently blank the ones after it."""
        app = self._run_page(
            "pages/06_Products_and_Pricing.py",
            session,
            make_user(RoleCode.PRICING_ADMIN.value),
        )
        assert not app.exception, app.exception
        rendered = _text(app)
        # Catalogue tab guidance, pricing tab guard, and tiers tab caption.
        assert "add a product by hand" in rendered.lower()
        assert "before recording prices" in rendered
        assert "minimum-container figure" in rendered

    def test_a_pricing_administrator_gets_the_maintenance_forms(
        self, session, make_user
    ):
        """These were the two Phase 2 gaps: product/variant maintenance and
        price-tier management had services but no UI."""
        from modules.authorization import load_auth_user
        from modules.catalogue_service import create_product
        from modules.validation import ProductInput

        admin = make_user(RoleCode.PRICING_ADMIN.value)
        # The variant form only appears once a product exists to attach one to.
        create_product(
            session,
            load_auth_user(session, admin),
            ProductInput(item_number="WB-12", name='12" White', size_label='12" White'),
        )
        session.commit()

        app = self._run_page("pages/06_Products_and_Pricing.py", session, admin)
        labels = {b.label for b in app.button}
        assert {"Create product", "Create variant", "Save tier", "Create tier"} <= labels

    def test_a_sales_user_gets_no_maintenance_forms(self, session, make_user):
        app = self._run_page(
            "pages/06_Products_and_Pricing.py", session, make_user(RoleCode.SALES.value)
        )
        labels = {b.label for b in app.button}
        assert "Create product" not in labels
        assert "Save tier" not in labels
        assert "Editing tiers requires" in _text(app)


class TestQuotationPages:
    @staticmethod
    def _run_page(path: str, session, user, **state):  # noqa: ANN001
        from datetime import UTC, datetime

        from modules.authorization import load_auth_user

        app = AppTest.from_file(path, default_timeout=60)
        app.session_state["auth_user"] = load_auth_user(session, user)
        app.session_state["auth_last_seen"] = datetime.now(UTC)
        for key, value in state.items():
            app.session_state[key] = value
        return app.run()

    @pytest.fixture
    def priced_quotation(self, session, make_user):
        """A customer, a priced variant and a saved draft with one line."""
        import datetime as dt
        from decimal import Decimal as D

        from modules.authorization import load_auth_user
        from modules.catalogue_service import create_product, create_variant, set_price
        from modules.constants import PriceTierCode
        from modules.customer_service import create_customer
        from modules.quotation_service import add_line, create_draft
        from modules.validation import (
            CustomerInput,
            PriceInput,
            ProductInput,
            VariantInput,
        )

        admin_row = make_user(RoleCode.SYS_ADMIN.value, username="root")
        admin = load_auth_user(session, admin_row)

        customer = create_customer(
            session, admin,
            CustomerInput(customer_number="CUST-0001", company_name="Bunzl Canada"),
        )
        product = create_product(
            session, admin,
            ProductInput(item_number="WB-12", name='12" White', size_label='12" White'),
        )
        session.flush()
        variant = create_variant(
            session, admin, product.id,
            VariantInput(
                variant_item_number="WB-12-A",
                board_quality="WT110 HPFL115 KM135",
                case_pack=50,
            ),
        )
        set_price(
            session, admin,
            PriceInput(
                product_variant_id=variant.id,
                price_tier_code=PriceTierCode.STANDARD.value,
                price_per_pack=D("7.42"),
                effective_from=dt.date(2026, 1, 1),
            ),
        )
        quotation = create_draft(
            session, admin, customer.id, quote_date=dt.date(2026, 8, 3)
        )
        add_line(
            session, admin, quotation,
            product_variant_id=variant.id,
            price_tier_code=PriceTierCode.STANDARD.value,
            quantity_packs=D("1000"),
        )
        session.commit()
        return {"admin_row": admin_row, "quotation_id": quotation.id}

    def test_the_new_quotation_form_renders(self, session, make_user):
        app = self._run_page(
            "pages/02_Create_Quotation.py", session, make_user(RoleCode.SALES.value)
        )
        assert not app.exception, app.exception

    def test_it_says_so_when_there_are_no_customers(self, session, make_user):
        app = self._run_page(
            "pages/02_Create_Quotation.py", session, make_user(RoleCode.SALES.value)
        )
        assert "no customers yet" in _text(app)

    def test_an_existing_quotation_opens_with_its_totals(
        self, session, priced_quotation
    ):
        app = self._run_page(
            "pages/02_Create_Quotation.py",
            session,
            priced_quotation["admin_row"],
            active_quotation_id=priced_quotation["quotation_id"],
        )
        assert not app.exception, app.exception
        assert any("QT-2026-0001" in t.value for t in app.title)
        assert any(m.value == "$7,420.00" for m in app.metric)

    def test_the_lines_tab_offers_per_row_actions(self, session, priced_quotation):
        """Edit and delete sit on the row, not in a form below the table.

        The button labels are the assertion because the buttons are what the
        operator clicks; ``st.dataframe`` cannot hold one, which is why the
        table is drawn as columns.
        """
        app = self._run_page(
            "pages/02_Create_Quotation.py",
            session,
            priced_quotation["admin_row"],
            active_quotation_id=priced_quotation["quotation_id"],
        )
        assert not app.exception, app.exception
        labels = [b.label for b in app.button]
        assert "✏️" in labels, "no edit button on the line"
        assert "🗑" in labels, "no delete button on the line"
        assert "Actions" in _text(app)

    def test_the_change_a_line_section_is_gone(self, session, priced_quotation):
        """Removed from the interface, not hidden.

        Editing is only through the row's own button now, so a second editor
        further down the page would be two ways to do one thing — and the one
        people found first was the one that made them scroll.
        """
        app = self._run_page(
            "pages/02_Create_Quotation.py",
            session,
            priced_quotation["admin_row"],
            active_quotation_id=priced_quotation["quotation_id"],
        )
        assert not app.exception, app.exception
        text = _text(app)
        assert "Change a line" not in text
        assert "Re-price at tier" not in text
        assert "Save line" not in [b.label for b in app.button]
        # Adding a product is untouched.
        assert "Add a product" in text

    def test_history_lists_the_quotation(self, session, priced_quotation):
        app = self._run_page(
            "pages/03_Quotation_History.py", session, priced_quotation["admin_row"]
        )
        assert not app.exception, app.exception
        frame = app.dataframe[0].value
        assert "QT-2026-0001" in frame["Number"].to_list()

    def test_history_is_scoped_to_what_the_user_may_see(
        self, session, priced_quotation, make_user
    ):
        """A different salesperson must not see someone else's quotation."""
        app = self._run_page(
            "pages/03_Quotation_History.py",
            session,
            make_user(RoleCode.SALES.value, username="bob"),
        )
        assert not app.exception, app.exception
        assert "No quotations match" in _text(app)

    def test_history_renders_with_no_quotations_at_all(self, session, make_user):
        app = self._run_page(
            "pages/03_Quotation_History.py", session, make_user(RoleCode.SALES.value)
        )
        assert not app.exception, app.exception


class TestApprovalQueuePage:
    @staticmethod
    def _run(session, user, **state):  # noqa: ANN001
        from datetime import UTC, datetime

        from modules.authorization import load_auth_user

        app = AppTest.from_file("pages/04_Approval_Queue.py", default_timeout=60)
        app.session_state["auth_user"] = load_auth_user(session, user)
        app.session_state["auth_last_seen"] = datetime.now(UTC)
        for key, value in state.items():
            app.session_state[key] = value
        return app.run()

    def test_an_empty_queue_says_so(self, session, make_user):
        app = self._run(session, make_user(RoleCode.SALES_MANAGER.value))
        assert not app.exception, app.exception
        assert "Nothing is waiting for you" in _text(app)

    def test_sales_cannot_open_the_queue(self, session, make_user):
        app = self._run(session, make_user(RoleCode.SALES.value))
        assert not app.exception, app.exception
        assert "do not have permission" in _text(app)


class TestSessionExpiry:
    def test_a_stale_session_returns_to_the_login_form(self, session, make_user):
        from datetime import UTC, datetime, timedelta

        from modules.authorization import load_auth_user

        user = make_user(RoleCode.SALES.value, password=PASSWORD)
        app = AppTest.from_file(APP, default_timeout=60)
        app.session_state["auth_user"] = load_auth_user(session, user)
        app.session_state["auth_last_seen"] = datetime.now(UTC) - timedelta(minutes=90)
        app.run()

        assert "auth_user" not in app.session_state
        assert any("Username or email" == i.label for i in app.text_input)
        assert any("signed out" in i.value for i in app.info)

    def test_disabling_an_account_ends_the_session_on_the_next_run(
        self, session, make_user
    ):
        from datetime import UTC, datetime

        from modules.authorization import load_auth_user

        user = make_user(RoleCode.SALES.value, password=PASSWORD)
        app = AppTest.from_file(APP, default_timeout=60)
        app.session_state["auth_user"] = load_auth_user(session, user)
        app.session_state["auth_last_seen"] = datetime.now(UTC)
        app.run()
        assert "auth_user" in app.session_state

        user.is_active = False
        session.commit()

        app.run()
        assert "auth_user" not in app.session_state
        assert any("Username or email" == i.label for i in app.text_input)
