"""The parts of email delivery that fail dangerously rather than loudly.

Three claims, in order of what getting them wrong would cost:

* a capability URL never exists in the database in a form anybody could use,
  and never reaches a log, a repr or an exception message;
* nothing a customer or an employee typed can escape from text into markup, a
  header, or a template path;
* a message that must not carry a link cannot be given one.
"""
from __future__ import annotations

import base64
import datetime as dt
import logging

import pytest

from modules import email_backend, email_templates, secret_box
from modules.constants import (
    INTERNAL_MESSAGES,
    LINK_BEARING_MESSAGES,
    EmailMessageType,
)

SECRET_URL = "https://quotes.test.invalid/quote/public/TOK3N-that-must-never-leak"


def _keyring(active: str = "k1", versions: tuple[str, ...] = ("k1", "k2")):
    spec = ",".join(f"{v}:{secret_box.generate_key()}" for v in versions)
    return secret_box.parse_keyring(spec, active)


def _aad(quotation_id: int = 7, revision_no: int = 2, recipient: str = "dana@x.invalid"):
    return secret_box.binding(
        quotation_id=quotation_id, revision_no=revision_no,
        recipient=recipient, purpose="email-invitation",
    )


# --------------------------------------------------------------------------- #
# Authenticated encryption
# --------------------------------------------------------------------------- #

class TestSealing:
    def test_a_sealed_value_round_trips(self):
        ring = _keyring()
        sealed = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)
        assert secret_box.open_sealed(sealed, aad=_aad(), keyring=ring) == SECRET_URL

    def test_the_ciphertext_does_not_contain_the_plaintext(self):
        ring = _keyring()
        sealed = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)

        assert "TOK3N" not in sealed
        assert "quotes.test.invalid" not in sealed
        assert SECRET_URL not in sealed

    def test_each_sealing_uses_a_fresh_nonce(self):
        """Nonce reuse under one key is the mistake AES-GCM does not survive."""
        ring = _keyring()
        seals = {secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring) for _ in range(20)}
        assert len(seals) == 20
        nonces = {s.split(".")[2] for s in seals}
        assert len(nonces) == 20

    def test_the_key_version_travels_with_the_payload(self):
        ring = _keyring(active="k2")
        sealed = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)
        assert sealed.split(".")[1] == "k2"
        assert sealed.startswith(f"{secret_box.FORMAT_PREFIX}.")

    def test_a_flipped_bit_is_rejected(self):
        ring = _keyring()
        sealed = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)
        prefix, version, nonce, ciphertext = sealed.split(".")
        raw = bytearray(base64.urlsafe_b64decode(ciphertext + "=" * (-len(ciphertext) % 4)))
        raw[0] ^= 0x01
        tampered = ".".join([
            prefix, version, nonce,
            base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("="),
        ])
        with pytest.raises(secret_box.SecretBoxError):
            secret_box.open_sealed(tampered, aad=_aad(), keyring=ring)

    def test_a_truncated_payload_is_rejected(self):
        ring = _keyring()
        sealed = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)
        for broken in (sealed[:-6], sealed.split(".", 1)[1], "", "sb1.k1.x", "garbage"):
            with pytest.raises(secret_box.SecretBoxError):
                secret_box.open_sealed(broken, aad=_aad(), keyring=ring)


class TestAssociatedDataBinding:
    """A payload sealed for one row must not open as another."""

    @pytest.mark.parametrize(
        "changed",
        [
            {"quotation_id": 8},
            {"revision_no": 3},
            {"recipient": "someone.else@x.invalid"},
        ],
    )
    def test_moving_a_payload_between_rows_fails(self, changed):
        ring = _keyring()
        sealed = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)
        with pytest.raises(secret_box.SecretBoxError):
            secret_box.open_sealed(sealed, aad=_aad(**changed), keyring=ring)

    def test_a_different_purpose_fails(self):
        ring = _keyring()
        sealed = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)
        other = secret_box.binding(
            quotation_id=7, revision_no=2, recipient="dana@x.invalid",
            purpose="something-else",
        )
        with pytest.raises(secret_box.SecretBoxError):
            secret_box.open_sealed(sealed, aad=other, keyring=ring)

    def test_recipient_case_and_spacing_do_not_matter(self):
        """A cosmetic difference in capture must not become a decrypt failure."""
        ring = _keyring()
        sealed = secret_box.seal(
            SECRET_URL, aad=_aad(recipient="  Dana@X.INVALID "), keyring=ring
        )
        assert secret_box.open_sealed(
            sealed, aad=_aad(recipient="dana@x.invalid"), keyring=ring
        ) == SECRET_URL


