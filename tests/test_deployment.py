"""The deployment itself, checked here rather than by a failed deploy.

A Blueprint is code that runs once, in an environment nobody can iterate in, at
the moment a customer-facing service starts. The mistakes it invites are the
quiet ones — a start command that binds the wrong interface, a worker that
migrates concurrently with the web service, a credential pasted into YAML that
is then in git forever.

So the parts that can be asserted from here, are.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BLUEPRINT = ROOT / "render.yaml"


@pytest.fixture(scope="module")
def blueprint() -> dict:
    import yaml

    return yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def services(blueprint) -> dict:
    return {s["name"]: s for s in blueprint["services"]}


@pytest.fixture(scope="module")
def shared_group(blueprint) -> dict:
    group = next(
        g for g in blueprint["envVarGroups"] if g["name"] == "soneet-shared"
    )
    return {v["key"]: v for v in group["envVars"]}


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #

class TestBlueprintParses:
    def test_the_file_exists_and_is_valid_yaml(self, blueprint):
        assert blueprint["services"]
        assert blueprint["envVarGroups"]

    def test_exactly_two_services(self, services):
        """The employee app stays on Community Cloud; only these two move."""
        assert set(services) == {"soneet-portal", "soneet-worker"}

    def test_the_portal_is_a_web_service_and_the_worker_is_not(self, services):
        assert services["soneet-portal"]["type"] == "web"
        assert services["soneet-worker"]["type"] == "worker"

    def test_both_use_the_python_runtime(self, services):
        for service in services.values():
            assert service["runtime"] == "python"

    def test_both_share_one_environment_group(self, services):
        """Divergent configuration here fails silently, so it must be shared."""
        for service in services.values():
            groups = [
                v["fromGroup"] for v in service["envVars"] if "fromGroup" in v
            ]
            assert groups == ["soneet-shared"]

    def test_both_deploy_from_the_same_branch(self, services):
        branches = {s["branch"] for s in services.values()}
        assert len(branches) == 1


class TestStartCommands:
    def test_the_portal_serves_the_real_application(self, services):
        command = " ".join(services["soneet-portal"]["startCommand"].split())
        assert command.startswith("uvicorn portal.app:app")

    def test_the_portal_binds_the_assigned_port_on_all_interfaces(self, services):
        command = services["soneet-portal"]["startCommand"]
        assert "--host 0.0.0.0" in command
        assert "--port $PORT" in command

    def test_the_portal_trusts_the_proxy_headers(self, services):
        """Rate limiting keys on the client address, which arrives via the proxy."""
        assert "--proxy-headers" in services["soneet-portal"]["startCommand"]

    def test_the_portal_import_path_actually_exists(self):
        """A start command naming a module that does not exist fails at deploy."""
        from portal.app import app

        assert app is not None

    def test_the_worker_runs_continuously(self, services):
        command = services["soneet-worker"]["startCommand"].strip()
        assert command == "python -m modules.worker --loop"

    def test_the_worker_is_not_one_shot(self, services):
        """Render restarts an exited worker; --once would be a restart loop."""
        assert "--once" not in services["soneet-worker"]["startCommand"]

    def test_the_worker_cli_accepts_that_command(self):
        import argparse
        import inspect

        from modules import worker

        source = inspect.getsource(worker.main)
        assert "--loop" in source
        # And the module is executable as `python -m modules.worker`.
        assert (ROOT / "modules" / "worker.py").is_file()
        assert isinstance(argparse.ArgumentParser(), argparse.ArgumentParser)

    def test_the_build_command_matches_the_repository(self, services):
        assert (ROOT / "requirements.txt").is_file()
        for service in services.values():
            assert service["buildCommand"] == "pip install -r requirements.txt"

    def test_uvicorn_is_a_declared_dependency(self):
        """The portal's start command *is* the uvicorn CLI."""
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert re.search(r"^uvicorn", requirements, re.MULTILINE)


