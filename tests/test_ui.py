"""Tests for the web interface: templates, filters, view helpers and the HTML routes.

Pages are rendered against real files (archive, inbox, trash, rules) with the
real archive operations; only slow or external collaborators (pipeline,
worker, watcher) are replaced by small fakes. One part runs through the
complete application of ``web/app.py`` with a real login and CSRF check.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

from archive_support import add_archive_file, add_archive_pdf, attach_archive, make_ready_item
from docushelf.channels.sendung import read_sendung
from docushelf.models import ScanStats
from docushelf.util.timeutil import timezone
from docushelf.web import routes_ui, security, templating, views

USER = security.User(username="alex", name="Alex Beispiel", email="alex@example.org", groups=(), method="oidc")
STATIC = templating.STATIC_DIR
TEMPLATES = templating.TEMPLATE_DIR


# ---------------------------------------------------------------------------
# HTML inspection


class _PageParser(HTMLParser):
    """Collects what the CSP and CSRF checks need: tags with attributes and forms."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.forms: list[dict] = []
        self._form: dict | None = None
        self._script: dict[str, str | None] | None = None
        self.inline_scripts: list[str] = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        self.tags.append((tag, data))
        if tag == "form":
            self._form = {"attrs": data, "inputs": []}
            self.forms.append(self._form)
        elif tag in ("input", "button", "textarea", "select") and self._form is not None:
            self._form["inputs"].append(data)
        elif tag == "script":
            self._script = data

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "script":
            self._script = None

    def handle_data(self, data):
        if self._script is not None and data.strip():
            if self._script.get("type") != "application/json":
                self.inline_scripts.append(data)


def parse(html: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(html)
    return parser


def assert_clean_page(html: str, *, csrf: str | None = None) -> _PageParser:
    """No inline script, no style attribute, no event handler; every POST form carries the CSRF token."""
    page = parse(html)
    assert not page.inline_scripts, page.inline_scripts
    for tag, attrs in page.tags:
        assert "style" not in attrs, (tag, attrs)
        handlers = [name for name in attrs if name.startswith("on")]
        assert not handlers, (tag, handlers)
        if tag == "script":
            assert attrs.get("src") or attrs.get("type") == "application/json", attrs
            if attrs.get("src"):
                assert attrs["src"].startswith("/static/"), attrs
        for name in ("href", "src", "action"):
            value = attrs.get(name) or ""
            assert not value.lower().startswith("javascript:"), (tag, attrs)
            assert not value.startswith(("http://", "https://", "//")), (tag, attrs)
    for form in page.forms:
        if (form["attrs"].get("method") or "get").lower() != "post":
            continue
        tokens = [i.get("value") for i in form["inputs"] if i.get("name") == "csrf_token"]
        assert tokens and tokens[0], form["attrs"]
        if csrf is not None:
            assert tokens[0] == csrf, form["attrs"]
    # Inline JSON must not be able to close its script element.
    for block in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        assert "<" not in block
    return page


def csrf_of(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]*)"', html)
    assert match, "page has no CSRF meta tag"
    return match.group(1)


def flash_texts(html: str) -> list[str]:
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m)).strip() for m in re.findall(r'<p class="flash-text">(.*?)</p>', html, re.S)]


# ---------------------------------------------------------------------------
# Fakes for slow or background collaborators


class FakePipeline:
    def __init__(self, services):
        self.services = services
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    def retry(self, item_id, *, skip_blank_removal=None, skip_ocr=None):
        self.calls.append(("retry", item_id, skip_blank_removal, skip_ocr))
        if self.error:
            raise self.error
        return self.services.inbox.load(item_id)

    def refresh_suggestions(self, item_id):
        self.calls.append(("refresh", item_id))
        if self.error:
            raise self.error
        return self.services.inbox.load(item_id)

    def queue_refresh(self, *, only_stale=False):
        self.calls.append(("queue_refresh", only_stale))
        if self.error:
            raise self.error
        return len(self.services.inbox.list("bereit"))


class FakeWorker:
    def __init__(self):
        self.running_ids: list[str] = []

    def status(self):
        return {
            "running": True,
            "threads": 1,
            "threads_alive": 1,
            "current_jobs": [{"kind": "process", "key": key, "seconds": 2.5} for key in self.running_ids],
            "queue": {"process:queued": 1},
            "last_error": None,
        }


class FakeWatcher:
    def __init__(self, running=True):
        self.running = running
        self.requests: list[str] = []

    def request_scan(self, reason=""):
        self.requests.append(reason)

    def status(self):
        return {
            "running": self.running,
            "scanning": False,
            "polling": False,
            "last_scan_at": None,
            "last_error": None,
            "last_result": ScanStats(added=2, updated=1).to_dict(),
        }


class FakeIntake:
    def __init__(self):
        self.triggered = 0

    def trigger(self):
        self.triggered += 1

    def status(self):
        return {"running": True, "last_scan_at": None, "last_error": None, "pending_files": 0}


@dataclass
class OcrStub:
    enabled: bool = True
    available: bool | None = True
    version: str | None = "16.0.0"
    languages: list = None
    missing_languages: list = None


@pytest.fixture
def svc(base_services, monkeypatch):
    """Real archive operations, trash, rules, tokens and search on temporary files."""
    from docushelf.index.search import Searcher
    from docushelf.suggest.rules import RuleStore
    from docushelf.web.tokens import TokenStore

    services = attach_archive(base_services)
    services.rules = RuleStore(services.layout.rules_file)
    services.tokens = TokenStore(services.layout.tokens_file)
    services.searcher = Searcher(services)
    services.pipeline = FakePipeline(services)
    services.worker = FakeWorker()
    services.watcher = FakeWatcher()
    services.intake = FakeIntake()
    services.imap = None
    services.smtp = None
    services.rebuilt = []

    def rebuild(*, user=None):
        services.rebuilt.append(user)
        return ScanStats(added=3, duration_ms=1250)

    services.rebuild = rebuild
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda settings: OcrStub(languages=["deu", "eng"], missing_languages=[]))
    return services


def build_app(services, user=USER) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret", session_cookie="docushelf_session")
    app.state.services = services
    app.state.settings = services.settings
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(routes_ui.router)
    app.dependency_overrides[security.require_user] = lambda: user
    app.dependency_overrides[security.verify_csrf] = lambda: None
    if hasattr(security, "require_session_user"):
        app.dependency_overrides[security.require_session_user] = lambda: user
    return app


@pytest.fixture
def client(svc):
    with TestClient(build_app(svc), base_url="http://testserver") as test_client:
        yield test_client


def get_page(client, url: str, status: int = 200) -> str:
    response = client.get(url)
    assert response.status_code == status, (url, response.status_code, response.text[:500])
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert_clean_page(html, csrf=csrf_of(html))
    return html


def post(client, url: str, data=None, files=None, status: int = 303, csrf: str | None = None):
    payload = dict(data or {})
    payload.setdefault("csrf_token", csrf or "test")
    response = client.post(url, data=payload, files=files, follow_redirects=False)
    assert response.status_code == status, (url, response.status_code, response.text[:800])
    return response