class TestKeyRotation:
    def test_a_row_sealed_under_the_previous_key_still_opens(self):
        ring = _keyring(active="k1")
        old = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)

        rotated = secret_box.Keyring(keys=ring.keys, active="k2")
        assert secret_box.open_sealed(old, aad=_aad(), keyring=rotated) == SECRET_URL
        # New rows use the new key.
        assert secret_box.seal("x", aad=_aad(), keyring=rotated).split(".")[1] == "k2"

    def test_removing_a_key_makes_its_rows_unreadable_and_says_so(self):
        ring = _keyring(active="k1")
        old = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)

        without = secret_box.Keyring(keys={"k2": ring.keys["k2"]}, active="k2")
        with pytest.raises(secret_box.KeyringError) as caught:
            secret_box.open_sealed(old, aad=_aad(), keyring=without)
        assert "version" in str(caught.value)

    def test_an_active_version_with_no_key_is_refused(self):
        with pytest.raises(secret_box.KeyringError):
            secret_box.parse_keyring(f"k1:{secret_box.generate_key()}", "k9")

    @pytest.mark.parametrize(
        "spec",
        ["", "notakey", "k1:", "k1:short", "k1:!!!not-base64!!!", ":abc"],
    )
    def test_a_malformed_keyring_is_refused(self, spec):
        with pytest.raises(secret_box.KeyringError):
            secret_box.parse_keyring(spec, "k1")


class TestNothingLeaks:
    def test_the_keyring_repr_hides_key_material(self):
        ring = _keyring()
        text = repr(ring)
        assert "k1" in text and "active" in text
        for key in ring.keys.values():
            assert base64.urlsafe_b64encode(key).decode().rstrip("=") not in text

    def test_failure_messages_quote_nothing(self):
        ring = _keyring()
        sealed = secret_box.seal(SECRET_URL, aad=_aad(), keyring=ring)
        with pytest.raises(secret_box.SecretBoxError) as caught:
            secret_box.open_sealed(sealed, aad=_aad(quotation_id=99), keyring=ring)

        message = str(caught.value)
        assert SECRET_URL not in message
        assert "TOK3N" not in message
        assert sealed not in message
        # And the chain ends here, so no frame holds the plaintext.
        assert caught.value.__cause__ is None

    def test_a_sealing_failure_does_not_echo_the_plaintext(self):
        ring = _keyring()
        with pytest.raises(secret_box.SecretBoxError) as caught:
            secret_box.seal("x" * (secret_box.MAX_PLAINTEXT_BYTES + 1), aad=_aad(),
                            keyring=ring)
        assert "xxxx" not in str(caught.value)

    def test_the_portal_log_filter_scrubs_a_token_from_a_worker_log(self, caplog):
        """The worker installs the portal's redaction filter."""
        from portal.security import install_log_redaction

        install_log_redaction()
        logger = logging.getLogger("worker.leaktest")
        with caplog.at_level(logging.INFO):
            logger.info("about to send %s", SECRET_URL)

        combined = "\n".join(record.getMessage() for record in caplog.records)
        assert "TOK3N-that-must-never-leak" not in combined
        assert "[redacted]" in combined


# --------------------------------------------------------------------------- #
# Message construction
# --------------------------------------------------------------------------- #