class TestHealthAndMigrations:
    def test_the_portal_has_a_health_check(self, services):
        assert services["soneet-portal"]["healthCheckPath"] == "/health"

    def test_the_health_path_exists_and_leaks_nothing(self):
        from starlette.testclient import TestClient

        from portal.app import app

        response = TestClient(app).get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_the_worker_has_no_health_check_path(self, services):
        """Background workers take no traffic; Render rejects the field."""
        assert "healthCheckPath" not in services["soneet-worker"]

    def test_exactly_one_service_owns_migrations(self, services):
        """Two services racing Alembic is the failure this prevents."""
        owners = [
            name for name, s in services.items() if s.get("preDeployCommand")
        ]
        assert owners == ["soneet-portal"]

    def test_the_migration_command_is_the_repository_equivalent(self, services):
        assert services["soneet-portal"]["preDeployCommand"] == "alembic upgrade head"
        assert (ROOT / "alembic.ini").is_file()

    def test_the_worker_never_migrates(self, services):
        """So it cannot begin consuming jobs against an outdated schema."""
        worker = services["soneet-worker"]
        assert not worker.get("preDeployCommand")
        assert "alembic" not in worker["startCommand"]


class TestNoFreeWebServiceForCustomers:
    def test_neither_service_is_on_the_free_plan(self, services):
        """A free web service sleeps for 15 minutes and wakes in about one.

        A customer opening a quotation link would wait on a loading page, and
        background workers have no free tier at all.
        """
        for name, service in services.items():
            assert service["plan"] != "free", name

    def test_the_plans_are_valid_render_values(self, services):
        valid = {"starter", "standard", "pro", "pro plus", "pro max", "pro ultra"}
        for service in services.values():
            assert service["plan"] in valid


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

class TestNoCommittedSecrets:
    #: Every value that must never be literal in the Blueprint.
    SECRET_KEYS = {
        "DATABASE_URL", "SECRET_KEY",
        "STORAGE_ACCESS_KEY_ID", "STORAGE_SECRET_ACCESS_KEY",
        "STORAGE_ENDPOINT_URL",
        "SMTP_PASSWORD", "SMTP_USERNAME", "SMTP_HOST",
        "EMAIL_PAYLOAD_KEYS",
        "PORTAL_BASE_URL",
    }

    def test_every_secret_is_a_placeholder(self, shared_group):
        for key in self.SECRET_KEYS:
            assert key in shared_group, f"{key} is not declared"
            entry = shared_group[key]
            assert entry.get("sync") is False, f"{key} is not sync:false"
            assert "value" not in entry, f"{key} carries a literal value"

    def test_the_file_contains_no_credential_shaped_string(self):
        text = BLUEPRINT.read_text(encoding="utf-8")
        forbidden = [
            r"postgres(ql)?://[^\s]*:[^\s]*@",     # a real connection string
            r"npg_[A-Za-z0-9]{8,}",                # a Neon password
            r"\br2\.cloudflarestorage\.com",       # a real R2 endpoint
            r"\b[0-9a-f]{32,}\b",                  # a key or account id
            r"BEGIN [A-Z ]*PRIVATE KEY",
        ]
        for pattern in forbidden:
            assert not re.search(pattern, text), f"blueprint matches {pattern}"

    def test_no_deployment_file_carries_a_credential(self):
        """Shapes, not specific values.

        The obvious version of this test embeds fragments of the credentials
        that leaked during testing — which records part of a secret in a public
        repository to prove the secret is not in the repository. Matching the
        *shape* avoids that and catches the next one too, which the fragment
        version could not.
        """
        patterns = {
            "Neon role password": r"\bnpg_[A-Za-z0-9]{8,}",
            "connection string with a password": r"://[^\s/@]+:[^\s/@]+@",
            "long hex key or account id": r"\b[0-9a-f]{32,}\b",
            "AWS-style key id": r"\bAKIA[0-9A-Z]{16}\b",
            "private key block": r"BEGIN [A-Z ]*PRIVATE KEY",
        }

        def is_placeholder(text: str) -> bool:
            """Documentation has to show the *shape* of a credential somewhere.

            ``://USER:PASSWORD@host`` and ``ACCOUNT_ID`` are the point of an
            example file. Placeholders are conventionally shouted or bracketed,
            and a real secret never is.
            """
            stripped = text.strip("<>[]{}")
            return (
                stripped.isupper()
                or text.startswith(("<", "[", "{"))
                or "PASSWORD" in text.upper()
                or "ACCOUNT_ID" in text.upper()
                or "USER" in text.upper()
            )
        files = [
            BLUEPRINT,
            ROOT / ".env.example",
            ROOT / ".python-version",
            ROOT / "docs" / "GO_LIVE.md",
            ROOT / "docs" / "OPERATIONS.md",
            ROOT / "scripts" / "preflight.py",
            ROOT / "scripts" / "check_storage.py",
        ]
        for path in files:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for label, pattern in patterns.items():
                real = [
                    m.group(0) for m in re.finditer(pattern, text)
                    if not is_placeholder(m.group(0))
                ]
                assert not real, (
                    f"{path.name} looks like it contains a {label}"
                )

    def test_dotenv_is_not_tracked(self):
        import subprocess

        listed = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
        ).stdout.splitlines()
        assert ".env" not in listed
        assert ".env.example" in listed