def failed_item(services, samples, tmp_path, name="kaputt.pdf"):
    original = samples.text_pdf(tmp_path / "drop-failed" / name, ["Defekte Datei"])
    item = services.inbox.create("smb", [original])
    item.error = {"stage": "ocr_done", "label": "Texterkennung (OCR)", "message": "OCR ist abgestürzt", "at": item.received_at}
    services.inbox.save(item)
    services.inbox.log(item, "Fehler bei Texterkennung (OCR): OCR ist abgestürzt")
    services.inbox.move(item, "fehler")
    return item


def processing_item(services, samples, tmp_path, name="scan-0001.pdf"):
    original = samples.text_pdf(tmp_path / "drop-busy" / name, ["Wird gerade verarbeitet"])
    item = services.inbox.create("upload", [original])
    item.stage = "deblanked"
    services.inbox.save(item)
    return item


# ---------------------------------------------------------------------------
# Filters and helpers


def test_date_and_size_filters():
    assert templating.date_de(date(2026, 3, 5)) == "05.03.2026"
    assert templating.date_de("2026-11-23") == "23.11.2026"
    assert templating.date_de("2026-11-23T23:30:00+00:00") == "24.11.2026"  # shown in local time
    assert templating.date_de(None) == ""
    assert templating.datetime_de("2026-11-23T14:05:09+01:00") == "23.11.2026, 14:05"
    assert templating.filesize(512) == "512 Bytes"
    assert templating.filesize(1536) == "1,5 KB"
    assert templating.filesize(5 * 1024 * 1024) == "5,0 MB"
    assert templating.filesize(250 * 1024 * 1024) == "250 MB"
    assert templating.filesize(None) == ""
    assert templating.percent(0.824) == "82 %"


def test_reltime_in_german():
    now = datetime(2026, 11, 23, 15, 0, tzinfo=timezone())
    rel = lambda value: templating.reltime(value, reference=now)  # noqa: E731
    assert rel("2026-11-23T14:59:40+01:00") == "gerade eben"
    assert rel("2026-11-23T14:55:00+01:00") == "vor 5 Minuten"
    assert rel("2026-11-23T12:00:00+01:00") == "vor 3 Stunden"
    assert rel("2026-11-22T09:15:00+01:00") == "gestern, 09:15"
    assert rel("2026-11-19T09:15:00+01:00") == "vor 4 Tagen"
    assert rel("2026-10-01T09:15:00+02:00") == "am 01.10.2026"
    assert rel("2026-12-03T15:00:00+01:00") == "in 10 Tagen"
    assert rel(date(2026, 11, 23)) == "heute"
    assert rel("") == ""


def _request(session: dict | None = None) -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    if session is not None:
        scope["session"] = session
    return Request(scope)


def test_flash_messages_roundtrip():
    session: dict = {}
    request = _request(session)
    views.flash(request, "Gespeichert.")
    views.flash(request, "Kaputt", "error", link="https://evil.example", action="/papierkorb/x/wiederherstellen")
    views.flash(request, "x" * 1000, "unbekannt")
    flashes = views.pop_flashes(request)
    assert [f["category"] for f in flashes] == ["success", "error", "info"]
    assert "link" not in flashes[1] and flashes[1]["action"] == "/papierkorb/x/wiederherstellen"
    assert len(flashes[2]["message"]) == views.MAX_FLASH_CHARS
    assert views.pop_flashes(request) == []
    # Without a session nothing breaks.
    views.flash(_request(None), "ignoriert")
    assert views.pop_flashes(_request(None)) == []


def test_safe_redirect_targets():
    assert views.safe_next("/eingang/x") == "/eingang/x"
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", "eingang", "", None, "/x\r\nSet-Cookie: a"):
        assert views.safe_next(bad, "/eingang") == "/eingang"


def test_snippets_keep_only_marks():
    html = views.safe_snippet('Strom &amp; Gas <mark>abschlag</mark> &lt;script&gt;alert(1)&lt;/script&gt; <mark>x')
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<mark>abschlag</mark>" in html and html.endswith("</mark>")
    assert "&amp;" in html


def test_form_parsing():
    assert views.parse_date_input("2026-03-05") == date(2026, 3, 5)
    assert views.parse_date_input("5.3.2026") == date(2026, 3, 5)
    assert views.parse_date_input("05.03.26") == date(2026, 3, 5)
    assert views.parse_date_input("") is None
    for bad in ("31.02.2026", "morgen", "2026-13-01"):
        with pytest.raises(ValueError):
            views.parse_date_input(bad)
    assert views.parse_tags(" Garantie, steuerrelevant 2026;garantie,, ") == ["Garantie", "steuerrelevant 2026"]
    assert views.parse_tags(["a, b", "B"]) == ["a", "b"]
    request = views.filing_request_from_form(
        {"folder": " Finanzen/Bank ", "title": "  Konto   auszug ", "doc_date": "2026-01-31", "tags": "a, b", "sender": "", "create_rule": "1"}
    )
    assert (request.folder, request.title, request.doc_date, request.tags, request.sender, request.create_rule) == (
        "Finanzen/Bank", "Konto auszug", date(2026, 1, 31), ["a", "b"], None, True
    )
    assert views.filing_request_from_suggestion({"folder": "A", "title": ""}) is None
    suggested = views.filing_request_from_suggestion({"folder": "A", "title": "T", "doc_date": "2025-01-02", "tags": ["x"]})
    assert suggested.doc_date == date(2025, 1, 2) and suggested.tags == ["x"]


def test_folder_tree_and_counts():
    folders = ["Finanzen", "Finanzen/Bank", "Finanzen/Steuer", "Wohnung", "Wohnung/Strom"]
    counts = {"Finanzen/Bank": 4, "Finanzen": 1, "Wohnung/Strom": 2, "": 1}
    totals = views.recursive_counts(folders, counts)
    assert totals["Finanzen"] == 5 and totals["Wohnung"] == 2 and totals[""] == 8
    tree = views.folder_tree(folders, counts, "Finanzen/Bank")
    assert [n.name for n in tree] == ["Finanzen", "Wohnung"]
    finanzen = tree[0]
    assert finanzen.open and not finanzen.current and finanzen.total == 5
    assert [c.name for c in finanzen.children] == ["Bank", "Steuer"] and finanzen.children[0].current
    assert not tree[1].open
    assert views.breadcrumbs("A b/C#d")[-1]["url"] == "/archiv/A%20b/C%23d"


def test_processing_steps_and_labels():
    assert views.processing_step("received")["number"] == 1
    step = views.processing_step("deblanked")
    assert step["done"] == 2 and step["label"] == "Texterkennung (OCR)"
    assert [s["state"] for s in step["steps"]] == ["done", "done", "current", "todo", "todo"]
    assert views.source_label("watch-scanner") == "Ordner „scanner“"
    assert views.source_label("mail") == "E-Mail"
    entry = views.audit_entry({"at": "2026-11-23T10:00:00+01:00", "user": "alex", "action": "rename_folder", "folder": "A", "new_folder": "B"})
    assert entry["label"] == "Ordner umbenannt" and entry["details"] == "A → B"
    assert views.audit_entry({"action": "upload", "files": 3})["details"] == "Dateien: 3"


def test_one_time_store():
    store = views.OneTimeStore(ttl=60)
    key = store.put({"token": "dsk_secret"})
    assert store.take(key) == {"token": "dsk_secret"}
    assert store.take(key) is None
    assert store.take(None) is None
    expired = views.OneTimeStore(ttl=-1)
    assert expired.take(expired.put("x")) is None


