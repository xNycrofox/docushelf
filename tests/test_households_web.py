"""Households in the web layer: every request works on exactly one household.

Two households share one installation: "familie-a" lives in the data root,
"eltern" in ``haushalte/eltern``. Anna belongs to Familie A, Opa to Eltern,
Berta to both, Zoe to none; the local emergency account may use both. Nothing
of one household may be visible from the other: inbox, archive, documents and
their files and page images, search and autocomplete, trash, rules, tokens,
audit and automatic filings, settings, uploads.
"""

from __future__ import annotations

import re
import time
from datetime import date
from types import SimpleNamespace

import pytest
import yaml

from archive_support import add_archive_pdf, make_ready_item
from docushelf.config import load_settings
from docushelf.households import Registry
from docushelf.models import FilingRequest
from docushelf.suggest.rules import Rule
from docushelf.web import routes_ui
from docushelf.web.app import SESSION_COOKIE, create_app
from web_support import FakeProvider, client_for, login_local, oidc_login, session_data, signed_session

HOUSEHOLDS = [
    {"id": "familie-a", "name": "Familie A", "path": ".", "groups": ["familie-a"]},
    {"id": "eltern", "name": "Eltern", "groups": ["eltern"], "users": ["opa"]},
]
USERS = {
    "anna": ["familie-a"],
    "opa": [],
    "berta": ["familie-a", "eltern"],
    "zoe": ["sonstwer"],
}


def _rule(name: str, folder: str) -> Rule:
    return Rule.from_dict({"name": name, "match": {"sender": [name]}, "action": {"folder": folder}})


def _fill(services, samples, tmp_path, word: str, folder: str) -> SimpleNamespace:
    """Documents, an inbox item, a trash entry, a rule, a token and an automatic filing."""
    tmp = tmp_path / word
    doc = add_archive_pdf(services, samples, f"{folder}/2026-01-05 {word} Brief.pdf", text=f"{word} Brief Inhalt")
    doomed = add_archive_pdf(services, samples, f"{folder}/2026-01-06 {word} Alt.pdf", text=f"{word} alt")
    trash_id = services.ops.delete_document(doomed.id, user="test")
    item = make_ready_item(services, samples, tmp, text=f"{word} Eingang")
    filed_item = make_ready_item(services, samples, tmp, text=f"{word} Automatisch")
    auto = services.ops.file_item(
        filed_item.id,
        FilingRequest(folder=folder, title=f"{word} Automatisch", doc_date=date(2026, 1, 7)),
        user="automatisch",
        auto={"confidence": 0.9, "review": True, "sources": ["rule"], "reasons": [f"{word} Regel"]},
    )
    services.rules.add(_rule(f"{word}Regel", folder))
    token, entry = services.tokens.create(f"{word}-Telefon", "admin")
    return SimpleNamespace(
        doc=doc, trash_id=trash_id, item=item, auto=auto, token=token, token_id=entry["id"], word=word, folder=folder
    )


@pytest.fixture
def two(env, data_dir, tmp_path, samples, monkeypatch):
    path = data_dir / "config" / "households.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"households": HOUSEHOLDS}), encoding="utf-8")
    provider = FakeProvider()
    settings = load_settings(env).with_overrides(**provider.settings())
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda s: routes_ui._OcrInfo(False, None, None, [], []))
    registry = Registry(settings)
    a, b = registry.services("familie-a"), registry.services("eltern")
    app = create_app(settings, registry=registry)
    data = SimpleNamespace(
        app=app,
        registry=registry,
        provider=provider,
        a=a,
        b=b,
        da=_fill(a, samples, tmp_path, "Alphaversicherung", "Alpha/Versicherung"),
        db=_fill(b, samples, tmp_path, "Betakasse", "Beta/Kasse"),
    )
    yield data
    registry.stop()
    registry.close()


def browser(two, name: str):
    """A browser logged in via SSO as ``name``."""
    client = client_for(two.app)
    two.provider.claims.update(
        {
            "sub": f"user-{name}",
            "preferred_username": name,
            "email": f"{name}@example.test",
            "name": name.title(),
            "groups": USERS[name],
        }
    )
    with two.provider.running():
        response = oidc_login(client, two.provider)
    assert response.status_code == 303, response.headers.get("location")
    return client