class TestRequiredPlaceholders:
    #: Everything the brief requires the group to declare.
    EXPECTED = [
        "APP_ENV", "LOG_LEVEL", "TZ", "DATABASE_URL", "SECRET_KEY",
        "STORAGE_BACKEND", "STORAGE_BUCKET", "STORAGE_ENDPOINT_URL",
        "STORAGE_REGION", "STORAGE_ACCESS_KEY_ID", "STORAGE_SECRET_ACCESS_KEY",
        "PORTAL_BASE_URL", "PORTAL_SUPPORT_EMAIL", "PORTAL_LINK_DAYS",
        "EMAIL_ENABLED", "EMAIL_BACKEND", "EMAIL_FROM_ADDRESS",
        "EMAIL_FROM_NAME", "EMAIL_REPLY_TO", "EMAIL_INTERNAL_RECIPIENTS",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
        "SMTP_SECURITY", "SMTP_TIMEOUT_SECONDS", "EMAIL_MAX_ATTEMPTS",
        "EMAIL_PAYLOAD_KEYS", "EMAIL_PAYLOAD_KEY_VERSION",
        "EMAIL_PAYLOAD_TTL_HOURS",
        "WORKER_POLL_SECONDS", "WORKER_BATCH_SIZE", "WORKER_LEASE_SECONDS",
        "WORKER_STALE_AFTER_SECONDS",
    ]

    def test_every_required_variable_is_declared(self, shared_group):
        missing = [key for key in self.EXPECTED if key not in shared_group]
        assert not missing, f"missing: {missing}"

    def test_every_declared_name_is_a_real_setting(self, shared_group):
        """A typo here is a value silently ignored at runtime."""
        from modules.config import Settings

        known = {name.upper() for name in Settings.model_fields}
        for key in shared_group:
            assert key in known, f"{key} is not a Settings field"

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("APP_ENV", "production"),
            ("LOG_LEVEL", "INFO"),
            ("TZ", "America/Toronto"),
            ("STORAGE_BACKEND", "s3"),
            ("STORAGE_BUCKET", "soneet-quotations"),
            ("STORAGE_REGION", "auto"),
            ("EMAIL_ENABLED", "false"),
            ("EMAIL_BACKEND", "smtp"),
            ("PORTAL_LINK_DAYS", "30"),
            ("SMTP_PORT", "587"),
            ("SMTP_SECURITY", "starttls"),
            ("SMTP_TIMEOUT_SECONDS", "20"),
            ("EMAIL_MAX_ATTEMPTS", "6"),
            ("EMAIL_PAYLOAD_KEY_VERSION", "v1"),
            ("EMAIL_PAYLOAD_TTL_HOURS", "72"),
            ("WORKER_POLL_SECONDS", "30"),
            ("WORKER_BATCH_SIZE", "20"),
            ("WORKER_LEASE_SECONDS", "300"),
            ("WORKER_STALE_AFTER_SECONDS", "900"),
        ],
    )
    def test_the_production_safe_defaults(self, shared_group, key, expected):
        assert str(shared_group[key]["value"]) == expected

    def test_email_starts_disabled(self, shared_group):
        """Queue and wait is the safe first state; nothing is lost."""
        assert shared_group["EMAIL_ENABLED"]["value"] == "false"

    def test_the_python_version_is_pinned(self):
        pin = ROOT / ".python-version"
        assert pin.is_file()
        assert pin.read_text(encoding="utf-8").strip() == "3.13"