class TestRecipientValidation:
    @pytest.mark.parametrize(
        "address",
        [
            "", "   ", "no-at-sign", "@nodomain.invalid", "user@", "user@nodot",
            "user@@double.invalid", "a" * 250 + "@x.invalid",
        ],
    )
    def test_bad_addresses_are_refused(self, address):
        with pytest.raises(email_backend.InvalidRecipientError):
            email_backend.validate_address(address)

    @pytest.mark.parametrize(
        "address",
        [
            "dana@harbour.invalid",
            "dana.whitfield+quotes@harbour.co.invalid",
            "d-w_1%x@sub.harbour.invalid",
        ],
    )
    def test_ordinary_addresses_are_accepted(self, address):
        assert email_backend.validate_address(address) == address


class TestHeaderInjection:
    @pytest.mark.parametrize(
        "payload",
        [
            "dana@x.invalid\r\nBcc: attacker@evil.invalid",
            "dana@x.invalid\nBcc: attacker@evil.invalid",
            "dana@x.invalid\x00",
        ],
    )
    def test_crlf_in_a_recipient_is_refused(self, payload):
        with pytest.raises(email_backend.InvalidRecipientError):
            email_backend.validate_address(payload)

    @pytest.mark.parametrize(
        "subject",
        [
            "Quotation\r\nBcc: attacker@evil.invalid",
            "Quotation\nX-Injected: yes",
            "Quotation\x00truncated",
        ],
    )
    def test_crlf_in_a_subject_is_refused(self, subject):
        with pytest.raises(email_backend.EmailDeliveryError) as caught:
            email_backend.EmailMessage(
                to_email="dana@x.invalid", subject=subject,
                html_body="<p>x</p>", text_body="x",
            )
        assert caught.value.code == "header_injection"
        assert not caught.value.temporary

    def test_crlf_in_a_display_name_is_refused(self):
        with pytest.raises(email_backend.EmailDeliveryError):
            email_backend.EmailMessage(
                to_email="dana@x.invalid", subject="Quotation",
                to_name="Dana\r\nBcc: evil@x.invalid",
                html_body="<p>x</p>", text_body="x",
            )

    def test_a_refused_header_is_not_silently_stripped(self):
        """Stripping would send a message whose subject is not what was written."""
        with pytest.raises(email_backend.EmailDeliveryError):
            email_backend.EmailMessage(
                to_email="dana@x.invalid", subject="Line one\r\nLine two",
                html_body="<p>x</p>", text_body="x",
            )