def test_icons_used_anywhere_exist_in_the_sprite():
    sprite = (STATIC / "icons.svg").read_text(encoding="utf-8")
    available = set(re.findall(r'<symbol id="([a-z0-9-]+)"', sprite))
    used: set[str] = set()
    for template in TEMPLATES.glob("*.html"):
        used |= set(re.findall(r"icon\('([a-z0-9-]+)'", template.read_text(encoding="utf-8")))
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    used |= set(re.findall(r'icon\("([a-z0-9-]+)"', script))
    used |= {icon for _label, icon in views.SOURCE_LABELS.values()}
    used |= {icon for _label, icon in views.SUGGESTION_SOURCES.values()}
    used |= {item[3] for item in templating.NAV_ITEMS}
    used |= {"folder", "file", "euro", "card", "hash", "mail", "user", "building", "file-text", "archive", "tag", "edit"}
    missing = used - available
    assert not missing, missing


def test_static_files_are_self_contained():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for text in (css, js):
        assert "http://" not in text.replace("http://www.w3.org/2000/svg", "")
        assert "https://" not in text
        assert "@import" not in text
    assert "prefers-color-scheme: dark" in css and "prefers-reduced-motion" in css
    assert "eval(" not in js and "innerHTML" not in js and "setAttribute(\"style\"" not in js
    assert (STATIC / "favicon.svg").read_text(encoding="utf-8").startswith("<svg")


# ---------------------------------------------------------------------------
# Rendering the shared templates


def test_render_login_and_error_pages(svc):
    app = build_app(svc)
    seen = {}

    @app.get("/probe-login")
    def probe_login(request: Request):
        return templating.render(
            request,
            "login.html",
            {"oidc_enabled": True, "oidc_label": "Mit Authentik anmelden", "local_login_enabled": True,
             "error": "Dieses Konto ist für DocuShelf nicht freigeschaltet.", "next": "/eingang/x?a=1", "logged_out": True},
        )

    @app.get("/probe-error")
    def probe_error(request: Request):
        response = templating.render(
            request, "error.html", {"status_code": 404, "title": "Nicht gefunden", "message": "Gibt es nicht."}, status_code=404
        )
        seen["ok"] = True
        return response

    with TestClient(app) as test_client:
        html = test_client.get("/probe-login").text
        page = assert_clean_page(html)
        assert "Mit Authentik anmelden" in html and "/login/oidc?next=/eingang/x%3Fa%3D1" in html
        assert "Notfallzugang" in html and 'action="/login/local"' in html
        assert "Du wurdest abgemeldet" in html and "nicht freigeschaltet" in html
        assert 'class="sidebar"' not in html  # nobody is logged in yet
        login_form = next(f for f in page.forms if f["attrs"].get("action") == "/login/local")
        names = {i.get("name") for i in login_form["inputs"]}
        assert {"csrf_token", "next", "username", "password"} <= names
        response = test_client.get("/probe-error")
        assert response.status_code == 404 and seen["ok"]
        assert "Gibt es nicht." in response.text and "Fehler 404" in response.text
        assert_clean_page(response.text)


def test_render_login_without_any_method(svc):
    app = build_app(svc)

    @app.get("/probe")
    def probe(request: Request):
        return templating.render(
            request, "login.html", {"oidc_enabled": False, "local_login_enabled": False, "error": None, "next": None, "logged_out": False}
        )

    with TestClient(app) as test_client:
        html = test_client.get("/probe").text
        assert "noch keine Anmeldung eingerichtet" in html and "/login/oidc" not in html


# ---------------------------------------------------------------------------
# Inbox


def test_root_redirects_to_inbox(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 303, 307) and response.headers["location"] == "/eingang"


def test_empty_inbox(client):
    html = get_page(client, "/eingang")
    assert "Alles erledigt!" in html and "Dokumente hierher ziehen" in html
    assert 'data-api="/api/v1/upload"' in html and 'name="combine"' in html
    assert "Bereit zur Ablage" not in html
    assert 'aria-current="page"' in html and "Alex Beispiel" in html and 'action="/logout"' in html


def test_inbox_with_ready_processing_and_failed_items(client, svc, samples, tmp_path):
    ready = make_ready_item(svc, samples, tmp_path)
    ready.suggestions[0].update(new_folder=True, sender="Stadtwerke Musterstadt", doc_date="2026-01-15")
    ready.duplicate_of = {"kind": "archive", "id": "h-" + "0" * 32, "path": "Wohnung/Strom/2025-12-15 Strom.pdf"}
    svc.inbox.save(ready)
    no_suggestion = make_ready_item(svc, samples, tmp_path)
    no_suggestion.suggestions = []
    svc.inbox.save(no_suggestion)
    busy = processing_item(svc, samples, tmp_path)
    svc.worker.running_ids = [busy.id]
    failed_item(svc, samples, tmp_path)

    html = get_page(client, "/eingang")
    assert "Bereit zur Ablage" in html and "Stromrechnung" in html and "Wohnung/Strom" in html
    assert f'/api/v1/inbox/{ready.id}/pages/1.png?w=320' in html
    assert "Mögliches Duplikat" in html and ">neu<" in html and "15.01.2026" in html
    assert "In Arbeit" in html and "Texterkennung (OCR) …" in html and f'data-ids="{busy.id}"' in html
    assert "konnte</strong>" not in html and "nicht verarbeitet werden" in html and 'href="/eingang/fehler"' in html
    assert 'action="/eingang/sammelablage"' in html and 'data-api="/api/v1/inbox/file-batch"' in html
    page = parse(html)
    boxes = {a.get("data-id"): a for t, a in page.tags if t == "input" and a.get("name") == "items"}
    assert "disabled" not in boxes[ready.id] and "disabled" in boxes[no_suggestion.id]
    # The checkbox carries the fingerprint of the suggestion shown (and that the card shows a duplicate).
    fingerprint = views.suggestion_fingerprint(svc.inbox.find(ready.id).top_suggestion)
    assert boxes[ready.id]["value"] == f"{ready.id}:{fingerprint}:dup" and boxes[ready.id]["data-expect"] == fingerprint
    assert "data-duplicate" in boxes[ready.id] and "data-duplicate" not in boxes[no_suggestion.id]
    # The badge in the navigation counts the ready items, the dot the failed ones.
    assert re.search(r'class="nav-badge"[^>]*>2<', html) and 'class="nav-alert"' in html