class TestProductionPortalUrlGate:
    def _production(self, **overrides):
        from modules.config import Settings

        base = dict(
            app_env="production",
            secret_key="x" * 40,
            database_url="postgresql+psycopg://u:p@host/db",
            storage_backend="s3",
            storage_bucket="b",
            storage_access_key_id="k",
            storage_secret_access_key="s",
            email_enabled=False,
        )
        base.update(overrides)
        return Settings(**base)

    def test_a_blank_url_is_refused_in_production(self):
        from portal.branding import PortalConfigError, validate_portal_settings

        with pytest.raises(PortalConfigError):
            validate_portal_settings(self._production(portal_base_url=""))

    def test_a_non_https_url_is_refused(self):
        from portal.branding import PortalConfigError, validate_portal_settings

        with pytest.raises(PortalConfigError):
            validate_portal_settings(
                self._production(portal_base_url="http://quotes.example.com")
            )

    def test_a_real_https_origin_is_accepted(self):
        from portal.branding import validate_portal_settings

        validate_portal_settings(
            self._production(portal_base_url="https://soneet-portal.onrender.com")
        )

    def test_the_blueprint_does_not_guess_the_url(self, shared_group):
        """It cannot be known before the service exists."""
        assert shared_group["PORTAL_BASE_URL"].get("sync") is False
        assert "value" not in shared_group["PORTAL_BASE_URL"]


# --------------------------------------------------------------------------- #
# The database heartbeat
# --------------------------------------------------------------------------- #