class TestMessageLimits:
    def test_both_bodies_are_required(self):
        for html, text in (("<p>x</p>", ""), ("", "x"), ("  ", "  ")):
            with pytest.raises(email_backend.EmailDeliveryError) as caught:
                email_backend.EmailMessage(
                    to_email="d@x.invalid", subject="s",
                    html_body=html, text_body=text,
                )
            assert caught.value.code in {"empty_body", "empty_subject"}

    def test_an_oversized_body_is_refused(self):
        with pytest.raises(email_backend.EmailDeliveryError) as caught:
            email_backend.EmailMessage(
                to_email="d@x.invalid", subject="s",
                html_body="x" * (email_backend.MAX_BODY_CHARS + 1), text_body="x",
            )
        assert caught.value.code == "body_too_large"

    def test_a_long_subject_is_truncated_not_refused(self):
        message = email_backend.EmailMessage(
            to_email="d@x.invalid", subject="Q" * 500,
            html_body="<p>x</p>", text_body="x",
        )
        assert len(message.subject) == email_backend.MAX_SUBJECT_CHARS

    def test_the_message_repr_withholds_the_bodies(self):
        message = email_backend.EmailMessage(
            to_email="d@x.invalid", subject="Quotation",
            html_body=f"<a href='{SECRET_URL}'>link</a>", text_body=SECRET_URL,
        )
        assert SECRET_URL not in repr(message)
        assert "TOK3N" not in repr(message)


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class TestBackendSafety:
    def test_the_memory_backend_captures_and_sends_nothing(self):
        backend = email_backend.MemoryBackend()
        message = email_backend.EmailMessage(
            to_email="d@x.invalid", subject="s", html_body="<p>x</p>", text_body="x",
        )
        result = backend.send(message)
        assert result.accepted
        assert backend.sent == [message]

    def test_the_test_and_development_backends_cannot_reach_the_network(self):
        """Structural: neither has any code that could reach it.

        Parsed rather than grepped. ConsoleBackend's docstring says the word
        "socket" — because it explains that it has none — and a substring
        search would call that a violation.
        """
        import ast
        import inspect
        import textwrap

        for backend_class in (email_backend.MemoryBackend, email_backend.ConsoleBackend):
            tree = ast.parse(textwrap.dedent(inspect.getsource(backend_class)))
            imported = {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            forbidden = imported & {
                "socket", "smtplib", "urllib", "requests", "httpx", "http",
                "ssl", "asyncio",
            }
            assert not forbidden, f"{backend_class.__name__} imports {forbidden}"

    def test_no_network_call_escapes_during_a_capture_send(self, monkeypatch):
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("a backend attempted a network connection")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)

        for backend in (email_backend.MemoryBackend(), email_backend.ConsoleBackend()):
            backend.send(email_backend.EmailMessage(
                to_email="d@x.invalid", subject="s",
                html_body="<p>x</p>", text_body="x",
            ))

    def test_the_console_backend_never_logs_a_body(self, caplog):
        backend = email_backend.ConsoleBackend()
        with caplog.at_level(logging.INFO):
            backend.send(email_backend.EmailMessage(
                to_email="d@x.invalid", subject="Quotation",
                html_body=f"<a href='{SECRET_URL}'>x</a>", text_body=SECRET_URL,
            ))
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert "TOK3N" not in combined
        assert "withheld" in combined

    def test_smtp_refuses_an_unencrypted_mode(self):
        with pytest.raises(email_backend.EmailConfigurationError):
            email_backend.SmtpBackend(
                "mail.x.invalid", 25, security="none", from_address="a@x.invalid",
            )

    def test_smtp_refuses_a_missing_host(self):
        with pytest.raises(email_backend.EmailConfigurationError):
            email_backend.SmtpBackend("", 587, from_address="a@x.invalid")

    def test_smtp_refuses_an_invalid_sender(self):
        with pytest.raises(email_backend.InvalidRecipientError):
            email_backend.SmtpBackend(
                "mail.x.invalid", 587, from_address="not-an-address",
            )

    def test_smtp_never_disables_certificate_verification(self):
        """There must be no route to an unverified context."""
        import inspect

        source = inspect.getsource(email_backend.SmtpBackend)
        for forbidden in (
            "_create_unverified_context", "CERT_NONE", "check_hostname = False",
            "verify=False",
        ):
            assert forbidden not in source

    def test_the_smtp_repr_hides_the_password(self):
        backend = email_backend.SmtpBackend(
            "mail.x.invalid", 587, username="user", password="hunter2",
            from_address="a@x.invalid",
        )
        assert "hunter2" not in repr(backend)

    def test_a_message_cannot_choose_its_own_sender_or_host(self):
        """Envelope values come from configuration, never from the message."""
        import inspect

        fields = set(email_backend.EmailMessage.__dataclass_fields__)
        assert not fields & {"from_email", "from_address", "smtp_host", "host", "bcc", "cc"}

        built = inspect.getsource(email_backend.SmtpBackend._build)
        assert "self.from_address" in built
        assert "message.from" not in built

    def test_a_send_timeout_is_bounded(self):
        backend = email_backend.SmtpBackend(
            "mail.x.invalid", 587, timeout=7, from_address="a@x.invalid",
        )
        assert backend.timeout == 7

    def test_a_provider_timeout_becomes_a_temporary_failure(self, monkeypatch):
        import smtplib

        backend = email_backend.SmtpBackend(
            "mail.x.invalid", 587, from_address="a@x.invalid",
        )

        def timeout(*_args, **_kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr(smtplib, "SMTP", timeout)
        with pytest.raises(email_backend.EmailDeliveryError) as caught:
            backend.send(email_backend.EmailMessage(
                to_email="d@x.invalid", subject="s",
                html_body="<p>x</p>", text_body="x",
            ))
        assert caught.value.temporary
        assert caught.value.code == "connection_failed"
        # No chained traceback: its frames hold the password and the body.
        assert caught.value.__cause__ is None

    @pytest.mark.parametrize(
        ("code", "temporary"), [(451, True), (421, True), (550, False), (554, False)],
    )
    def test_smtp_response_codes_are_classified(self, monkeypatch, code, temporary):
        import smtplib

        backend = email_backend.SmtpBackend(
            "mail.x.invalid", 587, from_address="a@x.invalid",
        )

        class FakeServer:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def starttls(self, **_kwargs): return None
            def ehlo(self, *_args): return None
            def login(self, *_args): return None
            def send_message(self, _mime):
                raise smtplib.SMTPResponseException(code, b"nope")

        monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: FakeServer())
        with pytest.raises(email_backend.EmailDeliveryError) as caught:
            backend.send(email_backend.EmailMessage(
                to_email="d@x.invalid", subject="s",
                html_body="<p>x</p>", text_body="x",
            ))
        assert caught.value.temporary is temporary
        assert caught.value.code == f"smtp_{code}"