def test_inbox_ready_detail(client, svc, samples, tmp_path):
    item = make_ready_item(
        svc, samples, tmp_path, source="mail",
        context={"mail_subject": "Ihre Rechnung", "mail_from": "rechnung@stadtwerke.example", "mail_from_name": "Stadtwerke"},
    )
    item.page_count = 3
    item.pages_removed = [2]
    item.analysis = {
        "sender": "Stadtwerke Musterstadt", "doc_type": "Rechnung", "doc_date": "2026-01-15",
        "dates": [{"value": "2026-01-15", "text": "15.01.2026", "score": 0.9, "label": "Rechnungsdatum"},
                  {"value": "2026-02-01", "text": "01.02.2026", "score": 0.3, "label": None}],
        "amounts": ["123,45 €", "10,00 €"], "total_amount": "123,45 €",
        "ibans": ["DE89370400440532013000"], "references": {"Kundennummer": ["4711"]},
        "keywords": ["strom", "abschlag"],
    }
    item.suggestions = [
        {"folder": "Wohnung/Strom", "title": "Stromrechnung Januar", "doc_date": "2026-01-15", "tags": ["Strom"],
         "sender": "Stadtwerke Musterstadt", "score": 1.0, "source": "rule", "reasons": ["Regel „Strom“", "Absender Stadtwerke"]},
        {"folder": "Finanzen/Rechnungen", "title": "Rechnung", "score": 0.4, "source": "similarity",
         "reasons": ["ähnlich zu „Rechnung Dezember“ (40 %)"], "new_folder": True},
    ]
    svc.inbox.save(item)
    add_archive_pdf(svc, samples, "Wohnung/Strom/2025-12-15 Strom Dezember.pdf", text="Strom")
    svc.inbox.log(item, "Analyse fertig")

    html = get_page(client, f"/eingang/{item.id}")
    assert "<iframe" not in html and 'data-viewer data-src="/api/v1/inbox/' + item.id + '/document"' in html
    assert f'/api/v1/inbox/{item.id}/pages/1.png?w=1400' in html  # page images until PDF.js takes over
    assert f"/api/v1/inbox/{item.id}/pages/3.png?w=700" in html  # every page, not just the first
    assert f'action="/eingang/{item.id}/ablegen"' in html and 'value="Wohnung/Strom"' in html
    assert 'value="Stromrechnung Januar"' in html and 'value="2026-01-15"' in html and 'value="Strom"' in html
    assert "2026-01-15 Stromrechnung Januar.pdf" in html  # file name preview
    assert "Regel „Strom“ · Absender Stadtwerke" in html and "100 %" in html and "Ähnliche Dokumente" in html
    assert "Rechnungsdatum" in html and 'data-set-date="2026-02-01"' in html
    assert "123,45 €" in html and "DE89 3704 0044 0532 0130 00" in html and "Kundennummer" in html and "4711" in html
    assert "Ihre Rechnung" in html and "Stadtwerke &lt;rechnung@stadtwerke.example&gt;" in html
    assert "1 leere Seite entfernt (Seite 2" in html and "Ohne Entfernung neu verarbeiten" in html
    assert "Regel anlegen" in html and "Stadtwerke Musterstadt</strong>" in html
    assert 'id="folders-data"' in html and "Wohnung/Strom" in parse(html).tags[0][1].get("lang", "de") + html
    assert "Verarbeitungsprotokoll" in html and "Analyse fertig" in html
    assert f'/api/v1/inbox/{item.id}/original/1?download=1' in html
    assert "1 von 1" in html


def test_inbox_processing_and_failed_detail(client, svc, samples, tmp_path):
    busy = processing_item(svc, samples, tmp_path)
    html = get_page(client, f"/eingang/{busy.id}")
    assert "Wartet auf Verarbeitung" in html and "Schritt 3 von 5" in html and 'data-watch-item="' + busy.id in html
    assert '<noscript><meta http-equiv="refresh" content="5"></noscript>' in html

    failed = failed_item(svc, samples, tmp_path)
    html = get_page(client, f"/eingang/{failed.id}")
    assert "Verarbeitung fehlgeschlagen" in html and "OCR ist abgestürzt" in html and "Texterkennung (OCR)" in html
    assert f'action="/eingang/{failed.id}/neu-verarbeiten"' in html and "Erneut verarbeiten" in html
    assert f"/api/v1/inbox/{failed.id}/original/1?download=1" in html

    html = get_page(client, "/eingang/fehler")
    assert "Fehler im Eingang" in html and "kaputt.pdf" in html and "OCR ist abgestürzt" in html
    assert "Protokoll anzeigen" in html


def test_inbox_unknown_item_is_404(client):
    for url in ("/eingang/20200101-000000-abcdef", "/eingang/nicht-da"):
        response = client.get(url)
        assert response.status_code == 404
        assert "nicht (mehr) im Eingang" in response.text
        assert_clean_page(response.text)
    assert client.get("/eingang/..%2Fetc").status_code == 404


def test_file_item_goes_to_next_and_flashes(client, svc, samples, tmp_path):
    first = make_ready_item(svc, samples, tmp_path)
    second = make_ready_item(svc, samples, tmp_path)
    response = post(client, f"/eingang/{first.id}/ablegen", {
        "folder": "Wohnung/Strom", "title": "Stromrechnung Januar", "doc_date": "2026-01-15",
        "tags": "Strom, steuerrelevant 2026", "sender": "Stadtwerke Musterstadt",
    })
    assert response.headers["location"] == f"/eingang/{second.id}"
    target = svc.layout.archive / "Wohnung/Strom/2026-01-15 Stromrechnung Januar.pdf"
    assert target.is_file()
    record = svc.index.get_by_path("Wohnung/Strom/2026-01-15 Stromrechnung Januar.pdf")
    assert record is not None and record.tags == ["Strom", "steuerrelevant 2026"]
    assert svc.inbox.find(first.id).state == "erledigt"
    html = get_page(client, response.headers["location"])
    assert any("„Stromrechnung Januar“ liegt jetzt in Wohnung/Strom." in text for text in flash_texts(html))
    assert f'href="/dokument/{record.id}"' in html

    # The last item leads back to the inbox; with "Regel anlegen" a rule is created.
    response = post(client, f"/eingang/{second.id}/ablegen", {
        "folder": "Wohnung/Strom", "title": "Abschlag", "doc_date": "", "sender": "Stadtwerke Musterstadt", "create_rule": "1",
    })
    assert response.headers["location"] == "/eingang"
    assert any(rule.action.folder == "Wohnung/Strom" for rule in svc.rules.load())
    html = get_page(client, "/eingang")
    assert any("Regel „" in text and "angelegt" in text for text in flash_texts(html))


def test_file_item_with_invalid_input_keeps_what_was_typed(client, svc, samples, tmp_path):
    item = make_ready_item(svc, samples, tmp_path)
    response = post(client, f"/eingang/{item.id}/ablegen", {"folder": "Neu/Ordner", "title": "  ", "tags": "eins, zwei"}, status=400)
    assert "Bitte einen Titel angeben." in response.text and 'value="Neu/Ordner"' in response.text
    assert 'value="eins, zwei"' in response.text
    response = post(client, f"/eingang/{item.id}/ablegen", {"folder": "A", "title": "T", "doc_date": "31.02.2026"}, status=400)
    assert "kein gültiges Datum" in response.text
    response = post(client, f"/eingang/{item.id}/ablegen", {"folder": "../etc", "title": "T"}, status=400)
    assert "Ungültiger Ordnername" in response.text
    assert svc.inbox.find(item.id).state == "bereit"
    assert list(svc.layout.archive.rglob("*.pdf")) == []


def test_file_item_that_vanished(client, svc):
    response = post(client, "/eingang/20200101-000000-abcdef/ablegen", {"folder": "A", "title": "T"})
    assert response.headers["location"] == "/eingang"
    assert any("nicht mehr im Eingang" in text for text in flash_texts(get_page(client, "/eingang")))