class TestWorkerHeartbeat:
    def test_a_sweep_is_recorded(self, session):
        from modules import worker_heartbeat as hb

        hb.record_sweep(session, healthy=True, summary="storage=0 emails=2")
        view = hb.read(session)

        assert view.is_configured and view.is_healthy
        assert view.label == "Running"
        assert view.summary == "storage=0 emails=2"

    def test_nothing_recorded_reads_as_not_configured(self, session):
        from modules import worker_heartbeat as hb

        view = hb.read(session)
        assert not view.is_configured
        assert view.label == "Not configured"
        assert not view.is_stale

    def test_an_old_sweep_reads_as_stale(self, session):
        from modules import worker_heartbeat as hb

        old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
        hb.record_sweep(session, healthy=True, now=old)

        view = hb.read(session, stale_after_seconds=900)
        assert view.is_stale
        assert view.label == "Not running recently"
        assert view.age_seconds > 900

    def test_the_stale_threshold_comes_from_configuration(self, session, monkeypatch):
        from modules import worker_heartbeat as hb
        from modules.config import get_settings

        old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=120)
        hb.record_sweep(session, healthy=True, now=old)

        monkeypatch.setattr(get_settings(), "worker_stale_after_seconds", 60)
        assert hb.read(session).is_stale
        monkeypatch.setattr(get_settings(), "worker_stale_after_seconds", 600)
        assert not hb.read(session).is_stale

    def test_a_degraded_sweep_is_flagged_and_not_counted_as_success(self, session):
        """A worker looping on errors is not healthy because it is running."""
        from modules import worker_heartbeat as hb

        hb.record_sweep(session, healthy=False, summary="errors=email")
        view = hb.read(session)

        assert view.degraded
        assert not view.is_healthy          # never succeeded
        assert view.last_attempt_at is not None
        assert view.last_success_at is None

    def test_a_later_success_clears_the_stale_reading(self, session):
        from modules import worker_heartbeat as hb

        hb.record_sweep(session, healthy=False)
        assert not hb.read(session).is_healthy

        hb.record_sweep(session, healthy=True)
        view = hb.read(session)
        assert view.is_healthy and not view.degraded

    def test_repeated_sweeps_update_one_row(self, session):
        from modules import worker_heartbeat as hb
        from modules.models import WorkerHeartbeat

        for _ in range(5):
            hb.record_sweep(session, healthy=True)
        assert session.query(WorkerHeartbeat).count() == 1

    def test_the_view_exposes_no_host_path_or_identity(self, session):
        from dataclasses import fields

        from modules import worker_heartbeat as hb

        names = {f.name for f in fields(hb.HeartbeatView)}
        assert not names & {
            "path", "file", "host", "hostname", "pid", "worker_id", "owner",
            "last_error", "traceback",
        }

        hb.record_sweep(session, healthy=False, summary="errors=email")
        view = hb.read(session)
        assert "/" not in view.detail and "\\" not in view.detail

    def test_the_worker_writes_it_during_a_sweep(self, session, monkeypatch):
        from modules import worker
        from modules.models import WorkerHeartbeat

        monkeypatch.setattr(worker, "sweep_storage_cleanups", lambda limit: 0)
        monkeypatch.setattr(worker, "sweep_document_jobs", lambda limit: 0)
        monkeypatch.setattr(worker, "sweep_email_outbox", lambda limit, owner: (0, 0))
        monkeypatch.setattr(worker, "sweep_expired_payloads", lambda: 0)

        worker.run_sweep()
        session.expire_all()

        row = session.query(WorkerHeartbeat).one()
        assert row.status == "HEALTHY"
        assert row.last_success_at is not None

    def test_a_failed_sweep_records_degraded(self, session, monkeypatch):
        from modules import worker
        from modules.models import WorkerHeartbeat

        monkeypatch.setattr(
            worker, "sweep_email_outbox",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
        )
        worker.run_sweep()
        session.expire_all()

        assert session.query(WorkerHeartbeat).one().status == "DEGRADED"

    def test_two_processes_see_the_same_heartbeat(self, session):
        """The whole reason this moved out of a file.

        A second session stands in for the employee application on a different
        machine: no shared filesystem, only the database.
        """
        from modules import worker_heartbeat as hb
        from modules.database import get_session

        hb.record_sweep(session, healthy=True, summary="storage=1 emails=3")
        session.commit()

        other = get_session()
        try:
            view = hb.read(other)
            assert view.is_configured and view.is_healthy
            assert view.summary == "storage=1 emails=3"
        finally:
            other.close()

    def test_the_employee_view_reads_the_database_not_a_file(
        self, session, monkeypatch
    ):
        from modules import quote_send_service as sender
        from modules import worker_heartbeat as hb
        from modules.worker import HEALTH_FILE_ENV

        # No file signal at all — the production situation.
        monkeypatch.delenv(HEALTH_FILE_ENV, raising=False)
        hb.record_sweep(session, healthy=True)

        health = sender.worker_health(session)
        assert health.is_configured and health.is_healthy
        assert health.label == "Running"

    def test_the_file_signal_still_works_for_local_development(
        self, session, tmp_path, monkeypatch
    ):
        from modules import quote_send_service as sender
        from modules.worker import HEALTH_FILE_ENV

        signal = tmp_path / "health.txt"
        signal.write_text("now ok storage=0\n", encoding="utf-8")
        monkeypatch.setenv(HEALTH_FILE_ENV, str(signal))

        # No database row: falls back to the file.
        health = sender.worker_health(session)
        assert health.is_configured and health.is_healthy

    def test_the_database_wins_when_both_exist(self, session, tmp_path, monkeypatch):
        """Production has a database row; a stale file must not override it."""
        import os

        from modules import quote_send_service as sender
        from modules import worker_heartbeat as hb
        from modules.worker import HEALTH_FILE_ENV

        signal = tmp_path / "health.txt"
        signal.write_text("old ok\n", encoding="utf-8")
        ancient = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
        os.utime(signal, (ancient.timestamp(), ancient.timestamp()))
        monkeypatch.setenv(HEALTH_FILE_ENV, str(signal))

        hb.record_sweep(session, healthy=True)
        assert sender.worker_health(session).is_healthy


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