# --------------------------------------------------------------------------- #
# Production configuration
# --------------------------------------------------------------------------- #

class TestProductionFailsClosed:
    def _settings(self, **overrides):
        from modules.config import Settings

        base = dict(
            app_env="production",
            secret_key="x" * 40,
            database_url="postgresql://u:p@host/db",
            storage_backend="s3",
            storage_bucket="b",
            storage_access_key_id="k",
            storage_secret_access_key="s",
            portal_base_url="https://quotes.example",
        )
        base.update(overrides)
        return Settings(**base)

    def test_production_without_email_enabled_is_fine(self):
        """Not having turned sending on yet is a state, not a fault."""
        settings = self._settings(email_enabled=False)
        assert settings.is_production
        assert not settings.email_enabled

    def test_enabling_email_without_a_sender_refuses_to_start(self):
        with pytest.raises(ValueError) as caught:
            self._settings(
                email_enabled=True, email_backend="smtp", smtp_host="mail.x",
                email_payload_keys="k1:" + secret_box.generate_key(),
                email_from_address="",
            )
        assert "EMAIL_FROM_ADDRESS" in str(caught.value)

    def test_enabling_email_without_encryption_keys_refuses_to_start(self):
        with pytest.raises(ValueError) as caught:
            self._settings(
                email_enabled=True, email_backend="smtp", smtp_host="mail.x",
                email_from_address="a@x.invalid", email_payload_keys="",
            )
        assert "EMAIL_PAYLOAD_KEYS" in str(caught.value)

    def test_enabling_email_without_an_smtp_host_refuses_to_start(self):
        with pytest.raises(ValueError) as caught:
            self._settings(
                email_enabled=True, email_backend="smtp", smtp_host="",
                email_from_address="a@x.invalid",
                email_payload_keys="k1:" + secret_box.generate_key(),
            )
        assert "SMTP_HOST" in str(caught.value)

    def test_a_capture_backend_in_production_refuses_to_start(self):
        """'Enabled' plus a backend that delivers nothing is silent data loss."""
        with pytest.raises(ValueError) as caught:
            self._settings(
                email_enabled=True, email_backend="console",
                email_from_address="a@x.invalid",
                email_payload_keys="k1:" + secret_box.generate_key(),
            )
        assert "EMAIL_BACKEND" in str(caught.value)

    def test_a_valid_production_email_configuration_starts(self):
        settings = self._settings(
            email_enabled=True, email_backend="smtp", smtp_host="mail.x.invalid",
            email_from_address="quotes@x.invalid",
            email_payload_keys="k1:" + secret_box.generate_key(),
        )
        assert settings.email_enabled

    def test_credentials_and_keys_are_redacted_from_diagnostics(self):
        settings = self._settings(
            email_enabled=False,      # this test is about redaction, not the guard
            smtp_username="user", smtp_password="hunter2",
            email_payload_keys="k1:" + secret_box.generate_key(),
        )
        shown = settings.redacted()
        assert shown["smtp_password"] == "***"
        assert shown["smtp_username"] == "***"
        assert shown["email_payload_keys"] == "***"
        assert "hunter2" not in str(shown)


