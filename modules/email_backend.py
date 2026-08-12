"""Sending an email, without the rest of the application knowing how.

Four shapes and one method. Everything above this file — the outbox, the
worker, the templates — deals in :class:`EmailMessage` and never learns whether
delivery is SMTP, an HTTP API, a log line or a list in memory. Swapping provider
is then a configuration change and a new class, not an edit to the queue.

Three backends ship:

``memory``
    Captures messages in a list and sends nothing. What the tests use.
``console``
    Logs a redacted one-line summary. **Structurally incapable of reaching the
    network** — it has no socket code at all — which is what makes it safe as
    the development default.
``smtp``
    The only one that talks to anything. Refuses to run without transport
    security, never disables certificate verification, and takes its host,
    credentials and sender from configuration alone: a message cannot nominate
    where it is sent from or through.

Delivery failure is split into two kinds, because they need opposite handling.
A greylisting relay or a dropped connection is *temporary* and the message
should be tried again; a malformed address or a rejected sender is *permanent*
and retrying it forever only fills the queue.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache

log = logging.getLogger(__name__)

#: Deliberately conservative. Not RFC 5322 in full — that grammar accepts
#: addresses no provider will take — but enough to reject the things that cause
#: header injection or a bounced send.
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

#: Anything that could start a new header line. Checked on every value that
#: reaches a header, because a newline in a display name is how an attacker adds
#: a Bcc to somebody else's message.
_HEADER_UNSAFE = re.compile(r"[\r\n\x00]")

MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 200_000
MAX_NAME_CHARS = 120


class EmailError(RuntimeError):
    """Base for everything in this module."""


class EmailConfigurationError(EmailError):
    """The backend cannot be built from the current configuration."""


class EmailDeliveryError(EmailError):
    """A send did not succeed.

    ``temporary`` decides whether the outbox tries again. ``code`` is a short,
    safe token for the failure category — never the provider's message, which
    can quote the recipient, the subject or the body back at us and would then
    be stored and displayed.
    """

    def __init__(self, message: str, *, temporary: bool, code: str = "unknown") -> None:
        super().__init__(message)
        self.temporary = temporary
        self.code = code


class InvalidRecipientError(EmailDeliveryError):
    """The address is not one we will attempt. Permanent by definition."""

    def __init__(self, message: str = "The recipient address is not valid.") -> None:
        super().__init__(message, temporary=False, code="invalid_recipient")


# --------------------------------------------------------------------------- #
# The message
# --------------------------------------------------------------------------- #

def _clean_header(value: str, *, limit: int, what: str) -> str:
    """Reject CR, LF and NUL outright rather than stripping them.

    Stripping would quietly send a message whose subject is not what the caller
    wrote. A value containing a newline is either a bug or an injection attempt,
    and neither should be papered over.
    """
    text = (value or "").strip()
    if _HEADER_UNSAFE.search(text):
        raise EmailDeliveryError(
            f"The {what} contains a line break and was refused.",
            temporary=False, code="header_injection",
        )
    return text[:limit]


def validate_address(address: str) -> str:
    """Return the address, or raise. Also the CRLF guard for envelope values."""
    candidate = (address or "").strip()
    if not candidate or _HEADER_UNSAFE.search(candidate):
        raise InvalidRecipientError()
    if len(candidate) > 254 or not _ADDRESS.match(candidate):
        raise InvalidRecipientError()
    return candidate


@dataclass(frozen=True)
class EmailMessage:
    """One message, already rendered. Validated at construction.

    Both bodies are required. A quotation invitation that arrives as HTML only
    is unreadable in a text client and looks like spam to several filters, and
    the plain-text part is where the secure link has to be usable by hand.

    There is no free-form header mapping on purpose. Arbitrary headers are how
    a template ends up able to set Bcc.
    """

    to_email: str
    subject: str
    html_body: str
    text_body: str
    to_name: str = ""
    reply_to: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "to_email", validate_address(self.to_email))
        object.__setattr__(
            self, "to_name",
            _clean_header(self.to_name, limit=MAX_NAME_CHARS, what="recipient name"),
        )
        object.__setattr__(
            self, "subject",
            _clean_header(self.subject, limit=MAX_SUBJECT_CHARS, what="subject"),
        )
        if self.reply_to:
            object.__setattr__(self, "reply_to", validate_address(self.reply_to))
        if not self.subject:
            raise EmailDeliveryError(
                "The message has no subject.", temporary=False, code="empty_subject",
            )
        if not self.html_body.strip() or not self.text_body.strip():
            raise EmailDeliveryError(
                "A message needs both an HTML and a plain-text body.",
                temporary=False, code="empty_body",
            )
        if len(self.html_body) > MAX_BODY_CHARS or len(self.text_body) > MAX_BODY_CHARS:
            raise EmailDeliveryError(
                "The message body is too large to send.",
                temporary=False, code="body_too_large",
            )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # No bodies. An invitation body contains the capability URL, and a repr
        # ends up in tracebacks and debugger output.
        return (
            f"EmailMessage(to={self.to_email!r}, subject={self.subject!r}, "
            f"html={len(self.html_body)}b, text={len(self.text_body)}b)"
        )


@dataclass(frozen=True)
class EmailSendResult:
    """What the provider said. ``message_id`` is stored when one is offered."""

    accepted: bool
    message_id: str = ""
    detail: str = ""


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class EmailBackend(ABC):
    """One method, so a new provider is one class.

    Implementations raise :class:`EmailDeliveryError` rather than returning a
    failed result: the caller must not be able to ignore a failure by forgetting
    to check a boolean.
    """

    name = "abstract"

    @abstractmethod
    def send(self, message: EmailMessage) -> EmailSendResult: ...

    def check_ready(self) -> None:
        """Raise if this backend cannot work. Called at worker startup."""
        return None


@dataclass
class MemoryBackend(EmailBackend):
    """Captures messages. Sends nothing, ever.

    Used by the tests and by template verification. Keeping the sent list on the
    instance rather than in a module global means two tests cannot see each
    other's messages.
    """

    name: str = "memory"
    sent: list[EmailMessage] = field(default_factory=list)
    #: Set to make the next send fail, for exercising the retry paths.
    fail_with: EmailDeliveryError | None = None

    def send(self, message: EmailMessage) -> EmailSendResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
        return EmailSendResult(
            accepted=True, message_id=f"memory-{len(self.sent)}", detail="captured",
        )

    def clear(self) -> None:
        self.sent.clear()
        self.fail_with = None


class ConsoleBackend(EmailBackend):
    """Logs that a message would have been sent. Cannot open a socket.

    The development default. Note what is logged and what is not: recipient and
    subject, because they are what a developer needs to see the flow working,
    and never a body — the invitation body contains the customer's capability
    URL, and a development log is not a place to put one.
    """

    name = "console"

    def send(self, message: EmailMessage) -> EmailSendResult:
        log.info(
            "[email:console] would send to %s | subject: %s | %d bytes html, "
            "%d bytes text (body withheld)",
            message.to_email, message.subject,
            len(message.html_body), len(message.text_body),
        )
        return EmailSendResult(accepted=True, message_id="", detail="console")


class SmtpBackend(EmailBackend):
    """Real delivery over SMTP.

    Configuration comes from settings only. A message cannot choose its host,
    its port, its credentials or its From address — otherwise a template bug
    becomes an open relay.

    Certificate verification is on and there is no switch to turn it off. If a
    server's certificate does not validate, that is a server to fix, not a check
    to disable — and the credentials plus the customer's link are what travel
    over the connection.
    """

    name = "smtp"

    def __init__(
        self, host: str, port: int, *, username: str = "", password: str = "",
        security: str = "starttls", timeout: int = 20,
        from_address: str = "", from_name: str = "",
    ) -> None:
        if not host:
            raise EmailConfigurationError("SMTP_HOST is not configured.")
        if security not in {"starttls", "tls"}:
            raise EmailConfigurationError(
                "SMTP_SECURITY must be 'starttls' or 'tls'. There is no "
                "unencrypted mode: the session carries both the credentials "
                "and the customer's quotation link."
            )
        self.host = host
        self.port = int(port)
        self.username = username
        self._password = password
        self.security = security
        self.timeout = int(timeout)
        self.from_address = validate_address(from_address)
        self.from_name = _clean_header(
            from_name, limit=MAX_NAME_CHARS, what="sender name"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SmtpBackend(host={self.host!r}, port={self.port}, "
            f"security={self.security!r}, user={'set' if self.username else 'none'})"
        )

    def check_ready(self) -> None:
        if not self.from_address:
            raise EmailConfigurationError("EMAIL_FROM_ADDRESS is not configured.")

    def _build(self, message: EmailMessage):  # noqa: ANN202
        from email.message import EmailMessage as MimeMessage
        from email.utils import formataddr, make_msgid

        mime = MimeMessage()
        mime["Subject"] = message.subject
        mime["From"] = formataddr((self.from_name or None, self.from_address))
        mime["To"] = formataddr((message.to_name or None, message.to_email))
        if message.reply_to:
            mime["Reply-To"] = message.reply_to
        # A Message-ID we generate, so a retry that reaches the provider twice
        # is recognisable as the same message rather than looking like two.
        mime["Message-ID"] = make_msgid(domain=self.from_address.split("@")[-1])
        # Signals to well-behaved clients that this is transactional, not bulk.
        mime["Auto-Submitted"] = "auto-generated"

        mime.set_content(message.text_body)
        mime.add_alternative(message.html_body, subtype="html")
        return mime

    def send(self, message: EmailMessage) -> EmailSendResult:
        import smtplib
        import ssl

        mime = self._build(message)
        # Default context: verifies the certificate chain and the hostname.
        # Never replaced with an unverified one.
        context = ssl.create_default_context()

        try:
            if self.security == "tls":
                server = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout, context=context,
                )
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with server:
                if self.security == "starttls":
                    server.starttls(context=context)
                    # Re-greet: capabilities advertised before the upgrade are
                    # not trustworthy and may not include AUTH.
                    server.ehlo()
                if self.username:
                    server.login(self.username, self._password)
                server.send_message(mime)
        except smtplib.SMTPAuthenticationError as exc:
            # Permanent: a wrong password will still be wrong in ten minutes,
            # and repeated failures get an account locked.
            raise EmailDeliveryError(
                "The mail server rejected the credentials.",
                temporary=False, code="auth_failed",
            ) from _stripped(exc)
        except smtplib.SMTPRecipientsRefused:
            raise EmailDeliveryError(
                "The mail server refused the recipient address.",
                temporary=False, code="recipient_refused",
            ) from None
        except smtplib.SMTPSenderRefused:
            raise EmailDeliveryError(
                "The mail server refused the sender address.",
                temporary=False, code="sender_refused",
            ) from None
        except smtplib.SMTPResponseException as exc:
            # 4xx is "come back later", 5xx is "no". The distinction is the
            # whole reason the outbox has two failure categories.
            temporary = 400 <= int(exc.smtp_code) < 500
            raise EmailDeliveryError(
                "The mail server rejected the message.",
                temporary=temporary,
                code=f"smtp_{int(exc.smtp_code)}",
            ) from None
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            raise EmailDeliveryError(
                "The mail server could not be reached.",
                temporary=True, code="connection_failed",
            ) from _stripped(exc)

        return EmailSendResult(
            accepted=True, message_id=str(mime["Message-ID"]), detail="smtp",
        )


def _stripped(exc: BaseException) -> None:
    """Drop the original exception from the chain.

    ``raise ... from exc`` would attach a traceback whose frames hold the
    password passed to ``login`` and the message passed to ``send_message``.
    Returning None makes the chain end here, deliberately.
    """
    return None


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def get_backend() -> EmailBackend:
    """The configured backend, built once.

    Note the production guard: reaching the internet requires *both* an
    explicit ``smtp`` backend and ``email_enabled``. Neither alone is enough, so
    a half-finished configuration cannot start mailing customers.
    """
    from modules.config import get_settings

    settings = get_settings()

    if settings.email_backend == "memory":
        return MemoryBackend()
    if settings.email_backend == "console":
        return ConsoleBackend()

    return SmtpBackend(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        security=settings.smtp_security,
        timeout=settings.smtp_timeout_seconds,
        from_address=settings.email_from_address,
        from_name=settings.email_from_name,
    )


def reset_backend_cache() -> None:
    """Used by tests, which swap backends between cases."""
    get_backend.cache_clear()
