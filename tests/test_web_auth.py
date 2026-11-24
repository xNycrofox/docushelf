"""Tests for login (local and OIDC), sessions, CSRF, password hashes, rate limiting,
security headers, error pages and the health check."""

from __future__ import annotations

import os
import stat
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Depends, Request
from fastapi.responses import PlainTextResponse

from docushelf.web import security
from docushelf.web.app import SESSION_COOKIE
from docushelf.web.security import (
    LoginRateLimiter,
    User,
    check_local_login,
    hash_password,
    oidc_user_allowed,
    require_user,
    safe_next,
    verify_csrf,
    verify_password,
)
from web_support import (
    ADMIN_PASSWORD,
    FakeProvider,
    client_for,
    csrf_for,
    login_local,
    make_app,
    oidc_login,
    session_data,
    signed_session,
)


def add_test_routes(app) -> None:
    """A protected page and a protected form action, like the UI routes use them."""

    @app.get("/geschuetzt")
    async def protected(user: User = Depends(require_user)):
        return PlainTextResponse(f"hallo {user.username}")

    @app.post("/aktion", dependencies=[Depends(verify_csrf)])
    async def action(user: User = Depends(require_user)):
        return PlainTextResponse(f"ok {user.method}")

    @app.post("/anonym", dependencies=[Depends(verify_csrf)])
    async def anonymous_action():
        return PlainTextResponse("ok")

    @app.get("/pflanzen")
    async def plant(request: Request):
        request.session["planted"] = "von außen"
        return PlainTextResponse("gepflanzt")


@pytest.fixture
def app(base_services):
    application = make_app(base_services)
    add_test_routes(application)
    return application


@pytest.fixture
def client(app):
    return client_for(app)


# ---------------------------------------------------------------------------
# Password hashes


def test_password_hash_roundtrip():
    encoded = hash_password("Geheim ä€", n=2**12)
    scheme, n, r, p, salt, digest = encoded.split("$")
    assert (scheme, n, r, p) == ("scrypt", "4096", "8", "1")
    assert salt and digest
    assert verify_password("Geheim ä€", encoded)
    assert not verify_password("Geheim ae€", encoded)
    assert not verify_password("", encoded)
    assert hash_password("x", n=2**12) != hash_password("x", n=2**12)  # random salt


def test_password_hash_default_parameters_and_unicode_normalization():
    encoded = hash_password("Pässwort")  # decomposed "ä"
    assert encoded.startswith("scrypt$32768$8$1$")
    assert verify_password("Pässwort", encoded)  # composed form matches


def test_password_hash_tolerates_doubled_dollars_and_rejects_garbage():
    encoded = hash_password("pw", n=2**12)
    assert verify_password("pw", encoded.replace("$", "$$"))
    for broken in ("", "plain", "scrypt$1$8$1$abc$def", "bcrypt$4096$8$1$YWJjZGVmZ2g$YWJj", "scrypt$4096$8$1$!!$!!", None):
        assert not verify_password("pw", broken)
    with pytest.raises(ValueError):
        hash_password("")
    with pytest.raises(ValueError):
        hash_password("x", n=1000)


def test_check_local_login(settings):
    hashed = settings.with_overrides(admin_password=None, admin_password_hash=hash_password("richtig", n=2**12))
    assert check_local_login(hashed, "admin", "richtig")
    assert check_local_login(hashed, "ADMIN", "richtig")
    assert not check_local_login(hashed, "admin", "falsch")
    assert not check_local_login(hashed, "root", "richtig")
    plain = settings.with_overrides(admin_password="klartext", admin_password_hash=None)
    assert check_local_login(plain, "admin", "klartext")
    assert not check_local_login(plain, "admin", "klartext ")
    none = settings.with_overrides(admin_password=None, admin_password_hash=None)
    assert not check_local_login(none, "admin", "")


# ---------------------------------------------------------------------------
# Helpers


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/eingang", "/eingang"),
        ("/suche?q=Strom&tag=a", "/suche?q=Strom&tag=a"),
        ("/archiv/Finanzen%2FBank", "/archiv/Finanzen%2FBank"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example/", "/"),
        ("javascript:alert(1)", "/"),
        ("eingang", "/"),
        ("/\t/evil.example", "/"),
        ("/login", "/"),
        ("/login/oidc?next=/x", "/"),
        ("/auth/callback", "/"),
        ("", "/"),
        (None, "/"),
        ("/" + "a" * 3000, "/"),
    ],
)
def test_safe_next(value, expected):
    assert safe_next(value) == expected


