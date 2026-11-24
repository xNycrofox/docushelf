"""Regression tests for the web layer: API tokens tied to their owner, revocable
emergency sessions, batch filing that only files what the page showed, the
document edit form that keeps concurrent changes, signed and protected PDFs,
Paperless metadata on the document page and the deployment files."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
import unicodedata
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

from archive_support import make_ready_item
from docushelf.errors import NotFound, ProcessingError
from docushelf.models import DocMeta, MetadataUpdate
from docushelf.web import routes_ui, security, views
from docushelf.web.app import SESSION_COOKIE
from docushelf.web.security import User, hash_password
from docushelf.web.tokens import TokenStore
from web_support import (
    ADMIN_PASSWORD,
    FakeProvider,
    client_for,
    login_local,
    make_app,
    oidc_login,
    session_data,
    signed_session,
)

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"


# ---------------------------------------------------------------------------
# Helpers


class _Forms(HTMLParser):
    """The fields a browser would submit for each form (checked boxes only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._form: dict | None = None

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "form":
            self._form = {"action": data.get("action"), "fields": {}, "inputs": []}
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None and data.get("name"):
            self._form["inputs"].append(data)
            if data.get("type") in ("checkbox", "radio") and "checked" not in data:
                return
            self._form["fields"][data["name"]] = data.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None


def form_of(html: str, action: str) -> dict:
    parser = _Forms()
    parser.feed(html)
    for form in parser.forms:
        if form["action"] == action:
            return form
    raise AssertionError(f"no form with action {action}")


class _Inline(HTMLParser):
    """Finds what the Content-Security-Policy would block: style attributes, event handlers, inline scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self._script: dict | None = None

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if "style" in data or any(name.startswith("on") for name in data):
            self.problems.append(f"{tag} {data}")
        if tag == "script":
            self._script = data

    def handle_endtag(self, tag):
        if tag == "script":
            self._script = None

    def handle_data(self, data):
        if self._script is not None and data.strip() and self._script.get("type") != "application/json":
            self.problems.append("inline script")


def assert_csp_clean(html: str) -> None:
    parser = _Inline()
    parser.feed(html)
    assert not parser.problems, parser.problems


def flash_texts(html: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m)).strip()
        for m in re.findall(r'<p class="flash-text">(.*?)</p>', html, re.S)
    ]


def csrf_of(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]*)"', html)
    assert match, "page has no CSRF meta tag"
    return match.group(1)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signed_pdf(samples, path: Path, text: str = "Rechnung mit Signatur", *, sig_flags: int | None = 3, perms: bool = False) -> Path:
    """A PDF with a filled signature field (the signature bytes are a placeholder)."""
    import pikepdf

    plain = samples.text_pdf(path.with_name(path.stem + "-plain.pdf"), [text])
    with pikepdf.open(plain) as pdf:
        value = pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.Sig, Filter=pikepdf.Name("/Adobe.PPKLite"), Contents=pikepdf.String(b"\x00" * 32))
        )
        widget = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Sig,
                T=pikepdf.String("Signatur"), V=value, Rect=pikepdf.Array([0, 0, 0, 0]),
            )
        )
        form = pikepdf.Dictionary(Fields=pikepdf.Array([widget]))
        if sig_flags is not None:
            form["/SigFlags"] = sig_flags
        pdf.Root["/AcroForm"] = pdf.make_indirect(form)
        if perms:
            pdf.Root["/Perms"] = pikepdf.Dictionary(DocMDP=value)
        pdf.save(path)
    plain.unlink()
    return path


def encrypted_pdf(samples, path: Path, *, user_password: str = "geheim") -> Path:
    import pikepdf

    plain = samples.text_pdf(path.with_name(path.stem + "-plain.pdf"), ["Vertraulich"])
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(owner="besitzer", user=user_password, R=6))
    plain.unlink()
    return path


def ready_item_from(services, original: Path, *, context: dict | None = None):
    """An inbox item in ``bereit`` whose document is a byte copy of ``original``,
    as the pipeline leaves signed or encrypted PDFs."""
    item = services.inbox.create("upload", [original], context=context or {}, move=False)
    shutil.copyfile(item.original_paths()[0], item.document_path)
    item.stage = "ready"
    item.suggestions = [{"folder": "Verträge", "title": "Vertrag", "score": 0.9, "source": "rule", "reasons": []}]
    services.inbox.save(item)
    services.inbox.move(item, "bereit")
    return item


@pytest.fixture
def app_client(services, monkeypatch):
    """The complete application on the real service container, logged in locally."""
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda settings: routes_ui._OcrInfo(True, True, "16", [], []))
    app = make_app(services)
    client = client_for(app)
    assert login_local(client).status_code == 303
    client.app_ref = app
    return client


def _page(client, url: str, status: int = 200) -> str:
    response = client.get(url)
    assert response.status_code == status, (url, response.status_code, response.text[:400])
    return response.text


def _archive_pdf(services, samples, rel: str, text: str = "Inhalt") -> object:
    path = services.layout.archive / rel
    samples.text_pdf(path, [text])
    services.reconciler.scan(full=True)
    record = services.index.get_by_path(rel)
    assert record is not None
    return record


# ---------------------------------------------------------------------------
# API tokens remember their owner and stop with the owner's access


def test_token_store_keeps_an_owner_snapshot(tmp_path):
    store = TokenStore(tmp_path / "config" / "tokens.yaml")
    owner = User(username="anna", name="Anna", email="anna@example.test", groups=("familie", "familie", " "), method="oidc")
    token, entry = store.create("iPhone", "anna", owner=owner)
    assert (entry["method"], entry["email"], entry["groups"]) == ("oidc", "anna@example.test", ["familie"])
    stored = yaml.safe_load(store.path.read_text(encoding="utf-8"))[0]
    assert stored["method"] == "oidc" and stored["groups"] == ["familie"] and stored["email"] == "anna@example.test"
    assert TokenStore(store.path).verify(token) == entry

    # Command line tokens (no owner) keep the previous file format and are not tied to an account.
    _cli, cli_entry = store.create("Skript", "max")
    raw = yaml.safe_load(store.path.read_text(encoding="utf-8"))[1]
    assert set(raw) == {"id", "name", "user", "hash", "prefix", "created_at"}
    assert cli_entry["method"] is None and cli_entry["groups"] == []
    # A token cannot pass on a snapshot of its own.
    _other, from_token = store.create("Weiter", "anna", owner=User(username="anna", name="anna", method="token"))
    assert from_token["method"] is None


@pytest.mark.parametrize(
    "extra",
    [
        "  method: sso\n",  # unknown login method
        "  method: oidc\n  groups: familie\n",  # groups must be a list
        "  groups: [familie]\n",  # groups without a method
    ],
)
def test_token_file_with_a_broken_owner_snapshot_fails_closed(tmp_path, extra):
    store = TokenStore(tmp_path / "tokens.yaml")
    token = "dsk_handgeschrieben-0123456789"
    store.path.write_text(
        f"- id: a\n  user: anna\n  hash: {hashlib.sha256(token.encode()).hexdigest()}\n{extra}", encoding="utf-8"
    )
    assert store.verify(token) is None
    assert store.error and "tokens.yaml" in store.error


def test_oidc_token_stops_working_when_its_owner_is_no_longer_allowed(base_services):
    provider = FakeProvider()
    app = make_app(base_services, **provider.settings(allowed_users=(), allowed_groups=("familie",), allow_all_oidc_users=False))
    browser = client_for(app)
    with provider.running():
        assert oidc_login(browser, provider).status_code == 303
    created = browser.post("/api/v1/tokens", json={"name": "iPhone"}, headers={"X-CSRF-Token": session_data(browser)["csrf"]})
    assert created.status_code == 201
    token = created.json()["token"]
    assert created.json()["entry"]["groups"] == ["familie"]
    cli_token, _entry = base_services.tokens.create("Skript", "anna")
    api = client_for(app)
    assert api.get("/api/v1/status", headers=bearer(token)).status_code == 200

    # Anna is no longer allowed: her session and her token end, the command line token stays.
    app.state.settings = app.state.settings.with_overrides(allowed_groups=("andere",))
    refused = api.get("/api/v1/status", headers=bearer(token))
    assert refused.status_code == 401 and "API-Token" in refused.json()["detail"]
    assert api.post("/api/v1/upload", headers=bearer(token)).status_code == 401
    assert browser.get("/api/v1/status").status_code == 401
    assert api.get("/api/v1/status", headers=bearer(cli_token)).status_code == 200

    # Allowed again (now by user name): the token works again.
    app.state.settings = app.state.settings.with_overrides(allowed_users=("anna",))
    assert api.get("/api/v1/status", headers=bearer(token)).json()["user"]["username"] == "anna"


def test_token_from_the_settings_page_ends_with_the_emergency_login(app_client, services):
    app = app_client.app_ref
    html = _page(app_client, "/einstellungen")
    response = app_client.post("/einstellungen/tokens", data={"name": "Scanner-Skript", "csrf_token": csrf_of(html)}, follow_redirects=False)
    assert response.status_code == 303
    token = re.search(r'value="(dsk_[^"]+)"', _page(app_client, "/einstellungen/handy")).group(1)
    assert services.tokens.list()[0]["method"] == "local"
    api = client_for(app)
    assert api.get("/api/v1/status", headers=bearer(token)).status_code == 200
    app.state.settings = app.state.settings.with_overrides(admin_password=None, admin_password_hash=None)
    assert api.get("/api/v1/status", headers=bearer(token)).status_code == 401


def test_purging_the_trash_needs_a_browser_login(app_client, services, samples):
    record = _archive_pdf(services, samples, "Finanzen/2025-01-01 Alte Rechnung.pdf")
    services.ops.delete_document(record.id, user="anna")
    entry_id = services.trash.list()[0].id
    token, _entry = services.tokens.create("Skript", "anna")
    api = client_for(app_client.app_ref)

    refused = api.delete(f"/api/v1/trash/{entry_id}", headers=bearer(token))
    assert refused.status_code == 403 and "Browser" in refused.json()["detail"]
    page = api.post(f"/papierkorb/{entry_id}/loeschen", headers={**bearer(token), "Accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 403
    assert [entry.id for entry in services.trash.list()] == [entry_id]
    # Moving things into the trash and restoring stays possible with a token.
    assert api.post(f"/api/v1/trash/{entry_id}/restore", headers=bearer(token)).status_code == 200
    services.ops.delete_document(services.index.get_by_path(record.path).id, user="anna")
    entry_id = services.trash.list()[0].id

    html = _page(app_client, "/papierkorb")
    response = app_client.post(f"/papierkorb/{entry_id}/loeschen", data={"csrf_token": csrf_of(html)}, follow_redirects=False)
    assert response.status_code == 303 and services.trash.list() == []


# ---------------------------------------------------------------------------
# Sessions of the emergency account end when its password changes


def test_changing_the_emergency_password_ends_local_sessions(base_services):
    app = make_app(base_services)
    client = client_for(app)
    login_local(client)
    assert client.get("/api/v1/status").status_code == 200
    raw = base64.b64decode(client.cookies.get(SESSION_COOKIE).strip('"').split(".")[0]).decode("utf-8")
    assert ADMIN_PASSWORD not in raw  # the fingerprint reveals nothing about the password

    # Same configuration after a restart: the session stays valid.
    restarted = client_for(make_app(base_services))
    restarted.cookies.set(SESSION_COOKIE, client.cookies.get(SESSION_COOKIE))
    assert restarted.get("/api/v1/status").status_code == 200

    app.state.settings = app.state.settings.with_overrides(admin_password="ein-neues-passwort")
    assert client.get("/api/v1/status").status_code == 401
    assert "user" not in session_data(client)
    assert login_local(client, password="ein-neues-passwort").status_code == 303
    assert client.get("/api/v1/status").status_code == 200

    # Switching to a hash counts as a change as well.
    app.state.settings = app.state.settings.with_overrides(
        admin_password=None, admin_password_hash=hash_password("ein-neues-passwort", n=2**12)
    )
    assert client.get("/api/v1/status").status_code == 401


def test_local_session_without_fingerprint_is_ended(base_services):
    app = make_app(base_services)
    client = client_for(app)
    user = {"username": "admin", "name": "admin", "email": None, "groups": [], "method": "local"}
    secret = app.state.settings.secret_key
    client.cookies.set(SESSION_COOKIE, signed_session(secret, {"user": user, "auth_at": int(time.time()), "csrf": "c" * 43}))
    assert client.get("/api/v1/status").status_code == 401
    other = security.credential_fingerprint(app.state.settings, "anderer-schluessel")
    session = {"user": user, "auth_at": int(time.time()), "csrf": "c" * 43, "cred": other}
    client.cookies.set(SESSION_COOKIE, signed_session(secret, session))
    assert client.get("/api/v1/status").status_code == 401


def test_sso_sessions_do_not_depend_on_the_emergency_password(base_services):
    provider = FakeProvider()
    app = make_app(base_services, **provider.settings(allowed_users=("anna",), allowed_groups=(), allow_all_oidc_users=False))
    client = client_for(app)
    with provider.running():
        oidc_login(client, provider)
    assert "cred" not in session_data(client)
    app.state.settings = app.state.settings.with_overrides(admin_password="anders")
    assert client.get("/api/v1/status").status_code == 200


# ---------------------------------------------------------------------------
# Batch filing files only what the page showed (contract C5)


def test_suggestion_fingerprint_contract():
    top = {"folder": "Wohnung/Strom", "title": "Stromrechnung März", "doc_date": "2026-03-01", "tags": ["Strom"],
           "sender": "Stadtwerke", "score": 0.9, "source": "rule", "reasons": ["Regel"]}
    data = {key: top[key] for key in ("folder", "title", "doc_date", "tags", "sender")}
    expected = hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    assert views.suggestion_fingerprint(top) == expected and len(expected) == 12
    assert views.suggestion_fingerprint({**top, "score": 0.2, "reasons": [], "source": "similarity"}) == expected
    for key, value in (("folder", "Verträge"), ("title", "Abschlag"), ("doc_date", None), ("tags", []), ("sender", "EnBW")):
        assert views.suggestion_fingerprint({**top, key: value}) != expected
    assert views.suggestion_fingerprint(None) == views.suggestion_fingerprint({})
    assert views.parse_batch_selection(["20260101-000000-abcdef:0123456789ab:dup", "x", 5, "20260101-000000-abcdef"]) == {
        "20260101-000000-abcdef": ("0123456789ab", True)
    }


def _change_suggestion(services, item, **changes):
    item = services.inbox.load(item.id)
    item.suggestions[0].update(changes)
    services.inbox.save(item)


def test_api_batch_files_only_what_the_page_showed(app_client, services, samples, tmp_path):
    shown, changed, new_duplicate, known_duplicate, plain = (make_ready_item(services, samples, tmp_path) for _ in range(5))
    known_duplicate.duplicate_of = {"kind": "inbox", "id": shown.id}
    services.inbox.save(known_duplicate)
    expect = {item.id: views.suggestion_fingerprint(services.inbox.load(item.id).top_suggestion) for item in (shown, changed, new_duplicate, known_duplicate)}
    # After the page was rendered: a refresh changes one suggestion, another item turns out to be a duplicate.
    _change_suggestion(services, changed, folder="Verträge/Mobilfunk", title="Vertragsunterlagen")
    fresh = services.inbox.load(new_duplicate.id)
    fresh.duplicate_of = {"kind": "inbox", "id": shown.id}
    services.inbox.save(fresh)

    token, _entry = services.tokens.create("Skript", "anna")
    api = client_for(app_client.app_ref)
    items = [
        {"id": shown.id, "expect": expect[shown.id]},
        {"id": changed.id, "expect": expect[changed.id]},
        {"id": new_duplicate.id, "expect": expect[new_duplicate.id]},
        {"id": known_duplicate.id, "expect": expect[known_duplicate.id], "accept_duplicate": True},
        plain.id,
    ]
    body = api.post("/api/v1/inbox/file-batch", json={"items": items}, headers=bearer(token)).json()
    assert sorted(entry["item"] for entry in body["filed"]) == sorted([shown.id, known_duplicate.id, plain.id])
    errors = {entry["item"]: entry["error"] for entry in body["failed"]}
    assert errors == {changed.id: views.STALE_SUGGESTION, new_duplicate.id: views.NEW_DUPLICATE}
    assert services.inbox.find(changed.id).state == "bereit" and services.inbox.find(new_duplicate.id).state == "bereit"
    assert not (services.layout.archive / "Verträge").exists()
    bad = api.post("/api/v1/inbox/file-batch", json={"items": [{"id": changed.id, "expect": 5}]}, headers=bearer(token))
    assert bad.json()["failed"][0]["error"] == "„expect“ muss ein Text sein."


def test_batch_form_without_javascript_checks_the_fingerprints(app_client, services, samples, tmp_path):
    first, second, third = (make_ready_item(services, samples, tmp_path) for _ in range(3))
    html = _page(app_client, "/eingang")
    form = form_of(html, "/eingang/sammelablage")
    values = {box["data-id"]: box["value"] for box in form["inputs"] if box.get("name") == "items"}
    assert values[first.id] == f"{first.id}:{views.suggestion_fingerprint(services.inbox.load(first.id).top_suggestion)}"
    _change_suggestion(services, second, folder="Verträge/Mobilfunk")
    fresh = services.inbox.load(third.id)
    fresh.duplicate_of = {"kind": "inbox", "id": first.id}
    services.inbox.save(fresh)

    response = app_client.post(
        "/eingang/sammelablage",
        data={"csrf_token": csrf_of(html), "items": [values[first.id], values[second.id], values[third.id]]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert services.inbox.find(first.id).state == "erledigt"
    assert services.inbox.find(second.id).state == "bereit" and services.inbox.find(third.id).state == "bereit"
    texts = " ".join(flash_texts(_page(app_client, "/eingang")))
    assert "1 Dokument abgelegt." in texts
    assert views.STALE_SUGGESTION in texts and views.NEW_DUPLICATE in texts


def test_inbox_script_sends_the_fingerprint():
    script = (ROOT / "docushelf" / "static" / "app.js").read_text(encoding="utf-8")
    assert "box.dataset.expect" in script and "accept_duplicate" in script and "dataset.id" in script


# ---------------------------------------------------------------------------
# The document edit form keeps what changed in the meantime


def _uuid_document(services, samples, rel: str, text: str = "Stadtwerke Rechnung Strom"):
    record = _archive_pdf(services, samples, rel, text)
    # The first metadata write gives the file its permanent id (XMP), so moves keep it.
    return services.ops.update_document(record.id, MetadataUpdate(sender="Stadtwerke"), user="test")


def _edit_form(client, record) -> dict:
    html = _page(client, f"/dokument/{record.id}")
    fields = dict(form_of(html, f"/dokument/{record.id}/bearbeiten")["fields"])
    return fields


def test_stale_edit_form_keeps_a_move_made_in_the_finder(app_client, services, samples):
    record = _uuid_document(services, samples, "Eingang alt/2025-02-01 Rechnung Strom.pdf")
    fields = _edit_form(app_client, record)
    assert fields["orig_folder"] == "Eingang alt" and fields["orig_title"] == "Rechnung Strom"
    # Meanwhile the file is moved and renamed in the Finder.
    old = services.layout.archive / record.path
    new = services.layout.archive / "Wohnung/Strom/2025-02-01 Stromabrechnung Stadtwerke.pdf"
    new.parent.mkdir(parents=True)
    os.rename(old, new)
    services.reconciler.scan(full=True)
    assert services.index.get(record.id).path == "Wohnung/Strom/2025-02-01 Stromabrechnung Stadtwerke.pdf"

    fields["tags"] = "steuerrelevant 2025"
    response = app_client.post(f"/dokument/{record.id}/bearbeiten", data=fields, follow_redirects=False)
    assert response.status_code == 303
    assert new.is_file() and not old.exists() and not (services.layout.archive / "Eingang alt" / old.name).exists()
    updated = services.index.get_by_path("Wohnung/Strom/2025-02-01 Stromabrechnung Stadtwerke.pdf")
    assert updated.tags == ["steuerrelevant 2025"] and updated.title == "Stromabrechnung Stadtwerke"


def test_stale_edit_form_keeps_other_peoples_tags(app_client, services, samples):
    record = _uuid_document(services, samples, "Haushalt/2025-05-01 Garantie Kühlschrank.pdf")
    fields = _edit_form(app_client, record)
    services.ops.update_document(record.id, MetadataUpdate(tags=["Garantie"]), user="partner")
    fields["title"] = "Garantie Kühlschrank Bosch"
    response = app_client.post(f"/dokument/{record.id}/bearbeiten", data=fields, follow_redirects=False)
    assert response.status_code == 303
    updated = services.index.get_by_path("Haushalt/2025-05-01 Garantie Kühlschrank Bosch.pdf")
    assert updated.tags == ["Garantie"] and updated.sender == "Stadtwerke"


def test_conflicting_edit_asks_again_instead_of_overwriting(app_client, services, samples):
    record = _uuid_document(services, samples, "Wohnung/2025-03-01 Nebenkosten.pdf")
    fields = _edit_form(app_client, record)
    renamed = services.layout.archive / "Wohnung/2025-03-01 Nebenkostenabrechnung 2024.pdf"
    os.rename(services.layout.archive / record.path, renamed)
    services.reconciler.scan(full=True)

    fields["title"] = "Betriebskosten 2024"
    response = app_client.post(f"/dokument/{record.id}/bearbeiten", data=fields, follow_redirects=False)
    assert response.status_code == 409
    assert "Die Datei wurde inzwischen geändert (Titel jetzt „Nebenkostenabrechnung 2024“)" in response.text
    assert_csp_clean(response.text)
    assert renamed.is_file()  # nothing was overwritten
    again = dict(form_of(response.text, f"/dokument/{record.id}/bearbeiten")["fields"])
    assert again["title"] == "Betriebskosten 2024" and again["orig_title"] == "Nebenkostenabrechnung 2024"

    # Saving the re-rendered form applies the user's title deliberately.
    response = app_client.post(f"/dokument/{record.id}/bearbeiten", data=again, follow_redirects=False)
    assert response.status_code == 303
    assert (services.layout.archive / "Wohnung/2025-03-01 Betriebskosten 2024.pdf").is_file() and not renamed.exists()


def test_input_errors_keep_the_rendered_values(app_client, services, samples):
    record = _uuid_document(services, samples, "Wohnung/2025-03-01 Wasser.pdf")
    fields = _edit_form(app_client, record)
    fields.update(doc_date="31.02.2025", title="Wasserrechnung")
    response = app_client.post(f"/dokument/{record.id}/bearbeiten", data=fields, follow_redirects=False)
    assert response.status_code == 400 and "kein gültiges Datum" in response.text
    again = form_of(response.text, f"/dokument/{record.id}/bearbeiten")["fields"]
    assert again["title"] == "Wasserrechnung" and again["orig_title"] == "Wasser" and again["orig_doc_date"] == "2025-03-01"


@pytest.mark.parametrize("folder", ["Kfz  Versicherung", "Vereine/TSV Dresden e.V."])
def test_title_edit_keeps_folders_the_app_would_spell_differently(app_client, services, samples, folder):
    record = _archive_pdf(services, samples, f"{folder}/2025-01-10 Beitrag.pdf")
    assert record.folder == folder
    fields = _edit_form(app_client, record)
    assert fields["folder"] == folder
    fields["title"] = "Beitrag 2025"
    assert app_client.post(f"/dokument/{record.id}/bearbeiten", data=fields, follow_redirects=False).status_code == 303
    assert (services.layout.archive / folder / "2025-01-10 Beitrag 2025.pdf").is_file()
    # A form without the rendered values: the folder field equal to the current folder is no move either.
    moved = services.index.get_by_path(f"{folder}/2025-01-10 Beitrag 2025.pdf")
    legacy = {"csrf_token": fields["csrf_token"], "title": "Beitrag 2025 Verein", "doc_date": "2025-01-10",
              "sender": "", "tags": "", "folder": f"  {folder} "}
    assert app_client.post(f"/dokument/{moved.id}/bearbeiten", data=legacy, follow_redirects=False).status_code == 303
    assert (services.layout.archive / folder / "2025-01-10 Beitrag 2025 Verein.pdf").is_file()
    folders = sorted(p.relative_to(services.layout.archive).as_posix() for p in services.layout.archive.rglob("*") if p.is_dir())
    assert folders == sorted({folder, *(folder.split("/")[:1])})


def test_edit_plan_compares_folders_after_unicode_normalization():
    nfd = unicodedata.normalize("NFD", "Rechnungen/März")
    current = {"folder": views.folder_key(nfd), "title": "A"}
    plan = views.plan_document_edit({"folder": views.folder_key(" Rechnungen/März/ "), "title": "A"}, None, current)
    assert plan.changes == {} and plan.conflicts == {}
    plan = views.plan_document_edit({"folder": "", "title": "B"}, {"folder": "X", "title": "A"}, {"folder": "Y", "title": "A"})
    assert plan.changes == {"title": "B"}  # an empty folder field keeps the folder
    plan = views.plan_document_edit({"title": "B"}, {"title": "A"}, {"title": "C"})
    assert plan.conflicts == {"title": "C"} and plan.changes == {}
    plan = views.plan_document_edit({"title": "C"}, {"title": "A"}, {"title": "C"})
    assert plan.changes == {} and plan.conflicts == {}  # the same change was made elsewhere


# ---------------------------------------------------------------------------
# Signed and password-protected PDFs (contract C3)


def test_protection_is_detected_without_changing_the_file(samples, tmp_path):
    plain = samples.text_pdf(tmp_path / "plain.pdf", ["Text"])
    assert views.pdf_protection(plain) is None
    signed = signed_pdf(samples, tmp_path / "signed.pdf")
    before = signed.read_bytes()
    assert views.pdf_protection(signed) == "signed"
    assert signed.read_bytes() == before
    assert views.pdf_protection(signed_pdf(samples, tmp_path / "field.pdf", sig_flags=None)) == "signed"
    assert views.pdf_protection(signed_pdf(samples, tmp_path / "perms.pdf", sig_flags=None, perms=True)) == "signed"
    assert views.pdf_protection(encrypted_pdf(samples, tmp_path / "locked.pdf")) == "encrypted"
    assert views.pdf_protection(encrypted_pdf(samples, tmp_path / "owner.pdf", user_password="")) == "encrypted"
    assert views.pdf_protection(tmp_path / "fehlt.pdf") is None


def test_unrenderable_pages_get_a_placeholder_image(app_client, services, samples, tmp_path):
    locked = encrypted_pdf(samples, tmp_path / "quelle" / "Vertrag.pdf")
    item = ready_item_from(services, locked, context={"pdf_protection": "encrypted"})
    response = app_client.get(f"/api/v1/inbox/{item.id}/pages/1.png?w=320")
    assert response.status_code == 200 and response.headers["content-type"].startswith("image/svg+xml")
    assert "Keine Vorschau möglich" in response.text and response.headers["cache-control"] == "private, no-cache"
    assert app_client.get(f"/api/v1/inbox/{item.id}/pages/0.png").status_code == 404

    target = services.layout.archive / "Verträge/2025-06-01 Vertrag.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(locked.read_bytes())
    services.reconciler.scan(full=True)
    record = services.index.get_by_path("Verträge/2025-06-01 Vertrag.pdf")
    response = app_client.get(f"/api/v1/documents/{record.id}/pages/1.png")
    assert response.status_code == 200 and response.headers["content-type"].startswith("image/svg+xml")
    html = _page(app_client, f"/dokument/{record.id}")
    assert "Diese PDF ist verschlüsselt" in html and 'name="tags"' not in html and 'name="sender"' not in html


def test_render_errors_give_the_placeholder_but_missing_pages_stay_404(app_client, services, samples, monkeypatch):
    from docushelf.pdf import render

    record = _archive_pdf(services, samples, "A/2025-01-01 Test.pdf")
    failure: list[Exception] = [ProcessingError("PDF kann nicht geöffnet werden: x.pdf")]

    def broken(*args, **kwargs):
        raise failure[0]

    monkeypatch.setattr(render, "render_page_png", broken)
    response = app_client.get(f"/api/v1/documents/{record.id}/pages/1.png?w=99")
    assert response.status_code == 200 and response.headers["content-type"].startswith("image/svg+xml")
    failure[0] = NotFound("Seite 7 existiert nicht")
    missing = app_client.get(f"/api/v1/documents/{record.id}/pages/7.png?w=99")
    assert missing.status_code == 404 and missing.json()["detail"] == "Seite 7 existiert nicht"


@pytest.mark.parametrize(("kind", "label", "notice"), [
    ("signed", "signiert", "Diese PDF ist digital signiert."),
    ("encrypted", "passwortgeschützt", "Diese PDF ist mit einem Passwort geschützt"),
])
def test_inbox_explains_protected_pdfs(app_client, services, samples, tmp_path, kind, label, notice):
    item = make_ready_item(services, samples, tmp_path, context={"pdf_protection": kind})
    plain = make_ready_item(services, samples, tmp_path)
    html = _page(app_client, "/eingang")
    assert html.count("data-protection") == 1 and f"{label}</span>" in html and notice in html  # badge with tooltip
    detail = _page(app_client, f"/eingang/{item.id}")
    assert notice in detail and "Tags und Absender lassen sich nicht in der Datei speichern" in detail
    assert_csp_clean(html)
    assert_csp_clean(detail)
    assert "data-protection" not in _page(app_client, f"/eingang/{plain.id}")


def test_document_page_of_a_signed_pdf(app_client, services, samples):
    path = signed_pdf(samples, services.layout.archive / "Telefon" / "2026-03-05 Rechnung Telekom.pdf")
    services.reconciler.scan(full=True)
    record = services.index.get_by_path("Telefon/2026-03-05 Rechnung Telekom.pdf")
    html = _page(app_client, f"/dokument/{record.id}")
    assert "Diese PDF ist digital signiert" in html and "Tags und Absender" in html
    assert_csp_clean(html)
    fields = form_of(html, f"/dokument/{record.id}/bearbeiten")["fields"]
    assert "tags" not in fields and "sender" not in fields and "orig_tags" not in fields
    assert set(fields) >= {"title", "doc_date", "folder", "orig_title", "orig_doc_date", "orig_folder"}
    assert path.is_file()


# ---------------------------------------------------------------------------
# Paperless notes and custom fields on the document page (contract C2)


def test_document_extras_are_split_into_entries():
    extras = views.document_extras(
        {
            "PaperlessNotes": "2024-03-01 anna: Mit Karte bezahlt | 2024-04-02 max: Widerspruch eingelegt",
            "PaperlessFields": "Betrag: EUR199.99; Status: bezahlt; Adresse: Hauptstr. 1; 01067 Dresden",
            "ArchiveSerialNumber": "17",
            "MailFrom": "  ",
            "Unbekannt": "x",
        }
    )
    by_key = {entry["key"]: entry for entry in extras}
    assert list(by_key) == ["PaperlessNotes", "PaperlessFields", "ArchiveSerialNumber"]
    assert by_key["PaperlessNotes"]["values"] == ["2024-03-01 anna: Mit Karte bezahlt", "2024-04-02 max: Widerspruch eingelegt"]
    assert by_key["PaperlessFields"]["values"] == ["Betrag: EUR199.99", "Status: bezahlt", "Adresse: Hauptstr. 1; 01067 Dresden"]
    assert views.document_extras(None) == []


def test_document_page_shows_paperless_notes_and_fields(app_client, services, samples, monkeypatch):
    from docushelf.pdf import xmp

    record = _archive_pdf(services, samples, "Finanzen/2024-03-01 Rechnung Möbel.pdf")
    original = xmp.read_meta

    def read_meta(path):
        meta = original(path)
        meta.extra.update(
            PaperlessNotes="2024-03-01 anna: Mit Karte bezahlt <b>sofort</b>",
            PaperlessFields="Betrag: EUR199.99; Status: bezahlt",
            PaperlessId="42",
        )
        return meta

    monkeypatch.setattr(xmp, "read_meta", read_meta)
    html = _page(app_client, f"/dokument/{record.id}")
    assert_csp_clean(html)
    assert "Weitere Angaben" in html and "Notizen" in html and "Eigene Felder" in html
    assert "<p>2024-03-01 anna: Mit Karte bezahlt &lt;b&gt;sofort&lt;/b&gt;</p>" in html
    assert "<p>Betrag: EUR199.99</p><p>Status: bezahlt</p>" in html and "Paperless-ID" in html


def test_document_page_reads_the_metadata_from_the_file(app_client, services, samples):
    from docushelf.pdf import xmp

    extra = {"ArchiveSerialNumber": "17", "PaperlessId": "42"}
    if "PaperlessNotes" in xmp.EXTRA_PROPERTIES:
        extra["PaperlessNotes"] = "2024-03-01 anna: Mit Karte bezahlt"
    path = services.layout.archive / "Finanzen/2024-03-01 Rechnung.pdf"
    samples.text_pdf(path, ["Rechnung"])
    tmp = path.with_name("tmp.pdf")
    xmp.write_meta(path, tmp, DocMeta(title="Rechnung", doc_date=date(2024, 3, 1), extra=extra))
    os.replace(tmp, path)
    services.reconciler.scan(full=True)
    record = services.index.get_by_path("Finanzen/2024-03-01 Rechnung.pdf")
    html = _page(app_client, f"/dokument/{record.id}")
    assert "Archivnummer (ASN)" in html and "<p>17</p>" in html and "<p>42</p>" in html
    if "PaperlessNotes" in extra:
        assert "Mit Karte bezahlt" in html


def test_inbox_shows_paperless_custom_fields(app_client, services, samples, tmp_path):
    context = {"paperless": {"id": 7, "notes": [{"text": "Mit Karte bezahlt"}],
                             "custom_fields": [{"name": "Betrag", "value": "EUR199.99"}, {"name": "Leer", "value": None}]}}
    item = make_ready_item(services, samples, tmp_path, context=context)
    html = _page(app_client, f"/eingang/{item.id}")
    assert "Eigene Felder" in html and "Betrag: EUR199.99" in html and "Leer:" not in html


# ---------------------------------------------------------------------------
# Rules page


def test_rules_page_explains_what_happens_to_comments(app_client):
    html = _page(app_client, "/regeln")
    assert '<label for="r-sender">Erkannter Absender enthält</label>' in html
    assert "Kommentare" in html and "rules.yaml.bak" in html


# ---------------------------------------------------------------------------
# Deployment files


def _active_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_example_configuration_binds_no_fixed_vpn_address():
    active = _active_lines(DEPLOY / ".env.example")
    assert not any(line.startswith(("DOCUSHELF_PRIVATE_IP=", "DOCUSHELF_SMTP_BIND=")) for line in active)
    text = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    assert "# DOCUSHELF_PRIVATE_IP=" in text and "# DOCUSHELF_SMTP_BIND=" in text
    # Access is granted by group first; user names are the identity provider's user names.
    assert any(line.startswith("DOCUSHELF_ALLOWED_GROUPS=") for line in active)
    assert not any(line.startswith("DOCUSHELF_ALLOWED_USERS=") for line in active)


def test_app_container_publishes_smtp_on_its_own_address():
    compose = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))
    ports = [str(port) for port in compose["services"]["docushelf"]["ports"]]
    assert "${DOCUSHELF_SMTP_BIND:-127.0.0.1}:2525:2525" in ports
    assert not any("DOCUSHELF_PRIVATE_IP" in port for port in ports)
    for name in ("samba", "ftp"):
        assert all("${DOCUSHELF_PRIVATE_IP:-127.0.0.1}" in str(port) for port in compose["services"][name]["ports"])


def test_documentation_mentions_the_new_settings():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    for text in (readme, deploy):
        assert "DOCUSHELF_SMTP_BIND" in text and "DOCUSHELF_OWNER_NAMES" in text
    assert "Notizen und eigene Felder wandern mit" not in deploy
    assert "secret.key" in deploy and "DOCUSHELF_SESSION_MAX_AGE" in deploy and "preferred_username" in deploy