# --------------------------------------------------------------------------- #
# Template safety
# --------------------------------------------------------------------------- #

BRAND = email_templates.BrandSnapshot(
    name="Northwind Packaging", slogan="Corrugated, done properly",
    address_lines=("84 Kiln Road", "Hamilton Ontario L8P 1A1"),
    phone="+1 (905) 555 0142", email="sales@northwind.invalid",
    legal_footer="Confidential.", primary="#1f4e79",
)


def _data(**overrides) -> dict:
    base = {
        "customer_name": "Dana Whitfield",
        "quote_number": "QT-2026-0001",
        "revision_label": "Rev 0",
        "project_name": "Retail Shipper Program",
        "customer_company": "Harbour Foods Inc.",
        "total_label": "Quotation total",
        "total_display": "$19,662.00 USD",
        "valid_until_display": "10 Sep 2026",
        "has_optional_items": True,
        "sales_representative": "Lee Live",
        "preheader": "Your quotation is ready",
        "previous_revision_label": "Rev 0",
        "change_summary": "Reduced the pallet quantity.",
        "accepted_at_display": "12 Aug 2026",
        "selected_items": ["Two-colour print"],
        "deposit_display": "$4,915.50 USD",
        "comment": "Please reduce the quantity.",
        "rows": [("Customer", "Harbour Foods Inc."), ("Quotation", "QT-2026-0001")],
    }
    base.update(overrides)
    return base


def _render(message_type: EmailMessageType, **overrides):
    needs_link = message_type in LINK_BEARING_MESSAGES
    return email_templates.render(
        message_type,
        data=_data(**overrides),
        brand=BRAND,
        recipient_email="dana@harbour.invalid",
        recipient_name="Dana Whitfield",
        secure_url=SECRET_URL if needs_link else "",
    )


class TestTemplateEscaping:
    @pytest.mark.parametrize("message_type", list(EmailMessageType))
    def test_markup_in_data_is_escaped_in_html(self, message_type):
        message = _render(
            message_type,
            customer_name='<script>alert("xss")</script>',
            customer_company='Harbour <img src=x onerror=alert(1)>',
            comment='<b>bold</b> & "quoted"',
            project_name="A & B",
        )
        # What matters is that none of it is *markup*. The words survive as
        # text, escaped, which is exactly right: a customer legitimately called
        # "A & B <Holdings>" should see their own name back.
        assert "<script>" not in message.html_body
        assert "<img" not in message.html_body
        assert "&lt;" in message.html_body

    @pytest.mark.parametrize("message_type", list(EmailMessageType))
    def test_a_rendered_message_is_still_a_valid_message(self, message_type):
        """Rendering cannot produce something the header guards would refuse."""
        message = _render(message_type, customer_name="Dana Whitfield")
        assert message.subject
        assert "\r" not in message.subject and "\n" not in message.subject

    def test_an_ampersand_survives_readably_in_plain_text(self):
        """Text bodies are not HTML-escaped: 'A &amp; B' would be shown literally."""
        message = _render(EmailMessageType.QUOTE_INVITATION, project_name="A & B")
        assert "A & B" in message.text_body
        assert "&amp;" not in message.text_body

    def test_a_missing_value_fails_loudly_rather_than_rendering_blank(self):
        with pytest.raises(email_templates.TemplateError):
            email_templates.render(
                EmailMessageType.QUOTE_INVITATION,
                data={"quote_number": "QT-1"},     # everything else absent
                brand=BRAND, recipient_email="d@x.invalid", secure_url=SECRET_URL,
            )

    def test_template_data_cannot_reach_a_dict_method(self):
        """``data.items`` must not silently render a bound method."""
        readonly = email_templates._Readonly({"a": 1})
        assert readonly.a == 1
        with pytest.raises(AttributeError):
            readonly.items


