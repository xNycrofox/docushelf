"""Login and logout: single sign-on via OpenID Connect and the local emergency account.

* ``GET /login`` – the login page (``login.html``).
* ``GET /login/oidc`` – starts the authorization code flow (PKCE S256, state, nonce).
* ``GET /auth/callback`` – finishes it: the ID token is verified (signature,
  issuer, audience, expiry, nonce), the claims of the userinfo endpoint are
  added, access is checked and the session starts.
* ``POST /login/local`` – the local emergency login (rate limited).
* ``POST /logout`` – ends the session and, after an SSO login, also the session
  at the identity provider when it offers an ``end_session_endpoint``.

The OIDC callback URL is always ``<base_url>/auth/callback``; it is never
derived from request headers, which a client could forge.
"""

from __future__ import annotations

import html
import importlib.util
import logging
import math
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import TemplateNotFound
from starlette.concurrency import run_in_threadpool

from .security import (
    SESSION_ID_TOKEN,
    SESSION_NEXT,
    LoginRateLimiter,
    User,
    check_local_login,
    client_address,
    csrf_token,
    current_user,
    end_session,
    get_settings,
    household_services,
    login_url,
    oidc_login_allowed,
    safe_next,
    start_session,
    verify_csrf,
)

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

CALLBACK_PATH = "/auth/callback"
OIDC_CLIENT_NAME = "oidc"
OIDC_HTTP_TIMEOUT = 15.0
_TEMPLATING_MODULE = (__package__ or "docushelf.web") + ".templating"
_NO_STORE = {"Cache-Control": "no-store"}

# Error codes passed to the login page as ``?error=`` (only codes, never free text).
LOGIN_ERRORS = {
    "oidc_failed": "Die Anmeldung über SSO ist fehlgeschlagen. Bitte erneut versuchen.",
    "oidc_state": "Die Anmeldung ist abgelaufen oder wurde in einem anderen Fenster begonnen. Bitte erneut versuchen.",
    "oidc_cancelled": "Die Anmeldung wurde beim Anmeldedienst abgebrochen.",
    "oidc_unavailable": "Der Anmeldedienst ist gerade nicht erreichbar. Bitte später erneut versuchen.",
    "oidc_disabled": "Die Anmeldung über SSO ist nicht eingerichtet.",
    "no_username": "Der Anmeldedienst hat keinen Benutzernamen übermittelt.",
    "denied": "Dieses Konto ist für DocuShelf nicht freigeschaltet.",
}
GENERIC_LOGIN_ERROR = "Die Anmeldung ist fehlgeschlagen."


# ---------------------------------------------------------------------------
# Pages