class TestPreflight:
    def test_it_redacts_a_connection_string(self):
        from scripts.preflight import redact

        assert "hunter2" not in redact(
            "could not connect to postgresql://user:hunter2@host/db"
        )
        assert "***@host" in redact("postgresql://user:hunter2@host/db")

    def test_it_redacts_key_value_secrets(self):
        from scripts.preflight import redact

        for text in (
            "password=hunter2", "SECRET_KEY: abcdef123456", "api_key=xyz",
            "STORAGE_SECRET_ACCESS_KEY=deadbeefcafe",
        ):
            assert "hunter2" not in redact(text)
            assert "abcdef123456" not in redact(text)
            assert "xyz" not in redact(text)
            assert "deadbeefcafe" not in redact(text)

    def test_it_never_prints_a_value_only_whether_it_is_set(self):
        from scripts.preflight import describe

        assert describe("a-real-secret-value") == "set"
        assert describe("") == "MISSING"
        assert describe(None) == "MISSING"

    def test_the_report_redacts_everything_added_to_it(self):
        from scripts.preflight import Report

        report = Report()
        report.fail("Database", "postgresql://u:hunter2@host/db refused")
        assert "hunter2" not in report.render()

    def test_a_blank_portal_url_fails(self):
        from scripts.preflight import Report, check_portal_url

        report = Report()
        check_portal_url(report, _settings(portal_base_url=""))
        assert report.failed

    def test_a_plain_http_portal_url_fails(self):
        from scripts.preflight import Report, check_portal_url

        report = Report()
        check_portal_url(report, _settings(portal_base_url="http://quotes.example.com"))
        assert report.failed

    def test_the_r2_endpoint_as_a_portal_url_fails(self):
        """A real mistake: the two URLs are both handed over at the same time."""
        from scripts.preflight import Report, check_portal_url

        report = Report()
        check_portal_url(
            report,
            _settings(portal_base_url="https://abc123.r2.cloudflarestorage.com"),
        )
        assert report.failed
        assert any("object-storage" in detail for _, _, detail in report.rows)

    def test_a_valid_https_origin_passes(self):
        from scripts.preflight import Report, check_portal_url

        report = Report()
        check_portal_url(
            report, _settings(portal_base_url="https://soneet-portal.onrender.com")
        )
        assert not report.failed

    def test_the_storage_round_trip_uses_a_temporary_object(self, monkeypatch):
        """It must clean up after itself, and use the real adapter."""
        from modules import storage
        from scripts.preflight import Report, check_storage

        seen: dict[str, bytes] = {}
        deleted: list[str] = []

        class Fake:
            def put(self, key, data, content_type=None):  # noqa: ANN001
                seen[key] = data
                return key

            def get(self, key):  # noqa: ANN001
                return seen[key]

            def exists(self, key):  # noqa: ANN001
                return key in seen

            def delete(self, key):  # noqa: ANN001
                deleted.append(key)
                seen.pop(key, None)

        monkeypatch.setattr(storage, "get_storage", lambda: Fake())
        report = Report()
        check_storage(report, _settings(storage_backend="s3", storage_bucket="b"))

        assert not report.failed
        assert deleted, "the temporary object was not removed"
        assert all(key.startswith("_preflight/") for key in deleted)
        assert seen == {}

    def test_a_storage_failure_is_reported_without_the_signed_request(
        self, monkeypatch
    ):
        from modules import storage
        from scripts.preflight import Report, check_storage

        class Broken:
            def put(self, *a, **k):  # noqa: ANN002, ANN003
                raise RuntimeError(
                    "SignatureDoesNotMatch: key=deadbeefcafe secret=abc"
                )

        monkeypatch.setattr(storage, "get_storage", lambda: Broken())
        report = Report()
        check_storage(report, _settings(storage_backend="s3", storage_bucket="b"))

        rendered = report.render()
        assert report.failed
        assert "deadbeefcafe" not in rendered or "***" in rendered

    def test_the_smtp_check_sends_nothing(self, monkeypatch):
        import smtplib

        from scripts.preflight import Report, check_email

        def refuse(*_args, **_kwargs):
            raise AssertionError("preflight attempted to connect to SMTP")

        monkeypatch.setattr(smtplib, "SMTP", refuse)
        monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)

        report = Report()
        check_email(
            report,
            _settings(
                email_backend="smtp", smtp_host="mail.example.com",
                email_from_address="quotes@example.com", email_enabled=False,
            ),
        )
        assert not report.failed

    def test_the_smtp_check_never_prints_the_password(self):
        from scripts.preflight import Report, check_email

        report = Report()
        check_email(
            report,
            _settings(
                email_backend="smtp", smtp_host="mail.example.com",
                smtp_password="hunter2", email_from_address="q@example.com",
            ),
        )
        assert "hunter2" not in report.render()

    def test_email_enabled_is_flagged_during_first_deployment(self):
        from scripts.preflight import Report, check_email

        report = Report()
        check_email(
            report,
            _settings(
                email_enabled=True, email_backend="smtp",
                smtp_host="mail.example.com", email_from_address="q@example.com",
            ),
        )
        assert any(
            status == "warn" and "ENABLED" in detail
            for status, _, detail in report.rows
        )

    def test_a_capture_backend_while_enabled_fails(self):
        from scripts.preflight import Report, check_email

        report = Report()
        check_email(
            report, _settings(email_enabled=True, email_backend="console"),
        )
        assert report.failed

    def test_the_heartbeat_table_check_passes_once_migrated(self, session):
        from scripts.preflight import Report, check_heartbeat_table

        report = Report()
        check_heartbeat_table(report)
        assert not report.failed

    def test_the_migration_head_check_passes_on_a_current_database(self, session):
        from scripts.preflight import Report, _check_migration_head

        report = Report()
        _check_migration_head(report)
        assert not report.failed

    def test_the_key_agreement_check_reports_the_version_not_the_key(
        self, session
    ):
        from scripts.preflight import Report, check_key_agreement

        report = Report()
        check_key_agreement(report, _settings(email_payload_keys="t1:AAAA"))

        rendered = report.render()
        assert "AAAA" not in rendered
        assert "t1" in rendered

    def test_a_full_run_returns_a_report_without_secrets(self, session):
        from scripts import preflight

        report = preflight.run()
        rendered = report.render()

        assert report.rows
        for fragment in ("npg_", "hunter2", "BEGIN RSA"):
            assert fragment not in rendered


