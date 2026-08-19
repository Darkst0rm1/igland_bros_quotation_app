"""How the Streamlit secrets panel reaches Settings — and how it silently didn't.

The panel is documented, and universally written, in UPPERCASE. Those keys were
handed to ``Settings(**secrets)`` as keyword arguments, which pydantic matches
against field names *case-sensitively* — ``case_sensitive=False`` governs the
environment source only. ``extra="ignore"`` then dropped every unmatched key
without a word.

It survived unnoticed because Streamlit also promotes secrets into os.environ,
where the case-insensitive environment source picks them up — but only for
str/int/float. Booleans are excluded there deliberately (bool is a subclass of
int, and Streamlit distinguishes them for exactly this promotion). So every
string setting arrived by the second path and looked fine, while
``email_enabled`` — the only bool in Settings — had no second path and quietly
fell back to its default False. The app reported "Email delivery is switched
off" while the panel plainly said ``EMAIL_ENABLED = true``.

These tests pin the normalisation, and pin the environment fallback that must
keep working alongside it.
"""
from __future__ import annotations

import pytest

# Settings whose absence would trip the production validator; every test here
# runs as development so that case-mapping is the only thing under examination.
_BASE = {"APP_ENV": "development"}


@pytest.fixture()
def load(monkeypatch):
    """Build Settings from a given secrets dict, with the environment silenced.

    ``.env`` is loaded into os.environ at import, so a developer machine already
    has EMAIL_ENABLED set. Left in place it would satisfy every assertion here
    through the environment and the tests would pass against the bug.
    """
    from modules import config

    for name in (
        "EMAIL_ENABLED", "EMAIL_BACKEND", "EMAIL_FROM_ADDRESS", "SMTP_HOST", "APP_ENV",
    ):
        monkeypatch.delenv(name, raising=False)

    def _load(secrets: dict, **env: str):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(config, "_streamlit_secrets", lambda: dict(secrets))
        config.get_settings.cache_clear()
        return config.get_settings()

    yield _load
    config.get_settings.cache_clear()


class TestSecretsKeyCase:
    """Every casing the panel can produce has to reach the same fields."""

    def test_uppercase_keys_load(self, load):
        s = load({**_BASE, "EMAIL_BACKEND": "smtp", "EMAIL_FROM_ADDRESS": "s@x.com"})
        assert s.email_backend == "smtp"
        assert s.email_from_address == "s@x.com"

    def test_a_toml_boolean_reaches_email_enabled(self, load):
        """The regression. `EMAIL_ENABLED = true` in TOML is a Python bool.

        Not a string: Streamlit will not promote it to os.environ, so the
        keyword-argument path is the only way in. Before the fix this returned
        False, which is precisely the reported symptom.
        """
        s = load({**_BASE, "EMAIL_ENABLED": True})
        assert s.email_enabled is True

    def test_a_toml_boolean_false_is_also_carried(self, load):
        """False must arrive as a real value, not be indistinguishable from absent."""
        s = load({**_BASE, "EMAIL_ENABLED": False})
        assert s.email_enabled is False

    def test_a_quoted_string_still_loads(self, load):
        """`EMAIL_ENABLED = "true"` — the workaround — must keep working."""
        assert load({**_BASE, "EMAIL_ENABLED": "true"}).email_enabled is True

    def test_lowercase_keys_load(self, load):
        s = load({"app_env": "development", "email_enabled": True, "email_backend": "smtp"})
        assert s.email_enabled is True
        assert s.email_backend == "smtp"

    @pytest.mark.parametrize("key", ["Email_Enabled", "eMaIl_EnAbLeD", "EMAIL_enabled"])
    def test_mixed_case_keys_load(self, load, key):
        assert load({**_BASE, key: True}).email_enabled is True

    def test_an_unknown_key_is_still_ignored(self, load):
        """Normalising must not turn a stray panel entry into a hard failure."""
        s = load({**_BASE, "NOT_A_SETTING": "x", "EMAIL_ENABLED": True})
        assert s.email_enabled is True


class TestEnvironmentStillLoads:
    """The environment path carried this application while secrets were dead.

    It must not regress: it is what Render uses, where there is no st.secrets at
    all and every value arrives as an environment variable.
    """

    def test_environment_variables_load_with_no_secrets(self, load):
        s = load({}, APP_ENV="development", EMAIL_ENABLED="true", EMAIL_BACKEND="smtp")
        assert s.email_enabled is True
        assert s.email_backend == "smtp"

    def test_environment_is_case_insensitive(self, load):
        assert load({}, APP_ENV="development", email_enabled="true").email_enabled is True

    def test_secrets_win_over_the_environment(self, load):
        """The documented resolution order: st.secrets first, environment second."""
        s = load({**_BASE, "EMAIL_BACKEND": "smtp"}, EMAIL_BACKEND="console")
        assert s.email_backend == "smtp"

    def test_the_environment_fills_what_secrets_omit(self, load):
        s = load({**_BASE, "EMAIL_ENABLED": True}, SMTP_HOST="smtp.example.com")
        assert s.email_enabled is True
        assert s.smtp_host == "smtp.example.com"


class TestDiagnosticLeaksNothing:
    """The diagnostic exists to be screenshotted. It must carry no secret."""

    def test_delivery_status_reports_presence_not_content(self, load):
        from modules.config import delivery_status

        load({
            **_BASE,
            "EMAIL_ENABLED": True,
            "SMTP_USERNAME": "sales@example.com",
            "SMTP_PASSWORD": "hunter2-super-secret",
            "EMAIL_FROM_ADDRESS": "sales@example.com",
            "EMAIL_PAYLOAD_KEYS": "v1:AAAA",
        })
        rendered = " ".join(f"{k} {v}" for k, v in delivery_status().items())

        assert "hunter2-super-secret" not in rendered
        assert "sales@example.com" not in rendered
        assert "v1:AAAA" not in rendered
        assert "set" in rendered and "on" in rendered

    def test_database_identity_omits_credentials(self, load):
        """The SYS_ADMIN view. Host and name, so two services can be compared."""
        from modules.config import database_identity

        load({**_BASE, "DATABASE_URL": "postgresql+psycopg://dbuser:dbpass@db.example.com/appdb"})
        shown = database_identity()

        assert "dbuser" not in shown
        assert "dbpass" not in shown
        assert "db.example.com" in shown and "appdb" in shown

    def test_database_kind_hides_the_host(self, load):
        """The pre-auth startup screen. It renders before anyone signs in, so it
        must not name the server — only say whether a real one was reached."""
        from modules.config import database_kind

        load({**_BASE, "DATABASE_URL": "postgresql+psycopg://dbuser:dbpass@db.example.com/appdb"})
        shown = database_kind()

        assert "dbuser" not in shown
        assert "dbpass" not in shown
        assert "db.example.com" not in shown
        assert "appdb" not in shown
        assert "postgresql" in shown

    def test_database_kind_still_names_the_sqlite_fallback(self, load):
        """The one thing that screen exists to disambiguate."""
        from modules.config import database_kind

        load({**_BASE, "DATABASE_URL": "sqlite:///./soneet.db"})
        assert "sqlite" in database_kind()