def render_page(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """Render a template of the web UI; without the UI a plain built-in page is used."""
    context = dict(context or {})
    if importlib.util.find_spec(_TEMPLATING_MODULE) is not None:
        from . import templating

        try:
            response = templating.render(request, name, context, status_code=status_code)
        except TemplateNotFound:
            log.warning("template %s is missing; showing a plain page instead", name)
        else:
            if headers:
                response.headers.update(headers)
            return response
    return plain_page(request, name, context, status_code=status_code, headers=headers)


def plain_page(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """A minimal page without templates, scripts or styles (login and error pages)."""
    esc = html.escape
    if name == "login.html":
        title = "Anmelden"
        body = _plain_login_body(request, context)
    else:
        title = str(context.get("title") or "Fehler")
        message = str(context.get("message") or "")
        body = f"<h1>{esc(title)}</h1>\n<p>{esc(message)}</p>\n<p><a href=\"/\">Zur Startseite</a></p>"
    page = (
        "<!DOCTYPE html>\n<html lang=\"de\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{esc(title)} – DocuShelf</title>\n</head>\n<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n"
    )
    return HTMLResponse(page, status_code=status_code, headers=headers)


def _plain_login_body(request: Request, context: dict[str, Any]) -> str:
    esc = html.escape
    target = safe_next(context.get("next"))
    parts = ["<h1>DocuShelf</h1>"]
    if context.get("logged_out"):
        parts.append("<p>Abgemeldet.</p>")
    if context.get("error"):
        parts.append(f"<p role=\"alert\">{esc(str(context['error']))}</p>")
    if context.get("oidc_enabled"):
        href = "/login/oidc" + ("?" + urlencode({"next": target}) if target != "/" else "")
        parts.append(f"<p><a href=\"{esc(href)}\">{esc(str(context.get('oidc_label') or 'Mit SSO anmelden'))}</a></p>")
    if context.get("local_login_enabled"):
        parts.append(
            "<form method=\"post\" action=\"/login/local\">\n"
            f"<input type=\"hidden\" name=\"csrf_token\" value=\"{esc(csrf_token(request))}\">\n"
            f"<input type=\"hidden\" name=\"next\" value=\"{esc(target)}\">\n"
            "<p><label>Benutzername <input name=\"username\" autocomplete=\"username\" required></label></p>\n"
            "<p><label>Passwort <input type=\"password\" name=\"password\" autocomplete=\"current-password\" required>"
            "</label></p>\n<p><button type=\"submit\">Anmelden</button></p>\n</form>"
        )
    if not context.get("oidc_enabled") and not context.get("local_login_enabled"):
        parts.append("<p>Es ist keine Anmeldemethode eingerichtet.</p>")
    return "\n".join(parts)


def login_context(request: Request, *, error: str | None = None, next_url: str = "/", logged_out: bool = False) -> dict[str, Any]:
    settings = get_settings(request)
    return {
        "oidc_enabled": bool(settings.oidc_enabled),
        "oidc_label": settings.oidc_label,
        "local_login_enabled": bool(settings.local_login_enabled),
        "error": error,
        "next": safe_next(next_url),
        "logged_out": logged_out,
    }


def _login_page(
    request: Request,
    *,
    error: str | None = None,
    next_url: str = "/",
    logged_out: bool = False,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    context = login_context(request, error=error, next_url=next_url, logged_out=logged_out)
    # The local login form needs the token; creating it here also sets the session cookie.
    context["csrf_token"] = csrf_token(request)
    return render_page(request, "login.html", context, status_code=status_code, headers={**_NO_STORE, **(headers or {})})


def _to_login(next_url: str, error: str | None = None) -> RedirectResponse:
    return RedirectResponse(login_url(next_url, error=error), status_code=303)


def _audit(request: Request, user: str, action: str, *, services: Any = None, **details: Any) -> None:
    """Write an audit entry into the log of the request's household.

    With several households, events without a household (a refused login) only
    go to the server log.
    """
    if services is None:
        services = household_services(request)
    audit = getattr(services, "audit", None)
    if audit is None:
        return
    try:
        audit.write(user, action, **details)
    except Exception:
        log.exception("cannot write the audit log")


def get_login_limiter(request: Request) -> LoginRateLimiter:
    limiter = getattr(request.app.state, "login_limiter", None)
    if limiter is None:
        limiter = request.app.state.login_limiter = LoginRateLimiter()
    return limiter


# ---------------------------------------------------------------------------
# OIDC client


def discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def callback_url(settings: Any) -> str:
    return settings.base_url.rstrip("/") + CALLBACK_PATH


def oidc_scope(scopes: str | None) -> str:
    """The configured scopes, always including ``openid`` (the ID token is required)."""
    parts = [part for part in (scopes or "").replace(",", " ").split() if part]
    if "openid" not in parts:
        parts.insert(0, "openid")
    return " ".join(dict.fromkeys(parts))


def get_oidc_client(request: Request) -> Any:
    """The Authlib client for the configured provider (one per application)."""
    client = getattr(request.app.state, "oidc_client", None)
    if client is None:
        settings = get_settings(request)
        client_kwargs: dict[str, Any] = {
            "scope": oidc_scope(settings.oidc_scopes),
            "code_challenge_method": "S256",
            "timeout": OIDC_HTTP_TIMEOUT,
        }
        if not settings.oidc_client_secret:
            # Public client: PKCE only, no client authentication at the token endpoint.
            client_kwargs["token_endpoint_auth_method"] = "none"
        oauth = OAuth()
        oauth.register(
            name=OIDC_CLIENT_NAME,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=discovery_url(settings.oidc_issuer),
            client_kwargs=client_kwargs,
        )
        client = oauth.create_client(OIDC_CLIENT_NAME)
        request.app.state.oidc_client = client
    return client


def _check_issuer(settings: Any, metadata: dict[str, Any]) -> None:
    discovered = str(metadata.get("issuer") or "")
    if discovered and discovered.rstrip("/") != str(settings.oidc_issuer or "").rstrip("/"):
        log.warning(
            "the provider announces the issuer %s, but %s is configured; ID tokens are checked against %s",
            discovered,
            settings.oidc_issuer,
            discovered,
        )


def _claims_options(settings: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    """Claims the ID token must carry: our client id as audience and the provider as issuer."""
    options: dict[str, Any] = {"aud": {"essential": True, "values": [settings.oidc_client_id]}}
    issuer = metadata.get("issuer")
    if issuer:
        options["iss"] = {"essential": True, "values": [issuer]}
    return options


def _claim_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = " ".join(str(value).split())
    return text or None


def _email_verified(value: Any) -> bool:
    """Absent counts as verified (many providers omit the claim); an explicit false does not."""
    if value is False:
        return False
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no"):
        return False
    return True


def _claim_groups(value: Any) -> list[str]:
    items = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else []
    groups: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("name")
        text = _claim_text(item)
        if text:
            groups.append(text)
    return list(dict.fromkeys(groups))


def _with_query(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


# ---------------------------------------------------------------------------
# Routes


@router.get("/login")
async def login_page(
    request: Request,
    next_url: str | None = Query(None, alias="next"),
    error: str | None = None,
    logged_out: str | None = None,
) -> Response:
    target = safe_next(next_url)
    user = current_user(request)
    if user is not None and user.method != "token" and not error:
        return RedirectResponse(target, status_code=303)
    message = LOGIN_ERRORS.get(error, GENERIC_LOGIN_ERROR) if error else None
    return _login_page(request, error=message, next_url=target, logged_out=bool(logged_out))


@router.get("/login/oidc")
async def login_oidc(request: Request, next_url: str | None = Query(None, alias="next")) -> Response:
    settings = get_settings(request)
    target = safe_next(next_url)
    if not settings.oidc_enabled:
        return _to_login(target, "oidc_disabled")
    client = get_oidc_client(request)
    try:
        metadata = await client.load_server_metadata()
        _check_issuer(settings, metadata)
        request.session[SESSION_NEXT] = target
        return await client.authorize_redirect(request, callback_url(settings))
    except Exception:
        log.warning("cannot start the SSO login with %s", settings.oidc_issuer, exc_info=True)
        return _to_login(target, "oidc_unavailable")


@router.get(CALLBACK_PATH)
async def auth_callback(request: Request) -> Response:
    settings = get_settings(request)
    target = safe_next(request.session.pop(SESSION_NEXT, None))
    if not settings.oidc_enabled:
        return _to_login(target, "oidc_disabled")
    client = get_oidc_client(request)
    try:
        metadata = await client.load_server_metadata()
        token = await client.authorize_access_token(request, claims_options=_claims_options(settings, metadata))
    except OAuthError as exc:
        code = {"access_denied": "oidc_cancelled", "mismatching_state": "oidc_state"}.get(exc.error or "", "oidc_failed")
        log.warning("SSO login failed: %s (%s)", exc.error, exc.description)
        return _to_login(target, code)
    except httpx.HTTPError:
        log.warning("the identity provider could not be reached during the SSO login", exc_info=True)
        return _to_login(target, "oidc_unavailable")
    except Exception:
        # Invalid ID token (signature, issuer, audience, expiry, nonce) or an unexpected answer.
        log.warning("SSO login rejected", exc_info=True)
        return _to_login(target, "oidc_failed")

    id_claims = token.get("userinfo")
    if not id_claims:
        log.warning("the identity provider returned no ID token; the SSO login was rejected")
        return _to_login(target, "oidc_failed")
    claims = dict(id_claims)
    if metadata.get("userinfo_endpoint"):
        try:
            info = await client.userinfo(token=token)
        except Exception:
            log.warning("cannot read the userinfo endpoint; using the claims of the ID token", exc_info=True)
        else:
            if str(info.get("sub") or "") != str(id_claims.get("sub") or ""):
                log.warning("userinfo subject does not match the ID token; the SSO login was rejected")
                return _to_login(target, "oidc_failed")
            claims.update(info)

    raw_email = _claim_text(claims.get("email"))
    # An unverified address is neither shown nor used to grant access.
    email = raw_email if raw_email and _email_verified(claims.get("email_verified")) else None
    username = _claim_text(claims.get("preferred_username")) or email
    if not username:
        log.warning("the identity provider sent neither preferred_username nor a verified e-mail address")
        return _to_login(target, "no_username")
    groups = _claim_groups(claims.get("groups"))
    name = (
        _claim_text(claims.get("name"))
        or " ".join(filter(None, (_claim_text(claims.get("given_name")), _claim_text(claims.get("family_name")))))
        or username
    )
    account = User(username=username, name=name[:200], email=email, groups=tuple(groups), method="oidc")
    # With several households the account must belong to at least one of them.
    if not oidc_login_allowed(request, account):
        log.warning("SSO login of %s refused: not among the allowed users or groups", username)
        _audit(request, username, "login_denied", method="oidc")
        return _to_login(target, "denied")
    user = start_session(request, account, id_token=token.get("id_token"))
    _audit(request, user.username, "login", method="oidc")
    log.info("%s logged in via SSO", user.username)
    return RedirectResponse(target, status_code=303)


@router.post("/login/local")
async def login_local(request: Request) -> Response:
    settings = get_settings(request)
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        raise HTTPException(status_code=415, detail="Die Anmeldung erwartet ein Formular.")
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = form.get("password")
    password = password if isinstance(password, str) else ""
    target = safe_next(form.get("next"))
    try:
        await verify_csrf(request)
    except HTTPException:
        return _login_page(
            request,
            error="Die Sitzung ist abgelaufen. Bitte erneut anmelden.",
            next_url=target,
            status_code=403,
        )
    if not settings.local_login_enabled:
        return _login_page(request, error="Die lokale Anmeldung ist nicht eingerichtet.", next_url=target, status_code=403)

    limiter = get_login_limiter(request)
    address = client_address(request)
    wait = limiter.retry_after(address, username)
    if wait > 0:
        minutes = max(1, math.ceil(wait / 60))
        unit = "Minute" if minutes == 1 else "Minuten"
        log.warning("local login for %r from %s refused: too many failed attempts", username[:100], address)
        return _login_page(
            request,
            error=f"Zu viele fehlgeschlagene Anmeldeversuche. Bitte in {minutes} {unit} erneut versuchen.",
            next_url=target,
            status_code=429,
            headers={"Retry-After": str(math.ceil(wait))},
        )
    if not await run_in_threadpool(check_local_login, settings, username, password):
        limiter.record_failure(address, username)
        log.warning("failed local login for %r from %s", username[:100], address)
        return _login_page(request, error="Benutzername oder Passwort ist falsch.", next_url=target, status_code=401)
    limiter.reset(address, username)
    user = start_session(request, User(username=settings.admin_user, name=settings.admin_user, method="local"))
    _audit(request, user.username, "login", method="local")
    log.info("%s logged in with the local account from %s", user.username, address)
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
async def logout(request: Request) -> Response:
    settings = get_settings(request)
    logged_out = login_url(logged_out="1")
    user = current_user(request)
    if user is None or user.method == "token":
        # Nothing to end (e.g. the session expired in the meantime).
        return RedirectResponse(logged_out, status_code=303)
    await verify_csrf(request)
    services = household_services(request)
    previous = end_session(request)
    _audit(request, user.username, "logout", services=services)
    if user.method == "oidc" and settings.oidc_enabled:
        url = await _provider_logout_url(request, settings, previous.get(SESSION_ID_TOKEN))
        if url:
            return RedirectResponse(url, status_code=303)
    return RedirectResponse(logged_out, status_code=303)


async def _provider_logout_url(request: Request, settings: Any, id_token: Any) -> str | None:
    """RP-initiated logout at the identity provider, if it supports it."""
    try:
        metadata = await get_oidc_client(request).load_server_metadata()
    except Exception:
        log.warning("cannot load the provider metadata for the logout", exc_info=True)
        return None
    endpoint = metadata.get("end_session_endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        return None
    params = {
        "client_id": str(settings.oidc_client_id),
        "post_logout_redirect_uri": settings.base_url.rstrip("/") + login_url(logged_out="1"),
    }
    if isinstance(id_token, str) and id_token:
        params["id_token_hint"] = id_token
    return _with_query(endpoint, params)
