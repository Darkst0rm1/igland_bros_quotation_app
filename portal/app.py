"""The public customer quotation service.

Separate from the employee application on purpose. That one holds costs,
margins and every quotation ever raised, and is deployed privately; this one is
reachable by anyone holding a link and can therefore see only what
:mod:`portal.projection` allows.

Shares the database and the domain layer, so a price shown to a customer here
and a price shown to an employee there come from the same code.

No cookies are set. The one-time nonce travels in the form and is signed with
the application secret, so there is no session to fixate, steal or need a
consent banner for.
"""
from __future__ import annotations

import logging
import pathlib
from decimal import Decimal
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from modules import portal_service, settings_service
from modules.config import get_settings
from modules.database import session_scope
from modules.models import PortalResponse, Quotation, User
from modules.constants import PortalResponseType
from modules.portal_service import (
    PortalAccessError,
    PortalError,
    PortalStateError,
)
from modules.utilities import format_money
from portal import assets, projection
from portal.branding import resolve_brand, validate_portal_settings
from portal.security import (
    RateLimiter,
    SecurityHeadersMiddleware,
    client_fingerprint,
    install_log_redaction,
    origin_is_allowed,
    redact,
)

log = logging.getLogger("portal")

BASE_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _money(value: Decimal | None, currency: str = "USD") -> str:
    """Format at the presentation boundary only.

    The projection carries Decimal the whole way; this is the single place a
    figure becomes a string, and it reuses the same formatter the PDF uses so
    the two documents cannot disagree on how a number looks.
    """
    if value is None:
        return ""
    code = (currency or "").upper()
    formatted = format_money(value, code)
    return f"{formatted} {code}".strip() if code else formatted


def _qty(value: Decimal | None) -> str:
    """Quantities without trailing zeros: 3500, not 3500.000."""
    if value is None:
        return ""
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal(1))
    return f"{normalised:,f}"


TEMPLATES.env.filters["money"] = _money
TEMPLATES.env.filters["qty"] = _qty