def csrf(client) -> str:
    return session_data(client)["csrf"]


def api(client, method: str, url: str, **kwargs):
    headers = {"X-CSRF-Token": csrf(client), **kwargs.pop("headers", {})}
    return client.request(method, url, headers=headers, **kwargs)


# ---------------------------------------------------------------------------
# Isolation


def test_households_live_in_their_own_directories(two, data_dir):
    assert two.a.layout.data == data_dir
    assert two.b.layout.data == data_dir / "haushalte" / "eltern"
    assert (data_dir / "archiv" / "Alpha").is_dir() and not (data_dir / "archiv" / "Beta").exists()
    assert (data_dir / "haushalte" / "eltern" / "archiv" / "Beta").is_dir()
    assert two.a.tokens.verify(two.db.token) is None and two.b.tokens.verify(two.da.token) is None
    assert two.a.tokens.verify(two.da.token) is not None and two.b.tokens.verify(two.db.token) is not None


def test_inbox_and_archive_show_only_the_own_household(two):
    anna, opa = browser(two, "anna"), browser(two, "opa")
    assert session_data(anna)["household"] == "familie-a"
    assert session_data(opa)["household"] == "eltern"
    for client, own, other in ((anna, two.da, two.db), (opa, two.db, two.da)):
        inbox = client.get("/eingang").text
        assert own.item.id in inbox and other.item.id not in inbox
        assert client.get(f"/eingang/{own.item.id}").status_code == 200
        assert client.get(f"/eingang/{other.item.id}").status_code == 404
        listed = client.get("/api/v1/inbox").json()
        assert own.item.id in str(listed) and other.item.id not in str(listed)
        for suffix in ("", "/document", "/pages/1.png", "/original/1"):
            response = client.get(f"/api/v1/inbox/{other.item.id}{suffix}")
            assert response.status_code == 404, (suffix, response.status_code)
            assert other.word not in response.text
        response = api(client, "POST", f"/api/v1/inbox/{other.item.id}/file", json={"folder": "X", "title": "Y"})
        assert response.status_code == 404
        assert client.post(
            f"/eingang/{other.item.id}/loeschen", data={"csrf_token": csrf(client)}, follow_redirects=False
        ).status_code in (303, 404)
        assert (other is two.db and two.b or two.a).inbox.load(other.item.id).id == other.item.id

        archive = client.get("/archiv").text
        assert own.folder.split("/")[0] in archive and other.folder.split("/")[0] not in archive
        assert other.word not in client.get(f"/archiv/{own.folder}").text
        assert other.word not in client.get(f"/archiv/{other.folder}").text
        folders = client.get("/api/v1/folders").text
        assert own.folder.split("/")[0] in folders and other.folder.split("/")[0] not in folders