class TestTemplateSelection:
    def test_every_message_type_has_a_registered_template(self):
        assert set(email_templates.TEMPLATES) == set(EmailMessageType)

    def test_the_registry_is_keyed_by_enum_not_by_string(self):
        assert all(
            isinstance(key, EmailMessageType) for key in email_templates.TEMPLATES
        )

    def test_an_unregistered_type_raises_rather_than_guessing(self):
        with pytest.raises(email_templates.TemplateError):
            email_templates.render(
                "../../etc/passwd",  # type: ignore[arg-type]
                data=_data(), brand=BRAND, recipient_email="d@x.invalid",
            )

    def test_no_template_name_is_built_from_data(self):
        import inspect

        source = inspect.getsource(email_templates)
        # Template names come from the closed registry only.
        assert 'get_template(html_name)' in source
        assert 'get_template(f"' not in source

    def test_the_loader_cannot_climb_out_of_the_template_directory(self):
        from jinja2 import TemplateNotFound

        env = email_templates.get_environment()
        for escape in ("../secrets.txt", "/etc/passwd", "..\\..\\x.html"):
            with pytest.raises((TemplateNotFound, Exception)):
                env.get_template(escape)


class TestLinkContainment:
    @pytest.mark.parametrize("message_type", sorted(LINK_BEARING_MESSAGES))
    def test_an_invitation_carries_the_link_in_both_bodies(self, message_type):
        message = _render(message_type)
        assert SECRET_URL in message.html_body
        assert SECRET_URL in message.text_body

    @pytest.mark.parametrize(
        "message_type",
        sorted(set(EmailMessageType) - LINK_BEARING_MESSAGES),
    )
    def test_every_other_message_refuses_a_link(self, message_type):
        with pytest.raises(email_templates.TemplateError) as caught:
            email_templates.render(
                message_type, data=_data(), brand=BRAND,
                recipient_email="d@x.invalid", secure_url=SECRET_URL,
            )
        assert "never carry" in str(caught.value)

    @pytest.mark.parametrize("message_type", sorted(INTERNAL_MESSAGES))
    def test_internal_messages_contain_no_customer_link(self, message_type):
        message = _render(message_type)
        assert SECRET_URL not in message.html_body
        assert SECRET_URL not in message.text_body
        assert "quote/public" not in message.html_body
        assert "quote/public" not in message.text_body

    @pytest.mark.parametrize("message_type", sorted(LINK_BEARING_MESSAGES))
    def test_an_invitation_without_a_link_is_refused(self, message_type):
        with pytest.raises(email_templates.TemplateError):
            email_templates.render(
                message_type, data=_data(), brand=BRAND,
                recipient_email="d@x.invalid", secure_url="",
            )


class TestNoRemoteResources:
    @pytest.mark.parametrize("message_type", list(EmailMessageType))
    def test_no_message_fetches_anything(self, message_type):
        """No tracking pixel, no CDN, no hosted font, no remote logo."""
        message = _render(message_type)
        html = message.html_body

        assert "<img" not in html
        assert "<script" not in html
        assert "<iframe" not in html
        assert "@import" not in html
        assert "url(" not in html
        for marker in ("cdn.", "googleapis", "gstatic", "tracking", "pixel"):
            assert marker not in html.lower()

    @pytest.mark.parametrize("message_type", list(EmailMessageType))
    def test_the_only_external_links_are_ours(self, message_type):
        import re

        message = _render(message_type)
        urls = re.findall(r'https?://[^\s"\'<>]+', message.html_body)
        for url in urls:
            assert url.startswith(SECRET_URL[:32]) or "test.invalid" in url

    def test_a_missing_logo_changes_nothing(self):
        """Branding is text; there is no image to fail to load."""
        plain = email_templates.BrandSnapshot(name="Northwind")
        message = email_templates.render(
            EmailMessageType.QUOTE_INVITATION, data=_data(), brand=plain,
            recipient_email="d@x.invalid", secure_url=SECRET_URL,
        )
        assert "Northwind" in message.html_body
        assert "<img" not in message.html_body