# Docs endpoints are disabled: this is a customer-facing site, not an API
# product, and a schema listing is surface area with no audience.
app = FastAPI(
    title="Quotation Portal",
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

install_log_redaction()

_settings = get_settings()
# Refuses to start a production portal without a valid https PORTAL_BASE_URL.
validate_portal_settings(_settings)
_view_limiter = RateLimiter(limit=_settings.portal_view_rate_per_minute, window_seconds=60)
_submit_limiter = RateLimiter(limit=_settings.portal_submit_rate_per_hour, window_seconds=3600)


# --------------------------------------------------------------------------- #
# Shared responses
# --------------------------------------------------------------------------- #

def _not_found(request: Request, status_code: int = 404) -> HTMLResponse:
    """One page for every access failure.

    Unknown, malformed, expired, revoked, superseded and belonging-to-someone-
    else all render identically. Telling a prober which of those applied would
    confirm that a token exists, which is the only thing they are trying to
    learn.
    """
    return TEMPLATES.TemplateResponse(
        request=request, name="not_found.html", status_code=status_code,
        context={"support_email": _settings.portal_support_email},
    )


def _too_many(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request=request, name="rate_limited.html", status_code=429, context={}
    )


def _sales_rep_name(session, quotation: Quotation) -> str:
    if not quotation.sales_user_id:
        return ""
    rep = session.get(User, quotation.sales_user_id)
    return rep.employee_name if rep else ""


def _accepted_response(session, quotation: Quotation) -> PortalResponse | None:
    return session.execute(
        select(PortalResponse)
        .where(
            PortalResponse.quotation_id == quotation.id,
            PortalResponse.response_type == PortalResponseType.APPROVED,
        )
        .order_by(PortalResponse.id.desc())
    ).scalars().first()


def _render_quote(
    request: Request, session, token, quotation: Quotation,
    selected_ids: list[int], *, status_code: int = 200, error: str = "",
) -> HTMLResponse:
    totals = portal_service.compute_selection_totals(quotation, selected_ids)
    accepted = _accepted_response(session, quotation)
    view = projection.build_quote_view(
        quotation, token, totals,
        company_settings=settings_service.get_company_settings(session),
        selected_ids=selected_ids,
        accepted_response=accepted,
        sales_representative=_sales_rep_name(session, quotation),
    )
    nonce, signature = portal_service.issue_submission_nonce(token)
    return TEMPLATES.TemplateResponse(
        request=request, name="quote.html", status_code=status_code,
        context={
            "quote": view,
            "brand": resolve_brand(_settings, settings_service.get_company_settings(session)),
            "nonce": nonce,
            "signature": signature,
            "error": error,
            # The plaintext token stays in the URL and in this form action for
            # the lifetime of the request. It is never written to a title, a
            # comment, an analytics event or a log line.
            "token": request.path_params["token"],
        },
    )


@app.exception_handler(StarletteHTTPException)
async def _uniform_http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
    """Render every HTTP error as the same page.

    Without this, a path the router does not match at all — say
    ``/quote/public/../../etc/passwd`` — returns FastAPI's default
    ``{"detail":"Not Found"}`` JSON, while an invalid token returns the styled
    page. That difference tells a prober whether their URL reached the route,
    which is exactly the signal the generic page exists to withhold.
    """
    if exc.status_code == 429:
        return _too_many(request)
    if request.url.path == "/health":     # keep the probe machine-readable
        return JSONResponse({"status": "error"}, status_code=exc.status_code)
    return _not_found(request, status_code=404)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health() -> JSONResponse:
    """Liveness only. No database status, no configuration, no version leak."""
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    # Nothing to see at the root: quotations are only reachable by link.
    return _not_found(request, status_code=404)


@app.get("/quote/public/{token}", response_class=HTMLResponse)
async def view_quote(request: Request, token: str) -> HTMLResponse:
    if not _view_limiter.allow(client_fingerprint(request, _settings.secret_key)):
        return _too_many(request)

    with session_scope() as session:
        try:
            access = portal_service.resolve_token(session, token)
        except PortalAccessError:
            return _not_found(request)

        quotation = session.get(Quotation, access.quotation_id)
        portal_service.record_view(session, access)
        return _render_quote(request, session, access, quotation, selected_ids=[])


@app.post("/quote/public/{token}/preview", response_class=HTMLResponse)
async def preview_selection(
    request: Request,
    token: str,
    selected: Annotated[list[str], Form()] = [],
) -> HTMLResponse:
    """Re-price with the customer's current ticks, without recording anything.

    A POST rather than a GET because it carries the selection, but it is not a
    state change: nothing is persisted until the customer submits a response.
    """
    if not _view_limiter.allow(client_fingerprint(request, _settings.secret_key)):
        return _too_many(request)

    with session_scope() as session:
        try:
            access = portal_service.resolve_token(session, token)
        except PortalAccessError:
            return _not_found(request)
        quotation = session.get(Quotation, access.quotation_id)
        chosen = projection.resolve_refs(access, quotation, selected)
        return _render_quote(request, session, access, quotation, selected_ids=chosen)


@app.post("/quote/public/{token}/approve", response_class=HTMLResponse)
async def approve_quote(
    request: Request,
    token: str,
    customer_name: Annotated[str, Form()] = "",
    job_title: Annotated[str, Form()] = "",
    customer_email: Annotated[str, Form()] = "",
    signature_name: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    accepted_terms: Annotated[str, Form()] = "",
    nonce: Annotated[str, Form()] = "",
    signature: Annotated[str, Form()] = "",
    selected: Annotated[list[str], Form()] = [],
) -> HTMLResponse:
    fingerprint = client_fingerprint(request, _settings.secret_key)
    if not _submit_limiter.allow(fingerprint):
        return _too_many(request)
    if not origin_is_allowed(request, _settings.portal_base_url):
        log.warning("Rejected cross-origin submission for %s", redact(str(request.url)))
        return _not_found(request, status_code=403)

    with session_scope() as session:
        try:
            access = portal_service.resolve_token(session, token)
        except PortalAccessError:
            return _not_found(request)

        quotation = session.get(Quotation, access.quotation_id)
        chosen = projection.resolve_refs(access, quotation, selected)

        try:
            portal_service.verify_submission_nonce(access, nonce, signature)
            response = portal_service.approve(
                session, access,
                customer_name=customer_name,
                customer_email=customer_email,
                job_title=job_title,
                signature_name=signature_name,
                comment=comment,
                accepted_terms=accepted_terms in {"on", "true", "1", "yes"},
                selected_ids=chosen,
                nonce=nonce,
            )
        except (PortalError, PortalStateError) as exc:
            return _render_quote(
                request, session, access, quotation, chosen,
                status_code=400, error=str(exc),
            )
        except IntegrityError:
            # A duplicate nonce or a second acceptance of the same revision.
            session.rollback()
            return _not_found(request, status_code=409)

        return TEMPLATES.TemplateResponse(
            request=request, name="confirmation.html",
            context={
                "heading": "Quotation accepted",
                "message": (
                    f"Thank you, {response.customer_name}. We have recorded your "
                    f"acceptance of {quotation.quote_number} and your "
                    f"representative will be in touch to confirm next steps."
                ),
                "reference": quotation.quote_number,
                "total": response.grand_total,
                "currency": response.currency,
            },
        )


@app.post("/quote/public/{token}/request-changes", response_class=HTMLResponse)
async def request_changes(
    request: Request,
    token: str,
    customer_name: Annotated[str, Form()] = "",
    customer_email: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
    nonce: Annotated[str, Form()] = "",
    signature: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if not _submit_limiter.allow(client_fingerprint(request, _settings.secret_key)):
        return _too_many(request)
    if not origin_is_allowed(request, _settings.portal_base_url):
        return _not_found(request, status_code=403)

    with session_scope() as session:
        try:
            access = portal_service.resolve_token(session, token)
        except PortalAccessError:
            return _not_found(request)

        quotation = session.get(Quotation, access.quotation_id)
        try:
            portal_service.verify_submission_nonce(access, nonce, signature)
            response = portal_service.request_changes(
                session, access,
                customer_name=customer_name,
                customer_email=customer_email,
                comment=comment,
                nonce=nonce,
            )
        except (PortalError, PortalStateError) as exc:
            return _render_quote(
                request, session, access, quotation, [],
                status_code=400, error=str(exc),
            )
        except IntegrityError:
            session.rollback()
            return _not_found(request, status_code=409)

        return TEMPLATES.TemplateResponse(
            request=request, name="confirmation.html",
            context={
                "heading": "Change request received",
                "message": (
                    f"Thank you, {response.customer_name}. We have passed your "
                    f"comments to your representative, who will send a revised "
                    f"quotation."
                ),
                "reference": quotation.quote_number,
                "total": None,
                "currency": "",
            },
        )


@app.get("/brand.css")
async def brand_stylesheet() -> Response:
    """Brand colours as a real stylesheet.

    Served rather than inlined so the Content-Security-Policy can stay at
    ``style-src 'self'`` — an inline block would need 'unsafe-inline' on every
    page just to set three colours.
    """
    with session_scope() as session:
        brand = resolve_brand(_settings, settings_service.get_company_settings(session))
    return Response(content=brand.css(), media_type="text/css")


@app.get("/quote/public/{token}/assets/product/{item_ref}")
async def product_image(request: Request, token: str, item_ref: str) -> Response:
    """A product photograph, proxied.

    Deliberately not counted as a quotation view: a page with eight products
    would otherwise register nine views and make "first viewed" meaningless.
    """
    if not _view_limiter.allow(client_fingerprint(request, _settings.secret_key)):
        return _too_many(request)

    with session_scope() as session:
        try:
            access = portal_service.resolve_token(session, token)
        except PortalAccessError:
            # Same generic answer as everywhere else: an image request must not
            # become an oracle for whether a token is live.
            return _not_found(request)

        quotation = session.get(Quotation, access.quotation_id)
        item = assets.resolve_item_by_ref(access, quotation, item_ref)
        if item is None:
            payload = assets.placeholder()
        else:
            payload = assets.load_product_image(item)

    return Response(
        content=payload.content,
        media_type=payload.media_type,
        headers={"Content-Disposition": "inline"},
    )


@app.get("/quote/public/{token}/assets/logo")
async def company_logo(request: Request, token: str) -> Response:
    """The company logo, behind the same token check."""
    if not _view_limiter.allow(client_fingerprint(request, _settings.secret_key)):
        return _too_many(request)

    with session_scope() as session:
        try:
            portal_service.resolve_token(session, token)
        except PortalAccessError:
            return _not_found(request)
        company = settings_service.get_company_settings(session)
        payload = assets.load_company_logo(company.logo_key if company else None)

    if payload is None:
        return _not_found(request)
    return Response(
        content=payload.content,
        media_type=payload.media_type,
        headers={"Content-Disposition": "inline"},
    )


# GET must never change state: the approval and change-request paths are POST
# only, and FastAPI returns 405 for a GET against them without extra work.
@app.get("/quote/public/{token}/approve")
@app.get("/quote/public/{token}/request-changes")
async def no_state_change_via_get(request: Request, token: str) -> RedirectResponse:
    return RedirectResponse(url=f"/quote/public/{token}", status_code=303)
