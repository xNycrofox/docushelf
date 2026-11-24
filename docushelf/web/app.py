"""The FastAPI application: middleware, error pages, health check and routers.

``create_app()`` wires everything together. The household registry lives in
``app.state.registry``; with a single household its service container is also
``app.state.services``. Background components are started and stopped with the
application lifespan when ``settings.start_background`` is set.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import secrets
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import ClientDisconnect
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__
from ..config import Settings, load_settings
from ..errors import AuthError, ConfigError, Conflict, DocuShelfError, NotFound, PathError, ProcessingError
from ..util.names import OWN_TEMP_PREFIX
from . import auth, routes_api, routes_share
from .security import (
    NO_HOUSEHOLD_TITLE,
    LoginRateLimiter,
    LoginRequired,
    NoHousehold,
    current_user,
    multi_household,
    wants_json,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SESSION_COOKIE = "docushelf_session"
# Room for multipart framing and form fields on top of the upload limit.
BODY_OVERHEAD = 1024 * 1024

# ``form-action`` stays last: security_headers() appends the identity provider to it.
#
# The document viewer (static/viewer.js with the vendored PDF.js build) needs no
# more than this: the library and its worker are module scripts from 'self'
# (``worker-src 'self'``), the file is fetched from the same origin, sizes and the
# text layer are set through the CSS object model (no inline styles), and eval
# is switched off in the viewer (``isEvalSupported: false``). PDF.js decodes a few
# rare image formats (JPEG 2000, ICC colour) with WebAssembly when the policy
# allows ``'wasm-unsafe-eval'`` and otherwise falls back to its JavaScript
# decoders, so that keyword is deliberately not granted.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data: blob:; frame-src 'self'; object-src 'self'; "
    "manifest-src 'self'; worker-src 'self'; "
    "frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
)

ERROR_TITLES = {
    400: "Ungültige Anfrage",
    401: "Anmeldung erforderlich",
    403: "Kein Zugriff",
    404: "Nicht gefunden",
    405: "Nicht erlaubt",
    409: "Konflikt",
    413: "Zu groß",
    415: "Nicht unterstützt",
    422: "Verarbeitung nicht möglich",
    429: "Zu viele Versuche",
    500: "Interner Fehler",
    503: "Nicht verfügbar",
}
DEFAULT_MESSAGES = {
    400: "Die Anfrage ist ungültig.",
    401: "Anmeldung erforderlich.",
    403: "Dafür fehlt die Berechtigung.",
    404: "Die Seite oder das Dokument wurde nicht gefunden.",
    405: "Diese Aktion ist hier nicht möglich.",
    409: "Die Aktion steht im Konflikt mit dem aktuellen Zustand.",
    415: "Dieses Format wird nicht unterstützt.",
    422: "Die Anfrage konnte nicht verarbeitet werden.",
    429: "Zu viele Anfragen. Bitte etwas warten.",
    500: "Ein interner Fehler ist aufgetreten. Details stehen im Server-Log.",
    503: "Der Dienst ist gerade nicht verfügbar.",
}
_STATUS_FOR_ERROR: tuple[tuple[type[DocuShelfError], int], ...] = (
    (NotFound, 404),
    (Conflict, 409),
    (PathError, 400),
    (ConfigError, 400),
    (ProcessingError, 422),
)


def security_headers(settings: Settings) -> dict[str, str]:
    """Headers added to every response.

    ``form-action`` also applies to the redirects that follow a form submission,
    so the identity provider must be allowed there: the logout form ends with a
    redirect to its end-session endpoint.
    """
    policy = CONTENT_SECURITY_POLICY
    if settings.oidc_enabled and settings.oidc_issuer:
        parts = urlsplit(settings.oidc_issuer)
        if parts.scheme in ("http", "https") and parts.netloc:
            policy += f" {parts.scheme}://{parts.netloc}"
    return {
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "X-Frame-Options": "SAMEORIGIN",
    }


def too_large_message(settings: Any) -> str:
    return f"Die Anfrage ist zu groß. Erlaubt sind höchstens {settings.max_upload_mb} MB pro Upload."


# ---------------------------------------------------------------------------
# Middleware


class SecurityHeadersMiddleware:
    """Adds the security headers to every HTTP response that does not set them itself."""

    def __init__(self, app: ASGIApp, headers: dict[str, str]):
        self.app = app
        self.headers = dict(headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestTooLarge(StarletteHTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413)


class BodySizeLimitMiddleware:
    """Refuses request bodies larger than ``max_bytes`` with 413.

    A too large ``Content-Length`` is refused before anything is read; bodies
    without a length (chunked) are counted while the application reads them.
    """

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.strip().isdigit() and int(declared) > self.max_bytes:
            request = Request(scope, receive)
            response = error_response(request, 413, too_large_message(request.app.state.settings))
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestTooLarge()
            return message

        await self.app(scope, limited_receive, send)


# ---------------------------------------------------------------------------
# Errors


def error_response(
    request: Request,
    status_code: int,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    """JSON ``{"detail": …}`` for the API, otherwise the error page."""
    headers = dict(headers or {})
    if wants_json(request):
        return JSONResponse({"detail": message}, status_code=status_code, headers=headers)
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        phrase = "Fehler"
    context = {"status_code": status_code, "title": ERROR_TITLES.get(status_code, phrase), "message": message}
    try:
        return auth.render_page(request, "error.html", context, status_code=status_code, headers=headers)
    except Exception:
        log.exception("cannot render the error page")
        return auth.plain_page(request, "error.html", context, status_code=status_code, headers=headers)


def _default_phrase(status_code: int) -> str | None:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return None


async def _handle_login_required(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, LoginRequired)
    return RedirectResponse(exc.location, status_code=303)


async def _handle_no_household(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, NoHousehold)
    message = str(exc.detail)
    if wants_json(request):
        return JSONResponse({"detail": message}, status_code=403)
    user = current_user(request)
    # Shown without the navigation: every page needs a household.
    context = {"user": None, "account": user, "title": NO_HOUSEHOLD_TITLE, "message": message, "status_code": 403}
    try:
        return auth.render_page(request, "no_household.html", context, status_code=403, headers={"Cache-Control": "no-store"})
    except Exception:
        log.exception("cannot render the page for users without household")
        return auth.plain_page(request, "error.html", context, status_code=403)


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    status_code = exc.status_code
    headers = dict(exc.headers or {})
    if status_code < 400:
        return Response(status_code=status_code, headers=headers)
    detail = exc.detail
    if wants_json(request) and not isinstance(detail, str):
        return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)
    if isinstance(detail, str) and detail and detail != _default_phrase(status_code):
        message = detail
    elif status_code == 413:
        message = too_large_message(request.app.state.settings)
    else:
        message = DEFAULT_MESSAGES.get(status_code) or f"Fehler {status_code}"
    return error_response(request, status_code, message, headers=headers)


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    if wants_json(request):
        return await request_validation_exception_handler(request, exc)
    return error_response(request, 400, "Die Anfrage enthält ungültige Angaben.")


async def _handle_app_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, DocuShelfError)
    if isinstance(exc, AuthError):
        status_code = 403 if current_user(request) is not None else 401
    else:
        status_code = next((code for cls, code in _STATUS_FOR_ERROR if isinstance(exc, cls)), 400)
    message = str(exc).strip()
    if isinstance(exc, NotFound) and message and not any(char.isspace() for char in message):
        # Bare identifiers ("NotFound(item_id)") become a sentence.
        message = f"„{message}“ wurde nicht gefunden."
    if not message:
        message = DEFAULT_MESSAGES.get(status_code, "Die Aktion ist fehlgeschlagen.")
    log.info("%s %s: %s (%d)", request.method, request.url.path, message, status_code)
    return error_response(request, status_code, message)


async def _handle_value_error(request: Request, exc: Exception) -> Response:
    log.warning("invalid value in %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return error_response(request, 400, str(exc).strip() or DEFAULT_MESSAGES[400])


async def _handle_client_disconnect(request: Request, exc: Exception) -> Response:
    # Nobody is left to read an answer (e.g. an upload was cancelled).
    log.info("client went away during %s %s", request.method, request.url.path)
    return Response(status_code=400)


async def _handle_unexpected(request: Request, exc: Exception) -> Response:
    # The server logs the traceback: Starlette re-raises the exception after this response.
    log.error("unexpected %s in %s %s", type(exc).__name__, request.method, request.url.path)
    response = error_response(request, 500, DEFAULT_MESSAGES[500])
    # This response is sent outside the middleware stack; add the headers here.
    for name, value in getattr(request.app.state, "security_headers", {}).items():
        response.headers.setdefault(name, value)
    return response


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LoginRequired, _handle_login_required)
    app.add_exception_handler(NoHousehold, _handle_no_household)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(DocuShelfError, _handle_app_error)
    app.add_exception_handler(ValueError, _handle_value_error)
    app.add_exception_handler(ClientDisconnect, _handle_client_disconnect)
    app.add_exception_handler(Exception, _handle_unexpected)


# ---------------------------------------------------------------------------
# Health check


def _probe_write(directory: Path) -> None:
    """Create, write and delete a small file in ``directory``."""
    path = directory / f"{OWN_TEMP_PREFIX}health-{os.getpid()}-{secrets.token_hex(4)}.part"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"ok")
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _health_checks(services: Any) -> tuple[bool, dict[str, str]]:
    checks: dict[str, str] = {}
    healthy = True
    try:
        services.index.conn().execute("SELECT 1").fetchone()
        checks["database"] = "ok"
    except Exception as exc:
        healthy = False
        checks["database"] = f"Fehler: {exc}"
        log.warning("health check: index database not usable: %s", exc)
    layout = services.layout
    try:
        _probe_write(Path(layout.data))
        checks["data_dir"] = "ok"
    except OSError as exc:
        healthy = False
        checks["data_dir"] = f"Fehler: {exc.strerror or exc}"
        log.warning("health check: data directory not writable: %s", exc)
    archive = Path(layout.archive)
    if archive.is_dir() and os.access(archive, os.R_OK | os.X_OK):
        checks["archive"] = "ok"
    else:
        healthy = False
        checks["archive"] = "Fehler: Archivordner fehlt oder ist nicht lesbar"
        log.warning("health check: archive %s missing or not readable", archive)
    return healthy, checks


def healthz(request: Request) -> JSONResponse:
    """Liveness and readiness without login: index database, data volume, archive.

    With several households every household is checked; a failing check names
    the household (``"Fehler (eltern): …"``). Nothing else about the households
    is revealed.
    """
    if not multi_household(request):
        healthy, checks = _health_checks(request.app.state.services)
    else:
        registry = request.app.state.registry
        healthy, checks = True, {}
        for household in registry.households:
            try:
                ok, partial = _health_checks(registry.services(household.id))
            except Exception as exc:
                log.warning("health check: household %s not usable: %s", household.id, exc)
                ok, partial = False, {"database": f"Fehler: {exc}"}
            healthy = healthy and ok
            for name, value in partial.items():
                if value != "ok":
                    checks[name] = value.replace("Fehler:", f"Fehler ({household.id}):", 1)
                else:
                    checks.setdefault(name, "ok")
    return JSONResponse(
        {"status": "ok" if healthy else "error", "checks": checks},
        status_code=200 if healthy else 503,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Application


def _include_ui(app: FastAPI) -> bool:
    module = (__package__ or "docushelf.web") + ".routes_ui"
    if importlib.util.find_spec(module) is None:
        log.warning("the web interface is not available; serving login and API only")
        return False
    from . import routes_ui

    app.include_router(routes_ui.router)
    return True


class SingleRegistry:
    """A registry around one existing service container (one household).

    Used when ``create_app`` receives a ready-made container, e.g. in tests or
    when the application is embedded.
    """

    def __init__(self, settings: Settings, services: Any):
        self.settings = settings
        self._services = services
        household = getattr(services, "household", None)
        if household is None:
            from ..households import load_households

            households = load_households(settings)
            household = next((h for h in households if h.id == "default"), households[0])
            try:
                services.household = household
            except AttributeError:
                pass
        self.households = [household]
        self.default = household
        self.configured = False
        # A single household keeps its own SMTP receiver (``services.smtp``).
        self.smtp: Any = None

    @property
    def multi(self) -> bool:
        return False

    def __iter__(self) -> Any:
        return iter(self.households)

    def settings_for(self, household_id: str) -> Settings:
        self.get(household_id)
        return self._services.settings

    def get(self, household_id: str) -> Any:
        if household_id != self.default.id:
            raise NotFound(household_id)
        return self.default

    def services(self, household_id: str) -> Any:
        self.get(household_id)
        return self._services

    def for_user(self, user: Any) -> list[Any]:
        return [self.default] if user is not None else []

    def by_smtp_recipient(self, address: str) -> Any:
        return self.default

    def recover(self) -> None:
        self._services.recover()

    def start(self) -> None:
        self._services.start()

    def stop(self) -> None:
        self._services.stop()

    def close(self) -> None:
        _close_indexes(self)


def _close_indexes(registry: Any) -> None:
    """Close the index databases of services that were never started."""
    for household in registry.households:
        try:
            registry.services(household.id).index.close()
        except Exception:
            log.debug("cannot close the index of household %s", household.id, exc_info=True)


def create_app(settings: Settings | None = None, *, services: Any = None, registry: Any = None) -> FastAPI:
    """Build the application.

    Without ``services`` or ``registry`` the households are loaded from the
    configuration and a service container is created for each of them.
    ``services`` wraps one existing container (a single household).
    """
    owns_services = services is None and registry is None
    if settings is None:
        if registry is not None:
            settings = getattr(registry, "settings", None) or load_settings()
        else:
            settings = services.settings if services is not None else load_settings()
    if registry is None:
        if services is not None:
            registry = SingleRegistry(settings, services)
        else:
            from ..households import Registry

            registry = Registry(settings)
    # Create every container now, so configuration errors show at startup.
    containers = {household.id: registry.services(household.id) for household in registry.households}
    if registry.default is not None:
        services = containers[registry.default.id]
    else:
        services = None
        log.info(
            "%d households: %s", len(registry.households), ", ".join(h.id for h in registry.households)
        )

    secret_key = settings.secret_key
    if not secret_key:
        log.warning("no secret key configured; sessions end whenever the process restarts")
        secret_key = secrets.token_urlsafe(48)
    if settings.admin_password and not settings.admin_password_hash:
        log.warning(
            "the local admin password is configured in plain text; "
            "use DOCUSHELF_ADMIN_PASSWORD_HASH (docushelf hash-password) outside of development"
        )
    single = registry.default is not None
    if single and settings.oidc_enabled and not (
        settings.allowed_users or settings.allowed_groups or settings.allow_all_oidc_users
    ):
        log.warning("SSO is configured, but no user or group is allowed yet (DOCUSHELF_ALLOWED_USERS / _GROUPS)")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started = False
        if settings.start_background:
            await run_in_threadpool(registry.start)
            started = True
        try:
            yield
        finally:
            await run_in_threadpool(routes_api.stop_jobs, app)
            if started:
                await run_in_threadpool(registry.stop)
            elif owns_services:
                await run_in_threadpool(_close_indexes, registry)

    app = FastAPI(
        title="DocuShelf",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    headers = security_headers(settings)
    app.state.registry = registry
    # With several households there is no single container: use get_services(request).
    app.state.services = services
    app.state.settings = settings
    app.state.security_headers = headers
    app.state.login_limiter = LoginRateLimiter()
    # Also keys the credential fingerprint of local sessions (web/security.py).
    app.state.session_secret = secret_key

    upload_limit = max(1, int(settings.max_upload_mb)) * 1024 * 1024
    # Added innermost first: the last middleware added is the outermost one.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=upload_limit + BODY_OVERHEAD)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=settings.session_max_age or None,
        same_site="lax",
        https_only=settings.cookies_secure,
    )
    app.add_middleware(SecurityHeadersMiddleware, headers=headers)
    install_exception_handlers(app)

    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
    app.add_api_route("/healthz", healthz, methods=["GET", "HEAD"], include_in_schema=False)
    # Installable web app: manifest and service worker without login, sharing from phones.
    app.add_api_route("/manifest.webmanifest", routes_share.manifest, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/sw.js", routes_share.service_worker, methods=["GET", "HEAD"], include_in_schema=False)
    app.include_router(auth.router)
    app.include_router(routes_share.router)
    _include_ui(app)
    app.include_router(routes_api.router)
    return app
