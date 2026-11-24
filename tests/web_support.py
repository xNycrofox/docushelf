"""Helpers for the web tests: app factory, session cookie access and a fake OIDC provider."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
import itsdangerous
import respx
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from starlette.testclient import TestClient

from docushelf.web.app import SESSION_COOKIE, create_app
from docushelf.web.tokens import TokenStore

ADMIN_PASSWORD = "test-admin-pw"  # see tests/conftest.py


def make_app(services: Any, settings: Any = None, **overrides: Any):
    """An application on top of ``services`` (real or fake); settings overrides are applied."""
    settings = settings or services.settings
    if overrides:
        settings = settings.with_overrides(**overrides)
    services.settings = settings
    if not hasattr(services, "tokens"):
        services.tokens = TokenStore(services.layout.tokens_file)
    return create_app(settings, services=services)


def client_for(app) -> TestClient:
    return TestClient(app, base_url="http://testserver")


def session_data(client: TestClient) -> dict[str, Any]:
    raw = client.cookies.get(SESSION_COOKIE)
    if not raw or raw == "null":
        return {}
    payload = raw.strip('"').split(".")[0]
    return json.loads(base64.b64decode(payload))


def signed_session(secret_key: str, data: dict[str, Any]) -> str:
    """A session cookie value as SessionMiddleware would write it."""
    payload = base64.b64encode(json.dumps(data).encode("utf-8"))
    return itsdangerous.TimestampSigner(secret_key).sign(payload).decode("utf-8")


def csrf_for(client: TestClient) -> str:
    """Make sure the session has a CSRF token and return it."""
    token = session_data(client).get("csrf")
    if not token:
        client.get("/login")
        token = session_data(client).get("csrf")
    assert token, "the login page did not create a CSRF token"
    return token


def login_local(client: TestClient, *, username: str = "admin", password: str = ADMIN_PASSWORD, next_url: str | None = None):
    token = csrf_for(client)
    form = {"username": username, "password": password, "csrf_token": token}
    if next_url is not None:
        form["next"] = next_url
    return client.post("/login/local", data=form, follow_redirects=False)


# ---------------------------------------------------------------------------
# Fake OpenID Connect provider


class FakeProvider:
    """A minimal OIDC provider answering through respx: discovery, JWKS, token, userinfo."""

    host = "https://idp.example.test"
    issuer = "https://idp.example.test/application/o/docushelf/"
    client_id = "docushelf"

    def __init__(self, client_secret: str | None = "geheim"):
        self.client_secret = client_secret
        self.key = RSAKey.generate_key(2048, parameters={"kid": "key-1", "alg": "RS256", "use": "sig"})
        self.claims: dict[str, Any] = {
            "sub": "user-anna",
            "preferred_username": "anna",
            "email": "anna@example.test",
            "email_verified": True,
            "name": "Anna Beispiel",
            "groups": ["familie"],
        }
        self.userinfo_claims: dict[str, Any] = {}
        self.id_token_overrides: dict[str, Any] = {}
        self.userinfo_status = 200
        self.issue_id_token = True
        self.pending: dict[str, dict[str, str]] = {}
        self.token_requests: list[tuple[dict[str, str], dict[str, str]]] = []
        self.last_authorize: dict[str, str] = {}

    # -- URLs --------------------------------------------------------------------
    @property
    def discovery_url(self) -> str:
        return self.issuer + ".well-known/openid-configuration"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.host + "/application/o/authorize/",
            "token_endpoint": self.host + "/application/o/token/",
            "userinfo_endpoint": self.host + "/application/o/userinfo/",
            "jwks_uri": self.issuer + "jwks/",
            "end_session_endpoint": self.issuer + "end-session/",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
        }

    def settings(self, **overrides: Any) -> dict[str, Any]:
        values = {
            "oidc_issuer": self.issuer,
            "oidc_client_id": self.client_id,
            "oidc_client_secret": self.client_secret,
        }
        values.update(overrides)
        return values

    # -- endpoints ---------------------------------------------------------------
    def authorize(self, location: str) -> dict[str, str]:
        """The user logs in at the provider; returns the parameters of the callback."""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == self.metadata["authorization_endpoint"]
        query = dict(parse_qsl(parts.query))
        assert query["response_type"] == "code"
        assert query["client_id"] == self.client_id
        assert query["code_challenge_method"] == "S256"
        assert query.get("nonce") and query.get("state") and query.get("code_challenge")
        self.last_authorize = query
        code = secrets.token_urlsafe(16)
        self.pending[code] = query
        return {"code": code, "state": query["state"]}

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode("utf-8")))
        self.token_requests.append((dict(request.headers), form))
        if self.client_secret:
            expected = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            if request.headers.get("authorization") != f"Basic {expected}":
                return httpx.Response(401, json={"error": "invalid_client"})
        elif form.get("client_id") != self.client_id:
            return httpx.Response(401, json={"error": "invalid_client"})
        data = self.pending.pop(form.get("code", ""), None)
        if data is None or form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier = form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge != data["code_challenge"] or form.get("redirect_uri") != data["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        body: dict[str, Any] = {"access_token": "at-" + secrets.token_hex(8), "token_type": "Bearer", "expires_in": 300}
        if self.issue_id_token:
            claims = {"iss": self.issuer, "aud": self.client_id, "iat": now, "exp": now + 300, "nonce": data["nonce"]}
            claims.update(self.claims)
            claims.update(self.id_token_overrides)
            body["id_token"] = jwt.encode({"alg": "RS256", "kid": "key-1"}, claims, self.key)
        return httpx.Response(200, json=body)

    def _userinfo(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("authorization", "").startswith("Bearer at-"):
            return httpx.Response(401)
        if self.userinfo_status != 200:
            return httpx.Response(self.userinfo_status)
        data = dict(self.claims)
        data.update(self.userinfo_claims)
        return httpx.Response(200, json=data)

    @contextmanager
    def running(self) -> Iterator[respx.MockRouter]:
        with respx.mock(assert_all_called=False) as router:
            router.get(self.discovery_url).mock(return_value=httpx.Response(200, json=self.metadata))
            router.get(self.metadata["jwks_uri"]).mock(
                return_value=httpx.Response(200, json=KeySet([self.key]).as_dict(private=False))
            )
            router.post(self.metadata["token_endpoint"]).mock(side_effect=self._token)
            router.get(self.metadata["userinfo_endpoint"]).mock(side_effect=self._userinfo)
            yield router


def oidc_login(client: TestClient, provider: FakeProvider, *, next_url: str | None = None, state: str | None = None):
    """Run the whole authorization code flow; returns the response of the callback."""
    start = client.get("/login/oidc" + (f"?next={quote(next_url)}" if next_url else ""), follow_redirects=False)
    assert start.status_code == 302, start.text
    params = provider.authorize(start.headers["location"])
    if state is not None:
        params["state"] = state
    return client.get("/auth/callback", params=params, follow_redirects=False)
