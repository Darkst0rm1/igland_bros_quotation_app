"""Authenticated encryption for the one secret the database has to carry.

Quotation links are capability URLs: holding one *is* the authorisation. The
portal therefore stores only a SHA-256 hash of the token, so a database
disclosure hands out no working links — that property is the foundation of the
whole portal design and nothing here may weaken it.

But an invitation email has to contain the link, and the email is queued now and
sent minutes later by a different process. Something has to carry the plaintext
across that gap.

**Why encryption rather than the alternatives.** Storing the token in the outbox
would undo the hash-only model outright. Deriving it deterministically would
make every link forgeable from the quotation id. Rendering the email body at
issue time and storing that just moves the plaintext into a bigger column.
Encrypting with a key held only in the environment keeps the original property
intact: a database dump on its own still yields nothing, because the key is not
in the database.

AES-256-GCM, so the ciphertext is authenticated as well as confidential. The
associated data binds each payload to the quotation, the revision, the recipient
and the purpose it was made for, which means a ciphertext lifted from one row
cannot be replayed into another — the tag check fails before anything decrypts.

Keys live in configuration, are versioned, and the version travels with the
ciphertext, so a rotation can decrypt rows queued under the previous key while
encrypting new ones under the current key.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache

log = logging.getLogger(__name__)

#: AES-256. Keys are supplied base64-encoded and must decode to exactly this.
KEY_BYTES = 32
#: 96-bit nonce, the size AES-GCM is specified for.
NONCE_BYTES = 12

#: What a stored payload looks like: ``sb1.<version>.<b64 nonce>.<b64 ct+tag>``.
#: The prefix is a format marker, so a future scheme can be told apart from this
#: one without guessing.
FORMAT_PREFIX = "sb1"
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,16}$")

#: Refuse anything larger. A capability URL is a few hundred bytes; a megabyte
#: arriving here means something is wrong upstream.
MAX_PLAINTEXT_BYTES = 4096


class SecretBoxError(RuntimeError):
    """Encryption or decryption failed.

    The message is deliberately vague and carries no ciphertext, no key
    material and no plaintext. Anything more would end up in a log line or an
    exception report, which is precisely where this data must not appear.
    """


class KeyringError(SecretBoxError):
    """The configured keys are missing or malformed."""


@dataclass(frozen=True)
class Keyring:
    """The keys this process may use, and which one it encrypts with.

    ``keys`` maps version label to raw key bytes. Decryption may use any of
    them; encryption always uses ``active``. That asymmetry is the whole of key
    rotation: add the new key, point ``active`` at it, and rows queued under the
    old one still open.
    """

    keys: dict[str, bytes]
    active: str

    def __post_init__(self) -> None:
        if self.active not in self.keys:
            raise KeyringError(
                "The active encryption key version is not among the configured keys."
            )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Never the key bytes. A repr lands in tracebacks, debuggers and logs.
        return f"Keyring(versions={sorted(self.keys)!r}, active={self.active!r})"

    def key_for(self, version: str) -> bytes:
        try:
            return self.keys[version]
        except KeyError:
            raise KeyringError(
                "No key is configured for the version this payload was written "
                "under. Restore the previous key to read it."
            ) from None


def _decode_key(raw: str, version: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(_pad(raw.strip()))
    except Exception:  # noqa: BLE001 — any decode failure is the same refusal
        raise KeyringError(
            f"The encryption key for version {version!r} is not valid base64."
        ) from None
    if len(key) != KEY_BYTES:
        raise KeyringError(
            f"The encryption key for version {version!r} must decode to "
            f"{KEY_BYTES} bytes; it decoded to {len(key)}."
        )
    return key


def _pad(value: str) -> str:
    """base64 without padding is common in env vars; add it back."""
    return value + "=" * (-len(value) % 4)


def parse_keyring(spec: str, active: str) -> Keyring:
    """Build a keyring from ``version:base64key`` pairs.

    Separated from configuration so it can be tested directly and so a bad
    value produces one clear message rather than a decode error deep inside a
    send.
    """
    keys: dict[str, bytes] = {}
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        version, separator, material = chunk.partition(":")
        version = version.strip()
        if not separator or not _VERSION_PATTERN.match(version):
            raise KeyringError(
                "Each encryption key must be written as 'version:base64key', "
                "where the version is short and alphanumeric."
            )
        keys[version] = _decode_key(material, version)

    if not keys:
        raise KeyringError("No encryption keys are configured.")
    return Keyring(keys=keys, active=(active or "").strip() or next(iter(keys)))


@lru_cache(maxsize=1)
def get_keyring() -> Keyring:
    """The process-wide keyring, from configuration.

    In development a key is generated per process when none is configured, so
    nobody has to set one up to run the app locally — and so a developer key can
    never accidentally be the production key, because it does not outlive the
    process. Production has no such fallback: :mod:`modules.config` refuses to
    start without a real key.
    """
    from modules.config import get_settings

    settings = get_settings()
    spec = settings.email_payload_keys.strip()

    if not spec:
        if settings.is_production:
            raise KeyringError(
                "EMAIL_PAYLOAD_KEYS is required in production."
            )
        ephemeral = base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode()
        log.warning(
            "No EMAIL_PAYLOAD_KEYS configured; using a per-process development "
            "key. Queued invitations will not survive a restart."
        )
        return Keyring(keys={"dev": _decode_key(ephemeral, "dev")}, active="dev")

    return parse_keyring(spec, settings.email_payload_key_version)


def reset_keyring_cache() -> None:
    """Used by tests, which rotate keys deliberately."""
    get_keyring.cache_clear()


def generate_key() -> str:
    """A fresh base64 key, for an operator setting one up. Never called at runtime."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode().rstrip("=")