def test_delete_item_offers_undo(client, svc, samples, tmp_path):
    first = make_ready_item(svc, samples, tmp_path)
    second = make_ready_item(svc, samples, tmp_path)
    response = post(client, f"/eingang/{first.id}/loeschen", {"after": "next"})
    assert response.headers["location"] == f"/eingang/{second.id}"
    entries = svc.trash.list()
    assert len(entries) == 1 and entries[0].item_id == first.id
    html = get_page(client, response.headers["location"])
    assert "liegt jetzt im Papierkorb" in html
    assert f'action="/papierkorb/{entries[0].id}/wiederherstellen"' in html and "Rückgängig" in html
    response = post(client, f"/papierkorb/{entries[0].id}/wiederherstellen")
    assert response.headers["location"] == f"/eingang/{first.id}"
    assert svc.inbox.find(first.id).state == "bereit" and svc.trash.list() == []


def test_retry_and_refresh(client, svc, samples, tmp_path):
    failed = failed_item(svc, samples, tmp_path)
    response = post(client, f"/eingang/{failed.id}/neu-verarbeiten", {"skip_blank_removal": "1", "after": "/eingang/fehler"})
    assert response.headers["location"] == "/eingang/fehler"
    assert svc.pipeline.calls[-1] == ("retry", failed.id, True, False)
    assert svc.audit.tail(1)[0]["action"] == "retry"
    response = post(client, f"/eingang/{failed.id}/neu-verarbeiten", {"after": "https://evil.example"})
    assert response.headers["location"] == f"/eingang/{failed.id}"

    ready = make_ready_item(svc, samples, tmp_path)
    response = post(client, f"/eingang/{ready.id}/vorschlaege")
    assert response.headers["location"] == f"/eingang/{ready.id}" and svc.pipeline.calls[-1] == ("refresh", ready.id)
    assert any("Vorschläge neu berechnet (1 Vorschlag)" in t for t in flash_texts(get_page(client, f"/eingang/{ready.id}")))

    from docushelf.errors import Conflict

    svc.pipeline.error = Conflict("Das Dokument wird gerade verarbeitet")
    response = post(client, f"/eingang/{ready.id}/neu-verarbeiten")
    assert response.headers["location"] == f"/eingang/{ready.id}"
    assert "Das Dokument wird gerade verarbeitet" in flash_texts(get_page(client, f"/eingang/{ready.id}"))