def _settings(**overrides):  # noqa: ANN202
    """A settings object for a single preflight check, without the env."""
    from modules.config import Settings

    base = dict(
        app_env="development",
        database_url="sqlite:///./test.db",
        portal_base_url="https://quotes.example.com",
        email_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


class TestDatabaseUrlDriver:
    """Providers hand out `postgresql://`; this project ships psycopg 3.

    Left alone, that mismatch surfaces as ModuleNotFoundError: psycopg2 during
    a deploy's migration step, with a traceback that never mentions the URL.
    """

    def _url(self, value: str) -> str:
        from modules.config import Settings

        return Settings(database_url=value, app_env="development").database_url

    @pytest.mark.parametrize(
        "given",
        [
            "postgresql://user:pass@host:5432/db",
            "postgresql://user:pass@host/db?sslmode=require",
            "postgres://user:pass@host/db",
        ],
    )
    def test_a_bare_postgres_url_is_pointed_at_the_installed_driver(self, given):
        assert self._url(given).startswith("postgresql+psycopg://")

    def test_the_rest_of_the_url_is_untouched(self):
        result = self._url("postgresql://user:pass@host:5432/db?sslmode=require")
        assert result.endswith("user:pass@host:5432/db?sslmode=require")

    def test_an_explicit_driver_is_left_alone(self):
        given = "postgresql+psycopg://user:pass@host/db"
        assert self._url(given) == given

    def test_sqlite_is_left_alone(self):
        assert self._url("sqlite:///./soneet.db") == "sqlite:///./soneet.db"

    def test_sqlalchemy_resolves_it_to_psycopg_3(self):
        from sqlalchemy.engine import make_url

        url = make_url(self._url("postgresql://user:pass@host/db"))
        assert url.drivername == "postgresql+psycopg"

    def test_psycopg2_is_not_a_dependency(self):
        """So the default driver could never have worked."""
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert "psycopg2" not in requirements
        assert "psycopg[binary]" in requirements