# --------------------------------------------------------------------------- #
# Associated data
# --------------------------------------------------------------------------- #

def binding(
    *, quotation_id: int, revision_no: int, recipient: str, purpose: str
) -> bytes:
    """The associated data a payload is sealed against.

    Not encrypted — associated data is authenticated, not hidden — but any
    change to it makes the tag check fail. That is what stops a ciphertext being
    moved between rows: a payload sealed for one quotation, revision, recipient
    and purpose cannot be opened as another.

    The recipient is lower-cased and stripped so that a cosmetic difference in
    how an address was captured does not turn into a decryption failure.
    """
    parts = (
        str(int(quotation_id)),
        str(int(revision_no)),
        (recipient or "").strip().lower(),
        (purpose or "").strip().lower(),
    )
    return "|".join(parts).encode("utf-8")


# --------------------------------------------------------------------------- #
# Sealing and opening
# --------------------------------------------------------------------------- #

def seal(plaintext: str, *, aad: bytes, keyring: Keyring | None = None) -> str:
    """Encrypt ``plaintext``, returning a storable string.

    The result carries its own key version, so the row can be opened later even
    after a rotation, and carries a fresh random nonce per call — reusing a
    nonce under one key is the one mistake AES-GCM does not survive.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if plaintext is None:
        raise SecretBoxError("Nothing to encrypt.")
    data = plaintext.encode("utf-8")
    if len(data) > MAX_PLAINTEXT_BYTES:
        raise SecretBoxError("The value is too large to seal.")

    ring = keyring or get_keyring()
    nonce = os.urandom(NONCE_BYTES)
    try:
        ciphertext = AESGCM(ring.key_for(ring.active)).encrypt(nonce, data, aad)
    except Exception:  # noqa: BLE001
        # No detail: an encryption failure message is not worth leaking shape.
        raise SecretBoxError("The value could not be sealed.") from None

    return ".".join((
        FORMAT_PREFIX,
        ring.active,
        base64.urlsafe_b64encode(nonce).decode().rstrip("="),
        base64.urlsafe_b64encode(ciphertext).decode().rstrip("="),
    ))


def open_sealed(payload: str, *, aad: bytes, keyring: Keyring | None = None) -> str:
    """Decrypt a payload produced by :func:`seal`, or raise.

    Every failure — wrong key, wrong associated data, truncated ciphertext,
    flipped bit, unknown format — raises the same exception type with a message
    that describes none of them. A caller cannot use this as an oracle, and
    nothing quotable ends up in a log.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not payload:
        raise SecretBoxError("There is nothing to open.")

    parts = payload.split(".")
    if len(parts) != 4 or parts[0] != FORMAT_PREFIX:
        raise SecretBoxError("This value is not in a recognised sealed format.")

    _prefix, version, nonce_b64, ciphertext_b64 = parts
    ring = keyring or get_keyring()

    try:
        nonce = base64.urlsafe_b64decode(_pad(nonce_b64))
        ciphertext = base64.urlsafe_b64decode(_pad(ciphertext_b64))
    except Exception:  # noqa: BLE001
        raise SecretBoxError("This sealed value could not be read.") from None

    if len(nonce) != NONCE_BYTES:
        raise SecretBoxError("This sealed value could not be read.")

    key = ring.key_for(version)      # KeyringError names the version, not the key
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad).decode("utf-8")
    except Exception:  # noqa: BLE001 — InvalidTag and decode errors alike
        raise SecretBoxError(
            "This sealed value failed verification and was discarded."
        ) from None