def test_documents_files_and_page_images_of_another_household_are_404(two):
    anna = browser(two, "anna")
    own, other = two.da.doc.id, two.db.doc.id
    assert anna.get(f"/dokument/{own}").status_code == 200
    assert anna.get(f"/api/v1/documents/{own}/file").status_code == 200
    assert anna.get(f"/api/v1/documents/{own}/pages/1.png").status_code == 200
    for url in (
        f"/dokument/{other}",
        f"/api/v1/documents/{other}",
        f"/api/v1/documents/{other}?text=1",
        f"/api/v1/documents/{other}/file",
        f"/api/v1/documents/{other}/pages/1.png",
    ):
        response = anna.get(url)
        assert response.status_code == 404, url
        assert "Betakasse" not in response.text and "Beta/Kasse" not in response.text
    for method, url, kwargs in (
        ("PATCH", f"/api/v1/documents/{other}", {"json": {"title": "Übernommen"}}),
        ("DELETE", f"/api/v1/documents/{other}", {}),
        ("POST", f"/api/v1/auto/{other}/ok", {}),
    ):
        assert api(anna, method, url, **kwargs).status_code == 404, url
    token = csrf(anna)
    for url in (f"/dokument/{other}/bearbeiten", f"/dokument/{other}/loeschen"):
        # Answered like a document that no longer exists.
        response = anna.post(url, data={"csrf_token": token, "title": "X"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/archiv", url
        assert "Betakasse" not in anna.get("/archiv").text
    assert two.b.index.get(other).title == two.db.doc.title
    assert (two.b.layout.archive / two.db.doc.path).is_file()


def test_search_and_autocomplete_stay_in_the_household(two):
    anna = browser(two, "anna")
    assert "Alphaversicherung" in anna.get("/suche", params={"q": "Alphaversicherung"}).text
    page = anna.get("/suche", params={"q": "Betakasse"}).text
    assert two.db.doc.id not in page and "Beta/Kasse" not in page
    hits = anna.get("/api/v1/search", params={"q": "Betakasse"}).json()
    assert hits.get("total", 0) == 0 and not hits.get("hits")
    assert anna.get("/api/v1/search", params={"q": "Alphaversicherung"}).json()["total"] >= 1
    terms = anna.get("/api/v1/suggest", params={"prefix": "beta"}).json()["terms"]
    assert not any("betakasse" in str(term).lower() for term in terms)
    assert any("alphaversicherung" in str(t).lower() for t in anna.get("/api/v1/suggest", params={"prefix": "alpha"}).json()["terms"])


def test_trash_rules_tokens_audit_and_automatic_filings_stay_apart(two):
    anna = browser(two, "anna")
    trash = anna.get("/papierkorb").text
    assert "Alphaversicherung Alt" in trash and "Betakasse" not in trash
    assert two.db.trash_id not in anna.get("/api/v1/trash").text
    token = csrf(anna)
    response = anna.post(f"/papierkorb/{two.db.trash_id}/wiederherstellen", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code in (303, 404)
    assert api(anna, "POST", f"/api/v1/trash/{two.db.trash_id}/restore").status_code == 404
    assert api(anna, "DELETE", f"/api/v1/trash/{two.db.trash_id}").status_code in (403, 404)
    assert two.db.trash_id in {entry.id for entry in two.b.trash.list()}

    rules = anna.get("/regeln").text
    assert "AlphaversicherungRegel" in rules and "BetakasseRegel" not in rules
    assert "BetakasseRegel" not in anna.get("/api/v1/rules").text

    tokens = anna.get("/api/v1/tokens").text
    assert "Alphaversicherung-Telefon" in tokens and "Betakasse-Telefon" not in tokens
    assert api(anna, "DELETE", f"/api/v1/tokens/{two.db.token_id}").status_code == 404
    assert two.b.tokens.verify(two.db.token) is not None

    settings = anna.get("/einstellungen/handy").text
    assert "Alphaversicherung-Telefon" in settings and "Betakasse-Telefon" not in settings
    assert "Familie A" in settings
    assert "Eltern" not in settings and "haushalte/eltern" not in settings
    assert anna.get("/einstellungen/haushalte").status_code == 403

    audit = anna.get("/api/v1/audit").text
    assert two.da.auto.id in audit and two.db.auto.id not in audit
    auto = anna.get("/eingang/automatisch").text
    assert two.da.auto.id in auto and two.db.auto.id not in auto
    response = anna.post(f"/eingang/automatisch/{two.db.auto.id}/passt", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code in (303, 404)
    assert not any(e.get("doc_id") == two.db.auto.id and e["action"] == "auto_review" for e in two.b.audit.tail(50))


def test_rule_and_token_changes_go_to_the_current_household(two):
    berta = browser(two, "berta")
    token = csrf(berta)
    assert berta.post("/haushalt", data={"csrf_token": token, "id": "eltern"}, follow_redirects=False).status_code == 303
    response = berta.post("/einstellungen/tokens", data={"csrf_token": token, "name": "Bertas Handy"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Bertas Handy" in str(two.b.tokens.list()) and "Bertas Handy" not in str(two.a.tokens.list())
    response = berta.post(
        "/regeln/neu",
        data={"csrf_token": token, "name": "ElternRegel", "sender": "Rentenkasse", "folder": "Rente"},
        follow_redirects=False,
    )
    assert response.status_code in (303, 400)
    assert "ElternRegel" not in two.a.rules.raw_text()
    assert "token_create" in {e.get("action") for e in two.b.audit.tail(10)}
    assert not any(e.get("action") == "token_create" for e in two.a.audit.tail(50))


def test_share_and_api_upload_land_in_the_own_household(two, samples, tmp_path):
    opa = browser(two, "opa")
    pdf = samples.text_pdf(tmp_path / "share" / "Rente.pdf", ["Rentenbescheid"])
    response = opa.post(
        "/teilen",
        files={"files": ("Rente.pdf", pdf.read_bytes(), "application/pdf")},
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    uploaded_b = list(two.b.layout.source_dir("upload").rglob("Rente.pdf"))
    uploaded_a = list(two.a.layout.source_dir("upload").rglob("Rente.pdf"))
    assert uploaded_b and not uploaded_a

    client = client_for(two.app)
    response = client.post(
        "/api/v1/upload",
        files={"files": ("Police.pdf", pdf.read_bytes(), "application/pdf")},
        headers={"Authorization": f"Bearer {two.da.token}"},
    )
    assert response.status_code == 202, response.text
    assert list(two.a.layout.source_dir("upload").rglob("Police.pdf"))
    assert not list(two.b.layout.source_dir("upload").rglob("Police.pdf"))


def test_a_page_of_the_previous_household_cannot_act_after_a_switch(two, samples, tmp_path):
    berta = browser(two, "berta")
    token = csrf(berta)
    page = berta.get("/regeln")
    assert page.status_code == 200
    assert '<meta name="household" content="familie-a">' in page.text
    assert '<input type="hidden" name="household" value="familie-a">' in page.text
    # Another tab switches to the parents' household.
    assert berta.post("/haushalt", data={"csrf_token": token, "id": "eltern"}, follow_redirects=False).status_code == 303

    # The old form (Familie A) must not create a rule in the parents' household.
    response = berta.post(
        "/regeln/neu",
        data={"csrf_token": token, "household": "familie-a", "name": "AltRegel", "sender": "Stadtwerke", "folder": "Strom"},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "AltRegel" not in two.b.rules.raw_text() and "AltRegel" not in two.a.rules.raw_text()

    # An upload from the old page (JavaScript sends the household header) is refused as well.
    pdf = samples.text_pdf(tmp_path / "stale" / "Privat.pdf", ["Privat"])
    response = api(
        berta,
        "POST",
        "/api/v1/upload",
        files={"files": ("Privat.pdf", pdf.read_bytes(), "application/pdf")},
        headers={"X-DocuShelf-Household": "familie-a"},
    )
    assert response.status_code == 409
    assert not list(two.b.layout.source_dir("upload").rglob("Privat.pdf"))
    assert not list(two.a.layout.source_dir("upload").rglob("Privat.pdf"))

    # A page of the current household works.
    response = api(
        berta,
        "POST",
        "/api/v1/upload",
        files={"files": ("Privat.pdf", pdf.read_bytes(), "application/pdf")},
        headers={"X-DocuShelf-Household": "eltern"},
    )
    assert response.status_code == 202, response.text
    assert list(two.b.layout.source_dir("upload").rglob("Privat.pdf"))
    assert not list(two.a.layout.source_dir("upload").rglob("Privat.pdf"))


# ---------------------------------------------------------------------------
# API tokens


def test_a_token_only_reaches_its_own_household(two):
    client = client_for(two.app)
    auth_a = {"Authorization": f"Bearer {two.da.token}"}
    auth_b = {"Authorization": f"Bearer {two.db.token}"}
    assert client.get(f"/api/v1/documents/{two.da.doc.id}", headers=auth_a).status_code == 200
    for url in (
        f"/api/v1/documents/{two.db.doc.id}",
        f"/api/v1/documents/{two.db.doc.id}/file",
        f"/api/v1/documents/{two.db.doc.id}/pages/1.png",
        f"/api/v1/inbox/{two.db.item.id}",
    ):
        response = client.get(url, headers=auth_a)
        assert response.status_code == 404, url
        assert "Betakasse" not in response.text
    assert client.get(f"/api/v1/documents/{two.db.doc.id}", headers=auth_b).status_code == 200
    assert client.get(f"/api/v1/documents/{two.da.doc.id}", headers=auth_b).status_code == 404
    assert client.get("/api/v1/search", params={"q": "Betakasse"}, headers=auth_a).json()["total"] == 0
    status = client.get("/api/v1/status", headers=auth_b).json()
    assert status["household"] == {"id": "eltern", "name": "Eltern"}
    # A token cannot switch households or manage tokens.
    response = client.post("/haushalt", data={"id": "eltern"}, headers=auth_a, follow_redirects=False)
    assert response.status_code == 403
    listed = client.get("/api/v1/tokens", headers=auth_a).text
    assert "Alphaversicherung-Telefon" in listed and "Betakasse-Telefon" not in listed
    assert client.post("/api/v1/tokens", json={"name": "Mehr"}, headers=auth_a).status_code == 403
    assert client.get("/api/v1/documents/x", headers={"Authorization": "Bearer falsch"}).status_code == 401


def test_a_token_stops_working_when_its_owner_leaves_the_household(two):
    anna = browser(two, "anna")
    response = anna.post("/einstellungen/tokens", data={"csrf_token": csrf(anna), "name": "Annas Handy"}, follow_redirects=False)
    assert response.status_code == 303
    secret = anna.get("/einstellungen/handy").text
    token = re.search(r"dsk_[A-Za-z0-9_\-]{20,}", secret)
    assert token, "the new token is shown once"
    client = client_for(two.app)
    headers = {"Authorization": f"Bearer {token.group(0)}"}
    assert client.get("/api/v1/status", headers=headers).status_code == 200
    entry = next(e for e in two.a.tokens.list() if e["name"] == "Annas Handy")
    assert entry["groups"] == ["familie-a"]
    two.a.tokens._write(  # the owner snapshot now names a group no household grants
        [dict(e, groups=["sonstwer"]) if e["id"] == entry["id"] else e for e in two.a.tokens._editable()]
    )
    assert client.get("/api/v1/status", headers=headers).status_code == 401


# ---------------------------------------------------------------------------
# Switching households


def test_switcher_only_for_users_with_several_households(two):
    anna, berta = browser(two, "anna"), browser(two, "berta")
    assert 'action="/haushalt"' not in anna.get("/eingang").text
    page = berta.get("/eingang").text
    assert 'action="/haushalt"' in page and 'name="id" value="eltern"' in page and "Familie A" in page
    assert two.da.item.id in page

    token = csrf(berta)
    assert berta.post("/haushalt", data={"id": "eltern"}, follow_redirects=False).status_code == 403  # no CSRF token
    response = berta.post("/haushalt", data={"csrf_token": token, "id": "eltern"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/eingang"
    assert session_data(berta)["household"] == "eltern"
    page = berta.get("/eingang").text
    assert two.db.item.id in page and two.da.item.id not in page
    assert "Haushalt „Eltern“" in page
    assert berta.get(f"/dokument/{two.da.doc.id}").status_code == 404
    assert berta.get(f"/dokument/{two.db.doc.id}").status_code == 200

    response = berta.post("/haushalt", data={"csrf_token": token, "id": "gibtsnicht"}, follow_redirects=False)
    assert response.status_code == 404 and session_data(berta)["household"] == "eltern"


def test_switching_to_a_foreign_household_is_refused(two):
    anna = browser(two, "anna")
    response = anna.post("/haushalt", data={"csrf_token": csrf(anna), "id": "eltern"}, follow_redirects=False)
    assert response.status_code == 404
    assert "Betakasse" not in response.text
    assert session_data(anna)["household"] == "familie-a"
    assert two.da.item.id in anna.get("/eingang").text


def test_a_stale_household_choice_falls_back_to_a_permitted_one(two):
    data = session_data(browser(two, "anna"))
    data["household"] = "eltern"  # e.g. access was taken away since the choice was made
    anna = client_for(two.app)
    anna.cookies.set(SESSION_COOKIE, signed_session(two.app.state.session_secret, data), domain="testserver.local")
    page = anna.get("/eingang").text
    assert two.da.item.id in page and two.db.item.id not in page
    assert session_data(anna)["household"] == "familie-a"


def test_local_emergency_account_may_use_every_household(two):
    admin = client_for(two.app)
    assert login_local(admin).status_code == 303
    page = admin.get("/eingang").text
    assert two.da.item.id in page and 'name="id" value="eltern"' in page
    response = admin.post("/haushalt", data={"csrf_token": csrf(admin), "id": "eltern"}, follow_redirects=False)
    assert response.status_code == 303
    assert two.db.item.id in admin.get("/eingang").text
    assert admin.get(f"/api/v1/documents/{two.db.doc.id}").status_code == 200
    assert admin.get(f"/api/v1/documents/{two.da.doc.id}").status_code == 404
    settings = admin.get("/einstellungen/haushalte").text
    assert "Eltern" in settings and 'name="user" value="opa"' in settings and 'name="group" value="eltern"' in settings
    assert str(two.b.layout.data) in settings and "du arbeitest gerade hier" in settings


# ---------------------------------------------------------------------------
# Users without household, login gate


def test_sso_login_needs_a_household(two):
    client = client_for(two.app)
    two.provider.claims.update({"preferred_username": "zoe", "email": "zoe@example.test", "groups": ["sonstwer"]})
    with two.provider.running():
        response = oidc_login(client, two.provider)
    assert response.status_code == 303
    assert "error=denied" in response.headers["location"]
    assert "user" not in session_data(client)
    assert client.get("/eingang", follow_redirects=False).status_code == 303


def test_a_session_without_household_gets_the_403_page(two):
    client = client_for(two.app)
    session = {
        "user": {"username": "zoe", "name": "Zoe", "email": None, "groups": ["sonstwer"], "method": "oidc"},
        "auth_at": int(time.time()),
        "csrf": "x" * 32,
    }
    client.cookies.set(SESSION_COOKIE, signed_session(two.app.state.session_secret, session), domain="testserver.local")
    for url in ("/eingang", "/archiv", "/einstellungen", f"/dokument/{two.da.doc.id}", "/teilen"):
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 403, url
        assert "Kein Haushalt freigegeben" in response.text
        assert "Alphaversicherung" not in response.text and "Betakasse" not in response.text
        assert 'action="/logout"' in response.text
    response = client.get("/api/v1/status")
    assert response.status_code == 403 and "Haushalt" in response.json()["detail"]
    with two.provider.running():
        response = client.post("/logout", data={"csrf_token": "x" * 32}, follow_redirects=False)
    assert response.status_code == 303 and "user" not in session_data(client)


def test_anonymous_requests_still_go_to_the_login(two):
    client = client_for(two.app)
    response = client.get("/eingang", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert client.get("/api/v1/status").status_code == 401
    assert client.get("/login").status_code == 200
    assert client.get("/healthz").json()["status"] == "ok"


def test_login_and_logout_are_audited_in_the_household(two):
    anna = browser(two, "anna")
    assert two.a.audit.tail(1)[0]["action"] == "login"
    assert not any(e.get("user") == "anna" for e in two.b.audit.tail(50))
    response = anna.post("/logout", data={"csrf_token": csrf(anna)}, follow_redirects=False)
    assert response.status_code == 303
    assert two.a.audit.tail(1)[0]["action"] == "logout"


# ---------------------------------------------------------------------------
# A single household stays as it was


def test_single_household_shows_nothing_new(services, samples, tmp_path):
    from web_support import make_app

    app = make_app(services)
    client = client_for(app)
    assert login_local(client).status_code == 303
    page = client.get("/eingang").text
    assert 'action="/haushalt"' not in page and "household-menu" not in page
    assert "household" not in session_data(client)
    response = client.post("/haushalt", data={"csrf_token": csrf(client), "id": "default"}, follow_redirects=False)
    assert response.status_code == 404
    settings = client.get("/einstellungen").text
    assert 'id="haushalt"' not in settings
    assert "household" not in client.get("/api/v1/status").json()
