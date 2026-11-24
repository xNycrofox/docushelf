"""Authentication and request protection for the web layer.

* :class:`User` is whoever makes a request. :func:`current_user` reads it from
  the session cookie or from an ``Authorization: Bearer`` API token.
* :func:`require_user` protects routes: browser pages redirect to the login
  page (through :class:`LoginRequired`), the JSON API answers 401.
* :func:`csrf_token` / :func:`verify_csrf` guard every state-changing request
  that relies on the session cookie. Requests authenticated with an API token
  are exempt, because browsers never attach that header on their own.
* :func:`hash_password` / :func:`verify_password` implement scrypt hashes for
  the local emergency login; :class:`LoginRateLimiter` slows down guessing.

The session is a signed cookie and holds only the minimum: the user (name,
e-mail, groups, login method), the login time, the CSRF token and the OIDC id
token needed to log out at the identity provider. Sessions are checked against
the current configuration on every request, so removing a user from the
allowed users locks them out after the next restart instead of when their
cookie expires. Sessions of the local emergency account also carry a keyed
fingerprint of the configured password (hash), so changing it ends them.
Nothing else is stored on the server: a new secret key ends every session.

API tokens created in the browser are checked the same way: a token is only
accepted while its owner (as recorded when the token was created) would still
be allowed in.

Households: with several households (``config/households.yaml``) every request
works on exactly one of them, chosen by :func:`get_services`: an API token
belongs to the household whose ``tokens.yaml`` holds it; a browser session
uses ``session["household"]`` while the user still belongs to it, otherwise
the first household the user belongs to. Membership comes from the groups and
users of each household; the local emergency account may use every household.
Admins (:func:`is_household_admin`) may change the users of every household
in the web interface; such a change applies from the next request on.
A logged-in user without any household gets 403 (:class:`NoHousehold`). With
a single household nothing of this applies and everything works as before.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, NoReturn
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)

SESSION_USER = "user"
SESSION_AUTH_AT = "auth_at"
SESSION_CSRF = "csrf"
SESSION_ID_TOKEN = "id_token"
SESSION_NEXT = "login_next"
SESSION_CRED = "cred"
SESSION_HOUSEHOLD = "household"

CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
# The household a page was rendered for (several households only). A request
# that names one must still be in that household: after a switch in another
# tab, an old page would otherwise act on the new household.
HOUSEHOLD_HEADER = "X-DocuShelf-Household"
HOUSEHOLD_FIELD = "household"
STALE_HOUSEHOLD_MESSAGE = (
    "Inzwischen wurde (z. B. in einem anderen Tab) der Haushalt gewechselt. "
    "Bitte die Seite neu laden und es erneut versuchen."
)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Request scope key caching the result of authentication for one request.
_AUTH_SCOPE_KEY = "docushelf.auth"
# Request scope keys: the household an API token belongs to, and the household of the request.
_TOKEN_HOUSEHOLD_KEY = "docushelf.token_household"
_HOUSEHOLD_SCOPE_KEY = "docushelf.household"
# Browsers drop cookies above 4 KB; the session stays well below that.
MAX_SESSION_COOKIE = 3800
_MAX_NEXT_LENGTH = 2048
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


# ---------------------------------------------------------------------------
# Users


@dataclass(frozen=True)
class User:
    username: str
    name: str
    email: str | None = None
    groups: tuple[str, ...] = ()
    method: str = "local"  # "oidc" | "local" | "token"

    def __post_init__(self) -> None:
        if not isinstance(self.groups, tuple):
            object.__setattr__(self, "groups", tuple(self.groups or ()))

    @property
    def display_name(self) -> str:
        return self.name or self.username

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "name": self.name,
            "email": self.email,
            "groups": list(self.groups),
            "method": self.method,
        }

    @classmethod
    def from_session(cls, data: Any) -> "User | None":
        """The user stored in a session, or None if the data is not usable."""
        if not isinstance(data, dict):
            return None
        username = data.get("username")
        method = data.get("method")
        if not isinstance(username, str) or not username or method not in ("oidc", "local"):
            return None
        name = data.get("name")
        email = data.get("email")
        groups = data.get("groups")
        return cls(
            username=username,
            name=name if isinstance(name, str) and name else username,
            email=email if isinstance(email, str) and email else None,
            groups=tuple(g for g in groups if isinstance(g, str)) if isinstance(groups, list) else (),
            method=method,
        )


NO_HOUSEHOLD_TITLE = "Kein Haushalt freigegeben"
NO_HOUSEHOLD_MESSAGE = (
    "Für dein Konto ist noch kein Haushalt freigegeben. "
    "Bitte wende dich an die Person, die DocuShelf eingerichtet hat."
)


class NoHousehold(HTTPException):
    """The logged-in user belongs to no household (403)."""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail=NO_HOUSEHOLD_MESSAGE)


def get_registry(request: Request) -> Any:
    """The household registry of the application (None for applications built without one)."""
    return getattr(request.app.state, "registry", None)


def multi_household(request: Request) -> bool:
    """Whether the installation has several households (``config/households.yaml``)."""
    registry = get_registry(request)
    return registry is not None and getattr(registry, "default", None) is None


def get_services(request: Request) -> Any:
    """The service container of the request's household.

    With a single household this is always ``app.state.services``. With several,
    the household is resolved once per request (see the module documentation);
    raises :class:`NoHousehold` for users without a household and asks for a
    login when nobody is logged in.
    """
    if not multi_household(request):
        return request.app.state.services
    household = current_household(request)
    if household is None:
        user, problem = _auth_state(request)
        if user is None:
            _login_required(request, problem)
        raise NoHousehold()
    return get_registry(request).services(household.id)


def household_services(request: Request) -> Any:
    """Like :func:`get_services`, but None instead of an error (pages, audit entries)."""
    if not multi_household(request):
        return getattr(request.app.state, "services", None)
    household = current_household(request)
    if household is None:
        return None
    try:
        return get_registry(request).services(household.id)
    except Exception:
        log.exception("cannot open household %s", household.id)
        return None


def get_settings(request: Request) -> Any:
    settings = getattr(request.app.state, "settings", None)
    return settings if settings is not None else get_services(request).settings


def client_address(request: Request) -> str:
    client = request.client
    return client.host if client is not None and client.host else "unbekannt"


def wants_json(request: Request) -> bool:
    """True for API requests and clients that prefer JSON over HTML."""
    path = request.url.path
    if path == "/api" or path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


# ---------------------------------------------------------------------------
# Redirect targets


def safe_next(value: Any, default: str = "/") -> str:
    """``value`` if it is a relative path on this site, else ``default``.

    Rejects absolute and protocol-relative URLs (``//host``), backslashes and
    control characters (browsers turn ``/\\host`` and ``/<TAB>/host`` into
    ``//host``) and targets that would lead back into the login flow.
    """
    if not isinstance(value, str):
        return default
    value = value.strip()
    if not value or len(value) > _MAX_NEXT_LENGTH:
        return default
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return default
    try:
        parts = urlsplit(value)
    except ValueError:
        return default
    if parts.scheme or parts.netloc:
        return default
    if parts.path in ("/login", "/logout", "/auth/callback") or parts.path.startswith("/login/"):
        return default
    return value


def login_url(next_url: str | None = None, **params: Any) -> str:
    """``/login`` with a sanitized ``next`` and further query parameters."""
    query: dict[str, str] = {}
    target = safe_next(next_url)
    if target != "/":
        query["next"] = target
    query.update({key: str(value) for key, value in params.items() if value})
    return "/login" + ("?" + urlencode(query) if query else "")


class LoginRequired(Exception):
    """Raised by :func:`require_user` for browser pages; answered with a redirect to the login page."""

    def __init__(self, next_url: str = "/"):
        super().__init__(next_url)
        self.next_url = safe_next(next_url)

    @property
    def location(self) -> str:
        return login_url(self.next_url)


def _return_path(request: Request) -> str:
    """Where to continue after logging in."""
    if request.method.upper() not in ("GET", "HEAD"):
        return "/"
    query = request.url.query
    return safe_next(request.url.path + ("?" + query if query else ""))


# ---------------------------------------------------------------------------
# Authorization rules


def oidc_user_allowed(settings: Any, *, username: str | None, email: str | None, groups: Iterable[str]) -> bool:
    """Whether an OIDC account may use the application (deny by default).

    Allowed are accounts whose user name or e-mail address is listed in
    ``allowed_users`` (case-insensitive) and members of ``allowed_groups``.
    With both lists empty, only ``allow_all_oidc_users`` lets anyone in.
    """
    users = {str(u).strip().casefold() for u in settings.allowed_users or () if str(u).strip()}
    wanted = {str(g).strip().casefold() for g in settings.allowed_groups or () if str(g).strip()}
    if not users and not wanted:
        return bool(settings.allow_all_oidc_users)
    if username and username.strip().casefold() in users:
        return True
    if email and email.strip().casefold() in users:
        return True
    return any(str(group).strip().casefold() in wanted for group in groups or ())


def session_user_allowed(settings: Any, user: User) -> bool:
    """Whether a logged-in session is still valid under the current configuration."""
    if user.method == "local":
        admin = str(settings.admin_user or "")
        return bool(settings.local_login_enabled) and bool(admin) and user.username.casefold() == admin.casefold()
    if user.method == "oidc":
        return bool(settings.oidc_enabled) and oidc_user_allowed(
            settings, username=user.username, email=user.email, groups=user.groups
        )
    return False


def user_households(request: Request, user: User | None) -> list[Any]:
    """The households ``user`` may use (in the order of ``households.yaml``).

    With a single household that is the household itself for every logged-in
    user (access is decided by the login rules). An API token is limited to the
    household it belongs to.
    """
    if user is None:
        return []
    registry = get_registry(request)
    if registry is None:
        household = getattr(getattr(request.app.state, "services", None), "household", None)
        return [household] if household is not None else []
    if not multi_household(request):
        return [registry.default]
    if user.method == "token":
        household_id = request.scope.get(_TOKEN_HOUSEHOLD_KEY)
        return [h for h in registry.households if h.id == household_id]
    try:
        return list(registry.for_user(user))
    except Exception:
        log.exception("cannot determine the households of %s", user.username)
        return []


def current_household(request: Request) -> Any:
    """The household this request works on, or None (nobody logged in, no household).

    With several households the choice of a browser session is kept in
    ``session["household"]`` while the user still belongs to that household;
    otherwise the first household of the user is chosen and remembered.
    """
    cached = request.scope.get(_HOUSEHOLD_SCOPE_KEY)
    if cached is not None:
        return cached[0]
    registry = get_registry(request)
    if registry is None:
        household = getattr(getattr(request.app.state, "services", None), "household", None)
    elif not multi_household(request):
        household = registry.default
    else:
        user = current_user(request)
        allowed = user_households(request, user)
        household = None
        session = request.scope.get("session")
        if user is not None and user.method != "token" and isinstance(session, dict):
            chosen = session.get(SESSION_HOUSEHOLD)
            household = next((h for h in allowed if h.id == chosen), None)
        if household is None and allowed:
            household = allowed[0]
            if user.method != "token" and isinstance(session, dict) and session.get(SESSION_HOUSEHOLD) != household.id:
                session[SESSION_HOUSEHOLD] = household.id
    request.scope[_HOUSEHOLD_SCOPE_KEY] = (household,)
    return household


def switchable_households(request: Request) -> list[Any]:
    """The households the browser user may switch between (empty with fewer than two)."""
    if not multi_household(request):
        return []
    user = current_user(request)
    if user is None or user.method == "token":
        return []
    households = user_households(request, user)
    return households if len(households) >= 2 else []


def household_key(request: Request) -> str | None:
    """The id of the request's household with several households, else None.

    Used to keep per-household state (caches, background jobs) apart.
    """
    if not multi_household(request):
        return None
    get_services(request)  # asks for a login or refuses users without household
    return current_household(request).id


def switch_household(request: Request, household_id: str) -> Any:
    """Make ``household_id`` the household of this browser session.

    Returns the household, or None if the user may not use it (or it does not exist).
    """
    user = current_user(request)
    if user is None or user.method == "token" or not multi_household(request):
        return None
    household = next((h for h in user_households(request, user) if h.id == household_id), None)
    if household is None:
        return None
    request.session[SESSION_HOUSEHOLD] = household.id
    request.scope[_HOUSEHOLD_SCOPE_KEY] = (household,)
    return household


def is_household_admin(request: Request, user: User | None) -> bool:
    """Whether ``user`` may manage household members (several households, browser login only)."""
    if user is None or user.method == "token" or not multi_household(request):
        return False
    is_admin = getattr(get_registry(request), "is_admin", None)
    if is_admin is None:
        return False
    try:
        return bool(is_admin(user))
    except Exception:
        log.exception("cannot determine whether %s is an admin", user.username)
        return False


def _relevant_groups(request: Request, settings: Any) -> set[str]:
    """Groups that decide access: the allowed groups and the groups of every household."""
    groups = {str(g).strip().casefold() for g in settings.allowed_groups or ()}
    registry = get_registry(request)
    for household in getattr(registry, "households", None) or ():
        groups.update(str(g).strip().casefold() for g in getattr(household, "groups", ()) or ())
    return groups


def _user_allowed(request: Request, user: User) -> bool:
    """Whether a logged-in user is still allowed in under the current configuration.

    With several households the households decide who may do what: an SSO
    session stays valid while SSO is configured, and a user without household
    is answered with 403 (:class:`NoHousehold`) on every page.
    """
    settings = get_settings(request)
    if user.method == "oidc" and multi_household(request):
        return bool(settings.oidc_enabled)
    return session_user_allowed(settings, user)


def oidc_login_allowed(request: Request, user: User) -> bool:
    """The login gate for SSO accounts.

    With a single household the allowed users and groups decide; with several
    the account must belong to at least one household.
    """
    if not multi_household(request):
        return oidc_user_allowed(get_settings(request), username=user.username, email=user.email, groups=user.groups)
    try:
        return bool(get_registry(request).for_user(user))
    except Exception:
        log.exception("cannot determine the households of %s", user.username)
        return False


# ---------------------------------------------------------------------------
# Authentication


def token_owner(entry: Any) -> User | None:
    """The user a token entry was created for, as recorded in its owner snapshot.

    None for tokens without a snapshot (command line, older entries); those
    are not tied to an account.
    """
    if not isinstance(entry, dict) or entry.get("method") not in ("oidc", "local"):
        return None
    username = str(entry.get("user") or "").strip()
    email = entry.get("email")
    groups = entry.get("groups")
    return User(
        username=username,
        name=username,
        email=email if isinstance(email, str) and email else None,
        groups=tuple(g for g in groups if isinstance(g, str)) if isinstance(groups, list) else (),
        method=entry["method"],
    )


def _verify_token(store: Any, token: str) -> dict[str, Any] | None:
    if not token or store is None:
        return None
    try:
        return store.verify(token)
    except Exception:
        log.exception("API token verification failed")
        return None


def _find_token(request: Request, token: str) -> tuple[dict[str, Any] | None, Any]:
    """The token entry and the household whose token store holds it.

    With several households every household's store is asked; tokens are random,
    so at most one of them knows a token.
    """
    if not multi_household(request):
        return _verify_token(getattr(get_services(request), "tokens", None), token), None
    registry = get_registry(request)
    for household in registry.households:
        try:
            store = getattr(registry.services(household.id), "tokens", None)
        except Exception:
            log.exception("cannot open the token store of household %s", household.id)
            continue
        entry = _verify_token(store, token)
        if entry is not None:
            return entry, household
    return None, None


def _token_owner_allowed(request: Request, owner: User, household: Any) -> bool:
    if household is None:
        return session_user_allowed(get_settings(request), owner)
    if owner.method == "oidc" and not get_settings(request).oidc_enabled:
        return False
    if owner.method == "local" and not session_user_allowed(get_settings(request), owner):
        return False
    try:
        return any(h.id == household.id for h in get_registry(request).for_user(owner))
    except Exception:
        log.exception("cannot determine the households of %s", owner.username)
        return False


def _token_user(request: Request, token: str) -> tuple[User | None, str | None]:
    entry, household = _find_token(request, token)
    username = str((entry or {}).get("user") or "").strip()
    if not username:
        return None, "invalid_token"
    owner = token_owner(entry)
    if owner is not None and not _token_owner_allowed(request, owner, household):
        log.warning(
            "API token %s (%s) refused: its owner %s is no longer allowed",
            entry.get("id"),
            entry.get("name"),
            username,
        )
        return None, "invalid_token"
    if household is not None:
        request.scope[_TOKEN_HOUSEHOLD_KEY] = household.id
    return User(username=username, name=username, method="token"), None


def _forget_login(session: Any) -> None:
    for key in (SESSION_USER, SESSION_AUTH_AT, SESSION_ID_TOKEN, SESSION_CRED):
        session.pop(key, None)


def _fingerprint_key(request: Request) -> str:
    """The key for credential fingerprints: the secret that signs the session cookie."""
    secret = getattr(request.app.state, "session_secret", None)
    return secret or str(getattr(get_settings(request), "secret_key", "") or "")


def credential_fingerprint(settings: Any, key: str) -> str | None:
    """A short keyed fingerprint of the configured emergency password (hash).

    Stored in local sessions: when the password or its hash changes, the
    fingerprint no longer matches and those sessions end. The cookie can be
    read by its holder, so the value is an HMAC that reveals nothing about the
    credential without the secret key. None if no local password is configured.
    """
    credential = getattr(settings, "admin_password_hash", None) or getattr(settings, "admin_password", None)
    if not credential:
        return None
    digest = hmac.new(key.encode("utf-8"), b"local-login\x00" + str(credential).encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:16]


def _local_credential_current(request: Request, session: Any) -> bool:
    expected = credential_fingerprint(get_settings(request), _fingerprint_key(request))
    return expected is not None and _same_secret(expected, session.get(SESSION_CRED))


def _session_user(request: Request) -> User | None:
    session = request.scope.get("session")
    if not session or SESSION_USER not in session:
        return None
    user = User.from_session(session.get(SESSION_USER))
    settings = get_settings(request)
    auth_at = session.get(SESSION_AUTH_AT)
    max_age = int(getattr(settings, "session_max_age", 0) or 0)
    if user is None or isinstance(auth_at, bool) or not isinstance(auth_at, (int, float)):
        _forget_login(session)
        return None
    if max_age > 0 and time.time() - auth_at > max_age:
        _forget_login(session)
        return None
    if not _user_allowed(request, user):
        log.info("session of %s is no longer authorized and was ended", user.username)
        _forget_login(session)
        return None
    if user.method == "local" and not _local_credential_current(request, session):
        log.info("session of %s ended: the emergency password was changed", user.username)
        _forget_login(session)
        return None
    return user


def _authenticate(request: Request) -> tuple[User | None, str | None]:
    header = request.headers.get("authorization")
    if header:
        scheme, _, credentials = header.strip().partition(" ")
        if scheme.lower() == "bearer":
            return _token_user(request, credentials.strip())
    return _session_user(request), None


def _auth_state(request: Request) -> tuple[User | None, str | None]:
    state = request.scope.get(_AUTH_SCOPE_KEY)
    if state is None:
        state = _authenticate(request)
        request.scope[_AUTH_SCOPE_KEY] = state
    return state


def current_user(request: Request) -> User | None:
    """The user of this request (session or API token), or None."""
    return _auth_state(request)[0]


async def require_user(request: Request) -> User:
    """Dependency: the logged-in user; otherwise 401 (API) or a redirect to the login page."""
    user, problem = _auth_state(request)
    if user is not None:
        if multi_household(request) and current_household(request) is None:
            log.info("%s belongs to no household", user.username)
            raise NoHousehold()
        return user
    _login_required(request, problem)


def _login_required(request: Request, problem: str | None) -> NoReturn:
    """401 for the API and invalid tokens, otherwise the redirect to the login page."""
    if problem == "invalid_token":
        raise HTTPException(
            status_code=401,
            detail="Das API-Token ist ungültig oder wurde widerrufen.",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    if wants_json(request):
        raise HTTPException(status_code=401, detail="Anmeldung erforderlich.", headers={"WWW-Authenticate": "Bearer"})
    raise LoginRequired(_return_path(request))


async def require_session_user(request: Request) -> User:
    """Like :func:`require_user`, but refuses API tokens (e.g. for managing tokens)."""
    user = await require_user(request)
    if user.method == "token":
        raise HTTPException(
            status_code=403,
            detail="Diese Aktion ist nur nach einer Anmeldung im Browser möglich, nicht mit einem API-Token.",
        )
    return user


def start_session(request: Request, user: User, *, id_token: str | None = None) -> User:
    """Log ``user`` in. The previous session content is discarded first, so a
    session prepared by someone else is never promoted to a logged-in one.

    Returns the user as stored (groups may be reduced to the ones that matter
    for authorization if the cookie would get too large).
    """
    settings = get_settings(request)
    session = request.session
    session.clear()
    payload: dict[str, Any] = {
        SESSION_USER: user.to_dict(),
        SESSION_AUTH_AT: int(time.time()),
        SESSION_CSRF: secrets.token_urlsafe(32),
    }
    if id_token:
        payload[SESSION_ID_TOKEN] = id_token
    if user.method == "local":
        fingerprint = credential_fingerprint(settings, _fingerprint_key(request))
        if fingerprint:
            payload[SESSION_CRED] = fingerprint
    if _cookie_size(payload) > MAX_SESSION_COOKIE and SESSION_ID_TOKEN in payload:
        log.info("id token of %s does not fit into the session cookie; it is not kept", user.username)
        del payload[SESSION_ID_TOKEN]
    if _cookie_size(payload) > MAX_SESSION_COOKIE and user.groups:
        relevant = _relevant_groups(request, settings)
        user = replace(user, groups=tuple(g for g in user.groups if g.casefold() in relevant))
        payload[SESSION_USER] = user.to_dict()
    session.update(payload)
    request.scope[_AUTH_SCOPE_KEY] = (user, None)
    request.scope.pop(_HOUSEHOLD_SCOPE_KEY, None)
    return user


def end_session(request: Request) -> dict[str, Any]:
    """Log out: clear the session and return what it contained."""
    session = request.session
    previous = dict(session)
    session.clear()
    request.scope[_AUTH_SCOPE_KEY] = (None, None)
    request.scope.pop(_HOUSEHOLD_SCOPE_KEY, None)
    return previous


def _cookie_size(payload: dict[str, Any]) -> int:
    # Signed cookie = base64(JSON) + timestamp and signature (~40 bytes) + cookie name.
    return len(base64.b64encode(json.dumps(payload).encode("utf-8"))) + 64


# ---------------------------------------------------------------------------
# CSRF


def csrf_token(request: Request) -> str:
    """The CSRF token of the current session (created on first use)."""
    session = request.session
    token = session.get(SESSION_CSRF)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session[SESSION_CSRF] = token
    return token


def _same_secret(expected: Any, supplied: Any) -> bool:
    if not isinstance(expected, str) or not isinstance(supplied, str) or not expected or not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


async def verify_csrf(request: Request) -> None:
    """Dependency: reject state-changing requests without the session's CSRF token.

    The token is taken from the ``X-CSRF-Token`` header or the form field
    ``csrf_token``. Safe methods and requests authenticated with an API token
    need none.
    """
    if request.method.upper() in SAFE_METHODS:
        return
    user, _problem = _auth_state(request)
    if user is not None and user.method == "token":
        return
    session = request.scope.get("session") or {}
    expected = session.get(SESSION_CSRF)
    is_form = request.headers.get("content-type", "").lower().startswith(_FORM_TYPES)
    form: Any = None
    supplied: Any = request.headers.get(CSRF_HEADER)
    if not supplied and is_form:
        form = await request.form()
        supplied = form.get(CSRF_FIELD)
    if not _same_secret(expected, supplied):
        log.info("CSRF check failed for %s %s", request.method, request.url.path)
        raise HTTPException(
            status_code=403,
            detail="Die Sicherheitsprüfung ist fehlgeschlagen (CSRF). Bitte die Seite neu laden und es erneut versuchen.",
        )
    if user is not None and multi_household(request):
        marker: Any = request.headers.get(HOUSEHOLD_HEADER)
        if not marker and is_form:
            form = form if form is not None else await request.form()
            marker = form.get(HOUSEHOLD_FIELD)
        if isinstance(marker, str) and marker:
            household = current_household(request)
            if household is None or household.id != marker:
                log.info(
                    "%s %s refused: the page belongs to household %r, the session is in %r",
                    request.method,
                    request.url.path,
                    marker[:40],
                    getattr(household, "id", None),
                )
                raise HTTPException(status_code=409, detail=STALE_HOUSEHOLD_MESSAGE)


# ---------------------------------------------------------------------------
# Passwords

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16


def _check_scrypt_params(n: int, r: int, p: int) -> None:
    if n < 2 or n & (n - 1) or n > 2**20:
        raise ValueError("scrypt: n muss eine Zweierpotenz zwischen 2 und 2^20 sein")
    if not 1 <= r <= 32 or not 1 <= p <= 16:
        raise ValueError("scrypt: r muss zwischen 1 und 32, p zwischen 1 und 16 liegen")
    if 128 * r * n > 512 * 1024 * 1024:
        raise ValueError("scrypt: die Parameter brauchen zu viel Speicher")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    data = unicodedata.normalize("NFC", password).encode("utf-8")
    maxmem = 128 * r * (n + p + 2) + (1 << 20)
    return hashlib.scrypt(data, salt=salt, n=n, r=r, p=p, maxmem=maxmem, dklen=dklen)


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    text = text.strip()
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except binascii.Error:
        return base64.urlsafe_b64decode(padded)


def hash_password(password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """Hash a password as ``scrypt$n$r$p$salt_b64$hash_b64``."""
    if not isinstance(password, str) or password == "":
        raise ValueError("Das Passwort darf nicht leer sein.")
    _check_scrypt_params(n, r, p)
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    digest = _scrypt(password, salt, n, r, p, _SCRYPT_DKLEN)
    return "$".join(("scrypt", str(n), str(r), str(p), _b64encode(salt), _b64encode(digest)))


def _parse_scrypt_hash(encoded: Any) -> tuple[int, int, int, bytes, bytes] | None:
    if not isinstance(encoded, str):
        return None
    # "$$" is how a literal "$" is written in Compose files; accept it when it slipped through.
    parts = [part for part in encoded.strip().split("$") if part]
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _b64decode(parts[4]), _b64decode(parts[5])
        _check_scrypt_params(n, r, p)
    except (ValueError, binascii.Error):
        return None
    if len(salt) < 8 or not 16 <= len(digest) <= 128:
        return None
    return n, r, p, salt, digest


def verify_password(password: str, encoded: str | None) -> bool:
    """Check a password against a hash from :func:`hash_password` (constant time)."""
    parsed = _parse_scrypt_hash(encoded)
    if parsed is None:
        if encoded:
            log.warning("the configured password hash has an unknown format (expected scrypt$n$r$p$salt$hash)")
        return False
    if not isinstance(password, str):
        return False
    n, r, p, salt, expected = parsed
    try:
        actual = _scrypt(password, salt, n, r, p, len(expected))
    except (ValueError, MemoryError):
        log.warning("cannot compute the scrypt hash with the configured parameters", exc_info=True)
        return False
    return hmac.compare_digest(actual, expected)


def _text_digest(value: str) -> bytes:
    return hashlib.sha256(unicodedata.normalize("NFC", value or "").encode("utf-8")).digest()


def check_local_login(settings: Any, username: str, password: str) -> bool:
    """Whether the credentials belong to the local emergency account.

    The password is always checked, so a wrong user name takes as long as a
    wrong password. A plain ``admin_password`` (meant for development) is
    compared in constant time as well.
    """
    expected_user = str(settings.admin_user or "")
    user_ok = bool(expected_user) and hmac.compare_digest(
        _text_digest((username or "").strip().casefold()), _text_digest(expected_user.casefold())
    )
    if settings.admin_password_hash:
        password_ok = verify_password(password or "", settings.admin_password_hash)
    elif settings.admin_password:
        password_ok = hmac.compare_digest(_text_digest(password or ""), _text_digest(settings.admin_password))
    else:
        password_ok = False
    return user_ok and password_ok and bool(password)


# ---------------------------------------------------------------------------
# Rate limiting


class LoginRateLimiter:
    """Counts failed logins per client address and user name.

    After ``max_failures`` failures within ``window`` seconds for the same
    address and user name, or ``max_failures_per_address`` failures from one
    address in total, further attempts are refused until the oldest of these
    failures leaves the window. ``max_failures_per_account`` additionally caps
    the failures for one user name from all addresses together, in case client
    addresses can be spoofed through forwarding headers. Refused attempts are
    not counted, so a block ends on time. Memory is bounded by ``max_keys``.
    """

    def __init__(
        self,
        max_failures: int = 5,
        window: float = 15 * 60,
        *,
        max_failures_per_address: int = 20,
        max_failures_per_account: int = 30,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max(1, int(max_failures))
        self.max_failures_per_address = max(self.max_failures, int(max_failures_per_address))
        self.max_failures_per_account = max(self.max_failures, int(max_failures_per_account))
        self.window = float(window)
        self.max_keys = max(10, int(max_keys))
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[tuple[str, ...], deque[float]] = {}

    def _keys(self, address: str, username: str) -> tuple[tuple[tuple[str, ...], int], ...]:
        address = address or "unbekannt"
        user = unicodedata.normalize("NFC", username or "").strip().casefold()
        return (
            (("user", address, user), self.max_failures),
            (("address", address), self.max_failures_per_address),
            (("account", user), self.max_failures_per_account),
        )

    def _recent(self, key: tuple[str, ...], now: float) -> deque[float] | None:
        times = self._failures.get(key)
        if times is None:
            return None
        while times and now - times[0] >= self.window:
            times.popleft()
        if not times:
            del self._failures[key]
            return None
        return times

    def retry_after(self, address: str, username: str) -> float:
        """Seconds until another attempt is allowed (0 if it is allowed now)."""
        now = self._clock()
        wait = 0.0
        with self._lock:
            for key, limit in self._keys(address, username):
                times = self._recent(key, now)
                if times is not None and len(times) >= limit:
                    wait = max(wait, times[0] + self.window - now)
        return max(0.0, wait)

    def record_failure(self, address: str, username: str) -> None:
        now = self._clock()
        with self._lock:
            for key, limit in self._keys(address, username):
                times = self._recent(key, now)
                if times is None:
                    if len(self._failures) >= self.max_keys:
                        self._evict(now)
                    times = self._failures[key] = deque(maxlen=limit)
                times.append(now)

    def reset(self, address: str, username: str) -> None:
        """Forget the failures of this user name from this address (after a successful login)."""
        with self._lock:
            self._failures.pop(self._keys(address, username)[0][0], None)

    def _evict(self, now: float) -> None:
        for key in [k for k, times in self._failures.items() if not times or now - times[-1] >= self.window]:
            del self._failures[key]
        excess = len(self._failures) - self.max_keys + 1
        if excess > 0:
            oldest = sorted(self._failures.items(), key=lambda item: item[1][-1])[:excess]
            for key, _times in oldest:
                del self._failures[key]