def test_oidc_user_allowed(settings):
    rules = settings.with_overrides(allowed_users=("Anna", "max@example.test"), allowed_groups=("Familie",))
    assert oidc_user_allowed(rules, username="anna", email=None, groups=[])
    assert oidc_user_allowed(rules, username="x", email="MAX@example.test", groups=[])
    assert oidc_user_allowed(rules, username="x", email=None, groups=["familie"])
    assert not oidc_user_allowed(rules, username="x", email="x@example.test", groups=["gäste"])
    closed = settings.with_overrides(allowed_users=(), allowed_groups=(), allow_all_oidc_users=False)
    assert not oidc_user_allowed(closed, username="anna", email=None, groups=["familie"])
    everyone = closed.with_overrides(allow_all_oidc_users=True)
    assert oidc_user_allowed(everyone, username="anna", email=None, groups=[])


def test_rate_limiter_blocks_and_recovers():
    now = [1000.0]
    limiter = LoginRateLimiter(max_failures=3, window=60, max_failures_per_address=5, clock=lambda: now[0])
    for _ in range(3):
        assert limiter.retry_after("10.0.0.1", "admin") == 0
        limiter.record_failure("10.0.0.1", "Admin")
    assert limiter.retry_after("10.0.0.1", "admin") == pytest.approx(60)
    # Another address or user name is not affected by the per-user limit.
    assert limiter.retry_after("10.0.0.2", "admin") == 0
    assert limiter.retry_after("10.0.0.1", "anna") == 0
    now[0] += 30
    assert limiter.retry_after("10.0.0.1", "admin") == pytest.approx(30)
    now[0] += 31
    assert limiter.retry_after("10.0.0.1", "admin") == 0
    # The per-address limit counts all user names together.
    limiter.record_failure("10.0.0.1", "a")
    limiter.record_failure("10.0.0.1", "b")
    limiter.record_failure("10.0.0.1", "c")
    limiter.record_failure("10.0.0.1", "d")
    limiter.record_failure("10.0.0.1", "e")
    assert limiter.retry_after("10.0.0.1", "f") > 0
    limiter.reset("10.0.0.1", "a")
    assert limiter.retry_after("10.0.0.1", "f") > 0


def test_rate_limiter_memory_is_bounded():
    limiter = LoginRateLimiter(max_failures=2, window=60, max_keys=50)
    for number in range(500):
        limiter.record_failure(f"10.0.{number // 250}.{number % 250}", "admin")
    assert len(limiter._failures) <= 50


# ---------------------------------------------------------------------------
# Local login


def test_login_page_without_ui_templates_or_with_them(client):
    response = client.get("/login?next=/eingang")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "text/html" in response.headers["content-type"]
    assert session_data(client).get("csrf")


def test_local_login_success(client, base_services):
    response = login_local(client, next_url="/geschuetzt")
    assert response.status_code == 303
    assert response.headers["location"] == "/geschuetzt"
    data = session_data(client)
    assert data["user"]["username"] == "admin"
    assert data["user"]["method"] == "local"
    assert client.get("/geschuetzt").text == "hallo admin"
    status = client.get("/api/v1/status").json()
    assert status["user"]["username"] == "admin"
    entries = base_services.audit.tail(5)
    assert entries[0]["action"] == "login" and entries[0]["user"] == "admin"


def test_local_login_rejects_foreign_next(client):
    response = login_local(client, next_url="https://evil.example/")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_local_login_wrong_password(client):
    response = login_local(client, password="falsch")
    assert response.status_code == 401
    assert "Benutzername oder Passwort ist falsch" in response.text
    assert "user" not in session_data(client)
    assert client.get("/api/v1/status").status_code == 401


def test_local_login_is_rate_limited(client):
    for _ in range(5):
        assert login_local(client, password="falsch").status_code == 401
    blocked = login_local(client)  # even the right password is refused now
    assert blocked.status_code == 429
    assert "Zu viele fehlgeschlagene Anmeldeversuche" in blocked.text
    assert int(blocked.headers["retry-after"]) > 0
    assert "user" not in session_data(client)


