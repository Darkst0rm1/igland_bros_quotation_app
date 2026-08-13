"""Security posture for the public portal: headers, origin checks, rate limits, log hygiene.

The portal is the only part of this system an unauthenticated stranger can
reach, so it assumes hostility. Three things are enforced here that the service
layer cannot express:

* a page that loads nothing from anywhere else — no CDN, no font host, no
  analytics, no tracking pixel — so a customer's visit is not disclosed to a
  third party and there is no external script to compromise;
* state changes only from our own forms, checked twice: a signed one-time nonce
  and an Origin/Referer match;
* the access token never reaching a log file, which is the usual way a
  capability URL escapes.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

#: No external origins at all. 'self' only, and inline styles are not permitted
#: — the stylesheet is served from this service.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # same-origin, not no-referrer. The URL is the credential, so it must never
    # reach a third party — same-origin achieves that, because a cross-origin
    # request sends no Referer at all.
    #
    # no-referrer looked stricter and was worse: it also strips the header from
    # our *own* form posts, and Chromium suppresses Origin alongside it, so
    # origin_is_allowed() saw neither and refused every approval. The stricter
    # policy made the feature impossible to use rather than safer.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

#: A quotation is priced information behind a capability URL. It must not sit
#: in a shared proxy cache or a browser's disk cache.
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, private, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class SecurityHeadersMiddleware:
    """Attach the headers to every response, including error responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                for key, value in {**SECURITY_HEADERS, **NO_STORE_HEADERS}.items():
                    headers.append((key.lower().encode(), value.encode()))
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------- #
# Keeping the token out of logs
# --------------------------------------------------------------------------- #

#: Matches the token segment of a portal URL in any log line.
_TOKEN_IN_PATH = re.compile(r"(/quote/public/)[A-Za-z0-9_\-]{16,}")
_REDACTED = r"\1[redacted]"


class TokenRedactingFilter(logging.Filter):
    """Strip access tokens from log records before they are emitted.

    Applied to this service's loggers *and* to uvicorn's access log, which
    writes the full request line by default — the single most likely place for
    a capability URL to end up on disk.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_PATH.sub(_REDACTED, record.msg)
        if record.args:
            record.args = tuple(
                _TOKEN_IN_PATH.sub(_REDACTED, a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True


def redact(text: str) -> str:
    """Public helper so callers can scrub a string before logging it."""
    return _TOKEN_IN_PATH.sub(_REDACTED, text or "")


def install_log_redaction() -> None:
    """Attach the filter everywhere a request path can be emitted.

    Both to the named loggers *and* to the root handlers. A filter on a logger
    only sees records logged through that logger — records propagating up from
    a child bypass it entirely — so handler-level filtering is what actually
    guarantees a token cannot reach the output.
    """
    filt = TokenRedactingFilter()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "portal"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, TokenRedactingFilter) for f in logger.filters):
            logger.addFilter(filt)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, TokenRedactingFilter) for f in handler.filters):
            handler.addFilter(filt)


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

def client_fingerprint(request: Request, secret: str) -> str:
    """A short, salted digest of the client address.

    Hashed rather than stored raw: rate limiting needs to tell callers apart,
    not to know who they are. The salt is the application secret, so the digest
    is not reversible with a rainbow table of the IPv4 space.
    """
    client = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        client = forwarded.split(",")[0].strip()
    return hashlib.sha256(f"{secret}:{client}".encode("utf-8")).hexdigest()[:16]


@dataclass
class RateLimiter:
    """A fixed-window counter held in memory.

    In-process on purpose: this service is small and the limit is a courtesy
    against scripted abuse, not a billing control. Running several instances
    means each enforces its own window — documented rather than hidden. Move to
    Redis if the portal is ever scaled horizontally.
    """

    limit: int
    window_seconds: int
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# --------------------------------------------------------------------------- #
# Origin checking
# --------------------------------------------------------------------------- #

def origin_is_allowed(request: Request, allowed_base: str) -> bool:
    """Second line of defence on state-changing requests, after the nonce.

    A browser sends Origin on cross-site form POSTs, so a mismatch is a strong
    signal. When neither Origin nor Referer is present the request did not come
    from a browser form at all, and is refused: our own pages always send one.
    """
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    candidate = origin or referer
    if not candidate:
        return False

    if not allowed_base:
        # No configured public URL: fall back to the Host this request arrived
        # on, which still blocks a POST originating from another site.
        host = request.headers.get("host", "")
        return bool(host) and host in candidate

    return candidate.rstrip("/").startswith(allowed_base.rstrip("/"))