def test_batch_filing_fallback(client, svc, samples, tmp_path):
    good = make_ready_item(svc, samples, tmp_path)
    good.suggestions[0]["doc_date"] = "2026-02-01"
    svc.inbox.save(good)
    bare = make_ready_item(svc, samples, tmp_path)
    bare.suggestions = []
    svc.inbox.save(bare)
    response = client.post(
        "/eingang/sammelablage",
        data={"csrf_token": "t", "items": [good.id, bare.id, "kein-item"]},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/eingang"
    assert (svc.layout.archive / "Wohnung/Strom/2026-02-01 Stromrechnung.pdf").is_file()
    assert svc.inbox.find(bare.id).state == "bereit"
    texts = flash_texts(get_page(client, "/eingang"))
    assert "1 Dokument abgelegt." in texts
    assert any("kein vollständiger Vorschlag" in t for t in texts)


def test_upload_fallback_writes_a_batch(client, svc, samples, tmp_path):
    pdf = samples.text_pdf(tmp_path / "up" / "Brief.pdf", ["Hallo"]).read_bytes()
    image = io.BytesIO()
    samples.page_image(["Seite"]).save(image, format="PNG")
    files = [
        ("files", ("Brief.pdf", pdf, "application/pdf")),
        ("files", ("foto1.png", image.getvalue(), "image/png")),
        ("files", ("foto2.png", image.getvalue(), "image/png")),
        ("files", ("notiz.docx", b"PK\x03\x04", "application/octet-stream")),
    ]
    response = client.post("/eingang/hochladen", data={"csrf_token": "t", "combine": "1"}, files=files, follow_redirects=False)
    assert response.status_code == 303
    batches = [p for p in svc.layout.source_dir("upload").iterdir() if p.is_dir()]
    assert len(batches) == 1
    sendung = read_sendung(batches[0])
    assert sendung is not None
    assert sorted(len(d.files) for d in sendung.documents) == [1, 2]  # the PDF alone, both photos together
    assert sendung.context["uploader"] == "alex" and sendung.options == {"combine": True}
    assert svc.intake.triggered == 1 and svc.audit.tail(1)[0]["action"] == "upload"
    texts = flash_texts(get_page(client, "/eingang"))
    assert "3 Dateien hochgeladen – wird jetzt verarbeitet." in texts
    assert any("notiz.docx" in t for t in texts)

    response = client.post("/eingang/hochladen", data={"csrf_token": "t"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Bitte mindestens eine Datei auswählen." in flash_texts(get_page(client, "/eingang"))


def test_upload_fallback_respects_the_size_limit(client, svc):
    svc.settings = svc.settings.with_overrides(max_upload_mb=1)
    big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10)
    response = client.post(
        "/eingang/hochladen", data={"csrf_token": "t"}, files=[("files", ("gross.pdf", big, "application/pdf"))], follow_redirects=False
    )
    assert response.status_code == 303
    assert not any(p.is_dir() for p in svc.layout.source_dir("upload").iterdir())
    assert any("zu groß" in t for t in flash_texts(get_page(client, "/eingang")))


# ---------------------------------------------------------------------------
# Archive and documents


@pytest.fixture
def archive(svc, samples):
    docs = SimpleNamespace(
        bank=add_archive_pdf(svc, samples, "Finanzen/Bank/2025-03-12 Kontoauszug März.pdf", text="Sparkasse Kontoauszug März Saldo"),
        tax=add_archive_pdf(svc, samples, "Finanzen/Steuer/2024-06-01 Steuerbescheid 2023.pdf", text="Finanzamt Bescheid Einkommensteuer"),
        strom=add_archive_pdf(svc, samples, "Wohnung/Strom/2025-12-15 Stromrechnung Dezember.pdf", text="Stadtwerke Stromrechnung Abschlag"),
        photo=add_archive_file(svc, "Wohnung/Garantie Kühlschrank.jpg", b"\xff\xd8\xff\xe0 not really a jpeg"),
    )
    (svc.layout.archive / "Leer" / "Noch leerer").mkdir(parents=True)
    svc.index.add_folder_with_parents("Leer/Noch leerer")
    return docs


def test_archive_root_and_folder(client, archive):
    html = get_page(client, "/archiv")
    assert "Archiv" in html and "Finanzen" in html and "Wohnung" in html and "Leer" in html
    assert re.search(r"Finanzen</span>\s*<span class=\"folder-card-meta\">2 Dokumente · 2 Ordner", html)
    assert "Umbenennen" not in html  # the archive root itself cannot be renamed

    html = get_page(client, "/archiv/Finanzen/Bank")
    assert "Kontoauszug März" in html and "12.03.2025" in html and f'href="/dokument/{archive.bank.id}"' in html
    assert 'aria-current="page">Bank<' in html and 'href="/archiv/Finanzen"' in html
    assert 'action="/archiv/ordner/umbenennen"' in html and 'action="/archiv/ordner/loeschen"' not in html

    html = get_page(client, "/archiv/Wohnung")
    assert "Garantie Kühlschrank" in html and ">JPG<" in html

    html = get_page(client, "/archiv/Leer/Noch%20leerer")
    assert "Dieser Ordner ist leer" in html and 'action="/archiv/ordner/loeschen"' in html


def test_archive_sorting_and_missing_folders(client, archive, svc, samples):
    add_archive_pdf(svc, samples, "Finanzen/Bank/2025-01-31 Kontoauszug Januar.pdf", text="Januar")
    html = get_page(client, "/archiv/Finanzen/Bank?sort=date_asc")
    assert html.index("Kontoauszug Januar") < html.index("Kontoauszug März")
    html = get_page(client, "/archiv/Finanzen/Bank?sort=date_desc")
    assert html.index("Kontoauszug März") < html.index("Kontoauszug Januar")
    # Unknown values keep the remembered choice; without one the order is by name.
    html = get_page(client, "/archiv/Finanzen/Bank?sort=unsinn")
    assert html.index("Kontoauszug März") < html.index("Kontoauszug Januar")
    client.cookies.delete(views.SORT_COOKIE)
    html = get_page(client, "/archiv/Finanzen/Bank?sort=unsinn")
    assert html.index("Kontoauszug Januar") < html.index("Kontoauszug März")
    assert client.get("/archiv/Gibt/Es/Nicht").status_code == 404
    assert client.get("/archiv/Finanzen/%2E%2E/%2E%2E").status_code == 404
    assert client.get("/archiv/.versteckt").status_code == 404


def test_folder_actions(client, svc, archive):
    response = post(client, "/archiv/ordner/neu", {"parent": "Finanzen", "name": "Versicherungen"})
    assert response.headers["location"] == "/archiv/Finanzen/Versicherungen"
    assert (svc.layout.archive / "Finanzen/Versicherungen").is_dir()

    response = post(client, "/archiv/ordner/umbenennen", {"path": "Finanzen/Versicherungen", "name": "Policen"})
    assert response.headers["location"] == "/archiv/Finanzen/Policen"
    assert (svc.layout.archive / "Finanzen/Policen").is_dir()

    response = post(client, "/archiv/ordner/loeschen", {"path": "Finanzen/Bank"})
    assert response.headers["location"] == "/archiv/Finanzen/Bank"
    assert (svc.layout.archive / "Finanzen/Bank").is_dir()
    assert any("nicht leer" in t for t in flash_texts(get_page(client, "/archiv/Finanzen/Bank")))

    response = post(client, "/archiv/ordner/loeschen", {"path": "Finanzen/Policen"})
    assert response.headers["location"] == "/archiv/Finanzen"
    assert not (svc.layout.archive / "Finanzen/Policen").exists()

    response = post(client, "/archiv/ordner/neu", {"parent": "", "name": ""})
    assert response.headers["location"] == "/archiv"
    response = post(client, "/archiv/ordner/umbenennen", {"path": "", "name": "x"})
    assert response.headers["location"] == "/archiv"


def test_document_page(client, svc, archive):
    html = get_page(client, f"/dokument/{archive.bank.id}")
    assert "Kontoauszug März" in html and "12.03.2025" in html
    assert "<iframe" not in html and f'data-viewer data-src="/api/v1/documents/{archive.bank.id}/file"' in html
    assert f"/api/v1/documents/{archive.bank.id}/pages/1.png?w=700&amp;v={archive.bank.content_hash[:12]}" in html
    assert f'action="/dokument/{archive.bank.id}/bearbeiten"' in html and f'action="/dokument/{archive.bank.id}/loeschen"' in html
    assert "Technische Details" in html and "Finanzen/Bank/2025-03-12 Kontoauszug März.pdf" in html
    assert "vorläufig" in html and "keine – Datei wurde von außen abgelegt" in html
    assert "Erkannter Text" in html and "Sparkasse Kontoauszug" in html
    assert 'href="/archiv/Finanzen/Bank"' in html

    html = get_page(client, f"/dokument/{archive.photo.id}")
    assert "Kein PDF" in html or "nur Titel, Datum und Ordner" in html
    assert 'name="tags"' not in html and 'name="sender"' not in html
    assert client.get("/dokument/h-ffffffffffffffffffffffffffffffff").status_code == 404


def test_document_edit_rename_and_move(client, svc, archive):
    response = post(client, f"/dokument/{archive.bank.id}/bearbeiten", {
        "title": "Kontoauszug März 2025", "doc_date": "2025-03-31", "sender": "Sparkasse",
        "tags": "Bank", "folder": "Finanzen/Bank/2025",
    })
    location = response.headers["location"]
    assert location.startswith("/dokument/") and location != f"/dokument/{archive.bank.id}"  # provisional id became a UUID
    moved = svc.layout.archive / "Finanzen/Bank/2025/2025-03-31 Kontoauszug März 2025.pdf"
    assert moved.is_file() and not (svc.layout.archive / archive.bank.path).exists()
    html = get_page(client, location)
    assert "Gespeichert und nach „Finanzen/Bank/2025“ verschoben." in flash_texts(html)
    assert "Sparkasse" in html and "XMP-Metadaten" in html

    new_id = location.rsplit("/", 1)[-1]
    response = post(client, f"/dokument/{new_id}/bearbeiten", {
        "title": "Kontoauszug März 2025", "doc_date": "2025-03-31", "sender": "Sparkasse", "tags": "Bank", "folder": "Finanzen/Bank/2025",
    })
    assert "Es gab nichts zu ändern." in flash_texts(get_page(client, response.headers["location"]))

    response = post(client, f"/dokument/{new_id}/bearbeiten", {"title": "", "doc_date": "", "folder": "X"}, status=400)
    assert "Bitte einen Titel angeben." in response.text and 'value="X"' in response.text
    response = post(client, f"/dokument/{new_id}/bearbeiten", {"title": "T", "doc_date": "gestern"}, status=400)
    assert "kein gültiges Datum" in response.text


def test_document_edit_of_non_pdf_ignores_tags(client, svc, archive):
    response = post(client, f"/dokument/{archive.photo.id}/bearbeiten", {
        "title": "Garantie Kühlschrank Bosch", "doc_date": "2024-05-01", "tags": "wird ignoriert", "folder": "Wohnung",
    })
    assert (svc.layout.archive / "Wohnung/2024-05-01 Garantie Kühlschrank Bosch.jpg").is_file()
    assert "Änderungen gespeichert." in flash_texts(get_page(client, response.headers["location"]))


def test_document_delete_and_trash(client, svc, archive):
    response = post(client, f"/dokument/{archive.tax.id}/loeschen")
    assert response.headers["location"] == "/archiv/Finanzen/Steuer"
    assert not (svc.layout.archive / archive.tax.path).exists()
    html = get_page(client, "/archiv/Finanzen/Steuer")
    assert "„Steuerbescheid 2023“ liegt jetzt im Papierkorb." in flash_texts(html)

    html = get_page(client, "/papierkorb")
    assert "Steuerbescheid 2023" in html and "Finanzen" in html and "von alex" in html
    assert "30 Tage" in html and "endgültig gelöscht" in html
    entry = svc.trash.list()[0]
    response = post(client, f"/papierkorb/{entry.id}/wiederherstellen", {"after": "/papierkorb"})
    assert response.headers["location"] == "/papierkorb"
    assert (svc.layout.archive / archive.tax.path).is_file()
    assert any("ist wieder in Finanzen/Steuer" in t for t in flash_texts(get_page(client, "/papierkorb")))

    record = svc.index.get_by_path(archive.tax.path)
    post(client, f"/dokument/{record.id}/loeschen")
    entry = svc.trash.list()[0]
    response = post(client, f"/papierkorb/{entry.id}/loeschen")
    assert response.headers["location"] == "/papierkorb" and svc.trash.list() == []
    html = get_page(client, "/papierkorb")
    assert "wurde endgültig gelöscht" in " ".join(flash_texts(html)) and "Der Papierkorb ist leer" in html
    response = post(client, "/papierkorb/20200101-000000-abcdef/loeschen")
    assert any("nicht gefunden" in t for t in flash_texts(get_page(client, "/papierkorb")))


# ---------------------------------------------------------------------------
# Search


def test_search_page(client, archive):
    html = get_page(client, "/suche")
    assert "So findest du alles" in html and "Zuletzt hinzugekommen" in html and "ordner:Finanzen" in html

    html = get_page(client, "/suche?q=Kontoauszug")
    assert "1 Treffer" in html and "Kontoauszug März" in html
    assert f'href="/dokument/{archive.bank.id}#page=1"' in html and "S. 1" in html and "<mark>" in html

    html = get_page(client, "/suche?q=&ordner=Wohnung")
    assert "Stromrechnung Dezember" in html and "Kontoauszug" not in html.split('class="hits"')[1]
    assert "aktiv" in html

    html = get_page(client, "/suche?q=Stromrechnug")
    assert "Meintest du" in html and "stromrechnung" in html.lower()

    html = get_page(client, "/suche?q=Quittungsblock")
    assert "Nichts gefunden" in html

    html = get_page(client, "/suche?q=Kontoauszug&ab=morgen")
    assert "„morgen“ ist kein gültiges Datum" in html


def test_search_survives_a_failing_searcher(client, svc):
    class Broken:
        def search(self, query):
            raise RuntimeError("kaputt")

    svc.searcher = Broken()
    html = get_page(client, "/suche?q=x")
    assert "gerade nicht verfügbar" in html


# ---------------------------------------------------------------------------
# Rules


def test_rules_page_add_and_delete(client, svc, archive):
    html = get_page(client, "/regeln")
    assert "Noch keine Regeln" in html and "Neue Regel" in html and "Regeldatei bearbeiten" in html
    assert "rules: []" in html and "Nebenkostenabrechnung" in html  # template + known document types

    response = post(client, "/regeln/neu", {"sender": "Stadtwerke, DREWAG", "keywords_any": "Abschlag", "folder": "Wohnung/Strom", "tags": "steuerrelevant {year}"})
    assert response.headers["location"] == "/regeln"
    rules = svc.rules.load()
    assert len(rules) == 1 and rules[0].match.sender == ["Stadtwerke", "DREWAG"] and rules[0].action.tags == ["steuerrelevant {year}"]
    html = get_page(client, "/regeln")
    assert "Stadtwerke → Wohnung/Strom" in html and "erkannter Absender enthält" in html and "DREWAG" in html
    assert '<label for="r-sender">Erkannter Absender enthält</label>' in html
    assert "Ist kein Absender erkannt, zählt nur der Briefkopf auf Seite 1." in html

    response = post(client, "/regeln/neu", {"name": "Leer", "folder": "A"}, status=400)
    assert "mindestens eine Bedingung" in response.text and 'value="Leer"' in response.text
    response = post(client, "/regeln/neu", {"sender": "X", "folder": "A", "title": "{unbekannt}"}, status=400)
    assert "unbekannter Platzhalter" in response.text

    response = post(client, "/regeln/0/loeschen", {"name": "falscher Name"})
    assert len(svc.rules.load()) == 1
    assert any("inzwischen geändert" in t for t in flash_texts(get_page(client, "/regeln")))
    post(client, "/regeln/0/loeschen", {"name": rules[0].name})
    assert svc.rules.load() == []
    assert svc.audit.tail(1)[0]["action"] == "delete_rule"


def test_rules_yaml_editor(client, svc):
    html = get_page(client, "/regeln")
    base_hash = re.search(r'name="base_hash" value="([0-9a-f]+)"', html).group(1)
    good = "rules:\n  - name: Bank\n    match:\n      sender: [Sparkasse]\n    action:\n      folder: Finanzen/Bank\n"
    response = post(client, "/regeln/yaml", {"yaml": good, "base_hash": base_hash})
    assert response.headers["location"] == "/regeln"
    assert svc.rules.load()[0].name == "Bank"
    assert "Regeln gespeichert (1 Regel)." in flash_texts(get_page(client, "/regeln"))

    broken = "rules:\n  - name: Kaputt\n    match: {}\n"
    response = post(client, "/regeln/yaml", {"yaml": broken}, status=400)
    assert "Kaputt" in response.text and "mindestens eine Bedingung" in response.text
    assert svc.rules.load()[0].name == "Bank"

    # Someone else changed the file in the meantime: no silent overwrite.
    response = post(client, "/regeln/yaml", {"yaml": good.replace("Bank", "Alt"), "base_hash": base_hash}, status=409)
    assert "inzwischen geändert" in response.text and 'name="force"' in response.text
    assert svc.rules.load()[0].name == "Bank"
    post(client, "/regeln/yaml", {"yaml": good.replace("Bank", "Neu"), "base_hash": base_hash, "force": "1"})
    assert svc.rules.load()[0].name == "Neu"


# ---------------------------------------------------------------------------
# Settings


def test_settings_page(client, svc, archive, samples, tmp_path):
    failed_item(svc, samples, tmp_path)
    svc.audit.write("alex", "file", title="Stromrechnung", path="Wohnung/Strom/x.pdf")
    html = get_page(client, "/einstellungen")
    for text in ("Einstellungen", "Abgleich", "Jetzt abgleichen", "Index neu aufbauen", "Verarbeitung", "Texterkennung",
                 "OCRmyPDF 16.0.0", "Eingangskanäle", "Upload", "Scanner (SMB)", "IMAP-Postfach", "API-Tokens",
                 "Protokoll", "Abgelegt", "„Stromrechnung“", "Lokaler Notfallzugang"):
        assert text in html, text
    assert re.search(r'class="stat-value">4</span><span class="stat-label">Dokumente', html)
    assert "2 neu · 1 geändert" in html


def test_settings_actions(client, svc):
    response = post(client, "/einstellungen/abgleich")
    assert response.headers["location"] == "/einstellungen" and svc.watcher.requests
    assert any("im Hintergrund" in t for t in flash_texts(get_page(client, "/einstellungen")))

    class Reconciler:
        def scan(self, full=True):
            return ScanStats(added=1, updated=2, moved=3, removed=4)

    svc.watcher = FakeWatcher(running=False)
    svc.reconciler = Reconciler()
    post(client, "/einstellungen/abgleich")
    assert "Abgleich abgeschlossen: 1 neu, 2 geändert, 3 verschoben, 4 entfernt." in flash_texts(get_page(client, "/einstellungen"))

    post(client, "/einstellungen/index-neu-aufbauen")
    assert svc.rebuilt == ["alex"]
    assert "Index neu aufgebaut: 3 Dokumente in 1,2 s." in flash_texts(get_page(client, "/einstellungen"))


def test_tokens_are_shown_once(client, svc):
    response = post(client, "/einstellungen/tokens", {"name": "iPhone Kurzbefehl"})
    assert response.headers["location"] == "/einstellungen#tokens"
    page = client.get("/einstellungen")
    assert page.headers.get("cache-control") == "no-store"
    token = re.search(r'id="new-token-value" aria-label="Neuer API-Token"|value="(dsk_[^"]+)"', page.text)
    shown = re.search(r'value="(dsk_[^"]+)"', page.text)
    assert token and shown and svc.tokens.verify(shown.group(1)) is not None
    assert "iPhone Kurzbefehl" in page.text and "nur dieses eine Mal" in page.text
    assert shown.group(1) not in client.get("/einstellungen").text  # gone on the next visit
    entry = svc.tokens.list()[0]
    assert svc.audit.tail(1)[0]["action"] == "token_create"

    post(client, f"/einstellungen/tokens/{entry['id']}/widerrufen", {"name": entry["name"]})
    assert svc.tokens.list() == [] and svc.tokens.verify(shown.group(1)) is None
    assert any("widerrufen" in t for t in flash_texts(get_page(client, "/einstellungen")))
    post(client, "/einstellungen/tokens/gibtsnicht/widerrufen")
    assert any("nicht gefunden" in t for t in flash_texts(get_page(client, "/einstellungen")))
    post(client, "/einstellungen/tokens", {"name": "  "})
    assert any("Bitte einen Namen" in t for t in flash_texts(get_page(client, "/einstellungen")))


def test_pages_tolerate_missing_optional_components(svc):
    for name in ("tokens", "worker", "intake", "watcher"):
        delattr(svc, name)
    with TestClient(build_app(svc)) as test_client:
        html = get_page(test_client, "/einstellungen")
        assert "nicht verfügbar" in html
        get_page(test_client, "/eingang")


# ---------------------------------------------------------------------------
# Through the complete application (web/app.py): login, CSRF, headers


@pytest.fixture
def full_client(svc):
    from docushelf.web.app import create_app

    app = create_app(svc.settings, services=svc)
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


def _login(test_client) -> str:
    page = test_client.get("/login")
    assert page.status_code == 200
    token = csrf_of(page.text)
    assert_clean_page(page.text, csrf=token)
    response = test_client.post(
        "/login/local",
        data={"username": "admin", "password": "test-admin-pw", "csrf_token": token, "next": "/eingang"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text[:500]
    return csrf_of(test_client.get("/eingang").text)


def test_full_app_requires_login(full_client):
    response = full_client.get("/eingang", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    response = full_client.post("/archiv/ordner/neu", data={"name": "X"}, follow_redirects=False)
    assert response.status_code in (303, 403)


def test_full_app_pages_and_csrf(full_client, svc, samples, tmp_path, archive):
    item = make_ready_item(svc, samples, tmp_path)
    token = _login(full_client)
    for url in ("/eingang", f"/eingang/{item.id}", "/eingang/fehler", "/archiv", "/archiv/Finanzen/Bank",
                f"/dokument/{archive.bank.id}", "/suche?q=Strom", "/papierkorb", "/regeln", "/einstellungen"):
        response = full_client.get(url)
        assert response.status_code == 200, (url, response.text[:300])
        assert "default-src 'self'" in response.headers.get("content-security-policy", "")
        assert_clean_page(response.text, csrf=token)
    assert full_client.get("/static/app.css").status_code == 200
    assert full_client.get("/static/icons.svg").status_code == 200

    # Without the token the action is refused and nothing happens.
    response = full_client.post(f"/eingang/{item.id}/ablegen", data={"folder": "A", "title": "T"}, follow_redirects=False)
    assert response.status_code == 403 and "Sicherheitsprüfung" in response.text
    assert svc.inbox.find(item.id).state == "bereit"
    # With the token it goes through.
    response = full_client.post(
        f"/eingang/{item.id}/ablegen", data={"folder": "A", "title": "T", "csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303 and svc.inbox.find(item.id).state == "erledigt"
    assert (svc.layout.archive / "A" / "T.pdf").is_file()
    assert "„T“ liegt jetzt in A. Ansehen" in flash_texts(full_client.get("/eingang").text)

    # The error page of the application uses the UI template.
    response = full_client.get("/dokument/h-ffffffffffffffffffffffffffffffff")
    assert response.status_code == 404 and 'class="error-page"' in response.text


def test_full_app_api_answers_match_what_the_pages_expect(full_client, svc, samples, tmp_path):
    """The scripts read these API answers: keep their shape in sync with app.js."""
    ready = make_ready_item(svc, samples, tmp_path)
    busy = processing_item(svc, samples, tmp_path)
    token = _login(full_client)
    data = full_client.get("/api/v1/inbox?state=arbeit").json()
    assert [entry["id"] for entry in data["items"]] == [busy.id]
    assert {"stage", "source", "display_name"} <= set(data["items"][0])
    response = full_client.post("/api/v1/inbox/file-batch", json={"items": [ready.id]}, headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    result = response.json()
    assert [entry["item"] for entry in result["filed"]] == [ready.id] and result["failed"] == []
    assert "terms" in full_client.get("/api/v1/suggest?prefix=Strom").json()


# ---------------------------------------------------------------------------
# With the real service container


def test_pages_with_the_real_service_container(services, samples, monkeypatch):
    monkeypatch.setattr(routes_ui, "_ocr_info", lambda settings: OcrStub(languages=[], missing_languages=[]))
    samples.text_pdf(services.layout.archive / "Finanzen/Bank/2025-01-31 Kontoauszug Januar.pdf", ["Sparkasse Kontoauszug Januar"])
    services.reconciler.scan(full=True)
    record = services.index.list_in_folder("Finanzen/Bank")[0]
    with TestClient(build_app(services)) as test_client:
        for url in ("/eingang", "/eingang/fehler", "/archiv", "/archiv/Finanzen/Bank", f"/dokument/{record.id}",
                    "/suche?q=Kontoauszug", "/papierkorb", "/regeln", "/einstellungen"):
            get_page(test_client, url)
        assert "Kontoauszug Januar" in get_page(test_client, "/suche?q=Kontoauszug")

        # The watcher is not running here, so the reconciliation runs right away.
        post(test_client, "/einstellungen/abgleich")
        assert any(t.startswith("Abgleich abgeschlossen") for t in flash_texts(get_page(test_client, "/einstellungen")))
        post(test_client, "/einstellungen/index-neu-aufbauen")
        texts = " ".join(flash_texts(get_page(test_client, "/einstellungen")))
        assert "Index neu aufgebaut: 1 Dokument" in texts
        assert services.index.get_by_path(record.path) is not None


def test_new_folder_badge_follows_the_current_archive(client, svc, samples, tmp_path):
    ready = make_ready_item(svc, samples, tmp_path)
    ready.suggestions[0].update(folder="Wohnung/Strom", new_folder=False)
    svc.inbox.save(ready)
    # The suggestion was computed when the folder existed; it is gone now (e.g. renamed in the Finder).
    assert ">neu<" in get_page(client, "/eingang")
    svc.index.add_folder_with_parents("Wohnung/Strom")
    assert ">neu<" not in get_page(client, "/eingang")
    assert ">neu<" not in get_page(client, f"/eingang/{ready.id}")


def test_refresh_all_queues_every_ready_item(client, svc, samples, tmp_path):
    make_ready_item(svc, samples, tmp_path)
    html = get_page(client, "/eingang")
    assert 'form="refresh-all-form"' in html and 'action="/eingang/vorschlaege"' in html
    response = post(client, "/eingang/vorschlaege")
    assert response.headers["location"] == "/eingang"
    assert svc.pipeline.calls[-1] == ("queue_refresh", False)
    assert any("werden neu berechnet" in t for t in flash_texts(get_page(client, "/eingang")))