def test_successful_login_resets_the_failure_count(client):
    for _ in range(4):
        login_local(client, password="falsch")
    assert login_local(client).status_code == 303
    client.cookies.clear()
    for _ in range(4):
        assert login_local(client, password="falsch").status_code == 401
    assert login_local(client).status_code == 303


def test_local_login_needs_csrf_token(client):
    client.get("/login")
    response = client.post(
        "/login/local", data={"username": "admin", "password": ADMIN_PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 403
    assert "Sitzung ist abgelaufen" in response.text
    assert "user" not in session_data(client)


def test_local_login_with_password_hash(base_services):
    app = make_app(base_services, admin_password=None, admin_password_hash=hash_password("Notfall!", n=2**12))
    client = client_for(app)
    assert login_local(client, password=ADMIN_PASSWORD).status_code == 401
    assert login_local(client, password="Notfall!").status_code == 303


def test_local_login_disabled(base_services):
    app = make_app(base_services, admin_password=None, admin_password_hash=None)
    client = client_for(app)
    response = login_local(client)
    assert response.status_code == 403
    assert "nicht eingerichtet" in response.text


def test_login_clears_previous_session_content(client):
    client.get("/pflanzen")
    before = session_data(client)
    assert before["planted"] == "von außen"
    csrf_before = csrf_for(client)
    login_local(client)
    after = session_data(client)
    assert "planted" not in after
    assert after["csrf"] != csrf_before
    assert set(after) <= {"user", "auth_at", "csrf", "id_token", "cred"}
    assert after["cred"]  # local sessions carry the fingerprint of the emergency password


def test_login_page_redirects_when_logged_in(client):
    login_local(client)
    response = client.get("/login?next=/suche", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/suche"


# ---------------------------------------------------------------------------
# Protected routes, CSRF, logout


def test_html_route_redirects_to_login_with_next(client):
    response = client.get("/geschuetzt?x=1", follow_redirects=False)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query)["next"] == ["/geschuetzt?x=1"]


def test_api_answers_401_json(client):
    response = client.get("/api/v1/status")
    assert response.status_code == 401
    assert response.json()["detail"] == "Anmeldung erforderlich."
    assert response.headers["www-authenticate"] == "Bearer"


def test_csrf_header_and_form_field(client):
    login_local(client)
    token = session_data(client)["csrf"]
    assert client.post("/aktion").status_code == 403
    assert client.post("/aktion", headers={"X-CSRF-Token": "falsch"}).status_code == 403
    assert client.post("/aktion", headers={"X-CSRF-Token": token}).text == "ok local"
    assert client.post("/aktion", data={"csrf_token": token}).text == "ok local"
    assert client.post("/aktion", files={"datei": ("a.txt", b"x")}, data={"csrf_token": token}).text == "ok local"
    assert client.post("/aktion", data={"csrf_token": "falsch"}).status_code == 403


def test_csrf_also_protects_anonymous_forms(client):
    assert client.post("/anonym").status_code == 403
    token = csrf_for(client)
    assert client.post("/anonym", headers={"X-CSRF-Token": token}).text == "ok"


def test_csrf_failure_page_is_german(client):
    login_local(client)
    response = client.post("/aktion", headers={"Accept": "text/html"})
    assert response.status_code == 403
    assert "Sicherheitsprüfung" in response.text


def test_bearer_token_auth_is_exempt_from_csrf(client, base_services):
    token, _entry = base_services.tokens.create("Kurzbefehl", "anna")
    response = client.post("/aktion", headers={"Authorization": f"Bearer {token}"})
    assert response.text == "ok token"
    status = client.get("/api/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert status.json()["user"] == {
        "username": "anna",
        "name": "anna",
        "email": None,
        "groups": [],
        "method": "token",
    }


def test_invalid_bearer_token_is_rejected_even_with_session(client):
    login_local(client)
    response = client.get("/api/v1/status", headers={"Authorization": "Bearer dsk_falsch"})
    assert response.status_code == 401
    assert "API-Token" in response.json()["detail"]
    assert client.post("/aktion", headers={"Authorization": "Bearer dsk_falsch"}).status_code in (401, 403)


def test_logout(client, base_services):
    login_local(client)
    token = session_data(client)["csrf"]
    assert client.post("/logout", follow_redirects=False).status_code == 403
    response = client.post("/logout", headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?logged_out=1"
    assert "user" not in session_data(client)
    assert client.get("/api/v1/status").status_code == 401
    assert base_services.audit.tail(1)[0]["action"] == "logout"
    page = client.get("/login?logged_out=1")
    assert page.status_code == 200


def test_logout_without_session_just_redirects(client):
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?logged_out=1"


def test_session_expires_after_max_age(base_services):
    app = make_app(base_services)
    client = client_for(app)
    secret = app.state.settings.secret_key
    user = {"username": "admin", "name": "admin", "email": None, "groups": [], "method": "local"}
    cred = security.credential_fingerprint(app.state.settings, secret)
    fresh = {"user": user, "auth_at": int(time.time()) - 60, "csrf": "c" * 43, "cred": cred}
    client.cookies.set(SESSION_COOKIE, signed_session(secret, fresh))
    assert client.get("/api/v1/status").status_code == 200
    old = dict(fresh, auth_at=int(time.time()) - app.state.settings.session_max_age - 10)
    client.cookies.set(SESSION_COOKIE, signed_session(secret, old))
    assert client.get("/api/v1/status").status_code == 401


def test_forged_session_cookie_is_ignored(base_services):
    app = make_app(base_services)
    client = client_for(app)
    user = {"username": "admin", "name": "admin", "email": None, "groups": [], "method": "local"}
    client.cookies.set(SESSION_COOKIE, signed_session("other-secret", {"user": user, "auth_at": int(time.time())}))
    assert client.get("/api/v1/status").status_code == 401


def test_local_session_ends_when_local_login_is_switched_off(base_services):
    app = make_app(base_services)
    client = client_for(app)
    login_local(client)
    assert client.get("/api/v1/status").status_code == 200
    app.state.settings = app.state.settings.with_overrides(admin_password=None, admin_password_hash=None)
    assert client.get("/api/v1/status").status_code == 401


# ---------------------------------------------------------------------------
# OIDC


@pytest.fixture
def provider():
    return FakeProvider()


def oidc_app(base_services, provider, **overrides):
    values = provider.settings(allowed_users=("anna",), allowed_groups=(), allow_all_oidc_users=False)
    values.update(overrides)
    app = make_app(base_services, **values)
    add_test_routes(app)
    return app


def test_oidc_login_allowed_by_username(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    with provider.running():
        response = oidc_login(client, provider, next_url="/geschuetzt")
    assert response.status_code == 303, response.headers.get("location")
    assert response.headers["location"] == "/geschuetzt"
    data = session_data(client)
    assert data["user"] == {
        "username": "anna",
        "name": "Anna Beispiel",
        "email": "anna@example.test",
        "groups": ["familie"],
        "method": "oidc",
    }
    assert data["id_token"].count(".") == 2
    assert set(data) == {"user", "auth_at", "csrf", "id_token"}
    assert client.get("/geschuetzt").text == "hallo anna"
    # PKCE and the redirect URI from the configuration (not from request headers).
    headers, form = provider.token_requests[-1]
    assert form["code_verifier"] and form["redirect_uri"] == "http://testserver/auth/callback"
    assert provider.last_authorize["redirect_uri"] == "http://testserver/auth/callback"
    assert "openid" in provider.last_authorize["scope"].split()
    entry = base_services.audit.tail(1)[0]
    assert entry["action"] == "login" and entry["user"] == "anna" and entry["method"] == "oidc"


def test_oidc_redirect_uri_ignores_forwarded_host(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    with provider.running():
        start = client.get(
            "/login/oidc", headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"}, follow_redirects=False
        )
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["redirect_uri"] == ["http://testserver/auth/callback"]


def test_oidc_login_allowed_by_email_and_by_group(base_services, provider):
    app = oidc_app(base_services, provider, allowed_users=("ANNA@example.test",))
    client = client_for(app)
    provider.claims["preferred_username"] = "a.beispiel"
    with provider.running():
        assert oidc_login(client, provider).status_code == 303
    assert session_data(client)["user"]["username"] == "a.beispiel"

    app = oidc_app(base_services, provider, allowed_users=(), allowed_groups=("Familie",))
    client = client_for(app)
    with provider.running():
        response = oidc_login(client, provider)
    assert response.headers["location"] == "/"
    assert session_data(client)["user"]["groups"] == ["familie"]


def test_oidc_groups_from_userinfo(base_services, provider):
    app = oidc_app(base_services, provider, allowed_users=(), allowed_groups=("docushelf",))
    client = client_for(app)
    provider.claims["groups"] = []
    provider.userinfo_claims = {"groups": ["docushelf", "familie"]}
    with provider.running():
        assert oidc_login(client, provider).headers["location"] == "/"
    assert session_data(client)["user"]["groups"] == ["docushelf", "familie"]


def test_oidc_denied_user(base_services, provider):
    app = oidc_app(base_services, provider, allowed_users=("max",), allowed_groups=("admins",))
    client = client_for(app)
    with provider.running():
        response = oidc_login(client, provider, next_url="/eingang")
    location = urlsplit(response.headers["location"])
    assert response.status_code == 303 and location.path == "/login"
    assert parse_qs(location.query) == {"next": ["/eingang"], "error": ["denied"]}
    assert "user" not in session_data(client)
    page = client.get(response.headers["location"])
    assert "nicht freigeschaltet" in page.text
    assert base_services.audit.tail(1)[0]["action"] == "login_denied"


def test_oidc_deny_by_default(base_services, provider):
    app = oidc_app(base_services, provider, allowed_users=(), allowed_groups=())
    client = client_for(app)
    with provider.running():
        response = oidc_login(client, provider)
    assert "error=denied" in response.headers["location"]

    app = oidc_app(base_services, provider, allowed_users=(), allowed_groups=(), allow_all_oidc_users=True)
    client = client_for(app)
    with provider.running():
        assert oidc_login(client, provider).headers["location"] == "/"


def test_oidc_unverified_email_grants_nothing(base_services, provider):
    app = oidc_app(base_services, provider, allowed_users=("anna@example.test",))
    client = client_for(app)
    provider.claims.update(preferred_username="mallory", email_verified=False)
    with provider.running():
        response = oidc_login(client, provider)
    assert "error=denied" in response.headers["location"]

    provider.claims.pop("preferred_username")
    with provider.running():
        response = oidc_login(client, provider)
    assert "error=no_username" in response.headers["location"]


def test_oidc_state_mismatch(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    with provider.running():
        response = oidc_login(client, provider, state="manipuliert")
    assert "error=oidc_state" in response.headers["location"]
    assert "user" not in session_data(client)


def test_oidc_callback_without_started_login(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    with provider.running():
        response = client.get("/auth/callback?code=abc&state=xyz", follow_redirects=False)
    assert "error=oidc_state" in response.headers["location"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"nonce": "falsch"},
        {"aud": "anderer-client"},
        {"iss": "https://evil.example/"},
        {"exp": 1000},
    ],
)
def test_oidc_rejects_invalid_id_tokens(base_services, provider, overrides):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    provider.id_token_overrides = overrides
    with provider.running():
        response = oidc_login(client, provider)
    assert "error=oidc_failed" in response.headers["location"]
    assert "user" not in session_data(client)


def test_oidc_rejects_token_signed_with_another_key(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    original = provider.key
    with provider.running():
        # Same key id, different key material; the JWKS still publishes the original key.
        provider.key = FakeProvider().key
        response = oidc_login(client, provider)
    provider.key = original
    assert "error=oidc_failed" in response.headers["location"]


def test_oidc_requires_an_id_token(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    provider.issue_id_token = False
    with provider.running():
        response = oidc_login(client, provider)
    assert "error=oidc_failed" in response.headers["location"]


def test_oidc_userinfo_subject_must_match(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    provider.userinfo_claims = {"sub": "someone-else"}
    with provider.running():
        response = oidc_login(client, provider)
    assert "error=oidc_failed" in response.headers["location"]


def test_oidc_userinfo_failure_falls_back_to_id_token(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    provider.userinfo_status = 500
    with provider.running():
        response = oidc_login(client, provider)
    assert response.headers["location"] == "/"
    assert session_data(client)["user"]["username"] == "anna"


def test_oidc_cancelled_at_provider(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    with provider.running():
        start = client.get("/login/oidc", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        response = client.get(f"/auth/callback?error=access_denied&state={state}", follow_redirects=False)
    assert "error=oidc_cancelled" in response.headers["location"]


def test_oidc_provider_unreachable(base_services, provider):
    import httpx
    import respx

    app = oidc_app(base_services, provider)
    client = client_for(app)
    with respx.mock() as router:
        router.get(provider.discovery_url).mock(side_effect=httpx.ConnectError("down"))
        response = client.get("/login/oidc", follow_redirects=False)
    assert response.status_code == 303
    assert "error=oidc_unavailable" in response.headers["location"]


def test_oidc_disabled(client):
    response = client.get("/login/oidc", follow_redirects=False)
    assert "error=oidc_disabled" in response.headers["location"]
    response = client.get("/auth/callback?code=x&state=y", follow_redirects=False)
    assert "error=oidc_disabled" in response.headers["location"]


def test_oidc_public_client_without_secret(base_services):
    provider = FakeProvider(client_secret=None)
    app = oidc_app(base_services, provider)
    client = client_for(app)
    with provider.running():
        assert oidc_login(client, provider).headers["location"] == "/"
    headers, form = provider.token_requests[-1]
    assert "authorization" not in {key.lower() for key in headers}
    assert form["client_id"] == provider.client_id


def test_oidc_logout_at_provider(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    with provider.running():
        oidc_login(client, provider)
        id_token = session_data(client)["id_token"]
        token = session_data(client)["csrf"]
        response = client.post("/logout", headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == provider.metadata["end_session_endpoint"]
    query = parse_qs(location.query)
    assert query["id_token_hint"] == [id_token]
    assert query["client_id"] == [provider.client_id]
    assert query["post_logout_redirect_uri"] == ["http://testserver/login?logged_out=1"]
    assert "user" not in session_data(client)


def test_oidc_session_ends_when_user_is_no_longer_allowed(base_services, provider):
    app = oidc_app(base_services, provider)
    client = client_for(app)
    with provider.running():
        oidc_login(client, provider)
    assert client.get("/api/v1/status").status_code == 200
    app.state.settings = app.state.settings.with_overrides(allowed_users=("max",))
    assert client.get("/api/v1/status").status_code == 401
    assert "user" not in session_data(client)


def test_large_group_lists_keep_the_cookie_small(base_services, provider):
    app = oidc_app(base_services, provider, allowed_users=(), allowed_groups=("docushelf",))
    client = client_for(app)
    provider.claims["groups"] = ["docushelf"] + [f"gruppe-mit-einem-langen-namen-{n:04d}" for n in range(200)]
    with provider.running():
        assert oidc_login(client, provider).headers["location"] == "/"
    assert len(client.cookies.get(SESSION_COOKIE)) < 4000
    assert session_data(client)["user"]["groups"] == ["docushelf"]
    assert client.get("/api/v1/status").status_code == 200


def test_csp_allows_the_identity_provider_for_form_redirects(base_services, provider):
    app = oidc_app(base_services, provider)
    policy = client_for(app).get("/login").headers["content-security-policy"]
    assert policy.endswith("form-action 'self' https://idp.example.test")


# ---------------------------------------------------------------------------
# Security headers, error pages, health check

EXPECTED_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; frame-src 'self'; object-src 'self'; "
    "manifest-src 'self'; worker-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
)


@pytest.mark.parametrize("path", ["/healthz", "/login", "/gibt-es-nicht", "/api/v1/status", "/api/v1/gibt-es-nicht"])
def test_security_headers(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "unsafe-inline" not in response.headers["content-security-policy"]


def test_session_cookie_flags(client):
    cookie_header = login_local(client).headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE}=" in cookie_header
    assert "httponly" in cookie_header.lower()
    assert "samesite=lax" in cookie_header.lower()
    assert "secure" not in cookie_header.lower()  # base_url is http in tests


def test_secure_cookie_with_https_base_url(base_services):
    app = make_app(base_services, base_url="https://docs.example.test")
    response = TestClientHTTPS(app).get("/login")
    assert "secure" in response.headers["set-cookie"].lower()


def TestClientHTTPS(app):  # noqa: N802 - mirrors the TestClient name
    from starlette.testclient import TestClient

    return TestClient(app, base_url="https://docs.example.test")


def test_unknown_page_shows_german_error_page(client):
    response = client.get("/gibt-es-nicht", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "nicht gefunden" in response.text


def test_unknown_api_route_is_json(client):
    response = client.get("/api/v1/gibt-es-nicht")
    assert response.status_code in (401, 404)
    assert response.headers["content-type"].startswith("application/json")


def test_unexpected_errors_render_500_with_headers(base_services):
    app = make_app(base_services)

    @app.get("/kaputt")
    async def broken():
        raise RuntimeError("boom")

    from starlette.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/kaputt", headers={"Accept": "text/html"})
    assert response.status_code == 500
    assert "interner Fehler" in response.text
    assert "boom" not in response.text
    assert response.headers["content-security-policy"]


def test_too_large_body_is_refused_before_reading(base_services):
    app = make_app(base_services, max_upload_mb=1)
    client = client_for(app)
    response = client.post(
        "/login/local",
        content=b"x" * (3 * 1024 * 1024),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413
    assert "zu groß" in response.text


def test_healthz_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "data_dir": "ok", "archive": "ok"}}
    assert response.headers["cache-control"] == "no-store"
    assert not [p for p in client.app.state.services.layout.data.iterdir() if p.name.startswith(".docushelf-")]


def test_healthz_reports_a_broken_database(base_services):
    class BrokenIndex:
        def conn(self):
            raise RuntimeError("database is locked")

    base_services.index = BrokenIndex()
    response = client_for(make_app(base_services)).get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error" and body["checks"]["database"].startswith("Fehler")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_healthz_reports_read_only_data_dir_and_missing_archive(base_services, tmp_path):
    data = base_services.layout.data
    mode = stat.S_IMODE(os.stat(data).st_mode)
    base_services.tokens = None
    base_services.layout = SimpleNamespace(data=data, archive=tmp_path / "fehlt")
    os.chmod(data, 0o500)
    try:
        response = client_for(make_app(base_services)).get("/healthz")
    finally:
        os.chmod(data, mode)
    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["data_dir"].startswith("Fehler")
    assert checks["archive"].startswith("Fehler")
    assert checks["database"] == "ok"


def test_rendering_uses_the_plain_page_without_templates(monkeypatch, base_services):
    from docushelf.web import auth

    monkeypatch.setattr(auth, "_TEMPLATING_MODULE", "docushelf.web.gibt_es_nicht")
    client = client_for(make_app(base_services))
    page = client.get("/login?next=/eingang")
    assert 'name="csrf_token"' in page.text and 'value="/eingang"' in page.text
    assert "<script" not in page.text and "style=" not in page.text
    error = client.get("/fehlt", headers={"Accept": "text/html"})
    assert error.status_code == 404 and "nicht gefunden" in error.text


def test_user_from_session_rejects_garbage():
    assert security.User.from_session(None) is None
    assert security.User.from_session({"username": "", "method": "local"}) is None
    assert security.User.from_session({"username": "a", "method": "token"}) is None
    user = security.User.from_session({"username": "a", "method": "oidc", "groups": ["x", 3]})
    assert user == User(username="a", name="a", email=None, groups=("x",), method="oidc")


def test_rate_limiter_caps_one_account_across_addresses():
    limiter = LoginRateLimiter(max_failures=5, window=60, max_failures_per_address=20, max_failures_per_account=8)
    for number in range(8):
        limiter.record_failure(f"10.1.0.{number}", "admin")
    assert limiter.retry_after("10.9.9.9", "admin") > 0
    assert limiter.retry_after("10.9.9.9", "anna") == 0


def test_chunked_body_over_the_limit_is_refused(base_services):
    app = make_app(base_services, max_upload_mb=1)
    client = client_for(app)

    def body():
        for _ in range(3):
            yield b"x" * (1024 * 1024)

    response = client.post("/login/local", content=body(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413
    assert "zu groß" in response.text


def test_client_disconnect_during_upload_is_not_an_error(base_services, caplog):
    import anyio

    app = make_app(base_services)
    token, _entry = base_services.tokens.create("Test", "anna")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/upload",
        "raw_path": b"/api/v1/upload",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"multipart/form-data; boundary=xyz"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    messages = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    async def run():
        await app(scope, receive, send)

    with caplog.at_level("INFO", logger="docushelf.web.app"):
        anyio.run(run)
    assert messages[0]["type"] == "http.response.start" and messages[0]["status"] == 400
    assert any("client went away" in record.getMessage() for record in caplog.records)
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_ui_pages_redirect_to_login(client):
    import importlib.util

    if importlib.util.find_spec("docushelf.web.routes_ui") is None:
        pytest.skip("web interface not available")
    response = client.get("/eingang", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Feingang"
    login_local(client)
    page = client.get("/eingang")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