def is_sealed(value: str | None) -> bool:
    """Whether a stored value looks like one of ours. Cheap, and does not decrypt."""
    return bool(value) and value.startswith(f"{FORMAT_PREFIX}.")


# --------------------------------------------------------------------------- #
# Do two processes hold the same key?
# --------------------------------------------------------------------------- #

#: Fixed input for the fingerprint. Constant so the same key always produces
#: the same identifier, and unrelated to any real payload.
_FINGERPRINT_LABEL = b"soneet:email-payload-key:v1"


def key_fingerprint(version: str | None = None, keyring: Keyring | None = None) -> str:
    """A short, non-reversible identifier for the key behind a version label.

    Exists because the version label is chosen by whoever writes the
    configuration, and two processes can easily hold *different key material
    under the same label* — a copy-paste that drops a character, a rotation
    applied to one host and not the other, a worker deployed from an older
    secrets file.

    That failure is silent and late: the app seals an invitation with its key,
    the worker cannot open it, and the message fails as ``link_unsealable``
    minutes later on a machine nobody is watching. The customer simply never
    receives their quotation.

    HMAC of a fixed label under the key, truncated. It identifies the key
    without revealing anything about it, so it is safe to store, log and print
    on a diagnostics screen.
    """
    import hashlib
    import hmac

    ring = keyring or get_keyring()
    label = version or ring.active
    digest = hmac.new(ring.key_for(label), _FINGERPRINT_LABEL, hashlib.sha256)
    return f"{label}:{digest.hexdigest()[:16]}"


class KeyAgreementError(SecretBoxError):
    """This process holds different key material from the rest of the deployment."""


#: Where the agreed fingerprint is recorded. A plain app setting rather than a
#: new column: it is one string, and adding a migration for it would be more
#: machinery than the fact deserves.
AGREEMENT_SETTING = "email_payload_key_fingerprint"


def verify_key_agreement(session, *, record: bool = True) -> str:  # noqa: ANN001
    """Check this process's key against the one the deployment already uses.

    First process to run records its fingerprint; every process afterwards
    compares. A mismatch raises here — at startup, on the machine that is wrong,
    naming what to fix — instead of surfacing as undelivered mail hours later.

    Returns the fingerprint. Never raises for a *missing* key in development,
    where an ephemeral per-process key is normal and comparing it would fail
    every restart.
    """
    from modules.config import get_settings
    from modules.models import AppSetting

    settings = get_settings()
    try:
        current = key_fingerprint()
    except KeyringError:
        if settings.is_production:
            raise
        return ""

    if not settings.email_payload_keys.strip():
        # A development key that dies with the process. Recording it would make
        # every restart look like a mismatch.
        return current

    row = session.query(AppSetting).filter_by(key=AGREEMENT_SETTING).one_or_none()

    if row is None:
        if record:
            session.add(AppSetting(
                key=AGREEMENT_SETTING,
                value_json=current,
                value_type="string",
                category="email",
                description=(
                    "Fingerprint of the encryption key this deployment seals "
                    "invitation links with. Not the key itself. Every process "
                    "must agree, or queued invitations cannot be opened and sent."
                ),
            ))
            session.flush()
        return current

    recorded = str(row.value_json or "")
    if recorded == current:
        return current

    recorded_version = recorded.split(":", 1)[0] if ":" in recorded else "?"
    current_version = current.split(":", 1)[0]

    if recorded_version == current_version:
        detail = (
            f"Both are labelled {current_version!r} but the key material differs. "
            "One of them is out of date — check that EMAIL_PAYLOAD_KEYS is "
            "identical on every host."
        )
    else:
        detail = (
            f"This process seals with {current_version!r}; the deployment "
            f"recorded {recorded_version!r}. If this is a deliberate rotation, "
            "keep both keys in EMAIL_PAYLOAD_KEYS everywhere and clear the "
            f"recorded fingerprint ({AGREEMENT_SETTING}) once every host is "
            "updated."
        )

    raise KeyAgreementError(
        "This process holds different email encryption keys from the rest of "
        f"the deployment. {detail} Queued invitations sealed elsewhere cannot "
        "be opened, so they would fail to send."
    )


def clear_key_agreement(session) -> None:  # noqa: ANN001
    """Forget the recorded fingerprint, so the next process records a new one.

    The deliberate step in a rotation: once every host carries the new key,
    clearing this lets the deployment settle on it.
    """
    from modules.models import AppSetting

    row = session.query(AppSetting).filter_by(key=AGREEMENT_SETTING).one_or_none()
    if row is not None:
        session.delete(row)
        session.flush()
